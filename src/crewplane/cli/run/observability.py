from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from functools import partial
from typing import Protocol

from rich.console import Console

from crewplane.architecture.contracts import (
    AgentInvoker,
    DashboardSnapshot,
    LogLevel,
    ObserverCapabilities,
    RunContext,
)
from crewplane.architecture.ports import ArtifactStorePort, StatusObserver
from crewplane.architecture.ports.runtime import RuntimeComponents
from crewplane.core.preflight import PreflightExecutionPlan
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.observability import (
    ObservabilityHub,
    PersistentRunLogger,
    RunResult,
)
from crewplane.observability.events import (
    EventSink,
    ExecutionEvent,
    runtime_log_event,
)
from crewplane.observability.observer import Observer
from crewplane.observability.persistent import render_run_summary_terminal
from crewplane.observability.types import WorkflowTopology
from crewplane.runtime.agent.process.drain import unconfirmed_process_cleanup

from .best_effort_thread import run_best_effort_thread
from .blocking_work import settle_run_work
from .terminalization import (
    TerminalizationCommitter,
    commit_terminalization_with_retry,
)

UI_STOP_POLL_INTERVAL_SECONDS = 0.1
EXTERNAL_CANCEL_REASON = "external_cancellation"
UI_STOP_CANCEL_REASON = "ui_stop_requested"
WORKFLOW_CANCELLED_MESSAGE = "Workflow cancelled by live dashboard quit request."
WARNING_RECORD_TIMEOUT_SECONDS = 0.1


class WorkflowCancelledByUser(RuntimeError):
    """Raised when a live UI requests workflow cancellation."""


class ExecuteWorkflowCallable(Protocol):
    async def __call__(
        self,
        plan: PreflightExecutionPlan,
        output: ArtifactStorePort,
        invoker: AgentInvoker,
        secret_context: SecretContext,
        event_sink: EventSink | None = None,
        run_id: str | None = None,
        suppress_progress_output: bool = False,
        workflow_identity: str | None = None,
        resumed_node_ids: tuple[str, ...] = (),
    ) -> PreflightExecutionPlan: ...


@dataclass
class WorkflowWarningRecorder:
    workflow: WorkflowPlan
    console: Console
    queued_warnings: list[str] = field(default_factory=list)
    run_id: str | None = None
    persistent_logger: PersistentRunLogger | None = None

    def bind_run_id(self, run_id: str) -> None:
        self.run_id = run_id

    def bind_logger(self, persistent_logger: PersistentRunLogger) -> None:
        self.persistent_logger = persistent_logger

    def sink(self, message: str) -> None:
        self.console.print(f"[yellow]WARN[/] {message}")
        if not self._can_write_to_logger():
            self.queued_warnings.append(message)
            return
        self._record_warning(message)

    def flush_queued(self) -> None:
        if not self._can_write_to_logger():
            return
        while self.queued_warnings:
            self._record_warning(self.queued_warnings.pop(0))

    def warning_event(self, message: str) -> ExecutionEvent:
        if self.run_id is None:
            raise RuntimeError("Run ID unavailable while recording workflow warning.")
        return runtime_log_event(
            workflow_name=self.workflow.name,
            run_id=self.run_id,
            level=LogLevel.WARNING,
            message=message,
            operation="runtime_warning",
        )

    def _can_write_to_logger(self) -> bool:
        return (
            self.persistent_logger is not None
            and self.run_id is not None
            and self.persistent_logger.started
        )

    def _record_warning(self, message: str) -> None:
        if self.persistent_logger is None:
            return

        def record_warning_event() -> None:
            if self.persistent_logger is not None:
                self.persistent_logger.record_event(self.warning_event(message))

        result = run_best_effort_thread(
            record_warning_event,
            name="crewplane-warning-persist",
            timeout_seconds=WARNING_RECORD_TIMEOUT_SECONDS,
        )
        if result.start_error is not None:
            self.console.print(
                "[yellow]WARN[/] persistent run logging unavailable while recording "
                f"workflow warning: {result.start_error}"
            )
            return
        if result.timed_out:
            self.console.print(
                "[yellow]WARN[/] persistent run logging timed out while recording "
                "workflow warning."
            )
            return
        if result.operation_error is not None:
            self.console.print(
                "[yellow]WARN[/] persistent run logging failed while recording "
                f"workflow warning: {result.operation_error}"
            )


class ObservabilityHubInstance(Protocol):
    @property
    def active_observer_count(self) -> int: ...

    @property
    def stop_requested(self) -> bool: ...

    def __enter__(self) -> ObservabilityHubInstance: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool | None: ...

    def emit(self, event: ExecutionEvent) -> None: ...

    def set_terminal_result(self, result: RunResult) -> None: ...


class TerminalizationCoordinatorProtocol(TerminalizationCommitter, Protocol):
    def acknowledge_observer_shutdown(self) -> None: ...


class ObservabilityHubFactory(Protocol):
    def __call__(
        self,
        workflow_topology: WorkflowTopology,
        run_id: str,
        observers: list[Observer],
        refresh_per_second: int = 4,
        warning_sink: Callable[[str], None] | None = None,
    ) -> ObservabilityHubInstance: ...


def observer_is_active(
    hub: ObservabilityHubInstance,
    observer: Observer,
    non_ui_observer_count: int = 0,
) -> bool:
    checker = getattr(hub, "observer_is_active", None)
    if callable(checker):
        return bool(checker(observer))
    return hub.active_observer_count > non_ui_observer_count


async def wait_for_stop_request(hub: ObservabilityHubInstance) -> None:
    while not hub.stop_requested:
        await asyncio.sleep(UI_STOP_POLL_INTERVAL_SECONDS)


async def await_workflow_or_stop_request[ResultT](
    workflow_call: Coroutine[object, object, ResultT],
    hub: ObservabilityHubInstance,
) -> ResultT:
    workflow_task = asyncio.create_task(workflow_call)
    stop_task = asyncio.create_task(wait_for_stop_request(hub))
    try:
        done, _ = await asyncio.wait(
            {workflow_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and not workflow_task.done():
            workflow_task.cancel()
            try:
                await workflow_task
            except asyncio.CancelledError as exc:
                raise WorkflowCancelledByUser(WORKFLOW_CANCELLED_MESSAGE) from exc
            raise WorkflowCancelledByUser(WORKFLOW_CANCELLED_MESSAGE)
        return await workflow_task
    except asyncio.CancelledError as cancellation:
        if not workflow_task.done():
            workflow_task.cancel()
        try:
            await workflow_task
        except BaseException as exc:
            cleanup_error = unconfirmed_process_cleanup(exc)
            if cleanup_error is not None:
                cancellation.__cause__ = cleanup_error
            elif exc is not cancellation and cancellation.__cause__ is None:
                cancellation.__cause__ = exc
        raise
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)


async def call_execute_workflow(
    execute_workflow_impl: ExecuteWorkflowCallable,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    invoker: AgentInvoker,
    secret_context: SecretContext,
    event_sink: EventSink | None = None,
    run_id: str | None = None,
    suppress_progress_output: bool = False,
    workflow_identity: str | None = None,
    resumed_node_ids: tuple[str, ...] = (),
) -> PreflightExecutionPlan:
    return await execute_workflow_impl(
        plan,
        output,
        invoker=invoker,
        secret_context=secret_context,
        event_sink=event_sink,
        run_id=run_id,
        suppress_progress_output=suppress_progress_output,
        workflow_identity=workflow_identity,
        resumed_node_ids=resumed_node_ids,
    )


def print_end_of_run_summary(
    console: Console,
    persistent_logger: PersistentRunLogger | None,
) -> None:
    if persistent_logger is None:
        return
    summary = persistent_logger.last_summary
    if summary is None:
        return
    console.print("")
    console.print(
        render_run_summary_terminal(summary, console), markup=False, highlight=False
    )


def _resolve_observability_hub(
    observability_hub_cls: ObservabilityHubFactory | None,
) -> ObservabilityHubFactory:
    if observability_hub_cls is not None:
        return observability_hub_cls
    return ObservabilityHub


def _active_non_ui_observer_count(
    status_observer: _GuardedStatusObserver | None,
) -> int:
    """Count the non-UI observers a hub without per-observer checks delivers to.

    ``active_observer_count`` is the only hub signal on that path, so the
    fallback subtracts the observers the run path knows about. The persistent
    logger is required and already started; the status observer counts only
    while it still receives deliveries.
    """
    if status_observer is None or status_observer.delivery_failed:
        return 1
    return 2


def _is_live_dashboard_available(
    hub: ObservabilityHubInstance,
    components: RuntimeComponents,
    status_observer: _GuardedStatusObserver | None,
) -> bool:
    if not components.observers:
        return False
    non_ui_observer_count = _active_non_ui_observer_count(status_observer)
    return any(
        observer_is_active(
            hub,
            observer,
            non_ui_observer_count=non_ui_observer_count,
        )
        for observer in components.observers
    )


def _selected_progress_mode(
    components: RuntimeComponents,
    has_live_dashboard: bool,
) -> bool:
    return (
        False
        if (components.observers and not has_live_dashboard)
        else components.suppress_progress_output
    )


async def _execute_observed_workflow(
    execute_workflow_impl: ExecuteWorkflowCallable,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    components: RuntimeComponents,
    secret_context: SecretContext,
    hub: ObservabilityHubInstance,
    workflow_identity: str | None,
    resumed_node_ids: tuple[str, ...],
    suppress_progress_output: bool,
    terminalization: TerminalizationCoordinatorProtocol,
) -> PreflightExecutionPlan | BaseException:
    try:
        return await await_workflow_or_stop_request(
            call_execute_workflow(
                execute_workflow_impl,
                plan,
                output,
                invoker=components.base_invoker,
                secret_context=secret_context,
                event_sink=hub.emit,
                run_id=output.run_id,
                suppress_progress_output=suppress_progress_output,
                workflow_identity=workflow_identity,
                resumed_node_ids=resumed_node_ids,
            ),
            hub,
        )
    except asyncio.CancelledError as exc:
        commit_terminalization_with_retry(
            terminalization,
            hub,
            "cancelled",
            EXTERNAL_CANCEL_REASON,
        )
        return exc
    except WorkflowCancelledByUser as exc:
        commit_terminalization_with_retry(
            terminalization,
            hub,
            "cancelled",
            UI_STOP_CANCEL_REASON,
        )
        return exc
    except Exception as exc:
        commit_terminalization_with_retry(
            terminalization,
            hub,
            "failed",
            str(exc),
        )
        return exc


async def _run_scheduler_post_hook(
    plan: PreflightExecutionPlan,
    on_scheduler_succeeded: Callable[[PreflightExecutionPlan], None],
    terminalization: TerminalizationCoordinatorProtocol,
    hub: ObservabilityHubInstance,
) -> BaseException | None:
    try:
        _, cancellation = await settle_run_work(partial(on_scheduler_succeeded, plan))
    except Exception as exc:
        commit_terminalization_with_retry(
            terminalization,
            hub,
            "failed",
            str(exc),
        )
        return exc

    if cancellation is not None:
        commit_terminalization_with_retry(
            terminalization,
            hub,
            "cancelled",
            EXTERNAL_CANCEL_REASON,
        )
        return cancellation

    commit_terminalization_with_retry(
        terminalization,
        hub,
        "succeeded",
    )
    return None


def _handle_shutdown(
    shutdown_error: BaseException,
    terminal_error: BaseException | None,
) -> None:
    if terminal_error is not None:
        raise shutdown_error from terminal_error
    raise shutdown_error


def _final_run_result(
    terminal_error: BaseException | None,
    shutdown_error: BaseException | None,
) -> RunResult:
    """Map the last failure recorded by the run path to the terminal status."""
    error = shutdown_error if shutdown_error is not None else terminal_error
    if error is None:
        return RunResult(status="succeeded")
    if isinstance(error, (asyncio.CancelledError, WorkflowCancelledByUser)):
        return RunResult(status="cancelled", cancel_reason=str(error))
    return RunResult(status="failed")


class _GuardedStatusObserver:
    """Hub-facing lifecycle view of a status observer under the status contract.

    Composition validates the adapter contract once, but the hub keeps polling
    ``stop_requested`` on every active observer for the whole run and applies
    the observer's own capabilities when delivery fails. The guard pins that
    hub-facing contract, so a status observer can neither cancel the run nor
    become required after composition. It also records when the hub stops
    delivering to the observer, so the run path never assumes it stayed active.
    """

    capabilities: ObserverCapabilities

    def __init__(self, observer: StatusObserver) -> None:
        self._observer = observer
        self.capabilities = ObserverCapabilities(
            required=False,
            synchronous_snapshot_delivery=True,
            cleanup_after_start_timeout=(
                observer.capabilities.cleanup_after_start_timeout
            ),
        )
        self._delivery_failed = False

    @property
    def stop_requested(self) -> bool:
        return False

    @property
    def delivery_failed(self) -> bool:
        """Whether a lifecycle call failed, so status delivery stopped for this run."""
        return self._delivery_failed

    def start(self, context: RunContext) -> None:
        self._forward(self._observer.start, context)

    def on_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
    ) -> None:
        self._forward(self._observer.on_snapshot, event, snapshot)

    def stop(self, result: RunResult) -> None:
        self._observer.stop(result)

    def _forward(self, call: Callable[..., None], *args: object) -> None:
        """Record the failure the hub uses to disable this observer."""
        try:
            call(*args)
        except Exception:
            self._delivery_failed = True
            raise


def publish_terminal_status(
    observer: StatusObserver | None,
    result: RunResult,
    warning_sink: Callable[[str], None],
) -> None:
    """Publish the terminal status after the final outcome is durable.

    The observer's shutdown already ran inside the hub, before required
    observers stopped and the manifest was committed. Reporting stays best
    effort here too: a late write failure warns and never alters the run
    result or exit code.
    """
    if observer is None:
        return
    try:
        observer.publish_terminal_status(result)
    except Exception as exc:
        _warn_terminal_status_failure(warning_sink, exc)


def _warn_terminal_status_failure(
    warning_sink: Callable[[str], None],
    error: Exception,
) -> None:
    """Report a failed terminal status without ever failing the run.

    The default sink prints to the same console stream the status writer uses,
    so a persistently broken terminal makes the warning fail as well. This is
    the outer boundary of the publication path, mirroring how the hub warns and
    then disables an observer: reporting cannot replace a successful result or
    mask an error that is already propagating.
    """
    try:
        warning_sink(f"status reporting failed after run completion: {error}")
    except Exception:
        return


async def execute_workflow_with_observability(
    components: RuntimeComponents,
    workflow_topology: WorkflowTopology,
    plan: PreflightExecutionPlan,
    secret_context: SecretContext,
    output: ArtifactStorePort,
    execute_workflow_impl: ExecuteWorkflowCallable,
    persistent_logger: PersistentRunLogger,
    warning_recorder: WorkflowWarningRecorder,
    observability_hub_cls: ObservabilityHubFactory | None,
    on_scheduler_succeeded: Callable[[PreflightExecutionPlan], None],
    terminalization: TerminalizationCoordinatorProtocol,
    workflow_identity: str | None = None,
    resumed_node_ids: tuple[str, ...] = (),
) -> None:
    observability_hub_factory = _resolve_observability_hub(observability_hub_cls)
    terminal_error: BaseException | None = None
    shutdown_error: BaseException | None = None
    status_observer = (
        None
        if components.status_observer is None
        else _GuardedStatusObserver(components.status_observer)
    )
    hub_observers: list[Observer] = [persistent_logger, *components.observers]
    if status_observer is not None:
        hub_observers.append(status_observer)
    try:
        with observability_hub_factory(
            workflow_topology=workflow_topology,
            run_id=output.run_id,
            observers=hub_observers,
            refresh_per_second=0,
            warning_sink=warning_recorder.sink,
        ) as hub:
            warning_recorder.flush_queued()
            has_live_dashboard = _is_live_dashboard_available(
                hub,
                components,
                status_observer,
            )
            if components.observers and not has_live_dashboard:
                warning_recorder.sink(
                    "live dashboard unavailable; continuing without live dashboard."
                )
            selected_suppress_progress_output = _selected_progress_mode(
                components,
                has_live_dashboard,
            )
            completion = await _execute_observed_workflow(
                execute_workflow_impl=execute_workflow_impl,
                plan=plan,
                output=output,
                components=components,
                secret_context=secret_context,
                hub=hub,
                workflow_identity=workflow_identity,
                resumed_node_ids=resumed_node_ids,
                suppress_progress_output=selected_suppress_progress_output,
                terminalization=terminalization,
            )
            if isinstance(completion, BaseException):
                terminal_error = completion
            else:
                terminal_error = await _run_scheduler_post_hook(
                    completion,
                    on_scheduler_succeeded,
                    terminalization,
                    hub,
                )
        terminalization.acknowledge_observer_shutdown()
    except BaseException as exc:
        shutdown_error = exc
    finally:
        publish_terminal_status(
            components.status_observer,
            _final_run_result(terminal_error, shutdown_error),
            warning_recorder.sink,
        )
    if shutdown_error is not None:
        _handle_shutdown(shutdown_error, terminal_error)
    if terminal_error is not None:
        raise terminal_error

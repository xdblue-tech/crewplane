"""Status-adapter plugins the container loads by dotted path, as an external plugin would.

They deliberately do not import the in-tree status adapters: the container must
work through the port contract alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from rich.console import Console

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    JsonObject,
    ObserverCapabilities,
    RunResult,
)
from crewplane.core.config import Config


class FactoryCall(NamedTuple):
    factory: str
    config: Config
    run_id: str
    console: Console
    options: JsonObject | None
    workflow_path: Path | None


class RecordingObserver:
    capabilities = ObserverCapabilities(
        required=False,
        synchronous_snapshot_delivery=True,
    )

    def __init__(self, factory: str, run_id: str) -> None:
        self.factory = factory
        self.run_id = run_id
        self.terminal_states: list[str] = []

    @property
    def stop_requested(self) -> bool:
        return False

    def start(self, context: object) -> None:
        del context

    def on_snapshot(self, event: object, snapshot: object) -> None:
        del event, snapshot

    def stop(self, result: RunResult) -> None:
        del result

    def publish_terminal_status(self, result: RunResult) -> None:
        self.terminal_states.append(result.status)


class _StatusPlugin:
    calls: list[FactoryCall] = []
    fail = False

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        del options
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options={},
            option_scopes={},
        )

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()
        cls.fail = False


class LegacyStatusReporter(_StatusPlugin):
    """The original factory: no keyword the plugin did not declare."""

    calls: list[FactoryCall] = []

    def create_reporter(
        self,
        config: Config,
        run_id: str,
        console: Console,
        options: JsonObject | None = None,
    ) -> RecordingObserver:
        self.calls.append(
            FactoryCall("create_reporter", config, run_id, console, options, None)
        )
        if self.fail:
            raise TypeError("legacy plugin failed inside create_reporter")
        return RecordingObserver("create_reporter", run_id)


class WorkflowStatusReporter(LegacyStatusReporter):
    """Opts into the workflow-aware factory beside the original one."""

    calls: list[FactoryCall] = []

    def create_reporter_for_workflow(
        self,
        config: Config,
        run_id: str,
        console: Console,
        options: JsonObject | None = None,
        *,
        workflow_path: Path,
    ) -> RecordingObserver:
        self.calls.append(
            FactoryCall(
                "create_reporter_for_workflow",
                config,
                run_id,
                console,
                options,
                workflow_path,
            )
        )
        return RecordingObserver("create_reporter_for_workflow", run_id)


class BrokenWorkflowStatusReporter(WorkflowStatusReporter):
    """Opts in but returns something that is not a status observer."""

    calls: list[FactoryCall] = []

    def create_reporter_for_workflow(
        self,
        config: Config,
        run_id: str,
        console: Console,
        options: JsonObject | None = None,
        *,
        workflow_path: Path,
    ) -> object:
        self.calls.append(
            FactoryCall(
                "create_reporter_for_workflow",
                config,
                run_id,
                console,
                options,
                workflow_path,
            )
        )
        return object()

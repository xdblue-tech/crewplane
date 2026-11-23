from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from rich.console import Console

from crewplane.architecture.contracts import JsonObject, Observer, RunResult
from crewplane.core.config import Config

from .options import IntegrationOptionsCanonicalizerPort


class StatusObserver(Observer, Protocol):
    """Best-effort observer that reports run status to the hosting terminal."""

    def publish_terminal_status(self, result: RunResult) -> None:
        """Publish the terminal state once the run outcome is final.

        The observability hub stops observers before required-observer shutdown
        and manifest publication resolve, so ``stop`` must not publish the
        terminal state. The run path calls this method afterwards; a failure is
        reported as a warning and never changes the run result.
        """


class StatusReporterPort(IntegrationOptionsCanonicalizerPort, Protocol):
    """Factory contract for optional program status reporting."""

    def create_reporter(
        self,
        config: Config,
        run_id: str,
        console: Console,
        options: JsonObject | None = None,
    ) -> StatusObserver:
        """Build the observer that reports run status to the hosting terminal."""


@runtime_checkable
class WorkflowStatusReporterPort(Protocol):
    """Optional status-factory capability for reporting the run's workflow file.

    It is a separate method, not another keyword on :class:`StatusReporterPort`,
    so an existing plugin written against the original factory keeps receiving
    exactly its original arguments. An adapter opts in by implementing this
    method; otherwise only :meth:`StatusReporterPort.create_reporter` is called.
    """

    def create_reporter_for_workflow(
        self,
        config: Config,
        run_id: str,
        console: Console,
        options: JsonObject | None = None,
        *,
        workflow_path: Path,
    ) -> StatusObserver:
        """Build the observer for a run whose resolved workflow file is known.

        ``workflow_path`` is the resolved file the run was started from, so the
        adapter derives workflow identity from the run's own source rather than
        from a human title or a filesystem scan.
        """

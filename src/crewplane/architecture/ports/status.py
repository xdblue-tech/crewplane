from __future__ import annotations

from typing import Protocol

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

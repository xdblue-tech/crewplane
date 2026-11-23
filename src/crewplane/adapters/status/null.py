from __future__ import annotations

from rich.console import Console

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    DashboardSnapshot,
    ExecutionEvent,
    JsonObject,
    ObserverCapabilities,
    RunContext,
    RunResult,
)
from crewplane.core.config import Config

from .validation import validate_reporter_request


class NullStatusObserver:
    """Report nothing; the default leaves terminal output unchanged."""

    capabilities = ObserverCapabilities(synchronous_snapshot_delivery=True)

    @property
    def stop_requested(self) -> bool:
        return False

    def start(self, context: RunContext) -> None:
        del context

    def on_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
    ) -> None:
        del event, snapshot

    def stop(self, result: RunResult) -> None:
        del result

    def publish_terminal_status(self, result: RunResult) -> None:
        del result


class NullStatusReporter:
    """Create a no-op status observer."""

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        if options:
            raise ValueError(
                "none status implementation does not support options; "
                f"got: {sorted(options)}"
            )
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options={},
            option_scopes={},
        )

    def create_reporter(
        self,
        config: Config,
        run_id: str,
        console: Console,
        options: JsonObject | None = None,
    ) -> NullStatusObserver:
        """Return an observer that publishes no status sequences."""

        validate_reporter_request(config, run_id, console)
        self.canonicalize_options("none", self.__class__.__module__, options)
        return NullStatusObserver()

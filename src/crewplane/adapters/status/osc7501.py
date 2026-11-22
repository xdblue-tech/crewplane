"""Program Status Protocol (OSC 7501) reporting for terminal hosts.

A host such as herdr-core tracks ``ESC ] 7501 ; key=value : ... ESC \\``
sequences written to the program's terminal stream. The reporter observes the
run through the same hub contract as every other observer, so a broken write
disables reporting without touching execution.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Literal

from rich.console import Console

from crewplane.architecture.contracts import (
    TERMINAL_WORKFLOW_EVENT_TYPES,
    CanonicalIntegrationConfig,
    DashboardSnapshot,
    DashboardState,
    ExecutionEvent,
    ExecutionStatus,
    JsonObject,
    ObserverCapabilities,
    RunContext,
    RunResult,
)
from crewplane.core.config import Config

from .validation import validate_reporter_request

StatusState = Literal["idle", "working", "done", "blocked", "error"]
StatusWriter = Callable[[Console, bytes], None]

STATUS_APP_NAME = "crewplane"

_OSC_INTRODUCER = b"\x1b]7501;"
_STRING_TERMINATOR = b"\x1b\\"
_TERMINAL_STATES: dict[str, StatusState] = {
    "succeeded": "done",
    "failed": "error",
    "cancelled": "idle",
}
_CLEAR_SEQUENCE = _OSC_INTRODUCER + b"state=clear" + _STRING_TERMINATOR


def format_status_sequence(
    state: StatusState,
    app: str = STATUS_APP_NAME,
    msg: str | None = None,
) -> bytes:
    """Render one status sequence terminated by the string terminator."""

    pairs = [f"state={state}", f"app={app}"]
    if msg is not None:
        pairs.append(f"msg={_encode_message(msg)}")
    return _OSC_INTRODUCER + ":".join(pairs).encode("ascii") + _STRING_TERMINATOR


def write_status_sequence(console: Console, sequence: bytes) -> None:
    """Write one sequence to the console stream of an interactive terminal.

    ``Console.print`` adds markup handling, width calculation, and a newline, so
    the sequence is written to the underlying stream instead. The sequence is
    ASCII, so the text write is byte-identical to the protocol bytes.
    """

    if not console.is_terminal:
        return
    stream = console.file
    stream.write(sequence.decode("ascii"))
    stream.flush()


def write_clear_status(console: Console) -> None:
    """Release the state a host latched onto the terminal after a run exits.

    ``clear`` carries no ``app`` or ``msg``: it reports that the terminal no
    longer belongs to a tracked program rather than a new program state.
    """

    write_status_sequence(console, _CLEAR_SEQUENCE)


def _encode_message(msg: str) -> str:
    return base64.b64encode(msg.encode("utf-8")).decode("ascii")


def _activity_label(state: DashboardState) -> str:
    for node_id in state.node_order:
        node = state.nodes.get(node_id)
        if node is not None and node.status == ExecutionStatus.RUNNING:
            return node_id
    return state.workflow_name


def _is_terminal_event(event: ExecutionEvent | None) -> bool:
    return event is not None and event.event_type in TERMINAL_WORKFLOW_EVENT_TYPES


class Osc7501StatusObserver:
    """Publish workflow lifecycle as Program Status Protocol sequences."""

    capabilities = ObserverCapabilities(
        required=False,
        synchronous_snapshot_delivery=True,
    )

    def __init__(self, console: Console, writer: StatusWriter | None = None) -> None:
        self._console = console
        self._write = write_status_sequence if writer is None else writer
        self._published: bytes | None = None
        self._terminal_published = False
        self._write_failed = False

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
        if self._terminal_published or _is_terminal_event(event):
            return
        self._publish(
            format_status_sequence("working", msg=_activity_label(snapshot.state))
        )

    def stop(self, result: RunResult) -> None:
        """Defer the terminal state; :meth:`publish_terminal_status` follows.

        The hub stops observers before required-observer shutdown and manifest
        publication resolve, so the shutdown result is not a final outcome.
        """
        del result

    def publish_terminal_status(self, result: RunResult) -> None:
        """Publish the terminal state once the run path knows the final outcome."""
        if self._terminal_published:
            return
        self._terminal_published = True
        self._publish(format_status_sequence(_TERMINAL_STATES[result.status]))

    def _publish(self, sequence: bytes) -> None:
        if self._write_failed or sequence == self._published:
            return
        self._published = sequence
        try:
            self._write(self._console, sequence)
        except Exception:
            self._write_failed = True
            raise


class Osc7501StatusReporter:
    """Create the OSC 7501 status observer."""

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        if options:
            raise ValueError(
                "osc7501 status implementation does not support options; "
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
    ) -> Osc7501StatusObserver:
        """Return an observer that publishes OSC 7501 lifecycle sequences."""

        validate_reporter_request(config, run_id, console)
        self.canonicalize_options("osc7501", self.__class__.__module__, options)
        return Osc7501StatusObserver(console)

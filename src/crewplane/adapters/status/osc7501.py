"""Program Status Protocol (OSC 7501) reporting for terminal hosts.

A host such as herdr-core tracks ``ESC ] 7501 ; key=value : ... ESC \\``
sequences written to the program's terminal stream. The reporter observes the
run through the same hub contract as every other observer, so a broken write
disables reporting without touching execution.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from pathlib import Path
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
_RECORD_ID_SEGMENT = re.compile(r"[A-Za-z0-9_.+-]{1,32}")
_MAX_RECORD_ID_BYTES = 128
_WORKFLOW_SUFFIX = ".task.md"


def format_status_sequence(
    state: StatusState,
    app: str = STATUS_APP_NAME,
    msg: str | None = None,
    record_id: str | None = None,
    title: str | None = None,
) -> bytes:
    """Render one status sequence terminated by the string terminator."""

    pairs = [f"state={state}"]
    if record_id is not None:
        pairs.append(f"id={record_id}")
    pairs.append(f"app={app}")
    if title is not None:
        pairs.append(f"title={_encode_text(title)}")
    if msg is not None:
        pairs.append(f"msg={_encode_text(msg)}")
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


def _encode_text(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def companion_record_id(run_id: str) -> str | None:
    """The companion record id for a run, or None when the exact id cannot fit.

    The protocol gives a record path up to 128 bytes of one-to-eight `/`-separated
    segments of one-to-32 characters from `[A-Za-z0-9_.+-]`. A run id that needs
    another segment or escaping has no companion record; it is never hashed or
    truncated into a claimed run id.
    """

    if _RECORD_ID_SEGMENT.fullmatch(run_id) is None:
        return None
    record_id = f"{STATUS_APP_NAME}/{run_id}"
    if len(record_id.encode("ascii")) > _MAX_RECORD_ID_BYTES:
        return None
    return record_id


def companion_title(workflow_path: Path | None) -> str | None:
    """The workflow file's basename without `.task.md`, from its resolved path."""

    if workflow_path is None:
        return None
    name = workflow_path.name
    if name.endswith(_WORKFLOW_SUFFIX):
        name = name[: -len(_WORKFLOW_SUFFIX)]
    return name or None


def _running_node(state: DashboardState) -> str | None:
    for node_id in state.node_order:
        node = state.nodes.get(node_id)
        if node is not None and node.status == ExecutionStatus.RUNNING:
            return node_id
    return None


def _activity_label(state: DashboardState) -> str:
    return _running_node(state) or state.workflow_name


def _is_terminal_event(event: ExecutionEvent | None) -> bool:
    return event is not None and event.event_type in TERMINAL_WORKFLOW_EVENT_TYPES


class Osc7501StatusObserver:
    """Publish workflow lifecycle as Program Status Protocol sequences."""

    capabilities = ObserverCapabilities(
        required=False,
        synchronous_snapshot_delivery=True,
    )

    def __init__(
        self,
        console: Console,
        writer: StatusWriter | None = None,
        *,
        record_id: str | None = None,
        title: str | None = None,
    ) -> None:
        self._console = console
        self._write = write_status_sequence if writer is None else writer
        self._record_id = record_id
        self._title = title
        self._published: tuple[bytes, ...] | None = None
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
            format_status_sequence("working", msg=_activity_label(snapshot.state)),
            self._companion_sequence("working", msg=_running_node(snapshot.state)),
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
        state = _TERMINAL_STATES[result.status]
        self._publish(
            format_status_sequence(state),
            self._companion_sequence(state),
        )

    def _companion_sequence(
        self,
        state: StatusState,
        msg: str | None = None,
    ) -> bytes | None:
        if self._record_id is None or self._title is None:
            return None
        return format_status_sequence(
            state,
            msg=msg,
            record_id=self._record_id,
            title=self._title,
        )

    def _publish(self, *sequences: bytes | None) -> None:
        selected = tuple(sequence for sequence in sequences if sequence is not None)
        if self._write_failed or selected == self._published:
            return
        self._published = selected
        try:
            for sequence in selected:
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
        workflow_path: Path | None = None,
    ) -> Osc7501StatusObserver:
        """Return an observer that publishes OSC 7501 lifecycle sequences."""

        validate_reporter_request(config, run_id, console, workflow_path)
        self.canonicalize_options("osc7501", self.__class__.__module__, options)
        return Osc7501StatusObserver(
            console,
            record_id=companion_record_id(run_id),
            title=companion_title(workflow_path),
        )

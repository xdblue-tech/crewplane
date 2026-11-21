from __future__ import annotations

import base64
import io
from collections.abc import Iterable

import pytest
from rich.console import Console

from crewplane.adapters.status import (
    Osc7501StatusObserver,
    Osc7501StatusReporter,
    format_status_sequence,
    write_status_sequence,
)
from crewplane.architecture.contracts import RunResult
from crewplane.core.config import Config
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import ProviderSpec, WorkflowNode, WorkflowPlan
from crewplane.observability.events import (
    EventType,
    ExecutionEvent,
    apply_event,
    build_initial_state,
    node_event,
    workflow_event,
)
from crewplane.observability.layout import compute_topology_layout
from crewplane.observability.types import (
    DashboardSnapshot,
    RunContext,
    WorkflowTopology,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.observability import topology_from_workflow

RUN_ID = "run-1"
WORKFLOW_NAME = "status-flow"


def _topology(node_ids: tuple[str, ...] = ("first", "second")) -> WorkflowTopology:
    return topology_from_workflow(
        WorkflowPlan(
            name=WORKFLOW_NAME,
            nodes=[
                WorkflowNode(
                    id=node_id,
                    mode="sequential",
                    providers=[
                        ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)
                    ],
                )
                for node_id in node_ids
            ],
        )
    )


class _SnapshotFeed:
    """Apply events to one dashboard state and freeze the resulting snapshots."""

    def __init__(self, topology: WorkflowTopology) -> None:
        self._topology = topology
        self._state = build_initial_state(topology, RUN_ID)
        self._layout = compute_topology_layout(topology)

    def snapshot(self, events: Iterable[ExecutionEvent] = ()) -> DashboardSnapshot:
        for event in events:
            apply_event(self._state, event)
        return DashboardSnapshot(state=self._state, layout=self._layout, now=0.0)


class _RecordingWriter:
    def __init__(self) -> None:
        self.sequences: list[bytes] = []

    def __call__(self, console: Console, sequence: bytes) -> None:
        del console
        self.sequences.append(sequence)


def _append_sequence(writer: _RecordingWriter) -> Osc7501StatusObserver:
    console = Console(file=io.StringIO(), force_terminal=True, color_system=None)
    return Osc7501StatusObserver(console, writer=writer)


def test_format_status_sequence_encodes_message_as_base64() -> None:
    sequence = format_status_sequence("working", msg="Task")

    assert sequence == b"\x1b]7501;state=working:app=crewplane:msg=VGFzaw==\x1b\\"
    assert b"msg=" + base64.b64encode(b"Task") in sequence


def test_format_status_sequence_omits_absent_optional_keys() -> None:
    assert format_status_sequence("done") == b"\x1b]7501;state=done:app=crewplane\x1b\\"
    assert (
        format_status_sequence("error") == b"\x1b]7501;state=error:app=crewplane\x1b\\"
    )


def test_format_status_sequence_encodes_non_ascii_messages() -> None:
    sequence = format_status_sequence("working", msg="Prüfung: 完成")

    payload = sequence.removeprefix(b"\x1b]7501;").removesuffix(b"\x1b\\")
    encoded_message = dict(pair.split(b"=", 1) for pair in payload.split(b":")[2:3])[
        b"msg"
    ]
    assert base64.b64decode(encoded_message).decode("utf-8") == "Prüfung: 完成"


def test_stop_defers_terminal_state_until_publish_terminal_status() -> None:
    writer = _RecordingWriter()
    observer = _append_sequence(writer)

    observer.stop(RunResult(status="succeeded"))

    assert writer.sequences == []

    observer.publish_terminal_status(RunResult(status="succeeded"))

    assert writer.sequences == [format_status_sequence("done")]


@pytest.mark.parametrize(
    ("status", "state"),
    [("succeeded", "done"), ("failed", "error"), ("cancelled", "idle")],
)
def test_publish_terminal_status_maps_outcome_without_message(
    status: str,
    state: str,
) -> None:
    writer = _RecordingWriter()
    observer = _append_sequence(writer)

    observer.publish_terminal_status(RunResult(status=status))  # type: ignore[arg-type]

    assert writer.sequences == [format_status_sequence(state)]  # type: ignore[arg-type]
    assert b"msg=" not in writer.sequences[0]


def test_publish_terminal_status_publishes_one_terminal_state_per_run() -> None:
    writer = _RecordingWriter()
    observer = _append_sequence(writer)

    observer.publish_terminal_status(RunResult(status="succeeded"))
    observer.publish_terminal_status(RunResult(status="failed"))

    assert writer.sequences == [format_status_sequence("done")]


def test_snapshot_publishes_workflow_name_then_running_node() -> None:
    writer = _RecordingWriter()
    observer = _append_sequence(writer)
    topology = _topology()
    feed = _SnapshotFeed(topology)
    observer.start(
        RunContext(workflow_topology=topology, run_id=RUN_ID, refresh_per_second=4)
    )

    observer.on_snapshot(None, feed.snapshot())
    observer.on_snapshot(
        node_event(EventType.NODE_STARTED, WORKFLOW_NAME, RUN_ID, "second"),
        feed.snapshot(
            [node_event(EventType.NODE_STARTED, WORKFLOW_NAME, RUN_ID, "second")]
        ),
    )

    assert writer.sequences == [
        format_status_sequence("working", msg=WORKFLOW_NAME),
        format_status_sequence("working", msg="second"),
    ]


def test_repeated_activity_publishes_each_state_once() -> None:
    writer = _RecordingWriter()
    observer = _append_sequence(writer)
    feed = _SnapshotFeed(_topology())

    observer.on_snapshot(None, feed.snapshot())
    observer.on_snapshot(None, feed.snapshot())
    observer.on_snapshot(
        workflow_event(EventType.WORKFLOW_STARTED, WORKFLOW_NAME, RUN_ID),
        feed.snapshot(
            [workflow_event(EventType.WORKFLOW_STARTED, WORKFLOW_NAME, RUN_ID)]
        ),
    )

    assert writer.sequences == [format_status_sequence("working", msg=WORKFLOW_NAME)]


def test_terminal_workflow_event_publishes_no_status() -> None:
    writer = _RecordingWriter()
    observer = _append_sequence(writer)
    feed = _SnapshotFeed(_topology())

    for event_type in (
        EventType.WORKFLOW_FINISHED,
        EventType.WORKFLOW_FAILED,
        EventType.WORKFLOW_CANCELLED,
    ):
        event = workflow_event(event_type, WORKFLOW_NAME, RUN_ID)
        observer.on_snapshot(event, feed.snapshot([event]))

    assert writer.sequences == []


def test_snapshots_after_terminal_state_publish_nothing() -> None:
    writer = _RecordingWriter()
    observer = _append_sequence(writer)
    observer.publish_terminal_status(RunResult(status="succeeded"))

    observer.on_snapshot(None, _SnapshotFeed(_topology()).snapshot())

    assert writer.sequences == [format_status_sequence("done")]


def test_write_status_sequence_skips_non_terminal_console() -> None:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None)

    write_status_sequence(console, format_status_sequence("done"))

    assert stream.getvalue() == ""


def test_write_status_sequence_writes_exact_bytes_without_newline() -> None:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, color_system=None)
    sequence = format_status_sequence("working", msg=WORKFLOW_NAME)

    write_status_sequence(console, sequence)

    assert stream.getvalue() == sequence.decode("ascii")
    assert stream.getvalue().encode("ascii") == sequence
    assert not stream.getvalue().endswith("\n")


def test_adapter_rejects_options() -> None:
    adapter = Osc7501StatusReporter()

    with pytest.raises(ValueError, match="does not support options"):
        adapter.canonicalize_options("osc7501", "status.adapter", {"progress": 10})

    assert adapter.canonicalize_options("osc7501", "status.adapter").options == {}


def test_create_reporter_returns_synchronous_best_effort_observer() -> None:
    observer = Osc7501StatusReporter().create_reporter(
        config=Config(version=SCHEMA_VERSION, agents={}),
        run_id=RUN_ID,
        console=Console(file=io.StringIO(), force_terminal=True),
    )

    assert isinstance(observer, Osc7501StatusObserver)
    assert observer.capabilities.required is False
    assert observer.capabilities.synchronous_snapshot_delivery is True
    assert observer.stop_requested is False


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("config", {}, TypeError),
        ("run_id", "", ValueError),
        ("console", object(), TypeError),
    ],
)
def test_create_reporter_rejects_invalid_boundary_values(
    field: str,
    value: object,
    error: type[Exception],
) -> None:
    request: dict[str, object] = {
        "config": Config(version=SCHEMA_VERSION, agents={}),
        "run_id": RUN_ID,
        "console": Console(file=io.StringIO()),
    }
    request[field] = value

    with pytest.raises(error, match=field):
        Osc7501StatusReporter().create_reporter(**request)  # type: ignore[arg-type]

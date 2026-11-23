from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

import crewplane.adapters.status.osc7501 as status_adapter
import crewplane.artifacts.locks as resume_locks
import crewplane.cli.app as cli
import crewplane.cli.run.hold as hold_module
import crewplane.cli.run.terminalization as terminalization
from crewplane.architecture.contracts import RunContext
from crewplane.artifacts.locks import LOCK_OWNER_FILENAME, acquire_same_context_lock
from crewplane.artifacts.manager import OutputManager
from crewplane.cli.run import execution
from crewplane.cli.run.hold import HOLD_PROMPT
from crewplane.cli.run.observability import (
    WorkflowCancelledByUser,
    WorkflowWarningRecorder,
    publish_terminal_status,
)
from crewplane.core.config import Config, IntegrationSpec
from crewplane.observability import ObservabilityHub, RunResult
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from crewplane.runtime.execution import WorkflowExecutionError
from tests.helpers.resume_locks import FakeProcessInspector
from tests.integration.cli.cli_workflow_helpers import (
    ConsoleFactory,
    write_basic_config,
    write_basic_workflow,
)
from tests.integration.cli.repeat_force_run_support import RepeatProject, create_project
from tests.integration.cli.terminal_recovery_support import RequiredStopFailureHub
from tests.integration.cli.workflow_runner_support import (
    mock_runner_config,
    run_directories,
    run_workflow,
    runner_workflow,
)

_STATUS_SEQUENCE_PATTERN = re.compile(r"\x1b\]7501;(?P<payload>[^\x1b]*)\x1b\\")
_TERMINAL_STATES = {"done", "error", "idle", "blocked"}


def _status_config(
    *, status_implementation: str = "osc7501", **options: object
) -> Config:
    config = mock_runner_config()
    config.settings.integrations.status = IntegrationSpec(
        implementation=status_implementation,
        options=dict(options),
    )
    return config


def _status_pairs(console_text: str) -> list[dict[str, str]]:
    return [
        dict(pair.split("=", 1) for pair in match.group("payload").split(":"))
        for match in _STATUS_SEQUENCE_PATTERN.finditer(console_text)
    ]


def _root_pairs(console_text: str) -> list[dict[str, str]]:
    return [
        pairs
        for pairs in _status_pairs(console_text)
        if "id" not in pairs and pairs["state"] != "clear"
    ]


def _record_clear_pairs(console_text: str) -> list[dict[str, str]]:
    return [
        pairs
        for pairs in _status_pairs(console_text)
        if "id" in pairs and pairs["state"] == "clear"
    ]


def _companion_pairs(console_text: str) -> list[dict[str, str]]:
    """Titled companion records; the untitled startup replacement is not one."""
    return [
        pairs
        for pairs in _status_pairs(console_text)
        if "id" in pairs and pairs["state"] != "clear" and "title" in pairs
    ]


def _terminal_pairs(console_text: str) -> list[dict[str, str]]:
    return [
        pairs
        for pairs in _root_pairs(console_text)
        if pairs["state"] in _TERMINAL_STATES
    ]


def _decoded_message(pairs: dict[str, str]) -> str:
    return base64.b64decode(pairs["msg"]).decode("utf-8")


def _decoded_title(pairs: dict[str, str]) -> str:
    return base64.b64decode(pairs["title"]).decode("utf-8")


def _encoded(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _marked_root(state: str, run_id: str) -> dict[str, str]:
    """One root report that names its companion record by the exact run id."""

    return {
        "state": state,
        "app": "crewplane",
        "title": _encoded(f"crewplane/{run_id}"),
    }


def _only_run_id(root: Path) -> str:
    (run_dir,) = run_directories(root)
    manifest = json.loads((run_dir / "manifests/run.json").read_text(encoding="utf-8"))
    return manifest["run_id"]


class _TerminalStreamBreakingAtTheTerminalState:
    """Console stream that fails every write from the terminal state onwards.

    The status writer and the warning sink print to the same stream, so a
    persistently broken terminal makes the terminal write attempt fail and the
    warning that reports it fail for the same reason.
    """

    encoding = "utf-8"
    errors = "strict"

    def __init__(self) -> None:
        self.written: list[str] = []
        self.failed_writes: list[str] = []
        self.terminal_attempts: list[str] = []

    def write(self, text: str) -> int:
        if "\x1b]7501;state=done" in text:
            self.terminal_attempts.append(text)
        if self.terminal_attempts:
            self.failed_writes.append(text)
            raise OSError("terminal stream is broken")
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        if self.terminal_attempts:
            raise OSError("terminal stream is broken")

    def isatty(self) -> bool:
        return True

    def close(self) -> None:
        pass


@pytest.fixture
def status_console() -> tuple[Console, io.StringIO]:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, color_system=None, width=120)
    return console, stream


@pytest.mark.parametrize("no_live", [False, True])
def test_successful_run_reports_working_then_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
    no_live: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    console, stream = status_console

    asyncio.run(
        run_workflow(
            runner_workflow(),
            _status_config(),
            console,
            no_live=no_live,
        )
    )

    pairs = _root_pairs(stream.getvalue())
    assert pairs[0]["state"] == "working"
    assert pairs[0]["app"] == "crewplane"
    assert _decoded_message(pairs[0]) == "Task"
    assert pairs[-1] == _marked_root("done", _only_run_id(tmp_path))
    assert _terminal_pairs(stream.getvalue()) == [
        _marked_root("done", _only_run_id(tmp_path))
    ]
    assert {_decoded_message(working) for working in pairs[:-1]} == {
        "Task",
        "build.node",
    }


def test_successful_run_reports_one_companion_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
) -> None:
    monkeypatch.chdir(tmp_path)
    console, stream = status_console

    asyncio.run(run_workflow(runner_workflow(), _status_config(), console))

    run_id = _only_run_id(tmp_path)
    companions = _companion_pairs(stream.getvalue())

    assert companions
    assert {pairs["id"] for pairs in companions} == {f"crewplane/{run_id}"}
    assert {pairs["app"] for pairs in companions} == {"crewplane"}
    assert {_decoded_title(pairs) for pairs in companions} == {"workflow"}
    assert "msg" not in companions[0]
    assert {_decoded_message(pairs) for pairs in companions if "msg" in pairs} == {
        "build.node"
    }
    assert companions[-1] == {
        "state": "done",
        "id": f"crewplane/{run_id}",
        "app": "crewplane",
        "title": base64.b64encode(b"workflow").decode("ascii"),
    }


def test_a_marked_run_replaces_its_exact_record_before_its_first_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
) -> None:
    monkeypatch.chdir(tmp_path)
    console, stream = status_console

    asyncio.run(run_workflow(runner_workflow(), _status_config(), console))

    key = _only_run_id(tmp_path)
    run_id = f"crewplane/{key}"
    first_three = _status_pairs(stream.getvalue())[:3]

    assert first_three[0] == {"state": "idle", "id": run_id, "app": "crewplane"}
    assert first_three[1] == {
        "state": "working",
        "app": "crewplane",
        "title": base64.b64encode(run_id.encode()).decode("ascii"),
        "msg": base64.b64encode(b"Task").decode("ascii"),
    }
    assert first_three[2]["state"] == "working"
    assert first_three[2]["id"] == run_id
    assert _decoded_title(first_three[2]) == "workflow"
    assert _record_clear_pairs(stream.getvalue()) == []
    assert {_decoded_title(pairs) for pairs in _root_pairs(stream.getvalue())} == {
        run_id
    }


def test_force_run_reports_the_resolved_workflow_basename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
) -> None:
    monkeypatch.chdir(tmp_path)
    console, stream = status_console

    asyncio.run(
        run_workflow(
            runner_workflow(),
            _status_config(),
            console,
            force=True,
            workflow_path=Path.cwd()
            / ".crewplane"
            / "workflows"
            / "discovered.task.md",
        )
    )

    companions = _companion_pairs(stream.getvalue())
    assert companions
    assert {_decoded_title(pairs) for pairs in companions} == {"discovered"}


def test_failed_run_reports_error_without_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
) -> None:
    monkeypatch.chdir(tmp_path)
    console, stream = status_console
    config = mock_runner_config({"fail_when": [{"node_id": "build.node"}]})
    config.settings.integrations.status = IntegrationSpec(implementation="osc7501")

    with pytest.raises(WorkflowExecutionError):
        asyncio.run(run_workflow(runner_workflow(), config, console))

    assert _terminal_pairs(stream.getvalue()) == [
        _marked_root("error", _only_run_id(tmp_path))
    ]
    assert _companion_pairs(stream.getvalue())[-1] == {
        "state": "error",
        "id": f"crewplane/{_only_run_id(tmp_path)}",
        "app": "crewplane",
        "title": base64.b64encode(b"workflow").decode("ascii"),
    }


def test_export_failure_after_scheduling_reports_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
) -> None:
    monkeypatch.chdir(tmp_path)
    console, stream = status_console
    monkeypatch.setattr(
        execution,
        "fulfill_branch_exports",
        Mock(side_effect=RuntimeError("branch export failed")),
    )

    with pytest.raises(RuntimeError, match="branch export failed"):
        asyncio.run(run_workflow(runner_workflow(), _status_config(), console))

    assert _terminal_pairs(stream.getvalue()) == [
        _marked_root("error", _only_run_id(tmp_path))
    ]


def test_cancelled_run_reports_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
) -> None:
    monkeypatch.chdir(tmp_path)
    console, stream = status_console
    hubs: list[_StopRequestedHub] = []

    class _StopRequestedHub(ObservabilityHub):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self.test_stop_requested = False
            hubs.append(self)

        @property
        def stop_requested(self) -> bool:
            return self.test_stop_requested

    async def execution_started_then_cancelled(*args: object, **kwargs: object) -> None:
        del args, kwargs
        hubs[-1].test_stop_requested = True
        await asyncio.sleep(60)

    with pytest.raises(WorkflowCancelledByUser):
        asyncio.run(
            run_workflow(
                runner_workflow(),
                _status_config(),
                console,
                execute_workflow_impl=execution_started_then_cancelled,
                observability_hub_cls=_StopRequestedHub,
            )
        )

    assert _terminal_pairs(stream.getvalue()) == [
        _marked_root("idle", _only_run_id(tmp_path))
    ]


def test_required_observer_shutdown_failure_reports_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
) -> None:
    monkeypatch.chdir(tmp_path)
    console, stream = status_console

    with pytest.raises(RuntimeError, match="required observer stop failed"):
        asyncio.run(
            run_workflow(
                runner_workflow(),
                _status_config(),
                console,
                observability_hub_cls=RequiredStopFailureHub,
            )
        )

    assert _terminal_pairs(stream.getvalue()) == [
        _marked_root("error", _only_run_id(tmp_path))
    ]


def test_manifest_publication_failure_reports_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
) -> None:
    monkeypatch.chdir(tmp_path)
    console, stream = status_console

    with (
        patch.object(
            OutputManager,
            "update_run_manifest_status",
            side_effect=OSError("manifest publication failed"),
        ),
        pytest.raises(OSError, match="manifest publication failed"),
    ):
        asyncio.run(run_workflow(runner_workflow(), _status_config(), console))

    assert _terminal_pairs(stream.getvalue()) == [
        _marked_root("error", _only_run_id(tmp_path))
    ]


def test_terminal_write_failure_warns_without_changing_the_run_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
) -> None:
    monkeypatch.chdir(tmp_path)
    console, stream = status_console
    attempts: list[bytes] = []

    def fail_terminal_write(target: Console, sequence: bytes) -> None:
        del target
        attempts.append(sequence)
        if b"state=done" in sequence:
            raise OSError("terminal write failed")

    monkeypatch.setattr(status_adapter, "write_status_sequence", fail_terminal_write)

    asyncio.run(run_workflow(runner_workflow(), _status_config(), console))

    (run_dir,) = run_directories(tmp_path)
    manifest = json.loads((run_dir / "manifests/run.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "succeeded"
    assert any(b"state=working" in sequence for sequence in attempts)
    marked_done = status_adapter.format_status_sequence(
        "done", title=f"crewplane/{manifest['run_id']}"
    )
    assert [sequence for sequence in attempts if b"state=done" in sequence] == [
        marked_done
    ]
    assert attempts[-1] == marked_done
    assert "\x1b]7501;state=done" not in stream.getvalue()
    assert "status reporting failed after run completion" in stream.getvalue()


def test_terminal_status_publication_survives_a_persistently_broken_terminal() -> None:
    """A terminal that fails the final write and its warning keeps the result.

    Publication is the last best-effort step of the run path, so neither the
    status write nor the warning that reports it may raise into the caller and
    replace an outcome that is already durable.
    """
    stream = _TerminalStreamBreakingAtTheTerminalState()
    console = Console(file=stream, force_terminal=True, color_system=None, width=120)
    observer = status_adapter.Osc7501StatusObserver(console)
    warning_recorder = WorkflowWarningRecorder(
        workflow=runner_workflow(),
        console=console,
    )
    result = RunResult(status="succeeded")

    publish_terminal_status(observer, result, warning_recorder.sink)

    assert stream.terminal_attempts == [
        status_adapter.format_status_sequence("done").decode("ascii")
    ]
    assert any(
        "status reporting failed after run completion" in text
        for text in stream.failed_writes
    )
    assert result.status == "succeeded"


def test_status_observer_cannot_cancel_the_run_after_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
) -> None:
    """A status observer that starts requesting cancellation cannot stop the run."""
    monkeypatch.chdir(tmp_path)
    console, stream = status_console

    class _CancellingAfterStartObserver(status_adapter.Osc7501StatusObserver):
        def __init__(self, target_console: Console) -> None:
            super().__init__(target_console)
            self.cancel_requested = False

        @property
        def stop_requested(self) -> bool:
            return self.cancel_requested

        def start(self, context: RunContext) -> None:
            super().start(context)
            self.cancel_requested = True

    observer = _CancellingAfterStartObserver(console)
    monkeypatch.setattr(
        status_adapter.Osc7501StatusReporter,
        "create_reporter",
        Mock(return_value=observer),
    )

    asyncio.run(run_workflow(runner_workflow(), _status_config(), console))

    (run_dir,) = run_directories(tmp_path)
    manifest = json.loads((run_dir / "manifests/run.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "succeeded"
    assert _terminal_pairs(stream.getvalue()) == [
        _marked_root("done", _only_run_id(tmp_path))
    ]


def test_reporter_write_failure_leaves_execution_and_event_log_unaffected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
) -> None:
    monkeypatch.chdir(tmp_path)
    console, stream = status_console
    write_attempts = Mock(side_effect=OSError("terminal write failed"))
    monkeypatch.setattr(status_adapter, "write_status_sequence", write_attempts)

    asyncio.run(run_workflow(runner_workflow(), _status_config(), console))

    (run_dir,) = run_directories(tmp_path)
    manifest = json.loads((run_dir / "manifests/run.json").read_text(encoding="utf-8"))
    event_types = [
        json.loads(line)["event_type"]
        for line in (run_dir / "logs/events.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert manifest["status"] == "succeeded"
    assert "workflow_finished" in event_types
    assert write_attempts.call_count == 1
    assert write_attempts.call_args.args[1] == status_adapter.format_status_sequence(
        "idle", record_id=f"crewplane/{_only_run_id(tmp_path)}"
    )
    assert "\x1b]7501;" not in stream.getvalue()
    assert "observability observer disabled after error" in stream.getvalue()


def test_non_terminal_console_emits_no_sequences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None)

    asyncio.run(
        run_workflow(runner_workflow(), _status_config(), console, no_live=False)
    )

    assert "\x1b]7501;" not in stream.getvalue()


def test_none_status_implementation_emits_no_sequences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_console: tuple[Console, io.StringIO],
) -> None:
    monkeypatch.chdir(tmp_path)
    console, stream = status_console

    asyncio.run(
        run_workflow(
            runner_workflow(),
            _status_config(status_implementation="none"),
            console,
        )
    )

    assert "\x1b]7501;" not in stream.getvalue()


def test_repeated_passes_report_status_per_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, 2, node_count=1)
    project.config["settings"]["integrations"]["status"] = {
        "implementation": "osc7501",
        "options": {},
    }
    project.write()
    stream = io.StringIO()
    monkeypatch.setattr(
        cli,
        "Console",
        ConsoleFactory(file=stream, force_terminal=True, color_system=None, width=120),
    )

    result = project.run()

    assert result.exit_code == 0, (result.output, result.exception)
    passes: list[list[dict[str, str]]] = []
    current_pass: list[dict[str, str]] = []
    for pairs in _root_pairs(stream.getvalue()):
        current_pass.append(pairs)
        if pairs["state"] in _TERMINAL_STATES:
            passes.append(current_pass)
            current_pass = []
    assert current_pass == []
    assert [run_pairs[-1]["state"] for run_pairs in passes] == ["done", "done"]
    for run_pairs in passes:
        assert any(pair["state"] == "working" for pair in run_pairs[:-1])
    companions = _companion_pairs(stream.getvalue())
    assert {pairs["id"] for pairs in companions} == {
        f"crewplane/{manifest['run_id']}" for manifest in project.manifests()
    }
    assert {_decoded_title(pairs) for pairs in companions} == {"repeat"}


def test_default_status_integration_emits_no_sequences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, color_system=None, width=120)

    asyncio.run(run_workflow(runner_workflow(), mock_runner_config(), console))

    assert "\x1b]7501;" not in stream.getvalue()


@pytest.mark.parametrize("command", ["validate", "dry-run"])
def test_invalid_status_adapter_fails_preflight_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.yml"
    workflow_path = tmp_path / "workflow.task.md"
    write_basic_config(config_path)
    write_basic_workflow(workflow_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "\n".join(
            [
                "",
                "settings:",
                "  integrations:",
                "    status:",
                '      implementation: "missing"',
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    stream = io.StringIO()
    monkeypatch.setattr(
        cli,
        "Console",
        ConsoleFactory(file=stream, force_terminal=False, color_system=None, width=120),
    )
    args = (
        ["validate", str(workflow_path), "--config", str(config_path)]
        if command == "validate"
        else [
            "run",
            "--tasks",
            str(workflow_path),
            "--config",
            str(config_path),
            "--dry-run",
        ]
    )

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 1, (result.output, result.exception)
    assert "Unknown status implementation 'missing'" in " ".join(
        stream.getvalue().split()
    )


@dataclass
class HoldDismissal:
    """Key reader that checks the run's state at the moment the hold waits."""

    root: Path
    stream: io.StringIO
    expected_state: str
    interrupted: bool = False
    calls: list[str] = field(default_factory=list)

    def __call__(self) -> str:
        published = [
            pairs["state"] for pairs in _terminal_pairs(self.stream.getvalue())
        ]
        assert published and published[-1] == self.expected_state, (
            self.stream.getvalue()
        )
        assert _lock_owners(self.root) == [], (
            "the run lock is held while the hold waits"
        )
        self.calls.append(self.expected_state)
        if self.interrupted:
            raise KeyboardInterrupt
        return "a"


def _lock_owners(root: Path) -> list[Path]:
    """Same-context lock owner files, which the release path removes."""
    return sorted((root / ".crewplane" / "locks").glob(f"*/{LOCK_OWNER_FILENAME}"))


def _patch_hold_input(
    monkeypatch: pytest.MonkeyPatch, read_key: Callable[[], str]
) -> None:
    """Install a key reader for a process whose stdout and stdin are terminals."""
    monkeypatch.setattr(hold_module, "read_terminal_key", read_key)
    monkeypatch.setattr(hold_module, "terminal_key_available", lambda: True)


def _record_hold_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []

    def read_key() -> str:
        reads.append("read")
        return "a"

    _patch_hold_input(monkeypatch, read_key)
    return reads


def _hold_project(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    hold_after_run: bool,
    failing: bool = False,
    status_implementation: str = "osc7501",
) -> tuple[RepeatProject, io.StringIO]:
    """Write one single-pass project and capture its console output."""
    root.mkdir(exist_ok=True)
    project = create_project(root, None, node_count=1)
    project.config["settings"]["hold_after_run"] = hold_after_run
    project.config["settings"]["integrations"]["status"] = {
        "implementation": status_implementation,
        "options": {},
    }
    if failing:
        invoker_options = project.config["settings"]["integrations"]["invoker"][
            "options"
        ]
        invoker_options["fail_when"] = [{"node_id": "node0"}]
    project.write()
    stream = io.StringIO()
    monkeypatch.setattr(
        cli,
        "Console",
        ConsoleFactory(file=stream, force_terminal=True, color_system=None, width=120),
    )
    monkeypatch.chdir(root)
    return project, stream


def test_hold_keeps_the_terminal_state_then_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, stream = _hold_project(tmp_path, monkeypatch, hold_after_run=True)
    dismissal = HoldDismissal(tmp_path, stream, expected_state="done")
    _patch_hold_input(monkeypatch, dismissal)

    result = project.run("--hold")

    assert result.exit_code == 0, (result.output, result.exception)
    assert dismissal.calls == ["done"]
    states = [pairs["state"] for pairs in _status_pairs(stream.getvalue())]
    assert states[-2:] == ["done", "clear"]
    assert [
        pairs for pairs in _status_pairs(stream.getvalue()) if pairs["state"] == "clear"
    ] == [{"state": "clear"}]
    assert _terminal_pairs(stream.getvalue()) == [
        _marked_root("done", _only_run_id(tmp_path))
    ]
    assert HOLD_PROMPT in stream.getvalue()


def test_hold_keeps_the_companion_metadata_until_the_global_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, stream = _hold_project(tmp_path, monkeypatch, hold_after_run=True)
    seen_at_hold: list[dict[str, str]] = []

    def read_key() -> str:
        seen_at_hold.extend(_companion_pairs(stream.getvalue()))
        return "a"

    _patch_hold_input(monkeypatch, read_key)

    result = project.run("--hold")

    assert result.exit_code == 0, (result.output, result.exception)
    run_id = f"crewplane/{_only_run_id(tmp_path)}"
    assert seen_at_hold
    assert {pairs["id"] for pairs in seen_at_hold} == {run_id}
    assert {_decoded_title(pairs) for pairs in seen_at_hold} == {"repeat"}
    assert seen_at_hold[-1] == {
        "state": "done",
        "id": run_id,
        "app": "crewplane",
        "title": base64.b64encode(b"repeat").decode("ascii"),
    }
    assert _record_clear_pairs(stream.getvalue()) == []
    assert _status_pairs(stream.getvalue())[-1] == {"state": "clear"}


def test_piped_run_never_prompts_or_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, stream = _hold_project(tmp_path, monkeypatch, hold_after_run=True)
    reads = _record_hold_reads(monkeypatch)
    monkeypatch.setattr(
        cli,
        "Console",
        ConsoleFactory(file=stream, force_terminal=False, color_system=None, width=120),
    )

    result = project.run("--hold")

    assert result.exit_code == 0, (result.output, result.exception)
    assert reads == []
    assert HOLD_PROMPT not in stream.getvalue()
    assert "\x1b]7501;" not in stream.getvalue()


@pytest.mark.parametrize(
    ("failing", "expected_exit_code"),
    [(False, 0), (True, 1)],
    ids=["success", "failure"],
)
def test_hold_preserves_the_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing: bool,
    expected_exit_code: int,
) -> None:
    expected_state = "error" if failing else "done"
    opted_out, unheld_stream = _hold_project(
        tmp_path / "opted-out", monkeypatch, hold_after_run=True, failing=failing
    )
    unheld_reads = _record_hold_reads(monkeypatch)
    unheld = opted_out.run("--no-hold")
    defaulted, held_stream = _hold_project(
        tmp_path / "defaulted", monkeypatch, hold_after_run=True, failing=failing
    )
    dismissal = HoldDismissal(tmp_path / "defaulted", held_stream, expected_state)
    _patch_hold_input(monkeypatch, dismissal)
    held = defaulted.run()

    assert unheld.exit_code == expected_exit_code, (
        unheld.output,
        unheld.exception,
    )
    assert held.exit_code == unheld.exit_code, (held.output, held.exception)
    assert unheld_reads == []
    assert HOLD_PROMPT not in unheld_stream.getvalue()
    assert _root_pairs(unheld_stream.getvalue())[-1] == _marked_root(
        expected_state, _only_run_id(tmp_path / "opted-out")
    )
    assert dismissal.calls == [expected_state]
    assert HOLD_PROMPT in held_stream.getvalue()
    assert _status_pairs(held_stream.getvalue())[-1] == {"state": "clear"}


@pytest.mark.parametrize(
    ("failing", "expected_exit_code"),
    [(False, 0), (True, 1)],
    ids=["success", "failure"],
)
def test_ctrl_c_during_the_hold_keeps_the_run_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing: bool,
    expected_exit_code: int,
) -> None:
    expected_state = "error" if failing else "done"
    project, stream = _hold_project(
        tmp_path, monkeypatch, hold_after_run=True, failing=failing
    )
    dismissal = HoldDismissal(
        tmp_path, stream, expected_state=expected_state, interrupted=True
    )
    _patch_hold_input(monkeypatch, dismissal)

    result = project.run("--hold")

    assert result.exit_code == expected_exit_code, (result.output, result.exception)
    assert dismissal.calls == [expected_state]
    assert _status_pairs(stream.getvalue())[-1] == {"state": "clear"}
    assert ("Workflow 'Repeat' failed" in stream.getvalue()) is failing


def test_hold_flag_overrides_a_disabled_config_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, stream = _hold_project(tmp_path, monkeypatch, hold_after_run=False)
    dismissal = HoldDismissal(tmp_path, stream, expected_state="done")
    _patch_hold_input(monkeypatch, dismissal)

    result = project.run("--hold")

    assert result.exit_code == 0, (result.output, result.exception)
    assert dismissal.calls == ["done"]
    assert _status_pairs(stream.getvalue())[-1] == {"state": "clear"}


def test_omitted_flag_keeps_a_disabled_config_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, stream = _hold_project(tmp_path, monkeypatch, hold_after_run=False)
    reads = _record_hold_reads(monkeypatch)

    result = project.run()

    assert result.exit_code == 0, (result.output, result.exception)
    assert reads == []
    assert HOLD_PROMPT not in stream.getvalue()
    assert _root_pairs(stream.getvalue())[-1] == _marked_root(
        "done", _only_run_id(tmp_path)
    )


def test_dismissal_clears_the_status_without_a_status_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, stream = _hold_project(
        tmp_path, monkeypatch, hold_after_run=True, status_implementation="none"
    )
    reads = _record_hold_reads(monkeypatch)

    result = project.run("--hold")

    assert result.exit_code == 0, (result.output, result.exception)
    assert reads == ["read"]
    assert _status_pairs(stream.getvalue()) == [{"state": "clear"}]


def test_lock_contention_never_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, stream = _hold_project(tmp_path, monkeypatch, hold_after_run=True)
    reads = _record_hold_reads(monkeypatch)
    first = project.run("--no-hold")
    assert first.exit_code == 0, (first.output, first.exception)

    (manifest,) = project.manifests()
    lock = acquire_same_context_lock(
        tmp_path / ".crewplane",
        "Repeat",
        manifest["workflow_identity"],
        manifest["workflow_signature"],
        process_inspector=FakeProcessInspector(100, "old"),
    )
    lock.update_run(manifest["run_id"], manifest["run_key_name"])
    try:
        with pytest.MonkeyPatch.context() as live_owner:
            live_owner.setattr(
                resume_locks,
                "ProcessInspector",
                partial(FakeProcessInspector, 200, "new", live=True),
            )
            contended = project.run("--hold")
        assert _lock_owners(tmp_path) == [lock.lock_dir / LOCK_OWNER_FILENAME]
    finally:
        lock.release()

    assert contended.exit_code == 1, (contended.output, contended.exception)
    assert "Run lock unavailable" in stream.getvalue()
    assert reads == []
    assert HOLD_PROMPT not in stream.getvalue()


def test_preflight_failure_never_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, stream = _hold_project(tmp_path, monkeypatch, hold_after_run=True)
    reads = _record_hold_reads(monkeypatch)
    project.prompts["node0"] = "Read {{file:missing.txt}}"
    project.write()

    result = project.run("--hold")

    assert result.exit_code == 1, (result.output, result.exception)
    assert reads == []
    assert HOLD_PROMPT not in stream.getvalue()
    assert _status_pairs(stream.getvalue()) == []


def test_terminalization_failure_never_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, stream = _hold_project(tmp_path, monkeypatch, hold_after_run=True)
    reads = _record_hold_reads(monkeypatch)

    def fail_publication(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("run manifest could not be published")

    with pytest.MonkeyPatch.context() as failed_terminalization:
        failed_terminalization.setattr(
            terminalization, "finalize_run_manifest", fail_publication
        )
        result = project.run("--hold")

    assert result.exit_code == 1, (result.output, result.exception)
    assert reads == []
    assert HOLD_PROMPT not in stream.getvalue()
    # The unfinalized run keeps its lock, so nothing may hold that terminal.
    assert _lock_owners(tmp_path) != []


def test_duplicate_context_skip_never_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, stream = _hold_project(tmp_path, monkeypatch, hold_after_run=True)
    reads = _record_hold_reads(monkeypatch)

    first = project.run("--hold")
    second = project.run("--hold")

    assert first.exit_code == 0, (first.output, first.exception)
    assert second.exit_code == 0, (second.output, second.exception)
    assert "Identical context detected" in stream.getvalue()
    # The skipped run publishes no status, so only the first run may wait.
    assert reads == ["read"]
    assert stream.getvalue().count(HOLD_PROMPT) == 1
    assert len(project.manifests()) == 1
    assert _status_pairs(stream.getvalue()).count({"state": "clear"}) == 1
    assert _status_pairs(stream.getvalue())[-1] == {"state": "clear"}


def test_failure_diagnostic_prints_before_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, stream = _hold_project(
        tmp_path, monkeypatch, hold_after_run=True, failing=True
    )
    shown_at_hold: list[str] = []

    def read_key() -> str:
        shown_at_hold.append(stream.getvalue())
        return "a"

    _patch_hold_input(monkeypatch, read_key)

    result = project.run("--hold")

    assert result.exit_code == 1, (result.output, result.exception)
    assert shown_at_hold, "the run failed without holding"
    assert "✗ Workflow 'Repeat' failed" in shown_at_hold[0]
    assert stream.getvalue().count("✗ Workflow 'Repeat' failed") == 1
    assert _status_pairs(stream.getvalue())[-1] == {"state": "clear"}


def test_unconfirmed_cleanup_reports_the_failure_and_its_evidence_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow diagnostic and the cleanup evidence each print once, unheld."""
    project, stream = _hold_project(
        tmp_path, monkeypatch, hold_after_run=True, failing=True
    )
    reads = _record_hold_reads(monkeypatch)
    completed_run = execution.execute_workflow_run
    cleanup_error = ProcessDrainError(
        ProcessDrainEvidence(
            pid=4242,
            process_group_id=4242,
            leader_stopped=False,
            process_group_stopped=False,
        ),
        "provider drain unresolved",
    )

    async def fail_after_finalizing(*args: object, **kwargs: object) -> bool:
        with pytest.raises(WorkflowExecutionError) as failed_nodes:
            await completed_run(*args, **kwargs)
        failure = WorkflowExecutionError(
            "Workflow 'Repeat' failed:\n- failed: node0 (forced failure)"
        )
        assert failed_nodes.value.__cause__ is not None
        failure.__cause__ = cleanup_error
        raise failure

    monkeypatch.setattr(
        cli.workflow_runner, "execute_workflow_run", fail_after_finalizing
    )

    result = project.run("--hold")

    assert result.exit_code == 1, (result.output, result.exception)
    output = stream.getvalue()
    # Unconfirmed cleanup keeps the failure unfinalized, so even an explicit
    # hold flag never reaches the wait.
    assert reads == []
    assert HOLD_PROMPT not in output
    assert output.count("✗ Invalid: Workflow 'Repeat' failed:") == 1
    assert output.count("- failed: node0 (forced failure)") == 1
    assert output.count("PID 4242") == 1
    assert output.count("process group 4242") == 1


def test_interrupted_run_reports_unconfirmed_cleanup_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted run that passes its own cleanup error reports it once."""
    project, stream = _hold_project(tmp_path, monkeypatch, hold_after_run=True)
    reads = _record_hold_reads(monkeypatch)
    cleanup_error = ProcessDrainError(
        ProcessDrainEvidence(
            pid=4242,
            process_group_id=4242,
            leader_stopped=False,
            process_group_stopped=False,
        ),
        "provider drain unresolved",
    )

    async def interrupt_with_unconfirmed_cleanup(
        *args: object, **kwargs: object
    ) -> bool:
        del args, kwargs
        raise KeyboardInterrupt from cleanup_error

    monkeypatch.setattr(
        cli.workflow_runner, "execute_workflow_run", interrupt_with_unconfirmed_cleanup
    )

    result = project.run("--hold")

    assert result.exit_code == 1, (result.output, result.exception)
    output = stream.getvalue()
    assert reads == []
    assert HOLD_PROMPT not in output
    assert output.count("provider drain unresolved") == 1
    assert output.count("PID 4242") == 1
    assert output.count("process group 4242") == 1

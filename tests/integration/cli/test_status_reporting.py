from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

import crewplane.adapters.status.osc7501 as status_adapter
import crewplane.cli.app as cli
from crewplane.architecture.contracts import RunContext
from crewplane.artifacts.manager import OutputManager
from crewplane.cli.run import execution
from crewplane.cli.run.observability import (
    WorkflowCancelledByUser,
    WorkflowWarningRecorder,
    publish_terminal_status,
)
from crewplane.core.config import Config, IntegrationSpec
from crewplane.observability import ObservabilityHub, RunResult
from crewplane.runtime.execution import WorkflowExecutionError
from tests.integration.cli.cli_workflow_helpers import (
    ConsoleFactory,
    write_basic_config,
    write_basic_workflow,
)
from tests.integration.cli.repeat_force_run_support import create_project
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


def _terminal_pairs(console_text: str) -> list[dict[str, str]]:
    return [
        pairs
        for pairs in _status_pairs(console_text)
        if pairs["state"] in _TERMINAL_STATES
    ]


def _decoded_message(pairs: dict[str, str]) -> str:
    return base64.b64decode(pairs["msg"]).decode("utf-8")


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

    pairs = _status_pairs(stream.getvalue())
    assert pairs[0]["state"] == "working"
    assert pairs[0]["app"] == "crewplane"
    assert _decoded_message(pairs[0]) == "Task"
    assert pairs[-1] == {"state": "done", "app": "crewplane"}
    assert _terminal_pairs(stream.getvalue()) == [{"state": "done", "app": "crewplane"}]
    assert {_decoded_message(working) for working in pairs[:-1]} == {
        "Task",
        "build.node",
    }


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
        {"state": "error", "app": "crewplane"}
    ]


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
        {"state": "error", "app": "crewplane"}
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

    assert _terminal_pairs(stream.getvalue()) == [{"state": "idle", "app": "crewplane"}]


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
        {"state": "error", "app": "crewplane"}
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
        {"state": "error", "app": "crewplane"}
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
    assert [sequence for sequence in attempts if b"state=done" in sequence] == [
        status_adapter.format_status_sequence("done")
    ]
    assert attempts[-1] == status_adapter.format_status_sequence("done")
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
    assert _terminal_pairs(stream.getvalue()) == [{"state": "done", "app": "crewplane"}]


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
    for pairs in _status_pairs(stream.getvalue()):
        current_pass.append(pairs)
        if pairs["state"] in _TERMINAL_STATES:
            passes.append(current_pass)
            current_pass = []
    assert current_pass == []
    assert [run_pairs[-1]["state"] for run_pairs in passes] == ["done", "done"]
    for run_pairs in passes:
        assert any(pair["state"] == "working" for pair in run_pairs[:-1])


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

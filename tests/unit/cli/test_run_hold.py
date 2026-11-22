from __future__ import annotations

import io
import os
import signal
import types

import pytest
from rich.console import Console

import crewplane.cli.run.hold as hold_module
from crewplane.cli.run.hold import (
    HOLD_PROMPT,
    hold_after_execution_failure,
    hold_after_run,
    hold_requested,
    terminal_key_available,
)
from crewplane.core.config import Config, Settings
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from crewplane.runtime.execution import WorkflowExecutionError
from crewplane.version import SCHEMA_VERSION

_CLEAR_BYTES = "\x1b]7501;state=clear\x1b\\"

# The hold restores the terminal from the handlers of suspension and termination
# signals; only POSIX exposes all three.
_TERMINAL_SIGNALS = tuple(
    signum
    for name in ("SIGTSTP", "SIGTERM", "SIGHUP")
    if isinstance(signum := getattr(signal, name, None), int)
)


def _config(*, hold_after_run: bool) -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={},
        settings=Settings(hold_after_run=hold_after_run),
    )


def _console(*, terminal: bool) -> tuple[Console, io.StringIO]:
    stream = io.StringIO()
    console = Console(
        file=stream,
        force_terminal=terminal,
        color_system=None,
        width=120,
    )
    return console, stream


class _UnwritableStream(io.StringIO):
    """A terminal stream that fails every write, like a closed pane."""

    def write(self, text: str) -> int:
        del text
        raise OSError("terminal stream is broken")


def _raise_oserror() -> bool:
    raise OSError("stdin is unavailable")


@pytest.mark.parametrize(
    ("flag", "configured", "expected"),
    [
        (None, False, False),
        (None, True, True),
        (True, False, True),
        (False, True, False),
    ],
    ids=["default-off", "default-on", "flag-on", "flag-off"],
)
def test_flag_overrides_the_project_default(
    flag: bool | None,
    configured: bool,
    expected: bool,
) -> None:
    assert hold_requested(flag, _config(hold_after_run=configured)) is expected


def _drain_failure() -> ProcessDrainError:
    return ProcessDrainError(
        ProcessDrainEvidence(
            pid=4242,
            process_group_id=4242,
            leader_stopped=False,
            process_group_stopped=False,
        ),
        "provider process group could not be drained",
    )


def test_execution_failure_holds_only_after_a_finalized_run() -> None:
    assert hold_after_execution_failure(WorkflowExecutionError("nodes failed"))

    unconfirmed = WorkflowExecutionError("nodes failed")
    unconfirmed.__cause__ = _drain_failure()

    assert not hold_after_execution_failure(unconfirmed)


def test_non_terminal_console_never_prompts_or_reads() -> None:
    console, stream = _console(terminal=False)
    reads: list[str] = []
    clears: list[Console] = []

    def record_read() -> str:
        reads.append("read")
        return "a"

    hold_after_run(console, read_key=record_read, clear=clears.append)

    assert reads == []
    assert clears == []
    assert HOLD_PROMPT not in stream.getvalue()


def test_redirected_stdin_clears_without_prompting_or_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, stream = _console(terminal=True)
    reads: list[str] = []
    clears: list[Console] = []
    monkeypatch.setattr(hold_module, "terminal_key_available", lambda: False)
    monkeypatch.setattr(
        hold_module, "read_terminal_key", lambda: reads.append("read") or "a"
    )

    hold_after_run(console, clear=clears.append)

    assert reads == []
    assert clears == [console]
    assert HOLD_PROMPT not in stream.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="POSIX stdin gating")
@pytest.mark.parametrize(
    ("isatty", "expected"),
    [(False, False), (True, True)],
    ids=["redirected", "terminal"],
)
def test_terminal_key_available_follows_stdin(
    monkeypatch: pytest.MonkeyPatch,
    isatty: bool,
    expected: bool,
) -> None:
    monkeypatch.setattr(
        hold_module.sys, "stdin", types.SimpleNamespace(isatty=lambda: isatty)
    )

    assert terminal_key_available() is expected


def test_dismissal_prompts_and_clears_the_status_through_the_injected_writer() -> None:
    console, stream = _console(terminal=True)
    clears: list[Console] = []

    hold_after_run(console, read_key=lambda: "a", clear=clears.append)

    assert HOLD_PROMPT in stream.getvalue()
    assert clears == [console]


def test_dismissal_clears_the_status_on_the_terminal_stream() -> None:
    console, stream = _console(terminal=True)

    hold_after_run(console, read_key=lambda: "a")

    assert _CLEAR_BYTES in stream.getvalue()


def test_clear_failure_warns_without_raising() -> None:
    console, stream = _console(terminal=True)

    def fail(clear_console: Console) -> None:
        del clear_console
        raise OSError("terminal stream is broken")

    hold_after_run(console, read_key=lambda: "a", clear=fail)

    output = stream.getvalue()
    assert "status clear failed" in output
    assert "terminal stream is broken" in output


def test_ctrl_c_byte_clears_the_status_and_returns() -> None:
    """``msvcrt.getwch`` returns Ctrl+C as a byte instead of raising SIGINT."""
    console, stream = _console(terminal=True)
    clears: list[Console] = []

    hold_after_run(console, read_key=lambda: "\x03", clear=clears.append)

    assert clears == [console]
    assert HOLD_PROMPT in stream.getvalue()


def test_interrupted_read_clears_the_status_and_returns() -> None:
    """The cbreak wait keeps ISIG, so SIGINT arrives as an interrupt."""
    console, stream = _console(terminal=True)
    clears: list[Console] = []

    def interrupted_read() -> str:
        raise KeyboardInterrupt

    hold_after_run(console, read_key=interrupted_read, clear=clears.append)

    assert clears == [console]
    assert HOLD_PROMPT in stream.getvalue()


def test_unreadable_key_clears_the_status_and_returns() -> None:
    console, stream = _console(terminal=True)
    clears: list[Console] = []

    def fail_read() -> str:
        raise OSError("stdin is not a terminal")

    hold_after_run(console, read_key=fail_read, clear=clears.append)

    assert clears == [console]
    assert HOLD_PROMPT in stream.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="POSIX stdin gating")
@pytest.mark.parametrize(
    "unavailable",
    [
        pytest.param(None, id="missing-stdin"),
        pytest.param(types.SimpleNamespace(isatty=_raise_oserror), id="isatty-raises"),
    ],
)
def test_unavailable_stdin_clears_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
    unavailable: object,
) -> None:
    console, stream = _console(terminal=True)
    reads: list[str] = []
    clears: list[Console] = []
    monkeypatch.setattr(hold_module.sys, "stdin", unavailable)
    monkeypatch.setattr(
        hold_module, "read_terminal_key", lambda: reads.append("read") or "a"
    )

    hold_after_run(console, clear=clears.append)

    assert reads == []
    assert clears == [console]
    assert HOLD_PROMPT not in stream.getvalue()


def test_broken_output_stream_clears_without_waiting() -> None:
    console = Console(
        file=_UnwritableStream(),
        force_terminal=True,
        color_system=None,
        width=120,
    )
    reads: list[str] = []
    clears: list[Console] = []

    def record_read() -> str:
        reads.append("read")
        return "a"

    hold_after_run(console, read_key=record_read, clear=clears.append)

    assert reads == []
    assert clears == [console]


def test_clear_failure_on_a_broken_stream_cannot_raise() -> None:
    stream = _UnwritableStream()
    console = Console(
        file=stream,
        force_terminal=True,
        color_system=None,
        width=120,
    )

    def fail(clear_console: Console) -> None:
        del clear_console
        raise OSError("terminal stream is broken")

    hold_after_run(console, read_key=lambda: "a", clear=fail)

    assert stream.getvalue() == ""


def test_exit_restores_the_terminal_after_a_failed_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def ignore_handler_install(signum: int, handler: object) -> None:
        del signum, handler

    monkeypatch.setattr(signal, "signal", ignore_handler_install)
    calls: list[str] = []

    with (
        pytest.raises(OSError, match="terminal went away"),
        hold_module.cbreak_terminal(
            lambda: calls.append("restore"), lambda: calls.append("cbreak")
        ),
    ):
        raise OSError("terminal went away")

    assert calls == ["cbreak", "restore"]


def test_wait_restores_the_previous_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handled: dict[int, object] = {
        signum: signal.SIG_IGN for signum in _TERMINAL_SIGNALS
    }
    monkeypatch.setattr(signal, "getsignal", lambda signum: handled[signum])
    monkeypatch.setattr(
        signal, "signal", lambda signum, handler: handled.__setitem__(signum, handler)
    )

    with hold_module.restored_terminal_signals(lambda: None, lambda: None):
        assert all(callable(handled[signum]) for signum in _TERMINAL_SIGNALS)

    assert handled == dict.fromkeys(_TERMINAL_SIGNALS, signal.SIG_IGN)


def test_unavailable_signal_api_keeps_the_wait_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(signum: int, handler: object) -> None:
        del handler
        raise ValueError(f"signal {signum} only works in main thread")

    monkeypatch.setattr(signal, "signal", refuse)
    waiting: list[str] = []

    with hold_module.restored_terminal_signals(lambda: None, lambda: None):
        waiting.append("holding")

    assert waiting == ["holding"]


def test_signal_protection_spans_the_whole_cbreak_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal during either transition still finds the terminal restored."""
    events: list[str] = []
    handled: dict[int, object] = dict.fromkeys(_TERMINAL_SIGNALS, signal.SIG_IGN)

    def record_handler(signum: int, handler: object) -> None:
        events.append("install" if callable(handler) else "uninstall")
        handled[signum] = handler

    monkeypatch.setattr(signal, "getsignal", lambda signum: handled[signum])
    monkeypatch.setattr(signal, "signal", record_handler)
    signal_count = len(_TERMINAL_SIGNALS)

    with hold_module.cbreak_terminal(
        lambda: events.append("restore"), lambda: events.append("cbreak")
    ):
        events.append("wait")

    assert events == [
        *["install"] * signal_count,
        "cbreak",
        "wait",
        "restore",
        *["uninstall"] * signal_count,
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX suspension and termination signals")
@pytest.mark.parametrize(
    "_signum", _TERMINAL_SIGNALS, ids=lambda signum: signal.Signals(signum).name
)
def test_standalone_handler_restores_the_terminal_then_takes_the_default_action(
    monkeypatch: pytest.MonkeyPatch,
    _signum: int,
) -> None:
    """A handler built outside a guard has no wait to re-enter."""
    events: list[object] = []
    installs: list[tuple[int, object]] = []

    def record_install(signum: int, installed: object) -> None:
        installs.append((signum, installed))

    monkeypatch.setattr(signal, "signal", record_install)
    monkeypatch.setattr(
        signal, "raise_signal", lambda signum: events.append(("raise", signum))
    )
    handler = hold_module.restore_then_default_handler(
        lambda: events.append("restore"), lambda: events.append("resume")
    )

    handler(_signum, None)

    assert events == ["restore", ("raise", _signum)]
    # The default action ends a termination and returns on resumption, where the
    # handler keeps itself installed; only a guard's teardown removes it.
    assert installs == [(_signum, signal.SIG_DFL), (_signum, handler)]


# A handler installed before the guard starts; nothing the guard installs or
# restores looks like it.
_PREVIOUS_HANDLER = 0


class _TerminalState:
    """The terminal mode the guard's own restore and cbreak calls leave."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.in_cbreak = False

    def restore(self) -> None:
        self.calls.append("restore")
        self.in_cbreak = False

    def enter_cbreak(self) -> None:
        self.calls.append("cbreak")
        self.in_cbreak = True


class _SignalTable:
    """The handler table a real signal would inherit, keyed like ``signal`` does.

    Installation records into the table, so a test can see which handlers the
    guard installed, what they were installed over, and take a handler out of
    the table exactly when a signal lands in a transition.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.handlers: dict[int, object] = dict.fromkeys(
            _TERMINAL_SIGNALS, _PREVIOUS_HANDLER
        )
        self.installs: list[int] = []
        monkeypatch.setattr(hold_module.signal, "raise_signal", self.raise_signal)
        monkeypatch.setattr(hold_module.signal, "getsignal", self.getsignal)
        monkeypatch.setattr(hold_module.signal, "signal", self.signal)

    def getsignal(self, signum: int) -> object:
        return self.handlers[signum]

    def signal(self, signum: int, handler: object) -> None:
        self.installs.append(signum)
        self.handlers[signum] = handler

    def raise_signal(self, signum: int) -> None:
        """Deliver nothing: the test calls the handler it is about directly."""

    def pending_handler(self, signum: int) -> object:
        handler = self.handlers[signum]
        assert callable(handler)
        return handler


class _SuspendingTable(_SignalTable):
    """A handler table that delivers one suspension as the guard installs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, signum: int) -> None:
        super().__init__(monkeypatch)
        self.suspend_signum = signum
        self.suspended: list[bool] = []
        self.reinstalled: list[bool] = []

    def signal(self, signum: int, handler: object) -> None:
        _SignalTable.signal(self, signum, handler)
        if signum != self.suspend_signum or self.suspended:
            return
        self.suspended.append(True)
        self.pending_handler(signum)(signum, None)
        self.reinstalled.append(callable(self.handlers[signum]))


@pytest.mark.skipif(os.name == "nt", reason="POSIX suspension and termination signals")
def test_suspension_during_terminal_restoration_cannot_reenter_cbreak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown restores the terminal before it removes the handlers."""
    signum = _TERMINAL_SIGNALS[0]
    table = _SignalTable(monkeypatch)
    terminal = _TerminalState()
    waited_in_cbreak: list[bool] = []
    suspended: list[bool] = []

    def restore() -> None:
        terminal.restore()
        if not suspended:
            suspended.append(True)
            table.pending_handler(signum)(signum, None)

    with hold_module.cbreak_terminal(restore, terminal.enter_cbreak):
        waited_in_cbreak.append(terminal.in_cbreak)

    assert suspended == [True]
    assert waited_in_cbreak == [True]
    # Resuming re-enters cbreak only from an active wait, so cbreak after
    # teardown's restore would mean the terminal was left in cbreak mode for the
    # shell that replaces crewplane.
    assert terminal.calls == ["cbreak", "restore", "restore"]
    assert not terminal.in_cbreak
    # A resumption's reinstall cannot outlive the guard's own teardown.
    assert all(handler is _PREVIOUS_HANDLER for handler in table.handlers.values())


@pytest.mark.skipif(os.name == "nt", reason="POSIX suspension and termination signals")
def test_suspension_during_handler_installation_keeps_every_signal_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal can land between the guard's handler installs."""
    signum = _TERMINAL_SIGNALS[0]
    table = _SuspendingTable(monkeypatch, signum)
    terminal = _TerminalState()
    protected: list[bool] = []
    waited_in_cbreak: list[bool] = []

    with hold_module.cbreak_terminal(terminal.restore, terminal.enter_cbreak):
        protected.append(
            all(callable(table.handlers[each]) for each in _TERMINAL_SIGNALS)
        )
        waited_in_cbreak.append(terminal.in_cbreak)

    assert table.suspended == [True]
    # Resuming reinstalls the handler, so a second signal still restores the
    # terminal instead of stopping the process under SIG_DFL.
    assert table.reinstalled == [True]
    assert protected == [True]
    # The resumption happened outside the wait, so it restored the terminal
    # without putting cbreak back; the guard entered cbreak for the key wait.
    assert terminal.calls == ["restore", "cbreak", "restore"]
    assert waited_in_cbreak == [True]
    assert not terminal.in_cbreak


@pytest.mark.skipif(os.name == "nt", reason="POSIX suspension and termination signals")
def test_suspension_during_cbreak_entry_keeps_the_wait_in_cbreak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal can land after cbreak is entered but before the wait begins."""
    signum = _TERMINAL_SIGNALS[0]
    table = _SignalTable(monkeypatch)
    terminal = _TerminalState()
    suspended: list[bool] = []
    waited_in_cbreak: list[bool] = []

    def enter_cbreak() -> None:
        terminal.enter_cbreak()
        if not suspended:
            suspended.append(True)
            table.pending_handler(signum)(signum, None)

    with hold_module.cbreak_terminal(terminal.restore, enter_cbreak):
        waited_in_cbreak.append(terminal.in_cbreak)

    assert suspended == [True]
    # The suspension restored the terminal; resuming re-enters cbreak, so the
    # key wait never begins in the terminal's restored mode.
    assert terminal.calls == ["cbreak", "restore", "cbreak", "restore"]
    assert waited_in_cbreak == [True]
    assert not terminal.in_cbreak


def test_failed_handler_installation_leaves_no_handler_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure between installs must not leak the handlers already installed."""
    failed: list[bool] = []

    class _FailingTable(_SignalTable):
        def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
            super().__init__(monkeypatch)
            monkeypatch.setattr(hold_module.signal, "signal", self.signal)

        def signal(self, signum: int, handler: object) -> None:
            if failed:
                # The guard's rollback restores the handler it installed over.
                _SignalTable.signal(self, signum, handler)
                return
            failed.append(True)
            _SignalTable.signal(self, signum, handler)
            raise KeyboardInterrupt

    failing = _FailingTable(monkeypatch)

    with (
        pytest.raises(KeyboardInterrupt),
        hold_module.cbreak_terminal(lambda: None, lambda: None),
    ):
        pytest.fail("the wait must not start without its handlers")

    assert len(_TERMINAL_SIGNALS) > 1
    # The second entry is the guard restoring the handler it installed over.
    assert failing.installs == [_TERMINAL_SIGNALS[0], _TERMINAL_SIGNALS[0]]
    assert all(handler is _PREVIOUS_HANDLER for handler in failing.handlers.values())

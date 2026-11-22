"""Keep the terminal open after a finished run so a host keeps the status.

herdr-app closes a pane when the program inside it exits, which removes the
entry the OSC 7501 reporter published for the run. Holding is a CLI-layer wait:
the status observer stays observational, and everything here happens after the
run outcome is final, published, and unlocked.
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from types import FrameType

from rich.console import Console

from crewplane.adapters.status import write_clear_status
from crewplane.core.config import Config
from crewplane.runtime.agent.process.drain import unconfirmed_process_cleanup

KeyReader = Callable[[], str]
ClearWriter = Callable[[Console], None]
TerminalRestore = Callable[[], None]
SignalHandler = Callable[[int, FrameType | None], object]
SignalHandlers = SignalHandler | int | None

HOLD_PROMPT = "Run complete — press any key to exit"

# Suspension and termination do not unwind Python frames, so these signals
# restore the saved terminal state from their handlers. ``signal`` only exposes
# the POSIX set, and this module is imported on every platform.
_RESTORED_SIGNALS: tuple[int, ...] = tuple(
    signum
    for name in ("SIGTSTP", "SIGTERM", "SIGHUP")
    if isinstance(signum := getattr(signal, name, None), int)
)


class _WaitingPhase:
    """Whether the terminal guard is in the key wait rather than a transition.

    The guard installs its signal handlers, enters cbreak, waits, restores the
    terminal, and only then removes the handlers. A suspension inside a
    transition resumes straight into the handler's default action's return, so
    the handler reinstalls itself in every phase but re-enters cbreak only while
    the wait owns the terminal.
    """

    def __init__(self, *, active: bool = False) -> None:
        self.active = active


def hold_requested(flag: bool | None, config: Config) -> bool:
    """Resolve the per-run flag over the project default."""
    return config.settings.hold_after_run if flag is None else flag


def hold_after_execution_failure(error: BaseException) -> bool:
    """Whether a failed run published its outcome before anything holds.

    Unconfirmed provider cleanup makes the lock policy retain the same-context
    lock, so that run has not finalized and the terminal still belongs to it.
    """
    return unconfirmed_process_cleanup(error) is None


def hold_after_run(
    console: Console,
    read_key: KeyReader | None = None,
    clear: ClearWriter | None = None,
) -> None:
    """Wait for one key press, then clear the terminal status.

    Nothing here outranks the run: a non-terminal console never blocks or
    writes, and a hold that has no usable prompt or reader clears the status and
    returns without waiting. Dismissal — any key or Ctrl+C — clears the status
    and returns, so a pending run outcome keeps its own exception and exit code.
    """
    if not console.is_terminal:
        return

    reader = read_key
    if reader is None:
        if not terminal_key_available():
            # A published status with no reader would stay latched on the shell
            # that replaces crewplane (herdr-core issue #57), so release it even
            # though nothing can wait.
            _clear_status(console, clear)
            return
        reader = read_terminal_key

    if not _write_hold_output(console, HOLD_PROMPT):
        # A prompt the terminal never showed leaves a wait nobody can see or
        # dismiss; release the status instead of waiting on a live stdin.
        _clear_status(console, clear)
        return
    # A reader that cannot produce a key press, whether stdin went away before
    # the read or the read failed, had no wait anyone could dismiss, so it
    # releases the status rather than leaving it latched on the exit shell.
    _await_dismissal(reader)
    _clear_status(console, clear)


def terminal_key_available() -> bool:
    """Whether the default reader has a terminal to wait on.

    A terminal on stdout alone means stdin is redirected, where a read fails or
    returns at once. The hold then clears the status and returns without
    prompting, instead of waiting on a run nobody can dismiss. stdin that is
    missing entirely cannot be a terminal either.
    """
    if os.name == "nt":
        return True
    stdin = sys.stdin
    if stdin is None:
        return False
    try:
        return stdin.isatty()
    except Exception:
        return False


def read_terminal_key() -> str:
    """Read one key press from the process terminal without a newline."""

    if os.name != "nt":
        return _read_key_from_stdin()
    try:
        import msvcrt
    except ImportError:
        # Native Windows support is pending (ADR 0018); never block without a
        # terminal reader to wait on.
        return ""
    getwch: Callable[[], str] = vars(msvcrt)["getwch"]
    return getwch()


def _await_dismissal(reader: KeyReader) -> None:
    """Wait until the hold is dismissed, treating an unreadable stdin as one.

    Ctrl+C dismisses the hold: SIGINT stays enabled in the cbreak wait, and
    turning it into an interrupt would replace the run outcome the hold exists
    to keep on screen. A read that fails, and a key press alike, end the wait so
    the run keeps its outcome either way.
    """
    try:
        reader()
    except KeyboardInterrupt:
        return
    except Exception:
        # A closed or redirected stdin has no key press to wait for; the caller
        # releases the status so nothing stays latched on the terminal.
        return


def _write_hold_output(console: Console, message: str) -> bool:
    """Write hold output without ever failing the run.

    The hold runs after the outcome is decided, so a terminal that cannot take
    output must not replace a successful result or mask an error that is
    already propagating. The result says whether the console took the text.
    """
    try:
        console.print(message)
    except Exception:
        return False
    return True


def _clear_status(console: Console, clear: ClearWriter | None) -> None:
    writer = write_clear_status if clear is None else clear
    try:
        writer(console)
    except Exception as exc:
        _write_hold_output(console, f"[yellow]WARN[/] status clear failed: {exc}")


def _read_key_from_stdin() -> str:
    """Wait for one key press with the process terminal in cbreak mode."""
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(descriptor)

    def restore() -> None:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)

    def enter_cbreak() -> None:
        tty.setcbreak(descriptor)

    with cbreak_terminal(restore, enter_cbreak):
        return _wait_for_key(descriptor)


def _wait_for_key(descriptor: int) -> str:
    """Return the next key press, waiting again if a signal interrupts it."""
    while True:
        try:
            data = os.read(descriptor, 1)
        except InterruptedError:
            # A suspension stopped the wait; the terminal guard has already put
            # cbreak back before this read runs again.
            continue
        if not data:
            return ""
        # Latin-1 maps every byte to one character, so any key press round-trips.
        return data.decode("latin-1")


@contextmanager
def cbreak_terminal(
    restore: TerminalRestore,
    enter_cbreak: TerminalRestore,
) -> Iterator[None]:
    """Keep cbreak on, restoring the terminal state on every exit path.

    Signal protection is installed before cbreak is entered and lifted only
    after cbreak is restored, so no transition window leaves echo disabled under
    a default signal action. A signal that arrives before cbreak was entered
    still restores the terminal, which is unchanged and therefore cheap to
    restore.
    """
    waiting = _WaitingPhase()
    with restored_terminal_signals(restore, enter_cbreak, waiting):
        try:
            # The wait claims the terminal before cbreak is entered, so a
            # suspension in the window between the two resumes into cbreak
            # instead of starting the key wait in the restored mode.
            waiting.active = True
            enter_cbreak()
            yield
        finally:
            waiting.active = False
            restore()


@contextmanager
def restored_terminal_signals(
    restore: TerminalRestore,
    resume: TerminalRestore,
    waiting: _WaitingPhase | None = None,
) -> Iterator[None]:
    """Restore the terminal before a suspending or terminating signal acts.

    Installation is skipped where the signal module cannot be used, such as off
    the main thread; the terminal guard's ``finally`` still covers every path
    that unwinds.
    """
    phase = _WaitingPhase(active=True) if waiting is None else waiting
    installed = _install_restoring_handlers(restore, resume, phase)
    try:
        yield
    finally:
        _restore_handlers(installed)


def _install_restoring_handlers(
    restore: TerminalRestore,
    resume: TerminalRestore,
    waiting: _WaitingPhase,
) -> dict[int, SignalHandlers]:
    """Install the restore-then-default handlers that the platform accepts.

    A platform that refuses a handler is not an error, but any other failure
    leaves the handlers already installed behind, so they are removed before the
    failure propagates.
    """
    installed: dict[int, SignalHandlers] = {}
    for signum in _RESTORED_SIGNALS:
        try:
            previous = signal.getsignal(signum)
            handler = restore_then_default_handler(restore, resume, waiting)
            installed[signum] = previous
            signal.signal(signum, handler)
        except (OSError, ValueError):
            installed.pop(signum, None)
            return installed
        except BaseException:
            # An unexpected failure, such as a KeyboardInterrupt between two
            # installs, must not leave handlers pointing at a finished guard.
            _restore_handlers(installed)
            raise
    return installed


def _restore_handlers(installed: dict[int, SignalHandlers]) -> None:
    for signum, previous in installed.items():
        try:
            signal.signal(signum, previous)
        except (OSError, ValueError):
            return


def restore_then_default_handler(
    restore: TerminalRestore,
    resume: TerminalRestore,
    waiting: _WaitingPhase | None = None,
) -> SignalHandler:
    """Build a handler that restores the terminal, then takes the default action.

    ``signal.raise_signal`` runs that action: termination ends the process, and
    a suspension returns here once the process resumes. Resuming reinstalls the
    handler in every phase but re-enters cbreak only while the wait is active,
    so a signal inside installation or teardown cannot put the terminal back
    into cbreak after the run restored it.
    """
    phase = _WaitingPhase() if waiting is None else waiting

    def handle(received: int, frame: FrameType | None) -> None:
        del frame
        try:
            restore()
        finally:
            # A terminal that cannot be restored must not swallow the signal.
            signal.signal(received, signal.SIG_DFL)
            signal.raise_signal(received)
            # Only a suspension returns here. The handler keeps itself installed
            # so a signal that lands while the guard is still installing or
            # already restoring is protected too; re-entering cbreak stays with
            # the wait that owns the terminal.
            with suppress(OSError, ValueError):
                signal.signal(received, handle)
            if phase.active:
                resume()

    return handle

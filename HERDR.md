# Wiring Crewplane Status into herdr-app

Operational guide for making crewplane runs visible in
[herdr-app](https://github.com/herdrdev/herdr-app) — the agent sidebar in
general, and the `spaces-agents` mod in particular — via the OSC 7501 "Program
Status Protocol" status integration. For the architecture analysis, tiers, and
scope decisions behind this integration, see [INTEGRATION.md](INTEGRATION.md).
For the user-facing reference of the integration itself, see
[docs/reference/integrations.md](docs/reference/integrations.md).

## How herdr detects crewplane

herdr-core parses OSC 7501 sequences **only from the ptys of terminals it
manages**. There is no agent whitelist, no plugin registration, and no process
matching: any program printing a valid sequence to a herdr-managed terminal
appears as an agent. Consequences that matter for wiring:

- The launch wrapper is irrelevant. `uv run --project <repo> --locked crewplane
  run ...`, a PATH-installed `crewplane`, or a shell script all work equally
  well — herdr keys on the emitted `app=crewplane` label, not on `uv`, `python`,
  or the executable name.
- A run launched from a plain terminal, tmux pane, or IDE terminal is invisible
  to herdr no matter how correct the sequences are. Nothing bridges external
  terminals.
- `crewplane` does not need to be registered in herdr-app's `programs.json`
  for status display. Program registration only affects launching and
  iconography.

## Prerequisites

- A crewplane build that includes the `status` integration kind (after 0.4.3;
  the `osc7501` implementation under `src/crewplane/adapters/status/`).
- herdr-core / herdr-app with OSC 7501 support (late 2026 versions).
- The run must execute inside a herdr-managed terminal (a herdr tab/pane).

## Wiring into an existing project

The status integration is opt-in per project. In the project's
`.crewplane/config.yml`, add under `settings.integrations` (next to `ui:`):

```yaml
settings:
  integrations:
    status:
      implementation: "osc7501"
```

It has no options. The default is `none`, which leaves terminal output
unchanged — an unmodified project emits no OSC bytes at all, which is the most
common reason a run does not show up in herdr.

Then run the workflow from inside a herdr pane:

```sh
cd /path/to/project
crewplane run -t .crewplane/workflows/my.task.md --no-live
```

`--no-live` is recommended under herdr: crewplane's own tmux dashboard conflicts
with herdr's terminal ownership, and the status integration works independently
of the live UI (see `src/crewplane/bootstrap/container.py` — the status
reporter is created regardless of the live-UI gate).

If your herdr-app uses the `spaces-agents` mod, note its scope filter: by
default it shows only agents whose tab is in the currently selected space. A
run in a tab of another space (or outside any space) is hidden until you
toggle the mod to "All spaces".

## What herdr displays

- While the run executes: `state=working` with `app=crewplane` and a `msg`
  label that is the base64 of the workflow name, or of the currently running
  node id when one is active. The label changes as nodes start.
- When the run can name a companion record, that same root also carries
  `title=<base64 crewplane/<run-id>>`: the record this run owns. "Can" means
  the exact run ID is one protocol segment and the resolved workflow file has a
  name. Beside the root, the companion holds the display identity:
  `id=crewplane/<run-id>:app=crewplane:title=<base64 workflow basename>` with
  `msg` set only to the first running node in workflow order. The basename
  comes from the resolved workflow path, so auto-discovered (`--force`) and
  wrapper launches carry it too. Every report replaces the record, so a
  terminal state keeps the title without a stale node.
- Run IDs can repeat across workflows, so before its first marked root a run
  replaces any record an earlier run left at that same key with an ordinary
  untitled idle report: `state=idle:id=crewplane/<run-id>:app=crewplane`. That
  replaces the one exact record, not its descendants or the terminal's other
  records; no clear is added. Then every working and final report writes root,
  then companion. A run that cannot name a companion writes the plain root
  alone: no pointer, no exact-key replacement. A record that is missing,
  unmarked, from another program, or not in the same raw state is not verified
  identity, and a reader falls back rather than reading another record.
- The host's record store is bounded. Replacing an existing key does not evict
  another record; inserting an absent key while the store is full can evict one
  under the host's normal policy. The installed herdr-core held 255 records in
  the live check. That is a host limit, not a Crewplane-configured one: the
  producer neither detects nor enforces capacity.
- At the end, exactly one terminal state: `done` on success, `error` on
  failure, `idle` on cancellation. It is published only after the run outcome
  is final (required observers stopped, manifest published), so a late failure
  reports `error` rather than a premature `done`. The companion mirrors that
  state.
- A host that clears the terminal with `state=clear` (no `id`) clears the root
  and companion records together, for example when a held run is dismissed.
  The `spaces-agents` mod reads the record its own terminal's root points at,
  showing the workflow name, run ID, and current node, and falls back to the
  safe `--tasks` name and then `Crewplane` when no record is verified. Its
  contract is documented in that mod's `README.md` under
  `~/.config/herdr-app/mods/spaces-agents/`.
- Visibility is bounded by herdr's lifecycle rules: a normal program exit
  closes the pane and removes its entry. A crewplane run appears as "Working"
  while it runs, but does not linger as a "Done" entry the way long-lived agents
  (Claude Code, Codex) do, unless the run is
  [held open](#holding-the-run-open). Status seen only through OSC 7501 can
  still remain latched on the shell that replaces the program (herdr-core issue
  #57), which is what the hold's `state=clear` prevents. The authoritative
  post-run record is the exit code plus
  `.crewplane/execution-results/<run-key>/manifests/run.json`.

Crewplane never reports protocol `blocked` (its node-blocked event is an
internal dependency wait), no `progress` percentage, and no `msg` on `error`
(base64 is an encoding, not redaction; provider error text may be sensitive).
Failures before the reporter exists (config/context loading errors, resume-lock
contention) report no status — use the process exit code.

## Holding the run open

herdr removes the entry when the program exits, so a finished run normally
disappears with its pane. Ask `crewplane run` to hold the terminal instead:

```sh
crewplane run --no-live --hold -t .crewplane/workflows/my.task.md
```

Set `settings.hold_after_run: true` in `.crewplane/config.yml` to make holding
the project default. The per-run flag wins: `--no-hold` disables a default-on
project and `--hold` enables a default-off one.

While held, the terminal keeps the run's real outcome — `done` or `error`, never
the protocol `blocked` state, which is reserved for "cannot proceed until the
user acts". Anything pressed dismisses the hold, and Ctrl+C counts as a
dismissal rather than an interrupt: crewplane writes `state=clear`, then exits
with the run's normal exit code, so a successful run still exits 0 and a failed
run still exits 1.

Only a run that finalized is held: success, or an execution failure that
reports `error`. Finalizing is what the hold needs, not a particular published
state, so a run whose status integration is `none` holds too. A duplicate-context
skip publishes no status of its own, so it never holds. Lock contention,
preflight failures, and user cancellation never hold: their diagnostics print
after a hold would begin, and cancellation means someone is already at the
terminal. A workflow failure prints its diagnostic before the hold starts
waiting. A node or scheduling failure reads `✗ Workflow '<name>' failed:` with
each failed and blocked node; a postcondition failure instead reports
`Workflow postconditions failed:` with the first cleanup or descriptor error.

The clear on dismissal is deliberate. herdr-core can otherwise latch the run's
`done` state onto the recovery shell that replaces the program (herdr-core issue
#57). `state=clear` is written even when the configured status integration is
`none`, because a hold always releases the terminal it kept. The write is
best-effort: a terminal that rejects it warns, and the run keeps its own outcome
and exit code. herdr also shows a `done` entry as `idle` once it has been
viewed, so a held pane that has already been read no longer claims fresh
attention.

Holding needs an interactive terminal and is off by default: crewplane holds
only when the console and, on POSIX, stdin are both terminals. If stdin is
unavailable or reading it fails while stdout is a terminal, crewplane writes
`state=clear` and exits without waiting, so nothing is left latched and the run
keeps its exit code. Non-terminal output never blocks and stays byte-identical,
so piped runs are safe; a CI job or script that allocates a tty can block, so
keep the flag and the config off in automation.
Under `hc run` the terminal is real, so a held run keeps its terminal alive
until someone presses a key: leave holding off for automated herdr terminals,
and never rely on `hc wait` returning while a hold is pending.

## Verifying

Two levels:

1. **Without herdr** — prove the bytes are emitted. Run under a pseudo-terminal
   so crewplane's console sees a tty (sequences are skipped when stdout is not
   a terminal):

   ```sh
   script -qec "crewplane run --no-live -t .crewplane/workflows/my.task.md" /dev/null \
     | grep -ao "7501;[^\x1b]*"
   ```

   Expect repeated `state=working:app=crewplane:msg=<base64>` root lines with
   `title=crewplane/<run-id>`, one companion line per report
   (`id=crewplane/<run-id>:app=crewplane:title=...`, with `msg` only while a
   node runs), and one final `state=done` (or `error`/`idle`) root plus
   companion. The first line is the exact-key untitled idle replacement,
   `state=idle:id=crewplane/<run-id>:app=crewplane`.
   Piping the run directly (no `script` wrapper) must yield zero matches —
   piped output stays byte-identical.

2. **With herdr** — run the workflow in a herdr tab and watch
   `hc events` (or `hc get review`) for a `status` fact with `state=working`
   and `app=crewplane` on that terminal, plus a `records` entry keyed
   `crewplane/<run-id>`. If events show the status but the sidebar does not,
   the mod's space scope is hiding it; if no event arrives, the run is not on a
   herdr-managed pty or the project config does not enable the integration.

## Failure semantics

Status reporting is best-effort and observational: a failed write disables
reporting for the rest of the run with a warning, execution and artifacts are
unaffected, and toggling the integration does not change the effective
execution signature, so saved results stay reusable.

## Alternative: the herdr launcher wrapper

A pre-existing launcher registered in herdr-app
(`~/.config/herdr-app/programs.json` → `~/.config/herdr-app/bin/crewplane-herdr`)
wraps any `crewplane` on PATH and emits its own coarse `working` / `done` /
`error` sequences. It works without the config change above but knows nothing
about workflow nodes — no per-node `msg` labels — and it runs the PATH
install, so it only reports lifecycle states. Prefer the native integration
documented here once your crewplane build includes it.

For builds with the native integration, `crewplane run --hold` supersedes that
wrapper's remaining benefit of keeping the finished run on screen. Removing the
wrapper's entry from `programs.json` is a step on your machine, not part of this
repository.

## Source references

- Emission: `src/crewplane/adapters/status/osc7501.py` (root pointer and
  companion wire format, exact-key idle replacement, run-id fit check,
  workflow basename, tty gating, dedupe, write-failure disable, `state=clear`
  on hold dismissal)
- Run-path publication: `src/crewplane/cli/run/observability.py`
  (`publish_terminal_status`, after manifest finalization)
- Hold after the run: `src/crewplane/cli/run/hold.py`, wired in
  `src/crewplane/cli/app.py`
- Config wiring and default: `src/crewplane/core/config.py`,
  `src/crewplane/bootstrap/container.py`

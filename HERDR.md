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
- At the end, exactly one terminal state: `done` on success, `error` on
  failure, `idle` on cancellation. It is published only after the run outcome
  is final (required observers stopped, manifest published), so a late failure
  reports `error` rather than a premature `done`.
- Visibility is bounded by herdr's lifecycle rules: status facts are reset when
  the program exits and the shell returns to the foreground. A crewplane run
  appears as "Working" while it runs, but does not linger as a "Done" entry the
  way long-lived agents (Claude Code, Codex) do. The authoritative post-run
  record is the exit code plus
  `.crewplane/execution-results/<run-key>/manifests/run.json`.

Crewplane never reports protocol `blocked` (its node-blocked event is an
internal dependency wait), no `progress` percentage, and no `msg` on `error`
(base64 is an encoding, not redaction; provider error text may be sensitive).
Failures before the reporter exists (config/context loading errors, resume-lock
contention) report no status — use the process exit code.

## Verifying

Two levels:

1. **Without herdr** — prove the bytes are emitted. Run under a pseudo-terminal
   so crewplane's console sees a tty (sequences are skipped when stdout is not
   a terminal):

   ```sh
   script -qec "crewplane run --no-live -t .crewplane/workflows/my.task.md" /dev/null \
     | grep -ao "7501;[^\x1b]*"
   ```

   Expect repeated `state=working:app=crewplane:msg=<base64>` lines and one
   final `state=done` (or `error`/`idle`). Piping the run directly (no `script`
   wrapper) must yield zero matches — piped output stays byte-identical.

2. **With herdr** — run the workflow in a herdr tab and watch
   `hc events` (or `hc get review`) for a `status` fact with `state=working`
   and `app=crewplane` on that terminal. If events show the status but the
   sidebar does not, the mod's space scope is hiding it; if no event arrives,
   the run is not on a herdr-managed pty or the project config does not enable
   the integration.

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

## Source references

- Emission: `src/crewplane/adapters/status/osc7501.py` (wire format, tty
  gating, dedupe, write-failure disable)
- Run-path publication: `src/crewplane/cli/run/observability.py`
  (`publish_terminal_status`, after manifest finalization)
- Config wiring and default: `src/crewplane/core/config.py`,
  `src/crewplane/bootstrap/container.py`

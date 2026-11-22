# Crewplane ↔ herdr-core / herdr-app Integration

Analysis of wiring crewplane into [herdr-core](https://github.com/herdrdev/herdr-core)
(the `hc` terminal runtime) and [herdr-app](https://github.com/herdrdev/herdr-app)
(the desktop window layer). Written against the `xdblue-tech/crewplane` fork on
`master`; herdr-core/herdr-app sources were not available locally — findings rest
on their published docs, the `hc` skill manual, generated mod typings, and
consumer mods in local checkouts. Operational wiring instructions for the
tier-2 status integration live in [HERDR.md](./HERDR.md).

## First usable workflow (no code changes)

Drive today's crewplane from herdr-core as a background terminal, then collect
results from the on-disk artifacts:

```sh
hc run --bg --name my-workflow --cwd /path/to/repo \
    crewplane run -t .crewplane/workflows/my.task.md --no-live

hc wait my-workflow
hc file read /path/to/repo/.crewplane/execution-results/<run-key>/manifests/run.json
```

Supporting pieces:

- Register a reusable role with a template at
  `~/.config/herdr-core/templates/crewplane.md` (frontmatter `command: crewplane`,
  `ready: quiet`). Templates wrap a shell command; they install nothing.
- Always pass `--no-live`: crewplane's own tmux dashboard conflicts with herdr's
  terminal ownership, and execution is fully functional without it.
- Treat exit codes as the process contract: `0` success, `130` cancelled,
  `1` failure (including run-lock contention). Do not parse stdout for semantics;
  `manifests/run.json` is the authoritative state.

## Integration contract on the herdr side

herdr-core has no agent whitelist and no plugin registration API for external
tools. The contract is process-level and open to any program:

| Surface | Mechanism |
| --- | --- |
| Launch | `hc run [--bg] [--name N] [--cwd D] [--on M] <cmd\|template> [task]`; `--bg` returns immediately |
| One-shot | `hc exec [--timeout S]` — 60 s default, 600 s max; too short for real workflows, prefer `hc run --bg` |
| Steering | `hc send/keys/wait/read/get/ls/kill/interrupt/rm` |
| Events | `hc events` — live JSON fact stream |
| Files | `hc file read|write|edit [--on M]`, including remote machines |
| Status | OSC 7501 "Program Status Protocol": a program prints `printf '\e]7501;state=blocked:app=my-agent\e\\'` and herdr-core tracks it natively ("no screen scraping, no list of known agents") |
| Reporting | Parent/child terminals; children report via `hc send $HC_TERMINAL_ID`; `hc wait <t>` blocks until a terminal stops |

herdr-app adds `herdr-app do <command>` (space/tab/pane control) and
trusted-JavaScript **mods** under `~/.config/herdr-app/mods/<name>/`
(`main.js`, `mod.json`, hot-reloaded). Mods shell out to external CLIs through
the `herdr` module bindings (`exec()`, `hc()`, `run(name, args)`); existing mods
in `herdr-extension`/`herdr-mods` follow this pattern for `git`, `rg`, `gh`, and
`hc` itself.

Sources: <https://herdr.dev/core/>, herdr-core
[commands](https://github.com/herdrdev/herdr-core/blob/master/docs/commands.md)
and [templates](https://github.com/herdrdev/herdr-core/blob/master/docs/templates.md)
docs, [herdr-app SKILL.md](https://github.com/herdrdev/herdr-app/blob/master/src/SKILL.md).

## What the crewplane codebase implies

- **Clean process contract.** `crewplane run` is a foreground blocking command
  (`src/crewplane/cli/app.py:342`) with meaningful exit codes. Machine-readable
  state lives in `.crewplane/execution-results/<run-key>/manifests/run.json`;
  run inventory is scannable via `crewplane runs list` or the internal
  `scan_run_history` (`src/crewplane/artifacts/run_history.py:34-77`).
- **CWD/config resolution matters.** Project root comes from the nearest-ancestor
  `.crewplane/config.yml` (`src/crewplane/cli/paths.py:68-107`); launch with
  `--cwd <repo>` or pass `--config` explicitly.
- **One run per workflow context.** A live resume lock makes a concurrent second
  run exit 1 with `ResumeLockError` (`src/crewplane/cli/app.py:356-365`); the
  herdr side must not blindly relaunch or delete locks.
- **No public SDK.** Only the `crewplane` console script is a supported entry
  point (`pyproject.toml:37-38`); in-process embedding would track internal APIs.
- **Streaming is limited.** `crewplane runs logs --node <id> --follow` is the
  only follow primitive; `runs list` renders a Rich table, not JSON.

## Integration tiers and what each costs

| Tier | What | herdr patching | crewplane changes | Scope |
| --- | --- | --- | --- | --- |
| 1 | Template + `hc run --bg` launch, artifact collection | None | None | Config only |
| 2 | OSC 7501 status emission (`state=working\|done\|error\|idle`) at run/node boundaries | None — herdr-core already listens | Done — see below | M |
| 3 | herdr-app mod rendering results / launching runs | None — mods are the sanctioned surface | Optional (JSON output or read manifests from disk) | M |

Tier 2 is the "properly wired" version: herdr's `hc wait`, `hc events`, and the
herdr-app agent menu show real workflow lifecycle instead of a black box.

**Implemented** as a fourth integration kind, `status`, beside the tmux UI
integration rather than inside it — under herdr you run with the tmux UI off and
status emission on. No new dependency; emitting an OSC sequence prints an escape
string to stdout.

```yaml
settings:
  integrations:
    status:
      implementation: "osc7501"
```

What crewplane emits (`src/crewplane/adapters/status/osc7501.py`):

- `state=working:app=crewplane:msg=<base64 workflow name or running node id>`
  while the run executes;
- exactly one terminal state per run: `done` on success, `error` on failure,
  `idle` on cancellation.

Scope boundary for v1:

- No `state=blocked`. Crewplane's `NODE_BLOCKED` event is an internal dependency
  wait, not a host-facing permission/question/auth block.
- No `progress` percentage, and no `msg` on `error`: base64 is an encoding, not
  redaction, and provider error text may be sensitive. Exit codes and
  `manifests/run.json` carry failure detail.
- No status for failures before the reporter exists (config/context loading,
  resume-lock contention). Use the process contract instead.
- Sequences are written only when stdout is an interactive terminal, so piped
  and CI output stays byte-identical to earlier releases. A failed write
  disables reporting for the rest of the run without affecting execution.

Status is observational: toggling it does not change the effective execution
signature, so saved results stay reusable.

Holding a finished run on screen is separate from that port. `crewplane run
--hold` and `settings.hold_after_run` wait for a keypress in the CLI layer, after
the terminal state is published and the run lock is released, then emit
`state=clear` and exit with the run's normal code. The status observer stays
observational, and the scope boundary above is unchanged: `blocked` is still
never emitted, and the hold reports the run's real `done` or `error` outcome. See
[HERDR.md](./HERDR.md#holding-the-run-open).

Tier 3 (mod) can consume `manifests/run.json` directly from disk, avoiding any
crewplane JSON-output work; a `runs list --json` flag is only needed if the mod
prefers the CLI surface. Known mod constraints from the generated typings
(`herdr-extension/mods/blueprint/gpui-kit.d.ts:22190-22400`): `exec()` caps at
30 s / 8 MiB (launch-and-poll via `hc --json`, not held subprocesses), and
whether mods can host resident background services is unverified.

## Gaps and assumptions

- Tier 2 implements the OSC 7501 fields `state`, `app`, and base64 `msg`. The
  `kind` (blocked subtypes) and `progress` fields stay unused in v1, so host
  support for them is unverified against a real herdr-core.
- herdr-core and herdr-app source unavailable locally; internals rest on docs,
  the `hc` skill manual, generated typings, and consumer mods.
- `herdr` (older TUI multiplexer) and herdr-core (`hc`) are distinct CLIs;
  integration targets `hc`, never `herdr`.
- The mapping of crewplane onto herdr's contract is an inference from
  documented contracts, not an exercised prototype.

## Recommended move

1. Prove tier 1 end-to-end: template file, one real workflow launched with
   `hc run --bg`, validate `--cwd`, exit codes, and collection via
   `hc file read`.
2. Verify tier 2 against a real herdr-core (`hc run --bg` + `hc events`) with
   `settings.integrations.status.implementation: "osc7501"`.
3. Defer tier 3 until a results view in the herdr-app window is actually wanted.

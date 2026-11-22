# Integrations Reference

Use this page when you need the adapter contract behind
`settings.integrations`: choosing the built-in `mock` or `cli` invoker,
disabling the live UI, tuning artifact options, or selecting an external
adapter. Generated projects start with the `mock` invoker; switch to `cli` only
when Crewplane should start real provider commands.

For task-oriented setup, use [Provider setup](../getting-started/provider-setup.md),
[Mock validation](../guides/mock-validation.md),
[Watch Runs Live and Inspect Results](../guides/watch-runs-live-and-inspect-results.md), and
[Inspecting Run Records](../guides/inspecting-artifacts.md).

| Need | Integration |
| --- | --- |
| Real provider CLI execution | `cli` invoker |
| Provider-free testing | `mock` invoker |
| Live dashboard | `tmux` UI |
| No live UI | `none` UI |
| Local run records | `filesystem` artifacts |
| Status reporting to a terminal host | `osc7501` status |

These aliases are valid values for
`settings.integrations.<kind>.implementation`:

| Kind | Aliases |
| --- | --- |
| `invoker` | `cli`, `mock` |
| `ui` | `tmux`, `none` |
| `artifacts` | `filesystem` |
| `status` | `osc7501`, `none` |

Aliases resolve through the internal registry. An external adapter can also be
selected with `package.module:ClassName` or `package.module.ClassName`.

## Invokers

### `cli`

The `cli` invoker runs configured provider commands from `agents`. It owns
prompt transport, retries, quota detection, timeout handling, output extraction,
and provider usage parsing.

Use `options.providers` under `settings.integrations.invoker` to add installed
provider plugins. Omit this map or leave it empty to use only built-in providers.
`providers` is the only supported option for the `cli` invoker.

```yaml
options:
  providers:
    ollama:
      implementation: crewplane_ollama:OLLAMA
      distribution: crewplane-ollama
```

Each entry requires these two fields:

| Field | Meaning |
| --- | --- |
| `implementation` | The plugin's exported object, written as `module:object`. |
| `distribution` | The installed Python package name, such as `crewplane-ollama`. |

Use the values supplied by the plugin. Both must be nonempty strings without
leading or trailing spaces; additional fields are rejected. Crewplane adds the
installed package `version` to its saved records. Do not set `version` yourself.

The entry name is the `provider_kind` used in agent profiles. It must match
`[a-z][a-z0-9_-]*` and cannot reuse a built-in name: `claude`, `codex`, `copilot`,
`gemini`, `kilo`, `pi`, `deepseek`, `opencode`, or `generic`. Every agent profile
must name a built-in or registered provider, even if the workflow does not use it.

The official Ollama plugin is installed with Crewplane. Install additional
plugins in Crewplane's Python environment before configuring them.
Plugins load through explicit registration or an explicitly selected official
onboarding recipe. Onboarding and workflow runs do not install packages or scan
for arbitrary plugins. Use trusted packages, since their Python code runs with
Crewplane's permissions.

Validation fails if a package is missing, its object cannot be loaded, or the
object is not a valid capability with a matching `provider_kind`. Changing a
registration or installed package version can prevent reuse of saved results,
even when the workflow does not use that plugin.

See the [Ollama plugin setup](../../plugins/crewplane-ollama/docs/setup.md)
for a complete configuration example and the [plugin reference](../../plugins/crewplane-ollama/docs/reference.md)
for its execution contract.

### Authoring a CLI capability

Import the public types and helpers from `crewplane.adapters.invokers.cli_invoker`:
`CliProviderCapability`, `CliInvocationRequest`, `CliCommand`,
`extract_strict_stdout`, `run_command_once`, and `unconfirmed_process_cleanup`.
Result, environment, output, and classification types are available from
`crewplane.architecture.contracts`.

Export a `CliProviderCapability` instance whose `provider_kind` matches the
registration name. Provide request validation and command-building callbacks.
Choose how to read answers and usage, recognize failures and usage limits,
display logs, retry failures, and handle idle timeouts. Use the model supplied
in `CliInvocationRequest.model`; ignore `model_arg`, which is for `generic` only.
Declare compatible Crewplane versions in your package dependencies.

Use `CliInvocationRequest.environment` for command resolution and availability
probes. It contains the inherited environment with the agent's configured
[overrides](configuration.md#agentsname) applied.

#### Availability checks

`check_availability` is optional and defaults to `None`. When provided, it must
have type `Callable[[CliInvocationRequest], Awaitable[None]]`. Return `None` on
success, or raise `ValueError` when the provider is unavailable. Returning another
value is a contract error.

Crewplane checks the request and executable before calling this function during
validation and normal runs. Dry-run and loading registrations do not call it.

#### Onboarding discovery

The optional `CliProviderCapability.onboarding` field accepts a
`CliProviderOnboarding` with two async callbacks:

| Callback | Result |
| --- | --- |
| `discover(request)` | `CliProviderDiscovery`: command tuple, endpoint choices, and diagnostics. Each `CliDiscoveredEndpoint` has a label and environment overrides. |
| `list_models(request, endpoint)` | `CliDiscoveredModels`: model names and diagnostics. |

`CliDiscoveryRequest` supplies the project root, environment, executable lookup,
and a positive, finite `timeout_seconds` budget (default `15`). Callbacks own
read-only discovery within that budget and cleanup on cancellation. They must
not install software, start servers, or make inference requests. Empty command,
endpoint, or model collections prevent setup from being applied.

Only explicitly selected official recipes (currently Ollama) use discovery;
registration, validation, dry-run, and execution do not invoke it. The CLI owns
selection and configuration writes.

#### Files, processes, and cancellation

Callbacks must support concurrent requests without storing request data in
shared mutable globals. Importing the plugin must be free of side effects,
and request validation must not create invocation files.

Command builders must clean up temporary files if they fail before returning a
complete command. Once those files are passed to Crewplane with the command,
Crewplane cleans them up after success, failure, or cancellation. Output readers,
classifiers, and usage decoders must leave captured-file cleanup to the owner.

An availability callback owns the captured output files returned by
`run_command_once`. Delete both captures in `finally` and let deletion errors
propagate. The helper owns the files until it returns. Use
`ChildProcessEnvironment` for subprocess environment settings. In availability
checks, pass `None` for `log_file`, `log_header`, and `invocation_context`.

Wait for owned subprocess work and bounded cleanup before returning or
propagating cancellation. Preserve exception chains and notes. Before converting
an exception to `ValueError`, call `unconfirmed_process_cleanup(error)`. If it
returns a cleanup error, re-raise the original exception.

See [ADR 0019](../architecture/adr/0019-explicit-plugin-system.md) for the design
and lifecycle guarantees.

### `mock`

The `mock` invoker provides deterministic provider-free execution. It writes
normal run records without starting provider CLIs.

Options:

- `delay_seconds`
- `observation_delay_seconds`
- `output_mode`
- `output_dir`
- `strict_file_mode`
- `seed`
- `fail_when`

`output_mode` is one of `lorem`, `echo`, and `file`. `fail_when[]` selectors
support `node_id`, `task_id`, `provider`, `role`, `audit_round_num`, and
`round_num`.

## UI

### `tmux`

The `tmux` UI opens a compact live dashboard when output is a terminal,
`--no-live` is not set, tmux is available, and provider CLI output logging is
enabled. Missing tmux degrades to a warning and normal execution.

Options:

- `auto_close_session`
- `tmux_executable`
- `quiet_after_seconds`
- `log_tail_lines`

### `none`

The `none` UI disables live observers.

Options: none.

## Status

Status integrations report workflow lifecycle to the terminal that hosts the
run. They are independent of the `ui` integration: reporting works with
`--no-live`, in a plain terminal, and under a host that owns the terminal.

### `osc7501`

The `osc7501` status integration writes Program Status Protocol sequences to
stdout for hosts such as [herdr-core](https://github.com/herdrdev/herdr-core),
which track them without screen scraping:

```
ESC ] 7501 ; state=working : app=crewplane : msg=<base64> ESC \
```

| Field | Meaning |
| --- | --- |
| `state` | `working` during the run; `done`, `error`, or `idle` once at the end. |
| `app` | Always `crewplane`. |
| `msg` | Base64-encoded one-line activity label: the workflow name, or the running node id. Absent on terminal states. |

A successful run ends with `state=done`, a failed run with `state=error`, and a
cancelled run with `state=idle`. `working` is deduplicated as a whole sequence,
so it repeats whenever the activity label changes; the terminal state is reported
at most once per run. The terminal state is written only after the outcome is
final, once required observers have stopped and manifest publication has
resolved, so a late failure reports `error` rather than a premature `done`.
Crewplane never reports the protocol `blocked` state, because its own node-blocked
event is an internal dependency wait, and it reports no progress percentage.

Options: none.

Sequences are written only when the injected console is a terminal. Piped and CI
output stays byte-identical, so nothing changes unless a host terminal is
present. A failed write disables reporting for the rest of the run with a
warning; execution, artifacts, and the event log are unaffected. A failure while
writing the terminal state is warn-only for the same reason.

Status covers runs that reach observed execution. Failures before the reporter
is created, such as config or context loading errors and run-lock contention,
report no status; use the process exit code and the run manifest instead.

#### Holding the terminal open

A host such as herdr closes the pane when the program in it exits, which removes
the entry the terminal state just set. `crewplane run --hold` keeps the terminal
open after the run finishes until any key is pressed; `--no-hold` turns holding
off for one run. `settings.hold_after_run: true` makes holding the project
default. The per-run flag wins over the config key, and both default to off.

The held state is the run's real outcome: `done` on success, `error` on an
execution failure. Crewplane never holds a run as `blocked`, because that state
means "cannot proceed until the user acts". A run is held once it finalized, not
because of the state it published, so a run whose status integration is `none`
also holds. A duplicate-context skip, lock contention, preflight failures, and
user cancellation never hold. A workflow failure prints its diagnostic before
the hold starts waiting: a node or scheduling failure reads
`✗ Workflow '<name>' failed:` with each failed and blocked node, while a
postcondition failure reports `Workflow postconditions failed:` with the first
cleanup or descriptor error. Dismissing the hold writes `state=clear` — no `app`
and no `msg`, since the terminal no longer belongs to a tracked program — and
then exits with the run's normal exit code; Ctrl+C is a dismissal too, so the
run keeps its own exit code instead of becoming an interrupt.

`state=clear` is written whenever a terminal hold is dismissed, including when
the configured status integration is `none`, because a held terminal is always
released. The write is best-effort: if the terminal rejects it, crewplane warns
and the run outcome and exit code still win. The clear keeps hosts from latching
`done` onto the shell that replaces crewplane. Holding changes no artifacts,
manifests, locks, or exit codes, and it runs after the terminal state is
published and the run lock is released.

Holding, like the sequences themselves, needs an interactive terminal: a
non-terminal console never blocks and stays byte-identical, and on POSIX the
hold also requires a terminal on stdin. If stdin is unavailable or reading it
fails while stdout is a terminal, crewplane writes `state=clear` and exits
without waiting, so nothing is left latched and the run keeps its exit code. A CI
job or script that allocates a tty can block, so keep the flag and the config
off in automation. Under a host that allocates a terminal for the run, such as
`hc run`, a held run keeps waiting until someone presses a key; leave holding
off for automated terminals.

### `none`

The `none` status integration disables status reporting. It is the default.

Options: none.

## Artifacts

### `filesystem`

The `filesystem` artifact backend stores stages, results, logs, manifests,
preflight bundles, locks, and workspace state under `.crewplane/`.

Options:

- `log_cli_output`

Template authorization is configured separately through the core-owned
`settings.file_access.allowed_template_paths` policy.

Real runs currently require the built-in `filesystem` adapter. External
artifact adapters can be used only with `crewplane validate` and
`crewplane run --dry-run`.

Artifact adapters must provide `create_terminal_history_reader()` so preflight
can resolve terminal results through the selected storage integration. The
factory receives the same canonical artifact options used for runtime component
construction. The loader rejects adapters that omit this factory.

## Extension Contract

Every adapter canonicalization result must have an `option_scopes` key set that
exactly matches its canonical `options` key set. Scope each option as
`execution`, `artifact`, `observer`, or `validation`; unscoped and unknown
entries fail composition. Scopes remain attached to top-level option keys even
when their values are nested JSON.

Sensitive option declarations use RFC 6901 JSON Pointers, such as `/api_token`
or `/credentials/0/value`. Crewplane redacts sensitive values at any depth. All
option and capability values must be finite JSON-compatible data.

Observers implement the architecture `Observer` lifecycle contract and expose
one immutable `ObserverCapabilities` value. Runtime observers receive the
architecture `ExecutionEvent`, `DashboardSnapshot`, and `RunResult` contracts.
Composition validates `capabilities`, `stop_requested`, `start`, `on_snapshot`,
and `stop` before a run begins. Status integrations return an observer from
`create_reporter()`; those observers are best effort, receive snapshots
synchronously, and are carried separately from UI observers. Composition rejects
a status observer that declares `required=True`, defers snapshot delivery, or
requests run cancellation, and it publishes the terminal status through a
separate method after the run outcome is final. The run path also pins that
contract for the observer's whole lifecycle, so a status observer cannot request
cancellation after it starts. Persistent required observers and best-effort UI
observers express their delivery and cleanup behavior through that capability
value rather than dynamic attributes.

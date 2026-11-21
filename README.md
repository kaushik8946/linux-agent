# linux-agent

A local Python CLI agent for Linux. You describe an outcome in plain language.
An OpenAI-compatible model proposes the commands or typed operations it needs.
Local code decides what may run: it validates each request, classifies risk,
asks you to approve anything that changes the system, runs it without a shell,
checks the result, and helps you save a Markdown report.

**The model proposes; local code decides.** Nothing that changes the system
runs without your explicit approval, and the model cannot approve on your
behalf.

## Setup

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e ".[dev]"
    cp .env.example .env && chmod 600 .env   # then fill in real values

Requires Python 3.11 or newer.

### Configuration

Settings come from the environment or a `.env` file. Real environment
variables take precedence over `.env`.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | yes | | Gateway API key (never logged or printed) |
| `OPENAI_BASE_URL` | yes | | OpenAI-compatible gateway URL, `http(s)://...` |
| `OPENAI_MODEL` | yes | | A tool-calling-capable model your gateway allows |
| `AGENT_REQUEST_TIMEOUT` | no | `60` | Seconds per gateway request (1-600) |
| `AGENT_MAX_TOOL_ROUNDS` | no | `6` | Model requests per question (1-50) |
| `AGENT_REPORT_DIR` | no | `reports` | Where `/report` writes (created on first use) |
| `AGENT_LOG_DIR` | no | `logs` | Where the private task journal is kept |
| `AGENT_WORKSPACE_ROOT` | no | `~/projects` | The only directory new projects are created in |

Configuration problems are reported together, without echoing secret values.

## Usage

    linux-agent                 # start the interactive session
    linux-agent --help
    linux-agent --version
    linux-agent --inspect       # show detected host facts (no gateway needed)
    linux-agent --check-gateway # verify text, tool calling and plan proposals
    linux-agent --preview-plan plan.json   # validate and classify a plan; runs nothing

`python main.py` is equivalent to `linux-agent`.

### Interactive session

Type a request, for example:

    linux-agent> Check why my laptop is running slowly.
    linux-agent> Is cron running? Start it if it is not.
    linux-agent> Set EDITOR to vim for my future shells.

You see each command plan or operation the model proposes, its risk and what it
would change. Read-only steps run automatically. Anything else waits for your
answer, and the default is No. After a change you see its verification result,
then the model's answer, which separates observations from hypotheses.

| Command | Purpose |
| --- | --- |
| `/help` | Show help |
| `/inspect` | Show detected host and capability information |
| `/history` | List this session's requests (session-only) |
| `/report` | Save a Markdown report of the last request |
| `/exit` | Leave (Ctrl+D also works) |

Ctrl+C at the prompt is ignored. Ctrl+C during a request cancels it, stops any
running command, and keeps the session usable.

### Reports

`/report` writes `report-<time>-<session>-<n>.md` to the report directory with
the request, session ID and timestamp, host facts, tools and commands used,
short redacted output excerpts, warnings and missing capabilities, a facts
versus hypotheses note, and the final result. Reports never overwrite each
other, are written atomically (an interrupted write leaves nothing partial),
and never contain secrets or full command output.

## What the agent can do

### Read-only (runs without approval)

- **Host context:** `get_host_summary`, `get_command_environment`.
- **Any diagnostic command** the model proposes, if the policy positively
  recognises it as read-only (for example `uname`, `free`, `df`, `ps`,
  `systemctl status`). Commands it cannot classify are blocked.
- **Packages and services:** `query_package`, `service_status`,
  `list_dev_tools`.
- **Environment variables:** `list_managed_env`, `get_managed_env`.
- **Docker:** `docker_status`, `list_containers`, `list_images`,
  `inspect_container` (environment variables are never returned), and
  `container_logs` (redacted and bounded).
- **Network:** `dns_lookup`, `tcp_check` (opens and closes a connection, sends
  no data), `resolver_config`, `default_route`.
- **Research:** `fetch_web_page` (allow-listed documentation sites over HTTPS
  only) and `compare_install_methods`.
- **Provisioning previews:** `list_profiles`, `plan_profile`, `check_profile`.

### Changes (each needs your approval)

Every change shows the exact command or file it touches, the baseline, the
expected end state, what it affects, how it will be verified and how to undo
it. After it runs, the agent re-checks the real system and reports the result.
An exit code of zero is never treated as success on its own.

| Operation | What it does | Verified by |
| --- | --- | --- |
| `install_package` | Installs one named package with apt or dnf | Package state and installed version |
| `start_service` | Starts one systemd service | Active state, and that it stays up |
| `set_env_var`, `unset_env_var` | Edits the agent's own environment file | File contents, and a fresh process that sources the file |
| `start_container`, `stop_container`, `restart_container` | Changes one container's state | Container state, and that it stays up |
| `pull_image` | Downloads one image | Image is present locally |
| `init_project` | Creates a fixed template project inside the workspace | Every template file exists, nothing extra |
| `install_dev_tool` | Installs a tool from a fixed list (git, curl, jq, make, python3-venv, shellcheck, sqlite3, tmux) | Package installed and executable on PATH |

Provisioning profiles (`python-dev`, `shell-tools`, `minimal`) are not a
separate kind of change. Each step is one of the operations above, approved and
verified on its own.

### Deliberately not offered

- Package removal or purge; service stop, disable or mask.
- Removing containers, images, networks or volumes, and pruning.
- Firewall or SSH changes, disk or filesystem operations.
- Repository or distribution-upgrade changes.
- Arbitrary package names through `install_dev_tool` (use `install_package`).
- Passing a sudo password or configuring passwordless sudo.

If you want to undo a change, the operation's recovery note gives the command
to run yourself.

## How it works

1. **Discover** the host: distribution, package and service managers,
   privileges, container indicators, available utilities. Unknown values are
   reported as unknown, never guessed.
2. **Ask the model** with the host facts, the typed tools and operations, and
   the generic `execute_command_plan` tool.
3. **Validate** each request locally: strict schema, command policy, executable
   resolution, paths, environment, limits, and the operation's own checks.
4. **Approve** anything that changes the system. Approval is bound to the exact
   operation (a fingerprint of its tool, arguments and commands) and expires
   after 5 minutes. Changing the operation invalidates it.
5. **Run** through the executor, which re-checks the operation immediately
   before running, journals every step, and never uses a shell.
6. **Verify** the real end state, stop on failure, collect safe diagnostics and
   propose a recovery that needs its own approval.
7. **Stop** at the tool-round, command-round or time limit, or when the model
   answers.

A command plan has a `mode`: `batch` runs all steps and returns every result;
`stepwise` runs one step and reports the rest as pending, so the model picks
the next command from what it observed.

## Safety model

- **No shell.** Commands run with `shell=False` from an argument array.
  `$()`, `|`, `;`, `>` and `*` stay literal text. Shells and wrappers (`bash`,
  `env`, `xargs`, `ssh`, ...) are rejected.
- **Read-only by default.** A command runs automatically only if the policy
  positively recognises it as read-only. Modifying, privileged, destructive,
  credential-touching and unknown commands are blocked, as are dangerous flags
  on safe tools (`find -exec`, `tail -f`, `ps e`). If any step in a plan is
  blocked, nothing in that plan runs.
- **Privilege is blocked in command plans.** `sudo` and similar wrappers are
  refused there. Elevation happens only inside the dedicated adapters, as a
  fixed `sudo -- <absolute path> ...` command. The agent never sees your
  password.
- **Model approval is ignored.** An `approved` flag or any other approval field
  in model output is rejected. Approval comes only from your answer at the
  prompt.
- **Trusted executables.** Commands resolve only from root-owned,
  non-group/world-writable directories on `PATH`. Ambiguous names are rejected.
- **Bounded.** Timeout, output size, working directory and environment are
  enforced by the application whatever the plan asks. The child process gets a
  minimal environment, with no inherited secrets. A timeout or cancel kills
  the whole process group.
- **Redacted.** Secrets are removed from command output, model-bound results,
  terminal display, journal and reports. The journal is private (mode 0600),
  append-only and fsynced.
- **Re-checked before running.** A change is re-prepared just before it runs,
  so a state change since approval is noticed. A completed operation is not
  repeated silently.
- **Evidence, not instructions.** Command output, web pages and container logs
  are marked untrusted. The system prompt tells the model to ignore
  instructions found in them.
- **Safe display.** Terminal escape sequences in model or command text are
  stripped, and Markdown code fences in reports cannot be closed early.
- **Network limits.** Web fetches are HTTPS on port 443, from an allow-listed
  set of documentation hosts, to public addresses only, with the connection
  pinned to the address that was checked and redirects re-validated.

## Project layout

    agent/        gateway client, command plans and policy, approval, executor,
                  verifier, operations, task journal, orchestrator, prompts
    cli/          argument handling and the interactive session
    hostplatform/ host discovery, the controlled command runner, privileged
                  command helper, package adapters (apt, dnf), service adapter
                  (systemd), environment store, Docker client, workspace,
                  network diagnostics, guarded web fetch
    tools/        typed tools and operations: host context, packages and
                  services, environment variables, Docker, workspace, network,
                  development tools, research, provisioning, and the adapter
                  registration in tools/adapters.py
    tests/        unit and integration tests (fakes only; no network or real
                  system changes)
    docs/         architecture overview and build plan

`hostplatform/` is named to avoid shadowing Python's standard `platform`
module.

## Development

    pytest -q
    ruff check . && ruff format --check .
    mypy agent hostplatform tools cli config.py main.py tests

Tests use fake gateways, scripted process runners and fixture host trees, so
they need no network, no credentials and no root. Real system changes should be
tried only in a disposable VM.

## Known limitations

- **Adapters are tested with fakes.** The apt and systemd adapters have been
  exercised on one Ubuntu host with read-only checks and one declined install.
  The dnf adapter has only scripted tests. Test any adapter in a disposable VM
  before relying on it.
- **Docker** requires a reachable daemon and a user who may use it. Docker
  group membership is effectively root, which is why container changes are
  classified as system changes.
- **Allowlists are hand-written.** The read-only command list and the
  documentation-host list only cover what is listed. Anything else is blocked
  and the model must choose another route.
- **Only one package manager per host** is used for typed package operations
  (apt if present, otherwise dnf). Other managers (snap, flatpak, pipx, npm)
  are reported by `compare_install_methods` but cannot be installed by this
  agent yet.
- **No automatic rollback** for arbitrary operations. Recovery is proposed,
  and each recovery needs its own approval.
- **History and reports are per session.** There is no persistent memory.
- **The gateway must accept your configured model and support tool calling.**
  Run `linux-agent --check-gateway` to verify.

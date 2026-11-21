# Linux General-Purpose AI Agent

## Project and Implementation Overview

**Project type:** Personal Linux automation agent\
**Target platform:** Linux distributions (distribution- and
capability-aware)\
**Interface for Version 1:** CLI only\
**Future interface:** Local web UI (not part of the initial
implementation)\
**Architecture:** Hybrid --- Python orchestration and Bash execution
scripts\
**Autonomy model:** Hybrid --- automatic read-only operations; approval
for system changes\
**LLM integration:** OpenAI-compatible API gateway\
**Status:** Architecture and implementation blueprint

------------------------------------------------------------------------

## 1. Executive Summary

Linux Agent is a local, general-purpose AI system administration and
automation agent. It can prepare a newly installed Linux machine and
investigate, troubleshoot, configure, install, repair, and maintain an
existing Linux system across supported distributions.

The agent has **two primary operating modes**:

1.  **Fresh Linux provisioning:** Take a newly installed Linux machine
    and prepare it according to the user's requirements. Discover the
    machine, understand what the user wants, research current
    installation methods, compare options, build a setup plan, install
    and configure selected software after approval, verify the result,
    and generate a setup report.
2.  **Existing Linux administration:** Investigate, troubleshoot,
    configure, install, repair, and maintain an existing system.
    Understand the desired outcome, inspect relevant system state,
    identify root causes, research unfamiliar issues when needed,
    propose a safe plan, execute approved changes, verify the result,
    and document what happened.

Both modes use the same investigation, planning, research, tool
execution, permission, verification, and reporting engine. The agent
should not be split into separate installation and troubleshooting bots.

The user interacts with the agent in natural language and does not need
to know or choose predefined agent commands. The model dynamically
figures out the steps needed for the requested outcome and proposes the
Linux commands needed to perform those steps. The Python orchestrator
collects host context, sends the model the available execution contract,
validates each proposed command plan, applies local safety policy,
requests approval when required, and executes approved commands through
a controlled executor. Command output and errors are returned to the
model, which decides what to do next, interprets the evidence, and
verifies the requested end state.

The model is allowed to generate command plans at runtime; it is not
allowed to execute them directly. The local application remains the
security boundary. It resolves executables, validates structured
arguments and paths, classifies risk, enforces timeouts and output
limits, obtains local approval, and invokes processes without passing
model-generated text to an unrestricted shell. Registered Python tools
remain useful for host discovery, web research, reports, and other
structured capabilities, but they are not a hardcoded catalogue of all
Linux tasks or commands.

The initial implementation must provide a **command-line interface (CLI)
only**. A local browser-based web UI is a future enhancement and must
not be built as part of the initial version. Keep the agent service,
orchestrator, tool registry, execution layer, and permission policies
independent of the CLI presentation layer so a web UI can be added later
without rewriting the core.

### Primary goals

-   Support both fresh Linux provisioning and ongoing administration of
    existing Linux machines.
-   Bootstrap a newly installed Linux distribution into a configured
    workstation or service host.
-   Investigate Linux problems and determine likely root causes rather
    than blindly applying fixes.
-   Install, configure, update, repair, and where explicitly authorized
    remove user-selected applications.
-   Research current installation and troubleshooting information on the
    web, prioritize authoritative sources, compare viable options, and
    let the user choose.
-   Reduce repetitive Linux setup and maintenance work.
-   Make common system diagnostics available through natural language.
-   Standardize installation and verification of development tools and
    services.
-   Manage environment variables, configuration files, packages,
    services, storage, networking, and project dependencies.
-   Verify important changes instead of assuming a command succeeded.
-   Keep API credentials and local system access under the user's
    control.
-   Make operations traceable, predictable, and easy to extend.

### Non-goals for the initial release

-   Unrestricted autonomous shell access.
-   Automatic execution of unvalidated or unapproved commands proposed
    by an LLM or copied from webpages.
-   Passing model-generated command text directly to an unrestricted
    shell or executing it with `shell=True`.
-   Automatic deletion of user data, application data, or project files.
-   Replacing a full configuration-management platform.
-   Remote administration of other machines.
-   A multi-user hosted service.

## 2. Requirements and Scope

### 2.1 Two operating modes

The agent must support these two modes from the same application and
shared toolset.

#### Mode A --- Fresh Linux provisioning

**Purpose:** Take a newly installed machine running a supported Linux
distribution and prepare it according to the user's requirements.

Expected workflow: 1. Discover Linux distribution and release,
architecture, CPU, memory, storage, network connectivity, package
manager, privileges, and pre-installed software. 2. Ask what the user
wants the machine to be used for and which applications or development
components are required. 3. Search current web sources for supported
installation methods and compatibility. 4. Compare available methods,
versions, sources, update behavior, privilege requirements, and
trade-offs. 5. Let the user choose the applications and installation
methods. 6. Build a setup plan showing packages, repositories,
configuration files, services, ports, dependencies, and expected impact.
7. Detect conflicts and pre-existing data before changing the system. 8.
Request local approval for system modifications. 9. Install and
configure selected applications and components using reviewed
procedures. 10. Verify versions, paths, services, connectivity, and
functional behavior. 11. Generate a provisioning report with sources,
choices, changes, and verification results.

Example requests: - "Prepare this fresh Linux machine for software
development." - "Set up this machine according to the applications I
select." - "Search for installation options for this application and let
me choose." - "Install these applications using the methods I
approve." - "Configure a complete development environment, but show me
the plan first."

#### Mode B --- Existing Linux administration

**Purpose:** Investigate, troubleshoot, configure, install, repair, and
maintain an existing system on any supported Linux distribution.

Expected workflow: 1. Understand the desired outcome or reported
problem. 2. Inspect only the relevant system state, configuration, logs,
packages, services, processes, and network details. 3. Identify observed
facts and likely root causes; do not assume the first visible symptom is
the cause. 4. Search current documentation or known issue reports when
the problem or software behavior requires current information. 5. Build
a plan with proposed commands, files to change, risks, alternatives, and
expected results. 6. Request local approval for modifications according
to the risk policy. 7. Execute approved changes through registered tools
and controlled command execution. 8. Re-run relevant checks to verify
whether the original issue is resolved. 9. Explain the root cause,
actions performed, verification results, and remaining concerns. 10.
Generate a troubleshooting or maintenance report when useful.

Example requests: - "Why is this environment variable missing? Find the
cause and fix it." - "Investigate why this service is failing." - "Find
what is consuming my disk space and suggest safe cleanup." - "Diagnose
this network or DNS issue." - "Set up the dependencies needed to run
this project." - "Repair this development environment without deleting
my existing work."

#### Shared engine

Both modes must use the same components: - Natural-language request
understanding. - System discovery and evidence collection. - Web
research and source evaluation. - Task planning and option comparison. -
Tool registry and controlled command executor. - Risk classification and
local approval. - Python and Bash automation. - Post-change
verification. - Audit logging and report generation.

The operating mode determines the workflow emphasis, not a separate
codebase. The agent may move between provisioning and administration
during a single conversation when the user's task requires it.

### 2.2 Initial modules

  -----------------------------------------------------------------------
  Module                  Initial capabilities    Default risk
  ----------------------- ----------------------- -----------------------
  Environment management  Inspect, add, update,   Modification requires
                          and remove environment  approval
                          variables; manage       
                          user-level shell        
                          configuration           

  System diagnostics      OS, kernel, CPU,        Read-only
                          memory, disk, storage,  
                          uptime, processes,      
                          network and service     
                          checks                  

  Docker management       Docker status,          Inspection is
                          containers, images,     read-only; cleanup
                          disk usage, logs, safe  requires approval
                          cleanup                 

  Development environment Detect and install      Installation requires
  installation            selected tools such as  approval
                          Git, Python, Java,      
                          Node.js and Docker;     
                          verify versions         

  Project initialization  Create selected project File creation requires
                          templates and starter   approval or an explicit
                          files in an approved    workspace policy
                          workspace               

  Troubleshooting reports Collect diagnostic      Collection is
                          evidence, summarize     read-only; fixes
                          likely causes,          require approval
                          recommend fixes, export 
                          Markdown/JSON reports   
                          ning Install and        
                          configu                 
  -----------------------------------------------------------------------

### Fresh setup is in scope

The agent must support both maintaining existing Linux machines and
provisioning newly installed Linux systems. This includes installing
software from a clean state, configuring services, verifying that they
work, and generating a record of the setup. the selected database server
installation and configuration is an explicit example and an initial
supported provisioning workflow. Fresh setup must detect existing
installations and data first; "fresh install" must never mean deleting
existing data without a separate, explicit high-risk operation.

### 2.3 Natural-language-first interaction (no fixed command catalogue)

The user should describe the desired outcome in ordinary language. The
AI decides which diagnostic commands, installation commands,
configuration commands, or verification commands are needed. The user
must not have to memorize or select from a fixed list such as
`system health`, `docker status`, or `dev install java`.

Illustrative requests:

``` text
"Find out why my laptop is running slowly."
"Install Java 21 and configure JAVA_HOME permanently."
"Set up Docker and verify that it works."
"Find what is consuming disk space, but do not delete anything."
"Investigate why this project cannot connect to MySQL and fix it."
"Prepare this machine for Python and Spring Boot development."
"Show me the commands you intend to run before making changes."
```

For each request, the model should dynamically produce a structured
command plan. A plan contains one or more sequential command steps, each
with an executable, an argument array, an optional working directory,
a bounded timeout, and explicit input/output handling. The CLI renders
a readable shell-quoted preview for the user, but the preview is not
what gets executed. The executor runs the validated executable and
argument array directly.

The model may use multiple steps and adapt later steps based on earlier
outputs. It can request additional discovery or research, revise its
plan, and propose verification commands. There is no predefined list of
user-facing task commands; the supported boundary is the command
execution policy, the host's actual capabilities, and the tools the
agent can safely access.

Version 1 should not interpret arbitrary shell syntax. Do not pass a
model-generated command string to `bash -c`, `sh -c`, or
`subprocess.run(..., shell=True)`. Represent each command as structured
`executable + argv` data. Pipelines, redirections, and compound shell
expressions should be represented as explicit plan features only after
they have dedicated validation and tests. If a request cannot be safely
represented, explain the limitation rather than silently falling back
to unrestricted shell execution.

------------------------------------------------------------------------

## 3. Architecture

### 3.1 High-level flow

``` text
                         USER
                          |
                Natural-language request
                          |
                          v
                CLI only
                          |
                          v
                 Python Agent Orchestrator
                          |
             +------------+-------------+
             |                          |
             v                          v
       Fresh Provisioning        Existing Administration
       workflow                  workflow
             |                          |
             +------------+-------------+
             |                          |
             v                          v
       LLM API Gateway           Local policy engine
       (planning/tool calls)     (allowlist and risk)
             |                          |
             +------------+-------------+
                          |
                          v
                  Registered tool layer
                          |
             +------------+-------------+
             |                          |
             v                          v
        Python tools                Bash scripts
             |                          |
             +------------+-------------+
                          |
                          v
                  Linux host system
                          |
                          v
             Structured result and logs
                          |
                          v
                 LLM response/report
                          |
                          v
                         USER
```

### 3.2 Component responsibilities

**CLI** - Accepts user input. - Displays progress, approval prompts,
results, and reports. - Contains no system-operation logic; it delegates
to the shared agent service layer.

**Python agent orchestrator** - Maintains the conversation for the
current session. - Sends the request, host context, registered Python
tool schemas, and structured command-plan schema to the configured
model. - Parses model-proposed command plans. - Validates each command
step and applies operation policies before execution. - Runs approved
steps through the controlled executor and returns bounded results to
the model. - Enforces maximum tool/command rounds, total task duration,
and cancellation.

**LLM gateway** - Provides natural-language interpretation and tool
selection. - Uses a configurable OpenAI-compatible base URL and model
identifier. - Must be treated as an external service: do not send
secrets, private keys, access tokens, or unnecessary personal data in
prompts.

**Policy and permission layer** - Classifies registered application
tools and dynamically proposed command steps as read-only, modifying,
privileged, destructive, or unknown. - Requires local user approval for
modifications and stronger typed confirmation for high-impact actions.
- Rejects unknown application tools, malformed command plans, unsafe
arguments, and unsupported execution features. - Does not trust a
model-supplied `confirm=true` value as proof of user approval.

**Command planner and executor** - The model proposes structured command
steps dynamically from the user's request and observed system state. -
Each step contains an executable and argument array rather than a raw
shell program. - The local executor resolves the executable, validates
arguments and paths, classifies risk, enforces policy, asks for approval
when required, and runs the process with `shell=False`. - It captures
bounded stdout/stderr, exit status, duration, and timeout information,
then returns structured results to the model for the next decision.

**Registered Python tools** - Provide typed interfaces for capabilities
that are better represented as application functions, such as host
discovery, web research, report generation, and task-state operations.
They complement the dynamic command executor; they do not define a
fixed catalogue of all tasks the agent can perform.

**Bash scripts** - Encapsulate repeatable Linux procedures and call
distribution-specific adapters only after capability detection. - Use
strict error handling and clear exit codes. - Are version-controlled and
independently runnable where practical. - Must not silently elevate
privileges.

**Reports and logs** - Store diagnostic reports and execution
metadata. - Avoid storing API keys, environment-variable values that may
contain secrets, or full command output containing credentials.

------------------------------------------------------------------------

### 3.3 Capability-based Linux compatibility layer

The agent must be **Linux-general and capability-based**, not a
collection of distribution-name checks. Distribution identity is useful
context, but it is not proof that a particular command, package manager,
init system, filesystem feature, or service manager is available.

At startup and before relevant tasks, build a structured host capability
profile. Re-check capabilities when the environment may have changed.

**Discovery inputs**

-   `/etc/os-release` for `ID`, `ID_LIKE`, `VERSION_ID`, and descriptive
    metadata. Treat these values as hints for adapter selection, not as
    the sole source of truth.
-   `uname` for kernel and architecture.
-   Executable discovery (`shutil.which`) and safe version/help probes
    for available tools.
-   Process and filesystem evidence to identify container, VM, chroot,
    or restricted environments where relevant.
-   Read-only checks for privileges, package database access, service
    manager availability, network tools, and storage utilities.

**Capability profile examples**

``` python
HostCapabilities(
    distribution_id="example",
    distribution_like=["debian"],
    distribution_version="x.y",
    architecture="x86_64",
    kernel="...",
    package_manager="apt",       # discovered, not assumed
    package_manager_available=True,
    service_manager="systemd",   # discovered, not assumed
    service_manager_available=True,
    privilege_methods=["sudo"],
    network_tools=["ip", "resolvectl"],
    is_container=False,
)
```

The actual implementation should use validated typed models and allow
unknown or unavailable fields. Do not infer that every capability exists
because a distribution belongs to a familiar family.

**Adapter design**

Expose distribution-independent operations through interfaces such as:

-   `PackageManager`: query installed packages, search, install, update,
    remove, and verify package state.
-   `ServiceManager`: inspect service state, logs, enable/disable,
    start, stop, and verify service health.
-   `NetworkTools`: inspect interfaces, routes, DNS and connectivity
    using available tools.
-   `FirewallManager`: inspect and manage firewall rules only when a
    supported backend is detected.
-   `UserManager` and `StorageManager`: perform supported user and
    storage operations through capability-specific implementations.

Initial package-manager adapter candidates include APT/DPKG, DNF/RPM,
Pacman, Zypper/RPM, and APK. Initial service-manager adapter candidates
include systemd and OpenRC. These are implementation targets, not a
claim of complete support until the relevant tests pass. This list is
extensible and must not be treated as a guarantee that all operations
are available on every distribution.

**Capability selection rules**

1.  Detect the capability relevant to the requested task.
2.  Validate the executable and its expected behavior with a safe probe.
3.  Select a registered adapter that declares support for that
    operation.
4.  If multiple backends exist, explain the choice or ask the user when
    the choice has meaningful consequences.
5.  If no compatible adapter exists, stop safely, explain the
    limitation, and offer a read-only alternative where possible.
6.  Never fall back to a different command merely because the preferred
    command failed; first determine whether the fallback is semantically
    equivalent and safe.
7.  Cache discovery only for the current session or with an explicit
    invalidation strategy. Re-discover after package, service, or system
    configuration changes.

For example, service operations must first detect whether `systemd` is
actually active and `systemctl` is usable. Only then may the systemd
adapter invoke `systemctl`. If the host uses OpenRC, the OpenRC adapter
must be selected. If the service manager is unknown, do not guess.

## 4. Recommended Project Layout

``` text
linux-agent/
├── README.md
├── PROJECT_OVERVIEW.md
├── requirements.txt
├── pyproject.toml
├── .env.example
├── .env                    # Local only; never commit
├── .gitignore
│
├── main.py                 # CLI entry point
├── config.py               # Configuration loading and validation
│
├── platform/               # Capability-based Linux abstraction layer
│   ├── discovery.py         # Detect distro, kernel, tools and capabilities
│   ├── capabilities.py     # Typed host capability model
│   ├── packages/            # APT, DNF, Pacman, Zypper, APK adapters
│   ├── services/            # systemd, OpenRC and other supported adapters
│   ├── networking.py        # Capability-aware network operations
│   └── storage.py           # Capability-aware storage operations
│
├── agent/
│   ├── __init__.py
│   ├── orchestrator.py     # Model/tool-call loop
│   ├── registry.py         # Registered tools and schemas
│   ├── policy.py           # Risk levels and approval rules
│   ├── executor.py         # Validated tool execution
│   └── prompts.py          # System prompt and agent instructions
│
├── tools/
│   ├── __init__.py
│   ├── environment.py
│   ├── system.py
│   ├── docker.py
│   ├── development.py
│   ├── projects.py
│   └── troubleshooting.py
│
├── scripts/
│   ├── diagnostics.sh
│   ├── environment.sh
│   ├── install_java.sh
│   ├── install_python.sh
│   ├── install_node.sh
│   ├── docker_status.sh
│   ├── docker_cleanup.sh
│   └── project_init.sh
│
├── templates/
│   ├── python/
│   ├── springboot/
│   └── react/
│
├── reports/                # Generated locally; normally gitignored
├── logs/                   # Local execution logs; normally gitignored
└── tests/
    ├── test_policy.py
    ├── test_environment.py
    ├── test_system.py
    ├── test_docker.py
    └── test_projects.py
```

Keep the first implementation small. Add modules as they become useful;
do not create empty abstractions just to fill the directory tree.

------------------------------------------------------------------------

## 5. Technology Choices

  -----------------------------------------------------------------------
  Area                    Recommended technology  Reason
  ----------------------- ----------------------- -----------------------
  Main language           Python 3                Good subprocess,
                                                  filesystem, API and
                                                  testing support

  OS automation           Bash                    Native Linux
                                                  administration and
                                                  shell configuration

  LLM client              OpenAI Python SDK       Supports
                                                  OpenAI-compatible
                                                  endpoints through a
                                                  custom base URL

  Configuration           `python-dotenv`         Simple local `.env`
                                                  configuration

  CLI output              `rich`                  Readable tables, status
                                                  and prompts

  Validation              Pydantic or explicit    Validate tool inputs
                          Python validation       and configuration

  Tests                   `pytest`                Unit and integration
                                                  testing

  Reports                 Markdown and JSON       Human-readable and
                                                  machine-readable output

  Optional state          SQLite                  Session metadata and
                                                  execution history if
                                                  needed

  Future web UI           FastAPI plus frontend   Optional later phase;
                                                  reuse agent services
  -----------------------------------------------------------------------

Avoid adding a large agent framework initially. A small explicit
tool-calling loop is easier to inspect, secure, and debug.

------------------------------------------------------------------------

## 6. Linux Agent Setup and Installation

### 6.1 Verify the host and prerequisites

The setup instructions must not assume APT or any single distribution.
Start with read-only discovery:

``` bash
cat /etc/os-release
uname -a
uname -m
python3 --version
python3 -m venv --help
git --version
```

The project should document the supported Python version and check that
the host meets it. If a prerequisite is missing, detect the available
package manager and present the correct distribution-specific install
command. Do not execute a package-manager command until the detected
capability and target package manager have been validated.

Common package-manager families include:

  -----------------------------------------------------------------------
  Distribution family     Examples                Typical package manager
  ----------------------- ----------------------- -----------------------
  Debian family           Debian, Ubuntu, Linux   APT / DPKG
                          Mint, Pop!\_OS          

  Fedora / RHEL family    Fedora, RHEL, Rocky     DNF / RPM
                          Linux, AlmaLinux        

  Arch family             Arch Linux, Manjaro,    Pacman
                          EndeavourOS             

  SUSE family             openSUSE, SUSE Linux    Zypper / RPM
                          Enterprise              

  Alpine                  Alpine Linux            APK
  -----------------------------------------------------------------------

These are examples, not an exhaustive compatibility promise. The agent
must detect the actual package manager and its capabilities rather than
selecting one only from a distribution-name lookup.

### 6.2 Create the project

``` bash
mkdir -p ~/projects/linux-agent
cd ~/projects/linux-agent

mkdir -p agent tools scripts templates reports logs tests
touch main.py config.py
touch agent/__init__.py tools/__init__.py
```

### 6.3 Create and activate a virtual environment

``` bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### 6.4 Initial dependencies

A minimal `requirements.txt`:

``` text
openai
python-dotenv
rich
pytest
```

Install:

``` bash
python -m pip install -r requirements.txt
```

### 6.5 Environment configuration

Create `.env.example`:

``` dotenv
OPENAI_API_KEY=replace_with_gateway_key
OPENAI_BASE_URL=https://your-openai-compatible-gateway.example/v1
OPENAI_MODEL=replace_with_supported_model_id

AGENT_MAX_TOOL_ROUNDS=5
AGENT_REQUEST_TIMEOUT=60
AGENT_REPORT_DIR=reports
AGENT_LOG_DIR=logs
AGENT_WORKSPACE_DIR=~/projects
```

## 7. API Gateway Integration

### 7.1 Configuration loader

`config.py` should: 1. Load `.env`. 2. Validate that the API key, base
URL, and model are present. 3. Validate numeric timeout and iteration
settings. 4. Expand the workspace and report paths. 5. Fail with a clear
message when configuration is incomplete.

Example:

``` python
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY")
BASE_URL = os.getenv("OPENAI_BASE_URL")
MODEL = os.getenv("OPENAI_MODEL")

MAX_TOOL_ROUNDS = int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "5"))
REQUEST_TIMEOUT = int(os.getenv("AGENT_REQUEST_TIMEOUT", "60"))

WORKSPACE = Path(
    os.getenv("AGENT_WORKSPACE_DIR", "~/projects")
).expanduser().resolve()

REPORT_DIR = Path(
    os.getenv("AGENT_REPORT_DIR", "reports")
).expanduser().resolve()

LOG_DIR = Path(
    os.getenv("AGENT_LOG_DIR", "logs")
).expanduser().resolve()

if not API_KEY or not BASE_URL or not MODEL:
    raise RuntimeError(
        "Set OPENAI_API_KEY, OPENAI_BASE_URL and OPENAI_MODEL in .env"
    )
```

### 7.2 Client initialization

``` python
from openai import OpenAI
from config import API_KEY, BASE_URL, REQUEST_TIMEOUT

client = OpenAI(
    api_key=API_KEY,
    base_url=BASE_URL,
    timeout=REQUEST_TIMEOUT,
    max_retries=2,
)
```

### 7.3 Gateway compatibility checks

Before implementing the agent loop, verify:

-   The gateway accepts the configured base URL.
-   The API key is valid and has access to the selected model.
-   The model supports the API endpoint being used.
-   The model supports function/tool calling.
-   Tool-call arguments are returned in the expected format.
-   The gateway's supported JSON schema subset is understood.
-   The gateway's context and output limits are sufficient.

A simple connectivity check can list models if the gateway exposes the
standard models endpoint:

``` python
from openai import OpenAI
from config import API_KEY, BASE_URL

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

for model in client.models.list().data:
    print(model.id)
```

A successful model listing does not prove that a particular model
supports tool calling. Test an actual tool-call request before relying
on it.

------------------------------------------------------------------------

## 8. Agent Execution Design

### 8.1 Tool-calling lifecycle

1.  Receive a user request.
2.  Add the request to the current conversation.
3.  Send the conversation and registered tool schemas to the model.
4.  Receive either a text response or tool calls.
5.  Check each requested tool against the local registry.
6.  Validate arguments against the tool's schema.
7.  Determine the tool's risk level.
8.  If needed, display a local approval prompt.
9.  Execute the registered implementation.
10. Capture structured output, exit status, and safe diagnostic
    metadata.
11. Return the tool result to the model.
12. Let the model explain the result.
13. Stop when the model responds without further tool calls or the
    iteration limit is reached.

### 8.2 Tool schema example

A system diagnostic tool should have a narrow schema:

``` python
{
    "type": "function",
    "function": {
        "name": "system_diagnostics",
        "description": "Collect read-only Linux system information",
        "parameters": {
            "type": "object",
            "properties": {
                "include_network": {
                    "type": "boolean"
                }
            },
            "required": [],
            "additionalProperties": False
        }
    }
}
```

The tool registry should be the source of truth. Never use `eval`,
`exec`, or a model-provided shell string to dispatch operations.

### 8.3 Execution result format

Prefer structured results over raw strings:

``` json
{
  "tool": "system_diagnostics",
  "success": true,
  "exit_code": 0,
  "summary": "System diagnostics completed",
  "data": {
    "kernel": "example-kernel",
    "memory": "example-memory-output"
  },
  "warnings": []
}
```

Truncate very large command output and redact secrets before sending
results to the model.

------------------------------------------------------------------------

## 9. Permission and Safety Model

This is a core architectural requirement, not an optional enhancement.

### 9.1 Risk levels

  -----------------------------------------------------------------------
  Risk                    Examples                Policy
  ----------------------- ----------------------- -----------------------
  Read-only               `uname`, `free`, `df`,  May execute
                          `docker ps`, service    automatically
                          status                  

  Reversible modification Create a project        Show proposed change
                          directory, update a     and request approval by
                          user-level environment  default
                          file                    

  System modification     Install packages, edit  Explicit approval
                          system configuration,   required
                          restart services        

  Destructive             Delete files, prune     Strong confirmation;
                          Docker volumes, remove  often require typed
                          packages, overwrite     target or phrase
                          projects                

  Privileged              `sudo`, changing        Separate reviewed
                          ownership or            workflow; never
                          permissions, firewall   model-authorized
                          changes                 
  -----------------------------------------------------------------------

### 9.2 Approval must be local

A model response such as `{"confirm": true}` is not user approval.

The application must independently prompt the person using the terminal:

``` text
Requested operation: Docker cleanup
Potential impact: Removes unused Docker resources
Command category: Destructive

Continue? Type 'yes' to proceed:
```

For high-impact operations, show the exact target and require a typed
confirmation phrase.

### 9.3 Dynamic AI-generated command plans (without unrestricted shell)

The agent must support AI-generated Linux commands at runtime. Do not
force every task into a hand-written Python tool or a fixed command
catalogue. The model may propose commands it determines are appropriate
for the user's request, including commands discovered through current
system inspection or authoritative research.

However, a model-generated command is a proposal, not permission to run
it. The executor must receive a structured plan rather than a free-form
shell string. The initial plan schema should support:

-   Sequential steps with a unique step ID.
-   Executable name or resolved executable path.
-   Argument array (each argument is a separate string).
-   Optional approved working directory.
-   Bounded timeout and output limits.
-   Explicit environment-variable allowlist; never inherit or expose all
    secrets by default.
-   Optional stdin sourced from a previous step's bounded output.
-   Per-step purpose, expected result, and risk rationale.

The CLI should show the exact command preview, working directory,
privilege requirement, affected resources, and expected impact. The
preview must be rendered safely (shell-quoted for display only); actual
execution uses the original argument array.

**Version 1 execution constraints:**

-   Invoke processes with `subprocess` argument arrays and `shell=False`.
-   Do not use `eval`, `exec`, `bash -c`, `sh -c`, or
    `subprocess.run(model_generated_command, shell=True)`.
-   Do not support shell expansion, command substitution, arbitrary
    redirection, or compound shell syntax in the first executor.
-   Support multiple sequential command steps; later steps may depend
    on bounded, structured results from earlier steps.
-   Resolve executables against the discovered environment and reject
    missing or ambiguous executables.
-   Apply command-aware risk rules. Read-only operations may run
    automatically when their classification is reliable. Modifying
    operations require local approval. Destructive, security-sensitive,
    or high-impact operations require a stronger typed confirmation or
    may be blocked entirely in Version 1.
-   Unknown or unclassifiable commands must not be treated as read-only;
    default to requiring approval or blocking them according to policy.
-   Validate paths, working directories, package names, service names,
    and target resources before execution.
-   Apply timeouts, output limits, cancellation, and secret redaction.
-   Return exit code, stdout, stderr, duration, timeout status, and
    execution metadata to the model as untrusted evidence.
-   Re-evaluate the plan after every command result. Do not execute a
    long chain blindly when an earlier step fails or produces unexpected
    output.

Do not solve safety by restricting the agent to a tiny fixed list of
commands. Instead, allow the model to propose commands dynamically while
the local policy engine constrains what can actually execute. Never
allow the model to approve its own plan or bypass local controls.

### 9.4 Privilege handling

-   Do not run the entire agent as root.
-   Do not put the API key in a shell script or command-line argument.
-   Do not configure passwordless unrestricted `sudo`.
-   Prefer documented package-manager operations that prompt for the
    user's password through a reviewed workflow.
-   Do not pipe remote scripts directly into a root shell.
-   Never automatically change firewall, SSH, bootloader, disk
    partition, or user-account settings.

### 9.5 Path safety

-   Restrict project creation to a configured workspace such as
    `~/projects`.
-   Resolve paths before writing.
-   Reject path traversal and unexpected absolute paths.
-   Do not overwrite an existing project unless the user explicitly
    approves it.
-   Do not follow symlinks outside the allowed workspace without a
    separate policy.
-   Use temporary files and atomic replacement for configuration
    updates.

### 9.6 Secret handling

-   Keep `.env` out of version control.
-   Set restrictive file permissions.
-   Redact tokens, passwords, private keys, and credential-like values
    from logs and reports.
-   Never ask the model to inspect or print the complete environment.
-   For environment-variable inspection, display names by default and
    mask values likely to contain secrets.
-   Avoid including shell history or full process command lines in
    reports unless specifically needed and reviewed.

------------------------------------------------------------------------

## 10. Module Implementation Overview

### 10.1 Environment Management

**Purpose:** Make user-level environment variable setup consistent and
repeatable.

Capabilities: - List variable names without exposing sensitive values. -
Read a selected variable with masking where appropriate. - Set or update
a variable in a dedicated agent-managed configuration file. - Remove a
variable from that managed file. - Explain how to reload the shell
configuration. - Verify values in a new shell.

Recommended approach: - Manage a dedicated file such as
`~/.config/linux-agent/env.sh`. - Source it from the user's chosen shell
startup file only after approval. - Do not repeatedly append duplicate
exports to `.bashrc`. - Preserve existing files and create backups
before modification. - Support Bash first; add Zsh only when needed.

Example generated line:

``` bash
export JAVA_HOME="/usr/lib/jvm/example"
```

The agent should distinguish between setting a variable for the current
process, persisting it for future shells, and configuring it for a
particular application.

### 10.2 System Diagnostics and Capability Discovery

**Purpose:** Build a reliable, privacy-conscious picture of the Linux
host and discover which operations it actually supports.

Start with broadly available checks where possible:

  ---------------------------------------------------------------------
  Area                               Discovery / checking commands or
                                     sources
  ---------------------------------- ----------------------------------
  Distribution                       `/etc/os-release`; `lsb_release`
                                     only if installed

  Kernel / architecture              `uname -a`, `uname -m`

  CPU                                `lscpu` when available;
                                     `/proc/cpuinfo` as a fallback

  Memory                             `free -h` when available;
                                     `/proc/meminfo` as a fallback

  Disk space                         `df -h`

  Block devices                      `lsblk` when available;
                                     `/proc/partitions` as a fallback

  Uptime                             `uptime` or `/proc/uptime`

  Processes                          `ps`

  Network                            `ip addr`, `ip route`; detect
                                     alternatives if `ip` is absent

  Services                           Use the detected service-manager
                                     adapter

  Package state                      Use the detected package-manager
                                     adapter
  ---------------------------------------------------------------------

Diagnostics should be read-only and report missing commands or
unsupported capabilities as warnings rather than failing the entire
report. Prefer standard files under `/proc` and `/etc` as discovery
fallbacks where appropriate.

Do not assume `systemd`, `systemctl`, `apt`, `dnf`, `pacman`, `sudo`,
`lsb_release`, or any other optional utility exists. Check for the
capability before invoking a command.

Avoid collecting sensitive information by default, including full
environment dumps, private IP inventories when unnecessary, browser
profiles, SSH configuration contents, and full process arguments.

### 10.3 Docker Management

**Purpose:** Inspect Docker and perform controlled maintenance.

Read-only operations: - Check whether the Docker CLI is installed. -
Check whether the daemon is reachable. - List running and stopped
containers. - List images and volumes. - Inspect Docker disk usage. -
Read a bounded number of recent log lines from a specified container.

Modifying operations: - Stop or remove a specified container. - Remove a
specified image. - Prune unused resources. - Remove volumes only through
a separate high-risk workflow.

Important distinction: - `docker system prune` and
`docker system prune -a` have different effects. - Volume pruning can
remove persistent data. - Never include `--volumes` in a default cleanup
operation. - Show the proposed command and expected scope before
approval. - Do not assume a stopped container or unused image is
disposable.

### 10.4 Development Environment Installation

**Purpose:** Detect, install, and verify commonly used development
tools.

Initial candidates: - Git - Python and Python virtual-environment
support - Java JDK - Node.js and npm - Docker Engine or Docker CLI,
depending on the user's requirements - Build tools needed by selected
project templates

For each tool, implement three distinct operations:

1.  **Detect:** Is it installed? Which version?
2.  **Plan:** Which package source and installation method will be used?
3.  **Install and verify:** Obtain approval, install, then check the
    resulting version.

Do not assume the newest version is always the correct one. Support an
explicit version or installation channel where practical. Prefer the
detected distribution's native repositories for simple packages; use
official vendor repositories only when needed and document the trust and
key-management implications.

For Docker, follow the current official installation instructions for
the user's Linux distribution and release rather than embedding stale
repository commands indefinitely.

### 10.5 Project Initialization

**Purpose:** Create repeatable project structures in an approved
workspace.

Initial templates: - Python CLI project - Spring Boot project -
React/Vite project

A project generator should: - Validate the project name. - Restrict
output to the configured workspace. - Check whether the target directory
already exists. - Preview the files it intends to create. - Require
approval before writing. - Avoid overwriting files. - Include a README
and `.gitignore`. - Offer Git initialization as a separate action.

Keep templates as ordinary files in `templates/`, not as large strings
inside the agent logic.

For generated projects, record template version and selected options in
a small metadata file if useful.

### 10.7 Web Search and Installation Option Discovery

**Purpose:** Research current, relevant installation choices before the
agent proposes changes to the Linux system.

The agent must not assume there is only one correct installation method.
Software may be available through the detected distribution's
repositories, an official vendor repository, a standalone package, a
language-specific package manager, a container image, a source build, or
another supported distribution channel. The available choices depend on
the application, Linux distribution and release, architecture, support
policy, and user requirements.

#### Research workflow

1.  Identify the requested application or component and the user's goal.
2.  Detect the Linux distribution and release, CPU architecture, and
    relevant existing software versions.
3.  Search the web for current installation methods and compatibility
    information.
4.  Prioritize official project documentation, official vendor
    documentation, distribution documentation, and maintained package
    repositories.
5.  Check publication/update dates, supported distribution releases,
    package availability, maintenance status, signing-key instructions,
    and known caveats.
6.  Compare viable methods and explain their trade-offs.
7.  Present the options to the user before installing anything.
8.  Ask the user to select an option or explicitly authorize a
    recommended option.
9.  Generate a concrete installation plan showing packages,
    repositories, configuration changes, privileges, services, ports,
    and expected impact.
10. Require local approval before system modifications.
11. Execute the model's structured installation command plan only
    after local validation and approval of the exact steps.
12. Verify the installation and produce a report with the selected
    method and source references.

#### Installation option presentation

When multiple valid methods exist, show a concise comparison such as:

  -----------------------------------------------------------------------
  Option                  Typical characteristics Considerations
  ----------------------- ----------------------- -----------------------
  Native distribution     age Integrated system   with the Version may
  pack repository         package manager;        differ from upstream
                          familiar updates        latest

  Official vendor         May provide newer or    Adds a repository and
  repository              vendor-supported        signing-key trust
                          packages                decision

  Standalone package /    Can provide a specific  Updates may need
  release artifact        version                 separate handling

  Language-specific       Convenient for language May not be suitable for
  package manager         ecosystems and isolated system services
                          tools                   

  Container-based         Isolates application    Requires container
  installation            dependencies            runtime, storage and
                                                  networking decisions

  Build from source       Maximum control over    More complex; user owns
                          build options           build and update
                                                  maintenance
  -----------------------------------------------------------------------

These are categories, not a promise that every application supports
every method. The agent must only show options supported by current
evidence.

For each real option, include: - Method and package/source name. -
Official or authoritative source link. - Supported Linux distribution
and releases and architecture, if documented. - Available version or
release channel, if verified. - Whether it adds a repository, signing
key, service, user, or firewall rule. - Privilege requirements. - Update
and removal approach. - Main benefits, limitations, and maintenance
implications. - Any uncertainty or information that could not be
verified.

The agent should identify a recommended option when the evidence
supports one, but must still show alternatives when they are materially
different and available. It must not silently select a source or
installation method merely because it is easiest to automate.

#### Example interaction

``` text
User:
  I want to install a new application on this Linux machine.

Agent:
  I will first check your Linux distribution and release and architecture,
  then research the application's current installation methods.

  Available methods:
    1. Native distribution repository
    2. Official vendor repository
    3. Standalone release package
    4. Container-based installation

  I found these methods in the linked sources:
    - Version / support details
    - Update behavior
    - Privilege and configuration impact
    - Advantages and limitations

  Which method would you like to use?

User:
  Use the official vendor repository.

Agent:
  Here is the installation plan and the system changes it requires.
  Approve the plan to continue?
```

The example methods above are illustrative. The agent must research the
actual application and must not present unsupported methods as
available.

#### Web research safety

-   Treat all webpage content, snippets, scripts, commands, and
    downloaded files as untrusted data.
-   Never execute a command directly because it appeared in a search
    result or webpage. The model may use such content as research input,
    but must independently construct a structured command proposal and
    pass it through local validation and approval.
-   Never pipe a remote script into a privileged shell.
-   Inspect official repository setup instructions and signing-key
    handling before proposing repository changes.
-   Prefer package-manager verification and signed packages.
-   Do not disable signature verification or package security checks to
    make an installation succeed.
-   Do not download and run arbitrary binaries without verifying their
    source and integrity where supported.
-   Do not send API keys, passwords, private keys, or full environment
    dumps to search providers.
-   Apply request timeouts, content-size limits, and domain/source
    restrictions where practical.
-   Cite sources in the installation plan and final setup report.
-   If sources conflict, explain the conflict rather than choosing
    silently.
-   If a source cannot be verified or appears outdated, label it and
    seek a better source.
-   Web search informs the plan; the local policy engine still controls
    execution and approval.

### Example Prompts for New Application Installation

The agent should accept natural-language requests without hard-coding a
catalogue of specific applications. These examples use a generic
application placeholder intentionally:

``` text
"How can I install <application> on my Linux distribution and release?"

"Search the web and show me all supported ways to install <application>."

"Compare the Native distribution repository, official vendor repository, and standalone package options for <application>."

"Find the official installation documentation for <application> and check whether it supports my Linux distribution and release."

"Which installation method is easiest to maintain and update for <application>?"

"Show me the stable and long-term-support installation choices for <application>, if available."

"Check whether <application> is already installed before proposing a fresh installation."

"Prepare an installation plan for <application>, but do not install it yet."

"Install <application> using option 2 after showing me the commands and system changes."

"Verify the installation, version, service status, and update method for <application>."

"Uninstall <application>, but first show me what packages, configuration, and user data would be affected."
```

The last example is a removal workflow and must use a separate,
higher-risk approval process. The agent must never assume that removing
an application also authorizes deleting its configuration or user data.

### 10.8 Fresh Setup and Workstation Provisioning

**Purpose:** Turn a clean Linux installation into a configured, verified
development workstation or service host. Fresh setup is a general
capability for user-selected software; the project must not be
hard-coded around a particular new application.

The agent should support: - **Single-application setup:** install one
application selected by the user. - **Multi-component setup:** prepare a
machine with a user-selected set of development tools and services. -
**Client-only setup:** install command-line or desktop client components
without installing or enabling a local server, when the software
supports that distinction. - **Existing-installation handling:**
inspect, repair, upgrade, or configure an existing installation rather
than assuming the machine is empty.

#### Provisioning workflow

1.  **Discover:** Identify Linux distribution and release, architecture,
    package manager, existing installations, available disk space,
    memory, network access, and whether the user has administrative
    privileges.
2.  **Clarify scope:** Determine the requested application, desired
    version/channel, installation purpose, required components,
    local-only versus network access, authentication needs, and whether
    existing data must be preserved.
3.  **Research:** Search current web sources for supported installation
    methods and compatibility with the detected Linux distribution and
    release.
4.  **Compare:** Present viable installation methods, source links,
    version availability, update behavior, privilege requirements,
    trade-offs, and maintenance implications.
5.  **Choose:** Let the user select an installation method. The agent
    may recommend one with reasons, but must not silently choose when
    meaningful alternatives exist.
6.  **Plan:** Produce a human-readable plan listing packages,
    repositories, configuration files, services, ports, firewall
    changes, and expected disk usage.
7.  **Check conflicts:** Detect existing services, occupied ports,
    incompatible versions, existing application data, and configuration
    files that would be changed.
8.  **Approve:** Ask for local approval before package installation,
    repository changes, service configuration, account creation,
    firewall changes, or other modifications.
9.  **Execute:** Let the model propose structured installation and
    configuration command steps based on the selected method and
    detected host. Validate each step locally, show its command preview,
    obtain approval, and execute the argument array with timeouts and
    logged results. Do not execute raw shell strings generated by the
    model or copied from webpages.
10. **Verify:** Check package versions, service state, listening ports,
    health checks, authentication, and a minimal functional test where
    appropriate.
11. **Document:** Save a setup report describing what was installed,
    selected method and sources, configuration paths, service names,
    ports, verification results, and manual steps.
12. **Recover:** If a step fails, stop safely, preserve logs, explain
    what changed, and provide a rollback or recovery plan. Do not
    automatically remove data or purge packages to "start over."

#### Installation choices to present

The actual choices must be researched for the application requested.
Depending on what the publisher supports, possible methods may include:

  -----------------------------------------------------------------------
  Method                  Typical characteristics Considerations
  ----------------------- ----------------------- -----------------------
  Native distribution     age Integrated system   with the Version and
  pack repository         package manager         release cadence may
                                                  differ from upstream

  Official vendor         May provide             Adds a repository and
  repository              vendor-supported or     signing-key trust
                          newer packages          decision

  Standalone package or   Can provide a specific  Updates may need
  release artifact        version                 separate handling

  Language-specific       Convenient for language Not always appropriate
  package manager         ecosystems and isolated for system-wide
                          tools                   services

  Container-based         Isolates application    Requires runtime,
  installation            dependencies            storage, networking and
                                                  lifecycle decisions

  Build from source       Offers control over     More complex; user owns
                          build options           build and update
                                                  maintenance

  Official installer or   May provide a           Must verify provenance,
  setup utility           publisher-supported     integrity, privileges
                          setup flow              and update behavior
  -----------------------------------------------------------------------

These are categories, not a promise that every application supports
every method. The agent must show only methods supported by current
evidence.

For every viable option, show: - Installation method and package/source
name. - Official or authoritative source link. - Supported Linux
distributions and releases and CPU architectures, if documented. -
Available version or release channel, if verified. - Whether it adds a
repository, signing key, service, user, or firewall rule. - Privilege
requirements. - Update and removal approach. - Main benefits,
limitations, and maintenance implications. - Any uncertainty or
information that could not be verified.

#### Example prompts for fresh application installation

The agent should accept natural-language requests without maintaining a
hard-coded catalogue of specific applications. Example prompts:

``` text
"How can I install <application> on my Linux distribution and release?"

"Search the web and show me the supported ways to install <application>."

"Compare the available installation methods for <application>."

"Find the official installation documentation for <application> and check whether it supports my Linux distribution and release."

"Which installation method for <application> is easiest to maintain and update?"

"Show me stable and long-term-support installation choices for <application>, if available."

"Check whether <application> is already installed before proposing a fresh installation."

"Prepare an installation plan for <application>, but do not install it yet."

"Show me the advantages and disadvantages of each installation option."

"I choose option 2. Show me the exact changes and commands you plan to perform."

"Install <application> using my selected option after I approve the plan."

"Verify the installed version, service status, and update method for <application>."

"Set up a fresh Linux development environment. First inspect my system and ask which components I want."

"Find applications that can perform <task>, compare their official Linux installation options, and let me choose."

"Uninstall <application>, but first show me what packages, configuration, and user data would be affected."
```

The uninstall example is a separate, higher-risk workflow. Removing an
application does not authorize deleting its configuration or user data.

#### Fresh setup safety boundaries

-   Detect existing packages, services, ports, configuration and data
    before proposing changes.
-   Never assume that "fresh install" means deleting an existing
    installation or its data.
-   Do not overwrite configuration files without a backup and a
    displayed change plan.
-   Do not remove existing data directories as part of installation or
    repair.
-   Do not open firewall ports or expose a service to the network unless
    explicitly requested and approved.
-   Do not create weak or hard-coded credentials.
-   Do not place passwords in shell command arguments, tool-call
    transcripts, reports, or source files.
-   Use an interactive secure input mechanism or a protected secret
    store for credentials.
-   Do not disable package signature verification or security checks to
    make an installation succeed.
-   Do not pipe remote scripts into a privileged shell.
-   Do not run arbitrary commands copied from webpages.
-   Do not assume a service name, configuration path, default account,
    or port without checking authoritative documentation for the
    selected software and installation method.
-   Treat removal, purge, data deletion, reset, and downgrade as
    separate high-risk operations.
-   If the application is already present, offer inspection,
    configuration, repair, or upgrade planning instead of treating it as
    a clean machine.

#### Idempotency and repeat runs

A provisioning operation should be safe to run more than once: - Detect
installed packages and existing services before installing. - Avoid
duplicate repository entries and environment exports. - Avoid
overwriting existing configuration. - Distinguish "already installed and
healthy" from "partially installed" and "installed but misconfigured." -
Offer a repair plan for partial states. - Record what the agent changed
so it can explain or reverse supported changes.

## 11. Bash Script Standards

Bash scripts should be small, predictable, and independently testable.

Recommended header:

``` bash
#!/usr/bin/env bash
set -Eeuo pipefail

log() {
    printf '[INFO] %s\n' "$*"
}

fail() {
    printf '[ERROR] %s\n' "$*" >&2
    exit 1
}
```

Standards: - Quote variable expansions. - Validate required arguments. -
Use `command -v` to detect dependencies. - Keep scripts narrowly scoped
and reviewed; never use `eval`. These scripts are reusable helper
procedures, not a fixed catalogue that limits the model's command
planning. - Use timeouts for commands that may hang. - Return meaningful
exit codes. - Send diagnostics to standard error and structured results
to standard output where useful. - Do not silently invoke `sudo`. - Do
not suppress all errors. - Make scripts idempotent where practical. -
Back up files before changing them.

A script should not assume it is running from the project root. Resolve
its own location or receive explicit paths.

------------------------------------------------------------------------

## 12. User Interface: CLI (Version 1)

Version 1 provides a **CLI only**. The CLI is the sole user-facing
interface in the initial implementation. A local web UI is explicitly
deferred to a future phase; do not add FastAPI, frontend code,
browser-based approvals, or web-server deployment to Version 1.

### 12.1 Command-line interface

The CLI should provide: - Interactive natural-language prompt loop. -
Direct commands for common deterministic operations. - `help`, `exit`,
and `quit`. - Clear progress updates for investigation, research,
planning, execution, and verification. - Readable tables and structured
command output. - Approval prompts before system modifications. - Strong
typed confirmation for destructive or privileged operations. - Access to
task history and generated reports. - Useful errors for gateway,
permission, network, and dependency failures.

The CLI should remain useful for deterministic local diagnostics even
when the API gateway is unavailable, where the relevant direct tools do
not require the model.

### 12.2 Future addition: local web interface

When the web UI is implemented in a later version, it may provide: - A
chat-style natural-language interface for both operating modes. - A
system overview showing detected OS, hardware, storage, network, and
relevant software. - Task progress with visible investigation, research,
plan, execution, and verification stages. - Installation-method
comparisons and selectable options. - A reviewable action plan before
changes are made. - In-browser approval and typed confirmation flows for
state-changing operations. - Live or refreshable command output, with
secrets redacted. - Task history, logs, and downloadable reports. -
Clear display of errors, warnings, and verification results.

The future web UI should reuse the same backend services and support the
same provisioning and administration workflows as the CLI. It is not a
Version 1 deliverable.

### 12.3 Architecture prepared for a future web UI

The CLI calls the shared application service and policy-controlled
execution path. A future web UI must call these same services rather
than implementing its own system-operation logic:

``` text
                 CLI                         Browser UI
                  |                              |
                  v                              v
          CLI interface layer            FastAPI web layer
                  |                              |
                  +--------------+---------------+
                                 |
                                 v
                       Shared agent service
                                 |
                                 v
                    Orchestrator and planner
                                 |
                    +------------+------------+
                    |                         |
                    v                         v
              Tool registry             Policy engine
                    |                         |
                    +------------+------------+
                                 |
                                 v
                     Controlled execution layer
                                 |
                                 v
                       Linux tools and scripts
```

Keep the core agent independent of CLI-specific input and output. A
future web interface must not duplicate agent logic, command execution,
or permission rules; all approvals must still be validated by the
backend and bound to the exact pending action.

### 12.4 Future web UI security requirements

-   Bind the web service to `127.0.0.1` by default.
-   Do not expose a privileged agent to the LAN or internet by default.
-   Never put the gateway API key or other secrets in frontend
    JavaScript.
-   Validate every request on the backend; do not trust browser-supplied
    commands, paths, permissions, or approval state.
-   Protect state-changing endpoints and use secure session handling and
    CSRF protection where applicable.
-   Keep the same local approval policy regardless of which interface
    initiated the task.
-   Make remote access an explicitly configured, separately secured
    feature if it is ever added; it is not enabled by default.

### 12.5 Running Version 1

Provide separate entry points, for example: - `python main.py` --- start
the CLI. - `python -m webapp` --- start the local web service.

Both entry points must load the same configuration and shared agent
services. The user should be able to run either interface independently,
and the architecture should permit both to be active at the same time
without creating competing execution or approval state.

------------------------------------------------------------------------

## 13. Logging, Reports, and Session State

### Logs

Log: - Timestamp - Session identifier - Tool name - Validated arguments,
with sensitive values redacted - Approval decision - Execution
duration - Exit code - Success or failure

Do not log: - API keys - Passwords - Access tokens - Private keys - Full
unfiltered environment variables

### Reports

Use unique timestamped names, for example:

``` text
reports/
├── system-health-2026-11-17-110000.md
├── docker-troubleshooting-2026-11-17-113000.md
└── network-diagnostics-2026-11-17-120000.json
```

### Persistence

Do not introduce a database until there is a concrete need. The first
version can use files for reports and keep conversation state in memory.

SQLite can later store: - Session metadata - Tool execution history -
User-approved preferences - Report indexes

Do not store secrets or raw unrestricted command output in the database.

------------------------------------------------------------------------

## 14. Testing and Verification Strategy

Testing is a core feature of the agent, not an optional final step.
Whenever a task has a meaningful and safe way to check its outcome, the
agent must run the relevant verification commands after execution.

Distinguish two related activities:

-   **Software testing:** Tests for the agent's own Python code,
    adapters, policies, and scripts.
-   **Task verification:** Checks performed on the user's Linux system
    to establish whether the requested task actually succeeded.

### 14.1 Task-level verification is part of every plan

Before executing a modifying task, the planner should define:

1.  The expected end state.
2.  The pre-change baseline.
3.  The exact checks that will establish success.
4.  The expected output or acceptable result for each check.
5.  Whether the check is read-only, state-changing, disruptive, or
    potentially destructive.
6.  What to do if a check fails or cannot be run.

After execution, run applicable checks automatically when they are
read-only and safe. If a verification check itself changes system state,
requires elevated privileges, causes downtime, sends external traffic,
or has a meaningful cost, explain it and obtain approval first.

A successful command exit code alone is not sufficient evidence of task
success. Check the actual resulting state.

### 14.2 Verification examples

  ---------------------------------------------------------------------
  Task                               Example checks (choose based on
                                     detected capabilities)
  ---------------------------------- ----------------------------------
  Install an application             Query package state using the
                                     active package adapter; check
                                     executable path; run `--version`
                                     or a minimal safe functional check

  Install a command-line tool        `command -v <tool>`;
                                     `<tool> --version`; execute a safe
                                     smoke test

  Configure an environment variable  Start a new shell or process with
                                     the expected startup
                                     configuration; print only the
                                     selected variable, with secret
                                     masking

  Configure a service                Query the active service manager;
                                     inspect bounded logs; verify the
                                     expected process, socket, or
                                     health endpoint

  Configure DNS                      Inspect resolver configuration;
                                     run a DNS lookup using an
                                     available resolver utility;
                                     compare with the expected answer

  Configure networking               Inspect interface state, addresses
                                     and routes; test only the intended
                                     connectivity target

  Configure storage                  Check mount table, `findmnt` if
                                     available, filesystem type and
                                     free space; never run destructive
                                     filesystem checks without approval

  Configure a firewall               Inspect active rules using the
                                     detected firewall backend; verify
                                     intended allow/deny behavior
                                     without locking out the user

  Create a project                   Check expected files, directory
                                     structure and permissions; run the
                                     project's safe test/build command
                                     if configured

  Repair a problem                   Repeat the original diagnostic and
                                     compare with the pre-change
                                     baseline
  ---------------------------------------------------------------------

Commands above are examples, not a fixed universal command list. The
agent must select checks using the capability profile and task context.
It must not assume a particular command exists on every Linux
distribution.

### 14.3 Verification outcomes

Every task report must use explicit outcomes:

-   **PASSED:** All required checks completed and met their expected
    conditions.
-   **PARTIALLY PASSED:** Some checks passed, but one or more checks
    failed or could not be completed.
-   **FAILED:** The requested end state was not achieved, or required
    checks demonstrate that it was not achieved.
-   **NOT TESTED:** No safe or meaningful verification was available.
-   **BLOCKED:** Verification could not run because of permissions,
    missing dependencies, network conditions, or another stated blocker.

Never report success solely because an install/configuration command
returned exit code zero. Include command exit codes, concise outputs,
and evidence in the report, with secrets redacted.

### 14.4 Failure handling and recovery

If verification fails:

1.  Do not repeatedly rerun the same modifying command blindly.
2.  Capture relevant diagnostics and compare them with the baseline.
3.  Determine whether the failure is caused by the agent's change,
    environment constraints, or an unrelated pre-existing condition.
4.  Propose a corrective action or rollback plan.
5.  Obtain approval before additional modifications.
6.  Re-run the relevant checks after the corrective action.
7.  Clearly report unresolved issues and avoid claiming completion.

Rollback is not always safe or possible. Do not promise automatic
rollback for operations that cannot be reliably reversed.

### 14.5 Unit tests

Test:

-   Configuration validation.
-   Capability discovery and unknown-field handling.
-   Distribution and adapter selection.
-   Package-manager and service-manager adapter contracts.
-   Tool argument validation.
-   Risk classification and approval behavior.
-   Path traversal rejection.
-   Secret redaction.
-   Timeout and output-size enforcement.
-   Verification outcome classification.
-   Report formatting.

Use fixtures and mocks for `/etc/os-release`, executable discovery,
command outputs, package databases, and service-manager results.

### 14.6 Integration and cross-distribution tests

Test in disposable virtual machines or containers where practical. Build
a compatibility matrix covering representative environments:

-   Debian or Ubuntu (APT/DPKG).
-   Fedora (DNF/RPM).
-   Arch Linux (Pacman).
-   openSUSE (Zypper/RPM).
-   Alpine Linux (APK).
-   A systemd-based host and a non-systemd host such as OpenRC.
-   A minimal environment with missing optional utilities.
-   A container or restricted environment with limited privileges.

Do not claim support for a distribution or adapter until its discovery,
read-only operations, approved modification paths, and verification
behavior have been tested.

Integration tests should include:

-   Read-only system diagnostics.
-   Correct capability detection and adapter selection.
-   Missing package manager and service manager cases.
-   Package query and installation simulation.
-   Service status and log inspection.
-   Environment file backup and update.
-   Project creation in a temporary directory.
-   Failed commands, timeouts, partial output, and unavailable network.
-   Gateway errors and malformed tool-call arguments.
-   Post-action verification success and failure cases.

### 14.7 Safety tests

The following must always be tested:

-   Unknown tools and unsupported capabilities are rejected.
-   Extra or malformed arguments are rejected.
-   A model-provided confirmation flag cannot bypass local approval.
-   Destructive tools do not run when approval is declined.
-   Project creation cannot escape the configured workspace.
-   Existing files are not silently overwritten.
-   Package removal and system cleanup require appropriate approval.
-   Verification does not unexpectedly modify system state.
-   Secrets are redacted from reports and logs.
-   The agent does not run as root by default.
-   A failed adapter probe does not trigger an unsafe guessed fallback.

Use mocks for package installation and destructive operations in unit
tests. Do not test destructive operations against a personal machine.

## 15. Implementation Roadmap

### Phase 1 --- Foundation

Deliverables: - Project skeleton. - Python virtual environment. - `.env`
configuration. - API gateway connectivity check. - CLI prompt loop. -
Tool registry. - Basic logging.

Acceptance criteria: - The agent starts with valid configuration. -
Missing configuration produces a useful error. - The selected model can
complete a simple request. - API failures do not crash the application.

### Phase 2 --- Host discovery and dynamic read-only command execution

Deliverables: - Structured host capability profile. - Package-manager
and service-manager discovery. - systemd/OpenRC and container/VM
context detection. - Generic command-plan schema. - Dynamic command
executor using executable/argument arrays and `shell=False`. - Bounded
stdout/stderr capture, timeout and cancellation handling. - CLI command
preview and execution trace. - A small number of Python context tools
for host facts, research, and reporting.

Acceptance criteria: - The user can ask for a diagnostic outcome in
natural language. - The model decides which read-only commands to
propose. - The executor validates and runs supported commands without
using a shell. - Results are returned to the model for follow-up
commands. - Missing executables and unknown capabilities are handled
safely. - No system-modifying command can run yet.

### Phase 3 --- Command risk, approval, controlled changes, and verification

Deliverables: - Command-aware risk classification. - Local approval
prompts showing exact command steps and impact. - Privilege handling
without running the whole agent as root. - Path and environment
restrictions. - Task success criteria and verification plans. -
Structured verification results.

Acceptance criteria: - Reliably classified read-only operations can run
automatically. - Modifying operations require local approval. - Declined operations never
execute. - Model output cannot bypass policy. - Each applicable
modifying task has a verification plan and reports evidence-based
results.

### Phase 4 --- Environment and development setup

Deliverables: - Managed environment-variable file. - Java, Python,
Node.js and Git detection. - Installation plans and verification. -
Backups for changed configuration files.

Acceptance criteria: - The user sees the proposed changes before
approval. - Existing configuration is preserved. - Installation results
are verified.

### Phase 5 --- Fresh setup and provisioning

Deliverables: - Web research tool for current installation guidance and
source comparison. - Installation option comparison and source
citations. - Provisioning planner and reusable installation profiles. -
Development workstation profile for Java, Python, Node.js, Git and
Docker. - Service verification and setup reports. - Idempotent behavior
and safe handling of pre-existing installations.

Acceptance criteria: - The agent detects existing packages, services,
ports, configuration and data before changes. - It researches current
installation choices and cites authoritative sources. - It presents
package sources, versions, ports and configuration changes. -
Installation and service changes require local approval. - Existing
database data and configuration are never silently overwritten.

### Phase 6 --- Docker and project initialization

Deliverables: - Docker inspection and safe cleanup. - Project
templates. - Workspace restrictions. - Explicit overwrite protection.

Acceptance criteria: - Routine cleanup never prunes volumes. - Project
generation does not overwrite existing files. - Every modifying action
is recorded.

### Phase 7 --- Troubleshooting and reports

Deliverables: - Docker, network, Java, Python, Node and database
troubleshooting profiles. - Markdown and JSON report output. -
Evidence-based recommendations.

Acceptance criteria: - Reports distinguish facts from hypotheses. -
Fixes require approval. - Verification is performed after approved
changes.

### Phase 8 --- Future enhancement: add a local web UI

Deliverables: - Local FastAPI service. - Browser UI. - Reuse of the same
agent, tools and policies.

Acceptance criteria: - Local-only binding by default. - No gateway
secret in the frontend. - State-changing actions retain the same
approval rules.

------------------------------------------------------------------------

## 16. Operational Checklist

Before using the agent regularly:

-   [ ] `.env` is excluded from Git.
-   [ ] `.env` has restrictive permissions.
-   [ ] The gateway endpoint and model are verified.
-   [ ] Tool calling works with the selected model.
-   [ ] The agent runs as a normal user.
-   [ ] Application-level Python tools are registered through an
    allowlist; Linux command steps use the separate command-plan policy.
-   [ ] Model-generated command plans are structured, validated, and
    executed without passing raw command strings to a shell.
-   [ ] All modifying actions require local approval.
-   [ ] Destructive actions have stronger confirmation.
-   [ ] Project creation is confined to the workspace.
-   [ ] Docker volume deletion is disabled by default.
-   [ ] Web research prioritizes current official documentation and
    cites installation sources.
-   [ ] Web content is treated as untrusted reference data and never
    executed directly.
-   [ ] Installation methods are compared and presented before the user
    chooses.
-   [ ] Fresh setup checks existing packages, services, ports,
    configuration and data before changing anything.
-   [ ] database services default to local-only access; remote access
    and firewall changes require separate approval.
-   [ ] application credentials are never hard-coded or written to
    logs/reports.
-   [ ] Existing database data directories are never initialized,
    deleted or replaced automatically.
-   [ ] Logs and reports redact secrets.
-   [ ] Timeouts and output-size limits are enabled.
-   [ ] Unit and safety tests pass.
-   [ ] Capability detection works on each declared supported test
    environment.
-   [ ] Applicable post-task verification checks run and their outcomes
    are reported.
-   [ ] Backups exist for managed configuration files.

------------------------------------------------------------------------

## 17. Definition of Done for Version 1

Version 1 is complete when both operating modes are usable across the
declared supported Linux environments and the user can:

1.  Start the agent from a terminal.
2.  Select or invoke fresh Linux provisioning for a newly installed
    machine.
3.  Use existing Linux administration to investigate and maintain an
    already configured machine.
4.  Connect it to the configured OpenAI-compatible gateway.
5.  Ask for system health diagnostics in natural language.
6.  Inspect Docker status and selected container logs.
7.  Inspect and safely manage user-level environment variables.
8.  Request a development-tool installation plan and approve
    installation.
9.  Provision a fresh Linux development environment from selected
    profiles.
10. Install a fresh local the selected database server or client-only
    tools, with configuration and health verification.
11. Create a supported project template inside the configured workspace.
12. Request a troubleshooting report for a supported problem.
13. Review every proposed modification before it runs.
14. Find setup reports, diagnostic reports, and execution logs locally.
15. See applicable verification checks, their evidence, and an explicit
    PASSED / PARTIALLY PASSED / FAILED / NOT TESTED / BLOCKED outcome.

Fresh workstation provisioning and database setup are part of the
intended Version 1 capability set. Deliver them after the read-only
tools and permission engine are stable. Version 1 is CLI-only. A local
browser-based web UI is a future enhancement and must not delay or
expand the initial implementation.

------------------------------------------------------------------------

## 18. Final Design Recommendation

Build this as a **local-first, capability-based Linux provisioning and
administration agent with two operating modes**:

-   Python owns orchestration, configuration, validation, permissions,
    capability discovery, task verification, and reporting.
-   Bash and other native system tools perform repeatable OS-level
    procedures through controlled, distribution-aware adapters.
-   The OpenAI-compatible gateway provides natural-language
    understanding, planning, and tool selection.
-   The local policy engine, not the LLM, decides whether an operation
    is allowed.
-   Distribution identity is context, not a substitute for capability
    detection. Detect the active package manager, service manager,
    tools, privileges, and environment before selecting an operation.
-   Fresh-machine provisioning is a core capability for user-selected
    applications and development components across supported Linux
    distributions.
-   Web research compares current installation methods and authoritative
    sources before the user chooses an option.
-   Every applicable task defines expected outcomes and verification
    checks. The agent tests the actual resulting system state and
    reports evidence rather than assuming success from command exit
    codes.
-   Version 1 is CLI-only; a local web UI is a future presentation layer
    that reuses the shared agent services.

The most important engineering decisions are to let the LLM dynamically
generate command plans while keeping process execution under local
control. Do not require a predefined command for every user task and do
not pass model-generated text to an unrestricted shell. Validate
structured executable/argument steps, detect capabilities, classify
risk, require local approval for changes, bound execution, inspect each
result, and verify the requested end state. This gives the agent broad
Linux task flexibility without turning a model mistake into an
uncontrolled system change.

------------------------------------------------------------------------

## 17. CLI Interface Plan (Version 1)

### 17.1 Interface goals

Version 1 will use an interactive terminal interface. The CLI is the
primary user-facing layer; it must not contain the agent's core
investigation, planning, policy, execution, or verification logic.

The interface should:

-   Start with a concise host and capability summary.
-   Accept natural-language requests in an interactive session.
-   Show what the agent is investigating and summarize findings.
-   Present a readable action plan before any modification.
-   Clearly show risk, privilege requirements, expected impact, and
    verification steps.
-   Require explicit local approval for changes according to the risk
    policy.
-   Display progress, failures, verification outcomes, and a final
    report.
-   Support both interactive use and non-interactive command-line
    arguments where safe and practical.
-   Remain usable over SSH and in terminals with limited color support.

Use a terminal UI library such as Rich for formatting, tables, status
indicators, and prompts. Keep presentation code separate from the
agent's service layer so a future local web interface can reuse the same
agent functionality.

### 17.2 Startup screen and host summary

Example startup experience:

``` text
$ linux-agent

Linux General-Purpose AI Agent
================================

Host
  Operating system : Linux
  Distribution     : Ubuntu
  Architecture     : x86_64
  Package manager  : APT
  Service manager  : systemd
  Execution mode   : Local CLI

Agent: What would you like to do?

You:
```

The values above are illustrative. At runtime, show only facts actually
detected. Display `Unknown`, `Unavailable`, or `Not detected` when a
capability cannot be established; never infer a capability from the
distribution name alone.

Avoid displaying sensitive host details or a long inventory by default.
Provide a separate inspect command for users who want more detail.

### 17.3 Investigation, plan, and approval experience

Example interaction for an existing-system troubleshooting task:

``` text
You: Investigate why Docker is not starting.

Agent: I'll inspect the Docker installation, service state,
       and recent service logs. I will not change the system
       during this investigation.

[1/3] Checking Docker CLI and installation
[2/3] Checking the detected service manager and Docker service
[3/3] Reading a bounded set of recent service logs

Investigation complete.

Findings
  Docker CLI       : Installed
  Docker service   : Inactive
  Service manager  : systemd
  Likely cause     : Docker daemon is not running

This is an observed service state, not yet proof of the
underlying root cause.

Proposed plan
  1. Start the Docker service.
  2. Enable the service at startup, if requested.
  3. Verify service state and daemon availability.

Expected impact
  - Starts a system service.
  - Enabling startup changes persistent boot configuration.
  - No container, image, or volume data will be intentionally removed.

Risk                 : Medium
Privilege             : Required
Approval              : Required
Verification          : Service state and Docker daemon response

Proceed with this plan? [y/N]
```

The example is illustrative, not a fixed diagnosis or universal
procedure. The actual plan must be based on investigation evidence and
the detected service-manager adapter.

Approval requirements:

-   Default to `No` when the user presses Enter or provides an invalid
    response.
-   Show the exact planned operations and their impact before asking.
-   Do not treat model-generated approval fields as user consent.
-   Obtain approval through a trusted local CLI prompt.
-   Require stronger confirmation for destructive or high-impact
    operations, such as typing a clearly stated confirmation phrase.
-   If the plan changes materially after approval, request approval
    again.
-   Allow the user to cancel before execution and between safe execution
    steps where possible.

Starting a service and enabling it at boot are separate changes. The
agent must not bundle them without explaining the persistent effect and
obtaining approval for both.

### 17.4 Task progress and final result

During execution, show concise progress without exposing secrets or
unbounded command output.

Example:

``` text
Execution
  [1/2] Starting Docker service ............ completed
  [2/2] Enabling startup ................... completed

Verification
  [1/3] Service active ..................... passed
  [2/3] Docker daemon reachable ............ passed
  [3/3] Docker version query ............... passed

Result: PASSED

Summary
  Docker service is running and configured to start at boot.

Changes made
  - Started the Docker service.
  - Enabled the Docker service at startup.

Not changed
  - Containers, images, and volumes were not intentionally modified.

Evidence
  - Service-manager status check passed.
  - Docker daemon query succeeded.
  - Docker version query succeeded.

Task ID: <task-id>
Report: reports/<task-id>.md
```

Use the defined verification outcomes consistently:

-   `PASSED`
-   `PARTIALLY PASSED`
-   `FAILED`
-   `NOT TESTED`
-   `BLOCKED`

Never display `PASSED` merely because the modifying command returned
exit code zero. The verification engine must establish the expected end
state.

### 17.5 Interactive commands

The initial CLI may support these built-in commands:

  Command      Purpose
  ------------ ---------------------------------------------------
  `/help`      Show available interaction commands
  `/inspect`   Display detected host and capability information
  `/plan`      Show or regenerate the current task plan
  `/dry-run`   Show intended operations without applying changes
  `/history`   List recent task summaries
  `/report`    Display the current task report
  `/cancel`    Cancel the current task when safe
  `/exit`      Exit the interactive session

These commands control the CLI session; they are not substitutes for the
typed tools used by the agent to interact with Linux.

### 17.6 Non-interactive CLI entry points

Provide a small initial command-line interface for scripting and
diagnostics:

``` bash
linux-agent
linux-agent --version
linux-agent --inspect
linux-agent --help
```

A later iteration may add request-oriented options such as:

``` bash
linux-agent --request "Investigate why a service is failing"
linux-agent --plan "Prepare this machine for development"
linux-agent --dry-run --request "Install a selected application"
```

Non-interactive execution must not bypass approval requirements. If an
operation requires local confirmation and no trusted approval channel is
available, stop and report that approval is required. Do not add an
unsafe `--yes` or `--force` option that silently bypasses the policy
engine.

### 17.7 CLI presentation rules

-   Use color as an enhancement, not as the only way to communicate
    status. Provide readable labels for color-blind users and plain
    terminals.
-   Distinguish observations, hypotheses, recommendations, and confirmed
    results.
-   Keep normal output concise; provide detailed diagnostics on request.
-   Show bounded and sanitized command output, not raw environment
    dumps.
-   Never display passwords, API keys, tokens, private keys, or other
    secrets.
-   Clearly distinguish planned actions from actions already performed.
-   Display the task ID in reports and error messages for traceability.
-   Handle Ctrl+C gracefully: stop starting new operations, allow the
    current operation to reach a safe interruption point where possible,
    record partial progress, and report the resulting system state.
-   Do not claim that an operation was rolled back unless recovery was
    actually performed and independently verified.

### 17.8 CLI implementation boundary

The CLI should call application services rather than implement agent
logic itself.

``` text
CLI presentation
    |
    v
Application / session service
    |
    v
Agent orchestrator
    |
    +--> Investigator
    +--> Planner
    +--> Policy and approval service
    +--> Controlled executor
    +--> Verification engine
    +--> Task history and reporting
```

The CLI owns input collection, rendering, and trusted local approval
prompts. The application layer owns task state and business logic. Tool
execution and privilege enforcement remain in the controlled execution
and policy layers.

A future local web interface should call the same application services
and must not duplicate or bypass the policy, approval, execution, or
verification logic.

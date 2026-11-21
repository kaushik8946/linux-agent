# linux-agent

A local Python CLI agent that uses an OpenAI-compatible gateway to generate
Linux command plans, which are validated and executed locally without a shell.

See `docs/Linux_Agent_Build_Plan_AI_Command_Execution.md` for the build plan.

## Setup

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e ".[dev]"
    cp .env.example .env && chmod 600 .env

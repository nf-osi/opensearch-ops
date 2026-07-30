#!/usr/bin/env python3
"""Create or update the `orchestrator` coordinator Managed Agent.

Re-run whenever SKILL.md changes, or after re-running goldie's/tuner's own agent_setup.py —
the coordinator's roster is pinned to specific sub-agent versions at creation/update time and
won't pick up newer ones otherwise. By default this UPDATES the coordinator already recorded
in .env in place (client.beta.agents.update — bumps its version, same agent_id, same
environment, re-pinning the roster to goldie's/tuner's current .env versions) rather than
minting a new one. Pass --new the first time (no .env yet) or whenever you deliberately want
a fresh agent_id/environment instead.

Wraps SKILL.md (this agent's instructions) as an Anthropic Managed Agent
configured as a `multiagent` coordinator (`client.beta.agents`, `multiagent`
param) that can delegate to the already-created `goldie` and `tuner`
agents, per
https://platform.claude.com/docs/en/managed-agents/multi-agent .

Run sciops/agents/goldie/agent_setup.py and sciops/agents/tuner/agent_setup.py FIRST — this
script reads their `.env` files for their agent id/version. Nothing else: both specialists
carry their instructions and scripts as Agent Skills, and in a coordinated session each thread
runs with its own agent's configuration (including its own skills). There is no union of mounts
to assemble and no ORCHESTRATOR_SCRIPT_FILE_IDS.

Deliberately repo-agnostic: there is no configured default repo anywhere in
this pipeline. A golden/fields set must be either already mounted (e.g. a
Slack upload) or fetched from a source given explicitly in the request; if
neither, the coordinator says so and stops.

Usage:
    python3 agent_setup.py                # update the coordinator in .env (or create, if none yet)
    python3 agent_setup.py --new          # force a brand-new coordinator + environment
    python3 agent_setup.py --smoke-test <ASK>   # run a session against the agent+environment
                                                 # already recorded in .env — NEVER creates new
                                                 # resources; run with no args first if .env
                                                 # doesn't have one yet

Requires `anthropic>=0.91.0` with the managed-agents beta enabled on the API key. Saves
ORCHESTRATOR_ENV_ID, ORCHESTRATOR_AGENT_ID, ORCHESTRATOR_AGENT_VERSION to .env for
sciops/slacker/slack_bot.py.
"""
import argparse
import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import dotenv_values, load_dotenv, set_key

HERE = Path(__file__).resolve().parent
AGENTS_DIR = HERE.parent
ENV_FILE = HERE / ".env"
SKILL_MD_PATH = HERE / "SKILL.md"

GOLDIE_ENV_FILE = AGENTS_DIR / "goldie" / ".env"
TUNER_ENV_FILE = AGENTS_DIR / "tuner" / ".env"

# Coordination benefits from a smarter model — it does the "what does the
# user actually want, what already exists" reasoning, not just execution.
# Bare string means default effort; Managed Agents runs thinking on by default.
MODEL = os.environ.get("ORCHESTRATOR_AGENT_MODEL", "claude-opus-5")

def _load_sub_agent(env_file: Path, prefix: str) -> dict:
    """Both specialists now carry their payload as Agent Skills, so all this needs is the
    (id, version) pair to pin into the coordinator's roster. In a coordinated session each
    thread runs with its own agent's configuration — including its own skills — so the
    coordinator does not attach or relay the specialists' files at all."""
    values = dotenv_values(env_file)
    required = [f"{prefix}_AGENT_ID", f"{prefix}_AGENT_VERSION"]
    missing = [k for k in required if not values.get(k)]
    if missing:
        raise SystemExit(f"{env_file} missing {missing} — run that agent's own agent_setup.py first")
    return {
        "id": values[f"{prefix}_AGENT_ID"],
        "version": int(values[f"{prefix}_AGENT_VERSION"]),
    }


def build_system_prompt() -> str:
    skill_md = SKILL_MD_PATH.read_text()
    if skill_md.startswith("---"):
        _, _, skill_md = skill_md[3:].partition("---")
    return skill_md.strip() + "\n"


def create_environment(client: Anthropic):
    return client.beta.environments.create(
        name="opensearch-orchestrator-env",
        config={
            "type": "cloud",
            "networking": {"type": "unrestricted"},
            "packages": {"type": "packages", "pip": ["pyyaml"]},
        },
    )


def _agent_kwargs(goldie: dict, tuner: dict) -> dict:
    """Shared by create_agent/update_agent so the two paths can't drift apart."""
    return dict(
        name="opensearch-orchestrator",
        model=MODEL,
        system=build_system_prompt(),
        tools=[
            {
                "type": "agent_toolset_20260401",
                "default_config": {
                    "enabled": True,
                    "permission_policy": {"type": "always_allow"},
                },
                "configs": [
                    {"name": "web_search", "enabled": False},
                    {"name": "web_fetch", "enabled": False},
                ],
            }
        ],
        multiagent={
            "type": "coordinator",
            "agents": [
                {"type": "agent", "id": goldie["id"], "version": goldie["version"]},
                {"type": "agent", "id": tuner["id"], "version": tuner["version"]},
            ],
        },
    )


def create_agent(client: Anthropic, goldie: dict, tuner: dict):
    return client.beta.agents.create(**_agent_kwargs(goldie, tuner))


def update_agent(client: Anthropic, agent_id: str, version: int, goldie: dict, tuner: dict):
    return client.beta.agents.update(agent_id, version=version, **_agent_kwargs(goldie, tuner))


def run_smoke_test(client: Anthropic, env_id: str, agent, ask: str):
    # No `resources=` anywhere in this pipeline any more: each specialist's payload rides in
    # its own Agent Skill, and in a coordinated session every thread runs with its own agent's
    # skills. Only user-supplied Slack attachments are ever mounted (see slacker/slack_bot.py).
    session = client.beta.sessions.create(
        environment_id=env_id,
        agent={"type": "agent", "id": agent.id, "version": agent.version},
        title=f"Smoke test: {ask}",
    )
    client.beta.sessions.events.send(
        session.id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": ask}]}],
    )
    print(f"Session {session.id} running...")
    for ev in client.beta.sessions.events.stream(session.id):
        t = ev.type
        if t == "agent.message":
            for block in ev.content:
                if block.type == "text":
                    print(block.text[:300] + ("..." if len(block.text) > 300 else ""))
        elif t == "session.thread_created":
            print(f"  [delegating -> {ev.agent_name}]")
        elif t in ("agent.tool_use", "agent.mcp_tool_use"):
            print(f"  [{ev.name}]")
        elif t == "session.status_idle":
            break
        elif t == "session.status_terminated":
            raise RuntimeError(
                f"Session terminated before idle. Trace: https://platform.claude.com/sessions/{session.id}"
            )

    outputs = client.beta.files.list(scope_id=session.id, betas=["managed-agents-2026-04-01"])
    for f in outputs.data:
        print("output:", f.filename, f.size_bytes)
    client.beta.sessions.archive(session.id)


def _run_smoke_test_against_existing(client: Anthropic, ask: str):
    """`--smoke-test` NEVER creates resources — it only runs against whatever's already
    recorded in .env, so repeated smoke-testing can't orphan agents/environments (create()
    always mints a brand-new agent_id, even for a duplicate name — see the rename discussion
    in SLACK_AGENT.md/memory). If nothing's recorded yet, run this script with no args first."""
    values = dotenv_values(ENV_FILE)
    required = ["ORCHESTRATOR_ENV_ID", "ORCHESTRATOR_AGENT_ID", "ORCHESTRATOR_AGENT_VERSION",
]
    missing = [k for k in required if not values.get(k)]
    if missing:
        raise SystemExit(f"{ENV_FILE} missing {missing} — run `python3 agent_setup.py` "
                         f"(no args) first to create the coordinator, then re-run --smoke-test")
    agent = client.beta.agents.retrieve(
        values["ORCHESTRATOR_AGENT_ID"], version=int(values["ORCHESTRATOR_AGENT_VERSION"]),
        betas=["managed-agents-2026-04-01"],
    )
    run_smoke_test(client, values["ORCHESTRATOR_ENV_ID"], agent, ask)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", metavar="ASK",
                        help="run a real session against the agent+environment already in "
                             ".env, e.g. 'generate goldens for syn74909065' — never creates "
                             "new resources")
    parser.add_argument("--new", action="store_true",
                        help="force a brand-new coordinator + environment instead of "
                             "updating the one already recorded in .env (the default)")
    args = parser.parse_args()

    load_dotenv(ENV_FILE)
    client = Anthropic()

    if args.smoke_test:
        _run_smoke_test_against_existing(client, args.smoke_test)
        return

    goldie = _load_sub_agent(GOLDIE_ENV_FILE, "GOLDIE")
    tuner = _load_sub_agent(TUNER_ENV_FILE, "TUNER")

    existing = dotenv_values(ENV_FILE)
    has_existing = not args.new and all(
        existing.get(k) for k in
        ("ORCHESTRATOR_ENV_ID", "ORCHESTRATOR_AGENT_ID", "ORCHESTRATOR_AGENT_VERSION")
    )

    if has_existing:
        env_id = existing["ORCHESTRATOR_ENV_ID"]
        old_version = existing["ORCHESTRATOR_AGENT_VERSION"]
        agent = update_agent(client, existing["ORCHESTRATOR_AGENT_ID"], int(old_version), goldie, tuner)
        print(f"Updated coordinator agent {agent.id} v{old_version} -> v{agent.version} "
              f"(environment {env_id} reused; roster: goldie v{goldie['version']}, "
              f"tuner v{tuner['version']})")
    else:
        if args.new and existing.get("ORCHESTRATOR_AGENT_ID"):
            print(f"--new: leaving {existing['ORCHESTRATOR_AGENT_ID']} "
                  f"(env {existing.get('ORCHESTRATOR_ENV_ID')}) as-is — archive it yourself "
                  f"once you've confirmed the new one works.")
        env = create_environment(client)
        print(f"Created environment {env.id}")
        agent = create_agent(client, goldie, tuner)
        print(f"Created coordinator agent {agent.id} v{agent.version} "
              f"(roster: goldie v{goldie['version']}, tuner v{tuner['version']})")
        env_id = env.id

    set_key(str(ENV_FILE), "ORCHESTRATOR_ENV_ID", env_id)
    set_key(str(ENV_FILE), "ORCHESTRATOR_AGENT_ID", agent.id)
    set_key(str(ENV_FILE), "ORCHESTRATOR_AGENT_VERSION", str(agent.version))
    print(f"Saved ORCHESTRATOR_ENV_ID, ORCHESTRATOR_AGENT_ID, ORCHESTRATOR_AGENT_VERSION, "
          f"to {ENV_FILE}")


if __name__ == "__main__":
    main()

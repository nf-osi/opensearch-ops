#!/usr/bin/env python3
"""Create or update the `goldie` Managed Agent + environment.

Re-run whenever SKILL.md or the vendored scripts change. By default this UPDATES the agent
already recorded in .env in place (client.beta.agents.update — bumps its version, same
agent_id, same environment) rather than minting a new one — agent_id is not something
anything downstream should have to re-learn every redeploy. Pass --new the first time (no
.env yet) or whenever you deliberately want a fresh agent_id/environment instead (e.g. a
breaking change you don't want live sessions on the old one to see) — client.beta.agents
has no way to move an existing agent_id to a new environment_id, so --new creates both.

Creates an Anthropic Managed Agent (`client.beta.agents`) in a persistent sandboxed
`environment` (`client.beta.environments`). SKILL.md and the Python scripts are NOT inlined
or mounted here — they ship as a custom Agent Skill built by `skill_setup.py`, which must run
first. That keeps everything goldie needs inside the agent version, so nothing that starts a
session has to know which files goldie depends on.

Usage:
    python3 skill_setup.py            # FIRST — build/version the skill
    python3 agent_setup.py            # update the agent in .env (or create, if none yet)
    python3 agent_setup.py --new      # force a brand-new agent + environment
    python3 agent_setup.py --smoke-test <SEARCH_INDEX_ID>   # run a real session against the
                                       # agent+environment already recorded in .env — NEVER
                                       # creates new resources; run with no args first if
                                       # .env doesn't have one yet

Requires `anthropic>=0.91.0` with the managed-agents beta enabled on the API key. Saves
GOLDIE_ENV_ID, GOLDIE_AGENT_ID, GOLDIE_AGENT_VERSION to .env. GOLDIE_SCRIPT_FILE_IDS is
gone — there are no session file mounts to propagate any more.
"""
import argparse
import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import dotenv_values, load_dotenv, set_key

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
SKILL_MD_PATH = HERE / "SKILL.md"

OUTPUT_DIR = "/mnt/session/outputs"

MODEL = os.environ.get("GOLDIE_AGENT_MODEL", "claude-sonnet-5")


def _load_skill() -> dict:
    """The skill carries SKILL.md AND the helper scripts, so it must exist before the agent
    can reference it. Created by skill_setup.py, which writes these three keys."""
    values = dotenv_values(ENV_FILE)
    required = ["GOLDIE_SKILL_ID", "GOLDIE_SKILL_VERSION", "GOLDIE_SKILL_DIR"]
    missing = [k for k in required if not values.get(k)]
    if missing:
        raise SystemExit(
            f"{ENV_FILE} missing {missing} — run `python3 skill_setup.py` first; the agent "
            f"references its instructions and scripts as a Skill, not as session file mounts."
        )
    return {
        "id": values["GOLDIE_SKILL_ID"],
        "version": values["GOLDIE_SKILL_VERSION"],
        "directory": values["GOLDIE_SKILL_DIR"],
    }


def build_system_prompt(skill: dict) -> str:
    """Deliberately short. SKILL.md is no longer inlined here — it ships inside the skill and
    loads on demand (progressive disclosure), so the ~400 lines of procedure cost context only
    once the agent actually starts a golden-set task. What stays is the deployment-specific
    context the skill text can't know."""
    return f"""\
You generate benchmark golden relevance cases for Synapse SearchIndex tables.

Your full procedure lives in the `goldie` skill. Read `{skill['directory']}/SKILL.md`
before doing anything else and follow it exactly — it is the authoritative spec for the
`golden.yaml` format, the profiling steps, and the case-selection rules. The helper scripts
it references sit in that same directory. If that relative path does not resolve, locate it
once with `find / -name SKILL.md -path '*{skill['directory']}*' 2>/dev/null | head -1` and
use its directory throughout.

Deployment-specific notes the skill text can't know:

- Write your outputs to `{OUTPUT_DIR}/` — they are collected from there and posted back.
- No Synapse token is configured — anonymous access only. Only proceed with public
  tables/indexes. If a table needs a token to query, say so in your final message and stop.
- Your final message becomes what gets posted back to the Slack thread — confirm with a line
  like "Saved: golden.yaml, README.md" plus your usual case-count / topical vs known-item
  summary (see the skill's "Step 5").
"""


def create_environment(client: Anthropic):
    return client.beta.environments.create(
        name="opensearch-goldie-env",
        config={
            "type": "cloud",
            "networking": {"type": "unrestricted"},  # needs repo-prod.prod.sagebase.org
            "packages": {"type": "packages", "pip": ["pyyaml"]},
        },
    )


def _agent_kwargs(skill: dict) -> dict:
    """Shared by create_agent/update_agent so the two paths can't drift apart."""
    return dict(
        name="opensearch-goldie",
        model=MODEL,
        system=build_system_prompt(skill),
        skills=[{
            "type": "custom",
            "skill_id": skill["id"],
            # Pinned, not "latest": the agent version and the skill version should move
            # together, so a skill_setup.py run can't silently change a pinned agent's
            # behaviour. Re-run agent_setup.py to adopt a new skill version.
            "version": skill["version"],
        }],
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
    )


def create_agent(client: Anthropic, skill: dict):
    return client.beta.agents.create(**_agent_kwargs(skill))


def update_agent(client: Anthropic, agent_id: str, version: int, skill: dict):
    return client.beta.agents.update(agent_id, version=version, **_agent_kwargs(skill))


def run_smoke_test(client: Anthropic, env_id: str, agent, index_id: str):
    # No `resources=` — the scripts arrive with the skill, which is part of the agent
    # version. This is the whole point of the change: nothing that starts a session needs
    # to know what files goldie depends on.
    session = client.beta.sessions.create(
        environment_id=env_id,
        agent={"type": "agent", "id": agent.id, "version": agent.version},
        title=f"Smoke test: {index_id}",
    )
    prompt = (
        f"Generate 5 golden relevance cases for SearchIndex {index_id}. "
        f"Follow the skill instructions exactly."
    )
    client.beta.sessions.events.send(
        session.id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": prompt}]}],
    )
    print(f"Session {session.id} running...")
    for ev in client.beta.sessions.events.stream(session.id):
        t = ev.type
        if t == "agent.message":
            for block in ev.content:
                if block.type == "text":
                    print(block.text[:300] + ("..." if len(block.text) > 300 else ""))
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


def _run_smoke_test_against_existing(client: Anthropic, index_id: str):
    """`--smoke-test` NEVER creates resources — it only runs against whatever's already
    recorded in .env, so repeated smoke-testing can't orphan agents/environments (create()
    always mints a brand-new agent_id, even for a duplicate name). If nothing's recorded
    yet, run this script with no args first."""
    values = dotenv_values(ENV_FILE)
    required = ["GOLDIE_ENV_ID", "GOLDIE_AGENT_ID", "GOLDIE_AGENT_VERSION"]
    missing = [k for k in required if not values.get(k)]
    if missing:
        raise SystemExit(f"{ENV_FILE} missing {missing} — run `python3 agent_setup.py` "
                         f"(no args) first to create the agent, then re-run --smoke-test")
    agent = client.beta.agents.retrieve(
        values["GOLDIE_AGENT_ID"], version=int(values["GOLDIE_AGENT_VERSION"]),
        betas=["managed-agents-2026-04-01"],
    )
    run_smoke_test(client, values["GOLDIE_ENV_ID"], agent, index_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", metavar="SEARCH_INDEX_ID",
                        help="run a real session against the agent+environment already in "
                             ".env — never creates new resources")
    parser.add_argument("--new", action="store_true",
                        help="force a brand-new agent + environment instead of updating the "
                             "one already recorded in .env (the default)")
    args = parser.parse_args()

    load_dotenv(ENV_FILE)
    client = Anthropic()

    if args.smoke_test:
        _run_smoke_test_against_existing(client, args.smoke_test)
        return

    existing = dotenv_values(ENV_FILE)
    has_existing = not args.new and all(
        existing.get(k) for k in ("GOLDIE_ENV_ID", "GOLDIE_AGENT_ID", "GOLDIE_AGENT_VERSION")
    )

    skill = _load_skill()
    print(f"Attaching skill {skill['id']} v{skill['version']} (dir {skill['directory']}/)")

    if has_existing:
        env_id = existing["GOLDIE_ENV_ID"]
        old_version = existing["GOLDIE_AGENT_VERSION"]
        agent = update_agent(client, existing["GOLDIE_AGENT_ID"], int(old_version), skill)
        print(f"Updated agent {agent.id} v{old_version} -> v{agent.version} (environment {env_id} reused)")
    else:
        if args.new and existing.get("GOLDIE_AGENT_ID"):
            print(f"--new: leaving {existing['GOLDIE_AGENT_ID']} (env {existing.get('GOLDIE_ENV_ID')}) "
                  f"as-is — archive it yourself once you've confirmed the new one works.")
        env = create_environment(client)
        print(f"Created environment {env.id}")
        agent = create_agent(client, skill)
        print(f"Created agent {agent.id} v{agent.version}")
        env_id = env.id

    set_key(str(ENV_FILE), "GOLDIE_ENV_ID", env_id)
    set_key(str(ENV_FILE), "GOLDIE_AGENT_ID", agent.id)
    set_key(str(ENV_FILE), "GOLDIE_AGENT_VERSION", str(agent.version))
    print(f"Saved GOLDIE_ENV_ID, GOLDIE_AGENT_ID, GOLDIE_AGENT_VERSION to {ENV_FILE}")


if __name__ == "__main__":
    main()

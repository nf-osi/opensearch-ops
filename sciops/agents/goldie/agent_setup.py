#!/usr/bin/env python3
"""Create or update the `goldie` Managed Agent + environment.

Re-run whenever the model, tools, or environment change — or after `sciops/publish_skill.py goldie`
publishes a new skill version, since the skill version is pinned here. By default this UPDATES the agent
already recorded in .env in place (client.beta.agents.update — bumps its version, same
agent_id, same environment) rather than minting a new one — agent_id is not something
anything downstream should have to re-learn every redeploy. Pass --new the first time (no
.env yet) or whenever you deliberately want a fresh agent_id/environment instead (e.g. a
breaking change you don't want live sessions on the old one to see) — client.beta.agents
has no way to move an existing agent_id to a new environment_id, so --new creates both.

Creates an Anthropic Managed Agent (`client.beta.agents`) in a persistent sandboxed
`environment` (`client.beta.environments`). SKILL.md and the Python scripts are NOT inlined
or mounted here — they ship as a custom Agent Skill built by `sciops/publish_skill.py`, which must run
first. That keeps everything goldie needs inside the agent version, so nothing that starts a
session has to know which files goldie depends on.

Usage:
    python3 ../../../sciops/publish_skill.py goldie   # FIRST — publish the skill
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
# SKILL.md and the skill-only assets now live in .claude/skills/goldie/ and are published by
# sciops/publish_skill.py — nothing here reads them.

OUTPUT_DIR = "/mnt/session/outputs"

MODEL = os.environ.get("GOLDIE_AGENT_MODEL", "claude-sonnet-5")


# (skill name, .env holding its ids, key prefix). One entry, and that's the whole dependency:
# the goldie skill carries SKILL.md plus every script it needs (the profiling trio and
# validate_golden.py), so there is no second skill to attach and no shared skill-local .env.
SKILL_SOURCES = [("goldie", ENV_FILE, "GOLDIE")]


def _load_skills() -> list:
    """Read each required skill's published ids. Built by `sciops/publish_skill.py <name>`."""
    skills = []
    for name, env_file, prefix in SKILL_SOURCES:
        values = dotenv_values(env_file)
        required = [f"{prefix}_SKILL_ID", f"{prefix}_SKILL_VERSION", f"{prefix}_SKILL_DIR"]
        missing = [k for k in required if not values.get(k)]
        if missing:
            raise SystemExit(
                f"{env_file} missing {missing} — run `python3 sciops/publish_skill.py {name}` (from the"
                f" repo root) first. The agent references its instructions and scripts as Skills, "
                f"not as session file mounts."
            )
        skills.append({
            "name": name,
            "id": values[f"{prefix}_SKILL_ID"],
            "version": values[f"{prefix}_SKILL_VERSION"],
            "directory": values[f"{prefix}_SKILL_DIR"],
        })
    return skills


def build_system_prompt(skills: list) -> str:
    """Deliberately short. SKILL.md is no longer inlined here — it ships inside the skill and
    loads on demand (progressive disclosure), so the ~400 lines of procedure cost context only
    once the agent actually starts a golden-set task. What stays is the deployment-specific
    context the skill text can't know."""
    goldie = {s["name"]: s for s in skills}["goldie"]
    return f"""\
You generate benchmark golden relevance cases for Synapse SearchIndex tables.

Your full procedure lives in the `goldie` skill. Read `{goldie['directory']}/SKILL.md`
before doing anything else and follow it exactly — it is the authoritative spec for the
`golden.yaml` format, the profiling steps, and the case-selection rules.

Every script it names — `profile_index.py`, `profile_table.py`, `synapse_client.py`,
`validate_golden.py` — is in that same skill at `{goldie['directory']}/scripts/`. If a
relative path in SKILL.md does not resolve, locate it once with
`find / -name profile_table.py 2>/dev/null | head -1` and use its directory throughout.

Deployment-specific notes the skill text can't know:

- `<OUT_DIR>` = `{OUTPUT_DIR}` — substitute it wherever the skill says `<OUT_DIR>`. Write
  your outputs there; they are collected from it and posted back.
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


def _agent_kwargs(skills: list) -> dict:
    """Shared by create_agent/update_agent so the two paths can't drift apart."""
    return dict(
        name="opensearch-goldie",
        model=MODEL,
        system=build_system_prompt(skills),
        # Pinned versions, not "latest": the agent version and its skill versions should move
        # together, so republishing a skill can't silently change a pinned agent's behaviour.
        # Re-run this script to adopt new skill versions.
        skills=[
            {"type": "custom", "skill_id": s["id"], "version": s["version"]}
            for s in skills
        ],
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


def create_agent(client: Anthropic, skills: list):
    return client.beta.agents.create(**_agent_kwargs(skills))


def update_agent(client: Anthropic, agent_id: str, version: int, skills: list):
    return client.beta.agents.update(agent_id, version=version, **_agent_kwargs(skills))


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

    skills = _load_skills()
    for s_ in skills:
        print(f"Attaching skill {s_['name']}: {s_['id']} v{s_['version']} (dir {s_['directory']}/)")

    if has_existing:
        env_id = existing["GOLDIE_ENV_ID"]
        old_version = existing["GOLDIE_AGENT_VERSION"]
        agent = update_agent(client, existing["GOLDIE_AGENT_ID"], int(old_version), skills)
        print(f"Updated agent {agent.id} v{old_version} -> v{agent.version} (environment {env_id} reused)")
    else:
        if args.new and existing.get("GOLDIE_AGENT_ID"):
            print(f"--new: leaving {existing['GOLDIE_AGENT_ID']} (env {existing.get('GOLDIE_ENV_ID')}) "
                  f"as-is — archive it yourself once you've confirmed the new one works.")
        env = create_environment(client)
        print(f"Created environment {env.id}")
        agent = create_agent(client, skills)
        print(f"Created agent {agent.id} v{agent.version}")
        env_id = env.id

    set_key(str(ENV_FILE), "GOLDIE_ENV_ID", env_id)
    set_key(str(ENV_FILE), "GOLDIE_AGENT_ID", agent.id)
    set_key(str(ENV_FILE), "GOLDIE_AGENT_VERSION", str(agent.version))
    print(f"Saved GOLDIE_ENV_ID, GOLDIE_AGENT_ID, GOLDIE_AGENT_VERSION to {ENV_FILE}")


if __name__ == "__main__":
    main()

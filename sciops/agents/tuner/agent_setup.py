#!/usr/bin/env python3
"""Create or update the `tuner` Managed Agent + environment.

Re-run whenever SKILL.md or the vendored harness scripts change. By default this UPDATES
the agent already recorded in .env in place (client.beta.agents.update — bumps its version,
same agent_id, same environment) rather than minting a new one. Pass --new the first time
(no .env yet) or whenever you deliberately want a fresh agent_id/environment instead.

Wraps the `tuner` Agent Skill (SKILL.md + the harness, published by sciops/publish_skill.py) as an
Anthropic Managed Agent (`client.beta.agents`) running in a persistent
sandboxed `environment` (`client.beta.environments`), per the pattern in
https://platform.claude.com/cookbook/managed-agents-slack-data-bot .

Self-contained, like `goldie`, AND no nested credential: this agent IS the
proposal step (reads `<OUT_DIR>/<table>/round_context.json`, authors
`candidates.json` itself using its own reasoning). The harness makes no
Anthropic API call of its own — it only has `init`/`add-candidates`/`finalize`,
all driven by the calling agent (see .claude/skills/tuner/README.md) — so no
ANTHROPIC_API_KEY ever needs to exist in this sandbox.

The per-table `benchmark/<table>/golden.yaml` is NOT uploaded by slacker
either, and this agent has no configured default repo to fetch it from
(deliberately repo-agnostic) — it only proceeds if it's already present (e.g.
a coordinating agent placed it there first, or it was uploaded in Slack) or a
source is given explicitly in the request (see SKILL.md); otherwise it says
so and stops. `fields.yaml` (an existing search strategy) is optional and
handled the same way if present — but if it's missing, the harness bootstraps
a starting one itself by profiling the index (see `tune.py`'s `load_table`),
so it's never a reason to stop.

Usage:
    python3 agent_setup.py            # update the agent in .env (or create, if none yet)
    python3 agent_setup.py --new      # force a brand-new agent + environment
    python3 agent_setup.py --smoke-test <TABLE>   # run a real session against the
                                       # agent+environment already recorded in .env — NEVER
                                       # creates new resources; run with no args first if
                                       # .env doesn't have one yet (fetches <TABLE>'s golden
                                       # itself, bootstraps fields.yaml if it doesn't have one)

Requires `anthropic>=0.91.0` with the managed-agents beta enabled on the API key. Saves
TUNER_ENV_ID, TUNER_AGENT_ID, TUNER_AGENT_VERSION to .env. TUNER_SCRIPT_FILE_IDS is gone —
the harness ships as an Agent Skill (run `sciops/publish_skill.py tuner` first), so there are no
session file mounts for the bot to propagate.
"""
import argparse
import json
import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import dotenv_values, load_dotenv, set_key

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent  # sciops/agents/tuner -> sciops/agents -> sciops -> repo root
ENV_FILE = HERE / ".env"

OUTPUT_DIR = "/mnt/session/outputs"
# The writable directories this deployment gives the harness — the skill bundle is mounted
# read-only, so tune.py cannot write beside its own code. These are passed to it as
# --bench/--out via the system prompt, NOT baked into SKILL.md, so the skill stays reusable.
# BENCH_DIR is also where the orchestrator drops goldie's golden.yaml for the handoff, so it
# must match agents/orchestrator/SKILL.md.
BENCH_DIR = "/mnt/session/work/benchmark"
OUT_DIR = "/mnt/session/work/runs"

# (skill name, .env holding its ids, key prefix). tuner attaches only its own skill, and that
# is the whole dependency: the harness carries its own Synapse client and index profiler, so
# there is no second skill to attach and nothing vendored from one.
SKILL_SOURCES = [("tuner", ENV_FILE, "TUNER")]   # must match publish_skill.SKILLS["tuner"].env_file


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
                f" repo root) first. The agent carries its harness as a Skill, not session mounts."
            )
        skills.append({"name": name, "id": values[f"{prefix}_SKILL_ID"],
                       "version": values[f"{prefix}_SKILL_VERSION"],
                       "directory": values[f"{prefix}_SKILL_DIR"]})
    return skills


MODEL = os.environ.get("TUNER_AGENT_MODEL", "claude-sonnet-5")


def build_system_prompt(skills: list) -> str:
    """Short by design — SKILL.md ships inside the skill and loads on demand, so its procedure
    costs context only once a tuning task actually starts.

    Everything deployment-specific lives HERE, not in SKILL.md: the skill documents that the
    harness takes --bench/--out and never writes beside its own code, and this prompt supplies
    the concrete directories for this deployment. That keeps the skill reusable anywhere."""
    tuner = {s["name"]: s for s in skills}["tuner"]
    return f"""\
You recommend better query-time search configurations for a benchmark table.

Your full procedure lives in the `tuner` skill. Read `{tuner['directory']}/SKILL.md` before
doing anything else and follow it exactly. The harness is at `{tuner['directory']}/scripts/`,
mounted read-only. If that path doesn't resolve, locate it once with
`find / -name tune.py -path '*{tuner['directory']}*' 2>/dev/null | head -1`.

This deployment's directories — substitute these wherever the skill says `<BENCH_DIR>` or
`<OUT_DIR>`, and pass them on every `tune.py` command:

- `<BENCH_DIR>` = `{BENCH_DIR}` — where the golden set is read from. A coordinating agent may
  have placed `<table>/golden.yaml` here already; you share its filesystem.
- `<OUT_DIR>` = `{OUT_DIR}` — where run artifacts are written.

Create them before the first command: `mkdir -p {BENCH_DIR} {OUT_DIR}`

Other deployment notes:

- Copy your three deliverables (`leaderboard.json`, `tuned_fields.yaml`, `report.md`) to
  `{OUTPUT_DIR}/` — they are collected from there and posted back.
- Your final message becomes what gets posted back to the Slack thread; always lead with the
  headline metric change (see the skill's reporting section).
"""


def create_environment(client: Anthropic):
    return client.beta.environments.create(
        name="opensearch-tuner-env",
        config={
            "type": "cloud",
            "networking": {"type": "unrestricted"},  # needs repo-prod.prod.sagebase.org + wherever a given source points to
            "packages": {"type": "packages", "pip": ["pyyaml"]},
        },
    )


def _agent_kwargs(skills: list) -> dict:
    """Shared by create_agent/update_agent so the two paths can't drift apart."""
    return dict(
        name="opensearch-tuner",
        model=MODEL,
        system=build_system_prompt(skills),
        # Pinned versions, not "latest", so republishing a skill can't silently change a
        # pinned agent's behaviour. Re-run this script to adopt new skill versions.
        skills=[{"type": "custom", "skill_id": s["id"], "version": s["version"]}
                for s in skills],
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


def run_smoke_test(client: Anthropic, env_id: str, agent, table: str):
    # No `resources=` — the harness arrives with the skill, which is part of the agent version.
    # Note this smoke test provides no golden.yaml, so the agent is expected to say it needs a
    # source (or fetch one it was given) rather than to complete a tuning run.
    session = client.beta.sessions.create(
        environment_id=env_id,
        agent={"type": "agent", "id": agent.id, "version": agent.version},
        title=f"Smoke test: {table}",
    )
    prompt = f"Tune search relevance for the `{table}` benchmark table. Use --max-cases 6 for speed."
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


def _run_smoke_test_against_existing(client: Anthropic, table: str):
    """`--smoke-test` NEVER creates resources — it only runs against whatever's already
    recorded in .env, so repeated smoke-testing can't orphan agents/environments (create()
    always mints a brand-new agent_id, even for a duplicate name). If nothing's recorded
    yet, run this script with no args first."""
    values = dotenv_values(ENV_FILE)
    required = ["TUNER_ENV_ID", "TUNER_AGENT_ID", "TUNER_AGENT_VERSION"]
    missing = [k for k in required if not values.get(k)]
    if missing:
        raise SystemExit(f"{ENV_FILE} missing {missing} — run `python3 agent_setup.py` "
                         f"(no args) first to create the agent, then re-run --smoke-test")
    agent = client.beta.agents.retrieve(
        values["TUNER_AGENT_ID"], version=int(values["TUNER_AGENT_VERSION"]),
        betas=["managed-agents-2026-04-01"],
    )
    run_smoke_test(client, values["TUNER_ENV_ID"], agent, table)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", metavar="TABLE",
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
        existing.get(k) for k in ("TUNER_ENV_ID", "TUNER_AGENT_ID", "TUNER_AGENT_VERSION")
    )


    skills = _load_skills()
    for s_ in skills:
        print(f"Attaching skill {s_['name']}: {s_['id']} v{s_['version']} (dir {s_['directory']}/)")

    if has_existing:
        env_id = existing["TUNER_ENV_ID"]
        old_version = existing["TUNER_AGENT_VERSION"]
        agent = update_agent(client, existing["TUNER_AGENT_ID"], int(old_version), skills)
        print(f"Updated agent {agent.id} v{old_version} -> v{agent.version} (environment {env_id} reused)")
    else:
        if args.new and existing.get("TUNER_AGENT_ID"):
            print(f"--new: leaving {existing['TUNER_AGENT_ID']} (env {existing.get('TUNER_ENV_ID')}) "
                  f"as-is — archive it yourself once you've confirmed the new one works.")
        env = create_environment(client)
        print(f"Created environment {env.id}")
        agent = create_agent(client, skills)
        print(f"Created agent {agent.id} v{agent.version}")
        env_id = env.id

    set_key(str(ENV_FILE), "TUNER_ENV_ID", env_id)
    set_key(str(ENV_FILE), "TUNER_AGENT_ID", agent.id)
    set_key(str(ENV_FILE), "TUNER_AGENT_VERSION", str(agent.version))
    print(f"Saved TUNER_ENV_ID, TUNER_AGENT_ID, TUNER_AGENT_VERSION to {ENV_FILE}")


if __name__ == "__main__":
    main()

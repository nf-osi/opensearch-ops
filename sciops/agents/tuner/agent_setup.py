#!/usr/bin/env python3
"""Create or update the `tuner` Managed Agent + environment.

Re-run whenever SKILL.md or the vendored harness scripts change. By default this UPDATES
the agent already recorded in .env in place (client.beta.agents.update — bumps its version,
same agent_id, same environment) rather than minting a new one. Pass --new the first time
(no .env yet) or whenever you deliberately want a fresh agent_id/environment instead.

Wraps SKILL.md (this agent's instructions) + the sciops/tuning/ harness as an
Anthropic Managed Agent (`client.beta.agents`) running in a persistent
sandboxed `environment` (`client.beta.environments`), per the pattern in
https://platform.claude.com/cookbook/managed-agents-slack-data-bot .

Self-contained, like `goldie`, AND no nested credential: this agent IS the
proposal step (reads `sciops/tuning/<table>/round_context.json`, authors
`candidates.json` itself using its own reasoning) — the harness's
`sciops/tuning/propose.py`, which makes its own separate Anthropic API call, is
deliberately NOT uploaded here; it's for local human-CLI use only
(`tune.py run`, see sciops/tuning/README.md). No ANTHROPIC_API_KEY ever needs to
exist in this sandbox.

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

Requires `anthropic>=0.91.0` with the managed-agents beta enabled on the API
key. Saves TUNER_ENV_ID, TUNER_AGENT_ID, TUNER_AGENT_VERSION, and
TUNER_SCRIPT_FILE_IDS (json map mount_path -> file_id) to .env for
sciops/slacker/slack_bot.py.
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
SKILL_MD_PATH = HERE / "SKILL.md"

# Mirrors the real repo layout so the harness's own relative imports
# (query.py, benchmark/run.py, sciops/tuning/*.py, sciops/agents/goldie/*.py)
# resolve unchanged in the sandbox — relative to MOUNT_ROOT, that is.
#
# IMPORTANT: MOUNT_ROOT is the `mount_path` REQUESTED from the resources API — keep it as
# "/mnt/session/repo" (unchanged). The platform does NOT honor mount_path literally: it
# always nests the file under /mnt/session/uploads/ instead (confirmed empirically via a
# real goldie run's session-events trace: requesting "/mnt/session/skill/x.py" made the
# file land at "/mnt/session/uploads/skill/x.py"). We do NOT know the exact transformation
# rule (e.g. whether requesting ".../uploads/repo/x.py" directly would double-nest to
# ".../uploads/uploads/repo/x.py") — untested, so don't guess by changing this constant.
# SKILL.md's own text (hardcoded, not templated from this constant) separately tells the
# agent the real resolved location: /mnt/session/uploads/repo/... — see its own edits.
MOUNT_ROOT = "/mnt/session/repo"
OUTPUT_DIR = "/mnt/session/outputs"

# (path relative to repo root) to upload once. profile_index.py/profile_table.py/
# synapse_client.py are goldie's — `tune.py init`/`add-candidates` use profile_index.py to
# profile the index for round_context.json, AND `load_table` uses it to bootstrap a
# starting fields.yaml when a table doesn't have one yet. profile_table.py's
# resolve_index_name()/resolve_table_to_index() resolve whatever identifier tuner was given
# (index name / source table id) down to a SearchIndex before any of that (see SKILL.md's
# Inputs section). sciops/tuning/propose.py is deliberately NOT included: it's the
# local-human-CLI-only path with its own Anthropic API call.
STATIC_FILES = [
    "query.py",
    "benchmark/run.py",
    "sciops/tuning/candidate.py",
    "sciops/tuning/evaluate.py",
    "sciops/tuning/optimize.py",
    "sciops/tuning/probe.py",
    "sciops/tuning/tune.py",
    "sciops/agents/goldie/profile_index.py",
    "sciops/agents/goldie/profile_table.py",
    "sciops/agents/goldie/synapse_client.py",
]

MODEL = os.environ.get("TUNER_AGENT_MODEL", "claude-sonnet-5")


def build_system_prompt() -> str:
    skill_md = SKILL_MD_PATH.read_text()
    if skill_md.startswith("---"):
        _, _, skill_md = skill_md[3:].partition("---")
    return skill_md.strip() + "\n"


def create_environment(client: Anthropic):
    return client.beta.environments.create(
        name="opensearch-tuner-env",
        config={
            "type": "cloud",
            "networking": {"type": "unrestricted"},  # needs repo-prod.prod.sagebase.org + wherever a given source points to
            "packages": {"type": "packages", "pip": ["pyyaml"]},
        },
    )


def _agent_kwargs() -> dict:
    """Shared by create_agent/update_agent so the two paths can't drift apart."""
    return dict(
        name="opensearch-tuner",
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
    )


def create_agent(client: Anthropic):
    return client.beta.agents.create(**_agent_kwargs())


def update_agent(client: Anthropic, agent_id: str, version: int):
    return client.beta.agents.update(agent_id, version=version, **_agent_kwargs())


def upload_scripts(client: Anthropic) -> dict:
    """Returns {mount_path: file_id} for the static harness code (excludes
    per-table golden/fields.yaml — the agent fetches those itself)."""
    file_ids = {}
    for rel_path in STATIC_FILES:
        local_path = REPO_ROOT / rel_path
        mount_path = f"{MOUNT_ROOT}/{rel_path}"
        with local_path.open("rb") as f:
            uploaded = client.beta.files.upload(file=(local_path.name, f, "text/x-python"))
        file_ids[mount_path] = uploaded.id
        print(f"Uploaded {rel_path} ({uploaded.size_bytes} bytes) as {uploaded.id} -> {mount_path}")
    return file_ids


def script_resources(file_ids: dict) -> list:
    return [
        {"type": "file", "file_id": fid, "mount_path": path}
        for path, fid in file_ids.items()
    ]


def run_smoke_test(client: Anthropic, env_id: str, agent, file_ids: dict, table: str):
    session = client.beta.sessions.create(
        environment_id=env_id,
        agent={"type": "agent", "id": agent.id, "version": agent.version},
        resources=script_resources(file_ids),
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
    required = ["TUNER_ENV_ID", "TUNER_AGENT_ID", "TUNER_AGENT_VERSION", "TUNER_SCRIPT_FILE_IDS"]
    missing = [k for k in required if not values.get(k)]
    if missing:
        raise SystemExit(f"{ENV_FILE} missing {missing} — run `python3 agent_setup.py` "
                         f"(no args) first to create the agent, then re-run --smoke-test")
    agent = client.beta.agents.retrieve(
        values["TUNER_AGENT_ID"], version=int(values["TUNER_AGENT_VERSION"]),
        betas=["managed-agents-2026-04-01"],
    )
    file_ids = json.loads(values["TUNER_SCRIPT_FILE_IDS"])
    run_smoke_test(client, values["TUNER_ENV_ID"], agent, file_ids, table)


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

    file_ids = upload_scripts(client)

    if has_existing:
        env_id = existing["TUNER_ENV_ID"]
        old_version = existing["TUNER_AGENT_VERSION"]
        agent = update_agent(client, existing["TUNER_AGENT_ID"], int(old_version))
        print(f"Updated agent {agent.id} v{old_version} -> v{agent.version} (environment {env_id} reused)")
    else:
        if args.new and existing.get("TUNER_AGENT_ID"):
            print(f"--new: leaving {existing['TUNER_AGENT_ID']} (env {existing.get('TUNER_ENV_ID')}) "
                  f"as-is — archive it yourself once you've confirmed the new one works.")
        env = create_environment(client)
        print(f"Created environment {env.id}")
        agent = create_agent(client)
        print(f"Created agent {agent.id} v{agent.version}")
        env_id = env.id

    set_key(str(ENV_FILE), "TUNER_ENV_ID", env_id)
    set_key(str(ENV_FILE), "TUNER_AGENT_ID", agent.id)
    set_key(str(ENV_FILE), "TUNER_AGENT_VERSION", str(agent.version))
    set_key(str(ENV_FILE), "TUNER_SCRIPT_FILE_IDS", json.dumps(file_ids))
    print(f"Saved TUNER_ENV_ID, TUNER_AGENT_ID, TUNER_AGENT_VERSION, TUNER_SCRIPT_FILE_IDS to {ENV_FILE}")


if __name__ == "__main__":
    main()

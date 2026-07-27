#!/usr/bin/env python3
"""PROTOTYPE — package goldie's instructions + helper scripts as a custom Agent Skill.

WHY THIS EXISTS: a Skill is *agent configuration* (`skills=[...]` on
client.beta.agents.create), whereas `resources=[{"type": "file", ...}]` is *session state*
that only the sessions.create() caller can supply. That asymmetry is the whole reason
slack_bot.py has to carry ORCHESTRATOR_SCRIPT_FILE_IDS: the orchestrator's instructions can
name a mount path, but naming it doesn't make the file exist. Moving the scripts into a Skill
puts them where the agent version already lives, so the bot supplies nothing.

Run this BEFORE agent_setup.py. Saves GOLDIE_SKILL_ID / GOLDIE_SKILL_VERSION /
GOLDIE_SKILL_DIR to .env, which agent_setup.py reads.

Two things worth knowing vs. the agents API:
  - Skills ARE deletable, unlike agents (which only archive, permanently). Delete every
    version first, then the skill itself, or the delete 400s:
        for v in client.beta.skills.versions.list(skill_id): versions.delete(...)
        client.beta.skills.delete(skill_id)
    So this is cheap to iterate on and cheap to abandon.
  - Custom skills are workspace-wide — every member of the API workspace sees this skill,
    not just this bot.

Usage:
    python3 skill_setup.py          # add a version to the skill in .env, or create the first
    python3 skill_setup.py --new    # force a brand-new skill_id
"""
import argparse
import shutil
import tempfile
from pathlib import Path

from anthropic import Anthropic
from anthropic.lib import files_from_dir
from dotenv import dotenv_values, set_key

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
SKILL_MD_PATH = HERE / "SKILL.md"

# The bundle's top-level directory name. files_from_dir() records paths relative to
# `directory.parent`, so staging into <tmp>/goldie/ uploads "goldie/SKILL.md",
# "goldie/profile_index.py", ... — i.e. this name IS the skill-relative prefix the agent
# uses to reach the scripts, which is why it can be substituted into SKILL.md below
# rather than discovered empirically the way session mount_path had to be.
SKILL_DIR_NAME = "goldie"

# Explicit allowlist, NOT a glob over HERE — this directory also contains .env (Slack and
# Anthropic credentials) and agent_setup.py. A glob would upload both into a
# workspace-visible skill bundle.
SKILL_SCRIPTS = [
    "synapse_client.py",
    "profile_index.py",
    "profile_table.py",
    "validate_golden.py",
]

# SKILL.md's own relative references to its scripts, rewritten to the bundle-relative form.
# No trailing slash: SKILL.md uses both `sciops/agents/goldie/profile_table.py` (command
# paths) and bare `sys.path.insert(0, "sciops/agents/goldie")` (so its inline Python can
# import synapse_client). A slash-terminated prefix silently misses the second form and the
# imports break at runtime.
SKILL_LOCAL_PREFIX = "sciops/agents/goldie"

# Frontmatter keys the Skills API documents: `name` (≤64 chars, lowercase/digits/hyphens, no
# "anthropic"/"claude") and `description` (≤1024 chars, must say what AND when — it's what
# the model matches a request against). goldie's SKILL.md also carries `dependencies:`, which
# is not a documented API field; it's dropped here rather than risk a rejected upload. Its
# content (pyyaml) is already provided by the environment's pip packages in agent_setup.py.
DOCUMENTED_FRONTMATTER = ("name", "description")


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise SystemExit(f"{SKILL_MD_PATH} has no YAML frontmatter — a Skill requires one")
    fm_text, _, body = text[3:].partition("---")
    fm: dict[str, str] = {}
    key = None
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace() and ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fm[key] = value.strip()
        elif key:  # continuation of a wrapped value
            fm[key] += " " + line.strip()
    return fm, body.strip()


def build_bundle(dest: Path) -> dict:
    """Stage the skill directory. Returns the parsed frontmatter for reporting."""
    dest.mkdir(parents=True)
    fm, body = _split_frontmatter(SKILL_MD_PATH.read_text())

    missing = [k for k in DOCUMENTED_FRONTMATTER if not fm.get(k)]
    if missing:
        raise SystemExit(f"{SKILL_MD_PATH} frontmatter missing {missing}")
    if len(fm["description"]) > 1024:
        raise SystemExit(f"description is {len(fm['description'])} chars — the API caps it at 1024")

    # Scripts now sit alongside SKILL.md inside the skill, so the path the agent uses is
    # the bundle-relative one.
    body = body.replace(SKILL_LOCAL_PREFIX, SKILL_DIR_NAME)
    if SKILL_LOCAL_PREFIX in body:  # belt-and-braces; the replace above is total
        raise SystemExit("SKILL.md still references the repo-relative script path")

    kept = "\n".join(f"{k}: {fm[k]}" for k in DOCUMENTED_FRONTMATTER)
    (dest / "SKILL.md").write_text(f"---\n{kept}\n---\n\n{body}\n")

    for name in SKILL_SCRIPTS:
        src = HERE / name
        if not src.is_file():
            raise SystemExit(f"{src} missing — refusing to build an incomplete skill")
        shutil.copy2(src, dest / name)

    dropped = sorted(set(fm) - set(DOCUMENTED_FRONTMATTER))
    if dropped:
        print(f"Dropped undocumented frontmatter keys: {dropped}")
    print(f"Staged {SKILL_DIR_NAME}/ with SKILL.md + {len(SKILL_SCRIPTS)} scripts")
    return fm


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--new", action="store_true",
                        help="force a brand-new skill_id instead of versioning the one in .env")
    args = parser.parse_args()

    client = Anthropic()
    existing = dotenv_values(ENV_FILE)
    skill_id = None if args.new else existing.get("GOLDIE_SKILL_ID")

    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / SKILL_DIR_NAME
        build_bundle(bundle)
        files = files_from_dir(bundle)

        if skill_id:
            version = client.beta.skills.versions.create(skill_id, files=files)
            print(f"Added version {version.version} to skill {skill_id}")
        else:
            skill = client.beta.skills.create(files=files)
            skill_id = skill.id
            # create() returns latest_version; re-read the version object for `directory`.
            version = client.beta.skills.versions.retrieve(
                skill.latest_version, skill_id=skill_id
            )
            print(f"Created skill {skill_id} version {version.version}")

    # The API reports the directory the skill occupies. Trust it over our assumption, but
    # flag a mismatch loudly: SKILL.md's script paths were rewritten using SKILL_DIR_NAME,
    # so a divergence means the bundled instructions point somewhere the files aren't.
    directory = getattr(version, "directory", None) or SKILL_DIR_NAME
    if directory != SKILL_DIR_NAME:
        print(f"WARNING: API reports directory {directory!r}, bundle assumed "
              f"{SKILL_DIR_NAME!r}. SKILL.md's script paths were rewritten to "
              f"{SKILL_DIR_NAME}/ — set SKILL_DIR_NAME to {directory!r} and re-run.")

    set_key(str(ENV_FILE), "GOLDIE_SKILL_ID", skill_id)
    set_key(str(ENV_FILE), "GOLDIE_SKILL_VERSION", str(version.version))
    set_key(str(ENV_FILE), "GOLDIE_SKILL_DIR", directory)
    print(f"Saved GOLDIE_SKILL_ID, GOLDIE_SKILL_VERSION, GOLDIE_SKILL_DIR to {ENV_FILE}")
    print("Next: python3 agent_setup.py   (attaches this skill to the agent)")


if __name__ == "__main__":
    main()

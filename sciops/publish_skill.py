#!/usr/bin/env python3
"""Build and publish a custom Agent Skill.

One driver for every skill. The SKILLS registry below declares what goes in each bundle and
where — deliberately here rather than in the skill directories, so `.claude/skills/<name>/`
holds only what the skill IS (SKILL.md + scripts/) and nothing about how it gets published.

WHY SKILLS AT ALL: a Skill is *agent configuration* (`skills=[...]` on agents.create), while
`resources=[{"type": "file", ...}]` is *session state* only the sessions.create() caller can
supply. That asymmetry is why slack_bot.py used to carry ORCHESTRATOR_SCRIPT_FILE_IDS — an
agent's instructions can name a mount path, but naming it doesn't make the file exist. Skills
move the payload into the agent version, so nothing that starts a session needs to know which
files a specialist depends on. Both specialists are converted and that variable is gone.

Skills live in `.claude/skills/<name>/` — Claude Code picks them up from there directly, with
no upload. This script publishes the same skill to the Managed Agents API for the sciops
agents, translating the local repo-relative paths into bundle-relative ones on the way.

Usage:
    python3 sciops/publish_skill.py goldie           # add a version, or create if new
    python3 sciops/publish_skill.py goldie --new     # force a fresh skill_id
    python3 sciops/publish_skill.py goldie --dry-run # stage and print the bundle; no API calls

Skills are deletable, unlike agents (which only archive, permanently) — but versions first,
or the delete 400s:
    for v in client.beta.skills.versions.list(skill_id): versions.delete(v.version, skill_id=...)
    client.beta.skills.delete(skill_id)
Note custom skills are workspace-wide: everyone in the API workspace sees what you publish.
"""
import argparse
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent      # sciops/ -> repo root
# Skills live in .claude/skills/ so Claude Code discovers and runs them locally with no upload
# (project-scoped skills are filesystem-based). Publishing to the Managed Agents API is a
# separate step — custom skills do NOT sync across surfaces — which is what this script does.
SKILLS_ROOT = ".claude/skills/"
SKILLS_DIR = REPO_ROOT / SKILLS_ROOT

# SKILL.md names its scripts by the path that works LOCALLY, relative to the repo root Claude
# Code runs from (`.claude/skills/goldie/scripts/foo.py`). Stripping that prefix yields the
# bundle-relative form the Managed Agent sees, since in a published bundle the skill directory
# IS the root. Data paths are placeholders the caller substitutes, so they need no rewrite.
REWRITES = [(SKILLS_ROOT, "")]

# Frontmatter keys the Skills API documents. `name` is ≤64 chars, lowercase/digits/hyphens,
# and may not contain "anthropic" or "claude". `description` is ≤1024 chars and must say both
# what the skill does AND when to use it — it's what the model matches a request against, and
# the only part that always occupies context. Anything else (e.g. goldie's `dependencies:`) is
# dropped rather than risking a rejected upload.
DOCUMENTED_FRONTMATTER = ("name", "description")


@dataclass(frozen=True)
class Skill:
    """What to stage for one skill, and where its published ids are recorded.

    `files` is an explicit (source, destination) list rather than "copy scripts/" on purpose —
    see build_bundle() for why a directory is never uploaded in place.
    """
    name: str
    env_file: str            # repo-relative; publish() writes <PREFIX>_SKILL_ID/_VERSION/_DIR here
    env_prefix: str
    files: list              # [(repo-relative source, bundle-relative destination), ...]

    @property
    def skill_md(self) -> str:
        return f"{SKILLS_ROOT}{self.name}/SKILL.md"


def _scripts(skill: str, modules, subdir: str = "") -> list:
    """(source, destination) pairs for modules living flat in the skill's scripts/<subdir>/.

    Both skills keep their modules in ONE flat directory because that is how they find each
    other: each does `sys.path.insert(0, HERE)` then a plain `from <sibling> import ...`. The
    bundle mirrors the repo layout exactly, so the same files run in either place unchanged.
    """
    rel = f"scripts/{subdir}".rstrip("/")
    return [(f"{SKILLS_ROOT}{skill}/{rel}/{m}.py", f"{rel}/{m}.py") for m in modules]


# Every skill is self-contained: no entry stages another skill's scripts, so there is no
# shared source of truth to keep in step and nothing to attach alongside. See
# .claude/skills/README.md for what that duplicates and why it's the trade we picked.
SKILLS = {
    "goldie": Skill(
        name="goldie",
        env_file="sciops/agents/goldie/.env",
        env_prefix="GOLDIE",
        files=_scripts("goldie", (
            "synapse_client",     # repo-prod client; the other three import it as a sibling
            "profile_index",      # index sample → column roles (order-biased, not truth)
            "profile_table",      # source-table SQL → real distributions (the oracle)
            "validate_golden",    # structure + the live id-in-index gate
        )),
    ),
    "tuner": Skill(
        name="tuner",
        env_file="sciops/agents/tuner/.env",
        env_prefix="TUNER",
        files=_scripts("tuner", (
            "candidate", "client", "evaluate", "index_profile", "optimize", "probe", "tune",
        ), subdir="tuning"),
    ),
}


def load_skill(name: str) -> Skill:
    if name not in SKILLS:
        raise SystemExit(f"unknown skill {name!r}. Available: {sorted(SKILLS)}")
    return SKILLS[name]


def split_frontmatter(text: str, source: Path) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise SystemExit(f"{source} has no YAML frontmatter — a Skill requires one")
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
        elif key:                      # continuation of a wrapped value
            fm[key] += " " + line.strip()
    return fm, body.strip()


def build_bundle(m, dest: Path) -> dict:
    """Stage the bundle at `dest` (a directory named m.name). Returns the frontmatter.

    Always stages into a temp dir rather than uploading a source directory in place:
    anthropic.lib.files_from_dir() recurses with NO filtering — no dotfiles skipped, no
    __pycache__ skipped, no size cap — so pointing it at a working directory would publish
    .env and stale .pyc files into a workspace-visible skill. Staging from an explicit
    manifest makes that impossible by construction.
    """
    dest.mkdir(parents=True)

    skill_md = REPO_ROOT / m.skill_md
    if not skill_md.is_file():
        raise SystemExit(f"{skill_md} not found")
    fm, body = split_frontmatter(skill_md.read_text(), skill_md)

    missing = [k for k in DOCUMENTED_FRONTMATTER if not fm.get(k)]
    if missing:
        raise SystemExit(f"{skill_md} frontmatter missing {missing}")
    if fm["name"] != m.name:
        raise SystemExit(
            f"frontmatter name {fm['name']!r} != registry name {m.name!r}; the "
            f"bundle directory and the skill name must agree or SKILL.md's own rewritten "
            f"script paths won't resolve"
        )
    if len(fm["description"]) > 1024:
        raise SystemExit(f"description is {len(fm['description'])} chars; the API caps it at 1024")

    for old, new in REWRITES:
        body = body.replace(old, new)
    for old, _ in REWRITES:
        if old in body:
            raise SystemExit(f"{skill_md} still contains {old!r} after rewriting")

    kept = "\n".join(f"{k}: {fm[k]}" for k in DOCUMENTED_FRONTMATTER)
    (dest / "SKILL.md").write_text(f"---\n{kept}\n---\n\n{body}\n")

    for src_rel, dest_rel in m.files:
        src = REPO_ROOT / src_rel
        if not src.is_file():
            raise SystemExit(f"{src} missing — refusing to build an incomplete skill")
        target = dest / dest_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)

    dropped = sorted(set(fm) - set(DOCUMENTED_FRONTMATTER))
    if dropped:
        print(f"Dropped undocumented frontmatter keys: {dropped}")
    return fm


def publish(m, bundle: Path, new: bool) -> tuple[str, object]:
    from anthropic import Anthropic
    from anthropic.lib import files_from_dir
    from dotenv import dotenv_values

    client = Anthropic()
    env_path = REPO_ROOT / m.env_file
    existing = dotenv_values(env_path)
    skill_id = None if new else existing.get(f"{m.env_prefix}_SKILL_ID")
    files = files_from_dir(bundle)

    if skill_id:
        version = client.beta.skills.versions.create(skill_id, files=files)
        print(f"Added version {version.version} to skill {skill_id}")
    else:
        skill = client.beta.skills.create(files=files)
        skill_id = skill.id
        # create() returns latest_version; re-read the version for its `directory` field.
        version = client.beta.skills.versions.retrieve(skill.latest_version, skill_id=skill_id)
        print(f"Created skill {skill_id} version {version.version}")
    return skill_id, version


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("skill", help=f"skill to publish: {sorted(SKILLS)}")
    parser.add_argument("--new", action="store_true",
                        help="force a brand-new skill_id instead of versioning the one in .env")
    parser.add_argument("--dry-run", action="store_true",
                        help="stage and print the bundle without calling the API")
    args = parser.parse_args()

    m = load_skill(args.skill)

    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / m.name
        fm = build_bundle(m, bundle)
        staged = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file())
        print(f"Staged {m.name}/ ({len(staged)} files, "
              f"description {len(fm['description'])} chars):")
        for rel in staged:
            print(f"  {m.name}/{rel}")

        if args.dry_run:
            print("\n--dry-run: nothing uploaded")
            return

        skill_id, version = publish(m, bundle, args.new)

    # The API reports which directory the skill occupies. Trust it over SKILL_NAME, but flag a
    # mismatch loudly: SKILL.md's script paths were rewritten using SKILL_NAME, so divergence
    # means the bundled instructions point where the files aren't.
    directory = getattr(version, "directory", None) or m.name
    if directory != m.name:
        print(f"WARNING: API reports directory {directory!r} but the bundle assumed "
              f"{m.name!r}; SKILL.md's paths were rewritten to {m.name!r}. "
              f"Reconcile before running the agent.")

    from dotenv import set_key
    env_path = str(REPO_ROOT / m.env_file)
    for key, value in (("SKILL_ID", skill_id),
                       ("SKILL_VERSION", str(version.version)),
                       ("SKILL_DIR", directory)):
        set_key(env_path, f"{m.env_prefix}_{key}", value)
    print(f"Saved {m.env_prefix}_SKILL_ID/_VERSION/_DIR to {env_path}")
    print(f"Next: python3 sciops/agents/<agent>/agent_setup.py to pin the new version")


if __name__ == "__main__":
    main()

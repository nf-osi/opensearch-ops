# Agent Skills

Each specialist's instructions and helper scripts are published as a **custom Agent Skill**.

Every skill is **self-contained**: its bundle holds every script it needs, and no skill
reaches into another's directory. A skill directory holds only what the skill *is* — SKILL.md
and `scripts/`. What goes in each bundle is declared in `sciops/publish_skill.py`'s `SKILLS`
registry, deliberately not in the skill directory, so nothing publishing-related ships with
the skill or clutters what Claude Code loads locally.

```
.claude/skills/
├── goldie/
│   ├── SKILL.md              instructions (source of truth)
│   └── scripts/              flat — each module imports the others as siblings
│       ├── synapse_client.py     repo-prod client; the other three import it
│       ├── profile_index.py      index sample → column roles (order-biased, not truth)
│       ├── profile_table.py      source-table SQL → real distributions (the oracle)
│       └── validate_golden.py    structure + the live id-in-index gate
└── tuner/
    ├── SKILL.md
    └── scripts/tuning/       the tuning harness — SELF-CONTAINED: candidate, client,
                              evaluate, index_profile, optimize, probe, tune. Nothing
                              borrowed from another skill or from the repo tree, and the
                              calling agent is the proposal step (no nested API call).
```

Executable code goes in **`scripts/`** inside the skill, per the Agent Skills layout
convention. SKILL.md links to it by **repo-relative path** (`.claude/skills/goldie/scripts/…`)
so the commands work verbatim when Claude Code runs them from the repo root; the publisher's
`REWRITES = [(".claude/skills/", "")]` strips that prefix at publish time, because in a
published bundle the skill directory *is* the root. Bundles produced from the registry:

```
goldie/                          tuner/
├── SKILL.md                     ├── SKILL.md
└── scripts/                     └── scripts/
    ├── synapse_client.py            └── tuning/*.py
    ├── profile_index.py
    ├── profile_table.py
    └── validate_golden.py
```

```bash
python3 sciops/publish_skill.py goldie --dry-run   # stage + print the bundle, no API calls
python3 sciops/publish_skill.py goldie             # publish; then the agent's agent_setup.py
```

## Constraints

**A skill's modules must be siblings for discovery.** Both skills
resolve imports with `sys.path.insert(0, HERE)` and a plain `from <module> import ...` — no
package, no path search. So a skill's scripts have to land in one flat directory:

- `goldie` is flat under `scripts/` — `profile_index.py`, `profile_table.py`, and
  `validate_golden.py` all do `from synapse_client import ...`, so all four stay together.
- `tuner` is flat under `scripts/tuning/` — its seven modules import each other and nothing
  else, so the tree *above* `tuning/` is irrelevant to them.

A registry entry still declares explicit `(source, destination)` pairs rather than "copy
`scripts/`", so staging never sweeps in a `.env` or a `__pycache__` (see the next section) and
a destination can differ from the source layout if it ever needs to. Sources are
repo-root-relative, so live code has exactly one copy per skill.

## Never point `files_from_dir()` at a working directory

`anthropic.lib.files_from_dir()` recurses with **no filtering at all** — no dotfiles skipped,
no `__pycache__` skipped, no size cap. It uploads whatever it finds and would publish `.env` 
(Slack and Anthropic credentials) and stale `.pyc` files into a skill, 
and **custom skills are workspace-wide** —  visible to everyone in the API workspace. 
`publish_skill.py` therefore always stages into a temp directory from the registry's
explicit file list; there is no code path that uploads a directory in place.

## Adding a skill

1. `mkdir -p .claude/skills/<name>/scripts/`, write `SKILL.md` with `name` + `description` frontmatter
2. (Optional, if publishing) Add a `Skill(...)` entry to `sciops/publish_skill.py`'s `SKILLS` registry, keyed by the
   same name as the frontmatter `name` and the directory: its `env_file` (the agent's `.env`,
   where the published ids get written), its `env_prefix`, and its `files` — use the
   `_scripts()` helper if the modules sit flat under `scripts/`. Keep      y the skill
   self-contained: give it its own copy of whatever it needs rather than reaching into
   another skill.
3. `python3 sciops/publish_skill.py <name> --dry-run`, check the staged tree, then publish. 
  Note: Skills are **deletable**, but delete every
  version first or the delete returns 400.
4. Reference it from the agent: `skills=[{"type": "custom", "skill_id": ..., "version": ...}]`.
   Pin the version rather than using `latest`, so republishing a skill can't silently change a
   pinned agent's behaviour.


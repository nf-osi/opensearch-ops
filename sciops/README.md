## SciOps Architecture

Managed Agents + Slack bot.

Skills live at the **repo root** in `.claude/skills/` (goldie, tuner) so
Claude Code can run them locally with no upload; `sciops/publish_skill.py` uploads the same
skill to the Managed Agents API for the agents below. See `.claude/skills/README.md`.

```
sciops/
├── agents/             agent CONFIG: agent_setup.py + .env per specialist
│   ├── goldie/         agent_setup.py + .env  (attaches goldie)
│   ├── tuner/          agent_setup.py + .env  (attaches tuner)
│   └── orchestrator/   SKILL.md + agent_setup.py + .env  (coordinator)
├── publish_skill.py    the SKILLS registry (what goes in each bundle) + uploader
├── slacker/            the Slack bot itself — slack_bot.py, slack_app_manifest.yaml,
│   │                   requirements.txt, .env, README.md (local setup + usage)
│   └── deploy/         DEPLOY.md (ECS Fargate) + task definition + IAM policy JSON
└── Dockerfile          builds the bot image; build context is sciops/
```

The tuning harness lives in `.claude/skills/tuner/scripts/tuning/` (with its design notes in
`.claude/skills/tuner/README.md`); its run artifacts go to a gitignored `./tuner-runs/`
wherever it's invoked, not to any directory tracked here.

- **Each specialist owns its own Managed Agent**, split into *config* under `agents/<key>/`
  (`agent_setup.py` + `.env`) and *payload* under `.claude/skills/<key>/` (`SKILL.md` + `scripts/`, nothing else —
  what to stage lives in `publish_skill.py`'s `SKILLS` registry).
  Each `agent_setup.py` creates its agent and a sandboxed environment and saves
  `<PREFIX>_ENV_ID`/`<PREFIX>_AGENT_ID`/`<PREFIX>_AGENT_VERSION` to its own `.env`.

  **Both specialists are on the skills model.** First publish the relevant 
  skill(s) with `python3 sciops/publish_skill.py <name>`. Each `agent_setup.py` pins the relevant skill's
  version into its agent version, and each attaches exactly one: its own. Both skills are
  self-contained — goldie owns the profiling scripts outright, tuner carries its own Synapse
  client and index profiler — so there is no shared skill to attach alongside. The only thing
  ever mounted now is a user's Slack attachment. See `.claude/skills/README.md`.

  Worth knowing: a skill bundle is mounted **read-only**, so the tuning harness
  can't write beside its own code. `tune.py` therefore takes `--bench DIR` (where to read
  `<table>/{golden,fields}.yaml`) and `--out DIR` (where to write run artifacts); both default
  to the directory you run it from, and `.claude/skills/tuner/SKILL.md` passes a writable sandbox directory. That
  same directory, `/mnt/session/work/benchmark/<table>/`, is where the orchestrator drops
  goldie's `golden.yaml` for the handoff.

- **`sciops/agents/orchestrator/agent_setup.py`** creates the coordinator: an Agent configured with
  `multiagent: {type: coordinator, agents: [...]}` referencing goldie's and tuner's already-
  created (id, version) pairs — read from their `.env` files, so this step must come after
  the above. It needs nothing else from them: per the multi-agent docs each thread in a
  coordinated session runs with **its own agent's configuration, including its own skills**, so
  there is no union of mounts to assemble. All threads still **share one sandbox filesystem**,
  which is what makes the handoff work — the coordinator copies goldie's `golden.yaml` into
  `/mnt/session/work/benchmark/<table>/` and tuner reads it from there.

- **`sciops/slacker/slack_bot.py`** — long-running Slack Bolt app (Socket Mode, no public URL
  needed). Only talks to the coordinator; needs `ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`,
  `SLACK_APP_TOKEN`, and `ORCHESTRATOR_ENV_ID`/`ORCHESTRATOR_AGENT_ID`/
  `ORCHESTRATOR_AGENT_VERSION`. It reads these **from process
  env first, falling back to `sciops/slacker/.env`** — its own directory, *not*
  `sciops/agents/orchestrator/.env`, so the three `ORCHESTRATOR_*` values must be copied over
  from `sciops/agents/orchestrator/.env` after setup (see "Setup" below). One Managed Agent
  **session per Slack thread** — sub-agent delegation happens in separate "threads" *within*
  that one session, but is relayed to Slack via the session-level (primary thread) event
  stream, so this bot doesn't need to know delegation is happening beyond posting a short
  "delegating to X" note (`session.thread_created` events). Uploads generated files back into
  the thread when the session goes idle. Thread replies continue the same session.
- **`sciops/slacker/slack_app_manifest.yaml`** — Slack app manifest (scopes + event
  subscriptions); create the Slack app from it.
- **`sciops/slacker/README.md`** — local setup + usage for the bot. **`sciops/slacker/deploy/DEPLOY.md`**
  — running it long-lived on ECS Fargate.

## Specialists

- **`goldie`** (`sciops/agents/goldie/`) — generates a benchmark golden relevance set
  (`golden.yaml`/`README.md`) for a Synapse SearchIndex table, given a syn ID. Fully
  self-contained: needs only that syn ID, no live repo files. Anonymous Synapse access only
  (no token wired into the sandbox); only works on public tables/indexes, and is instructed
  to say so and stop rather than guess if a table needs a token.

- **`tuner`** (`sciops/agents/tuner/`) — recommends a better query-time search config for an
  existing `benchmark/<table>/`, proposing entirely new candidate structures each round (not
  just re-weighting the existing config). **The calling agent (tuner itself) IS the proposal step** — 
  it reads `<out>/<table>/round_context.json` (profile, leaderboard, diagnostics, candidate schema)
  and authors `candidates.json` using its own reasoning 
  (see `.claude/skills/tuner/scripts/tuning/tune.py`'s `init`/`add-candidates`/`finalize` subcommands). 
  It fetches the table's `golden.yaml` (required) and `fields.yaml` (optional — a nice-to-have
  existing strategy to compare against) from wherever they're already mounted (e.g. placed
  there by the coordinator) or a source given in the request — deliberately repo-agnostic,
  with no configured default repo to fall back to, so there's no dependency on this bot's own
  checkout or a stale Docker image. If no `fields.yaml` exists, it bootstraps a starting field
  list itself by profiling the live index rather than requiring one be supplied.

- **`orchestrator`** (`sciops/agents/orchestrator/`) — the coordinator both of the above sit behind.
  Determines the table, checks whether a golden set already exists (a Slack upload or a source
  the user gave) before deciding whether `goldie` needs to run first, relays files
  between sub-agent threads over their shared filesystem, and decides whether the user wants
  a golden set, tuning, or both.

## Setup

Set up goldie and tuner first, then the orchestrator (which reads their `.env` files). Each
script resolves its own `.env` relative to itself, so these work from any directory — paths
below are from `sciops/`:

```bash
# Publish the skills first — each agent_setup.py exits with the missing keys if you skip it.
python3 ../publish_skill.py goldie   # or from the repo root:
python3 ../publish_skill.py tuner    #   python3 sciops/publish_skill.py <name>

python3 agents/goldie/agent_setup.py --new
python3 agents/tuner/agent_setup.py --new
python3 agents/orchestrator/agent_setup.py --new
```

Re-publishing a skill does not change a running agent — versions are pinned — so re-run that
specialist's `agent_setup.py` afterwards to adopt the new skill version.

Pass `--new` only on a first run, when there's no `.env` yet — the default (no flag) *updates*
the agent already recorded in `.env` in place, bumping its version. Each saves its agent/env
ids to its own `.env` (gitignored). Optionally smoke-test the orchestrator directly with
`--smoke-test "<ask>"` before wiring up Slack.

Then give the bot its config. `agent_setup.py` writes only to its own directory, so the four
`ORCHESTRATOR_*` values have to reach `sciops/slacker/.env` (which otherwise holds just the
Slack tokens) — copy them in, or export them in the bot's environment:

```bash
cat agents/orchestrator/.env >> slacker/.env
```

`slacker/.env` also needs `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` (from creating the Slack app)
and `ANTHROPIC_API_KEY`, unless those are already exported in the shell that runs the bot —
process env always wins over the file. See `sciops/slacker/README.md` for creating the Slack
app and running it locally.

## Deployment

`sciops/slacker/deploy/DEPLOY.md` has the full runbook. In brief: the bot is a single
long-lived process, so it deploys as **one ECS Fargate task** (`desiredCount=1`, no
horizontal scaling — it holds one stateful Socket Mode WebSocket). Socket Mode is outbound
only, so there's **no ALB, no public URL, and a security group with no inbound rules**.

- `sciops/Dockerfile` builds the image — build context is `sciops/`, so:
  `docker build -f sciops/Dockerfile -t search-sciops-bot sciops` (from the repo root).
- Of the seven required settings, only the three real credentials
  (`ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`) live in Secrets Manager under a
  `search-sciops-bot/` prefix. The four `ORCHESTRATOR_*` ids aren't secret, so they sit in the
  task definition's `environment` block — free, and it keeps the running agent version visible
  in the task definition. ECS injects both as plain env vars; the app makes no AWS API calls,
  so there's an execution role but no task role.
- The service is deployed with `minimumHealthyPercent=0, maximumPercent=100` so a redeploy
  stops the old task before starting the new one — two concurrent Socket Mode connections
  would let Slack deliver a thread reply to the task that doesn't hold that thread's session.
  `deploymentCircuitBreaker` rolls back a task that can't stay up.
- On SIGTERM the bot archives its active sessions and tells those threads it's restarting
  (`stopTimeout: 120` gives it room), so a redeploy doesn't strand open sessions.
- Optional non-secret knobs (`SESSION_INACTIVITY_TIMEOUT_S`, `SESSION_SWEEP_INTERVAL_S`,
  `MAX_CONCURRENT_THREADS`, `SHUTDOWN_GRACE_S`) also go in `environment`.
- Redeploying code is build → push → `aws ecs update-service --force-new-deployment`.
- Sized at `cpu: 256` (the Fargate floor) with `memory: 1024` for file-buffering headroom, so
  running cost is roughly **$16/month** of AWS infrastructure plus per-request Anthropic
  usage — see `sciops/slacker/README.md`'s "Cost".

## Adding a new specialist

1. Create `.claude/skills/<key>/` with a `SKILL.md` + `scripts/`, add an entry to
   `publish_skill.py`'s `SKILLS` registry (see `.claude/skills/README.md`), and
   `agents/<key>/` with an `agent_setup.py` (model on `sciops/agents/goldie/agent_setup.py`)
   that creates the Managed Agent + environment, pins the skill, and saves
   `<PREFIX>_ENV_ID`/`<PREFIX>_AGENT_ID`/`<PREFIX>_AGENT_VERSION` to its own `.env`.
   Publish the skill first: `python3 sciops/publish_skill.py <key>`. Prefer having it fetch any live data itself
   (over the network, like goldie's Synapse calls or tuner's fetch from a source named in the
   request) rather than having this bot upload it — keeps the bot stateless and the Docker
   image slim.
2. Add it to `sciops/agents/orchestrator/agent_setup.py`'s `multiagent.agents` roster and combined
   `script_file_ids`, and mention it in `sciops/agents/orchestrator/SKILL.md` so the coordinator
   knows when to delegate to it.
3. Re-run `sciops/agents/orchestrator/agent_setup.py` (the roster is pinned to specific sub-agent
   versions at creation time), then propagate the new `ORCHESTRATOR_*` values as under
   "Updating an agent". No change to `slack_bot.py` or the Docker image is needed — the
   coordinator only pins (id, version) pairs, and the new specialist's payload rides in its
   own skill.

## Updating an agent

When a specialist's `SKILL.md` or scripts have changed, re-run its own `agent_setup.py` (no
`--new`) — agent versions are immutable, so this creates a new version and updates its `.env`
in place. **Then re-run `sciops/agents/orchestrator/agent_setup.py`** — the coordinator's roster
is snapshotted at creation time and won't pick up the new version otherwise.

Then propagate the new orchestrator version to whichever way the bot is running:

- **Locally** — re-copy the `ORCHESTRATOR_*` lines into `slacker/.env` and restart
  `slack_bot.py`; it reads the ids once at startup. Appending again is safe rather than
  wrong — on a duplicate key python-dotenv takes the last occurrence — but replacing the old
  lines keeps the file readable.
- **On ECS** — update the `search-sciops-bot/orchestrator-*` secrets in Secrets Manager,
  register a new task definition revision, then `update-service` to it
  (`sciops/slacker/deploy/DEPLOY.md`, "Redeploying after a code change"). No image rebuild is
  needed for an agent-only change.

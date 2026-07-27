# Slack front-end for the search-relevance orchestrator

Fronts a single Anthropic **Managed Agent coordinator** (`sciops/agents/orchestrator/`) with a
Slack bot, following https://platform.claude.com/cookbook/managed-agents-slack-data-bot and
https://platform.claude.com/docs/en/managed-agents/multi-agent. The coordinator itself
delegates to specialist sub-agents (`goldie`, `tuner`) as needed; 
the coordinator reads plain language, decides what's needed, and figures out sequencing 
(e.g. generate a golden set first if one doesn't exist yet, then tune).

This doc covers local setup for development; commands below are run from `sciops/slacker/`.
For running it long-lived on ECS Fargate, see `deploy/DEPLOY.md` — the app reads all of the
config below from either `sciops/slacker/.env` (dev) or real environment variables
(production; real env vars always take precedence), so nothing here changes between the two,
only how the values get set.

One implementation detail that matters: the Slack bot does more than forward
the user's text. On every new Managed Agent session it also mounts the static
helper files that `goldie` and `tuner` expect in the sandbox. Those file IDs
are produced by the agents' own `agent_setup.py` scripts and saved through the
orchestrator setup as `ORCHESTRATOR_SCRIPT_FILE_IDS`. Slack-uploaded files
(`golden.yaml`, `fields.yaml`, etc.) are additional per-request resources, not
a replacement for those static mounts.

## One-time setup

This assumes the orchestrator and specialist agents have already been set up in Managed
Agents, per `sciops/README.md`'s "Setup".

1. Install dependencies (ideally in a virtualenv):
   ```bash
   pip install -r requirements.txt
   ```
   Requires an Anthropic API key (`ANTHROPIC_API_KEY`) with the Managed Agents / Agent
   Sessions / multi-agent betas enabled.

2. Point the bot at the coordinator. `agent_setup.py` writes only to its own directory, so
   copy its four ids into this directory's `.env` (or export them):
   ```bash
   cat ../agents/orchestrator/.env >> .env
   ```
   That supplies `ORCHESTRATOR_ENV_ID`, `ORCHESTRATOR_AGENT_ID`,
   `ORCHESTRATOR_AGENT_VERSION`, and `ORCHESTRATOR_SCRIPT_FILE_IDS`. Re-copy whenever the
   orchestrator is re-created or bumped to a new version.

3. Create the Slack app:
   - Go to https://api.slack.com/apps → **Create New App → From a manifest**.
   - Paste the contents of `slack_app_manifest.yaml`.
   - Install the app to your workspace.
   - Under **OAuth & Permissions**, copy and save the **Bot User OAuth Token** (`xoxb-...`) as `SLACK_BOT_TOKEN`.
   - Under **Basic Information → App-Level Tokens**, generate a token with the
     `connections:write` scope (`xapp-...`) and save as `SLACK_APP_TOKEN`.
   - Invite the bot to a channel: `/invite @search-sciops-bot`.

4. Run the bot:
   ```bash
   python3 slack_bot.py
   ```
   On startup it checks for all seven required settings (the three keys/tokens above plus the
   four `ORCHESTRATOR_*` ids) and names the missing one and the `.env` path if any are absent.

## Usage

In a channel the bot is in, ask for help, possibly attaching a `golden.yaml`/`fields.yaml`:

```
@search-sciops-bot pls generate goldens for syn74909065 focused on antibody queries
@search-sciops-bot tune ranking for the tools table
@search-sciops-bot generate goldens for syn74909065 and then tune ranking for it
```

- The coordinator figures out the table, whether a golden set already exists, and what the user
  actually want (just a golden set, just tuning, or both).
- If a file is attached — on the opening `@mention` **or on any later reply in the same
  thread** — it's mounted under `/mnt/session/uploads/` (original filename); the coordinator
  inspects it rather than assuming an exact name. Later-reply mounts use
  `sessions.resources.add()` (https://platform.claude.com/docs/en/managed-agents/files#managing-files-on-a-running-session)
  rather than the `resources` param `sessions.create()` takes, since a file attached mid-thread
  arrives after the session already exists.
- If something's missing or ambiguous (no table given, tuning asked for with no golden set
  anywhere — `fields.yaml` alone is never blocking, `tuner` bootstraps one), it says so or asks.
- User sees a `-> delegating to goldie` / `-> delegating to tuner` note when the coordinator
  hands off, then progress updates, then its summary plus any generated files.
- Reply in the same thread to continue same Managed Agent session 
  (e.g. "now also tune it", "use recall instead of ndcg", or just attach a file).

## After a run

Neither specialist has permissions to write into a real repo, hence outputs are returned in the Slack thread. 
To commit a result:

**From goldie:**
1. Download `golden.yaml` and `README.md` from the thread.
2. Do SME review.
2. Place final approved product under the appropriate location in project-specific repo, such as `benchmark/<table>/` 
(or merge into an existing `golden.yaml` per `sciops/agents/goldie/SKILL.md`'s versioning rules).

**From tuner:**
1. Download `leaderboard.json`, `tuned_fields.yaml`, `report.md` from the thread.
2. Review `report.md`, then `cp tuned_fields.yaml benchmark/<table>/fields.yaml`.
3. Run `python3 benchmark/run.py <table> --label tuned` to confirm on the live index.
4. Note the report's stated data source (pre-supplied / given source) — if it fetched from a
   given repo, that's only as fresh as the last push there.

## Session lifecycle

Per https://platform.claude.com/docs/en/managed-agents/sessions and
https://platform.claude.com/docs/en/managed-agents/session-operations: a session moves
through `idle` (waiting for input — the state it starts and ends each turn in) → `running`
(actively executing) → back to `idle`, or occasionally `rescheduling` (a transient error,
retried automatically). `terminated` only happens on an **unrecoverable error** — the docs
document no automatic idle-timeout or max-lifetime for a session or its sandbox. Sessions
persist **until you explicitly `archive()` or `delete()` them**.

`slack_bot.py` handles this itself: a background sweeper thread
(`_sweep_inactive_sessions`) checks every `SESSION_SWEEP_INTERVAL_S` seconds (default 60)
for threads inactive longer than `SESSION_INACTIVITY_TIMEOUT_S` (default 1800 = 30 minutes)
and archives them (`client.beta.sessions.archive`), then posts "this session has been
stopped ... mention me again to start a new one" to that Slack thread. "Inactive" resets
whenever a message arrives for that thread AND whenever a turn finishes going idle — so a
long-running tuning session isn't penalized for taking a while, only a thread nobody has
touched since the last response. If a session happens to still be `running` right when the
sweep tries to archive it (`archive()` requires `idle`), the attempt is skipped and retried
on the next sweep rather than losing track of it. Tune both env vars per deployment if the
default feels too aggressive or too lax.

On SIGTERM or SIGINT (`Ctrl-C` locally, every redeploy or scale-in on ECS) `_shutdown` does
the same for *all* tracked sessions: it posts an "I'm restarting, so this thread won't carry
over" note to each active thread, then archives their sessions, retrying any that are still
mid-turn (`archive()` requires `idle`) until `SHUTDOWN_GRACE_S` seconds (default 90) elapse.
Anything still not idle at the deadline is logged by session id.

`threads` is still only an in-process map, so a *hard* stop — crash, SIGKILL, Fargate Spot
interruption — bypasses all of the above: those sessions stay open on Anthropic's side until
manually archived, and their Slack threads can't be continued. Idle sessions don't accrue
runtime charges (see "Cost"), so the cost of leaking one is the open sandbox, not billed time.

## Cost

Two independent parts: **fixed AWS infrastructure** (charged 24/7, because Socket Mode needs a
persistently connected process — you pay this even in a month where nobody mentions the bot)
and **variable Anthropic usage** (charged per request).

### AWS, per month

At the task definition's `cpu: 256` / `memory: 1024`, `us-east-1` list prices, 730 h:

| Item | Rate | Monthly |
|---|---|---|
| Fargate vCPU (0.25) | $0.04048 / vCPU-hour | $7.39 |
| Fargate memory (1 GB) | $0.004445 / GB-hour | $3.24 |
| Public IPv4 address (`assignPublicIp=ENABLED`) | $0.005 / hour | $3.65 |
| Secrets Manager (3 secrets) | $0.40 / secret | $1.20 |
| CloudWatch Logs (30-day retention, low volume) | $0.50 / GB ingested | <$0.10 |
| ECR storage (~250 MB image) | $0.10 / GB | ~$0.03 |
| **Total** | | **~$16** |

Two things to know about that number:

- **vCPU is at the Fargate floor; memory deliberately isn't.** 0.25 vCPU is ample for a process
  holding one WebSocket and making HTTP calls, and it's the single biggest line item, so that's
  where the saving is. Memory is provisioned at 1 GB rather than the 512 MB floor for $1.62/month
  more, because the bot buffers whole files in memory (`_download_slack_file`, and
  `files.download(...).read()` when returning results) and `MAX_CONCURRENT_THREADS` allows 25
  threads doing that at once. The files in play are small text ones — `golden.yaml`,
  `report.md`, `leaderboard.json` — so 512 MB would very likely have held, but ECS kills the
  task on OOM and $1.62 is cheap insurance against that failure mode.
- The **public IPv4 charge** is the price of avoiding a NAT Gateway. The private-subnet
  alternative in `deploy/DEPLOY.md` §5 costs ~$32/month for the NAT Gateway alone, so the
  public-subnet setup is ~9× cheaper on networking; only switch if your org forbids public IPs.

### Anthropic, per request

Billed on two dimensions (see `sciops/agents/cost_report.py`, which reconstructs both):

- **Session runtime** — $0.08 per session-hour, counted only while the session is `running`.
  Idle time is free, which is why a 30-minute inactivity window is cheap to leave open and why
  the sweeper exists to bound *sandbox* lifetime rather than to save runtime cost. A 20-minute
  tuning run is about $0.03.
- **Tokens, per model** — the orchestrator runs on `claude-opus-5` ($5/M input, $0.50/M
  cache read, $25/M output) and delegates to goldie and tuner on `claude-sonnet-5` ($2/M
  input, $10/M output through 2026-08-31; $3/M and $15/M after). Tokens dominate the runtime
  charge in practice.

This repo has **no measured per-request figures yet**, and the spread between a small golden-set
generation and a multi-round tuning run is wide enough that a made-up average would mislead.
Get real numbers by running the cost reporter on your first few sessions:

```bash
python3 ../agents/cost_report.py <SESSION_ID> [<SESSION_ID> ...]
```

The session id is printed in the bot's logs, and `--json` emits machine-readable output for
aggregating across sessions. Both that script's rates and the AWS table above are list-price
snapshots as of 2026-07 with no negotiated discounts applied.

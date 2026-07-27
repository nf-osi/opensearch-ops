#!/usr/bin/env python3
"""Slack front-end for the search-relevance orchestrator Managed Agent.

Per https://platform.claude.com/cookbook/managed-agents-slack-data-bot and
https://platform.claude.com/docs/en/managed-agents/multi-agent : this bot
talks to a single coordinator agent (`sciops/agents/orchestrator/`) that itself
delegates to specialist sub-agents (`goldie`, `tuner`) as needed — there is
no separate classify-then-dispatch step here anymore. The coordinator reads
plain language, decides what's needed, and figures out the sequencing
(e.g. generate a golden set first if one doesn't exist yet, then tune).

Setup: run sciops/agents/goldie/agent_setup.py, sciops/agents/tuner/agent_setup.py, then
sciops/agents/orchestrator/agent_setup.py (which reads the first two's ids), then:
  1. Create a Slack app from `slack_app_manifest.yaml`, install it, invite it
     to a channel.
  2. `python3 slack_bot.py` (reads required settings from process env first,
     then falls back to local `.env` in this directory).
  3. In the channel: `@search-sciops-bot <anything, e.g. generate goldens for
     syn74909065 focused on antibodies, then tune ranking for it>`

One Managed Agent session per Slack thread. Sub-agent delegation happens in
separate "threads" within that one session (see the multi-agent docs above)
but is still relayed to Slack via the session-level (primary thread) event
stream, so this file doesn't need to know delegation happened, beyond
optionally noting when it starts. The coordinator writes result files into
its sandbox; this bot uploads them back into the Slack thread once the
session goes idle. Thread replies continue the same session — attachments
work both on the opening `@mention` (mounted via `sessions.create()`'s
`resources` param) and on later replies (mounted via
`sessions.resources.add()`, per
https://platform.claude.com/docs/en/managed-agents/files#managing-files-on-a-running-session
— that endpoint is why file mounts aren't limited to session creation).

Managed Agent sessions have no documented idle-timeout of their own (see
https://platform.claude.com/docs/en/managed-agents/session-operations) —
they persist until explicitly archived. A background sweeper thread here
archives sessions that have gone SESSION_INACTIVITY_TIMEOUT_S seconds
without activity and posts a "this session has stopped, start a new
thread" note, rather than leaving them (and their sandboxes) running
forever. A SIGTERM/SIGINT handler does the same for every tracked session
on the way out, so a redeploy doesn't strand them (see _shutdown).
"""
import io
import json
import os
import signal
import threading
import time
from pathlib import Path

import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from markdown_to_mrkdwn import SlackMarkdownConverter
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"

REQUIRED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "ORCHESTRATOR_ENV_ID",
    "ORCHESTRATOR_AGENT_ID",
    "ORCHESTRATOR_AGENT_VERSION",
    "ORCHESTRATOR_SCRIPT_FILE_IDS",
)


def _load_local_env_if_needed() -> None:
    if all(os.environ.get(key) for key in REQUIRED_ENV_VARS):
        return
    load_dotenv(ENV_FILE, override=False)


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if value:
        return value
    raise RuntimeError(
        f"{key} not set. Export it or add it to {ENV_FILE}."
    )


_load_local_env_if_needed()

ANTHROPIC_API_KEY = _require_env("ANTHROPIC_API_KEY")
SLACK_BOT_TOKEN = _require_env("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = _require_env("SLACK_APP_TOKEN")
ORCHESTRATOR_ENV_ID = _require_env("ORCHESTRATOR_ENV_ID")
ORCHESTRATOR_AGENT_ID = _require_env("ORCHESTRATOR_AGENT_ID")
try:
    ORCHESTRATOR_AGENT_VERSION = int(_require_env("ORCHESTRATOR_AGENT_VERSION"))
except ValueError as e:
    raise RuntimeError(
        f"ORCHESTRATOR_AGENT_VERSION must be an integer in {ENV_FILE} or process env."
    ) from e

try:
    ORCHESTRATOR_SCRIPT_FILE_IDS = json.loads(_require_env("ORCHESTRATOR_SCRIPT_FILE_IDS"))
except json.JSONDecodeError as e:
    raise RuntimeError(
        f"ORCHESTRATOR_SCRIPT_FILE_IDS must be valid JSON in {ENV_FILE} or process env."
    ) from e

client = Anthropic(api_key=ANTHROPIC_API_KEY)
app = App(token=SLACK_BOT_TOKEN)

# `threads` holds active sessions; `pending_threads` holds mentions that have
# reserved a slot and are still creating their session. Both are guarded by
# `_threads_lock` since the sweeper thread and per-message handler threads
# touch them concurrently.
threads: dict[str, dict] = {}
pending_threads: set[str] = set()
_threads_lock = threading.Lock()

MAX_CONCURRENT_THREADS = int(os.environ.get("MAX_CONCURRENT_THREADS", 25))
SESSION_INACTIVITY_TIMEOUT_S = int(os.environ.get("SESSION_INACTIVITY_TIMEOUT_S", 30 * 60))
SESSION_SWEEP_INTERVAL_S = int(os.environ.get("SESSION_SWEEP_INTERVAL_S", 60))
# How long _shutdown may spend archiving sessions before exiting anyway. Keep
# this comfortably under the ECS task definition's stopTimeout (120s), since
# SIGKILL lands there regardless of what we're still doing.
SHUTDOWN_GRACE_S = int(os.environ.get("SHUTDOWN_GRACE_S", 90))

_shutting_down = threading.Event()

mrkdwn = SlackMarkdownConverter()

USAGE = (
    "Tell me what you need, e.g. `@search-sciops-bot generate goldens for "
    "syn74909065 focused on antibodies` or `@search-sciops-bot tune ranking for tools`, "
    "and you can attach a golden.yaml/fields.yaml directly if you have one."
)


@app.event("app_mention")
def on_mention(event, say, ack):
    ack()
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    text = event["text"].split(">", 1)[-1].strip()
    files = event.get("files") or []

    if not text and not files:
        say(text=USAGE, thread_ts=thread_ts)
        return

    if not _reserve_thread_slot(thread_ts):
        say(
            text=(
                f"I already have {MAX_CONCURRENT_THREADS} active requests. "
                "Wait for one to finish or time out before starting a new thread."
            ),
            thread_ts=thread_ts,
        )
        return

    say(text="On it — figuring out what you need...", thread_ts=thread_ts)
    threading.Thread(target=start_job, args=(channel, thread_ts, text, files)).start()


def _download_slack_file(slack_file: dict) -> bytes:
    resp = requests.get(
        slack_file["url_private"],
        headers={"Authorization": f"Bearer {app.client.token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def _upload_to_files_api(slack_file: dict) -> str:
    """Download a Slack attachment and re-upload it to the Anthropic Files
    API. Returns the new file_id (not yet mounted anywhere)."""
    content = _download_slack_file(slack_file)
    mime = slack_file.get("mimetype", "application/octet-stream")
    uploaded = client.beta.files.upload(file=(slack_file["name"], io.BytesIO(content), mime))
    return uploaded.id


def _mount_path_for(slack_file: dict) -> str:
    # Original filename, not exactly golden.yaml/fields.yaml — the
    # orchestrator's own instructions inspect this directory rather than
    # assuming exact names.
    return f"/mnt/session/uploads/{slack_file['name']}"


def _upload_slack_files(slack_files: list) -> list:
    """For session CREATION: returns `resources` entries to pass to
    sessions.create()."""
    resources = []
    for f in slack_files:
        file_id = _upload_to_files_api(f)
        resources.append({"type": "file", "file_id": file_id, "mount_path": _mount_path_for(f)})
    return resources


def _mount_slack_files_on_session(session_id: str, slack_files: list) -> None:
    """For an ALREADY-CREATED session (e.g. a file attached on a thread
    reply, not the opening mention): mount files via the session resources
    API (client.beta.sessions.resources.add) instead of the `resources`
    param, which only applies at sessions.create() time."""
    for f in slack_files:
        file_id = _upload_to_files_api(f)
        client.beta.sessions.resources.add(
            session_id, type="file", file_id=file_id, mount_path=_mount_path_for(f)
        )


def _register(thread_ts: str, session_id: str, channel: str) -> None:
    with _threads_lock:
        pending_threads.discard(thread_ts)
        threads[thread_ts] = {"session_id": session_id, "channel": channel, "last_active": time.time()}


def _touch(thread_ts: str) -> None:
    with _threads_lock:
        if thread_ts in threads:
            threads[thread_ts]["last_active"] = time.time()


def _forget(thread_ts: str) -> None:
    with _threads_lock:
        threads.pop(thread_ts, None)
        pending_threads.discard(thread_ts)


def _reserve_thread_slot(thread_ts: str) -> bool:
    with _threads_lock:
        if thread_ts in threads or thread_ts in pending_threads:
            return True
        if len(threads) + len(pending_threads) >= MAX_CONCURRENT_THREADS:
            return False
        pending_threads.add(thread_ts)
        return True


def _is_pending(thread_ts: str) -> bool:
    with _threads_lock:
        return thread_ts in pending_threads


def start_job(channel: str, thread_ts: str, text: str, files: list) -> None:
    try:
        resources = [
            {"type": "file", "file_id": fid, "mount_path": path}
            for path, fid in ORCHESTRATOR_SCRIPT_FILE_IDS.items()
        ]
        if files:
            resources += _upload_slack_files(files)

        session = client.beta.sessions.create(
            environment_id=ORCHESTRATOR_ENV_ID,
            agent={"type": "agent", "id": ORCHESTRATOR_AGENT_ID, "version": ORCHESTRATOR_AGENT_VERSION},
            resources=resources,
            title=text.strip()[:80] or "search-relevance request",
            metadata={"slack_channel": channel, "slack_thread_ts": thread_ts},
        )
        _register(thread_ts, session.id, channel)

        message = text or "See the attached file(s)."
        client.beta.sessions.events.send(
            session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": message}]}],
        )
        relay_stream(session.id, channel, thread_ts)
    except Exception as e:
        _forget(thread_ts)
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=f"Request failed: {type(e).__name__}: {e}"
        )


def relay_stream(session_id: str, channel: str, thread_ts: str) -> None:
    summary = ""
    posted_progress = False
    for ev in client.beta.sessions.events.stream(session_id):
        t = ev.type
        if t == "agent.message":
            for b in ev.content:
                if b.type == "text" and b.text.strip():
                    summary = b.text
        elif t == "session.thread_created":
            # A sub-agent (goldie/tuner) was just delegated to.
            app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=f"-> delegating to `{ev.agent_name}`..."
            )
        elif t == "agent.tool_use" and not posted_progress:
            app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Working on it...")
            posted_progress = True
        elif t == "session.status_idle":
            break
        elif t == "session.status_terminated":
            trace = f"https://platform.claude.com/sessions/{session_id}"
            app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"Session terminated unexpectedly. Trace: {trace}",
            )
            _forget(thread_ts)  # can't be continued — don't let it linger for the sweeper
            return

    if summary:
        text = mrkdwn.convert(summary)
        if len(text) > 3900:
            text = text[:3900] + "\n_(truncated)_"
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

    outputs = client.beta.files.list(scope_id=session_id, betas=["managed-agents-2026-04-01"])
    for f in outputs.data:
        if not f.downloadable:
            continue
        content = client.beta.files.download(f.id).read()
        app.client.files_upload_v2(
            channel=channel, thread_ts=thread_ts, filename=f.filename, content=content
        )

    # Session is now idle, waiting on a possible reply — reset the inactivity
    # clock from here, not from when the turn started.
    _touch(thread_ts)


def continue_session(session_id: str, channel: str, thread_ts: str, text: str, files: list) -> None:
    try:
        if files:
            _mount_slack_files_on_session(session_id, files)
        message = text or "See the attached file(s)."
        client.beta.sessions.events.send(
            session_id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": message}]}],
        )
        relay_stream(session_id, channel, thread_ts)
    except Exception as e:
        app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=f"Request failed: {type(e).__name__}: {e}"
        )


@app.event("message")
def on_thread_reply(event, ack):
    ack()
    thread_ts = event.get("thread_ts")
    if event.get("subtype"):
        return
    if not thread_ts or event.get("bot_id"):
        return
    with _threads_lock:
        info = threads.get(thread_ts)
    if not info:
        if _is_pending(thread_ts):
            app.client.chat_postMessage(
                channel=event["channel"],
                thread_ts=thread_ts,
                text="Still starting this session. Give me a moment and try again.",
            )
        return
    _touch(thread_ts)  # reply just arrived — not inactive, regardless of what the sweeper sees
    threading.Thread(
        target=continue_session,
        args=(info["session_id"], event["channel"], thread_ts, event["text"], event.get("files") or []),
    ).start()


def _archive_session(session_id: str, why: str) -> bool:
    """Archive a session. Returns False if it refused — most likely because
    it's still `running` (archive requires `idle`), so the caller should keep
    it tracked and retry rather than losing sight of it."""
    try:
        client.beta.sessions.archive(session_id)
        return True
    except Exception as e:
        print(f"[{why}] could not archive session {session_id}: {e}")
        return False


def _archive_and_notify(thread_ts: str) -> None:
    with _threads_lock:
        info = threads.get(thread_ts)
    if not info:
        return
    if not _archive_session(info["session_id"], "sweep"):
        return  # still running — the next sweep will retry
    _forget(thread_ts)
    minutes = SESSION_INACTIVITY_TIMEOUT_S // 60
    try:
        app.client.chat_postMessage(
            channel=info["channel"],
            thread_ts=thread_ts,
            text=(
                f"This session has been stopped after {minutes} minutes of inactivity. "
                f"Mention me again in a new message to start a new one."
            ),
        )
    except Exception as e:
        print(f"[sweep] could not notify thread {thread_ts}: {e}")


def _sweep_inactive_sessions() -> None:
    while True:
        time.sleep(SESSION_SWEEP_INTERVAL_S)
        now = time.time()
        with _threads_lock:
            stale = [ts for ts, info in threads.items()
                     if now - info["last_active"] > SESSION_INACTIVITY_TIMEOUT_S]
        for thread_ts in stale:
            _archive_and_notify(thread_ts)


def _shutdown(signum, _frame) -> None:
    """Archive every tracked session before exiting.

    ECS sends SIGTERM before SIGKILL on every redeploy and scale-in. Without
    this, each session in `threads` is orphaned: still open on Anthropic's side
    holding a sandbox (idle time isn't billed, but it's never cleaned up), and
    invisible to the replacement process, whose `threads` map starts empty — so
    nothing would ever archive it. Runs in the main thread, which is otherwise
    parked in SocketModeHandler.start().
    """
    if _shutting_down.is_set():
        return
    _shutting_down.set()

    with _threads_lock:
        active = [(ts, dict(info)) for ts, info in threads.items()]
    print(f"[shutdown] signal {signum}: archiving {len(active)} session(s)")

    # Tell people first — true whether or not the archive below succeeds, since
    # the replacement process can't continue these threads either way.
    for thread_ts, info in active:
        try:
            app.client.chat_postMessage(
                channel=info["channel"],
                thread_ts=thread_ts,
                text=("I'm restarting, so this thread won't carry over. "
                      "Mention me again in a new message to start a new one."),
            )
        except Exception as e:
            print(f"[shutdown] could not notify thread {thread_ts}: {e}")

    # archive() only succeeds on an `idle` session, so retry mid-turn ones
    # until the grace period runs out instead of abandoning them immediately.
    remaining = [ts for ts, _ in active]
    deadline = time.time() + SHUTDOWN_GRACE_S
    while remaining and time.time() < deadline:
        for thread_ts in list(remaining):
            with _threads_lock:
                info = threads.get(thread_ts)
            if info is None or _archive_session(info["session_id"], "shutdown"):
                _forget(thread_ts)
                remaining.remove(thread_ts)
        if remaining:
            time.sleep(5)

    if remaining:
        with _threads_lock:
            stuck = [threads[ts]["session_id"] for ts in remaining if ts in threads]
        print(f"[shutdown] {len(remaining)} session(s) still not idle at the "
              f"{SHUTDOWN_GRACE_S}s grace deadline; they stay open and need "
              f"archiving by hand: {', '.join(stuck)}")
    else:
        print("[shutdown] all sessions archived")

    # Worker threads are blocked reading their event streams and won't join, so
    # exit rather than waiting on them.
    os._exit(0)


if __name__ == "__main__":
    # SocketModeHandler.start() parks the main thread in Event().wait(), which
    # is signal-interruptible, so these run there. SIGINT is just for local
    # Ctrl-C — on Windows start() resets it to SIG_DFL, but ECS is Linux.
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    threading.Thread(target=_sweep_inactive_sessions, daemon=True).start()
    SocketModeHandler(app, SLACK_APP_TOKEN).start()

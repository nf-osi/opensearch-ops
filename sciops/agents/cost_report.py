#!/usr/bin/env python3
"""Estimate the USD cost of one or more Managed Agent sessions.

There's no single "cost" field on a session — this reconstructs it from two billed
dimensions (see https://platform.claude.com/docs/en/about-claude/pricing#claude-managed-agents-pricing):

  1. Tokens, per sub-agent/model — a coordinator session can run several models (e.g. the
     orchestrator on claude-opus-5 delegating to goldie on claude-sonnet-5), so this reads
     `sessions.threads.list()`'s per-thread `usage` (base input, 5m-cache-write, cache-read,
     output) rather than the session-level `usage`, which is just the sum and hides which
     model to price each token against.
  2. Session runtime — billed at $0.08/session-hour for wall-clock time spent `running`
     (idle/rescheduling time doesn't count). There's no duration field on the session object;
     this derives it from `sessions.events.list()`'s `processed_at` timestamps by pairing up
     every status_running -> status_idle/status_terminated span (a session can cycle through
     this more than once across a multi-turn thread, not just at the very end).

PRICING below is a snapshot of published per-model rates (see the pricing page above) — it
WILL go stale as prices/models change; if a session uses a model not listed, its tokens are
still shown but excluded from the cost total (flagged, not silently dropped). Sonnet 5 has a
time-bound introductory rate (through 2026-08-31); the session's `created_at` picks which
rate applies.

This is a list-price ESTIMATE — no account-specific discounts/negotiated rates applied.

Usage:
    python3 cost_report.py <SESSION_ID> [<SESSION_ID> ...]
    python3 cost_report.py <SESSION_ID> --json
"""
import argparse
import json
from datetime import datetime, timezone

from anthropic import Anthropic

BETAS = ["managed-agents-2026-04-01"]

RUNTIME_RATE_PER_HOUR = 0.08

# {model_id: [(effective_from, {base_in, cache_5m, cache_read, out}), ...]}
# Rates are USD per million tokens. Multiple entries = time-bound pricing (picks the latest
# entry whose effective_from <= the session's created_at). Snapshot as of 2026-07.
PRICING = {
    "claude-opus-5": [
        (datetime.min.replace(tzinfo=timezone.utc),
         {"base_in": 5, "cache_5m": 6.25, "cache_read": 0.50, "out": 25}),
    ],
    "claude-opus-4-8": [
        (datetime.min.replace(tzinfo=timezone.utc),
         {"base_in": 5, "cache_5m": 6.25, "cache_read": 0.50, "out": 25}),
    ],
    "claude-opus-4-7": [
        (datetime.min.replace(tzinfo=timezone.utc),
         {"base_in": 5, "cache_5m": 6.25, "cache_read": 0.50, "out": 25}),
    ],
    "claude-opus-4-6": [
        (datetime.min.replace(tzinfo=timezone.utc),
         {"base_in": 5, "cache_5m": 6.25, "cache_read": 0.50, "out": 25}),
    ],
    "claude-opus-4-5": [
        (datetime.min.replace(tzinfo=timezone.utc),
         {"base_in": 5, "cache_5m": 6.25, "cache_read": 0.50, "out": 25}),
    ],
    "claude-sonnet-5": [
        (datetime.min.replace(tzinfo=timezone.utc),
         {"base_in": 2, "cache_5m": 2.50, "cache_read": 0.20, "out": 10}),  # through 2026-08-31
        (datetime(2026, 9, 1, tzinfo=timezone.utc),
         {"base_in": 3, "cache_5m": 3.75, "cache_read": 0.30, "out": 15}),  # from 2026-09-01
    ],
    "claude-sonnet-4-6": [
        (datetime.min.replace(tzinfo=timezone.utc),
         {"base_in": 3, "cache_5m": 3.75, "cache_read": 0.30, "out": 15}),
    ],
    "claude-sonnet-4-5": [
        (datetime.min.replace(tzinfo=timezone.utc),
         {"base_in": 3, "cache_5m": 3.75, "cache_read": 0.30, "out": 15}),
    ],
    "claude-haiku-4-5": [
        (datetime.min.replace(tzinfo=timezone.utc),
         {"base_in": 1, "cache_5m": 1.25, "cache_read": 0.10, "out": 5}),
    ],
    "claude-fable-5": [
        (datetime.min.replace(tzinfo=timezone.utc),
         {"base_in": 10, "cache_5m": 12.50, "cache_read": 1.0, "out": 50}),
    ],
}


def _rate_for(model_id, when):
    schedule = PRICING.get(model_id)
    if not schedule:
        return None
    applicable = [rates for effective_from, rates in schedule if effective_from <= when]
    return applicable[-1] if applicable else schedule[0][1]


def _token_cost(usage, rates):
    cache = usage.get("cache_creation") or {}
    cache_5m = cache.get("ephemeral_5m_input_tokens", 0)
    return (
        usage.get("input_tokens", 0) * rates["base_in"]
        + cache_5m * rates["cache_5m"]
        + usage.get("cache_read_input_tokens", 0) * rates["cache_read"]
        + usage.get("output_tokens", 0) * rates["out"]
    ) / 1_000_000


def _running_seconds(events):
    total = 0.0
    run_start = None
    for ev in events:
        if ev.type == "session.status_running" and run_start is None:
            run_start = ev.processed_at
        elif ev.type in ("session.status_idle", "session.status_terminated") and run_start is not None:
            total += (ev.processed_at - run_start).total_seconds()
            run_start = None
    return total


def report(client: Anthropic, session_id: str) -> dict:
    session = client.beta.sessions.retrieve(session_id, betas=BETAS)
    threads = client.beta.sessions.threads.list(session_id, betas=BETAS).data
    events = list(client.beta.sessions.events.list(session_id, betas=BETAS))

    thread_reports = []
    token_cost_total = 0.0
    unpriced_models = set()
    for t in threads:
        model_id = t.agent.model.id
        usage = t.usage.model_dump() if t.usage else {}
        rates = _rate_for(model_id, session.created_at)
        cost = _token_cost(usage, rates) if rates else None
        if cost is None:
            unpriced_models.add(model_id)
        else:
            token_cost_total += cost
        thread_reports.append({
            "thread_id": t.id, "agent_name": t.agent.name, "model": model_id,
            "usage": usage, "token_cost_usd": cost,
        })

    running_s = _running_seconds(events)
    runtime_cost = running_s / 3600 * RUNTIME_RATE_PER_HOUR

    return {
        "session_id": session_id,
        "created_at": str(session.created_at),
        "status": session.status,
        "threads": thread_reports,
        "running_seconds": running_s,
        "runtime_cost_usd": runtime_cost,
        "token_cost_usd": token_cost_total,
        "total_cost_usd": token_cost_total + runtime_cost,
        "unpriced_models": sorted(unpriced_models),
    }


def print_report(r: dict):
    print(f"\nsession {r['session_id']}  (status={r['status']}, created {r['created_at']})")
    for t in r["threads"]:
        u = t["usage"]
        cache_5m = (u.get("cache_creation") or {}).get("ephemeral_5m_input_tokens", 0)
        cost_str = f"${t['token_cost_usd']:.4f}" if t["token_cost_usd"] is not None else "? (no pricing for this model)"
        print(f"  [{t['agent_name']}] model={t['model']}")
        print(f"    input={u.get('input_tokens', 0)}  cache_write_5m={cache_5m}  "
              f"cache_read={u.get('cache_read_input_tokens', 0)}  output={u.get('output_tokens', 0)}")
        print(f"    token cost: {cost_str}")
    print(f"  session runtime: {r['running_seconds']/60:.2f} min -> ${r['runtime_cost_usd']:.4f}")
    if r["unpriced_models"]:
        print(f"  WARNING: no pricing on file for {r['unpriced_models']} — "
              f"total below EXCLUDES their tokens. Update PRICING in this script.")
    print(f"  TOTAL (list price, no discounts): ${r['total_cost_usd']:.4f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_ids", nargs="+", metavar="SESSION_ID")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of a printed report")
    args = ap.parse_args()

    client = Anthropic()
    reports = [report(client, sid) for sid in args.session_ids]

    if args.json:
        print(json.dumps(reports, indent=2, default=str))
        return

    for r in reports:
        print_report(r)

    if len(reports) > 1:
        grand_total = sum(r["total_cost_usd"] for r in reports)
        print(f"\nGRAND TOTAL across {len(reports)} sessions: ${grand_total:.4f}")


if __name__ == "__main__":
    main()

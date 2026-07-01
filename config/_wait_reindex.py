#!/usr/bin/env python3
"""Poll the nf-tools index match_all count until a rebuild settles.

Heuristic: a rebuild deletes + recreates the index then re-streams rows, so the
count typically dips below the prior max and recovers. We declare "settled" once
the count is back at/above the baseline AND has held steady for STABLE_READS
consecutive polls. Bails out after MAX_MIN minutes regardless.
"""
import sys, time
sys.path.insert(0, ".")
from query import search, NF_TOOLS

BASELINE = 1216          # count observed just before the rebuild was triggered
STABLE_READS = 3
INTERVAL_S = 30
MAX_MIN = 20


def count():
    r = search(NF_TOOLS, {"query": {"match_all": {}}, "size": 0})
    return r.get("totalHits")


def main():
    deadline = time.time() + MAX_MIN * 60
    seen_dip = False
    last = None
    stable = 0
    while time.time() < deadline:
        try:
            c = count()
        except Exception as e:
            c = f"err:{e}"
        ts = time.strftime("%H:%M:%S")
        print(f"{ts}  totalHits={c}  seen_dip={seen_dip}  stable={stable}", flush=True)
        if isinstance(c, int):
            if c < BASELINE:
                seen_dip = True
            if c == last and c >= BASELINE:
                stable += 1
            else:
                stable = 0
            last = c
            # Settled: recovered to baseline and held steady; require a dip first
            # only if we ever saw one (fast rebuilds may never show a dip).
            if stable >= STABLE_READS and c >= BASELINE:
                print(f"SETTLED at totalHits={c} (seen_dip={seen_dip})", flush=True)
                return 0
        time.sleep(INTERVAL_S)
    print("TIMEOUT — count never stabilized within window", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())

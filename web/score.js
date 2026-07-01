// Browser port of the scoring in benchmark/run.py (reciprocal_rank, score_case,
// rt_stats, and the per-strategy aggregate). MUST stay in sync with run.py — the
// benchmark parity check is the guard.

// Mean-reciprocal-rank contribution: 1/rank of the first relevant hit (else 0).
export function reciprocalRank(rankedIds, relevant) {
  for (let i = 0; i < rankedIds.length; i++) {
    if (relevant.has(rankedIds[i])) return { rr: 1 / (i + 1), rank: i + 1 };
  }
  return { rr: 0, rank: null };
}

// Score one case's ranked ids against its relevant set.
export function scoreCase(rankedIds, relevant, k) {
  const rel = relevant instanceof Set ? relevant : new Set(relevant);
  const topk = rankedIds.slice(0, k);
  const { rr, rank } = reciprocalRank(rankedIds, rel);
  const found = new Set(topk.filter((id) => rel.has(id)));
  return {
    rr,
    first_rel_rank: rank,
    recall_at_k: rel.size ? found.size / rel.size : 0,
    hit_at_1: rankedIds.length && rel.has(rankedIds[0]) ? 1 : 0,
    hit_at_k: found.size ? 1 : 0,
    n_relevant: rel.size,
    n_found_in_k: found.size,
  };
}

// Round-trip latency spread (median/p95 are the meaningful ones; mean is skewed by
// poll-interval stalls — same caveat as run.py).
export function rtStats(rts) {
  const s = [...rts].sort((a, b) => a - b);
  const n = s.length;
  if (!n) return { rt_ms_mean: 0, rt_ms_median: 0, rt_ms_min: 0, rt_ms_max: 0, rt_ms_p95: 0 };
  const pct = (p) => s[Math.min(n - 1, Math.round((p / 100) * (n - 1)))];
  return {
    rt_ms_mean: s.reduce((a, b) => a + b, 0) / n,
    rt_ms_median: pct(50),
    rt_ms_min: s[0],
    rt_ms_max: s[n - 1],
    rt_ms_p95: pct(95),
  };
}

// Aggregate per-case scores into the strategy row shown in the scoreboard.
export function aggregate(caseScores, rts) {
  const n = caseScores.length;
  const mean = (key) => (n ? caseScores.reduce((a, c) => a + c[key], 0) / n : 0);
  return {
    mrr: mean("rr"),
    recall_at_k: mean("recall_at_k"),
    hit_at_1: mean("hit_at_1"),
    hit_at_k: mean("hit_at_k"),
    n_cases: n,
    ...rtStats(rts),
  };
}

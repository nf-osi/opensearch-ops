# Cross-index (unified) search

Portal owners want one search that spans several object-type indices — e.g. NF **tools**
(`syn75081636`), **datasets** (`syn75081630`), **publications** (`syn75081631`), **studies**,
etc. Each type is its own `SearchIndex` with its own `definingSQL` and schema, and the query
API takes a **single `searchIndexId`**. So unified search is built by *orchestration over the
existing per-type indices*, not by a new index.

> [!NOTE]
> **Out of scope: a combined union index.** Merging the types into one index would require a
> shared schema / union materialized view, but these are very different objects in their own
> tables (a tool's `rrid`/`targetAntigen` vs a dataset's `assay`/`species` vs a publication's
> `title`/`year`). Forcing them into one mapping loses the per-type richness and isn't worth
> it. We pursue **B** and **C** only.

The hard part of both is that **relevance scores are not comparable across indices**: a BM25
score for a tool and one for a dataset come from different corpora, fields, and IDF
statistics. B avoids the problem; C works around it with rank, not score.

---

## Option B — Federated / grouped (≈ what we have today)

**How:** fan the query out to each index, rank *within* each type, and present the results
**grouped** — sections or tabs like "Tools (12) · Datasets (8) · Publications (30)". No
cross-type merging, so no score-comparability problem.

**This is what we have today.** The portal framework already implements B: one header search
bar routes to a `/Search` results page that renders **one tab per object type**, each with a
result-count badge. See `apps/synapse-portal-framework/src/components/PortalSearch/` —
`HeaderSearchBox.tsx` (the bar), `PortalSearchPage.tsx` (tab orchestration), `PortalSearchTabs.tsx`
(the tab bar with counts), and `SearchParamAwareQueryWrapperPlotNav.tsx` (per-tab query). Each
tab runs its own query against its own type and is ranked independently — no cross-type merge.
(Per-tab querying can use either the older table full-text search or the new `SearchQueryWrapper`
SearchIndex backend, per config.)

**Relevance layer (the configurable part):** the set/order of tabs and the default-tab rule;
each tab's results are ranked independently by its own index/query.

**Default tab = the type with the most results (current behavior).** In
`PortalSearchPage.tsx` (`onQueryResultBundleChange`, ~lines 47–87; comment "PORTALS-3382"):
once every tab's count is known and no tab is explicitly selected in the URL, it redirects to
a default tab. If a `SEARCH_ROLE` URL param maps (via `roleMapping`) to a tab with a non-zero
count, it picks that; **otherwise it navigates to the highest-count tab**
(`reduce((max, tab) => tab.count > max.count ? tab : max, searchPageTabs[0])`). Ties resolve to
the **first tab in configured order** (strict `>`, accumulator seeded at `searchPageTabs[0]`),
and a directly-navigated tab is respected (the redirect only fires when none is selected). Note
this defaults on *match count*, not relevance — a high-volume type (NF is ~70% antibodies) will
tend to win the default tab. The illustration's active Tools tab is just an example.

**Trade-off:** cheapest and lowest-risk, reuses the current API unchanged; but the user sees
*buckets*, never a single "most relevant result regardless of type" list. If that single
blended list is required, you need C.

---

## Option C — Fan-out + weighted rank fusion (one blended list)

Use when product needs a **single ranked list mixing types**.

### Why rank fusion (and an important nuance)

Because raw scores aren't comparable across indices, we merge on **rank position**, not score.
The standard tool is the Reciprocal Rank Fusion transform `1/(k + rank)`.

**Nuance specific to our setup — the indices are disjoint.** A tool only appears in the tools
index, a dataset only in the datasets index, so **every document appears in exactly one
list.** That means this is *not* classic multi-list RRF, whose value is rewarding documents
that rank highly across *several* rankers. Here we're using the RRF scoring function purely as
a **score-free normalizer** that puts disjoint per-type lists onto one comparable scale, with
**per-type weights** controlling the blend. It's effectively *weighted rank interleaving*. (An
alternative is per-type score normalization — min-max/z-score then weighted merge — but raw
scores are noisier, so rank-based is the more robust default.)

### What we need to implement

1. **Fan-out orchestrator** — a frontend multi-index wrapper or a backend aggregation
   endpoint. It sends the *same* `queryText` to each participating index **in parallel** via
   the existing `start`/poll async API. Each index searches its own fields, so with today's
   bare `multi_match` (no `fields`) there's no per-index field config to maintain.
2. **Per-index retrieval depth** `K_fetch` (e.g. 50) — fetch top-`K_fetch` from each index so
   the merge has enough candidates below the fold.
3. **Hit normalization** — from each hit capture a stable id (`resourceId`/`rowId`), the
   **type/index label**, and its **within-list rank** (1-based, by returned order). Keep the
   raw `score` only if you also want the score-normalization variant.
4. **Fusion** — `fused(d) = wᵢ / (k_rrf + rankᵢ)` where `i` is `d`'s index, `rankᵢ` its rank
   in that list, `k_rrf ≈ 60`. Sort descending, stable.
5. **Per-type weights `wᵢ`** — the portal-owned relevance knob (default `1.0` each). This is
   the cross-type analogue of field boosting: "datasets matter more than tools here" → raise
   `w_datasets`.
6. **Diversity / quotas (optional)** — a per-type cap or round-robin minimum in the top-N so
   one large type (NF is ~70% antibodies) can't crowd out a single relevant dataset.
7. **Tie-breaking** — deterministic, stable order for equal fused scores.
8. **Pagination** — `K_fetch` per index bounds how deep the blended list goes; deep pages
   require a larger `K_fetch` or re-fetch. Document this limit rather than hiding it.
9. **Mixed rendering** — each merged hit carries its type, so the UI renders the right card
   per result.
10. **Latency** — parallel fan-out makes wall-clock ≈ the *slowest* index, not the sum;
    fusion itself is negligible.

### Pseudocode

```text
inputs: queryText, indices = [(type, searchIndexId, weight)], K_fetch=50, k_rrf=60
# fan out in parallel, reuse the existing async query helper per index
for (type, id, w) in indices in parallel:
    hits[type] = search(id, {query:{multi_match:{query:queryText, fuzziness:'AUTO'}}, size:K_fetch}).hits

merged = []
for (type, id, w) in indices:
    for rank, hit in enumerate(hits[type], start=1):
        merged.append({ id: hit.id, type, rank, fused: w / (k_rrf + rank), hit })

merged.sort(by fused desc, stable)
# optional: enforce per-type quota / round-robin here
return merged[offset : offset + pageSize]
```

### Tunable knobs = the relevance layer (and they're benchmarkable)

`wᵢ` (per-type weights), `k_rrf` (rank-decay steepness), `K_fetch`, and any quota rule are the
configuration surface portal owners would own. All of them can be tuned **offline in the
harness**, exactly as `strategies.py` simulates field boosts today — they just need:

- **cross-type golden cases**: queries whose relevant answers may be *any* type, with **graded
  relevance** across types, and
- **nDCG over the blended list** as the metric (binary MRR/Recall don't capture mixed-type
  ordering well).

So the type-weighting payoff can be characterized before any of this is wired into a portal.

---

## OpenSearch's native affordances (and why they're not reachable today)

OpenSearch itself has rich, native machinery for combining and reranking results — and yes,
it supports plugging in **custom ML models**. The catch is *where* it lives.

- **Hybrid query + normalization processor** (since OpenSearch 2.10): a `hybrid` query runs
  multiple sub-queries; a search-pipeline `normalization-processor` normalizes their scores
  (min-max or L2) and combines them (arithmetic / geometric / harmonic mean). Score-based
  fusion.
- **Score-ranker processor / RRF** (since 2.19): rank-based **Reciprocal Rank Fusion** of a
  hybrid query's sub-results — the native version of the merge we hand-roll in Option C.
- **Rerank processor with a cross-encoder** (since 2.12): a response processor that reranks
  the top-N hits with a **cross-encoder model** (`TEXT_SIMILARITY`) hosted in **ML Commons** —
  an OpenSearch-provided model, a **custom local model** (TorchScript/ONNX), or a **remote
  connector** (e.g. SageMaker, Cohere). This is the "custom ML model" path, and it's actually
  the *cleanest* answer to cross-type ranking: a learned reranker scores `(query, document)`
  pairs on one consistent scale, so a tool and a dataset become directly comparable — it
  sidesteps BM25 score-incomparability entirely instead of working around it. ML Commons also
  offers an ML-inference response processor and a Learning-to-Rank plugin for trained models.

**But none of this is reachable through the Synapse SearchIndex API today.** All of it
requires the `hybrid` query type, **search pipelines**, and/or **ML model hosting**
configured on the OpenSearch cluster. The Synapse API exposes only a single `searchIndexId`,
a raw query-DSL `searchQuery`, and `responseParts` — no search-pipeline parameter, no
ML model/connector, no hybrid or multi-index orchestration. The native fusion/RRF is also
designed to combine sub-queries *within one `_search`* (e.g. lexical + semantic of one
corpus), not to merge disjoint per-type index *entities*. So:

- Portal owners cannot configure any of it through Synapse as it stands.
- Adopting it needs **Synapse platform support** — exposing search pipelines / the rerank
  processor, or a multi-index hybrid+rerank endpoint — or cluster-side configuration that NF
  does not control.

Practical split:

- **Now (client-side, no platform changes):** Option C's weighted RRF over fan-out API calls.
- **Better relevance, later (platform-dependent):** a cross-encoder reranker (ML Commons) over
  a unified candidate set — the highest-quality cross-type ranker, but gated on Synapse
  exposing ML/rerank pipelines.

Refs: [Hybrid search](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/index/) ·
[RRF for hybrid search](https://opensearch.org/blog/introducing-reciprocal-rank-fusion-hybrid-search/) ·
[Rerank processor](https://docs.opensearch.org/latest/search-plugins/search-pipelines/rerank-processor/) ·
[Custom local models](https://docs.opensearch.org/latest/ml-commons-plugin/custom-local-models/)

## Recommendation

- If a grouped/tabbed UX is acceptable, **B** is the answer — it's the smallest step from
  today (add a unified entry point over the existing per-type searches).
- If a single blended list is required, **C with weighted rank fusion** is the near-term
  build: keeps the per-type indices, needs no reindex, and gives portal owners one clean
  per-type weight knob.
- The longer-term, highest-relevance option is a **cross-encoder reranker** over a unified
  candidate set (OpenSearch ML Commons), which ranks all types on one learned scale — but it
  depends on Synapse exposing ML/rerank pipelines, so it's a platform ask, not something we
  can configure now.

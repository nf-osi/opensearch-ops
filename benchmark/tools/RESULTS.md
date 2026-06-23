# NF search benchmark — strategies & results

This is the stakeholder-facing summary of how different search query strategies perform
on the **`nf-tools`** index (the NF Research Tools Central registry, ~1,216 tools:
antibodies, cell lines, animal models, and protocols).

## What we are measuring

We have a **golden set** of realistic searches where we already know which tool(s) the
user is looking for (see [`golden.yaml`](golden.yaml) — commented YAML, easy to review
and edit). For each search we run every *query strategy*, 
look at the ranked list of results it returns, and check where the
correct tool(s) landed. Two kinds of cases:

- **Known-item** — there is one defensible right answer (an exact tool name, an RRID, or
  a known synonym). These scores are trustworthy.
- **Topical** — a broader query ("plexiform neurofibroma", "neurofibromin antibody")
  where several tools are relevant. These are seeded with examples but **need subject
  matter expert (SME) curation** before their scores should be trusted.

### Metrics

| Metric | What it answers | Range |
| --- | --- | --- |
| **MRR** (Mean Reciprocal Rank) | "How high up is the first correct result?" #1 = 1.0, #2 = 0.5, #3 = 0.33… averaged over all searches. | 0–1, higher better |
| **Recall@10** | "Of all the tools that *should* match, what fraction showed up in the top 10?" | 0–1, higher better |
| **Hit@1** | "How often is the very first result correct?" | 0–1, higher better |
| **Hit@10** | "How often does at least one correct result appear in the top 10?" | 0–1, higher better |

MRR and Hit@1 reward putting the right answer at the very top (matters most for
typeahead and "I know what I want" searches). Recall@10 matters more for browse/explore
searches where the user scans a list.

## The query strategies

Each strategy is a different recipe for turning what the user typed into a search. They
differ in **which fields they look at**, **how much each field counts** ("boosting"), and
**how forgiving they are** about word order and spelling.

The fields searched are: `resourceName`, `synonyms`, `rrid`, `targetAntigen`,
`diseaseType`, and `description`. A boost is written `field^N` — e.g. `resourceName^5`
means a match in the tool name counts five times as much as an unboosted field.

| Strategy | What it does | Best for | Trade-off |
| --- | --- | --- | --- |
| **frontend_default** | **Exactly what the live Synapse frontend sends today**: a bare `multi_match` over all fields with `fuzziness: AUTO`, no boosts, no explicit type. | The baseline. | Carries every weakness below at once (no boosts, fuzzy noise); included to measure, not to recommend. |
| **simple_query_string** | A forgiving "Google-style" query across all fields, equally weighted. Supports operators the user might type (`+`, `-`, quotes). Never errors on odd syntax. | A safe general-purpose default. | No field prioritization — a description match competes equally with a name match. |
| **multi_match_best** | Searches all fields equally and scores each result by its single best-matching field ("best fields"). | General search where the strongest single signal should win. | Equal field weighting; doesn't reward a tool that matches in several fields. |
| **multi_match_boosted** | Same as best-fields but with **our boosts** (`resourceName^5`, `synonyms^4`, `rrid^4`, `targetAntigen^2`, …) so name/synonym/RRID matches outrank description matches. | Pushing the obvious canonical match to the top. | If the boosts are mis-tuned they can *demote* a correct match found in a lower-weighted field (we see this below). |
| **multi_match_cross** | "Cross fields" — treats the searched fields as one combined field, so a query whose words are spread across several fields (e.g. part in name, part in synonym) still matches well. Uses the same boosts. | Queries where terms are scattered across fields. | Stricter about every term matching somewhere; can miss loosely-related hits. |
| **boosted_fuzzy** | The boosted strategy plus **typo tolerance** (`fuzziness: AUTO`) so "schwan" still finds "Schwann". | Misspellings and near-misses. | Fuzziness adds noise — on identifier-heavy data (RRIDs, clone names) it pulls in wrong matches and **hurt both precision and recall** here. |
| **phrase_prefix** | Treats the last word as a prefix, like live typeahead ("neurofib…" matches "neurofibromin"). | Autocomplete / as-you-type search boxes. | Phrase semantics are stricter on word order; weaker for unordered keyword queries. |

These strategies are the main lever this harness exists to test, alongside index-side
config (custom analyzers and the `org.synapse.nf` synonym sets, not yet enabled).

## Latest results

**Run:** 2026-06-22 · k=10 · 48 cases (12 known-item, 36 topical) · **15-field** config
([`fields.yaml`](fields.yaml)). `rt_ms` is round-trip latency (median / p95); it includes
client poll interval + async-queue wait, so compare on the **median**, use p95 for tail cost.

| strategy | MRR | Recall@10 | Hit@1 | Hit@10 | rt_med | rt_p95 |
| --- | --- | --- | --- | --- | --- | --- |
| multi_match_cross | **0.744** | **0.693** | **0.667** | **0.896** | 1185 | 1727 |
| frontend_default | 0.659 | 0.642 | 0.583 | 0.792 | 1672 | 3572 |
| multi_match_boosted | 0.654 | 0.630 | 0.583 | 0.812 | 1179 | 1869 |
| multi_match_best | 0.645 | 0.651 | 0.562 | 0.833 | 1187 | 2188 |
| simple_query_string | 0.631 | 0.644 | 0.542 | 0.812 | 1180 | 1680 |
| boosted_fuzzy | 0.627 | 0.597 | 0.521 | 0.812 | 1188 | 1733 |
| phrase_prefix | 0.541 | 0.480 | 0.458 | 0.688 | 1174 | 2210 |

### Summary

- **`multi_match_cross` is the strongest strategy** — it leads every quality metric (MRR
  0.744, Recall@10 0.693, Hit@1 0.667, Hit@10 0.896). Treating the searched fields as one
  combined field, with name/synonym boosted above the disease/organism/category fields, both
  ranks the right answer near the top and surfaces the most relevant results.
- **Recall driven by fields defined in strategy.** The boosted/best strategies search a
  curated 15-field set ([`fields.yaml`](fields.yaml)) covering names, synonyms, RRIDs, and the
  discovery columns (`cellLineManifestation`, `species`, `cellLineCategory`, `resourceType`,
  `vectorType`, …). `frontend_default` searches *all* fields, which is why its recall (0.642)
  is respectable, but the noise from boilerplate columns leaves its ranking (MRR 0.659) below
  the more refined strategies.
- **Latency: a curated field list is fast and consistent.** The curated-field strategies run
  at **~1180ms median** (p95 1680–2210). Searching all fields (`frontend_default`) is the
  outlier at **1672ms median / 3572ms p95** — ~40% slower at the median, ~2× at the tail.
  Among the curated strategies the median barely varies; query type only nudges the p95 tail
  (`phrase_prefix`/`multi_match_best` highest, `simple_query_string`/`cross` lowest).
- **`phrase_prefix` is the weakest** (MRR 0.541): multi-word discovery queries punish
  phrase/word-order strictness. `boosted_fuzzy` also sits low — fuzziness adds noise on this
  identifier-heavy data (RRIDs, clone names) without a recall payoff.
- **Six queries are true recall gaps** (`recall_gap_current: true` — no relevant result in the
  top 20): `pnf`, `melanoma-cell-line`, `metabolic-mouse-model`, `cafe-au-lait-spots`,
  `nf1-cell-line-black`, `nf1-bacterial-vector`. They hinge on low-signal or sparsely-annotated
  fields (race, the metabolic/café-au-lait manifestations, bacterial-expression vector type, the
  `pnf` abbreviation). The levers are the bound `nf_tools_search_config` (analyzer routing +
  synonyms, e.g. a `pnf`→plexiform synonym) and registry annotation (tagging the café-au-lait
  models) — not query-strategy tuning.
- **`k=10` undersells the discovery cases.** Many topical queries have `expected_pool` in the
  tens-to-hundreds (e.g. melanoma 238) but a 3–5 item ideal head, so Recall@10 is a floor. nDCG
  over the ranked heads would discriminate better — a future harness addition.

## Levers and expected return

### Working assumption: portal search is discovery-heavy

**We care more about *discovery* queries than *known-item* re-finding,** similar to open web searches [1], [2].
A discovery query explores ("antibodies for plexiform neurofibroma", "NF1 mouse models");
a known-item query re-finds a specific resource one already know exists.

### Where field boosting actually matters

Field boosting (`resourceName^5`, `synonyms^4`, …) matters more for results when **several
documents compete across different fields** — e.g. a topical/browse query like
"neurofibromin antibody" or "schwann cell line", where we want a name/synonym match to
outrank an incidental description match. It does **nothing** for *known-item* lookups (find
a tool by its exact name, RRID, or synonym): those already resolve to the #1 result, so
there is no headroom, and over-boosting the name field can even *demote* a correct synonym
hit (some runs have seen this).

### Lever comparison

| Lever | Layer | Expected return | Measured | Notes |
| --- | --- | --- | --- | --- |
| **Fields searched** (which columns the query covers) | frontend query (hard-coded in SRC) | **High** | Curated 15-field set currently produces the best strategy (`multi_match_cross`). | The frontend hard-codes an all-fields query; realizing results curated `fields` is an update in frontend src. |
| **Query type** (best_fields / cross_fields / phrase) | frontend query (hard-coded in SRC) | **Medium** | `cross_fields` is the strongest type (MRR 0.744); `best_fields` 0.645; `phrase_prefix` the weakest (0.541) — a ~0.2 MRR spread. | `cross_fields` pairs best with the curated boosted fields. |
| **Field boosted** (`field^N`) | frontend query (hard-coded in SRC) | **Small–Medium** | Boosting modestly beats equal weighting (`multi_match_boosted` 0.654 vs `multi_match_best` 0.645) and combines with `cross_fields` for the top score; it moves topical/ambiguous queries, ~0 on known-item. | Over-boosting the name field can demote a correct synonym hit. |
| **Fuzziness** (currently `AUTO`) | frontend query (hard-coded in SRC) | **Small** (from turning it off) | `boosted_fuzzy` is among the weakest (MRR 0.627, Recall@10 0.597); on identifier-heavy data (RRIDs, clone names) fuzziness adds noise with no recall payoff. | Cheapest, safest change |
| **Analyzers / synonyms** | config (index) | **Unknown — potentially high for the recall gaps** | Not yet measured: config is created but unbound. | Each iteration is a full delete-recreate-reindex, highest cost-per-experiment. |


### References

1. Broder (2002), AltaVista (1,000 queries). [SIGIR Forum PDF](https://sigir.org/files/forum/F2002/broder.pdf)
2. Jansen, Booth & Spink (2008), Dogpile logs. [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S030645730700163X)
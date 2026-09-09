# NF search benchmark — strategies & levers

This is the stakeholder-facing summary of the search query strategies we test on the
**`nf-tools`** index (the NF Research Tools Central registry: antibodies, cell lines, animal
models, and protocols) and which levers move results. How each strategy currently scores is
on the dashboard, not here — see [Results](#results).

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

The curated field list and its weights live in [`fields.yaml`](fields.yaml); read them
there rather than here, so the two cannot drift. A boost is written `field^N` — e.g.
`resourceName^5` means a match in the tool name counts five times as much as an unboosted
field.

| Strategy | What it does | Best for | Trade-off |
| --- | --- | --- | --- |
| **production_current** | **What the nf-tools page actually sends today** — best-fields over production's own six boosted columns, no fuzziness, transcribed from `resources.ts` and compiled from the `production:` block in [`fields.yaml`](fields.yaml). Includes the routing rule that diverts quoted phrases and Synapse ids to `simple_query_string`. | **The control for this table.** A gain over this row is a gain over what users get right now. | Searches six columns; blind to the discovery columns the wider strategies reach. |
| **frontend_default** | **The Synapse front-end's platform default** — what a portal gets when it sets no search config: a bare `multi_match` over all fields with `fuzziness: AUTO`, no boosts, no explicit type (plus the same quoted / Synapse-id routing). NF tools does not run this; it ships its own recipe, so here it is a reference point rather than the control. It *is* the control for the 12 NF indexes that ship no config. | Measuring what "no configuration at all" buys. | Carries every weakness below at once (no boosts, fuzzy noise); included to measure, not to recommend. Its one structural advantage is coverage: with no field list it searches every column, including ones `fields.yaml` omits. |
| **simple_query_string** | A forgiving "Google-style" query across the curated fields, equally weighted. Supports operators the user might type (`+`, `-`, quotes). Never errors on odd syntax. | A safe general-purpose default. | No field prioritization — a description match competes equally with a name match. |
| **simple_query_string_boosted** | The same clause with **our boosts** applied. This is the exact shape production falls back to whenever a query contains a quoted phrase or a Synapse id, so it is the only strategy that mirrors that path in isolation. | Exact-phrase and identifier lookups, where typo tolerance does more harm than good. | Operator syntax is interpreted, so a stray `+` or `-` changes the query rather than being tokenised away. |
| **multi_match_best** | Searches all fields equally and scores each result by its single best-matching field ("best fields"). | General search where the strongest single signal should win. | Equal field weighting; doesn't reward a tool that matches in several fields. |
| **multi_match_boosted** | Same as best-fields but with **our boosts** (see [`fields.yaml`](fields.yaml)) so name/synonym/RRID matches outrank description matches. | Pushing the obvious canonical match to the top. | If the boosts are mis-tuned they can *demote* a correct match found in a lower-weighted field. |
| **multi_match_cross** | "Cross fields" — treats the searched fields as one combined field, so a query whose words are spread across several fields (e.g. part in name, part in synonym) still matches well. Uses the same boosts. | Queries where terms are scattered across fields. | Stricter about every term matching somewhere; can miss loosely-related hits. |
| **boosted_fuzzy** | The boosted strategy plus **typo tolerance** (`fuzziness: AUTO`) so "schwan" still finds "Schwann". | Misspellings and near-misses. | Fuzziness adds noise — on identifier-heavy data (RRIDs, clone names) it pulls in wrong matches, hurting both precision and recall here. |
| **phrase_prefix** | Treats the last word as a prefix, like live typeahead ("neurofib…" matches "neurofibromin"). | Autocomplete / as-you-type search boxes. | Phrase semantics are stricter on word order; weaker for unordered keyword queries. |

These strategies are the main lever this harness exists to test, alongside index-side
config (custom analyzers and the `org.synapse.nf` synonym sets, not yet enabled).

## Results

Scores are not reproduced in this document. They change with every run, and a number copied
into prose goes stale the moment the next run lands. The dashboard reads the committed runs
directly and is the only place results are stated:

**<https://nf-osi.github.io/opensearch-ops/>** — recipe leaderboard, where correct answers
land, quality against latency, the lookup/discovery split, per-case ranks, and the searches
that currently fail.

Locally: `python3 build_site.py && python3 -m http.server -d site`. The runs behind it are
committed as JSON in [`results/`](results/); [`site.yaml`](../../site.yaml) decides which of
them are published.

### What the runs have shown that is not a number

Qualitative findings only — for the current standing of any strategy, read the dashboard.

- **Recall follows the field list.** The boosted and best-field strategies search a curated
  set ([`fields.yaml`](fields.yaml)) covering names, synonyms, RRIDs, and the discovery
  columns (`species`, `cellLineCategory`, `resourceType`, `vectorType`, …).
  `frontend_default` searches *all* fields, so it recalls respectably but carries noise from
  boilerplate columns into its ranking.

  > [!NOTE]
  > Runs before 2026-09-09 understate this. Five names in `fields.yaml` — including the
  > disease and manifestation columns this finding used to credit — did not exist on the
  > index, so the curated strategies were searching 10 fields, not 15, and no strategy but
  > `frontend_default` saw `manifestation` or `geneticDisorder` at all. Fixed in #22 and
  > re-scored on 2026-09-09; the dashboard reflects the corrected list.

- **A field list is a ceiling, not just a ranking.** `quoted-pdx-phrase` is the clearest
  case: all 12 patient-derived xenografts carry the literal phrase in
  `pdmModelSystemType`, one of the columns the 2026-08 table revision added. The platform
  default finds every one of them because it sends no field list; every strategy that does
  send one — production included — scores zero, purely because the column is not in it.
  Widening `fields.yaml` is a different lever from re-weighting it, and this is the case
  that separates them.
- **Binding the search config cost more than it bought.** `nf_tools_search_config` was
  bound to the index on 2026-09-09 and unbound the same day. Measured across two bound
  states, MRR fell for seven of nine strategies and rose for none. The per-case split is
  consistent: the abbreviation and discovery gaps the config was built for improve sharply
  — `pnf` and `cnf` go from missing to rank 1 — while distinctive-name lookups
  (`nf1-flox`, `nf1-grd`, `mpnst`, `lambda-greek-symbol`) fall just as sharply. The
  plausible mechanism is that broader matching dilutes the signal a rare token carries,
  but that is inference from the case split, not something isolated per analyzer: probes
  intended to confirm stemming on individual columns did not discriminate between the
  bound and unbound index. The verdict is also specific to this case mix — roughly
  two-thirds known-item — and a discovery-weighted golden set could reverse it. The index
  runs unbound today, so the platform default is measured with no customization at either
  layer.

  > [!NOTE]
  > A rebuild triggered by a config change does not alter the document count, so polling
  > `match_all` cannot tell you whether one has finished. Use a query whose result differs
  > between the two analyzer states — the KEYWORD columns are the reliable probe
  > (`race:black` returns 0 under KEYWORD, 9 without it).

- **Analyzer reach and ranking are different problems.** `nf1-cell-line-black` is the
  worked example. Its `race` column was both KEYWORD-analyzed (exact, case-sensitive) and
  missing from the field list; both were fixed, `black` against `race` alone now returns
  the whole pool at ranks 1 and 6-9, and the case's score did not move. The query's
  generic tokens swamp the one unboosted signal that answers it. Making a column
  reachable is a precondition for ranking it, not a substitute. The same holds in
  aggregate: adding `race`, `sex` and `investigatorName` to the searched fields moved no
  strategy by more than 0.001 MRR, with the index state held constant. Widening the field
  list buys recall only where the added column is what the query is actually about.

- **Fuzziness is a poor fit for this index.** NF tool data is identifier-heavy (RRIDs like
  `CVCL_8478`, clone names like `ipNF95.11b`), where fuzzy matching pulls in wrong hits
  without a recall payoff.
- **Phrase and word-order strictness hurt discovery.** Multi-word topical queries punish
  `phrase_prefix`, which is built for as-you-type rather than whole-query search.
- **Boosting moves topical queries, not known-item ones.** Over-boosting the name field can
  demote a correct synonym hit; see [Where field boosting actually matters](#where-field-boosting-actually-matters).
- **Six queries are true recall gaps** (`recall_gap_current: true` in the golden set): `pnf`,
  `melanoma-cell-line`, `metabolic-mouse-model`, `cafe-au-lait-spots`, `nf1-cell-line-black`,
  `nf1-bacterial-vector`. They hinge on low-signal or sparsely-annotated fields (race, the
  metabolic and café-au-lait manifestations, bacterial-expression vector type, the `pnf`
  abbreviation). The levers are the bound `nf_tools_search_config` (analyzer routing +
  synonyms, e.g. a `pnf`→plexiform synonym) and registry annotation (tagging the café-au-lait
  models) — not query-strategy tuning.
- **`k=10` undersells the discovery cases.** Many topical queries have an `expected_pool` far
  larger than their three-to-five item ideal head, so Recall@10 is a floor. nDCG over the
  ranked heads would discriminate better — a future harness addition.

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
| **Fields searched** (which columns the query covers) | frontend query (hard-coded in SRC) | **High** | Yes — the curated field set carries the strongest strategies. | The platform default hard-codes an all-fields query; adopting a curated `fields` list is a frontend src change. |
| **Query type** (best_fields / cross_fields / phrase) | frontend query (hard-coded in SRC) | **Medium** | Yes — query type spreads the field wider than boosting does, with `cross_fields` ahead and `phrase_prefix` behind. | `cross_fields` pairs best with the curated boosted fields. |
| **Field boosted** (`field^N`) | frontend query (hard-coded in SRC) | **Small–Medium** | Yes — boosting modestly beats equal weighting and combines with `cross_fields`; it moves topical and ambiguous queries, and does nothing for known-item lookups. | Over-boosting the name field can demote a correct synonym hit. |
| **Fuzziness** (currently `AUTO`) | frontend query (hard-coded in SRC) | **Small** (from turning it off) | Yes — the fuzzy strategies sit among the weakest; on identifier-heavy data (RRIDs, clone names) fuzziness adds noise with no recall payoff. | Cheapest, safest change |
| **Analyzers / synonyms** | config (index) | **Unknown — potentially high for the recall gaps** | Not yet measured: config is created but unbound. | Each iteration is a full delete-recreate-reindex, highest cost-per-experiment. |


### References

1. Broder (2002), AltaVista (1,000 queries). [SIGIR Forum PDF](https://sigir.org/files/forum/F2002/broder.pdf)
2. Jansen, Booth & Spink (2008), Dogpile logs. [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S030645730700163X)
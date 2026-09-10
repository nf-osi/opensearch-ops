# NF search benchmark: strategies and levers

This guide describes the search strategies evaluated for the **`nf-tools`** index, the NF
Research Tools Central registry. Current scores are published on the [dashboard](#results).

## Measurement

The benchmark uses a [golden set](golden.yaml) of queries with relevant tools. Each strategy
is run for every query and evaluated against the ranked results.

- **Known-item:** exact names, RRIDs, or established synonyms with a clearly defined result.
- **Topical:** broader discovery queries with multiple relevant tools. These require SME
  curation before their scores are used for decisions.

| Metric | Interpretation | Range |
| --- | --- | --- |
| **MRR** | Rank of the first relevant result, averaged across queries. | 0–1; higher is better |
| **Recall@10** | Share of relevant tools returned in the first 10 results. | 0–1; higher is better |
| **Hit@1** | Queries with a relevant first result. | 0–1; higher is better |
| **Hit@10** | Queries with at least one relevant result in the first 10. | 0–1; higher is better |

MRR and Hit@1 emphasize known-item and typeahead queries. Recall@10 is more useful for
discovery queries where users inspect a result list.

## Query strategies

Strategies vary by searched fields, field weights, and tolerance for word order or spelling.
The canonical fields and boosts are in [fields.yaml](fields.yaml); `field^N` assigns weight
`N` to a field.

| Strategy | Description | Primary use | Limitation |
| --- | --- | --- | --- |
| **production_current** | Current nf-tools query: best-fields over six production-weighted columns, without fuzziness. Quoted phrases and Synapse IDs use `simple_query_string`. | **Table control;** represents the deployed experience. | Searches explicitly configured fields. |
| **frontend_default** | Synapse platform default: fuzzy, all-fields `multi_match`, with phrase and Synapse-ID routing. | Reference for an unconfigured portal. | Not the nf-tools control; all-fields fuzzy matching can introduce noise. |
| **simple_query_string** | Equal-weight curated-field query supporting operators and quotes. | General-purpose search. | Does not prioritize names or identifiers. |
| **simple_query_string_boosted** | `simple_query_string` with benchmark boosts. | Phrase and identifier queries. | Query operators affect interpretation. |
| **multi_match_best** | Equal-weight best-fields matching. | Queries with one dominant field signal. | Does not reward evidence across fields. |
| **multi_match_boosted** | Best-fields matching with curated boosts. | Promoting canonical name, synonym, and RRID matches. | Poorly calibrated boosts can reduce relevant rankings. |
| **multi_match_cross** | Cross-fields matching with curated boosts. | Terms distributed across fields. | Can exclude loosely related results. |
| **boosted_fuzzy** | Boosted best-fields matching with `fuzziness: AUTO`. | Misspellings. | Produces noise for identifiers and clone names. |
| **phrase_prefix** | Prefix matching on the final term. | Typeahead. | Strict word order limits discovery. |

The harness evaluates these query-side options alongside index configuration, including
custom analyzers and `org.synapse.nf` synonym sets.

## Results

The dashboard is the authoritative source for scores, ranks, latency, and failures:

**<https://nf-osi.github.io/opensearch-ops/>**

Build it locally with `python3 build_site.py && python3 -m http.server -d site`. Published
runs are JSON files in [results/](results/); [site.yaml](../../site.yaml) selects them.

### Findings

- **Recall depends on field coverage.** Curated strategies search the fields in
  [fields.yaml](fields.yaml); `frontend_default` searches all fields and therefore finds
  more content, but also ranks boilerplate matches.

  > [!NOTE]
  > Runs before 2026-09-09 used five nonexistent field names, so curated strategies searched
  > 10 rather than 15 fields. This was corrected in #22 and the dashboard was re-scored.

- **A field list controls recall as well as ranking.** All 12 patient-derived xenografts in
  `quoted-pdx-phrase` contain the target phrase in `pdmModelSystemType`. The all-fields
  platform default retrieves them; strategies with a field list omit that column and return
  none. Expanding field coverage and changing field weights are separate interventions.

- **The bound search configuration did not improve this case mix.** On 2026-09-09, it
  improved abbreviation and discovery cases such as `pnf` and `cnf`, but reduced MRR for
  distinctive-name lookups. This conclusion is specific to a golden set weighted toward
  known-item queries; a discovery-weighted set may differ.

  > [!NOTE]
  > Document count does not indicate whether a configuration-triggered rebuild has completed.
  > Use a query with different results under each analyzer state; KEYWORD columns are useful
  > probes (`race:black` returns 0 with KEYWORD and 9 without it).

- **Field reach and ranking are distinct.** Adding `race`, `sex`, and `investigatorName`
  made those fields searchable but had little aggregate effect. For `nf1-cell-line-black`,
  generic query terms still outweigh the relevant unboosted `race` value. A field must be
  searchable before it can affect ranking, but reach alone does not ensure a ranking gain.

- **Benchmark coverage determines what a field evaluation can show.** `investigatorName`
  appeared inactive until `investigator-*` cases were added. Those queries show that omitting
  the column causes complete retrieval failure; boost tuning cannot compensate for a field
  that is not searched.

- **Fuzziness should be field-specific.** It is poorly suited to RRIDs and clone names, but
  can recover misspelled surnames. `investigator-gutmann-typo` also shows that it may rank
  unrelated institutions above the intended laboratory.

- **Phrase-prefix matching is unsuitable for broad discovery.** Its word-order constraints
  reduce performance on multi-word topical queries.

- **Six current recall gaps** (`recall_gap_current: true`) are `pnf`,
  `melanoma-cell-line`, `metabolic-mouse-model`, `cafe-au-lait-spots`,
  `nf1-cell-line-black`, and `nf1-bacterial-vector`. They require improved analyzers,
  synonyms, or registry annotations rather than query-strategy adjustments.

- **Recall@10 is conservative for discovery.** Some topical queries have an
  `expected_pool` larger than the relevant head. nDCG is a candidate future metric.

## Levers

### Evaluation priority

Discovery queries are the primary product objective; known-item queries represent re-finding
a specific resource. Field boosts are most useful when several documents match different
fields, as in topical queries. Exact known-item queries often already rank first, leaving
little opportunity for weighting to help.

For first-page retrieval, interpret Hit@10 changes as absolute percentage points: a 10-point
gain means 10 additional successful searches per 100 queries. A 7–15 point gain is a
medium-to-large practical improvement. Assess it with MRR or Hit@1 as well, since Hit@10
does not indicate where on the page the relevant result appears.

| Lever | Layer | Expected return | Status | Notes |
| --- | --- | --- | --- | --- |
| **Searched fields** | Frontend query | High | Measured | Field coverage has the largest effect on retrieval. |
| **Query type** | Frontend query | Medium | Measured | `cross_fields` generally exceeds `phrase_prefix`. |
| **Field boosts** | Frontend query | Small–medium | Measured | Most useful for topical and ambiguous queries. |
| **Fuzziness** | Frontend query | Small | Measured | Disabling broad fuzziness reduces identifier noise. |
| **Analyzers and synonyms** | Index configuration | High | Measured | Most relevant to current recall gaps; costly to iterate. |

### References

1. Broder (2002), AltaVista (1,000 queries). [SIGIR Forum PDF](https://sigir.org/files/forum/F2002/broder.pdf)
2. Jansen, Booth & Spink (2008), Dogpile logs. [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S030645730700163X)

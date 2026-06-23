# opensearch-ops

Testing and benchmarking workspace for tuning Synapse's OpenSearch-backed search:
indexes, analyzers, synonyms, and query parameters. The goal is to iterate on the
NF search index definitions and search configuration, and measure their effect on
result quality.

## What an "index object" is

The index objects are Synapse entities of type **`SearchIndex`**
(`org.sagebionetworks.repo.model.search.table.SearchIndex`), stored as children of
project [`syn74909065`](https://www.synapse.org/Synapse:syn74909065)
(*"Portal Search Search Index Collections"*).

Each `SearchIndex` is defined by a **`definingSQL`** query (`SELECT ... FROM <synId>`)
against a source Synapse table/view. That query is the content that gets indexed into
OpenSearch. The project holds SearchIndex objects for multiple DCC portals
(adkp, ampals, ark, b2ai, cckp, challenges, classic, dh, elite, **nf**); the `nf-`
prefixed ones below are ours.

> [!NOTE]
> `SearchIndex` is a distinct value (`searchindex`) in the Synapse `EntityType` enum.
> `POST /repo/v1/entity/children` returns these objects only when `searchindex` is included
> in `includeTypes`.

## NF index objects (`nf-` prefixed)

Inventory as of 2026-06-19. All are children of `syn74909065`.

| SearchIndex id | name | definingSQL (source) |
| --- | --- | --- |
| `syn75081630` | `nf-datasets` | `SELECT * FROM syn50913342` |
| `syn75081641` | `nf-development-publications` | `SELECT * FROM syn51735467` |
| `syn75081638` | `nf-funders` | `SELECT * FROM syn16858699` |
| `syn75081639` | `nf-hackathons` | `SELECT * FROM syn25585549` |
| `syn75081635` | `nf-initiatives` | `SELECT * FROM syn24189696 ORDER BY initiative ASC` |
| `syn75081643` | `nf-mutations` | `SELECT externalMutationID, alleleType, mutationType, mutationMethod, affectedGeneSymbol, affectedGeneName, sequenceVariation, proteinVariation, animalModelMutation, humanClinVarMutation, chromosome FROM syn51750823` |
| `syn75081640` | `nf-observations` | `SELECT * FROM syn51735464` |
| `syn75081637` | `nf-people` | `SELECT * FROM syn23564971` |
| `syn75081631` | `nf-publications` | `SELECT * FROM syn16857542` |
| `syn75081644` | `nf-publications-v2` | `SELECT * FROM syn51735450` |
| `syn75081633` | `nf-studies` | `SELECT * FROM syn52694652` |
| `syn75081642` | `nf-tool-study` | `SELECT * FROM syn26461958` |
| `syn75081636` | `nf-tools` | `SELECT * FROM syn51730943` |

## Search Management API

`SearchIndex` objects and their tuning knobs are managed by **Search Management
Services** (base `https://repo-prod.prod.sagebase.org/repo/v1`). Full docs:
<https://rest-docs.synapse.org/rest/index.html#org.sagebionetworks.repo.web.controller.SearchManagementController>

The index objects themselves are managed as entities:

| Action | Endpoint |
| --- | --- |
| List index objects in a project | `POST /entity/children` with `includeTypes:["searchindex"]` |
| Get an index object | `GET /entity/{id}` |
| Run a search query (async) | `POST /search/query/async/start` → `GET /search/query/async/get/{asyncToken}` |
| Autocomplete | `POST /search/autocomplete` |
| Bind/get/unbind a search config to an entity | `PUT` / `GET` / `DELETE /entity/{entityId}/searchconfig/binding` |

### Where to bind a config (best practice: on the `SearchIndex` object)

A `SearchConfiguration` is an **override** of the platform default analysis; it only
takes effect once it is *bound* to an entity, and the binding lookup
(`GET /entity/{id}/searchconfig/binding`) resolves **up the entity hierarchy** (it returns
the nearest config found on the entity or any ancestor). Per the rest-docs:

> Attach a `SearchConfiguration` to an entity (SearchIndex, Folder, or Project) by creating
> a binding.

So all three are legal targets — `SearchIndex` is explicitly allowed, and it's the one we want.

**Bind a table-specific config directly to its `SearchIndex` object** (e.g. the NF-tools
config → `syn75081636`), **not** to the folder/project. The reason is the entity layout:
all portals' indexes are flat children of one shared collection project
[`syn74909065`](https://www.synapse.org/Synapse:syn74909065), so a config bound at the
project level would resolve down to **every portal's** index, not just ours. The
`SearchIndex` object is the only entity with the right per-index scope. (Note the index
object and its `definingSQL` source table live in *different* project trees — the source
table's project, e.g. NF's `syn26338068`, is **not** an ancestor of the index object, so
binding there would not reach the index.)

The documented target list (SearchIndex → Folder → Project) is exactly the index object's
own ancestor chain, which is consistent with binding resolution anchoring on the
`SearchIndex` and walking up — so the index object is both an allowed and the
most-specific target.

> [!IMPORTANT]
> **Config/binding edits don't rebuild the index on their own — updating the `SearchIndex`
> entity does.** A rebuild fires on `SearchIndex` entity lifecycle events
> (create/update/delete), not on source-table changes: it deletes and recreates the index
> with the currently-bound config and re-streams all rows. Recipe: edit/bind the config, then
> **`PUT /entity/{id}`** on the `SearchIndex` to trigger a full rebuild.
>
> Constraints: creating/updating a `SearchIndex` entity is restricted to Sage
> employees/admins; indexing runs anonymously, so only public **OPEN_DATA** rows are indexed.
> Query-time changes (DSL boosts, fuzziness, the search-analyzer half of an override) need no
> rebuild.

> [!NOTE]
> **Current state:** the NF config objects exist (`nf_tools_search_config`, id `9`) but aren't
> bound yet. Binding and the rebuild touch require `UPDATE` on the `SearchIndex` entity
> `syn75081636` (which has its own ACL); the service account that creates the org-scoped
> objects lacks it, so this must be done by an authorized principal — via an ACL grant or an
> owner running the final steps. Check any entity's binding with [`check_config.py`](check_config.py).

The tuning objects (organization-scoped; list with optional `organizationName` filter):

| Object type | List | Get / Update |
| --- | --- | --- |
| Text analyzer | `POST /search/text/analyzer/list` | `GET` / `PUT /search/text/analyzer/{id}` |
| Column analyzer override | `POST /search/column/analyzer/override/list` | `GET` / `PUT /search/column/analyzer/override/{id}` |
| Synonym set | `POST /search/synonym/set/list` | `GET` / `PUT /search/synonym/set/{synonymSetId}` |
| Search configuration | `POST /search/configuration/list` | `GET` / `PUT /search/configuration/{searchConfigurationId}` |

### NF tuning objects (org `org.synapse.nf`)

| Type | id | name | notes |
| --- | --- | --- | --- |
| Synonym set | `16` | `standard_synonyms` | `synonym_graph`; `nf→neurofibromatosis`, `mpnst→…`, `pnf→plexiform neurofibroma`, etc. |
| Synonym set | `17` | `synonym_rules` | `synonym_graph`, empty (placeholder) |
| Text analyzer | `1017` | `nf_scientific_synonyms` | standard+lowercase+english stop/stemmer; `default_search` adds set 16 via `synonym_graph` (search-time only) |
| Column analyzer override | `9` | `nf_tools_columns` | per-column map for nf-tools: IDENTIFIER for id fields, `nf_scientific_synonyms` for discovery free-text, KEYWORD for clean categoricals |
| Search configuration | `9` | `nf_tools_search_config` | `defaultAnalyzer`=STANDARD + `columnAnalyzerOverrides`=[`nf_tools_columns`]; **created, not yet bound** (see below) |

These NF objects are versioned in [`config/`](config/) and (re)applied with
[`config/apply_config.py`](config/apply_config.py). They reference the platform's shared
built-in text analyzers (org `org.sagebionetworks`), available to any config:
`SCIENTIFIC` (1), `STANDARD` (2), `IDENTIFIER` (3), `KEYWORD` (4), `AUTOCOMPLETE` (5).

The custom `nf_scientific_synonyms` analyzer deliberately omits `word_delimiter_graph`:
OpenSearch parses each synonym definition through the filters *preceding* the
`synonym_graph` filter and rejects graph filters (like `word_delimiter_graph`) there, so
only `lowercase` precedes the synonyms. Identifier-style token splitting is instead handled
by the `IDENTIFIER` analyzer on the id columns.

## Querying an index (focus: `nf-tools`)

> [!IMPORTANT]
> **The deployed API differs from the public rest-docs.** The rest-docs OpenAPI describes a
> *structured* `SearchQuery` (`queryType`, `queryFields`, `termsFilters`, `fuzziness`,
> `limit`, …), but `repo-prod` no longer runs that model — it accepts a **raw OpenSearch
> query DSL** object in `searchQuery` and rejects the structured fields (`"JSON Element ...
> Unsupported: queryType/limit/queryText"`). This gives the client the full power of
> OpenSearch; benchmark against the raw DSL.

Query flow (async job pattern):
1. `POST /search/query/async/start` with a `SearchIndexQuery` → returns `{token}`.
2. `GET /search/query/async/get/{token}` → while still running it returns HTTP 200 with
   `jobState: "PROCESSING"` (keep polling); the final payload omits `jobState` and
   contains `hits`, `totalHits`, `selectColumns`.

Request body:
```json
{
  "concreteType": "org.sagebionetworks.repo.model.search.table.SearchIndexQuery",
  "searchIndexId": "syn75081636",
  "responseParts": ["HITS", "TOTAL_HITS", "SELECT_COLUMNS"],
  "searchQuery": { "query": { "match_all": {} }, "size": 3 }
}
```
`searchQuery` is raw OpenSearch DSL — `query`, `size`, `from`, `sort`, `highlight`, `aggs`, etc.
See the OpenSearch [Query DSL](https://docs.opensearch.org/latest/query-dsl/) docs, and in
particular [`multi_match` query types](https://docs.opensearch.org/latest/query-dsl/full-text/multi-match/)
(`best_fields`, `cross_fields`, `phrase`, `phrase_prefix`, …) — the `type` the benchmark
[strategies](benchmark/strategies.py) vary.

> [!WARNING]
> Synapse's JSON adapter rejects OpenSearch shorthand. Use the verbose object form for every
> clause — `{"match":{"description":{"query":"plexiform"}}}`, **not**
> `{"match":{"description":"plexiform"}}` (the latter errors with
> `JSONObject["description"] is not a JSONObject`).

**No authentication is required; these indexes are public and queries work anonymously.**

Use [`query.py`](query.py) (handles polling and field flattening):
```bash
python3 query.py '{"query":{"multi_match":{"query":"schwann","fields":["resourceName^3","description","synonyms"]}},"size":5}'
```

### `nf-tools` index (syn75081636)

- Source: `SELECT * FROM syn51730943` (NF Research Tools Central registry)
- ~1,216 documents, 46 indexed columns:

  `resourceId, rrid, resourceName, synonyms, description, resourceType,
  investigatorName, institution, investigatorSynapseId, orcid, usageRequirements,
  howToAcquire, species, cellLineCategory, cellLineGeneticDisorder,
  cellLineManifestation, backgroundStrain, backgroundSubstrain,
  animalModelGeneticDisorder, animalModelOfManifestation, insertName, insertSpecies,
  vectorType, targetAntigen, reactiveSpecies, hostOrganism, biobankName, biobankURL,
  specimenTissueType, specimenPreparationMethod, diseaseType, tumorType,
  specimenFormat, specimenType, contact, race, sex, age, dateAdded, dateModified,
  latestPublicationDate, completenessCategory, availabilityCategory,
  criticalInfoCategory, otherInfoCategory, observationCategory`

  Each hit returns `rowId`, `rowVersion`, `score`, and `fields` (column name/value
  pairs; multi-value columns are JSON-encoded strings).

## Benchmark harness

[`benchmark/`](benchmark/) compares query strategies against a golden (ground) relevance set so
we can measure the effect of config and query changes.

**Per-table layout.** Each table's benchmark lives in its own subfolder
`benchmark/<table>/` (e.g. [`benchmark/tools/`](benchmark/tools/) for `nf-tools`; later
`benchmark/studies/`, …), so goldens, docs, and results don't collide across tables. The
shared harness scripts live at `benchmark/` root.

Per-table (`benchmark/tools/`):
- [`golden.yaml`](benchmark/tools/golden.yaml) — test cases mapping a query to the
  `resourceId`s that should be retrieved (YAML, with inline comments, for easy SME review
  and curation). `known-item` cases have defensible exact ground truth; `topical` cases
  are seeded and flagged for human curation.
- [`GOLDEN.md`](benchmark/tools/GOLDEN.md) — dataset documentation for `golden.yaml`:
  case provenance (the `source:` field) and **coverage gaps** (queries deliberately not
  turned into cases, and why — schema gaps, cross-record links, other indexes).
- `results/<label>.json` — one file per run, diffable as config changes.

Shared harness (`benchmark/` root):
- [`benchmark/strategies.py`](benchmark/strategies.py) — query builders (`query_text → DSL`):
  `frontend_default` (mirrors the live frontend exactly — bare `multi_match` + `fuzziness:
  AUTO`, the production baseline), `simple_query_string`, `multi_match_best`,
  `multi_match_boosted`, `multi_match_cross`, `boosted_fuzzy`, `phrase_prefix`. The functions
  are table-agnostic; the field-boost lists
  default to nf-tools. A table whose schema differs can override by adding its own
  `benchmark/<table>/strategies.py` (run.py prefers it, else falls back here).
- [`benchmark/run.py`](benchmark/run.py) — runs every (case × strategy) for a table, scores
  **MRR**, **Recall@k**, **Hit@1**, **Hit@k**; prints a table and writes
  `benchmark/<table>/results/<label>.json` (`--label` defaults to `latest`).

Golden authoring (used to *create* cases, not run them) lives with its skill:
[`.claude/skills/generate-goldens/`](.claude/skills/generate-goldens/) holds `SKILL.md`
and `profile_table.py` (the schema-agnostic table profiler).

```bash
pip install pyyaml   # one-time; run.py reads the golden set from YAML
python3 benchmark/run.py tools                                                   # -> results/latest.json
python3 benchmark/run.py tools --label boost-v2 --strategy multi_match_boosted   # after editing benchmark/strategies.py
```

**Strategy explanations, metric definitions, and the latest results live in
[benchmark/tools/RESULTS.md](benchmark/tools/RESULTS.md)** — stakeholder-facing summary. Update
that doc for new results.

**What the live frontend actually has** (a bare `multi_match` + `fuzziness: AUTO`, no
field boosts) is documented with code line references in
[docs/INTEGRATION.md](docs/INTEGRATION.md).

**Cross-index / unified search** discussion for search across multiple object-type indices (tools, datasets, …) —
the federated (B) and rank-fusion (C) options — in
[docs/MULTI_INDEX.md](docs/MULTI_INDEX.md).

## Rechecking SearchIndex object inventory

No auth token needed — these objects are public, so `entity/children` and
`entity/{id}` both work anonymously.

```bash
B="https://repo-prod.prod.sagebase.org/repo/v1"

# List all SearchIndex objects in the project (filter for nf- in name)
curl -s -X POST "$B/entity/children" \
  -H "Content-Type: application/json" \
  -d '{"parentId":"syn74909065","includeTypes":["searchindex"]}'

# Inspect one index object's definingSQL
curl -s "$B/entity/syn75081636"
```

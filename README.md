# opensearch-ops

Workspace for testing and benchmarking Synapse OpenSearch indexes, analyzers, synonyms, and
query parameters. It supports iterative tuning of NF search indexes and measurement of search
quality.

## SearchIndex objects

Synapse `SearchIndex` entities (`org.sagebionetworks.repo.model.search.table.SearchIndex`) are
children of project [syn74909065](https://www.synapse.org/Synapse:syn74909065), *Portal Search
Search Index Collections*. Each has a `definingSQL` query against a Synapse table or view; its
result is indexed in OpenSearch. This shared project holds indexes for several DCC portals; the
`nf-` indexes are maintained here.

> [!NOTE]
> `SearchIndex` maps to the `searchindex` Synapse `EntityType`. Include `searchindex` in
> `POST /repo/v1/entity/children` requests to list these entities.

### NF indexes

Inventory as of 2026-06-19; all are children of `syn74909065`.

| SearchIndex ID | Name | definingSQL source |
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

Search Management Services use base URL `https://repo-prod.prod.sagebase.org/repo/v1`.
See the [SearchManagementController documentation](https://rest-docs.synapse.org/rest/index.html#org.sagebionetworks.repo.web.controller.SearchManagementController).

| Action | Endpoint |
| --- | --- |
| List index objects | `POST /entity/children` with `includeTypes:["searchindex"]` |
| Get an index object | `GET /entity/{id}` |
| Run an asynchronous search | `POST /search/query/async/start` → `GET /search/query/async/get/{asyncToken}` |
| Autocomplete | `POST /search/autocomplete` |
| Bind/get/unbind a project or folder configuration | `PUT` / `GET` / `DELETE /entity/{entityId}/searchconfig/binding` |
| Bind a configuration directly to an index | Set `searchConfigurationId` with `PUT /entity/{id}` |

### Binding configurations

Bind a table-specific configuration through the `SearchIndex` entity's
`searchConfigurationId` field. The generic binding endpoint accepts only Projects and Folders;
legacy `SearchIndex` bindings can still be resolved but not recreated through that endpoint.

Configuration can take effect in two ways:

1. **Direct:** set the optional `SearchIndex.searchConfigurationId` field with `PUT /entity/{id}`.
   Any SearchIndex entity update triggers a full rebuild.
2. **Inherited:** when that field is unset, Synapse uses the nearest Project or Folder binding.
   Platform defaults apply when no binding exists.

Use a direct binding for NF indexes. All portal indexes are children of the shared collection
project, so a project-level binding would affect every portal. The source table's project is not
an ancestor of the SearchIndex and cannot provide an inherited configuration.

> [!IMPORTANT]
> Updating `searchConfigurationId` triggers the rebuild; no separate update is required.
> Touch the entity only to rebuild without changing its effective configuration. SearchIndex
> updates require Sage employee or administrator permissions. Indexing is anonymous and includes
> only public `OPEN_DATA` rows. Query-time changes do not require rebuilding.

> [!NOTE]
> Registering a configuration does not bind it. A configuration listed under `org.synapse.nf`
> takes effect only once some index carries it in `searchConfigurationId`, so the two states
> have to be checked separately: [`config/config.py list`](config/config.py) shows what is
> registered, [`check`](config/config.py) shows what is actually bound, and
> [`apply <id> --index <name-or-synId>`](config/config.py) /
> [`unbind`](config/config.py) change it. Bindings move, so read the live entity rather than
> any list written down here — including the benchmark's assumption that `nf-tools` is the one
> `nf-` index with a configuration bound to it.

| Object | List | Get / update |
| --- | --- | --- |
| Text analyzer | `POST /search/text/analyzer/list` | `GET` / `PUT /search/text/analyzer/{id}` |
| Column analyzer override | `POST /search/column/analyzer/override/list` | `GET` / `PUT /search/column/analyzer/override/{id}` |
| Synonym set | `POST /search/synonym/set/list` | `GET` / `PUT /search/synonym/set/{synonymSetId}` |
| Search configuration | `POST /search/configuration/list` | `GET` / `PUT /search/configuration/{searchConfigurationId}` |

### NF tuning objects

The following objects are in `org.synapse.nf`.

| Type | ID | Name | Purpose |
| --- | --- | --- | --- |
| Synonym set | `16` | `standard_synonyms` | `synonym_graph`; NF terms such as `nf`, `mpnst`, and `pnf` |
| Synonym set | `17` | `synonym_rules` | Empty `synonym_graph` placeholder |
| Text analyzer | `1017` | `nf_scientific_synonyms` | Standard analysis plus lowercase, English stemming, and search-time set `16` |
| Column analyzer override | `9` | `nf_tools_columns` | `nf-tools` field-specific analyzers |
| Search configuration | `9` | `nf_tools_search_config` | STANDARD default plus `nf_tools_columns`; bound to `syn75081636` |

Other configured NF indexes use their own column override and search configuration while
sharing `nf_scientific_synonyms`. Run [`config/config.py list`](config/config.py) for the
current registry. Definitions are versioned in [config/](config/) and registered with
[`config/config.py register`](config/config.py), then applied with
[`config/config.py apply <id>`](config/config.py).

Built-in `org.sagebionetworks` analyzers are `SCIENTIFIC` (1), `STANDARD` (2), `IDENTIFIER`
(3), `KEYWORD` (4), and `AUTOCOMPLETE` (5). `nf_scientific_synonyms` omits
`word_delimiter_graph`: synonym definitions are parsed through preceding filters, and OpenSearch
rejects graph filters in that position. Identifier tokenization uses the `IDENTIFIER` analyzer.

## Querying indexes

The deployed API accepts a raw OpenSearch query DSL object in `searchQuery`, rather than the
structured `SearchQuery` described by the public REST documentation. It rejects structured
fields such as `queryType`, `limit`, and `queryText`. Public indexes can be queried
anonymously.

Use [query.py](query.py), which handles asynchronous polling and field flattening:

```bash
python3 query.py '{"query":{"multi_match":{"query":"schwann","fields":["resourceName^3","description","synonyms"]}},"size":5}'
```

The asynchronous flow is:

1. `POST /search/query/async/start` with a `SearchIndexQuery` returns `{token}`.
2. Poll `GET /search/query/async/get/{token}` while it returns `jobState: "PROCESSING"`.
   The completed response contains `hits`, `totalHits`, and `selectColumns`.

```json
{
  "concreteType": "org.sagebionetworks.repo.model.search.table.SearchIndexQuery",
  "searchIndexId": "syn75081636",
  "responseParts": ["HITS", "TOTAL_HITS", "SELECT_COLUMNS"],
  "searchQuery": { "query": { "match_all": {} }, "size": 3 }
}
```

`searchQuery` supports raw OpenSearch DSL, including `query`, `size`, `from`, `sort`,
`highlight`, and `aggs`. See the OpenSearch [Query DSL](https://docs.opensearch.org/latest/query-dsl/)
and [`multi_match` types](https://docs.opensearch.org/latest/query-dsl/full-text/multi-match/).

> [!WARNING]
> Synapse requires verbose object clauses. Use
> `{"match":{"description":{"query":"plexiform"}}}`, not
> `{"match":{"description":"plexiform"}}`.

### `nf-tools` (`syn75081636`)

Source: `SELECT * FROM syn51730943` (NF Research Tools Central). As of 2026-09-09, it has
approximately 1,218 documents and 70 indexed columns. Hits contain `rowId`, `rowVersion`,
`score`, and `fields`; multi-value fields are JSON-encoded strings.

The indexed columns are:

`resourceId, rrid, resourceName, synonyms, description, aiSummary, resourceType,
usageRequirements, howToAcquire, dateAdded, dateModified, geneticDisorder, manifestation,
tumorType, organ, tissue, cellLineCategory, backgroundStrain, backgroundSubstrain, animalState,
insertName, insertSpecies, vectorType, targetAntigen, reactiveSpecies, hostOrganism, conjugate,
biobankName, biobankURL, specimenTissueType, specimenPreparationMethod, specimenFormat,
specimenType, contact, pdmModelSystemType, pdmHostStrain, engraftmentSite, organoidType,
organoidModelType, organoidDerivationSource, organoidCellTypes, cultureSystem,
computationalToolType, computationalToolLanguage, computationalToolPlatformSupport, downloadURL,
licenseType, clinicalAssessmentType, clinicalAssessmentTargetPopulation,
clinicalAssessmentDiseaseSpecific, availabilityStatus, resistance, selectableMarker, softwareType,
modelType, availability, investigatorName, institution, investigatorSynapseId, orcid, species,
race, sex, age, latestPublicationDate, completenessCategory, availabilityCategory,
criticalInfoCategory, otherInfoCategory, observationCategory`

## Benchmark harness

[benchmark/](benchmark/) compares query strategies against per-table golden relevance sets.
Each table has `benchmark/<table>/golden.yaml`, `fields.yaml`, documentation, and result files;
the shared harness is at `benchmark/` root.

- [benchmark/strategies.py](benchmark/strategies.py) builds OpenSearch clauses for shared
  strategies. `production_current` is compiled from each table's `production:` configuration.
  Quoted phrases and Synapse IDs route to `simple_query_string`. A table may supply its own
  `strategies.py` when its schema requires it.
- [benchmark/run.py](benchmark/run.py) runs each case/strategy pair and records MRR, Recall@k,
  Hit@1, Hit@k, latency, and provenance in `benchmark/<table>/results/<label>.json`.

`control:` in a table's `fields.yaml` selects its baseline. For `nf-tools`, the control is
`production_current`, which represents the deployed portal query. Other uncustomized NF portals
use `frontend_default`.

> [!IMPORTANT]
> `frontend_default` represents the platform default only on an index without a bound
> SearchConfiguration. To measure it, unbind with [`config/config.py unbind`](config/config.py),
> score it under its own label, then re-bind. Keep that run as its own result file: it measured a
> different index state, so it is not interchangeable with a run scored while the configuration
> was bound. `run.py` warns when it scores `frontend_default` against an index that still has
> one, and [`site.yaml`](site.yaml)'s `constant:` key pins the dashboard's `frontend_default` row
> to that unbound run, so re-scoring the headline against today's configuration cannot silently
> move it.

```bash
pip install pyyaml
python3 benchmark/run.py tools
python3 benchmark/run.py tools --label boost-v2 --strategy multi_match_boosted
```

Every result records a golden fingerprint, a query configuration fingerprint, and the
`search_config_id` bound while it was scored, so a result file can say which dataset and which
index state it measured. Before scoring, `run.py` compares the run against the other result
files in the same `results/` directory and warns when they were not scored against the same
golden. See [benchmark/tools/RESULTS.md](benchmark/tools/RESULTS.md) for the `nf-tools` strategy
summary and interpretation guidance.

## Interactive site (benchmark results + search lab)

[`web/`](web/) is a static, dependency-free browser app (no build tooling, no backend) that
publishes the benchmark to a general audience. Two tabs:

- **Benchmark results** (the landing tab) — the reporting face. Reads the runs **committed**
  in `benchmark/<table>/results/`. Opens on a portfolio view of every
  `nf-` SearchIndex (which have a golden set, which have a config, the platform default vs the
  best recipe tested), then drills into one index: recipe leaderboard, where correct answers
  land, quality against latency, lookup vs discovery split, a per-case rank heatmap with
  filters, **the searches that fail today**, how the golden set was built, and the field
  boosts in play. Charts are custom SVG in [`web/charts.js`](web/charts.js). 
  Every figure carries a table view. 
- **Search lab** — the hands-on face. Type a query and compare two ranking *recipes* side by
  side with per-field boost editors, then score a recipe set against the golden set **live in
  the browser**. Live scoring reports the same metrics as
  `run.py`.

**Works against any SearchIndex, any portal.** while nf-tools is the default, the index
picker is **populated by listing every SearchIndex in the collection project**
(`syn74909065`) via `entity/children` — any portal's index is one click away. When a
non-curated index is chosen, the app discovers its columns live (`SELECT_COLUMNS`) and
**auto-generates a field-boost config** from column type + name heuristics
([`web/boostgen.js`](web/boostgen.js)).
Curated tables (those with a `golden.yaml`) ship hand-tuned boosts and enable scoring; any
other index runs in playground-only mode (no golden set → nothing to score against).
The query recipes are identical for every index.

Search lab is a visual **complement** to `benchmark/run.py`, not a replacement: `run.py` is the
engineer/CI path, the site is for generally accessible investigation. The recipes and scoring are JS ports
of [`strategies.py`](benchmark/strategies.py) and [`run.py`](benchmark/run.py); the golden
cases, field boosts and committed results are **generated** from the same YAML/JSON at build
time to prevent drift. The standing drift guard for the ported *logic* is a parity
check: live scoring's per-strategy MRR/Recall/Hit must match `python3 benchmark/run.py tools`
within rounding.

Every section is **linkable**: the URL carries `#/<tab>/<index>/<section>`, so
`#/results/tools/failures` opens the results tab on nf-tools scrolled to the failing
searches. Hovering a section heading reveals a control that copies that link
([`web/route.js`](web/route.js)).

> [!NOTE]
> The site optimizes **query-time** levers only (recipe, field boosts, fuzziness) and
> reflects the index's *current* production config. Index-time config (analyzers,
> synonyms in [`config/`](config/)) needs a Sage-admin index rebuild and isn't adjustable
> client-side.

### Usage

[`build_site.py`](build_site.py) assembles the site into `site/` (gitignored) — it copies
`web/` and emits:

- `site/data/<table>.json` — the golden set with its per-case provenance, the field/boost
  config, and **every** committed run in `results/` including per-case detail (what the rank
  heatmap, rank bands and failure list are built from);
- `site/data/manifest.json` — the portfolio: the `nf-` index inventory (parsed from the table
  in this README), which indexes have a golden set or a committed config, and each table's
  headline run metrics.

### Publishing config — [`site.yaml`](site.yaml)

Publishing is declared by `build_site.py` reading `site.yaml`.

| Key | Meaning |
| --- | --- |
| `headline` | The run of record (`latest` — `run.py`'s default label). Published for every table that has it, and the run each index opens on, so an extra run with a newer timestamp can't headline an index with a partial strategy set. |
| `extra_runs` | Additional labels per table, in run-switcher order. Empty at present — the two other runs on disk are both excluded, for the reasons recorded there. |
| `constant` | Pins one strategy's row to a fixed index state by taking it from another run of the same table. Used for `tools: frontend_default`, which is taken from the `unbound` run so the platform-default control is not measured on a configured index. The build refuses the splice if the two runs scored different goldens. |
| `excluded` | Runs in `results/` deliberately not published, **with the reason**. Listing one turns a skipped file from an oversight into a decision. |

A run reaches the published site only if `site.yaml` names it **and** it is committed.
The build reports every case:

```
held constant: tools/latest: frontend_default taken from unbound
excluded by site.yaml: tools/unbound, studies/tuned
WARNING: unpublished run not accounted for: … — add it to extra_runs or excluded in site.yaml
WARNING: <table>: no dataset fingerprint on latest — re-score under the same label(s) to make
         comparability checkable
```

The fingerprint warning is expected for `datasets`, `publications`, `studies` and
`usage-publications`: their committed runs predate the provenance fields and clear once each is
re-scored.

The resolved selection is recorded in `site/data/manifest.json` (`runs_published`,
`headline_label`), so a published payload states the policy it was built under.
[`benchmark.yml`](.github/workflows/benchmark.yml) runs the benchmark, while
[`publish.yml`](.github/workflows/publish.yml) builds and publishes the dashboard from committed
results. The benchmark workflow lets the operator select a table with a golden set and opens or
updates a pull request for review of that table's generated `results/latest.json` before it can
be published.

```bash
pip install pyyaml
python3 benchmark/run.py tools --label latest   # refresh the run of record, then commit it
python3 build_site.py                           # -> site/ (prints what it published & skipped)
python3 -m http.server -d site                  # open http://localhost:8000
```

## Index health monitoring

[monitor/check_index.py](monitor/check_index.py) verifies each `nf-` index by running
`match_all` and comparing its document count with a live `SELECT COUNT(*)` against the source
parsed from `definingSQL`. The default permitted difference is
`max(3 rows, 2% of source rows)`. The absolute threshold accommodates small-index lag; the
relative threshold detects material gaps in large indexes.

```bash
python3 monitor/check_index.py
python3 monitor/check_index.py syn75081636
python3 monitor/check_index.py --repair
```

`--repair` rebuilds `EMPTY`, `MISMATCH`, and `UNQUERYABLE` indexes. `UNVERIFIED` indicates that
the source query or count could not be evaluated and is not repaired automatically; `ERROR`
indicates an unreadable entity. Unverified indexes are never reported healthy.

Repair uses `config/config.py` rebuild logic and requires `$NF_SERVICE_TOKEN`; checking does not.
The command exits nonzero for unhealthy indexes and always writes `--out`, including on fatal
errors. [.github/workflows/index-monitor.yml](.github/workflows/index-monitor.yml) runs daily:
it checks, optionally repairs when the token secret is configured, rechecks, and opens or closes
an `index-health` GitHub issue.

## Refreshing index inventory

Public index entities can be queried anonymously.

```bash
B="https://repo-prod.prod.sagebase.org/repo/v1"

curl -s -X POST "$B/entity/children" \
  -H "Content-Type: application/json" \
  -d '{"parentId":"syn74909065","includeTypes":["searchindex"]}'

curl -s "$B/entity/syn75081636"
```

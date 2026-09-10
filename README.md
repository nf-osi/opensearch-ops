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
> was bound. `run.py` warns when it scores `frontend_default` against an index that still has one.

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

## Interactive site

[web/](web/) is the Synapse Portal Search Lab: a static browser application with no build
dependencies and no backend, querying the public repo-prod API from the page. Two tabs:

- **Search playground:** runs a query through two recipes side by side and compares the ranked
  results, with badges for how far each hit moved between them.
- **Benchmark scoreboard:** scores the selected recipes over the table's golden set live in the
  browser, or shows the scoreboard embedded at build time.

A sidebar edits per-field boosts, which apply to the boosted recipes in both tabs; only free-text
columns (`STRING`, `STRING_LIST`, `LARGETEXT`, `MEDIUMTEXT`) are eligible match targets. The site
can adjust query-time recipes only — analyzer and synonym changes need a configuration change and
a Sage-admin rebuild.

The index picker lists every SearchIndex in `syn74909065` and also takes a pasted synId. A
curated table loads its golden set and versioned boosts from `site/data/<table>.json` and can be
scored; any other index has its columns discovered live through `SELECT_COLUMNS` and its boosts
generated from column names and types, so it is playground-only.

> [!NOTE]
> The browser recipes in [web/strategies.js](web/strategies.js) mirror only a subset of
> [benchmark/strategies.py](benchmark/strategies.py) — they currently omit `production_current`,
> `simple_query_string_boosted`, and the quoted-phrase / Synapse-ID routing. An in-browser score
> is therefore not a substitute for a `run.py` run.

### Build and publication

[build_site.py](build_site.py) creates the ignored `site/` directory, copying [web/](web/) and
writing:

- `site/data/<table>.json`: the table's golden cases, its `fields.yaml` boosts, and a slim
  scoreboard read from `benchmark/<table>/results/latest.json` when that file exists.
- `site/data/manifest.json`: the table-to-SearchIndex inventory the index picker reads.

Both are generated from the same YAML the harness reads, so the golden set and boosts stay
single-sourced. `latest.json` is the only result file embedded — other runs in `results/` are
kept for comparison but not published, and a table without a `latest.json` simply ships with no
precomputed scoreboard.

[.github/workflows/benchmark.yml](.github/workflows/benchmark.yml) is a manually triggered
workflow that runs the benchmark, builds the site, and deploys it to GitHub Pages. Note that it
scores under its own label rather than `latest`, so it publishes the committed scoreboard rather
than the run it just made.

```bash
pip install pyyaml
python3 benchmark/run.py tools --label latest
python3 build_site.py
python3 -m http.server -d site
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

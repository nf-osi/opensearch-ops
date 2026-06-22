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

> Listing note: `SearchIndex` is a distinct value (`searchindex`) in the Synapse
> `EntityType` enum. `POST /repo/v1/entity/children` returns these objects only when
> `searchindex` is included in `includeTypes`.

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
| Synonym set | `16` | `standard_synonyms` | created `synonym_graph`, to be updated |
| Synonym set | `17` | `synonym_rules` | created `synonym_graph` to be updated |

NF-owned search configuration, text analyzer, or column analyzer overrides are being created and refined. 
In the meanwhile, we use platform's "off-the-shelf" shared built-in text analyzers (org `org.sagebionetworks`) available to any config: 
`SCIENTIFIC` (1), `STANDARD` (2), `IDENTIFIER` (3), `KEYWORD` (4), `AUTOCOMPLETE` (5).

## Querying an index (focus: `nf-tools`)

> **Potential doc vs. deployed mismatch (important).** The public rest-docs OpenAPI describes a
> *structured* `SearchQuery` (`queryType`, `queryFields`, `termsFilters`, `fuzziness`,
> `limit`, …). That model is **not** what `repo-prod` currently runs; there has been recent API changes.
> The deployed API now accepts a **raw OpenSearch query DSL** object in `searchQuery` and rejects the
> structured fields (`"JSON Element ... Unsupported: queryType/limit/queryText"`).
> **This lets the frontend client get the full power of OpenSearch.**
> Benchmark against the raw DSL.

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

> Syntax gotcha: Synapse's JSON adapter rejects OpenSearch shorthand. Use the verbose
> object form for every clause — `{"match":{"description":{"query":"plexiform"}}}`,
> **not** `{"match":{"description":"plexiform"}}` (the latter errors with
> `JSONObject["description"] is not a JSONObject`).

**No authentication is required; auth should not be used — these indexes are public and queries work anonymously.**

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


---
name: goldie
description: Generate benchmark golden relevance cases for a Synapse SearchIndex table. Use when the user wants to expand benchmark with new, SME-style search queries for a given table/index ("generate goldens for <table>", "add benchmark cases for this index", "make more relevance test queries"). For this workflow, profile the table, reason like a domain SME to invent realistic discovery queries, and select the ideal ranked head set of results for each.
dependencies: python>=3.8, pyyaml>=5.1
---

# Generating golden relevance cases

Your job: given a Synapse SearchIndex table, act like a **subject-matter expert (SME)
for that data domain** and produce realistic search queries plus the *ideal* set of
results each should return in a specific `golden.yaml` format. These become ground
truth for the search benchmark, so they must reflect what a knowledgeable user would
search for *and* what a *perfect* search would return, **not** what the current search
happens to return.

Your tools all live in `.claude/skills/goldie/scripts/` — run them from the repo root:
- `profile_index.py <INDEX_ID>` — profile the *index* sample (roles, feel). Order-biased; not ground truth.
- `profile_table.py <SRC_TABLE_ID>` (or `<INDEX_ID> --index-id`) — pull the *real* category
  distributions/counts from the source table (your topical seeds + `expected_pool`). This is the oracle.
- `synapse_client.py` — the repo-prod client the other scripts import (`_call` / `search` /
  `hit_dict`); also runnable as a CLI to dump raw index records (see Step 1).
- `validate_golden.py <GOLDEN_FILE>` — structural checks **plus** the mandatory live gate that
  every `relevant` id actually exists in the index.

**Two different APIs, used for two different things — do not confuse them:**
- **The Synapse Table Query API** (SQL against the `definingSQL` *source table*) is your
  **ground-truth oracle**. The relevant set for every case is derived from source-table
  data, not from search. See Step 1/Step 3. Source-table queries go through
  `profile_table.tquery(...)` (built on `synapse_client._call`). Public tables work anonymously.
- **The SearchIndex query API** (`synapse_client.search`, anonymous) runs the *actual search*
  you are benchmarking. Use it ONLY to (a) profile the index and (b) compute the derived
  `recall_gap_current` flag *if the user explicitly asks for it* (optional — off by default).
  **Never** use it to decide what the relevant answers are.

## Inputs

The user points you at a table. Accept either:
- a **SearchIndex id** (`synXXXXXXXX`, type `SearchIndex`) — use it directly; or
- a **source table id** (the `definingSQL` source). Resolve it to its SearchIndex:
  ```bash
  curl -s -X POST "https://repo-prod.prod.sagebase.org/repo/v1/entity/children" \
    -H "Content-Type: application/json" \
    -d '{"parentId":"syn74909065","includeTypes":["searchindex"]}' \
  | python3 -c "import sys,json; [print(o['id'],o['name']) for o in json.load(sys.stdin)['page']]"
  ```
  then `GET /entity/{id}` to confirm its `definingSQL` references the source table.

**No SearchIndex built yet?** That's fine — proceed. Golden generation is entirely
source-derived, so a source table with no index still gets a full golden set. Only the two
**index-dependent** steps defer until the index exists: the optional `recall_gap_current`
flag (Step 3) and the live id-in-index gate in `validate_golden.py` (Step 5). Point
`profile_table.py` (no `--index-id` — you already have the source table id) and
`validate_golden.py` at the **source table id** directly; author the golden now, leave
`index:` blank (or a placeholder) in the header, and note that it must be re-validated with
the live gate once the index is built. `profile_index.py` and the `match_all` size check
below need an index and are simply skipped — use the source table for schema, vocabulary,
and row count instead.

Also accept an optional target count (default: 15 new cases) and any focus the
user gives (e.g. "more antibody queries").

## STOP — size gate (run this before anything else)

The size that matters is the **source table's true row count** — read it from the source
profiler, NOT from the search index:

```bash
python3 .claude/skills/goldie/scripts/profile_table.py <SRC_TABLE_ID>            # you have the source table id
python3 .claude/skills/goldie/scripts/profile_table.py <INDEX_ID> --index-id     # you only have the SearchIndex id
# either way, read the `source rows:` line
```

`source rows:` is a real `COUNT(*)` from the ground-truth oracle; it's authoritative and
available whether or not an index exists. (Don't size off the index `match_all` totalHits —
it only approximates the source, and is absent before the index is built.)

Relevance is derived from source SQL — exact `COUNT(*)`, paged `GROUP BY`, unbounded
`WHERE` — so a large *per-query* pool is NOT a problem: `expected_pool` is the exact source
count and `relevant` is only the ideal head (see the 238-row `melanoma-cell-line` case).
What still degrades as the table grows is your ability to (a) achieve genuine topical
*coverage* by synthesizing queries and (b) hand-verify membership / reject false friends at
scale. Gate on that — **do not skip this.**

- **≤ ~2,000 rows → ACCEPT.** The `GROUP BY` vocabulary from `profile_table.py` covers the
  category space; proceed normally.

- **~2,000 < rows ≤ ~10,000 → ACCEPT ONLY IF you scope and disclose.** Lean on the source
  `GROUP BY` distributions for coverage, and read a few hundred real rows for feel
  (`tquery("SELECT * FROM <SRC> ...", SRC)`); generate only queries you can ground in that
  vocabulary plus solid domain knowledge; tell the user up front that coverage is partial and
  sub-domains you didn't cover aren't covered.

- **> ~10,000 rows → REJECT whole-table generation. Do not proceed.** At this scale
  synthesized queries can't credibly cover a heterogeneous table and you can't hand-verify
  membership across it. Offer the real alternatives and stop:
  - mine **actual user query logs / search analytics** (these beat synthesized queries), or
  - re-invoke this skill on a **narrow, well-scoped slice** (one category/filter) small
    enough to curate and verify.
  Resume only if the user explicitly narrows the job to such a slice.

**Also REJECT or force-narrow regardless of row count when** the columns you'd build queries
from are high-cardinality free text (thousands of distinct names/abstracts) that no `GROUP
BY` collapses into clean category seeds, or the table is heavily used and query logs exist —
prefer the logs.

Exception: exact `known-item` / sanity-check point-lookups are safe at any size. If the user
only wants those, you may proceed; it is topical curation and query *coverage* that fail as
the table grows. You are a **bootstrap** for small-to-medium tables without query logs — not
a tool for large indexes. When in doubt, reject and explain rather than emit
goldens you cannot stand behind.

## Step 1 — Profile the table (discover the schema — assume NOTHING)

**Every table is different, especially across portals.** Do not assume any particular
column names exist. Discover the schema from the data.

Run the profiler — it infers each column's *role* (identifier / name / category /
free-text) without hardcoding names:

```bash
python3 .claude/skills/goldie/scripts/profile_index.py <INDEX_ID> --n 100
```

Then read full records for vocabulary and feel:

```bash
python3 .claude/skills/goldie/scripts/synapse_client.py '{"query":{"match_all":{}},"size":20}' <INDEX_ID>
```

> **The index `match_all` sample is order-biased — do not trust it for coverage.** It
> returns the first N rows in index order, which often over-represents one or two resource
> kinds and hides others entirely. (Real case: the first 100 nf-tools rows were only Cell
> Line + Antibody, so a sample-based pass never saw Animal Model, Biobank, Genetic Reagent,
> PDX, or Organoid Protocol rows at all.) Use the index sample for *feel*, but get the real
> vocabulary and counts from the source table below.

### Profile the SOURCE table (the ground-truth schema + vocabularies)

The source table is where you learn the TRUE controlled vocabularies and pool sizes 
independent of search. **Use `profile_table.py`; do not hand-write the Table Query loop.**
Given a source table id directly (the default), it queries it as-is; given `--index-id`, it
resolves the SearchIndex to its `definingSQL` source table first. Either way it prints the
column models (name / type / facet), and — for every enumeration and multi-value column —
the *real* value distribution with counts straight from the source (full-table GROUP BYs,
not a sample):

```bash
python3 .claude/skills/goldie/scripts/profile_table.py <SRC_TABLE_ID>                        # auto: enum + list cols
python3 .claude/skills/goldie/scripts/profile_table.py <SRC_TABLE_ID> --col age --col race   # specific cols
python3 .claude/skills/goldie/scripts/profile_table.py <INDEX_ID> --index-id                 # only have the index id
```

Those counts are your topical-query **seeds** AND your `expected_pool` numbers. For anything
`profile_table.py` doesn't cover (an ad-hoc predicate, a `LIKE` on a name), import its
`tquery(sql, src)` / `resolve(index_id)` / `rows(bundle)` helpers rather than
re-implementing the async poll. **`tquery()` returns the raw Synapse query bundle, not a
list of rows** — its actual data is nested at `queryResult.queryResults.rows[].values`, not
a flat `bundle["rows"]` you might guess at. Always unpack it with `rows(bundle)`:

```python
import sys; sys.path.insert(0, ".claude/skills/goldie/scripts")
from profile_table import resolve, tquery, rows
SRC, _ = resolve("<INDEX_ID>")           # -> the definingSQL source table id (only needed
                                          # if you don't already have it)
bundle = tquery('SELECT resourceId, resourceName FROM %s WHERE resourceName LIKE \'%%KRAS%%\'' % SRC, SRC)
for resource_id, resource_name in rows(bundle):
    print(resource_id, resource_name)
```

(Pass `token=...` to `resolve`/`tquery` only if the source table isn't public.)

Note the source table's column *types*: multi-value columns are JSON-array lists, queried
with `HAS (...)` (e.g. `WHERE species HAS ('Sus scrofa')`); scalar text columns use `=` or
`LIKE '%...%'`. You'll use these exact predicates to build relevant sets in Step 3.

From the profile, establish (and verify against the records — the roles are heuristic):

- **The identifier column → `id_field`.** The column whose values you'll list under
  `relevant`, and what the runner matches hits on. Pick a stable, ~unique, high-fill column
  (the profiler's "identifier candidates"). It varies by table/portal — `resourceId`, a
  Synapse id, an accession, a DOI, etc. **If no column is a clean identifier, use the
  index's own `rowId`** (present on every hit; set `id_field: rowId`).
- **The display-name column** (profiler "name/title candidates") — for the human-readable
  `# comment` next to each id, and a primary search target.
- **Category columns** (low-cardinality) — the richest seeds for *topical* queries: each
  distinct value (a disease, species, assay, technique, vendor, modality, study…) is a
  candidate discovery query. Read the value distributions the profiler prints.
- **Free-text columns** (descriptions/abstracts) — concept queries, and where to verify
  relevance.
- **The domain & vocabulary** generally: what is this table about, and how would a
  researcher in that field phrase a search, especially using domain-specific jargon and shorthands? 
  Research tools (cell lines, animal models, antibodies) is *one* example — 
  a datasets, publications, studies, or people table needs entirely 
  different queries and a different `id_field`.

## Step 2 — Invent realistic queries (think like an SME)

Generate queries a real researcher in this domain would type into search. Aim for
the distribution below.

- **Most cases must be `type: topical`** (discovery / exploratory): a concept, category,
  disease, technique, target, species, or abbreviation where *several* instances are
  legitimately relevant. These are what portal search is really for. Examples for research tools:
  `optic glioma`, `schwann cell`, `nonsense mutation`, `pten mouse model`, `gfap-cre`,
  `hybridoma cell line` — note these are mostly qualified/multi-word, per the next point.
- **Avoid bare one-word queries as the default** — this shapes the examples above. Real
  users are usually more specific, so prefer qualified multi-word phrasing: `kras mutant`
  not `kras`, `pten mouse model` not `pten`, `melanoma cell line` not `melanoma`.
  (Specificity also exposes ranking gaps a single word hides: `melanoma` ranked the lines #1,
  but `melanoma cell line` buried all of them past rank 20 — the extra generic tokens swamped
  the manifestation match. That gap is only visible with the realistic query.) A few bare
  terms are fine for testing exactly that contrast, but they should be the minority.
- **A few `type: known-item`** (lookup): a specific named tool / identifier the user wants
  to re-find. Include **at most one or two trivial sanity checks** (an exact full resource
  name, or an exact RRID) — mark them clearly as sanity checks in `notes`; they confirm the
  harness works but don't discriminate between strategies.
- **Match how users actually type: lowercase, terse, real shorthands and jargon — not
  spelled-out forms.** Researchers type `pdx` not `patient-derived xenograft`, `ipsc` not
  `induced pluripotent stem cell`, `mpnst`/`cnf`/`pnf` not the expansions, `gfap-cre` not
  `glial fibrillary acidic protein cre`; expect partial identifiers and occasional
  misspellings too. Mix query "shapes": bare concept vs. qualified (`neurofibromin` vs
  `neurofibromin antibody`) — these can rank very differently and both are useful (keeping
  bare terms the minority, per above).
- Cover the table's breadth: different diseases, techniques, species, papers —
  not ten variants of one thing.

## Step 3 — Build the ideal head set per query (the hard part)

For each query, decide the **ideal ranked head**: the handful of results (usually 3–5) a
*perfect* search would return first, ordered best-first. This is SME ground truth, derived
from the **source table**, **independent of what the current search returns**.

**Derive the relevant set by querying the SOURCE TABLE, never by searching the index.**
Translate the query's intent into a source-table predicate over the metadata columns or
names you profiled in Step 1, run it with `tquery(sql, SRC)` (from `profile_table.py`, see
Step 1), unpack it with **`rows(bundle)`** (never guess at the bundle's shape — it's nested,
not a flat `bundle["rows"]`), and take the result rows as the true relevant pool. Examples
(substitute YOUR columns/values):

```python
# categorical intent  -> filter the controlled-vocab column
rows(tquery(f"SELECT resourceId, resourceName FROM {SRC} WHERE resourceType='Patient-Derived Model'", SRC))          # 'pdx'
rows(tquery(f"SELECT resourceId, resourceName FROM {SRC} WHERE species HAS ('Sus scrofa')", SRC))                     # 'minipig model'
rows(tquery(f"SELECT resourceId, resourceName FROM {SRC} WHERE cellLineManifestation HAS ('Melanoma','Cutaneous Melanoma')", SRC))  # 'melanoma cell line'
# gene / reagent intent with no data column -> LIKE on the stored name/synonyms
rows(tquery(f"SELECT resourceId, resourceName FROM {SRC} WHERE resourceName LIKE '%KRAS%' AND resourceName LIKE '%G12%'", SRC))      # 'kras mutant'
```

Then:
1. **`expected_pool` = the row count the source query returns** — a real number, not a guess.
2. **Pick the ideal head** (strongest 3–5) from those rows and order best-first (canonical /
   most-representative members first). The order encodes the target ranking for nDCG.
3. **Sanity-check membership, reject false friends.** A `LIKE` can over-match (a shared
   substring) and `HAS` can under-match (a value spelled differently). Read the borderline
   rows' full records and keep only genuine semantic matches — e.g. exclude a Drosophila
   *antibody* from a `species HAS ('Drosophila melanogaster')` fly-*model* set, exclude
   zebrafish rows from an `nf1 p53 mouse` set. Tighten the predicate rather than hand-prune
   when you can.
4. **The head can and should include items the current search misses** — that's the recall
   signal the benchmark exists to catch, and it's exactly what source-derived ground truth
   makes possible.

Keep the head focused (the strongest 3–5) even when `expected_pool` is large; the
`relevant` list is just the ideal *head*.

### (Optional, only on request) Compute `recall_gap_current`

**Do NOT compute this by default.** It is the one legitimate use of the search index in
this skill, but only do it when the user explicitly asks for recall-gap flags. By default,
omit `recall_gap_current` entirely — generating goldens is about ground truth, and the gap
flag is a separate live-verification step. **If no index exists yet it cannot be computed at
all** — omit it and note it's deferred until the index is built.

When requested: after the relevant set is fixed from the source table, run the
**frontend-style** query against the index and check whether ANY relevant id lands in the
top 20 (Hit@20):

```python
import sys; sys.path.insert(0, ".claude/skills/goldie/scripts")
from synapse_client import search, hit_dict
r = search("<INDEX_ID>", {"query":{"multi_match":{"query":"<query>","fuzziness":"AUTO"}},"size":20})
top20 = [hit_dict(h)["<id_field>"] for h in r.get("hits",[])]
gap = not any(rid in top20 for rid in relevant_ids)   # True = real recall gap
```

`recall_gap_current: true` means none of the ideal answers reach the top 20 — a real gap.
`false` means at least one does (surfacing more / ranking higher is a separate
ranking-depth concern). Record it per case (verified live).

## Step 4 — Emit the cases (golden.yaml format)

### The `golden.yaml` schema (complete — this skill is self-contained)

A `golden.yaml` is a header (which index/table + how the runner matches) followed by a list
of `cases`. This is the full spec; you do NOT need the `benchmark/` files to author one.

```yaml
# ---- header (once per file) ----
index:      synXXXXXXXX     # the SearchIndex id the cases run against. Leave blank / "# TODO"
                            #   if the index isn't built yet (see the note below).
index_name: <slug>          # human label, e.g. nf-tools.
version:    2026.07.05      # dataset version of THIS golden set. Default: today's date as
                            #   YYYY.MM.DD (dotted). Use a semantic version (e.g. 2.1.0) ONLY
                            #   if the user specifies one. Bump it whenever you add/curate cases.
k:          10              # cutoff for Recall@k / Hit@k in the runner.
id_field:   resourceId      # THE KEY FIELD: which column the `relevant` ids below are values
                            #   of, i.e. what the runner matches hits on. Varies by table
                            #   (resourceId, an accession, a Synapse id…). Use "rowId" to key
                            #   on the index's own per-row id when no column is a stable id.

cases:
  - id: <short-kebab-slug>    # REQUIRED. Unique within the file.
    query: <what the user types>   # REQUIRED. The search-box string.
    type: topical             # REQUIRED. topical | known-item (see taxonomy below).
    source: agent-generated   # provenance tag; this skill always writes agent-generated.
    notes: "Why these are the ideal answers; caveats; mark sanity checks."  # free text.
    relevant:                 # REQUIRED, non-empty. The ideal ranked HEAD, best-first.
      - <id_field value>   # <display name>   # order encodes the target ranking (nDCG).
      - <id_field value>   # <display name>
    # ---- optional fields ----
    expected_pool: 24         # true count of relevant rows from your Step 3 source query,
                              #   when it's larger than the head you listed. `relevant` is
                              #   just the head of this pool, so Recall@k is a floor.
    entity_types: [Cell Line] # OMIT unless the table distinguishes different resource kinds AND a
                              #   query's results span more than one
    recall_gap_current: false # OPTIONAL, omit by default (see Step 3). A live Hit@20 flag.
    source_url: "https://…"   # OPTIONAL provenance link.
```

**`type` taxonomy** (standard IR search taxonomy, for SMEs who aren't search specialists):
- **`known-item`** ≈ *navigational* / lookup — the user has ONE specific tool in mind and
  wants to re-find it (exact name, RRID, a specific identifier). One defensible right answer.
- **`topical`** ≈ *informational* / exploratory (discovery) — the user explores what tools
  exist on a concept; several are legitimately relevant. **MOST cases must be topical.**
- (Refs: Broder, *A taxonomy of web search*, SIGIR Forum 2002; Marchionini, *Exploratory
  search: from finding to understanding*, CACM 2006.)

**Do NOT emit** `recall_gap_legacy_search` (compares against the retired MySQL search — only
meaningful on cases carried over from that comparison), and do not invent other fields.

### Where do the cases go?

Write your output files directly to the outputs directory the caller gave you (`<OUT_DIR>`): `golden.yaml` and `README.md`
(the dataset-documentation companion, see Step 5).
- **Appending to an existing table's golden set**: if a `golden.yaml` for this table was
  already available when you started, rather than generated from scratch, treat it as the base — append your
  new cases under a clearly-marked provenance block. The `relevant` ids MUST be values of
  that file's `id_field`. **Bump the header `version`, following its current convention.**
  Write the full merged file to `<OUT_DIR>/golden.yaml`.
- **A new table/index**: write a fresh `golden.yaml` — header `index`, `index_name`,
  **`version`**, `k`, and **`id_field`** set to the identifier column you chose in Step 1 (or
  `rowId`). Set `version` to today's date as `YYYY.MM.DD` (dotted) unless the user specifies a
  different convention such as semver. Don't mix indexes in one file. **If the SearchIndex
  isn't built yet, leave `index:` blank (or a `# TODO` placeholder)** and say in your summary
  that it needs filling in — then re-validating — once the index exists; the golden itself is
  otherwise complete (`benchmark/run.py` just can't execute against it until `index` is set).

Emit each case per the schema above. Beyond the field semantics already given there, follow
these authoring rules:
- Always put a `# <display name>` comment after each id so a reviewer can scan them.
- **Quote** any `notes`/`query` value containing a colon-space or `#` (YAML treats a bare `#`
  as a comment — this has bitten us before).

Example block header to prepend:

```yaml
  # ---------------------------------------------------------------------------
  # Agent-generated (generate-goldens skill), <index_name> <INDEX_ID>, <date>.
  # Relevant sets derived from the SOURCE table (<SRC_ID>) data.
  # SME-simulated discovery queries + ideal ranked heads. DRAFT — review before trusting.
  # ---------------------------------------------------------------------------
```

## Step 5 — Validate and hand off

1. **Run the validator against the file you wrote to `<OUT_DIR>`**:
   ```bash
   python3 .claude/skills/goldie/scripts/validate_golden.py <OUT_DIR>/golden.yaml
   ```
   It checks structure (parses, has `id_field`, unique case ids, no empty/duplicate
   `relevant`, mostly-topical split) and that every `relevant` id
   actually exists in the index. However, you can use `--no-index` 
   when the index doesn't exist yet or only for quick offline structural pass while drafting.
2. **Write `README.md` to `<OUT_DIR>`** — the dataset-documentation companion to
   `golden.yaml` (if a README for this table was already mounted alongside an existing
   `golden.yaml` you're appending to, update it in place instead of starting fresh). Cover:
   - **What this is** — the index (`index_name` + id) and its `definingSQL` source table.
   - **`id_field` rationale** — which column you chose and why (fill/uniqueness check; note if
     you fell back to `rowId` and why a cleaner id wasn't available).
   - **Case provenance** — a short table of the `source:` values used and what each means.
   - **Coverage gaps** — the important part: intents/queries you deliberately did NOT turn
     into cases and *why* (attribute absent from the schema, high-cardinality free text that
     no `GROUP BY` collapses, cross-record/relational links a single-column filter can't
     express, mutation- or portal-level questions that belong to another index). Recording
     these stops them being silently re-attempted and separates *metadata gaps* from *search
     bugs*.
   - **Notable judgment calls** — false friends you excluded, borderline relevance, ordering
     choices a reviewer should sanity-check.
   Keep it factual and short; it is the SME's map to the golden set.
3. Summarize for the user: how many cases, the topical/known-item split, the domain themes
   covered, the README you wrote, and explicitly flag that these are **drafts for SME review**
   — especially the topical relevant sets and any judgment calls (borderline relevance, false
   friends you excluded).

## Principles

- **Ground truth from the source table, not the search index.** Relevant sets come from
  source-table data (Table Query API), never from searching the index — sampling
  relevance from search is circular and blinds the benchmark to the gaps it exists to find.
  The search index is for profiling and (only on request) the `recall_gap_current` flag.
- **Ground truth, not current behavior.** The golden encodes what *should* be returned.
  Never let the current search's output define relevance — that would make the benchmark
  unable to detect gaps.
- **Verify before including.** Every relevant id must have a real semantic link to the
  query; reject token-coincidence false friends.
- **Realistic over exhaustive.** A dozen genuine, well-judged discovery queries beat fifty
  mechanical ones.
- **Stay honest about uncertainty.** Mark drafts, sanity checks, and shaky judgments in
  `notes` so the SME knows where to look.

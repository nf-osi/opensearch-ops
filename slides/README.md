# Lay-audience slide deck — “Search that knows what you mean”

A single self-contained HTML deck explaining the MySQL → OpenSearch search rebuild to a
**non-specialist audience** (business owners, programme leadership, SMEs). 13 slides, paced
for **20 minutes including a ~5-minute live demo** of the Search Lab.

```bash
xdg-open slides/index.html          # no build, no server, no dependencies
```

Styling shares the Search Lab's palette (`web/style.css`) — warm paper, teal, clay — so
switching to the live demo on slide 10 has no jarring handoff. Typography deliberately differs
from the Lab: **Chivo at weight 900** for headings (a heavy technical grotesque, not the Lab's
Fraunces serif), **Public Sans** body and **IBM Plex Mono** for queries and data, both shared
with the Lab. Fonts load from Google Fonts; offline it falls back to system faces and stays legible.

## Two versions

| File | Audience | Use |
| --- | --- | --- |
| `index.html` | mixed / technical-friendly | The original. Precise terminology, full work inventory, file paths as provenance. |
| **`index-exec.html`** | **funder / programme exec, non-technical** | Plain-English rewrite, **12 slides** (2 and 3 merged), same verified numbers. Reviewed in persona by a non-technical exec reader; see "Exec copy" below. |

Both share the CSS, palette, typography, navigation and speaker-notes system. **Numbers are
identical in both** — only wording, emphasis, density and slide count differ. If you change a
figure, change it in both.

> The exec copy is **12 slides**; `index.html` is still 13. Slide numbering therefore differs after
> slide 2 — the live demo is slide **9** in the exec copy and **10** in the original.

## Exec copy — what differs from `index.html`

Driven by an in-persona review (busy funder exec, basic starting context). Substantive changes:

**Terminology.** "index" → **catalogue** throughout; field/column → *the tumour or symptom*, *the
study title*, *part of a record*; row → *record*; analyzer → *how the search reads text*; boosts →
*what counts most* / *equal vote*; golden set → **answer key**; ground truth → *the right answers*;
schema change and migration → *rebuilding the database*; fuzziness → *forgiving spelling mistakes*;
cross fields → *our tuned settings*. `flox` and `hybridoma` are glossed in one clause each.

**All file paths and config identifiers removed.** No `benchmark/run.py`, no `config/config.py`, no
`fields.yaml`, no `org.synapse.nf`. Reproducibility is now claimed in words ("we can re-run any of
this on demand") rather than shown as a command. The only monospace strings left on screen are the
actual searches and identifiers being discussed — `pnf`, `ipNF95`, `CVCL_8478`, `NTAP`/`ntap`.

**Tone.** Eyebrow "Being straight about it" **deleted** (it framed honesty as an occasion and
invited the question of what the other twelve slides were); now `REMAINING GAPS · 5 OF 48 SEARCHES`.
"Evidence, not opinion" → "How we know it is better". "What tuning is worth, in numbers" → "Does it
actually work better? Here's how much." "Real work, measurable benefit" → "Researchers can now
search the way they talk". "The machinery behind those numbers" → "Enough of this is built that
tuning is now routine". "Levers" → "settings we control". *"A database was doing a search engine's
job"* kept — the reviewer rated it the deck's best headline.

**Slide 4 is a straight demonstration, not an incident story.** One search — `hybridoma cell line`
— shown under two reading rules, with opposite outcomes: `0 results` against `all 3 found`. Nothing
in either deck claims a live outage or that users were affected; **the exact-label case was found in
testing before release**, and both decks now say so. The three "readings" are cut to two and the
42px specimen code is gone — the reviewer spent their attention decoding it and missed the point.

**Slides 2 and 3 merged into one (12 slides total).** The old slide 2's bulleted "why" list was the
same four points as slide 3's comparison table, so the table survives (cut from 8 rows to 4: what it
asks, vocabulary, what counts most, making a change) and absorbs the two things from slide 2 that
were unique:

- the four `0 results` searches, now a compact horizontal band of chips rather than four stacked rows
- *"The data was always there — the search could not reach it"*, promoted to headline weight

**A visual coverage board closes that slide.** Named chips instead of a footnote: six in solid teal
for the catalogues already running search settings we wrote, lighter chips for the rest, an italic
*"…and every other catalogue"*, and a dashed **"Files — not yet"** for the one exception. Headline:
*"**Every** catalogue in the NF portal now runs on OpenSearch."*

**No exact catalogue count anywhere in the exec copy.** "13" is gone from all visible text — the
coverage board says *every*, slide 8's stat tiles dropped from four to three, and slide 11's tile
reads "6 catalogues, and counting". `index.html` **still says 13** in three places; port it across if
you want the two decks consistent.

**Slide 7 leads with the human number.** "1 search in 2 → 2 in 3" is now the big figure; the
`0.620 → 0.732` decimal is retained but demoted and annotated *"a ranking score out of 1.00"*. The
reviewer read the near-equal bars as "no real difference", so the bars are no longer the argument.

**Slide 8** cut from four measures to two, plus two additions the reviewer asked for: the answer
keys are **written and maintained by the portal team** (stated plainly — it does not claim
independence from the people being graded), and **there are no usage figures from real researchers
yet**, said before an exec can ask.

**Slide 11 slimmed 6 tiles → 3**, no file paths, reframed to capability. Closes on *"We can change
how search behaves, measure whether it actually helped, and do both in an afternoon."*

**Slide 2** promotes *"The data was always there — the search could not reach it"* from an 11px
footnote to a headline-weight line. It was the reviewer's most-quoted sentence.

**The demo slide (9 in the exec copy)** keeps only the five step headlines on screen; expected ranks
moved into the speaker notes, so the room watches the demo instead of marking your homework. It also
carries a **prominent link to the live Search Lab**, opening in a new tab:

    https://nf-osi.github.io/opensearch-ops/

Verified live (HTTP 200). The closing slide repeats the URL as a clickable link, since that is the
slide people photograph. Published by the `benchmark` workflow, which is **manual-trigger only** —
if the Lab looks stale, run it before presenting.

> **Bug fixed in both decks while adding this.** The click-to-advance zones sat *above* slide content
> at `z-index:30`, so any link or button underneath them was swallowed — the demo slide's copy buttons
> were already affected where they crossed the right-hand zone. The active slide now paints above the
> zones with `pointer-events:none`, and `.slide a,.slide button` opt back in. Hit-tested with
> `elementFromPoint`: the link resolves even where it overlaps the "previous" zone, copy buttons
> resolve where they overlap the "next" zone, and background clicks still advance.

### Shipping language — read this before presenting

The exec copy states the tuned settings as **in hand and landing with this deliverable**, on the
basis that they will ship by the end of the deliverable. It does *not* claim they are live today:
slide 9's third column reads "shipping with this deliverable", and its footnote instructs the
presenter to say out loud that the right-hand column is not live this minute. **If the ship date
slips, revert that framing** — the earlier wording is in `index.html`.

### The ask changed, and needs your confirmation

Because the portal change now ships, slide 12's third gap and the ask were rewritten. The third gap
is now *"our answer key is still small"*, and the ask is **expert time on the answer keys** plus
**curation of the labelling gaps** — both of which follow from the deck's own content. It carries a
visible `[Confirm who, and how much of their time, before presenting.]` marker. **Confirm this is
the ask you want before showing it**; the original frontend ask is preserved in `index.html`.

## Presenting

| Key | Action |
| --- | --- |
| `→` `Space` `Enter` | Next slide |
| `←` | Previous slide |
| `1`–`9` | Jump to a slide |
| `0` | Jump straight to the **live demo** (slide 10) |
| `N` | Speaker notes (bottom panel; every slide has them) |
| `T` | Reset the 20-minute pace clock |
| `?` | Shortcut help |

The clock in the left spine starts on your first keypress or click and turns clay past 20:00.
Clicking the right third of a slide advances; the left edge goes back. `index.html#7`
deep-links. On slide 10, **clicking any query copies it** to the clipboard for pasting into
the Lab.

## Structure

| # | Slide | Job | ~time |
| --- | --- | --- | --- |
| 1 | Hero — `pnf` returns nothing vs. ranked hits | The whole argument in one image | 60s |
| 2 | A database doing a search engine's job | What legacy search could not do, and 4 recorded misses | 75s |
| 3 | What we gained by moving to OpenSearch | The feature-by-feature comparison | 80s |
| 4 | One string, three readings | **Analyzers** + the hybridoma near-miss the benchmark caught | 80s |
| 5 | Teaching the search our vocabulary | **Synonyms** — domain knowledge becomes search behaviour | 70s |
| 6 | Not every column deserves an equal vote | **Field weights**, and why only 3 of 17 columns are searched | 70s |
| 7 | What tuning is worth, in numbers | Two bars, two stats, one headline. No query-shape jargon. | 75s |
| 8 | We wrote down the right answers first | The golden set and the four measures, in plain language | 80s |
| 9 | What actually changed for the user | Legacy / today / tuned, per query — including one that still fails | 85s |
| 10 | **See it live — the Search Lab** | Demo entrypoint with copy-paste queries | **~5 min** |
| 11 | The machinery behind those numbers | Inventory of work delivered | 70s |
| 12 | What is still hard — and who can fix it | Honest gaps split by owner; the frontend ask | 75s |
| 13 | Real work, measurable benefit | User-facing benefits and the one number to remember | 55s |

Slides 1–9 run about 11 minutes, leaving ~5 for the demo and ~3 for the close, inside 20.

## The demo (slide 10)

Five steps, ordered to build trust before showing a failure. Each was **verified live
2026-07-30** against the query the portal sends today.

| Query | Expected | Demonstrates |
| --- | --- | --- |
| `CVCL_8478` | exactly 1 hit — `10/9CRC1` | identifiers resolve cleanly; earns trust |
| `ipNF95` | 9 hits, `ipNF95.11b` clones at ranks 2–3 | the analyzer — a fragment finds the whole |
| `neurofibromin` → `neurofibromin antibody` | no antibodies near the top (first at 18) → antibody at rank 1 | field weighting; **the most persuasive moment** |
| `hybridoma cell line` | Default site search **5–7** vs Cross fields **1, 2, 3** | switch recipes and watch it reorder |
| `melanoma` → `melanoma cell line` | ranks 1 and 4 → 16 and off the page | the honest failure |

For step 4, set the two Playground columns to **Default site search** and **Cross fields**
before searching. If the network misbehaves, slides 7 and 9 carry the same results — skip
ahead rather than stall.

> **Re-verify before presenting.** The live index moves. Everything above is reproducible with
> `python3 query.py '{"query":{"multi_match":{"query":"pnf","fuzziness":"AUTO"}},"size":10}'`
> and `python3 benchmark/run.py tools --strategy frontend_default --strategy multi_match_cross`.

## Where every number comes from

Nothing here is illustrative. **All current-state figures were measured against the live
nf-tools index on 2026-07-30**, replacing the older ranks recorded in the golden files (several
had drifted, and some were recorded against the tuned recipe rather than the live default —
which is why slide 9 now separates those two columns).

**Legacy (MySQL) behaviour** — `benchmark/tools/golden.yaml`, the `recall_gap_legacy_search:
true` flag on `pnf`, `cnf`, `neurofibromin`, `nf1-flox` (4 of 20 SME phrases). Recorded by SMEs
against the legacy search in the [NF tool search benchmarking
spreadsheet](https://docs.google.com/spreadsheets/d/1qL7lpD3F_ncKtWzR6itJmgrEid8Y5O4gvuXmwrs2RIY/view);
the mechanism (MySQL `TEXT_MATCHES`) is in that file's header. These are recorded observations
for those queries, **not a controlled A/B** against MySQL — the deck says "recorded on the
legacy search," and it should keep saying that.

**Slides 1, 5, 9 — current ranks** — measured live 2026-07-30 with the frontend's own query
(`multi_match` + `fuzziness: AUTO`):

| Query | Live default today | Tuned recipe (`multi_match_cross` + our weights) |
| --- | --- | --- |
| `pnf` | plexiform head at 1, 2, 7 (of 819 hits) | 14, 15, 16 — *tuning makes this one worse* |
| `cnf` | all 5 at 2, 4, 17, 18, 19 | all 5 at 2, 7, 12, 13, 15 |
| `nf1-flox` | rank 9 | rank 1 |
| `neurofibromin` | rank 18, 1 of 3 | ranks 4, 8, 11 |
| `hybridoma cell line` | ranks 5, 6, 7 | ranks 1, 2, 3 |
| `lung adenocarcinoma cell line` | none in top 15 | ranks 7, 10, 11 |
| `melanoma` / `melanoma cell line` | 1 and 4 / rank 16 then off page | none in top 25 |

> Slide 1 quotes the **`plexiform-neurofibroma`** golden head for `pnf`, not the
> `pnf` case's own `relevant` list. Those two 3PNF_ iPSC lines are a naming coincidence (see
> that case's notes) and have fallen out of the top 40; the plexiform lines are the honest and
> stronger illustration of the synonym working.

**Slide 4, hybridoma** — `benchmark/tools/golden.yaml` case `hybridoma-cell-line`: 0 hits under
a KEYWORD analyzer on `cellLineCategory`, fixed 2026-07-07 by falling back to STANDARD. The
deck says "all 3 found" rather than "rank #1" because rank #1 is the *tuned* result; the live
default gives 5–7.

**Slide 4, `ipNF95`** — verified live: 9 hits, `ipNF95.11b C` and `C/T` at ranks 2 and 3.

**Slide 5, synonyms** — synonym set `standard_synonyms` (id 16, org `org.synapse.nf`) via the
`nf_scientific_synonyms` text analyzer. Root `README.md` tuning-objects table and `config/`.

**Slide 6, field weights** — `benchmark/studies/fields.yaml`: `manifestation^10`,
`studyName^6`, `summary^3.5`. The "3 of 17" rationale and the `NTAP` vs `ntap`
case-sensitivity example are that file's own header comment.

**Slide 7** — `benchmark/tools/results/deck-preflight.json`, both strategies scored in **one
run** 2026-07-30, 48 cases, k=10: `frontend_default` MRR 0.620 / Hit@1 52% ·
`multi_match_cross` MRR 0.732 / Hit@1 65%. The 84% → 100% callout is the studies Hit@10 from
`benchmark/studies/results/tuned.json` (31 cases).

> The bars use a **zero baseline**, so they look close. That is honest; the two stat blocks
> carry the comparison. Don't rescale to exaggerate the gap.

**Slide 8, golden-set totals** — 168 cases and 607 relevant ids summed across the five
`benchmark/*/golden.yaml` files (tools 48, studies 31, publications 30, usage-publications 30,
datasets 29). 13 NF indexes: root `README.md`. The `drosophila model` example and the
source-table-not-search-index rule come from `benchmark/tools/golden.yaml`.

**Slide 12, remaining gaps** — the five `recall_gap_current: true` cases in
`benchmark/tools/golden.yaml`: `melanoma-cell-line`, `cafe-au-lait-spots`,
`metabolic-mouse-model`, `nf1-cell-line-black`, `nf1-bacterial-vector`. The frontend constraint
(bare `multi_match` + hard-coded `fuzziness: AUTO`, no field list or boosts) is documented with
code line references in `docs/INTEGRATION.md`.

**Slide 13 kicker** — studies Hit@10 0.839 → 1.000, i.e. 31 of 31 cases, from
`benchmark/studies/results/tuned.json`.

## Caveats worth stating if asked

- **The tuned recipe is measured, not shipped.** Slide 9's right column is not what users get.
  That's the slide-12 ask.
- **Tuning is not uniformly better.** `pnf` gets *worse* under the tuned recipe. The deck says
  so on slide 9 rather than hiding it.
- **31 and 48 cases are small.** `manifestation^10` is load-bearing on the studies result —
  halving it costs 0.034 ndcg. Worth re-checking as the golden sets grow.
- **Topical cases need ongoing SME curation.** Their `relevant` lists are the ideal *head* of a
  larger pool, so Recall@10 is a floor, not the full picture.
- **The golden files contain some drifted ranks.** They were recorded on various dates and
  partly against the tuned recipe. This deck's numbers supersede them; consider back-porting.

## Editing

One file, no build step. Slide content is plain HTML in `<section class="slide">`; speaker
notes are the `data-notes` attribute; pacing is `data-sec`. Design tokens are the `:root`
custom properties, mirroring `web/style.css` — `--teal` = the new system, `--green` = measured
gain, `--clay` = caution/gaps, `--faint` = legacy.

**Weights are scoped deliberately:** every rule that sets `font-family:var(--f-disp)` uses
`font-weight:900` (16 of them) except slide 2's pull quote at 500. Public Sans labels and
eyebrows stay at 700 — Public Sans is only loaded up to 700, so pushing them to 900 would give
you synthesised faux-bold. If you swap the display face again, change *only* the `--f-disp`
lines. Direct children of a slide carrying
`class="rise"` animate in sequence, so keep them as direct children. Demo queries are
`<button data-copy="…">` — the copy handler picks them up automatically.

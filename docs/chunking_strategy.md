# Chunking Strategy — Rationale (Phase 2 findings)

> Finalized in README (Phase 5). Data-driven decision per architecture.md §2.2.

## Classification: **Mixed** (key–value stats + markdown tables + FAQ/glossary prose)

Observed in the extracted Groww scheme pages (~38–83k chars/page):

- **Stat stacks** — label and value on *separate* lines: `Expense ratio` ⏎ `1.04%`
- **Glossary prose** — "Understand terms" sections (definitions)
- **Markdown tables** — holdings, returns, exit-load history
- **"About" paragraphs** — risk rating, min SIP, exit load, benchmark in prose

## Chosen strategy: **hybrid — heading-aware split + label/value glue + unit packing**

1. **Link cleaning** — internal nav links (`[...](/path)`) removed entirely (page
   junk + similar-fund lists); external link text (e.g. SID) kept. Signed return
   figures (`+7.87%`) dropped (**C3: never index returns**); unsigned facts
   (`1.04%`) kept. Pure nav/chart junk lines are dropped too (exact-line
   patterns, so no fact can match): chart range tabs (`1M`, `3Y`, `All`),
   `3Y annualised`, top-nav rows (`Stocks: F&O`, `Credit: Loan against…`),
   `Download the App` promos. This keeps the stat-stack chunk semantically
   clean — with the junk in it, its embedding diluted and the chunk fell
   below the retrieved top-k for its own queries (M3 retrieval fix).
2. **Heading-aware sections** — split at every markdown heading (`h1–h6`);
   preamble = "Key facts" (holds the page's stat stack). Sections are skipped by
   a data-driven skip-list: `Return calculator`, `Holdings (N)`,
   `Returns and rankings`, `Compare similar funds` — navigation noise,
   holdings dumps, and performance tables (C3).
3. **Label/value glue (fact integrity)** — a short label-like line is glued to
   the following block: `Expense ratio` + `1.04%` → `Expense ratio: 1.04%`.
   Lines starting with `₹`/digit are values, never labels.
4. **Unit packing** — tables packed row-wise (header carried), long prose split
   at sentence boundaries, paragraphs kept whole. Units packed greedily into
   chunks ≤ **500 chars** with **~12% sentence-tail overlap**
   (`CHUNK_OVERLAP = 0.12`).
5. **Fact-integrity guard** — a chunk may not end with a dangling label
   (`Exit load:`); it is carried into the next chunk with its value.
6. **De-dup** — repeated sections on the same page (the page repeats
   "Understand terms"/"Exit load") are dropped; tiny fragments (<40 chars)
   are folded into the previous chunk.
7. **Page-JSON fact mining (Phase 3 fix)** — a few facts exist *only* in the
   page's embedded analysis JSON and header badge, which normal extraction
   strips — notably the ELSS **`Lock-in period: 3Y`** (the required fact type).
   A whitelist extracts `analysis_subject ∈ {lock_in, exit_load}` values into a
   `#### Fund data` section. Return/comparison subjects (`*_return_analysis`,
   `alpha_analysis`) are deliberately **not** whitelisted (C3) — verified zero
   performance text in the index after the change.

## Result

- **105 chunks** across 5 schemes (avg ~210 chars, max 491 — within limit)
- `Expense ratio: X.XX%`, `Min. for SIP: ₹N`, `Exit load of 1%...`,
  `rated Very High risk`, `Lock-in period: 3Y`,
  `Fund benchmarkNIFTY …` all verified intact within a single chunk
- Zero signed-return strings in the index

## Retrieval note (M3): `TOP_K` 5 → 8

At `TOP_K = 5` the fact-bearing `Key facts` chunk ranked **#6–#7** for some
queries (e.g. expense ratio), so the model never saw the number and correctly
answered "couldn't find" — bad retrieval, not bad generation. Measured ranks
after the nav-junk cleanup: 2–6 for all seven fact queries → `TOP_K = 8`
covers them with margin (context stays ≈ 4k chars). `architecture.md` §5
range updated to 4–8.

## Retrieval note (M3): hybrid re-ranking (cosine + IDF lexical overlap)

A second retrieval miss surfaced after the 2026-10-07 source refresh: the
ELSS **lock-in** fact (page-JSON-mined into the bare two-line `Fund data`
chunk: `- Exit load is zero` / `- Lock-in period: 3Y`) ranked **#9–#16** for
natural phrasings — outside `TOP_K = 8` — because the chunk carries no
scheme-name tokens while every user query does, so name-heavy prose
(`About …`) drowned it in pure cosine.

Fix in `retrieve.py` (spec-safe: dense cosine over the same MiniLM embedding
is unchanged — the lexical term only re-ranks a wider pool):

1. Fetch `CANDIDATE_POOL = 32` dense candidates (≥ the largest per-scheme
   category, so filtered queries re-rank essentially the whole scheme).
2. Score = `cosine + LEXICAL_WEIGHT(0.3) · IDF-overlap`, where IDF is
   computed **over the candidate pool** and the query is stripped of
   stop-words *and* navigational tokens (scheme names, plan classes,
   category words — already handled by the metadata filter + dense embed).
   Tokenizing on `[a-z0-9]+` also matches across `lock in` / `lock-in`.
3. Re-sort, cut to `TOP_K`.

Measured (answer-bearing chunk rank, dense → hybrid): lock-in 16→6, 9→2,
13→4; expense 8→4; exit load 5→2; NAV 3→2; risk 2→1; all other fact,
manager and benchmark queries unchanged or improved — 16/16 in the top-8
(previously 11/16), zero regressions. Verified end-to-end: the lock-in
question now answers "3 years", grounded.

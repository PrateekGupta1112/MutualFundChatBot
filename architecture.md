# Architecture — Mutual Fund FAQ RAG Chatbot (Facts-Only Q&A)

| | |
|---|---|
| **Project** | Mutual Fund FAQs — Facts-Only Q&A Assistant |
| **Documents** | Derived from [`PRD.md`](./PRD.md); source of truth: `docs/problemstatement.txt` |
| **Status** | v1.0 |
| **Stack** | Python · `sentence-transformers/all-MiniLM-L6-v2` · ChromaDB · LLM (grounded generation) · lightweight chat UI |

---

## 1. Architecture Overview

The system is a classic two-phase RAG pipeline. **Ingestion** runs offline (rebuildable via script); **retrieval + generation** runs at query time with a guardrail layer in front of and behind the LLM.

```
═════════════════════════════ OFFLINE: DATA INGESTION ═════════════════════════════

  ┌────────────────┐   ┌───────────────┐   ┌──────────────┐   ┌───────────────────┐
  │ 1. LOADER      │──▶│ 2. CHUNKER    │──▶│ 3. EMBEDDER  │──▶│ 4. VECTOR STORE   │
  │ Fetch 5 scheme │   │ Strategy per  │   │ all-MiniLM-  │   │ ChromaDB          │
  │ pages + official│  │ data shape    │   │ L6-v2        │   │ ./chroma_db       │
  │ AMC/SEBI/AMFI  │   │ fact-integrity│   │ (384-dim)    │   │ + metadata        │
  │ pages (HTML/PDF)│  │ rules         │   │              │   │                   │
  └────────────────┘   └───────────────┘   └──────────────┘   └───────────────────┘
         │                    │                                      ▲
         ▼                    ▼                                      │
  ┌────────────────┐   ┌───────────────┐                             │
  │ Raw snapshots  │   │ Chunk records │─────────────────────────────┘
  │ ./data/raw/    │   │ + metadata    │
  │ (audit/proven.)│   │ (in-memory)   │
  └────────────────┘   └───────────────┘

═════════════════════════════ ONLINE: DATA RETRIEVAL ══════════════════════════════

  User question
       │
       ▼
┌─────────────────────┐   advisory / PII / performance intent detected
│ 5. GUARDRAIL INPUT  │──────────────────────────────────────────────┐
│  - intent gate      │                                              │
│  - PII screen       │                                              ▼
└─────────┬───────────┘                              ┌──────────────────────────────┐
          │ clean factual question                   │ 8a. STATIC REFUSAL           │
          ▼                                          │  • Advice → polite refusal + │
┌─────────────────────┐                              │    educational link          │
│ 6. QUERY EMBED      │                              │  • PII → "don't share PII"   │
│ same embedding model│                              │    (nothing logged/stored)   │
└─────────┬───────────┘                              │  • Performance → factsheet   │
          ▼                                          │    link (no numbers)         │
┌─────────────────────┐   top-k chunks              └──────────────────────────────┘
│ 7. TOP-K SEARCH     │─── + source metadata ───┐
│ ChromaDB similarity │                         │
│ + scheme metadata   │                         ▼
│ filter (when named) │            ┌──────────────────────────────┐
└─────────────────────┘            │ 9. GROUNDED GENERATION (LLM) │
                                   │  • answer ONLY from context  │
                                   │  • ≤ 3 sentences             │
                                   │  • exactly one citation URL  │
                                   │  • "Last updated from ...: d"│
                                   │  • no returns/perf compute   │
                                   └──────────────┬───────────────┘
                                                  ▼
                                   ┌──────────────────────────────┐
                                   │ 10. GUARDRAIL OUTPUT         │
                                   │  • sentence-count check      │
                                   │  • citation present check    │
                                   │  • fallback: "insufficient   │
                                   │    info" + source page link  │
                                   └──────────────┬───────────────┘
                                                  ▼
                                   ┌──────────────────────────────┐
                                   │ 11. CHAT UI                  │
                                   │ welcome • 3 examples • note  │
                                   │ "Facts-only. No investment   │
                                   │  advice."                    │
                                   └──────────────────────────────┘
```

---

## 2. Component Specifications

### 2.1 Stage 1 — Loader (Loading)

| Aspect | Decision |
|--------|----------|
| **Inputs** | 5 Groww scheme pages (primary) + official AMC/SEBI/AMFI supplements (factsheets, KIM/SID, scheme FAQs, fee/charges, riskometer/benchmark notes, statement/tax-doc guides) |
| **Fetch** | HTTP GET for HTML; PDF parser for documents; polite timeouts + retries |
| **Extract** | HTML → clean markdown/text (strip nav/scripts/boilerplate); preserve headings |
| **Provenance** | Every extracted segment carries `source_url`, `page_title`, `fetched_at` |
| **Raw store** | Original responses snapshotted to `./data/raw/` for auditability and reproducible source list (`sources.csv`/`sources.md`) |

**Source allow-list (enforced):** only `groww.in` scheme pages + `hdfcfmf.com` / `amfiindia.org` / `sebi.gov.in` domains. No third-party blogs (C1).

### 2.2 Stage 2 — Chunker (Chunking)

Chunking strategy is **decided from the actual ingested data** at implementation time. Decision matrix (from PRD §4.2):

| Data shape observed | Strategy |
|---|---|
| Structured key–value facts (expense ratio, exit load, min SIP, lock-in) | Small **structure-aware chunks** — split at label/field boundaries so a fact's label and value never separate |
| Long FAQ / prose pages | **Semantic paragraph chunking** with heading context carried into the chunk |
| Mixed (expected) | **Hybrid**: heading-aware split + ~10–15% token overlap |

**Invariants (regardless of strategy):**

- **Fact integrity:** never split `label + value` across a boundary (e.g., "Exit Load: 1% if redeemed within 365 days" stays whole).
- **Metadata on every chunk:**

```json
{
  "source_url": "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
  "scheme": "HDFC Large Cap Fund - Direct Plan - Growth",
  "category": "Large Cap",
  "section": "Exit Load",
  "ingested_at": "2026-10-06T00:00:00Z",
  "chunk_index": 12
}
```

- Final strategy + rationale documented in `README.md`.

### 2.3 Stage 3 — Embedder (Embedding)

| Aspect | Decision |
|--------|----------|
| **Model** | `sentence-transformers/all-MiniLM-L6-v2` (fixed by spec) |
| **Dimension** | 384 |
| **Usage** | Identical model for document and query vectors (mandatory for valid similarity) |
| **Runtime** | Local CPU inference; model cached locally after first download |
| **Batching** | Documents embedded in batches at ingestion; single-vector at query time |

### 2.4 Stage 4 — Vector Store

| Aspect | Decision |
|--------|----------|
| **DB** | **ChromaDB** (persistent client) |
| **Path** | `./chroma_db` (git-ignored; rebuildable) |
| **Collection** | Single collection `mf_facts` (metadata filter by `scheme`/`category` provides partitioning) |
| **Contents** | embedding + document text + metadata (§2.2) |
| **Rebuild** | `ingest.py` is idempotent — drop & recreate collection from raw snapshots |

### 2.5 Stages 5–6 — Query Embedding & Retrieval

1. **Guardrails first** (see §2.6) — blocked queries never reach retrieval.
2. Query embedded with the same MiniLM model.
3. **Hybrid top-k retrieval:** fetch a `CANDIDATE_POOL` (32) of dense cosine
   candidates from ChromaDB, re-rank with `cosine + LEXICAL_WEIGHT ·
   IDF-overlap` (stop-words + navigational/scheme tokens stripped from the
   lexical term — the metadata filter already scopes the scheme), cut to
   `TOP_K` (8). Pure cosine buried bare two-line fact chunks (e.g. the mined
   ELSS `Fund data / Lock-in period: 3Y`) below the cut for name-heavy
   queries; dense cosine itself is unchanged, so the spec-fixed embedding
   contract still holds.
4. **Metadata pre-filter:** if the question names a scheme (e.g., "HDFC ELSS"), filter `scheme`/`category` before similarity search to prevent cross-scheme fact mixing.
5. Retrieved chunks + their `source_url` are passed as the LLM context.

### 2.6 Guardrail Layer (input & output)

Runs as an explicit layer around generation — never relies on the LLM alone.

```
INPUT GUARD                          OUTPUT GUARD
────────────                         ────────────
1. PII screen (regex + pattern       1. Sentence count ≤ 3 (else regenerate /
   checks: PAN, Aadhaar, phone,          truncate with fallback)
   email, OTP, account numbers)     2. Exactly one citation URL present
   → reject, log nothing            3. Citation URL ∈ source allow-list
2. Advice-intent gate                4. No numeric return/%-performance
   ("should I buy/sell/invest",          strings beyond quoted facts
   "best fund", "recommend")        5. `Last updated from sources: <date>`
   → static refusal + educational       line appended
   link (SEBI/AMFI investor edu.)   6. Fallback if context insufficient:
3. Performance-intent gate              "couldn't find in sources" +
   ("returns", "compare", "CAGR")       scheme source page link
   → official factsheet link, no
     generated numbers
```

| Detected intent | Response | Link |
|---|---|---|
| Advice / opinion | Polite, consistent facts-only refusal | SEBI/AMFI investor education page |
| PII present | "Please don't share personal information…" — nothing stored/logged | — |
| Performance / returns | Redirect to official factsheet; no computed or compared figures | Factsheet URL |
| Insufficient context | Say so plainly | The scheme's source page |

### 2.7 Grounded Generation (LLM)

**Prompt contract:**

- System role: factual MF FAQ assistant; answer **only** from `<context>`; ≤ 3 sentences; one citation; end with `Last updated from sources: <date>`; never advise; never compute returns; if context lacks the answer, say so and cite the source page.
- Context = top-k retrieved chunks (each tagged with its `source_url` so the model picks the right citation).
- Answer format: `<answer text>` + `Source: <url>` + `Last updated from sources: <date>`.

**Model choice:** any instruction-tuned LLM available for the demo (local or API). The architecture isolates it behind a single `generate(context, question)` interface so the model can be swapped without touching the pipeline.

### 2.8 UI

Minimal chat surface (Streamlit/Gradio or equivalent lightweight framework):

1. **Welcome line** — facts-only MF FAQ helper intro.
2. **3 clickable example questions:**
   - "What is the exit load of HDFC Large Cap Fund?"
   - "What is the lock-in period for HDFC ELSS Tax Saver?"
   - "How do I download a capital-gains statement?"
3. **Persistent note:** *"Facts-only. No investment advice."*
4. **Answer rendering:** ≤ 3 sentences, one clickable citation, `Last updated from sources: <date>`.
5. **Refusal rendering:** uniform message for advice/PII/performance queries.

---

## 3. Data Flow Summary

| Phase | Step | Input → Output |
|---|---|---|
| **Ingestion** | 1. Load | URLs → clean text + `source_url` metadata (+ raw snapshot) |
| | 2. Chunk | Clean text → metadata-tagged chunks (fact-integrity preserved) |
| | 3. Embed | Chunks → 384-dim vectors (`all-MiniLM-L6-v2`) |
| | 4. Store | Vectors + text + metadata → ChromaDB (`./chroma_db`) |
| **Retrieval** | 5. Guard input | Question → pass / static refusal |
| | 6. Embed query | Question → query vector |
| | 7. Top-k search | Query vector → k chunks (+ optional scheme filter) |
| | 8. Generate | Context + question → grounded answer + 1 citation + last-updated |
| | 9. Guard output | Answer → validated answer (or fallback) |
| | 10. Render | Answer → chat UI (with disclaimer note) |

---

## 4. Project Structure

```
Mutual Fund Chat Bot/
├── PRD.md                      # Product requirements (source)
├── architecture.md             # This document
├── docs/
│   └── problemstatement.txt    # Original milestone brief
├── README.md                   # Setup, scope, known limits (deliverable D3)
├── sources.csv | sources.md    # Source list of used URLs (deliverable D2)
├── sample_qa.md                # 5–10 queries + answers + links (deliverable D4)
├── disclaimer.md               # UI disclaimer snippet (deliverable D5)
├── data/
│   └── raw/                    # Cached source snapshots (audit/reproducibility)
├── src/
│   ├── ingest.py               # Loading → chunking → embedding → ChromaDB
│   ├── retrieve.py             # Query embed + top-k search + filters
│   ├── guardrails.py           # PII / advice / performance gates (in & out)
│   ├── generate.py             # Grounded LLM generation + answer formatting
│   └── config.py               # Model name, paths, allow-list, k, dates
├── app.py                      # Chat UI (welcome, 3 examples, note)
├── chroma_db/                  # Persistent vector store (git-ignored)
└── requirements.txt
```

---

## 5. Configuration Knobs

| Knob | Default | Notes |
|---|---|---|
| `EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Fixed by spec |
| `CHROMA_PATH` | `./chroma_db` | Rebuildable via `ingest.py` |
| `TOP_K` | 4–8 | Tuned during M3 (final: 8 — at 5, fact stacks ranked #6–7 for some queries) |
| `CANDIDATE_POOL` | 32 | Dense candidates fetched for the hybrid re-rank (≥ largest per-scheme category) |
| `LEXICAL_WEIGHT` | 0.3 | λ in `score = cosine + λ · IDF-overlap`; 0 = dense-only (M3 lock-in fix) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | Data-dependent (§2.2) | Final values + rationale → README |
| `SOURCE_ALLOWLIST` | groww.in, hdfcfmf.com, amfiindia.org, sebi.gov.in | Enforced at load and citation-check |
| `MAX_ANSWER_SENTENCES` | 3 | Enforced by output guard |
| `EDUCATIONAL_LINK` | SEBI/AMFI investor education URL | Used in advice refusals |
| `FACTSHEET_LINK` | Official factsheet URL | Used in performance redirects |

---

## 6. Design Decisions & Rationale

| Decision | Rationale |
|---|---|
| **Offline ingestion, script-driven** | Corpus is fixed (5 schemes); rebuild is reproducible and cheap; supports `sources.csv` deliverable |
| **Single ChromaDB collection + metadata filters** | Simpler than per-scheme collections; scheme filter still prevents cross-scheme fact mixing |
| **Same embedder for docs & query** | Required for meaningful cosine similarity |
| **Hybrid re-rank (cosine + IDF overlap)** | Pure cosine buried scheme-name-less fact chunks below `TOP_K`; lexical term is exact-wording insurance, dense still dominates ranking (see docs/chunking_strategy.md) |
| **Guardrails as explicit code layer (not just prompt)** | PRD constraints C2/C3/C6 must be deterministic and testable, not probabilistic |
| **Raw snapshots on disk** | Auditability, offline rebuilds, protection against source pages changing/blocking (PRD risk R1) |
| **Output-side validation** | Enforces ≤3 sentences, exactly-one-citation, allow-listed URL, last-updated line even if the LLM deviates |
| **Isolated `generate()` interface** | LLM swappable (local vs API) without pipeline changes |

---

## 7. Non-Functional Requirements

| Area | Requirement |
|---|---|
| **Correctness** | Facts never split across chunks; citation URL always the true origin of the answer |
| **Transparency** | Every answer: ≤3 sentences, one citation, `Last updated from sources: <date>` |
| **Privacy** | PII never accepted, stored, or logged (C2) |
| **Safety** | No advice, no performance computation — enforced pre- and post-generation |
| **Reproducibility** | `ingest.py` rebuilds the index from `data/raw/` deterministically |
| **Portability** | Runs locally on CPU; demo-grade hosting acceptable (video fallback allowed) |
| **Testability** | Sample Q&A file doubles as a regression suite for guardrails & citations |

---

## 8. Constraints → Architecture Traceability

| PRD constraint | Where enforced |
|---|---|
| C1 Public sources only | Loader allow-list (§2.1) + citation allow-list check (§2.6) |
| C2 No PII | Input PII screen, nothing logged (§2.6) |
| C3 No performance claims | Performance-intent gate + output numeric check (§2.6) + prompt contract (§2.7) |
| C4 ≤3 sentences + last-updated | Prompt contract + output guard (§2.6) |
| C5 One citation per answer | Context tagged with `source_url` + output citation check (§2.6) |
| C6 No advice | Advice-intent gate → static refusal + educational link (§2.6) |

---

## 9. Milestone Mapping

| Milestone | Architecture components |
|---|---|
| **M1** Corpus + source list | §2.1 Loader, `data/raw/`, `sources.csv` |
| **M2** Ingestion pipeline | §2.1–§2.4 (load → chunk → embed → ChromaDB), `ingest.py` |
| **M3** Retrieval + generation | §2.5, §2.7, output guard in §2.6 |
| **M4** Guardrails | §2.6 full layer (PII / advice / performance) |
| **M5** UI + docs | §2.8, README, sample Q&A, disclaimer |
| **M6** Demo | Working prototype or ≤3-min video |

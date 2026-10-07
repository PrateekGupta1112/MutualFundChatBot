# PRD — Mutual Fund FAQ RAG Chatbot (Facts-Only Q&A)

| | |
|---|---|
| **Project** | Mutual Fund FAQs — Facts-Only Q&A Assistant |
| **Type** | Class demo / milestone prototype |
| **End output** | A working RAG chatbot with a tiny UI |
| **Status** | Draft v1.0 |
| **Source of truth** | `docs/problemstatement.txt` |

---

## 1. Problem Statement

Retail investors and support/content teams repeatedly ask the same factual questions about mutual fund schemes: *What is the expense ratio? What is the exit load? What is the minimum SIP? What is the ELSS lock-in? What is the riskometer/benchmark? How do I download a capital-gains statement?*

Today these answers are scattered across AMC pages, factsheets, KIM/SID documents, and regulator sites. People either wade through dense PDFs or land on third-party blogs of uncertain accuracy.

**Goal:** Build a small FAQ assistant that answers **factual questions only** about a scoped set of mutual fund schemes, using **only official public pages**, with **one source citation in every answer** and **no investment advice**.

**Who this helps:**
- Retail users comparing schemes on factual parameters.
- Support/content teams answering repetitive MF questions.

---

## 2. Scope

### 2.1 In scope

- **One AMC: HDFC Mutual Fund**, with **5 schemes**:

| # | Category | Scheme page (corpus source) |
|---|----------|-----------------------------|
| 1 | Large Cap | https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth |
| 2 | Flexi Cap | https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth |
| 3 | ELSS (Tax Saver) | https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth |
| 4 | Small Cap | https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth |
| 5 | Balanced Advantage (Hybrid) | https://groww.in/mutual-funds/hdfc-balanced-advantage-fund-direct-growth |

- **Additional official public pages** as corpus supplements: AMC factsheets, KIM/SID, scheme FAQs, fee/charges pages, riskometer/benchmark notes, statement/tax-doc guides (AMC / SEBI / AMFI).
- **Factual Q&A** over the corpus: expense ratio, exit load, minimum SIP, lock-in (ELSS), riskometer, benchmark, how to download statements.
- **One citation link per answer.**
- **Refusal of opinionated/portfolio questions** with a polite facts-only message + a relevant educational link.
- **Tiny UI**: welcome line, 3 example questions, and the note *"Facts-only. No investment advice."*

### 2.2 Out of scope

- ❌ Investment advice, recommendations, buy/sell opinions ("Should I buy X?").
- ❌ Computing or comparing returns/performance claims of any kind.
- ❌ Any source beyond the scoped official public pages (no third-party blogs).
- ❌ User accounts, login, or any PII (PAN, Aadhaar, account numbers, OTPs, emails, phone numbers) — never accepted, never stored.
- ❌ Multi-AMC coverage, portfolio tracking, NAV charts, or real-time market data.
- ❌ Production hosting/SLAs (demo-grade prototype is sufficient).

---

## 3. Constraints & Guardrails (Key Constraints)

| # | Constraint | Implementation implication |
|---|-----------|----------------------------|
| C1 | **Public sources only** | Ingest only the 5 scheme pages + official AMC/SEBI/AMFI pages; source list must be reproducible. |
| C2 | **No PII** | No input fields or storage for PAN, Aadhaar, account numbers, OTPs, emails, phone numbers; add input-side PII screening. |
| C3 | **No performance claims** | Never compute/compare returns; if asked, redirect to the official factsheet link. |
| C4 | **Clarity & transparency** | Answers ≤ 3 sentences; every answer ends with `Last updated from sources: <date>`. |
| C5 | **One citation per answer** | Every answer displays exactly one clear source link. |
| C6 | **No advice** | Opinionated questions are refused with a polite, facts-only message + educational link. |

---

## 4. Solution Architecture — Full RAG Pipeline

All RAG stages must be implemented end-to-end: **Data ingestion** (Loading → Chunking → Embedding → Vector store) and **Data retrieval** (query → retrieval → generation → citation).

```
┌────────────────────────── DATA INGESTION ──────────────────────────┐
│  1. LOADING      Fetch the 5 scheme pages + official AMC/SEBI/     │
│                  AMFI pages → clean markdown/text (with URL)       │
│  2. CHUNKING     Strategy decided from the data shape (see 4.2)    │
│  3. EMBEDDING    sentence-transformers/all-MiniLM-L6-v2            │
│  4. VECTOR STORE ChromaDB (persist locally, with source metadata)  │
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────── DATA RETRIEVAL ──────────────────────────┐
│  5. QUERY EMBED   User question → same embedding model            │
│  6. TOP-K SEARCH  ChromaDB similarity search (+ metadata filter)  │
│  7. GROUNDED GEN  LLM answers ONLY from retrieved chunks,         │
│                   ≤3 sentences, exactly one citation URL          │
│  8. GUARDRAILS    Intent gate: advice/PII/performance → refuse    │
│                   or redirect with educational link               │
│  9. UI            Welcome + 3 examples + disclaimer note          │
└────────────────────────────────────────────────────────────────────┘
```

### 4.1 Stage 1 — Loading

- Fetch each source URL (HTML/PDF) and extract clean text, preserving the **source URL as metadata** on every extracted segment.
- Store raw snapshots for auditability (source list must be reproducible).

### 4.2 Stage 2 — Chunking

Chunking strategy to be **decided based on the actual data** during implementation. Decision inputs:

| Data characteristic | Likely strategy |
|---|---|
| Structured key-value facts (expense ratio, exit load, SIP min) | Smaller, structure-aware chunks (sentence/label-bounded) so facts stay intact |
| Long FAQ/prose pages | Semantic/paragraph chunking with heading context |
| Mixed | Hybrid: heading-aware split with a small overlap (e.g., 10–15%) |

**Requirements regardless of strategy:**
- Each chunk carries metadata: `source_url`, `scheme`, `category`, `section heading`, `ingested_at`.
- Chunks must not split a single fact (value + label) across boundaries.
- Final strategy documented in the README with rationale.

### 4.3 Stage 3 — Embedding

- **Model:** `sentence-transformers/all-MiniLM-L6-v2` (fixed by spec).
- Same model used for documents and query vectors.

### 4.4 Stage 4 — Vector Store

- **ChromaDB**, persisted locally (e.g., `./chroma_db`).
- Collection(s) hold embeddings + metadata; rebuild script provided in README.

### 4.5 Stage 5–8 — Retrieval & Generation

- **Top-k similarity search** over ChromaDB; metadata filters used when the question names a scheme.
- **Generation rules:**
  - Answer **only** from retrieved context (grounded); if context is insufficient, say so and link the scheme's source page.
  - **≤ 3 sentences**, plus `Last updated from sources: <date>`.
  - **Exactly one citation link** per answer.
  - **No performance computations or comparisons.**
- **Guardrail layer (before/after generation):**
  - *Advice intent* ("should I buy/sell/invest") → polite refusal: facts-only message + relevant educational link (e.g., SEBI/AMFI investor education page).
  - *PII* detected in input → reject with a "please don't share personal information" message; nothing logged or stored.
  - *Performance questions* → redirect to the official factsheet link, no numbers generated.

---

## 5. UI Requirements

Minimal chat interface:

1. **Welcome line** — introduces the assistant as a facts-only MF FAQ helper.
2. **3 example questions** (clickable suggestions), e.g.:
   - "What is the exit load of HDFC Large Cap Fund?"
   - "What is the lock-in period for HDFC ELSS Tax Saver?"
   - "How do I download a capital-gains statement?"
3. **Persistent note:** *"Facts-only. No investment advice."*
4. **Answer rendering:** answer text ≤ 3 sentences, one clickable citation, `Last updated from sources: <date>`.
5. **Refusal rendering:** polite, consistent message for advice/PII queries.

---

## 6. Deliverables

| # | Deliverable | Description |
|---|-------------|-------------|
| D1 | **Working prototype** | Running app/notebook link — or a ≤ 3-min demo video if hosting isn't possible |
| D2 | **Source list** | CSV/MD of the 5 URLs used (plus any official supplementary pages) |
| D3 | **README** | Setup steps, scope (AMC + schemes), known limits |
| D4 | **Sample Q&A** | 5–10 queries with the assistant's answers + links |
| D5 | **Disclaimer snippet** | The facts-only, no-advice text used in the UI |
| D6 | **This PRD** | `PRD.md` |

---

## 7. Acceptance Criteria

- [ ] Full RAG pipeline runs end-to-end: Loading → Chunking → Embedding → ChromaDB → retrieval → generation.
- [ ] Embedding model is `sentence-transformers/all-MiniLM-L6-v2`.
- [ ] Corpus is exactly HDFC AMC with the 5 scoped schemes (+ allowed official supplements).
- [ ] Factual questions (expense ratio, exit load, min SIP, ELSS lock-in, riskometer, benchmark, statement download) are answered correctly from the corpus.
- [ ] Every answer is **≤ 3 sentences**, has **exactly one citation link**, and shows **"Last updated from sources: \<date\>"**.
- [ ] Advice questions ("Should I buy/sell?") are refused politely with an educational link.
- [ ] PII inputs are refused and never stored; no return/performance figures are ever computed.
- [ ] UI shows welcome line, 3 example questions, and the "Facts-only. No investment advice." note.
- [ ] All six deliverables (D1–D6) present.

---

## 8. Risks & Known Limits

| Risk | Mitigation |
|------|-----------|
| Source pages (Groww/AMC) change or block scraping | Cache raw snapshots; document refresh steps in README |
| Facts split across chunks → wrong/merged answers | Structure-aware chunking; fact-integrity rule in 4.2 |
| Model answers beyond retrieved context | Grounded-prompting + "insufficient info" fallback to source link |
| Advice-style questions slipping through the intent gate | Keyword + LLM classifier double-check; refusal tested in sample Q&A |
| Stale data | Always display `Last updated from sources:`; rebuild index periodically |
| Demo-grade hosting limits | Fall back to ≤ 3-min demo video (allowed by spec) |

---

## 9. Milestones

| M | Milestone | Outcome |
|---|-----------|---------|
| M1 | Corpus + source list | 5 scheme pages + official supplements collected, `sources.csv/md` done |
| M2 | Ingestion pipeline | Load → chunk → embed → ChromaDB script runs reproducibly |
| M3 | Retrieval + generation | Grounded answers with citation + last-updated line |
| M4 | Guardrails | Advice refusal, PII block, performance redirect |
| M5 | UI + docs | Tiny chat UI, README, sample Q&A, disclaimer snippet |
| M6 | Demo | Working prototype or ≤ 3-min video submitted |

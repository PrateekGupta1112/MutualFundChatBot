# Implementation Guide — Mutual Fund FAQ RAG Chatbot

| | |
|---|---|
| **Purpose** | Phase-by-phase build instructions to hand to an AI coding assistant (Cursor) — one phase at a time |
| **Documents** | Implements [`architecture.md`](./architecture.md) · Requirements from [`PRD.md`](./PRD.md) |
| **How to use** | Work through phases in order. Give Cursor **one phase per session**, complete its "Done when" checklist before moving on. Do not skip ahead — each phase depends on the previous. |

---

## Global Rules (apply to every phase)

1. **One phase at a time.** Paste only the phase block you're working on into Cursor.
2. **Follow the architecture.** File layout, module names, config knobs, and metadata fields are specified in `architecture.md` §4/§5 — match them exactly.
3. **No scope creep.** Anything not in the phase = not this phase.
4. **Fixed by spec (never change):**
   - Embedding model: `sentence-transformers/all-MiniLM-L6-v2`
   - Vector DB: ChromaDB at `./chroma_db`
   - Answers ≤ 3 sentences · exactly one citation · `Last updated from sources: <date>` line
   - No PII, no advice, no performance computation
5. **Source allow-list (C1):** only `groww.in`, `hdfcfmf.com`, `amfiindia.org`, `sebi.gov.in`. No third-party blogs, ever.
6. **Done when:** every item in the phase's "Done when" checklist passes before starting the next phase.

### Suggested Cursor prompt template

```
Read PRD.md, architecture.md, and implementation.md.
We are on Phase <N>: <phase name>.
Implement ONLY Phase <N> as specified in implementation.md,
following architecture.md for file structure and module contracts.
Do not implement later phases.
```

---

# Phase 0 — Project Scaffold & Environment

**Goal:** Empty-but-runnable project skeleton with dependencies installed.

### Tasks

1. Create the project structure from `architecture.md` §4 (folders `docs/`, `data/raw/`, `src/`, and empty placeholder files `app.py`, `requirements.txt`, `.gitignore`).
2. `requirements.txt` (pin loosely, demo-grade):
   ```
   sentence-transformers
   chromadb
   streamlit            # UI framework (swap to gradio only if explicitly requested)
   requests
   beautifulsoup4
   markdownify
   pypdf
   python-dotenv
   ```
3. `.gitignore`: `chroma_db/`, `data/raw/`, `.env`, `__pycache__/`, `*.pyc`, `.venv/`.
4. Create `src/config.py` with ALL knobs from `architecture.md` §5:
   ```python
   EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
   CHROMA_PATH = "./chroma_db"
   COLLECTION_NAME = "mf_facts"
   TOP_K = 8              # tuned 5→8 during M3 (fact chunks ranked #6–7 at 5)
   CANDIDATE_POOL = 32    # M3: dense candidates for hybrid re-rank (≥ largest scheme)
   LEXICAL_WEIGHT = 0.3   # M3: score = cosine + λ·IDF-overlap (0 = dense-only)
   CHUNK_SIZE = 500          # tokens/chars — finalized in Phase 2
   CHUNK_OVERLAP = 0.12      # 10–15%
   SOURCE_ALLOWLIST = ["groww.in", "hdfcfmf.com", "amfiindia.org", "sebi.gov.in"]
   MAX_ANSWER_SENTENCES = 3
   EDU_LINK = "https://www.sebi.gov.in/investors.html"        # advice refusals
   FACTSHEET_LINK = ""       # official HDFC factsheet page URL — set in Phase 1
   LAST_UPDATED = ""         # set from snapshot fetch date at ingest time
   SCHEMES = [  # the 5 scoped schemes
     {"scheme": "HDFC Large Cap Fund - Direct Plan - Growth",         "category": "Large Cap", "url": "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth"},
     {"scheme": "HDFC Equity Fund - Direct Plan - Growth",            "category": "Flexi Cap", "url": "https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth"},
     {"scheme": "HDFC ELSS Tax Saver Fund - Direct Plan - Growth",    "category": "ELSS",      "url": "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-growth"},
     {"scheme": "HDFC Small Cap Fund - Direct Plan - Growth",         "category": "Small Cap", "url": "https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth"},
     {"scheme": "HDFC Balanced Advantage Fund - Direct Plan - Growth","category": "Hybrid",    "url": "https://groww.in/mutual-funds/hdfc-balanced-advantage-fund-direct-growth"},
   ]
   ```
5. Create `src/__init__.py`. Create stub modules with function signatures only:
   - `ingest.py` → `load_sources()`, `chunk_documents()`, `build_index()`
   - `retrieve.py` → `search(query, top_k, scheme_filter=None)`
   - `guardrails.py` → `check_input(text) -> verdict`, `check_output(answer) -> verdict`
   - `generate.py` → `generate(context_chunks, question) -> str`

### Done when

- [x] Folder/file tree matches `architecture.md` §4
- [x] `pip install -r requirements.txt` succeeds; `python -c "import src.config"` works
- [x] All config knobs from `architecture.md` §5 exist in `src/config.py`
- [x] Stub modules import without error

---

# Phase 1 — Loading: Corpus Collection & Loader (Milestone M1)

**Goal:** Fetch the 5 scheme pages (+ optional official supplements), snapshot them, and produce the source list deliverable.

### Tasks

1. **`src/ingest.py` → `load_sources()`:**
   - For each entry in `config.SCHEMES`: HTTP GET the URL (timeout, 3 retries, browser-like User-Agent).
   - Save raw HTML to `data/raw/<category>.html`.
   - Extract clean markdown/text: strip `script`, `style`, nav, footer, ads; **preserve headings** (`h1–h4`); use `markdownify` or equivalent.
   - Return a list of document records:
     ```python
     {"source_url": str, "page_title": str, "fetched_at": iso_datetime,
      "category": str, "scheme": str, "text": str}
     ```
   - **Allow-list enforcement (C1):** refuse to fetch any URL whose domain is not in `SOURCE_ALLOWLIST`; raise a clear error.
2. **Optional supplements (only if easily fetchable):** HDFC factsheet page, HDFC statement/tax-doc guide, AMFI investor education page. Same snapshot + extract pipeline. If a PDF: parse with `pypdf`, snapshot to `data/raw/`.
3. **Set `FACTSHEET_LINK`** in config to an official factsheet URL discovered during collection.
4. **Generate deliverable D2:** `sources.csv` with columns `url, scheme, category, fetched_at, status(ok/failed)` — written automatically by `load_sources()`. Also emit `sources.md` (same content, human-readable).
5. Handle failures gracefully: log `status=failed` in the source list, continue with the rest.

### Done when

- [x] `python -m src.ingest --load-only` (or equivalent) fetches all 5 scheme pages; raw HTML exists in `data/raw/`
- [x] Extracted text for each scheme contains the key facts (expense ratio, exit load, minimum SIP, benchmark) — verify by grepping
- [x] `sources.csv` and `sources.md` exist listing exactly the URLs used (deliverable D2)
- [x] Attempting a non-allow-listed URL raises an error
- [x] No PII-related storage code exists anywhere (nothing to store by design)

---

# Phase 2 — Chunking & Embedding & Vector Store (Milestone M2)

**Goal:** Full offline ingestion pipeline: text → chunks → vectors → ChromaDB.

### Tasks

1. **Inspect the loaded data first** (this decides the chunking strategy):
   - Print a few extracted text samples per scheme.
   - Classify: mostly structured key–value tables, mostly FAQ prose, or mixed → apply the matching strategy from `architecture.md` §2.2 (expected: **hybrid — heading-aware split + ~12% overlap**).
2. **`chunk_documents(doc_records) -> list[chunk]`:**
   - Split on headings (`h1–h4`) first, then paragraphs/sentences within a section.
   - **Fact-integrity rule:** never split a `label: value` pair across a boundary (regex-guard: a chunk cannot end with a dangling label like `Exit Load:` whose value lands in the next chunk).
   - Target `CHUNK_SIZE` ≈ 500 chars/tokens with `CHUNK_OVERLAP` 10–15%; tune after looking at real data.
   - Chunk metadata (exact fields from `architecture.md` §2.2):
     ```json
     {"source_url": "...", "scheme": "...", "category": "...", "section": "...",
      "ingested_at": "...", "chunk_index": 12}
     ```
3. **`build_index()`:**
   - Load `sentence-transformers/all-MiniLM-L6-v2` (384-dim); embed chunks in batches.
   - Open ChromaDB **persistent** client at `CHROMA_PATH`, collection `mf_facts`.
   - **Idempotent:** delete the collection if it exists, then recreate and add (deterministic `ids`, e.g., `f"{category}_{chunk_index}"`).
   - Store `documents`, `metadatas`, `ids` (Chroma handles embeddings).
   - Set `LAST_UPDATED` = max `fetched_at` from snapshots; persist to `data/meta.json` so generation/UI can read it.
4. **CLI:** `python -m src.ingest` runs load (or reuses snapshots if present) → chunk → embed → store, and prints counts: sources, chunks, collection size.
5. **`sources.csv` regeneration** must remain in this flow.

### Done when

- [x] `python -m src.ingest` completes end-to-end; `./chroma_db` created
- [x] Running it twice does not duplicate entries (idempotent)
- [x] Chunk spot-check: every fact (label+value) is intact within a single chunk
- [x] Every chunk has all 6 metadata fields
- [x] Embedding model is exactly `all-MiniLM-L6-v2` (assert in code)
- [x] Strategy + rationale noted for the README (finalize in Phase 5)

---

# Phase 3 — Retrieval + Grounded Generation (Milestone M3)

**Goal:** Question in → retrieved chunks → grounded answer with citation and last-updated line.

### Tasks

1. **`src/retrieve.py` → `search(query, top_k=TOP_K, scheme_filter=None)`:**
   - Embed query with the same MiniLM model.
   - ChromaDB `query` on `mf_facts`, `n_results=max(CANDIDATE_POOL, top_k)`.
   - **Scheme metadata pre-filter:** detect if the question names a scheme/category (keyword map: `"large cap"`, `"ELSS"/"tax saver"`, `"flexi"/"equity fund"`, `"small cap"`, `"balanced advantage"`) → pass Chroma `where={"category": ...}`.
   - **Hybrid re-rank (M3 fix):** `score = cosine + LEXICAL_WEIGHT · IDF-overlap`
     over the pool (stop-words + navigational/scheme tokens stripped from the
     query's lexical side) → sort → cut to `top_k`. Prevents name-heavy prose
     from burying bare fact chunks (e.g. mined `Fund data / Lock-in period: 3Y`)
     outside the window — rationale + measurements in `docs/chunking_strategy.md`.
   - Return chunk dicts: `{text, source_url, scheme, category, section, score, dense}`.
2. **`src/generate.py` → `generate(context_chunks, question)`:**
   - Build prompt:
     - **System:** "You are a factual mutual fund FAQ assistant for HDFC schemes. Answer ONLY from the provided context. Maximum 3 sentences. End with the line exactly: `Last updated from sources: <date>` (use the date given). Never give investment advice. Never compute or compare returns. If the context doesn't contain the answer, say you couldn't find it in the sources and cite the scheme's source page."
     - **User:** `<context>` — each chunk prefixed with `[Source: <source_url>]` … then the question.
   - LLM behind a single swappable interface: `generate(context, question) -> str` (OpenAI-compatible API or local model via env vars `LLM_MODEL`/`LLM_API_KEY` in `.env`; document the choice).
   - Post-process: ensure a `Source: <url>` line (use top-ranked chunk's `source_url` if the model omitted it) and the `Last updated from sources:` line (append from `data/meta.json` if missing).
3. **Minimal end-to-end runner:** `python -m src.ask "What is the exit load of HDFC Large Cap Fund?"` prints answer + citation + last-updated.
4. Collect answers produced during testing — they seed the Phase 5 `sample_qa.md`.

### Done when

- [x] Factual questions across all 7 fact types (expense ratio, exit load, min SIP, ELSS lock-in, riskometer, benchmark, statement download) return correct, source-grounded answers
- [x] Every printed answer: ≤ 3 sentences, exactly one citation URL, `Last updated from sources: <date>`
- [x] Scheme-filtered question retrieves only that scheme's chunks
- [x] Unanswerable question triggers the "couldn't find it in sources" path (not a hallucination)
- [x] At least one correct answer verified for each of the 5 schemes

---

# Phase 4 — Guardrails (Milestone M4)

**Goal:** Deterministic input/output guard layer (PRD constraints C2, C3, C6) — enforced in code, not just prompt.

### Tasks

1. **`src/guardrails.py` → INPUT `check_input(text) -> dict`** — runs before retrieval; returns `{"allow": bool, "type": ..., "reply": str, "link": str}`:
   - **PII screen (C2)** — regex patterns for: PAN (`[A-Z]{5}[0-9]{4}[A-Z]`), Aadhaar (12 digits, optional `XXXX` groups), Indian mobile (10 digits starting 6–9, with/without +91), email, OTP-ish phrases ("otp", "one time password"), bank account + IFSC.
     → Reply: *"Please don't share personal information (PAN, Aadhaar, phone, OTP, account details). This assistant doesn't need or store it."* **Nothing from the input is logged or persisted.**
   - **Advice-intent gate (C6):** patterns like `should i (buy|sell|invest|switch|exit)`, `best fund`, `recommend`, `worth investing`, `better fund`, `which fund should`.
     → Polite static refusal + `config.EDU_LINK`.
   - **Performance-intent gate (C3):** `returns?`, `cagr`, `1yr/3yr/5yr return`, `compare`, `outperform`, `annualized`.
     → Redirect: facts-only message + `config.FACTSHEET_LINK`; **no numbers ever generated**.
   - Order: PII → advice → performance → allow.
2. **`check_output(answer) -> dict`** — runs after generation:
   - Sentence count ≤ `MAX_ANSWER_SENTENCES` (simple splitter; if violated → replace with the insufficient-info fallback answer).
   - Exactly one `http(s)` URL in the answer, and its domain ∈ `SOURCE_ALLOWLIST` (or `EDU_LINK`/`FACTSHEET_LINK` domains for refusals) — else swap citation to the top chunk's `source_url`.
   - `Last updated from sources:` line present — append if missing.
   - Fallback answer template: *"I couldn't find that in the official sources. You can check the scheme page directly: <source_url>"* (≤ 3 sentences).
3. **Wire into the ask flow:** `check_input` → (blocked ? static reply : retrieve → generate → `check_output`) → return.
4. **Static refusal texts** (define once, reuse everywhere):
   - Advice refusal: polite, mentions "facts-only, no investment advice", includes the educational link.
   - Performance redirect: "I don't compute or compare returns. Please refer to the official factsheet: <FACTSHEET_LINK>".

### Done when

- [x] Each of these inputs is BLOCKED with the right message: a PAN number, an email, a phone number, an OTP request
- [x] "Should I buy HDFC Small Cap Fund?" → advice refusal + educational link
- [x] "Which fund gave better returns?" → factsheet redirect, **no % figures anywhere in the reply**
- [x] Blocked inputs leave **no trace** in any log/state (verify no print/log calls in the PII path)
- [x] All 7 factual question types still pass through and answer normally
- [x] `check_output` catches a deliberately over-long answer and a bad citation (assert-style checks)

---

# Phase 5 — Chat UI + Docs (Milestone M5)

**Goal:** The tiny UI plus all documentation deliverables.

### Tasks

1. **`app.py` (Streamlit):**
   - **Welcome line:** e.g., "Mutual Fund FAQ Assistant — ask factual questions about HDFC schemes."
   - **3 clickable example questions** (buttons that fill/send the input):
     - "What is the exit load of HDFC Large Cap Fund?"
     - "What is the lock-in period for HDFC ELSS Tax Saver?"
     - "How do I download a capital-gains statement?"
   - **Persistent note** (sidebar or footer): **"Facts-only. No investment advice."**
   - Chat flow: user input → Phase 4 guard/retrieve/generate pipeline → render answer.
   - **Answer rendering:** answer text, one clickable citation (`Source: <url>`), `Last updated from sources: <date>` on its own line.
   - **Refusal rendering:** uniform styled message for advice/PII/performance replies (with their links).
   - No chat-history storage of any user input beyond ephemeral session state.
2. **`README.md` (deliverable D3):**
   - Setup steps (Python version, `pip install`, `.env` keys, run `python -m src.ingest`, run `streamlit run app.py`)
   - Scope: HDFC AMC + the 5 schemes (table with URLs)
   - Chunking strategy + rationale (finalize from Phase 2 findings)
   - Known limits (from `PRD.md` §8 + lessons learned)
3. **`sample_qa.md` (deliverable D4):** 5–10 real queries run through the app — each with question, answer (≤3 sentences), citation link, last-updated date. Must cover: expense ratio, exit load, min SIP, ELSS lock-in, riskometer, benchmark, statement download, one refusal, one performance redirect.
4. **`disclaimer.md` (deliverable D5):** the exact disclaimer snippet used in the UI (e.g., "Facts-only. No investment advice. Answers come from public sources and are for information only, not recommendations.").
5. **`sources.csv` / `sources.md`** — confirm present and current (deliverable D2).

### Done when

- [x] `streamlit run app.py` shows welcome line, 3 working example buttons, and the "Facts-only. No investment advice." note
- [x] A factual question renders ≤3 sentences + one clickable citation + last-updated line
- [x] An advice question renders the uniform refusal
- [x] README, sample_qa.md, disclaimer.md, sources.csv all present (D2–D5)
- [x] Demo walk-through works top to bottom without console errors

---

# Phase 6 — Demo, Acceptance & Submission (Milestone M6)

**Goal:** Prove every PRD acceptance criterion and package the submission.

### Tasks

1. **Acceptance pass against `PRD.md` §7** — run each checkbox manually and record results:
   - Full pipeline: Loading → Chunking → Embedding → ChromaDB → retrieval → generation ✔
   - Embedding model = `all-MiniLM-L6-v2` ✔
   - Corpus = HDFC + 5 schemes only ✔
   - All 7 factual types answer correctly from corpus ✔
   - Every answer ≤3 sentences, one citation, last-updated line ✔
   - Advice refused politely + educational link ✔
   - PII blocked, nothing stored; no return/performance figures generated ✔
   - UI elements present ✔
   - Deliverables D1–D6 present ✔
2. **Regression sweep:** replay everything in `sample_qa.md` plus 5 adversarial inputs (PAN, "should I buy", "best fund", "compare returns 3yr", an off-corpus question) — all must behave per spec.
3. **Deliverable D1 decision:** host the app (Streamlit Cloud / local share) **or** record a ≤3-min demo video: pipeline overview → ingest run → 3 factual Q&As (citation shown) → 1 refusal → UI disclaimer.
4. **Final README polish:** add "Known limits" and the demo/video link.

### Done when

- [ ] Every `PRD.md` §7 acceptance checkbox checked and verified
- [ ] All deliverables D1–D6 complete
- [ ] Regression sweep passes (no hallucinated advice, no PII leakage, no performance numbers)

---

## Phase → Milestone → Deliverable Map

| Phase | Milestone (PRD §9) | Deliverables produced |
|---|---|---|
| 0 Scaffold | — | project skeleton |
| 1 Loader | **M1** | D2 `sources.csv/md`, `data/raw/` |
| 2 Ingestion | **M2** | ChromaDB index, chunking rationale notes |
| 3 Retrieval + Gen | **M3** | working Q&A path |
| 4 Guardrails | **M4** | refusal/PII/performance behavior |
| 5 UI + Docs | **M5** | D3 README, D4 sample_qa, D5 disclaimer, UI |
| 6 Demo + Acceptance | **M6** | D1 prototype/video, verified PRD §7 |

## Troubleshooting Notes for Cursor Sessions

- **Wrong/empty answers in Phase 3** → first check ChromaDB actually has chunks (`count()`), then check query embedding uses the same model, then check the scheme filter isn't over-restricting.
- **Facts split across chunks** → tighten the fact-integrity regex; split at heading boundaries rather than mid-field; reduce `CHUNK_SIZE`.
- **Groww blocks fetches** → rely on the `data/raw/` snapshots; adjust headers/timeout; document the issue — do NOT substitute a domain outside the allow-list.
- **LLM ignoring the 3-sentence rule** → `check_output` must enforce it regardless; the prompt is best-effort, the guard is the guarantee.
- **Sentence-count false positives** (e.g., "₹1% within 365 days.") → refine the splitter; prefer failing safe to the fallback answer over shipping a 5-sentence reply.

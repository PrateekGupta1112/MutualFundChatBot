# Mutual Fund FAQ RAG Chatbot

A **facts-only** retrieval-augmented generation (RAG) assistant for factual
questions about five HDFC Mutual Fund schemes. It answers in ≤ 3 sentences,
always cites exactly one public source, always shows how fresh the data is,
and **never** gives investment advice, discusses returns, or touches PII.

```
Question → guardrails → retrieval (ChromaDB + hybrid re-rank) → grounded LLM answer → output guard → answer + citation + date
```

Built from [`PRD.md`](PRD.md) · architecture per [`architecture.md`](architecture.md)
· phase log in [`implementation.md`](implementation.md).

---

## 1. Setup

**Requirements:** Python 3.12 (tested on macOS x86_64 / Intel).

```bash
pip install -r requirements.txt          # torch/numpy are pinned — see note below
```

> ⚠️ **Intel Mac pinning:** PyPI stopped shipping x86_64 torch wheels after
> 2.2.2 and `transformers` 5.x needs torch ≥ 2.5. Keep the pins in
> `requirements.txt` (`torch==2.2.2`, `numpy<2`, `sentence-transformers==3.4.1`)
> — do not upgrade them.

**Configure the LLM** — create a `.env` (git-ignored):

```bash
LLM_API_KEY=gsk_...            # Groq API key (any OpenAI-compatible endpoint works)
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=openai/gpt-oss-120b  # verify current ids: GET {LLM_BASE_URL}/models
```

If `LLM_API_KEY` is missing or the API fails after 2 retries, the bot falls
back to a deterministic **extractive** answer mode — the pipeline never dies.

**Build the index, then run the app:**

```bash
python3 -m src.ingest            # fetch/reuse snapshots → chunk → embed → ChromaDB
streamlit run app.py             # chat UI  (http://localhost:8501)
```

CLI alternative (no UI):

```bash
python3 -m src.ask "What is the exit load of HDFC Large Cap Fund?"
python3 -m src.ask               # interactive Q> loop
python3 -m src.ask --chunks      # also show retrieved chunks + hybrid scores
```

**Refreshing the data** (snapshots are a point-in-time copy):

```bash
python3 -m src.ingest --refresh  # re-fetch the 5 pages, re-chunk, re-embed (~30 s)
```

Run this right before a demo so NAVs match the live site; the
`Last updated from sources: <date>` line then shows the new fetch date.

---

## 2. Scope — one AMC, five schemes

| Scheme | Category | Source |
|---|---|---|
| HDFC Large Cap Fund – Direct – Growth | Large Cap | [groww.in/…/hdfc-large-cap-fund-direct-growth](https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth) |
| HDFC Equity Fund – Direct – Growth *(now Flexi Cap)* | Flexi Cap | [groww.in/…/hdfc-equity-fund-direct-growth](https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth) |
| HDFC ELSS Tax Saver Fund – Direct – Growth | ELSS | [groww.in/…/hdfc-elss-tax-saver-fund-direct-plan-growth](https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth) |
| HDFC Small Cap Fund – Direct – Growth | Small Cap | [groww.in/…/hdfc-small-cap-fund-direct-growth](https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth) |
| HDFC Balanced Advantage Fund – Direct – Growth | Hybrid | [groww.in/…/hdfc-balanced-advantage-fund-direct-growth](https://groww.in/mutual-funds/hdfc-balanced-advantage-fund-direct-growth) |

- **Source allow-list (C1):** `groww.in`, `hdfcfmf.com`, `amfiindia.org`,
  `sebi.gov.in` — enforced in code at fetch time *and* at citation check.
  Nothing outside the list is ever ingested or cited.
- Full source list with fetch status: [`sources.csv`](sources.csv) /
  [`sources.md`](sources.md) (deliverable D2).
- **Out of scope:** any other scheme/AMC, prices, NAVs beyond the snapshot,
  returns, rankings, opinions.

---

## 3. How it works

### Loading (Phase 1)
HTTP GET each scheme page with a browser-like UA (3 retries, timeout) →
snapshot raw HTML to `data/raw/<category>.html` → strip scripts/nav/footers
with BeautifulSoup + `markdownify`, preserving `h1–h4` headings → clean text
per page. Page-JSON fact mining additionally extracts whitelisted facts
(`lock_in`, `exit_load`) that the rendered page hides.

### Chunking strategy + rationale (Phase 2)
Data inspection showed **mixed** content — key–value fact stacks, tables, and
glossary prose — so the strategy is a **hybrid**:

1. **Heading-aware split** — sections first (`Key facts`, `Expense ratio`,
   `Fund manager`, …), so a chunk never straddles two topics.
2. **Label/value glue** — a `label: value` pair is never split across chunks
   (regex fact-integrity guard: no chunk may end on a dangling label).
3. **Packing** — lines pack into chunks up to `CHUNK_SIZE = 500` chars with
   `CHUNK_OVERLAP = 12%`, so a fact survives at a boundary.
4. **Noise filters** — nav-junk lines (chart tabs, top-nav rows) and signed
   return figures are dropped at chunk time (C3: no performance data in the
   index at all).

Result: **105 chunks / 5 schemes**, every fact intact in one chunk.
Full rationale + measurements: [`docs/chunking_strategy.md`](docs/chunking_strategy.md).

### Embedding + vector store (Phase 2)
`sentence-transformers/all-MiniLM-L6-v2` (384-dim, **fixed by spec**) →
persistent ChromaDB at `./chroma_db`, single collection `mf_facts`,
idempotent rebuild (drop + recreate, deterministic ids). All 6 metadata
fields per chunk: `source_url, scheme, category, section, ingested_at,
chunk_index`.

### Retrieval (Phase 3)
1. Category **pre-filter** from the question ("flexi cap", "tax saver", …)
   so cross-scheme facts can never mix.
2. Query embedded with the **same** MiniLM model.
3. Dense pool of 32 candidates, **hybrid re-ranked**:
   `score = cosine + 0.3 · IDF-overlap` (stop-words and scheme-name tokens
   stripped from the lexical side), cut to `TOP_K = 8`.
   Pure cosine buried short bare-fact chunks (e.g. `Lock-in period: 3Y`)
   below the cut for name-heavy queries — measured 16/16 facts in-window
   after the fix vs 11/16 before.

### Guarded generation (Phases 3–4)
**Input guard** (order: PII → advice → performance → allow) runs *before*
retrieval; blocked inputs return a static reply and never reach the LLM.
The prompt constrains the model to the retrieved context, ≤ 3 sentences,
with the date line. **Output guard** then enforces in code: sentence count,
exactly one allow-listed citation URL (swapped to the top chunk's URL if
bad), and the `Last updated from sources: <date>` line. Guards are code,
not prompts.

---

## 4. UI (`app.py`)

```bash
streamlit run app.py
```

- Welcome line + **3 clickable example questions**
- Chat input → full guard/retrieve/generate pipeline
- Answers render as: text · one clickable `Source:` link ·
  `Last updated from sources: <date>` on its own line
- Refusals render as a uniform styled card (advice / performance / PII)
- Persistent **"Facts-only. No investment advice."** note (sidebar + footer)
- Questions live only in ephemeral `st.session_state` — nothing is stored

Exact UI text: [`disclaimer.md`](disclaimer.md) (D5).

---

## 5. Deliverables

| # | Deliverable | File |
|---|---|---|
| D1 | Working prototype | this repo — `streamlit run app.py` (+ demo video) |
| D2 | Source list | [`sources.csv`](sources.csv) / [`sources.md`](sources.md) |
| D3 | README | this file |
| D4 | Sample Q&A | [`sample_qa.md`](sample_qa.md) |
| D5 | Disclaimer snippet | [`disclaimer.md`](disclaimer.md) |
| D6 | PRD | [`PRD.md`](PRD.md) |

---

## 6. Known limits

| Limit | Notes / mitigation |
|---|---|
| **Snapshot data** | Answers reflect the last fetch, not the live site. The `Last updated from sources:` line discloses it; refresh with `python3 -m src.ingest --refresh`. |
| **Statement-download questions unanswerable** | The scheme pages only carry a footer link to statement downloads — the how-to text isn't in the corpus, so the bot honestly answers "couldn't find" and points at the scheme page (verified behaviour, not a bug). |
| **Supplement sources unreachable** | `hdfcfund.com` bot-blocks server-side fetches (403) and `amfiindia.org`/`sebi.gov.in` were unreachable from this network — the corpus is the 5 Groww pages; the official factsheet/education URLs are used as *links* only. |
| **Groq free-tier rate limits** | Bursts of requests can 429. Two retries with 2s/4s backoff, then a deterministic extractive fallback keeps the demo alive (`[warn]` goes to stderr). |
| **LLM formatting quirks** | `gpt-oss-120b` emits narrow no-break spaces, curly quotes and occasional markdown/citation decorations — `_postprocess` normalizes these; `check_output` is the guarantee, the prompt is best-effort. |
| **Sentence-count splitter** | Decimals like `₹100.` can false-positive; policy is to fail safe to the fallback answer rather than ship a > 3-sentence reply. |
| **Off-corpus questions** | Asking about a non-scoped scheme (e.g. "HDFC Infrastructure Fund") hits a deterministic "couldn't find" path — scope is exactly the 5 schemes. |
| **Intel Mac stack** | torch/transformers pins must not be upgraded (see Setup). |

---

## 7. Project layout

```
├── app.py                  # Streamlit UI (Phase 5)
├── PRD.md · architecture.md · implementation.md   # specs
├── requirements.txt · .env (git-ignored) · .gitignore
├── sources.csv / sources.md               # D2
├── sample_qa.md · disclaimer.md           # D4 · D5
├── docs/
│   ├── chunking_strategy.md               # chunking + retrieval rationale
│   └── phase3_qa_notes.md                 # verified Q/A log
├── src/
│   ├── config.py           # all knobs (architecture §5)
│   ├── ingest.py           # load → chunk → embed → ChromaDB (CLI)
│   ├── retrieve.py         # query embed + category filter + hybrid re-rank
│   ├── generate.py         # LLM client + prompt + post-process + fallback
│   ├── guardrails.py       # input gates + output contract (C2/C3/C6)
│   └── ask.py              # end-to-end runner (CLI / UI entry point)
├── data/
│   ├── raw/                # HTML snapshots (git-ignored)
│   ├── meta.json           # last_updated, chunk counts
│   └── chunks.txt          # exported chunks + embeddings
└── chroma_db/              # persistent vector store (git-ignored)
```

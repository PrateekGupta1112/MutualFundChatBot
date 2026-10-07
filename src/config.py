"""Central configuration for the Mutual Fund FAQ RAG chatbot.

All knobs from architecture.md §5 live here. Values marked "finalized in
Phase N" are defaults for scaffolding and may be tuned in that phase.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"                 # cached source snapshots (git-ignored)
META_PATH = DATA_DIR / "meta.json"         # last-updated date, set at ingest
CHROMA_PATH = str(BASE_DIR / "chroma_db")  # persistent vector store (git-ignored)
SOURCES_CSV = BASE_DIR / "sources.csv"     # deliverable D2
SOURCES_MD = BASE_DIR / "sources.md"       # deliverable D2

# ---------------------------------------------------------------------------
# Embedding + vector store (fixed by spec — never change)
# ---------------------------------------------------------------------------
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384
COLLECTION_NAME = "mf_facts"

# ---------------------------------------------------------------------------
# Retrieval + chunking
# ---------------------------------------------------------------------------
TOP_K = 8             # tuned 5→8 in M3: fact-stack chunks ranked #6–7 for
                       # expense/benchmark queries at 5 (verified in docs/chunking_strategy.md)
CANDIDATE_POOL = 32    # dense candidates fetched for hybrid re-rank (≥ largest
                       # per-scheme category, so filtered queries re-rank the
                       # whole scheme); fix for the M3 "lock-in" miss.
LEXICAL_WEIGHT = 0.3   # λ in score = cosine + λ·IDF-overlap (0 = dense-only).
                       # Tuned so bare fact chunks (e.g. ELSS "Fund data") rise
                       # into TOP_K without displacing prose chunks — see
                       # docs/chunking_strategy.md § hybrid re-ranking.
CHUNK_SIZE = 500          # tokens/chars — finalized in Phase 2
CHUNK_OVERLAP = 0.12      # 10–15%

# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
SOURCE_ALLOWLIST = ["groww.in", "hdfcfmf.com", "amfiindia.org", "sebi.gov.in"]
MAX_ANSWER_SENTENCES = 3
EDU_LINK = "https://www.sebi.gov.in/investors.html"  # advice refusals
# Official HDFC MF factsheet page (Phase 1 follow-up; verified via Wayback 200
# @ 2026-09-14 — hdfcfund.com bot-blocks server-side fetches with 403).
FACTSHEET_LINK = "https://www.hdfcfund.com/mutual-funds/factsheets"
LAST_UPDATED = ""         # set from snapshot fetch date at ingest time

# ---------------------------------------------------------------------------
# Scope: one AMC (HDFC) + 5 schemes
# ---------------------------------------------------------------------------
SCHEMES = [
    {
        "scheme": "HDFC Large Cap Fund - Direct Plan - Growth",
        "category": "Large Cap",
        "url": "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
    },
    {
        "scheme": "HDFC Equity Fund - Direct Plan - Growth",
        "category": "Flexi Cap",
        "url": "https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth",
    },
    {
        "scheme": "HDFC ELSS Tax Saver Fund - Direct Plan - Growth",
        "category": "ELSS",
        # PRD's URL (…-direct-growth) 404s on Groww; corrected to the live slug.
        "url": "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth",
    },
    {
        "scheme": "HDFC Small Cap Fund - Direct Plan - Growth",
        "category": "Small Cap",
        "url": "https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth",
    },
    {
        "scheme": "HDFC Balanced Advantage Fund - Direct Plan - Growth",
        "category": "Hybrid",
        "url": "https://groww.in/mutual-funds/hdfc-balanced-advantage-fund-direct-growth",
    },
]

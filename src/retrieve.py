"""Query-side retrieval: query embedding + hybrid re-rank (Phase 3).

The query is embedded with the SAME model used at ingest
(`sentence-transformers/all-MiniLM-L6-v2`) and passed to Chroma as an
explicit embedding — no other embedding model touches the index (spec-fixed).

A wider dense pool is then re-ranked with cosine + IDF-weighted lexical
overlap (see docs/chunking_strategy.md § hybrid re-ranking): name-heavy
queries otherwise drown short bare-fact chunks (e.g. the mined ELSS
"Fund data / Lock-in period: 3Y" chunk) below TOP_K.
"""

from __future__ import annotations

import math
import re

import chromadb

from . import config

# Keyword → category metadata pre-filter (architecture §2.3).
# If the question names exactly one category, Chroma filters on it; if it names
# several (or none), the search stays unfiltered rather than over-restricting.
_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("Large Cap", ("large cap", "largecap", "large-cap")),
    ("Flexi Cap", ("flexi cap", "flexicap", "flexi-cap", "flexi",
                   "equity fund")),
    ("ELSS", ("elss", "tax saver", "tax-saver", "tax saving")),
    ("Small Cap", ("small cap", "smallcap", "small-cap")),
    ("Hybrid", ("balanced advantage", "hybrid")),
]

_model = None  # lazily loaded SentenceTransformer (shared across queries)

# Function words stripped from the query before lexical matching (the IDF
# weights already down-weight scheme-name tokens that appear in most chunks).
_STOPWORDS = frozenset(
    "a an the is are was were be to of in on at for and or not no do does did "
    "who whom whose which what when where why how my your our its it this that "
    "these those i you he she they we me us them him her as by from with "
    "please tell about".split()
)

# Navigational tokens — scheme names, plan classes and category words — are
# also stripped from the LEXICAL term: the category filter and the dense
# embedding already scope the scheme, and letting these tokens into the
# IDF denominator lets scheme-name-heavy prose ("About …") monopolize the
# lexical score while short bare-fact chunks ("Lock-in period: 3Y") starve.
_NAV_TOKENS = frozenset(
    "hdfc fund funds scheme schemes direct plan growth option amc mutual "
    "large cap caps flexi small hybrid elss tax saver saving equity "
    "balanced advantage".split()
)


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens — also splits "lock-in" ⇒ lock, in, so the
    lexical term matches across hyphen/spacing variants the dense model misses."""
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _lexical_scores(query: str, pool: list[dict]) -> list[float]:
    """IDF-weighted overlap of query tokens with each chunk, normalized to [0, 1].

    IDF is computed over the candidate pool, so tokens common to every chunk
    ("fund", scheme names) contribute almost nothing while rare fact tokens
    ("lock", "period", "0.77") dominate. Query tokens absent from the whole
    pool (e.g. "second" in ordinal questions) are excluded from the divisor.
    """
    q_tokens = [t for t in _tokenize(query)
                if t not in _STOPWORDS and t not in _NAV_TOKENS]
    if not q_tokens or not pool:
        return [0.0] * len(pool)

    n = len(pool)
    chunk_tokens: list[set[str]] = []
    df: dict[str, int] = {}
    for chunk in pool:
        # Match against section title too — it often carries the signal
        # ("Key facts", "DM Dhruv Muchhal Jun 2023 - Present").
        toks = set(_tokenize(chunk.get("section", ""))) | set(_tokenize(chunk["text"]))
        chunk_tokens.append(toks)
        for t in toks:
            df[t] = df.get(t, 0) + 1

    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 1))
           for t in set(q_tokens) if df.get(t)}
    denom = sum(idf.values())
    if not denom:
        return [0.0] * len(pool)
    return [sum(idf[t] for t in idf if t in toks) / denom for toks in chunk_tokens]


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        # Same model as ingest — fixed by spec, never change.
        assert config.EMBED_MODEL == "sentence-transformers/all-MiniLM-L6-v2"
        _model = SentenceTransformer(config.EMBED_MODEL)
    return _model


def detect_scheme_filter(query: str) -> str | None:
    """Return the category named in the query, else None (none/ambiguous)."""
    q = (query or "").lower()
    matched = [cat for cat, kws in _CATEGORY_KEYWORDS
               if any(kw in q for kw in kws)]
    return matched[0] if len(matched) == 1 else None


def search(query, top_k=None, scheme_filter=None):
    """Embed query and return top-k chunks (optionally filtered by scheme).

    Returns [{"text", "source_url", "scheme", "category", "section", "score",
    "dense"}] where `score` = dense cosine + LEXICAL_WEIGHT · IDF-overlap is the
    ranking score (collection space is L2 over normalized vectors ⇒
    cos = 1 − d²/2, kept unchanged in `dense`).
    """
    top_k = top_k or config.TOP_K
    category = scheme_filter or detect_scheme_filter(query)

    collection = chromadb.PersistentClient(path=config.CHROMA_PATH).get_collection(
        config.COLLECTION_NAME
    )
    query_embedding = _get_model().encode(
        [query], normalize_embeddings=True, convert_to_numpy=True
    )[0]

    res = collection.query(
        query_embeddings=[query_embedding.tolist()],
        n_results=max(config.CANDIDATE_POOL, top_k),
        where={"category": category} if category else None,
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    for text, meta, dist in zip(
        res["documents"][0], res["metadatas"][0], res["distances"][0]
    ):
        chunks.append({
            "text": text,
            "source_url": meta["source_url"],
            "scheme": meta["scheme"],
            "category": meta["category"],
            "section": meta["section"],
            "dense": round(1 - (dist ** 2) / 2, 4),
            "score": round(1 - (dist ** 2) / 2, 4),
        })

    # Hybrid re-rank: dense cosine + λ · IDF-weighted lexical overlap.
    lam = getattr(config, "LEXICAL_WEIGHT", 0.0)
    if lam and chunks:
        for chunk, lex in zip(chunks, _lexical_scores(query, chunks)):
            chunk["score"] = round(chunk["dense"] + lam * lex, 4)
        chunks.sort(key=lambda c: c["score"], reverse=True)
    return chunks[:top_k]

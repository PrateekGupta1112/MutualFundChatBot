"""Offline ingestion pipeline: loading → chunking → embedding → ChromaDB.

Phase 1: load_sources() + source list (D2).
Phase 2: chunk_documents() + build_index() + full CLI.

Chunking strategy (data-driven; see docs/chunking_strategy.md):
hybrid — heading-aware section split → label/value glue → unit packing
(~500 chars, ~12% overlap) with a fact-integrity guard.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import chromadb
import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from . import config

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
FETCH_TIMEOUT = 30
FETCH_RETRIES = 3
RETRY_BACKOFF = 2.0

# Boilerplate tags to drop before converting to markdown (C1: clean extract).
_STRIP_TAGS = ["script", "style", "noscript", "svg", "iframe", "form", "header",
               "footer", "nav", "aside", "button"]

# Sections excluded from the index (data-driven, documented in
# docs/chunking_strategy.md): navigation/calculator noise, holdings dumps,
# and performance tables — we never discuss returns (C3).
_SECTION_SKIP = re.compile(
    r"^(return calculator|holdings\b.*|returns and rankings|compare similar funds)$",
    re.I,
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LABEL_LINE_RE = re.compile(r"^[A-Za-z₹][A-Za-z0-9₹ .,:/&()%'-]*$")
# Pure nav/chart junk lines (Phase 3 retrieval fix): the stat-stack preamble
# is polluted with top-nav rows and chart range tabs, which dilute the chunk's
# embedding so real facts ("Expense ratio: 1.04%") fall out of the top-k.
# Patterns are exact-line so no fact line can match:
#   chart tabs      "1M" "6M" "1Y" "3Y" "5Y" "All"
#   chart caption   "3Y annualised"
#   top-nav rows    "Stocks: F&O" "Mutual Funds: More" "Credit: Loan..."
#   app promo       "Download the App: GROWW"
_NAV_JUNK_RE = re.compile(
    r"^(?:"
    r"(?:1D|1W|1M|3M|6M|1Y|3Y|5Y|10Y|All)"
    r"|\d+[YMWD] annualised"
    r"|(?:Stocks|Mutual Funds|Credit|ETF|IPO|MTF|F&O|GOLD|More)(?::.*)?"
    r"|Loan against securities and Personal loan"
    r"|Download the App.*"
    r")$"
)
# Signed return figures from the page's chart stats (C3: never index returns).
_NOISE_LINE_RE = re.compile(r"^[+-]\d+(?:\.\d+)?%(?:\s+[0-9A-Za-z]+)?$")
# AMC-wide boilerplate mislabelled as scheme facts (fund-size ambiguity fix):
# every scheme page repeats "The fund currently has an AUM of ₹9,86,237 Cr
# and the Latest NAV ..." — an IDENTICAL figure on all 5 pages (it's the AMC
# total, not the scheme's) paired with the scheme's NAV, plus a "Total AUM₹…"
# widget. Both contradict the scheme's own "Fund size (AUM)" line, so the
# generator could legitimately quote either number. Two shapes:
#  1) a sentence embedded MID-PARAGRAPH in the About text (removed anywhere
#     via _AMC_AUM_SENT_RE, bounded so it can't eat neighbouring facts), and
#  2) a standalone "Total AUM₹…" widget line (exact-line _AMC_AUM_RE).
# Neither can touch scheme fact lines like "Fund size (AUM): ₹1,13,606.46 Cr".
_AMC_AUM_RE = re.compile(r"^Total AUM₹.*$")
_AMC_AUM_SENT_RE = re.compile(
    r"The fund currently has an Asset Under Management\(AUM\) of "
    r".*? Latest NAV as of .*? is ₹[\d,.]+\.?\s*"
)
# Page-JSON fact mining (Phase 3 fix): some facts — notably the ELSS 3Y
# lock-in — exist ONLY in Groww's embedded analysis JSON and header badge,
# both stripped by normal extraction. Whitelist non-performance subjects
# only, so no return/comparison text ever enters the index (C3).
_JSON_FACT_SUBJECTS = {"lock_in", "exit_load"}
_JSON_FACT_RE = re.compile(
    r'"analysis_type":"[A-Z]+","analysis_subject":"([^"]+)",'
    r'"analysis_desc":"([^"]+)"'
)


class SourceNotAllowedError(Exception):
    """Raised when a URL's domain is outside config.SOURCE_ALLOWLIST (C1)."""


# ---------------------------------------------------------------------------
# Stage 1 — Loading
# ---------------------------------------------------------------------------

def _check_allowlist(url: str) -> None:
    """Refuse any domain not on the source allow-list (PRD constraint C1)."""
    host = (urlparse(url).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in config.SOURCE_ALLOWLIST):
        raise SourceNotAllowedError(
            f"Domain not allowed by SOURCE_ALLOWLIST (C1): {url}"
        )


def _fetch(url: str) -> requests.Response:
    """HTTP GET with browser-like UA, timeout, and bounded retries."""
    last_exc: Exception | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            resp = requests.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT
            )
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 — retry any transport/HTTP error
            last_exc = exc
            if attempt < FETCH_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"Failed to fetch {url} after {FETCH_RETRIES} attempts: {last_exc}")


def _extract_text(html: str) -> tuple[str, str]:
    """Return (page_title, clean markdown) from raw HTML.

    Strips scripts/styles/nav/boilerplate, preserves h1–h4 headings.
    """
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = md(str(main), heading_style="ATX")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Mine whitelisted facts from the page's embedded analysis JSON
    # (e.g. "Lock-in period: 3Y" — otherwise lost with the stripped scripts).
    facts: list[str] = []
    for subject, desc in _JSON_FACT_RE.findall(html):
        if subject in _JSON_FACT_SUBJECTS:
            fact = desc.replace("\\n", " ").strip()
            if fact and fact not in facts:
                facts.append(fact)
    if facts:
        text += "\n\n#### Fund data\n" + "\n".join(f"- {f}" for f in facts)
    return title, text


def load_sources(refresh: bool = False) -> list[dict]:
    """Fetch every scoped source → snapshot raw HTML → clean text + metadata.

    Reuses data/raw/<category>.html snapshots when present (unless refresh=True);
    fetched_at is then the snapshot's file mtime.

    Returns document records:
        {"source_url", "page_title", "fetched_at", "category", "scheme", "text"}

    Writes data/raw/<category>.html snapshots and the sources.csv / sources.md
    deliverables (D2). Failed fetches are recorded with status=failed and do
    not abort the run.
    """
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    docs: list[dict] = []
    rows: list[dict] = []

    for entry in config.SCHEMES:
        url, category, scheme = entry["url"], entry["category"], entry["scheme"]
        row = {"url": url, "scheme": scheme, "category": category,
               "fetched_at": "", "status": "failed"}
        snapshot = config.RAW_DIR / f"{category}.html"
        try:
            _check_allowlist(url)  # C1 — raises SourceNotAllowedError
            if snapshot.exists() and not refresh:
                html = snapshot.read_text(encoding="utf-8")
                fetched_at = datetime.fromtimestamp(
                    snapshot.stat().st_mtime, tz=timezone.utc
                ).isoformat(timespec="seconds")
            else:
                resp = _fetch(url)
                html = resp.text
                snapshot.write_text(html, encoding="utf-8")
                fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            title, text = _extract_text(html)
            docs.append({
                "source_url": url,
                "page_title": title,
                "fetched_at": fetched_at,
                "category": category,
                "scheme": scheme,
                "text": text,
            })
            row.update(fetched_at=fetched_at, status="ok")
            print(f"[ok]    {category}: {len(text)} chars extracted")
        except Exception as exc:  # noqa: BLE001 — record failure, keep going
            row["status"] = f"failed ({exc})"
            print(f"[fail]  {category}: {exc}", file=sys.stderr)
        rows.append(row)

    _write_source_list(rows)
    return docs


def _write_source_list(rows: list[dict]) -> None:
    """Write deliverable D2: sources.csv + sources.md (url, scheme, category,
    fetched_at, status)."""
    fields = ["url", "scheme", "category", "fetched_at", "status"]
    with config.SOURCES_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    lines = ["# Source List", "",
             "| url | scheme | category | fetched_at | status |",
             "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['url']} | {r['scheme']} | {r['category']} "
                     f"| {r['fetched_at']} | {r['status']} |")
    config.SOURCES_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Stage 2 — Chunking (hybrid: heading-aware + label glue + packed units)
# ---------------------------------------------------------------------------

def _clean_links(text: str) -> str:
    """Remove internal nav links entirely (junk), keep external link text.

    Internal links (`[...](/path)`) are page navigation/similar-fund lists.
    External links (`[...](https://...)`, e.g. SID) keep their display text.
    """
    text = re.sub(r"\[([^\]]*?)\]\((/[^)]*?)\)", "", text, flags=re.S)
    text = re.sub(r"\[([^\]]*?)\]\((https?://[^)]*?)\)", r"\1", text, flags=re.S)
    # Drop nav glyph artifacts (">>>" breadcrumb arrows etc.)
    text = re.sub(r"^\s*>>+\s*", "", text, flags=re.M)
    # Drop signed return figures (C3) — unsigned facts like "1.04%" are kept.
    # Drop nav/chart junk lines so fact stacks embed cleanly (retrieval fix).
    text = _AMC_AUM_SENT_RE.sub("", text)          # AMC-wide AUM sentence (any position)
    text = "\n".join(l for l in text.splitlines()
                     if not _NOISE_LINE_RE.match(l.strip())
                     and not _NAV_JUNK_RE.match(l.strip())
                     and not _AMC_AUM_RE.match(l.strip()))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Fund-manager bio rows on Groww render as avatar headings, e.g.
#   "DM Dhruv Muchhal Jun 2023 - Present View details"
# The name/tenure live ONLY in that heading — never in the body — so the
# chunk embedding contained no "fund manager" signal and manager lookups
# missed the second/third manager (e.g. Chirag Setalvad on Small Cap).
# Match → retitle the section AND inject the label into the body text so
# the chunk is self-describing in both dense and lexical retrieval.
_MANAGER_ROW_RE = re.compile(
    r"^[A-Z]{2}\s+(?P<name>.+?)\s+(?P<since>\w{3} \d{4})\s+-\s+"
    r"(?P<until>Present|\w{3} \d{4})\s+View details$"
)


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Heading-aware split: [(section_title, body)].

    Preamble (before the first heading) becomes section "Key facts" — it holds
    the page's stat stack (expense ratio, min SIP, NAV). Skips nav/noise/
    performance sections per _SECTION_SKIP.
    """
    sections: list[tuple[str, list[str]]] = [("Key facts", [])]
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            title = m.group(2).strip()
            if not sections[-1][1] and sections[-1][0] != "Key facts":
                sections[-1] = (title, [])       # empty section → retitle
            else:
                sections.append((title, []))
        else:
            sections[-1][1].append(line)
    out = []
    for title, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body or _SECTION_SKIP.match(title):
            continue
        out.append((title, body))
    return out


def _glue_labels(body: str) -> list[str]:
    """Split a section body into blocks; glue label-only blocks to their value.

    Fact integrity: pages stack stats as `Expense ratio` \\n\\n `1.04%` —
    these must become one unit (`Expense ratio: 1.04%`), never split.
    """
    blocks = [b.strip() for b in re.split(r"\n{2,}", body) if b.strip()]
    glued: list[str] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        # Single short line without terminal punctuation → label; glue its value.
        # Never treat a VALUE (starts with ₹/$ or a digit) as a label.
        nxt = blocks[i + 1] if i + 1 < len(blocks) else ""
        is_label = (
            "\n" not in block
            and len(block) <= 70
            and not block[-1] in ".!?,;"
            and len(block.split()) <= 9
            and _LABEL_LINE_RE.match(block) is not None
            and re.match(r"^([₹$]|\d)", block) is None
            and 0 < len(nxt) <= 300        # never glue to a huge block
        )
        if is_label:
            block = f"{block}: {nxt}"
            i += 1
        glued.append(block)
        i += 1
    return glued


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _table_packs(block: str, size: int) -> list[str]:
    """Split a markdown table into row-packs ≤ size, header carried each time."""
    lines = [l for l in block.splitlines() if l.strip()]
    header = lines[:2] if len(lines) >= 2 and set(lines[1].replace("|", "").replace(" ", "")) <= set(":-") else lines[:1]
    rows = lines[len(header):]
    packs, cur = [], list(header)
    for row in rows:
        if sum(len(l) + 1 for l in cur + [row]) > size and len(cur) > len(header):
            packs.append("\n".join(cur))
            cur = list(header)
        cur.append(row)
    if len(cur) > len(header) or not packs:
        packs.append("\n".join(cur))
    return packs


def _is_label_tail(text: str) -> str:
    """Return the dangling label-like tail line of a chunk (or "")."""
    if not text.strip():
        return ""
    last = text.rstrip().splitlines()[-1]
    if len(last) > 70 or not _LABEL_LINE_RE.match(last) or len(last.split()) > 9:
        return ""
    if last.endswith(":"):                  # true dangling label ("Exit load:")
        return last
    if re.search(r"\d", last) or "%" in last:
        return ""                           # contains a value → complete fact
    return last                             # bare word label (e.g. nav junk)


def _pack_units(units: list[str], size: int, overlap_frac: float) -> list[str]:
    """Greedy-pack units into ≤size chunks with sentence-tail overlap.

    Fact-integrity guard: a chunk may not END with a dangling label line —
    it is carried into the next chunk together with its value.
    """
    overlap = int(size * overlap_frac)
    chunks: list[str] = []
    cur = ""
    for unit in units:
        candidate = f"{cur}\n\n{unit}" if cur else unit
        if len(candidate) <= size:
            cur = candidate
            continue
        # Close current chunk (carrying any dangling label into the next).
        head, dangling = cur, _is_label_tail(cur)
        if dangling:
            head = cur[: len(cur) - len(dangling)].rstrip()
            cur = ""
        if head:
            chunks.append(head)
        # Overlap: tail of the emitted chunk, only if it keeps unit intact.
        tail = ""
        if overlap > 0 and head:
            tail_src = head[-overlap:]
            cut = re.search(r"(?<=[.!?])\s", tail_src)
            tail = tail_src[cut.start():] if cut else ""
        prefix = f"{dangling}\n\n" if dangling else ""
        cand = f"{prefix}{tail}\n\n{unit}" if tail else f"{prefix}{unit}"
        cur = cand if len(cand) <= int(size * 1.2) else f"{prefix}{unit}"
    if cur:
        chunks.append(cur)
    return [c.strip() for c in chunks if c.strip()]


def _merge_tiny(pieces: list[str], size: int) -> list[str]:
    """Fold tiny fragments (<40 chars) into the previous chunk of the section."""
    out: list[str] = []
    for p in pieces:
        if out and len(p) < 40 and len(out[-1]) + len(p) + 2 <= int(size * 1.3):
            out[-1] = f"{out[-1]}\n\n{p}"
        else:
            out.append(p)
    return out


def chunk_documents(doc_records: list[dict]) -> list[dict]:
    """Clean text → metadata-tagged chunks with fact integrity (Phase 2).

    Returns [{"text": str, "metadata": {6 fields}}] with metadata exactly
    source_url, scheme, category, section, ingested_at, chunk_index.
    """
    size = int(config.CHUNK_SIZE)
    chunks: list[dict] = []
    for doc in doc_records:
        text = _clean_links(doc["text"])
        seen: set[str] = set()
        chunk_index = 0
        for section, body in _split_sections(text):
            mgr = _MANAGER_ROW_RE.match(section)
            if mgr:
                section = (f"Fund manager: {mgr.group('name')}"
                           f" ({mgr.group('since')} - {mgr.group('until')})")
                # Terminal '.' keeps _glue_labels from merging this label
                # with the following Education/Experience block.
                body = f"{section}.\n\n{body}"
            key = f"{section.lower()}::{re.sub(r'[^a-z0-9]+', '', body.lower())[:200]}"
            if key in seen:          # drop duplicated sections on the same page
                continue
            seen.add(key)
            units: list[str] = []
            for block in _glue_labels(body):
                if block.lstrip().startswith("|"):
                    units.extend(_table_packs(block, size))
                elif len(block) > size:
                    units.extend(_split_sentences(block))
                else:
                    units.append(block)
            for piece in _merge_tiny(_pack_units(units, size, config.CHUNK_OVERLAP), size):
                chunks.append({
                    "text": piece,
                    "metadata": {
                        "source_url": doc["source_url"],
                        "scheme": doc["scheme"],
                        "category": doc["category"],
                        "section": section,
                        "ingested_at": doc["fetched_at"],
                        "chunk_index": chunk_index,
                    },
                })
                chunk_index += 1
    return chunks


# ---------------------------------------------------------------------------
# Stage 3+4 — Embedding & vector store
# ---------------------------------------------------------------------------

def _write_meta(docs: list[dict], num_chunks: int) -> str:
    """Persist data/meta.json; return last-updated date (YYYY-MM-DD)."""
    latest = max((d["fetched_at"] for d in docs), default="")
    last_updated = latest[:10] if latest else ""
    meta = {
        "last_updated": last_updated,
        "latest_fetch": latest,
        "num_sources": len(docs),
        "num_chunks": num_chunks,
        "embedding_model": config.EMBED_MODEL,
    }
    config.META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return last_updated


def build_index(chunks: list[dict], docs: list[dict]) -> int:
    """Embed chunks with all-MiniLM-L6-v2 and persist them in ChromaDB.

    Idempotent: the collection is dropped and recreated on every run.
    Returns the collection size.
    """
    assert config.EMBED_MODEL == "sentence-transformers/all-MiniLM-L6-v2", \
        "Embedding model is fixed by spec"
    from sentence_transformers import SentenceTransformer

    texts = [c["text"] for c in chunks]
    ids = [
        f"{c['metadata']['category'].replace(' ', '_')}_{c['metadata']['chunk_index']}"
        for c in chunks
    ]
    metadatas = [c["metadata"] for c in chunks]

    print(f"Embedding {len(texts)} chunks with {config.EMBED_MODEL} ...")
    model = SentenceTransformer(config.EMBED_MODEL)
    embeddings = model.encode(
        texts, batch_size=32, convert_to_numpy=True,
        normalize_embeddings=True, show_progress_bar=True,
    )

    client = chromadb.PersistentClient(path=config.CHROMA_PATH)
    try:
        client.delete_collection(config.COLLECTION_NAME)
    except Exception:  # noqa: BLE001 — collection simply doesn't exist yet
        pass
    collection = client.create_collection(config.COLLECTION_NAME)
    collection.add(
        ids=ids, documents=texts, metadatas=metadatas,
        embeddings=[e.tolist() for e in embeddings],
    )
    count = collection.count()
    config.LAST_UPDATED = _write_meta(docs, len(chunks))
    print(f"Index ready: {count} chunks in '{config.COLLECTION_NAME}' "
          f"@ {config.CHROMA_PATH}")
    print(f"Last updated from sources: {config.LAST_UPDATED}")
    return count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def export_chunks_txt(out_path=None) -> str:
    """Dump every indexed chunk + its embedding vector to a readable txt file.

    Writes data/chunks.txt: id, metadata, chunk text, and the 384-dim
    embedding vector (rounded to 6 decimals) for each chunk.
    """
    out = Path(out_path) if out_path else config.DATA_DIR / "chunks.txt"
    client = chromadb.PersistentClient(path=config.CHROMA_PATH)
    collection = client.get_collection(config.COLLECTION_NAME)
    data = collection.get(include=["documents", "metadatas", "embeddings"])

    # Sort ids naturally (Large_Cap_0 < Large_Cap_1 < ... < ELSS_0 ...)
    order = sorted(
        range(len(data["ids"])),
        key=lambda i: (data["metadatas"][i]["category"],
                       int(data["ids"][i].rsplit("_", 1)[1])),
    )
    lines = [
        "Mutual Fund FAQ RAG — Chunks & Embeddings Export",
        f"Collection : {config.COLLECTION_NAME} @ {config.CHROMA_PATH}",
        f"Model      : {config.EMBED_MODEL} ({config.EMBED_DIM}-dim)",
        f"Chunks     : {collection.count()}",
        f"Exported   : {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "=" * 78,
        "",
    ]
    for n, i in enumerate(order, 1):
        meta = data["metadatas"][i]
        vec = data["embeddings"][i]
        lines += [
            f"[{n}] id: {data['ids'][i]}",
            f"category    : {meta['category']}",
            f"scheme      : {meta['scheme']}",
            f"section     : {meta['section']}",
            f"source_url  : {meta['source_url']}",
            f"ingested_at : {meta['ingested_at']}  |  chunk_index: {meta['chunk_index']}",
            f"chars       : {len(data['documents'][i])}  |  embedding: {len(vec)}-dim",
            "-" * 78,
            "TEXT:",
            data["documents"][i],
            "",
            f"EMBEDDING ({len(vec)} floats, rounded to 6 dp):",
            _wrap_vector([round(float(x), 6) for x in vec]),
            "",
            "=" * 78,
            "",
        ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out)


def _wrap_vector(vec: list[float], width: int = 6) -> str:
    """Format floats as a wrapped, comma-separated block."""
    rows = [", ".join(f"{x:.6f}" for x in vec[i:i + width])
            for i in range(0, len(vec), width)]
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mutual Fund FAQ ingestion pipeline")
    parser.add_argument("--load-only", action="store_true",
                        help="Only fetch sources + write snapshots/source list")
    parser.add_argument("--refresh", action="store_true",
                        help="Force refetch even if snapshots exist")
    parser.add_argument("--export-chunks", action="store_true",
                        help="Export all chunks + embedding vectors to data/chunks.txt")
    args = parser.parse_args(argv)

    if args.export_chunks:
        try:
            path = export_chunks_txt()
        except Exception as exc:  # noqa: BLE001 — friendly message if index missing
            print(f"Export failed: {exc}\nRun `python3 -m src.ingest` first "
                  f"(index not built yet).", file=sys.stderr)
            return 1
        print(f"Exported: {path}")
        return 0

    docs = load_sources(refresh=args.refresh)
    if not docs:
        print("No sources loaded — aborting.", file=sys.stderr)
        return 1
    if args.load_only:
        print(f"\nLoaded {len(docs)}/{len(config.SCHEMES)} sources (load-only)")
        print(f"Snapshots: {config.RAW_DIR}")
        print(f"Source list: {config.SOURCES_CSV} | {config.SOURCES_MD}")
        return 0

    chunks = chunk_documents(docs)
    print(f"\nStrategy: hybrid heading-aware split + label/value glue + "
          f"packing (size={config.CHUNK_SIZE}, overlap={config.CHUNK_OVERLAP:.0%})")
    print(f"Sources: {len(docs)} | Chunks: {len(chunks)}")
    build_index(chunks, docs)
    print(f"Source list: {config.SOURCES_CSV} | {config.SOURCES_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Grounded LLM generation behind a single swappable interface (Phase 3).

Backend: any OpenAI-compatible chat-completions endpoint configured in `.env`
(verified with Groq):

    LLM_API_KEY   — API key (if unset, the offline fallback is used)
    LLM_MODEL     — model id   (default: llama-3.3-70b-versatile)
    LLM_BASE_URL  — endpoint    (default: https://api.groq.com/openai/v1)

Called with plain `requests` — no vendor SDK required, so any OpenAI-compatible
provider (OpenAI, Groq, OpenRouter, a local Ollama/LM-Studio server) works by
editing `.env` only.

If no key is configured — or the API call fails — a deterministic extractive
fallback answers from the retrieved chunks, so the Q&A path never crashes.
The output guard (src/guardrails.check_output) enforces the ≤3-sentence /
citation / last-updated contract regardless of backend.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

import requests
from dotenv import load_dotenv

from . import config

load_dotenv(config.BASE_DIR / ".env")

# Rate-limit resilience: Groq's free tier 429s on bursts; retry with backoff
# before degrading to the extractive fallback.
LLM_RETRIES = 2
LLM_RETRY_BACKOFF = 2.0  # seconds; sleeps 2s, 4s between attempts

SYSTEM_PROMPT = (
    "You are a factual mutual fund FAQ assistant for HDFC schemes. "
    "Answer ONLY from the provided context. Maximum 3 sentences. "
    "End with the line exactly: Last updated from sources: <date> "
    "(use the date given). Never give investment advice. "
    "Never compute or compare returns. "
    "If the context doesn't contain the answer, say you couldn't find it in "
    "the sources and cite the scheme's source page."
)

_STOPWORDS = {
    "the", "a", "an", "of", "for", "is", "are", "what", "how", "does", "do",
    "i", "to", "in", "on", "and", "with", "my", "this", "that", "fund",
    "hdfc", "?",
}


def _last_updated() -> str:
    if config.LAST_UPDATED:
        return config.LAST_UPDATED
    try:
        meta = json.loads(config.META_PATH.read_text(encoding="utf-8"))
        return str(meta.get("last_updated", ""))
    except Exception:  # noqa: BLE001 — missing/corrupt meta ⇒ no date
        return ""


def _build_user_prompt(context_chunks: list[dict], question: str, date: str) -> str:
    blocks = []
    for c in context_chunks:
        blocks.append(
            f"[Source: {c['source_url']}] ({c['scheme']} | {c['section']})\n"
            f"{c['text']}"
        )
    context = "\n\n---\n\n".join(blocks)
    date_line = f"Last updated from sources: {date}" if date else "(date unknown)"
    return (
        f"<context>\n{context}\n</context>\n\n{date_line}\n\n"
        f"Question: {question}"
    )


def _llm_chat(user_prompt: str) -> str:
    """Single call to the OpenAI-compatible endpoint (Groq by default)."""
    key = os.getenv("LLM_API_KEY", "").strip()
    base = os.getenv("LLM_BASE_URL", "").rstrip("/")
    model = os.getenv("LLM_MODEL", "").strip()
    if not (key and base and model):
        raise RuntimeError("LLM_API_KEY / LLM_BASE_URL / LLM_MODEL not set")
    resp = None
    for attempt in range(LLM_RETRIES + 1):
        resp = requests.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 400,
            },
            timeout=45,
        )
        if resp.status_code < 500 and resp.status_code != 429:
            break                                    # client error → no retry
        if attempt < LLM_RETRIES:
            time.sleep(LLM_RETRY_BACKOFF * (attempt + 1))
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _split_sentences(text: str) -> list[str]:
    """Sentence-ish units: split on [.!?] boundaries AND newlines, so
    label/value lines ("Expense ratio: 0.78%") are candidates too."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if len(p.strip()) >= 2]


def _score(sentence: str, q_tokens: set) -> float:
    """Length-normalized query relevance.

    Substring matching (not token equality) so glued labels like
    "Fund benchmarkNIFTY 100..." still match "benchmark".
    """
    s = sentence.lower()
    hits = sum(1 for t in q_tokens if len(t) >= 3 and t in s)
    if not hits:
        return 0.0
    return hits / (1 + len(s.split()) ** 0.5)


def _extractive(context_chunks: list[dict], question: str) -> str:
    """Offline fallback: best-matching sentences from the retrieved chunks.

    Ranks sentences across ALL chunks (the fact may sit outside the top-1
    chunk), keeps document order, <= MAX_ANSWER_SENTENCES sentences.
    Deterministic and fully grounded — never invents text outside the chunks.
    """
    if not context_chunks:
        return ""
    q_tokens = {
        w for w in re.findall(r"[a-z0-9%₹.]+", question.lower())
        if w not in _STOPWORDS
    }
    # (score, chunk_idx, sentence_idx, sentence) — skip oversized units
    # (e.g. table blocks) so the answer stays <= 3 sentences.
    candidates = [
        (_score(s, q_tokens), ci, si, s)
        for ci, c in enumerate(context_chunks)
        for si, s in enumerate(_split_sentences(c["text"]))
        if len(s) <= 400
    ]
    if not candidates:
        return ""
    # Top sentences by relevance (fall back to the first ones if no overlap),
    # then restore document order.
    top = [t for t in sorted(candidates, key=lambda x: x[0], reverse=True)
           if t[0] > 0][: config.MAX_ANSWER_SENTENCES]
    if not top:
        top = candidates[: config.MAX_ANSWER_SENTENCES]
    top.sort(key=lambda x: (x[1], x[2]))
    return " ".join(t[3] for t in top)


def _postprocess(answer: str, context_chunks: list[dict], date: str) -> str:
    """Ensure exactly one `Source:` line and one `Last updated` line (spec).

    The system prompt tells the model to emit both trailer lines itself, but it
    sometimes decorates them (`【Source: url】`) or echoes the date mid-body.
    Capture the model's citation (spec: keep it if present), strip every echo,
    then append both lines in canonical order: body / Source: / Last updated.
    """
    text = (answer or "").strip()
    cited = re.search(r"https?://[^\s】\]\)）]+", text)
    model_url = cited.group(0).rstrip(".,;") if cited else ""
    # The pipeline owns both trailer lines. Strip every artifact the model may
    # echo from the prompt — decorated citations, bare Source lines, the
    # "(scheme | section)" chunk headers, and bare "(<section name>)" echoes:
    text = re.sub(r"【[^】]*】", " ", text)          # 【Source: url】 spans
    text = re.sub(r"[\[(]\s*Source:\s*https?://[^\])]*[\])]", " ", text)
    text = re.sub(r"\s*Source:\s*https?://\S+", " ", text)   # bare "Source: url"
    text = re.sub(r"\s*Last updated from sources:[^\n]*", " ", text)
    text = re.sub(r"\s*\([^)]*\|[^)]*\)", " ", text)  # (scheme | section) echo
    for c in context_chunks or []:                    # (Key facts) echo
        text = re.sub(rf"\(\s*{re.escape(c['section'])}\s*\)", " ", text)
    text = re.sub(r"[【】]", " ", text)                # stray full-width glyphs
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)      # "Cr ." -> "Cr."
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    url = model_url or (context_chunks[0]["source_url"] if context_chunks else "")
    if url:
        text += f"\nSource: {url}"
    if date:
        text += f"\nLast updated from sources: {date}"
    return text.strip()


def generate(context_chunks, question) -> str:
    """Answer `question` using ONLY `context_chunks`.

    Returns answer text with exactly one citation and the
    "Last updated from sources: <date>" line.
    """
    date = _last_updated()
    user_prompt = _build_user_prompt(context_chunks, question, date)

    answer = ""
    if os.getenv("LLM_API_KEY", "").strip():
        try:
            answer = _llm_chat(user_prompt)
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the demo
            print(f"[warn] LLM call failed ({exc}); using extractive fallback",
                  file=sys.stderr)
    if not answer:
        answer = _extractive(context_chunks, question)

    return _postprocess(answer, context_chunks, date)

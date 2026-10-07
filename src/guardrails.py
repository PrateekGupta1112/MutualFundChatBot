"""Guardrail layer: input gates (PII/advice/performance) and output checks.

Deterministic enforcement of PRD constraints C2, C3, C6 (Phase 4).
Guards are code, not prompts — they run regardless of what the LLM does.

Design notes:
- Order of input gates: PII -> advice -> performance -> allow.
- NO print/log/persist anywhere in this module: blocked inputs leave no trace
  (C2 — nothing from the user's input is ever logged or stored).
- `check_output` guarantees the output contract: <= MAX_ANSWER_SENTENCES
  sentences, exactly one allow-listed citation URL, and the
  "Last updated from sources: <date>" line.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from . import config

# ---------------------------------------------------------------------------
# Static refusal texts — defined once, reused everywhere (UI imports these)
# ---------------------------------------------------------------------------

PII_REPLY = (
    "Please don't share personal information (PAN, Aadhaar, phone, OTP, "
    "account details). This assistant doesn't need or store it."
)

ADVICE_REPLY = (
    "I'm a facts-only assistant and don't provide investment advice or "
    "recommendations. For investor education resources, see: {link}"
)

PERFORMANCE_REPLY = (
    "I don't compute or compare returns. Please refer to the official "
    "factsheet: {link}"
)

FALLBACK_TEMPLATE = (
    "I couldn't find that in the official sources. "
    "You can check the scheme page directly: {source_url}"
)


# ---------------------------------------------------------------------------
# INPUT gate — check_input(text)
# ---------------------------------------------------------------------------

# C2 — PII patterns (any match blocks the input; nothing is logged/persisted).
_PII_PATTERNS = [
    ("pan", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")),
    ("aadhaar", re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")),
    ("mobile", re.compile(r"(?:\+91[\s-]?)?[6-9]\d{9}\b")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("otp", re.compile(r"\botp\b|\bone[\s-]?time[\s-]?password\b", re.I)),
    ("ifsc", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")),
    ("bank_account", re.compile(
        r"\b(?:a/c|acct|account)[^\d]{0,15}\d(?:[\s-]?\d){8,17}\b", re.I)),
]

# C6 — advice-intent gate.
_ADVICE_RE = re.compile(
    r"\bshould\s+i\s+(?:buy|sell|invest|switch|exit|subscribe|redeem|start|stop)\b"
    r"|\bbest\s+(?:mutual\s+)?fund"
    r"|\brecommend"
    r"|worth\s+invest(?:ing|ed)"
    r"|\bbetter\s+fund"
    r"|which\s+fund\s+should"
    r"|which\s+(?:one\s+)?should\s+i\s+(?:buy|invest|pick|choose|choose\s+between)\b"
    r"|which\s+(?:fund|scheme)\s+(?:is\s+)?better\b"
    r"|\bbetter\s+than\b",
    re.I,
)

# C3 — performance-intent gate (facts-only: no returns discussion at all).
_PERFORMANCE_RE = re.compile(
    r"\breturns?\b"
    r"|\bcagr\b"
    r"|\b(?:1|3|5|10)\s*[-\s]?\s*(?:yr|year)s?\b"
    r"|\bcompare\b|\bcomparison\b|\bversus\b|\bvs\.?\b"
    r"|\boutperform\w*"
    r"|\bannual(?:i|iz)ed\b|\bannualised\b|\bannualizing\b|\bannualising\b",
    re.I,
)


def check_input(text: str) -> dict:
    """Screen a user question before retrieval.

    Returns {"allow": bool, "type": ..., "reply": str, "link": str}.
    type: "ok" | "pii" | "advice" | "performance".
    C2: on a PII match nothing from `text` is logged or persisted — this
    function contains no print/log/write calls.
    """
    t = text or ""

    # 1. PII screen (C2)
    for _name, pattern in _PII_PATTERNS:
        if pattern.search(t):
            return {"allow": False, "type": "pii", "reply": PII_REPLY, "link": ""}

    # 2. Advice-intent gate (C6)
    if _ADVICE_RE.search(t):
        return {
            "allow": False,
            "type": "advice",
            "reply": ADVICE_REPLY.format(link=config.EDU_LINK),
            "link": config.EDU_LINK,
        }

    # 3. Performance-intent gate (C3)
    if _PERFORMANCE_RE.search(t):
        return {
            "allow": False,
            "type": "performance",
            "reply": PERFORMANCE_REPLY.format(link=config.FACTSHEET_LINK),
            "link": config.FACTSHEET_LINK,
        }

    # 4. Allow
    return {"allow": True, "type": "ok", "reply": "", "link": ""}


# ---------------------------------------------------------------------------
# OUTPUT gate — check_output(answer)
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_SOURCE_LINE_RE = re.compile(r"^Source:\s*https?://\S+\s*$", re.I)
_UPDATED_LINE_RE = re.compile(r"^Last updated from sources:\s*\S+", re.I)


def _last_updated() -> str:
    """Date for the 'Last updated' line: config, else data/meta.json."""
    if config.LAST_UPDATED:
        return config.LAST_UPDATED
    try:
        meta = json.loads(config.META_PATH.read_text(encoding="utf-8"))
        return str(meta.get("last_updated", ""))
    except Exception:  # noqa: BLE001 — missing/corrupt meta ⇒ no date
        return ""


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _domain_allowed(url: str) -> bool:
    """Allow-list: source domains + the EDU/FACTSHEET link domains (refusals)."""
    domains = set(config.SOURCE_ALLOWLIST)
    for link in (config.EDU_LINK, config.FACTSHEET_LINK):
        if link:
            domains.add(_domain(link))
    d = _domain(url)
    return any(d == x or d.endswith("." + x) for x in domains)


def _count_sentences(body: str) -> int:
    """Simple splitter; conservative (fails safe to the fallback answer)."""
    flat = re.sub(r"\s+", " ", body).strip()
    if not flat:
        return 0
    parts = re.split(r"(?<=[.!?])\s+(?=[\"'“(]*[A-Z])", flat)
    return len([p for p in parts if p.strip()])


def _fallback(fallback_url: str | None) -> str:
    """The insufficient-info fallback answer (<= 3 sentences, one citation)."""
    url = fallback_url if fallback_url and _domain_allowed(fallback_url) else ""
    answer = FALLBACK_TEMPLATE.format(source_url=url).strip()
    lu = _last_updated()
    if lu:
        answer += f"\nLast updated from sources: {lu}"
    return answer


def _result(ok: bool, answer: str, type_: str, reason: str = "") -> dict:
    return {"ok": ok, "answer": answer, "type": type_, "reason": reason}


def check_output(answer: str, fallback_url: str | None = None) -> dict:
    """Validate a generated answer (sentences, citation, last-updated line).

    Returns {"ok": bool, "answer": str, "type": str, "reason": str}.
    - `answer` is ALWAYS safe to render: invalid input is repaired (citation
      swapped to `fallback_url`, last-updated line appended) or replaced with
      the insufficient-info fallback answer.
    - `ok=False` means the original answer violated a content rule
      (over-long, bad/missing citation, empty) — see `type`:
      "ok" | "empty" | "too_long" | "citation" | "no_date".
    - A missing "Last updated from sources:" line is routine normalization
      (appended from data/meta.json) and does not set ok=False.
    """
    text = (answer or "").strip()
    if not text:
        return _result(False, _fallback(fallback_url), "empty", "empty answer")

    # Split off the mandated trailer lines (they are not counted as sentences).
    body_lines, src_lines, upd_lines = [], [], []
    for line in text.splitlines():
        s = line.strip()
        if _SOURCE_LINE_RE.match(s):
            src_lines.append(s)
        elif _UPDATED_LINE_RE.match(s):
            upd_lines.append(s)
        else:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()

    # 1. Sentence count <= MAX_ANSWER_SENTENCES
    n = _count_sentences(body)
    if n > config.MAX_ANSWER_SENTENCES:
        return _result(
            False, _fallback(fallback_url), "too_long",
            f"{n} sentences > {config.MAX_ANSWER_SENTENCES}; replaced with fallback",
        )

    # 2. Exactly one http(s) URL, domain in allow-list (else swap citation).
    urls = _URL_RE.findall(body) + [u for l in src_lines for u in _URL_RE.findall(l)]
    ok, type_ = True, "ok"
    if len(urls) == 1 and _domain_allowed(urls[0]):
        citation = urls[0]
        if not any(citation in l for l in src_lines):
            # Inline citation → normalize into a single "Source: <url>" line.
            body = body.replace(citation, "")
            body = re.sub(r"\s{2,}", " ", body)
            body = re.sub(r"\(\s*\)", "", body)
            body = re.sub(r"\s+([.,;:])", r"\1", body)
            body = body.strip()
            src_lines = [f"Source: {citation}"]
    elif fallback_url and _domain_allowed(fallback_url):
        # Bad / multiple / missing citation → swap to the top chunk's URL.
        # The original answer violated the contract, so ok=False ("caught").
        body = _URL_RE.sub("", body)
        body = re.sub(r"\s{2,}", " ", body)
        body = re.sub(r"\(\s*\)", "", body)          # tidy empty "( )"-style leftovers
        body = re.sub(r"\s+([.,;:])", r"\1", body)
        body = body.strip()
        src_lines = [f"Source: {fallback_url}"]
        ok, type_ = False, "citation"
    else:
        return _result(
            False, _fallback(fallback_url), "citation",
            f"citation missing/not allow-listed ({urls}) and no usable fallback_url",
        )

    # 3. "Last updated from sources: <date>" line (append if missing).
    if not upd_lines:
        lu = _last_updated()
        if not lu:
            return _result(
                False, _fallback(fallback_url), "no_date",
                "no last-updated date available from data/meta.json",
            )
        upd_lines = [f"Last updated from sources: {lu}"]

    final = "\n".join(x for x in [body, *src_lines, *upd_lines] if x).strip()
    return _result(ok, final, type_)

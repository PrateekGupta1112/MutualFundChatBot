# Disclaimer — Mutual Fund FAQ Assistant (deliverable D5)

This file contains the **exact text** rendered in the UI (`app.py`) and used
by the guardrail layer (`src/guardrails.py`). Do not change one without the
other.

## 1. Facts-only note (UI sidebar + UI footer)

> **Facts-only. No investment advice.**
> Answers come from public sources and are for information only, not
> recommendations.

Rendered persistently in the Streamlit sidebar and as the page footer, plus
the header caption:

> Mutual Fund FAQ Assistant — ask factual questions about HDFC schemes.

## 2. Static refusal texts (shown by the guardrail layer)

Defined once in `src/guardrails.py` and reused by the CLI (`src/ask.py`) and
the UI (`app.py`).

**Advice refusal** (C6 — opinionated questions; `{link}` = `config.EDU_LINK`,
the SEBI investor-education page):

> I'm a facts-only assistant and don't provide investment advice or
> recommendations. For investor education resources, see: https://www.sebi.gov.in/investors.html

**Performance redirect** (C3 — returns/comparisons are never computed; `{link}`
= `config.FACTSHEET_LINK`, the official HDFC MF factsheet page):

> I don't compute or compare returns. Please refer to the official factsheet:
> https://www.hdfcfund.com/mutual-funds/factsheets

**PII block** (C2 — nothing from the input is logged or stored):

> Please don't share personal information (PAN, Aadhaar, phone, OTP, account
> details). This assistant doesn't need or store it.

**Insufficient-info fallback** (`{source_url}` = the retrieved scheme page —
also appended automatically by `check_output` when an answer violates the
output contract):

> I couldn't find that in the official sources. You can check the scheme page
> directly: {source_url}

## 3. Answer footer line (every answer, C4)

> Last updated from sources: `YYYY-MM-DD`

The date is the snapshot fetch date from `data/meta.json` — it states how
fresh the corpus is, not today's date.

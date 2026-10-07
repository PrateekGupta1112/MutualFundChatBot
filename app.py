"""Chat UI — welcome line, 3 example questions, facts-only disclaimer (Phase 5).

Run with:  streamlit run app.py

Layout:  chat history renders ABOVE; the input bar (`st.chat_input`) is
pinned to the BOTTOM of the page, so every new question appears above the
input — standard chat order.

Flow:  user input (bottom chat bar or example button)  →  src.ask.ask()  →
render. `ask()` runs the full Phase 4 guard → retrieve → generate →
check_output pipeline, so the UI never talks to the LLM directly and the
output contract (<= 3 sentences, exactly one allow-listed citation,
last-updated line) is already enforced by the time anything is rendered.

Privacy (C2): questions live only in ephemeral `st.session_state` for
display — nothing is written to disk or logged by this file.
"""

from __future__ import annotations

import json
import re

import streamlit as st

from src import config
from src.ask import ask

st.set_page_config(page_title="Mutual Fund FAQ Assistant", layout="centered")

# Example questions (Phase 5 spec) — buttons that send the question directly.
EXAMPLES = [
    "What is the exit load of HDFC Large Cap Fund?",
    "What is the lock-in period for HDFC ELSS Tax Saver?",
    "How do I download a capital-gains statement?",
]

FOOTER = "Facts-only. No investment advice."


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _split_answer(answer: str) -> tuple[str, str, str]:
    """Split the validated answer into (body, citation_url, last_updated)."""
    body, citation, updated = [], "", ""
    for line in answer.splitlines():
        stripped = line.strip()
        if stripped.startswith("Source:"):
            citation = stripped.removeprefix("Source:").strip()
        elif stripped.startswith("Last updated from sources:"):
            updated = stripped.removeprefix("Last updated from sources:").strip()
        else:
            body.append(line)
    return "\n".join(body).strip(), citation, updated


def _auto_link(text: str) -> str:
    """Turn bare URLs in refusal messages into clickable markdown links."""
    return re.sub(r"(https?://[^\s,)]+)", r"[\1](\1)", text)


def _render_answer(result: dict) -> None:
    """Render one assistant turn: refusal (styled) or grounded answer."""
    answer = result["answer"]

    if result["blocked"]:
        # Uniform refusal card for advice / performance / PII (C6/C3/C2).
        st.warning(_auto_link(answer))
        return
    if result["type"] == "error":
        st.error(answer)
        return

    body, citation, updated = _split_answer(answer)
    if body:
        st.markdown(body)                       # answer text (<= 3 sentences)
    if citation:
        st.markdown(f"🔗 [Source: {citation}]({citation})")   # one clickable citation
    if updated:
        st.caption(f"Last updated from sources: {updated}")   # own line


def _answer_question(question: str) -> dict:
    """Run one question through the pipeline; errors become friendly cards."""
    try:
        return ask(question)
    except Exception as exc:                   # noqa: BLE001 — friendly UI error
        return {"question": question, "type": "error", "blocked": False,
                "chunks": [],
                "answer": f"Something went wrong running the pipeline: {exc}"}


# ---------------------------------------------------------------------------
# Sidebar — persistent facts-only note + last-updated date
# ---------------------------------------------------------------------------

st.sidebar.markdown(f"### {FOOTER}")
st.sidebar.markdown(
    "Answers come from public sources and are for information only, "
    "not recommendations."
)
try:
    meta = json.loads(config.META_PATH.read_text(encoding="utf-8"))
    st.sidebar.caption(f"Data as of: {meta.get('last_updated', 'unknown')}")
except Exception:                               # noqa: BLE001 — cosmetic only
    st.sidebar.caption("Data as of: unknown")

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("Mutual Fund FAQ Assistant")
st.caption("Mutual Fund FAQ Assistant — ask factual questions about HDFC schemes.")

# Example buttons (send on click).
cols = st.columns(len(EXAMPLES))
example_clicked = None
for col, question in zip(cols, EXAMPLES):
    if col.button(question, key=f"example::{question}", use_container_width=True):
        example_clicked = question

# ---------------------------------------------------------------------------
# Chat history — rendered ABOVE the input (ephemeral session state only)
# ---------------------------------------------------------------------------

if "messages" not in st.session_state:
    st.session_state["messages"] = []   # [{"question", "result"}] — display only

# Example-button path: answer before rendering, so the turn appears at once.
if example_clicked:
    with st.spinner("Searching the sources…"):
        result = _answer_question(example_clicked)
    st.session_state["messages"].append(
        {"question": example_clicked, "result": result})

for turn in st.session_state["messages"]:
    with st.chat_message("user"):
        st.markdown(turn["question"])
    with st.chat_message("assistant"):
        _render_answer(turn["result"])

if st.session_state["messages"]:
    if st.button("Clear chat"):
        st.session_state["messages"] = []
        st.rerun()

# ---------------------------------------------------------------------------
# Footer — persistent note
# ---------------------------------------------------------------------------

st.divider()
st.caption(f"{FOOTER} Answers come from public sources and are for "
           "information only, not recommendations.")

# ---------------------------------------------------------------------------
# Input — pinned at the BOTTOM of the page; every turn above is history.
# Streamlit floats st.chat_input at the bottom of the viewport; after a
# submit we answer here (spinner) and rerun so the new turn lands in the
# history section above.
# ---------------------------------------------------------------------------

prompt = st.chat_input("Ask a factual question about HDFC schemes…")
question = (prompt or "").strip() or None

if question:
    with st.spinner("Searching the sources…"):
        result = _answer_question(question)
    st.session_state["messages"].append({"question": question, "result": result})
    st.rerun()

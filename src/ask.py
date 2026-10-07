"""Minimal end-to-end Q&A runner (Phase 3).

Pipeline:  check_input (Phase 4 guard) → search → generate → check_output.

Usage:
    # one-shot
    python -m src.ask "What is the exit load of HDFC Large Cap Fund?"
    python -m src.ask What is the lock-in period for HDFC ELSS Tax Saver?

    # interactive — keep typing questions, 'exit' to quit
    python -m src.ask
    python -m src.ask --chunks      # also print the retrieved top-k per question

Prints the validated answer: text + one citation (`Source: <url>`) +
`Last updated from sources: <date>` (blocked inputs print the static reply).
"""

from __future__ import annotations

import argparse

from . import config
from .generate import generate
from .guardrails import check_input, check_output
from .retrieve import detect_scheme_filter, search


def ask(question: str) -> dict:
    """Run one question through the full guard → retrieve → generate pipeline.

    Returns {"question", "answer", "type", "blocked", "chunks"} where
    - blocked=True  ⇒ input gate hit; `answer` is the static reply,
      `type` is "pii" | "advice" | "performance";
    - blocked=False ⇒ `answer` is the validated, source-grounded answer,
      `type` from check_output ("ok" when the answer needed no repair).
    """
    verdict = check_input(question)
    if not verdict["allow"]:
        return {"question": question, "answer": verdict["reply"],
                "type": verdict["type"], "blocked": True, "chunks": []}

    chunks = search(question)
    if not chunks:
        return {
            "question": question,
            "type": "error",
            "blocked": False,
            "chunks": [],
            "answer": "The knowledge base isn't built yet — run "
                      "`python3 -m src.ingest` first, then ask again.",
        }

    # Unanswerable/off-corpus guard: the question names HDFC but none of the
    # 5 scoped schemes (e.g. "HDFC Infrastructure Fund") → deterministic
    # "couldn't find" path, never an unrelated scheme's fact.
    if "hdfc" in question.lower() and detect_scheme_filter(question) is None:
        raw = "I couldn't find that in the official sources."
    else:
        raw = generate(chunks, question)

    out = check_output(raw, fallback_url=chunks[0]["source_url"])
    return {"question": question, "answer": out["answer"], "type": out["type"],
            "blocked": False, "chunks": chunks}


def _show(result: dict, show_chunks: bool) -> None:
    if show_chunks and not result["blocked"] and result["chunks"]:
        filt = detect_scheme_filter(result["question"])
        print(f"[filter: {filt or 'none'} | {len(result['chunks'])} chunks]")
        for i, c in enumerate(result["chunks"]):
            print(f"  [{i}] {c['score']:.3f}  {c['category']:10} "
                  f"| {c['section']}")
    print(result["answer"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ask the Mutual Fund FAQ bot one question "
                    "(no question = interactive mode)."
    )
    parser.add_argument("question", nargs="*", help="the question to ask")
    parser.add_argument("--chunks", action="store_true",
                        help="print the retrieved top-k chunks above each answer")
    args = parser.parse_args(argv)

    # One-shot: a question was given on the command line.
    if args.question:
        _show(ask(" ".join(args.question)), args.chunks)
        return 0

    # Interactive: keep asking until 'exit'/'quit' (or Ctrl-D / Ctrl-C).
    print("Facts-only retrieval tester — type a question, 'exit' to quit.")
    while True:
        try:
            q = input("\nQ> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q.lower() in {"exit", "quit", "q"}:
            break
        _show(ask(q), args.chunks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

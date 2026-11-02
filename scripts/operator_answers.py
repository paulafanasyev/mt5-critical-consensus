"""Whole-answer parsing for the confirm-mode operator prompt, shared by the CLI and the
demo execution runner.

A single letter of a longer word once approved every later position ("в" in Russian,
"tất" in Vietnamese), so only complete phrases map to a verdict and the Latin tokens
always work regardless of the console code page.
"""
from __future__ import annotations

from dataclasses import dataclass

ANSWER_VERDICTS = {
    "y": "y", "yes": "y", "да": "y", "vâng": "y", "có": "y",
    "n": "n", "no": "n", "нет": "n", "không": "n", "khong": "n",
    "a": "a", "all": "a", "все": "a", "tất cả": "a", "tat ca": "a",
    "q": "q", "quit": "q", "stop": "q", "стоп": "q", "dừng": "q", "dung": "q",
}


@dataclass
class ApprovalState:
    """What the operator decided for the rest of the run, kept outside the agent."""

    accept_all: bool = False
    decline_all: bool = False
    stop_requested: bool = False


def verdict(line: str) -> str | None:
    answer = " ".join(line.split()).lower()
    found = ANSWER_VERDICTS.get(answer)
    if found is not None:
        return found
    # A cp1251 console sends bytes that are not valid UTF-8; stdin was decoded with
    # ``surrogateescape``, so re-encode to the original bytes and try the legacy code page
    # instead of silently treating a real "да" as an unanswered prompt.
    try:
        recovered = answer.encode("utf-8", "surrogateescape").decode("cp1251")
    except (UnicodeError, LookupError):
        return None
    return ANSWER_VERDICTS.get(" ".join(recovered.split()).lower())

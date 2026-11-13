"""Prompts for the demo agent. They describe only the fictional dataset and the demo clock."""
from __future__ import annotations

from typing import Any, Dict, List

from pierre.agent.backend import AS_OF

SYSTEM_PROMPT = (
    "You answer questions about a small demo dataset: 30 invented venture-capital firms, their portfolio companies "
    "and a few news items about their deals. The tools are the only source of facts about these firms. "
    f"The demo clock is fixed at {AS_OF}: 'today' means {AS_OF}, 'the last six months' means the six months up to "
    "that date, and 'this year' means 2026. Amounts are in euros. "
    "Look facts up with the tools before you state them, and say plainly when a tool returned nothing or failed. "
    "Change the shortlist or save a list only when the user asks for it. Keep answers short."
)


def repair_message(fabricated: List[Dict[str, Any]]) -> str:
    """The user message that sends unsupported claims back. Only ``fabricated`` claims are listed."""
    lines = []
    for claim in fabricated:
        note = f" (checker note: {claim['evidence']})" if claim.get("evidence") else ""
        lines.append(f"- {claim['claim_text']}{note}")
    return (
        "An automatic check found claims in your last answer that the tool results in this conversation do not "
        "support:\n"
        + "\n".join(lines)
        + "\n\nFor each one, either find evidence for it with the tools or remove it from the answer. The earlier "
        "tool results are already in this conversation, so do not call a tool again with the same arguments. "
        "Reply with the complete corrected answer to the user's last question, written as the answer itself: "
        "do not address this message or mention the check."
    )

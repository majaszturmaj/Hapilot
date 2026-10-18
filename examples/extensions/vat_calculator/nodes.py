"""VAT calculator node — deterministic, no language model."""

import re
from decimal import ROUND_HALF_UP, Decimal

from langchain_core.messages import AIMessage

# An amount ("2 450,50 zł"), optionally labelled: "1845 zł brutto" → the amount is gross.
AMOUNT = re.compile(r"(\d[\d \u00a0]*(?:[.,]\d{1,2})?)\s*(?:zł|pln)?\s*(brutto|netto)?", re.IGNORECASE)
RATE = re.compile(r"(\d{1,2})\s*%")
# Without a label next to the amount: does the user ask for the net amount (so the amount given is gross)?
ASKS_FOR_NET = re.compile(r"\b(?:z|od)\s+(?:kwoty\s+)?brutto\b|\bnetto\s*\?|\bna\s+netto\b", re.IGNORECASE)


def _money(value: Decimal) -> str:
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, frac = f"{q:.2f}".split(".")
    return f"{int(whole):,}".replace(",", " ") + f",{frac} zł"


def calculate(text: str, default_rate: int = 23) -> str:
    rate_match = RATE.search(text)
    rate = Decimal(rate_match.group(1)) if rate_match else Decimal(default_rate)
    found = [m for m in AMOUNT.finditer(RATE.sub(" ", text)) if re.search(r"\d", m.group(1))]
    if not found:
        return "Nie znalazłem kwoty do przeliczenia."
    amount = Decimal(re.sub(r"[ \u00a0]", "", found[0].group(1)).replace(",", "."))
    label = (found[0].group(2) or "").lower()
    if label == "brutto" or (not label and ASKS_FOR_NET.search(text)):
        net = amount / (1 + rate / 100)
        gross = amount
    else:
        net = amount
        gross = amount * (1 + rate / 100)
    return f"Stawka VAT: {rate}%\n- netto: {_money(net)}\n- VAT: {_money(gross - net)}\n- brutto: {_money(gross)}"


def run_vat(state):
    from src.agent_graph import _advance_plan_step, _resolve_effective_user_message
    from src.core.extension_manager import get_active

    plan, idx = state.get("task_plan") or {}, state.get("current_step")
    text = _resolve_effective_user_message(state["messages"])
    if plan and idx is not None and 0 <= idx < len(plan.get("steps", [])):
        text = plan["steps"][idx].get("input") or text
    rate = get_active().get_config("vat_calculator").get("default_rate", 23)
    return {"messages": [AIMessage(content=calculate(text, rate))], **_advance_plan_step(state)}

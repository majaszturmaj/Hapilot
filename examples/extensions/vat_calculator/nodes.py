"""VAT calculator node — deterministic, no language model."""

import re
from decimal import ROUND_HALF_UP, Decimal

from langchain_core.messages import AIMessage

AMOUNT = re.compile(r"(\d[\d  ]*(?:[.,]\d{1,2})?)\s*(?:zł|pln)?", re.IGNORECASE)
RATE = re.compile(r"(\d{1,2})\s*%")
GROSS_TO_NET = re.compile(r"\b(?:z|od)\s+(?:kwoty\s+)?brutto\b|\bile\s+(?:wynosi\s+)?netto\b|\bna\s+netto\b", re.IGNORECASE)


def _money(value: Decimal) -> str:
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, frac = f"{q:.2f}".split(".")
    return f"{int(whole):,}".replace(",", " ") + f",{frac} zł"


def calculate(text: str, default_rate: int = 23) -> str:
    rate_match = RATE.search(text)
    rate = Decimal(rate_match.group(1)) if rate_match else Decimal(default_rate)
    cleaned = RATE.sub(" ", text)
    amounts = [m.group(1) for m in AMOUNT.finditer(cleaned) if re.search(r"\d", m.group(1))]
    if not amounts:
        return "Nie znalazłem kwoty do przeliczenia."
    amount = Decimal(amounts[0].replace(" ", "").replace(" ", "").replace(",", "."))
    if GROSS_TO_NET.search(text):
        net = amount / (1 + rate / 100)
        gross = amount
    else:
        net = amount
        gross = amount * (1 + rate / 100)
    return (f"Stawka VAT: {rate}%\n- netto: {_money(net)}\n- VAT: {_money(gross - net)}\n- brutto: {_money(gross)}")


def run_vat(state):
    from src.agent_graph import _advance_plan_step, _resolve_effective_user_message
    from src.core.extension_manager import get_active

    plan, idx = state.get("task_plan") or {}, state.get("current_step")
    text = _resolve_effective_user_message(state["messages"])
    if plan and idx is not None and 0 <= idx < len(plan.get("steps", [])):
        text = plan["steps"][idx].get("input") or text
    rate = get_active().get_config("vat_calculator").get("default_rate", 23)
    return {"messages": [AIMessage(content=calculate(text, rate))], **_advance_plan_step(state)}

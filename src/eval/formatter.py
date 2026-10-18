"""Producing and checking the JSON answer of a task (docs/EVALUATION_INTERFACE.md, §3)."""

from __future__ import annotations

import json
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from src import tracing
from src.core.tools_common import extract_json_object

MAX_TOOL_CONTEXT = 8000


def format_block(answer_format: dict) -> str:
    """The format description appended to the instruction, as a user would type it."""
    lines = ["", "", "Oczekiwany format odpowiedzi (JSON):"]
    if answer_format.get("description"):
        lines.append(str(answer_format["description"]))
    lines += [f"- {name}: {desc}" for name, desc in answer_format["fields"].items()]
    if answer_format.get("schema"):
        lines.append("Schemat JSON: " + json.dumps(answer_format["schema"], ensure_ascii=False))
    return "\n".join(lines)




def check(obj: Optional[dict], answer_format: dict) -> Optional[str]:
    """None if ``obj`` satisfies the format, else the reason."""
    if not isinstance(obj, dict):
        return "no JSON object found"
    missing = [f for f in answer_format["fields"] if f not in obj]
    if missing:
        return f"missing fields: {', '.join(missing)}"
    if answer_format.get("schema"):
        import jsonschema

        try:
            jsonschema.validate(obj, answer_format["schema"])
        except jsonschema.ValidationError as exc:
            return f"schema: {exc.message}"
    return None


def produce(instruction: str, answer_format: dict, answer_text: str, tool_context: str, mode: str) -> dict:
    """{"answer_json", "answer_json_valid", "answer_json_error", "attempts"}; records an ``answer_format`` trace event."""
    if mode == "parse":
        obj = extract_json_object(answer_text)
        error = check(obj, answer_format)
        tracing.event("answer_format", mode="parse", messages_ref=None, response_ref=None, valid=error is None,
                      error=error, attempts=0)
        return {"answer_json": obj, "answer_json_valid": error is None, "answer_json_error": error, "attempts": 0}

    from src.core.tools_common import invoke_llm_and_normalize
    from src.llm_config import get_llm

    llm, system_prompt = get_llm("answer_formatter")
    prompt = (f"POLECENIE UŻYTKOWNIKA:\n{instruction}\n{format_block(answer_format)}\n\n"
              f"ODPOWIEDŹ ASYSTENTA:\n{answer_text}\n\n"
              f"WYNIKI NARZĘDZI (kontekst):\n{tool_context[-MAX_TOOL_CONTEXT:] or '(brak)'}\n\n"
              "Przepisz odpowiedź asystenta do wymaganego obiektu JSON. Użyj wyłącznie informacji z odpowiedzi i wyników "
              "narzędzi. Zwróć tylko obiekt JSON, bez komentarza.")
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]
    obj, error, attempts = None, None, 0
    for attempts in (1, 2):
        reply = invoke_llm_and_normalize(llm, messages)
        content = getattr(reply, "content", "") or ""
        obj = extract_json_object(content)
        error = check(obj, answer_format) if not content.startswith("[LLM") else content[:300]
        if error is None:
            break
        messages += [reply, HumanMessage(content=f"Ta odpowiedź nie spełnia formatu ({error}). Zwróć poprawiony obiekt JSON.")]
    tracing.event("answer_format", mode="formatter", valid=error is None, error=error, attempts=attempts)
    return {"answer_json": obj if error is None else obj, "answer_json_valid": error is None,
            "answer_json_error": error, "attempts": attempts}

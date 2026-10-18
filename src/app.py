"""Chainlit front end.

Run on the GPU host with ``scripts/hapilotctl.py ui up`` (binds to 127.0.0.1); open it through an
SSH tunnel. Each chat session has its own workspace and its own model profile (chosen in the ⚙️
settings panel from the profiles defined by the operator in config/profiles.yaml).
"""

import os
import sys
from pathlib import Path

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT_DIR)

from src.privacy import apply_offline_env  # noqa: E402

apply_offline_env()

import chainlit as cl  # noqa: E402
from chainlit.input_widget import Select  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402

from src import attachments, run_context  # noqa: E402
from src.agent_graph import get_app  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.graph_runner import (extract_hitl_request, final_text, generated_pdf_name, last_generated_file,  # noqa: E402
                              new_messages, run_graph_streaming)
from src.ui_auth import check_password, users_file  # noqa: E402

_CORE_NODE_LABELS = {
    "task_planner": "📋 Przygotowuję plan",
    "router": "🔀 Kieruję do odpowiedniego modułu",
    "hitl_confirm": "🤝 Czekam na Twoją decyzję",
}
_CORE_NODE_DESCRIPTIONS = {
    "task_planner": "Analizuję Twoje zapytanie i przygotowuję plan działania",
    "router": "Dobieram najlepsze narzędzie do Twojego zadania",
    "hitl_confirm": "Potrzebuję Twojego potwierdzenia, zanim przejdę dalej",
}


def _node_display_config():
    labels, descriptions = dict(_CORE_NODE_LABELS), dict(_CORE_NODE_DESCRIPTIONS)
    try:
        from src.core.extension_manager import extension_manager

        labels.update(extension_manager.get_node_labels())
        descriptions.update(extension_manager.get_node_descriptions())
    except Exception:
        pass
    return labels, descriptions


def _summarize_node_output(node_name: str, output: dict) -> str:
    if node_name == "task_planner":
        plan = output.get("task_plan", {})
        if isinstance(plan, dict) and plan.get("steps"):
            icons = {"rag_search": "🔍", "llm_synthesis": "✍️", "template_pdf": "📄", "python": "🐍", "calendar": "📅",
                     "email": "📧", "coder": "💻", "hitl_confirm": "🛑", "modify_document": "📝"}
            lines = [f"**Plan:** {plan.get('complexity', '?')} | **Kroków:** {len(plan['steps'])}"]
            lines += [f"  {s.get('id', '?')}. {icons.get(s.get('tool', ''), '🔧')} {s.get('action', '?')}" for s in plan["steps"]]
            return "\n".join(lines)
        return "Proste zadanie — działam od razu"
    if node_name == "router":
        return f"**Wybieram:** {output.get('next_step', '?')}"
    if output.get("hitl_request"):
        return "🤝 Czekam na Twoją odpowiedź"
    messages = output.get("messages") or []
    if messages:
        content = getattr(messages[-1], "content", str(messages[-1]))
        return content[:500] + "…" if len(content) > 500 else content
    status = output.get("execution_status", "")
    if status:
        return "✅ Gotowe!" if status == "success" else "❌ Coś poszło nie tak"
    return "Gotowe"


# --------------------------------------------------------------------------- optional login

if users_file():
    @cl.password_auth_callback
    def _auth(username: str, password: str):
        if check_password(username, password):
            return cl.User(identifier=username, metadata={"provider": "credentials"})
        return None


# --------------------------------------------------------------------------- session

def _profile_label(name: str) -> str:
    profile = get_settings().profile(name)
    return f"{name} — {profile.description}" if profile.description else name


async def _send_settings() -> None:
    settings = get_settings()
    names = list(settings.profiles)
    current = cl.user_session.get("profile") or settings.default_profile
    await cl.ChatSettings([
        Select(id="profile", label="🧠 Profil modeli dla tej rozmowy", values=names, initial_value=current,
               description=" · ".join(_profile_label(n) for n in names)),
    ]).send()


@cl.on_chat_start
async def start():
    settings = get_settings()
    cl.user_session.set("messages", [])
    cl.user_session.set("last_generated_file", "")
    cl.user_session.set("profile", settings.default_profile)
    await _send_settings()
    profile = settings.profile(settings.default_profile)
    await cl.Message(
        content=(
            "👋 **Cześć! Jestem Hapilot — Twój lokalny asystent AI.**\n\n"
            "Oto co razem możemy zrobić:\n"
            "- 📄 **Tworzyć dokumenty** — listy, raporty i protokoły gotowe do druku\n"
            "- 🧠 **Znajdować odpowiedzi** w dokumentach firmy\n"
            "- 🐍 **Uruchamiać kod Python** w izolowanym środowisku — obliczenia, wykresy, analiza danych\n"
            "- 💬 **Rozmawiać** — po prostu zapytaj\n\n"
            f"Profil modeli: **{profile.name}** ({profile.description}). Zmienisz go w ⚙️."
        ),
        author="Hapilot",
    ).send()


@cl.on_settings_update
async def settings_update(values: dict):
    name = values.get("profile")
    if name and name in get_settings().profiles and name != cl.user_session.get("profile"):
        cl.user_session.set("profile", name)
        await cl.Message(content=f"⚙️ Profil modeli dla tej rozmowy: **{name}**", author="Hapilot").send()


def _run_config() -> dict:
    return run_context.make_config(cl.context.session.id, profile=cl.user_session.get("profile"),
                                   extra_collections=cl.user_session.get("extra_collections") or None)


def _inputs(history: list, hitl_response=None) -> dict:
    return {"messages": history, "next_step": "", "execution_status": "", "retry_count": 0, "task_plan": {},
            "task_id": "", "is_complex_task": False, "current_step": None, "generated_code": None,
            "last_generated_file": cl.user_session.get("last_generated_file") or "",
            "hitl_request": None, "hitl_response": hitl_response, "halt": False}


# --------------------------------------------------------------------------- running the graph

async def _run(inputs: dict, user_content: str):
    labels, descriptions = _node_display_config()
    async with cl.Step(name="🔄 Pracuję nad Twoim zapytaniem", type="run") as main_step:
        main_step.input = user_content

        async def on_event(node_name: str, output: dict):
            async with cl.Step(name=labels.get(node_name, node_name), type="tool") as sub:
                sub.input = descriptions.get(node_name, "")
                sub.output = _summarize_node_output(node_name, output)

        run = await run_graph_streaming(get_app(), inputs, _run_config(), on_event=on_event)
        path = " → ".join(labels.get(name, name) for name, _ in run.events)
        main_step.output = f"**Przeszedłem przez:** {path}" + (f"\n\n❌ {run.error}" if run.error else "")
    return run


def _remember(history: list, run) -> None:
    cl.user_session.set("messages", history + new_messages(run.events))
    lgf = last_generated_file(run.events)
    if lgf:
        cl.user_session.set("last_generated_file", lgf)


async def _finish(history: list, run) -> None:
    _remember(history, run)
    hitl = None if run.error else extract_hitl_request(run.events)
    if hitl:
        await _handle_hitl_request(hitl)
        return
    text = final_text(run)
    elements = []
    pdf_name = generated_pdf_name(text)
    if pdf_name:
        with run_context.use_context(session_id=cl.context.session.id):
            try:
                pdf_path = run_context.workspace_file(pdf_name)
            except ValueError:
                pdf_path = None
        if pdf_path is not None and pdf_path.is_file():
            elements.append(cl.File(name=pdf_name, path=str(pdf_path), display="inline"))
            text += "\n\n✅ **Twój dokument PDF jest gotowy! Kliknij poniżej, aby go pobrać.**"
        else:
            text += "\n\n⚠️ *Nie znalazłem wygenerowanego pliku w tej sesji. Spróbujmy jeszcze raz.*"
    await cl.Message(content=text, elements=elements, author="Hapilot").send()


def _attach(message: cl.Message) -> str:
    """Uploaded files → workspace + text for the message (same code as evaluation tasks)."""
    files = [e for e in (message.elements or []) if getattr(e, "path", None)]
    if not files:
        return ""
    with run_context.use_context(session_id=cl.context.session.id):
        prepared = attachments.prepare([Path(e.path) for e in files], display_names=[e.name for e in files],
                                       index_path=get_settings().data_dir / "attachments_index")
    if prepared.collection:
        collections = cl.user_session.get("extra_collections") or []
        if list(prepared.collection) not in collections:
            collections.append(list(prepared.collection))
        cl.user_session.set("extra_collections", collections)
    return prepared.message_block


@cl.on_message
async def main(message: cl.Message):
    history = cl.user_session.get("messages")
    block = await cl.make_async(_attach)(message)
    history.append(HumanMessage(content=message.content + block))
    run = await _run(_inputs(history), message.content)
    await _finish(history, run)


async def _continue_after_hitl(hitl_response: dict):
    history = cl.user_session.get("messages")
    run = await _run(_inputs(history, hitl_response), "[kontynuacja po decyzji użytkownika]")
    await _finish(history, run)


# --------------------------------------------------------------------------- human in the loop

async def _handle_hitl_request(hitl_request: dict):
    kind = hitl_request.get("type", "confirm")
    continuation = hitl_request.get("continuation", {})
    cl.user_session.set("hitl_continuation", continuation)
    if kind == "form":
        await _handle_hitl_form(hitl_request, continuation)
    elif kind == "action":
        cl.user_session.set("hitl_action_defs", hitl_request.get("actions", []))
        actions = [cl.Action(name="hitl_select", label=a.get("label", a.get("name", f"Opcja {i + 1}")),
                             payload={"index": i, **(a.get("payload", {}))}) for i, a in enumerate(hitl_request.get("actions", []))]
        actions.append(cl.Action(name="hitl_cancel", label="↩️ Anuluj", payload={"action": "cancel"}))
        await cl.Message(content=hitl_request.get("content", "Którą opcję wybierasz?"), author="Hapilot", actions=actions).send()
    else:
        actions = [cl.Action(name="hitl_yes", label="✅ Tak, działaj!", payload={"action": "yes"}),
                   cl.Action(name="hitl_no", label="↩️ Nie, dziękuję", payload={"action": "no"})]
        await cl.Message(content=hitl_request.get("content", "Kontynuować?"), author="Hapilot", actions=actions).send()


async def _handle_hitl_form(hitl_request: dict, continuation: dict):
    fields = hitl_request.get("fields", [])
    element = cl.CustomElement(name="hitl_form", display="inline", props={
        "title": hitl_request.get("content") or "Parametry dokumentu", "fields": fields,
        "template_id": continuation.get("template_id", "")})
    res = await cl.AskElementMessage(
        content="📋 Przygotowałem wstępne dane dokumentu. Sprawdź je i popraw co trzeba, a potem kliknij **Generuj PDF**:",
        element=element, author="Hapilot", timeout=600).send()
    if res is None or not res.get("submitted"):
        await cl.Message(content="Rozumiem — anulowałem operację. Daj znać, jeśli zmienisz zdanie! 😊", author="Hapilot").send()
        return
    response = {**continuation, "continuation_type": continuation.get("type", "generate_pdf"),
                "parameters": dict(continuation.get("parameters", {}))}
    for f in fields:
        fid = f.get("id", "")
        value = res.get(fid) if fid in res else f.get("value", "")
        if fid == "_template_id":
            response["template_id"] = value
        elif fid:
            response["parameters"][fid] = value
    await cl.Message(content="✅ Dane zatwierdzone — tworzę dokument, to potrwa chwilę...", author="Hapilot").send()
    await _continue_after_hitl(response)


@cl.action_callback("hitl_cancel")
async def on_hitl_cancel(action: cl.Action):
    await action.remove()
    await cl.Message(content="Rozumiem — anulowałem. Jestem tu, gdybyś potrzebował czegoś innego! 😊", author="Hapilot").send()


@cl.action_callback("hitl_yes")
async def on_hitl_yes(action: cl.Action):
    continuation = cl.user_session.get("hitl_continuation") or {}
    await action.remove()
    await _continue_after_hitl({**continuation, "continuation_type": continuation.get("type", ""), "confirmed": True})


@cl.action_callback("hitl_no")
async def on_hitl_no(action: cl.Action):
    continuation = cl.user_session.get("hitl_continuation") or {}
    await action.remove()
    if continuation.get("type") == "resume_node":  # let the node record the refusal and finish its plan
        await _continue_after_hitl({**continuation, "continuation_type": "resume_node", "confirmed": False})
        return
    await cl.Message(content="Rozumiem, anulowałem. Daj znać, jeśli będę mógł pomóc w czymś innym!", author="Hapilot").send()


@cl.action_callback("hitl_select")
async def on_hitl_select(action: cl.Action):
    defs = cl.user_session.get("hitl_action_defs") or []
    continuation = cl.user_session.get("hitl_continuation") or {}
    await action.remove()
    payload = dict(action.payload) if isinstance(action.payload, dict) else {}
    idx = payload.pop("index", 0)
    selected = defs[idx] if idx < len(defs) else {}
    await _continue_after_hitl({**continuation, "continuation_type": continuation.get("type", ""),
                                "selected_action": selected.get("name", f"action_{idx}"),
                                "selected_label": action.label, "payload": payload})

"""Chainlit front end.

Run on the GPU host with ``scripts/hapilotctl.py ui up`` (binds to 127.0.0.1); open it through an
SSH tunnel. Each chat has its own workspace and its own model profile (chosen in the ⚙️
settings panel from the profiles defined by the operator in config/profiles.yaml).

With login on (``HAPILOT_UI_USERS_FILE``) chats are kept per user (src/ui_history.py): past chats
are listed in the left sidebar and can be continued — the agent gets the earlier messages back, and
the chat's workspace and side panel are the same as before. Workspaces are keyed by the chat
(thread) id, so they survive a reconnect. Every logged-in user also gets the agent panel at
``/hapilot/admin`` (src/ui_admin.py) and runs on their own agent: built-in nodes, global custom nodes
and their own private ones (src/admin.py).
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT_DIR)

from src.privacy import apply_offline_env  # noqa: E402

apply_offline_env()

import chainlit as cl  # noqa: E402
from chainlit.input_widget import Select  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict, messages_to_dict  # noqa: E402

from src import attachments, run_context, ui_panel  # noqa: E402
from src.agent_graph import get_app  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.graph_runner import (display_text, extract_hitl_request, final_text, generated_file_names,  # noqa: E402
                              last_generated_file, new_messages, run_graph_streaming)
from src.graph_runner import form_title  # noqa: E402
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


def _node_display_config(manager=None):
    labels, descriptions = dict(_CORE_NODE_LABELS), dict(_CORE_NODE_DESCRIPTIONS)
    try:
        from src.core.extension_manager import extension_manager

        manager = manager or extension_manager
        labels.update(manager.get_node_labels())
        descriptions.update(manager.get_node_descriptions())
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


ui_panel.install_route()  # PDF pages and downloads for the right-hand panel

# --------------------------------------------------------------------------- optional login and chat history

_STORE = None

if users_file():
    from src.chat_store import ChatStore
    from src.ui_history import HistoryDataLayer, db_path

    _STORE = ChatStore(db_path())

    # agent panel (/hapilot/admin): custom nodes and prompt overrides apply to the UI only (src/admin.py)
    from src import admin as _admin
    from src.ui_admin import install_routes as _install_admin_routes

    _admin.enable_for_ui()
    _install_admin_routes()

    @cl.password_auth_callback
    def _auth(username: str, password: str):
        if check_password(username, password):
            return cl.User(identifier=username, metadata={"provider": "credentials"})
        return None

    @cl.data_layer
    def _data_layer():
        return HistoryDataLayer(_STORE)


class _ChatState:
    """The agent's state of a chat. Not JSON-serialisable on purpose: Chainlit copies the user session into
    the thread metadata, and this state is stored separately (src/chat_store.py, ``agent_state``)."""

    def __init__(self) -> None:
        self.messages: list = []
        self.panel_turns: list = []
        self.edit_buttons: list = []   # [{"message_id", "file"}]: "edit and generate again" under PDF answers


def _chat() -> str:
    """The chat (thread) id: stays the same when a chat is resumed, unlike the websocket session id."""
    return cl.context.session.thread_id


def _state() -> _ChatState:
    state = cl.user_session.get("chat_state")
    if not isinstance(state, _ChatState):
        state = _ChatState()
        cl.user_session.set("chat_state", state)
    return state


def _save_chat() -> None:
    if _STORE is None:
        return
    state, chat = _state(), _chat()
    _STORE.save_state(chat, "messages", messages_to_dict(state.messages))
    _STORE.save_state(chat, "panel_turns", state.panel_turns)
    _STORE.save_state(chat, "panel_tokens", ui_panel.export_tokens(chat))
    _STORE.save_state(chat, "edit_buttons", state.edit_buttons)
    _STORE.save_state(chat, "last_generated_file", cl.user_session.get("last_generated_file") or "")


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
    _state()
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
            "- 📎 **Pracować na Twoich plikach** (PDF, Word, e-mail, Excel, tekst) — streścić, poprawić "
            "albo dodać do bazy wiedzy\n"
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


@cl.on_chat_resume
async def resume(thread: dict):
    """A past chat is opened again: restore the agent's messages, the side panel and the profile."""
    settings = get_settings()
    saved = await cl.make_async(_STORE.load_state)(_chat()) if _STORE else {}
    state = _ChatState()
    state.messages = messages_from_dict(saved.get("messages") or [])
    state.panel_turns = saved.get("panel_turns") or []
    state.edit_buttons = saved.get("edit_buttons") or []
    cl.user_session.set("chat_state", state)
    cl.user_session.set("last_generated_file", saved.get("last_generated_file") or "")
    ui_panel.import_tokens(_chat(), saved.get("panel_tokens") or {})
    if cl.user_session.get("profile") not in settings.profiles:
        cl.user_session.set("profile", settings.default_profile)
    await _send_settings()
    for turn in state.panel_turns:  # buttons are not stored with the messages; put them back
        if turn.get("message_id"):
            await _panel_action(turn["id"]).send(for_id=turn["message_id"])
    for button in state.edit_buttons:
        await _edit_action(button["file"]).send(for_id=button["message_id"])


@cl.on_chat_end
async def end():
    ui_panel.forget_session(_chat())


def _agent():
    """(compiled graph, extension manager) for this chat: with login, the user's own agent (built-in nodes plus
    the custom nodes they may use, src/admin.py); without login, the shared graph."""
    user = getattr(cl.context.session, "user", None)
    if _STORE is not None and user is not None:
        manager, app = _admin.agent_for(user.identifier)
        return app, manager
    return get_app(), None


def _user() -> Optional[str]:
    """The logged-in user (None without login): picks their custom templates (src/doc_templates.py)."""
    user = getattr(cl.context.session, "user", None)
    return user.identifier if _STORE is not None and user is not None else None


def _run_config(manager=None) -> dict:
    pending = cl.user_session.get("panel_pending")
    return run_context.make_config(_chat(), profile=cl.user_session.get("profile"),
                                   extra_collections=cl.user_session.get("extra_collections") or None,
                                   observer=pending.observer if pending else None, extension_manager=manager,
                                   user=_user())


def _inputs(history: list, hitl_response=None) -> dict:
    return {"messages": history, "next_step": "", "execution_status": "", "retry_count": 0, "task_plan": {},
            "task_id": "", "is_complex_task": False, "current_step": None, "generated_code": None,
            "last_generated_file": cl.user_session.get("last_generated_file") or "",
            "hitl_request": None, "hitl_response": hitl_response, "halt": False}


# --------------------------------------------------------------------------- running the graph

async def _run(inputs: dict, user_content: str):
    app, manager = await cl.make_async(_agent)()
    labels, descriptions = _node_display_config(manager)
    async with cl.Step(name="kroki zadania", type="run") as main_step:  # shown as "Wykonuję / Wykonano kroki zadania"
        main_step.input = user_content

        async def on_event(node_name: str, output: dict):
            async with cl.Step(name=labels.get(node_name, node_name), type="tool") as sub:
                sub.input = descriptions.get(node_name, "")
                sub.output = _summarize_node_output(node_name, output)

        run = await run_graph_streaming(app, inputs, _run_config(manager), on_event=on_event)
        path = " → ".join(labels.get(name, name) for name, _ in run.events)
        main_step.output = f"**Przeszedłem przez:** {path}" + (f"\n\n❌ {run.error}" if run.error else "")
    return run


def _remember(history: list, run) -> None:
    _state().messages = history + new_messages(run.events)
    lgf = last_generated_file(run.events)
    if lgf:
        cl.user_session.set("last_generated_file", lgf)
    _save_chat()


async def _finish(history: list, run) -> None:
    _remember(history, run)
    hitl = None if run.error else extract_hitl_request(run.events)
    if hitl:
        await _handle_hitl_request(hitl)
        return
    await _send_answer(final_text(run))


async def _send_answer(text: str) -> None:
    """Send an answer: downloads for the files it announces, the side panel, and the buttons."""
    elements, missing = [], []
    for name in generated_file_names(text):
        with run_context.use_context(session_id=_chat()):
            try:
                path = run_context.workspace_file(name)
            except ValueError:
                path = None
        if path is not None and path.is_file():
            elements.append(cl.File(name=name, path=str(path), display="inline"))
        else:
            missing.append(name)
    panel = await _panel_turn(text)
    text = display_text(text)
    if elements:
        text += "\n\n⬇️ **Plik do pobrania poniżej.**" if len(elements) == 1 else "\n\n⬇️ **Pliki do pobrania poniżej.**"
    if missing:
        text += f"\n\n⚠️ *Nie znalazłem pliku {', '.join(missing)} w tej rozmowie. Spróbujmy jeszcze raz.*"
    editable = [e.name for e in elements if await cl.make_async(_document_params)(e.name)]
    actions = ([_panel_action(panel["id"])] if panel else []) + [_edit_action(name) for name in editable]
    message = cl.Message(content=text, elements=elements, actions=actions, author="Hapilot")
    await message.send()
    if panel:
        panel["message_id"] = message.id  # a resumed chat puts the buttons back under this message
    _state().edit_buttons += [{"message_id": message.id, "file": name} for name in editable]
    if panel or editable:
        await cl.make_async(_save_chat)()
    if panel:
        await ui_panel.show(_state().panel_turns, panel["id"])


def _panel_action(turn_id: int) -> cl.Action:
    return cl.Action(name="show_panel", label="📎 Źródła i pliki", payload={"turn": turn_id})


async def _panel_turn(answer: str):
    """Close the request's panel turn; returns it when it has sources or changed files."""
    pending = cl.user_session.get("panel_pending")
    cl.user_session.set("panel_pending", None)
    if pending is None:
        return None
    try:
        turn = await cl.make_async(ui_panel.build_turn)(_chat(), pending, answer)
    except Exception as exc:  # the answer matters more than the panel
        print(f"⚠️ [PANEL] {type(exc).__name__}: {exc}")
        return None
    if turn is None:
        return None
    state = _state()
    state.panel_turns = ui_panel.add_turn(state.panel_turns, turn)
    await cl.make_async(_save_chat)()
    return turn


@cl.action_callback("show_panel")
async def on_show_panel(action: cl.Action):
    turns = _state().panel_turns
    wanted = (action.payload or {}).get("turn")
    if any(t["id"] == wanted for t in turns):
        await ui_panel.show(turns, wanted)
    else:
        await cl.Message(content="Ta odpowiedź jest już za stara — panel pamięta ostatnie "
                                 f"{ui_panel.MAX_TURNS} odpowiedzi ze źródłami lub plikami.", author="Hapilot").send()


def _attach(message: cl.Message) -> str:
    """Uploaded files → workspace + text for the message (same code as evaluation tasks)."""
    files = [e for e in (message.elements or []) if getattr(e, "path", None)]
    if not files:
        return ""
    with run_context.use_context(session_id=_chat()):
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
    history = _state().messages
    block = await cl.make_async(_attach)(message)
    cl.user_session.set("panel_pending", await cl.make_async(ui_panel.start_turn)(_chat(), message.content))
    history.append(HumanMessage(content=message.content + block))
    run = await _run(_inputs(history), message.content)
    await _finish(history, run)


async def _continue_after_hitl(hitl_response: dict):
    history = _state().messages
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
        cl.user_session.set("hitl_actions", actions)
        await cl.Message(content=hitl_request.get("content", "Którą opcję wybierasz?"), author="Hapilot", actions=actions).send()
    else:
        actions = [cl.Action(name="hitl_yes", label="✅ Tak, działaj!", payload={"action": "yes"}),
                   cl.Action(name="hitl_no", label="↩️ Nie, dziękuję", payload={"action": "no"})]
        cl.user_session.set("hitl_actions", actions)
        await cl.Message(content=hitl_request.get("content", "Kontynuować?"), author="Hapilot", actions=actions).send()


async def _ask_document_form(fields: list, title: str, template_id: str, intro: str,
                             parameters: Optional[dict] = None) -> Optional[dict]:
    """Show the document form; returns {"template_id", "parameters"} when submitted, else None (and says so)."""
    element = cl.CustomElement(name="hitl_form", display="inline",
                               props={"title": title, "fields": fields, "template_id": template_id})
    ask = cl.AskElementMessage(content=intro, element=element, author="Hapilot", timeout=600)
    res = await ask.send()
    # Chainlit replaces the question with an English status ("Thanks for submitting"); say it in Polish
    ask.content = ("📋 Dane dokumentu zatwierdzone." if res and res.get("submitted") else
                   "📋 Formularz zamknięty bez zatwierdzenia." if res else "📋 Czas na wypełnienie formularza minął.")
    await ask.update()
    if res is None or not res.get("submitted"):
        await cl.Message(content="Rozumiem — anulowałem operację. Daj znać, jeśli zmienisz zdanie! 😊", author="Hapilot").send()
        return None
    answer = {"template_id": template_id, "parameters": dict(parameters or {})}
    for f in fields:
        fid = f.get("id", "")
        value = res.get(fid) if fid in res else f.get("value", "")
        if fid == "_template_id":
            answer["template_id"] = value
        elif fid:
            answer["parameters"][fid] = value
    return answer


async def _handle_hitl_form(hitl_request: dict, continuation: dict):
    answer = await _ask_document_form(
        hitl_request.get("fields", []), form_title(hitl_request.get("content") or ""), continuation.get("template_id", ""),
        "📋 Przygotowałem wstępne dane dokumentu. Sprawdź je i popraw co trzeba, a potem kliknij **Generuj PDF**:",
        continuation.get("parameters"))
    if answer is None:
        return
    response = {**continuation, "continuation_type": continuation.get("type", "generate_pdf"), **answer}
    await cl.Message(content="✅ Dane zatwierdzone — tworzę dokument, to potrwa chwilę...", author="Hapilot").send()
    await _continue_after_hitl(response)


# --------------------------------------------------------------------------- edit a generated document again

EDIT_LABEL = "✏️ Edytuj dane i wygeneruj ponownie"


def _edit_action(pdf_name: str) -> cl.Action:
    return cl.Action(name="edit_document", label=EDIT_LABEL, payload={"file": pdf_name})


def _document_params(pdf_name: str) -> Optional[dict]:
    """The template and parameters a PDF of this chat was generated from (saved next to it), if any."""
    if not str(pdf_name).lower().endswith(".pdf"):
        return None
    with run_context.use_context(session_id=_chat()):
        try:
            path = run_context.workspace_file(Path(pdf_name).stem + ".params.json")
        except ValueError:
            return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None
    return saved if isinstance(saved, dict) and saved.get("template_id") else None


def _compile_document(template_id: str, parameters: dict, signature: Optional[str] = None) -> str:
    from src.agent_tools import tool_generate_pdf_from_template

    with run_context.use_context(session_id=_chat(), profile=cl.user_session.get("profile"), user=_user()):
        return tool_generate_pdf_from_template(template_id, parameters, signature=signature)


@cl.action_callback("edit_document")
async def on_edit_document(action: cl.Action):
    """Open the form again with the values of an existing PDF; a submit compiles a new version (no model call)."""
    from src.agent_tools import _load_templates_metadata, build_document_hitl_form

    name = (action.payload or {}).get("file", "")
    saved = await cl.make_async(_document_params)(name)
    if saved is None:
        await cl.Message(content=f"⚠️ Nie mogę już edytować **{name}** — brak zapisanych danych tego dokumentu.",
                         author="Hapilot").send()
        return
    with run_context.use_context(user=_user()):
        form = build_document_hitl_form(saved["template_id"], saved.get("parameters") or {}, _load_templates_metadata(),
                                        signature=saved.get("signature"))
    answer = await _ask_document_form(
        form["fields"], f"Edycja dokumentu {name}", saved["template_id"],
        f"✏️ Popraw dane dokumentu **{name}** i kliknij **Generuj PDF**. Powstanie nowa wersja pliku; "
        "poprzednia zostaje w rozmowie.", saved.get("parameters"))
    if answer is None:
        return
    cl.user_session.set("panel_pending", await cl.make_async(ui_panel.start_turn)(_chat(), f"Nowa wersja: {name}"))
    async with cl.Step(name="📄 Generuję nową wersję dokumentu", type="tool") as step:
        step.input = f"{name} → szablon {answer['template_id']}"
        from src.signatures import mode_from_form

        result = await cl.make_async(_compile_document)(answer["template_id"], answer["parameters"],
                                                        mode_from_form(answer["parameters"].get("_signature")))
        step.output = display_text(result)
    state = _state()
    state.messages = state.messages + [
        HumanMessage(content=f"Poprawiłem dane dokumentu {name} w formularzu i poprosiłem o nową wersję."),
        AIMessage(content=result)]
    new_pdf = generated_file_names(result)
    if new_pdf:
        cl.user_session.set("last_generated_file", Path(new_pdf[0]).with_suffix(".tex").name)
    await cl.make_async(_save_chat)()
    await _send_answer(result)


async def _clear_actions(clicked: cl.Action) -> None:
    """Remove the clicked button and the other buttons of the same question."""
    await clicked.remove()
    for other in cl.user_session.get("hitl_actions") or []:
        if other.id != clicked.id:
            try:
                await other.remove()
            except Exception as exc:  # a stale button is harmless; say so in the log
                print(f"⚠️ [UI] could not remove button {other.label!r}: {exc}")
    cl.user_session.set("hitl_actions", [])


@cl.action_callback("hitl_cancel")
async def on_hitl_cancel(action: cl.Action):
    await _clear_actions(action)
    await cl.Message(content="Rozumiem — anulowałem. Jestem tu, gdybyś potrzebował czegoś innego! 😊", author="Hapilot").send()


@cl.action_callback("hitl_yes")
async def on_hitl_yes(action: cl.Action):
    continuation = cl.user_session.get("hitl_continuation") or {}
    await _clear_actions(action)
    await _continue_after_hitl({**continuation, "continuation_type": continuation.get("type", ""), "confirmed": True})


@cl.action_callback("hitl_no")
async def on_hitl_no(action: cl.Action):
    continuation = cl.user_session.get("hitl_continuation") or {}
    await _clear_actions(action)
    if continuation.get("type") == "resume_node":  # let the node record the refusal and finish its plan
        await _continue_after_hitl({**continuation, "continuation_type": "resume_node", "confirmed": False})
        return
    await cl.Message(content="Rozumiem, anulowałem. Daj znać, jeśli będę mógł pomóc w czymś innym!", author="Hapilot").send()


@cl.action_callback("hitl_select")
async def on_hitl_select(action: cl.Action):
    defs = cl.user_session.get("hitl_action_defs") or []
    continuation = cl.user_session.get("hitl_continuation") or {}
    await _clear_actions(action)
    payload = dict(action.payload) if isinstance(action.payload, dict) else {}
    idx = payload.pop("index", 0)
    selected = defs[idx] if idx < len(defs) else {}
    await _continue_after_hitl({**continuation, "continuation_type": continuation.get("type", ""),
                                "selected_action": selected.get("name", f"action_{idx}"),
                                "selected_label": action.label, "payload": payload})

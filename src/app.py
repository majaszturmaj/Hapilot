import sys
import os
import glob
import re

# --- KONFIGURACJA ŚCIEŻEK (PRIORYTETOWA) ---
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(current_dir, ".."))
sys.path.insert(0, root_dir)

import chainlit as cl
from chainlit.input_widget import Select, TextInput
from langchain_core.messages import HumanMessage

from src.agent_graph import app
from src.agent_tools import SANDBOX_WORKSPACE
from src.model_registry import registry

SANDBOX_PATH = SANDBOX_WORKSPACE

# --- Konfiguracja wyświetlania węzłów grafu (dynamic from extensions) ---
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

def _build_node_display_config():
    """Merge core node labels with extension-provided labels."""
    labels = dict(_CORE_NODE_LABELS)
    descriptions = dict(_CORE_NODE_DESCRIPTIONS)
    try:
        from src.core.extension_manager import extension_manager
        labels.update(extension_manager.get_node_labels())
        descriptions.update(extension_manager.get_node_descriptions())
    except Exception:
        pass
    return labels, descriptions

NODE_LABELS, NODE_DESCRIPTIONS = _build_node_display_config()



def _summarize_node_output(node_name, node_output):
    """Generuje czytelne podsumowanie wyniku węzła grafu."""

    if node_name == "task_planner":
        plan = node_output.get("task_plan", {})
        if isinstance(plan, dict) and plan.get("steps"):
            steps = plan["steps"]
            complexity = plan.get("complexity", "?")
            tool_icons = {
                "rag_search": "🔍", "llm_synthesis": "✍️", "template_pdf": "📄",
                "python": "🐍", "calendar": "📅", "email": "📧", "coder": "💻",
                "hitl_confirm": "🛑", "modify_document": "📝",
            }
            lines = [f"**Plan:** {complexity} | **Kroków:** {len(steps)}"]
            for s in steps:
                icon = tool_icons.get(s.get("tool", ""), "🔧")
                lines.append(f"  {s.get('id', '?')}. {icon} {s.get('action', '?')}")
            return "\n".join(lines)
        is_complex = node_output.get("is_complex_task", False)
        return "Rozbieram to na kilka kroków — zaraz się tym zajmę" if is_complex else "Proste zadanie — działam od razu"

    elif node_name == "router":
        next_step = node_output.get("next_step", "?")
        target_labels = {
            "knowledge": "🧠 Szukam w bazie wiedzy",
            "document_generator": "📄 Tworzę dokument PDF",
            "coder": "💻 Piszę kod Python",
            "chat": "💬 Rozmawiam",
            "writer": "✍️ Redaguję tekst",
            "execute": "⚙️ Uruchamiam kod",
            "hitl_confirm": "🤝 Pytam o Twoją decyzję",
        }
        return f"**Wybieram:** {target_labels.get(next_step, next_step)}"

    else:
        # Check for HITL request in the output
        hitl_req = node_output.get("hitl_request")
        if hitl_req:
            hitl_type = hitl_req.get("type", "?")
            return f"🤝 Czekam na Twoją odpowiedź"

        msgs = node_output.get("messages", [])
        if msgs:
            content = msgs[-1].content if hasattr(msgs[-1], 'content') else str(msgs[-1])
            if len(content) > 500:
                return content[:500] + "…"
            return content

        status = node_output.get("execution_status", "")
        if status:
            emoji = "✅" if status == "success" else "❌"
            return f"{emoji} {'Gotowe!' if status == 'success' else 'Coś poszło nie tak'}"

        return "Gotowe"


async def _build_settings() -> cl.ChatSettings:
    """Build the Chat Settings panel from the current model registry state.
    Called both on startup and after every settings update so the dropdown
    always reflects the live list of registered models.
    """
    labels = registry.labels()
    if not labels:
        labels = ["(nie dodano jeszcze żadnych modeli)"]

    main_label  = registry.main_model_label()
    coder_label = registry.coder_model_label()

    # Guard: if stored label no longer exists in list (e.g. model was removed)
    if main_label not in labels:
        main_label = labels[0]
    if coder_label not in labels:
        coder_label = labels[0]

    return cl.ChatSettings([
        TextInput(
            id="new_model",
            label="➕ Podłącz nowy model",
            placeholder="Nazwa | http://host:port | model-id | api-key (opcjonalnie)",
            description=(
                "Podaj dane modelu, aby go dodać do Hapilota. "
                "Przykład: \"Llama 3 Local | http://localhost:1234 | llama-3-8b-instruct | \"."
                " Po zapisaniu model pojawi się na liście."
            ),
            initial="",
        ),
        Select(
            id="main_model",
            label="🧠 Główny model (rozumowanie, wiedza, dokumenty, czat)",
            values=labels,
            initial_value=main_label,
            description="Model odpowiedzialny za rozumienie zapytań, wyszukiwanie wiedzy i tworzenie dokumentów.",
        ),
        Select(
            id="coder_model",
            label="💻 Model do kodu (Python)",
            values=labels,
            initial_value=coder_label,
            description="Model, który pisze i naprawia kod Python uruchamiany w bezpiecznym środowisku.",
        ),
    ])


@cl.on_chat_start
async def start():
    """Funkcja startowa."""
    cl.user_session.set("messages", [])
    cl.user_session.set("last_generated_file", "")
    cl.user_session.set("pending_hitl_continuation", None)
    cl.user_session.set("hitl_pending", False)
    cl.user_session.set("hitl_fields", None)
    cl.user_session.set("hitl_continuation", None)

    # Send the model management settings panel (gear icon ⚙️ in the UI)
    settings = await _build_settings()
    await settings.send()

    m     = registry.main_model
    coder = registry.coder_model
    model_info = (
        f"- 🧠 **Główny model:** {m.display_name}\n"
        f"- 💻 **Model do kodu:** {coder.display_name}\n\n"
        if m and coder else ""
    )

    await cl.Message(
        content=(
            "👋 **Cześć! Jestem Hapilot — Twój asystent AI.**\n\n"
            "Pomogę Ci pracować szybciej i wygodniej. Oto co razem możemy zrobić:\n"
            "- 📄 **Tworzyć dokumenty** — listy, raporty i protokoły gotowe do druku\n"
            "- 🧠 **Znajdować odpowiedzi** — przeszukam Twoją bazę wiedzy w kilka sekund\n"
            "- 🐍 **Uruchamiać kod Python** — obliczenia, wykresy, analiza danych\n"
            "- 💬 **Rozmawiać** — po prostu zapytaj o cokolwiek\n\n"
            + model_info +
            "*Chcesz zmienić modele? Kliknij ⚙️ w prawym górnym rogu.*"
        ),
        author="Hapilot"
    ).send()


@cl.on_settings_update
async def settings_update(settings: dict):
    """
    Fired whenever the user clicks Save in the ⚙️ settings panel.

    Handles three actions (all in one save):
      1. Add a new model if the 'new_model' text field is filled.
      2. Update the active main model assignment.
      3. Update the active coder model assignment.

    After any change the settings panel is refreshed so the dropdowns
    immediately reflect the new model list without a page reload.
    """
    feedback_lines = []

    # ------------------------------------------------------------------ #
    # 1. Add new model (if the text field was filled)                     #
    # ------------------------------------------------------------------ #
    new_model_raw = (settings.get("new_model") or "").strip()
    if new_model_raw:
        parts = [p.strip() for p in new_model_raw.split("|")]
        if len(parts) >= 3:
            disp_name = parts[0]
            base_url  = parts[1]
            model_id  = parts[2]
            api_key   = parts[3] if len(parts) > 3 else ""

            if not disp_name or not base_url or not model_id:
                await cl.Message(
                    content="Hmm, brakuje mi kilku informacji. Podaj proszę wszystkie trzy wartości: `Nazwa | URL | ModelID`.",
                    author="Hapilot",
                ).send()
                return

            registry.add_model(disp_name, base_url, model_id, api_key)
            feedback_lines.append(
                f"✅ Dodano model **{disp_name}** — gotowy do pracy!"
            )
        else:
            await cl.Message(
                content=(
                    "Nie udało mi się odczytać danych modelu. Spróbuj w takim formacie:\n"
                    "`Nazwa | http://host:port | model-id | api-key`\n\n"
                    "Na przykład: `Llama 3 Local | http://localhost:1234 | llama-3-8b-instruct |`"
                ),
                author="Hapilot",
            ).send()
            return

    # ------------------------------------------------------------------ #
    # 2 & 3. Update active model assignments                              #
    # ------------------------------------------------------------------ #
    main_label  = settings.get("main_model", "")
    coder_label = settings.get("coder_model", "")

    if main_label:
        changed = registry.set_main_model_by_label(main_label)
        if changed:
            m = registry.main_model
            feedback_lines.append(f"🧠 **Główny Model** → {m.display_name}")

    if coder_label:
        changed = registry.set_coder_model_by_label(coder_label)
        if changed:
            c = registry.coder_model
            feedback_lines.append(f"💻 **Model Do Kodowania** → {c.display_name}")

    # ------------------------------------------------------------------ #
    # Refresh settings panel (updates dropdowns with any new model added) #
    # ------------------------------------------------------------------ #
    updated_settings = await _build_settings()
    await updated_settings.send()

    if feedback_lines:
        await cl.Message(
            content="⚙️ **Gotowe, zaktualizowałem ustawienia:**\n" + "\n".join(f"  - {l}" for l in feedback_lines),
            author="Hapilot",
        ).send()


@cl.on_message
async def main(message: cl.Message):
    """Główna pętla obsługi wiadomości z wizualizacją ścieżki agenta i HITL."""
    history = cl.user_session.get("messages")
    history.append(HumanMessage(content=message.content))

    inputs = {
        "messages": history,
        "next_step": "",
        "execution_status": "",
        "retry_count": 0,
        "task_plan": {},
        "task_id": "",
        "plan_mode": "auto",
        "is_complex_task": False,
        "current_step": None,
        "last_generated_file": cl.user_session.get("last_generated_file") or "",
        "hitl_request": None,
        "hitl_response": None,
    }
    config = {"recursion_limit": 50}

    # Check if there's a pending HITL response from a previous interaction
    pending_hitl = cl.user_session.get("pending_hitl_continuation")
    if pending_hitl:
        cl.user_session.set("pending_hitl_continuation", None)
        # This is a HITL continuation — re-invoke graph with user's response
        inputs["hitl_response"] = pending_hitl
        inputs["hitl_request"] = None

    # Run the graph
    collected_events = await _run_agent_graph(inputs, config, message.content)

    # --- Check for HITL request in collected events ---
    hitl_request = _extract_hitl_request(collected_events)

    if hitl_request:
        # Handle HITL interaction — show form/actions to user and wait for response
        await _handle_hitl_request(hitl_request, history, collected_events)
        return

    # --- Normal (non-HITL) response flow ---
    await _send_normal_response(collected_events, history)


async def _run_agent_graph(inputs: dict, config: dict, user_content: str) -> list:
    """Run the LangGraph agent and collect node events.

    Sub-steps are created in real-time as each graph node completes,
    using an asyncio.Queue to bridge the sync streaming thread with
    the async Chainlit step creation.
    """
    import asyncio

    collected_events = []
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _run_stream():
        try:
            for event in app.stream(inputs, config=config, stream_mode="updates"):
                for node_name, node_output in event.items():
                    if node_name != "__end__":
                        item = (node_name, dict(node_output))
                        collected_events.append(item)
                        loop.call_soon_threadsafe(queue.put_nowait, item)
        except TypeError:
            result = app.invoke(inputs, config=config)
            item = ("result", result)
            collected_events.append(item)
            loop.call_soon_threadsafe(queue.put_nowait, item)
        # Sentinel: signal the consumer that streaming is done
        loop.call_soon_threadsafe(queue.put_nowait, None)

    async with cl.Step(name="🔄 Pracuję nad Twoim zapytaniem", type="run") as main_step:
        main_step.input = user_content

        # Start streaming in a background thread (non-blocking)
        stream_task = asyncio.ensure_future(cl.make_async(_run_stream)())

        # Consume events in real-time and create sub-steps as they arrive
        while True:
            item = await queue.get()
            if item is None:  # sentinel — stream finished
                break
            node_name, node_output = item
            label = NODE_LABELS.get(node_name, node_name)
            desc = NODE_DESCRIPTIONS.get(node_name, "")
            summary = _summarize_node_output(node_name, node_output)
            async with cl.Step(name=label, type="tool") as sub_step:
                sub_step.input = desc
                sub_step.output = summary

        # Ensure the background thread is fully cleaned up
        await stream_task

        path_labels = [NODE_LABELS.get(name, name) for name, _ in collected_events]
        main_step.output = "**Przeszedłem przez:** " + " → ".join(path_labels)

    return collected_events


def _extract_hitl_request(collected_events: list) -> dict:
    """Check collected graph events for a hitl_request."""
    for _node_name, node_output in reversed(collected_events):
        hitl_req = node_output.get("hitl_request")
        if hitl_req and isinstance(hitl_req, dict):
            return hitl_req
    return None


async def _handle_hitl_request(hitl_request: dict, history: list, collected_events: list):
    """Present HITL UI to user and store continuation for the next message cycle."""
    hitl_type = hitl_request.get("type", "confirm")
    content = hitl_request.get("content", "Potrzebuję Twojej decyzji, zanim przejdę dalej.")
    continuation = hitl_request.get("continuation", {})

    # Update session messages with events so far
    all_new_messages = []
    for _n, no in collected_events:
        all_new_messages.extend(no.get("messages", []))
    cl.user_session.set("messages", history + all_new_messages)

    # Persist last_generated_file
    for _n, no in reversed(collected_events):
        lgf = no.get("last_generated_file")
        if lgf:
            cl.user_session.set("last_generated_file", lgf)
            break

    if hitl_type == "form":
        await _handle_hitl_form(hitl_request, continuation)
    elif hitl_type == "action":
        await _handle_hitl_action(hitl_request, continuation)
    else:  # confirm
        await _handle_hitl_confirm(hitl_request, continuation)


async def _handle_hitl_form(hitl_request: dict, continuation: dict):
    """Handle a HITL form — render interactive CustomElement form via AskElementMessage."""
    fields = hitl_request.get("fields", [])
    content = hitl_request.get("content", "Sprawdź poniższe dane i zatwierdź, gdy będą gotowe:")

    # Store context in session (for potential chat-based editing fallback)
    cl.user_session.set("hitl_fields", fields)
    cl.user_session.set("hitl_continuation", continuation)
    cl.user_session.set("hitl_pending", True)

    template_id = continuation.get("template_id", "")
    element_props = {
        "title": content or "Parametry dokumentu",
        "fields": fields,
        "template_id": template_id,
    }

    custom_element = cl.CustomElement(
        name="hitl_form",
        props=element_props,
        display="inline",
    )

    res = await cl.AskElementMessage(
        content="📋 Przygotowałem wstępne dane dokumentu. Sprawdź je i popraw co trzeba, a potem kliknij **Generuj PDF**:",
        element=custom_element,
        author="Hapilot",
        timeout=600,
    ).send()

    cl.user_session.set("hitl_pending", False)

    print(f"      🔍 [HITL FORM DEBUG] res from AskElementMessage: {res}")
    print(f"      🔍 [HITL FORM DEBUG] res type: {type(res)}")
    print(f"      🔍 [HITL FORM DEBUG] fields count: {len(fields)}")

    if res is None or not res.get("submitted"):
        await cl.Message(content="Rozumiem — anulowałem operację. Daj znać, jeśli zmienisz zdanie! 😊", author="Hapilot").send()
        return

    # Build hitl_response from the submitted form data.
    # Start from continuation's original parameters as base (LLM-generated values),
    # then override with form field values (which may come from res or original fields).
    original_params = continuation.get("parameters", {})
    hitl_response = {
        "continuation_type": continuation.get("type", "generate_pdf"),
        "template_id": template_id,
        "parameters": dict(original_params),  # base: LLM-generated parameters
    }
    for f in fields:
        fid = f.get("id", "")
        # Priority: res (user-submitted) > field value (form default) > original param
        fval = res.get(fid) if isinstance(res, dict) and fid in res else f.get("value", "")
        if fid == "_template_id":
            hitl_response["template_id"] = fval
        elif fid:
            hitl_response["parameters"][fid] = fval

    print(f"      🔍 [HITL FORM DEBUG] Built hitl_response: {hitl_response}")
    print(f"      🔍 [HITL FORM DEBUG] parameters keys: {list(hitl_response.get('parameters', {}).keys())}")
    await cl.Message(content="✅ Świetnie, dane zatwierdzone! Tworzę dokument — to potrwa chwilę...", author="Hapilot").send()
    await _continue_after_hitl(hitl_response)


async def _handle_hitl_action(hitl_request: dict, continuation: dict):
    """Handle a HITL action selection request — show buttons (callback-based)."""
    content = hitl_request.get("content", "Którą opcję wybierasz?")
    action_defs = hitl_request.get("actions", [])

    # Store context for callback
    cl.user_session.set("hitl_action_defs", action_defs)
    cl.user_session.set("hitl_continuation", continuation)

    actions = [
        cl.Action(
            name="hitl_select",
            label=a.get("label", a.get("name", f"Opcja {i+1}")),
            payload={"index": i, **(a.get("payload", {}))},
        )
        for i, a in enumerate(action_defs)
    ]
    actions.append(cl.Action(name="hitl_cancel", label="↩️ Anuluj", payload={"action": "cancel"}))

    await cl.Message(
        content=content,
        author="Hapilot",
        actions=actions,
    ).send()


async def _handle_hitl_confirm(hitl_request: dict, continuation: dict):
    """Handle a simple confirm/reject HITL request — show buttons (callback-based)."""
    content = hitl_request.get("content", "Kontynuować?")

    # Store context for callback
    cl.user_session.set("hitl_continuation", continuation)

    actions = [
        cl.Action(name="hitl_yes", label="✅ Tak, działaj!", payload={"action": "yes"}),
        cl.Action(name="hitl_no", label="↩️ Nie, dziękuję", payload={"action": "no"}),
    ]
    await cl.Message(
        content=content,
        author="Hapilot",
        actions=actions,
    ).send()


# ====== HITL Action Callbacks (for action/confirm types) ======

@cl.action_callback("hitl_cancel")
async def on_hitl_cancel(action: cl.Action):
    """User cancelled the HITL operation."""
    cl.user_session.set("hitl_pending", False)
    await action.remove()
    await cl.Message(content="Rozumiem — anulowałem. Jestem tu, gdybyś potrzebował czegoś innego! 😊", author="Hapilot").send()


@cl.action_callback("hitl_yes")
async def on_hitl_yes(action: cl.Action):
    """User confirmed the HITL confirmation dialog."""
    continuation = cl.user_session.get("hitl_continuation") or {}
    await action.remove()

    hitl_response = {
        "continuation_type": continuation.get("type", ""),
        "confirmed": True,
        **continuation,
    }
    await _continue_after_hitl(hitl_response)


@cl.action_callback("hitl_no")
async def on_hitl_no(action: cl.Action):
    """User rejected the HITL confirmation dialog."""
    await action.remove()
    await cl.Message(content="Rozumiem, anulowałem. Daj znać, jeśli będę mógł pomóc w czymś innym!", author="Hapilot").send()


@cl.action_callback("hitl_select")
async def on_hitl_select(action: cl.Action):
    """User selected an action from a HITL action list."""
    action_defs = cl.user_session.get("hitl_action_defs") or []
    continuation = cl.user_session.get("hitl_continuation") or {}
    await action.remove()

    payload = action.payload if isinstance(action.payload, dict) else {}
    idx = payload.pop("index", 0)
    selected_def = action_defs[idx] if idx < len(action_defs) else {}

    hitl_response = {
        "continuation_type": continuation.get("type", ""),
        "selected_action": selected_def.get("name", f"action_{idx}"),
        "selected_label": action.label,
        "payload": payload,
        **continuation,
    }
    await _continue_after_hitl(hitl_response)


async def _continue_after_hitl(hitl_response: dict):
    """Re-invoke the agent graph with the HITL response to continue processing."""
    print(f"      🔍 [CONTINUE HITL DEBUG] hitl_response received: {hitl_response}")
    print(f"      🔍 [CONTINUE HITL DEBUG] parameters keys: {list(hitl_response.get('parameters', {}).keys())}")
    history = cl.user_session.get("messages")

    inputs = {
        "messages": history,
        "next_step": "",
        "execution_status": "",
        "retry_count": 0,
        "task_plan": {},
        "task_id": "",
        "plan_mode": "auto",
        "is_complex_task": False,
        "current_step": None,
        "last_generated_file": cl.user_session.get("last_generated_file") or "",
        "hitl_request": None,
        "hitl_response": hitl_response,
    }
    config = {"recursion_limit": 50}

    collected_events = await _run_agent_graph(inputs, config, "[HITL kontynuacja]")

    # Check if this continuation produced ANOTHER HITL request (e.g. PDF form)
    hitl_request = _extract_hitl_request(collected_events)
    if hitl_request:
        await _handle_hitl_request(hitl_request, history, collected_events)
        return

    await _send_normal_response(collected_events, history)


async def _send_normal_response(collected_events: list, history: list):
    """Extract final response from graph events and send to user."""
    response_text = "Przepraszam, nie udało mi się przetworzyć tego zapytania. Spróbujmy inaczej — opisz mi, czego potrzebujesz, a razem znajdziemy rozwiązanie."
    all_new_messages = []

    for _node_name, node_output in collected_events:
        new_msgs = node_output.get("messages", [])
        all_new_messages.extend(new_msgs)

    if all_new_messages:
        last_msg = all_new_messages[-1]
        response_text = last_msg.content if hasattr(last_msg, 'content') else str(last_msg)

    # Zaktualizuj sesję
    cl.user_session.set("messages", history + all_new_messages)

    # Persist last_generated_file across requests
    for _node_name, node_output in reversed(collected_events):
        lgf = node_output.get("last_generated_file")
        if lgf:
            cl.user_session.set("last_generated_file", lgf)
            break

    # --- Detekcja Plików PDF (obsługa dynamicznych nazw) ---
    elements = []

    if "SUKCES" in response_text and ".pdf" in response_text:
        pdf_match = re.search(r'Wygenerowano plik:\s*(\S+\.pdf)', response_text)
        if pdf_match:
            pdf_filename = pdf_match.group(1)
        else:
            pdf_match = re.search(r'(\S+\.pdf)', response_text)
            pdf_filename = pdf_match.group(1) if pdf_match else None

        if pdf_filename:
            pdf_path = os.path.join(SANDBOX_PATH, pdf_filename)

            if os.path.exists(pdf_path):
                display_name = pdf_filename.replace("_", " ").replace(".pdf", "") + ".pdf"
                elements.append(
                    cl.File(
                        name=display_name,
                        path=pdf_path,
                        display="inline"
                    )
                )
                response_text += "\n\n✅ **Twój dokument PDF jest gotowy! Kliknij poniżej, aby go pobrać.**"
            else:
                pdf_files = sorted(glob.glob(os.path.join(SANDBOX_PATH, "*.pdf")), key=os.path.getmtime, reverse=True)
                if pdf_files:
                    latest_pdf = pdf_files[0]
                    display_name = os.path.basename(latest_pdf)
                    elements.append(
                        cl.File(
                            name=display_name,
                            path=latest_pdf,
                            display="inline"
                        )
                    )
                    response_text += f"\n\n✅ **Znalazłem Twój dokument: {display_name}. Kliknij poniżej, aby pobrać.**"
                else:
                    response_text += f"\n\n⚠️ *Wygląda na to, że dokument nie został zapisany. Spróbujmy jeszcze raz — opisz, co chcesz stworzyć.*"

    await cl.Message(
        content=response_text,
        elements=elements,
        author="Hapilot"
    ).send()

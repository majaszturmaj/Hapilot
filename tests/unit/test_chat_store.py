"""Chat history store (src/chat_store.py): users, threads per user, steps, elements, feedback, agent state."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict, messages_to_dict

from src.chat_store import ChatStore


@pytest.fixture
def store(tmp_path):
    s = ChatStore(tmp_path / "chats.sqlite")
    yield s
    s.close()


def _chat(store, thread_id, user, name, text="Cześć"):
    store.update_thread(thread_id, user_id=user["id"])
    store.upsert_step({"id": f"{thread_id}-s1", "threadId": thread_id, "type": "user_message", "output": text,
                       "createdAt": f"2026-10-22T10:00:0{len(thread_id) % 10}.000Z"})
    store.update_thread(thread_id, name=name)


def test_users_are_created_once(store):
    a = store.create_user("admin", {"provider": "credentials"})
    assert store.create_user("admin")["id"] == a["id"]
    assert store.get_user("admin")["metadata"] == {"provider": "credentials"}
    assert store.get_user("nobody") is None


def test_each_user_lists_only_own_threads_newest_first(store):
    admin, user = store.create_user("admin"), store.create_user("uzytkownik")
    _chat(store, "t1", admin, "Cennik")
    _chat(store, "t22", admin, "Urlopy")
    _chat(store, "t333", user, "Reklamacje")
    threads, more = store.list_threads(admin["id"])
    assert [t["name"] for t in threads] == ["Urlopy", "Cennik"] and not more
    assert [t["name"] for t in store.list_threads(user["id"])[0]] == ["Reklamacje"]
    assert store.thread_author("t333") == "uzytkownik" and store.thread_author("t1") == "admin"
    assert store.list_threads(None) == ([], False)


def test_threads_without_messages_are_not_listed(store):
    admin = store.create_user("admin")
    store.update_thread("empty", user_id=admin["id"])
    assert store.list_threads(admin["id"])[0] == []


def test_pagination_and_search(store):
    admin = store.create_user("admin")
    for i in range(5):
        store.update_thread(f"t{i}", user_id=admin["id"], name=f"Rozmowa {i}")
        store.upsert_step({"id": f"s{i}", "threadId": f"t{i}", "createdAt": f"2026-10-22T10:00:0{i}.000Z"})
        store._q("UPDATE threads SET created_at = ? WHERE id = ?", (f"2026-10-22T10:00:0{i}.000Z", f"t{i}"))
    page1, more = store.list_threads(admin["id"], first=2)
    assert [t["id"] for t in page1] == ["t4", "t3"] and more
    page2, more = store.list_threads(admin["id"], first=2, cursor="t3")
    assert [t["id"] for t in page2] == ["t2", "t1"] and more
    assert [t["id"] for t in store.list_threads(admin["id"], search="mowa 0")[0]] == ["t0"]
    assert store.list_threads(admin["id"], search="%")[0] == []  # LIKE wildcards are literal


def test_thread_with_steps_elements_feedback_and_metadata(store):
    admin = store.create_user("admin")
    _chat(store, "t1", admin, "Cennik", text="Ile kosztuje?")
    store.upsert_step({"id": "t1-s2", "threadId": "t1", "type": "assistant_message", "output": "60",
                       "createdAt": "2026-10-22T10:00:09.000Z"})
    store.upsert_step({"id": "t1-s2", "threadId": "t1", "output": "60 zł", "streaming": None})  # update keeps fields
    store.upsert_element({"id": "e1", "threadId": "t1", "type": "file", "name": "a.pdf", "path": "/x/a.pdf"})
    fid = store.upsert_feedback("t1-s2", 1, "dobrze")
    assert store.upsert_feedback("t1-s2", 0) == fid  # one feedback per step
    store.update_thread("t1", metadata={"profile": "best-local"})
    store.update_thread("t1", metadata={"extra": 1})  # merged
    thread = store.get_thread("t1")
    assert [s["output"] for s in thread["steps"]] == ["Ile kosztuje?", "60 zł"]
    assert thread["steps"][1]["type"] == "assistant_message" and thread["steps"][1]["feedback"]["value"] == 0
    assert thread["elements"][0]["name"] == "a.pdf" and store.get_element("t1", "e1")["path"] == "/x/a.pdf"
    assert thread["metadata"] == {"profile": "best-local", "extra": 1} and thread["userIdentifier"] == "admin"
    assert store.delete_feedback(fid) and not store.delete_feedback(fid)


def test_agent_state_round_trip_and_delete(store):
    admin = store.create_user("admin")
    _chat(store, "t1", admin, "Cennik")
    history = [HumanMessage(content="Ile kosztuje tłumaczenie?"), AIMessage(content="60 zł [cennik_uslug_2026.pdf]")]
    store.save_state("t1", "messages", messages_to_dict(history))
    store.save_state("t1", "panel_turns", [{"id": 1, "sources": [], "files": []}])
    state = store.load_state("t1")
    restored = messages_from_dict(state["messages"])
    assert [(type(m).__name__, m.content) for m in restored] == [("HumanMessage", "Ile kosztuje tłumaczenie?"),
                                                                 ("AIMessage", "60 zł [cennik_uslug_2026.pdf]")]
    assert state["panel_turns"][0]["id"] == 1
    store.delete_thread("t1")
    assert store.get_thread("t1") is None and store.load_state("t1") == {}
    assert store.list_threads(admin["id"])[0] == []


def test_concurrent_first_steps_create_the_thread_once(store):
    """Chainlit saves the first steps of a chat in parallel; each may be the one that creates the thread."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    admin = store.create_user("admin")
    query = store._q

    def slow_q(sql, args=()):  # widen the gap between "does the thread exist?" and "create it"
        rows = query(sql, args)
        if sql.startswith("SELECT * FROM threads"):
            time.sleep(0.005)
        return rows

    store._q = slow_q
    steps = [{"id": f"s{i}", "threadId": "t1", "type": "run", "createdAt": f"2026-10-22T10:00:{i:02d}.000Z"}
             for i in range(40)]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(store.upsert_step, steps))
        list(pool.map(lambda s: store.upsert_step({**s, "output": "gotowe"}), steps))
    store.update_thread("t1", user_id=admin["id"], name="Równolegle")
    thread = store.get_thread("t1")
    assert len(thread["steps"]) == 40 and all(s["output"] == "gotowe" for s in thread["steps"])
    assert store.list_threads(admin["id"])[0][0]["name"] == "Równolegle"


def test_store_survives_reopening(tmp_path):
    path = tmp_path / "chats.sqlite"
    s = ChatStore(path)
    admin = s.create_user("admin")
    _chat(s, "t1", admin, "Cennik")
    s.close()
    again = ChatStore(path)
    assert again.list_threads(admin["id"])[0][0]["name"] == "Cennik"
    again.close()


def test_panel_tokens_can_be_restored_for_a_resumed_chat(tmp_path):
    from src import ui_panel

    f = tmp_path / "plik.txt"
    f.write_text("x")
    url = ui_panel.url_for("chat-1", f)
    saved = ui_panel.export_tokens("chat-1")
    ui_panel.forget_session("chat-1")
    assert url.rsplit("/", 1)[1] not in ui_panel._tokens
    ui_panel.import_tokens("chat-1", saved)
    assert ui_panel._tokens[url.rsplit("/", 1)[1]]["path"] == f
    ui_panel.forget_session("chat-1")

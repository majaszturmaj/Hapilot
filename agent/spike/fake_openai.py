#!/usr/bin/env python3
"""Scripted OpenAI-compatible chat server for dry runs of the v3 agent container (no GPU, no model).

Every request gets the next scripted assistant turn as a streamed response with one tool call: write the answer file,
try one outbound connection from a shell command (it must fail inside the container), submit. After the script it
answers with plain text. Requests are logged to stdout as JSON lines (tools offered, message count), so a dry run also
shows which tools the runner sends to the model.

Usage: fake_openai.py --host 172.17.0.1 --port 8672 [--answer-path /praca/odpowiedz.json] [--script steps.json]
--script replaces the built-in turns with [[tool name, arguments], ...] (stage 2: Hapilot's MCP tools). Each request
also logs the result of the previous tool call (first 300 characters), so the dry run shows what the tools returned.
Stage 4 (the chat UI): a step may carry a third item, text the model writes before its tool call (a working note),
and --final-text replaces "Gotowe." with an answer streamed in small pieces; a step ["__final__"] gives that answer
in the middle of the script (several chat turns in one script).
"""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def script(answer_path: str) -> list[tuple[str, dict]]:
    return [
        ("editor", {"path": answer_path, "new_text": json.dumps({"odpowiedz": "test", "zrodla": []}, ensure_ascii=False)}),
        ("run_commands", {"commands": [
            "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), 3)\" && echo EGRESS_OPEN || echo EGRESS_BLOCKED",
            f"cat {answer_path}"]}),
        ("submit_and_exit", {"summary": "Odpowiedź zapisana i sprawdzona poleceniem cat.", "verified": True}),
    ]


def chunk(model: str, delta: dict, finish=None) -> bytes:
    body = {"id": "fake-1", "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n".encode()


FINAL = "__final__"  # a script step that answers with the final text (one turn of a chat ends here)


def pieces(text: str, size: int = 3) -> list[str]:
    words = text.split(" ")
    return [" ".join(words[i:i + size]) + (" " if i + size < len(words) else "") for i in range(0, len(words), size)]


def make_handler(steps: list, log: list, final_text: str = "Gotowe."):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.rstrip("/").endswith("/models") or self.path == "/health":
                body = json.dumps({"object": "list", "data": [{"id": "fake", "object": "model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            n = len(log)
            log.append(1)
            tools = [t.get("function", {}).get("name") for t in req.get("tools") or []]
            messages = req.get("messages") or []
            last = messages[-1] if messages else {}
            content = last.get("content")
            if isinstance(content, list):
                content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
            print(json.dumps({"request": n + 1, "messages": len(messages), "tools": tools,
                              "stream": req.get("stream"), "max_tokens": req.get("max_tokens"),
                              "last_role": last.get("role"), "last_content": str(content or "")[:300],
                              "keys": sorted(req)}, ensure_ascii=False), flush=True)
            model = req.get("model", "fake")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            out = [chunk(model, {"role": "assistant", "content": ""})]
            if n < len(steps) and steps[n][0] != FINAL:
                name, args = steps[n][0], steps[n][1]
                if len(steps[n]) > 2 and steps[n][2]:
                    out.append(chunk(model, {"content": steps[n][2]}))
                out.append(chunk(model, {"tool_calls": [{"index": 0, "id": f"call_{n + 1}", "type": "function",
                                                         "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}))
                out.append(chunk(model, {}, "tool_calls"))
            else:
                out += [chunk(model, {"content": piece}) for piece in pieces(final_text)]
                out.append(chunk(model, {}, "stop"))
            usage = {"id": "fake-1", "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                     "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
            out.append(f"data: {json.dumps(usage)}\n\n".encode())
            out.append(b"data: [DONE]\n\n")
            for part in out:
                self.wfile.write(part)
                self.wfile.flush()
                time.sleep(0.05)  # streamed, as a model writes

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8672)
    ap.add_argument("--answer-path", default="/praca/odpowiedz.json")
    ap.add_argument("--script", help="JSON file: [[tool name, arguments(, text before the call)], ...]")
    ap.add_argument("--final-text", default="Gotowe.", help="the answer after the script, streamed in pieces")
    a = ap.parse_args()
    steps = [tuple(s) for s in json.load(open(a.script, encoding="utf-8"))] if a.script else script(a.answer_path)
    server = ThreadingHTTPServer((a.host, a.port), make_handler(steps, [], a.final_text))
    print(json.dumps({"listening": f"{a.host}:{a.port}"}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

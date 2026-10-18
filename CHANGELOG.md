# Changelog

## Unreleased (stage 1: safety and correctness)

### Security and privacy
- Generated code and LaTeX run in a fresh container per run: no network, non-root, read-only root
  filesystem, capability drop, memory/CPU/process limits, timeout; only the session workspace is mounted.
- One workspace per chat session; files are never looked up or served across sessions.
- Model endpoints must be local/LAN addresses; the OpenAI test switch and other cloud code paths were removed.
- Hugging Face, Chroma and LangChain telemetry and run-time downloads are switched off; ChromaDB is
  embedded (no database container or port).
- The UI, model proxy and optional n8n bind to 127.0.0.1; optional password login.
- Models are selected per session from operator-defined profiles; the UI can no longer add endpoints.
- Egress test (`hapilotctl.py egress-test`): scenarios under strace plus an in-process guard.

### Fixes
- Python steps of a plan executed the step's text instead of the generated code.
- The UI waited forever when the graph raised an error.
- Editing a document sent only its first 3000 characters to the model.
- The rest of a plan was lost after a confirmation step.
- Keyword guards matched inside words ("zmienna") and ignored negation ("nie rób PDF").
- Plans longer than two steps always waited for approval, also in scripts (approval policy `ask`/`auto`).
- Calendar look-ups used a fixed date; e-mail steps pretended to send (now drafts only; n8n is an
  optional, disabled extension that creates Outlook drafts after confirmation).

### Verification (GPU host, RTX 4090, 2026-10-18, commit 0752d4f)
- Unit tests: 92 passed (Python 3.10 locally, 3.12 on the GPU host); ruff clean.
- Sandbox: non-root, no network (only `lo`), read-only root filesystem, no Docker socket, 60 s timeout enforced.
- Smoke scenarios: 7/7 with `best-local` (82.6 s) and `reference` (138.3 s).
- Egress test: passed for both profiles.
- The reference run found one more bug, fixed in 0752d4f: the chat node sent two assistant messages in a row,
  which llama.cpp rejects.

### Tooling
- `config/profiles.yaml` (`reference`, `best-local`), `src/config.py` (one settings module), `tools/modelswap.py`,
  `scripts/hapilotctl.py` (safe launcher), `scripts/smoke.py` + `tests/scenarios/smoke.yaml`.
- Unit tests with a scripted model and fake Docker; GitHub Actions CI (tests + ruff).
- Demo knowledge base for a fictional company (`examples/demo_company/`).

## 0.1.0 — 2026-10-18
- First public release.

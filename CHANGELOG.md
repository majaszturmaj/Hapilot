# Changelog

## Unreleased (stage 2: evaluation readiness)

- Evaluation interface v1 (`src/eval/`, `docs/EVALUATION_INTERFACE.md`, `docs/schemas/`): tasks in JSONL, results,
  per-task JSONL traces with content-addressed prompts, run metadata, resumable batch runs, schema validation,
  `hapilotctl.py eval`.
- Tracing of nodes, model calls, tool calls, retrievals, sandbox runs, approvals; per-task time budget.
- Documents: .docx, .eml and .xlsx alongside .pdf/.txt/.md (`src/extractors.py`); chunk metadata (relative path,
  page, sheet); unreadable files listed.
- Retrieval: hybrid E5 + BM25 with reciprocal rank fusion (default top 8); E5 passage/query prefixes.
- Attachments: one module for UI uploads and evaluation tasks; uploads enabled in the UI for the supported formats.
- Demo company documents in all formats; ten development tasks with a checker (`scripts/dev_eval_check.py`).
- Python steps get the results of earlier plan steps and can read the company documents (read-only `/data` in the
  sandbox, still without network) — found by the development tasks on the GPU host (the coder computed on
  numbers it never received).

## Stage 1: safety and correctness

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

### Configuration
- Default model profile: `best-local` (Qwen3.6-27B for all roles); `reference` stays selectable per session.

### Tooling
- `config/profiles.yaml` (`reference`, `best-local`), `src/config.py` (one settings module), `tools/modelswap.py`,
  `scripts/hapilotctl.py` (safe launcher), `scripts/smoke.py` + `tests/scenarios/smoke.yaml`.
- Unit tests with a scripted model and fake Docker; GitHub Actions CI (tests + ruff).
- Demo knowledge base for a fictional company (`examples/demo_company/`).

## 0.1.0 — 2026-10-18
- First public release.

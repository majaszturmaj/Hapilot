# Changelog

## Unreleased (stage 3: extension SDK)

Behaviour change after `eval-v1`: the planner prompt is now built from the extension manifests (same tools,
reworded descriptions). Evaluation runs record the commit, so results of `eval-v1` and later builds stay apart.

- Manifest version 1 (`hapilot_extension.json`, schema `docs/schemas/extension.schema.json`): nodes with
  their edges (`then`, or `after.policy` `plan` / `on_success` / `retry`), planner tools with Polish
  descriptions (`when_to_use`, `when_not_to_use`, `example`) and aliases, prompts, hooks, config. All
  built-in extensions migrated.
- The planner's tool catalog, the router's tool → node map and the graph's edges come from the loaded
  manifests; the hard-coded tool descriptions, edges and the unused route registry were removed. The planner
  system prompt moved to `src/core/prompts/task_planner.txt`.
- Loading checks: schema, imports, prompt files, duplicate node/tool names, edges to unknown nodes; a
  failing extension is skipped with a reason. `python -m src.core.extension_manager [DIR ...]` lists tools
  and errors.
- Extensions outside the repository: `HAPILOT_EXTENSIONS_DIRS`.
- Hooks (`BaseExtension`): `on_before_route` can redirect a step, `on_after_node` can observe or replace a
  node's output; both now run.
- Example extension `examples/extensions/vat_calculator` (deterministic VAT node, hooks, config) and a
  rewritten `docs/extension_development.md`.
- Planner prompt rewritten around the catalog (one step per part of the request, the last step delivers
  it; steps do not pre-decide values or formulas that come from the documents). New guard: an explicit
  request to create a document gets a `template_pdf` step when the planner left it out (seen with Bielik).

## eval-v1 — 2026-10-18 (stage 2: evaluation readiness)

Frozen evaluation interface (docs/EVALUATION_INTERFACE.md). Behaviour changes after this tag go to later tags.

Verification on the GPU host (RTX 4090): unit tests 119 passed; development tasks through the evaluation interface
10/10 `ok` and schema-valid with both profiles, checks 10/10 (`best-local`) and 8/10 (`reference`; two arithmetic or
date-reasoning answers by Bielik); a re-run resumed without repeating tasks; egress guard log empty; egress test
passed (commit 62a949e); smoke 7/7.

Found and fixed during verification: Python steps did not get earlier results or documents; Bielik's trailing `}`
dropped the plan; the launcher mistook TIME_WAIT connections for a port in use.

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

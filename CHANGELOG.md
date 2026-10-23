# Changelog

## Unreleased

UI changes, plus one agent fix in the document generator (below).

- Right-hand panel **📎 Źródła i pliki** (Chainlit element sidebar, `public/elements/workspace_panel.jsx`). Opens
  after an answer that used company documents or changed files; the button under the answer reopens it.
  - *Źródła*: the documents whose chunks went into the prompts for the answer, cited ones first. PDFs are shown
    as their original pages with the chunks highlighted on the page; .docx, .eml, .xlsx, .txt and .md as the
    full extracted text (spreadsheets as tables) with the chunks marked; buttons jump to each chunk.
  - *Pliki*: files created or modified by the request (workspace snapshot before/after), with preview,
    diff for text files and download.
  - A permanent tab on the right edge (`public/panel_tab.js`, Chainlit `custom_js`) minimises the panel and
    opens it again on the answer viewed last (Chainlit's own ← button drops the panel).
- Chat history (with login on): chats are stored per user in SQLite (`src/chat_store.py`, stdlib `sqlite3`;
  Chainlit data layer in `src/ui_history.py`). Past chats appear in the left sidebar; a reopened chat
  continues with its earlier messages, files and side panel. Users see only their own chats. Deleting a chat
  also deletes its workspace files and attachment index. Workspaces and
  attachment collections are now keyed by the chat id instead of the browser session.
- Generated documents: a button **✏️ Edytuj dane i wygeneruj ponownie** under every answer with a template PDF
  opens the same form with the values that PDF was made from; submitting compiles a new version directly (no
  model call, no plan replay) and keeps the previous file. Buttons come back when a chat is resumed.
- Agent panel (`/hapilot/admin`, with login): the agent graph (drawn from the loaded extensions and checked
  against the compiled LangGraph graph), custom prompt-only nodes and prompt editing. Every user adds, edits and
  deletes their own **private** nodes (used only in their chats); administrators (`HAPILOT_UI_ADMINS`) switch their
  nodes **global** (used in everyone's chats) and edit built-in prompts with version history and restore; others
  read them. Each user's chats run on their own compiled graph; an audit log records every change. UI only:
  evaluation runs never load custom nodes or prompt overrides.
- Document templates (agent panel, tab „📄 Szablony”): users create private PDF templates with a block builder or in
  LaTeX, with a live PDF preview compiled in the sandbox; a template is saved only if it compiles. Administrators share
  templates globally and edit the built-in ones (history, restore). Each user's chats see built-in + global + own
  templates; a template named in the request is used even if a built-in keyword matches. New fill filters
  `|lista`, `|tabela:N`, `|linie`, `|akapity`; form fields honour `multiline`. Evaluation runs use only `templates/`.
- Signatures: the document form offers „Podpis” — a blank place for a handwritten signature (default) or the
  user's own scan (uploaded in the panel, „✍️ Mój podpis”; private, PNG/JPEG ≤ 2 MB). Built-in templates and the
  builder's signature block have a fixed-size signature place (`{{PODPIS_SLOT}}`), so the layout is the same either
  way; in evaluation runs it is always blank (the letter's gap above the line is 1.2 cm instead of 1.6 cm, the
  report's and protocol's signature rows a few millimetres taller). The choice is kept for regeneration.
- `tracing.event` also passes events to an optional run `observer` (no effect on traces).
- Team installs on a local network (`docs/deployment.md`): `HAPILOT_UI_HOST` binds the UI to a LAN address or
  `0.0.0.0` (model servers stay on 127.0.0.1), `HAPILOT_UI_SSL_CERT`/`HAPILOT_UI_SSL_KEY` serve it over HTTPS,
  `hapilotctl.py make-cert` creates a self-signed certificate. `hapilotctl.py update` follows a git branch
  (`HAPILOT_UPDATE_BRANCH`, default `main`): waits until nobody has used the UI for 10 minutes, backs up chats,
  accounts, nodes, templates and signatures, installs changed requirements, runs the unit tests of the new commit
  in a worktree, switches, restarts the UI and checks it; on failure it goes back to the previous commit and does
  not retry that commit. `ui ensure` / `update --keep-running` for cron; `ui down` keeps it off.
- Unit tests no longer read the project's `.env` (`HAPILOT_SKIP_DOTENV`, set by `tests/conftest.py`): a running
  install's settings could point tests at its data.
- New dependency `pypdfium2==5.14.0` (Apache-2.0/BSD-3): PDF page rendering and character positions.

Agent fix (document generator):

- A follow-up such as „Wygeneruj ten list jako dokument PDF" now fills the template from the assistant's answer
  in the previous turn (passed to the template-filler prompt when the request refers back to earlier output;
  `_refers_to_previous_answer`, `_get_previous_turn_answer` in `src/agent_graph.py`). Before, only the current
  turn was used, so the model invented a different document or returned no JSON.
- When the template filler answers without a JSON object, it is asked once more for the JSON only before the
  step fails with „Model nie zwrócił poprawnego JSON".

## v0.3.1 — 2026-10-19 (stage 6: deployment and timing; fonts)

The agent's behaviour is unchanged; generated PDFs look different (fonts).

- PDFs in TeX Gyre Termes (Times-like) instead of the PL fonts; Polish text still copies correctly, and
  § € ° ± × come from the same typeface. Needs the sandbox image `hapilot-sandbox:0.2` (0.1 plus the `tex-gyre`
  package in a last layer; run `hapilotctl.py setup`). eval-v1 keeps using 0.1.

- `scripts/timing.py`: time per path (smoke scenarios repeated), development tasks with model calls and tokens,
  model loads and swaps from the proxy log; Markdown and JSON report.
- `docs/timing.md`: measurements of 2026-10-19 on an RTX 4090 with both profiles, from a fresh installation of
  `v0.3.0` in its own directory with its own ports; raw data in `docs/measurements/`.

## v0.3.0 — 2026-10-19 (stage 5: showcase)

Behaviour changes after v0.2.0 (the benchmark stays on `eval-v1`).

Verification on the GPU host (RTX 4090): integration suite passed at `ad9fa98` — `reference` smoke 11/11,
planner 25/25, dev 9/10 (D03 known); `best-local` smoke 11/11, planner 25/25, dev 10/10. Egress test passed at
`25a14c6` with both profiles (11/11 scenarios each). UI walk-through (`scripts/ui_demo.py`) 9/9 at `25a14c6`.
Unit tests 218.

- Uploaded files: new built-in extension `file_tools` — summary of a whole attached file (long files part by
  part), editing (.docx/.txt/.md keep their format, .pdf/.eml become .docx; Word styles, lists and tables kept),
  adding a file to the company knowledge base after confirmation (`HAPILOT_ADDED_DOCS_DIR`, indexed at once;
  never in evaluation runs).
- Planner guards read the user's own words, not attached text; an edit request with a new attachment is not
  forced to `modify_document`; a `modify_document` step that can only fail and a confirmation step at the end of
  a plan are dropped (both seen with Bielik in the integration runs).
- PDFs: Polish text can be copied and searched (OT4 encoding, PL fonts, Unicode map); signature lines in the
  letter and report; short reports get a compact layout; § × • ° ± ² ³ € → ← ≤ ≥ ≈ are set with scalable glyphs,
  unsupported symbols are dropped instead of failing the document; Markdown bold becomes bold.
- Fix: LaTeX commands in template fields (lists, line breaks, bold) were printed as `\_\_LATEX\_…` (also in
  `eval-v1`).
- UI: plain wording instead of `WYNIK: SUKCES`, program output in a code block, downloads for .docx/.txt/.md
  too, plain form title, Polish profile descriptions, welcome text mentions files; Chainlit's own labels in Polish
  (`.chainlit/translations/pl-PL.json`); after a click both buttons of a question disappear.
- Knowledge base: a file with the same content is not added twice; re-indexing while the UI runs no longer
  leaves the UI searching a deleted collection (found by the UI walk-through).
- Ports set in `.env` reach the launcher; port values are checked.
- Requirements: unused `langchain-community` removed; `pyproject.toml` matches `requirements.txt`; a test keeps
  it that way.
- Demo: files to upload (`examples/demo_company/uploads/`), presenter's script `docs/demo.md`, automated UI
  walk-through `scripts/ui_demo.py` (Playwright, optional); smoke scenarios with uploads run on a temporary
  knowledge base; planner scenarios with attachments.

## v0.2.0 — 2026-10-19 (stages 3–4: extension SDK, tests and CI)

### Stage 4: tests and CI

No behaviour change in the agent. Test and tooling changes only.

Verification on the GPU host (RTX 4090, commit `3a7c32a`): integration suite passed — `reference` smoke 7/7,
planner 20/20, dev 8/10 (D03, D05 known); `best-local` smoke 7/7, planner 20/20, dev 10/10; unit tests 181 passed;
evaluation runs schema-valid; no egress attempts.

- Integration suite for the GPU host: `hapilotctl.py integration` runs the unit tests, then per profile the smoke
  scenarios, a new planner check (`tests/scenarios/planner.yaml`, 20 requests, expected tools; planner node only,
  traced) and the development tasks through the evaluation interface; known failures per profile in
  `tests/integration/baseline.yaml`; Markdown and JSON report.
- New unit tests: editing a generated document through the graph, the knowledge node's no-data handling in plans,
  corpus and attachment indexing, the evaluation command line, the integration suite's own logic.
- CI: coverage gate (85 % over `src`, `extensions`, `examples`), extension manifest load check, timeout and
  concurrency group. `coverage` added to `requirements-test.txt`.
- `docs/testing.md`: unit tests, CI, integration suite.

### Stage 3: extension SDK

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

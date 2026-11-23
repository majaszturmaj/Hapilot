# Changelog

## v0.4.5 — 2026-11-23 (engine v3: back to a chat that is still working)

- A v3 chat opened again while its answer is being prepared (the user went to another chat and came back) shows the
  turn as running: the progress indicator and ⏹, a "⏳ Wciąż pracuję…" note, the next steps, the answer as it streams,
  the files and the side panel. Before, Chainlit's new session for the returning browser got nothing of the running
  turn: no indicator, and the answer only after reopening the chat once more.
- The agent's question (buttons or text) and the PDF form go to every session of the chat; the first answer counts.
- A message written in such a chat while the turn runs gets the "still working" note and keeps the indicator on.
- `📎 Źródła i pliki` under such an answer reads the panel from the saved chat state.

## v0.4.4 — 2026-11-23 (engine v3: data analysis in Polish formats)

- Rules (`agent/rules/hapilot.md`): Polish CSV files are read with `sep=";", decimal=","` after a look at the first
  rows and the column types; numbers on charts and in tables for the user are written the Polish way (21 671,38 zł);
  CSV files for the user open in Polish Excel (`;`, decimal comma, UTF-8 with BOM); helper scripts go to `/tmp`, so
  `/praca/wyniki` holds only the user's files. Found in three data-analysis checks on fictional CSV files: the numbers
  were right, but each test first read the CSV with the wrong decimal sign, chart labels read "29,480 zł" and a
  helper script was listed among the results.

## v0.4.3 — 2026-11-23 (engine v3: PDF form before generating; PDFs open on the right)

- A PDF the v3 agent prepares is a draft, as under v2: after the answer the document form shows the template and the
  values the agent filled in; the user edits them, chooses the signature (blank place or their scan) and clicks
  **Generuj PDF**, which compiles the template without a model call (`src/agent_documents.py`). A form closed
  without generating leaves a "📋 Otwórz formularz dokumentu" button. Evaluation runs still compile at once.
- Under a v3 PDF, **✏️ Edytuj dane i wygeneruj ponownie** opens the form with its values and makes a new version (the
  old one stays); the buttons come back when a chat is reopened.
- A PDF under a v3 answer opens in the right-hand panel when its name is clicked, instead of a download dialog.
- The v3 known limit "blank signature place" is gone: the form offers the user's scan.

## v0.4.2 — 2026-11-23 (engine v3: side panel back)

- The right-hand panel **📎 Źródła i pliki** works under v3 as under v2: after an answer it opens with the documents
  the answer names, the agent read (marked "📖 przeczytany przez agenta") or its `semantic_search` returned (fragments
  highlighted), each shown whole (PDF pages, text, tables), and the files the agent made in `/praca/wyniki` (a PDF as
  pages, text with a preview). The button under the answer opens it again, also in a resumed chat.
- Engine: `TurnResult.retrievals` (search fragments, recorded by the bridge), `TurnResult.opened` (document paths the
  agent read, from `read_files` and commands), and `status` created | modified per result file.

## v0.4.1 — 2026-11-23 (engine v3: search like a coding agent)

- Rules (`agent/rules/hapilot.md`, r4): the agent searches the documents as a coding agent does: `ls` and `grep`
  over the text mirror first, whole documents read, `semantic_search` only as a fallback hint when grep finds
  nothing or the question is vague, never an answer from its fragments, other versions of a document checked.
  With r3 the agent's first step was `semantic_search` in nearly every dev-set task.
- Agent image `hapilot-agent:0.7`: the `semantic_search` tool description says the same (the MCP server is in the
  image). Build it before updating: `docker build -f sandbox/agent.Dockerfile -t hapilot-agent:0.7 .`
- `agent/spike/spike.py run --tool-list`: choose the MCP tools offered in dev-set runs.

## v0.4.0 — 2026-11-19 (engine v3, opt-in; UI changes)

### Engine v3 (`HAPILOT_ENGINE=v3`; v2 stays the default and is unchanged)

A second agent engine: each chat message is one model loop on the Cline SDK (`@cline/sdk` 0.2.0, Apache-2.0,
licence and NOTICE in `agent/runner/`) with native tool calls, in a fresh container per message. The agent reads the
company documents as files instead of answering from retrieved chunks ([agent/README.md](agent/README.md)).

- Container (`hapilot-agent:0.6`, `sandbox/agent.Dockerfile`): internal network whose only way out is a relay to the
  local model server; read-only root and documents, no capabilities, `no-new-privileges`, memory/CPU/process limits;
  a Node guard refuses and logs any other connection. Telemetry off.
- Documents: a text mirror next to the index (`src/text_mirror.py`; page and sheet markers, a CSV per sheet), kept in
  step by ingest and the knowledge base's re-checks; per-conversation workspace `/praca` (`src/agent_workspace.py`).
- Tools: Cline's own (read files, search, shell and Python in the container, editor, questions to the user) and
  Hapilot's over MCP through a host bridge (`src/agent_bridge.py`): `semantic_search`, `pdf_document` (the template
  catalogue, users' own templates included), `working_days` (Polish holidays), `email_draft` (optional, approved in
  the chat). Polish rules in `agent/rules/hapilot.md`, including the handling of instructions planted in documents.
- Chat UI (`src/agent_engine.py`, `src/ui_agent.py`): streamed answer, tool steps, questions with buttons, approvals,
  files from `/praca/wyniki`, stop, resume, a one-slot queue with the user's place shown.
- Evaluation: `python -m src.eval run --engine v3` (same output files and schema ids; additive fields, documented in
  `docs/EVALUATION_INTERFACE.md` §9); the answer is checked at submit and refused once with the reason.
- Tests: `scripts/egress_v3.py`, `scripts/smoke_v3.py`, `integration.py --engine v3` (baseline keys `v3/<profile>`),
  agent profiles in `config/agent_profiles.yaml`; CI lints `agent/` and runs the runner's Node tests and self-check.
- Operation: `hapilotctl agent up|down|status|egress-test` (image, internal network, relay with
  `HAPILOT_AGENT_UPSTREAM`; [docs/deployment.md](docs/deployment.md), "Engine v3"); `hapilotctl status` shows it.
- Agent panel: under v3 the first tab shows the engine (loop, tools, limits) instead of v2's graph, the prompts tab
  shows the rules read-only, and custom-node and prompt edits, which would have no effect, are refused.
- Measured on the fictional dev set (ARONIA): Qwen3.6-27B tuning 27/28 and control 12/12, Gemma 4 31B 28/28 and
  11/12; integration dev tasks 10/10 on both; egress test 11/11.

Known limits of v3: no chat tool for adding a file to the knowledge base (use **📚 Baza wiedzy**); PDFs get a blank
signature place (the user's scan is not used yet); custom nodes and v2 prompt edits do not apply; one model request
at a time.

### Updater

- `update` waits up to 90 s for its lock instead of giving up at once. The cron lines for `update` (every 10 min) and
  `ui ensure` (every minute) start in the same second, and `ui ensure` usually took the lock first: on the pilot the
  log shows 263 skipped updates ("another update is running"), and updates went through only when the updater won
  the race.

### UI and v2

UI changes, plus one agent fix in the document generator (below).

- "Which documents do you have?" is answered from the index (`list_documents`, extensions/rag_knowledge 1.3.0):
  every document with pages, pages read by OCR or sheets, documents added from the chat marked, and the files attached
  in the conversation — no model, no search (the search sees only the best chunks; on the pilot the model listed one
  document of eleven). A planner guard (`asks_for_document_list`) routes the question; the tool is declared
  `"planner": false` (new manifest field), so the planner's catalog and prompt are unchanged.
- CSV files: uploads in the chat (`.chainlit/config.toml` accept is now a MIME → endings map; `.csv` also under
  `application/vnd.ms-excel` as Windows with Excel reports it, `.xls` still refused), knowledge base and the knowledge
  tab; read like a sheet, at most 2,000 rows with a note. The project context now tells the model which formats it
  accepts (from `extractors.SUPPORTED`) and that images are not read yet — on the pilot it claimed PNG and CSV worked.
- Coder: sees the structure of the session's .xlsx/.csv files (`src/data_preview.py`: sheets, rows, columns, first
  rows; a sheet whose cells hold whole CSV lines is recognised and the right `pandas` call given — on the pilot five
  attempts at such a file gave five empty charts) and the conversation's earlier requests and last code. A planner
  guard (`asks_about_chart`) sends chart requests and follow-ups ("wykres jest pusty") in a conversation with a data
  file or earlier code to the coder instead of chat.
- Writer: letter formulas („Szanowny Panie”, „Z poważaniem”, signature) only when the user asks for a letter, e-mail
  or other message — not in answers to questions; no units, values or conclusions that are not in the results.
- Crash fix: every use of pdfium (side panel pages, highlight boxes, scan detection, OCR rendering) goes through one
  process-wide lock (`source_view.PDFIUM_LOCK`). pdfium is not thread-safe; on the pilot the UI died with a segfault
  while a browser loaded a panel's page images in parallel (2026-10-26, and a general protection fault on 2026-10-23);
  a 16-thread stress run reproduced it 3 out of 3 times before the fix. PNG encoding stays outside the lock.
- `ui ensure` waits while an update runs and logs every restart; docs/deployment.md adds a cron line that runs it every
  minute (an outage after a crash lasted up to 10 minutes).
- Files a request created or changed (e.g. a chart saved by code) are attached under the answer — images shown inline —
  independent of the side panel; a file of the chat that an answer names is attached again; the user's own uploads,
  scripts and the `.tex` next to a generated PDF never are. A program's result lists the files it created
  ("Utworzone pliki: …"), shown separately from its output.
- Prompts: the writer describes only what tool results contain — it cannot see charts, so it no longer describes their
  shape — and says that created files are attached; the chat node names a requested file instead of claiming it
  cannot send files; both coder prompts save charts with `plt.savefig` (never `plt.show()`) and print the numbers that
  describe them.

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
- Company look (agent panel, tab „🎨 Wygląd”, administrators): the app's name, a 1–2 sentence company
  description, a logo (PNG/JPEG ≤ 1 MB, checked by content; SVG refused) and the main colour, for every user,
  without a restart. The name replaces "Hapilot" in the page title, login page, messages, welcome text, the
  "O aplikacji" page (with a credit to Hapilot) and — every inflected form — in the system prompts; the description
  becomes a `[FIRMA]` prompt section. Wide logos stay in the header and login page; favicon and avatar then show
  the name's first letter on the main colour. Versions with history, restore, reset to Hapilot, audit log; live
  preview. UI only (`src/branding.py`, `src/ui_branding.py`); evaluation runs always run as Hapilot. Chainlit's own
  logo, favicon and avatar are now sent with `Cache-Control: no-cache`. The file tools' confirmation no longer
  names the product.
- Knowledge base in the agent panel (tab „📚 Baza wiedzy”, administrators only; `src/knowledge_admin.py`): every
  document the knowledge base searches (company folder and documents added from the chat) with type, size, pages,
  chunks, OCR pages and confidence, who added it and when, and its indexing status; preview of the indexed text per
  page; download / open the original. Upload of several files at once or by drag and drop (≤ 50 MB each,
  `HAPILOT_KB_MAX_UPLOAD_MB`; type checked by content) straight into `HAPILOT_DOCS_DIR`; replace with a new version;
  delete. Replaced and deleted files go to a trash for 30 days (`HAPILOT_KB_TRASH_DAYS`) and can be restored. Indexing
  runs in a background thread of the UI and touches only the changed documents (`ingest_data.index_entries` /
  `remove_sources`, per-document list `<collection>.files.json`, fingerprint kept current, so the next `ingest` of
  an unchanged folder does nothing); the UI re-checks the folders at start-up and every 5 minutes
  (`HAPILOT_KB_RESCAN_MINUTES`). Chat uploads record who added the file. Every change is in the audit log; the list of
  who added what is in the updater's backup. `hapilotctl.py ingest` refuses to run while the UI runs (`up` skips it).
  The demo documents in the checkout are listed, never changed.
- Knowledge base fix: the BM25 index was cached by the number of chunks, so a document replaced by a version with
  as many chunks was still searched in its old wording; it is now keyed by the chunk ids (which hash the text).
- Signatures: the document form offers „Podpis” — a blank place for a handwritten signature (default) or the
  user's own scan (uploaded in the panel, „✍️ Mój podpis”; private, PNG/JPEG ≤ 2 MB). Built-in templates and the
  builder's signature block have a fixed-size signature place (`{{PODPIS_SLOT}}`), so the layout is the same either
  way; in evaluation runs it is always blank (the letter's gap above the line is 1.2 cm instead of 1.6 cm, the
  report's and protocol's signature rows a few millimetres taller). The choice is kept for regeneration.
- `tracing.event` also passes events to an optional run `observer` (no effect on traces).
- OCR for scanned PDFs (`src/ocr.py`): PDF pages without a text layer are rendered at 300 dpi (pypdfium2) and read
  by Tesseract (`pol+eng`) in a new image `hapilot-ocr:0.1` (`sandbox/ocr.Dockerfile`, built by `setup` /
  `build-images`; existing image tags are never rebuilt), run with the sandbox's isolation (no network, non-root, read-only); one container per document,
  results cached per page in `var/ocr_cache/`. Applies to the knowledge base and to attachments (also in evaluation
  runs); OCR chunks are marked `ocr`, ingest prints pages and mean confidence per file; the side panel shows scanned
  pages without highlight boxes, marked as scans. The OCR state is part of the corpus fingerprint (an existing index
  is rebuilt once). `HAPILOT_OCR=off` restores the previous behaviour. `scripts/ocr_check.py` measures accuracy on a
  simulated scan of a demo PDF.
- Team installs on a local network (`docs/deployment.md`): `HAPILOT_UI_HOST` binds the UI to a LAN address or
  `0.0.0.0` (model servers stay on 127.0.0.1), `HAPILOT_UI_SSL_CERT`/`HAPILOT_UI_SSL_KEY` serve it over HTTPS,
  `hapilotctl.py make-cert` creates a self-signed certificate. `hapilotctl.py update` follows a git branch
  (`HAPILOT_UPDATE_BRANCH`, default `main`): waits until nobody has used the UI for 10 minutes, backs up chats,
  accounts, nodes, templates and signatures, installs changed requirements, runs the unit tests of the new commit
  in a worktree, switches, restarts the UI and checks it; on failure it goes back to the previous commit and does
  not retry that commit. Between stopping and starting the UI it brings the knowledge base up to date with the new
  commit's `ingest`. `ui ensure` / `update --keep-running` for cron; `ui down` keeps it off.
- Unit tests no longer read the project's `.env` (`HAPILOT_SKIP_DOTENV`, set by `tests/conftest.py`): a running
  install's settings could point tests at its data.
- New dependency `pypdfium2==5.14.0` (Apache-2.0/BSD-3): PDF page rendering and character positions.

Agent fix (document generator):

- A follow-up such as „Wygeneruj ten list jako dokument PDF" now fills the template from the assistant's answer
  in the previous turn (passed to the template-filler prompt when the request refers back to earlier output;
  `_refers_to_previous_answer`, `_get_previous_turn_answer` in `src/agent_graph.py`). Before, only the current
  turn was used, so the model invented a different document or returned no JSON.
- When the template filler answers without a usable JSON object — no JSON, JSON the parser rejects (e.g. an
  unescaped quote or a trailing comma) or an object without `template_id` / `parameters` — it is asked again, like
  the coder: up to 3 attempts, each told what was wrong (the parser's message, line and column) and seeing its
  earlier answers; then the step fails with „Model nie zwrócił poprawnego JSON (3 próby)".

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

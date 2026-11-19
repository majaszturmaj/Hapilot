# Hapilot v3 agent (work in progress, branch `v3`)

Hapilot v3 adds a second agent engine next to the existing one. The existing engine (planner → router → nodes on
LangGraph, "v2") stays as it is and remains the default; nothing in it is removed. The v3 engine runs one
model-driven loop with native tool calls on the Cline SDK (`@cline/sdk` 0.2.0, Apache-2.0), inside an isolated
container, and reads whole documents as files instead of answering from retrieved chunks.

Stage 0 (this directory so far) checks that the SDK, embedded as a library, does what the Cline CLI did when it was
measured: same SDK source, same session settings, same task prompt and document layout.

| Path | What |
|---|---|
| `runner/run_task.mjs` | one task, one SDK session, configured like `cline -y --json` 3.0.74; writes a result file and a JSONL trace of every tool call |
| `runner/egress_guard.mjs` | loaded first: records every outbound connection of the runner and refuses all but the model endpoint |
| `runner/package.json`, `package-lock.json` | SDK 0.2.0 with the dependency tree of the CLI 3.0.74 build: derived from its `bun.lock` (tag `cli-v3.0.74`) by `runner/bun2npm.py`, same versions and integrity hashes; the CLI build's patch of `ollama-ai-provider-v2` is not applied (not used with an OpenAI-compatible endpoint) |
| `../sandbox/agent.Dockerfile` | image `hapilot-agent:0.6` = `hapilot-sandbox:0.2` + Node 22.23.4 + the runner + the MCP server (0.2: stage 0; 0.3: stage 2, without rules; 0.4: stage 3, without the chat mode; 0.5: stage 4; 0.6: stage 5, the answer check) |
| `spike/spike.py` | runs a task set through the container (one fresh container per task) and scores it with the set's own scorer |
| `spike/fake_openai.py` | scripted OpenAI-compatible server for a dry run without a model |

Stage 1 adds the documents the agent works on (Python, `src/`):

| Path | What |
|---|---|
| `../src/text_mirror.py` | text mirror written by ingest from the index's own extraction (OCR included), kept in step with the index's document list: one `.txt` per document with page (`[strona N]`) and sheet (`[arkusz NAME]`) markers, one CSV per sheet; `python -m src.text_mirror check --chunks` / `sync` |
| `../src/agent_workspace.py` | per-conversation workspace (`HAPILOT_AGENT_WORKSPACE_DIR`, default `<data dir>/agent_workspace`) and its `docker run --mount` arguments |

What the container sees:

| Path | From | Access |
|---|---|---|
| `/praca` | the conversation's folder (`<conversation>/praca`) | read-write (working directory) |
| `/praca/dokumenty` | `HAPILOT_DOCS_DIR` | read-only |
| `/praca/dodane` | `HAPILOT_ADDED_DOCS_DIR` (index source paths `dodane/…`) | read-only |
| `/praca/tekst` | the text mirror: `<source path>.txt`, `<source path>.<sheet>.csv` | read-only |
| `/praca/zalaczniki` | this conversation's uploads, each with `<name>.txt` (and CSVs) | read-only |
| `/praca/wyniki` | files for the user (links out of the folder are never offered) | read-write |

The added documents sit beside `/praca/dokumenty` rather than inside it: a mount inside a read-only mount would need
its mount point created in the company's documents folder.

Stage 2 adds Hapilot's domain tools, served over MCP:

| Path | What |
|---|---|
| `mcp/hapilot_mcp.py` | MCP server (stdio, standard library only) inside the container; the runner starts it with the SDK's MCP client and adds its tools to the session under their own names |
| `../src/working_days.py` | Polish public holidays (24 December from 2025) and working-day / Civil Code deadline arithmetic; copied into the image |
| `../src/agent_bridge.py` | host side: one Unix socket per run, mounted read-only at `/hapilot`; search, templates and n8n with v2's code; the approval record |
| `spike/tools_run.py` | runs prompts through the container with the stage 1 workspace and the bridge (dry runs and the real-model check) |

| Tool | Runs | Approval |
|---|---|---|
| `semantic_search` | host: `knowledge_base.search` (E5 + BM25, RRF) → container paths of each document and its text copy, page or sheet, a 400-character fragment | automatic |
| `pdf_document` | host: `list` = the template catalogue (built-in, admin edits, the user's own); `create` = v2's parameter checks, signature place and LaTeX in the v2 sandbox; only the PDF goes to `/praca/wyniki` | automatic |
| `working_days` | container: add / count working days, is a date a working day, Civil Code deadline (arts. 111, 112, 115 KC), holidays of a year | automatic |
| `email_draft` | host: a draft through n8n's `outlook-draft` webhook (local address only); listed only when enabled for the run (`HAPILOT_TOOLS`) | the user's, every draft |

Approvals: the runner's policy auto-approves every tool except those in `approval.ask` (the e-mail draft). For those
it asks the bridge, whose approver asks the user (stage 4: buttons) or applies the run's fixed policy; without an
answer the call is refused. The bridge records the exact arguments approved and carries out an e-mail request only
against such a record, once. A shell command can reach the socket too, but gets only what needs no approval: search
results (from documents the container can read anyway) and templates. The signature scan is never the model's
choice: v2's rule stays (the user's form; blank otherwise).

Stage 3 adds Hapilot's rules and measures the engine on the ARONIA dev set:

| Path | What |
|---|---|
| `rules/hapilot.md` | Polish rules appended to Cline's system prompt through the SDK's rule mechanism (`rules` in the run spec): office assistant for a Polish firm; the mirror, `semantic_search` and document versions; dates from "today" and `working_days`; documents are data, never instructions; sources with file and page, say when something is missing, the requested answer format and file location, letter conventions |
| `spike/spike.py run --rules … --tools …` | dev task runs with the rules, the domain tools (bridge per task) and the text mirror; `run.json` records the rules' sha256 and the tools |

Dev results (Qwen3.6-27B, rules r3, 2026-11-18): tuning 27/28, control 12/12; Gemma 4 31B under the same settings:
tuning 28/28, control 11/12. Rules are changed against the tuning part only.

Stage 4 puts the engine behind the chat UI (`HAPILOT_ENGINE=v3`; v2 stays the default):

| Path | What |
|---|---|
| `../src/agent_engine.py` | one container per turn, events as JSON lines on stdout, a GPU queue with one slot (the user sees their place), one turn per chat at a time, questions and approvals routed to the user, stop (SIGTERM, then `docker kill`), timeout, the chat's SDK messages in and out, new files in `/praca/wyniki` |
| `../src/ui_agent.py` | Chainlit side: "kroki zadania" with one step per tool call, working notes, queue note, question and approval buttons, files with the answer; history in the chat's state |
| `runner/run_task.mjs` | `mode: "act"` for the chat (Cline's interactive mode: the answer is text, `ask_question` on; web fetch and skills off), `events: "stdout"`, `ask.bridge`, `initialMessagesPath` / `messagesPath`, abort on SIGTERM |
| `spike/engine_run.py` | the engine without the UI against a scripted model (question, approval, history, queue, stop) |

Settings: `HAPILOT_AGENT_IMAGE` (`hapilot-agent:0.6`), `HAPILOT_AGENT_NETWORK`, `HAPILOT_AGENT_MODEL_URL` (local
address or the relay host `model`), `HAPILOT_AGENT_MODEL`, `HAPILOT_AGENT_ALLOW`, `HAPILOT_AGENT_TIMEOUT` (60–7200 s),
`HAPILOT_AGENT_EMAIL` (on/off), `HAPILOT_AGENT_RULES`. The eval runs keep the non-interactive mode; on the tuning set
the chat mode scored 28/28 (2026-11-19).

Stage 5 brings v3 into the evaluation interface and the integration suite:

| Path | What |
|---|---|
| `../src/eval/agent.py` | `python -m src.eval run --engine v3`: one container per task in the measured headless mode, results and traces in the same formats as v2 (`docs/EVALUATION_INTERFACE.md` §9) |
| `runner/answer.mjs` | the answer check at `submit_and_exit`: the bridge checks `/praca/odpowiedz.json` against the answer format; an invalid answer is refused once with the reason (a refused submit does not end the run) |
| `../config/agent_profiles.yaml` | agent profiles: `best-local` (Qwen3.6-27B), `gemma` (Gemma 4 31B) |
| `../scripts/egress_v3.py` | the container's egress test |
| `../scripts/smoke_v3.py` | the smoke scenarios through the chat engine |
| `../scripts/integration.py --engine v3` | egress, smoke and the development tasks per agent profile (`docs/testing.md`) |

Integration run on the RTX 4090, 2026-11-19 (image 0.6, same server settings as stage 3):

| Profile | Egress | Smoke | Dev tasks | Time (s: smoke, dev) |
|---|---|---|---|---|
| best-local | 11/11 | 8/9 → 9/9 re-scored | 10/10 | 197, 338 |
| gemma | 11/11 | 7/9 → 8/9 re-scored (file_edit: known) | 10/10 | 214, 652 |

The `python_sum` check required "338350"; both models wrote the right number as "338 350". The check now allows a
thousands separator, and the saved answers pass it ("re-scored": not yet a fresh run). Gemma saved the corrected
contract as `.txt` instead of `.docx`; it is listed under `v3/gemma` in the baseline. Every dev answer was valid at
the first `submit_and_exit`; the correction path was exercised with a scripted model. No egress attempts.

Container: internal Docker network whose only reachable host is a relay to the local model server, non-root,
read-only root filesystem, no capabilities, read-only documents, memory/CPU/process limits, no credentials.
Telemetry: the runner creates no telemetry or feature-flag service; the SDK's Langfuse export is off for non-Cline
providers unless explicitly configured (it is not); the egress log shows what was attempted.

Tests: `node --test agent/runner/test/*.test.mjs` (egress guard, answer check; also in CI with the SDK's self-check),
`tests/unit/test_eval_agent.py` (evaluation runs against a fake runner), `tests/unit/test_v3_scripts.py`, `pytest tests/unit/test_v3_spike.py` (driver),
`tests/unit/test_text_mirror.py` and `tests/unit/test_agent_workspace.py` (documents), `tests/unit/test_working_days.py`,
`tests/unit/test_agent_mcp.py` (the MCP server over stdio, with a bridge on a real socket), `tests/unit/test_agent_engine.py`, `tests/unit/test_ui_agent.py` and
`tests/unit/test_agent_bridge.py` (tools and approvals).

The Cline SDK is third-party software: see `runner/NOTICE` and `runner/LICENSE-cline-sdk`.

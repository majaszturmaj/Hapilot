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
| `../sandbox/agent.Dockerfile` | image `hapilot-agent:0.4` = `hapilot-sandbox:0.2` + Node 22.23.4 + the runner + the MCP server (0.2: stage 0; 0.3: stage 2, without rules) |
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

Container: internal Docker network whose only reachable host is a relay to the local model server, non-root,
read-only root filesystem, no capabilities, read-only documents, memory/CPU/process limits, no credentials.
Telemetry: the runner creates no telemetry or feature-flag service; the SDK's Langfuse export is off for non-Cline
providers unless explicitly configured (it is not); the egress log shows what was attempted.

Tests: `node --test agent/runner/test/*.test.mjs` (egress guard), `pytest tests/unit/test_v3_spike.py` (driver),
`tests/unit/test_text_mirror.py` and `tests/unit/test_agent_workspace.py` (documents), `tests/unit/test_working_days.py`,
`tests/unit/test_agent_mcp.py` (the MCP server over stdio, with a bridge on a real socket) and
`tests/unit/test_agent_bridge.py` (tools and approvals).

The Cline SDK is third-party software: see `runner/NOTICE` and `runner/LICENSE-cline-sdk`.

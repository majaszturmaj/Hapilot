# Hapilot — architecture

Developer documentation; for setup see the [README](../README.md). The code is the reference: file
links point to where each part lives.

## Request flow

```text
__start__ → task_planner ─┬─(HITL request / halt)──────────────→ __end__
                          └→ router → <node of the current plan step>
                                        │
            document_generator / modify_document ── success → next step's node, else __end__
            coder → execute ── success → next step's node; error → coder (max 3 attempts)
            knowledge, writer, chat, hitl_confirm, operations ── next step's node, or __end__
```

- **task_planner** ([src/agent_graph.py](../src/agent_graph.py)) asks the `task_planner` role for a JSON
  plan (`task_name`, `complexity`, `steps[{id, action, tool, input}]`). Only tools of enabled extensions
  are offered ([`available_planner_tools`](../src/agent_tools.py)). Deterministic guards then apply:
  - an edit request (`zmień`, `popraw`, …, matched on word boundaries) becomes a single
    `modify_document` step — only if a document was generated earlier in the session;
  - `template_pdf` steps are removed unless the user asked for a document (`pdf`, `podanie`, `raport`,
    `protokół`, …) and did not say otherwise (`nie rób PDF`, `bez PDF`, `tylko tekst`);
  - under the `ask` approval policy, plans longer than two steps wait for the user's approval.
- **router** maps the current step's tool to a node without calling a model; no plan → `chat`.
- After a node finishes its step it calls `_advance_plan_step`; the edge sends the run to the next
  step's node or ends it.
- **Resuming after the user answers** (plan approval, confirmation, document form, e-mail draft): the
  UI re-invokes the graph with `hitl_response`, which carries the saved plan and step; the planner
  restores them and the router continues at the right node.

## State (`AgentState`)

| Field | Meaning |
|---|---|
| `messages` | conversation; tool results are added as `HumanMessage("WYNIK: …")` |
| `task_plan`, `current_step` | plan and index of the step being executed (`None` = no plan) |
| `generated_code` | code produced by `coder`, executed by `execute` |
| `retry_count`, `execution_status` | Python repair attempts; `success` / `error` / `pending_hitl` |
| `last_generated_file` | last `.tex` generated in this session's workspace |
| `hitl_request`, `hitl_response` | question for the user / the user's answer |
| `halt` | the planner decided there is nothing left to run |

Per-run values (session id, model profile, approval policy, "today") are not in the state: they travel
in LangGraph's run config (`configurable`), see [src/run_context.py](../src/run_context.py).

## Nodes (extensions)

| Node | Extension | What it does |
|---|---|---|
| `knowledge` | `rag_knowledge` | rewrites the question into search terms, searches the knowledge base, answers with sources |
| `writer` | `writer` | text synthesis; e-mail steps produce drafts only |
| `chat` | `chat` | conversation |
| `document_generator` | `document_generator` | picks a template, fills its parameters; form (ask) or direct PDF (auto) |
| `modify_document` | `document_modifier` | edits a document generated in this session (via its saved parameters) |
| `coder`, `execute` | `python_coder` | writes Python, runs it in the sandbox, retries with the error |
| `hitl_confirm` | core | confirmation step (auto-confirmed under `auto`) |
| `operations` | `n8n_operations` (off) | Outlook draft after confirmation / calendar look-up via local n8n |

## Models

[src/llm_config.py](../src/llm_config.py): `get_llm(role)` returns the chat model and system prompt for
a role, using the session's profile from [config/profiles.yaml](../config/profiles.yaml). Endpoints are
checked with `assert_local_url`. When a role has its own model (e.g. the coder in `reference`) and the
server does not list it, the role falls back to the profile's default model and to the role's
`<role>_fallback` prompt if one is registered. System prompts come from the extensions' `prompts/`
files (prompt registry) plus security rules, project context and the current date.

## Documents

Templates: [templates/templates.json](../templates/templates.json) (parameters, descriptions, examples)
and `templates/*.tex` with `{{PARAMETER}}` placeholders. Values are LaTeX-escaped
(`_sanitize_latex_value`). Every generated document stores its parameters next to the `.tex`
(`<name>.params.json`); edits change those parameters and regenerate the PDF, so nothing is truncated.

## Knowledge base

[src/knowledge_base.py](../src/knowledge_base.py): ChromaDB `PersistentClient` (telemetry off) under
`HAPILOT_DATA_DIR/chroma`; embeddings `intfloat/multilingual-e5-base` on CPU with the E5 prefixes
(`passage:` / `query:`). Search is hybrid: the dense ranking and a BM25 ranking (lower case, Polish stop
words, 6-character prefix stemming) are fused by reciprocal rank fusion (k = 60); default top 8. Several
collections can be searched together (the corpus plus a session's attachments).

Indexing: [src/ingest_data.py](../src/ingest_data.py) with [src/extractors.py](../src/extractors.py) —
.pdf (per page), .docx (paragraphs and tables in order), .eml (headers, body, attached files), .xlsx (rows
per sheet, values), .txt, .md; chunks of 800 characters with 200 overlap (spreadsheet chunks start with
the column names); each chunk keeps its relative file path, page or sheet and an id `path#index:sha`.
Unreadable files are listed, not dropped. An unchanged corpus is not re-indexed.

## Attachments

[src/attachments.py](../src/attachments.py) (UI uploads and evaluation tasks): files are copied into the
session workspace (readable by sandboxed code) and their text is added to the message (up to 20,000
characters) or indexed into a per-session collection.

## Evaluation and tracing

[src/eval/](../src/eval/) implements [EVALUATION_INTERFACE.md](EVALUATION_INTERFACE.md).
[src/tracing.py](../src/tracing.py) records events when a recorder is attached to the run: nodes are
wrapped at graph build time (`node_start`/`node_end`, deadline check), every model goes through
`TracedModel` in `get_llm` (`llm_call`, full messages in `blobs/`), tool functions carry `@traced_tool`,
and retrieval, sandbox runs, approvals and the plan emit their own events. Without a recorder (the UI)
tracing does nothing.

## Sandbox

[src/sandbox.py](../src/sandbox.py): every code run or LaTeX compilation is a new container from
`hapilot-sandbox` ([sandbox/Dockerfile](../sandbox/Dockerfile)) with `network_mode=none`, the invoking
non-root user, `cap_drop=ALL`, `no-new-privileges`, read-only root filesystem, `/tmp` tmpfs, memory /
CPU / process limits and a timeout; the session workspace is mounted read-write at `/work`, the company
documents (the evaluation corpus in eval runs) read-only at `/data`. The coder gets the list of these files
and the results of earlier plan steps. The image is never pulled at run time.

## Serving models

[tools/modelswap.py](../tools/modelswap.py) exposes one OpenAI-compatible endpoint on 127.0.0.1 and
starts `llama-server` for the requested model (from `config/models.local.yaml`), stopping the previous
one; requests are handled one at a time. [scripts/hapilotctl.py](../scripts/hapilotctl.py) starts and
stops the proxy and the UI, records their PIDs, and runs setup, indexing, smoke tests and the egress test.

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
            knowledge, writer, chat, hitl_confirm, operations,
            summarize_file, edit_file, add_to_knowledge ── next step's node, or __end__
```

- **task_planner** ([src/agent_graph.py](../src/agent_graph.py)) asks the `task_planner` role for a JSON
  plan (`task_name`, `complexity`, `steps[{id, action, tool, input}]`). The system prompt is
  [src/core/prompts/task_planner.txt](../src/core/prompts/task_planner.txt); the list of tools with their
  descriptions (`KIEDY` / `KIEDY NIE` / `PRZYKŁAD`) is built from the manifests of the loaded extensions
  ([`planner_catalog`](../src/agent_tools.py)), so a disabled extension's tools are never offered.
  Deterministic guards then apply (each only when its tool is loaded). They read the user's own words,
  not the text of attached files:
  - an edit request (`zmień`, `popraw`, …, matched on word boundaries) becomes a single
    `modify_document` step — only if a document was generated earlier in the session and the message
    does not bring a new attachment (then the request is about that file);
  - `template_pdf` steps are removed unless the user asked for a document (`pdf`, `podanie`, `raport`,
    `protokół`, …) and did not say otherwise (`nie rób PDF`, `bez PDF`, `tylko tekst`);
  - an explicit request to create a document (a creation verb with a document noun or with "PDF", e.g.
    `przygotuj pismo w PDF`) gets a `template_pdf` step at the end if the plan has none;
  - a `modify_document` step next to other steps is dropped when no document was generated in the session
    or the message brings a new attachment (it could only fail);
  - a `hitl_confirm` step at the end of a plan is dropped (nothing follows it to confirm, and its message
    would replace the last real result);
  - under the `ask` approval policy, plans longer than two steps wait for the user's approval.
- **router** maps the current step's tool (or one of its aliases) to the node that provides it, without
  calling a model; unknown tool or no plan → `chat`. Extension hooks (`on_before_route`) may redirect.
- The edges after each node come from its manifest (`then`, or `after.policy`: `plan`, `on_success`,
  `retry`), see [extension_development.md](extension_development.md#edges).
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

Extensions are folders with a manifest `hapilot_extension.json` (version 1, schema
[docs/schemas/extension.schema.json](schemas/extension.schema.json)), loaded by
[src/core/extension_manager.py](../src/core/extension_manager.py) from `extensions/` and the folders in
`HAPILOT_EXTENSIONS_DIRS`. Manifests are validated, entry points imported and names and edges checked
before anything is registered; an extension that fails is skipped with a reason
(`python -m src.core.extension_manager` lists tools, warnings and errors). How to write one:
[extension_development.md](extension_development.md). The built-in extensions:

| Node | Extension | What it does |
|---|---|---|
| `knowledge` | `rag_knowledge` | rewrites the question into search terms, searches the knowledge base, answers with sources |
| `writer` | `writer` | text synthesis; e-mail steps produce drafts only |
| `chat` | `chat` | conversation |
| `document_generator` | `document_generator` | picks a template, fills its parameters; form (ask) or direct PDF (auto) |
| `modify_document` | `document_modifier` | edits a document generated in this session (via its saved parameters) |
| `coder`, `execute` | `python_coder` | writes Python, runs it in the sandbox, retries with the error |
| `hitl_confirm` | core | confirmation step (auto-confirmed under `auto`) |
| `summarize_file` | `file_tools` | summary of a whole attached file; long files part by part, then combined |
| `edit_file` | `file_tools` | changed copy of an attached .docx/.txt/.md (.pdf/.eml → .docx); Word page set-up, styles, lists, tables kept |
| `add_to_knowledge` | `file_tools` | adds an attached file to the company knowledge base after confirmation (never in evaluation runs) |
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
The templates use TeX Gyre Termes (Times-like, sandbox image 0.2) in the T1 encoding with a Unicode map (`glyphtounicode`), so text
copied from a PDF keeps its Polish letters. A report whose sections are short together
(`compact_below_chars` in templates.json) is laid out without title page and table of contents.

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
PDF pages without a text layer (scans: fewer than 25 non-space characters) are read by OCR
([src/ocr.py](../src/ocr.py)): rendered at 300 dpi in greyscale (pypdfium2) and read by Tesseract (`pol+eng`) in a
container of `hapilot-ocr` ([sandbox/ocr.Dockerfile](../sandbox/ocr.Dockerfile)) with the sandbox's isolation, one
container per document; results are cached per document and page in `HAPILOT_DATA_DIR/ocr_cache`, and their chunks
are marked `ocr`. Attachments go through the same extraction. What the OCR can do (off, unavailable, image and
languages) is part of the corpus fingerprint, so the index is rebuilt when it changes.
Unreadable files are listed, not dropped. An unchanged corpus is not re-indexed. Files added from the chat
live in `HAPILOT_ADDED_DOCS_DIR` and are indexed with the corpus under the source prefix `dodane/`
(`add_documents` indexes a new one at once and updates the corpus fingerprint).

## Attachments

[src/attachments.py](../src/attachments.py) (UI uploads and evaluation tasks): files are copied into the
session workspace (readable by sandboxed code) and their text is added to the message (up to 20,000
characters) or indexed into a per-session collection. The message lists the attached files
(`Załączone pliki …: a.pdf, b.docx`); the file tools find "the attached file" from that list
(`attachments.pick`: the files the request names, else those of the latest message with attachments).

## Evaluation and tracing

[src/eval/](../src/eval/) implements [EVALUATION_INTERFACE.md](EVALUATION_INTERFACE.md).
[src/tracing.py](../src/tracing.py) records events when a recorder is attached to the run: nodes are
wrapped at graph build time (`node_start`/`node_end`, deadline check), every model goes through
`TracedModel` in `get_llm` (`llm_call`, full messages in `blobs/`), tool functions carry `@traced_tool`,
and retrieval, sandbox runs, approvals and the plan emit their own events. Without a recorder (the UI)
nothing is written. A run may also carry an `observer` callable that receives every event: the UI uses
it for its side panel **📎 Źródła i pliki** ([src/ui_panel.py](../src/ui_panel.py),
[src/source_view.py](../src/source_view.py)). The panel lists the documents whose chunks went into the
prompts of a request (cited first: those the answer names by path or file name) and shows each one whole —
PDF pages rendered by pypdfium2 with boxes over the chunk text, other formats as the extracted text with
the chunks marked — and the files that the request created or changed (workspace snapshot before and
after; unified diff for text files). Page images and downloads are served at `/hapilot/panel/{token}`; a
token is random, bound to one session and one file, and forgotten when the chat ends.

The agent panel ([src/admin.py](../src/admin.py), routes in [src/ui_admin.py](../src/ui_admin.py)) adds
custom nodes per user: ordinary extensions under `var/custom_extensions/<owner>/<node>/` whose node calls
[src/core/custom_node.py](../src/core/custom_node.py) (one model call with the node's prompt). Each logged-in
user's requests run on their own compiled graph (`admin.agent_for`: built-in extensions + global custom nodes +
the user's private ones; cached, rebuilt when those files change), and the run config carries that graph's
extension manager, which `get_active()` returns inside the run. Administrator prompt overrides of built-in nodes
sit on top of the prompt registry (`set_override`). None of this is loaded by evaluation runs.

Document templates ([src/doc_templates.py](../src/doc_templates.py)) follow the same rules. `catalog(user)` gives a
run its templates: the repository's (`templates/`), administrator edits of them (`var/template_overrides/`), global
and the user's own (`var/custom_templates/<owner>/<id>/`); the run config carries `user`, and
`agent_tools._load_templates_metadata()` returns that catalog, so the document node, the form and regeneration all
see the same list. Without a user (evaluation, no login) the catalog is exactly `templates/`.

Signatures ([src/signatures.py](../src/signatures.py)): `tool_generate_pdf_from_template(..., signature=)` replaces
`{{PODPIS_SLOT}}` with a fixed-size box, blank or holding the user's scan (copied into the chat's workspace for the
sandbox). The choice comes only from the document form (`_signature`); a key of that name in model output is
dropped, and the scan is looked up by the run's `user`, so nobody can sign with another user's scan.

## Sandbox

[src/sandbox.py](../src/sandbox.py): every code run or LaTeX compilation is a new container from
`hapilot-sandbox` ([sandbox/Dockerfile](../sandbox/Dockerfile)) with `network_mode=none`, the invoking
non-root user, `cap_drop=ALL`, `no-new-privileges`, read-only root filesystem, `/tmp` tmpfs, memory /
CPU / process limits and a timeout; the session workspace is mounted read-write at `/work`, the company
documents (the evaluation corpus in eval runs) read-only at `/data`. The coder gets the list of these files
and the results of earlier plan steps. The image is never pulled at run time. OCR containers (`hapilot-ocr`) run
through the same function with the same options; they see only a temporary folder of page images.

## Serving models

[tools/modelswap.py](../tools/modelswap.py) exposes one OpenAI-compatible endpoint on 127.0.0.1 and
starts `llama-server` for the requested model (from `config/models.local.yaml`), stopping the previous
one; requests are handled one at a time. [scripts/hapilotctl.py](../scripts/hapilotctl.py) starts and
stops the proxy and the UI, records their PIDs, and runs setup, indexing, smoke tests and the egress test.
For installs used by a team it also binds the UI to the local network with HTTPS and follows a git branch
(`update`: back up, test the new commit in a worktree, switch, restart, roll back on failure); the UI records
running chat handlers in `run/ui_activity.json` ([src/ui_activity.py](../src/ui_activity.py)) so that a restart
waits until nobody is mid-answer ([deployment.md](deployment.md)).

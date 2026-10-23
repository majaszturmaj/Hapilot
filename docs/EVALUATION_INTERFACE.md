# Hapilot evaluation interface (v1)

**Status: approved 2026-10-18 (open points of §8 decided as recommended: JSON via a formatter call, the agent sees
the answer format, `sources_used` = files in prompts + attachments, with files cited in the answer reported
separately, 900 s per task, full texts in traces, attachments inline up to 20,000 characters). Implemented in
`src/eval/` (schemas in `docs/schemas/`); verified on the GPU host on 2026-10-18 and frozen as tag `eval-v1`.**

This is how an external benchmark harness runs Hapilot without the UI: it gives Hapilot tasks and a
document corpus, and gets back answers, structured JSON, the sources used, and a complete trace of
what the agent did (plan, model calls, tool calls, retrievals, files, approvals). The trace is meant
for Tool Call Accuracy, Agent Goal Accuracy, LLM-as-a-judge and RAG-triad scoring by the harness;
Hapilot itself does not score anything.

Principles:
- **Same agent as in the UI.** The runner calls the same graph, nodes, prompts and tools; there is no
  code path that looks at task ids or task content.
- **Isolation.** Every task gets a fresh session: new workspace, empty conversation, no state carried
  over. Only the corpus index is shared (read-only).
- **Local only.** Model endpoints must be local/LAN (as everywhere in Hapilot); the egress guard is on
  during runs and its log is part of the output.
- **Versioned output.** Every file carries a schema id (`hapilot.run/1`, `hapilot.result/1`,
  `hapilot.trace/1`, `hapilot.task/1`); JSON Schemas live in `docs/schemas/`.
- **Reproducible.** Every run records the Hapilot commit/tag, a hash of the effective configuration,
  the corpus fingerprint, library and model-server versions, and the date used as "today".

## 1. Inputs

### 1.1 Tasks — `tasks.jsonl` (schema `hapilot.task/1`, one task per line)

```json
{"id": "T001",
 "instruction": "Ile dni przed rozpoczęciem urlopu trzeba złożyć wniosek? Podaj podstawę.",
 "answer_format": {"description": "Obiekt JSON z polami:",
                   "fields": {"odpowiedz": "liczba dni (liczba całkowita)",
                              "zrodla": "lista nazw plików, na których opiera się odpowiedź"}},
 "attachments": ["attachments/T001/wniosek.docx"]}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | unique, `[A-Za-z0-9._-]{1,64}` (used in file names) |
| `instruction` | yes | the user's request in Polish, passed to the agent verbatim |
| `answer_format` | no | the JSON object the final answer must contain: `fields` maps field name → description; optional `description` (free text) and `schema` (a JSON Schema the object must satisfy) |
| `attachments` | no | files the user "attaches" to the request; paths relative to the tasks file or absolute |

### 1.2 Run configuration (CLI flags or `run.yaml`)

| Setting | Default | Meaning |
|---|---|---|
| `corpus_dir` | required | company documents the agent may search (.pdf, .docx, .eml, .xlsx, .txt, .md, recursively) |
| `today` | required | the date the agent treats as today (`YYYY-MM-DD`): in prompts and in tools |
| `profile` | `best-local` | model profile from `config/profiles.yaml` |
| `seed` | `42` | sent with every model request |
| `temperature` | `0` | overrides the profile for every role |
| `approval_policy` | `auto` | `auto`: plans run without approval, document parameters are taken as generated, confirmation steps pass; nothing is ever sent or created outside the workspace |
| `task_timeout_s` | `900` | per task; checked before every node and model call (model calls get the remaining time as their timeout) |
| `out_dir` | required | output directory (see §2) |
| `resume` | `true` | skip tasks already present in `results.jsonl` (`--retry-failed` re-runs `error`/`timeout`) |
| `answer_mode` | `formatter` | how JSON is produced when `answer_format` is given (§3) |

The corpus is indexed once per run into an isolated collection named after the corpus fingerprint
(`eval_<first 12 hex>`), under `HAPILOT_DATA_DIR/eval_index/`; a later run with the same corpus reuses
it. The UI's knowledge base is not touched.

## 2. Outputs

```text
<out_dir>/
├── run.json                  hapilot.run/1 — run metadata (written at start, completed at end)
├── results.jsonl             hapilot.result/1 — one line per finished task, appended and flushed per task
├── traces/<task_id>.jsonl    hapilot.trace/1 — ordered events of one task
├── blobs/<sha256>.json       full model inputs/outputs, content-addressed, referenced from traces
├── workspaces/<task_id>/     files the task created (PDF, .tex, scripts, charts) and its attachments
└── egress.jsonl              non-local connection attempts blocked by the guard (expected: empty)
```

### 2.1 `results.jsonl` (schema `hapilot.result/1`)

```json
{"schema": "hapilot.result/1", "run_id": "20261020-101500-ab12cd", "task_id": "T001",
 "status": "ok", "error": null,
 "answer_text": "Wniosek trzeba złożyć co najmniej 14 dni kalendarzowych …",
 "answer_json": {"odpowiedz": 14, "zrodla": ["regulamin_urlopow.pdf"]},
 "answer_json_valid": true, "answer_json_error": null,
 "sources_used": ["kadry/regulamin_urlopow.pdf"],
 "sources_cited": ["kadry/regulamin_urlopow.pdf"],
 "files_created": [{"path": "letter_20261020_101533.pdf", "bytes": 17474, "sha256": "…"}],
 "plan_tools": ["rag_search", "llm_synthesis"],
 "duration_s": 21.4, "llm_calls": 4, "prompt_tokens": 6120, "completion_tokens": 410,
 "trace": "traces/T001.jsonl"}
```

| Field | Meaning |
|---|---|
| `status` | `ok` (the graph finished), `error` (exception; message in `error`), `timeout` |
| `answer_text` | the agent's final message to the user, verbatim |
| `answer_json`, `answer_json_valid` | only with `answer_format`: the parsed object, and whether it is a JSON object with every field in `fields` (and valid against `schema` if given) |
| `sources_used` | corpus files (path relative to `corpus_dir`) whose text was put into a model prompt during the task, plus corpus files that executed Python code named by path (`/data/<file>`), plus attachments. In v1 every retrieved chunk goes into a prompt, so the first part equals the files of all retrieved chunks; the per-chunk detail is in the trace |
| `sources_cited` | corpus files named in list-valued fields of `answer_json` (matched by path or base name) — what the answer itself claims as its sources |
| `files_created` | files in the task workspace at the end that were not attachments |
| `plan_tools` | tools of the executed plan, in order (detail in the trace) |

### 2.2 Trace events — `traces/<task_id>.jsonl` (schema `hapilot.trace/1`)

Every line: `{"schema": "hapilot.trace/1", "run_id", "task_id", "seq", "ts", "type", …}`; `seq` is
strictly increasing within a task. Types:

| `type` | Fields |
|---|---|
| `task_start` | `session_id`, `instruction`, `answer_format`, `attachments` (name, bytes, sha256) |
| `plan` | `task_plan` as produced by the planner, after the guards (`steps[{id, action, tool, input}]`, `complexity`), `guards_applied` |
| `node_start` / `node_end` | `node`, `plan_step` (index or null), `duration_ms`, `status`, `next` |
| `llm_call` | `call_id`, `node`, `role`, `model`, `base_url`, `params` (temperature, max_tokens, seed, extra_body), `messages_ref` (sha256 of the full request messages in `blobs/`), `response_ref`, `prompt_tokens`, `completion_tokens`, `latency_ms`, `finish_reason`, `error` |
| `tool_call` | `name` (`rag_search`, `execute_python`, `generate_pdf_from_template`, `compile_latex`, `modify_document`, `read_file`), `depth` (0 = called by a node, 1 = called by another tool), `args` (as passed; long strings stored as blob refs), `result_summary` (≤ 500 chars), `duration_ms`, `exception` |
| `model_fallback` | `role`, `model` — the role's own model was not served and the profile's default model was used |
| `attachments` | `files` (name, bytes, sha256, chars, error), `inlined`, `collection` |
| `retrieval` | `query`, `method` (`hybrid`), `k`, `chunks[{rank, file, chunk_id, page, score, score_dense, score_bm25, text}]` |
| `code_data_access` | `files` — corpus files that generated code names by path (the sandbox sees the corpus read-only at `/data`) |
| `sandbox_run` | `command`, `exit_code`, `timed_out`, `network_mode`, `duration_ms` |
| `file_created` | `path`, `bytes`, `sha256` |
| `approval` | `kind` (`plan`, `document_form`, `confirm_step`, `email_draft`), `policy`, `decision` (`auto_approved`), `summary` |
| `answer_format` | `mode`, `valid`, `error`, `attempts` (the formatter's model calls are `llm_call` events with role `answer_formatter`) |
| `task_end` | `status`, `error`, `duration_s`, totals (`llm_calls`, tokens) |

Blobs: `blobs/<sha256>.json` contains `{"messages": [{"role", "content"}…]}` for requests and
`{"content", "reasoning_content", "usage", "finish_reason"}` for responses; identical prompts are stored once.

Model calls are recorded by Hapilot's own model wrapper (every call goes through `get_llm`), tool calls
by a decorator on the tool functions, and node transitions by the runner — not by LangChain callbacks,
so nothing depends on callback propagation.

### 2.3 `run.json` (schema `hapilot.run/1`)

```json
{"schema": "hapilot.run/1", "run_id": "20261020-101500-ab12cd",
 "started": "2026-10-20T10:15:00+02:00", "ended": "2026-10-20T11:02:13+02:00",
 "hapilot": {"commit": "…", "describe": "eval-v1", "dirty": false},
 "config": {"profile": "best-local", "config_hash": "sha256:…", "today": "2026-10-14", "seed": 42,
            "temperature": 0, "approval_policy": "auto", "task_timeout_s": 900, "answer_mode": "formatter",
            "retrieval": {"method": "hybrid", "k": 8, "rrf_k": 60}, "chunking": {"size": 800, "overlap": 200}},
 "models": [{"name": "qwen3.6-27b-q4km", "file": "Qwen3.6-27B-Q4_K_M.gguf", "bytes": 16523460000,
             "server": "llama.cpp build …"}],
 "embedding_model": {"name": "intfloat/multilingual-e5-base", "revision": "…"},
 "corpus": {"fingerprint": "sha256:…", "files": 120, "chunks": 1480, "collection": "eval_…", "skipped": []},
 "libraries": {"python": "3.12.3", "langgraph": "1.0.8", "langchain-core": "1.2.7", "chromadb": "1.4.1", "…": "…"},
 "gpu": "NVIDIA GeForce RTX 4090", "egress_guard": true,
 "tasks": {"total": 40, "ok": 39, "error": 0, "timeout": 1}}
```

`config_hash` covers the resolved profile, the run settings, retrieval and chunking parameters, the
embedding model, the sandbox image id, the OCR state and image id, the text of all prompts and templates, and the
corpus fingerprint —
two runs with the same hash ran the same configuration on the same corpus (the code itself is identified
by `hapilot.commit`). Resuming into a directory whose `run.json` has a different hash or a different
Hapilot commit is refused, so one directory never mixes configurations or code versions. No host names or user paths are recorded (model files by base name only).

## 3. Producing the JSON answer

When `answer_format` is given:

1. The agent sees it: the user message is the instruction followed by the format description, exactly as
   given (`"\n\nOczekiwany format odpowiedzi (JSON): …"`), as a user would type it.
2. **`answer_mode: formatter` (default):** after the graph finishes, one extra model call (role
   `answer_formatter`, same profile) receives the instruction, the format and the agent's final answer
   with the tool results of the task, and must return only the JSON object; if it does not parse or a
   field is missing, it is asked once more with the error. The call is in the trace (`answer_format`).
   `answer_text` stays the agent's own answer.
3. **`answer_mode: parse`:** no extra call; the first JSON object in the agent's final answer is used.

## 4. Attachments

Attachments go through `src/attachments.py`, the same module the UI will use for uploaded files:
the file is copied into the task workspace (so Python code in the sandbox can read it, e.g. an .xlsx),
its text is extracted with the same extractors as the corpus, and — up to 20,000 characters in total —
added to the user message as `[Załącznik: <name>]` blocks. Longer attachments are indexed into a
per-task collection that the knowledge search queries together with the corpus.

## 5. Corpus formats and retrieval

| Format | Extraction | Chunk metadata |
|---|---|---|
| .pdf | pypdf, per page; pages without a text layer (scans) by OCR — Tesseract `pol+eng` in the isolated `hapilot-ocr` container, cached ([src/ocr.py](../src/ocr.py)) | `page`; `ocr` on scanned pages |
| .docx | python-docx: paragraphs and tables (cells joined with ` \| `) | — |
| .eml | Python `email`: From, To, Cc, Date, Subject headers + plain-text body (HTML converted to text); names of attached files listed, text of attached .pdf/.docx/.txt extracted | — |
| .xlsx | openpyxl (values, not formulas): per sheet, rows as ` \| `-separated lines with the header row | `sheet` |
| .txt, .md | as is (UTF-8, fallback cp1250) | — |

Files that cannot be read are listed in `run.json` (`corpus.skipped`) — never silently dropped.

Retrieval for the agent becomes **hybrid**: BM25 over the same chunks (Polish-aware tokenisation: lower
case, prefix stemming) fused with the E5 vector search by reciprocal rank fusion; default k = 8.
These defaults are tuned only on Hapilot's own fixtures, never on the benchmark.

## 6. Commands

```bash
# on the GPU host, with the model proxy running (hapilotctl.py models up --profile best-local)
.venv/bin/python -m src.eval run --tasks tasks.jsonl --corpus /path/to/docs --today 2026-10-14 \
    --profile best-local --out runs/2026-10-20_best-local
.venv/bin/python -m src.eval validate runs/2026-10-20_best-local     # schema check of all output files
.venv/bin/python scripts/hapilotctl.py eval --tasks … --corpus … --today … --profile …   # models up + run + models down
```

Python API (one process, many tasks):

```python
from src.eval import EvalSession, RunConfig, Task

with EvalSession(RunConfig(corpus_dir="docs", out_dir="runs/x", today="2026-10-14", profile="best-local")) as run:
    result = run.run_task(Task(id="T001", instruction="…", answer_format={"fields": {"odpowiedz": "…"}}))
```

Exit codes of `run`: 0 — all tasks finished (`ok`); 1 — some tasks `error`/`timeout`; 2 — configuration
error (nothing run).

## 7. Versioning and freeze

The interface is frozen at tag `eval-v1`. After that, behaviour changes go to later tags and
`CHANGELOG.md`; every trace and `run.json` records the commit/tag. A breaking change to an output file
bumps its schema id (`/2`).

## 8. Decisions taken at approval (2026-10-18)

1. JSON answers: `formatter` is the default; `parse` is available.
2. The agent sees `answer_format` appended to the instruction.
3. `sources_used` = files whose text entered a model prompt, plus attachments; files the answer itself cites
   (a list-valued field of `answer_json` whose items are corpus file names) are reported separately as `sources_cited`.
4. `task_timeout_s` = 900 by default.
5. Traces keep full chunk texts and full prompts (`blobs/`).
6. Attachments are inlined up to 20,000 characters in total; longer ones get a per-task index.

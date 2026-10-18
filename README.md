# Hapilot

A local multi-agent assistant for office work in Polish companies: it answers questions from company
documents, writes letters, reports and meeting minutes as PDF, edits them, and runs Python for
calculations — with local language models, on your own hardware.

- **Local by design.** Models run on your GPU host; the knowledge base is embedded; generated code and
  LaTeX run in a throwaway container without network. Model endpoints must be local/LAN addresses.
- **Orchestrated with LangGraph.** A planner splits a request into steps; each step goes to a
  specialised node (knowledge search, writer, document generator, Python coder, …).
- **Extensible.** Capabilities are extensions: a folder with a JSON manifest and Python nodes. The planner, router
  and graph are built from the manifests; a new tool needs no change to the core (example:
  [examples/extensions/vat_calculator](examples/extensions/vat_calculator)).

> Status: under active development (version 0.1). See [CHANGELOG.md](CHANGELOG.md).

---

## How it works

```text
            Chainlit UI (127.0.0.1, open it through an SSH tunnel)
                                   │
                           ┌───────▼───────┐
                           │ task_planner  │  LLM → JSON plan of steps (+ guards, plan approval)
                           └───────┬───────┘
                           ┌───────▼───────┐
                           │    router     │  no LLM: step tool → node
                           └───────┬───────┘
   ┌──────────────┬──────────────┬─┴───────────┬────────────────────┬──────────────────┐
knowledge       writer          chat     document_generator   modify_document     coder → execute
(RAG search)  (text, e-mail   (conver-   (LaTeX template,      (edit a document    (Python in the
               drafts)         sation)    review form, PDF)     from this session)  sandbox, retries)
   └──────────────┴──────────────┴─────────────┴────────────────────┴──────────────────┘
                                   │  next plan step, or end
```

| Part | What it is |
|---|---|
| Orchestration | LangGraph graph built from the loaded extensions ([src/agent_graph.py](src/agent_graph.py)) |
| Models | Any OpenAI-compatible local server; roles mapped to models by **profiles** ([config/profiles.yaml](config/profiles.yaml)) |
| Model serving | [tools/modelswap.py](tools/modelswap.py): one local endpoint that loads one llama.cpp model at a time |
| Knowledge base | ChromaDB embedded in the Hapilot process; hybrid search: `intfloat/multilingual-e5-base` vectors (CPU) and BM25, fused by reciprocal rank fusion; .pdf, .docx, .eml, .xlsx, .txt, .md |
| Documents | LaTeX templates (letter, report, protocol) in [templates/](templates/), compiled to PDF in the sandbox |
| Sandbox | Fresh Docker container per run: no network, non-root, read-only root filesystem, CPU/memory/process limits, timeout; the session's workspace is the only writable mount, company documents are readable at `/data` ([src/sandbox.py](src/sandbox.py)) |
| UI | Chainlit, per-session workspace and model profile, optional password login |

Human in the loop: under the default `ask` policy Hapilot asks before running plans longer than two
steps, shows document parameters in a form before generating a PDF, and stops at confirmation steps.
Scripts and evaluation runs use the `auto` policy.

## Model profiles

| Profile | Roles | Notes |
|---|---|---|
| `reference` | Bielik-11B v3 (Q8_0) for language roles, Qwen3-Coder-30B-A3B (Q4_K_M) for Python | The two models do not fit together on a 24 GB GPU; the proxy swaps them |
| `best-local` | Qwen3.6-27B (Q4_K_M, reasoning off) for all roles | One model, no swapping |

Measured on an RTX 4090 (24 GB) on 2026-10-18 with llama.cpp, q8_0 KV cache: VRAM Bielik-11B Q8_0 (32k context)
15,220 MiB, Qwen3-Coder-30B-A3B Q4_K_M (16k) 18,964 MiB, Qwen3.6-27B Q4_K_M (32k) 17,016 MiB. A swap takes 2–3 s when
the model file is in the page cache (first load from disk: 52 s for Bielik, 120 s for Qwen3-Coder). The seven smoke
scenarios took 82.6 s in total with `best-local` and 138.3 s with `reference` (all passed).

The default profile is `best-local` (set in `config/profiles.yaml`); users choose a profile per chat session in the
⚙️ panel. Adding a model is a configuration change (profiles + `config/models.local.yaml`), not a UI action.

## Privacy

What Hapilot does to keep data on your machines:

- model endpoints are checked against a local/LAN allowlist (`src/config.py`); there is no cloud model path;
- Hugging Face, Chroma and LangChain telemetry and downloads are switched off at run time (`src/privacy.py`);
  the embedding model is downloaded once during setup;
- the sandbox has no network; the UI, model proxy and optional n8n listen on 127.0.0.1 only;
- each chat session has its own workspace; files are never served across sessions.

The claim is tested, not only stated: `scripts/hapilotctl.py egress-test` runs the scenario suite with
the model server, the UI, indexing and the agent under `strace`, blocks non-local connections from
Hapilot's Python process, and fails on any outbound connection attempt (including DNS).
**Last egress test: passed on 2026-10-18, commit `62a949e`, profile `best-local`** (earlier the same day also on
`0752d4f` with both profiles; 7/7 scenarios each;
traced: model server, UI start-up and page load, indexing, agent runs; no outbound connection, no DNS lookup; every
sandbox run had network mode `none`). Not covered by the trace: the Docker daemon itself (root) and a full browser chat
session over the UI's websocket (the agent code it runs is the same as in the scenarios).

The optional n8n extension (Outlook e-mail drafts and calendar) is outside this claim: n8n talks to
Office 365 by design. It is disabled by default.

## Running on a GPU host

Requirements: Linux with an NVIDIA GPU, Python 3.10+, Docker with Compose, llama.cpp's `llama-server`, GGUF model
files, `strace` (for the egress test). Verified on: RTX 4090 24 GB, Ubuntu, Python 3.12, Docker 29, llama.cpp from
LM Studio's CUDA 12 backend.

```bash
git clone https://github.com/majaszturmaj/Hapilot.git ~/Hapilot && cd ~/Hapilot
python3 -m venv .venv
.venv/bin/pip install torch==2.10.0 --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/hapilotctl.py setup     # sandbox image, embedding model, config/models.local.yaml
```

Edit `config/models.local.yaml` (paths to `llama-server` and the GGUF files), then:

```bash
.venv/bin/python scripts/hapilotctl.py up --profile best-local   # model proxy + knowledge base + UI
.venv/bin/python scripts/hapilotctl.py status
.venv/bin/python scripts/hapilotctl.py down
```

`hapilotctl` only stops processes it started itself (recorded in `var/run/`), refuses to start models
while another process uses the GPU, and refuses ports that are taken.

### Opening the UI

Everything binds to 127.0.0.1 on the GPU host. From your computer:

```bash
ssh -N -L 8601:127.0.0.1:8601 <user>@<gpu-host>
```

then open http://localhost:8601. For shared use, enable the login: `hapilotctl.py add-user NAME` and
set `HAPILOT_UI_USERS_FILE` in `.env`.

### Ports

| Port (127.0.0.1) | Service | Variable |
|---|---|---|
| 8601 | Chainlit UI | `HAPILOT_UI_PORT` |
| 8611 | Model swap proxy (OpenAI-compatible) | `HAPILOT_MODELSWAP_PORT` |
| 8612 | llama-server started by the proxy | `HAPILOT_BACKEND_PORT` |
| 8678 | n8n (optional, compose profile `n8n`) | `HAPILOT_N8N_PORT` |

### Your documents

Put company documents (.pdf, .docx, .eml, .xlsx, .txt, .md) in a folder, set `HAPILOT_DOCS_DIR` in `.env`, and run
`hapilotctl.py ingest`. The default is the fictional demo company in
[examples/demo_company/](examples/demo_company/). `data/private_docs/` is ignored by git.

## Evaluation (headless)

Benchmark harnesses run Hapilot without the UI through the evaluation interface
([docs/EVALUATION_INTERFACE.md](docs/EVALUATION_INTERFACE.md)): tasks in JSONL, a document corpus, the date to
treat as today and a profile in; answers, JSON answers, sources, created files and a full trace per task
(plan, every model and tool call, every retrieval with chunk texts, sandbox runs, approvals) out.

```bash
.venv/bin/python scripts/hapilotctl.py eval --tasks tests/eval/dev_tasks.jsonl --corpus examples/demo_company/docs \
    --today 2026-10-14 --profile best-local --out var/runs/dev
.venv/bin/python -m src.eval validate var/runs/dev
```

## Configuration

All settings are environment variables read by [src/config.py](src/config.py); see
[.env.example](.env.example) for the full list with defaults.

## Development

Unit tests need no model, GPU or network (a guard fails any test that tries to connect out):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest
.venv/bin/ruff check src extensions examples tests scripts tools
```

Smoke scenarios with real models (GPU host): `.venv/bin/python scripts/hapilotctl.py smoke --profile best-local`
([tests/scenarios/smoke.yaml](tests/scenarios/smoke.yaml)).

Extensions: see [docs/extension_development.md](docs/extension_development.md). Architecture details:
[docs/architecture.md](docs/architecture.md). Optional MCP servers exposing the knowledge base, templates
and n8n operations: `python -m src.knowledge_server`, `src.template_server`, `src.operations_server`.

## License

[MIT](LICENSE) © 2026 Maja Szturmaj

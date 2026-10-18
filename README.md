# Hapilot

A local multi-agent assistant for office work in Polish companies: it answers questions from company
documents, writes letters, reports and meeting minutes as PDF, edits them, and runs Python for
calculations — with local language models, on your own hardware.

- **Local by design.** Models run on your GPU host; the knowledge base is embedded; generated code and
  LaTeX run in a throwaway container without network. Model endpoints must be local/LAN addresses.
- **Orchestrated with LangGraph.** A planner splits a request into steps; each step goes to a
  specialised node (knowledge search, writer, document generator, Python coder, …).
- **Extensible.** Capabilities are extensions in `extensions/<name>/` with a JSON manifest.

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
| Knowledge base | ChromaDB embedded in the Hapilot process, `intfloat/multilingual-e5-base` embeddings on CPU |
| Documents | LaTeX templates (letter, report, protocol) in [templates/](templates/), compiled to PDF in the sandbox |
| Sandbox | Fresh Docker container per run: no network, non-root, read-only root filesystem, CPU/memory/process limits, timeout, only the session's workspace mounted ([src/sandbox.py](src/sandbox.py)) |
| UI | Chainlit, per-session workspace and model profile, optional password login |

Human in the loop: under the default `ask` policy Hapilot asks before running plans longer than two
steps, shows document parameters in a form before generating a PDF, and stops at confirmation steps.
Scripts and evaluation runs use the `auto` policy.

## Model profiles

| Profile | Roles | Notes |
|---|---|---|
| `reference` | Bielik-11B v3 (Q8_0) for language roles, Qwen3-Coder-30B-A3B (Q4_K_M) for Python | The two models do not fit together on a 24 GB GPU; the proxy swaps them |
| `best-local` | Qwen3.6-27B (Q4_K_M, reasoning off) for all roles | One model, no swapping |

The default profile is set in `config/profiles.yaml`; users choose a profile per chat session in the
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
**Last egress test: not yet run on the GPU host** (the date and commit of the last passing run will be recorded here).

The optional n8n extension (Outlook e-mail drafts and calendar) is outside this claim: n8n talks to
Office 365 by design. It is disabled by default.

## Running on a GPU host

Requirements: Linux with an NVIDIA GPU (tested target: RTX 4090, 24 GB), Python 3.10+, Docker with
Compose, llama.cpp's `llama-server`, GGUF model files, `strace` (for the egress test).

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

Put company documents (.txt, .md, .pdf) in a folder, set `HAPILOT_DOCS_DIR` in `.env`, and run
`hapilotctl.py ingest`. The default is the fictional demo company in
[examples/demo_company/](examples/demo_company/). `data/private_docs/` is ignored by git.

## Configuration

All settings are environment variables read by [src/config.py](src/config.py); see
[.env.example](.env.example) for the full list with defaults.

## Development

Unit tests need no model, GPU or network (a guard fails any test that tries to connect out):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest
.venv/bin/ruff check src extensions tests scripts tools
```

Smoke scenarios with real models (GPU host): `.venv/bin/python scripts/hapilotctl.py smoke --profile best-local`
([tests/scenarios/smoke.yaml](tests/scenarios/smoke.yaml)).

Extensions: see [docs/extension_development.md](docs/extension_development.md). Architecture details:
[docs/architecture.md](docs/architecture.md). Optional MCP servers exposing the knowledge base, templates
and n8n operations: `python -m src.knowledge_server`, `src.template_server`, `src.operations_server`.

## License

[MIT](LICENSE) © 2026 Maja Szturmaj

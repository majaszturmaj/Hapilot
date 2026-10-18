# Hapilot

An extensible multi-agent AI assistant platform.  
Uses **LangGraph** for orchestration, **local LLMs** (Bielik + Qwen) for reasoning and code generation, and **Docker** for isolated execution.

---

## Architecture

Hapilot is built on a **lightweight, extensible core** using a plugin-based architecture. All domain logic, prompts, and tools are decoupled into separate Extensions.

```text
                           ┌──────────────┐
                           │  Chainlit UI │
                           └──────┬───────┘
                                  │
                           ┌──────▼───────┐
                           │   Core Engine│
                           │(AgentState,  │
                           │ Ext. Manager)│
                           └──────┬───────┘
                                  │
                 ┌────────────────┼────────────────┐
                 │                │                │
          ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
          │  Extension  │  │  Extension  │  │  Extension  │
          │   (Chat)    │  │(PDF Writer) │  │  (Coder)    │
          └─────────────┘  └─────────────┘  └─────────────┘
                ... dynamically discovered ...
```

**Features of the Extensible Architecture:**
- **Zero-touch integration:** Add new capabilities by dropping a folder into `extensions/`. No modifications to core code required.
- **Dynamic Routing:** The router automatically discovers available extensions and dynamically builds its classification prompt.
- **Centralized Prompt Registry:** Extensions can override or provide prompts dynamically.

**LLM models:**
| Model | Default endpoint | Role |
|-------|-----------------|------|
| Bielik 11B v3.0 | `localhost:1234` (LM Studio) | Routing, knowledge, planning, template filling, text synthesis (writer), chat |
| Qwen3 Coder 30B | `localhost:1235` (second local server) | Python code generation only (auto-fallback to Bielik) |

---

## Folder Structure

```
hapilot/
├── src/                  # Core framework code
│   ├── app.py            #   Chainlit frontend
│   ├── agent_graph.py    #   Dynamic LangGraph builder
│   ├── llm_config.py     #   LLM endpoints configuration
│   └── core/             #   Core SDK & Abstractions
│       ├── extension_manager.py
│       ├── route_registry.py
│       ├── prompt_registry.py
│       └── base_extension.py
│
├── extensions/           # Plug-and-play feature modules
│   ├── chat/             #   Conversational handler
│   ├── python_coder/     #   Sandboxed code generation
│   ├── document_generator/ # PDF report creation
│   ├── document_modifier/  # Document updates
│   ├── rag_knowledge/    #   ChromaDB semantic search
│   ├── writer/           #   Text formatting & synthesis
│   └── n8n_operations/   #   Email and calendar integration
│
├── templates/            # LaTeX document templates
├── data/private_docs/    # RAG source documents
├── docker/               # Docker infrastructure
├── sandbox/              # Python/LaTeX execution sandbox
├── public/               # Chainlit UI assets (CSS, logos)
└── .chainlit/            # Chainlit framework config
```

---

## Extension Architecture

Hapilot is built as an extensible platform. Core routing, LLM handling, and graph orchestration are separated from domain-specific capabilities.

Capabilities like PDF generation, RAG, Python coding, and simple chat are all implemented as **Extensions**.

### How Extensions Work
Each extension lives in `extensions/<name>/` and must contain a `hapilot_extension.json` manifest.
The manifest declares:
- **Routes**: e.g., `A`, `B`, `C` mapping to intents.
- **Nodes**: LangGraph nodes that process the task.
- **Tools**: MCP-like tools that nodes can use (e.g., `python_sandbox`, `rag_search`).
- **Prompts**: Domain-specific prompts for the LLM.

### Managing Extensions
To disable an extension, simply change `"enabled": true` to `"enabled": false` in its `hapilot_extension.json`. The platform will automatically re-route requests and skip loading its tools/nodes.

For a full guide on building new extensions, see [docs/extension_development.md](docs/extension_development.md).

---

## Prerequisites

1. **Python 3.10+** with a virtual environment
2. **Docker** (with Compose v2)
3. **LM Studio** running Bielik model on `localhost:1234`
4. *(Optional)* Qwen Coder server on a remote machine — the system falls back to Bielik if unavailable

---

## Quick Start

### One-command startup

```bash
# Install Python dependencies (first time only)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt  # or install from your environment

# Copy and edit environment config
cp .env.example .env

# Start everything (Docker services + data ingestion + Chainlit UI)
./start_hapilot.sh
```

The UI will be available at **http://localhost:8080**.

### Manual startup

```bash
# 1. Start Docker infrastructure (ChromaDB + n8n)
docker compose -f docker/docker-compose.yml up -d

# 2. Start Python sandbox
cd sandbox && docker compose up -d && cd ..

# 3. Ingest RAG documents into ChromaDB (first time only)
./venv/bin/python src/ingest_data.py

# 4. Start Chainlit UI
chainlit run src/app.py --host 0.0.0.0 --port 8080
```

---

## Configuration

Copy `.env.example` → `.env` and adjust as needed. Key variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOCAL_LLM_URL` | `http://localhost:1234/v1` | Bielik LM Studio endpoint |
| `REMOTE_CODER_URL` | `http://127.0.0.1:1235/v1` | Qwen Coder endpoint |
| `CHROMA_HOST` / `CHROMA_PORT` | `localhost` / `8000` | ChromaDB vector database |
| `N8N_WEBHOOK_BASE` | `http://localhost:5678/webhook` | n8n automation webhooks |
| `PLAN_MODE` | `auto` | Task planning: `auto` / `explicit` / `off` |
| `EMBEDDING_MODEL_NAME` | `intfloat/multilingual-e5-base` | RAG embeddings model |

Model endpoints can also be changed at runtime through the Chainlit UI settings panel (persisted in `models_config.json`).

---

## Adding New Templates

1. Create a `.tex` file in `templates/` using `{{PARAMETER_NAME}}` placeholders
2. Add a metadata entry to `templates/templates.json` with parameter descriptions
3. The system auto-discovers new templates — no code changes needed

---

## n8n Integration (Optional)

The n8n service runs in Docker and exposes webhooks for:
- **Email drafts** — `POST /webhook/outlook-draft`
- **Calendar checks** — `POST /webhook/outlook-calendar`

These integrate with Office 365 via n8n workflows. To set up:

1. Start n8n: `docker compose -f docker/docker-compose.yml up -d n8n`
2. Open the n8n UI at `http://localhost:5679`
3. Import or create workflows that connect to your Office 365 account
4. The agent calls these via `src/operations_server.py`

> **Note:** n8n is not required for core functionality (RAG, PDF generation, code execution, chat). It only enables email and calendar features.

---

## Testing


See `docs/test_prompts.md` for detailed test scenarios.


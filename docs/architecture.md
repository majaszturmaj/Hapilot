# Hapilot — System Architecture

Detailed developer documentation. For setup and usage, see [README.md](../README.md).

---

## Agent Graph

```
__start__ → task_planner → router
  ├→ A) document_generator → check_document_result → writer → __end__
  ├→ B) knowledge → writer → __end__
  ├→ C) chat → writer → __end__
  └→ D) coder → execute → check_python_execution
                              ├→ (success) → writer → __end__
                              └→ (error, retries < 3) → coder (retry)
```

### State Definition (`src/agent_graph.py`)

```python
class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]
    next_step: str
    execution_status: str   # success / error
    retry_count: int        # Code retry counter (max 3)
    task_plan: dict         # STORM-like task plan
    task_id: str            # State persistence ID
    plan_mode: str          # auto | explicit | off
    is_complex_task: bool   # Multi-step flag
```

### Node Descriptions

| Node | Model | Function | Purpose |
|------|-------|----------|---------|
| `task_planner` | Bielik | `task_planner_node()` | Analyze complexity, generate task plan |
| `router` | Bielik | `router_node()` | 4-way classification: A=doc, B=knowledge, C=chat, D=python |
| `document_generator` | Bielik | `run_document_generator()` | Template selection + parameter filling + PDF compilation |
| `coder` | Qwen | `run_coder()` | Python code generation |
| `execute` | Docker | `run_execute()` | Execute Python in sandbox |
| `knowledge` | Bielik | `run_knowledge()` | RAG search + answer synthesis |
| `chat` | Bielik | `run_chat()` | General conversation |
| `writer` | Bielik | `run_writer()` | Format final response to user |

---

## LLM Configuration (`src/llm_config.py`)

### Bielik Roles & System Prompts

| Role | Prompt Variable | Purpose |
|------|----------------|---------|
| `router` | `BIELIK_SYSTEM_PROMPT_ROUTER` | 4-way intent classification (A/B/C/D) |
| `knowledge` | `BIELIK_SYSTEM_PROMPT_KNOWLEDGE` | RAG document retrieval and synthesis |
| `task_planner` | `BIELIK_SYSTEM_PROMPT_TASK_PLANNER` | STORM-like task decomposition |
| `template_filler` | `BIELIK_SYSTEM_PROMPT_TEMPLATE_FILLER` | Template selection and parameter filling for PDF |

### Usage

```python
from src.llm_config import get_bielik_chat, get_coder_chat

llm, system_prompt = get_bielik_chat(role="template_filler")
coder_llm, coder_prompt = get_coder_chat(role="coder")  # auto-fallback to Bielik
```

### Dynamic Model Registry (`src/model_registry.py`)

Models can be added/switched at runtime via the Chainlit UI settings panel. Configuration persists to `models_config.json`.

---

## Template-Based PDF Generation

### Why templates?
Bielik (11B) is not reliable at writing LaTeX from scratch. Templates ensure consistent, professional formatting while Bielik focuses on content extraction.

### Flow
```
User request → Bielik (template_filler role)
  → Reads templates/templates.json metadata
  → Selects best template (letter/report/protocol)
  → Generates parameter values from user context
  → _fill_template() replaces {{PARAM}} placeholders in .tex file
  → Writes filled .tex to sandbox/workspace/
  → pdflatex compiles in Docker sandbox container
  → PDF returned to user via Chainlit download link
```

### Available Templates

| Template | File | Parameters | Use Case |
|----------|------|------------|----------|
| `letter` | `templates/letter.tex` | 13 | Formal letters, applications |
| `report` | `templates/report.tex` | 14 | Institutional reports with TOC |
| `protocol` | `templates/protocol.tex` | 13 | Meeting minutes, protocols |

### Key Functions (`src/agent_tools.py`)

| Function | Purpose |
|----------|---------|
| `tool_select_template_and_generate()` | End-to-end: select template + fill + compile |
| `tool_list_templates()` | Returns list of available templates |
| `tool_generate_pdf_from_template()` | Fill specific template + compile |
| `_load_templates_metadata()` | Load templates.json |
| `_fill_template()` | Replace {{PARAM}} placeholders |

### MCP Server (`src/template_server.py`)

Standalone FastMCP server exposing template tools for external access:
- `list_templates` — List all available templates
- `generate_pdf_from_template` — Generate PDF from template + parameters
- `get_template_details` — Get full metadata for a specific template

---

## RAG Knowledge System

### Ingestion (`src/ingest_data.py`)
- Loads files from `data/private_docs/`
- RecursiveCharacterTextSplitter: chunk=512, overlap=50
- Embeddings: `intfloat/multilingual-e5-base` (forced CPU, no GPU)
- Storage: ChromaDB at `localhost:8000`

### Retrieval (`src/agent_tools.py`)
- `tool_rag_search(query)`: Searches ChromaDB, returns top 3 semantic matches
- Contains "OmegaCorp" canary data for hallucination testing

### Private Documents
Located in `data/private_docs/`:
- `procedury_zglaszania_pomyslu_projektu.txt` — Project proposal procedures
- `regulamin_ubiegania_o_podwyzke.txt` — Salary raise regulations
- `regulamin_urlopu_naukowego.txt` — Scientific leave regulations
- `wymagania_zrownowazony_rozwoj_ekologia.txt` — Sustainability requirements
- `zasady_ogloszen_o_prace.txt` — Job posting rules

---

## Python Code Execution

### Flow
```
Router (D=Python) → run_coder() → run_execute() → check_python_execution()
                         ↑                                    │
                         └──── retry (up to 3 attempts) ──────┘
```

- Qwen generates Python code
- `_extract_python_from_llm_output()` uses 5 strategies to clean LLM response
- Code runs in isolated Docker container (Python 3.10 + data-science libs)
- On error, the error message is fed back to Qwen for retry (up to 3x)

### Docker Sandbox
- **Build**: `sandbox/Dockerfile` (Python 3.10-slim + texlive + pandas/numpy/matplotlib/etc.)
- **Volume**: `sandbox/workspace/` mounted at `/workspace`
- **Security**: Isolated execution, resource-limited (1 CPU, 512MB RAM)

---

## MCP Servers

| Server | File | Tools |
|--------|------|-------|
| Knowledge | `src/knowledge_server.py` | `search_documentation(query)` |
| Operations | `src/operations_server.py` | `send_email_draft(to, subject, body)`, `check_calendar(date_from, date_to)` |
| Template | `src/template_server.py` | `list_templates()`, `generate_pdf_from_template()`, `get_template_details()` |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Qwen Coder unavailable | System auto-falls back to Bielik. Check remote server. |
| PDF compilation fails | Ensure Docker sandbox is running: `cd sandbox && docker compose up -d` |
| RAG returns no results | Re-ingest documents: `./venv/bin/python src/ingest_data.py` |
| CUDA OOM on embeddings | Embeddings force CPU execution. Ensure LM Studio isn't consuming all VRAM. |
| Template not found | Check `templates/templates.json` and that `.tex` file exists. |

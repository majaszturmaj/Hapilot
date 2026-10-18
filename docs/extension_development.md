# Writing a Hapilot extension

Every capability of Hapilot (knowledge search, letters and PDFs, Python, editing documents, chat) is an
**extension**: a folder with a manifest `hapilot_extension.json` and Python files. At start-up the extension
manager ([src/core/extension_manager.py](../src/core/extension_manager.py)) reads the manifests, checks them
against [docs/schemas/extension.schema.json](schemas/extension.schema.json), imports the nodes and hooks, and
the graph ([src/agent_graph.py](../src/agent_graph.py)) is built from what loaded. Nothing about an extension
is hard-coded in the core:

- the **planner** sees the tools of the loaded extensions, with their descriptions, in its prompt;
- the **router** sends a plan step to the node that provides the step's tool;
- the **edges** after a node come from the manifest;
- **prompts** are registered per model role.

A complete, small example is in [examples/extensions/vat_calculator/](../examples/extensions/vat_calculator/):
a deterministic VAT node, one planner tool, hooks and configuration.

## Quick start

```bash
cp -r examples/extensions/vat_calculator ~/my_extensions/my_extension   # then edit it
HAPILOT_EXTENSIONS_DIRS=~/my_extensions .venv/bin/python -m src.core.extension_manager
```

The check command prints every tool and the node it goes to, then warnings and errors, and exits with 1
if any extension failed to load. Start Hapilot with the same `HAPILOT_EXTENSIONS_DIRS` (for example in
`.env`) and the planner can use the new tool.

Extensions are looked up in `extensions/` of the repository and then in each folder of
`HAPILOT_EXTENSIONS_DIRS` (separated by `:`; relative paths are relative to the working directory). An
extension is a sub-folder that contains `hapilot_extension.json`.

```text
my_extension/
├── hapilot_extension.json   # manifest (required)
├── nodes.py                 # node functions
├── hooks.py                 # optional: lifecycle hooks
└── prompts/                 # optional: system prompts, one .txt per model role
    └── my_role.txt
```

## Manifest, version 1

```json
{
  "manifest_version": 1,
  "id": "my_extension",
  "name": "My extension",
  "version": "0.1.0",
  "description": "What it does",
  "author": "Your Name",
  "license": "MIT",
  "enabled": true,

  "nodes": [
    {"name": "my_node", "entry_point": "nodes:run_my_node",
     "label": "⚙️ Mój węzeł", "description": "Shown in the UI while the node runs",
     "after": {"policy": "on_success"}}
  ],

  "tools": [
    {"name": "my_tool", "node": "my_node",
     "description": "Co robi narzędzie (one line, Polish — the planner reads it)",
     "when_to_use": "Kiedy planer ma go użyć",
     "when_not_to_use": "Kiedy nie (i co wybrać zamiast)",
     "example": "„Przykładowe polecenie użytkownika” → my_tool",
     "aliases": ["my_alias"]}
  ],

  "prompts": {"my_role": "prompts/my_role.txt"},
  "hooks": "hooks:MyHooks",
  "config": {"any": "values the extension reads"}
}
```

| Field | Meaning |
|---|---|
| `manifest_version` | `1` (required). Older manifests without it are rejected with a message. |
| `id` | required; lower case, digits, `_`; unique across all loaded extensions |
| `name` | required; for people |
| `enabled` | `false` skips the extension (e.g. `n8n_operations`): its tools are not offered to the planner |
| `nodes[].name` | graph node name, unique across extensions and the core (`task_planner`, `router`, `hitl_confirm`) |
| `nodes[].entry_point` | `module:function` in the extension folder (`nodes:run_my_node` → `run_my_node` in `nodes.py`) |
| `nodes[].label`, `description` | shown in the UI while the node runs |
| `nodes[].then` | always continue with this node (e.g. `coder` → `execute`) |
| `nodes[].after` | what happens after the node, see [Edges](#edges); default `{"policy": "plan"}` |
| `tools[].name` | the name the planner writes in a plan step; unique |
| `tools[].node` | the node that runs the step; default: the extension's first node |
| `tools[].description` | required; the planner's one-line description of the tool |
| `tools[].when_to_use`, `when_not_to_use`, `example` | added to the planner's catalog as `KIEDY:`, `KIEDY NIE:`, `PRZYKŁAD:` |
| `tools[].aliases` | other names a model may write for this tool (e.g. `latex` for `template_pdf`); not shown to the planner |
| `prompts` | model role → `.txt` file in the extension (or inline text); `get_llm(role)` uses it |
| `hooks` | `module:Class`, a subclass of [BaseExtension](../src/core/base_extension.py) |
| `config` | free-form object; `get_active().get_config("my_extension")`, also passed to the hooks class |

The tool descriptions are the planner's only information about a tool, so they decide whether the tool is
used. Write them in Polish (the planner prompt is Polish), say what the tool does in one line, and say in
`when_not_to_use` which tool to choose instead.

### What is checked at load time

An extension that fails a check is skipped with a reason (shown by the check command and in the log); the
others still load.

- the manifest matches the schema (types, required fields, names, `then` and `after` not both set,
  `retry_to` present for `retry`);
- every `entry_point` and `hooks` class can be imported and is callable / a `BaseExtension` subclass;
- every prompt file exists;
- every tool points at a node of the same extension;
- no node or tool name is used twice (across extensions and the core);
- `then` and `retry_to` point at nodes that exist.

## How a request reaches your node

1. **Planner** (`task_planner`, a model call): the system prompt
   ([src/core/prompts/task_planner.txt](../src/core/prompts/task_planner.txt)) plus the catalog of the loaded
   tools, built from the manifests:
   ```text
   - my_tool: Co robi narzędzie
     KIEDY: Kiedy planer ma go użyć
     KIEDY NIE: Kiedy nie (i co wybrać zamiast)
     PRZYKŁAD: „Przykładowe polecenie użytkownika” → my_tool
   ```
   It answers with a plan: `{"steps": [{"id": 1, "tool": "my_tool", "action": "…", "input": "…"}]}`.
2. **Router** (no model call): looks up the step's tool (or alias) and sends the step to its node. An
   unknown tool goes to `chat`. Hooks may redirect the step first (see [Hooks](#hooks)).
3. **Your node** runs, returns a state update and moves the plan on with `_advance_plan_step(state)`.
4. **Edge** after your node: by default the next plan step's node, or the end of the run.

## Node functions

A node receives the graph state (a dict) and returns a partial update:

```python
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.core.tools_common import invoke_llm_and_normalize
from src.llm_config import get_llm


def run_my_node(state):
    from src.agent_graph import _advance_plan_step, _resolve_effective_user_message

    plan, idx = state.get("task_plan") or {}, state.get("current_step")
    request = _resolve_effective_user_message(state["messages"])
    if plan and idx is not None and 0 <= idx < len(plan.get("steps", [])):
        request = plan["steps"][idx].get("input") or request      # this step's input from the planner

    llm, system_prompt = get_llm("my_role")      # the session profile's model and the role's prompt
    answer = invoke_llm_and_normalize(llm, [SystemMessage(content=system_prompt), HumanMessage(content=request)])
    return {"messages": [answer], **_advance_plan_step(state)}   # move the plan on
```

- Import core helpers inside the function (avoids import cycles at start-up).
- Model calls go through `get_llm(role)`: the endpoint comes from the operator's profile and must be
  local (see [config/profiles.yaml](../config/profiles.yaml)). Do not create clients to other endpoints.
- Files: `src.run_context.workspace_file(name)` — each chat session has its own workspace.
- Running code: `src.sandbox.run(...)` (no network, non-root, limits).
- Per-run values: `src.run_context.approval_policy()`, `today()`, `session_id()`.
- Model and tool calls are traced automatically; decorate your own tool functions with
  `@traced_tool` from [src/tracing.py](../src/tracing.py) to record them in evaluation traces.
- Always return `**_advance_plan_step(state)` when the step is done: the edge looks at the plan's current
  step, so a node that does not move the plan on is sent the same step again.

## Edges

| Manifest | After the node |
|---|---|
| (nothing) or `"after": {"policy": "plan"}` | next plan step's node, or end; ends if the node asked the user (`hitl_request`) |
| `"after": {"policy": "on_success"}` | as `plan`, but ends unless the node set `execution_status: "success"` |
| `"after": {"policy": "retry", "retry_to": "coder", "max_attempts": 3}` | on success (`execution_status: "success"`) as `plan`; otherwise back to `retry_to` while `retry_count` < `max_attempts`, then end. The node increments `retry_count` on each failure; the router resets it for every plan step |
| `"then": "execute"` | always this node |

The built-in Python extension uses both: `coder` `then` `execute`, and `execute` retries `coder` on error
([extensions/python_coder/hapilot_extension.json](../extensions/python_coder/hapilot_extension.json)).

## Hooks

```python
from src.core.base_extension import BaseExtension


class MyHooks(BaseExtension):        # created with (extension_id, config)
    def on_load(self):               # once, after the extension loaded
        self.limit = self.config.get("limit", 10)

    def on_before_route(self, state):  # before every routing decision
        return None                      # or {"next_step": "<node>"} to send this step elsewhere

    def on_after_node(self, node_name, state, output):  # after every node, of any extension
        return None                                     # or a replacement state update

    def on_unload(self):             # at process exit
        pass
```

A hook that raises is logged and ignored. With several extensions, the last non-empty `on_before_route`
answer wins; `on_after_node` answers are applied in load order.

## Asking the user (human in the loop)

```python
from src.core.tools_common import create_hitl_confirm_request


def run_my_node(state):
    from src import run_context
    from src.agent_graph import _advance_plan_step

    answer = state.get("hitl_response") or {}
    if answer.get("resume_node") == "my_node":          # second pass: the user answered
        text = "Zrobione." if answer.get("confirmed") else "Anulowano."
        return {"messages": [AIMessage(content=text)], "hitl_response": None, **_advance_plan_step(state)}

    if run_context.approval_policy() == "auto":          # scripts / evaluation: do not wait
        ...
    request = create_hitl_confirm_request(
        content="Czy wykonać operację X?",
        continuation={"type": "resume_node", "resume_node": "my_node",
                      "saved_plan": state.get("task_plan") or {}, "saved_step": state.get("current_step")},
    )
    return {"messages": [AIMessage(content="Czekam na potwierdzenie.")], "hitl_request": request}
```

The UI shows the question; the answer comes back as `hitl_response` with the continuation's fields and
`confirmed: true/false`, and the planner resumes the saved plan at `my_node`. Under the `auto` approval
policy (evaluation runs) nothing waits: record the decision with a trace `approval` event and continue.

## Testing

Unit tests run without a model or Docker: the `llm` fixture installs a scripted model (answers per role)
and `docker` a fake Docker client ([tests/conftest.py](../tests/conftest.py)). To test an extension in the
graph, load it with its own manager:

```python
from src.agent_graph import build_dynamic_graph
from src.core.extension_manager import ExtensionManager

m = ExtensionManager([Path("extensions"), Path("path/to/my_extensions")])
m.discover_and_load()
assert m.errors == []
app = build_dynamic_graph(m)
llm.on("task_planner", '{"steps": [{"id": 1, "tool": "my_tool", "action": "…", "input": "…"}]}')
```

[tests/unit/test_extensions.py](../tests/unit/test_extensions.py) does this for the VAT example and covers
the load-time checks.

## Good practice

- No network access except to local services configured by the operator; no credentials in the repository.
- Return informative messages instead of raising; the UI shows them to the user.
- Keep prompts and tool descriptions in Polish where the users are Polish.
- Say in the tool description what the step's `input` should contain (e.g. „kwota z oznaczeniem netto albo
  brutto”): the planner rewrites the user's request into the input, and a node that parses text must
  accept the planner's wording, not only the user's.
- Keep the tool list small: every tool makes the planner's choice harder. Prefer one tool with a clear
  description to several similar ones.

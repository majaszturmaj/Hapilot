# Writing a Hapilot extension

Capabilities such as knowledge search, PDF generation or Python execution are **extensions**: folders in
`extensions/` with a manifest. The core ([src/core/extension_manager.py](../src/core/extension_manager.py))
discovers them at start-up, imports their node functions, registers their tools and prompts, and adds
the nodes to the LangGraph graph.

## Folder layout

```text
extensions/my_extension/
├── hapilot_extension.json   # manifest (required)
├── nodes.py                 # node functions
└── prompts/                 # system prompts, one .txt per role (optional)
    └── my_role.txt
```

## Manifest (`hapilot_extension.json`)

The format the loader reads (compare with the extensions in this repository):

```json
{
  "id": "my_extension",
  "name": "My Extension",
  "version": "0.1.0",
  "description": "What it does",
  "author": "Your Name",
  "license": "MIT",
  "enabled": true,

  "nodes": [
    {
      "name": "my_node",
      "entry_point": "nodes:run_my_node",
      "label": "⚙️ My node",
      "description": "Shown in the UI while the node runs"
    }
  ],

  "tools": [
    {
      "name": "my_tool",
      "planner_description": "What the tool does",
      "planner_when_to_use": "When the planner should choose it",
      "planner_when_not_to_use": ""
    }
  ],

  "prompts": {
    "my_role": "prompts/my_role.txt"
  }
}
```

| Field | Meaning |
|---|---|
| `id`, `name` | required |
| `enabled` | `false` skips the extension (e.g. `n8n_operations`) |
| `nodes[].entry_point` | `module:function` — `nodes:run_my_node` imports `run_my_node` from `nodes.py` in the extension folder |
| `tools[].name` | a tool name the planner can put in a plan step; the router sends such steps to the **first node** of the extension |
| `prompts` | role → `.txt` file (or an inline string); a role registered by an extension overrides the default prompt of that role |
| `routes` | optional and informational (the router is deterministic and does not use them) |

**Known limitation (v0.1):** the planner prompt describes only the tool names listed in
`PLANNER_TOOL_DESCRIPTIONS` in [src/agent_tools.py](../src/agent_tools.py); a new tool must be added
there to be offered to the planner. Nodes other than the built-in ones get the generic edge
(next plan step, or end).

## Node functions

A node receives the graph state (a dict) and returns a partial update:

```python
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.core.tools_common import invoke_llm_and_normalize
from src.llm_config import get_llm


def run_my_node(state):
    from src.agent_graph import _advance_plan_step, _resolve_effective_user_message

    request = _resolve_effective_user_message(state["messages"])
    llm, system_prompt = get_llm("my_role")            # model and prompt for this role from the session's profile
    answer = invoke_llm_and_normalize(llm, [SystemMessage(content=system_prompt), HumanMessage(content=request)])
    return {"messages": [answer], **_advance_plan_step(state)}   # move the plan on
```

- Import core helpers inside the function (avoids import cycles at start-up).
- Read the current step's input from `state["task_plan"]["steps"][state["current_step"]]` when the
  node runs as part of a plan.
- Files: use `src.run_context.workspace_file(name)` — every chat session has its own workspace.
- Running code: `src.sandbox.run(command, workdir, timeout)` (no network, non-root, limits).
- Per-run values: `src.run_context.approval_policy()`, `today()`, `session_id()`.

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
`confirmed: true/false`, and the planner resumes the saved plan at `my_node`.

## Testing

Unit tests run without a model: install a scripted model with the `llm` fixture and a fake Docker
client with the `docker` fixture (see [tests/conftest.py](../tests/conftest.py) and
[tests/unit/test_graph_flows.py](../tests/unit/test_graph_flows.py)).

## Good practice

- No network access except to local services configured by the operator; no credentials in the repository.
- Return informative error messages instead of raising; the UI shows them to the user.
- Keep prompts in Polish where the users are Polish; keep them in `prompts/*.txt`.

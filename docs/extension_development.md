# Hapilot Extension Development Guide

The Hapilot platform is designed to be highly extensible. All domain-specific capabilities (like RAG, PDF generation, or Python execution) are implemented as independent **extensions**. This guide explains how to create your own extension to add new capabilities to the platform.

## Architecture Overview

The core system handles:
1. **Routing** (analyzing user intent via `task_planner` and `router`)
2. **LLM Invocation** (standardized wrappers for the LLM)
3. **Graph Execution** (LangGraph orchestration)
4. **State Management** (HITL, memory, and persistence)

An **Extension** provides:
- **Routes**: A new option for the Router (e.g., "Analyze Data").
- **Nodes**: LangGraph nodes that actually process the data.
- **Prompts**: Specialized instructions for the LLM.
- **Tools**: Helper functions the nodes can invoke.

## Extension Structure

Every extension lives in the `extensions/` directory and must contain a manifest file named `hapilot_extension.json`.

A typical extension looks like this:

```text
extensions/my_extension/
├── hapilot_extension.json    # The manifest (Required)
├── nodes.py                # Graph nodes (Optional)
├── tools.py                # Tools (Optional)
└── prompts/                # Text files with prompts (Optional)
    └── my_role.txt
```

## 1. The Manifest (`hapilot_extension.json`)

The manifest is the entry point. It tells the `ExtensionManager` what your extension provides.

```json
{
  "id": "my_custom_feature",
  "name": "My Custom Feature",
  "version": "1.0.0",
  "description": "Adds the ability to process custom data.",
  "enabled": true,
  
  "entry_points": {
    "nodes": "nodes",
    "tools": "tools"
  },
  
  "routes": [
    {
      "id": "F",
      "name": "Custom Data Processing",
      "description": "KIEDY UŻYWAĆ: Gdy użytkownik chce przetworzyć niestandardowe dane.",
      "target_node": "custom_processor",
      "priority": 10
    }
  ],
  
  "nodes": [
    {
      "name": "custom_processor",
      "function": "run_custom_processor",
      "ui_label": "⚙️ Processing Data",
      "ui_description": "Processing user data using custom logic."
    }
  ],
  
  "prompts": [
    {
      "role": "custom_role",
      "file": "prompts/custom_role.txt"
    }
  ],
  
  "tools": [
    {
      "name": "my_tool",
      "function": "tool_my_tool"
    }
  ]
}
```

### Important Fields
- **`entry_points`**: Tells the system which Python files to import. `"nodes": "nodes"` means it will look for `nodes.py` in your extension folder.
- **`routes`**: Registers your extension with the LLM Router. The `description` is injected into the LLM prompt. `target_node` is the node to execute if the router picks this path.
- **`nodes`**: Registers functions to be added to the LangGraph. `function` must match the function name in your `nodes.py`.
- **`prompts`**: Maps an LLM "role" (e.g., `"router"`, `"coder"`) to a text file. This allows your extension to override core prompts or define its own.
- **`tools`**: Registers Python functions from your `tools.py` into the global tool registry, making them available to the `task_planner`.

## 2. Creating Nodes (`nodes.py`)

A node is a standard LangGraph Python function. It receives the `state` (a `dict`) and returns a partial state update.

```python
from langchain_core.messages import AIMessage
from src.core.tools_common import invoke_llm_and_normalize
from src.llm_config import get_bielik_chat

def run_custom_processor(state: dict):
    messages = state["messages"]
    
    # 1. Get the LLM configured with your custom prompt
    llm, system_prompt = get_bielik_chat(role="custom_role")
    
    # 2. Invoke the LLM
    final_output = invoke_llm_and_normalize(
        llm=llm,
        messages=messages,
        system_prompt=system_prompt,
        temperature=0.3
    )
    
    # 3. Update the state. 
    # Return next_step="writer" to send the output to the user.
    return {
        "messages": [AIMessage(content=final_output)],
        "next_step": "writer"
    }
```

### Graph Execution Rules
- Every node function MUST return a dictionary representing the state update.
- To route to the next node, set `"next_step": "target_node_name"`.
- If `"next_step"` is missing, the graph will follow the default edge defined in `agent_graph.py` (usually ending the graph or going to `writer`).

## 3. Creating Prompts (`prompts/`)

Prompts are stored as plain text files (`.txt`). They are automatically loaded into the `PromptRegistry`.

If your manifest registers:
```json
{ "role": "custom_role", "file": "prompts/custom_role.txt" }
```

Then in `nodes.py`, calling `get_bielik_chat(role="custom_role")` will automatically use the contents of that text file.

You can also **override** core prompts. For example, if you register a prompt with the role `"router"`, it will override the default router prompt for the entire system!

## 4. Creating Tools (`tools.py`)

Tools are utility functions that can be used by the Planner or by your own nodes.

```python
def tool_my_tool(data: str) -> str:
    """Processes the input data."""
    return f"Processed: {data}"
```

Tools are discovered based on the `tools` array in the manifest.

## 5. Human in the Loop (HITL)

If your extension requires user confirmation before proceeding, use the built-in HITL mechanisms.

In your node (`nodes.py`):
```python
from src.core.tools_common import request_hitl_confirm

def run_custom_processor(state: dict):
    # Check if the user already responded
    hitl_response = state.get("hitl_response")
    
    if not hitl_response:
        # Pause the graph and ask the user
        return request_hitl_confirm(
            content="Do you want to proceed with this custom action?",
            continuation_data={"type": "custom_action_confirm"}
        )
    
    # User confirmed!
    if hitl_response.get("confirmed"):
        return {"messages": [AIMessage(content="Action confirmed!")], "next_step": "writer"}
    else:
        return {"messages": [AIMessage(content="Action cancelled.")], "next_step": "writer"}
```

## Best Practices

1. **Isolation**: Your extension should not import files from other extensions directly.
2. **Lazy Loading**: Import core modules (`src.core...`) inside your node functions to prevent circular dependencies during the initialization phase.
3. **No Hardcoded API Keys**: Use `.env` variables if your extension needs external services.
4. **Graceful Failures**: If your extension relies on external services, handle timeouts gracefully and return informative error messages.

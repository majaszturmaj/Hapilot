import sys
import os
import asyncio

# Ensure project root is on path
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(current_dir, ".."))
sys.path.insert(0, root_dir)

from langchain_core.messages import HumanMessage
from src.agent_graph import app

async def run_test(message_text):
    inputs = {
        "messages": [HumanMessage(content=message_text)],
        "next_step": "",
        "execution_status": "",
        "retry_count": 0,
        "task_plan": {},
        "task_id": "",
        "plan_mode": "auto",
        "is_complex_task": False,
    }
    print("\n--- TEST INPUT:\n", message_text)
    try:
        res = await app.invoke(inputs, config={"recursion_limit": 50})
        print("--- RESPONSES: ")
        for m in res.get("messages", []):
            print(type(m), getattr(m, 'content', str(m))[:1000])
        # If the planner returned a task_plan, print it
        if res.get("task_plan"):
            print("--- TASK PLAN:")
            import json
            print(json.dumps(res.get("task_plan"), ensure_ascii=False, indent=2))
    except Exception as e:
        print("ERROR during invoke:", e)


async def main():
    tests = [
        "Co robić w razie pożaru w naszej firmie?",
        "Zredaguj bardzo miły, dziękczynny mail do pani Kasi Nowak z działu bezpieczeństwa prosząc uprzejmie o zorganizowanie szkolenia dla pracowników serwerowni i zrób to jako gotowy PDF.",
        "Wygeneruj teraz PDF z tą treścią: \\\"To jest testowy dokument.\\\"",
    ]
    for t in tests:
        await run_test(t)

if __name__ == '__main__':
    asyncio.run(main())

"""Evaluation interface v1: run Hapilot headless on tasks and record results and traces.

See docs/EVALUATION_INTERFACE.md. ``python -m src.eval run|validate``.
"""

from src.eval.config import RunConfig, Task, load_tasks
from src.eval.runner import EvalSession

__all__ = ["EvalSession", "RunConfig", "Task", "load_tasks"]

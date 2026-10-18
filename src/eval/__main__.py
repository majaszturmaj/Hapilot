"""Command line: ``python -m src.eval run …`` and ``python -m src.eval validate OUT_DIR``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.eval", description="Hapilot evaluation interface v1")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run tasks and write results and traces")
    run.add_argument("--tasks", type=Path, required=True)
    run.add_argument("--corpus", type=Path)
    run.add_argument("--out", type=Path)
    run.add_argument("--today")
    run.add_argument("--profile")
    run.add_argument("--config", type=Path, help="run.yaml with the same settings (flags win)")
    run.add_argument("--seed", type=int)
    run.add_argument("--timeout", type=float, dest="task_timeout_s")
    run.add_argument("--answer-mode", choices=["formatter", "parse"])
    run.add_argument("--only", help="comma-separated task ids")
    run.add_argument("--no-resume", action="store_true")
    run.add_argument("--retry-failed", action="store_true")
    val = sub.add_parser("validate", help="check an output directory against the schemas")
    val.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv)

    from src.config import ConfigError

    if args.command == "validate":
        from src.eval.validate import validate_run

        problems = validate_run(args.out_dir)
        for p in problems:
            print(p)
        print(f"{'VALID' if not problems else 'INVALID'}: {args.out_dir}")
        return 0 if not problems else 1

    try:
        from src.eval.config import RunConfig, load_run_yaml, load_tasks

        settings = load_run_yaml(args.config) if args.config else {}
        for key, value in {"corpus_dir": args.corpus, "out_dir": args.out, "today": args.today, "profile": args.profile,
                           "seed": args.seed, "task_timeout_s": args.task_timeout_s, "answer_mode": args.answer_mode}.items():
            if value is not None:
                settings[key] = value
        if args.no_resume:
            settings["resume"] = False
        if args.retry_failed:
            settings["retry_failed"] = True
        missing = [k for k in ("corpus_dir", "out_dir", "today") if k not in settings]
        if missing:
            raise ConfigError(f"missing settings: {', '.join(missing)}")
        cfg = RunConfig(**settings)
        tasks = load_tasks(args.tasks)
        if args.only:
            wanted = set(args.only.split(","))
            tasks = [t for t in tasks if t.id in wanted]
    except (ConfigError, TypeError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    from src.eval.runner import EvalSession

    with EvalSession(cfg) as session:
        session.run_batch(tasks)
    counts = session.meta["tasks"]
    print(f"[eval] done: {counts} → {cfg.out_dir}")
    return 0 if counts.get("error", 0) == 0 and counts.get("timeout", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

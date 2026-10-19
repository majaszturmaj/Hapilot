# Testing

Hapilot is tested at three levels. Only the first runs in GitHub Actions; the others need the models and
run on the GPU host.

| Level | What | Where | Command |
|---|---|---|---|
| Unit tests + lint | code paths with a scripted model and a fake Docker; no model, GPU or network | any machine, CI | `pytest`, `ruff check …` |
| Integration suite | smoke scenarios, planner check and development tasks with the real models, per profile | GPU host | `hapilotctl.py integration` |
| Egress test | the smoke scenarios under `strace`; fails on any outbound connection | GPU host | `hapilotctl.py egress-test` |

## Unit tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt
.venv/bin/ruff check src extensions examples tests scripts tools
.venv/bin/coverage run -m pytest -q && .venv/bin/coverage report
```

`requirements-test.txt` installs no model runtime, no torch and no ChromaDB. The test doubles live in
[tests/fakes.py](../tests/fakes.py) and [tests/conftest.py](../tests/conftest.py):

- `llm` fixture (`LLMScript`): replies per agent role, in order, and records every prompt;
- `docker` fixture (`FakeDocker`): runs generated Python in a subprocess and fakes `pdflatex`; it fails the
  test if Hapilot tries to pull an image;
- vector store: tests replace the knowledge-base backend or the Chroma client with in-memory fakes;
- egress guard: installed for the whole session; any non-local connection or DNS lookup fails the test.

Every bug fix lands with a regression test. Coverage is measured over `src/`, `extensions/` and
`examples/`; the gate (`fail_under` in [pyproject.toml](../pyproject.toml)) is 85 %. Not covered by unit
tests: the Chainlit UI (`src/app.py`) and the optional MCP servers (`src/*_server.py`), which need packages
the test environment does not install, and the launcher's process management (`scripts/hapilotctl.py`),
which is exercised on the GPU host by the integration suite.

## Continuous integration

[.github/workflows/ci.yml](../.github/workflows/ci.yml) runs on every push and pull request, on Python 3.10
and 3.12: lint, unit tests with the coverage gate, and a load check of the extension manifests
(`python -m src.core.extension_manager`, also for `examples/extensions`). It never starts a model.

## Integration suite (GPU host)

```bash
.venv/bin/python scripts/hapilotctl.py integration                       # every profile
.venv/bin/python scripts/hapilotctl.py integration --profile best-local  # one profile
.venv/bin/python scripts/hapilotctl.py integration --help                # --out, --skip, --planner-repeat
```

It first runs the unit tests in the deployed checkout, then for each profile starts the model proxy and
runs three suites on the demo company ([examples/demo_company/](../examples/demo_company/)):

| Suite | Input | Checks |
|---|---|---|
| smoke | [tests/scenarios/smoke.yaml](../tests/scenarios/smoke.yaml), 11 requests through the whole graph, 3 of them with uploaded files; indexes the demo corpus into a temporary knowledge base | the answer contains / does not contain given text; a PDF or other file was generated and contains / lacks given text; egress guard on |
| planner | [tests/scenarios/planner.yaml](../tests/scenarios/planner.yaml), 25 requests, planner node only (guards included) | required, alternative, forbidden tools and their order |
| dev | [tests/eval/dev_tasks.jsonl](../tests/eval/dev_tasks.jsonl), 10 tasks through the evaluation interface, date 2026-10-14 | schema validation; JSON answer values, sources, plan tools, created files ([scripts/dev_eval_check.py](../scripts/dev_eval_check.py)) |

The scenarios and tasks are about Hapilot's own fictional demo company. They are development checks, not
the thesis benchmark, and they are not used to score Hapilot there.

Known failures are listed per profile and suite in
[tests/integration/baseline.yaml](../tests/integration/baseline.yaml), each with the run that found it. A
listed failure is reported but does not fail the suite; any other failure, an invalid evaluation run, a
crashed suite or an egress attempt does. When a listed item passes, the report says so, so the baseline can
be tightened.

Output (default `var/integration/<timestamp>/`): `integration_report.md` (table per profile and suite),
`integration_report.json`, and per profile the smoke and planner reports, the planner's model-call traces
(`planner_traces/`, `hapilot.trace/1`), the evaluation run directory (`dev_eval/`) and the logs. The run
stops the model proxy it started; it refuses to start while Hapilot is already running.

**Last integration run: passed on 2026-10-19, commit `ad9fa98`, both profiles** (RTX 4090):

| Profile | Smoke | Planner | Dev tasks | Time (s, sum of suites) |
|---|---|---|---|---|
| reference | 11/11 | 25/25 | 9/10 (D03: known, listed in the baseline) | 392 |
| best-local | 11/11 | 25/25 | 10/10 | 410 |

Unit tests in the deployed checkout 217/217; both evaluation runs schema-valid; no egress attempts. Later commits
of the same day changed the UI, adding files to the knowledge base and re-loading a rebuilt index; they were
checked with the unit tests, the egress test (which runs the smoke scenarios) and the UI walk-through
(`scripts/ui_demo.py`, 9/9 steps).

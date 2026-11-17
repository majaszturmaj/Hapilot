# Hapilot v3 agent (work in progress, branch `v3`)

Hapilot v3 adds a second agent engine next to the existing one. The existing engine (planner → router → nodes on
LangGraph, "v2") stays as it is and remains the default; nothing in it is removed. The v3 engine runs one
model-driven loop with native tool calls on the Cline SDK (`@cline/sdk` 0.2.0, Apache-2.0), inside an isolated
container, and reads whole documents as files instead of answering from retrieved chunks.

Stage 0 (this directory so far) checks that the SDK, embedded as a library, does what the Cline CLI did when it was
measured: same SDK source, same session settings, same task prompt and document layout.

| Path | What |
|---|---|
| `runner/run_task.mjs` | one task, one SDK session, configured like `cline -y --json` 3.0.74; writes a result file and a JSONL trace of every tool call |
| `runner/egress_guard.mjs` | loaded first: records every outbound connection of the runner and refuses all but the model endpoint |
| `runner/package.json`, `package-lock.json` | SDK 0.2.0 with the dependency tree of the CLI 3.0.74 build: derived from its `bun.lock` (tag `cli-v3.0.74`) by `runner/bun2npm.py`, same versions and integrity hashes; the CLI build's patch of `ollama-ai-provider-v2` is not applied (not used with an OpenAI-compatible endpoint) |
| `../sandbox/agent.Dockerfile` | image `hapilot-agent:0.1` = `hapilot-sandbox:0.2` + Node 22.23.4 + the runner |
| `spike/spike.py` | runs a task set through the container (one fresh container per task) and scores it with the set's own scorer |
| `spike/fake_openai.py` | scripted OpenAI-compatible server for a dry run without a model |

Container: internal Docker network whose only reachable host is a relay to the local model server, non-root,
read-only root filesystem, no capabilities, read-only documents, memory/CPU/process limits, no credentials.
Telemetry: the runner creates no telemetry or feature-flag service; the SDK's Langfuse export is off for non-Cline
providers unless explicitly configured (it is not); the egress log shows what was attempted.

Tests: `node --test agent/runner/test/` (egress guard) and `pytest tests/unit/test_v3_spike.py` (driver).

The Cline SDK is third-party software: see `runner/NOTICE` and `runner/LICENSE-cline-sdk`.

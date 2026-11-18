# Hapilot v3 agent image (hapilot-agent:0.4): the sandbox image (Python data tools, TeX Live, TeX Gyre) plus Node 22
# and the agent runner on the pinned Cline SDK 0.2.0 (Apache-2.0, see agent/runner/NOTICE).
# One container per agent run: internal network (only the model relay is reachable), non-root, documents read-only,
# read-only root filesystem, no capabilities, resource limits (agent/spike/spike.py shows the flags).
# Build from the repository root: docker build -f sandbox/agent.Dockerfile -t hapilot-agent:0.4 .
# Existing tags are never rebuilt; a change gets a new tag (0.1: first build, its runner imported a name the SDK
# does not export; 0.2: stage 0 runner; 0.3: stage 2, Hapilot's MCP server agent/mcp/hapilot_mcp.py with
# src/working_days.py next to it; 0.4: stage 3, the runner adds Hapilot's rules to the system prompt).

# Node 22.23.4 from the official image, pinned by digest; npm only in this stage.
FROM node:22-bookworm@sha256:7702d8aa4d5910871692af4480eb3ba86f23047a6417a44d857d06c95d060d3f AS runner
WORKDIR /opt/hapilot-agent
COPY agent/runner/package.json agent/runner/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm cache clean --force

FROM hapilot-sandbox:0.2
# Command-line tools of the measured agent set-up: ripgrep, pdftotext (poppler-utils), git;
# Python readers for .docx and .pdf (pandas 2.3.3 and openpyxl 3.1.5 are already in the sandbox image).
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep poppler-utils git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir python-docx==1.2.0 pypdf==6.1.3
COPY --from=runner /usr/local/bin/node /usr/local/bin/node
COPY --from=runner /opt/hapilot-agent/node_modules /opt/hapilot-agent/node_modules
COPY agent/runner/package.json agent/runner/package-lock.json agent/runner/run_task.mjs agent/runner/egress_guard.mjs \
     agent/runner/NOTICE agent/runner/LICENSE-cline-sdk /opt/hapilot-agent/
COPY agent/mcp/hapilot_mcp.py src/working_days.py /opt/hapilot-agent/mcp/
RUN node --version && HOME=/tmp node /opt/hapilot-agent/run_task.mjs --self-check \
    && printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
       | HAPILOT_TOOLS=semantic_search,pdf_document,working_days,email_draft python3 /opt/hapilot-agent/mcp/hapilot_mcp.py
ENV DO_NOT_TRACK=1 OTEL_SDK_DISABLED=true NODE_ENV=production
WORKDIR /praca
CMD ["node", "--version"]

# Time per path

Measured on 2026-10-19 on the GPU host with [scripts/timing.py](../scripts/timing.py) in a fresh installation of
`v0.3.0` (the measured commit `v0.3.0-1-g120bb4e` adds only the timing script). Raw data:
[measurements/timing_2026-10-19.json](measurements/timing_2026-10-19.json). The same installation passed the
integration suite at `v0.3.0` the same day
([measurements/integration_2026-10-19_v0.3.0.json](measurements/integration_2026-10-19_v0.3.0.json)).

How to read the numbers:

- One request at a time, one user, approval policy `auto` (no forms or confirmations), model proxy already up.
  Times are wall-clock seconds from the request to the final answer, planner included; the UI adds little on top
  (the 9 demo steps through the UI took 179 s in total with `best-local`, see [demo.md](demo.md)).
- The smoke scenarios ([tests/scenarios/smoke.yaml](../tests/scenarios/smoke.yaml)) use each profile's temperature
  (0.3; 0 for code) and ran three times; the development tasks use the evaluation settings (temperature 0, seed 42)
  and ran once. "Plans seen" are the tools the planner chose; the planner, not the scenario, decides the path.
- Model loads were served from the operating system's page cache (warm): 2–3 s per load or swap. A first load
  from disk took 52 s for Bielik-11B and 120 s for Qwen3-Coder-30B-A3B on 2026-10-18 (README, *Model profiles*).
  In `reference`, every Python step swaps to the coder model and back (two loads, about 5 s).
- The knowledge base is the fictional demo company (7 documents); the 10-page upload is
  [examples/demo_company/uploads/raport_kwartalny_q3_2026.pdf](../examples/demo_company/uploads/raport_kwartalny_q3_2026.pdf).

## Results

Commit `v0.3.0-1-g120bb4e`; NVIDIA GeForce RTX 4090, 24564 MiB, 580.173.02; AMD Ryzen 9 7950X 16-Core Processor; 125 GB RAM; Python 3.12.3.
Smoke scenarios repeated 3×; times are wall-clock seconds per request (planner included).

### reference (bielik-11b-v3-q8, qwen3-coder-30b-a3b-q4km)

| Path | Scenario | Passed | Median (s) | Min (s) | Max (s) | Plans seen |
|---|---|---|---|---|---|---|
| conversation, no tools | `chat` | 3/3 | 7.3 | 7.3 | 7.3 | chat |
| knowledge search, short answer | `rag_leave` | 3/3 | 9.2 | 7.5 | 9.2 | rag_search |
| knowledge search, two facts | `rag_complaints` | 3/3 | 8.3 | 6.8 | 8.4 | rag_search |
| Python in the sandbox | `python_sum` | 3/3 | 6.1 | 5.9 | 6.1 | python |
| writer: e-mail text | `email_no_pdf` | 3/3 | 6.7 | 6.7 | 6.7 | llm_synthesis |
| document: letter as PDF | `pdf_letter` | 3/3 | 18.4 | 18.4 | 18.4 | rag_search → template_pdf |
| edit the generated PDF | `pdf_modify` | 3/3 | 8.4 | 7.9 | 8.4 | modify_document |
| summary of an uploaded 10-page PDF | `file_summary` | 3/3 | 80.3 | 80.3 | 80.3 | file_summary → rag_search → llm_synthesis |
| edit an uploaded Word file | `file_edit` | 3/3 | 11.0 | 11.0 | 11.0 | file_edit |
| add an uploaded file to the knowledge base | `file_to_knowledge` | 3/3 | 2.9 | 2.9 | 2.9 | hitl_confirm → file_to_knowledge |
| knowledge search incl. the added file | `rag_added_document` | 3/3 | 5.5 | 5.5 | 5.5 | rag_search |

| Development task | Status | Time (s) | Model calls | Prompt tokens | Completion tokens | Plan |
|---|---|---|---|---|---|---|
| D01 | ok | 11.33 | 4 | 10739 | 435 | rag_search |
| D02 | ok | 18.03 | 6 | 13619 | 828 | rag_search → python → llm_synthesis |
| D03 | ok | 23.48 | 6 | 14359 | 1125 | rag_search → python → llm_synthesis |
| D04 | ok | 12.27 | 5 | 12013 | 695 | rag_search → llm_synthesis |
| D05 | ok | 9.68 | 4 | 10914 | 544 | rag_search |
| D06 | ok | 9.64 | 3 | 4966 | 255 | python |
| D07 | ok | 27.89 | 5 | 13960 | 1661 | rag_search → template_pdf |
| D08 | ok | 6.38 | 4 | 10247 | 336 | rag_search |
| D09 | ok | 28.31 | 6 | 15026 | 1581 | rag_search → python → llm_synthesis |
| D10 | ok | 8.4 | 4 | 10549 | 442 | rag_search |
| **all 10** | | **155.4** (median 11.8) | 47 | 116392 | 7902 | |

| Model | Loads | First (s) | Median (s) | Max (s) |
|---|---|---|---|---|
| bielik-11b-v3-q8 | 8 | 2.0 | 2.0 | 2.0 |
| qwen3-coder-30b-a3b-q4km | 7 | 3.0 | 3.0 | 3.0 |

### best-local (qwen3.6-27b-q4km)

| Path | Scenario | Passed | Median (s) | Min (s) | Max (s) | Plans seen |
|---|---|---|---|---|---|---|
| conversation, no tools | `chat` | 3/3 | 10.7 | 10.5 | 10.7 | chat |
| knowledge search, short answer | `rag_leave` | 3/3 | 8.4 | 8.3 | 8.7 | rag_search |
| knowledge search, two facts | `rag_complaints` | 3/3 | 10.2 | 10.0 | 10.2 | rag_search |
| Python in the sandbox | `python_sum` | 3/3 | 5.6 | 5.5 | 5.6 | python |
| writer: e-mail text | `email_no_pdf` | 3/3 | 6.1 | 6.1 | 6.3 | llm_synthesis |
| document: letter as PDF | `pdf_letter` | 3/3 | 13.3 | 13.3 | 13.3 | template_pdf |
| edit the generated PDF | `pdf_modify` | 3/3 | 12.8 | 12.3 | 13.0 | modify_document |
| summary of an uploaded 10-page PDF | `file_summary` | 3/3 | 64.7 | 62.3 | 64.7 | file_summary → llm_synthesis |
| edit an uploaded Word file | `file_edit` | 3/3 | 12.9 | 12.9 | 13.0 | file_edit |
| add an uploaded file to the knowledge base | `file_to_knowledge` | 3/3 | 3.9 | 3.8 | 3.9 | file_to_knowledge |
| knowledge search incl. the added file | `rag_added_document` | 3/3 | 6.3 | 5.9 | 6.3 | rag_search |

| Development task | Status | Time (s) | Model calls | Prompt tokens | Completion tokens | Plan |
|---|---|---|---|---|---|---|
| D01 | ok | 18.51 | 5 | 8938 | 519 | rag_search → llm_synthesis |
| D02 | ok | 18.66 | 5 | 9218 | 692 | rag_search → python |
| D03 | ok | 14.8 | 5 | 8898 | 525 | rag_search → llm_synthesis |
| D04 | ok | 14.03 | 5 | 8993 | 491 | rag_search → llm_synthesis |
| D05 | ok | 17.02 | 5 | 9379 | 630 | rag_search → llm_synthesis |
| D06 | ok | 8.6 | 3 | 4016 | 288 | python |
| D07 | ok | 15.23 | 3 | 5704 | 576 | template_pdf |
| D08 | ok | 15.09 | 5 | 8989 | 542 | rag_search → llm_synthesis |
| D09 | ok | 15.27 | 5 | 8967 | 551 | rag_search → llm_synthesis |
| D10 | ok | 15.52 | 5 | 9152 | 560 | rag_search → llm_synthesis |
| **all 10** | | **152.7** (median 15.2) | 46 | 82254 | 5374 | |

| Model | Loads | First (s) | Median (s) | Max (s) |
|---|---|---|---|---|
| qwen3.6-27b-q4km | 1 | 3.2 | 3.2 | 3.2 |

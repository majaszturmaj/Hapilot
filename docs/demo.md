# 10-minute live demo

A scripted walk-through of Hapilot on the GPU host, with the fictional demo company
([examples/demo_company/](../examples/demo_company/)). Everything runs on the GPU host; the presenter's laptop
only opens the UI through an SSH tunnel. Timings are for the `best-local` profile on an RTX 4090; each step
says what to type, what the audience should notice, and what to do if it goes wrong.

## Before the audience arrives (5 minutes)

On the GPU host:

```bash
cd ~/Hapilot
.venv/bin/python scripts/hapilotctl.py status          # nothing running, GPU free
.venv/bin/python scripts/hapilotctl.py up --profile best-local
```

On the laptop:

```bash
ssh -N -L 8601:127.0.0.1:8601 <user>@<gpu-host>
```

Open http://localhost:8601, start a new chat and ask one warm-up question (the first answer loads the
embedding model). Keep the three files from
[examples/demo_company/uploads/](../examples/demo_company/uploads/) at hand on the laptop. Start every
step below in the same chat unless it says "new chat".

## Script

| Time | Step | Type or do | Point out |
|---|---|---|---|
| 0:00 | Welcome | Show the start screen and ⚙️ → model profile | Runs on one local GPU; the profile (model per role) is chosen per conversation |
| 0:45 | Question about company documents | `Ile dni przed urlopem wypoczynkowym trzeba złożyć wniosek?` | Answer **14 dni** with the source file `regulamin_urlopow.txt`; open the steps to show the plan and the search |
| 1:45 | Find, then write a document | `Sprawdź w umowie serwisowej karę za opóźnienie interwencji i przygotuj pismo do Serwis-Wymyślony Sp. z o.o. z wezwaniem do zapłaty kary za opóźnienie z 12 października 2026 r.` | The plan is shown for approval (more than two steps) → **Tak, działaj!**; the document form lets you check every field before the PDF is made |
| 3:30 | The PDF | Open the PDF, select and copy a sentence into the chat box | Polish letters copy correctly; signature line at the end |
| 4:00 | Edit the document | `Zmień w tym piśmie termin zapłaty na 7 dni.` | A new PDF; only the requested part changes |
| 4:45 | Calculation on data | `Ile reklamacji uznano w marcu 2026 r. według rejestru reklamacji i na jaką łączną kwotę?` | Python runs in an isolated container without network; the code reads the spreadsheet from the documents |
| 5:45 | Summarise an uploaded file (new chat) | Attach `raport_kwartalny_q3_2026.pdf` (📎) and type `Streść ten raport w pięciu punktach.` | A 10-page file is read whole, part by part: revenue 4 860 000 zł, operating profit 1 135 000 zł |
| 7:00 | Edit an uploaded Word file | Attach `projekt_umowy_sprzatanie.docx`, type `Popraw literówki i zmień termin płatności z 14 na 30 dni.` | A new .docx to download, same headings, list and table; the original stays unchanged |
| 8:00 | Add a file to the knowledge base | Attach `regulamin_parkingu.txt`, type `Dodaj ten regulamin do bazy wiedzy.` → confirm | Hapilot asks before it changes the shared knowledge base |
| 8:45 | Use the new knowledge (new chat) | `Gdzie parkują goście i ile jest dla nich miejsc?` | Answer **strefa B, 6 miejsc**, source `dodane/regulamin_parkingu.txt` |
| 9:30 | Privacy | Show the README section *Privacy* | No cloud APIs; endpoints must be local; the egress test runs the scenarios with outbound traffic blocked and recorded |

## If something goes wrong

| Problem | What to do |
|---|---|
| A step takes longer than a minute | Open the running step in the UI to show progress; models answer in turns of a few seconds each |
| The planner picks an unexpected tool | Say so (it is a language model planning the steps) and repeat the request with the tool's wording, e.g. "streść załączony plik" |
| The PDF form does not appear | The approval policy is `auto` in `.env`; the PDF is then made directly |
| The UI does not load | `hapilotctl.py status` on the GPU host; restart with `down` and `up` |

## After the demo

```bash
.venv/bin/python scripts/hapilotctl.py down
```

The parking policy stays in the knowledge base (`var/added_docs/`). To remove it, delete that file and
re-index: `hapilotctl.py ingest --force`.

## Automated run of the same steps

[scripts/ui_demo.py](../scripts/ui_demo.py) drives the same steps through the UI in a headless browser
(Playwright, not part of `requirements.txt`) over the SSH tunnel, checks the answers and saves a screenshot
per step, which is how the screenshots in the README were made:

```bash
pip install playwright && playwright install chromium     # on the laptop, once
python scripts/ui_demo.py --url http://localhost:8601 --out docs/img
```

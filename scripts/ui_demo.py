#!/usr/bin/env python3
"""Drive the demo (docs/demo.md) through the web UI in a headless browser; check the answers, save screenshots.

    pip install playwright && playwright install chromium        # once, on the machine with the browser
    python scripts/ui_demo.py --url http://localhost:8601 --out docs/img [--only 1,2] [--headed]

Run it on the laptop with the SSH tunnel to the GPU host open (Hapilot itself runs there). It uploads the files
from examples/demo_company/uploads through the UI's own upload button, answers the plan approval and the
document form the way a user would, and saves one screenshot per step (NN_name.png). Exit code 0 only if
every step's check passed. Playwright is not a Hapilot dependency.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPLOADS = ROOT / "examples" / "demo_company" / "uploads"

# (name, new chat?, file to attach, message, regexes the answer must match)
STEPS = [
    ("welcome", True, None, None, [r"Hapilot"]),
    ("rag_question", False, None, "Ile dni przed urlopem wypoczynkowym trzeba złożyć wniosek?", [r"\b14\b"]),
    ("find_then_letter", False, None,
     "Sprawdź w umowie serwisowej karę za opóźnienie interwencji i przygotuj pismo do Serwis-Wymyślony Sp. z o.o. "
     "z wezwaniem do zapłaty kary za opóźnienie z 12 października 2026 r.", [r"\.pdf"]),
    ("edit_letter", False, None, "Zmień w tym piśmie termin zapłaty na 7 dni.", [r"\.pdf"]),
    ("python_register", False, None,
     "Ile reklamacji uznano w marcu 2026 r. według rejestru reklamacji i na jaką łączną kwotę?", [r"\b2\b", r"2[  ]?280"]),
    ("summarize_upload", True, "raport_kwartalny_q3_2026.pdf", "Streść ten raport w pięciu punktach.",
     [r"4[  ]?860[  ]?000|4,86\s*mln", r"1[  ]?135[  ]?000|1,1\d?\s*mln"]),
    ("edit_upload", False, "projekt_umowy_sprzatanie.docx", "Popraw literówki i zmień termin płatności z 14 na 30 dni.",
     [r"\.docx"]),
    ("add_to_knowledge", False, "regulamin_parkingu.txt", "Dodaj ten regulamin do bazy wiedzy.", [r"Dodano do bazy wiedzy"]),
    ("ask_new_knowledge", True, None, "Gdzie parkują goście i ile jest dla nich miejsc?", [r"(?i)stref\w* B", r"\b6\b"]),
]

BUTTONS = ["Tak, działaj!", "Generuj PDF"]  # plan approval / confirmation, document form
RUNNING_LABEL = "Wykonuję kroki zadania"     # .chainlit/translations/pl-PL.json "using" + the step name in src/app.py


def wait_for_answer(page, timeout_s: float) -> list[str]:
    """Wait until the run ends, answering approvals and forms on the way; returns the buttons clicked."""
    clicked, idle_since, deadline = [], None, time.monotonic() + timeout_s
    seen = {label: page.get_by_role("button", name=label).count() for label in BUTTONS}  # buttons of earlier steps
    while time.monotonic() < deadline:
        for label in BUTTONS:
            button = page.get_by_role("button", name=label)
            # only a button that appeared in this step, once: a button left on screen is never clicked twice
            if button.count() > seen[label] and button.last.is_visible() and button.last.is_enabled():
                before = button.count()
                button.last.click()
                clicked.append(label)
                idle_since = None
                for _ in range(10):  # the UI removes a question's buttons once one is clicked
                    if button.count() < before:
                        break
                    time.sleep(0.5)
                seen[label] = button.count()
        # after a button click the run continues in a callback: no stop button then, but the step shows "Wykonuję"
        running = page.locator("#stop-button").count() > 0 or page.get_by_text(RUNNING_LABEL).count() > 0
        if running:
            idle_since = None
        elif idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since > 4:
            return clicked
        time.sleep(0.5)
    raise TimeoutError(f"no answer within {timeout_s:.0f} s")


def last_answer(page) -> str:
    messages = page.locator('[data-step-type="assistant_message"]')
    return messages.last.inner_text() if messages.count() else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8601")
    parser.add_argument("--out", type=Path, default=ROOT / "var" / "ui_demo")
    parser.add_argument("--only", help="comma-separated step numbers (1 = first step)")
    parser.add_argument("--timeout", type=float, default=600, help="seconds per step")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    args.out.mkdir(parents=True, exist_ok=True)
    wanted = {int(n) for n in args.only.split(",")} if args.only else None
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1280, "height": 860}, locale="pl-PL")
        for number, (name, new_chat, upload, message, checks) in enumerate(STEPS, 1):
            if wanted and number not in wanted:
                continue
            t0 = time.monotonic()
            if new_chat or not results:
                page.goto(args.url)
                page.wait_for_selector("#chat-input", timeout=60_000)
                page.wait_for_selector('[data-step-type="assistant_message"]', timeout=60_000)  # welcome message
            clicked, error = [], None
            try:
                if upload:
                    page.locator("#upload-button-input").set_input_files(str(UPLOADS / upload))
                    page.wait_for_timeout(1500)
                if message:
                    before = page.locator('[data-step-type="assistant_message"]').count()
                    page.locator("#chat-input").fill(message)
                    page.locator("#chat-submit").click()
                    page.wait_for_function(
                        f"document.querySelectorAll('[data-step-type=\"assistant_message\"]').length > {before}"
                        " || document.querySelector('#stop-button') !== null", timeout=60_000)
                    clicked = wait_for_answer(page, args.timeout)
            except Exception as exc:  # report and go on with the next step
                error = f"{type(exc).__name__}: {exc}"
            answer = last_answer(page)
            failed = [error] if error else [p for p in checks if not re.search(p, answer)]
            shot = args.out / f"{number:02d}_{name}.png"
            page.screenshot(path=str(shot))
            results.append({"step": number, "name": name, "ok": not failed, "failed": failed, "clicked": clicked,
                            "seconds": round(time.monotonic() - t0, 1), "answer_excerpt": answer[:300], "screenshot": shot.name})
            print(f"{'PASS' if not failed else 'FAIL'}  {number:2d} {name:20s} {results[-1]['seconds']:6.1f}s  "
                  f"{clicked or ''} {'; '.join(failed)}", flush=True)
        browser.close()
    (args.out / "ui_demo.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    passed = sum(r["ok"] for r in results)
    print(f"{passed}/{len(results)} steps passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

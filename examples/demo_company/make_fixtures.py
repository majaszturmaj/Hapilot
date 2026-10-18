#!/usr/bin/env python3
"""(Re)generate the binary demo documents of the fictional company Przykładowa Sp. z o.o.

    .venv/bin/python examples/demo_company/make_fixtures.py

Writes docs/umowa_serwisowa_7_2026.docx, docs/cennik_uslug_2026.pdf (via LibreOffice, from a
temporary .docx), docs/zgloszenie_awarii_2026-10-12.eml, docs/rejestr_reklamacji_2026.xlsx, and the
evaluation attachment tests/eval/attachments/rozliczenie_delegacji.xlsx. All content is invented;
e-mail addresses use example.com.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from docx import Document
from openpyxl import Workbook

HERE = Path(__file__).resolve().parent
DOCS = HERE / "docs"
ROOT = HERE.parent.parent
COMPANY = "Przykładowa Sp. z o.o., ul. Zmyślona 7, 50-001 Wrocław"


def service_agreement() -> None:
    doc = Document()
    doc.add_heading("Umowa serwisowa nr 7/2026", level=1)
    doc.add_paragraph(f"Zawarta 2 stycznia 2026 r. między {COMPANY} (Zamawiający) a Serwis-Wymyślony Sp. z o.o. "
                      "(Wykonawca).")
    doc.add_paragraph("§ 1. Przedmiot umowy: serwis serwerów i drukarek Zamawiającego w siedzibie we Wrocławiu.")
    doc.add_paragraph("§ 2. Zakres i ceny usług:")
    table = doc.add_table(rows=1, cols=3)
    for i, h in enumerate(["Usługa", "Cena netto", "Częstotliwość"]):
        table.rows[0].cells[i].text = h
    for row in [("Przegląd serwerowni", "1 200 zł", "raz na kwartał"), ("Przegląd drukarek", "450 zł", "raz na pół roku"),
                ("Interwencja awaryjna", "300 zł za godzinę", "na zgłoszenie")]:
        cells = table.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = v
    doc.add_paragraph("§ 3. Czas reakcji: Wykonawca podejmuje interwencję awaryjną w ciągu 8 godzin roboczych od zgłoszenia "
                      "na adres serwis@example.com.")
    doc.add_paragraph("§ 4. Kara umowna: za każdą rozpoczętą godzinę opóźnienia ponad czas reakcji Wykonawca zapłaci 100 zł.")
    doc.add_paragraph("§ 5. Umowa obowiązuje do 31 grudnia 2026 r. z miesięcznym okresem wypowiedzenia.")
    doc.save(DOCS / "umowa_serwisowa_7_2026.docx")


def price_list_pdf() -> None:
    soffice = shutil.which("soffice")
    if not soffice:
        print("LibreOffice (soffice) not found — skipping the PDF")
        return
    doc = Document()
    doc.add_heading("Cennik usług biurowych 2026", level=1)
    doc.add_paragraph(f"{COMPANY}. Ceny netto, obowiązują od 1 lutego 2026 r. Do cen doliczamy 23% VAT.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Usługa", "Cena netto"
    for name, price in [("Tłumaczenie strony A4 (polski–angielski)", "60 zł"), ("Skład raportu (do 20 stron)", "350 zł"),
                        ("Archiwizacja dokumentów (karton)", "25 zł"), ("Digitalizacja (100 stron)", "40 zł")]:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = name, price
    doc.add_paragraph("Zamówienia powyżej 2 000 zł netto realizujemy z rabatem 5%.")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "cennik_uslug_2026.docx"
        doc.save(src)
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(src)], check=True,
                       capture_output=True, timeout=120)
        shutil.copy(Path(tmp) / "cennik_uslug_2026.pdf", DOCS / "cennik_uslug_2026.pdf")


def failure_report_eml() -> None:
    msg = EmailMessage()
    msg["From"] = "Anna Nowak <anna.nowak@example.com>"
    msg["To"] = "helpdesk@example.com"
    msg["Date"] = "Mon, 12 Oct 2026 08:47:00 +0200"
    msg["Subject"] = "Awaria drukarki w Dziale Sprzedaży"
    msg.set_content("Dzień dobry,\n\nod rana drukarka sieciowa DR-2 w Dziale Sprzedaży (pokój 14) nie drukuje — "
                    "wyświetla błąd E-52. Restart nie pomógł. Prosimy o interwencję serwisu.\n\n"
                    "Pozdrawiam\nAnna Nowak\nDział Sprzedaży")
    msg.add_attachment("2026-10-12 08:15 DR-2 error E-52 (fuser)\n2026-10-12 08:31 DR-2 error E-52 (fuser)\n",
                       filename="dziennik_drukarki.txt")
    (DOCS / "zgloszenie_awarii_2026-10-12.eml").write_bytes(msg.as_bytes())


def complaints_register_xlsx() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Rejestr 2026"
    ws.append(["Nr", "Data wpływu", "Klient", "Kwota (zł)", "Decyzja", "Data odpowiedzi"])
    rows = [("R-01", date(2026, 3, 2), "Hurtownia Wymyślona", 1500, "uznana", date(2026, 3, 12)),
            ("R-02", date(2026, 3, 9), "Biuro Testowe", 320, "odrzucona", date(2026, 3, 20)),
            ("R-03", date(2026, 3, 23), "Hurtownia Wymyślona", 780, "uznana", date(2026, 4, 3)),
            ("R-04", date(2026, 4, 14), "Drukarnia Przykład", 2100, "uznana", date(2026, 4, 30)),
            ("R-05", date(2026, 5, 6), "Biuro Testowe", 95, "odrzucona", date(2026, 5, 15))]
    for r in rows:
        ws.append(list(r))
    wb.save(DOCS / "rejestr_reklamacji_2026.xlsx")


def delegation_attachment_xlsx() -> None:
    out = ROOT / "tests" / "eval" / "attachments"
    out.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Delegacje październik"
    ws.append(["Pracownik", "Cel", "Koszt (zł)"])
    for r in [("Jan Kowalski", "Poznań", 412.50), ("Anna Nowak", "Kraków", 389.00), ("Piotr Testowy", "Gdańsk", 655.25),
              ("Ewa Przykładowa", "Łódź", 243.00)]:
        ws.append(list(r))
    wb.save(out / "rozliczenie_delegacji.xlsx")


if __name__ == "__main__":
    DOCS.mkdir(parents=True, exist_ok=True)
    service_agreement()
    price_list_pdf()
    failure_report_eml()
    complaints_register_xlsx()
    delegation_attachment_xlsx()
    print("fixtures written to", DOCS)

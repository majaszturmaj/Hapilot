"""Polish holidays and working-day arithmetic (src/working_days.py), the `working_days` tool of the v3 agent."""

from datetime import date

import pytest

from src import working_days as w


@pytest.mark.parametrize("year, day", [(2011, date(2011, 4, 24)), (2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)),
                                       (2026, date(2026, 4, 5)), (2027, date(2027, 3, 28)), (2030, date(2030, 4, 21))])
def test_easter(year, day):
    assert w.easter(year) == day


def test_holiday_list_and_christmas_eve_from_2025():
    h2026 = w.holidays(2026)
    assert len(h2026) == 14 and len(w.holidays(2024)) == 13
    assert date(2024, 12, 24) not in w.holidays(2024)
    assert h2026[date(2026, 12, 24)] == "Wigilia Bożego Narodzenia"
    assert h2026[date(2026, 5, 24)] == "Zielone Świątki" and h2026[date(2026, 6, 4)] == "Boże Ciało"
    assert date(2026, 1, 6) in h2026 and date(2026, 11, 11) in h2026


def test_working_day_and_reason():
    assert w.is_working_day(date(2026, 11, 18))
    assert w.why_not_working(date(2025, 12, 24)) == "Wigilia Bożego Narodzenia"
    assert w.why_not_working(date(2026, 11, 14)) == "sobota"
    assert w.is_day_off(date(2026, 11, 15)) and not w.is_day_off(date(2026, 11, 14))  # Sunday yes, Saturday no


def test_add_working_days_skips_holidays_and_excludes_the_start():
    end, skipped = w.add_working_days(date(2026, 11, 3), 10)  # Tue 3 Nov 2026, 11 Nov is a holiday
    assert end == date(2026, 11, 18) and skipped == [(date(2026, 11, 11), "Narodowe Święto Niepodległości")]
    assert w.add_working_days(date(2026, 11, 13), 1)[0] == date(2026, 11, 16)  # Friday → Monday
    assert w.add_working_days(date(2026, 11, 18), -10)[0] == date(2026, 11, 3)
    assert w.add_working_days(date(2026, 11, 3), 0)[0] == date(2026, 11, 3)


def test_count_includes_both_ends():
    count, skipped = w.count_working_days(date(2026, 12, 1), date(2026, 12, 31))
    assert count == 21 and [d.day for d, _ in skipped] == [24, 25]
    assert w.count_working_days(date(2026, 12, 2), date(2026, 12, 1))[0] == 0


def test_civil_code_deadlines():
    r = w.deadline(date(2026, 12, 10), 14)  # ends on Wigilia, then 25, 26 (Saturday), 27 (Sunday)
    assert r["nominal"] == date(2026, 12, 24) and r["end"] == date(2026, 12, 28) and len(r["moved_over"]) == 4
    assert w.deadline(date(2026, 1, 31), 1, "miesiace")["nominal"] == date(2026, 2, 28)  # no 31 February
    assert w.deadline(date(2026, 1, 31), 1, "miesiace")["end"] == date(2026, 3, 2)
    assert w.deadline(date(2026, 3, 3), 2, "tygodnie")["end"] == date(2026, 3, 17)
    assert w.deadline(date(2024, 2, 29), 1, "lata")["nominal"] == date(2025, 2, 28)
    assert w.deadline(date(2026, 11, 4), 3)["end"] == date(2026, 11, 9)  # Saturday 7 → Monday 9


def test_tool_results_and_errors():
    r = w.run({"operation": "add_working_days", "date": "2026-11-03", "days": "10"})
    assert r["wynik"] == "2026-11-18 (środa)" and r["pominiete_swieta"] == ["2026-11-11 (środa): Narodowe Święto Niepodległości"]
    assert w.run({"operation": "holidays", "year": 2025})["swieta"][-3].startswith("2025-12-24")
    assert w.run({"operation": "holidays", "date": "2027-05-01"})["rok"] == 2027
    assert w.run({"operation": "is_working_day", "date": "2026-11-11"})["dzien_roboczy"] is False
    assert w.run({"operation": "deadline", "date": "2026-12-10", "days": 14})["termin_uplywa"] == "2026-12-28 (poniedziałek)"
    for bad, message in [({"operation": "x"}, "nieznana operacja"), ({"operation": "is_working_day"}, "brak pola date"),
                         ({"operation": "is_working_day", "date": "3.11.2026"}, "nie jest datą"),
                         ({"operation": "add_working_days", "date": "2026-11-03"}, "brak pola days"),
                         ({"operation": "holidays", "year": 2010}, "poza obsługiwanym zakresem"),
                         ({"operation": "deadline", "date": "2026-11-03", "days": 3, "unit": "godziny"}, "jednostka")]:
        with pytest.raises(w.WorkingDaysError, match=message):
            w.run(bad)


def test_schema_names_every_operation():
    assert w.SCHEMA["properties"]["operation"]["enum"] == list(w.OPERATIONS)
    assert set(w.SCHEMA["required"]) <= set(w.SCHEMA["properties"])

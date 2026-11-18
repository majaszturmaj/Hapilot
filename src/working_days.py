"""Polish public holidays and working-day arithmetic, deterministic and standard-library only.

Also copied into the v3 agent image next to its MCP server (agent/mcp/), where it backs the `working_days` tool.

Days off by law (ustawa z 18 stycznia 1951 r. o dniach wolnych od pracy, art. 1): Sundays and 1 Jan, 6 Jan, Easter
Sunday and Monday, 1 May, 3 May, Pentecost Sunday, Corpus Christi, 15 Aug, 1 Nov, 11 Nov, 24 Dec, 25 Dec, 26 Dec.
6 January is a day off again since 2011 (Dz.U. 2010 nr 224 poz. 1459, in force 1 Jan 2011); 24 December since 2025
(Dz.U. 2024 poz. 1965, in force 1 Feb 2025). Years before 2011 are refused rather than computed with a wrong list.

A working day here is Monday to Friday and not a day off by law (office sense). Deadlines in days, weeks, months or
years follow the Civil Code: the starting day is not counted (art. 111 § 2), weeks/months/years end on the day with the
same name or date, or on the month's last day (art. 112), and an end on a day off by law or a Saturday moves to the
next day that is neither (art. 115).
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

FIRST_YEAR = 2011
LAST_YEAR = 2099
MAX_SPAN_DAYS = 366 * 20
WEEKDAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
UNITS = ("dni", "tygodnie", "miesiace", "lata")
OPERATIONS = ("add_working_days", "count_working_days", "is_working_day", "deadline", "holidays")


class WorkingDaysError(ValueError):
    """Bad input: the message is meant for the model and the user."""


def easter(year: int) -> date:
    """Easter Sunday (Gregorian calendar; anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    day = (h + m - 7 * n + 114) % 31 + 1
    return date(year, month, day)


def _check_year(year: int) -> None:
    if not FIRST_YEAR <= year <= LAST_YEAR:
        raise WorkingDaysError(f"rok {year} poza obsługiwanym zakresem {FIRST_YEAR}–{LAST_YEAR}")


def holidays(year: int) -> dict[date, str]:
    """Public holidays of a year (days off by law other than ordinary Sundays), by date."""
    _check_year(year)
    e = easter(year)
    days = {
        date(year, 1, 1): "Nowy Rok",
        date(year, 1, 6): "Święto Trzech Króli",
        e: "Wielkanoc",
        e + timedelta(days=1): "Poniedziałek Wielkanocny",
        date(year, 5, 1): "Święto Pracy",
        date(year, 5, 3): "Święto Konstytucji 3 Maja",
        e + timedelta(days=49): "Zielone Świątki",
        e + timedelta(days=60): "Boże Ciało",
        date(year, 8, 15): "Wniebowzięcie Najświętszej Maryi Panny",
        date(year, 11, 1): "Wszystkich Świętych",
        date(year, 11, 11): "Narodowe Święto Niepodległości",
        date(year, 12, 25): "Boże Narodzenie (pierwszy dzień)",
        date(year, 12, 26): "Boże Narodzenie (drugi dzień)",
    }
    if year >= 2025:
        days[date(year, 12, 24)] = "Wigilia Bożego Narodzenia"
    return dict(sorted(days.items()))


def holiday_name(d: date) -> str | None:
    return holidays(d.year).get(d)


def is_day_off(d: date) -> bool:
    """A day off by law: a Sunday or a public holiday."""
    return d.weekday() == 6 or holiday_name(d) is not None


def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and holiday_name(d) is None


def why_not_working(d: date) -> str | None:
    """None for a working day, else the reason ("sobota", "niedziela" or the holiday's name)."""
    name = holiday_name(d)
    if name:
        return name
    if d.weekday() >= 5:
        return WEEKDAYS[d.weekday()]
    return None


def add_working_days(start: date, n: int) -> tuple[date, list[tuple[date, str]]]:
    """The n-th working day after ``start`` (before it for n < 0); ``start`` itself is not counted. Also returns the
    holidays falling on weekdays that were skipped on the way."""
    if abs(n) > MAX_SPAN_DAYS:
        raise WorkingDaysError(f"za duża liczba dni: {n}")
    step = 1 if n >= 0 else -1
    d, left, skipped = start, abs(n), []
    _check_year(start.year)
    while left:
        d += timedelta(days=step)
        name = holiday_name(d)
        if d.weekday() < 5 and name is None:
            left -= 1
        elif name and d.weekday() < 5:
            skipped.append((d, name))
    return d, skipped


def count_working_days(first: date, last: date) -> tuple[int, list[tuple[date, str]]]:
    """Working days from ``first`` to ``last``, both included (0 when ``last`` is before ``first``); also the holidays
    falling on weekdays in that range."""
    _check_year(first.year)
    _check_year(last.year)
    if (last - first).days > MAX_SPAN_DAYS:
        raise WorkingDaysError("za długi przedział")
    count, skipped, d = 0, [], first
    while d <= last:
        name = holiday_name(d)
        if d.weekday() < 5 and name is None:
            count += 1
        elif name and d.weekday() < 5:
            skipped.append((d, name))
        d += timedelta(days=1)
    return count, skipped


def _add_months(start: date, months: int) -> date:
    y, m = divmod(start.month - 1 + months, 12)
    year, month = start.year + y, m + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def deadline(start: date, amount: int, unit: str = "dni") -> dict:
    """End of a Civil Code deadline counted from the event on ``start`` (arts. 111, 112, 115 KC)."""
    if unit not in UNITS:
        raise WorkingDaysError(f"nieznana jednostka {unit!r}; dozwolone: {', '.join(UNITS)}")
    if not 0 <= amount <= 5000:
        raise WorkingDaysError("liczba jednostek musi być od 0 do 5000")
    _check_year(start.year)
    if unit == "dni":
        nominal = start + timedelta(days=amount)
    elif unit == "tygodnie":
        nominal = start + timedelta(weeks=amount)
    elif unit == "miesiace":
        nominal = _add_months(start, amount)
    else:
        nominal = _add_months(start, 12 * amount)
    _check_year(nominal.year)
    end, moved = nominal, []
    while end.weekday() >= 5 or holiday_name(end):
        moved.append((end, why_not_working(end)))
        end += timedelta(days=1)
    return {"nominal": nominal, "end": end, "moved_over": moved}


# --------------------------------------------------------------------------- the tool

SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string", "enum": list(OPERATIONS),
            "description": "add_working_days: date + N working days (start day not counted; N < 0 goes back). "
                           "count_working_days: working days from date to end_date, both included. "
                           "is_working_day: is the date a working day, and why not. "
                           "deadline: end of a Civil Code deadline of N days/weeks/months/years counted from the "
                           "event on date (the event day is not counted; an end on a Saturday or a day off by law "
                           "moves to the next working day). holidays: public holidays of a year.",
        },
        "date": {"type": "string", "description": "YYYY-MM-DD (start date / the date to check)"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD, for count_working_days"},
        "days": {"type": "integer", "description": "N for add_working_days (may be negative) and deadline"},
        "unit": {"type": "string", "enum": list(UNITS), "description": "deadline unit (default dni)"},
        "year": {"type": "integer", "description": "for holidays"},
    },
    "required": ["operation"],
    "additionalProperties": False,
}

DESCRIPTION = ("Polish working days and public holidays, computed exactly (Mon–Fri except days off by law, "
               "including 24 December since 2025). Use it for every date that depends on working days, holidays "
               "or a legal deadline instead of counting by hand.")


def _date(value, field: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise WorkingDaysError(f"brak pola {field} (RRRR-MM-DD)")
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        raise WorkingDaysError(f"pole {field}: {value!r} nie jest datą RRRR-MM-DD") from None


def _int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise WorkingDaysError(f"brak pola {field} (liczba całkowita)")
    try:
        number = int(str(value).strip())
    except ValueError:
        raise WorkingDaysError(f"pole {field}: {value!r} nie jest liczbą całkowitą") from None
    return number


def _day(d: date) -> str:
    return f"{d.isoformat()} ({WEEKDAYS[d.weekday()]})"


def _list(pairs: list[tuple[date, str]]) -> list[str]:
    return [f"{_day(d)}: {name}" for d, name in pairs]


def run(args: dict) -> dict:
    """The `working_days` tool: arguments as in SCHEMA, a JSON-ready result with Polish keys."""
    if not isinstance(args, dict):
        raise WorkingDaysError("argumenty muszą być obiektem JSON")
    op = args.get("operation")
    if op not in OPERATIONS:
        raise WorkingDaysError(f"nieznana operacja {op!r}; dozwolone: {', '.join(OPERATIONS)}")
    if op == "holidays":
        year = args.get("year")
        if year is None and isinstance(args.get("date"), str):
            year = args["date"].strip()[:4]
        year = _int(year, "year")
        return {"rok": year, "swieta": [f"{_day(d)}: {n}" for d, n in holidays(year).items()],
                "uwaga": "poza świętami dniem wolnym od pracy z mocy ustawy jest każda niedziela"}
    start = _date(args.get("date"), "date")
    if op == "is_working_day":
        reason = why_not_working(start)
        return {"data": _day(start), "dzien_roboczy": reason is None, "powod": reason}
    if op == "add_working_days":
        n = _int(args.get("days"), "days")
        end, skipped = add_working_days(start, n)
        return {"start": _day(start), "dni_robocze": n, "wynik": _day(end), "pominiete_swieta": _list(skipped),
                "zasada": "dnia startowego nie liczy się; liczą się poniedziałki–piątki poza świętami"}
    if op == "count_working_days":
        last = _date(args.get("end_date"), "end_date")
        count, skipped = count_working_days(start, last)
        return {"od": _day(start), "do": _day(last), "dni_robocze": count, "swieta_w_dni_powszednie": _list(skipped),
                "zasada": "oba końce przedziału wliczone"}
    n = _int(args.get("days"), "days")
    unit = args.get("unit") or "dni"
    r = deadline(start, n, unit)
    return {"zdarzenie": _day(start), "termin": f"{n} {unit}", "koniec_wyliczony": _day(r["nominal"]),
            "termin_uplywa": _day(r["end"]), "przesuniety_przez": _list(r["moved_over"]),
            "zasada": "art. 111 § 2 KC (dnia zdarzenia nie wlicza się), art. 112 KC, art. 115 KC (koniec w sobotę "
                      "lub dzień wolny od pracy przechodzi na następny dzień, który nim nie jest)"}

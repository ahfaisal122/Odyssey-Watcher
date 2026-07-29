#!/usr/bin/env python3
"""
odyssey_watch.py — watch Cinema City Flora (Prague) for newly listed
screenings of "Odyssea" / The Odyssey and push a notification with a
direct booking link.

It notifies. It does not buy. You click the link and complete checkout
yourself — seat choice and payment stay with you.

Usage:
    pip install requests
    export NTFY_TOPIC=odyssey-flora-<something-random>
    python3 odyssey_watch.py            # run once (for cron / GH Actions)
    python3 odyssey_watch.py --loop     # run forever, self-scheduling

Subscribe on your phone: install the ntfy app, add the same topic.
Pick a topic string nobody would guess — ntfy topics are public.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

# --- configuration -----------------------------------------------------

CINEMA_IDS = ["1052"]       # Cinema City Flora, Praha. Add more ids if needed.
BASE = "https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/10101"
LANG = "cs_CZ"

# Film id, taken straight from the booking URL (?for-movie=7268s2r).
FILM_ID = "7268s2r"
# Fallback if the id ever changes: substring match on the title.
FILM_MATCH = ("odyss",)

# The site's own filter slug is "70-mm". Related ids may appear as
# "imax-70mm" or "70mm", so normalise before comparing.
WANT_FORMAT = "70mm"        # set to None to accept any format
ATTR_FILTER = "70-mm"       # site's own slug, passed straight through as attr=

DAYS_AHEAD = 60             # how far forward to look
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen_events.json"))
HORIZON_LOG = Path(os.environ.get("HORIZON_LOG", "horizon.csv"))
REQUEST_GAP = 0.6           # seconds between requests — be polite
TIMEOUT = 20

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": "personal-showtime-watcher/1.0 (single user, polite interval)",
    "Accept": "application/json",
}

# --- api ---------------------------------------------------------------


def _get(url: str) -> dict:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def norm(s: str) -> str:
    """'70-mm' -> '70mm', 'IMAX-70MM' -> 'imax70mm'."""
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def fetch_dates(cinema_id: str, attr: str | None = None) -> list[str]:
    """Dates on which this cinema has anything scheduled. One cheap request.

    Passing attr (e.g. "70-mm") asks the API to return only dates having
    a screening with that attribute. If the server ignores or rejects the
    filter we fall back to the unfiltered list rather than going blind.
    """
    until = (date.today() + timedelta(days=DAYS_AHEAD)).isoformat()
    a = attr or ""
    url = f"{BASE}/dates/in-cinema/{cinema_id}/until/{until}?attr={a}&lang={LANG}"
    try:
        body = _get(url).get("body", {})
        dates = sorted(body.get("dates", []))
    except requests.RequestException:
        if not attr:
            raise
        dates = []

    if attr and not dates:
        print(f"[warn] attr={attr!r} gave no dates; falling back to unfiltered",
              file=sys.stderr)
        return fetch_dates(cinema_id, None)
    return dates


def fetch_events(cinema_id: str, day: str) -> list[dict]:
    """All screenings on one date, joined to their film titles."""
    url = f"{BASE}/film-events/in-cinema/{cinema_id}/at-date/{day}?attr=&lang={LANG}"
    body = _get(url).get("body", {})
    films = {f.get("id"): f for f in body.get("films", [])}

    out = []
    for ev in body.get("events", []):
        film_id = ev.get("filmId")
        film = films.get(film_id, {})
        title = film.get("name", "")

        is_ours = (FILM_ID and film_id == FILM_ID) or any(
            m in title.lower() for m in FILM_MATCH
        )
        if not is_ours:
            continue

        raw = list(ev.get("attributeIds") or []) + list(film.get("attributeIds") or [])
        attrs = {norm(a) for a in raw}
        if WANT_FORMAT and not any(WANT_FORMAT in a for a in attrs):
            continue
        if ev.get("soldOut"):
            continue

        out.append(
            {
                "id": ev.get("id"),
                "title": title,
                "when": ev.get("eventDateTime", ""),
                "auditorium": ev.get("auditoriumTinyName") or ev.get("auditorium", ""),
                "attrs": sorted(attrs),
                "link": ev.get("bookingLink", ""),
            }
        )
    return out


# --- notification ------------------------------------------------------


def notify(title: str, message: str, link: str = "") -> None:
    sent = False

    if NTFY_TOPIC:
        headers = {
            "Title": title.encode("utf-8"),
            "Priority": "urgent",
            "Tags": "clapper",
        }
        if link:
            headers["Click"] = link
        try:
            requests.post(
                f"{NTFY_SERVER}/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers=headers,
                timeout=TIMEOUT,
            )
            sent = True
        except requests.RequestException as e:
            print(f"[warn] ntfy failed: {e}", file=sys.stderr)

    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={
                    "chat_id": TG_CHAT,
                    "text": f"*{title}*\n{message}\n{link}",
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False,
                },
                timeout=TIMEOUT,
            )
            sent = True
        except requests.RequestException as e:
            print(f"[warn] telegram failed: {e}", file=sys.stderr)

    if not sent:
        print(f"[notify] {title}\n{message}\n{link}")


# --- state -------------------------------------------------------------


def load_seen() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            print("[warn] state file unreadable, starting fresh", file=sys.stderr)
    return set()


def save_seen(seen: set[str]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen)))
    tmp.replace(STATE_FILE)


# --- main --------------------------------------------------------------


def pretty(ev: dict) -> str:
    try:
        dt = datetime.fromisoformat(ev["when"])
        stamp = dt.strftime("%a %d.%m. %H:%M")
    except (ValueError, KeyError):
        stamp = ev.get("when", "?")
    fmt = ", ".join(a for a in ev["attrs"] if "70mm" in a or "imax" in a) or "—"
    hall = f" · {ev['auditorium']}" if ev["auditorium"] else ""
    return f"{stamp} · {fmt}{hall}"


def log_horizon(found: list[dict]) -> str | None:
    """Append the furthest bookable date to a CSV.

    Two observations aren't enough to know the release pattern. This
    builds the dataset: after a few Wednesdays the increment size and
    cadence become obvious from the file.
    """
    days = sorted({e["when"][:10] for e in found if e.get("when")})
    horizon = days[-1] if days else None

    header = not HORIZON_LOG.exists()
    with HORIZON_LOG.open("a") as fh:
        if header:
            fh.write("checked_at,weekday,horizon,horizon_weekday,n_screenings\n")
        now = datetime.now()
        hw = ""
        if horizon:
            try:
                hw = datetime.fromisoformat(horizon).strftime("%a")
            except ValueError:
                pass
        fh.write(
            f"{now.isoformat(timespec='seconds')},{now.strftime('%a')},"
            f"{horizon or ''},{hw},{len(found)}\n"
        )
    return horizon


def check_once(verbose: bool = True) -> int:
    seen = load_seen()
    first_run = not seen

    found: list[dict] = []
    scanned = 0

    for cinema_id in CINEMA_IDS:
        try:
            days = fetch_dates(cinema_id, ATTR_FILTER)
        except requests.RequestException as e:
            print(f"[error] dates for cinema {cinema_id}: {e}", file=sys.stderr)
            continue

        scanned += len(days)
        for day in days:
            try:
                found.extend(fetch_events(cinema_id, day))
            except requests.RequestException as e:
                print(f"[warn] {cinema_id} {day}: {e}", file=sys.stderr)
            time.sleep(REQUEST_GAP)

    if not scanned:
        return 0

    horizon = log_horizon(found)
    new = [e for e in found if e["id"] and e["id"] not in seen]

    if verbose:
        ts = datetime.now().strftime("%H:%M:%S")
        print(
            f"[{ts}] {scanned} days scanned, {len(found)} matching, "
            f"{len(new)} new, horizon {horizon or '—'}"
        )

    if new and first_run:
        # Don't spam on the very first run — just record the baseline.
        print(f"[init] baseline recorded: {len(new)} existing screenings, no alerts sent")
    elif new:
        new.sort(key=lambda e: e["when"])
        lines = [pretty(e) for e in new]
        notify(
            f"🎬 {len(new)} new Odyssey screening(s) at Flora",
            "\n".join(lines) + "\n\nGo book — these sell out fast.",
            new[0]["link"],
        )
        for e in new:
            print(f"  NEW  {pretty(e)}  {e['link']}")

    save_seen(seen | {e["id"] for e in found if e["id"]})
    return len(new)


def interval_seconds(now: datetime) -> int:
    """Poll hard around the Wednesday drop.

    Cinema City's own date picker states a new schedule appears every
    Wednesday, and the release unit is a cinema-week running Thursday
    through Wednesday. The exact hour isn't published, so cover from
    Tuesday midnight through the whole of Wednesday.
    """
    dow, hour = now.weekday(), now.hour        # Mon=0 ... Sun=6

    if dow == 1 and hour >= 22:                # Tue late — drop may be at 00:00
        return 60
    if dow == 2:                               # all of Wednesday
        return 60 if hour < 14 else 300
    if 7 <= hour < 23:
        return 900
    return 3600


def probe() -> None:
    """Verify assumptions before trusting the watcher.

    Prints what the API actually returns: which dates exist, which films
    are listed, and the exact attribute strings. Use this to confirm the
    cinema id, film id and format slug are right.
    """
    for cinema_id in CINEMA_IDS:
        print(f"\n=== cinema {cinema_id} ===")
        try:
            days = fetch_dates(cinema_id)
        except requests.RequestException as e:
            print(f"  FAILED to fetch dates: {e}")
            continue

        if not days:
            print("  no dates returned — wrong cinema id?")
            continue

        print(f"  {len(days)} dates, {days[0]} .. {days[-1]}")
        for d in days:
            wd = datetime.fromisoformat(d).strftime("%a")
            print(f"    {d} ({wd})")

        try:
            mm70_days = fetch_dates(cinema_id, ATTR_FILTER)
        except requests.RequestException as e:
            print(f"  FAILED to fetch attr={ATTR_FILTER!r} dates: {e}")
            mm70_days = []

        print(f"\n  attr={ATTR_FILTER!r} dates ({len(mm70_days)}):")
        for d in mm70_days:
            wd = datetime.fromisoformat(d).strftime("%a")
            print(f"    {d} ({wd})")
        print(f"  >> 70mm horizon: {mm70_days[-1] if mm70_days else '—'}")

        # Dump one date in full so the JSON shape is visible.
        day = days[0]
        until = f"{BASE}/film-events/in-cinema/{cinema_id}/at-date/{day}?attr=&lang={LANG}"
        try:
            body = _get(until).get("body", {})
        except requests.RequestException as e:
            print(f"  FAILED to fetch events: {e}")
            continue

        films = body.get("films", [])
        events = body.get("events", [])
        print(f"  {day}: {len(films)} films, {len(events)} events")

        if not films and not events:
            print("  ! empty — the response keys may differ. Raw top level:")
            print("   ", list(body.keys()))
            continue

        print("\n  films on this date:")
        for f in films:
            mark = "  <-- FILM_ID match" if f.get("id") == FILM_ID else ""
            print(f"    {f.get('id'):<12} {f.get('name','')}{mark}")

        attrs = sorted({str(a) for e in events for a in (e.get("attributeIds") or [])})
        print(f"\n  attribute ids seen ({len(attrs)}):")
        print("   ", ", ".join(attrs) or "(none)")
        hits = [a for a in attrs if WANT_FORMAT and WANT_FORMAT in norm(a)]
        print(f"  matching WANT_FORMAT={WANT_FORMAT!r}: {hits or 'NONE — fix this'}")

        if events:
            print("\n  one raw event:")
            print("   ", json.dumps(events[0], ensure_ascii=False)[:600])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="dump API structure and exit")
    ap.add_argument("--loop", action="store_true", help="run continuously")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.probe:
        probe()
        return

    if not args.loop:
        check_once(verbose=not args.quiet)
        return

    while True:
        try:
            check_once(verbose=not args.quiet)
        except Exception as e:  # keep the loop alive
            print(f"[error] {e}", file=sys.stderr)
        time.sleep(interval_seconds(datetime.now()))


if __name__ == "__main__":
    main()
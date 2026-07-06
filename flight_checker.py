"""
Daily flight price checker.

Reads a list of trackers from trackers.json (managed via the admin UI in
docs/index.html, or by hand), queries Google Flights for candidate date
pairs, and emails a summary of any flights under each tracker's price limit.

Each tracker can constrain:
  - which months to search (`months`)
  - which weekday(s) you're willing to depart on (`depart_days`)
  - which weekday(s) you're willing to return on (`return_days`, round-trip only)
  - how many nights the trip should span (`min_nights` / `max_nights`)
  - earliest/latest departure time on the outbound leg (`depart_after` / `depart_before`)
  - earliest/latest departure time on the return leg (`return_after` / `return_before`)

Weekday + night-range constraints are applied BEFORE querying (so a 6-month
"Friday out, Sunday back" search is ~26 queries, not ~180). Time-of-day
constraints are applied AFTER querying, since Google Flights doesn't accept
a time filter directly -- we check the actual departure time on the
returned itinerary's first leg in each direction.

Also tracks how many queries errored out (vs. legitimately returning "no
flights found"). A high error rate triggers a separate "bot may be broken"
email, since that usually means the underlying scraper needs an update.

Note: this uses an UNOFFICIAL scraper for Google Flights (the `fast-flights`
library). No API key, no guaranteed uptime. Keep request volume modest.
"""

from __future__ import annotations

import calendar
import json
import os
import smtplib
import sys
import time
from dataclasses import dataclass
# --- STANDARD DATETIME IMPORTS ---
from datetime import date, timedelta
from email.mime.text import MIMEText

# --- THIRD PARTY IMPORTS ---
from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights.exceptions import FlightsNotFound
from fast_flights.model import Flights, SimpleDatetime  # Ensure SimpleDatetime is here

TRACKERS_PATH = os.environ.get("TRACKERS_PATH", "trackers.json")

ERROR_RATE_ALERT_THRESHOLD = float(os.environ.get("ERROR_RATE_ALERT_THRESHOLD", "0.5"))
MIN_QUERIES_BEFORE_ALERTING = int(os.environ.get("MIN_QUERIES_BEFORE_ALERTING", "3"))

WEEKDAY_MAP = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


@dataclass
class Hit:
    tracker_name: str
    origin: str
    destination: str
    depart_date: str
    depart_: str
    return_date: str | None
    return_: str | None
    price: int
    airlines: list[str]
    currency: str


def load_trackers(path: str) -> list[dict]:
    with open(path, "r") as f:
        trackers = json.load(f)
    return [t for t in trackers if t.get("enabled", True)]


def month_to_dates(month_str: str) -> list[date]:
    year, month = (int(x) for x in month_str.split("-"))
    n_days = calendar.monthrange(year, month)[1]
    return [date(year, month, d) for d in range(1, n_days + 1)]


def parse_weekdays(days: list[str] | None) -> set[int] | None:
    if not days:
        return None
    return {WEEKDAY_MAP[d.strip().upper()[:3]] for d in days}


def parse__str(s: str | None) -> int | None:
    """'18:00' -> minutes since midnight."""
    if not s:
        return None
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)


def candidate_pairs(tracker: dict) -> list[tuple[date, date | None]]:
    """Every (depart, return) date pair worth querying for this tracker.
    return is None for one-way trackers."""
    all_days: list[date] = []
    for m in tracker["months"]:
        all_days.extend(month_to_dates(m))
    all_days = sorted(set(all_days))

    depart_days = parse_weekdays(tracker.get("depart_days"))
    valid_departures = [d for d in all_days if depart_days is None or d.weekday() in depart_days]

    is_one_way = tracker.get("one_way", False)
    if is_one_way:
        return [(d, None) for d in valid_departures]

    return_days = parse_weekdays(tracker.get("return_days"))
    trip_length = tracker.get("trip_length_days")

    if tracker.get("min_nights") is None and tracker.get("max_nights") is None and trip_length is not None:
        min_n = max_n = trip_length
    else:
        min_n = tracker.get("min_nights", 1)
        max_n = tracker.get("max_nights", trip_length or 14)

    pairs: list[tuple[date, date | None]] = []
    for d in valid_departures:
        for n in range(min_n, max_n + 1):
            ret = d + delta(days=n)
            if return_days is not None and ret.weekday() not in return_days:
                continue
            pairs.append((d, ret))
    return pairs


def leg_departure(flight: Flights, from_code: str) -> SimpleDatetime | None:
    """First leg of the itinerary departing from a given airport code."""
    for leg in flight.flights:
        if leg.from_airport.code == from_code:
            return leg.departure
    return None


def time_within(dt: SimpleDatetime | None, after_min: int | None, before_min: int | None) -> bool:
    if dt is None:
        return True  # can't verify -- don't silently drop a possible match
        
    # FIX: Guard against missing or empty/incomplete time lists
    if not hasattr(dt, "time") or not dt.time or len(dt.time) < 2:
        return True  # Can't parse the exact time structure; assume true to avoid skipping a cheap flight
        
    minutes = dt.time[0] * 60 + dt.time[1]
    if after_min is not None and minutes < after_min:
        return False
    if before_min is not None and minutes > before_min:
        return False
    return True


def fmt_time(dt: SimpleDatetime | None) -> str:
    if dt is None:
        return "?"
        
    # FIX: Guard against malformed time lists during string formatting
    if not hasattr(dt, "time") or not dt.time or len(dt.time) < 2:
        return "??"
        
    return f"{dt.time[0]:02d}:{dt.time[1]:02d}"


def check_one_pair(
    tracker_name: str,
    origin: str,
    destination: str,
    depart: date,
    ret: date | None,
    seat: str,
    max_stops: int | None,
    currency: str,
    depart_after: int | None,
    depart_before: int | None,
    return_after: int | None,
    return_before: int | None,
) -> tuple[list[Hit], bool]:
    """Returns (hits, was_error)."""
    if ret:
        flights = [
            FlightQuery(date=depart.isoformat(), from_airport=origin, to_airport=destination),
            FlightQuery(date=ret.isoformat(), from_airport=destination, to_airport=origin),
        ]
        trip = "round-trip"
    else:
        flights = [FlightQuery(date=depart.isoformat(), from_airport=origin, to_airport=destination)]
        trip = "one-way"

    query = create_query(
        flights=flights,
        seat=seat,
        trip=trip,
        passengers=Passengers(adults=1),
        currency=currency,
        max_stops=max_stops,
    )

    try:
        result = get_flights(query)
    except FlightsNotFound:
        return [], False
    except Exception as e:
        print(f"  ! error checking {origin}->{destination} {depart}/{ret}: {e}", file=sys.stderr)
        return [], True

    hits: list[Hit] = []
    for f in result:
        out_dep = leg_departure(f, origin)
        if not time_within(out_dep, depart_after, depart_before):
            continue

        ret_dep = None
        if ret:
            ret_dep = leg_departure(f, destination)
            if not time_within(ret_dep, return_after, return_before):
                continue

        hits.append(
            Hit(
                tracker_name=tracker_name,
                origin=origin,
                destination=destination,
                depart_date=depart.isoformat(),
                depart_time=fmt_time(out_dep),
                return_date=ret.isoformat() if ret else None,
                return_time=fmt_time(ret_dep) if ret else None,
                price=f.price,
                airlines=f.airlines,
                currency=currency or "USD",
            )
        )
    return hits, False


def run(trackers: list[dict], delay: float) -> tuple[list[Hit], int, int]:
    all_hits: list[Hit] = []
    total_queries = 0
    total_errors = 0

    for tracker in trackers:
        name = tracker.get("name", f"{tracker['from']}-{tracker['to']}")
        origin, destination = tracker["from"], tracker["to"]
        price_limit = tracker["price_limit"]
        seat = tracker.get("seat", "economy")
        max_stops = tracker.get("max_stops")
        currency = tracker.get("currency", "EUR")
        depart_after = parse_time_str(tracker.get("depart_after"))
        depart_before = parse_time_str(tracker.get("depart_before"))
        return_after = parse_time_str(tracker.get("return_after"))
        return_before = parse_time_str(tracker.get("return_before"))

        pairs = candidate_pairs(tracker)
        print(f"[{name}] Checking {origin} -> {destination} across {len(pairs)} date combination(s)...")

        for depart, ret in pairs:
            hits, was_error = check_one_pair(
                name, origin, destination, depart, ret, seat, max_stops, currency,
                depart_after, depart_before, return_after, return_before,
            )
            total_queries += 1
            if was_error:
                total_errors += 1
            for h in hits:
                if h.price <= price_limit:
                    all_hits.append(h)
            time.sleep(delay)

    return all_hits, total_queries, total_errors


def format_price_email(hits: list[Hit]) -> str:
    hits_sorted = sorted(hits, key=lambda h: h.price)
    lines = ["Flights found under your price limit:\n"]
    for h in hits_sorted:
        trip = f"{h.depart_date} {h.depart_time}"
        if h.return_date:
            trip += f"  ->  back {h.return_date} {h.return_time}"
        lines.append(
            f"- [{h.tracker_name}] {h.origin} -> {h.destination} | {trip} | "
            f"{h.price} {h.currency} | {', '.join(h.airlines)}"
        )
    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("ALERT_TO", smtp_user)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_addr], msg.as_string())


def main() -> None:
    trackers = load_trackers(TRACKERS_PATH)
    if not trackers:
        print("No enabled trackers found in trackers.json. Nothing to do.")
        return

    delay = float(os.environ.get("REQUEST_DELAY_SECONDS", "3"))
    hits, total_queries, total_errors = run(trackers, delay)

    error_rate = (total_errors / total_queries) if total_queries else 0
    print(f"Done. {total_queries} queries, {total_errors} errors ({error_rate:.0%}).")

    if total_queries >= MIN_QUERIES_BEFORE_ALERTING and error_rate >= ERROR_RATE_ALERT_THRESHOLD:
        send_email(
            subject="⚠️ Flight price bot may be broken",
            body=(
                f"{total_errors} out of {total_queries} queries failed with errors today "
                f"({error_rate:.0%}).\n\n"
                "This usually means Google Flights changed something and the "
                "unofficial `fast-flights` scraper needs an update (check for a "
                "newer version of the library, or check the GitHub Actions run "
                "log for the actual error).\n\n"
                "Your price trackers were NOT reliably checked today."
            ),
        )
        print("Breakage alert email sent.")

    if hits:
        send_email(
            subject=f"✈️ {len(hits)} flight(s) found under your price limit",
            body=format_price_email(hits),
        )
        print("Price alert email sent.")
    elif error_rate < ERROR_RATE_ALERT_THRESHOLD:
        print("No flights found under any tracker's price limit today.")


if __name__ == "__main__":
    main()

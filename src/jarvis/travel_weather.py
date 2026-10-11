"""Local-only travel evidence. Public providers receive a city, never an itinerary."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import settings

CITIES = {
    "rio rancho": "Rio Rancho, NM",
    "san jose": "San Jose, CA",
    "santa fe": "Santa Fe, NM",
    "albuquerque": "Albuquerque, NM",
    "san francisco": "San Francisco, CA",
    "palo alto": "Palo Alto, CA",
    "denver": "Denver, CO",
    "houston": "Houston, TX",
    "austin": "Austin, TX",
    "dallas": "Dallas, TX",
    "las vegas": "Las Vegas, NV",
    "new york": "New York, NY",
    "boston": "Boston, MA",
    "chicago": "Chicago, IL",
    "seattle": "Seattle, WA",
    "san diego": "San Diego, CA",
    "los angeles": "Los Angeles, CA",
    "phoenix": "Phoenix, AZ",
}
TRAVEL = re.compile(
    r"\b(?:travel|trip|itinerary|flight|hotel|check.in|amex|american express global business travel)\b", re.I
)


def cities(text):
    return sorted({city for name, city in CITIES.items() if re.search(r"\b" + name + r"\b", text, re.I)})


def event_end(meta):
    value = meta.get("end", {})
    try:
        stamp = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
        zone = {
            "Mountain Standard Time": "America/Denver",
            "Pacific Standard Time": "America/Los_Angeles",
            "UTC": "UTC",
        }.get(value.get("timeZone"), value.get("timeZone"))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=ZoneInfo(zone))
    except (KeyError, ValueError, TypeError):
        return None


def context(db, now=None):
    now = now or datetime.now(timezone.utc)
    home = db.get_state("research.home_city", "Rio Rancho, NM")
    saved_sync = db.get_state("outlook.last_sync")
    try:
        fresh = saved_sync and timedelta(0) <= now - datetime.fromisoformat(saved_sync) <= timedelta(
            minutes=30
        )
    except (ValueError, TypeError):
        fresh = False
    fresh = fresh and not db.get_state("outlook.error") and db.get_state("setup.enabled.outlook", True)
    rows = db.query(
        """SELECT id,kind,title,body,metadata,occurred_at FROM records WHERE NOT deleted AND
        ((kind='event' AND occurred_at BETWEEN %s AND %s) OR
         (kind='email' AND occurred_at>%s AND (title ~* 'travel|itinerary|flight|hotel|trip|amex')))
        ORDER BY occurred_at DESC LIMIT 100""",
        (now - timedelta(days=21), now + timedelta(days=30), now - timedelta(days=30)),
    )
    active, candidates = [], []
    for row in rows:
        meta = row.get("metadata") or {}
        if (
            meta.get("isCancelled") is True
            or meta.get("responseStatus", {}).get("response") == "declined"
            or re.search(r"\b(?:cancelled|canceled|cancellation)\b", row["title"], re.I)
        ):
            continue
        location = meta.get("location", {})
        location = location.get("displayName", "") if isinstance(location, dict) else str(location)
        title = row["title"]
        if not TRAVEL.search(title):
            continue
        matches = cities(title + " " + location + " " + (row.get("body") or "")[:6000])
        if len(matches) != 1:
            continue  # Multiple airport/city names do not establish a destination.
        city = matches[0]
        end = event_end(meta)
        start = row.get("occurred_at")
        candidate = {
            "id": row["id"],
            "title": title,
            "city": city,
            "status": "review_dates",
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        }
        # Only a current, explicit multi-hour travel/stay block supports automatic location.
        # Flight origin/destination and email received dates are never treated as travel dates.
        if (
            fresh
            and row["kind"] == "event"
            and (
                meta.get("isOrganizer") is True
                or (
                    bool(settings.outlook_browser_account)
                    and meta.get("organizer", {}).get("emailAddress", {}).get("address", "").casefold()
                    == settings.outlook_browser_account.casefold()
                )
                or re.search(r"\bmy (?:trip|travel|hotel)\b", title, re.I)
            )
            and end
            and start
            and start.tzinfo
            and timedelta(hours=6) <= end - start <= timedelta(days=21)
            and start <= now < end
            and not re.search(r"\bflight\b", title, re.I)
            and re.search(r"\b(?:travel|trip|hotel|stay)\b", title, re.I)
            and cities(title + " " + location) == [city]
        ):
            candidate["status"] = "active_calendar_trip"
            active.append(candidate)
        candidates.append(candidate)
    distinct = {row["city"] for row in active}
    selected = active[0] if len(distinct) == 1 else None
    confirmed = db.get_state("research.travel_plan")
    confirmed_active = (
        confirmed
        and confirmed["start"]
        <= now.astimezone(ZoneInfo("America/Denver")).date().isoformat()
        <= confirmed["end"]
    )
    return {
        "home_city": home,
        "city": confirmed["city"] if confirmed_active else selected["city"] if selected else home,
        "basis": "confirmed_travel_dates"
        if confirmed_active
        else "active_calendar_trip"
        if selected
        else "home",
        "source": selected,
        "candidates": candidates[:5],
        "confirmed_plan": confirmed,
        "calendar_fresh": bool(fresh),
        "note": "Email travel dates and ambiguous routes need review. Ordinary meetings do not change your location. Saved Outlook coverage may be incomplete.",
    }


def requested_city(text):
    matches = cities(text)
    if len(matches) == 1:
        return matches[0]
    explicit = re.search(r"\b(?:in|for)\s+([A-Za-z .'-]+,\s*[A-Za-z]{2})\b", text, re.I)
    if explicit:
        return explicit.group(1).strip()
    if re.search(r"\b(?:in|for)\s+(?!today\b|tomorrow\b|the\b|this\b|next\b|me\b|my\b)[A-Za-z]", text, re.I):
        raise ValueError("Please specify the US city and two-letter state for that forecast.")
    return None

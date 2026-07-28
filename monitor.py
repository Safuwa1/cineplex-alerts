"""
Cineplex Alert System - Final monitor (v2: catalog + seat-watch)
--------------------------------------------------------------------
Each run:
  1. Logs into cineplex-web-api (movie browsing) and checks for new
     movies / new ticket dates - broadcast to every subscriber.
  2. Logs into cineplex-ticket-api as a guest (no real account, no
     CAPTCHA bypass - just a normal browser using the site's own
     "Guest" checkout option) and:
       a. Builds a fresh catalog.json (every location, every movie,
          the next few days of showtimes) for the signup page.
       b. Checks seat availability for every show a subscriber picked,
          and emails just that subscriber if it changed.
"""

import asyncio
import csv
import io
import json
import os
import smtplib
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from playwright.async_api import async_playwright

HOME_URL = "https://www.cineplexbd.com/"
API_BASE = "https://cineplex-web-api.cineplexbd.com/api/v1"

TICKET_LOGIN_URL = "https://ticket.cineplexbd.com/login"
TICKET_API_BASE = "https://cineplex-ticket-api.cineplexbd.com/api/v1"

STATE_FILE = "state.json"
CATALOG_FILE = "catalog.json"
CATALOG_DAY_RANGE = 4  # today + next 3 days
BD_OFFSET = timedelta(hours=6)  # Bangladesh is UTC+6, for a closer "local date"

SUBSCRIBERS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTeWzI3wdsJvW_m8ofLzSOLJ9Ck8DXtemyGiwGeVAcD6vEm9cD1ErMNMWEXK2orijP4rbgJGIZ6If0-/pub?output=csv"

new_movie_alerts = []
new_date_alerts = []
category_change_alerts = []
warnings = []


# ---------------------------------------------------------------- utilities

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"movies": {}, "seats": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def format_time_12h(time_raw):
    try:
        t = datetime.strptime(time_raw, "%H:%M:%S")
        return t.strftime("%I:%M %p").lstrip("0")
    except Exception:  # noqa: BLE001
        return time_raw


def format_date_display(date_str):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.strftime("%d %b")
    except Exception:  # noqa: BLE001
        return date_str


def parse_show_selection(raw):
    """Parses 'LOC4|PROG22558|Title | Location | Date Time' from the signup form."""
    if not raw or "|" not in raw:
        return None
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2 or not parts[0].startswith("LOC") or not parts[1].startswith("PROG"):
        return None
    try:
        location = int(parts[0][3:])
        program_id = int(parts[1][4:])
    except ValueError:
        return None
    summary = " | ".join(parts[2:]) if len(parts) > 2 else f"location {location}, show {program_id}"
    return {"location": location, "programId": program_id, "summary": summary}


def get_subscribers():
    """Fetch (email, location, programId, summary) rows from the signup Sheet CSV."""
    subs = []
    try:
        req = urllib.request.Request(SUBSCRIBERS_CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
        rows = list(csv.reader(io.StringIO(content)))
        if not rows:
            return subs
        header = [h.strip().lower() for h in rows[0]]
        email_col = next((i for i, h in enumerate(header) if "email" in h), None)
        show_col = next((i for i, h in enumerate(header) if "show" in h), None)
        for row in rows[1:]:
            email = row[email_col].strip() if email_col is not None and email_col < len(row) else ""
            raw_show = row[show_col].strip() if show_col is not None and show_col < len(row) else ""
            if not email or "@" not in email:
                continue
            parsed = parse_show_selection(raw_show)
            subs.append(
                {
                    "email": email,
                    "location": parsed["location"] if parsed else None,
                    "programId": parsed["programId"] if parsed else None,
                    "summary": parsed["summary"] if parsed else None,
                }
            )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not fetch subscriber list: {exc}")
    return subs


def get_all_recipients(subscribers):
    emails = {s["email"] for s in subscribers if s["email"]}
    owner_email = os.environ.get("RECEIVER_EMAIL", "").strip()
    if owner_email:
        emails.add(owner_email)
    return sorted(emails)


def group_by_show(subscribers):
    groups = {}
    for s in subscribers:
        if s["location"] is None or s["programId"] is None:
            continue
        key = (s["location"], s["programId"])
        groups.setdefault(key, {"emails": set(), "summary": s["summary"]})
        groups[key]["emails"].add(s["email"])
    return groups


def send_email(subject: str, body: str, recipients):
    recipients = sorted(set(recipients))
    if not recipients:
        print(f"No recipients for '{subject}' - skipping.")
        return
    sender = os.environ["SENDER_EMAIL"]
    password = os.environ["SENDER_APP_PASSWORD"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = sender  # recipients live only in the envelope below, hidden from each other
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())
    print(f"Email sent to {len(recipients)} recipient(s): {subject}")


# ------------------------------------------------------- cineplex-web-api

async def api_call(page, token, path, body=None):
    return await page.evaluate(
        """async ({base, path, token, body}) => {
            const res = await fetch(base + path, {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + token,
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                },
                body: body ? JSON.stringify(body) : undefined,
            });
            const text = await res.text();
            return {status: res.status, body: text};
        }""",
        {"base": API_BASE, "path": path, "token": token, "body": body},
    )


async def check_movies_and_dates(page, state):
    """Existing logic: new movies + new ticket dates for running movies."""
    old_movies = state.get("movies", {})
    updated_movies = {}
    is_first_run = not old_movies

    token = None

    async def capture_token(response):
        nonlocal token
        if "/api/v1/login" in response.url and response.status == 200:
            try:
                data = json.loads(await response.text())
                if data.get("status") == "success":
                    token = data.get("data")
            except Exception:  # noqa: BLE001
                pass

    page.on("response", lambda r: asyncio.create_task(capture_token(r)))

    try:
        await page.goto(HOME_URL, wait_until="networkidle", timeout=45000)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(3000)

    if not token:
        warnings.append("Could not obtain cineplex-web-api token - skipped movie/date check this run.")
        return updated_movies

    try:
        list_result = await api_call(page, token, "/movie-list")
        list_data = json.loads(list_result["body"])
        categories = list_data.get("data", {}) or {}
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not fetch movie-list: {exc}")
        return updated_movies

    flat_movies = []
    for category, movies in categories.items():
        if isinstance(movies, list):
            for m in movies:
                flat_movies.append((category, m))

    for category, m in flat_movies:
        key = str(m.get("id"))
        title = m.get("title") or m.get("movie_title") or "Unknown title"
        slug = m.get("slug")
        prev = old_movies.get(key)

        if prev is None and not is_first_run:
            new_movie_alerts.append(f'NEW MOVIE: "{title}" ({category}) - release: {m.get("release", "n/a")}')
        elif prev is not None and prev.get("category") != category:
            category_change_alerts.append(f'"{title}" moved from {prev.get("category")} to {category}')

        updated_movies[key] = {
            "title": title,
            "category": category,
            "slug": slug,
            "dates": old_movies.get(key, {}).get("dates", []),
        }

    for category, m in flat_movies:
        if category.lower() != "running":
            continue
        key = str(m.get("id"))
        slug = m.get("slug")
        title = m.get("title") or "Unknown title"
        if not slug:
            continue

        previous_dates = set(updated_movies[key]["dates"])
        try:
            detail_result = await api_call(page, token, f"/movie/{slug}/detail")
            detail_data = json.loads(detail_result["body"])
            show_times = detail_data.get("data", {}).get("show_time", []) or []
            current_dates = sorted({s.get("raw_date") for s in show_times if s.get("raw_date")})
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'Could not fetch showtimes for "{title}": {exc}')
            current_dates = sorted(previous_dates)

        fresh_dates = sorted(set(current_dates) - previous_dates)
        if fresh_dates and previous_dates and not is_first_run:
            new_date_alerts.append(f'NEW TICKET DATES for "{title}": {", ".join(fresh_dates)}')

        updated_movies[key]["dates"] = current_dates

    return updated_movies


# ------------------------------------------------------ cineplex-ticket-api

async def ticket_api_call(page, token, device_key, path, body=None):
    return await page.evaluate(
        """async ({base, path, token, deviceKey, body}) => {
            const res = await fetch(base + path, {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + token,
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'appsource': 'web',
                    'device-key': deviceKey,
                },
                body: body ? JSON.stringify(body) : undefined,
            });
            const text = await res.text();
            return {status: res.status, body: text};
        }""",
        {"base": TICKET_API_BASE, "path": path, "token": token, "deviceKey": device_key, "body": body},
    )


async def ticket_guest_login(page):
    """Logs in via the site's own 'GUEST LOGIN' button - no real account,
    no CAPTCHA bypass (the browser just runs the page's own JS normally)."""
    holder = {"token": None, "device_key": None}

    def handle_req(request):
        try:
            if "cineplex-ticket-api.cineplexbd.com" not in request.url:
                return
            dk = request.headers.get("device-key")
            if dk and not holder["device_key"]:
                holder["device_key"] = dk
        except Exception:  # noqa: BLE001
            pass

    async def handle_resp(response):
        try:
            if "/guest-login" in response.url and response.status == 200:
                data = json.loads(await response.text())
                if data.get("status") == "success":
                    holder["token"] = data.get("data", {}).get("token")
        except Exception:  # noqa: BLE001
            pass

    page.on("request", handle_req)
    page.on("response", lambda r: asyncio.create_task(handle_resp(r)))

    try:
        await page.goto(TICKET_LOGIN_URL, wait_until="networkidle", timeout=45000)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(2500)

    try:
        guest_btn = page.get_by_text("GUEST LOGIN", exact=False).first
        await guest_btn.click(timeout=8000)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not click GUEST LOGIN: {exc}")
        return None, None

    await page.wait_for_timeout(2500)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001
        pass

    return holder["token"], holder["device_key"]


async def build_catalog(page, token, device_key):
    catalog = {"generated_at": datetime.now(timezone.utc).isoformat(), "locations": []}

    try:
        loc_result = await ticket_api_call(page, token, device_key, "/get-location")
        loc_data = json.loads(loc_result["body"]).get("data", [])
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not fetch get-location: {exc}")
        return catalog

    today = (datetime.now(timezone.utc) + BD_OFFSET).date()

    for loc in loc_data:
        loc_id = loc.get("id")
        loc_title = loc.get("locationTitle") or loc.get("code") or f"Location {loc_id}"
        if loc_id is None:
            continue

        try:
            sd_result = await ticket_api_call(page, token, device_key, "/get-showdate", {"location": loc_id})
            sd_data = json.loads(sd_result["body"]).get("data", [])
        except Exception:  # noqa: BLE001
            sd_data = []

        movies_seen = {}
        for entry in sd_data:
            for m in entry.get("availableMovies", []):
                if m.get("movie_id"):
                    movies_seen[m["movie_id"]] = m.get("movie_title", "Unknown")

        if not movies_seen:
            continue

        loc_entry = {"id": loc_id, "title": loc_title, "movies": []}

        for movie_id, movie_title in movies_seen.items():
            movie_entry = {"movieId": movie_id, "title": movie_title, "dates": []}

            for offset in range(CATALOG_DAY_RANGE):
                date_str = (today + timedelta(days=offset)).isoformat()
                try:
                    shows_result = await ticket_api_call(
                        page, token, device_key, "/get-shows",
                        {"location": loc_id, "movieId": movie_id, "showDate": date_str},
                    )
                    shows_data = json.loads(shows_result["body"]).get("data", [])
                except Exception:  # noqa: BLE001
                    continue

                slots = []
                for hall in shows_data:
                    hall_title = hall.get("screenTitle") or "Hall"
                    for st in hall.get("showTimes", []):
                        program_id = st.get("programId")
                        time_raw = st.get("showTime", "")
                        if not program_id or not time_raw:
                            continue
                        prices = st.get("seatPrices") or []
                        price = prices[0].get("unitPrice") if prices else None
                        slots.append(
                            {
                                "programId": program_id,
                                "time": time_raw,
                                "displayTime": format_time_12h(time_raw),
                                "hall": hall_title,
                                "price": price,
                            }
                        )

                if slots:
                    movie_entry["dates"].append(
                        {"date": date_str, "displayDate": format_date_display(date_str), "shows": slots}
                    )

            if movie_entry["dates"]:
                loc_entry["movies"].append(movie_entry)

        if loc_entry["movies"]:
            catalog["locations"].append(loc_entry)

    return catalog


async def check_seats(page, token, device_key, watched_shows, state):
    seat_state = state.setdefault("seats", {})
    alerts_by_email = {}

    for (location, program_id), info in watched_shows.items():
        key = f"{location}:{program_id}"
        try:
            result = await ticket_api_call(
                page, token, device_key, "/get-seat", {"location": location, "programId": program_id}
            )
            data = json.loads(result["body"]).get("data", {})
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not fetch seats for {info['summary']}: {exc}")
            continue

        available_titles = set()
        total = 0
        for seat_type in data.get("seatTypes", []):
            for seat in seat_type.get("seatStatus", []):
                total += 1
                if seat.get("seatStatus") == 1:
                    available_titles.add(seat.get("seatTitle"))

        prev = seat_state.get(key, {})
        prev_titles = set(prev.get("available", []))
        is_first_check = key not in seat_state

        seat_state[key] = {
            "available": sorted(t for t in available_titles if t),
            "total": total,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "summary": info["summary"],
        }

        if is_first_check:
            continue

        newly_available = sorted((available_titles - prev_titles) - {None})
        newly_booked = sorted((prev_titles - available_titles) - {None})

        if newly_available or newly_booked:
            lines = [f'SEAT UPDATE for "{info["summary"]}":', f"  Available now: {len(available_titles)} of {total}"]
            if newly_available:
                sample = ", ".join(newly_available[:20])
                extra = "" if len(newly_available) <= 20 else f" (+{len(newly_available) - 20} more)"
                lines.append(f"  Newly opened: {sample}{extra}")
            if newly_booked:
                lines.append(f"  Newly booked: {len(newly_booked)} seat(s)")
            message = "\n".join(lines)
            for email in info["emails"]:
                alerts_by_email.setdefault(email, []).append(message)

    return alerts_by_email


# --------------------------------------------------------------------- main

async def run_monitor():
    state = load_state()
    is_first_run = not os.path.exists(STATE_FILE)

    subscribers = get_subscribers()
    watched_shows = group_by_show(subscribers)

    seat_alerts_by_email = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )

        updated_movies = await check_movies_and_dates(page, state)
        state["movies"] = updated_movies or state.get("movies", {})

        ticket_token, device_key = await ticket_guest_login(page)
        if ticket_token and device_key:
            catalog = await build_catalog(page, ticket_token, device_key)
            try:
                with open(CATALOG_FILE, "w", encoding="utf-8") as f:
                    json.dump(catalog, f, indent=2, ensure_ascii=False)
                total_shows = sum(
                    len(d["shows"]) for loc in catalog["locations"] for mv in loc["movies"] for d in mv["dates"]
                )
                print(f"Catalog built: {len(catalog['locations'])} location(s), {total_shows} showtime slot(s).")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Could not save catalog.json: {exc}")

            if watched_shows:
                seat_alerts_by_email = await check_seats(page, ticket_token, device_key, watched_shows, state)
        else:
            warnings.append("Ticket-site guest login failed - skipped catalog build and seat checks this run.")

        await browser.close()

    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    return is_first_run, subscribers, seat_alerts_by_email


def main():
    is_first_run, subscribers, seat_alerts_by_email = asyncio.run(run_monitor())
    recipients = get_all_recipients(subscribers)

    if is_first_run:
        body = (
            "Your Star Cineplex alert system is now fully live.\n\n"
            "You'll get an email whenever a new movie appears, new ticket dates open, "
            "or seats change for any show a friend picked on the signup page."
        )
        send_email(subject="Star Cineplex Alerts - Setup complete", body=body, recipients=recipients)
    else:
        broadcast = new_movie_alerts + category_change_alerts + new_date_alerts
        if broadcast:
            body = "\n".join(broadcast)
            if warnings:
                body += "\n\n(Notes: " + "; ".join(warnings) + ")"
            body += "\n\nBook here: https://ticket.cineplexbd.com/home"
            send_email(subject=f"Star Cineplex Alert - {len(broadcast)} update(s)", body=body, recipients=recipients)
        else:
            print("No movie/date changes this run.")

    for email, messages in seat_alerts_by_email.items():
        body = "\n\n".join(messages) + "\n\nBook here: https://ticket.cineplexbd.com/home"
        send_email(subject=f"Seat update - {len(messages)} show(s)", body=body, recipients=[email])

    if warnings and not is_first_run and not (new_movie_alerts + category_change_alerts + new_date_alerts):
        print("Warnings:\n" + "\n".join(warnings))


if __name__ == "__main__":
    main()

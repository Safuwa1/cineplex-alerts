"""
Cineplex Alert System - v4: per-location date checking (fixes silent misses)
--------------------------------------------------------------------------------
Key fix from v3: /movie/{slug}/detail without a location parameter only
reflects ONE default branch's schedule. This version checks EVERY branch
separately for every movie, so a ticket drop at ANY location is caught -
and the alert names which branch it opened at.

Each run:
  1. Logs into cineplex-web-api (movie browsing).
  2. Logs into cineplex-ticket-api as a guest (site's own "Guest" option,
     no real account, no CAPTCHA bypass).
  3. Uses the ticket API's get-showdate (per location) to find which
     (location, movie) combinations actually exist - then checks each
     one's schedule via cineplex-web-api's per-location movie detail.
  4. Broadcasts: new movies (any category), category changes, and new
     ticket dates for any movie at any branch.
  5. For every (location, movie, date) a friend picked on the signup
     page, checks - via the same ticket-api session - whether tickets
     have opened. The moment one has, only that friend gets an email.
  6. Refreshes options.json so the signup page's picker stays current.
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
OPTIONS_FILE = "options.json"
BD_OFFSET = timedelta(hours=6)  # Bangladesh is UTC+6

SUBSCRIBERS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTeWzI3wdsJvW_m8ofLzSOLJ9Ck8DXtemyGiwGeVAcD6vEm9cD1ErMNMWEXK2orijP4rbgJGIZ6If0-/pub?output=csv"

new_movie_alerts = []
new_date_alerts = []
category_change_alerts = []
system_alerts = []
warnings = []


# ---------------------------------------------------------------- utilities

def log(msg):
    print(msg, flush=True)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"movies": {}, "ticket_alerts_sent": {}, "guest_login_fail_streak": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def today_bd():
    return (datetime.now(timezone.utc) + BD_OFFSET).date()


def format_date_display(date_str):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.strftime("%d %b")
    except Exception:  # noqa: BLE001
        return date_str


def parse_show_selection(raw):
    if not raw:
        return None
    try:
        data = json.loads(raw)
        loc = int(data.get("loc"))
        movies = [m for m in data.get("movies", []) if m.get("id") and m.get("title")]
        dates = [d for d in data.get("dates", []) if d]
        if not loc or not movies or not dates:
            return None
        return {"loc": loc, "locTitle": data.get("locTitle") or f"location {loc}", "movies": movies, "dates": dates}
    except Exception:  # noqa: BLE001
        return None


def get_subscribers():
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
        show_col = next((i for i, h in enumerate(header) if "show" in h or "selection" in h), None)
        for row in rows[1:]:
            email = row[email_col].strip() if email_col is not None and email_col < len(row) else ""
            raw_show = row[show_col].strip() if show_col is not None and show_col < len(row) else ""
            if not email or "@" not in email:
                continue
            subs.append({"email": email, "selection": parse_show_selection(raw_show)})
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not fetch subscriber list: {exc}")
    return subs


def get_all_recipients(subscribers):
    emails = {s["email"] for s in subscribers if s["email"]}
    owner_email = os.environ.get("RECEIVER_EMAIL", "").strip()
    if owner_email:
        emails.add(owner_email)
    return sorted(emails)


def build_watch_combos(subscribers):
    combos = {}
    today_str = today_bd().isoformat()
    for s in subscribers:
        sel = s.get("selection")
        if not sel:
            continue
        for m in sel["movies"]:
            for d in sel["dates"]:
                if d < today_str:
                    continue
                key = (sel["loc"], m["id"], d)
                entry = combos.setdefault(
                    key, {"emails": set(), "movieTitle": m["title"], "locTitle": sel["locTitle"]}
                )
                entry["emails"].add(s["email"])
    return combos


def send_email(subject: str, body: str, recipients):
    recipients = sorted(set(recipients))
    if not recipients:
        log(f"No recipients for '{subject}' - skipping.")
        return
    sender = os.environ["SENDER_EMAIL"]
    password = os.environ["SENDER_APP_PASSWORD"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = sender
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())
    log(f"Email sent to {len(recipients)} recipient(s): {subject}")


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


async def get_web_api_token(page):
    token_holder = {"token": None}

    async def capture_token(response):
        if "/api/v1/login" in response.url and response.status == 200:
            try:
                data = json.loads(await response.text())
                if data.get("status") == "success":
                    token_holder["token"] = data.get("data")
            except Exception:  # noqa: BLE001
                pass

    page.on("response", lambda r: asyncio.create_task(capture_token(r)))

    try:
        await page.goto(HOME_URL, wait_until="networkidle", timeout=45000)
    except Exception:  # noqa: BLE001
        log("Homepage networkidle timed out - continuing anyway.")
    await page.wait_for_timeout(3000)

    return token_holder["token"]


async def fetch_movie_list(page, token):
    try:
        list_result = await api_call(page, token, "/movie-list")
        list_data = json.loads(list_result["body"])
        categories = list_data.get("data", {}) or {}
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not fetch movie-list: {exc}")
        return []

    flat_movies = []
    for category, movies in categories.items():
        if isinstance(movies, list):
            for m in movies:
                flat_movies.append((category, m))
    log(f"movie-list: {len(flat_movies)} movie(s) across {len(categories)} categor(y/ies).")
    return flat_movies


async def fetch_locations(page, token):
    try:
        loc_result = await api_call(page, token, "/location")
        loc_data = json.loads(loc_result["body"]).get("data", [])
        return [
            {"id": l.get("id"), "title": l.get("location_name") or l.get("short_name")}
            for l in loc_data
            if l.get("id")
        ]
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not fetch locations: {exc}")
        return []


def track_new_movies_and_categories(flat_movies, state):
    old_movies = state.get("movies", {})
    updated_movies = {}
    is_first_run = not old_movies

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
            "movie_id": m.get("movie_id"),
            "dates_by_location": old_movies.get(key, {}).get("dates_by_location", {}),
        }

    return updated_movies, is_first_run


async def find_relevant_location_movie_pairs(page, ticket_token, device_key, all_locations):
    """Uses the ticket API's get-showdate (cheap: one call per location) to
    find which (location, movie_id) combinations actually exist right now,
    so we don't waste calls checking a movie at a branch that never shows
    it at all."""
    pairs = set()
    for loc in all_locations:
        loc_id = loc.get("id")
        if loc_id is None:
            continue
        try:
            result = await ticket_api_call(page, ticket_token, device_key, "/get-showdate", {"location": loc_id})
            data = json.loads(result["body"]).get("data", [])
            for entry in data:
                for movie in entry.get("availableMovies", []):
                    mid = movie.get("movie_id")
                    if mid:
                        pairs.add((loc_id, mid))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not fetch showdate for location {loc_id}: {exc}")
    return pairs


async def check_dates_per_location(page, token, updated_movies, location_movie_pairs, all_locations, is_first_run):
    """The core fix: check each movie's schedule AT EACH BRANCH separately,
    instead of relying on a single un-scoped call that only reflects one
    default branch."""
    loc_title_by_id = {loc["id"]: loc["title"] for loc in all_locations if loc.get("id") is not None}

    # Build movie_id -> state key + slug + title, from what we just tracked.
    by_movie_id = {}
    for key, info in updated_movies.items():
        if info.get("movie_id"):
            by_movie_id[info["movie_id"]] = {"key": key, "slug": info.get("slug"), "title": info.get("title")}

    checked = 0
    for loc_id, movie_id in sorted(location_movie_pairs):
        info = by_movie_id.get(movie_id)
        if not info or not info.get("slug"):
            continue
        key = info["key"]
        slug = info["slug"]
        title = info["title"]
        loc_title = loc_title_by_id.get(loc_id, f"location {loc_id}")
        loc_key = str(loc_id)

        dates_by_location = updated_movies[key]["dates_by_location"]
        previous_dates = set(dates_by_location.get(loc_key, []))

        try:
            detail_result = await api_call(page, token, f"/movie/{slug}/detail", {"location_id": loc_id})
            detail_data = json.loads(detail_result["body"])
            show_times = detail_data.get("data", {}).get("show_time", []) or []
            current_dates = sorted({s.get("raw_date") for s in show_times if s.get("raw_date")})
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'Could not fetch showtimes for "{title}" at {loc_title}: {exc}')
            current_dates = sorted(previous_dates)

        checked += 1
        fresh_dates = sorted(set(current_dates) - previous_dates)
        # Only alert if we've checked this (movie, location) before - avoids
        # a flood of "new" dates the very first time we ever look at a combo.
        if fresh_dates and previous_dates and not is_first_run:
            new_date_alerts.append(
                f'NEW TICKET DATES for "{title}" at {loc_title}: {", ".join(fresh_dates)}'
            )
            log(f"  -> NEW DATES: {title} @ {loc_title}: {fresh_dates}")

        dates_by_location[loc_key] = current_dates

    log(f"Checked {checked} (movie, location) combination(s) for new ticket dates.")


def build_options(all_locations, flat_movies):
    seen = {}
    for category, m in flat_movies:
        mid = m.get("movie_id")
        if mid and mid not in seen:
            seen[mid] = m.get("title") or m.get("movie_title") or "Unknown"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "locations": all_locations,
        "movies": [{"id": k, "title": v} for k, v in seen.items()],
    }


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

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            await page.goto(TICKET_LOGIN_URL, wait_until="networkidle", timeout=45000)
        except Exception:  # noqa: BLE001
            log(f"Ticket login page networkidle timed out (attempt {attempt}/{max_attempts}) - continuing anyway.")
        await page.wait_for_timeout(3000)

        try:
            guest_btn = page.get_by_text("GUEST LOGIN", exact=False).first
            await guest_btn.wait_for(state="visible", timeout=20000)
            await guest_btn.click(timeout=8000)
        except Exception as exc:  # noqa: BLE001
            log(f"GUEST LOGIN not clickable on attempt {attempt}/{max_attempts}: {exc}")
            if attempt < max_attempts:
                await page.wait_for_timeout(3000)
                continue
            try:
                await page.screenshot(path="debug_ticket_login.png", full_page=True)
                log("Saved debug_ticket_login.png showing the page at final failed attempt.")
            except Exception:  # noqa: BLE001
                pass
            warnings.append(f"Could not click GUEST LOGIN after {max_attempts} attempts: {exc}")
            return None, None

        await page.wait_for_timeout(2500)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass

        if holder["token"] and holder["device_key"]:
            return holder["token"], holder["device_key"]

        await page.wait_for_timeout(2000)
        if holder["token"] and holder["device_key"]:
            return holder["token"], holder["device_key"]

        log(f"Clicked GUEST LOGIN on attempt {attempt}/{max_attempts} but no session token was captured yet.")
        if attempt < max_attempts:
            continue

    warnings.append("Ticket-site guest login did not produce a session token after retries.")
    return None, None


async def check_ticket_availability(page, token, device_key, combos, state):
    sent_registry = state.setdefault("ticket_alerts_sent", {})
    alerts_by_email = {}

    log(f"Checking ticket availability for {len(combos)} personally-watched combo(s)...")

    for (loc, movie_id, date), info in combos.items():
        key = f"{loc}:{movie_id}:{date}"
        if sent_registry.get(key):
            continue

        try:
            result = await ticket_api_call(
                page, token, device_key, "/get-shows",
                {"location": loc, "movieId": movie_id, "showDate": date},
            )
            data = json.loads(result["body"]).get("data", [])
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'Could not check "{info["movieTitle"]}" on {date}: {exc}')
            continue

        has_shows = bool(data) and any(hall.get("showTimes") for hall in data)

        if has_shows:
            sent_registry[key] = {
                "movieTitle": info["movieTitle"],
                "locTitle": info["locTitle"],
                "date": date,
                "found_at": datetime.now(timezone.utc).isoformat(),
            }
            message = (
                f'TICKETS OPEN: "{info["movieTitle"]}" at {info["locTitle"]} '
                f'on {format_date_display(date)}!'
            )
            for email in info["emails"]:
                alerts_by_email.setdefault(email, []).append(message)
            log(f"  -> OPENED: {message}")

    return alerts_by_email


# --------------------------------------------------------------------- main

async def run_monitor():
    state = load_state()
    subscribers = get_subscribers()
    combos = build_watch_combos(subscribers)

    ticket_alerts_by_email = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )

        web_token = await get_web_api_token(page)
        if not web_token:
            warnings.append("Could not log into cineplex-web-api - skipped this run's broadcast checks.")
            await browser.close()
            state["last_checked"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            return subscribers, ticket_alerts_by_email

        flat_movies = await fetch_movie_list(page, web_token)
        all_locations = await fetch_locations(page, web_token)
        updated_movies, is_first_run = track_new_movies_and_categories(flat_movies, state)

        ticket_token, device_key = await ticket_guest_login(page)
        if ticket_token and device_key:
            state["guest_login_fail_streak"] = 0
            pairs = await find_relevant_location_movie_pairs(page, ticket_token, device_key, all_locations)
            await check_dates_per_location(page, web_token, updated_movies, pairs, all_locations, is_first_run)

            if combos:
                ticket_alerts_by_email = await check_ticket_availability(page, ticket_token, device_key, combos, state)
        else:
            warnings.append("Ticket-site guest login failed - skipped per-location date checks and ticket-drop checks this run.")
            streak = state.get("guest_login_fail_streak", 0) + 1
            state["guest_login_fail_streak"] = streak
            # Every 3rd consecutive failed run (~30 min apart), send one alert so it doesn't go unnoticed.
            if streak % 3 == 0:
                system_alerts.append(
                    f"Ticket-site guest login has now failed {streak} runs in a row. "
                    "Ticket-drop checks are NOT happening until this is fixed - the site's login flow may have changed."
                )

        state["movies"] = updated_movies

        try:
            options = build_options(all_locations, flat_movies)
            with open(OPTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(options, f, indent=2, ensure_ascii=False)
            log(f"options.json: {len(options['locations'])} location(s), {len(options['movies'])} movie(s).")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not save options.json: {exc}")

        await browser.close()

    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    return subscribers, ticket_alerts_by_email


def main():
    subscribers, ticket_alerts_by_email = asyncio.run(run_monitor())
    recipients = get_all_recipients(subscribers)

    broadcast = new_movie_alerts + category_change_alerts + new_date_alerts
    if broadcast:
        body = "\n".join(broadcast)
        if warnings:
            body += "\n\n(Notes: " + "; ".join(warnings) + ")"
        body += "\n\nBook here: https://ticket.cineplexbd.com/home"
        send_email(subject=f"Star Cineplex Alert - {len(broadcast)} update(s)", body=body, recipients=recipients)
    else:
        log("No movie/date changes this run.")
        if warnings:
            log("Warnings:\n" + "\n".join(warnings))

    for email, messages in ticket_alerts_by_email.items():
        body = "\n".join(messages) + "\n\nBook here: https://ticket.cineplexbd.com/home"
        send_email(subject=f"Tickets just opened - {len(messages)} show(s)", body=body, recipients=[email])

    if system_alerts:
        owner_email = os.environ.get("RECEIVER_EMAIL", "").strip()
        if owner_email:
            send_email(
                subject="Cineplex Alert bot needs attention",
                body="\n".join(system_alerts),
                recipients=[owner_email],
            )


if __name__ == "__main__":
    main()

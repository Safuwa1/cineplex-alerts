"""
Cineplex Alert System - v3: broadcast + per-subscriber ticket-drop watch
-----------------------------------------------------------------------------
Much lighter than the previous seat-tracking version. Each run:

  1. Broadcasts to everyone (via cineplex-web-api, no login needed beyond
     the site's own anonymous session):
       - a new movie appears (running OR upcoming)
       - a movie's category changes (e.g. upcoming -> running)
       - any movie (running OR upcoming) gets new ticket dates
  2. Refreshes options.json (locations + movies) so the signup page's
     picker stays current.
  3. For every (location, movie, date) combination a friend picked on the
     signup page, checks - via a real guest login on the ticket site - 
     whether tickets have opened yet. The moment they have, only that
     friend gets an email, listing exactly what opened.
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
warnings = []


# ---------------------------------------------------------------- utilities

def log(msg):
    print(msg, flush=True)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"movies": {}, "ticket_alerts_sent": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def format_date_display(date_str):
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.strftime("%d %b")
    except Exception:  # noqa: BLE001
        return date_str


def today_bd():
    return (datetime.now(timezone.utc) + BD_OFFSET).date()


def parse_show_selection(raw):
    """The signup page submits a JSON blob like:
    {"loc": 4, "locTitle": "Sony Square, Mirpur",
     "movies": [{"id": 1711, "title": "The Odyssey"}],
     "dates": ["2026-08-01", "2026-08-02"]}
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
        loc = int(data.get("loc"))
        movies = [m for m in data.get("movies", []) if m.get("id") and m.get("title")]
        dates = [d for d in data.get("dates", []) if d]
        if not loc or not movies or not dates:
            return None
        return {
            "loc": loc,
            "locTitle": data.get("locTitle") or f"location {loc}",
            "movies": movies,
            "dates": dates,
        }
    except Exception:  # noqa: BLE001
        return None


def get_subscribers():
    """Fetch subscriber rows (email + parsed watch selection) from the Sheet CSV."""
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
            subs.append({"email": email, "selection": parsed})
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
    """Groups every (location, movieId, date) a subscriber picked, across
    everyone, so each unique combo is only checked once per run."""
    combos = {}
    today_str = today_bd().isoformat()
    for s in subscribers:
        sel = s.get("selection")
        if not sel:
            continue
        for m in sel["movies"]:
            for d in sel["dates"]:
                if d < today_str:
                    continue  # don't bother watching dates already in the past
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
    msg["To"] = sender  # recipients live only in the envelope below, hidden from each other
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


async def check_movies_and_dates(page, token, state):
    """Broadcast checks: new movies (any category) + new ticket dates for
    ANY movie that already has showtimes (not just ones tagged 'running' -
    some movies go on sale before the site marks them as running)."""
    old_movies = state.get("movies", {})
    updated_movies = {}
    is_first_run = not old_movies

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

    log(f"movie-list: {len(flat_movies)} movie(s) across {len(categories)} categor(y/ies).")

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

    # Check EVERY movie's show_time (not just "running" ones) - tickets can
    # open before the site's own category label catches up.
    for category, m in flat_movies:
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
        if fresh_dates and not is_first_run:
            new_date_alerts.append(f'NEW TICKET DATES for "{title}": {", ".join(fresh_dates)}')

        updated_movies[key]["dates"] = current_dates

    return updated_movies


async def build_options(page, token):
    """Lightweight data for the signup page's picker: just locations and
    movies, reusing the same already-logged-in session. No ticket-site
    login needed for this part."""
    options = {"generated_at": datetime.now(timezone.utc).isoformat(), "locations": [], "movies": []}

    try:
        loc_result = await api_call(page, token, "/location")
        loc_data = json.loads(loc_result["body"]).get("data", [])
        options["locations"] = [
            {"id": l.get("id"), "title": l.get("location_name") or l.get("short_name")}
            for l in loc_data
            if l.get("id")
        ]
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not fetch locations for picker: {exc}")

    try:
        list_result = await api_call(page, token, "/movie-list")
        list_data = json.loads(list_result["body"]).get("data", {})
        seen = {}
        for category, movies in list_data.items():
            if isinstance(movies, list):
                for m in movies:
                    movie_id = m.get("movie_id")  # the id the TICKET api expects
                    if movie_id and movie_id not in seen:
                        seen[movie_id] = m.get("title") or m.get("movie_title") or "Unknown"
        options["movies"] = [{"id": k, "title": v} for k, v in seen.items()]
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not fetch movies for picker: {exc}")

    return options


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
        log("Ticket login page networkidle timed out - continuing anyway.")
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


async def check_ticket_availability(page, token, device_key, combos, state):
    """For every watched (location, movie, date) combo not yet flagged as
    open, checks the real booking system. The moment one opens, marks it
    (so we never alert twice) and queues a message for its subscribers."""
    sent_registry = state.setdefault("ticket_alerts_sent", {})
    alerts_by_email = {}

    log(f"Checking ticket availability for {len(combos)} watched combo(s)...")

    for (loc, movie_id, date), info in combos.items():
        key = f"{loc}:{movie_id}:{date}"
        if sent_registry.get(key):
            continue  # already told everyone about this one

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

    seat_alerts_by_email = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )

        web_token = await get_web_api_token(page)
        if web_token:
            updated_movies = await check_movies_and_dates(page, web_token, state)
            state["movies"] = updated_movies or state.get("movies", {})

            options = await build_options(page, web_token)
            try:
                with open(OPTIONS_FILE, "w", encoding="utf-8") as f:
                    json.dump(options, f, indent=2, ensure_ascii=False)
                log(f"options.json: {len(options['locations'])} location(s), {len(options['movies'])} movie(s).")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Could not save options.json: {exc}")
        else:
            warnings.append("Could not log into cineplex-web-api - skipped broadcast checks and options refresh.")

        if combos:
            ticket_token, device_key = await ticket_guest_login(page)
            if ticket_token and device_key:
                seat_alerts_by_email = await check_ticket_availability(page, ticket_token, device_key, combos, state)
            else:
                warnings.append("Ticket-site guest login failed - skipped ticket-drop checks this run.")
        else:
            log("No one is watching any specific show right now - skipping ticket-drop checks.")

        await browser.close()

    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    return subscribers, seat_alerts_by_email


def main():
    subscribers, ticket_alerts_by_email = asyncio.run(run_monitor())
    recipients = get_all_recipients(subscribers)
    is_first_run = not os.path.exists(STATE_FILE)  # note: state.json already saved by now on disk from this run

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


if __name__ == "__main__":
    main()

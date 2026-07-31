"""
Cineplex Alert System - v5: per-location checking WITHOUT the ticket site
--------------------------------------------------------------------------------
Why this version exists: ticket.cineplexbd.com now sits behind Cloudflare's
"Verify you are human" bot-check, so a headless browser can never log in
there as a guest. This version removes that dependency entirely - it never
visits ticket.cineplexbd.com or cineplex-ticket-api. Everything now runs
through cineplex-web-api only (the same one that already worked reliably).

Key idea: cineplex-web-api's /movie/{slug}/detail?location_id=X already
returns the full list of show dates for that movie AT THAT BRANCH. That's
enough to answer both questions we care about:
  - Broadcast: did any branch get a NEW date for any movie? (diff vs state)
  - Personal watch: has MY picked (location, movie, date) shown up yet?
    (membership check against that same per-branch date list)

Each run:
  1. Logs into cineplex-web-api (movie browsing).
  2. Fetches the movie list and the location list.
  3. For every (movie, location) pair worth checking - every branch for
     every "running" movie, plus any branch/movie a subscriber personally
     picked - fetches that branch's current show dates.
  4. Broadcasts: new movies (any category), category changes, and new
     ticket dates for any movie at any branch.
  5. For every (location, movie, date) a friend picked on the signup
     page, checks whether that date is now in the branch's show-date
     list. The moment it is, only that friend gets an email (sent once,
     tracked in state so it isn't repeated).
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
    """(location, movie_id, date) -> which subscriber emails want to know."""
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


def build_relevant_pairs(updated_movies, all_locations, combos):
    """Which (location_id, movie_id) pairs are worth checking this run:
    every branch for every currently-"running" movie, PLUS any branch/movie
    a subscriber personally picked (even if it's still "upcoming") so a
    watched movie's ticket-opening gets caught the moment it happens."""
    pairs = set()
    location_ids = [loc["id"] for loc in all_locations if loc.get("id") is not None]

    for info in updated_movies.values():
        if info.get("category", "").lower() == "running" and info.get("movie_id"):
            for loc_id in location_ids:
                pairs.add((loc_id, info["movie_id"]))

    for (loc_id, movie_id, _date) in combos.keys():
        pairs.add((loc_id, movie_id))

    return pairs


async def check_dates_per_location(page, token, updated_movies, pairs, all_locations, is_first_run, combos, state):
    """Checks each movie's schedule AT EACH BRANCH separately (the v4 fix,
    kept as-is) using only cineplex-web-api. Also cross-checks each
    subscriber's watched (location, movie, date) against the same
    per-branch date list, so personal alerts no longer need the
    Cloudflare-protected ticket site at all."""
    loc_title_by_id = {loc["id"]: loc["title"] for loc in all_locations if loc.get("id") is not None}

    by_movie_id = {}
    for key, info in updated_movies.items():
        if info.get("movie_id"):
            by_movie_id[info["movie_id"]] = {"key": key, "slug": info.get("slug"), "title": info.get("title")}

    # (loc, movie_id) -> list of (date, [emails]) this run needs to check
    combos_by_pair = {}
    for (loc_id, movie_id, date), info in combos.items():
        combos_by_pair.setdefault((loc_id, movie_id), []).append((date, info))

    sent_registry = state.setdefault("ticket_alerts_sent", {})
    alerts_by_email = {}

    checked = 0
    for loc_id, movie_id in sorted(pairs):
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
            show_times = []

        # DEBUG: for any (movie, location) someone is personally watching,
        # dump the raw show_time entries so we can see what field (if any)
        # distinguishes "on the schedule" from "tickets actually on sale".
        if (loc_id, movie_id) in combos_by_pair and show_times:
            log(f"  DEBUG raw show_time for \"{title}\" @ {loc_title}: {json.dumps(show_times, ensure_ascii=False)[:3000]}")

        checked += 1
        current_dates_set = set(current_dates)
        fresh_dates = sorted(current_dates_set - previous_dates)

        # Broadcast: only alert on genuinely new dates, and never on the
        # very first time we ever look at a (movie, location) combo.
        if fresh_dates and previous_dates and not is_first_run:
            new_date_alerts.append(
                f'NEW TICKET DATES for "{title}" at {loc_title}: {", ".join(fresh_dates)}'
            )
            log(f"  -> NEW DATES: {title} @ {loc_title}: {fresh_dates}")

        # Personal watch: does this branch/movie's current date list now
        # include a date someone specifically signed up to watch?
        for date, watch_info in combos_by_pair.get((loc_id, movie_id), []):
            reg_key = f"{loc_id}:{movie_id}:{date}"
            if sent_registry.get(reg_key):
                continue
            if date in current_dates_set:
                sent_registry[reg_key] = {
                    "movieTitle": watch_info["movieTitle"],
                    "locTitle": watch_info["locTitle"],
                    "date": date,
                    "found_at": datetime.now(timezone.utc).isoformat(),
                }
                message = (
                    f'TICKETS OPEN: "{watch_info["movieTitle"]}" at {watch_info["locTitle"]} '
                    f'on {format_date_display(date)}!'
                )
                for email in watch_info["emails"]:
                    alerts_by_email.setdefault(email, []).append(message)
                log(f"  -> PERSONAL ALERT: {message}")

        dates_by_location[loc_key] = current_dates

    log(f"Checked {checked} (movie, location) combination(s) for new ticket dates.")
    return alerts_by_email


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
            warnings.append("Could not log into cineplex-web-api - skipped this run's checks.")
            await browser.close()
            state["last_checked"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            return subscribers, ticket_alerts_by_email

        flat_movies = await fetch_movie_list(page, web_token)
        all_locations = await fetch_locations(page, web_token)
        updated_movies, is_first_run = track_new_movies_and_categories(flat_movies, state)

        pairs = build_relevant_pairs(updated_movies, all_locations, combos)
        ticket_alerts_by_email = await check_dates_per_location(
            page, web_token, updated_movies, pairs, all_locations, is_first_run, combos, state
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


if __name__ == "__main__":
    main()

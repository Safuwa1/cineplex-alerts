"""
Cineplex Alert System - Final monitor (multi-subscriber version)
--------------------------------------------------------------------
Runs on a schedule (see .github/workflows/monitor.yml). Each run:
  1. Logs into cineplexbd.com's API the same way the real website does.
  2. Fetches the current movie list.
  3. Fetches showtime dates for every currently-running movie.
  4. Compares everything against the last saved state (state.json).
  5. Emails every subscriber (from the signup Google Sheet, plus the
     owner's RECEIVER_EMAIL) if a new movie appeared, or new ticket
     dates opened. Recipients never see each other's addresses.
  6. Saves the new state so next run can compare against it.
"""

import asyncio
import csv
import io
import json
import os
import smtplib
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText

from playwright.async_api import async_playwright

HOME_URL = "https://www.cineplexbd.com/"
API_BASE = "https://cineplex-web-api.cineplexbd.com/api/v1"
STATE_FILE = "state.json"

# The published CSV link for the signup page's Google Sheet.
SUBSCRIBERS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTeWzI3wdsJvW_m8ofLzSOLJ9Ck8DXtemyGiwGeVAcD6vEm9cD1ErMNMWEXK2orijP4rbgJGIZ6If0-/pub?output=csv"

new_movie_alerts = []
new_date_alerts = []
category_change_alerts = []
warnings = []


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"movies": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_subscriber_emails():
    """Fetch signup emails from the published Google Sheet CSV."""
    emails = set()
    try:
        req = urllib.request.Request(
            SUBSCRIBERS_CSV_URL, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
        rows = list(csv.reader(io.StringIO(content)))
        if not rows:
            return emails
        header = [h.strip().lower() for h in rows[0]]
        email_col = next((i for i, h in enumerate(header) if "email" in h), None)
        if email_col is None and len(header) > 1:
            email_col = 1  # fallback: assume 2nd column (after Timestamp)
        for row in rows[1:]:
            if email_col is not None and email_col < len(row):
                val = row[email_col].strip()
                if "@" in val and "." in val.split("@")[-1]:
                    emails.add(val)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Could not fetch subscriber list: {exc}")
    return emails


def get_all_recipients():
    recipients = get_subscriber_emails()
    owner_email = os.environ.get("RECEIVER_EMAIL", "").strip()
    if owner_email:
        recipients.add(owner_email)
    return sorted(recipients)


async def api_call(page, token, path):
    return await page.evaluate(
        """async ({base, path, token}) => {
            const res = await fetch(base + path, {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + token,
                    'Accept': 'application/json',
                },
            });
            const text = await res.text();
            return {status: res.status, body: text};
        }""",
        {"base": API_BASE, "path": path, "token": token},
    )


async def run_monitor():
    is_first_run = not os.path.exists(STATE_FILE)
    state = load_state()
    old_movies = state.get("movies", {})
    updated_movies = {}

    token = None

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )

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

        await page.goto(HOME_URL, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(3000)

        if not token:
            await browser.close()
            raise RuntimeError("Could not obtain an auth token from the site - it may be down or changed.")

        list_result = await api_call(page, token, "/movie-list")
        list_data = json.loads(list_result["body"])
        categories = list_data.get("data", {}) or {}

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
                new_movie_alerts.append(
                    f'NEW MOVIE: "{title}" ({category}) - release: {m.get("release", "n/a")}'
                )
            elif prev is not None and prev.get("category") != category:
                category_change_alerts.append(
                    f'"{title}" moved from {prev.get("category")} to {category}'
                )

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

        await browser.close()

    state["movies"] = updated_movies
    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    return is_first_run, updated_movies


def send_email(subject: str, body: str):
    sender = os.environ["SENDER_EMAIL"]
    password = os.environ["SENDER_APP_PASSWORD"]
    recipients = get_all_recipients()

    if not recipients:
        print("No recipients found (no subscribers and no RECEIVER_EMAIL) - skipping send.")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    # Recipients are only listed in the SMTP envelope below, never in a
    # visible header, so subscribers can't see each other's addresses.
    msg["To"] = sender

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())

    print(f"Email sent to {len(recipients)} recipient(s).")


def main():
    is_first_run, updated_movies = asyncio.run(run_monitor())

    if is_first_run:
        running = [m["title"] for m in updated_movies.values() if m["category"].lower() == "running"]
        body = (
            "Your Star Cineplex alert system is now live and checking automatically.\n\n"
            "Currently tracked as 'running':\n- " + "\n- ".join(running or ["(none found)"]) +
            "\n\nYou'll get an email whenever a new movie appears, or new ticket dates open."
        )
        send_email(subject="Star Cineplex Alerts - Setup complete", body=body)
        print("First run complete. Baseline saved and confirmation email sent.")
        return

    alerts = new_movie_alerts + category_change_alerts + new_date_alerts
    if not alerts:
        print("No changes detected this run.")
        if warnings:
            print("Warnings:\n" + "\n".join(warnings))
        return

    body = "\n".join(alerts)
    if warnings:
        body += "\n\n(Notes: " + "; ".join(warnings) + ")"
    body += "\n\nBook here: https://ticket.cineplexbd.com/home"

    send_email(subject=f"Star Cineplex Alert - {len(alerts)} update(s)", body=body)
    print("Alert email sent:\n" + body)


if __name__ == "__main__":
    main()

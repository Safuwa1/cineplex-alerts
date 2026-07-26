"""
Cineplex Alert System - Phase 4 discovery: seat availability
-----------------------------------------------------------------
Goal: find the API Star Cineplex uses to report seat availability for
a specific showtime, using "The Odyssey" (28th, ~7:00, Sony Square) as
the real-world example. Tries direct educated-guess API calls AND a
best-effort UI click-through, capturing everything along the way.
"""

import asyncio
import json
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from urllib.parse import urlparse

from playwright.async_api import async_playwright

HOME_URL = "https://www.cineplexbd.com/"
API_BASE = "https://cineplex-web-api.cineplexbd.com/api/v1"
TARGET_MOVIE_HINT = "odyssey"
TARGET_DAY_HINT = "28"
TARGET_TIME_HINT = "7:00"

captured = []
notes = []


def log(note):
    notes.append(note)
    print(note)


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


async def handle_response(response):
    try:
        req = response.request
        url = response.url
        host = urlparse(url).hostname or ""
        if host != "cineplex-web-api.cineplexbd.com":
            return
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            return
        path = urlparse(url).path
        known = ["/login", "/location", "/slider", "/notices", "/blog", "/vote-movie-list"]
        if any(k in path for k in known):
            return
        body = await response.text()
        captured.append(
            {
                "source": "passive-capture",
                "url": url,
                "method": req.method,
                "status": response.status,
                "body_preview": body[:4000],
            }
        )
    except Exception as exc:  # noqa: BLE001
        captured.append({"url": response.url, "error": str(exc)})


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        context.on("response", lambda r: asyncio.create_task(handle_response(r)))

        page = await context.new_page()

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

        await page.goto(HOME_URL, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(3000)

        if not token:
            log("Could not get auth token - aborting.")
            await browser.close()
            return

        log("Got auth token.")

        # 1. Find "The Odyssey" slug from movie-list.
        list_result = await api_call(page, token, "/movie-list")
        list_data = json.loads(list_result["body"])
        categories = list_data.get("data", {}) or {}
        target_movie = None
        for category, movies in categories.items():
            if not isinstance(movies, list):
                continue
            for m in movies:
                title = m.get("title") or m.get("movie_title") or ""
                if TARGET_MOVIE_HINT in title.lower():
                    target_movie = m
                    log(f"Found target movie: {title} (category={category}, slug={m.get('slug')})")
                    break
            if target_movie:
                break

        if not target_movie:
            log(f"Could not find a movie matching '{TARGET_MOVIE_HINT}' in movie-list.")
            await browser.close()
            return

        slug = target_movie.get("slug")

        # 2. Get its full detail / show_time.
        detail_result = await api_call(page, token, f"/movie/{slug}/detail")
        detail_data = json.loads(detail_result["body"])
        show_time = detail_data.get("data", {}).get("show_time", []) or []
        captured.append({"source": "movie-detail-baseline", "show_time": show_time})

        target_slot = None
        target_date_entry = None
        for day in show_time:
            if TARGET_DAY_HINT in str(day.get("raw_date", "")).split("-")[-1]:
                for slot in day.get("slot", []):
                    if TARGET_TIME_HINT in str(slot.get("time", "")):
                        target_slot = slot
                        target_date_entry = day
                        break
            if target_slot:
                break

        if target_slot:
            log(
                f"Matched example showtime: {target_date_entry.get('date')} {target_slot.get('time')} "
                f"hall_id={target_slot.get('hall_id')} showtime_id={target_slot.get('showtime_id')} "
                f"schedule_id={target_slot.get('schedule_id')}"
            )
        else:
            log("Could not match the exact 28th/7:00 slot; using the first available slot instead for testing.")
            if show_time and show_time[0].get("slot"):
                target_date_entry = show_time[0]
                target_slot = show_time[0]["slot"][0]

        # 3. Test whether movie-detail is location-scoped, by passing a location_id.
        for loc_id in [1, 4]:
            try:
                loc_result = await api_call(page, token, f"/movie/{slug}/detail", body={"location_id": loc_id})
                loc_data = json.loads(loc_result["body"])
                loc_show_time = loc_data.get("data", {}).get("show_time", [])
                captured.append(
                    {
                        "source": f"movie-detail-with-location_id-{loc_id}",
                        "status": loc_result["status"],
                        "show_time_summary": [
                            {"date": d.get("raw_date"), "halls": [s.get("hall_id") for s in d.get("slot", [])]}
                            for d in loc_show_time
                        ],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                captured.append({"source": f"movie-detail-with-location_id-{loc_id}", "error": str(exc)})

        # 4. Try educated-guess seat endpoints using the real schedule_id/showtime_id.
        if target_slot:
            schedule_id = target_slot.get("schedule_id")
            showtime_id = target_slot.get("showtime_id")
            guesses = [
                ("/seat-plan", {"schedule_id": schedule_id}),
                ("/seat-plan", {"showtime_id": showtime_id}),
                ("/seatplan", {"schedule_id": schedule_id}),
                ("/show-seats", {"schedule_id": schedule_id}),
                ("/seats", {"schedule_id": schedule_id}),
                ("/booking/seats", {"schedule_id": schedule_id}),
                ("/schedule/seats", {"schedule_id": schedule_id}),
                ("/hall-seats", {"schedule_id": schedule_id}),
                ("/seat-layout", {"schedule_id": schedule_id}),
                ("/get-seats", {"schedule_id": schedule_id}),
            ]
            for path, body in guesses:
                try:
                    r = await api_call(page, token, path, body=body)
                    captured.append(
                        {
                            "source": f"guess {path}",
                            "body_sent": body,
                            "status": r["status"],
                            "body_preview": r["body"][:500],
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    captured.append({"source": f"guess {path}", "error": str(exc)})

        # 5. Best-effort UI click-through, to find the real "Book Now" flow/URL pattern.
        try:
            await page.goto(f"https://www.cineplexbd.com/detail/{slug}", wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(4000)
            log(f"Loaded detail page for {slug}")

            book_locator = None
            for text in ["Get Ticket", "Book Now", "Buy Ticket", "Book Ticket", "BOOK NOW", "Book"]:
                loc = page.get_by_text(text, exact=False).first
                try:
                    await loc.wait_for(timeout=2000)
                    book_locator = loc
                    log(f"Found a 'book' button with text matching '{text}'")
                    break
                except Exception:  # noqa: BLE001
                    continue

            if book_locator:
                try:
                    async with context.expect_page(timeout=5000) as new_page_info:
                        await book_locator.click()
                    new_page = await new_page_info.value
                    new_page.on("response", lambda r: asyncio.create_task(handle_response(r)))
                    await new_page.wait_for_load_state("networkidle", timeout=20000)
                    await new_page.wait_for_timeout(4000)
                    log(f"Book button opened new tab at: {new_page.url}")
                except Exception:
                    await book_locator.click(timeout=5000)
                    await page.wait_for_load_state("networkidle", timeout=20000)
                    await page.wait_for_timeout(4000)
                    log(f"Book button navigated same tab to: {page.url}")
            else:
                log("Could not find any 'Book/Get Ticket' button on the detail page via text search.")
        except Exception as exc:  # noqa: BLE001
            log(f"UI click-through attempt failed: {exc}")

        await browser.close()


def build_report():
    return (
        "NOTES:\n"
        + "\n".join(f"- {n}" for n in notes)
        + "\n\nCAPTURED:\n"
        + json.dumps(captured, indent=2, ensure_ascii=False)
    )


def send_email(subject: str, body: str):
    sender = os.environ["SENDER_EMAIL"]
    password = os.environ["SENDER_APP_PASSWORD"]
    receiver = os.environ["RECEIVER_EMAIL"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = receiver
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, [receiver], msg.as_string())


def main():
    asyncio.run(run())
    report = build_report()
    with open("discovery_output.json", "w", encoding="utf-8") as f:
        f.write(report)
    email_body = report[:18000]
    if len(report) > 18000:
        email_body += "\n\n...[truncated, full version is the workflow artifact]"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    send_email(f"[Cineplex Alert Setup] Seat discovery ({ts})", email_body)
    print("done")


if __name__ == "__main__":
    main()

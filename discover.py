"""
Cineplex Alert System - Phase 1.5: Deeper discovery
------------------------------------------------------
Builds on the first discovery pass: this version also tries to click
into a specific movie's page to find the showtime/ticket-date data the
site uses internally, and captures a larger preview of the movie list.
"""

import asyncio
import json
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText

from playwright.async_api import async_playwright

HOME_URL = "https://www.cineplexbd.com/"
TICKET_URL = "https://ticket.cineplexbd.com/home"
HOST_HINTS = ["cineplexbd"]
# A movie we already confirmed is currently running, used as a click target.
SAMPLE_MOVIE_TITLE = "Moana"

captured = []
notes = []


def log(note):
    notes.append(note)


async def handle_response(response):
    try:
        req = response.request
        url = response.url
        if not any(hint in url for hint in HOST_HINTS):
            return
        content_type = response.headers.get("content-type", "")
        looks_like_data = "json" in content_type.lower() or req.resource_type in ("xhr", "fetch")
        if not looks_like_data:
            return
        if response.status >= 400:
            captured.append({"url": url, "method": req.method, "status": response.status, "note": "error response"})
            return
        body = await response.text()
        limit = 6000 if "movie" in url else 3000
        captured.append(
            {
                "url": url,
                "method": req.method,
                "status": response.status,
                "content_type": content_type,
                "body_preview": body[:limit],
            }
        )
    except Exception as exc:  # noqa: BLE001
        captured.append({"url": response.url, "error": str(exc)})


async def try_click_movie(page, context):
    """Try a few strategies to open a movie's ticket/showtime page."""
    strategies_tried = []

    # Strategy A: click the movie title text, expecting it opens a new tab.
    try:
        locator = page.get_by_text(SAMPLE_MOVIE_TITLE, exact=False).first
        await locator.wait_for(timeout=8000)
        strategies_tried.append(f"Found text '{SAMPLE_MOVIE_TITLE}' on homepage")
        async with context.expect_page(timeout=6000) as new_page_info:
            await locator.click()
        new_page = await new_page_info.value
        await new_page.wait_for_load_state("networkidle", timeout=20000)
        await new_page.wait_for_timeout(4000)
        strategies_tried.append(f"New tab opened at: {new_page.url}")
        return strategies_tried
    except Exception as exc:  # noqa: BLE001
        strategies_tried.append(f"Strategy A (new tab on click) did not trigger: {exc}")

    # Strategy B: same-tab click.
    try:
        locator = page.get_by_text(SAMPLE_MOVIE_TITLE, exact=False).first
        await locator.click(timeout=5000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(4000)
        strategies_tried.append(f"Clicked in same tab, page is now at: {page.url}")
        return strategies_tried
    except Exception as exc:  # noqa: BLE001
        strategies_tried.append(f"Strategy B (same-tab click) failed: {exc}")

    # Strategy C: scan all links on the page for one that mentions the movie.
    try:
        hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        candidate = next((h for h in hrefs if "moana" in h.lower()), None)
        strategies_tried.append(f"Scanned {len(hrefs)} links, candidate found: {candidate}")
        if candidate:
            new_page = await context.new_page()
            new_page.on("response", lambda r: asyncio.create_task(handle_response(r)))
            await new_page.goto(candidate, wait_until="networkidle", timeout=20000)
            await new_page.wait_for_timeout(4000)
            strategies_tried.append(f"Navigated directly to candidate link: {new_page.url}")
    except Exception as exc:  # noqa: BLE001
        strategies_tried.append(f"Strategy C (href scan) failed: {exc}")

    return strategies_tried


async def run_discovery():
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
        try:
            await page.goto(HOME_URL, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(4000)
            log("Loaded homepage successfully")
        except Exception as exc:  # noqa: BLE001
            log(f"Homepage load failed: {exc}")

        click_log = await try_click_movie(page, context)
        notes.extend(click_log)

        try:
            ticket_page = await context.new_page()
            await ticket_page.goto(TICKET_URL, wait_until="networkidle", timeout=45000)
            await ticket_page.wait_for_timeout(4000)
            log("Loaded ticket.cineplexbd.com/home successfully")
        except Exception as exc:  # noqa: BLE001
            log(f"Ticket home load failed: {exc}")

        await browser.close()


def build_report():
    parts = [
        "NOTES:\n" + "\n".join(f"- {n}" for n in notes),
        "\nCAPTURED CALLS:\n" + json.dumps(captured, indent=2, ensure_ascii=False),
    ]
    return "\n".join(parts)


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
    asyncio.run(run_discovery())

    full_report = build_report()

    with open("discovery_output.json", "w", encoding="utf-8") as f:
        f.write(full_report)

    email_body = full_report[:15000]
    if len(full_report) > 15000:
        email_body += "\n\n...[truncated for email - full version attached as a workflow artifact]"

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    send_email(
        subject="[Cineplex Alert Setup] Discovery results (phase 2)",
        body=f"Discovery run completed at {timestamp}\n\n{email_body}",
    )
    print("Discovery complete. Email sent.")


if __name__ == "__main__":
    main()

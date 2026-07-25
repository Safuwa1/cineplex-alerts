"""
Cineplex Alert System - Phase 1.6: Focused discovery
--------------------------------------------------------
Navigates directly to a movie detail page (now that we know the route)
and captures ONLY the meaningful API responses, skipping already-known
endpoints and non-data files, so the important ticket/showtime data
isn't crowded out of the email report.
"""

import asyncio
import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from urllib.parse import urlparse

from playwright.async_api import async_playwright

DETAIL_URL = "https://www.cineplexbd.com/detail/moana"

API_HOST = "cineplex-web-api.cineplexbd.com"
# Endpoints we've already seen the shape of - skip printing their full body again.
KNOWN_LOW_VALUE = ["/slider", "/notices", "/blog", "/vote-movie-list", "/location", "/login"]

captured = []
notes = []


def log(note):
    notes.append(note)


async def handle_response(response):
    try:
        req = response.request
        url = response.url
        host = urlparse(url).hostname or ""
        if host != API_HOST:
            return
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            return
        if response.status >= 400:
            captured.append({"url": url, "method": req.method, "status": response.status, "note": "error response"})
            return

        path = urlparse(url).path
        if any(known in path for known in KNOWN_LOW_VALUE):
            captured.append({"url": url, "note": "already known endpoint, body skipped to save space"})
            return

        body = await response.text()
        captured.append(
            {
                "url": url,
                "method": req.method,
                "status": response.status,
                "content_type": content_type,
                "body_preview": body[:8000],
            }
        )
    except Exception as exc:  # noqa: BLE001
        captured.append({"url": response.url, "error": str(exc)})


async def try_click_a_date(page):
    """Best-effort: look for a clickable date-like element and click it."""
    date_pattern = re.compile(r"^\s*(\d{1,2})\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)?", re.I)
    checked = 0
    try:
        candidates = await page.locator("button, [role=button], a, div").all()
        for el in candidates[:400]:
            checked += 1
            try:
                text = (await el.inner_text(timeout=300)).strip()
            except Exception:
                continue
            if text and len(text) <= 10 and date_pattern.match(text):
                await el.click(timeout=3000)
                await page.wait_for_timeout(4000)
                return f"Clicked a date-like element: '{text}' (checked {checked} candidates)"
        return f"No clickable date-like element found (checked {checked} candidates)"
    except Exception as exc:  # noqa: BLE001
        return f"Date-click attempt failed after checking {checked}: {exc}"


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
            await page.goto(DETAIL_URL, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(6000)
            log(f"Loaded {DETAIL_URL} successfully")
        except Exception as exc:  # noqa: BLE001
            log(f"Detail page load failed: {exc}")

        date_click_note = await try_click_a_date(page)
        log(date_click_note)
        await page.wait_for_timeout(3000)

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

    email_body = full_report[:18000]
    if len(full_report) > 18000:
        email_body += "\n\n...[truncated for email - full version attached as a workflow artifact]"

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    send_email(
        subject="[Cineplex Alert Setup] Discovery results (phase 3)",
        body=f"Discovery run completed at {timestamp}\n\n{email_body}",
    )
    print("Discovery complete. Email sent.")


if __name__ == "__main__":
    main()

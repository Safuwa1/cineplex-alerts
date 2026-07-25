"""
Cineplex Alert System - Phase 1: Discovery
--------------------------------------------
This script visits cineplexbd.com and ticket.cineplexbd.com with a real
(headless) browser, listens to every data request the site makes behind
the scenes, and emails a report of what it found.

You do not need to understand this file. It is meant to be run once,
by GitHub Actions, so we can see the site's real data format and build
the final monitoring script around it.
"""

import asyncio
import json
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText

from playwright.async_api import async_playwright

TARGET_PAGES = [
    "https://www.cineplexbd.com/",
    "https://ticket.cineplexbd.com/home",
]

# Only capture responses whose URL contains one of these hints, to avoid
# noise from ads/analytics/fonts.
HOST_HINTS = ["cineplexbd"]

captured = []


async def handle_response(response):
    try:
        req = response.request
        url = response.url
        if not any(hint in url for hint in HOST_HINTS):
            return

        content_type = response.headers.get("content-type", "")
        looks_like_data = "json" in content_type.lower() or req.resource_type in (
            "xhr",
            "fetch",
        )
        if not looks_like_data:
            return
        if response.status >= 400:
            captured.append(
                {
                    "url": url,
                    "method": req.method,
                    "status": response.status,
                    "note": "error response, no body captured",
                }
            )
            return

        body = await response.text()
        captured.append(
            {
                "url": url,
                "method": req.method,
                "status": response.status,
                "content_type": content_type,
                "body_preview": body[:3000],
            }
        )
    except Exception as exc:  # noqa: BLE001
        captured.append({"url": response.url, "error": str(exc)})


async def run_discovery():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page.on("response", lambda r: asyncio.create_task(handle_response(r)))

        for url in TARGET_PAGES:
            try:
                await page.goto(url, wait_until="networkidle", timeout=45000)
                await page.wait_for_timeout(5000)
            except Exception as exc:  # noqa: BLE001
                captured.append({"url": url, "error": f"navigation failed: {exc}"})

        await browser.close()


def build_report():
    if not captured:
        return "No data calls were captured. The site may block automated browsers, or use a different loading pattern than expected."
    return json.dumps(captured, indent=2, ensure_ascii=False)


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

    # Always save the full, untruncated report as a file so it becomes a
    # downloadable GitHub Actions artifact, in case the email is very long.
    with open("discovery_output.json", "w", encoding="utf-8") as f:
        f.write(full_report)

    email_body = full_report[:15000]
    if len(full_report) > 15000:
        email_body += "\n\n...[truncated for email - full version attached as a workflow artifact]"

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    send_email(
        subject="[Cineplex Alert Setup] Discovery results",
        body=f"Discovery run completed at {timestamp}\n\n{email_body}",
    )
    print("Discovery complete. Email sent.")


if __name__ == "__main__":
    main()

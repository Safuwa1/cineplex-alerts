"""
Cineplex Alert System - Phase 5b discovery: try GUEST LOGIN
------------------------------------------------------------------
Tests whether clicking "GUEST LOGIN" on ticket.cineplexbd.com/login
lets us proceed into the booking flow (and reach seat data) WITHOUT
needing a real account or solving reCAPTCHA. No credentials involved.
"""

import asyncio
import json
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from urllib.parse import urlparse

from playwright.async_api import async_playwright

LOGIN_URL = "https://ticket.cineplexbd.com/login"

captured = []
notes = []


def log(note):
    notes.append(note)
    print(note)


async def handle_response(response):
    try:
        req = response.request
        url = response.url
        host = urlparse(url).hostname or ""
        if "cineplexbd.com" not in host:
            return
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            return
        body = await response.text()
        captured.append(
            {
                "url": url,
                "method": req.method,
                "status": response.status,
                "body_preview": body[:2000],
            }
        )
    except Exception as exc:  # noqa: BLE001
        captured.append({"url": response.url, "error": str(exc)})


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page.on("response", lambda r: asyncio.create_task(handle_response(r)))

        await page.goto(LOGIN_URL, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(3000)
        log(f"Loaded {LOGIN_URL}")

        try:
            guest_btn = page.get_by_text("GUEST LOGIN", exact=False).first
            await guest_btn.click(timeout=8000)
            log("Clicked GUEST LOGIN.")
            await page.wait_for_timeout(4000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            log(f"After clicking GUEST LOGIN, page is now at: {page.url}")

            # Check for any visible error text (e.g. recaptcha failure messages).
            body_text = await page.inner_text("body")
            lowered = body_text.lower()
            if "recaptcha" in lowered or "captcha" in lowered:
                log("Page text mentions 'captcha' somewhere - noting for review.")
            if "error" in lowered or "invalid" in lowered:
                log("Page text mentions 'error'/'invalid' somewhere - noting for review.")

        except Exception as exc:  # noqa: BLE001
            log(f"Clicking GUEST LOGIN failed: {exc}")

        await page.wait_for_timeout(3000)
        await browser.close()


def build_report():
    return (
        "NOTES:\n" + "\n".join(f"- {n}" for n in notes)
        + "\n\nCAPTURED:\n" + json.dumps(captured, indent=2, ensure_ascii=False)
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
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    send_email(f"[Cineplex Alert Setup] Guest login test ({ts})", report[:18000])
    print("done")


if __name__ == "__main__":
    main()

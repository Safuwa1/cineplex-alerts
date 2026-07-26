"""
Cineplex Alert System - Phase 5 discovery: inspect the real login page
--------------------------------------------------------------------------
No credentials are used here. This just loads ticket.cineplexbd.com/login
and reports what input fields exist (email? phone? password? OTP?) and
what API call the page makes when it loads, so we can plan a safe
"log in once, reuse the token" approach instead of repeated logins.
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
                "body_preview": body[:1500],
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
        await page.wait_for_timeout(4000)
        log(f"Loaded {LOGIN_URL}")

        # Inspect every input/button on the page without submitting anything.
        fields = await page.eval_on_selector_all(
            "input, button, textarea",
            """els => els.map(e => ({
                tag: e.tagName,
                type: e.type || null,
                name: e.name || null,
                id: e.id || null,
                placeholder: e.placeholder || null,
                text: (e.innerText || e.value || '').trim().slice(0, 40)
            }))"""
        )
        captured.append({"source": "login-page-form-fields", "fields": fields})
        log(f"Found {len(fields)} input/button/textarea elements on the login page.")

        # Also grab any visible headings/labels for context.
        texts = await page.eval_on_selector_all(
            "h1, h2, h3, label, p",
            "els => els.map(e => e.innerText.trim()).filter(t => t && t.length < 100)"
        )
        captured.append({"source": "login-page-text", "text_snippets": texts[:30]})

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
    send_email(f"[Cineplex Alert Setup] Login page inspection ({ts})", report[:18000])
    print("done")


if __name__ == "__main__":
    main()

"""
Cineplex Alert System - Phase 5f discovery: click all the way to seats
------------------------------------------------------------------------
Clicks through the REAL UI: guest login -> Sony Square -> Purchase
Ticket -> The Odyssey -> a specific showtime button -> (hopefully) the
seat layout screen. Logs every request+response to cineplex-ticket-api
along the way so we can see the real seat endpoint, whatever it's named.
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

request_log = []
response_log = []
notes = []


def log(note):
    notes.append(note)
    print(note)


def handle_request(request):
    try:
        host = urlparse(request.url).hostname or ""
        if "cineplex-ticket-api.cineplexbd.com" not in host:
            return
        request_log.append(
            {
                "url": request.url,
                "method": request.method,
                "post_data": request.post_data,
            }
        )
    except Exception as exc:  # noqa: BLE001
        request_log.append({"error": str(exc)})


async def handle_response(response):
    try:
        host = urlparse(response.url).hostname or ""
        if "cineplex-ticket-api.cineplexbd.com" not in host:
            return
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            return
        body = await response.text()
        response_log.append({"url": response.url, "status": response.status, "body_preview": body[:2500]})
    except Exception as exc:  # noqa: BLE001
        response_log.append({"error": str(exc)})


async def try_click(page, texts, label, timeout=4000):
    for text in texts:
        try:
            loc = page.get_by_text(text, exact=False).first
            await loc.wait_for(timeout=timeout)
            await loc.click(timeout=timeout)
            log(f"[{label}] clicked element matching text '{text}'")
            await page.wait_for_timeout(3000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            return True
        except Exception:  # noqa: BLE001
            continue
    log(f"[{label}] could not find/click any of: {texts}")
    return False


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page.on("request", handle_request)
        page.on("response", lambda r: asyncio.create_task(handle_response(r)))

        await page.goto(LOGIN_URL, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(3000)

        guest_btn = page.get_by_text("GUEST LOGIN", exact=False).first
        await guest_btn.click(timeout=8000)
        await page.wait_for_timeout(3000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        log(f"Logged in as guest. Now at: {page.url}")

        await try_click(page, ["Sony Square", "Sony"], "select-location")
        await try_click(page, ["PURCHASE TICKET", "Purchase Ticket"], "purchase-ticket-button")
        await try_click(page, ["The Odyssey", "Odyssey"], "select-movie")

        # Try to click a showtime button - several possible text formats.
        clicked_showtime = await try_click(
            page,
            ["07:00 PM", "7:00 PM", "19:00", "07:00", "7:00", "500", "৳500"],
            "select-showtime",
        )

        if not clicked_showtime:
            # Dump visible clickable text so we can see what the showtime buttons actually say.
            visible_texts = await page.eval_on_selector_all(
                "button, a, div[role=button], span",
                """els => els.slice(0, 200).map(e => (e.innerText || '').trim()).filter(t => t && t.length < 30)"""
            )
            request_log.append({"note": "visible short texts on page after selecting movie", "texts": visible_texts})
            log(f"Dumped {len(visible_texts)} visible short text snippets for manual inspection.")

        log(f"Final URL: {page.url}")
        await page.wait_for_timeout(3000)
        await browser.close()


def build_report():
    return (
        "NOTES:\n" + "\n".join(f"- {n}" for n in notes)
        + "\n\nREQUESTS:\n" + json.dumps(request_log, indent=2, ensure_ascii=False)
        + "\n\nRESPONSES:\n" + json.dumps(response_log, indent=2, ensure_ascii=False)
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
    email_body = report[:18000]
    if len(report) > 18000:
        email_body += "\n\n...[truncated, full version is the workflow artifact]"
    send_email(f"[Cineplex Alert Setup] Click-to-seats discovery ({ts})", email_body)
    print("done")


if __name__ == "__main__":
    main()

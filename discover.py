"""
Cineplex Alert System - Phase 5n discovery: click the real '+' icon precisely
---------------------------------------------------------------------------------
Found the container: .ticket_booking_left_item.ticket_qty holds the quantity
icons as <img> elements (SVG icons, not text). This targets that container
directly - clicking the LAST icon in it (typically '+') - and also dumps
its full untruncated HTML for verification.
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
text_dumps = []


def log(note):
    notes.append(note)
    print(note)


def handle_request(request):
    try:
        host = urlparse(request.url).hostname or ""
        if "cineplex-ticket-api.cineplexbd.com" not in host:
            return
        request_log.append({"url": request.url, "method": request.method, "post_data": request.post_data})
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
        response_log.append({"url": response.url, "status": response.status, "body_preview": body[:1000]})
    except Exception as exc:  # noqa: BLE001
        response_log.append({"error": str(exc)})


async def dump_text(page, label):
    try:
        text = await page.inner_text("body")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        text_dumps.append({"stage": label, "lines": lines[:150]})
        log(f"[dump:{label}] {len(lines)} non-empty lines captured.")
    except Exception as exc:  # noqa: BLE001
        text_dumps.append({"stage": label, "error": str(exc)})


async def safe_wait_idle(page, timeout=12000, label=""):
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:  # noqa: BLE001
        log(f"[{label}] networkidle wait timed out - continuing anyway.")


async def try_click(page, texts, label, timeout=4000, wait_after=2500):
    for text in texts:
        try:
            loc = page.get_by_text(text, exact=False).first
            await loc.wait_for(timeout=timeout)
            await loc.click(timeout=timeout)
            log(f"[{label}] clicked element matching text '{text}'")
            await page.wait_for_timeout(wait_after)
            await safe_wait_idle(page, 12000, label)
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

        try:
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=45000)
        except Exception:  # noqa: BLE001
            log("Initial load networkidle timed out - continuing anyway.")
        await page.wait_for_timeout(3000)

        guest_btn = page.get_by_text("GUEST LOGIN", exact=False).first
        await guest_btn.click(timeout=8000)
        await page.wait_for_timeout(3000)
        await safe_wait_idle(page, 15000, "guest-login")
        log("Logged in as guest.")

        await try_click(page, ["Sony Square", "Sony"], "select-location")
        await try_click(page, ["PURCHASE TICKET", "Purchase Ticket"], "purchase-ticket-button")
        await try_click(page, ["The Odyssey", "Odyssey"], "select-movie")
        await try_click(page, ["07:00 PM", "7:00 PM"], "select-showtime")
        await dump_text(page, "after-select-showtime")

        await try_click(page, ["Premium"], "click-seat-type", wait_after=4000)
        await dump_text(page, "after-seat-type-click")

        try:
            qty_html = await page.eval_on_selector(
                ".ticket_booking_left_item.ticket_qty",
                "el => el ? el.outerHTML.slice(0, 3500) : 'NOT FOUND'"
            )
            text_dumps.append({"stage": "ticket_qty_full_html", "html": qty_html})
            log("Captured full ticket_qty container HTML.")
        except Exception as exc:  # noqa: BLE001
            log(f"Could not get ticket_qty HTML: {exc}")

        try:
            icons = page.locator(".ticket_booking_left_item.ticket_qty img")
            count = await icons.count()
            log(f"Found {count} icon(s) inside the ticket_qty container.")
            if count >= 2:
                await icons.last.click(timeout=5000)
                log("Clicked the LAST icon inside ticket_qty (assumed '+').")
                await page.wait_for_timeout(3000)
                await safe_wait_idle(page, 10000, "click-plus-icon")
        except Exception as exc:  # noqa: BLE001
            log(f"Could not click quantity icon: {exc}")

        await dump_text(page, "after-plus-icon-click")

        log(f"Final URL: {page.url}")
        await page.wait_for_timeout(2000)
        await browser.close()


def build_report():
    return (
        "NOTES:\n" + "\n".join(f"- {n}" for n in notes)
        + "\n\nTEXT DUMPS:\n" + json.dumps(text_dumps, indent=2, ensure_ascii=False)
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
    send_email(f"[Cineplex Alert Setup] Full text dump discovery ({ts})", email_body)
    print("done")


if __name__ == "__main__":
    main()

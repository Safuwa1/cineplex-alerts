"""
Cineplex Alert System - Phase 5c discovery: seat data via guest login
---------------------------------------------------------------------------
Uses the safe GUEST LOGIN flow (no real account, no CAPTCHA) on the
ticket API, then explores the ticket.cineplexbd.com/home page and tries
educated-guess API calls to find movie/showtime/seat-plan endpoints.
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
TICKET_API_BASE = "https://cineplex-ticket-api.cineplexbd.com/api/v1"

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
        path = urlparse(url).path
        if any(k in path for k in ["/guest-login", "/get-location"]):
            return  # already known, skip to save space
        body = await response.text()
        captured.append(
            {
                "source": "passive-capture",
                "url": url,
                "method": req.method,
                "status": response.status,
                "body_preview": body[:2500],
            }
        )
    except Exception as exc:  # noqa: BLE001
        captured.append({"url": response.url, "error": str(exc)})


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
        {"base": TICKET_API_BASE, "path": path, "token": token, "body": body},
    )


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

        token = None

        async def capture_token(response):
            nonlocal token
            if "/guest-login" in response.url and response.status == 200:
                try:
                    data = json.loads(await response.text())
                    if data.get("status") == "success":
                        token = data.get("data", {}).get("token")
                except Exception:  # noqa: BLE001
                    pass

        page.on("response", lambda r: asyncio.create_task(capture_token(r)))

        await page.goto(LOGIN_URL, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(3000)

        guest_btn = page.get_by_text("GUEST LOGIN", exact=False).first
        await guest_btn.click(timeout=8000)
        await page.wait_for_timeout(3000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        log(f"Logged in as guest. Now at: {page.url}")

        if not token:
            log("Did not capture a guest token - aborting further API tests.")
            await browser.close()
            return

        log(f"Captured guest ticket-api token (first 20 chars): {token[:20]}...")

        # Inspect the home page for navigation elements (location/movie pickers).
        elements = await page.eval_on_selector_all(
            "a, button, select, option",
            """els => els.slice(0, 150).map(e => ({
                tag: e.tagName,
                text: (e.innerText || e.value || '').trim().slice(0, 50),
                href: e.href || null
            })).filter(e => e.text || e.href)"""
        )
        captured.append({"source": "home-page-elements", "elements": elements})
        log(f"Collected {len(elements)} nav-ish elements from /home.")

        # Educated guesses for movie-list / showtime / seat-plan endpoints.
        guesses = [
            ("/get-movie-list", {}),
            ("/movie-list", {}),
            ("/get-movies", {"location_id": 4}),
            ("/get-showtime", {"location_id": 4}),
            ("/get-schedule", {"location_id": 4}),
            ("/get-schedule-list", {"location_id": 4}),
            ("/get-seat-plan", {"schedule_id": 129663}),
            ("/seat-plan", {"schedule_id": 129663}),
            ("/get-seats", {"schedule_id": 129663}),
            ("/get-hall-seat", {"schedule_id": 129663}),
        ]
        for path, body in guesses:
            try:
                r = await api_call(page, token, path, body=body)
                captured.append(
                    {
                        "source": f"guess {path}",
                        "body_sent": body,
                        "status": r["status"],
                        "body_preview": r["body"][:800],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                captured.append({"source": f"guess {path}", "error": str(exc)})

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
    email_body = report[:18000]
    if len(report) > 18000:
        email_body += "\n\n...[truncated, full version is the workflow artifact]"
    send_email(f"[Cineplex Alert Setup] Guest seat discovery ({ts})", email_body)
    print("done")


if __name__ == "__main__":
    main()

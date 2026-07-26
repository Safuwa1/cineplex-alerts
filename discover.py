"""
Cineplex Alert System - Phase 5e discovery: seat data via programId
------------------------------------------------------------------------
Full chain now known: guest-login -> get-location -> get-showdate ->
get-shows (gives programId per showtime). This tries to find the seat
endpoint using that real programId, with the correct headers
(appsource, device-key, authorization) captured from the real session.
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
session_info = {"token": None, "device_key": None}


def log(note):
    notes.append(note)
    print(note)


def handle_request(request):
    try:
        host = urlparse(request.url).hostname or ""
        if "cineplex-ticket-api.cineplexbd.com" not in host:
            return
        headers = request.headers
        if not session_info["device_key"] and headers.get("device-key"):
            session_info["device_key"] = headers.get("device-key")
    except Exception:  # noqa: BLE001
        pass


async def api_call(page, path, body=None):
    return await page.evaluate(
        """async ({base, path, token, deviceKey, body}) => {
            const res = await fetch(base + path, {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + token,
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'appsource': 'web',
                    'device-key': deviceKey,
                },
                body: body ? JSON.stringify(body) : undefined,
            });
            const text = await res.text();
            return {status: res.status, body: text};
        }""",
        {
            "base": TICKET_API_BASE,
            "path": path,
            "token": session_info["token"],
            "deviceKey": session_info["device_key"],
            "body": body,
        },
    )


async def try_click(page, texts, label, timeout=4000):
    for text in texts:
        try:
            loc = page.get_by_text(text, exact=False).first
            await loc.wait_for(timeout=timeout)
            await loc.click(timeout=timeout)
            log(f"[{label}] clicked element matching text '{text}'")
            await page.wait_for_timeout(3000)
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

        async def capture_token(response):
            if "/guest-login" in response.url and response.status == 200:
                try:
                    data = json.loads(await response.text())
                    if data.get("status") == "success":
                        session_info["token"] = data.get("data", {}).get("token")
                except Exception:  # noqa: BLE001
                    pass

        page.on("response", lambda r: asyncio.create_task(capture_token(r)))

        await page.goto(LOGIN_URL, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(3000)

        guest_btn = page.get_by_text("GUEST LOGIN", exact=False).first
        await guest_btn.click(timeout=8000)
        await page.wait_for_timeout(3000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        log(f"Logged in as guest. token={'yes' if session_info['token'] else 'NO'}, "
            f"device_key={'yes' if session_info['device_key'] else 'NO'}")

        await try_click(page, ["Sony Square", "Sony"], "select-location")
        await page.wait_for_load_state("networkidle", timeout=15000)
        await try_click(page, ["PURCHASE TICKET", "Purchase Ticket"], "purchase-ticket-button")
        await page.wait_for_load_state("networkidle", timeout=15000)

        if not session_info["token"] or not session_info["device_key"]:
            log("Missing token or device_key - cannot make direct API calls.")
            await browser.close()
            return

        # Re-confirm showtimes for The Odyssey to get a fresh, real programId.
        shows_result = await api_call(page, "/get-shows", {"location": 4, "movieId": 1711, "showDate": "2026-07-27"})
        captured.append({"source": "get-shows (fresh)", "status": shows_result["status"], "body": shows_result["body"][:2000]})

        program_id = None
        try:
            shows_data = json.loads(shows_result["body"])
            for hall in shows_data.get("data", []):
                for st in hall.get("showTimes", []):
                    if st.get("showTime", "").startswith("19"):
                        program_id = st.get("programId")
                        log(f"Using programId={program_id} for the 7 PM show.")
                        break
        except Exception as exc:  # noqa: BLE001
            log(f"Could not parse get-shows response: {exc}")

        if not program_id:
            log("Could not find a programId to test with.")
            await browser.close()
            return

        guesses = [
            ("/get-seat-plan", {"programId": program_id}),
            ("/get-seats", {"programId": program_id}),
            ("/seat-plan", {"programId": program_id}),
            ("/get-seat-layout", {"programId": program_id}),
            ("/get-hall-layout", {"programId": program_id}),
            ("/get-available-seats", {"programId": program_id}),
            ("/get-seat-status", {"programId": program_id}),
            ("/get-booking-seats", {"programId": program_id}),
            ("/seat-availability", {"programId": program_id}),
            ("/get-seatmap", {"programId": program_id}),
        ]
        for path, body in guesses:
            try:
                r = await api_call(page, path, body)
                captured.append(
                    {
                        "source": f"guess {path}",
                        "body_sent": body,
                        "status": r["status"],
                        "body_preview": r["body"][:1500],
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
    send_email(f"[Cineplex Alert Setup] Seat endpoint discovery ({ts})", email_body)
    print("done")


if __name__ == "__main__":
    main()

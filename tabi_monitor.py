#!/usr/bin/env python3
"""TabiToken registration monitor.

Fills the signup form at https://tabitoken.com/sign-up and reads the sonner
toast that comes back. As long as the toast says "New user registration has
been disabled by administrator", registration is closed. Any OTHER outcome
(a redirect, a success toast, even "username already exists") means the
registration request was actually processed => registration is OPEN.

Statuses:  closed  -> the disabled-by-admin toast appeared
           open    -> the request was processed / we were redirected
           unknown -> transient trouble (network, turnstile, page changed)

Notifications fire on state CHANGES only (closed -> open, open -> closed),
so an open site doesn't spam you every 30 minutes. OPEN results are
double-checked with a second pass before notifying.

Run once (cron / GitHub Actions):   python tabi_monitor.py
Run forever locally, every 30 min:  python tabi_monitor.py --loop

Env vars for notifications (optional):
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   and/or   DISCORD_WEBHOOK
Optional overrides:
  MONITOR_USERNAME / MONITOR_PASSWORD  (fixed on purpose - see below)
"""

import argparse
import asyncio
import json
import os
import urllib.request
from datetime import datetime, timezone

from playwright.async_api import async_playwright

SIGNUP_URL = "https://tabitoken.com/sign-up"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".monitor_state")

# Fixed credentials ON PURPOSE: once registration opens, the first check may
# create this account - the credentials are in the notification, so it's yours.
# On later checks "username already exists" still proves registration is open.
MONITOR_USERNAME = os.environ.get("MONITOR_USERNAME", "monitorcheckbot")
MONITOR_PASSWORD = os.environ.get("MONITOR_PASSWORD", "MonitorCheck2026!")  # 8-20 chars required

DISABLED_TEXT = "registration has been disabled"
TURNSTILE_HINTS = ("turnstile", "captcha", "challenge", "verify you are", "verification failed")


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] {msg}", flush=True)


def notify(title, body):
    text = f"{title}\n\n{body}"
    log(f"NOTIFY: {title}")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            payload = json.dumps({"chat_id": chat, "text": text}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload, headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=15).read()
            log("Telegram notification sent.")
        except Exception as e:
            log(f"Telegram notification FAILED: {e}")
    hook = os.environ.get("DISCORD_WEBHOOK")
    if hook:
        try:
            payload = json.dumps({"content": text}).encode()
            req = urllib.request.Request(hook, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
            log("Discord notification sent.")
        except Exception as e:
            log(f"Discord notification FAILED: {e}")
    if not (token and chat) and not hook:
        log("(no notification channel configured - set TELEGRAM_* or DISCORD_WEBHOOK)")


def read_state():
    try:
        with open(STATE_FILE) as f:
            return f.read().strip()
    except Exception:
        return "unknown"


def write_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            f.write(state)
    except Exception:
        pass


async def check_once():
    """One signup attempt. Returns (status, detail)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            context = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = await context.new_page()
            await page.goto(SIGNUP_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_selector("input[name='username']", timeout=30000)

            await page.fill("input[name='username']", MONITOR_USERNAME)
            await page.fill("input[name='password']", MONITOR_PASSWORD)
            await page.fill("input[name='confirmPassword']", MONITOR_PASSWORD)

            # Turnstile auto-solves on most IPs; wait for the token, but submit regardless.
            try:
                await page.wait_for_function(
                    "(() => { const el = document.querySelector('input[name=\"cf-turnstile-response\"]');"
                    " return el && el.value && el.value.length > 10; })()",
                    timeout=20000,
                )
                log("Turnstile token present.")
            except Exception:
                log("No Turnstile token after 20s - submitting anyway.")

            await page.click("button[type='submit']")

            toast = None
            try:
                el = await page.wait_for_selector("li[data-sonner-toast] div[data-title]", timeout=15000)
                toast = ((await el.text_content()) or "").strip()
            except Exception:
                pass
            await asyncio.sleep(2)
            await page.screenshot(path="monitor_last.png")
            url = page.url

            if toast and DISABLED_TEXT in toast.lower():
                return "closed", f"toast: {toast}"
            if toast and any(h in toast.lower() for h in TURNSTILE_HINTS):
                return "unknown", f"turnstile/challenge toast: {toast}"
            if toast:
                return "open", f"unexpected toast (request was processed!): {toast}"
            if "sign-up" not in url:
                return "open", f"redirected to {url} after submit"
            return "unknown", f"no toast appeared; still on {url}"
        finally:
            await browser.close()


async def run_once():
    try:
        status, detail = await check_once()
    except Exception as e:
        status, detail = "unknown", f"check error: {e}"
    log(f"Result: {status.upper()} - {detail}")

    prev = read_state()

    if status == "open":
        log("Open signal - confirming with a second pass in 10s...")
        await asyncio.sleep(10)
        try:
            status2, detail2 = await check_once()
            log(f"Confirmation: {status2.upper()} - {detail2}")
            if status2 != "open":
                status = "unknown"
                detail = f"first pass said open, confirmation said {status2} ({detail2})"
        except Exception as e:
            status, detail = "unknown", f"confirmation error: {e}"

    if status == "open" and prev != "open":
        notify(
            "🟢 TabiToken registration is OPEN!",
            f"{detail}\nChecked at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.\n"
            f"https://tabitoken.com/sign-up\n\n"
            f"If the check created an account, it's yours:\n"
            f"username: {MONITOR_USERNAME}\npassword: {MONITOR_PASSWORD}",
        )
    elif status == "closed" and prev == "open":
        notify("🔴 TabiToken registration closed again.", detail)
    else:
        log(f"No notification (state {prev} -> {status}).")

    write_state(status)
    return status


async def run_loop(interval_minutes):
    while True:
        await run_once()
        log(f"Sleeping {interval_minutes} minutes...")
        await asyncio.sleep(interval_minutes * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TabiToken registration monitor")
    parser.add_argument("--loop", action="store_true", help="run forever")
    parser.add_argument("--interval", type=int, default=30, help="minutes between checks in loop mode")
    args = parser.parse_args()
    if args.loop:
        asyncio.run(run_loop(args.interval))
    else:
        asyncio.run(run_once())

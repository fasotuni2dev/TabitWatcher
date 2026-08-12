#!/usr/bin/env python3
"""TabiToken registration monitor (two-layer).

Layer 1 (fast, no browser): TabiToken runs on the new-api project, which
exposes a public GET /api/status endpoint with a register_enabled flag -
the same flag the frontend reads. If that answers, we're done in seconds.

Layer 2 (fallback): a headless browser reproduces the manual test - fill the
signup form, wait for the Turnstile token (the submit button stays DISABLED
until it arrives), click Create account, read the sonner toast. The exact
"registration has been disabled" text => closed; any other toast or a
redirect => open; Turnstile/network trouble => unknown.

Notifications fire on state CHANGES only (closed <-> open), and OPEN results
are double-checked with a second pass before notifying.

Run once (cron / GitHub Actions):   python tabi_monitor.py
Run forever locally, every 30 min:  python tabi_monitor.py --loop

Env vars for notifications (optional):
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   and/or   DISCORD_WEBHOOK
Optional overrides:
  MONITOR_USERNAME / MONITOR_PASSWORD  (fixed on purpose - see check_via_browser)
"""

import argparse
import asyncio
import json
import os
import urllib.request
from datetime import datetime, timezone

from playwright.async_api import async_playwright

SITE = "https://tabitoken.com"
SIGNUP_URL = f"{SITE}/sign-up"
STATUS_URL = f"{SITE}/api/status"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".monitor_state")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

# Fixed credentials ON PURPOSE: once registration opens, the first browser check
# may create this account - the credentials go into the notification, so it's yours.
MONITOR_USERNAME = os.environ.get("MONITOR_USERNAME", "monitorcheckbot")
MONITOR_PASSWORD = os.environ.get("MONITOR_PASSWORD", "MonitorCheck2026!")  # 8-20 chars required

DISABLED_TEXT = "registration has been disabled"
TURNSTILE_HINTS = ("turnstile", "captcha", "challenge", "verify you are", "verification failed")
REGISTER_KEYS = ("register_enabled", "registerEnabled", "registration_enabled", "RegisterEnabled")


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


# ---------------------------------------------------------------- Layer 1: API
def check_via_api():
    """GET /api/status and read the register_enabled flag.
    Returns (status, detail) or None if the endpoint can't answer."""
    req = urllib.request.Request(STATUS_URL, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        data = json.loads(raw)
    except Exception as e:
        log(f"[api] /api/status unreachable or not JSON: {e}")
        return None

    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        payload = data if isinstance(data, dict) else {}

    for key in REGISTER_KEYS:
        if key in payload:
            enabled = bool(payload[key])
            log(f"[api] /api/status: {key}={payload[key]!r}")
            return ("open" if enabled else "closed"), f"/api/status: {key}={payload[key]!r}"

    log(f"[api] endpoint answered but no register flag found. Payload keys: {list(payload)[:20]}")
    return None


# ------------------------------------------------------------- Layer 2: browser
async def check_via_browser():
    """One real signup attempt in headless Chromium. Returns (status, detail)."""
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
                user_agent=BROWSER_UA,
                locale="en-US",
            )
            page = await context.new_page()
            await page.goto(SIGNUP_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_selector("input[name='username']", timeout=30000)

            await page.fill("input[name='username']", MONITOR_USERNAME)
            await page.fill("input[name='password']", MONITOR_PASSWORD)
            await page.fill("input[name='confirmPassword']", MONITOR_PASSWORD)

            # The submit button stays DISABLED until the Turnstile token exists.
            # Give the widget a real chance (slow on datacenter IPs), then bail
            # cleanly instead of timing out on a dead click.
            try:
                await page.wait_for_function(
                    "(() => { const el = document.querySelector('input[name=\"cf-turnstile-response\"]');"
                    " return el && el.value && el.value.length > 10; })()",
                    timeout=45000,
                )
                log("[browser] Turnstile token present.")
            except Exception:
                return "unknown", "Turnstile never auto-solved on this IP (no token after 45s)"

            try:
                await page.wait_for_selector("button[type='submit']:not([disabled])", timeout=10000)
            except Exception:
                return "unknown", "submit button stayed disabled even with a Turnstile token"

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


async def check_once():
    """Layered check: cheap API first, browser submit as fallback."""
    api_result = await asyncio.to_thread(check_via_api)
    if api_result is not None:
        log("[api] answered - skipping the browser check.")
        return api_result
    log("[api] couldn't answer - falling back to the browser check.")
    return await check_via_browser()


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
            f"{SIGNUP_URL}\n\n"
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

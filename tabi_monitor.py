#!/usr/bin/env python3
"""TabiToken + GoRouter registration monitor.

Fills the signup form on each site and reads the sonner toast that comes back.
As long as the toast says "New user registration has been disabled by
administrator", registration is closed. Any OTHER outcome (a redirect, a
success toast, even "username already exists") means the registration request
was actually processed => registration is OPEN.

Statuses per site:  closed  -> the disabled-by-admin toast appeared
                    open    -> the request was processed / we were redirected
                    unknown -> transient trouble (network, turnstile, page changed)

Notifications fire on state CHANGES only (closed -> open, open -> closed),
per site, so an open site doesn't spam you every 30 minutes. OPEN results are
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

# One probe per site per run, same layout on both. BARE /sign-up URLs on
# purpose: the monitor probes every 30 minutes, and carrying an ?aff= code
# would stuff the referral stats with bot hits.
SITES = [
    {"name": "TabiToken", "slug": "tabitoken", "url": "https://tabitoken.com/sign-up"},
    {"name": "GoRouter", "slug": "gorouter", "url": "https://gorouter.app/sign-up"},
]

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".monitor_state")

# Fixed credentials ON PURPOSE: once registration opens, the first check may
# create this account - the credentials are in the notification, so it's yours.
# On later checks "username already exists" still proves registration is open.
# The same pair is used on both sites; "already exists" means the same thing
# on each.
MONITOR_USERNAME = os.environ.get("MONITOR_USERNAME", "monitorcheckbot")
MONITOR_PASSWORD = os.environ.get("MONITOR_PASSWORD", "MonitorCheck2026!")  # 8-20 chars required

# Optional residential exit for the probes. GitHub Actions runs from datacenter
# IPs and Turnstile often refuses to auto-solve there - "no Turnstile token
# after 20s" on every probe, and on gorouter the missing token also keeps the
# submit button disabled. Set MONITOR_PROXY and the probes come from a real
# address, same as a browser at home.
# Format: http://user:pass@host:port  (a Decodo sticky session is ideal)
def _proxy_cfg():
    # Read at CALL time, not import time - a value injected after this module
    # loaded must still be honored (and it makes the config testable).
    url = os.environ.get("MONITOR_PROXY", "").strip()
    if not url:
        return None
    from urllib.parse import urlsplit
    u = urlsplit(url)
    cfg = {"server": "%s://%s:%s" % (u.scheme, u.hostname, u.port)}
    if u.username:
        cfg["username"] = u.username
        cfg["password"] = u.password or ""
    return cfg

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
    """Per-site last known statuses, e.g. {"tabitoken": "closed", "gorouter":
    "open"}. The file used to hold one bare word (tabitoken only) - read that
    as the tabitoken entry so the first upgraded run doesn't re-notify."""
    try:
        with open(STATE_FILE) as f:
            raw = f.read().strip()
    except Exception:
        return {}
    if raw.startswith("{"):
        try:
            return dict(json.loads(raw))
        except Exception:
            return {}
    # Legacy format: a single bare status word from the tabitoken-only build.
    return {"tabitoken": raw} if raw else {}


def write_state(states):
    try:
        with open(STATE_FILE, "w") as f:
            f.write(json.dumps(states))
    except Exception:
        pass


async def check_site(browser, site):
    """One signup probe against one site. Returns (status, detail).

    A fresh context per site, so the two probes share nothing (cookies, cache,
    storage) - one site's state can never leak into the other's result.
    """
    context = await browser.new_context(
        proxy=_proxy_cfg(),
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    try:
        page = await context.new_page()
        await page.goto(site["url"], timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_selector("input[name='username']", timeout=30000)

        # This stack validates on change/blur and keeps the submit button
        # disabled until it passes. A bare fill() never "touches" a field, so
        # the button never enabled (the 09:16 run: 58 click retries, 30s,
        # UNKNOWN). Type for real (per-key onChange) and Tab out of each field
        # (blur -> touched -> validation fires).
        for sel, value in (
            ("input[name='username']", MONITOR_USERNAME),
            ("input[name='password']", MONITOR_PASSWORD),
            ("input[name='confirmPassword']", MONITOR_PASSWORD),
        ):
            await page.click(sel)
            await page.press_sequentially(sel, value, delay=25)
            await page.press(sel, "Tab")

        # Turnstile auto-solves on most IPs; wait for the token, but submit regardless.
        try:
            await page.wait_for_function(
                "(() => { const el = document.querySelector('input[name=\"cf-turnstile-response\"]');"
                " return el && el.value && el.value.length > 10; })()",
                timeout=20000,
            )
            log(f"{site['name']}: Turnstile token present.")
        except Exception:
            log(f"{site['name']}: no Turnstile token after 20s - submitting anyway.")

        # Wait for the button to ENABLE rather than slamming a disabled one:
        # it unlocks when validation AND the Turnstile token are both in.
        submit_sel = "button[type='submit']"
        try:
            await page.wait_for_function(
                "() => { const b = document.querySelector('button[type=submit]');"
                " return b && !b.disabled && !b.hasAttribute('data-disabled'); }",
                timeout=15000,
            )
        except Exception:
            log(f"{site['name']}: submit still disabled after 15s - forcing it")
        try:
            await page.click(submit_sel, timeout=5000)
        except Exception:
            # The creds meet every rule, so a still-disabled button is a stuck
            # binding, not a real invalid form: strip the attribute and click
            # through JS. The server answers with a toast either way, and the
            # toast is the only thing the classifier reads.
            log(f"{site['name']}: click blocked - removing 'disabled' via JS")
            await page.eval_on_selector(
                submit_sel,
                "b => { b.removeAttribute('disabled'); b.removeAttribute('data-disabled'); b.click(); }",
            )

        toast = None
        try:
            el = await page.wait_for_selector("li[data-sonner-toast] div[data-title]", timeout=15000)
            toast = ((await el.text_content()) or "").strip()
        except Exception:
            pass
        await asyncio.sleep(2)
        await page.screenshot(path=f"monitor_last_{site['slug']}.png")
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
        await context.close()


async def run_once():
    prev_all = read_state()
    new_all = dict(prev_all)
    _pc = _proxy_cfg()
    log("Proxy: " + (_pc["server"] if _pc else "direct (datacenter IP)"))

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
            for site in SITES:
                # One site's crash (page changed, network, anything) is its own
                # "unknown" - never a reason to skip the other site.
                try:
                    status, detail = await check_site(browser, site)
                except Exception as e:
                    status, detail = "unknown", f"check error: {e}"
                log(f"{site['name']}: {status.upper()} - {detail}")

                prev = prev_all.get(site["slug"], "unknown")

                if status == "open":
                    log(f"{site['name']}: open signal - confirming with a second pass in 10s...")
                    await asyncio.sleep(10)
                    try:
                        status2, detail2 = await check_site(browser, site)
                        log(f"{site['name']} confirmation: {status2.upper()} - {detail2}")
                        if status2 != "open":
                            status = "unknown"
                            detail = f"first pass said open, confirmation said {status2} ({detail2})"
                    except Exception as e:
                        status, detail = "unknown", f"confirmation error: {e}"

                if status == "open" and prev != "open":
                    notify(
                        f"🟢 {site['name']} registration is OPEN!",
                        f"{detail}\nChecked at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.\n"
                        f"{site['url']}\n\n"
                        f"If the check created an account, it's yours:\n"
                        f"username: {MONITOR_USERNAME}\npassword: {MONITOR_PASSWORD}",
                    )
                elif status == "closed" and prev == "open":
                    notify(f"🔴 {site['name']} registration closed again.", detail)
                else:
                    log(f"{site['name']}: no notification (state {prev} -> {status}).")

                new_all[site["slug"]] = status
        finally:
            await browser.close()

    write_state(new_all)
    return new_all


async def run_loop(interval_minutes):
    while True:
        await run_once()
        log(f"Sleeping {interval_minutes} minutes...")
        await asyncio.sleep(interval_minutes * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TabiToken + GoRouter registration monitor")
    parser.add_argument("--loop", action="store_true", help="run forever")
    parser.add_argument("--interval", type=int, default=30, help="minutes between checks in loop mode")
    args = parser.parse_args()
    if args.loop:
        asyncio.run(run_loop(args.interval))
    else:
        asyncio.run(run_once())

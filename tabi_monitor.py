#!/usr/bin/env python3
"""TabiToken + GoRouter registration monitor (API edition, hardened).

Checks /api/status on each site for the register_enabled flag. No browser,
no Turnstile, no form submission - just a lightweight HTTP probe.

Hardened against false negatives:
- Cache-busting query param on every request
- Retries once after 3s if result is "closed" (guards against stale cache)
- Logs raw register_enabled value for debugging

Statuses per site:  closed  -> register_enabled is false (confirmed twice)
                    open    -> register_enabled is true
                    unknown -> network error, invalid JSON, missing key

Notifications fire on state CHANGES only (closed -> open, open -> closed),
per site. OPEN results are double-checked with a second pass before notifying.

Run once (cron / GitHub Actions):   python tabi_monitor.py
Run forever locally, every 30 min:  python tabi_monitor.py --loop

Env vars for notifications (optional):
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   and/or   DISCORD_WEBHOOK
"""

import argparse
import asyncio
import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

SITES = [
    {"name": "TabiToken", "slug": "tabitoken", "url": "https://tabitoken.com"},
    {"name": "GoRouter", "slug": "gorouter", "url": "https://gorouter.app"},
]

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".monitor_state")


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
    """Per-site last known statuses, e.g. {"tabitoken": "closed", "gorouter": "open"}.
    Legacy format (bare word) is treated as tabitoken-only for continuity."""
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
    return {"tabitoken": raw} if raw else {}


def write_state(states):
    try:
        with open(STATE_FILE, "w") as f:
            f.write(json.dumps(states))
    except Exception:
        pass


async def check_site(site, retry_on_closed=True):
    """Hit /api/status and classify registration status.
    
    Returns (status, detail) where status is 'open', 'closed', or 'unknown'.
    
    If result is 'closed' and retry_on_closed is True, waits 3s and checks
    again to guard against stale cache or transient false negatives.
    """
    url = f"{site['url']}/api/status"
    
    async def _single_attempt():
        def _fetch():
            # Cache-busting: add timestamp to defeat CDN/browser caching
            cache_buster = int(time.time())
            req = urllib.request.Request(
                f"{url}?_t={cache_buster}",
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; RegistrationMonitor/1.0)",
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        
        try:
            data = await asyncio.to_thread(_fetch)
            # Navigate to data.register_enabled, tolerating missing keys
            enabled = data.get("data", {}).get("register_enabled")
            
            # Log the raw value for debugging (helps catch API changes)
            log(f"{site['name']}: raw register_enabled = {enabled!r}")
            
            if enabled is True:
                return "open", "register_enabled: true"
            elif enabled is False:
                return "closed", "register_enabled: false"
            else:
                return "unknown", f"unexpected register_enabled value: {enabled!r}"
                
        except json.JSONDecodeError as e:
            return "unknown", f"invalid JSON from {url}: {e}"
        except urllib.error.HTTPError as e:
            return "unknown", f"HTTP {e.code} from {url}"
        except Exception as e:
            return "unknown", f"fetch error for {url}: {e}"
    
    # First attempt
    status, detail = await _single_attempt()
    
    # If we got "closed", wait 3s and retry once to rule out stale cache
    # or a transient server hiccup. Only confirm closed if both attempts agree.
    if status == "closed" and retry_on_closed:
        log(f"{site['name']}: got CLOSED, confirming with fresh request in 3s...")
        await asyncio.sleep(3)
        status2, detail2 = await _single_attempt()
        if status2 == "open":
            log(f"{site['name']}: retry says OPEN (first attempt was stale/cached)")
            return "open", f"register_enabled: true (confirmed on retry)"
        elif status2 == "closed":
            return "closed", detail  # Both attempts agree it's closed
        else:
            return "unknown", f"first pass closed, retry inconclusive: {detail2}"
    
    return status, detail


async def run_once():
    prev_all = read_state()
    new_all = dict(prev_all)
    
    log("Checking /api/status endpoints (cache-busted HTTP)")
    
    for site in SITES:
        try:
            status, detail = await check_site(site)
        except Exception as e:
            status, detail = "unknown", f"check error: {e}"
        
        log(f"{site['name']}: {status.upper()} - {detail}")
        prev = prev_all.get(site["slug"], "unknown")
        
        # Double-check open results before notifying (avoid false positives)
        if status == "open":
            log(f"{site['name']}: open signal - confirming with second pass in 5s...")
            await asyncio.sleep(5)
            try:
                status2, detail2 = await check_site(site)
                log(f"{site['name']} confirmation: {status2.upper()} - {detail2}")
                if status2 != "open":
                    status = "unknown"
                    detail = f"first pass open, confirmation said {status2} ({detail2})"
            except Exception as e:
                status, detail = "unknown", f"confirmation error: {e}"
        
        # Notify only on state changes
        if status == "open" and prev != "open":
            notify(
                f"🟢 {site['name']} registration is OPEN!",
                f"{detail}\nChecked at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.\n"
                f"{site['url']}/sign-up\n\n"
                f"Registration has been enabled via /api/status."
            )
        elif status == "closed" and prev == "open":
            notify(f"🔴 {site['name']} registration closed.", detail)
        else:
            log(f"{site['name']}: no notification (state {prev} -> {status}).")
        
        new_all[site["slug"]] = status
    
    write_state(new_all)
    return new_all


async def run_loop(interval_minutes):
    while True:
        await run_once()
        log(f"Sleeping {interval_minutes} minutes...")
        await asyncio.sleep(interval_minutes * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TabiToken + GoRouter registration monitor (API edition)")
    parser.add_argument("--loop", action="store_true", help="run forever")
    parser.add_argument("--interval", type=int, default=30, help="minutes between checks in loop mode")
    args = parser.parse_args()
    if args.loop:
        asyncio.run(run_loop(args.interval))
    else:
        asyncio.run(run_once())

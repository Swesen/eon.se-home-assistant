"""
get_tokens.py – Obtain E.ON tokens and optionally push them to Home Assistant.

Run this script on a machine that has Playwright installed (your PC, not HA).

Usage:
    # Print tokens (paste into HA config flow manually):
    python get_tokens.py

    # Authenticate AND push tokens directly to HA (no copy-pasting needed):
    python get_tokens.py --push https://homeassistant.swesen.net --token YOUR_HA_TOKEN

How to get a HA long-lived access token:
    HA → Profile (bottom left) → Long-Lived Access Tokens → Create Token

Requirements:
    pip install aiohttp playwright
    playwright install chromium
"""
from __future__ import annotations
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import argparse
import asyncio
import getpass
import importlib.util
import logging
import os
import types

logging.basicConfig(level=logging.WARNING)

import aiohttp

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

BASE = "custom_components/eon_se"
for pkg in ("custom_components", "custom_components.eon_se"):
    if pkg not in sys.modules:
        sys.modules[pkg] = types.ModuleType(pkg)

_load("custom_components.eon_se.const", f"{BASE}/const.py")
apimod = _load("custom_components.eon_se.api", f"{BASE}/api.py")

def _load_dotenv(path=".env"):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass

async def push_to_ha(ha_url: str, ha_token: str, access_token: str, refresh_token: str) -> None:
    """Call the eon_se.push_tokens service on Home Assistant."""
    url = f"{ha_url.rstrip('/')}/api/services/eon_se/push_tokens"
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "access_token": access_token,
        "refresh_token": refresh_token,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers, ssl=False) as resp:
            if resp.status in (200, 201):
                print("✓ Tokens pushed to Home Assistant successfully!")
            else:
                body = await resp.text()
                print(f"✗ Push failed (HTTP {resp.status}): {body[:300]}")
                sys.exit(1)

async def main():
    parser = argparse.ArgumentParser(description="Get E.ON tokens and optionally push to HA")
    parser.add_argument("--push", metavar="HA_URL",
                        help="Push tokens to HA at this URL, e.g. https://homeassistant.swesen.net")
    parser.add_argument("--token", metavar="HA_TOKEN",
                        help="HA long-lived access token (or set HA_TOKEN env var)")
    args = parser.parse_args()

    _load_dotenv()

    personnummer = os.environ.get("EON_PERSONNUMMER") or input("Personnummer (YYYYMMDDXXXX): ").strip()
    password     = os.environ.get("EON_PASSWORD")     or getpass.getpass("Password: ")
    ha_token     = args.token or os.environ.get("HA_TOKEN", "")

    if args.push and not ha_token:
        ha_token = getpass.getpass("HA long-lived access token: ")

    print("\nAuthenticating with E.ON (opens a headless browser — ~10 seconds)…\n")

    async with aiohttp.ClientSession() as session:
        client = apimod.EonApiClient(personnummer, password, session)
        try:
            await client.authenticate()
        except Exception as e:
            print(f"\n✗ Authentication failed: {e}")
            sys.exit(1)

    print("✓ Authenticated!\n")

    if args.push:
        print(f"Pushing tokens to {args.push} …")
        await push_to_ha(args.push, ha_token, client._bearer_token, client._refresh_token)
    else:
        print("=" * 60)
        print("Paste these into the Home Assistant config flow:")
        print("=" * 60)
        print(f"\nAccess token:\n{client._bearer_token}\n")
        print(f"Refresh token:\n{client._refresh_token}\n")
        print("=" * 60)
        print("\nTip: next time run with --push to skip copy-pasting:")
        print(f"  python get_tokens.py --push https://YOUR-HA-URL --token YOUR_HA_TOKEN")

if __name__ == "__main__":
    asyncio.run(main())


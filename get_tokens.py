"""
get_tokens.py – Obtain E.ON access + refresh tokens on your PC.

Run this script on a machine that has Playwright installed (your PC, not HA).
It will authenticate with E.ON and print the tokens for you to paste into
the Home Assistant config flow ("Paste tokens" option).

Usage:
    python get_tokens.py

Requirements:
    pip install aiohttp playwright
    playwright install chromium
"""
from __future__ import annotations
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

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

async def main():
    _load_dotenv()

    personnummer = os.environ.get("EON_PERSONNUMMER") or input("Personnummer (YYYYMMDDXXXX): ").strip()
    password     = os.environ.get("EON_PASSWORD")     or getpass.getpass("Password: ")

    print("\nAuthenticating with E.ON (this opens a headless browser — takes ~10 seconds)…\n")

    async with aiohttp.ClientSession() as session:
        client = apimod.EonApiClient(personnummer, password, session)
        try:
            await client.authenticate()
        except Exception as e:
            print(f"\n✗ Authentication failed: {e}")
            sys.exit(1)

    print("✓ Authentication successful!\n")
    print("=" * 60)
    print("Paste these values into the Home Assistant config flow:")
    print("=" * 60)
    print(f"\nAccess token:\n{client._bearer_token}\n")
    print(f"Refresh token:\n{client._refresh_token}\n")
    print("=" * 60)
    print("\nNote: The access token expires in ~15 minutes, but Home Assistant")
    print("will use the refresh token to renew it automatically — no browser needed.")

if __name__ == "__main__":
    asyncio.run(main())

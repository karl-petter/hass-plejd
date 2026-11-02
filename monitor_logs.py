#!/usr/bin/env python3
"""Polls a live Home Assistant instance's log over its WebSocket API and
prints new plejd/pyplejd entries as they appear - for watching, in real
time, for the keep-alive-ping/zombie-connection signature this repo is
gathering evidence for, rather than checking Settings -> System -> Logs
by hand.

Shares credentials with ha-lksystems (same Home Assistant instance): see
../ha-lksystems/.env / .env.example for HA_SSH_HOST and
HA_LONG_LIVED_TOKEN.

Usage: ./monitor_logs.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp.resolver import ThreadedResolver

POLL_SECONDS = 5
MATCH_TERMS = ("plejd",)


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if value:
            os.environ.setdefault(key.strip(), value.strip())


def _websocket_url() -> str:
    ha_url = os.environ.get("HA_URL")
    if ha_url:
        scheme = "wss" if ha_url.startswith("https") else "ws"
        netloc = ha_url.split("://", 1)[-1].rstrip("/")
        return f"{scheme}://{netloc}/api/websocket"
    host = os.environ["HA_SSH_HOST"]
    return f"wss://{host}:8123/api/websocket"


def _entry_key(entry: dict) -> tuple:
    return (entry.get("timestamp"), entry.get("name"), tuple(entry.get("message", [])))


def _matches(entry: dict) -> bool:
    haystack = " ".join([entry.get("name", ""), *entry.get("message", [])]).lower()
    return any(term in haystack for term in MATCH_TERMS)


async def _authenticated_connection(
    session: aiohttp.ClientSession, url: str, token: str
) -> aiohttp.ClientWebSocketResponse:
    ws = await session.ws_connect(url)
    await ws.receive_json()  # auth_required
    await ws.send_json({"type": "auth", "access_token": token})
    result = await ws.receive_json()
    if result.get("type") != "auth_ok":
        raise RuntimeError(f"Authentication failed: {result}")
    return ws


async def _run_session(
    session: aiohttp.ClientSession, url: str, token: str, seen: set[tuple], first_pass: bool
) -> bool:
    """Runs one authenticated WebSocket session until it drops.

    Returns whether this was still the very first session since startup
    (so the caller knows whether to keep suppressing the initial log dump
    on the next reconnect).
    """
    ws = await _authenticated_connection(session, url, token)
    print(f"Connected to {url}, watching for plejd log entries every {POLL_SECONDS}s...")
    msg_id = 1
    while True:
        await ws.send_json({"id": msg_id, "type": "system_log/list"})
        msg_id += 1
        response = await ws.receive_json()
        entries = response.get("result", []) if response.get("success") else []
        new_entries = [e for e in entries if _entry_key(e) not in seen]
        for entry in new_entries:
            seen.add(_entry_key(entry))
        if not first_pass:
            for entry in sorted(new_entries, key=lambda e: e.get("timestamp", 0)):
                if _matches(entry):
                    ts = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(entry.get("timestamp", 0))
                    )
                    print(f"[{ts}] {entry.get('level')} {entry.get('name')}: "
                          f"{' '.join(entry.get('message', []))}")
        first_pass = False
        await asyncio.sleep(POLL_SECONDS)


async def _poll(url: str, token: str) -> None:
    """Reconnects with backoff forever - a dropped WebSocket (sleep/wake,
    router hiccup, HA restart) must not end the monitor, since catching a
    rare bug depends on it staying up for days unattended.
    """
    connector = aiohttp.TCPConnector(resolver=ThreadedResolver(), ssl=False)
    seen: set[tuple] = set()
    first_pass = True
    backoff = 5
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                await _run_session(session, url, token, seen, first_pass)
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                ConnectionError,
                TypeError,
                RuntimeError,
            ) as e:
                first_pass = False
                print(f"Connection lost ({e!r}), reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 5


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    repo_root = Path(__file__).resolve().parent
    _load_env(repo_root.parent / "ha-lksystems" / ".env")
    token = os.environ.get("HA_LONG_LIVED_TOKEN")
    if not token:
        print("HA_LONG_LIVED_TOKEN not set (checked ../ha-lksystems/.env)", file=sys.stderr)
        sys.exit(1)
    url = _websocket_url()
    try:
        asyncio.run(_poll(url, token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

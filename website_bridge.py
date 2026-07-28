#!/usr/bin/env python3
"""
Website bridge for Surprise Attack.

Runs alongside the Discord bot (Render). Uses REST only (no gateway), so it
does NOT fight the bot token connection.

Every few seconds:
  1. Read the SA channel
  2. Find the live/scheduled leaderboard message the bot posts
  3. Upsert event + scores into Supabase for the Vercel site

Env (same .env as the bot):
  DISCORD_TOKEN
  SA_CHANNEL_ID
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY

Run:
  python website_bridge.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
CHANNEL_ID = (os.getenv("SA_CHANNEL_ID") or "").strip()
SUPABASE_URL = (
    os.getenv("SUPABASE_URL")
    or os.getenv("SA_SUPABASE_URL")
    or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
    or ""
).strip().strip('"').strip("'").rstrip("/")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SA_SUPABASE_SERVICE_KEY")
    or os.getenv("SUPABASE_SERVICE_KEY")
    or ""
).strip().strip('"').strip("'")

BOARD_MARKER = "⚡ Surprise Attack Leaderboard"
POLL_SEC = float(os.getenv("SA_BRIDGE_POLL_SEC") or "8")
MODES = ("arcade", "classic", "fusion")

# 🥇 **Name** — `1,234,567` · Hardcore
SCORE_LINE = re.compile(
    r"\*\*(.+?)\*\*\s*[—-]\s*`([0-9,]+)`",
    re.UNICODE,
)
EVENT_ID_RE = re.compile(r"\*\*Event:\*\*\s*`([^`]+)`")
SONG_RE = re.compile(r"\*\*Song:\*\*\s*(.+)")
STATUS_RE = re.compile(r"\*\*Status:\*\*\s*(\w+)", re.I)
DIFF_RE = re.compile(r"\*\*Difficulty:\*\*\s*(.+)")


def _log(msg: str) -> None:
    print(f"[bridge {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def discord_get(path: str):
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        headers={
            "Authorization": f"Bot {TOKEN}",
            "User-Agent": "SA-WebsiteBridge/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def supabase_request(method: str, path: str, body=None, params: str = ""):
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}{params}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def strip_md(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\*+", "", s)
    s = re.sub(r"_+", "", s)
    return s.strip()


def parse_song(song_field: str) -> tuple[str | None, str | None]:
    raw = strip_md(song_field)
    if not raw or raw.lower().startswith("any song"):
        return None, None
    if " — " in raw:
        a, b = raw.split(" — ", 1)
        return a.strip() or None, b.strip() or None
    if " - " in raw:
        a, b = raw.split(" - ", 1)
        return a.strip() or None, b.strip() or None
    return raw or None, None


def parse_board_message(msg: dict) -> dict | None:
    embeds = msg.get("embeds") or []
    if not embeds:
        return None
    header = embeds[0]
    title = header.get("title") or ""
    if BOARD_MARKER not in title and "Surprise Attack Leaderboard" not in title:
        return None

    desc = header.get("description") or ""
    status_m = STATUS_RE.search(desc)
    status_raw = (status_m.group(1) if status_m else "").upper()
    if status_raw == "LIVE":
        status = "live"
    elif status_raw == "SCHEDULED":
        status = "scheduled"
    else:
        status = "closed"

    eid_m = EVENT_ID_RE.search(desc)
    event_id = eid_m.group(1).strip() if eid_m else None
    if not event_id or event_id == "—":
        event_id = f"sa-bridge-{msg.get('id')}"

    song_title = song_artist = None
    song_m = SONG_RE.search(desc)
    if song_m:
        song_title, song_artist = parse_song(song_m.group(1))

    event_diff = None
    diff_m = DIFF_RE.search(desc)
    if diff_m:
        d = strip_md(diff_m.group(1)).lower()
        if "any" not in d:
            for known in ("hardcore", "extreme", "hard", "normal", "easy"):
                if known in d:
                    event_diff = known
                    break

    scores: list[dict] = []
    for emb in embeds[1:]:
        mode_title = (emb.get("title") or "").strip().lower()
        mode = None
        for m in MODES:
            if m in mode_title:
                mode = m
                break
        if not mode:
            continue
        block = emb.get("description") or ""
        if "No scores yet" in block:
            continue
        for line in block.splitlines():
            sm = SCORE_LINE.search(line)
            if not sm:
                continue
            player = sm.group(1).strip()
            score = int(sm.group(2).replace(",", ""))
            player_key = re.sub(r"\s+", " ", player.lower()).strip() or "unknown"
            scores.append(
                {
                    "event_id": event_id,
                    "mode": mode,
                    "player_key": player_key,
                    "player_name": player,
                    "player_hash": None,
                    "score": score,
                    "difficulty": event_diff,
                    "title": song_title,
                    "artist": song_artist,
                    "discord_user_id": None,
                    "max_combo": 0,
                    "accuracy": 0,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )

    return {
        "event_id": event_id,
        "status": status,
        "song_title": song_title,
        "song_artist": song_artist,
        "event_difficulty": event_diff,
        "scores": scores,
        "msg_id": msg.get("id"),
    }


def find_latest_board(messages: list[dict]) -> dict | None:
    for msg in messages:
        parsed = parse_board_message(msg)
        if parsed:
            return parsed
    return None


def upsert_event(parsed: dict) -> tuple[bool, str]:
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "event_id": parsed["event_id"],
        "song_title": parsed.get("song_title"),
        "song_artist": parsed.get("song_artist"),
        "event_difficulty": parsed.get("event_difficulty"),
        "status": parsed["status"],
        "started_at": now if parsed["status"] == "live" else None,
        "ended_at": now if parsed["status"] == "closed" else None,
        "updated_at": now,
    }
    # Don't clobber started_at with null on later polls for live events —
    # only set started_at on first live; use merge carefully.
    if parsed["status"] == "live":
        row.pop("ended_at", None)
        # Keep started_at only if we want update — better omit ended_at
    st, body = supabase_request(
        "POST",
        "sa_events",
        body=row,
        params="?on_conflict=event_id",
    )
    if st >= 400:
        return False, f"HTTP {st}: {body[:300]}"
    return True, f"event {parsed['event_id']} status={parsed['status']}"


def upsert_scores(scores: list[dict]) -> tuple[int, str]:
    ok_n = 0
    err = ""
    for row in scores:
        st, body = supabase_request(
            "POST",
            "sa_scores",
            body=row,
            params="?on_conflict=event_id,mode,player_key",
        )
        if st < 400:
            ok_n += 1
        else:
            err = f"HTTP {st}: {body[:200]}"
        time.sleep(0.05)
    return ok_n, err or "ok"


def sync_once() -> str:
    # Board can sit further back if the channel is chatty — pull enough history.
    msgs = discord_get(f"/channels/{CHANNEL_ID}/messages?limit=50")
    parsed = find_latest_board(msgs)
    if not parsed:
        return "no board message in last 50 channel msgs"
    e_ok, e_detail = upsert_event(parsed)
    if not e_ok:
        return f"EVENT FAIL {e_detail}"
    n, s_detail = upsert_scores(parsed.get("scores") or [])
    return (
        f"OK {e_detail} · {n} score(s) · song={parsed.get('song_title')!r} "
        f"· msg={parsed.get('msg_id')}"
        + (f" · score_err={s_detail}" if n == 0 and s_detail != "ok" and parsed.get("scores") else "")
    )


def main() -> int:
    if not TOKEN or not CHANNEL_ID:
        print("ERROR: DISCORD_TOKEN and SA_CHANNEL_ID required")
        return 1
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY required")
        return 1

    me = discord_get("/users/@me")
    _log(f"online as {me.get('username')} (REST only, no gateway)")
    _log(f"channel={CHANNEL_ID}")
    _log(f"supabase={SUPABASE_URL.replace('https://','')}")
    _log(f"poll every {POLL_SEC}s — Ctrl+C to stop")

    last = ""
    while True:
        try:
            detail = sync_once()
            if detail != last:
                _log(detail)
                last = detail
        except Exception as e:
            _log(f"error: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nbridge stopped")
        sys.exit(0)

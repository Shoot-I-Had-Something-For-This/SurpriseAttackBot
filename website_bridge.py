#!/usr/bin/env python3
"""
Website bridge for Surprise Attack.

Runs alongside the Discord bot (Render). Uses REST only (no gateway), so it
does NOT fight the bot token connection.

Every few seconds:
  1. Read the SA channel
  2. Find ALL leaderboard messages the bot posts
  3. Prefer a true LIVE board (never an older zombie or empty CLOSED shell)
  4. Upsert every unique event + its scores into Supabase for the Vercel site

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
    r"\*\*(.+?)\*\*\s*[—–-]\s*`([0-9,]+)`",
    re.UNICODE,
)
EVENT_ID_RE = re.compile(r"\*\*Event:\*\*\s*`([^`]+)`")
SONG_RE = re.compile(r"\*\*Song:\*\*\s*(.+)")
STATUS_RE = re.compile(r"\*\*Status:\*\*\s*(\w+)", re.I)
DIFF_RE = re.compile(r"\*\*Difficulty:\*\*\s*(.+)")
ROW_DIFF_RE = re.compile(r"·\s*([A-Za-z]+)\s*$")


def _log(msg: str) -> None:
    print(f"[bridge {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def discord_get(path: str):
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        headers={
            "Authorization": f"Bot {TOKEN}",
            "User-Agent": "SA-WebsiteBridge/1.1",
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
    for sep in (" — ", " – ", " - "):
        if sep in raw:
            a, b = raw.split(sep, 1)
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
            row_diff = event_diff
            dm = ROW_DIFF_RE.search(line)
            if dm:
                cand = dm.group(1).lower()
                if cand in ("easy", "normal", "hard", "extreme", "hardcore"):
                    row_diff = cand
            scores.append(
                {
                    "event_id": event_id,
                    "mode": mode,
                    "player_key": player_key,
                    "player_name": player,
                    "player_hash": None,
                    "score": score,
                    "difficulty": row_diff,
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
        "msg_id_int": int(msg.get("id") or 0),
    }


def parse_all_boards(messages: list[dict]) -> list[dict]:
    boards: list[dict] = []
    for msg in messages:
        parsed = parse_board_message(msg)
        if parsed:
            boards.append(parsed)
    boards.sort(key=lambda b: b["msg_id_int"], reverse=True)
    return boards


def normalize_zombie_lives(boards: list[dict]) -> list[dict]:
    """
    Only the newest board message may stay LIVE/SCHEDULED.
    Older LIVE embeds left behind by bot crashes are treated as closed so the
    website never shows a stale attack as current.
    """
    if not boards:
        return boards
    newest = boards[0]["msg_id_int"]
    out: list[dict] = []
    for b in boards:
        b = dict(b)
        if b["msg_id_int"] < newest and b["status"] in ("live", "scheduled"):
            b["status"] = "closed"
        out.append(b)
    return out


def dedupe_by_event(boards: list[dict]) -> list[dict]:
    """One row per event_id — prefer more scores, then newer message."""
    best: dict[str, dict] = {}
    for b in boards:
        eid = b["event_id"]
        prev = best.get(eid)
        if not prev:
            best[eid] = b
            continue
        n_new = len(b.get("scores") or [])
        n_old = len(prev.get("scores") or [])
        if n_new > n_old:
            best[eid] = b
        elif n_new < n_old:
            continue
        elif b["msg_id_int"] >= prev["msg_id_int"]:
            # Prefer live/scheduled status from the newer message when scores tie
            best[eid] = b
    return list(best.values())


def pick_primary(boards: list[dict]) -> dict | None:
    """What the site should treat as 'current' (for log line only)."""
    if not boards:
        return None
    for b in boards:
        if b["status"] == "live":
            return b
    for b in boards:
        if b["status"] == "scheduled":
            return b
    # Prefer closed board that actually has scores over empty shells
    with_scores = [b for b in boards if b.get("scores")]
    if with_scores:
        return max(with_scores, key=lambda b: b["msg_id_int"])
    return boards[0]


def started_at_from_event_id(event_id: str | None) -> str | None:
    if not event_id:
        return None
    m = re.match(r"sa-(\d{8})-(\d{6})$", str(event_id).strip())
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
        return dt.isoformat()
    except ValueError:
        return None


def upsert_event(parsed: dict) -> tuple[bool, str]:
    now = datetime.now(timezone.utc).isoformat()
    started = started_at_from_event_id(parsed.get("event_id")) or now
    row = {
        "event_id": parsed["event_id"],
        "song_title": parsed.get("song_title"),
        "song_artist": parsed.get("song_artist"),
        "event_difficulty": parsed.get("event_difficulty"),
        "status": parsed["status"],
        "started_at": started,
        "updated_at": now,
    }
    # Keep timestamps honest; never null out started_at on later polls.
    if parsed["status"] == "live":
        row["ended_at"] = None
    elif parsed["status"] == "closed":
        row["ended_at"] = now
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
    msgs = discord_get(f"/channels/{CHANNEL_ID}/messages?limit=100")
    boards = normalize_zombie_lives(parse_all_boards(msgs))
    if not boards:
        return "no board message in last 100 channel msgs"

    unique = dedupe_by_event(boards)
    primary = pick_primary(unique)

    total_scores = 0
    event_bits: list[str] = []
    errors: list[str] = []

    # Upsert oldest → newest so the final write of overlapping fields is newest
    for parsed in sorted(unique, key=lambda b: b["msg_id_int"]):
        e_ok, e_detail = upsert_event(parsed)
        if not e_ok:
            errors.append(f"EVENT FAIL {e_detail}")
            continue
        n, s_detail = upsert_scores(parsed.get("scores") or [])
        total_scores += n
        event_bits.append(
            f"{parsed['event_id']}[{parsed['status']}/{n}sc]"
        )
        if n == 0 and s_detail != "ok" and parsed.get("scores"):
            errors.append(f"score_err {parsed['event_id']}: {s_detail}")

    primary_bit = ""
    if primary:
        primary_bit = (
            f" primary={primary['event_id']} song={primary.get('song_title')!r}"
        )

    msg = (
        f"OK {len(unique)} event(s) · {total_scores} score row(s) · "
        + ", ".join(event_bits)
        + primary_bit
    )
    if errors:
        msg += " · " + "; ".join(errors[:2])
    return msg


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

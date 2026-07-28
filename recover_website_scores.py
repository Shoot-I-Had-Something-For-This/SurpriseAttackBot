#!/usr/bin/env python3
"""
Recover Surprise Attack website data from Discord boards + local history.

Fixes common damage from broken sync / half-finished fix scripts:
  1. Deletes sa-fix-* junk events from Supabase
  2. Parses every leaderboard message in the SA channel
  3. Closes zombie LIVE boards when a newer board exists after them
  4. Upserts real events + scores so the Vercel site matches Discord

Usage:
  python recover_website_scores.py
  python recover_website_scores.py --close-zombie-boards
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
CHANNEL_ID = (os.getenv("SA_CHANNEL_ID") or "").strip()
SUPABASE_URL = (
    os.getenv("SUPABASE_URL")
    or os.getenv("SA_SUPABASE_URL")
    or ""
).strip().strip('"').strip("'").rstrip("/")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SA_SUPABASE_SERVICE_KEY")
    or ""
).strip().strip('"').strip("'")

BOT_DIR = Path(__file__).resolve().parent
HISTORY_DIR = Path(os.getenv("SA_HISTORY_DIR") or BOT_DIR / "history")

BOARD_MARKER = "Surprise Attack Leaderboard"
MODES = ("arcade", "classic", "fusion")
SCORE_LINE = re.compile(
    r"\*\*(.+?)\*\*\s*[—–-]\s*`([0-9,]+)`",
    re.UNICODE,
)
EVENT_ID_RE = re.compile(r"\*\*Event:\*\*\s*`([^`]+)`")
SONG_RE = re.compile(r"\*\*Song:\*\*\s*(.+)")
STATUS_RE = re.compile(r"\*\*Status:\*\*\s*(\w+)", re.I)
DIFF_RE = re.compile(r"\*\*Difficulty:\*\*\s*(.+)")
ROW_DIFF_RE = re.compile(r"·\s*([A-Za-z]+)\s*$")


def log(msg: str) -> None:
    print(f"[recover] {msg}", flush=True)


def discord_get(path: str):
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        headers={
            "Authorization": f"Bot {TOKEN}",
            "User-Agent": "SA-Recover/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def discord_patch(path: str, body: dict):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        data=data,
        method="PATCH",
        headers={
            "Authorization": f"Bot {TOKEN}",
            "User-Agent": "SA-Recover/1.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def supabase_request(
    method: str,
    path: str,
    body=None,
    params: str = "",
    prefer: str = "resolution=merge-duplicates,return=minimal",
):
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}{params}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
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
    if BOARD_MARKER not in title and "Leaderboard" not in title:
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

    # Discord snowflake → rough order (higher = newer message)
    msg_id = int(msg.get("id") or 0)
    return {
        "event_id": event_id,
        "status": status,
        "song_title": song_title,
        "song_artist": song_artist,
        "event_difficulty": event_diff,
        "scores": scores,
        "msg_id": str(msg.get("id")),
        "msg_id_int": msg_id,
        "raw_embeds": embeds,
    }


def fetch_all_boards(limit: int = 100) -> list[dict]:
    msgs = discord_get(f"/channels/{CHANNEL_ID}/messages?limit={limit}")
    boards: list[dict] = []
    for msg in msgs:
        parsed = parse_board_message(msg)
        if parsed:
            boards.append(parsed)
    # Newest first (Discord already returns newest-first, keep that)
    boards.sort(key=lambda b: b["msg_id_int"], reverse=True)
    return boards


def close_discord_board(board: dict) -> bool:
    """Patch a LIVE/SCHEDULED board message to CLOSED on Discord."""
    embeds = board.get("raw_embeds")
    if not embeds:
        return False
    header = dict(embeds[0])
    desc = header.get("description") or ""
    desc = desc.replace("**Status:** LIVE", "**Status:** CLOSED")
    desc = desc.replace("**Status:** SCHEDULED", "**Status:** CLOSED")
    header["description"] = desc
    header["color"] = 0x64748B
    new_embeds = [header] + list(embeds[1:])
    st, body = discord_patch(
        f"/channels/{CHANNEL_ID}/messages/{board['msg_id']}",
        {"embeds": new_embeds},
    )
    if st >= 400:
        log(f"  close board fail HTTP {st}: {body[:200]}")
        return False
    log(f"  closed Discord board msg={board['msg_id']} event={board['event_id']}")
    return True


def delete_fake_events() -> int:
    st, body = supabase_request(
        "GET",
        "sa_events",
        params="?select=event_id&event_id=like.sa-fix-*",
        prefer="return=representation",
    )
    if st >= 400:
        log(f"list fake events fail HTTP {st}: {body[:200]}")
        return 0
    try:
        rows = json.loads(body or "[]")
    except json.JSONDecodeError:
        rows = []
    n = 0
    for row in rows:
        eid = row.get("event_id")
        if not eid:
            continue
        # Cascade deletes scores via FK
        dst, dbody = supabase_request(
            "DELETE",
            "sa_events",
            params=f"?event_id=eq.{urllib.request.quote(eid, safe='')}",
            prefer="return=minimal",
        )
        if dst < 400:
            log(f"  deleted fake event {eid}")
            n += 1
        else:
            log(f"  delete {eid} fail HTTP {dst}: {dbody[:200]}")
    return n


def started_at_from_event_id(event_id: str | None) -> str | None:
    """Parse sa-YYYYMMDD-HHMMSS → ISO utc (best-effort)."""
    if not event_id:
        return None
    m = re.match(r"sa-(\d{8})-(\d{6})$", event_id.strip())
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
    return True, f"{parsed['event_id']} status={parsed['status']} scores={len(parsed.get('scores') or [])}"


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
        time.sleep(0.04)
    return ok_n, err or "ok"


def seed_history_files() -> int:
    if not HISTORY_DIR.is_dir():
        return 0
    n = 0
    for path in sorted(HISTORY_DIR.glob("sa-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        eid = data.get("event_id")
        if not eid:
            continue
        song_title = data.get("song_title")
        song_artist = data.get("song_artist")
        now = datetime.now(timezone.utc).isoformat()
        event_row = {
            "event_id": eid,
            "song_title": song_title,
            "song_artist": song_artist,
            "event_difficulty": data.get("event_difficulty"),
            "status": "closed",
            "started_at": _unix_to_iso(data.get("started_at")),
            "ended_at": _unix_to_iso(data.get("ended_at")) or now,
            "updated_at": now,
        }
        st, body = supabase_request(
            "POST",
            "sa_events",
            body=event_row,
            params="?on_conflict=event_id",
        )
        if st >= 400:
            log(f"  history event {eid} fail: {body[:200]}")
            continue
        score_rows = []
        scores = data.get("scores") or {}
        for mode in MODES:
            bucket = scores.get(mode) or {}
            if not isinstance(bucket, dict):
                continue
            for player_key, row in bucket.items():
                if not isinstance(row, dict):
                    continue
                score_rows.append(
                    {
                        "event_id": eid,
                        "mode": mode,
                        "player_key": player_key,
                        "player_name": row.get("player_name") or "Unknown",
                        "player_hash": row.get("player_hash"),
                        "score": int(row.get("score") or 0),
                        "difficulty": row.get("difficulty"),
                        "title": row.get("title") or song_title,
                        "artist": row.get("artist") or song_artist,
                        "discord_user_id": (
                            str(row["discord_user_id"])
                            if row.get("discord_user_id") is not None
                            else None
                        ),
                        "max_combo": int(row.get("max_combo") or 0),
                        "accuracy": float(row.get("accuracy") or 0),
                        "updated_at": _unix_to_iso(row.get("updated_at")) or now,
                    }
                )
        ok_n, err = upsert_scores(score_rows)
        log(f"  history {path.name}: event ok, {ok_n} score(s)" + (f" err={err}" if err != "ok" else ""))
        n += 1
    return n


def _unix_to_iso(ts) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def dedupe_boards(boards: list[dict]) -> list[dict]:
    """Keep best board per event_id (most scores, then newest message)."""
    best: dict[str, dict] = {}
    for b in boards:
        eid = b["event_id"]
        prev = best.get(eid)
        if not prev:
            best[eid] = b
            continue
        # Prefer more scores
        if len(b.get("scores") or []) > len(prev.get("scores") or []):
            best[eid] = b
            continue
        if len(b.get("scores") or []) < len(prev.get("scores") or []):
            continue
        # Same score count: prefer closed truth from newer closed msg,
        # else prefer live, else newer msg
        if b["msg_id_int"] > prev["msg_id_int"]:
            # Newer message: if either is live keep live status from newer if live
            if b["status"] == "live" or prev["status"] != "live":
                best[eid] = b
    return list(best.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--close-zombie-boards",
        action="store_true",
        help="Patch Discord LIVE boards that are not the newest board to CLOSED",
    )
    ap.add_argument(
        "--no-history",
        action="store_true",
        help="Skip local history/ seed",
    )
    args = ap.parse_args()

    if not TOKEN or not CHANNEL_ID:
        print("ERROR: DISCORD_TOKEN and SA_CHANNEL_ID required")
        return 1
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY required")
        return 1

    me = discord_get("/users/@me")
    log(f"bot={me.get('username')} channel={CHANNEL_ID}")
    log(f"supabase={SUPABASE_URL.replace('https://', '')}")

    log("Removing fake sa-fix-* events…")
    deleted = delete_fake_events()
    log(f"  removed {deleted} fake event(s)")

    log("Reading Discord leaderboard messages…")
    boards = fetch_all_boards(100)
    log(f"  found {len(boards)} board message(s)")
    for b in boards:
        log(
            f"  · {b['event_id']} status={b['status']} "
            f"song={b.get('song_title')!r} scores={len(b.get('scores') or [])} "
            f"msg={b['msg_id']}"
        )

    if not boards:
        log("No boards in channel — seeding history only")
    else:
        newest_id = boards[0]["msg_id_int"]
        # Zombie LIVE: LIVE board that is NOT the newest board message
        zombies = [
            b for b in boards
            if b["status"] == "live" and b["msg_id_int"] < newest_id
        ]
        if zombies:
            log(f"Found {len(zombies)} zombie LIVE board(s) (older than newest board)")
            for z in zombies:
                if args.close_zombie_boards:
                    if close_discord_board(z):
                        z["status"] = "closed"
                else:
                    log(
                        f"  treating {z['event_id']} as closed for website "
                        f"(pass --close-zombie-boards to patch Discord too)"
                    )
                    z["status"] = "closed"

        # If newest board is CLOSED and nothing is truly live, force no live leftovers
        any_true_live = any(
            b["status"] == "live" and b["msg_id_int"] == newest_id for b in boards
        )
        if not any_true_live:
            for b in boards:
                if b["status"] == "live":
                    b["status"] = "closed"

        unique = dedupe_boards(boards)
        log(f"Upserting {len(unique)} unique event(s)…")
        for b in sorted(unique, key=lambda x: x["msg_id_int"]):
            ok, detail = upsert_event(b)
            if not ok:
                log(f"  EVENT FAIL {detail}")
                continue
            n, sdetail = upsert_scores(b.get("scores") or [])
            log(f"  OK {detail} · upserted {n} score row(s)" + (f" ({sdetail})" if sdetail != "ok" else ""))

    if not args.no_history:
        log("Seeding local history/ files…")
        seed_history_files()

    # Show website truth
    st, body = supabase_request(
        "GET",
        "sa_events",
        params="?select=event_id,status,song_title&order=updated_at.desc&limit=12",
        prefer="return=representation",
    )
    log(f"Supabase events HTTP {st}:")
    print(body[:1200] if body else "(empty)")

    st, body = supabase_request(
        "GET",
        "sa_scores",
        params="?select=event_id,mode,player_name,score&order=score.desc&limit=20",
        prefer="return=representation",
    )
    log(f"Supabase scores HTTP {st}:")
    print(body[:1200] if body else "(empty)")

    try:
        req = urllib.request.Request(
            "https://surprise-attack-leaderboard.vercel.app/api/leaderboard",
            headers={"User-Agent": "SA-Recover/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            site = json.loads(r.read().decode())
        ev = site.get("event") or {}
        scores = site.get("scoresByMode") or {}
        total = sum(len(scores.get(m) or []) for m in MODES)
        log(
            f"Vercel current: event={ev.get('event_id')} status={ev.get('status')} "
            f"song={ev.get('song_title')!r} score_rows={total}"
        )
        for m in MODES:
            for row in (scores.get(m) or [])[:5]:
                log(f"  {m}: {row.get('player_name')} = {row.get('score')}")
    except Exception as e:
        log(f"Vercel check failed: {e}")

    log("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted")
        sys.exit(1)

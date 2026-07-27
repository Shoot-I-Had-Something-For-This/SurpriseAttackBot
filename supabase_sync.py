"""
Push Surprise Attack events + scores to Supabase for the Vercel leaderboard.

Env (Render dashboard):
  SUPABASE_URL                  https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY     service_role secret (never put in the website)

Optional aliases:
  SA_SUPABASE_URL / SA_SUPABASE_SERVICE_KEY

If URL/key are missing, all functions no-op (bot still works offline).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

SUPABASE_URL = (
    os.getenv("SUPABASE_URL")
    or os.getenv("SA_SUPABASE_URL")
    or ""
).strip().rstrip("/")

SUPABASE_SERVICE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SA_SUPABASE_SERVICE_KEY")
    or ""
).strip()

MODES = ("arcade", "classic", "fusion")


def supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


def _unix_to_iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def event_status_from_state(state: dict) -> str:
    if state.get("active"):
        return "live"
    if state.get("scheduled_start_at") and not state.get("ended_at"):
        return "scheduled"
    return "closed"


def event_row_from_state(state: dict) -> dict[str, Any]:
    return {
        "event_id": state.get("event_id"),
        "song_title": state.get("song_title"),
        "song_artist": state.get("song_artist"),
        "event_difficulty": state.get("event_difficulty"),
        "status": event_status_from_state(state),
        "started_at": _unix_to_iso(state.get("started_at")),
        "ended_at": _unix_to_iso(state.get("ended_at")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def score_row(
    event_id: str,
    mode: str,
    player_key: str,
    row: dict,
) -> dict[str, Any]:
    updated = row.get("updated_at")
    if isinstance(updated, (int, float)):
        updated_iso = _unix_to_iso(updated)
    else:
        updated_iso = datetime.now(timezone.utc).isoformat()

    discord_id = row.get("discord_user_id")
    return {
        "event_id": event_id,
        "mode": mode,
        "player_key": player_key,
        "player_name": row.get("player_name") or "Unknown",
        "player_hash": row.get("player_hash"),
        "score": int(row.get("score") or 0),
        "difficulty": row.get("difficulty"),
        "title": row.get("title"),
        "artist": row.get("artist"),
        "discord_user_id": str(discord_id) if discord_id is not None else None,
        "max_combo": int(row.get("max_combo") or 0),
        "accuracy": float(row.get("accuracy") or 0),
        "updated_at": updated_iso or datetime.now(timezone.utc).isoformat(),
    }


async def _request(
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: dict[str, str] | None = None,
) -> bool:
    if not supabase_enabled():
        return False
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method,
                url,
                headers=_headers(),
                json=json_body,
                params=params,
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    print(f"Supabase {method} {path} → {resp.status}: {text[:300]}")
                    return False
                return True
    except Exception as e:
        print(f"Supabase request failed ({method} {path}): {e}")
        return False


async def upsert_event(state: dict) -> bool:
    if not supabase_enabled():
        return False
    event_id = state.get("event_id")
    if not event_id:
        return False
    row = event_row_from_state(state)
    ok = await _request(
        "POST",
        "sa_events",
        json_body=row,
        params={"on_conflict": "event_id"},
    )
    if ok:
        print(f"Supabase: event upserted {event_id} status={row['status']}")
    return ok


async def upsert_score(
    event_id: str,
    mode: str,
    player_key: str,
    row: dict,
) -> bool:
    if not supabase_enabled():
        return False
    if mode not in MODES or not event_id or not player_key:
        return False
    body = score_row(event_id, mode, player_key, row)
    ok = await _request(
        "POST",
        "sa_scores",
        json_body=body,
        params={"on_conflict": "event_id,mode,player_key"},
    )
    if ok:
        print(
            f"Supabase: score {mode} {body['player_name']}={body['score']} "
            f"event={event_id}"
        )
    return ok


async def sync_all_scores(state: dict) -> int:
    """Upsert every score in state. Returns count of successful mode rows."""
    if not supabase_enabled():
        return 0
    event_id = state.get("event_id")
    if not event_id:
        return 0
    await upsert_event(state)
    count = 0
    scores = state.get("scores") or {}
    for mode in MODES:
        bucket = scores.get(mode) or {}
        if not isinstance(bucket, dict):
            continue
        for player_key, row in bucket.items():
            if not isinstance(row, dict):
                continue
            if await upsert_score(event_id, mode, player_key, row):
                count += 1
            # light pacing
            await _sleep(0.05)
    print(f"Supabase: synced {count} score row(s) for {event_id}")
    return count


async def close_event(state: dict) -> bool:
    """Mark event closed and push final scores."""
    if not supabase_enabled():
        return False
    state = dict(state)
    state["active"] = False
    if not state.get("ended_at"):
        state["ended_at"] = int(time.time())
    await sync_all_scores(state)
    return True


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)

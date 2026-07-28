#!/usr/bin/env python3
"""
One-shot: push local history/*.json (+ optional live sa_state.json) into Supabase.

Usage (from SurpriseAttackBot folder):
  set SUPABASE_URL=https://xxxx.supabase.co
  set SUPABASE_SERVICE_ROLE_KEY=eyJ...
  python seed_supabase_history.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_DIR = Path(__file__).resolve().parent
HISTORY_DIR = Path(os.getenv("SA_HISTORY_DIR") or BOT_DIR / "history")
STATE_PATH = Path(os.getenv("SA_STATE_PATH") or BOT_DIR / "sa_state.json")

# Import after dotenv so env is loaded
from supabase_sync import close_event, supabase_enabled, sync_all_scores  # noqa: E402


def load_snapshot(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Skip {path.name}: {e}")
        return None
    if not isinstance(data, dict) or not data.get("event_id"):
        print(f"Skip {path.name}: missing event_id")
        return None
    # History files are closed events
    data.setdefault("active", False)
    if not data.get("ended_at") and path.name.startswith("sa-"):
        data["ended_at"] = data.get("started_at")
    data.setdefault("scores", {"arcade": {}, "classic": {}, "fusion": {}})
    return data


async def main() -> int:
    if not supabase_enabled():
        print("ERROR: Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
        return 1

    files = sorted(HISTORY_DIR.glob("sa-*.json")) if HISTORY_DIR.is_dir() else []
    if STATE_PATH.is_file():
        files.append(STATE_PATH)

    if not files:
        print(f"No history files in {HISTORY_DIR}")
        return 0

    ok = 0
    for path in files:
        snap = load_snapshot(path)
        if not snap:
            continue
        print(f"Seeding {path.name} ({snap.get('event_id')})…")
        if snap.get("active"):
            n, detail = await sync_all_scores(snap)
            print(f"  → {detail}")
            if n is not None and "event failed" not in detail:
                ok += 1
        else:
            closed_ok, detail = await close_event(snap)
            print(f"  → {detail}")
            if closed_ok:
                ok += 1
        await asyncio.sleep(0.2)

    print(f"Done. Processed {ok} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

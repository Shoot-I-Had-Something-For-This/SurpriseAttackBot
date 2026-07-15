#!/usr/bin/env python3
"""
Surprise Attack Discord bot (standalone).

One live channel per event:
  - Players forward game score embeds OR post scoreboard screenshots
  - Bot auto-sorts Arcade / Classic / Fusion
  - One live leaderboard message with three mode sections

Operator commands (role or Manage Server):
  !sa start [Song - Artist]              start now
  !sa start [Song - Artist] for 1h       start now, auto-end later
  !sa start [Song - Artist] in 30m       schedule put-up
  !sa start [Song - Artist] in 30m for 1h
  !sa end                                take down now
  !sa end in 45m                         schedule take-down
  !sa cancel                             cancel pending start/end timers
  !sa status / board / help
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# Bump this on every deploy-critical fix so !sa help proves which build is live.
BOT_VERSION = "2026-07-15-timers-v2"

BOT_DIR = Path(__file__).resolve().parent

# === CONFIG ===
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or "YOUR_BOT_TOKEN_HERE"

_channel_raw = os.getenv("SA_CHANNEL_ID", "").strip()
SA_CHANNEL_ID = int(_channel_raw) if _channel_raw else None

# Comma-separated role IDs that may run !sa commands (optional if they have Manage Server)
_role_raw = os.getenv("SA_OPERATOR_ROLE_IDS", "").strip()
SA_OPERATOR_ROLE_IDS: set[int] = set()
if _role_raw:
    for part in _role_raw.split(","):
        part = part.strip()
        if part.isdigit():
            SA_OPERATOR_ROLE_IDS.add(int(part))

LEADERBOARD_LIMIT = int(os.getenv("SA_LEADERBOARD_LIMIT", "10"))
STATE_PATH = Path(os.getenv("SA_STATE_PATH") or BOT_DIR / "sa_state.json")
HISTORY_DIR = Path(os.getenv("SA_HISTORY_DIR") or BOT_DIR / "history")

# Gemini OCR for scoreboard screenshots (same idea as the Indies score bot)
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
gemini_client = None
if GEMINI_API_KEY:
    try:
        from google import genai

        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini OCR enabled for scoreboard screenshots")
    except Exception as e:
        print(f"Gemini init failed: {e}")
else:
    print("GEMINI_API_KEY not set — screenshots disabled (embeds still work)")

BOARD_MARKER = "⚡ Surprise Attack Leaderboard"
MODES = ("arcade", "classic", "fusion")
MODE_LABELS = {
    "arcade": "🕹️ Arcade",
    "classic": "🥁 Classic",
    "fusion": "🔥 Fusion",
}
MODE_COLORS = {
    "arcade": 0x38BDF8,
    "classic": 0xA78BFA,
    "fusion": 0xF97316,
}

GAME_MODE_MAP = {
    "classic": "classic",
    "arcade": "arcade",
    "fusion": "fusion",
}

DIFFICULTY_MAP = {
    "easy": "easy",
    "normal": "normal",
    "hard": "hard",
    "extreme": "extreme",
    "hardcore": "hardcore",
}

intents = discord.Intents.default()
intents.message_content = True
# Server Members Intent is NOT required: role checks use message.author.roles
# (present on guild messages without the privileged members intent).
client = discord.Client(intents=intents)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def empty_state() -> dict:
    return {
        "active": False,
        "event_id": None,
        "song_title": None,
        "song_artist": None,
        "started_at": None,
        "ended_at": None,
        "channel_id": None,
        "board_message_id": None,
        "announce_message_id": None,
        # Public thread under #sa where players post scores (keeps main channel clean)
        "submit_thread_id": None,
        # Timers (unix seconds). scheduled_duration_sec = auto-end length after start.
        "scheduled_start_at": None,
        "scheduled_end_at": None,
        "scheduled_duration_sec": None,
        "scores": {m: {} for m in MODES},
        "history": [],  # recent closed event summaries (ids only; full dumps in history/)
    }


def load_state() -> dict:
    if not STATE_PATH.is_file():
        return empty_state()
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return empty_state()
        # ensure shape
        base = empty_state()
        base.update(data)
        if "scores" not in base or not isinstance(base["scores"], dict):
            base["scores"] = {m: {} for m in MODES}
        for m in MODES:
            base["scores"].setdefault(m, {})
        return base
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARNING: could not read state: {e}")
        return empty_state()


def save_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        print(f"Could not save state: {e}")


def new_event_id() -> str:
    return datetime.now(timezone.utc).strftime("sa-%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_player_name(name: str | None) -> str:
    if not name:
        return "Unknown"
    return re.sub(r"\s*\([^)]+\)\s*$", "", name).strip() or "Unknown"


def is_game_footer(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith("smash drums") or ("quest" in t and bool(re.search(r"\d+\.\d+", t)))


def normalize_difficulty(raw: str) -> str:
    key = (raw or "").lower().strip()
    return DIFFICULTY_MAP.get(key, key if key in DIFFICULTY_MAP.values() else "normal")


def normalize_game_mode(raw: str) -> str:
    key = (raw or "").lower().strip()
    if key in GAME_MODE_MAP:
        return GAME_MODE_MAP[key]
    if key in MODES:
        return key
    # soft synonyms
    if "fusion" in key:
        return "fusion"
    if "arcade" in key:
        return "arcade"
    if "classic" in key:
        return "classic"
    print(f"Unknown game mode {raw!r} — defaulting to classic")
    return "classic"


def difficulty_label(diff: str) -> str:
    return (diff or "normal").capitalize()


def game_mode_label(mode: str) -> str:
    return (mode or "classic").capitalize()


def parse_title_artist(text: str) -> tuple[str | None, str | None]:
    raw = (text or "").strip()
    if not raw:
        return None, None
    by_match = re.match(r"^(.+?)\s+by\s+(.+)$", raw, re.I)
    if by_match:
        return by_match.group(1).strip(), by_match.group(2).strip()
    for sep in (" - ", " – ", " — ", " / ", "|"):
        if sep in raw:
            left, right = raw.split(sep, 1)
            if left.strip() and right.strip():
                return left.strip(), right.strip()
    return raw, None


def parse_duration_token(token: str) -> int | None:
    """
    Parse a duration into seconds.
    Accepts: 30m, 30min, 30 minutes, 1h, 1hr, 2h30m, 90 (minutes), 45s, 1.5h, 1d
    """
    raw = (token or "").strip().lower()
    if not raw:
        return None
    # Normalize words → single-letter units (order matters: longer first)
    raw = re.sub(r"\bminutes?\b", "m", raw)
    raw = re.sub(r"\bmins?\b", "m", raw)
    raw = re.sub(r"\bhours?\b", "h", raw)
    raw = re.sub(r"\bhrs?\b", "h", raw)
    raw = re.sub(r"\bseconds?\b", "s", raw)
    raw = re.sub(r"\bsecs?\b", "s", raw)
    raw = re.sub(r"\bdays?\b", "d", raw)
    # glued words: 30min, 2hrs, 1hour
    raw = re.sub(r"(\d+(?:\.\d+)?)mins?\b", r"\1m", raw)
    raw = re.sub(r"(\d+(?:\.\d+)?)minutes?\b", r"\1m", raw)
    raw = re.sub(r"(\d+(?:\.\d+)?)hrs?\b", r"\1h", raw)
    raw = re.sub(r"(\d+(?:\.\d+)?)hours?\b", r"\1h", raw)
    raw = re.sub(r"(\d+(?:\.\d+)?)secs?\b", r"\1s", raw)
    raw = re.sub(r"(\d+(?:\.\d+)?)seconds?\b", r"\1s", raw)
    raw = re.sub(r"\s+", "", raw)

    if re.fullmatch(r"\d+", raw):
        return int(raw) * 60  # bare number = minutes

    parts = re.findall(r"(\d+(?:\.\d+)?)([smhd])", raw)
    if not parts:
        return None
    rebuilt = "".join(n + u for n, u in parts)
    if rebuilt != raw:
        return None

    total = 0.0
    for num_s, unit in parts:
        val = float(num_s)
        if unit == "s":
            total += val
        elif unit == "m":
            total += val * 60
        elif unit == "h":
            total += val * 3600
        elif unit == "d":
            total += val * 86400
    sec = int(total)
    return sec if sec > 0 else None


def format_duration(seconds: int | None) -> str:
    if not seconds or seconds <= 0:
        return "—"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    if secs and not parts:
        parts.append(f"{secs}s")
    elif secs and days == 0 and hours == 0:
        parts.append(f"{secs}s")
    return " ".join(parts) if parts else "0s"


def extract_schedule_args(rest: str) -> dict:
    """
    Strip trailing `in <dur>` / `for <dur>` (any order, up to one each).
    Returns song_rest, start_in_sec, duration_sec, error (if `in`/`for` present but invalid).
    """
    text = (rest or "").strip()
    start_in_sec: int | None = None
    duration_sec: int | None = None
    error: str | None = None

    # Allow multi-word durations: "in 30 minutes", "for 1 hour", "in 2h 30m"
    pat_in = re.compile(
        r"\bin\s+(\d+(?:\.\d+)?(?:\s*[a-z]+)?(?:\s+\d+(?:\.\d+)?(?:\s*[a-z]+)?)*)\s*$",
        re.I,
    )
    pat_for = re.compile(
        r"\bfor\s+(\d+(?:\.\d+)?(?:\s*[a-z]+)?(?:\s+\d+(?:\.\d+)?(?:\s*[a-z]+)?)*)\s*$",
        re.I,
    )

    for _ in range(2):
        m_in = pat_in.search(text)
        m_for = pat_for.search(text)
        picks: list[tuple[str, re.Match[str]]] = []
        if m_in:
            picks.append(("in", m_in))
        if m_for:
            picks.append(("for", m_for))
        if not picks:
            break
        kind, match = max(picks, key=lambda item: item[1].start())
        token = match.group(1).strip()
        sec = parse_duration_token(token)
        if sec is None:
            error = (
                f"Could not parse duration `{token}` after **{kind}**. "
                f"Use e.g. `30m`, `1h`, `2h30m`, or `30 minutes`."
            )
            break
        if kind == "in":
            start_in_sec = sec
        else:
            duration_sec = sec
        text = text[: match.start()].strip()

    # Fail closed: bare trailing "in …" / "for …" that didn't parse must not become a song title
    if error is None:
        dangling = re.search(r"\b(in|for)\s+(\S+(?:\s+\S+){0,3})\s*$", text, re.I)
        if dangling:
            maybe = parse_duration_token(dangling.group(2).replace(" ", ""))
            if maybe is None and re.search(r"\d", dangling.group(2)):
                error = (
                    f"Looks like a timer (`{dangling.group(0).strip()}`) but duration is invalid. "
                    f"Use `in 30m` (wait then put up) or `for 1h` (start now, auto take-down)."
                )

    return {
        "song_rest": text,
        "start_in_sec": start_in_sec,
        "duration_sec": duration_sec,
        "error": error,
    }


def clear_schedule_fields(state: dict) -> None:
    state["scheduled_start_at"] = None
    state["scheduled_end_at"] = None
    state["scheduled_duration_sec"] = None


def has_pending_schedule(state: dict) -> bool:
    return bool(state.get("scheduled_start_at") or state.get("scheduled_end_at"))


def titles_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return True  # no filter or missing title → accept
    def norm(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"^\[indies\]\s*", "", s)
        s = re.sub(r"[^a-z0-9]+", "", s)
        return s
    return norm(a) == norm(b) or norm(a) in norm(b) or norm(b) in norm(a)


def parse_embed(embed: discord.Embed) -> dict | None:
    if not embed:
        return None

    data: dict = {"artist": "", "title": ""}
    desc = embed.description or ""
    fields = {f.name.lower(): f.value for f in (embed.fields or [])}

    song_val = fields.get("song", "")
    indie_match = re.match(r"\[Indies\]\s*(\S+)", song_val, re.I)
    if indie_match:
        data["inGameSongId"] = indie_match.group(1)
        data["isIndie"] = True
        data["title"] = song_val.strip()  # hash until mapped; still usable for display
    elif song_val:
        data["title"] = song_val.strip()
    elif embed.title and "⭐" not in embed.title and "★" not in embed.title:
        data["title"] = embed.title.strip()

    score = 0
    for key in ("score", "points"):
        if key in fields:
            score = int(re.sub(r"[^\d]", "", fields[key]) or 0)
            break
    if not score and desc:
        m = re.search(r"([\d,]+)", desc)
        if m:
            score = int(re.sub(r"[^\d]", "", m.group(1)) or 0)
    data["score"] = score

    data["maxCombo"] = 0
    for key in ("combo", "max combo", "highest combo"):
        if key in fields:
            data["maxCombo"] = int(re.sub(r"[^\d]", "", fields[key]) or 0)
            break

    data["accuracy"] = 0.0
    if "mojo" in fields:
        try:
            mojo = float(fields["mojo"].replace("%", "").strip())
            data["accuracy"] = mojo / 100.0 if mojo > 1 else mojo
        except ValueError:
            pass
    elif embed.title:
        stars = embed.title.count("⭐") + embed.title.count("★")
        if stars:
            data["accuracy"] = stars / 5.0

    raw_diff = fields.get("difficulty") or fields.get("diff") or ""
    data["difficultyRaw"] = raw_diff.strip()
    data["difficulty"] = normalize_difficulty(raw_diff)

    raw_mode = fields.get("game mode") or fields.get("gamemode") or fields.get("mode") or ""
    data["gameModeRaw"] = raw_mode.strip()
    data["gameMode"] = normalize_game_mode(raw_mode)

    artist_match = re.search(r"Artist[:\s]+(.+?)(?:\n|$)", desc, re.I)
    if artist_match:
        data["artist"] = artist_match.group(1).strip()

    if embed.author and embed.author.name:
        data["playerName"] = parse_player_name(embed.author.name)
    elif embed.footer and embed.footer.text and not is_game_footer(embed.footer.text):
        data["playerName"] = parse_player_name(embed.footer.text)
    else:
        data["playerName"] = "Unknown"

    if data.get("score") or data.get("title") or data.get("inGameSongId"):
        return data
    return None


def _iter_message_embeds(message: discord.Message):
    """Embeds on the message, plus Discord 'Forward' snapshots."""
    for embed in message.embeds or []:
        yield embed
    for snap in getattr(message, "message_snapshots", None) or []:
        for embed in getattr(snap, "embeds", None) or []:
            yield embed


def _iter_message_attachments(message: discord.Message):
    for att in message.attachments or []:
        yield att
    for snap in getattr(message, "message_snapshots", None) or []:
        for att in getattr(snap, "attachments", None) or []:
            yield att


async def parse_game_score_message(message: discord.Message) -> dict | None:
    for embed in _iter_message_embeds(message):
        data = parse_embed(embed)
        if data:
            return data

    # Reply/forward reference: fetch original if we still have no embeds
    ref = message.reference
    if ref and ref.message_id:
        try:
            channel = message.channel
            if ref.resolved and isinstance(ref.resolved, discord.Message):
                original = ref.resolved
            else:
                original = await channel.fetch_message(ref.message_id)
            for embed in _iter_message_embeds(original):
                data = parse_embed(embed)
                if data:
                    return data
        except (discord.NotFound, discord.HTTPException, discord.Forbidden) as e:
            print(f"Could not resolve message reference: {e}")
    return None


def normalize_ocr_data(data: dict) -> dict:
    """Clean Gemini OCR output into the same shape as embed parsing."""
    title = (data.get("title") or "").strip()
    artist = (data.get("artist") or "").strip()

    by_match = re.match(r"^(.+?)\s+by\s+(.+)$", title, re.I)
    if by_match:
        title = by_match.group(1).strip()
        if not artist:
            artist = by_match.group(2).strip()

    if title.lower().startswith("[indies]"):
        title = re.sub(r"^\[Indies\]\s*", "", title, flags=re.I).strip()
        data["isIndie"] = True

    data["title"] = title
    data["artist"] = artist

    if data.get("inGameSongId"):
        data["inGameSongId"] = str(data["inGameSongId"]).strip()

    if data.get("difficulty"):
        data["difficultyRaw"] = str(data["difficulty"]).strip()
        data["difficulty"] = normalize_difficulty(data["difficultyRaw"])

    if data.get("gameMode"):
        data["gameModeRaw"] = str(data["gameMode"]).strip()
        data["gameMode"] = normalize_game_mode(data["gameModeRaw"])
    else:
        data["gameMode"] = "classic"

    if data.get("playerName"):
        data["playerName"] = parse_player_name(str(data["playerName"]))

    try:
        data["score"] = int(data.get("score") or 0)
    except (TypeError, ValueError):
        data["score"] = 0

    return data


async def extract_score_from_image(image_bytes: bytes) -> dict:
    """Use Gemini to read a Smash Drums end-of-song scoreboard screenshot."""
    if not gemini_client:
        return {"error": "Gemini not configured (add GEMINI_API_KEY to .env)"}

    try:
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))

        prompt = (
            "You are extracting data from a Smash Drums end-of-song scoreboard screenshot.\n\n"
            "Scoreboard song line layout:\n"
            '- Indies songs may show "[Indies]" near the song info.\n'
            '- Song is often "Title by Artist" (example: "Danger by Shotty Horroh").\n'
            "- An 8-character in-game song ID may appear — do NOT put that in the title.\n"
            "- Split title and artist into separate fields.\n"
            "- Game mode may be Classic, Arcade, or Fusion.\n"
            "- Difficulty may be Easy, Normal, Hard, Extreme, or Hardcore.\n\n"
            "Return ONLY valid JSON (no markdown, no extra text):\n\n"
            "{\n"
            '  "playerName": string or null,\n'
            '  "score": number or null,\n'
            '  "difficulty": "easy" | "normal" | "hard" | "extreme" | "hardcore" or null,\n'
            '  "gameMode": "classic" | "arcade" | "fusion" or null,\n'
            '  "title": string or null,\n'
            '  "artist": string or null,\n'
            '  "inGameSongId": string or null,\n'
            '  "isIndie": boolean or null,\n'
            '  "accuracy": number or null,\n'
            '  "maxCombo": number or null\n'
            "}\n\n"
            "Use null for any field you cannot confidently read."
        )

        response = await asyncio.to_thread(
            lambda: gemini_client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[prompt, img],
            )
        )
        text = (response.text or "").strip()

        if text.startswith("```"):
            text = text.split("```")[1].strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()

        return json.loads(text)
    except Exception as e:
        print(f"Gemini OCR error: {e}")
        return {"error": str(e)}


async def extract_score_from_attachments(message: discord.Message) -> dict | None:
    """OCR the first image attachment that looks like a scoreboard."""
    for attachment in _iter_message_attachments(message):
        ctype = (attachment.content_type or "").lower()
        name = (attachment.filename or "").lower()
        is_image = ctype.startswith("image/") or name.endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".gif")
        )
        if not is_image:
            continue
        try:
            image_bytes = await attachment.read()
        except Exception as e:
            print(f"Failed to download attachment: {e}")
            continue

        print(f"OCR: reading attachment {name or attachment.id} ({len(image_bytes)} bytes)")
        ocr = await extract_score_from_image(image_bytes)
        if ocr.get("error"):
            print(f"OCR error: {ocr['error']}")
            continue
        if not ocr.get("score") and not ocr.get("title"):
            print(f"OCR: no score/title in result keys={list(ocr.keys())}")
            continue
        return normalize_ocr_data(ocr)
    return None


async def extract_score_data(message: discord.Message) -> dict | None:
    """Prefer embed parse; fall back to screenshot OCR."""
    data = await parse_game_score_message(message)
    if data and data.get("score"):
        print(f"Parsed embed score={data.get('score')} mode={data.get('gameMode')}")
        return data

    ocr_data = await extract_score_from_attachments(message)
    if ocr_data and ocr_data.get("score"):
        print(
            f"Parsed OCR score={ocr_data.get('score')} mode={ocr_data.get('gameMode')} "
            f"title={ocr_data.get('title')!r}"
        )
        return ocr_data

    # Embed without score but with title — still return for clearer reject
    if data:
        return data
    return None


def looks_like_score_candidate(message: discord.Message) -> bool:
    if message.embeds:
        return True
    if getattr(message, "message_snapshots", None):
        return True
    if message.reference is not None:
        return True
    for attachment in _iter_message_attachments(message):
        ctype = (attachment.content_type or "").lower()
        name = (attachment.filename or "").lower()
        if ctype.startswith("image/") or name.endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".gif")
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def player_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "Unknown").strip().lower())


def record_score(state: dict, data: dict, discord_user_id: int | None = None) -> dict:
    """
    Record a personal-best for the event mode.
    Returns result dict: improved, mode, player, score, previous, rejected, reason
    """
    mode = normalize_game_mode(data.get("gameMode") or "classic")
    if mode not in MODES:
        mode = "classic"

    player = data.get("playerName") or "Unknown"
    score = int(data.get("score") or 0)
    difficulty = normalize_difficulty(data.get("difficulty") or "normal")
    title = (data.get("title") or "").strip()
    artist = (data.get("artist") or "").strip()

    if score <= 0:
        return {"rejected": True, "reason": "no score found"}

    # Optional song filter for the event (skip when embed only has an Indies hash)
    event_song = state.get("song_title")
    if event_song and title and not data.get("inGameSongId"):
        if not titles_match(title, event_song):
            return {
                "rejected": True,
                "reason": (
                    f"wrong song (event is **{event_song}**"
                    + (f" — {state['song_artist']}" if state.get("song_artist") else "")
                    + f"; got **{title}**)"
                ),
            }

    key = player_key(player)
    bucket = state["scores"].setdefault(mode, {})
    existing = bucket.get(key)
    previous = int(existing["score"]) if existing else None

    if existing and score <= previous:
        return {
            "rejected": False,
            "improved": False,
            "mode": mode,
            "player": player,
            "score": score,
            "previous": previous,
            "best": previous,
            "difficulty": difficulty,
        }

    bucket[key] = {
        "player_name": player,
        "score": score,
        "difficulty": difficulty,
        "title": title,
        "artist": artist,
        "discord_user_id": discord_user_id,
        "updated_at": int(time.time()),
        "max_combo": int(data.get("maxCombo") or 0),
        "accuracy": float(data.get("accuracy") or 0),
    }
    save_state(state)
    return {
        "rejected": False,
        "improved": True,
        "mode": mode,
        "player": player,
        "score": score,
        "previous": previous,
        "best": score,
        "difficulty": difficulty,
    }


def ranked_mode_scores(state: dict, mode: str, limit: int | None = None) -> list[dict]:
    rows = list((state.get("scores") or {}).get(mode, {}).values())
    rows.sort(key=lambda r: (-int(r.get("score") or 0), r.get("player_name") or ""))
    n = limit if limit is not None else LEADERBOARD_LIMIT
    return rows[:n]


def format_mode_block(rows: list[dict]) -> str:
    if not rows:
        return "_No scores yet._"
    lines = []
    for i, row in enumerate(rows, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`{i}.`")
        player = row.get("player_name") or "Unknown"
        score = int(row.get("score") or 0)
        diff = difficulty_label(row.get("difficulty") or "normal")
        lines.append(f"{medal} **{player}** — `{score:,}` · {diff}")
    return "\n".join(lines)


def song_line(state: dict) -> str:
    title = state.get("song_title")
    artist = state.get("song_artist")
    if title and artist:
        return f"**{title}** — {artist}"
    if title:
        return f"**{title}**"
    return "_Any song (no filter)_"


def timer_lines(state: dict) -> list[str]:
    """Human-readable schedule lines for status / board."""
    lines: list[str] = []
    start_at = state.get("scheduled_start_at")
    end_at = state.get("scheduled_end_at")
    if start_at and not state.get("active"):
        lines.append(f"**Put up:** <t:{int(start_at)}:F> · <t:{int(start_at)}:R>")
    if end_at and (state.get("active") or start_at):
        lines.append(f"**Take down:** <t:{int(end_at)}:F> · <t:{int(end_at)}:R>")
    dur = state.get("scheduled_duration_sec")
    if dur and not end_at and not state.get("active") and start_at:
        lines.append(f"**Duration after start:** {format_duration(int(dur))}")
    return lines


def build_board_embeds(state: dict) -> list[discord.Embed]:
    if state.get("active"):
        status = "LIVE"
        color = 0x22C55E
    elif state.get("scheduled_start_at"):
        status = "SCHEDULED"
        color = 0xEAB308
    else:
        status = "CLOSED"
        color = 0x64748B

    desc_parts = [
        f"**Status:** {status}",
        f"**Song:** {song_line(state)}",
        f"**Event:** `{state.get('event_id') or '—'}`",
    ]
    desc_parts.extend(timer_lines(state))
    desc_parts.append(f"_Updated <t:{int(time.time())}:R>_")

    header = discord.Embed(
        title=f"{BOARD_MARKER}",
        description="\n".join(desc_parts),
        color=color,
    )
    header.set_footer(text="One board · three modes · forward your score here")

    embeds = [header]
    for mode in MODES:
        rows = ranked_mode_scores(state, mode)
        emb = discord.Embed(
            title=MODE_LABELS[mode],
            description=format_mode_block(rows),
            color=MODE_COLORS[mode],
        )
        embeds.append(emb)
    return embeds


def build_announce_embed(state: dict) -> discord.Embed:
    thread_id = state.get("submit_thread_id")
    if thread_id:
        where = f"the **scores thread** → <#{thread_id}>"
    else:
        where = "the **scores thread** under this post"

    end_note = ""
    if state.get("scheduled_end_at"):
        end_at = int(state["scheduled_end_at"])
        end_note = f"\n\n⏱️ **Auto take-down:** <t:{end_at}:F> · <t:{end_at}:R>"

    emb = discord.Embed(
        title="⚡ Surprise Attack is LIVE",
        description=(
            f"Song: {song_line(state)}\n\n"
            "**How to play**\n"
            "1. Play the song (Arcade, Classic, or Fusion)\n"
            "2. Results → **Discord → Submit Score**, *or* take a scoreboard screenshot\n"
            f"3. Post it in {where}\n\n"
            "The bot sorts your score into Arcade / Classic / Fusion automatically.\n"
            "Leaderboard stays in this channel — only scores go in the thread."
            f"{end_note}"
        ),
        color=0x22C55E,
    )
    emb.add_field(
        name="Boards",
        value="Arcade · Classic · Fusion — live leaderboard in this channel",
        inline=False,
    )
    emb.set_footer(text=f"Event {state.get('event_id')}")
    return emb


def build_scheduled_announce_embed(state: dict) -> discord.Embed:
    start_at = int(state.get("scheduled_start_at") or 0)
    end_at = state.get("scheduled_end_at")
    dur = state.get("scheduled_duration_sec")
    lines = [
        f"Song: {song_line(state)}",
        "",
        "🔒 **NOT LIVE YET** — do not submit scores.",
        "This is only a heads-up. The attack is **not** open until the put-up time.",
        "",
        f"⏱️ **Goes live:** <t:{start_at}:F> · <t:{start_at}:R>",
    ]
    if end_at:
        lines.append(f"⏱️ **Take down:** <t:{int(end_at)}:F> · <t:{int(end_at)}:R>")
    elif dur:
        lines.append(f"⏱️ **Runs for:** {format_duration(int(dur))} after start")
    lines.append("")
    lines.append("When it goes live you will see a green **LIVE** post + scores thread.")
    lines.append("Operators: `!sa cancel` to scrap this timer.")
    emb = discord.Embed(
        title="📅 Surprise Attack scheduled (not open)",
        description="\n".join(lines),
        color=0xEAB308,
    )
    emb.set_footer(text="Put-up timer — scores closed until go-live")
    return emb


# ---------------------------------------------------------------------------
# Permissions & channel helpers
# ---------------------------------------------------------------------------

def is_in_sa_channel(channel) -> bool:
    """Main #sa channel or any thread under it (commands allowed here)."""
    if not SA_CHANNEL_ID:
        return False
    if getattr(channel, "id", None) == SA_CHANNEL_ID:
        return True
    parent_id = getattr(channel, "parent_id", None)
    return parent_id is not None and parent_id == SA_CHANNEL_ID


def is_score_submit_location(channel, state: dict) -> bool:
    """
    Where players may post scores.
    Prefer the event's scores thread; if none exists, allow main channel / its threads.
    """
    thread_id = state.get("submit_thread_id")
    if thread_id:
        return getattr(channel, "id", None) == int(thread_id)
    return is_in_sa_channel(channel)


async def ensure_submit_thread(
    channel: discord.TextChannel,
    announce: discord.Message | None,
    state: dict,
) -> discord.Thread | None:
    """Create (or recover) the per-event scores thread."""
    existing_id = state.get("submit_thread_id")
    if existing_id:
        try:
            ch = client.get_channel(int(existing_id)) or await client.fetch_channel(
                int(existing_id)
            )
            if isinstance(ch, discord.Thread):
                # Unarchive if a previous end archived it
                if ch.archived:
                    try:
                        await ch.edit(archived=False, locked=False)
                    except discord.HTTPException:
                        pass
                return ch
        except (discord.NotFound, discord.HTTPException, TypeError, ValueError):
            pass

    song = state.get("song_title") or "Open play"
    # Thread names max 100 chars
    name = f"Scores · {song}"[:100]
    try:
        if announce is not None:
            thread = await announce.create_thread(
                name=name,
                auto_archive_duration=1440,  # 24h
                reason="Surprise Attack score submissions",
            )
        else:
            thread = await channel.create_thread(
                name=name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,
                reason="Surprise Attack score submissions",
            )
    except discord.Forbidden:
        print("Cannot create scores thread — need Create Public Threads")
        return None
    except discord.HTTPException as e:
        print(f"Create scores thread failed: {e}")
        return None

    state["submit_thread_id"] = thread.id
    save_state(state)

    try:
        await thread.send(
            f"**Score submissions for** {song_line(state)}\n"
            "Post here only:\n"
            "• Forward **Discord → Submit Score** embed, or\n"
            "• Upload a full **scoreboard screenshot**\n\n"
            f"Live boards stay in <#{channel.id}> (Arcade · Classic · Fusion)."
        )
    except discord.HTTPException as e:
        print(f"Thread intro failed: {e}")

    return thread


async def close_submit_thread(state: dict) -> None:
    tid = state.get("submit_thread_id")
    if not tid:
        return
    try:
        ch = client.get_channel(int(tid)) or await client.fetch_channel(int(tid))
        if isinstance(ch, discord.Thread):
            await ch.send("_Event closed — no more scores for this attack._")
            await ch.edit(archived=True, locked=True, reason="Surprise Attack ended")
    except (discord.NotFound, discord.HTTPException, TypeError, ValueError) as e:
        print(f"Could not archive scores thread: {e}")


def is_operator(member: discord.Member | discord.User | None) -> bool:
    if member is None:
        return False
    if isinstance(member, discord.Member):
        if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
            return True
        if SA_OPERATOR_ROLE_IDS:
            role_ids = {r.id for r in member.roles}
            if role_ids & SA_OPERATOR_ROLE_IDS:
                return True
    return False


def bot_has_reaction(message: discord.Message, emoji: str) -> bool:
    for reaction in message.reactions:
        if str(reaction.emoji) == emoji and reaction.me:
            return True
    return False


# ---------------------------------------------------------------------------
# Board post / edit
# ---------------------------------------------------------------------------

async def get_sa_channel() -> discord.TextChannel | None:
    if not SA_CHANNEL_ID:
        return None
    try:
        ch = client.get_channel(SA_CHANNEL_ID) or await client.fetch_channel(SA_CHANNEL_ID)
    except discord.HTTPException as e:
        print(f"SA channel unavailable: {e}")
        return None
    if not isinstance(ch, discord.TextChannel):
        print("SA_CHANNEL_ID is not a text channel")
        return None
    return ch


async def find_board_message(channel: discord.TextChannel, state: dict) -> discord.Message | None:
    msg_id = state.get("board_message_id")
    if msg_id and state.get("channel_id") == channel.id:
        try:
            return await channel.fetch_message(int(msg_id))
        except (discord.NotFound, discord.HTTPException, TypeError, ValueError):
            pass
    async for message in channel.history(limit=40):
        if message.author.id != client.user.id:
            continue
        if BOARD_MARKER in (message.content or ""):
            return message
        if message.embeds and any(BOARD_MARKER in (e.title or "") for e in message.embeds):
            return message
    return None


async def update_board(state: dict, *, force_new: bool = False) -> bool:
    channel = await get_sa_channel()
    if not channel:
        return False

    embeds = build_board_embeds(state)
    content = f"**{BOARD_MARKER}**"

    try:
        existing = None if force_new else await find_board_message(channel, state)
        if existing:
            await existing.edit(content=content, embeds=embeds)
            state["board_message_id"] = existing.id
            state["channel_id"] = channel.id
            save_state(state)
            return True

        msg = await channel.send(content=content, embeds=embeds)
        state["board_message_id"] = msg.id
        state["channel_id"] = channel.id
        save_state(state)
        try:
            await msg.pin(reason="Surprise Attack live leaderboard")
            print(f"Board pinned in #{channel.name}")
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"Pin board skipped: {e}")
            # Role checkbox can look correct while the bot's *install* perms still block pin.
            try:
                await channel.send(
                    "📌 I posted the leaderboard but **could not pin it** "
                    "(Discord: missing pin permission).\n"
                    "**Fix:** Server Settings → **Integrations** → **Surprise Attack Bot** → "
                    "ensure **Manage Messages** is on, **or** re-authorize with the invite link "
                    "(see `INVITE_BOT.url`).\n"
                    "**Meanwhile:** right-click the board message → **Pin Message**."
                )
            except discord.HTTPException:
                pass
        return True
    except discord.Forbidden:
        print("Board update failed — need Send Messages (+ Manage Messages to pin)")
    except discord.HTTPException as e:
        print(f"Board update HTTP error: {e}")
    return False


def archive_event(state: dict) -> Path | None:
    if not state.get("event_id"):
        return None
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{state['event_id']}.json"
    snapshot = {
        "event_id": state.get("event_id"),
        "song_title": state.get("song_title"),
        "song_artist": state.get("song_artist"),
        "started_at": state.get("started_at"),
        "ended_at": state.get("ended_at"),
        "scores": state.get("scores"),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
        return path
    except OSError as e:
        print(f"Could not archive event: {e}")
        return None


# ---------------------------------------------------------------------------
# Start / end (manual + scheduled)
# ---------------------------------------------------------------------------

# Prevent double-fire if tick races (created lazily for the running loop)
_schedule_lock: asyncio.Lock | None = None
_scheduler_started = False


def _get_schedule_lock() -> asyncio.Lock:
    global _schedule_lock
    if _schedule_lock is None:
        _schedule_lock = asyncio.Lock()
    return _schedule_lock


async def begin_live_event(
    *,
    title: str | None,
    artist: str | None,
    duration_sec: int | None = None,
    preserve_board: bool = False,
) -> dict:
    """
    Open a live event: announce, scores thread, board.
    duration_sec → auto take-down after that many seconds.
    """
    state = empty_state()
    # Keep board message id if we're promoting a pre-announce in same channel
    if preserve_board:
        old = load_state()
        state["board_message_id"] = old.get("board_message_id")
        state["channel_id"] = old.get("channel_id")

    now = int(time.time())
    state["active"] = True
    state["event_id"] = new_event_id()
    state["song_title"] = title
    state["song_artist"] = artist
    state["started_at"] = now
    state["channel_id"] = SA_CHANNEL_ID
    state["scores"] = {m: {} for m in MODES}
    state["scheduled_start_at"] = None
    state["scheduled_duration_sec"] = None
    if duration_sec and duration_sec > 0:
        state["scheduled_end_at"] = now + int(duration_sec)
    else:
        state["scheduled_end_at"] = None
    save_state(state)

    channel = await get_sa_channel()
    ann = None
    thread = None
    if channel:
        try:
            ann = await channel.send(embed=build_announce_embed(state))
            state["announce_message_id"] = ann.id
            save_state(state)
        except discord.HTTPException as e:
            print(f"Announce post failed: {e}")

        thread = await ensure_submit_thread(channel, ann, state)
        if thread and ann:
            try:
                await ann.edit(embed=build_announce_embed(state))
            except discord.HTTPException:
                pass

        await update_board(state, force_new=not preserve_board)

    state = load_state()
    state["_thread_id"] = thread.id if thread else None
    return state


async def finish_live_event(*, auto: bool = False) -> dict:
    """Close the live event, archive, freeze board, lock scores thread."""
    state = load_state()
    if not state.get("active"):
        return state

    state["active"] = False
    state["ended_at"] = int(time.time())
    state["scheduled_end_at"] = None
    state["scheduled_duration_sec"] = None
    state["scheduled_start_at"] = None
    path = archive_event(state)
    save_state(state)
    await update_board(state)
    await close_submit_thread(state)

    lines = [
        f"**Surprise Attack ended** `{state.get('event_id')}`"
        + (" _(timer)_" if auto else ""),
        f"Song: {song_line(state)}",
    ]
    for mode in MODES:
        rows = ranked_mode_scores(state, mode, limit=3)
        if rows:
            top = ", ".join(f"{r['player_name']} `{int(r['score']):,}`" for r in rows)
            lines.append(f"{MODE_LABELS[mode]}: {top}")
        else:
            lines.append(f"{MODE_LABELS[mode]}: _no scores_")
    if path:
        lines.append(f"_Archived to `{path.name}`_")

    # Auto take-down has no operator message to reply to — post standings in channel.
    # Manual `!sa end` replies in the command handler instead (avoid double post).
    if auto:
        channel = await get_sa_channel()
        if channel:
            try:
                await channel.send("\n".join(lines))
            except discord.HTTPException as e:
                print(f"End announce failed: {e}")

    state["_summary_lines"] = lines
    state["_archive_path"] = str(path) if path else None
    return state


async def post_scheduled_notice(state: dict) -> None:
    """
    Heads-up only. Do NOT open a scores thread or post the full mode leaderboard —
    that looks like the event is live and people start reacting/submitting early.
    """
    channel = await get_sa_channel()
    if not channel:
        return
    try:
        msg = await channel.send(embed=build_scheduled_announce_embed(state))
        state["announce_message_id"] = msg.id
        # Clear any prior board id so we force a fresh LIVE board at put-up time
        state["board_message_id"] = None
        save_state(state)
        print(
            f"Scheduled notice posted (NOT live). "
            f"put-up unix={state.get('scheduled_start_at')} "
            f"song={state.get('song_title')!r}"
        )
    except discord.HTTPException as e:
        print(f"Scheduled announce failed: {e}")


async def scheduler_tick() -> None:
    """Fire due put-up / take-down timers."""
    async with _get_schedule_lock():
        state = load_state()
        now = int(time.time())

        # Put-up — only when not active and absolute start time has been reached
        start_at = state.get("scheduled_start_at")
        if start_at and not state.get("active"):
            start_at_i = int(start_at)
            if now < start_at_i:
                # Still waiting — never promote early
                pass
            else:
                title = state.get("song_title")
                artist = state.get("song_artist")
                duration = state.get("scheduled_duration_sec")
                # If end was pre-computed as absolute from schedule time, convert to remaining
                end_at = state.get("scheduled_end_at")
                if end_at and not duration:
                    remaining = int(end_at) - now
                    duration = remaining if remaining > 0 else None
                late_by = now - start_at_i
                print(
                    f"Scheduler: putting up event song={title!r} "
                    f"(due unix={start_at_i}, late_by={late_by}s)"
                )
                # Clear schedule stamp before go-live so a crash mid-start can't double-fire
                state_clear = load_state()
                state_clear["scheduled_start_at"] = None
                save_state(state_clear)

                await begin_live_event(
                    title=title,
                    artist=artist,
                    duration_sec=int(duration) if duration else None,
                    preserve_board=False,  # always new LIVE board at real put-up
                )
                channel = await get_sa_channel()
                if channel:
                    try:
                        st = load_state()
                        note = f"⚡ **Surprise Attack is LIVE** (timer) · {song_line(st)}"
                        if st.get("scheduled_end_at"):
                            note += f"\nTake-down: <t:{int(st['scheduled_end_at'])}:R>"
                        if st.get("submit_thread_id"):
                            note += f"\nScores: <#{st['submit_thread_id']}>"
                        await channel.send(note)
                    except discord.HTTPException:
                        pass
                return

        # Take-down
        state = load_state()
        end_at = state.get("scheduled_end_at")
        if state.get("active") and end_at and now >= int(end_at):
            print(f"Scheduler: taking down event {state.get('event_id')}")
            await finish_live_event(auto=True)


async def scheduler_loop() -> None:
    await client.wait_until_ready()
    print("Schedule timer loop running (check every 15s)")
    while not client.is_closed():
        try:
            await scheduler_tick()
        except Exception as e:
            print(f"Scheduler tick error: {e}")
        await asyncio.sleep(15)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def parse_sa_command(text: str) -> dict | None:
    raw = (text or "").strip()
    m = re.match(r"^(?:!|/)sa(?:\s+(\S+))?(?:\s+(.*))?$", raw, re.I | re.DOTALL)
    if not m:
        return None
    action = (m.group(1) or "help").lower()
    rest = (m.group(2) or "").strip()
    return {"action": action, "rest": rest}


async def handle_sa_command(message: discord.Message, cmd: dict) -> None:
    action = cmd["action"]
    rest = cmd["rest"]
    state = load_state()

    if action in ("help", "?", "commands", "timer", "timers"):
        # Embed so timer lines cannot get lost / look like an old short help list.
        emb = discord.Embed(
            title="Surprise Attack — commands",
            color=0xFACC15,
            description=(
                f"**Build:** `{BOT_VERSION}`\n"
                "If this build string is missing, Render is still on an **old** deploy."
            ),
        )
        emb.add_field(
            name="⏱️ Timers (put-up / take-down)",
            value=(
                "```\n"
                "!sa start Song - Artist for 1h\n"
                "    start NOW, auto take-down after 1 hour\n"
                "!sa start Song - Artist in 30m\n"
                "    schedule put-up in 30 minutes (not live yet)\n"
                "!sa start Song - Artist in 30m for 1h\n"
                "    put-up in 30m, then run 1 hour\n"
                "!sa end in 45m\n"
                "    schedule take-down (event stays live until then)\n"
                "!sa cancel\n"
                "    cancel pending put-up OR take-down timer\n"
                "!sa status\n"
                "    show live/scheduled + any timers\n"
                "```\n"
                "Durations: `30m`, `1h`, `2h30m`, `90` (= 90 minutes), "
                "`30 minutes`, `1 hour`."
            ),
            inline=False,
        )
        emb.add_field(
            name="Event basics",
            value=(
                "```\n"
                "!sa start [Song - Artist]     start now (no timer)\n"
                "!sa end                       take down now\n"
                "!sa board                     refresh leaderboard\n"
                "!sa where                     bound channel check\n"
                "!sa fake <mode> <score> [name]\n"
                "!sa clear [bot|all] [limit]\n"
                "!sa help                      this message\n"
                "```"
            ),
            inline=False,
        )
        emb.add_field(
            name="Notes",
            value=(
                "• Run commands in the **bound** `#sa` channel (`!sa where`).\n"
                "• Yellow **scheduled** post ≠ live. Live = green LIVE + scores thread.\n"
                "• Operators: Manage Server / Admin, or `SA_OPERATOR_ROLE_IDS`."
            ),
            inline=False,
        )
        emb.set_footer(text=f"SA bot {BOT_VERSION}")
        await message.reply(embed=emb, mention_author=False)
        return

    if action in ("where", "config", "channel"):
        ch = message.channel
        guild_name = ch.guild.name if getattr(ch, "guild", None) else "?"
        bound = SA_CHANNEL_ID
        here = getattr(ch, "id", None)
        parent = getattr(ch, "parent_id", None)
        watching_here = here == bound or parent == bound
        await message.reply(
            f"**Bound to channel ID:** `{bound}`\n"
            f"**This message is in:** `{here}` · **{guild_name}** · "
            f"#{getattr(ch, 'name', '?')}\n"
            + (
                "✅ This channel matches `.env` — scores/commands work here."
                if watching_here
                else "❌ This channel does **not** match `SA_CHANNEL_ID`. "
                "Update `.env` with this channel’s ID (Developer Mode → Copy Channel ID), "
                "then restart the bot."
            ),
            mention_author=False,
        )
        return

    if action == "status":
        ch = message.channel
        guild_name = ch.guild.name if getattr(ch, "guild", None) else "?"
        lines = [
            f"**Server:** {guild_name}",
            f"**Watching channel ID:** `{SA_CHANNEL_ID}`",
        ]
        if state.get("active"):
            lines.extend(
                [
                    f"**LIVE** `{state.get('event_id')}`",
                    f"Song: {song_line(state)}",
                ]
            )
            if state.get("submit_thread_id"):
                lines.append(f"**Scores thread:** <#{state['submit_thread_id']}>")
            if state.get("started_at"):
                lines.append(f"Started: <t:{int(state['started_at'])}:R>")
            lines.extend(timer_lines(state))
        elif state.get("scheduled_start_at"):
            lines.append("**SCHEDULED** (not live yet)")
            lines.append(f"Song: {song_line(state)}")
            lines.extend(timer_lines(state))
            lines.append("Operators: `!sa cancel` to scrap · `!sa start …` to go live now")
        else:
            lines.append("No Surprise Attack is live or scheduled.")
            lines.append("Operators: `!sa start Song - Artist` or `!sa start Song in 30m for 1h`")
        await message.reply("\n".join(lines), mention_author=False)
        return

    # Operator-only below
    if not is_operator(message.author):
        await message.reply(
            "Only **SA Operators** (or Manage Server) can run that command.",
            mention_author=False,
        )
        return

    if action in ("start", "open", "new"):
        if state.get("active"):
            await message.reply(
                f"An event is already live: `{state.get('event_id')}`.\n"
                "Run `!sa end` first (or `!sa end in 30m` to schedule take-down).",
                mention_author=False,
            )
            return

        sched = extract_schedule_args(rest)
        if sched.get("error"):
            await message.reply(sched["error"], mention_author=False)
            return

        start_in = sched["start_in_sec"]
        duration = sched["duration_sec"]
        song_rest = sched["song_rest"]
        title, artist = parse_title_artist(song_rest) if song_rest else (None, None)

        # Delayed put-up — never open scores / LIVE board until timer fires
        if start_in:
            if state.get("scheduled_start_at") and not state.get("active"):
                await message.reply(
                    "A put-up is already scheduled. "
                    "`!sa cancel` first, or wait for it to fire.",
                    mention_author=False,
                )
                return

            now = int(time.time())
            go_live_at = now + int(start_in)
            state = empty_state()
            state["active"] = False
            state["song_title"] = title
            state["song_artist"] = artist
            state["channel_id"] = SA_CHANNEL_ID
            state["scheduled_start_at"] = go_live_at
            if duration:
                state["scheduled_duration_sec"] = int(duration)
                state["scheduled_end_at"] = go_live_at + int(duration)
            else:
                state["scheduled_duration_sec"] = None
                state["scheduled_end_at"] = None
            save_state(state)
            print(
                f"Schedule armed: put-up in {start_in}s (unix {go_live_at}) "
                f"duration={duration} song={title!r}"
            )
            await post_scheduled_notice(state)

            reply = (
                f"📅 **Put-up scheduled** in {format_duration(start_in)}\n"
                f"Song: {song_line(state)}\n"
                f"Goes live: <t:{go_live_at}:F> · <t:{go_live_at}:R>\n"
                f"_A yellow **not open** notice was posted — that is **not** the live event. "
                f"Scores stay closed until then._"
            )
            if duration:
                reply += (
                    f"\nRuns for {format_duration(duration)} → "
                    f"take-down <t:{state['scheduled_end_at']}:R>"
                )
            reply += "\n`!sa cancel` to scrap the timer."
            await message.reply(reply, mention_author=False)
            return

        # Start now (optional auto take-down via `for`)
        state = await begin_live_event(
            title=title,
            artist=artist,
            duration_sec=int(duration) if duration else None,
        )
        thread_id = state.get("submit_thread_id") or state.get("_thread_id")
        thread_note = (
            f"Players post scores in <#{thread_id}>"
            if thread_id
            else "⚠️ Could not create scores thread — scores accepted in this channel "
            "(grant **Create Public Threads** and restart event)."
        )
        reply = (
            f"Surprise Attack **started** `{state['event_id']}`\n"
            f"Song: {song_line(state)}\n"
            f"{thread_note}\n"
            "Leaderboard stays in this channel."
        )
        if state.get("scheduled_end_at"):
            reply += (
                f"\n⏱️ Auto take-down: <t:{int(state['scheduled_end_at'])}:F> · "
                f"<t:{int(state['scheduled_end_at'])}:R>"
            )
        await message.reply(reply, mention_author=False)
        return

    if action in ("end", "close", "stop"):
        sched = extract_schedule_args(rest)
        end_in = sched["start_in_sec"]  # `in 45m` on end command
        # Also allow bare `!sa end 45m` or `!sa end for 45m`
        if end_in is None and sched["duration_sec"] is not None and not sched["song_rest"]:
            end_in = sched["duration_sec"]
        if end_in is None and rest.strip():
            # bare duration: !sa end 30m
            bare = parse_duration_token(rest.strip().split()[0]) if rest.strip() else None
            if bare:
                end_in = bare

        if end_in:
            if not state.get("active"):
                await message.reply(
                    "No live event to schedule take-down for.\n"
                    "Start one first, or use `!sa start Song in 30m for 1h`.",
                    mention_author=False,
                )
                return
            now = int(time.time())
            state["scheduled_end_at"] = now + int(end_in)
            state["scheduled_duration_sec"] = None
            save_state(state)
            await update_board(state)
            await message.reply(
                f"⏱️ **Take-down scheduled** in {format_duration(end_in)}\n"
                f"Ends: <t:{state['scheduled_end_at']}:F> · "
                f"<t:{state['scheduled_end_at']}:R>\n"
                f"`!sa end` now to close early · `!sa cancel` to clear this timer only "
                f"(event stays live).",
                mention_author=False,
            )
            return

        if not state.get("active"):
            if state.get("scheduled_start_at"):
                await message.reply(
                    "Nothing live — a **put-up** is scheduled. "
                    "Use `!sa cancel` to scrap it.",
                    mention_author=False,
                )
            else:
                await message.reply("No active event to close.", mention_author=False)
            return

        state = await finish_live_event(auto=False)
        lines = state.get("_summary_lines") or [
            f"**Surprise Attack ended** `{state.get('event_id')}`"
        ]
        await message.reply("\n".join(lines), mention_author=False)
        return

    if action in ("cancel", "unschedule", "abort"):
        state = load_state()
        had_start = bool(state.get("scheduled_start_at"))
        had_end = bool(state.get("scheduled_end_at"))
        if not had_start and not had_end:
            await message.reply("No pending put-up or take-down timer.", mention_author=False)
            return

        # Cancel scheduled put-up that never went live → wipe pending event shell
        if had_start and not state.get("active"):
            clear_schedule_fields(state)
            state["song_title"] = None
            state["song_artist"] = None
            state["event_id"] = None
            save_state(state)
            await update_board(state)
            await message.reply(
                "Cancelled scheduled **put-up**. No event is live.",
                mention_author=False,
            )
            return

        # Live event: only clear auto take-down
        state["scheduled_end_at"] = None
        state["scheduled_duration_sec"] = None
        state["scheduled_start_at"] = None
        save_state(state)
        await update_board(state)
        await message.reply(
            "Cancelled **take-down timer**. Event stays **LIVE** until `!sa end`.",
            mention_author=False,
        )
        return

    if action in ("board", "lb", "refresh"):
        ok = await update_board(state, force_new=False)
        await message.reply(
            "Leaderboard refreshed." if ok else "Could not update the board — check bot permissions.",
            mention_author=False,
        )
        return

    # Clear channel / thread messages (operator)
    # !sa clear          → bot messages only (default, safer)
    # !sa clear bot 50
    # !sa clear all 100  → everyone (needs Manage Messages)
    if action in ("clear", "purge", "clean"):
        parts = rest.split()
        mode = "bot"
        limit = 100
        for p in parts:
            low = p.lower()
            if low in ("bot", "me", "self"):
                mode = "bot"
            elif low in ("all", "everyone", "channel"):
                mode = "all"
            elif low.isdigit():
                limit = max(1, min(int(low), 200))

        channel = message.channel
        if not is_in_sa_channel(channel):
            await message.reply("Only use clear in the SA channel or its threads.", mention_author=False)
            return

        bot_id = client.user.id if client.user else 0

        def check(m: discord.Message) -> bool:
            # Never delete the command message mid-purge awkwardly — purge includes it if matching
            if mode == "bot":
                return m.author.id == bot_id
            return True

        try:
            # purge cannot delete messages older than 14 days (Discord limit)
            deleted = await channel.purge(limit=limit, check=check, reason="SA clear command")
            await channel.send(
                f"Cleared **{len(deleted)}** message(s) "
                f"({mode}, looked at up to {limit}). "
                f"_Messages older than 14 days can’t be bulk-deleted._",
                delete_after=8,
            )
        except discord.Forbidden:
            await message.reply(
                "Missing **Manage Messages** (needed to bulk-delete).",
                mention_author=False,
            )
        except discord.HTTPException as e:
            await message.reply(f"Clear failed: {e}", mention_author=False)
        return

    # Operator test scores (no game embed needed)
    # !sa fake arcade 1500000
    # !sa fake classic 2000000 Alice
    # !sa testscore fusion 999999
    if action in ("fake", "testscore", "mock", "seed"):
        if not state.get("active"):
            await message.reply(
                "Start an event first: `!sa start Test Song`",
                mention_author=False,
            )
            return

        parts = rest.split()
        if len(parts) < 2:
            await message.reply(
                "Usage: `!sa fake <arcade|classic|fusion> <score> [player name]`\n"
                "Examples:\n"
                "`!sa fake arcade 1500000`\n"
                "`!sa fake classic 2200000 Alice`\n"
                "`!sa fake fusion 1800000 Bob`",
                mention_author=False,
            )
            return

        mode_raw = parts[0].lower().strip()
        score_raw = parts[1].replace(",", "")
        player = " ".join(parts[2:]).strip() if len(parts) > 2 else (
            getattr(message.author, "display_name", None) or message.author.name
        )

        if mode_raw not in MODES:
            await message.reply(
                "Mode must be `arcade`, `classic`, or `fusion`.",
                mention_author=False,
            )
            return
        mode = mode_raw

        try:
            score = int(score_raw)
        except ValueError:
            await message.reply("Score must be a number, e.g. `1500000`.", mention_author=False)
            return

        if score <= 0:
            await message.reply("Score must be greater than 0.", mention_author=False)
            return

        fake = {
            "playerName": player,
            "score": score,
            "gameMode": mode,
            "difficulty": "hard",
            "title": state.get("song_title") or "Test Song",
            "artist": state.get("song_artist") or "",
            "maxCombo": 0,
            "accuracy": 0.0,
        }
        result = record_score(state, fake, discord_user_id=message.author.id)
        state = load_state()
        await update_board(state)

        if result.get("improved"):
            await message.reply(
                f"Test score logged → **{game_mode_label(mode)}** · "
                f"**{player}** · `{score:,}`\n"
                "Check the live leaderboard message above.",
                mention_author=False,
            )
        else:
            await message.reply(
                f"No improvement for **{player}** on **{game_mode_label(mode)}** "
                f"(best stays `{result.get('best', 0):,}`).",
                mention_author=False,
            )
        return

    await message.reply(
        f"Unknown command `{action}`. Try `!sa help`.",
        mention_author=False,
    )


# ---------------------------------------------------------------------------
# Score intake
# ---------------------------------------------------------------------------

async def handle_score_submission(message: discord.Message) -> None:
    state = load_state()
    print(
        f"Score candidate msg={message.id} author={message.author} "
        f"embeds={len(message.embeds or [])} "
        f"attachments={len(message.attachments or [])} "
        f"snapshots={len(getattr(message, 'message_snapshots', None) or [])} "
        f"ref={bool(message.reference)} active={bool(state.get('active'))}"
    )

    if not state.get("active"):
        try:
            await message.add_reaction("💤")
            await message.reply(
                "No Surprise Attack is live right now.\n"
                "An operator needs to run `!sa start` first.",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return

    if (
        bot_has_reaction(message, "✅")
        or bot_has_reaction(message, "❌")
        or bot_has_reaction(message, "❓")
        or bot_has_reaction(message, "⚠️")
    ):
        return

    # Screenshots need Gemini — react so the player knows why it's quiet
    has_image = any(
        ((a.content_type or "").startswith("image/")
         or (a.filename or "").lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")))
        for a in _iter_message_attachments(message)
    )
    has_embed = bool(list(_iter_message_embeds(message))) or bool(message.reference)

    if has_image and not has_embed and not gemini_client:
        try:
            await message.add_reaction("⚠️")
            await message.reply(
                "Screenshot received, but OCR is not configured "
                "(`GEMINI_API_KEY` missing on the bot host).\n"
                "Forward the in-game **Discord → Submit Score** embed instead, "
                "or ask an admin to set the Gemini key.",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return

    # Show the bot is working (OCR can take a few seconds)
    try:
        await message.add_reaction("⏳")
    except discord.HTTPException:
        pass

    data = await extract_score_data(message)
    if not data or not data.get("score"):
        try:
            await message.add_reaction("❓")
            await message.reply(
                "Could not read a score from that post.\n"
                "• Full **scoreboard screenshot** (score + mode visible), or\n"
                "• **Forward** the game Discord → Submit Score embed\n"
                f"Event song filter: {song_line(state)}",
                mention_author=False,
            )
        except discord.HTTPException:
            pass
        return

    player = data.get("playerName") or "Unknown"
    if player == "Unknown" or is_game_footer(player):
        data["playerName"] = parse_player_name(
            getattr(message.author, "global_name", None)
            or message.author.display_name
            or message.author.name
        )

    result = record_score(state, data, discord_user_id=message.author.id)

    if result.get("rejected"):
        try:
            await message.add_reaction("❌")
            await message.reply(result.get("reason") or "Score rejected.", mention_author=False)
        except discord.HTTPException:
            pass
        return

    mode = result["mode"]
    mode_label = game_mode_label(mode)
    diff_label = difficulty_label(result.get("difficulty") or "normal")

    try:
        await message.add_reaction("✅")
        if result.get("improved"):
            prev = result.get("previous")
            if prev is not None:
                text = (
                    f"**{result['player']}** · {mode_label} · {diff_label}\n"
                    f"`{prev:,}` → **`{result['score']:,}`** (new PB for this event)"
                )
            else:
                text = (
                    f"**{result['player']}** · {mode_label} · {diff_label}\n"
                    f"**`{result['score']:,}`** logged"
                )
        else:
            text = (
                f"**{result['player']}** · {mode_label} · {diff_label}\n"
                f"`{result['score']:,}` submitted — best stays **`{result['best']:,}`**"
            )
        await message.reply(text, mention_author=False)
    except discord.HTTPException as e:
        print(f"Score reply failed: {e}")

    if result.get("improved"):
        state = load_state()
        await update_board(state)


# ---------------------------------------------------------------------------
# Discord events
# ---------------------------------------------------------------------------

@client.event
async def on_ready():
    print(f"Surprise Attack bot logged in as {client.user} (id {client.user.id})")
    print(f"BOT_VERSION={BOT_VERSION}")
    guilds = list(client.guilds)
    if guilds:
        print(f"Servers ({len(guilds)}):")
        for g in guilds:
            print(f"  - {g.name} ({g.id})")
    else:
        print("WARNING: Bot is not in any server.")

    if SA_CHANNEL_ID:
        ch = await get_sa_channel()
        if ch:
            print(f"Watching #{ch.name} ({SA_CHANNEL_ID}) in {ch.guild.name}")
        else:
            print(f"WARNING: cannot access SA_CHANNEL_ID={SA_CHANNEL_ID}")
    else:
        print("WARNING: SA_CHANNEL_ID not set")

    if SA_OPERATOR_ROLE_IDS:
        print(f"Operator role IDs: {sorted(SA_OPERATOR_ROLE_IDS)}")
    else:
        print("Operator roles: none set — Manage Server / Admin can run !sa")

    if gemini_client:
        print("Screenshot OCR: ready")
    else:
        print("Screenshot OCR: disabled (set GEMINI_API_KEY to enable)")

    state = load_state()
    if state.get("active"):
        print(f"Resuming live event {state.get('event_id')}")
        if state.get("scheduled_end_at"):
            print(f"  take-down scheduled at unix {state['scheduled_end_at']}")
        asyncio.create_task(update_board(state))
    elif state.get("scheduled_start_at"):
        print(
            f"Pending put-up at unix {state['scheduled_start_at']} "
            f"song={state.get('song_title')!r}"
        )

    # Background put-up / take-down timers (survives redeploys via sa_state.json)
    global _scheduler_started
    if not _scheduler_started:
        _scheduler_started = True
        asyncio.create_task(scheduler_loop())


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    if not is_in_sa_channel(message.channel):
        return

    cmd = parse_sa_command(message.content or "")
    if cmd:
        await handle_sa_command(message, cmd)
        return

    if not looks_like_score_candidate(message):
        content = (message.content or "").strip()
        if content and not content.startswith("!"):
            print(f"Ignored non-score message {message.id}: {content[:80]!r}")
        return

    state = load_state()
    # Redirect scores posted in the main channel into the event thread
    if state.get("active") and state.get("submit_thread_id"):
        if not is_score_submit_location(message.channel, state):
            try:
                await message.add_reaction("🧵")
                await message.reply(
                    f"Post scores in the event thread: <#{state['submit_thread_id']}>",
                    mention_author=False,
                )
            except discord.HTTPException:
                pass
            return

    await handle_score_submission(message)


# ---------------------------------------------------------------------------
# Render / hosting health check
# ---------------------------------------------------------------------------

async def health_check(_request: web.Request) -> web.Response:
    """Render health probe — free-tier web services need a listening $PORT."""
    return web.Response(text="ok")


async def start_health_server() -> web.AppRunner | None:
    """Listen on $PORT when deployed (Render sets this automatically)."""
    port_raw = (os.getenv("PORT") or "").strip()
    if not port_raw:
        return None

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port_raw))
    await site.start()
    print(f"Health check server listening on 0.0.0.0:{port_raw}")
    return runner


async def run_bot() -> None:
    health_runner = await start_health_server()
    try:
        async with client:
            await client.start(DISCORD_TOKEN)
    finally:
        if health_runner is not None:
            await health_runner.cleanup()


def main() -> None:
    if DISCORD_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Set DISCORD_TOKEN in .env")
        sys.exit(1)
    if not SA_CHANNEL_ID:
        print("ERROR: Set SA_CHANNEL_ID in .env")
        print("Discord → Settings → Advanced → Developer Mode ON")
        print("Right-click the channel → Copy Channel ID")
        sys.exit(1)

    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Surprise Attack Discord bot (standalone).

One live channel per event:
  - Players forward game score embeds OR post scoreboard screenshots
  - Bot auto-sorts Arcade / Classic / Fusion
  - One live leaderboard message with three mode sections

Operator commands (role or Manage Server):
  !sa start [Song - Artist]
  !sa end
  !sa status
  !sa board
  !sa help
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


def build_board_embeds(state: dict) -> list[discord.Embed]:
    status = "LIVE" if state.get("active") else "CLOSED"
    color = 0x22C55E if state.get("active") else 0x64748B
    header = discord.Embed(
        title=f"{BOARD_MARKER}",
        description=(
            f"**Status:** {status}\n"
            f"**Song:** {song_line(state)}\n"
            f"**Event:** `{state.get('event_id') or '—'}`\n"
            f"_Updated <t:{int(time.time())}:R>_"
        ),
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

    if action in ("help", "?", "commands"):
        await message.reply(
            "**Surprise Attack bot**\n"
            "```\n"
            "!sa start [Song - Artist]   open a new event\n"
            "!sa end                     close event + freeze board\n"
            "!sa status                  what's live + which channel\n"
            "!sa where                   server/channel this bot is bound to\n"
            "!sa board                   refresh the leaderboard post\n"
            "!sa fake <mode> <score> [name]   test leaderboard (operator)\n"
            "!sa clear [bot|all] [limit] clear channel messages (operator)\n"
            "!sa help                    this message\n"
            "```\n"
            "On `!sa start` the bot opens a **scores thread** — players post embeds/screenshots there.\n"
            "Leaderboard stays in the main channel (Arcade / Classic / Fusion).\n"
            "Use `!sa fake` on a test server to fill the board without the game.\n\n"
            "Bot needs **Create Public Threads** (+ Manage Threads to lock on end).\n"
            "_Channel IDs are per-server — test and live always use different `.env` values._",
            mention_author=False,
        )
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
        if not state.get("active"):
            lines.append("No Surprise Attack is live right now.")
            lines.append("Operators: `!sa start Song - Artist`")
            await message.reply("\n".join(lines), mention_author=False)
            return
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
                "Run `!sa end` first.",
                mention_author=False,
            )
            return

        title, artist = parse_title_artist(rest) if rest else (None, None)
        state = empty_state()
        state["active"] = True
        state["event_id"] = new_event_id()
        state["song_title"] = title
        state["song_artist"] = artist
        state["started_at"] = int(time.time())
        state["channel_id"] = SA_CHANNEL_ID
        state["scores"] = {m: {} for m in MODES}
        save_state(state)

        channel = await get_sa_channel()
        ann = None
        thread = None
        if channel:
            try:
                # Placeholder announce first so we can attach a thread, then edit with link
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

            await update_board(state, force_new=True)

        thread_note = (
            f"Players post scores in <#{thread.id}>"
            if thread
            else "⚠️ Could not create scores thread — scores accepted in this channel "
            "(grant **Create Public Threads** and restart event)."
        )
        await message.reply(
            f"Surprise Attack **started** `{state['event_id']}`\n"
            f"Song: {song_line(state)}\n"
            f"{thread_note}\n"
            "Leaderboard stays in this channel.",
            mention_author=False,
        )
        return

    if action in ("end", "close", "stop"):
        if not state.get("active"):
            await message.reply("No active event to close.", mention_author=False)
            return

        state["active"] = False
        state["ended_at"] = int(time.time())
        path = archive_event(state)
        save_state(state)
        await update_board(state)
        await close_submit_thread(state)

        # Final standings summary
        lines = [f"**Surprise Attack ended** `{state.get('event_id')}`", f"Song: {song_line(state)}"]
        for mode in MODES:
            rows = ranked_mode_scores(state, mode, limit=3)
            if rows:
                top = ", ".join(
                    f"{r['player_name']} `{int(r['score']):,}`" for r in rows
                )
                lines.append(f"{MODE_LABELS[mode]}: {top}")
            else:
                lines.append(f"{MODE_LABELS[mode]}: _no scores_")
        if path:
            lines.append(f"_Archived to `{path.name}`_")

        await message.reply("\n".join(lines), mention_author=False)
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
        asyncio.create_task(update_board(state))


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

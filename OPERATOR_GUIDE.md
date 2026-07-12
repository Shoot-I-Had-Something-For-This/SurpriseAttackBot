# Surprise Attack — Operator Guide

This is the day-to-day guide for running events.  
You do **not** need to create Arcade / Classic / Fusion channels. The bot does that split for you.

---

## What you manage

| You do | Bot does |
|--------|----------|
| Start / end each event | Sorts scores into Arcade, Classic, Fusion |
| Announce the song (in `!sa start`) | Updates one live leaderboard |
| Keep the channel clear of spam | Archives results when you end |

**Per event you only touch one channel.**

---

## Channel layout (keep it simple)

Recommended:

```
📂 Surprise Attack
  #sa-howto      ← pin the player rules (optional)
  #sa-live       ← ONLY channel the bot watches
```

Do **not** make:

- `#sa-arcade-submit`, `#sa-classic-submit`, …
- separate leaderboard channels per mode

Those multiply fast if you run multiple events per day.

---

## Commands

Type these in **`#sa-live`**.

### Start an event

```
!sa start Song Name - Artist
```

Examples:

```
!sa start Danger - Shotty Horroh
!sa start Enter Sandman
!sa start
```

- With a song name: only matching scores count (best-effort title match).
- Without a song: any song is accepted (useful for testing).

The bot posts:

1. A “LIVE” announcement in the main channel  
2. A **scores thread** (players post embeds/screenshots **only there**)  
3. A leaderboard in the main channel: **Arcade · Classic · Fusion**

If someone posts a score in the main channel, the bot points them at the thread.

### End an event

```
!sa end
```

- Freezes the board  
- Posts top 3 per mode  
- Saves a result file on the bot host (`history/sa-….json`)

### Check status

```
!sa status
```

### Refresh the board

```
!sa board
```

### Clear the channel (or a thread)

Run these **in the channel/thread** you want cleaned:

```
!sa clear
!sa clear bot 100
!sa clear all 100
```

| Command | What it deletes |
|---------|-----------------|
| `!sa clear` | Bot messages only (safe default) |
| `!sa clear all` | Everyone’s recent messages |
| `… 50` | Optional limit (max 200) |

Discord cannot bulk-delete messages older than **14 days**. For those, delete by hand.

### Help

```
!sa help
```

---

## Who can run commands

- Anyone with **Manage Server** (or Admin)  
- Or anyone with the **SA Operator** role (if that was configured)

Players do **not** need a special role to submit scores.

---

## What players do

1. Play the event song in **Arcade**, **Classic**, or **Fusion**  
2. Results → **Discord → Submit Score**  
3. **Forward** that message into `#sa-live`  
4. Wait for ✅  

The bot reads the mode from the score and puts them on the right board.

| Reaction | Meaning |
|----------|---------|
| ✅ | Score logged (or already known PB) |
| ❌ | Rejected (usually wrong song) |

---

## Running several events in one day

For each event:

1. `!sa start Song - Artist`  
2. Let people play  
3. `!sa end`  
4. Repeat  

You never need new channels for each event or each mode.

---

## Tips

- Start the event **before** people submit. Scores before `!sa start` are ignored.  
- If the board looks stale: `!sa board`  
- If someone submits the wrong song, they get ❌ and a short reason.  
- Keep `#sa-live` for scores + bot commands only; chat can go in another channel.

---

## Handoff note (if you take this from someone’s test server)

The bot was probably tested on **another** Discord server. That server’s channel ID is **wrong for you**.

1. Create (or pick) **your** live channel, e.g. `#sa-live`.  
2. Developer Mode ON → right-click that channel → **Copy Channel ID**.  
3. In `.env` on the machine running the bot, set:

   ```env
   SA_CHANNEL_ID=paste_your_channel_id_here
   DISCORD_TOKEN=...
   ```

4. Restart the bot.  
5. In **your** channel, run `!sa where`.  
   - ✅ “matches” → you’re good  
   - ❌ does not match → ID is still the test server’s; fix `.env` and restart  

6. Optional: set `SA_OPERATOR_ROLE_IDS` to **your** operator role ID (not the test server’s).  

You do **not** need their test channel to keep working. Point the bot at your server and go.

Full technical setup: see `SETUP.md` and `README.md`.

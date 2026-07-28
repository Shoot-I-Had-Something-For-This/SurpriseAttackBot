# Surprise Attack Bot

Standalone Discord bot for **Surprise Attack** events.

- **Not** the Indies-DB score bot — separate app, token, and channel  
- **One** submission channel  
- **One** live leaderboard with **Arcade / Classic / Fusion**  
- Operators start/end events with simple commands  

Designed so you can try it on a **test server**, then hand the whole folder + operator guide to someone else if they like it.

---

## Why this shape

Surprise Attack can run **1–4 times per day**.  
If each event had 3 submission areas + 3 leaderboards, four events = **24 surfaces**.

This bot keeps:

```
1 event  →  1 channel  →  1 board (3 mode sections inside)
```

Mode is a **data field**, not a channel tree.

---

## Quick start

1. Follow **[SETUP.md](SETUP.md)** (test server + bot invite + `.env`)  
2. Run:

```powershell
pip install -r requirements.txt
python surprise_attack_bot.py
```

3. In `#sa-live`:

```
!sa start Your Song - Artist on hardcore
```

4. Day-to-day use: **[OPERATOR_GUIDE.md](OPERATOR_GUIDE.md)**  
5. Pin for players: **[PLAYER_BLURB.md](PLAYER_BLURB.md)**  
6. Host 24/7 on Render (control from phone): **[RENDER.md](RENDER.md)**

---

## Commands

| Command | Who | What |
|---------|-----|------|
| `!sa start [Song - Artist]` | Operator | Open event + post board |
| `!sa start … on hardcore` | Operator | Lock event to one difficulty (rejects others) |
| `!sa start … for 1h` | Operator | Start now, auto take-down |
| `!sa start … in 30m` | Operator | Schedule put-up |
| `!sa start … in 30m for 1h` | Operator | Delayed start + duration |
| `!sa end` | Operator | Close, summarize, archive |
| `!sa end in 45m` | Operator | Schedule take-down |
| `!sa cancel` | Operator | Cancel pending put-up / take-down timer |
| `!sa status` | Anyone | Live / scheduled + timers |
| `!sa board` | Operator | Refresh board message |
| `!sa help` | Anyone | Help text |

Operators = **Manage Server** / Admin, or roles listed in `SA_OPERATOR_ROLE_IDS`.

---

## Files

| File | Purpose |
|------|---------|
| `surprise_attack_bot.py` | The bot |
| `sa_state.json` | Current event (created at runtime) |
| `history/` | Closed event snapshots |
| `.env` | Secrets (never commit) |
| `OPERATOR_GUIDE.md` | Hand this to the person running events |
| `SETUP.md` | One-time install / handoff checklist |
| `PLAYER_BLURB.md` | Discord pin text |

---

## Handoff checklist (short)

Test server channel IDs are **not** production. Always re-point `.env`.

- [ ] Works on **your** test server (`!sa where` says match)  
- [ ] She can run `!sa start` / `!sa end` there (demo)  
- [ ] Players get ✅ and land on the right mode board  
- [ ] She has `OPERATOR_GUIDE.md`  
- [ ] Bot invited to **her** server; `#sa-live` created there  
- [ ] **Her** channel ID written to `SA_CHANNEL_ID` (not yours)  
- [ ] `!sa where` on **her** server says match  
- [ ] Token / hosting access transferred (or recreated under her account)  

---

## Notes

- Scores are stored **locally** on the bot host (`sa_state.json` + `history/`). Discord still works with no cloud DB.  
- **Optional website:** set `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` on the bot host → live events and scores also push to Supabase for the Vercel leaderboard. See **[WEBSITE.md](WEBSITE.md)**.  
- Fusion is supported as a third mode. If the game embed says something unexpected, the bot defaults to Classic and logs it.  
- Song filter is best-effort title matching when you pass a song to `!sa start`.
- Difficulty lock is optional: `on easy|normal|hard|extreme|hardcore` (also `difficulty …`, `diff …`, `@ …`). Omit = any difficulty.
- Operators: `!sa web` force-pushes the current event/board to the site; `!sa status` shows website sync ON/OFF.

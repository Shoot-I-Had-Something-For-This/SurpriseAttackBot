# Surprise Attack bot — Test server setup

Use this once on your test Discord server. When she’s happy with it, hand her the folder + token ownership (or create a fresh bot app for her).

---

## 1. Create the Discord bot application

1. Open [Discord Developer Portal](https://discord.com/developers/applications)  
2. **New Application** → name it e.g. `Surprise Attack`  
3. **Bot** → **Reset Token** → copy token (this goes in `.env`)  
4. Enable **Message Content Intent** (Bot → Privileged Gateway Intents) — **required**  
5. Server Members Intent: leave **off** (not needed for this bot)

### Invite link

OAuth2 → URL Generator:

- Scopes: `bot`  
- Permissions:
  - View Channels  
  - Send Messages  
  - Embed Links  
  - Add Reactions  
  - Read Message History  
  - Manage Messages (pin the board)  
  - (optional) Mention Everyone — not required  

Open the generated URL, pick **your test server**, authorize.

---

## 2. Create channels on the test server

1. Create category: **Surprise Attack**  
2. Create text channel: **`sa-live`**  
3. (Optional) **`sa-howto`** — paste the player blurb from `PLAYER_BLURB.md` and pin it  
4. Create role: **`SA Operator`** (give it to yourself + her later)  
5. Enable **Developer Mode**: User Settings → Advanced → Developer Mode  
6. Right-click `#sa-live` → **Copy Channel ID**  
7. Right-click role **SA Operator** → **Copy Role ID**

---

## 3. Install & configure the bot

On the machine that will run the bot (your PC for testing):

```powershell
cd Desktop\SurpriseAttackBot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`:

```env
DISCORD_TOKEN=paste_token_here
SA_CHANNEL_ID=paste_sa_live_channel_id
SA_OPERATOR_ROLE_IDS=paste_operator_role_id
SA_LEADERBOARD_LIMIT=10
```

---

## 4. Run

```powershell
.\.venv\Scripts\Activate.ps1
python surprise_attack_bot.py
```

You should see:

- `Surprise Attack bot logged in as …`  
- `Watching #sa-live …`

---

## 5. Smoke test (5 minutes)

In `#sa-live`:

1. `!sa help` — bot replies  
2. `!sa start Test Song - Test Artist` — announcement + board posts  
3. Forward a real Smash Drums score embed (any mode)  
4. Expect ✅ and the correct mode section updates  
5. `!sa end` — board freezes, summary posts  

If role commands fail but you’re admin, Manage Server still works.  
If role commands fail for a non-admin operator, check `SA_OPERATOR_ROLE_IDS` and Server Members Intent.

---

## 6. Handing it off to her

**Important:** Your test server’s `SA_CHANNEL_ID` will **never** be “the right channel” for her server.  
Channel IDs are unique per server. Handoff is not “ship a finished `.env`” — it’s “ship the bot + she points `.env` at *her* `#sa-live`.”

### What transfers vs what must be re-done

| Transfers as-is | Must re-do on her server |
|-----------------|---------------------------|
| Bot code + docs | Create `#sa-live` (or pick a channel) |
| How commands work | Copy **her** channel ID → `SA_CHANNEL_ID` |
| Operator guide / player blurb | Create **her** SA Operator role → `SA_OPERATOR_ROLE_IDS` |
| Same Discord bot app *if* invited to both servers | Invite bot to **her** server |
| | Restart bot after `.env` change |

You can invite **one** bot to both servers, but it still only **listens to one** `SA_CHANNEL_ID` at a time.  
While you test, `.env` points at **your** channel. When she goes live, change `.env` to **her** channel (or run a second instance with a second `.env`).

### Checklist when she takes over

| Item | Action |
|------|--------|
| Her channel | She creates `#sa-live` → Copy Channel ID → put in `.env` as `SA_CHANNEL_ID` |
| Verify | In that channel run `!sa where` — must say it matches |
| Bot ownership | Add her to the Discord Developer app team, **or** new app under her account + new token |
| Server | Invite bot, give her **SA Operator** |
| Code folder | Zip `SurpriseAttackBot` (skip `.venv`; **don’t** hand her your test `.env` as final — only as an example) |
| Secrets | She sets her own `.env` (token + **her** channel ID) |
| Docs for her | `OPERATOR_GUIDE.md` + `PLAYER_BLURB.md` |

You can leave the **Indies score bot** completely separate. This bot does not talk to Indies-DB.

---

## 7. Production-ish hosting (optional later)

Same as any Discord bot:

- Keep process alive (Render Web Service with a tiny health endpoint can be added later, or a VPS `systemd` service)  
- Set the same env vars  
- Point `SA_CHANNEL_ID` at the **production** `#sa-live` when you leave the test server  

For the first handoff, local / test server is enough.

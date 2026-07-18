# Deploy Surprise Attack bot on Render

This runs the bot **24/7 in the cloud** so you (or she) can control it from **Discord mobile** without leaving a PC on.

Same idea as the Indies score bot: **Web Service** + tiny `/health` endpoint (not a Background Worker — those aren’t free on Render).

---

## What you’ll need

| Item | Where |
|------|--------|
| GitHub (or GitLab) account | Free |
| Render account | [render.com](https://render.com) |
| This bot folder in a **git repo** | See step 1 |
| `DISCORD_TOKEN` | Discord Developer Portal → Bot |
| `SA_CHANNEL_ID` | Right-click `#sa` → Copy Channel ID |
| `GEMINI_API_KEY` (optional) | Same key you use for screenshot OCR |

---

## 1. Put the bot in a GitHub repo

If it isn’t already in its own repo:

1. Create a **new private** GitHub repository (e.g. `surprise-attack-bot`)
2. **Do not** commit `.env` (it’s in `.gitignore`)
3. From PowerShell:

```powershell
cd $env:USERPROFILE\Desktop\SurpriseAttackBot
git init
git add surprise_attack_bot.py requirements.txt render.yaml .env.example .gitignore
git add *.md
git commit -m "Surprise Attack Discord bot"
git branch -M main
git remote add origin https://github.com/YOUR_USER/surprise-attack-bot.git
git push -u origin main
```

Replace `YOUR_USER` / repo name with yours.

---

## 2. Create the Render Web Service

1. Log in at [dashboard.render.com](https://dashboard.render.com)
2. **New +** → **Web Service**
3. Connect the GitHub repo (`surprise-attack-bot`)
4. Settings:

| Field | Value |
|--------|--------|
| **Name** | `surprise-attack-bot` (any name) |
| **Region** | Closest to you |
| **Runtime** | Python |
| **Build command** | `pip install -r requirements.txt` |
| **Start command** | `python surprise_attack_bot.py` |
| **Instance type** | Free (or paid if you want fewer sleep issues) |

5. **Health check path:** `/health`  
   (Render Settings → Health → path `/health`)

Or use the included `render.yaml` (**New +** → **Blueprint** → select repo) so build/start/health are prefilled.

---

## 3. Environment variables (Render dashboard)

**Environment** → **Add Environment Variable**:

| Key | Value | Required |
|-----|--------|----------|
| `DISCORD_TOKEN` | Bot token | **Yes** |
| `SA_CHANNEL_ID` | Channel ID for `#sa` | **Yes** |
| `GEMINI_API_KEY` | Google AI key for screenshots | Recommended |
| `SA_OPERATOR_ROLE_IDS` | Comma-separated role IDs | Optional |
| `SA_LEADERBOARD_LIMIT` | `10` | Optional |
| `PYTHON_VERSION` | `3.12.8` | Optional (Blueprint sets this) |

**Do not** paste your local `.env` file into the repo. Only set secrets in Render’s UI.

Click **Save Changes** → service will redeploy.

---

## 4. Confirm it’s online

1. Render → your service → **Logs**
2. You want lines like:

```text
Health check server listening on 0.0.0.0:....
Surprise Attack bot logged in as Surprise Attack bot#....
Servers (1):
  - Surprise Attack Test Server
Watching #sa ...
Screenshot OCR: ready
```

3. Discord: bot should show **Online**
4. In `#sa`: `!sa where` then `!sa start Test Song`  
5. **`!sa help` must show** `build 2026-07-17-difficulty-v1` (or newer `BOT_VERSION`) **and DIFFICULTY + TIMERS**. If you see `timers-v3` or no difficulty lines, Discord is still on an **old process**.  
6. A real restart usually **blips the bot offline** briefly. “Redeploy” with **no** offline blip often means the instance didn’t actually restart (or wrong service).

### Prove the live build (Lesnar / operators)

**Git is not the usual problem anymore.** As of 2026-07-17, both of these remotes serve the same `main` tip (`3948252`, `BOT_VERSION=2026-07-17-difficulty-v1`):

- `https://github.com/JStillxSKS/SurpriseAttackBot` (redirect / old URL)
- `https://github.com/Shoot-I-Had-Something-For-This/SurpriseAttackBot` (canonical)

If Discord still looks wrong, **Render is not running that commit** (or env points at the wrong channel).

#### 60-second alignment checklist

| # | Check | Pass looks like |
|---|--------|------------------|
| 1 | Render service → **Settings → Build & Deploy → Repo** | Same org/user + `SurpriseAttackBot`, branch **`main`** |
| 2 | Latest **Deploy** → commit message / SHA | Mentions difficulty lock **or** SHA starts `3948252…` |
| 3 | Deploy finished **Live** (not failed / not stuck building) | Green Live |
| 4 | **Logs** after boot | `SA BOT ONLINE  BOT_VERSION=2026-07-17-difficulty-v1` |
| 5 | Discord bot goes **offline briefly** on that deploy | Real process restart |
| 6 | In the SA channel: `!sa help` | Build string **`2026-07-17-difficulty-v1`** + difficulty examples |
| 7 | `!sa where` | ✅ matches this channel (not ❌) |

If **4** is new but **6** is old → wrong bot token / second host still online.  
If **6** is new but commands “do nothing” → **`SA_CHANNEL_ID`** wrong (channel wipe / new channel).  
If deploy never shows `3948252` → wrong GitHub repo on the service, or **Build Filters** blocked the path, or they clicked an old deploy.

#### Hard restart (when “redeploy” lied)

1. Render → service → **Suspend**  
2. Wait until Discord shows the bot **offline**  
3. **Resume** (or **Manual Deploy → Clear build cache & deploy**)  
4. Re-check Logs for `BOT_VERSION=2026-07-17-difficulty-v1`  
5. Discord: `!sa help` again  

Optional nuclear: reset Discord **bot token**, set Render `DISCORD_TOKEN` once, deploy (only **one** host should use the token).

**Deploy Hook** (Settings → Deploy Hook) is a secret URL to *trigger* a deploy from outside tools.  
**Regenerating** it only rotates that URL — it does **not** by itself load new bot code. Use Manual Deploy / Suspend-Resume for “get new code live.”

### Build Filters (optional, recommended)

Render → service → **Settings** → **Build Filters**  
*Include or ignore specific paths when determining whether to trigger an auto-deploy. Paths are relative to the repo root.*

**Why use them:** Auto-deploy only when bot code changes — not every README/docs push.

**Suggested for this repo (include):**

```text
surprise_attack_bot.py
requirements.txt
render.yaml
```

Or ignore noise (exclude), e.g. `*.md`, `history/**` if Render’s UI supports ignore rules that way.

**Caveat:** If filters are too tight, a real code change in another path won’t deploy — widen include list if you add packages. After changing filters, still **Manual Deploy** once to confirm.

---

## 5. Free tier “sleep” (important)

On **free** Render web services, the instance can **spin down** after idle HTTP traffic. When it sleeps:

- Bot goes **offline** in Discord  
- Commands and scores stop until the next request wakes it  

### Options

**A. UptimeRobot keepalive (recommended, free)** — full steps below  
**B. Paid Render instance** — always-on; no sleep  
**C. Run on a VPS / home PC** — skip Render  

---

## 5b. UptimeRobot setup (keep the free bot awake)

[UptimeRobot](https://uptimerobot.com) hits your Render `/health` URL on a schedule so the free service doesn’t sleep.

### 1. Get your Render URL

1. Render dashboard → your **surprise-attack-bot** service  
2. Copy the public URL, e.g.  
   `https://surprise-attack-bot-xxxx.onrender.com`  
3. Health URL is:

```text
https://YOUR-SERVICE-NAME.onrender.com/health
```

(You can open that link in a browser — it should show `ok` when the bot is up.)

### 2. Create a free UptimeRobot account

1. Go to [https://uptimerobot.com](https://uptimerobot.com)  
2. Sign up (free plan is enough)  
3. Confirm email if asked  

### 3. Add a monitor

1. **+ Add New Monitor**  
2. Fill in:

| Field | Value |
|--------|--------|
| **Monitor Type** | **HTTP(s)** |
| **Friendly Name** | `Surprise Attack bot` (any label) |
| **URL (or IP)** | `https://YOUR-SERVICE-NAME.onrender.com/health` |
| **Monitoring Interval** | **5 minutes** (free plan minimum is often 5 min) |

3. Leave other options default unless you care about email alerts when it’s down  
4. **Create Monitor**

### 4. Confirm it’s working

1. UptimeRobot → your monitor should go **Up** (green) within a few minutes  
2. Optional: Render → **Logs** — you may see occasional GET `/health` traffic  
3. Discord: bot should stay **Online** over the next hour instead of dropping offline  

### 5. Alerts (optional)

In the monitor settings you can enable:

- Email when **Down** / **Up**  
Useful if Render crashes or the deploy fails — you’ll know the bot is offline.

### Notes

| Topic | Detail |
|--------|--------|
| **Interval** | 5 minutes is fine for Render free sleep prevention |
| **URL must be `/health`** | Don’t use only the homepage if you prefer the explicit health path (both work on this bot: `/` and `/health` return `ok`) |
| **HTTPS** | Use `https://` (Render provides SSL) |
| **Not a substitute for a good deploy** | If env vars are wrong, UptimeRobot will show Up (HTTP works) but Discord still won’t work — check Render **Logs** |
| **Handoff** | Add her email as a UptimeRobot team member, or she creates her own monitor with the same URL |

### Alternatives to UptimeRobot

Same idea — any free HTTP ping every 5–10 minutes:

- [cron-job.org](https://cron-job.org) → GET `…/health` every 5 minutes  
- Better Stack / Freshping / etc.  

UptimeRobot is just the most common choice for Discord bots on free Render.

---

## 6. Event state across restarts

The bot stores the live event in `sa_state.json` on disk.

| Hosting | What happens |
|---------|----------------|
| **Free web (no disk)** | Redeploy/restart can **wipe** open event state |
| **Render Disk** (optional paid add-on) | State survives restarts |

### Optional persistent disk

1. Render → service → **Disks** → add disk, mount path `/var/data`
2. Environment variables:

```text
SA_STATE_PATH=/var/data/sa_state.json
SA_HISTORY_DIR=/var/data/history
```

3. Redeploy  

Without a disk: after a deploy, just run `!sa start` again if an event was wiped.

---

## 7. Stop running it on your PC

If Render is up and the bot is Online:

1. Stop the local `python surprise_attack_bot.py` process  
2. **Only one** instance should use the same token (two instances fight over the gateway)

---

## 8. Hand off to her

| Step | Action |
|------|--------|
| 1 | Invite her to the Render team **or** transfer the service |
| 2 | She uses Discord mobile: `!sa start` / `!sa end` |
| 3 | If her server is different: change `SA_CHANNEL_ID` in Render env → redeploy |
| 4 | Give her `OPERATOR_GUIDE.md` (day-to-day) + this file only if she manages hosting |

She does **not** need Python on her phone.

---

## 9. Troubleshooting

| Problem | Fix |
|---------|-----|
| Deploy fails health check | Health path must be `/health`; start command `python surprise_attack_bot.py` |
| Bot offline after a while | Free sleep — set up [UptimeRobot](#5b-uptimerobot-setup-keep-the-free-bot-awake) on `/health` or upgrade |
| `SA_CHANNEL_ID` wrong | Update env var → **Manual Deploy** |
| No screenshot OCR | Set `GEMINI_API_KEY` on Render |
| `Create Public Threads` errors | Re-check bot role perms on the Discord server |
| Logs: not in any server | Invite the bot; Code Grant must stay **OFF** |
| Two bots online / flaky | Don’t run local + Render with the same token |

---

## Quick checklist

- [ ] Code on GitHub (no `.env`)  
- [ ] Render **Web Service** (not Background Worker)  
- [ ] Build: `pip install -r requirements.txt`  
- [ ] Start: `python surprise_attack_bot.py`  
- [ ] Health: `/health`  
- [ ] Env: `DISCORD_TOKEN`, `SA_CHANNEL_ID`, optional `GEMINI_API_KEY`  
- [ ] Logs show login + watching `#sa`  
- [ ] UptimeRobot monitor on `https://…onrender.com/health` every 5 min (or paid plan)  
- [ ] Stop local bot so only Render runs  

---

## Related files

| File | Role |
|------|------|
| `surprise_attack_bot.py` | Bot + `/health` for Render |
| `render.yaml` | Optional Blueprint |
| `requirements.txt` | Includes `aiohttp` for health server |
| `OPERATOR_GUIDE.md` | Day-to-day Discord commands |
| `SETUP.md` | Local / Discord app setup |

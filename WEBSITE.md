# Surprise Attack → Vercel website

The Discord bot keeps **all existing features**.  
Website publishing is an **add-on**: when Supabase env is set on the bot host, every live event and score also appears on the Vercel leaderboard.

```
Players → Discord score posts
        → Surprise Attack bot (Render / PC)
        → Supabase (sa_events + sa_scores)
        → Vercel site (Next.js leaderboard)
```

Without Supabase env: Discord board still works; site stays empty.

---

## What the bot already does for the site

| When | What gets pushed |
|------|------------------|
| `!sa start` / put-up timer | Event shell → LIVE on site |
| Score logged (new PB) | Full board sync to Supabase |
| `!sa scan` | Board refresh + full website sync |
| `!sa board` | Board refresh + website push |
| Every ~30s while live | Periodic full sync (safety net) |
| `!sa end` / take-down | Final scores + status CLOSED |
| `!sa web` | Operator force-push |

Also available (optional extras, not required if Render has Supabase env):

| Script | Purpose |
|--------|---------|
| `website_bridge.py` | REST poll of Discord boards → Supabase (backup if bot build is old) |
| `recover_website_scores.py` | One-shot repair: pull boards + history into Supabase |
| `seed_supabase_history.py` | Seed local `history/*.json` into Supabase |

---

## 1. Supabase (shared project for you + Lara)

1. Create project (or use existing SA project).
2. SQL Editor → run:

   `surprise-attack-leaderboard/supabase/schema.sql`

   (same tables: `sa_events`, `sa_scores`, RLS public read).

3. Project Settings → API — copy:

   | Key | Where it goes |
   |-----|----------------|
   | **Project URL** | Bot host **and** Vercel |
   | **anon public** | **Vercel only** |
   | **service_role** | **Bot host only** (Render `.env`) — never Vercel |

---

## 2. Bot host (Render) env

| Key | Required |
|-----|----------|
| `DISCORD_TOKEN` | Yes |
| `SA_CHANNEL_ID` | Yes |
| `SUPABASE_URL` | For website |
| `SUPABASE_SERVICE_ROLE_KEY` | For website |
| `GEMINI_API_KEY` | Screenshots (optional) |

After deploy, prove build:

```text
GET https://YOUR-RENDER-URL/health
→ ok 2026-07-28-website-v8 supabase=ON · ….supabase.co · key set (…)
```

In Discord: `!sa help` must show build `2026-07-28-website-v8` (or newer).  
`!sa status` must show **Website sync: ON**.

If health is bare `ok` with no version → Render is on an **old** process. Clear cache & deploy, or Suspend → Resume.

---

## 3. Vercel site (joint team account)

Repo (create/use): `Shoot-I-Had-Something-For-This/SurpriseAttackLeaderboard`  
(or deploy the local `surprise-attack-leaderboard` folder).

**Environment variables (Production + Preview):**

| Key | Value |
|-----|--------|
| `NEXT_PUBLIC_SUPABASE_URL` | same project URL as bot |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | **anon** key only |

Framework: Next.js. Root: repo root. Build: default `next build`.

After deploy: open `/api/leaderboard` — should return JSON with `configured: true`.

---

## 4. Smoke test (5 minutes)

1. `!sa start Test Song - Artist on hardcore`
2. Confirm Discord LIVE board posts
3. Confirm Discord reply includes **Website: LIVE push OK**
4. Open Vercel site → status Live, song matches
5. `!sa fake classic 1234567 TestPlayer` (operator)
6. Site shows the score under Classic within ~30s (or refresh)
7. `!sa end` → site status Closed, scores remain

---

## 5. If the site is empty but Discord has scores

```powershell
cd SurpriseAttackBot
python recover_website_scores.py --close-zombie-boards
```

Or in Discord while live: `!sa web` / `!sa scan`.

---

## Security

- **service_role** = full DB write. Render only.
- **anon** = public read (RLS). Vercel only.
- Never commit `.env` / `.env.local`.

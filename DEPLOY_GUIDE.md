# DEPLOY_GUIDE.md

This file is referenced by both `config.json` and the `Dockerfile`'s comments
and never existed until now. This is that document — the actual steps to
get this project running on Render's free tier.

Read `RISK_AND_LIMITATIONS.md` first if you haven't already. This guide
assumes `dry_run: true` and `exchange.sandbox: true` stay as they are —
simulated capital, no real funds.

---

## What you're deploying

- A Freqtrade bot (`v12_Strategy.py`) running in paper-trading mode
- Its REST API, which the included dashboard (`dashboard.html`) talks to
- Everything packaged in a single Docker image

## Step 1 — Push this project to GitHub

Render deploys from a Git repository. Create a new GitHub repo and push
everything in this folder to it:

```
git init
git add .
git commit -m "Initial commit — CROWN v12 paper trading bot"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

**Before you push**, double-check `config.json` does not contain real
secrets — the placeholder values (`CHANGE_ME_BEFORE_DEPLOY`, empty
`key`/`secret`) are meant to stay as placeholders in git. Real values go
into Render's environment variables in Step 3, never into the file itself.

## Step 2 — Create a Postgres database (recommended, so trade history survives restarts)

Render's free tier can sleep and restart your service. If you skip this
step, trade history is stored in a local SQLite file that gets wiped on
every restart — you'd lose your paper-trading history without warning.
Both **Neon** and **Supabase** offer a free Postgres tier that works here.

**Using Neon** (neon.tech):
1. Sign up, create a new project.
2. On the project dashboard, copy the connection string shown — it looks
   like `postgresql://user:password@host/dbname?sslmode=require`.
3. Freqtrade needs this in SQLAlchemy's `postgresql+psycopg` form. Change
   the scheme from `postgresql://` to `postgresql+psycopg://`; keep the
   rest identical.

**Using Supabase** (supabase.com):
1. Sign up, create a new project.
2. Go to Project Settings → Database → Connection string, and copy the
   URI form.
3. Same scheme change as above: `postgresql://` → `postgresql+psycopg://`.

Keep this connection string somewhere safe — you'll paste it into Render's
dashboard in Step 3 as `DB_URL`, never into `config.json` itself.

## Step 3 — Create the Render Web Service

1. Go to the Render Dashboard → **New → Web Service**.
2. Connect the GitHub repo you pushed in Step 1.
3. Render should auto-detect the `Dockerfile` and offer a Docker runtime.
   If it asks for a build/start command instead, make sure you've selected
   "Docker" as the environment — this project has no non-Docker
   build/start commands, everything is in the `Dockerfile`/`start.sh`.
4. Choose the **Free** compute plan.
5. Under **Advanced → Environment Variables**, add:

   | Key | Value |
   |---|---|
   | `DB_URL` | The Postgres connection string from Step 2 (if you set one up) |
   | `API_USERNAME` | A username you choose for logging into the dashboard |
   | `API_PASSWORD` | A strong password you choose |
   | `JWT_SECRET` | Any long random string (see below for how to generate one) |

   To generate a random `JWT_SECRET` locally:
   ```
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
   **This must be at least 32 characters** — confirmed against Freqtrade's
   own config schema, which rejects anything shorter and will fail to
   start with a `ConfigurationError` if it's too short. The command above
   produces 64 characters, comfortably over that minimum, so just use its
   output directly rather than typing your own shorter string.

6. Click **Create Web Service**. Render will build the Docker image and
   start the container — this can take a few minutes the first time
   (the TA-Lib C-library build step is the slowest part).

## Step 4 — Confirm the port is actually reachable (a real gap, fixed here)

Render expects your service to bind to the port in the `PORT` environment
variable it sets automatically (defaulting to `10000`). This project's
`config.json` hardcodes `listen_port: 8080` instead, and neither
`start.sh` nor the `Dockerfile` currently reads Render's `$PORT` at all.

Render's own docs state it can *usually* still detect and use a
differently-bound port — so this may well work as-is — but "usually" is
not "always," and there's no reason to leave this as an unverified gamble
on your first deploy when the fix is small. **This build's `start.sh` now
overrides the port from Render's `$PORT` automatically** (see the
`PORT_ARG` block near the top of `start.sh`) — you don't need to change
anything in `config.json` yourself. If you ever move this off Render to a
host that doesn't set `$PORT`, it falls back to `config.json`'s own
`8080` value unchanged.

## Step 5 — Open the dashboard

1. Once the Render deploy finishes, find your service's URL — it looks
   like `https://your-service-name.onrender.com`.
2. Open `dashboard.html` — either host it as a Render **Static Site** from
   the same repo, or simply open the file locally in a browser (it works
   either way, since it's a single self-contained HTML file that talks
   directly to your bot's API).
3. On the login screen, enter:
   - **API URL**: your Render service URL from step 1 above
   - **Username / Password**: whatever you set as `API_USERNAME` /
     `API_PASSWORD` in Step 3

You should now see your wallet balance, P&L, open position (if any), and
recent closed trades, refreshing every 5 seconds.

## Step 6 — Watch the logs for the first few trades

Render's dashboard shows live logs for your service. Watch for:
- `[Phase2.5]` lines confirming stop-loss calculations on trade entry
- `[Phase3.5-Checkpoint]` lines ~60 seconds into any trending-regime trade
- `[Phase3.5-WidenFix]` lines if volatility rises enough mid-trade to
  widen the stop (see `RISK_AND_LIMITATIONS.md` section 3 for why this
  log line exists — it's confirming a bug fix is actually firing, not
  just documented)
- `[Phase3-TimeBomb]` lines when a trade closes on the time-based exit
  rather than a stop-loss or the strategy's own signal logic

If you see repeated errors instead of these lines, the most common causes
are: `DB_URL` malformed (double-check the `postgresql+psycopg://` scheme
change from Step 2), or the exchange API keys being invalid — though with
`sandbox: true` and empty `key`/`secret`, no real exchange credentials are
needed at all for this to run in fully simulated mode.

## Free-tier reminder

Render's free web services sleep after a period of inactivity and wake on
the next incoming request, which can take on the order of a minute. This
means:
- The dashboard's first load after a period of inactivity may show a
  connection error briefly while the service wakes up — refresh after
  it does.
- The bot itself is not continuously running while the service sleeps,
  meaning it will not be evaluating new candles or trades during that
  window. If you need truly continuous operation, that requires Render's
  paid tier or an external uptime-ping service to keep it awake — neither
  is set up by default in this project.

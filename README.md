# FBF Stats Hub — local V1

A private Discord OAuth2 website for the existing FBF Stats Bot database. This is a **read-only** stats site: it does not change matches, players, or seasons. All protected routes verify the user's FBF guild membership using Discord OAuth2. It uses `identify guilds` scopes and does not need the bot token.

## Windows setup

1. Extract this folder into `E:\FBF Bot\FBF_Stats_Hub` (keep your existing `bot.py`, `divisions.py`, and `fbf_stats.sqlite3` untouched).
2. Open the Discord Developer Portal → your application → OAuth2 → Redirects. Add **exactly** `http://127.0.0.1:5000/callback` and save.
3. Copy `.env.example` to `.env`. Set `DISCORD_CLIENT_SECRET` to the application's secret **only in your local .env**. Never post or commit it. Set `SESSION_SECRET` to a random 64-character hex string, e.g. generate one with `py -c "import secrets; print(secrets.token_hex(32))"`. Verify `FBF_DATABASE` points to your real database file.
4. Open a Command Prompt in `E:\FBF Bot\FBF_Stats_Hub` and run:
   ```bat
   py -m pip install -r requirements.txt
   py app.py
   ```
5. Open `http://127.0.0.1:5000` in a browser. Sign in with Discord. Members of FBF server `1382141392071163955` should see the dashboard; nonmembers are denied.
6. Keep `py app.py` running while testing. You can run the Discord bot in a separate terminal. The website reads the same SQLite file.

## Security and deployment notes

- **Do not push `.env` or your SQLite database to GitHub**. `.gitignore` is included.
- This is a **local development build**, not yet a production deployment. Do not expose Flask's development server to the internet.
- In production, use HTTPS, `COOKIE_SECURE=true`, a production WSGI server, managed secret environment variables, and a deployed database strategy. A remote website cannot read a SQLite database on your Windows computer without synchronization or hosting changes.
- Discord OAuth access tokens are stored in local server-side session files (not browser cookies). For production, migrate the local filesystem session store to a managed server-side session store such as Redis, and keep it private. The login checks guild membership on every protected page request and fails closed if Discord is unavailable.
- Site pages currently include Dashboard, Leaderboard, Teams/Rosters, and Match Detail. Staff write actions and player-specific profiles are future work.
- If you get a database missing error, fix `FBF_DATABASE`. If you get `no such table: team_divisions`, make sure your current bot has run its divisions migration and you're pointing at the correct database.

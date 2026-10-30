
import os
import secrets
import sqlite3
import tempfile
import hmac
import threading
from pathlib import Path
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import Flask, redirect, render_template, request, session, url_for, abort
from dotenv import load_dotenv
from flask_session import Session

load_dotenv()

BASE = Path(__file__).resolve().parent
app = Flask(__name__)

app.secret_key = os.environ.get("SESSION_SECRET", "")
if len(app.secret_key) < 32:
    raise RuntimeError("SESSION_SECRET must be at least 32 characters.")

app.config.update(
    SESSION_TYPE="filesystem",
    SESSION_FILE_DIR=str(BASE / ".sessions"),
    SESSION_PERMANENT=True,
    SESSION_USE_SIGNER=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "COOKIE_SECURE",
        "true" if os.environ.get("RENDER") else "false"
    ).lower() == "true",
    PERMANENT_SESSION_LIFETIME=3600,
)
Session(app)

CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "1565174387320029345")
CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
GUILD_ID = os.environ.get("FBF_GUILD_ID") or os.environ.get(
    "DISCORD_GUILD_ID", "1382141392071163955"
)
REDIRECT_URI = os.environ.get(
    "DISCORD_REDIRECT_URI", "http://127.0.0.1:5000/callback"
)

DB = Path(os.environ.get(
    "FBF_DATABASE",
    str(BASE / "data" / "fbf_stats.sqlite3")
    if os.environ.get("RENDER")
    else str(BASE.parent / "fbf_stats.sqlite3")
)).resolve()

SYNC_TOKEN = os.environ.get("FBF_SYNC_TOKEN", "").strip()
SYNC_LOCK = threading.Lock()
API = "https://discord.com/api/v10"


@app.post("/internal/sync-database")
def sync_database():
    supplied = request.headers.get("X-FBF-Sync-Token", "").strip()

    if len(SYNC_TOKEN) < 32 or not hmac.compare_digest(supplied, SYNC_TOKEN):
        app.logger.warning("FBF sync rejected: authentication failed")
        abort(403, description="Invalid sync credentials.")

    limit = 50 * 1024 * 1024

    if request.content_length is None or request.content_length > limit:
        abort(413)

    if request.mimetype != "application/octet-stream":
        abort(415)

    with SYNC_LOCK:
        DB.parent.mkdir(parents=True, exist_ok=True)
        fd, tmpname = tempfile.mkstemp(
            prefix=".fbf-snapshot-",
            suffix=".sqlite3",
            dir=DB.parent
        )

        try:
            with os.fdopen(fd, "wb") as out:
                total = 0

                while True:
                    block = request.stream.read(1024 * 1024)
                    if not block:
                        break

                    total += len(block)
                    if total > limit:
                        abort(413)

                    out.write(block)

            con = sqlite3.connect(
                Path(tmpname).as_uri() + "?mode=ro", uri=True
            )
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    abort(400, description="Invalid SQLite snapshot")

                required = {
                    "teams", "players", "matches",
                    "game_stats", "config", "team_divisions"
                }
                actual = {
                    row[0] for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not required.issubset(actual):
                    abort(400, description="Snapshot missing required FBF tables")

                players = con.execute(
                    "SELECT COUNT(*) FROM players"
                ).fetchone()[0]
            finally:
                con.close()

            os.replace(tmpname, DB)
        finally:
            if os.path.exists(tmpname):
                os.unlink(tmpname)

    return {"ok": True, "message": f"Snapshot updated ({players} players)"}


def db():
    if not DB.is_file():
        abort(503, description="FBF stats have not been synced yet.")
    conn = sqlite3.connect(
        f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=5
    )
    conn.row_factory = sqlite3.Row
    return conn


def query(sql, args=()):
    conn = db()
    try:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def current_season():
    result = query("SELECT value FROM config WHERE key='season'")
    return result[0]["value"] if result else "Season 1"


def discord_get(path, token):
    try:
        response = requests.get(
            API + path,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def member_check():
    token = session.get("access_token")
    if not token:
        return False

    guilds = discord_get("/users/@me/guilds", token)
    if guilds is None:
        return False

    return any(str(g.get("id")) == GUILD_ID for g in guilds)


def protected(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("index"))

        if not member_check():
            session.clear()
            return render_template("denied.html"), 403

        return fn(*args, **kwargs)
    return wrapper


@app.route("/")
def index():
    if not session.get("user"):
        return render_template("login.html")
    return redirect(url_for("dashboard"))


@app.route("/login")
def login():
    if not CLIENT_SECRET:
        abort(503, description="Discord OAuth is not configured.")

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
        "prompt": "consent"
    }
    return redirect("https://discord.com/oauth2/authorize?" + urlencode(params))


@app.route("/callback")
def callback():
    state = session.pop("oauth_state", None)

    if not state or not secrets.compare_digest(
        state, request.args.get("state", "")
    ):
        abort(400, description="Invalid OAuth state.")

    code = request.args.get("code")
    if not code:
        abort(400, description="Discord did not return an authorization code.")

    try:
        response = requests.post(
            API + "/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI
            },
            timeout=12
        )
        response.raise_for_status()
        token = response.json()["access_token"]
    except (requests.RequestException, KeyError):
        abort(502, description="Discord login failed.")

    user = discord_get("/users/@me", token)
    guilds = discord_get("/users/@me/guilds", token)

    if not user or guilds is None:
        abort(502, description="Could not verify Discord account.")

    if not any(str(g.get("id")) == GUILD_ID for g in guilds):
        session.clear()
        return render_template("denied.html"), 403

    session.clear()
    session.permanent = True
    session["access_token"] = token
    session["user"] = {
        "id": user["id"],
        "name": user.get("global_name") or user["username"]
    }
    return redirect(url_for("dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
@protected
def dashboard():
    season = current_season()
    overview = query("""
        SELECT
            (SELECT COUNT(*) FROM teams) teams,
            (SELECT COUNT(*) FROM players) players,
            (SELECT COUNT(*) FROM matches
             WHERE verified=1 AND season=?) matches
    """, (season,))[0]

    recent = query("""
        SELECT m.id,m.season,m.phase,m.blue_wins,m.orange_wins,
               b.name blue,o.name orange
        FROM matches m
        JOIN teams b ON b.id=m.blue_id
        JOIN teams o ON o.id=m.orange_id
        WHERE m.verified=1
        ORDER BY m.id DESC LIMIT 8
    """)

    return render_template(
        "dashboard.html", season=season, overview=overview, recent=recent
    )


@app.route("/leaderboard")
@protected
def leaderboard():
    season = request.args.get("season", current_season())[:60]
    division = request.args.get("division", "")[:50]

    seasons = query("SELECT DISTINCT season FROM matches ORDER BY season DESC")
    divisions = query("""
        SELECT DISTINCT division FROM matches
        WHERE division IS NOT NULL AND division != ''
        ORDER BY division
    """)

    rows = query("""
        SELECT p.id,p.name,COUNT(*) games,
               SUM(g.goals) goals,SUM(g.assists) assists,
               SUM(g.saves) saves,SUM(g.shots) shots,SUM(g.score) score
        FROM game_stats g
        JOIN players p ON p.id=g.player_id
        JOIN matches m ON m.id=g.match_id
        WHERE m.verified=1 AND m.season=?
          AND (?='' OR m.division=?)
        GROUP BY g.player_id
        ORDER BY goals DESC,score DESC LIMIT 100
    """, (season, division, division))

    return render_template(
        "leaderboard.html",
        rows=rows, season=season, division=division,
        seasons=seasons, divisions=divisions
    )


@app.route("/teams")
@protected
def teams():
    season = current_season()

    rows = query("""
        SELECT t.id,t.name,d.division,COUNT(p.id) roster
        FROM teams t
        LEFT JOIN players p ON p.team_id=t.id
        LEFT JOIN team_divisions d
          ON d.team_id=t.id AND d.season=?
        GROUP BY t.id
        ORDER BY t.name
    """, (season,))

    return render_template("teams.html", rows=rows, season=season)


@app.route("/teams/<int:team_id>")
@protected
def team_detail(team_id):
    found = query("SELECT id,name FROM teams WHERE id=?", (team_id,))
    if not found:
        abort(404)

    players = query(
        "SELECT name FROM players WHERE team_id=? ORDER BY name", (team_id,)
    )

    matches = query("""
        SELECT m.id,m.season,m.blue_wins,m.orange_wins,
               b.name blue,o.name orange
        FROM matches m
        JOIN teams b ON b.id=m.blue_id
        JOIN teams o ON o.id=m.orange_id
        WHERE m.verified=1 AND (m.blue_id=? OR m.orange_id=?)
        ORDER BY m.id DESC LIMIT 20
    """, (team_id, team_id))

    return render_template(
        "team.html", team=found[0], players=players, matches=matches
    )


@app.route("/matches/<int:match_id>")
@protected
def match_detail(match_id):
    matches = query("""
        SELECT m.*,b.name blue,o.name orange
        FROM matches m
        JOIN teams b ON b.id=m.blue_id
        JOIN teams o ON o.id=m.orange_id
        WHERE m.id=? AND m.verified=1
    """, (match_id,))

    if not matches:
        abort(404)

    rows = query("""
        SELECT g.game,p.name player,t.name team,
               g.goals,g.assists,g.saves,g.shots,g.score
        FROM game_stats g
        JOIN players p ON p.id=g.player_id
        JOIN teams t ON t.id=g.team_id
        WHERE g.match_id=?
        ORDER BY g.game,g.score DESC
    """, (match_id,))

    return render_template(
        "match.html", match=matches[0], rows=rows
    )


@app.route("/players")
@protected
def players_directory():
    search = request.args.get("q", "").strip()[:80]
    season = current_season()

    rows = query("""
        SELECT p.id,p.name,t.name AS team,
               COUNT(CASE WHEN m.verified=1 AND m.season=?
                          THEN 1 END) AS games,
               COALESCE(SUM(CASE WHEN m.verified=1 AND m.season=?
                          THEN g.goals ELSE 0 END),0) AS goals,
               COALESCE(SUM(CASE WHEN m.verified=1 AND m.season=?
                          THEN g.assists ELSE 0 END),0) AS assists
        FROM players p
        LEFT JOIN teams t ON t.id=p.team_id
        LEFT JOIN game_stats g ON g.player_id=p.id
        LEFT JOIN matches m ON m.id=g.match_id
        WHERE (?='' OR p.name LIKE ?)
        GROUP BY p.id,p.name,t.name
        ORDER BY p.name COLLATE NOCASE LIMIT 250
    """, (season, season, season, search, "%" + search + "%"))

    return render_template(
        "players.html", rows=rows, search=search, season=season
    )


@app.route("/players/<int:player_id>")
@protected
def player_detail(player_id):
    found = query("""
        SELECT p.id,p.name,p.team_id,t.name AS team
        FROM players p
        LEFT JOIN teams t ON t.id=p.team_id
        WHERE p.id=?
    """, (player_id,))

    if not found:
        abort(404)

    player = found[0]
    season = request.args.get("season", current_season())[:60]

    seasons = query("""
        SELECT DISTINCT m.season
        FROM matches m
        JOIN game_stats g ON g.match_id=m.id
        WHERE g.player_id=? AND m.verified=1
        ORDER BY m.season DESC
    """, (player_id,))

    stats = query("""
        SELECT COUNT(*) AS games,
               COALESCE(SUM(g.goals),0) AS goals,
               COALESCE(SUM(g.assists),0) AS assists,
               COALESCE(SUM(g.saves),0) AS saves,
               COALESCE(SUM(g.shots),0) AS shots,
               COALESCE(SUM(g.score),0) AS score,
               COALESCE(ROUND(AVG(g.score),1),0) AS avg_score
        FROM game_stats g
        JOIN matches m ON m.id=g.match_id
        WHERE g.player_id=? AND m.verified=1 AND m.season=?
    """, (player_id, season))[0]

    career = query("""
        SELECT COUNT(*) AS games,
               COALESCE(SUM(g.goals),0) AS goals,
               COALESCE(SUM(g.assists),0) AS assists,
               COALESCE(SUM(g.saves),0) AS saves
        FROM game_stats g
        JOIN matches m ON m.id=g.match_id
        WHERE g.player_id=? AND m.verified=1
    """, (player_id,))[0]

    recent = query("""
        SELECT DISTINCT m.id,m.season,m.phase,
               m.blue_wins,m.orange_wins,
               b.name AS blue,o.name AS orange
        FROM matches m
        JOIN game_stats g ON g.match_id=m.id
        JOIN teams b ON b.id=m.blue_id
        JOIN teams o ON o.id=m.orange_id
        WHERE g.player_id=? AND m.verified=1
        ORDER BY m.id DESC LIMIT 12
    """, (player_id,))

    return render_template(
        "player.html",
        player=player, stats=stats, career=career,
        season=season, seasons=seasons, recent=recent
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)

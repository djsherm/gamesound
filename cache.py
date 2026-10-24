"""SQLite cache: remembers which soundtrack albums you confirmed for each game.

Two tables:
  albums   - metadata for every album we've shown you (so saved choices render
             instantly, with no Spotify call)
  choices  - game -> ordered list of album ids you ticked on the review page
"""
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("GAMESOUND_DB", Path(__file__).parent / "gamesound.db"))
LASTFM_CACHE_TTL = 7 * 24 * 60 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    artist       TEXT,
    year         TEXT,
    total_tracks INTEGER,
    image        TEXT
);
CREATE TABLE IF NOT EXISTS choices (
    game_key   TEXT PRIMARY KEY,
    game       TEXT NOT NULL,
    album_ids  TEXT NOT NULL,   -- JSON list, in the order they should be added
    track_ids  TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
    game_key   TEXT PRIMARY KEY,   -- what you typed, normalized
    canonical  TEXT,               -- RAWG's title; NULL = looked up, no confident match
    year       INTEGER,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_playlists (
    id         TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lastfm_stats (
    track_key  TEXT PRIMARY KEY,
    stats      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with _db() as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(choices)")}
        if "track_ids" not in columns:
            conn.execute("ALTER TABLE choices ADD COLUMN track_ids TEXT NOT NULL DEFAULT '{}' ")


def game_key(title: str) -> str:
    """Normalize so 'The Witcher 3' and 'the witcher 3 ' hit the same entry."""
    return " ".join(re.findall(r"[a-z0-9]+", title.lower()))


def save_albums(albums: list[dict]) -> None:
    rows = [
        (a["id"], a["name"], a.get("artist"), a.get("year"), a.get("total_tracks"), a.get("image"))
        for a in albums
    ]
    if not rows:
        return
    with _db() as conn:
        conn.executemany(
            """INSERT INTO albums (id, name, artist, year, total_tracks, image)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, artist=excluded.artist, year=excluded.year,
                 total_tracks=excluded.total_tracks, image=excluded.image""",
            rows,
        )


def get_choice(game: str) -> list[dict] | None:
    """Saved albums for this game (in saved order), or None if nothing is saved."""
    with _db() as conn:
        row = conn.execute(
            "SELECT album_ids FROM choices WHERE game_key = ?", (game_key(game),)
        ).fetchone()
        if not row:
            return None
        albums = []
        for album_id in json.loads(row["album_ids"]):
            a = conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
            if a:
                albums.append(dict(a))
    return albums or None


def save_choice(game: str, album_ids: list[str]) -> None:
    save_choice_with_tracks(game, album_ids)


def get_track_choices(game: str) -> dict[str, list[str]]:
    with _db() as conn:
        row = conn.execute(
            "SELECT track_ids FROM choices WHERE game_key = ?", (game_key(game),)
        ).fetchone()
    return json.loads(row["track_ids"]) if row else {}


def save_choice_with_tracks(
    game: str, album_ids: list[str], track_choices: dict[str, list[str]] | None = None
) -> None:
    with _db() as conn:
        row = conn.execute(
            "SELECT track_ids FROM choices WHERE game_key = ?", (game_key(game),)
        ).fetchone()
        previous = json.loads(row["track_ids"]) if row else {}
        saved_tracks = {album_id: previous[album_id] for album_id in album_ids if album_id in previous}
        for album_id, track_ids in (track_choices or {}).items():
            if album_id in album_ids:
                saved_tracks[album_id] = track_ids
        conn.execute(
            """INSERT INTO choices (game_key, game, album_ids, track_ids, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(game_key) DO UPDATE SET
                 game=excluded.game, album_ids=excluded.album_ids,
                 track_ids=excluded.track_ids, updated_at=excluded.updated_at""",
            (game_key(game), game, json.dumps(album_ids), json.dumps(saved_tracks), time.time()),
        )


def save_pending(payload: dict) -> str:
    pending_id = secrets.token_urlsafe(18)
    now = time.time()
    with _db() as conn:
        conn.execute("DELETE FROM pending_playlists WHERE created_at < ?", (now - 86400,))
        conn.execute(
            "INSERT INTO pending_playlists (id, payload, created_at) VALUES (?, ?, ?)",
            (pending_id, json.dumps(payload), now),
        )
    return pending_id


def consume_pending(pending_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT payload FROM pending_playlists WHERE id = ?", (pending_id,)
        ).fetchone()
        if row:
            conn.execute("DELETE FROM pending_playlists WHERE id = ?", (pending_id,))
    return json.loads(row["payload"]) if row else None


def _lastfm_track_key(artist: str, track: str) -> str:
    artist_key = " ".join(re.findall(r"[^\W_]+", artist.casefold(), flags=re.UNICODE))
    track_key = " ".join(re.findall(r"[^\W_]+", track.casefold(), flags=re.UNICODE))
    return f"{artist_key}\x1f{track_key}"


def get_lastfm_stats(artist: str, track: str) -> tuple[bool, dict | None]:
    with _db() as conn:
        row = conn.execute(
            "SELECT stats, updated_at FROM lastfm_stats WHERE track_key = ?",
            (_lastfm_track_key(artist, track),),
        ).fetchone()
    if row is None or time.time() - row["updated_at"] > LASTFM_CACHE_TTL:
        return False, None
    return True, json.loads(row["stats"])


def save_lastfm_stats(artist: str, track: str, stats: dict | None) -> None:
    with _db() as conn:
        conn.execute(
            """INSERT INTO lastfm_stats (track_key, stats, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(track_key) DO UPDATE SET
                 stats=excluded.stats, updated_at=excluded.updated_at""",
            (_lastfm_track_key(artist, track), json.dumps(stats), time.time()),
        )


def delete_choice(game: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM choices WHERE game_key = ?", (game_key(game),))


def get_game(game: str) -> dict | None:
    """Cached RAWG lookup: {'name', 'year'} (name is None for a cached miss), or None if never looked up."""
    with _db() as conn:
        row = conn.execute(
            "SELECT canonical, year FROM games WHERE game_key = ?", (game_key(game),)
        ).fetchone()
    return None if row is None else {"name": row["canonical"], "year": row["year"]}


def save_game(game: str, info: dict | None) -> None:
    """Remember a RAWG result, or a confirmed miss (info=None) so we don't ask again."""
    with _db() as conn:
        conn.execute(
            """INSERT INTO games (game_key, canonical, year, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(game_key) DO UPDATE SET
                 canonical=excluded.canonical, year=excluded.year, updated_at=excluded.updated_at""",
            (game_key(game), info["name"] if info else None, info.get("year") if info else None, time.time()),
        )

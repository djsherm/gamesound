"""SQLite cache: remembers which soundtrack albums you confirmed for each game.

Two tables:
  albums   - metadata for every album we've shown you (so saved choices render
             instantly, with no Spotify call)
  choices  - game -> ordered list of album ids you ticked on the review page
"""
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("GAMESOUND_DB", Path(__file__).parent / "gamesound.db"))

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
    with _db() as conn:
        conn.execute(
            """INSERT INTO choices (game_key, game, album_ids, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(game_key) DO UPDATE SET
                 game=excluded.game, album_ids=excluded.album_ids, updated_at=excluded.updated_at""",
            (game_key(game), game, json.dumps(album_ids), time.time()),
        )


def delete_choice(game: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM choices WHERE game_key = ?", (game_key(game),))

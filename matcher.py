"""Turn a game title into ranked candidate soundtrack albums on Spotify."""
import asyncio
import re

import spotify_client as sp

GOOD_WORDS = ("soundtrack", "ost", "original score", "game score", "original game", "game music")
BAD_WORDS = ("remix", "cover", "tribute", "karaoke", "lofi", "lo-fi", "lullaby",
             "piano collection", "for babies")
FILLER = {"the", "a", "an", "of", "and", "original", "game", "music", "from", "edition",
          "deluxe", "vol", "volume", "official", "soundtrack", "ost", "score", "video"}

AUTO_SELECT_THRESHOLD = 60  # preselect the top candidate at or above this score


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def score(game: str, album_name: str) -> int:
    """Heuristic 0-100: how likely is this album the soundtrack for this game?"""
    g_tokens = _tokens(game) - {"the", "a", "an", "of"}
    a_tokens = _tokens(album_name)
    if not g_tokens:
        return 0

    coverage = len(g_tokens & a_tokens) / len(g_tokens)
    s = coverage * 60

    if _norm(game) in _norm(album_name):
        s += 15

    lowered = album_name.lower()
    if any(w in lowered for w in GOOD_WORDS):
        s += 25
    if any(w in lowered for w in BAD_WORDS):
        s -= 35

    extra = len(a_tokens - g_tokens - FILLER)  # words unrelated to the game title
    s -= min(15, extra * 3)

    return max(0, min(100, round(s)))


async def _match_one(game: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            albums = await sp.search_albums(f"{game} soundtrack")
        except Exception as exc:  # keep one failure from sinking the batch
            return {"game": game, "candidates": [], "selected_id": None, "error": str(exc)}

    for a in albums:
        a["score"] = score(game, a["name"])
    albums.sort(key=lambda a: a["score"], reverse=True)
    top = albums[:3]

    selected = top[0]["id"] if top and top[0]["score"] >= AUTO_SELECT_THRESHOLD else None
    return {"game": game, "candidates": top, "selected_id": selected, "error": None}


async def match_games(games: list[str]) -> list[dict]:
    sem = asyncio.Semaphore(4)  # be polite to the API
    return await asyncio.gather(*(_match_one(g, sem) for g in games))
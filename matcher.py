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


def score(game: str, album_name: str, album_year=None, game_year: int | None = None) -> int:
    """Heuristic 0-100: how likely is this album the soundtrack for this game?

    album_year / game_year are optional; when both are known, an album released
    within two years of the game gets a small bonus.
    """
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

    if game_year and str(album_year or "").isdigit() and abs(int(album_year) - game_year) <= 2:
        s += 5

    # An album named exactly like the game ("Minecraft") is very often the
    # soundtrack itself. Floor its score so it beats partial title matches,
    # while an explicit "(Original Soundtrack)" album (100) still ranks above it.
    if _norm(album_name) == _norm(game):
        s = max(s, 80)

    return max(0, min(100, round(s)))


async def _match_one(game: str, sem: asyncio.Semaphore, info: dict | None = None) -> dict:
    # `info` is the RAWG lookup ({'name', 'year'}) when available. We search with
    # the canonical title, and also with what you typed if it differs, in case
    # RAWG picked the wrong game.
    canon = info["name"] if info else game
    game_year = info.get("year") if info else None

    # "<title> soundtrack" finds the usual OSTs; the bare title finds albums named
    # exactly like the game, which the first query can miss.
    queries = [f"{canon} soundtrack", canon]
    if _norm(game) != _norm(canon):
        queries.append(game)

    async with sem:
        batches = await asyncio.gather(
            *(sp.search_albums(q) for q in queries),
            return_exceptions=True,  # keep one failure from sinking the game
        )
    ok = [b for b in batches if not isinstance(b, Exception)]
    if not ok:
        return {"game": game, "candidates": [], "selected_ids": [], "error": str(batches[0])}

    albums, seen_ids = [], set()
    for batch in ok:
        for a in batch:
            if a["id"] not in seen_ids:
                seen_ids.add(a["id"])
                albums.append(a)

    names = {game, canon}  # score against what you typed and the canonical title; keep the better
    for a in albums:
        a["score"] = max(score(n, a["name"], a.get("year"), game_year) for n in names)
    albums.sort(key=lambda a: a["score"], reverse=True)
    top = albums[:5]  # show a few more: multi-part soundtracks often rank lower

    selected = [top[0]["id"]] if top and top[0]["score"] >= AUTO_SELECT_THRESHOLD else []
    return {"game": game, "candidates": top, "selected_ids": selected, "error": None,
            "canonical": canon if info else None, "year": game_year}


async def match_games(games: list[str], resolved: dict[str, dict] | None = None) -> list[dict]:
    """`resolved` maps each typed title to its RAWG info ({'name', 'year'}); optional."""
    resolved = resolved or {}
    sem = asyncio.Semaphore(4)  # be polite to the API
    return await asyncio.gather(*(_match_one(g, sem, resolved.get(g)) for g in games))

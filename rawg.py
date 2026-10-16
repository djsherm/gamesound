"""Optional game-name normalization through RAWG (https://rawg.io/apidocs).

Turns what you typed ("witcher 3") into RAWG's canonical title and release year
("The Witcher 3: Wild Hunt", 2015). Needs RAWG_API_KEY; without it the app
simply uses the titles exactly as typed.
"""
import os
import re

import httpx

API = "https://api.rawg.io/api/games"
STOP = {"the", "a", "an", "of"}
MIN_COVERAGE = 0.6  # share of your typed words that must appear in RAWG's title


def enabled() -> bool:
    return bool(os.environ.get("RAWG_API_KEY"))


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - STOP


async def _fetch(title: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(API, params={
            "key": os.environ["RAWG_API_KEY"],
            "search": title,
            "page_size": 8,
            "exclude_additions": "true",  # skip DLC/expansions where RAWG supports it
        })
        r.raise_for_status()
        return r.json().get("results", [])


def pick(title: str, results: list[dict]) -> dict | None:
    """Choose RAWG's best match for what was typed, or None if nothing is convincing.

    A result must contain at least MIN_COVERAGE of the typed words. Among those,
    prefer more coverage, then fewer unrelated extra words (so "Hades" beats
    "Hades II"), then RAWG's popularity count, then RAWG's own ordering.
    """
    typed = _tokens(title)
    if not typed:
        return None
    best, best_key = None, None
    for rank, r in enumerate(results):
        name = r.get("name") or ""
        words = _tokens(name)
        if not words:
            continue
        coverage = len(typed & words) / len(typed)
        if coverage < MIN_COVERAGE:
            continue
        key = (-coverage, len(words - typed), -(r.get("added") or 0), rank)
        if best_key is None or key < best_key:
            best, best_key = r, key
    if best is None:
        return None
    year = (best.get("released") or "")[:4]
    return {"name": best["name"], "year": int(year) if year.isdigit() else None, "id": best.get("id")}


async def resolve(title: str) -> dict | None:
    """Look the title up on RAWG. Raises on network/API errors; returns None for no match."""
    return pick(title, await _fetch(title))

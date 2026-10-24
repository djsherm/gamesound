import asyncio
import os
import secrets
import time
from html import escape
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from starlette.middleware.sessions import SessionMiddleware

import cache
import matcher
import rawg
import spotify_client as sp

load_dotenv()
cache.init()

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.environ["SESSION_SECRET"])
env = Environment(loader=FileSystemLoader(Path(__file__).parent), autoescape=True)


def render(template: str, **ctx) -> HTMLResponse:
    return HTMLResponse(env.get_template(template).render(**ctx))


@app.get("/")
async def index():
    return render("index.html", rawg_enabled=rawg.enabled())


async def _resolve_names(games: list[str], fresh: bool) -> dict[str, dict]:
    """Typed title -> {'name', 'year'} for games RAWG recognised.

    Empty when RAWG isn't configured. Lookups (including confirmed misses) are
    cached; lookup failures are not, so they are retried next time.
    """
    if not rawg.enabled():
        return {}
    sem = asyncio.Semaphore(4)

    async def one(g: str):
        if not fresh:
            saved = cache.get_game(g)
            if saved is not None:
                return g, (saved if saved["name"] else None)
        async with sem:
            try:
                info = await rawg.resolve(g)
            except Exception as exc:  # keep going with the typed title
                print(f"RAWG lookup failed for {g!r}: {exc}")
                return g, None
        cache.save_game(g, info)
        return g, info

    return {g: info for g, info in await asyncio.gather(*(one(g) for g in games)) if info}


@app.post("/match")
async def match(titles: str = Form(...), fresh: str = Form("")):
    """HTMX endpoint: returns the review form fragment."""
    seen, games = set(), []
    for line in titles.splitlines():
        t = line.strip()
        key = cache.game_key(t)
        if t and key not in seen:
            seen.add(key)
            games.append(t)
    games = games[:50]

    # Games you've already confirmed skip the Spotify search entirely.
    remembered = {}
    if not fresh:
        for g in games:
            saved = cache.get_choice(g)
            if saved:
                remembered[g] = saved

    to_search = [g for g in games if g not in remembered]
    resolved = await _resolve_names(to_search, bool(fresh))
    searched = {r["game"]: r for r in await matcher.match_games(to_search, resolved)}
    for r in searched.values():
        cache.save_albums(r["candidates"])  # so a later "create" can remember them

    results = []
    for g in games:
        if g in remembered:
            albums = remembered[g]
            track_choices = cache.get_track_choices(g)
            for a in albums:
                a["score"] = None  # not scored; these came from your saved choice
                a["track_selection_saved"] = a["id"] in track_choices
                a["selected_track_ids"] = track_choices.get(a["id"], [])
            results.append({"game": g, "candidates": albums,
                            "selected_ids": [a["id"] for a in albums],
                            "error": None, "remembered": True})
        else:
            results.append(searched[g])
    return render("review.html", results=results)


@app.get("/search")
async def search(q: str = "", gi: int = 0):
    """HTMX endpoint: free-text album search for one game on the review page.

    Returns checkbox rows that get appended to that game's list, so anything
    already ticked is kept. The query is sent to Spotify as typed.
    """
    q = q.strip()
    if not q:
        return HTMLResponse("")
    try:
        albums = await sp.search_albums(q)
    except Exception as exc:
        return HTMLResponse(f'<p class="warn">Search failed: {escape(str(exc))}</p>')
    if not albums:
        return HTMLResponse(f'<p class="meta">No albums found for "{escape(q)}".</p>')
    for a in albums:
        a["score"] = None  # not scored against the game; you're choosing by hand
    cache.save_albums(albums)  # so a later "create" can remember what you tick
    return render("_candidates.html", gi=gi, candidates=albums, selected_ids=[])


@app.get("/album-tracks/{album_id}")
async def album_tracks(album_id: str, gi: int = 0, saved: bool = False, selected: str = ""):
    """Load an album's ordered track list for optional per-track selection."""
    try:
        tracks = await sp.get_album_tracks(album_id)
    except Exception as exc:
        return HTMLResponse(f'<p class="warn">Could not load tracks: {escape(str(exc))}</p>')
    return render(
        "_tracks.html", gi=gi, album_id=album_id, tracks=tracks,
        saved=saved, selected_ids=selected.split(",") if selected else [],
    )


@app.post("/create")
async def create(request: Request):
    """Plain form post (not HTMX) so the Spotify login redirect works."""
    form = await request.form()
    album_ids, seen, track_choices = [], set(), {}
    for key, game in form.multi_items():
        if not key.startswith("game_"):
            continue
        gi = key[len("game_"):]
        picks = [v for v in form.getlist("pick_" + gi) if v]
        game_track_choices = {}
        saved_track_choices = cache.get_track_choices(game)
        for album_id in picks:
            if form.get("track_selection_" + album_id) is not None:
                selected_tracks = [v for v in form.getlist("track_pick_" + album_id) if v]
                game_track_choices[album_id] = selected_tracks
                track_choices[album_id] = selected_tracks
            elif album_id in saved_track_choices:
                track_choices.setdefault(album_id, saved_track_choices[album_id])
        # Remember exactly what was submitted: ticked albums are saved,
        # a game with everything unticked is forgotten.
        if picks:
            cache.save_choice_with_tracks(game, picks, game_track_choices)
        else:
            cache.delete_choice(game)
        for v in picks:
            if v not in seen:
                seen.add(v)
                album_ids.append(v)
    if not album_ids:
        return RedirectResponse("/", status_code=303)

    pending_id = cache.save_pending({
        "albums": [
            {"id": album_id, **({"track_ids": track_choices[album_id]} if album_id in track_choices else {})}
            for album_id in album_ids
        ],
        "name": (form.get("playlist_name") or "Games I've played").strip(),
    })
    request.session["pending_id"] = pending_id
    token = request.session.get("token")
    if token and time.time() < token["expires_at"]:
        return await _finish(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/login")
async def login(request: Request):
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    return RedirectResponse(sp.build_auth_url(state))


@app.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code or state != request.session.pop("oauth_state", None):
        return HTMLResponse("Spotify login failed or was cancelled. <a href='/'>Start over</a>", status_code=400)
    request.session["token"] = await sp.exchange_code(code)
    if "pending_id" not in request.session:
        return RedirectResponse("/")
    return await _finish(request)


async def _finish(request: Request):
    pending_id = request.session.pop("pending_id", None)
    pending = cache.consume_pending(pending_id) if pending_id else None
    if not pending:
        return RedirectResponse("/", status_code=303)
    try:
        result = await sp.create_playlist(
            request.session["token"]["access_token"], pending["name"], pending["albums"]
        )
    except httpx.HTTPStatusError as exc:
        # Show Spotify's own explanation instead of a blank 500.
        detail = exc.response.text[:500]
        print(f"Spotify error {exc.response.status_code} on {exc.request.url}: {detail}")
        return HTMLResponse(
            f"<h1>Spotify returned {exc.response.status_code}</h1>"
            f"<p><code>{exc.request.method} {exc.request.url}</code></p>"
            f"<pre>{detail}</pre><p><a href='/'>Start over</a></p>",
            status_code=502,
        )
    return render("done.html", name=pending["name"], **result)

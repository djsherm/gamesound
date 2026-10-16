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
    return render("index.html")


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

    searched = {r["game"]: r for r in await matcher.match_games([g for g in games if g not in remembered])}
    for r in searched.values():
        cache.save_albums(r["candidates"])  # so a later "create" can remember them

    results = []
    for g in games:
        if g in remembered:
            albums = remembered[g]
            for a in albums:
                a["score"] = None  # not scored; these came from your saved choice
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


@app.post("/create")
async def create(request: Request):
    """Plain form post (not HTMX) so the Spotify login redirect works."""
    form = await request.form()
    album_ids, seen = [], set()
    for key, game in form.multi_items():
        if not key.startswith("game_"):
            continue
        picks = [v for v in form.getlist("pick_" + key[len("game_"):]) if v]
        # Remember exactly what was submitted: ticked albums are saved,
        # a game with everything unticked is forgotten.
        if picks:
            cache.save_choice(game, picks)
        else:
            cache.delete_choice(game)
        for v in picks:
            if v not in seen:
                seen.add(v)
                album_ids.append(v)
    if not album_ids:
        return RedirectResponse("/", status_code=303)

    request.session["pending"] = {
        "ids": album_ids,
        "name": (form.get("playlist_name") or "Games I've played").strip(),
    }
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
    if "pending" not in request.session:
        return RedirectResponse("/")
    return await _finish(request)


async def _finish(request: Request):
    pending = request.session.pop("pending")
    try:
        result = await sp.create_playlist(
            request.session["token"]["access_token"], pending["name"], pending["ids"]
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

import os
import secrets
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader
from starlette.middleware.sessions import SessionMiddleware

import matcher
import spotify_client as sp

load_dotenv()

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.environ["SESSION_SECRET"])
env = Environment(loader=FileSystemLoader(Path(__file__).parent), autoescape=True)


def render(template: str, **ctx) -> HTMLResponse:
    return HTMLResponse(env.get_template(template).render(**ctx))


@app.get("/")
async def index():
    return render("index.html")


@app.post("/match")
async def match(titles: str = Form(...)):
    """HTMX endpoint: returns the review form fragment."""
    seen, games = set(), []
    for line in titles.splitlines():
        t = line.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            games.append(t)
    games = games[:50]
    results = await matcher.match_games(games)
    return render("review.html", results=results)


@app.post("/create")
async def create(request: Request):
    """Plain form post (not HTMX) so the Spotify login redirect works."""
    form = await request.form()
    album_ids, seen = [], set()
    for key, value in form.multi_items():
        if key.startswith("pick_") and value and value not in seen:
            seen.add(value)
            album_ids.append(value)
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

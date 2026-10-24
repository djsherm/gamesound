"""Thin async wrapper around the parts of the Spotify Web API we need."""
import asyncio
import base64
import os
import time
from urllib.parse import urlencode

import httpx

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
SCOPES = "playlist-modify-public playlist-modify-private"

_app_token = {"value": None, "expires": 0.0}


def _cfg():
    return (
        os.environ["SPOTIFY_CLIENT_ID"],
        os.environ["SPOTIFY_CLIENT_SECRET"],
        os.environ["SPOTIFY_REDIRECT_URI"],
    )


def _basic_header() -> str:
    cid, secret, _ = _cfg()
    return "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()


# ---------- auth ----------

def build_auth_url(state: str) -> str:
    cid, _, redirect = _cfg()
    params = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": SCOPES,
        "state": state,
    }
    return f"{ACCOUNTS}/authorize?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """Authorization-code exchange. Returns {'access_token', 'expires_at'}."""
    _, _, redirect = _cfg()
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{ACCOUNTS}/api/token",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect},
            headers={"Authorization": _basic_header()},
        )
        r.raise_for_status()
        data = r.json()
    return {
        "access_token": data["access_token"],
        "expires_at": time.time() + data["expires_in"] - 30,
    }


async def _get_app_token(client: httpx.AsyncClient) -> str:
    """Client-credentials token, used for searching (no user login needed)."""
    if _app_token["value"] and time.time() < _app_token["expires"]:
        return _app_token["value"]
    r = await client.post(
        f"{ACCOUNTS}/api/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": _basic_header()},
    )
    r.raise_for_status()
    data = r.json()
    _app_token["value"] = data["access_token"]
    _app_token["expires"] = time.time() + data["expires_in"] - 30
    return _app_token["value"]


# ---------- request helper with basic 429 handling ----------

async def _request(client, method, url, token, **kwargs) -> httpx.Response:
    for attempt in range(4):
        r = await client.request(
            method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if r.status_code == 429:
            await asyncio.sleep(int(r.headers.get("Retry-After", "1")) + 1)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


# ---------- search ----------

async def search_albums(query: str, limit: int = 10) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        token = await _get_app_token(client)
        r = await _request(
            client, "GET", f"{API}/search", token,
            params={"q": query, "type": "album", "limit": limit},
        )
    items = r.json().get("albums", {}).get("items", [])
    albums = []
    for a in items:
        if not a:
            continue
        images = a.get("images") or []
        albums.append({
            "id": a["id"],
            "name": a["name"],
            "artist": ", ".join(x["name"] for x in a.get("artists", [])),
            "year": (a.get("release_date") or "")[:4],
            "total_tracks": a.get("total_tracks", 0),
            "image": images[-1]["url"] if images else None,  # smallest image
        })
    return albums


async def get_album_tracks(album_id: str) -> list[dict]:
    """Return an album's tracks in album order, with popularity when available."""
    async with httpx.AsyncClient(timeout=20) as client:
        token = await _get_app_token(client)
        url = f"{API}/albums/{album_id}/tracks"
        params = {"limit": 50}
        items = []
        while url:
            data = (await _request(client, "GET", url, token, params=params)).json()
            items.extend(data["items"])
            url, params = data.get("next"), None

        popularity = {}
        for start in range(0, len(items), 50):
            ids = [track["id"] for track in items[start:start + 50] if track.get("id")]
            if not ids:
                continue
            try:
                data = (await _request(
                    client, "GET", f"{API}/tracks", token, params={"ids": ",".join(ids)}
                )).json()
            except httpx.HTTPError as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else str(exc)
                print(f"Spotify track popularity unavailable: {status}")
                break
            popularity.update({
                track["id"]: track.get("popularity")
                for track in data.get("tracks", []) if track
            })

    ordered = sorted(
        enumerate(items),
        key=lambda pair: (pair[1].get("disc_number", 1), pair[1].get("track_number", 0), pair[0]),
    )
    tracks = []
    for _, track in ordered:
        tracks.append({
            "id": track["id"],
            "name": track["name"],
            "artist": ", ".join(artist["name"] for artist in track.get("artists", [])),
            "disc_number": track.get("disc_number", 1),
            "track_number": track.get("track_number", 0),
            "duration": f"{track.get('duration_ms', 0) // 60000}:{(track.get('duration_ms', 0) // 1000) % 60:02}",
            "popularity": popularity.get(track["id"]),
            "url": track.get("external_urls", {}).get("spotify"),
        })
    return tracks


# ---------- playlist creation ----------

async def create_playlist(user_token: str, name: str, albums: list) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        # Explicit track selections arrive in album order from the review form.
        uris: list[str] = []
        for album in albums:
            if isinstance(album, dict):
                album_id = album["id"]
                track_ids = album.get("track_ids")
            else:
                album_id = album
                track_ids = None
            if track_ids is not None:
                uris.extend(f"spotify:track:{track_id}" for track_id in track_ids)
                continue

            url = f"{API}/albums/{album_id}/tracks"
            params = {"limit": 50}
            while url:
                data = (await _request(client, "GET", url, user_token, params=params)).json()
                uris.extend(t["uri"] for t in data["items"])
                url, params = data.get("next"), None  # 'next' already has the query string

        playlist = (await _request(
            client, "POST", f"{API}/me/playlists", user_token,  # Feb 2026: replaces /users/{id}/playlists
            json={
                "name": name,
                "public": False,
                "description": "Soundtracks from games I've played",
            },
        )).json()

        for i in range(0, len(uris), 100):  # API limit: 100 tracks per request
            await _request(
                client, "POST", f"{API}/playlists/{playlist['id']}/items", user_token,  # Feb 2026: replaces /tracks
                json={"uris": uris[i:i + 100]},
            )

    return {"url": playlist["external_urls"]["spotify"], "track_count": len(uris)}

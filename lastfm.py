"""Optional Last.fm scrobble lookup for album tracks."""
import asyncio
import os

import httpx

import cache

API = "https://ws.audioscrobbler.com/2.0/"


def enabled() -> bool:
    key = os.environ.get("LASTFM_API_KEY", "").strip()
    return bool(key and key != "your_api_key_here")


async def _track_stats(client: httpx.AsyncClient, artist: str, title: str) -> dict | None:
    response = await client.get(API, params={
        "method": "track.getInfo",
        "artist": artist,
        "track": title,
        "api_key": os.environ["LASTFM_API_KEY"],
        "format": "json",
    })
    response.raise_for_status()
    payload = response.json()
    if payload.get("error") == 6:
        return None
    if payload.get("error"):
        raise RuntimeError(f"Last.fm API error {payload['error']}")

    track = payload.get("track")
    if not track:
        raise RuntimeError("Last.fm returned no track data")
    return {
        "playcount": int(track.get("playcount") or 0),
        "listeners": int(track.get("listeners") or 0),
    }


async def enrich_tracks(tracks: list[dict]) -> list[dict]:
    """Add cached Last.fm play/listener counts without failing the track list."""
    if not enabled():
        return tracks

    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=12) as client:
        async def enrich(track: dict) -> None:
            artist = track.get("lastfm_artist") or track.get("artist", "")
            title = track.get("name", "")
            if not artist or not title:
                track["lastfm"] = None
                return

            cached, stats = cache.get_lastfm_stats(artist, title)
            if cached:
                track["lastfm"] = stats
                return

            async with semaphore:
                try:
                    stats = await _track_stats(client, artist, title)
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    print(f"Last.fm lookup failed for {artist!r} - {title!r}: {exc}")
                    track["lastfm"] = None
                    return
            cache.save_lastfm_stats(artist, title, stats)
            track["lastfm"] = stats

        await asyncio.gather(*(enrich(track) for track in tracks))
    return tracks

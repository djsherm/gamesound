# Game Soundtrack Playlist

Paste a list of games, review the matched soundtrack albums, and create a Spotify playlist.

## Setup

1. Create an app at https://developer.spotify.com/dashboard and add `http://127.0.0.1:8000/callback` as a Redirect URI.
2. Copy `.env.example` to `.env` and fill in the client ID, secret and a session secret.
3. Create a virtual environment:

```
python3 -m venv .venv
```

4. Activate it. On macOS/Linux:

```
source .venv/bin/activate
```

On Windows PowerShell:

```
.venv\Scripts\Activate.ps1
```

5. Install the dependencies and start the app:

```
pip install -r requirements.txt
uvicorn main:app --reload
```

6. Open http://127.0.0.1:8000

## How it works

- `matcher.py` searches Spotify for `"<game> soundtrack"` and scores results (title overlap, soundtrack/OST keywords, penalties for remixes/covers/tributes). The top candidate is preselected only if it scores 60+.
- `rawg.py` (optional) normalizes titles: "witcher 3" becomes "The Witcher 3: Wild Hunt" (2015). Get a free key at https://rawg.io/apidocs and put it in `.env` as `RAWG_API_KEY`; without it titles are used as typed. The canonical name is used for the Spotify searches and scoring (we also still search what you typed if it differs), and a release-year match gives albums a small bonus. Lookups are cached in SQLite, including "no match", and "Ignore saved matches" re-checks them. RAWG's free tier is for non-commercial use, allows 20,000 requests per month, and requires a link back to RAWG (the page shows one when the key is set). A title only resolves if RAWG's name contains most of your typed words, so abbreviations like "BOTW" fall back to the typed title.
- Each game gets two searches (`"<game> soundtrack"` and the bare title) so albums named exactly like the game are found too; an exact-name album scores at least 80.
- Under every game on the review page is a search box for albums the rules can't find (e.g. "Minecraft Volume Alpha"). Results are appended to that game's list, unticked, and your existing ticks are kept.
- Use an album's "Choose tracks" option to select songs in disc and track order. If you leave the list unopened, the whole album is added; custom selections are remembered with the album choice. Set `LASTFM_API_KEY` in `.env` to show Last.fm community scrobbles and listeners when a track match is available. These are not Spotify stream counts. Lookups, including misses, are cached for seven days and limited to four concurrent requests. Without Last.fm stats, the list shows Spotify's deprecated 0-100 popularity score when available.
- `spotify_client.py` wraps search (client-credentials token) and playlist creation (user OAuth, selected tracks added in batches of 100).
- `main.py` serves the HTMX UI. The Spotify login only happens when you click "Create playlist".
- `cache.py` keeps a local SQLite file (`gamesound.db`, git-ignored). When you click "Create playlist", the albums you ticked are remembered per game, and next time those games skip the Spotify search and show your saved choice. Untick everything for a game to forget it, or tick "Ignore saved matches" to search fresh. Set `GAMESOUND_DB` to move the file.
- Playlists are created private; change `public` in `spotify_client.create_playlist` if you want otherwise.

## Notes

- Spotify apps start in development mode, which limits them to users you add in the dashboard. That's fine for personal use.
- Many game soundtracks aren't on Spotify. Those show up as "No soundtrack found".
- If playlist creation returns a 403/404, check Spotify's current developer docs, since their endpoints and access rules change.

## Future Work
- Docker hosting
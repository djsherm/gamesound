# Game Soundtrack Playlist

Paste a list of games, review the matched soundtrack albums, and create a Spotify playlist.

## Setup

1. Create an app at https://developer.spotify.com/dashboard and add `http://127.0.0.1:8000/callback` as a Redirect URI.
2. Copy `.env.example` to `.env` and fill in the client ID, secret and a session secret.
3. Install and run:

```
pip install -r requirements.txt
uvicorn main:app --reload
```

4. Open http://127.0.0.1:8000

## How it works

- `matcher.py` searches Spotify for `"<game> soundtrack"` and scores results (title overlap, soundtrack/OST keywords, penalties for remixes/covers/tributes). The top candidate is preselected only if it scores 60+.
- `spotify_client.py` wraps search (client-credentials token) and playlist creation (user OAuth, tracks added in batches of 100).
- `main.py` serves the HTMX UI. The Spotify login only happens when you click "Create playlist".
- `cache.py` keeps a local SQLite file (`gamesound.db`, git-ignored). When you click "Create playlist", the albums you ticked are remembered per game, and next time those games skip the Spotify search and show your saved choice. Untick everything for a game to forget it, or tick "Ignore saved matches" to search fresh. Set `GAMESOUND_DB` to move the file.
- Playlists are created private; change `public` in `spotify_client.create_playlist` if you want otherwise.

## Notes

- Spotify apps start in development mode, which limits them to users you add in the dashboard. That's fine for personal use.
- Many game soundtracks aren't on Spotify. Those show up as "No soundtrack found".
- If playlist creation returns a 403/404, check Spotify's current developer docs, since their endpoints and access rules change.

## Next steps

- Steam library import (Steam Web API, needs a public profile).
- Canonical title lookup via IGDB/RAWG before searching.

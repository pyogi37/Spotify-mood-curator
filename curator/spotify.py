"""Thin Spotify Web API client.

Targets the February 2026 Development Mode rules:
- playlists are created with POST /me/playlists
- playlist contents live under /playlists/{id}/items, and each entry's
  track is under "item" (older responses used "track"; both are handled)
- search returns at most 10 results per request
- batch lookups (GET /tracks, /artists) are gone, so nothing here relies on them
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Iterator

import requests

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

SCOPES = [
    "user-top-read",
    "user-read-recently-played",
    "user-library-read",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
]


class SpotifyError(RuntimeError):
    pass


# ---------------------------------------------------------------- auth (PKCE)

def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login(client_id: str, redirect_uri: str, token_path: Path, open_browser: bool = True) -> dict:
    """Authorization Code + PKCE. No client secret needed.

    redirect_uri must be a loopback IP such as http://127.0.0.1:8888/callback and
    must be registered in the Spotify developer dashboard exactly as written.
    """
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "scope": " ".join(SCOPES),
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    parsed = urllib.parse.urlparse(redirect_uri)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Logged in. You can close this tab.")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 8888), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    print(f"Open this URL to log in:\n{url}\n")
    if open_browser:
        webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()

    if result.get("state") != state:
        raise SpotifyError("Login failed: state mismatch or no callback received.")
    if "error" in result:
        raise SpotifyError(f"Login failed: {result['error']}")

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }, timeout=30)
    if resp.status_code != 200:
        raise SpotifyError(f"Token exchange failed: {resp.status_code} {resp.text}")
    token = resp.json()
    token["expires_at"] = time.time() + token["expires_in"] - 60
    token["client_id"] = client_id
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(token, indent=2))
    return token


# ---------------------------------------------------------------- client

class Spotify:
    def __init__(self, token_path: Path, session: requests.Session | None = None, sleep=time.sleep):
        self.token_path = token_path
        self.session = session or requests.Session()
        self.sleep = sleep
        if not token_path.exists():
            raise SpotifyError("Not logged in. Run `curator login` first.")
        self.token = json.loads(token_path.read_text())

    # token handling
    def _refresh(self) -> None:
        resp = self.session.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": self.token["refresh_token"],
            "client_id": self.token["client_id"],
        }, timeout=30)
        if resp.status_code != 200:
            raise SpotifyError(f"Token refresh failed ({resp.status_code}). Run `curator login` again.")
        new = resp.json()
        self.token.update(new)
        self.token["expires_at"] = time.time() + new["expires_in"] - 60
        self.token_path.write_text(json.dumps(self.token, indent=2))

    def request(self, method: str, path: str, *, params=None, body=None, retries: int = 5) -> Any:
        if time.time() >= self.token.get("expires_at", 0):
            self._refresh()
        url = path if path.startswith("http") else f"{API}{path}"
        for attempt in range(retries):
            resp = self.session.request(
                method, url, params=params, json=body, timeout=30,
                headers={"Authorization": f"Bearer {self.token['access_token']}"},
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "2"))
                self.sleep(min(wait, 60))
                continue
            if resp.status_code == 401 and attempt == 0:
                self._refresh()
                continue
            if resp.status_code >= 500 and attempt < retries - 1:
                self.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise SpotifyError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.content else None
        raise SpotifyError(f"{method} {path} kept failing after {retries} attempts")

    def get(self, path, **params):
        return self.request("GET", path, params=params or None)

    def paginate(self, path: str, limit: int = 50, max_items: int | None = None, **params) -> Iterator[dict]:
        """Follow `next` links. Works for offset and cursor pagination."""
        page = self.get(path, limit=limit, **params)
        count = 0
        while page:
            for item in page.get("items", []):
                yield item
                count += 1
                if max_items and count >= max_items:
                    return
            nxt = page.get("next")
            page = self.get(nxt) if nxt else None

    # convenience wrappers
    def me(self) -> dict:
        return self.get("/me")

    def top(self, kind: str, time_range: str, max_items: int = 50) -> list[dict]:
        return list(self.paginate(f"/me/top/{kind}", time_range=time_range, max_items=max_items))

    def recently_played(self) -> list[dict]:
        return self.get("/me/player/recently-played", limit=50).get("items", [])

    def saved_tracks(self, max_items: int | None = None) -> list[dict]:
        return list(self.paginate("/me/tracks", max_items=max_items))

    def my_playlists(self) -> list[dict]:
        return list(self.paginate("/me/playlists"))

    def playlist_items(self, playlist_id: str) -> list[dict]:
        """Only works for playlists the user owns or collaborates on (Feb 2026 rule)."""
        out = []
        for entry in self.paginate(f"/playlists/{playlist_id}/items", limit=50):
            track = entry.get("item") or entry.get("track")
            if track and track.get("type", "track") == "track" and track.get("uri"):
                out.append({**entry, "item": track})
        return out

    def search_tracks(self, query: str, limit: int = 10) -> list[dict]:
        return self.get("/search", q=query, type="track", limit=min(limit, 10))["tracks"]["items"]

    def create_playlist(self, name: str, description: str, public: bool = False) -> dict:
        return self.request("POST", "/me/playlists", body={"name": name, "description": description, "public": public})

    def add_items(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            self.request("POST", f"/playlists/{playlist_id}/items", body={"uris": uris[i:i + 100]})

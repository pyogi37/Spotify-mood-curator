"""Taste audit: collect listening evidence, then weight it into a profile.

Evidence order, strongest first (same as the browser skill):
  plays (top tracks/artists, recently played) > saves (Liked Songs)
  > the user's own playlists > everything else.
Playlists this tool created are excluded so it never learns from its own picks.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .spotify import Spotify

TIME_RANGES = ("short_term", "medium_term", "long_term")

# How much one appearance in each source counts towards an artist's score.
SOURCE_WEIGHTS = {
    "top_tracks_long_term": 5.0,
    "top_tracks_medium_term": 4.0,
    "top_tracks_short_term": 3.0,
    "recently_played": 2.5,
    "saved": 1.5,
    "own_playlist": 1.0,
}
TOP_ARTIST_WEIGHTS = {"long_term": 30.0, "medium_term": 24.0, "short_term": 18.0}


def _slim_track(t: dict) -> dict:
    return {
        "uri": t["uri"],
        "name": t["name"],
        "artists": [{"id": a.get("id"), "name": a["name"]} for a in t.get("artists", [])],
        "album": (t.get("album") or {}).get("name"),
        "duration_ms": t.get("duration_ms"),
    }


def collect(sp: Spotify, created_ids: set[str], max_saved: int | None = 2000, log=lambda msg: None) -> dict:
    me = sp.me()
    log(f"Account: {me.get('display_name')}")
    snap: dict = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "user": {"id": me["id"], "display_name": me.get("display_name")},
        "top_tracks": {}, "top_artists": {},
    }
    for tr in TIME_RANGES:
        snap["top_tracks"][tr] = [_slim_track(t) for t in sp.top("tracks", tr)]
        snap["top_artists"][tr] = [
            {"id": a["id"], "name": a["name"], "genres": a.get("genres", [])} for a in sp.top("artists", tr)
        ]
        log(f"Top tracks and artists ({tr.replace('_', ' ')}): {len(snap['top_tracks'][tr])} tracks, "
            f"{len(snap['top_artists'][tr])} artists")
    log("Recently played...")
    snap["recently_played"] = [
        {**_slim_track(i["track"]), "played_at": i.get("played_at")} for i in sp.recently_played()
    ]
    log(f"Liked Songs (up to {max_saved}), 50 at a time...")
    snap["saved"] = [
        {**_slim_track(i["track"]), "added_at": i.get("added_at")}
        for i in sp.saved_tracks(max_items=max_saved) if i.get("track")
    ]
    log(f"Liked Songs read: {len(snap['saved'])}")
    playlists = []
    all_playlists = sp.my_playlists()
    log(f"Playlists in your library: {len(all_playlists)}. Reading the ones you own...")
    for p in all_playlists:
        own = p.get("owner", {}).get("id") == me["id"]
        entry = {
            "id": p["id"], "name": p["name"], "description": p.get("description") or "",
            "owned": own, "collaborative": p.get("collaborative", False),
            "created_by_curator": p["id"] in created_ids, "tracks": [],
        }
        if (own or entry["collaborative"]) and not entry["created_by_curator"]:
            entry["tracks"] = [
                {**_slim_track(i["item"]), "added_by": (i.get("added_by") or {}).get("id")}
                for i in sp.playlist_items(p["id"])
            ]
            log(f"  {p['name']}: {len(entry['tracks'])} songs")
        playlists.append(entry)
    snap["playlists"] = playlists
    return snap


def build_profile(snap: dict) -> dict:
    """Turn a raw snapshot into weighted artists, known tracks and lanes."""
    user_id = snap["user"]["id"]
    artist_score: dict[str, float] = defaultdict(float)
    artist_name: dict[str, str] = {}
    artist_genres: dict[str, list[str]] = {}
    tracks: dict[str, dict] = {}

    def note(track: dict, source: str, weight: float) -> None:
        rec = tracks.setdefault(track["uri"], {
            "uri": track["uri"], "name": track["name"],
            "artists": [a["name"] for a in track["artists"]], "sources": [],
        })
        if source not in rec["sources"]:
            rec["sources"].append(source)
        for a in track["artists"]:
            key = a.get("id") or a["name"].lower()
            artist_name[key] = a["name"]
            artist_score[key] += weight

    for tr in TIME_RANGES:
        for rank, t in enumerate(snap["top_tracks"].get(tr, [])):
            # Higher rank counts a bit more; never below half weight.
            note(t, f"top_tracks_{tr}", SOURCE_WEIGHTS[f"top_tracks_{tr}"] * max(0.5, 1 - rank / 100))
        for rank, a in enumerate(snap["top_artists"].get(tr, [])):
            artist_name[a["id"]] = a["name"]
            artist_genres[a["id"]] = a.get("genres", [])
            artist_score[a["id"]] += TOP_ARTIST_WEIGHTS[tr] * max(0.3, 1 - rank / 50)
    for t in snap.get("recently_played", []):
        note(t, "recently_played", SOURCE_WEIGHTS["recently_played"])
    for t in snap.get("saved", []):
        note(t, "saved", SOURCE_WEIGHTS["saved"])
    for p in snap.get("playlists", []):
        if p.get("created_by_curator"):
            continue
        for t in p.get("tracks", []):
            # In shared playlists, other people's additions count for less.
            w = SOURCE_WEIGHTS["own_playlist"] * (1.0 if t.get("added_by") in (None, user_id) else 0.4)
            note(t, "own_playlist", w)

    ranked = sorted(artist_score.items(), key=lambda kv: kv[1], reverse=True)
    artists = [
        {"key": k, "name": artist_name[k], "score": round(s, 1), "genres": artist_genres.get(k, [])}
        for k, s in ranked
    ]
    play_sources = {"top_tracks_long_term", "top_tracks_medium_term", "top_tracks_short_term", "recently_played"}
    favourite_tracks = sorted(
        tracks.values(),
        key=lambda t: (len(set(t["sources"]) & play_sources), len(t["sources"])), reverse=True,
    )
    return {
        "user": snap["user"],
        "collected_at": snap["collected_at"],
        "evidence_size": {
            "top_tracks": sum(len(v) for v in snap["top_tracks"].values()),
            "top_artists": sum(len(v) for v in snap["top_artists"].values()),
            "recently_played": len(snap.get("recently_played", [])),
            "saved": len(snap.get("saved", [])),
            "owned_playlists_read": sum(1 for p in snap.get("playlists", []) if p["tracks"]),
        },
        "artists": artists,
        "known_tracks": {uri: t["sources"] for uri, t in sorted(tracks.items())},
        "known_artist_names": sorted({a["name"].lower() for a in artists}),
        "favourite_tracks": favourite_tracks[:150],
        "playlist_names": [
            {"name": p["name"], "description": p["description"]}
            for p in snap.get("playlists", []) if p["owned"] and not p.get("created_by_curator")
        ],
    }


def profile_for_prompt(profile: dict, n_artists: int = 80, n_tracks: int = 100) -> str:
    """Compact text the planner sees. Raw IDs and URIs stay out of the prompt."""
    lines = [f"Evidence size: {json.dumps(profile['evidence_size'])}", "", "Artists by weighted evidence:"]
    for a in profile["artists"][:n_artists]:
        g = f" [{', '.join(a['genres'][:3])}]" if a["genres"] else ""
        lines.append(f"- {a['name']} ({a['score']}){g}")
    lines += ["", "Most-evidenced tracks (sources in brackets):"]
    for t in profile["favourite_tracks"][:n_tracks]:
        lines.append(f"- {t['name']} by {', '.join(t['artists'])} [{', '.join(t['sources'])}]")
    lines += ["", "The listener's own playlist names (how they already group music):"]
    for p in profile["playlist_names"][:60]:
        lines.append(f"- {p['name']}" + (f": {p['description']}" if p["description"] else ""))
    return "\n".join(lines)


def save(obj: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")

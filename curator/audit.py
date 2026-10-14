"""Taste audit: collect listening evidence, then weight it into a profile.

Evidence order, strongest first (same as the browser skill):
  plays (top tracks/artists, recently played) > saves (Liked Songs)
  > the user's own playlists > everything else.
Playlists this tool created are excluded so it never learns from its own picks.
"""

from __future__ import annotations

import html
import json
import re
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


# Sleep and background-noise tracks say how someone sleeps, not what music they like.
NOISE_WORDS = re.compile(
    r"\b(white noise|brown noise|pink noise|rain sounds?|thunder(storm)?s?|sounds? for (deep )?sleep|"
    r"sleep sounds?|deep sleep|ocean waves|fan noise|binaural|asmr)\b", re.I)


def is_noise(track: dict) -> bool:
    return bool(NOISE_WORDS.search(f"{track['name']} {track.get('album') or ''}"))


def _clean_description(d: str | None) -> str:
    d = html.unescape(d or "").strip()
    return "" if d.lower() in ("null", "none") else d


def build_profile(snap: dict, exclude: dict | None = None) -> dict:
    """Turn a raw snapshot into weighted artists, known tracks and lanes.

    exclude: {"playlists": [names], "artists": [names]}. Use it for playlists this tool
    (or anything else automated) made before the registry existed, and for artists you
    don't want treated as taste.
    """
    exclude = exclude or {}
    skip_playlists = {n.casefold() for n in exclude.get("playlists", [])}
    skip_artists = {n.casefold() for n in exclude.get("artists", [])}
    skipped = {"noise_tracks": 0, "excluded_artist_tracks": 0, "excluded_playlists": []}
    user_id = snap["user"]["id"]
    artist_score: dict[str, float] = defaultdict(float)
    artist_name: dict[str, str] = {}
    artist_genres: dict[str, list[str]] = {}
    tracks: dict[str, dict] = {}

    noise_by_artist: dict[str, int] = defaultdict(int)
    real_by_artist: dict[str, int] = defaultdict(int)

    def note(track: dict, source: str, weight: float) -> None:
        keys = [a.get("id") or a["name"].lower() for a in track["artists"]]
        if is_noise(track):
            skipped["noise_tracks"] += 1
            for k in keys:
                noise_by_artist[k] += 1
            return
        for k in keys:
            real_by_artist[k] += 1
        if any(a["name"].casefold() in skip_artists for a in track["artists"]):
            skipped["excluded_artist_tracks"] += 1
            return
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
            if a["name"].casefold() in skip_artists:
                continue
            artist_name[a["id"]] = a["name"]
            artist_genres[a["id"]] = a.get("genres", [])
            artist_score[a["id"]] += TOP_ARTIST_WEIGHTS[tr] * max(0.3, 1 - rank / 50)
    for t in snap.get("recently_played", []):
        note(t, "recently_played", SOURCE_WEIGHTS["recently_played"])
    for t in snap.get("saved", []):
        note(t, "saved", SOURCE_WEIGHTS["saved"])
    for p in snap.get("playlists", []):
        if p.get("created_by_curator") or p["name"].casefold() in skip_playlists:
            if p["name"].casefold() in skip_playlists:
                skipped["excluded_playlists"].append(p["name"])
            continue
        for t in p.get("tracks", []):
            # In shared playlists, other people's additions count for less.
            w = SOURCE_WEIGHTS["own_playlist"] * (1.0 if t.get("added_by") in (None, user_id) else 0.4)
            note(t, "own_playlist", w)

    # Top-artist lists carry no track names, so a noise-only artist can still arrive that way.
    # Drop artists whose every track in the data was noise.
    for key in list(artist_score):
        if noise_by_artist.get(key) and not real_by_artist.get(key):
            del artist_score[key]
            skipped.setdefault("noise_artists", []).append(artist_name.get(key, key))
    ranked = sorted(artist_score.items(), key=lambda kv: kv[1], reverse=True)
    artists = [
        {"key": k, "name": artist_name[k], "score": round(s, 1), "genres": artist_genres.get(k, [])}
        for k, s in ranked if artist_name.get(k, "").strip()   # local files can have blank artists
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
        "skipped": skipped,
        "known_tracks": {uri: t["sources"] for uri, t in sorted(tracks.items())},
        "track_index": {uri: {"name": t["name"], "artists": t["artists"]} for uri, t in sorted(tracks.items())},
        "known_artist_names": sorted({a["name"].lower() for a in artists}),
        "favourite_tracks": favourite_tracks[:400],
        "playlist_names": [
            {"name": p["name"], "description": _clean_description(p["description"])}
            for p in snap.get("playlists", [])
            if p["owned"] and not p.get("created_by_curator") and p["name"].casefold() not in skip_playlists
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

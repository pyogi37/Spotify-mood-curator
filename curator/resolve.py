"""Turn planned (title, artist) pairs into real Spotify tracks.

Matching is deliberately strict: a wrong version (live, sped up, karaoke, a cover)
is worse than a missing song, because it silently ruins the playlist.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .plan import Plan

# Version markers we reject unless the planned title itself asks for them.
BAD_VERSION = [
    "live", "remix", "sped up", "speed up", "slowed", "reverb", "lofi", "lo-fi", "lo fi",
    "karaoke", "cover", "instrumental", "8d", "jhankar", "nightcore", "tribute", "originally performed",
]
PLAY_SOURCES = {"top_tracks_long_term", "top_tracks_medium_term", "top_tracks_short_term", "recently_played"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\s+-\s+.*(remaster|version|edit|mono|stereo|mix).*$", "", s)
    s = re.sub(r"[\(\[].*?(remaster|feat\.?|ft\.|with |version|edit|mono|stereo).*?[\)\]]", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def bad_version(candidate: dict, planned_title: str) -> str | None:
    hay = f" {candidate['name']} {(candidate.get('album') or {}).get('name', '')} ".lower()
    wanted = planned_title.lower()
    for marker in BAD_VERSION:
        if re.search(rf"(?<![a-z]){re.escape(marker)}(?![a-z])", hay) and marker not in wanted:
            return marker
    return None


def score(candidate: dict, title: str, artist: str) -> tuple[float, float]:
    t = similarity(candidate["name"], title)
    names = [a["name"] for a in candidate.get("artists", [])]
    a = max((similarity(n, artist) for n in names), default=0.0)
    if any(norm(artist) and norm(artist) in norm(n) for n in names):
        a = max(a, 0.95)
    return t, a


def best_match(candidates: list[dict], title: str, artist: str) -> tuple[dict | None, str]:
    reasons = []
    best, best_s = None, 0.0
    for c in candidates:
        if not c or not c.get("uri"):
            continue
        marker = bad_version(c, title)
        if marker:
            reasons.append(f"skipped '{c['name']}' ({marker})")
            continue
        t, a = score(c, title, artist)
        if t >= 0.85 and a >= 0.8 and t + a > best_s:
            best, best_s = c, t + a
    return best, "; ".join(reasons[:3])


def label_evidence(track: dict, profile: dict) -> str:
    """Computed from Spotify data, never taken from the model."""
    sources = set(profile["known_tracks"].get(track["uri"], []))
    if sources & PLAY_SOURCES:
        return "known_history_track"
    if sources:
        return "known_saved_track"
    known_artists = set(profile["known_artist_names"])
    if any(a["name"].lower() in known_artists for a in track.get("artists", [])):
        return "known_artist_extension"
    return "exploratory"


def resolve(plan: Plan, sp, profile: dict, target_count: int) -> dict:
    resolved, missing, seen = [], [], set()
    for pt in plan.tracks:
        if len(resolved) >= target_count:
            break
        match, why = best_match(sp.search_tracks(f"track:{pt.title} artist:{pt.artist}"), pt.title, pt.artist)
        if not match:  # field filters can be too strict with accents or "feat."
            match, why2 = best_match(sp.search_tracks(f"{pt.title} {pt.artist}"), pt.title, pt.artist)
            why = "; ".join(x for x in (why, why2) if x)
        if not match:
            missing.append({"title": pt.title, "artist": pt.artist, "note": why or "no confident match"})
            continue
        if match["uri"] in seen:
            continue
        seen.add(match["uri"])
        evidence = label_evidence(match, profile)
        resolved.append({
            "position": len(resolved) + 1,
            "uri": match["uri"],
            "title": match["name"],
            "artists": [a["name"] for a in match["artists"]],
            "album": (match.get("album") or {}).get("name"),
            "duration_ms": match.get("duration_ms"),
            "planned_intent": pt.intent,
            "evidence": evidence,
            "why": pt.why,
        })
    return {"tracks": resolved, "missing": missing, "stats": mix_stats(resolved)}


def mix_stats(tracks: list[dict]) -> dict:
    n = len(tracks) or 1
    counts: dict[str, int] = {}
    for t in tracks:
        counts[t["evidence"]] = counts.get(t["evidence"], 0) + 1
    exploratory = counts.get("exploratory", 0)
    # "anchor" claims the data can't back up, and "discoveries" the listener already plays.
    false_anchor = sum(1 for t in tracks if t["planned_intent"] == "anchor" and t["evidence"] == "exploratory")
    stale_discovery = sum(1 for t in tracks if t["planned_intent"] == "discovery" and t["evidence"] in
                          ("known_history_track", "known_saved_track"))
    return {
        "count": len(tracks),
        "evidence_counts": counts,
        "discovery_share": round(exploratory / n, 2),
        "anchors_not_backed_by_data": false_anchor,
        "discoveries_already_known": stale_discovery,
        "minutes": round(sum(t.get("duration_ms") or 0 for t in tracks) / 60000),
    }

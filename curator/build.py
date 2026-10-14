"""Create the playlist, add tracks resumably, and verify what Spotify actually saved."""

from __future__ import annotations

import json
from pathlib import Path


def load_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def summary_table(plan: dict, resolved: dict) -> str:
    s = resolved["stats"]
    lines = [
        f"{plan['name']}  ({s['count']} songs, ~{s['minutes']} min, folder: {plan.get('folder') or '-'})",
        f"  \"{plan['description']}\"",
        f"  evidence: {s['evidence_counts']}  discovery share: {s['discovery_share']:.0%}",
    ]
    if s["anchors_not_backed_by_data"]:
        lines.append(f"  note: {s['anchors_not_backed_by_data']} songs the planner called anchors aren't in your data")
    if resolved["missing"]:
        lines.append(f"  not found on Spotify: {len(resolved['missing'])}")
    lines.append("")
    for t in resolved["tracks"]:
        tag = {"known_history_track": "played", "known_saved_track": "saved",
               "known_artist_extension": "artist", "exploratory": "NEW"}[t["evidence"]]
        lines.append(f"  {t['position']:>2}. [{tag:>6}] {t['title']} - {', '.join(t['artists'])}")
    return "\n".join(lines)


def build(sp, plan: dict, resolved: dict, run_dir: Path, registry_path: Path, public: bool = False) -> dict:
    """Idempotent: if a previous run stopped half way, it continues instead of duplicating."""
    progress = load_json(run_dir / "progress.json", {})
    uris = [t["uri"] for t in resolved["tracks"]]

    if not progress.get("playlist_id"):
        pl = sp.create_playlist(plan["name"], plan["description"], public=public)
        progress = {"playlist_id": pl["id"], "url": pl.get("external_urls", {}).get("spotify"), "added": 0}
        save_json(progress, run_dir / "progress.json")
        registry = load_json(registry_path, [])
        registry.append({"id": pl["id"], "name": plan["name"]})
        save_json(registry, registry_path)

    existing = [i["item"]["uri"] for i in sp.playlist_items(progress["playlist_id"])]
    if existing != uris[:len(existing)]:
        raise RuntimeError(
            "The playlist no longer matches the plan's opening songs (edited by hand?). "
            "Not adding anything. Run `curator verify` to see the difference.")
    remaining = uris[len(existing):]
    for i in range(0, len(remaining), 100):
        chunk = remaining[i:i + 100]
        sp.add_items(progress["playlist_id"], chunk)
        progress["added"] = len(existing) + i + len(chunk)
        save_json(progress, run_dir / "progress.json")
    progress["completed"] = True
    save_json(progress, run_dir / "progress.json")
    return progress


def verify(sp, playlist_id: str, resolved: dict) -> dict:
    items = sp.playlist_items(playlist_id)
    actual = [i["item"]["uri"] for i in items]
    planned = [t["uri"] for t in resolved["tracks"]]
    dupes = sorted({u for u in actual if actual.count(u) > 1})
    report = {
        "planned": len(planned),
        "saved": len(actual),
        "missing": [t["title"] for t in resolved["tracks"] if t["uri"] not in actual],
        "unexpected": [i["item"]["name"] for i in items if i["item"]["uri"] not in planned],
        "duplicates": dupes,
        "order_matches": actual == planned,
        "manifest": [
            {"position": n + 1, "title": i["item"]["name"],
             "artists": [a["name"] for a in i["item"].get("artists", [])], "uri": i["item"]["uri"]}
            for n, i in enumerate(items)
        ],
    }
    report["ok"] = not (report["missing"] or report["unexpected"] or dupes) and report["order_matches"]
    return report

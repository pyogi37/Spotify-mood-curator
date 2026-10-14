import json
import time
from types import SimpleNamespace

import pytest
import responses

from curator import audit
from curator.build import build, verify
from curator.plan import Plan, PlannedTrack, make_plan
from curator.resolve import bad_version, best_match, label_evidence, norm, resolve
from curator.spotify import API, Spotify


def trk(uri, name, artist, album="Album", ms=200000):
    return {"uri": uri, "name": name, "artists": [{"id": artist.lower(), "name": artist}],
            "album": {"name": album}, "duration_ms": ms, "type": "track"}


# ---------------------------------------------------------------- matching

def test_norm_strips_remaster_and_feat():
    assert norm("Lucy In The Sky With Diamonds - Remastered 2009") == "lucy in the sky with diamonds"
    assert norm("Lose Yourself to Dance (feat. Pharrell Williams)") == "lose yourself to dance"


@pytest.mark.parametrize("name,album,planned,expected", [
    ("Porch - Live", "Live On Ten Legs", "Porch", "live"),
    ("Alive", "Ten", "Alive", None),                     # "alive" is not "live"
    ("Kesariya (Sped Up)", "x", "Kesariya", "sped up"),
    ("Otherside - Nils Hoffmann Remix", "x", "Otherside (Nils Hoffmann Remix)", None),
])
def test_bad_version(name, album, planned, expected):
    assert bad_version({"name": name, "album": {"name": album}}, planned) == expected


def test_best_match_prefers_studio_version_and_right_artist():
    cands = [
        trk("u:live", "Black - Live", "Pearl Jam", "Live"),
        trk("u:cover", "Black", "Some Tribute Band"),
        trk("u:ok", "Black", "Pearl Jam", "Ten"),
    ]
    match, _ = best_match(cands, "Black", "Pearl Jam")
    assert match["uri"] == "u:ok"


def test_best_match_returns_none_when_unsure():
    match, _ = best_match([trk("u:1", "Something Else", "Pearl Jam")], "Black", "Pearl Jam")
    assert match is None


# ---------------------------------------------------------------- profile + evidence

def snapshot():
    return {
        "collected_at": "2026-10-14T00:00:00Z",
        "user": {"id": "me", "display_name": "Me"},
        "top_tracks": {"short_term": [], "medium_term": [], "long_term": [trk("u:breathe", "Breathe", "Pink Floyd")]},
        "top_artists": {"short_term": [], "medium_term": [{"id": "radiohead", "name": "Radiohead", "genres": ["art rock"]}],
                        "long_term": []},
        "recently_played": [],
        "saved": [trk("u:ghazal", "Chupke Chupke", "Ghulam Ali")],
        "playlists": [
            {"id": "p1", "name": "mine", "description": "", "owned": True, "collaborative": False,
             "created_by_curator": False,
             "tracks": [{**trk("u:friend", "Friend Pick", "Someone"), "added_by": "friend"}]},
            {"id": "p2", "name": "Rearview", "description": "", "owned": True, "collaborative": False,
             "created_by_curator": True, "tracks": [{**trk("u:own", "Own Pick", "Curator Artist"), "added_by": "me"}]},
        ],
    }


def test_profile_excludes_curator_playlists_and_ranks_plays_first():
    prof = audit.build_profile(snapshot())
    assert "u:own" not in prof["known_tracks"]
    names = [a["name"] for a in prof["artists"]]
    assert names[0] == "Radiohead"                         # top artist signal is strongest
    assert names.index("Pink Floyd") < names.index("Ghulam Ali") < names.index("Someone")
    assert "Rearview" not in [p["name"] for p in prof["playlist_names"]]
    assert "Radiohead (" in audit.profile_for_prompt(prof)


def test_label_evidence_is_computed_from_data():
    prof = audit.build_profile(snapshot())
    assert label_evidence(trk("u:breathe", "Breathe", "Pink Floyd"), prof) == "known_history_track"
    assert label_evidence(trk("u:ghazal", "Chupke Chupke", "Ghulam Ali"), prof) == "known_saved_track"
    assert label_evidence(trk("u:new", "Time", "Pink Floyd"), prof) == "known_artist_extension"
    assert label_evidence(trk("u:x", "Future Days", "CAN"), prof) == "exploratory"


# ---------------------------------------------------------------- fake Spotify for pipeline tests

class FakeSpotify:
    def __init__(self, catalog):
        self.catalog = catalog
        self.playlists = {}
        self.add_calls = 0
        self.fail_after = None

    def search_tracks(self, q, limit=10):
        q = q.lower()
        return [t for t in self.catalog if t["name"].lower() in q][:limit]

    def create_playlist(self, name, description, public=False):
        pid = f"pl{len(self.playlists) + 1}"
        self.playlists[pid] = []
        return {"id": pid, "external_urls": {"spotify": f"https://open.spotify.com/playlist/{pid}"}}

    def add_items(self, pid, uris):
        self.add_calls += 1
        if self.fail_after is not None and self.add_calls > self.fail_after:
            raise RuntimeError("network died")
        by_uri = {t["uri"]: t for t in self.catalog}
        self.playlists[pid] += [by_uri[u] for u in uris]

    def playlist_items(self, pid):
        return [{"item": t} for t in self.playlists[pid]]


def make_plan_obj(titles):
    return Plan(name="Test", description="test.", folder="", arc=["all"], version_policy="studio",
                tracks=[PlannedTrack(title=t, artist=a, intent="anchor", why="-") for t, a in titles])


def test_resolve_build_verify_end_to_end(tmp_path):
    catalog = [trk("u:breathe", "Breathe", "Pink Floyd"), trk("u:future", "Future Days", "CAN")]
    sp = FakeSpotify(catalog)
    prof = audit.build_profile(snapshot())
    plan = make_plan_obj([("Breathe", "Pink Floyd"), ("Future Days", "CAN"), ("Made Up Song", "Nobody")])
    resolved = resolve(plan, sp, prof, target_count=10)
    assert [t["uri"] for t in resolved["tracks"]] == ["u:breathe", "u:future"]
    assert resolved["missing"][0]["title"] == "Made Up Song"
    assert resolved["stats"]["discovery_share"] == 0.5
    assert resolved["stats"]["anchors_not_backed_by_data"] == 1   # CAN was called an anchor but isn't in the data

    progress = build(sp, plan.model_dump(), resolved, tmp_path / "run", tmp_path / "reg.json")
    report = verify(sp, progress["playlist_id"], resolved)
    assert report["ok"] and report["saved"] == 2
    assert json.loads((tmp_path / "reg.json").read_text())[0]["name"] == "Test"


def test_build_resumes_without_duplicates(tmp_path):
    catalog = [trk(f"u:{i}", f"Song {i}", "Artist") for i in range(150)]
    sp = FakeSpotify(catalog)
    resolved = {"tracks": [{"uri": t["uri"], "title": t["name"]} for t in catalog], "missing": [], "stats": {}}
    plan = {"name": "Big", "description": "big."}
    sp.fail_after = 1                                       # first chunk of 100 lands, then the network dies
    with pytest.raises(RuntimeError):
        build(sp, plan, resolved, tmp_path / "run", tmp_path / "reg.json")
    sp.fail_after = None
    progress = build(sp, plan, resolved, tmp_path / "run", tmp_path / "reg.json")
    assert len(sp.playlists) == 1                           # did not create a second playlist
    assert verify(sp, progress["playlist_id"], resolved)["ok"]


def test_build_refuses_when_playlist_was_edited(tmp_path):
    catalog = [trk("u:a", "A", "X"), trk("u:b", "B", "X"), trk("u:z", "Z", "X")]
    sp = FakeSpotify(catalog)
    resolved = {"tracks": [{"uri": "u:a", "title": "A"}, {"uri": "u:b", "title": "B"}], "missing": [], "stats": {}}
    progress = build(sp, {"name": "E", "description": "e."}, resolved, tmp_path / "run", tmp_path / "reg.json")
    sp.playlists[progress["playlist_id"]] = [catalog[2]]   # someone replaced the contents by hand
    with pytest.raises(RuntimeError, match="no longer matches"):
        build(sp, {"name": "E", "description": "e."}, resolved, tmp_path / "run", tmp_path / "reg.json")


# ---------------------------------------------------------------- planner (no network)

def test_planner_parses_forced_tool_output():
    plan_json = {"name": "Rearview", "description": "for long drives.", "folder": "everyday", "arc": ["1-5 warm"],
                 "version_policy": "studio", "tracks": [{"title": "Black", "artist": "Pearl Jam",
                                                         "intent": "anchor", "why": "opener"}]}
    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="submit_plan", input=plan_json)])

    plan = make_plan("profile", "driving", client=SimpleNamespace(messages=FakeMessages()))
    assert plan.tracks[0].artist == "Pearl Jam"
    assert captured["tool_choice"] == {"type": "tool", "name": "submit_plan"}
    assert "<listener_evidence>" in captured["messages"][0]["content"]


def test_manual_prompt_round_trip():
    from curator.plan import manual_prompt, parse_manual_reply
    prompt = manual_prompt("Brief: driving")
    assert "Brief: driving" in prompt and '"tracks"' in prompt
    reply = """Here's your plan!

```json
{"name": "Rearview", "description": "for long drives.", "folder": "everyday", "arc": ["warm start"],
 "version_policy": "studio", "tracks": [{"title": "Black", "artist": "Pearl Jam", "intent": "anchor", "why": "opener"}]}
```
Enjoy."""
    assert parse_manual_reply(reply).tracks[0].title == "Black"


def test_manual_mode_without_key(tmp_path, monkeypatch, capsys):
    import curator.cli as cli
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "DATA", tmp_path)
    (tmp_path / "profile.json").write_text(json.dumps(audit.build_profile(snapshot())))
    cli.main(["plan", "long drives", "--name", "drive"])
    assert (tmp_path / "runs" / "drive" / "prompt.md").exists()
    assert "--from-file" in capsys.readouterr().out


# ---------------------------------------------------------------- HTTP client

@responses.activate
def test_client_paginates_and_retries_on_429(tmp_path):
    tok = tmp_path / "t.json"
    tok.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "client_id": "c",
                               "expires_at": time.time() + 3600}))
    responses.add(responses.GET, f"{API}/me/tracks", status=429, headers={"Retry-After": "1"})
    responses.add(responses.GET, f"{API}/me/tracks", json={"items": [{"track": 1}], "next": f"{API}/me/tracks?offset=1"})
    responses.add(responses.GET, f"{API}/me/tracks", json={"items": [{"track": 2}], "next": None})
    slept = []
    sp = Spotify(tok, sleep=slept.append)
    assert [i["track"] for i in sp.saved_tracks()] == [1, 2]
    assert slept == [1]


@responses.activate
def test_playlist_items_reads_new_and_old_field_names(tmp_path):
    tok = tmp_path / "t.json"
    tok.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "client_id": "c",
                               "expires_at": time.time() + 3600}))
    responses.add(responses.GET, f"{API}/playlists/p/items", json={"items": [
        {"item": trk("u:1", "New Shape", "X")},
        {"track": trk("u:2", "Old Shape", "X")},
        {"item": {"type": "episode", "uri": "e:1", "name": "Podcast"}},
    ], "next": None})
    items = Spotify(tok).playlist_items("p")
    assert [i["item"]["uri"] for i in items] == ["u:1", "u:2"]


def test_files_are_utf8_regardless_of_locale(tmp_path, monkeypatch):
    import locale
    from curator.build import load_json, save_json
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *a, **k: "cp1252")
    data = {"title": "दिल से रे", "artist": "Ghulām ʿAlī"}
    save_json(data, tmp_path / "x.json")
    audit.save(data, tmp_path / "y.json")
    assert load_json(tmp_path / "x.json") == data
    assert json.loads((tmp_path / "y.json").read_bytes().decode("utf-8")) == data


def test_profile_drops_noise_and_excluded_playlists():
    snap = snapshot()
    snap["saved"] += [trk("u:rain", "Rain And Thunder For Deep Sleep", "Edy Boman")]
    snap["top_artists"]["short_term"] = [{"id": "edy boman", "name": "Edy Boman", "genres": []}]
    snap["playlists"][0]["name"] = "Old Mix"
    prof = audit.build_profile(snap, {"playlists": ["old mix"]})
    names = [a["name"] for a in prof["artists"]]
    assert "Edy Boman" not in names
    assert "u:rain" not in prof["known_tracks"]
    assert "Someone" not in names                          # only appeared in the excluded playlist
    assert prof["skipped"]["noise_artists"] == ["Edy Boman"]
    assert prof["skipped"]["excluded_playlists"] == ["Old Mix"]


def test_profile_ignores_blank_artists_and_null_descriptions():
    snap = snapshot()
    snap["saved"].append(trk("u:local", "Voice memo", " "))
    snap["playlists"][0]["description"] = "null"
    snap["playlists"][0]["created_by_curator"] = False
    prof = audit.build_profile(snap)
    assert all(a["name"].strip() for a in prof["artists"])
    assert prof["playlist_names"][0]["description"] == ""


def test_resolve_prefers_the_listeners_own_copy():
    snap = snapshot()
    snap["saved"].append(trk("u:singles", "Seasons", "Chris Cornell", "Singles OST"))
    prof = audit.build_profile(snap)
    deluxe = trk("u:deluxe", "Seasons", "Chris Cornell", "Chris Cornell (Deluxe Edition)")
    sp = FakeSpotify([deluxe])
    plan = make_plan_obj([("Seasons", "Chris Cornell")])
    out = resolve(plan, sp, prof, target_count=5)
    assert out["tracks"][0]["uri"] == "u:singles"
    assert out["tracks"][0]["evidence"] == "known_saved_track"
    assert out["tracks"][0]["source"] == "your library"

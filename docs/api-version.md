# API version (work in progress)

The same curation approach as the browser skill, rebuilt on the Spotify Web API and the Claude API. It's faster, it doesn't break when Spotify's UI changes, and every decision is written to a file you can inspect.

```
audit ──► profile.json ──► plan (Claude) ──► resolve (Spotify search) ──► you approve ──► build ──► verify
          weighted           ordered          strict matching,                          resumable   diff vs plan
          evidence           arc              evidence labels from data
```

## Design decisions

- **The model proposes songs and the data labels them.** Claude returns titles and artists with an intended role (anchor or discovery). Whether a song is a known favourite is decided afterwards from your Spotify data (`played`, `saved`, `artist`, `NEW`), never from the model's claim. The plan summary flags anchors the data can't back up.
- **Strict matching beats coverage.** A sped-up, live, karaoke or cover version is worse than a missing song, so those get rejected unless the plan asks for them, and unclear matches are reported as missing instead of guessed.
- **Nothing is written to Spotify until you approve.** `plan` only reads. `build` shows the full list and asks before creating anything.
- **Builds can resume.** If a build stops half way, running it again picks up where it stopped. If the playlist was edited by hand in the meantime, it refuses to add anything.
- **It never learns from its own output.** Playlists the tool created are logged and left out of later audits.
- **Evidence text is treated as data.** Playlist names and descriptions can be written by other people, so the prompt fences them off and tells the model to ignore any instructions inside them.

## Spotify rules this is built around (February 2026 Development Mode)

- **Premium required.** The app owner needs Spotify Premium, and a dev-mode app can have at most 5 users. That's fine for you plus a couple of friends to test with.
- **New endpoints.** Playlists are created with `POST /me/playlists` and filled with `/playlists/{id}/items`. Contents can only be read for playlists you own or collaborate on.
- **Smaller searches.** Search returns at most 10 results per request.
- **No batch lookups.** Batch endpoints (`GET /tracks`, `GET /artists`), `artists/{id}/top-tracks`, track and artist popularity, and (since late 2024) recommendations and audio features are unavailable. The musical judgement comes from the model, grounded in your listening evidence.

Source: [Spotify's February 2026 migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide).

## Setup

1. Create an app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard). Add the redirect URI `http://127.0.0.1:8888/callback` exactly. Spotify doesn't accept `localhost`, only the loopback IP. Add your own and your testers' Spotify emails under User Management.
2. Install it:
   ```
   python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -e ".[dev]"
   ```
3. Copy `.env.example` to `.env` and fill in `SPOTIFY_CLIENT_ID`. No client secret is needed, because login uses PKCE.
4. `ANTHROPIC_API_KEY` is optional. Without it, `plan` writes the prompt to `data/runs/<name>/prompt.md`. Paste that into a Claude chat, save the reply as `reply.txt`, and continue with `--from-file`. Same plan, same checks, no API key needed.

## Use

```
curator login
curator audit
curator plan "long drives alone, loud guitars, forward motion" --name rearview --count 25 --discovery 0.3 --folder everyday
curator build rearview
curator verify rearview
```

Everything is written to `data/` (git-ignored), with one folder per playlist under `data/runs/<name>/`: `plan.json`, `resolved.json`, `progress.json`, `verify.json`.

## What's measured right now

Every `plan` run reports:
- **The evidence mix:** songs you've played, songs you've saved, other songs by artists you know, and new songs.
- **The discovery share achieved** against the share you asked for.
- **Anchors not backed by data**, which shows how often the model's "you'll know this" claim was wrong.
- **Discoveries you already know**, meaning songs it called discoveries that you already play.
- **Songs not found on Spotify**, a rough measure of how often the model made a song up or got its name wrong.

## First live run (14 October 2026)

One account, Windows 11. The audit read 150 top tracks, 135 top artists, 50 recent plays, 2,000 Liked Songs and 43 owned playlists. It left out 15 playlists made earlier by the browser skill, plus 17 sleep-sound plays.

Brief: "everyday solo driving: long drives, loud guitars, getting out of my head", 25 songs, 30% discovery requested.

| Metric | Result |
|---|---|
| Songs found on Spotify | 25 / 25 (0 of 4 spares needed) |
| Wrong versions (live, sped up, cover) | 0 |
| Evidence mix | 6 played, 6 saved, 8 known artist, 5 new |
| Discovery share achieved | 20% (30% requested) |
| Planned discoveries the data showed were already known | 2 of 7 |
| Anchors not backed by data | 0 |
| Verify after build | 25/25 saved, order matches, no duplicates |

The main finding: the planner overestimates novelty. 2 of 7 "discoveries" were already in the library or by a known artist. The data-computed labels caught both. A re-plan loop that swaps those for new picks would fix it automatically.

Bugs found and fixed during the run: Windows cp1252 encoding crashes, no progress output during long reads, the planner's own earlier playlists counting as taste, sleep sounds ranking as a top artist, blank artist names from local files, and "null" playlist descriptions.

## Second live run: favourites only

Brief: "my personal favourites, centred on Pearl Jam, Audioslave, Jane's Addiction and Alice In Chains, with deep cuts I play a lot". 30 songs, `--discovery 0 --evidence-tracks 300`.

| Metric | Result |
|---|---|
| Songs found | 30 / 30 |
| Evidence mix, first preview | 10 played, 17 saved, 3 "known artist" |
| Evidence mix, after the own-copy fix | 10 played, 20 saved |
| Anchors not backed by data | 0 |
| Verify after build | 30/30 saved, order matches, no duplicates |

The finding: those 3 "known artist" songs were songs the listener already had, but search returned another album's copy (a deluxe edition instead of the *Singles* soundtrack, for example). The resolver now checks the listener's own library first and only searches when they don't have the song. That keeps the exact recording they play, keeps the evidence label honest, and needs fewer API calls.

## Not built yet

- [ ] Keep/skip evaluation: re-read the playlist after a week and compare it with recently played
- [ ] Library folders: the Web API can't create folders, so this stays in the browser skill
- [ ] Custom cover upload (`PUT /playlists/{id}/images`)
- [ ] Batch mode: several moods from one audit
- [ ] Automatic re-plan when the discovery share misses the target
- [x] A live run against a real account

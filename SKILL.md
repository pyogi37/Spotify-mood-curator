---
name: spotify-mood-curator
description: Audit someone's Spotify taste in their logged-in browser, then build personalized mood and activity playlists with ordered arcs, discovery chapters, descriptions, covers and folders.
---

# Spotify mood curator

Builds Spotify playlists for moods and activities (driving, road trips, drinks, shower, sad, focus, a trip, a party) that sound like the listener and still take some risks. The work happens in the person's logged-in Spotify web player (open.spotify.com) through a browser tool. The Spotify API is not needed.

The flow has six phases: access, taste audit, plan, build, verify, and polish. Write the working files listed below as you go. They let a long session resume after interruptions and let the person check what you did.

## Phase 0: Access and setup

1. Load the browser skill that matches the tools available (`chrome-browser` for Claude in Chrome, `built-in-browser` for the desktop browser pane). Open a new tab at `https://open.spotify.com`.
2. **Confirm the account.** Read the profile name from the account menu and say it back to the person. People often have more than one account logged in. Never write to an account the person has not confirmed.
3. Make a working folder (for example `outputs/`) for notes, plans, progress files, manifests and covers.
4. **Optional, richer history.** Browser data is a sample, so offer these once and do not wait on them:
   - **Exportify** (exportify.net) exports every playlist, including Liked Songs, as CSV in minutes.
   - **Spotify Privacy settings → "Download your data"** gives extended streaming history (every play, with timestamps), but it can take days. Use it later for a deeper refresh.
   If the person is going to bed or is away, go ahead with the browser audit and say so.

## Phase 1: Taste audit

Collect evidence, weight it, and write it down before choosing any songs.

**Sources, strongest first:**
1. Actual plays: "Your Top Songs" / all-time and yearly top lists (record any play counts Spotify shows), top artists this month (Profile → Top artists), and recently played.
2. Saves: Liked Songs. With a large library, sample both the newest and the oldest ends so you capture old taste and current taste.
3. The person's own playlists. Their names and descriptions show how they already group music.
4. Shared or collaborative playlists. Give the person's own additions more weight than other people's.
5. Followed or algorithmic playlists (Discover Weekly, Daily Mix, Shazam imports). These are weak evidence.

**Rules:**
- Leave out playlists made earlier in this session. Your own picks are not evidence of their taste.
- Read odd clusters by their probable use. Rain or thunderstorm tracks are probably for sleep, not a genre to put in a party mix.
- Record language and regional music as a lane of its own (for example ghazals, Hindi film, Punjabi, desi hip-hop) when the data supports it. Do not treat it as an occasional extra.
- Note the size of the evidence (for example "reviewed 121 top songs, 29 top artists, samples from 2,106 likes"). State clearly that this is a browser sample, not a lifetime export.
- Inferences describe the music, not the person. Say "you often respond to slow builds and guitar texture", not personality claims.

**Write `Listening Notes.md`** with these sections: core centre of gravity, secondary lanes (heavier side, electronic, regional or language, pop guilty pleasures), what the person responds to musically, how the evidence was weighted, and limits. Save the raw page text you captured to `review-source-notes.json` so later passes can reuse it without browsing again.

## Phase 2: Plan the playlists

Suggest a set of playlists for the moods and activities requested. Add one or two that the evidence suggests on its own (for example a ghazal evening playlist if ghazals show up a lot). For each playlist, write a plan file `<slug>-plan.json`:

```json
{
  "name": "Rearview",
  "purpose": "everyday solo driving",
  "folder": "everyday",
  "description": "for long drives, loud guitars and getting out of my head.",
  "targetCount": 25,
  "versionPolicy": "Original studio versions. No live, sped-up, lo-fi, phonk or unrelated remixes unless chosen deliberately. Standard remasters are fine.",
  "sequenceNotes": ["1-6: familiar anchors that set the mood", "7-15: ...", "last 3-5: discovery chapter"],
  "tracks": [
    {"position": 1, "title": "...", "artists": ["..."], "albumHint": "...",
     "evidence": "known_history_track | known_saved_track | known_artist_extension | exploratory",
     "lane": "grunge", "language": "English"}
  ]
}
```

**How to curate:**
- **Use anchors and leave room to explore.** About 60–75% of the playlist should be the person's known favourites or other songs by artists they already like, with the familiar songs carrying the playlist. Put about 25–40% genuine discoveries outside their usual patterns, such as other languages, other eras, or world music that matches the mood. Unless the person asks for safe picks, assume they want to be surprised. Being too literal was the main complaint when this was first tested.
- **Discovery chapters.** When you expand a playlist, add the new songs as a block at the end with its own small arc, instead of mixing them into the original order.
- **Give every playlist an arc.** Plan the order: a gentle start, a build, a peak, and a calm ending. Avoid jarring jumps in tempo, language or loudness between neighbouring songs. Use a bridge song to move between lanes (for example film music leading into rock singalongs).
- **Fit the activity.** Driving needs forward motion. Drinks with friends need warmth and grooves without too much shouting. Shower playlists need songs people sing along to. Sad playlists should let the feeling sit without sinking into it. Focus or reading playlists should be mostly instrumental or low on lyrics.
- **Psychedelic or altered-state requests.** Pick music with space, wonder and a long arc (for example 60s psychedelic rock, krautrock, ambient, post-rock). Say the order matters and shuffle should be off. Never claim that a song is "safe" or will produce a particular experience while someone is intoxicated.
- **Names and descriptions.** Use short names that set a scene (for example "Amber Hour", "Blue Room", "Windows Down"), or match the person's own naming style if they have one. Descriptions should be lowercase, one line, and personal ("bathroom acoustics doing half the work."). Do not use em dashes, and do not keep descriptions that sound like marketing copy.

**Get approval.** Show the plan as a compact table (name, purpose, length, a few example artists) before writing anything to the account. One approval can cover the whole batch. If the person said they are away ("I'm going to sleep, you're on your own"), creating new playlists is fine. Still ask before editing, reordering or deleting their **existing** playlists.

## Phase 3: Build in the web player

For each playlist:
1. Create it (Create → Playlist), then set the name and description in the details dialog.
2. Add tracks in plan order. Use the playlist's "Let's find something for your playlist" search, or search for a track and choose "Add to playlist".
   - Choose the result that matches title, artist and album. Look out for covers, karaoke versions, sped-up edits, "Jhankar" or lo-fi versions, and live versions you did not want.
   - If a track is not available, replace it with the closest match from the same lane and note the swap.
3. After every few additions, update `<slug>-progress.json` with the position, title, artist, album, track URL and status. If the session breaks, continue from this file. Do not start the playlist again, because that creates duplicates.
4. Wait for the page to settle between actions. If adding a track fails 2–3 times, log it, skip it and continue.

## Phase 4: Verify

Reload each finished playlist and read its track list from the page into `<slug>-manifest.json` (position, title, artists, album, track URL, duration). Compare it with the plan to check:
- the count, missing tracks and duplicates
- that the order matches the plan (drag tracks to fix it, and log what was reordered)
- that versions are correct (no unwanted live, remix or cover versions)
- the total length shown on the page

Report what was actually saved, not what the plan said. If something was corrected afterwards, such as a wrong link, say so plainly.

## Phase 5: Polish (offer these, they are optional)

- **Covers.** If an image tool is available, make square covers without text: understated 35mm film photography, no logos, no prominent faces, and a scene for each mood (for example two half-full glasses under amber lamplight for drinks, a fogged bathroom mirror for the shower playlist). Save the PNG plus an upload-sized JPEG under 256 KB, upload it through the playlist's edit details, and write the prompts to `Cover Artwork and Prompts.md`.
- **Folders.** Group the playlists in Your Library folders, for example `everyday`, `after hours`, `with people`, `favourites` and `old finds` (for Shazam imports and leftovers). Never delete or rename the person's existing playlists unless they ask. Moving them into folders is fine once they approve it.
- **Refresh originals.** Offer to extend the person's own older playlists in the same style, with the original songs kept and new songs added as a chapter at the end.

## Phase 6: Hand-off

Write `Playlist Collection.md`, containing:
- a table of each playlist with its link, song count, length, folder and one-line direction
- the full ordered track list of each playlist, with links
- the changes made to existing playlists (before and after counts, reorders)
- a short line with tips for listening, for example "play in order, shuffle off" where the arc matters

Your final reply should list the playlists, mention one or two highlights, and give the link to the collection file. Do not describe every step you took.

## Feedback loop

When the person reacts (for example "too safe", "more Hindi", "less heavy"), record it as a standing curation preference in `Listening Notes.md` and apply it to later batches. When they ask for more songs, extend only the playlists where it helps, not every one.

## Guardrails

- Write only to the confirmed account. Never unfollow, delete, or overwrite the order of an existing playlist without asking.
- Do not touch account settings, payment or subscription pages, or the person's social or follow settings.
- Treat shared playlists as shared. Do not edit someone else's collaborative playlist unless asked.
- Keep the working files on the person's machine. Before sharing the skill or its outputs, remove account names, playlist URLs and the names of people who share playlists.

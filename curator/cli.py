"""curator: command line for the API version.

  curator login                      one-time Spotify login (PKCE)
  curator audit                      read your listening data, write the taste profile
  curator plan "brief" --name slug   ask Claude for a plan, match it to Spotify, show it
  curator build slug                 create the playlist after you approve it
  curator verify slug                compare what Spotify saved with the plan
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from . import audit as audit_mod
from .build import build, load_json, save_json, summary_table, verify
from .plan import DEFAULT_MODEL, make_plan, manual_prompt, parse_manual_reply, user_message
from .resolve import resolve
from .spotify import Spotify, login

def _load_dotenv(path: Path = Path(".env")) -> None:
    """Read KEY=value lines from .env without overriding variables already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()
DATA = Path(os.environ.get("CURATOR_DATA", "data"))
TOKEN = DATA / "token.json"
REGISTRY = DATA / "created_playlists.json"


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "playlist"


def cmd_login(args):
    client_id = args.client_id or os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        sys.exit("Set SPOTIFY_CLIENT_ID or pass --client-id.")
    login(client_id, args.redirect_uri, TOKEN)
    print("Logged in as", Spotify(TOKEN).me().get("display_name"))


def cmd_audit(args):
    sp = Spotify(TOKEN)
    created = {p["id"] for p in load_json(REGISTRY, [])}
    snap = audit_mod.collect(sp, created, max_saved=args.max_saved, log=lambda m: print(m, flush=True))
    audit_mod.save(snap, DATA / "snapshot.json")
    profile = audit_mod.build_profile(snap)
    audit_mod.save(profile, DATA / "profile.json")
    summary = "\n".join([
        f"Account: {snap['user']['display_name']}",
        f"Evidence: {profile['evidence_size']}",
        "Top artists: " + ", ".join(a["name"] for a in profile["artists"][:25]),
    ])
    (DATA / "audit-summary.txt").write_text(summary + "\n", encoding="utf-8")
    print("\n" + summary)


def cmd_plan(args):
    profile = load_json(DATA / "profile.json")
    if not profile:
        sys.exit("Run `curator audit` first.")
    if args.from_file:
        plan = parse_manual_reply(Path(args.from_file).read_text(encoding="utf-8"))
    elif args.manual or not os.environ.get("ANTHROPIC_API_KEY"):
        slug = args.name or slugify(args.brief)[:40]
        prompt_path = DATA / "runs" / slug / "prompt.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        user = user_message(audit_mod.profile_for_prompt(profile), args.brief, args.count,
                            args.discovery, args.folder or "", args.feedback or "")
        prompt_path.write_text(manual_prompt(user), encoding="utf-8")
        reply_path = prompt_path.parent / "reply.txt"
        print("No API key in use, so here's the manual route:")
        print(f"  1. Open {prompt_path}, copy everything, and paste it into a new Claude chat.")
        print(f"  2. Save Claude's whole reply to {reply_path}")
        print(f"  3. Run:  curator plan \"{args.brief}\" --name {slug} --count {args.count} --from-file {reply_path}")
        return
    else:
        plan = make_plan(audit_mod.profile_for_prompt(profile), args.brief, target_count=args.count,
                         discovery_share=args.discovery, folder=args.folder or "",
                         feedback=args.feedback or "", model=args.model)
    slug = args.name or slugify(plan.name)
    run = DATA / "runs" / slug
    save_json(plan.model_dump(), run / "plan.json")
    resolved = resolve(plan, Spotify(TOKEN), profile, target_count=args.count)
    save_json(resolved, run / "resolved.json")
    print(summary_table(plan.model_dump(), resolved))
    for m in resolved["missing"]:
        print(f"  missing: {m['title']} - {m['artist']} ({m['note']})")
    print(f"\nSaved to {run}. Nothing has been written to Spotify yet.")
    print(f"Approve and create it with:  curator build {slug}")


def cmd_build(args):
    run = DATA / "runs" / args.slug
    plan, resolved = load_json(run / "plan.json"), load_json(run / "resolved.json")
    if not plan or not resolved:
        sys.exit(f"No plan found in {run}. Run `curator plan` first.")
    sp = Spotify(TOKEN)
    print(summary_table(plan, resolved))
    print(f"\nThis will create a new playlist in: {sp.me().get('display_name')}")
    if not args.yes and input("Create it? [y/N] ").strip().lower() != "y":
        sys.exit("Cancelled. Nothing was written.")
    progress = build(sp, plan, resolved, run, REGISTRY, public=args.public)
    report = verify(sp, progress["playlist_id"], resolved)
    save_json(report, run / "verify.json")
    print(f"\nDone: {progress.get('url')}")
    print("Verified OK" if report["ok"] else f"Verification found problems, see {run / 'verify.json'}")


def cmd_verify(args):
    run = DATA / "runs" / args.slug
    progress, resolved = load_json(run / "progress.json"), load_json(run / "resolved.json")
    if not progress:
        sys.exit("That playlist hasn't been built yet.")
    report = verify(Spotify(TOKEN), progress["playlist_id"], resolved)
    save_json(report, run / "verify.json")
    for k in ("planned", "saved", "order_matches", "missing", "unexpected", "duplicates"):
        print(f"{k}: {report[k]}")
    print("OK" if report["ok"] else "Differences found.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="curator", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login")
    s.add_argument("--client-id")
    s.add_argument("--redirect-uri", default=os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"))
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("audit")
    s.add_argument("--max-saved", type=int, default=2000, help="cap on Liked Songs to read")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("plan")
    s.add_argument("brief", help='e.g. "long drives alone, loud guitars, forward motion"')
    s.add_argument("--name", help="short id for this playlist run")
    s.add_argument("--count", type=int, default=25)
    s.add_argument("--discovery", type=float, default=0.3, help="share of discoveries, 0-1")
    s.add_argument("--folder")
    s.add_argument("--feedback", help='standing feedback, e.g. "too safe last time"')
    s.add_argument("--model", default=DEFAULT_MODEL)
    s.add_argument("--manual", action="store_true",
                   help="write the prompt to a file to paste into a Claude chat (default when no API key is set)")
    s.add_argument("--from-file", help="a saved Claude chat reply containing the plan JSON")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("build")
    s.add_argument("slug")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    s.add_argument("--public", action="store_true")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("verify")
    s.add_argument("slug")
    s.set_defaults(func=cmd_verify)

    # Windows consoles default to cp1252, which can't print many song titles.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

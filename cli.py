"""Command-line runner for observation and autonomous task modes."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from . import hunger
from .build_info import build_info
from .planner import LoopWatchdog
from .compatibility import (CompatibilityError, configure_cached_bundle,
                            default_cache_root, doctor, install_bundle,
                            load_lock, preflight_lock)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atrinik-playtester", allow_abbrev=False)
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {build_info()['label']}")
    p.add_argument(
        "--cache",
        type=Path,
        default=default_cache_root(),
        help="compatibility cache (default: %(default)s)",
    )
    p.add_argument("--host", default="host.docker.internal")
    p.add_argument("--port", type=int, default=1728)
    p.add_argument("--transport", choices=("auto", "quic"),
                   default="auto")
    p.add_argument("--quic-port", type=int, default=1730)
    p.add_argument(
        "--certificate-sha256",
        default=os.getenv("ATRINIK_BOT_CERTIFICATE_SHA256", ""),
        help="pinned SHA-256 fingerprint required for QUIC",
    )
    p.add_argument("--account", default=os.getenv("ATRINIK_BOT_ACCOUNT", ""))
    p.add_argument("--password", default=os.getenv("ATRINIK_BOT_PASSWORD", ""))
    p.add_argument("--character", default=os.getenv("ATRINIK_BOT_CHARACTER", ""))
    p.add_argument(
        "--access-code-file", type=Path,
        help="owner-only file containing the invite code; never pass the code in argv",
    )
    p.add_argument(
        "--operator-character",
        default=os.getenv("ATRINIK_BOT_OPERATOR_CHARACTER", ""),
        help="another character on the same account with operator permissions; "
             "enables the owner's operator-assist command (empty: disabled)",
    )
    p.add_argument("--party-name", default=os.getenv("ATRINIK_BOT_PARTY", ""),
                   help="form or rejoin this open party after every login")
    p.add_argument(
        "--chat-rules",
        default=os.getenv("ATRINIK_BOT_CHAT_RULES", ""),
        help="hot-reloadable JSON conversational rules",
    )
    p.add_argument(
        "--runtime-state",
        default=os.getenv("ATRINIK_BOT_RUNTIME_STATE", ""),
        help="SQLite memory path (prefer ATRINIK_BOT_RUNTIME_STATE)",
    )
    p.add_argument(
        "--runtime-content",
        default=os.getenv("ATRINIK_RUNTIME_CONTENT", ""),
        help="collected Atrinik runtime content containing lib/bmaps",
    )
    p.add_argument(
        "--persist-tile-facts", action="store_true",
        default=os.getenv("ATRINIK_BOT_PERSIST_TILE_FACTS", "").strip().casefold()
        in ("1", "true", "yes", "on"),
        help="on remembered no-magic squares, heal with a potion before a spell "
             "(default: off, the facts are only recorded and shown)",
    )
    p.add_argument(
        "--dialogue-edges", action="store_true",
        default=os.getenv("ATRINIK_BOT_DIALOGUE_EDGES", "").strip().casefold()
        in ("1", "true", "yes", "on"),
        help="plan with NPC dialogue voyages and quest-gated exits "
             "(default: off; the owner turns it on after a supervised voyage)",
    )
    p.add_argument(
        "--learned-route-costs", action="store_true",
        default=os.getenv("ATRINIK_BOT_LEARNED_ROUTE_COSTS", "").strip().casefold()
        in ("1", "true", "yes", "on"),
        help="plan with learned route costs, errand budgets and the essential "
             "override (default: off; the learned plans run in the shadow until "
             "the owner approves the flip)",
    )
    p.add_argument(
        "--watch-owner", action="store_true",
        default=os.getenv("ATRINIK_BOT_WATCH_OWNER", "").strip().casefold()
        in ("1", "true", "yes", "on"),
        help="record how the operator character fights while it is in view "
             "and she is not fighting (default: off; recording only)",
    )
    p.add_argument(
        "--food-obsolete", choices=hunger.SETTINGS,
        default=hunger.parse_setting(os.getenv("ATRINIK_BOT_FOOD_OBSOLETE", "")),
        help="whether food never drains on the server: on (the default, "
             "Classic 5.119.0 and later), off (the hunger rules of an older "
             "server) or auto (on once food stays unchanged for half an hour "
             "of moving and fighting)",
    )
    p.add_argument("--register", action="store_true")
    p.add_argument("--create-character", action="store_true")
    p.add_argument("--archetype", default="half_elf_male")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="task", required=True)
    dependencies = sub.add_parser(
        "dependencies", help="install the immutable compatibility bundle")
    dependencies.add_argument("--source-archive", type=Path)
    dependencies.add_argument("--runtime-archive", type=Path)
    dependencies.add_argument("--analyzer-archive", type=Path,
                              help="pre-downloaded farming analyzer release asset")
    dependencies.add_argument("--server-archive", type=Path,
                              help="pre-downloaded Classic server source archive")
    dependencies.add_argument("--offline", action="store_true")
    dependencies.add_argument("--json", action="store_true")
    preflight = sub.add_parser(
        "preflight", help="verify every locked repository, release, asset, and digest")
    preflight.add_argument("--json", action="store_true")
    diagnose = sub.add_parser(
        "doctor", help="diagnose locked protocol, pathfinding, and content inputs")
    diagnose.add_argument(
        "--preflight", action="store_true",
        help="also verify the locked release metadata over HTTPS",
    )
    diagnose.add_argument("--json", action="store_true")
    sub.add_parser("observe")
    web = sub.add_parser("web", help="run the persistent browser dashboard")
    web.add_argument("--listen", default="127.0.0.1")
    web.add_argument("--web-port", type=int, default=8765)
    autoplay = sub.add_parser(
        "autoplay",
        help="bootstrap and continuously progress without dashboard input")
    autoplay.add_argument("--listen", default="127.0.0.1")
    autoplay.add_argument("--web-port", type=int, default=8765)
    autoplay.add_argument("--until-level", type=int, default=115)
    navigate = sub.add_parser("navigate")
    navigate.add_argument("destination")
    navigate.add_argument("--x", type=int)
    navigate.add_argument("--y", type=int)
    quest = sub.add_parser("quest")
    quest.add_argument("--name", default="Escaping the Deserted Island")
    quest.add_argument("--all", action="store_true",
                       help="finish all 16 formal quests in recommended order")
    farm = sub.add_parser("farm")
    farm.add_argument("--zone", default="")
    farm.add_argument("--target", default="")
    farm.add_argument("--item", default="")
    farm.add_argument("--quantity", type=int, default=1)
    farm.add_argument("--until-level", type=int, default=0)
    farm.add_argument("--combat-skill", default="")
    farm.add_argument("--combat-spell", default="")
    farm.add_argument("--combat-skill-until-level", type=int, default=0)
    bank = sub.add_parser("bank")
    bank.add_argument("--banker", required=True)
    bank.add_argument("--amount", default="all")
    sell = sub.add_parser("sell")
    sell.add_argument("--merchant", default="shop-floor",
                      help="legacy task label; stand on a shop floor to sell")
    sell.add_argument("--junk", action="append", required=True)
    assist = sub.add_parser(
        "operator-assist",
        help="owner only: have the operator character give the running bot's "
             "character gold (at most 1 gold coin, once a day; audited)")
    assist.add_argument("--give-gold", type=int, required=True, metavar="COINS")
    assist.add_argument("--to", required=True, metavar="CHARACTER",
                        help="the character the running bot plays")
    assist.add_argument("--listen", default="127.0.0.1",
                        help="the running bot's dashboard address")
    assist.add_argument("--web-port", type=int, default=8765)
    assist.add_argument("--then-autoplay", action="store_true",
                        help="start autoplay again once the coin is picked up")
    assist.add_argument("--timeout", type=float, default=600.0)
    assist.add_argument(
        "--key-file", type=Path,
        help="the bot's owner-only operator key (default: "
             "ATRINIK_BOT_OPERATOR_KEY_FILE, else operator-assist.key next to "
             "the runtime state, else /var/lib/atrinik-playtester); run as "
             "the bot's service user")
    feedback = sub.add_parser(
        "feedback",
        help="her feedback queue in her memory database: list, show, approve, "
             "reject and edit (no network), and the separate filer (file)")
    feedback_sub = feedback.add_subparsers(dest="feedback_command", required=True)
    feedback_list = feedback_sub.add_parser("list", help="list feedback items")
    feedback_list.add_argument("--kind", default="")
    feedback_list.add_argument("--repo", default="")
    feedback_list.add_argument("--status", default="")
    feedback_list.add_argument("--limit", type=int, default=200)
    feedback_list.add_argument("--json", action="store_true")
    feedback_show = feedback_sub.add_parser(
        "show", help="one item's title and the exact body that would be filed")
    feedback_show.add_argument("item", help="its id, or its fingerprint (pt-...)")
    feedback_show.add_argument("--json", action="store_true")
    feedback_show.add_argument("--markdown", action="store_true",
                               help="print the Markdown copy to file by hand")
    feedback_approve = feedback_sub.add_parser(
        "approve", help="approve one screened item for the filer (run as the bot's "
                        "service user on CT130)")
    feedback_approve.add_argument("item", help="its id, or its fingerprint (pt-...)")
    feedback_approve.add_argument(
        "--file-anyway", action="store_true",
        help="for an item in needs-review: file it despite the possible duplicates")
    feedback_reject = feedback_sub.add_parser("reject", help="reject one item for good")
    feedback_reject.add_argument("item", help="its id, or its fingerprint (pt-...)")
    feedback_reject.add_argument(
        "--duplicate", metavar="ISSUE_URL", default="",
        help="mark it a duplicate of this existing issue instead")
    feedback_edit = feedback_sub.add_parser(
        "edit", help="change an item's title, body, labels, type, repository or "
                     "target; it becomes a draft again and is screened again")
    feedback_edit.add_argument("item", help="its id, or its fingerprint (pt-...)")
    feedback_edit.add_argument("--title")
    feedback_edit.add_argument("--body-file", type=Path,
                               help="the new Markdown body ('-' reads standard input)")
    feedback_edit.add_argument("--labels", help="comma-separated; empty for none")
    feedback_edit.add_argument("--type", dest="issue_type",
                               choices=("Bug", "Feature", "Task", "default"))
    feedback_edit.add_argument("--repo", choices=("atrinik/content", "atrinik/classic",
                                                  "atrinik/playtester"))
    feedback_edit.add_argument("--target", metavar="new-issue|ISSUE_URL",
                               help="a new issue, or a comment on this existing issue")
    feedback_file = feedback_sub.add_parser(
        "file", help="the filer: file approved items with the GitHub App (its own "
                     "systemd unit; without the App key, a dry run)")
    feedback_file.add_argument(
        "--app-id", default=os.getenv("ATRINIK_PLAYTESTER_FEEDBACK_APP_ID", ""),
        help="the GitHub App's ID or client ID")
    feedback_file.add_argument(
        "--app-key-file", type=Path,
        help="the App's private key (default: the unit's systemd credential "
             "feedback-app.pem in $CREDENTIALS_DIRECTORY)")
    feedback_file.add_argument("--dry-run", action="store_true",
                               help="send nothing; print the Markdown copies")
    feedback_file.add_argument("--limit", type=int, default=5,
                               help="at most this many items a run (default: %(default)s)")
    shadow = sub.add_parser(
        "route-shadow",
        help="her shadow planning report, read from her memory database (no network)")
    shadow.add_argument("--hours", type=float, default=24.0,
                        help="how far back to summarise (default: 24)")
    shadow.add_argument("--json", action="store_true")
    deposit = sub.add_parser("deposit")
    deposit.add_argument("--container", required=True)
    deposit.add_argument("--item", action="append", required=True)
    return p


def memory_path(runtime_state: str) -> Path:
    """Her memory database, as ``AtrinikClient`` resolves ``--runtime-state``
    (a legacy ``.json`` path keeps its database beside it)."""
    requested = (Path(runtime_state) if runtime_state else
                 Path(__file__).with_name("bot_memory.sqlite3"))
    return (requested.with_suffix(".sqlite3")
            if requested.suffix.casefold() == ".json" else requested)


FEEDBACK_WRITES = ("approve", "reject", "edit", "file")
NOT_SCREENED = "(not yet screened by the running bot)"


def feedback_command(args: argparse.Namespace) -> int:
    """``feedback list|show|approve|reject|edit|file``: local, and no
    network except ``file`` (the filer, feedback_filer.py).

    Reads the rows of the server, account and character given by
    ``--host``, ``--port``, ``--account`` and ``--character``, as the bot
    stores them; works whether or not the bot is running. ``list`` and
    ``show`` open the database read-only; ``approve``, ``reject``, ``edit``
    and ``file`` change rows but never create or migrate the database.
    """
    import sqlite3
    import sys

    from .feedback import common_terms
    from .memory import BotMemory

    path = memory_path(args.runtime_state)
    if not args.character:
        print("feedback: set --character (or ATRINIK_BOT_CHARACTER)")
        return 2
    if not path.is_file():
        print(f"feedback: no memory database at {path}")
        return 1
    if path.stat().st_uid != os.geteuid():
        # SQLite's -wal and -shm files, and the filer's lock, would belong to
        # another user and could stop the bot from opening its memory.
        print(f"feedback: run this as the user that owns {path} (the bot's service "
              "user)")
        return 2
    writes = args.feedback_command in FEEDBACK_WRITES
    server = f"{args.host.casefold()}:{args.port}"
    # Read-only for list and show: nothing is created, migrated or changed.
    memory = (BotMemory.open_existing if writes else BotMemory.open_read_only)(
        path, server=server, account=args.account)
    try:
        if not memory.has_table("feedback_items"):
            print(f"feedback: {path} has no feedback queue yet (no bot of this "
                  "version has opened it)")
            return 1
        if writes and not (memory.has_columns("feedback_items", ("edited", "file_anyway"))
                           and memory.has_table("feedback_filings")):
            print(f"feedback: {path} has a queue from an older version; start the bot "
                  "of this version once so it can update it")
            return 1
        senders = (memory.chat_senders() if memory.has_table("chat_log") else [])
    except sqlite3.Error as error:
        print(f"feedback: cannot read {path}: {error}")
        return 1
    # Every item is checked against the private terms this command knows
    # (the bot's own screening also uses names seen in its process).
    code = ""
    if args.access_code_file:
        from .access_codes import read_access_code
        try:
            code = read_access_code(args.access_code_file)
        except Exception as error:
            print(f"feedback: cannot read the access code file: {error}")
            return 1
    private = [args.account, args.password, args.host, args.character,
               args.certificate_sha256, args.party_name, args.operator_character,
               code, *senders]
    for term in common_terms(private):
        print(f"feedback: warning: the private term {term!r} is a common word; "
              "items containing it are withheld", file=sys.stderr)
    try:
        if args.feedback_command == "file":
            return _feedback_file(args, memory, path, private)
        if writes:
            return _feedback_decide(args, memory, private)
        return _feedback_read(args, memory, path, private)
    except sqlite3.Error as error:
        print(f"feedback: cannot read the queue in {path} (a database from another "
              f"version, or busy?): {error}")
        return 1


def _gate(items: list[dict], private: list) -> None:
    """Text only for items the running bot screened with its full terms
    since they last changed (feedback.readable), and that pass this
    command's own screen; the rest show metadata only."""
    from .feedback import readable, screen, short_fingerprint

    unscreened = [item for item in items if not readable(item)]
    screen(items, private)
    for item in unscreened:
        if not item["withheld"]:
            item.update(title=NOT_SCREENED, body_md="", subject="", labels_json="[]",
                        issue_type="", error="", screened=False)
    for item in items:
        item.setdefault("screened", not item["withheld"])
        item["short_fingerprint"] = short_fingerprint(item["fingerprint"])
        item.pop("evidence_json", None)


def _feedback_read(args: argparse.Namespace, memory, path: Path, private: list) -> int:
    from .feedback import markdown_copy

    if args.feedback_command == "list":
        items = memory.feedback_items(args.character, kind=args.kind, repo=args.repo,
                                      status=args.status, limit=args.limit)
    else:
        item = memory.feedback_item(args.character, args.item)
        items = [] if item is None else [item]
    _gate(items, private)
    if args.feedback_command == "list":
        for item in items:
            item.pop("body_md", None)
        if args.json:
            print(json.dumps(items, indent=2, sort_keys=True, ensure_ascii=False))
            return 0
        if not items:
            print("No feedback items.")
        for item in items:
            print(f"{item['id']:>5}  {item['short_fingerprint']}  "
                  f"{'withheld' if item['withheld'] else item['status']:<8} "
                  f"{item['kind']:<8} {item['repo']:<19} x{item['occurrences']:<4} "
                  f"{item['title']}")
        return 0
    if not items:
        print(f"feedback: no single item matches {args.item!r}")
        return 1
    item, = items
    if args.json:
        print(json.dumps(item, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    if args.markdown:
        if not item["screened"]:
            print(f"feedback: #{item['id']} is withheld or {NOT_SCREENED}")
            return 1
        print(markdown_copy(item), end="")
        return 0
    print(f"#{item['id']} {item['short_fingerprint']} "
          f"{'withheld' if item['withheld'] else item['status']} {item['kind']} "
          f"-> {item['repo']} ({item['target']})")
    print(f"Labels: {', '.join(json.loads(item['labels_json'] or '[]')) or 'none'}"
          f"{'; type ' + item['issue_type'] if item['issue_type'] else ''}")
    if item.get("issue_url"):
        print(f"Issue: {item['issue_url']}")
    if item["screened"] and item.get("error"):
        print(f"Error: {item['error']}")
    if item.get("remote_candidates_json") and item["status"] == "needs-review":
        for candidate in json.loads(item["remote_candidates_json"] or "[]"):
            print(f"Possible duplicate ({candidate.get('match')}): {candidate.get('url')}")
    print(f"Title: {item['title']}")
    print()
    print(item["body_md"], end="")
    return 0


def _feedback_decide(args: argparse.Namespace, memory, private: list) -> int:
    """``approve``, ``reject`` and ``edit`` (owner decision, 2026-11-19: the
    CLI is the only way to decide; the dashboard stays read-only). Each
    changes one row in one statement, only if it has not changed since it
    was read here."""
    import re
    import sys

    from . import feedback

    item = memory.feedback_item(args.character, args.item)
    if item is None:
        print(f"feedback: no single item matches {args.item!r}")
        return 1
    item_id, status, now = int(item["id"]), str(item["status"]), time.time()
    label = f"#{item_id} {feedback.short_fingerprint(item['fingerprint'])}"
    if status not in ("draft", "approved", "needs-review", "failed", "snoozed"):
        print(f"feedback: {label} is {status}; it can no longer be decided")
        return 1
    scope = {"expect_status": (status,), "updated_at": item["updated_at"]}
    changed = (f"feedback: {label} changed meanwhile (the bot, the filer or another "
               "command); look at it again")

    if args.feedback_command == "reject":
        fields = {"status": "rejected", "decided_at": now, "decided_via": "cli"}
        if args.duplicate:
            if re.fullmatch(r"https://github\.com/(atrinik/(content|classic|playtester))"
                            r"/issues/\d+", args.duplicate) is None:
                print("feedback: --duplicate must be an issue URL in atrinik/content, "
                      "atrinik/classic or atrinik/playtester")
                return 2
            fields.update(status="duplicate", issue_url=args.duplicate)
        if not memory.update_feedback(args.character, item_id, fields=fields, **scope):
            print(changed)
            return 1
        print(f"{'Marked' if args.duplicate else 'Rejected'} {label}"
              f"{' a duplicate of ' + args.duplicate if args.duplicate else ''}.")
        return 0

    # Approve and edit need text the running bot screened since it changed.
    if item["withheld"]:
        print(f"feedback: {label} is withheld (it names a private term); it can only "
              "be rejected")
        return 1
    if not feedback.readable(item):
        print(f"feedback: {label} is {NOT_SCREENED} since it last changed; the bot "
              "screens it within about a minute while it runs")
        return 1
    if feedback.screen([dict(item)], private):
        memory.withhold_feedback(args.character, [item_id], timeout=10.0)
        print(f"feedback: {label} names a private term; it is withheld")
        return 1

    if args.feedback_command == "approve":
        if status == "approved":
            print(f"feedback: {label} is already approved")
            return 0
        if status == "needs-review" and not args.file_anyway:
            print(f"feedback: {label} has possible duplicates (see `feedback show`); "
                  "reject it with --duplicate ISSUE_URL, or approve it with --file-anyway")
            return 1
        if args.file_anyway and status != "needs-review":
            print("feedback: --file-anyway is only for an item in needs-review")
            return 2
        fields = {"status": "approved", "decided_at": now, "decided_via": "cli",
                  "error": None, "file_anyway": int(args.file_anyway)}
        if not memory.update_feedback(args.character, item_id, fields=fields,
                                      require_screened=True, **scope):
            print(changed)
            return 1
        print(f"Approved {label}. The filer files it once the running bot has screened it "
              "again; without the App key it stays approved, and this is its Markdown "
              "copy to file by hand:\n")
        print(feedback.markdown_copy(item), end="")
        return 0

    # edit: the new text is cleaned, re-rendered or kept, and screened.
    try:
        title = feedback.clean_title(args.title) if args.title is not None else item["title"]
        labels = (feedback.clean_labels(args.labels.split(","))
                  if args.labels is not None else feedback.labels(item))
        # As the filer will send them: with the playtester label, at most ten.
        feedback.clean_labels([*labels, feedback.FILED_LABEL])
        issue_type = (("" if args.issue_type == "default" else args.issue_type)
                      if args.issue_type is not None else item["issue_type"])
        repo = args.repo or item["repo"]
        target = item["target"]
        if args.target is not None:
            target = ("new-issue" if args.target == "new-issue"
                      else f"comment:{args.target}")
        if target != "new-issue":
            if re.fullmatch(rf"comment:https://github\.com/{re.escape(repo)}/issues/\d+",
                            target) is None:
                raise ValueError(f"the target must be an issue URL in {repo}")
        if args.body_file is not None:
            raw = (sys.stdin.read(feedback.BODY_CHARS * 2) if str(args.body_file) == "-"
                   else args.body_file.read_text(encoding="utf-8")[:feedback.BODY_CHARS * 2])
            body = feedback.clean_body(raw, item["fingerprint"])
        elif item.get("edited"):
            body = feedback.clean_body(item["body_md"], item["fingerprint"])
        else:
            body = feedback.rerender(item)
    except (OSError, ValueError) as error:
        print(f"feedback: not edited: {error}")
        return 2
    fields = {"title": title, "body_md": body, "labels_json": json.dumps(labels),
              "issue_type": issue_type, "repo": repo, "target": target}
    candidate = {**item, **fields}
    if feedback.screen([candidate], private):
        print("feedback: not edited: the new text names a private term")
        return 1
    # Timestamped only now, after the body was read and screened, so a
    # bot pass in between cannot count the new text as screened.
    fields.update(status="draft", edited=1, updated_at=time.time(), error=None,
                  file_anyway=0,
                  remote_candidates_json=None)
    if not memory.update_feedback(args.character, item_id, fields=fields,
                                  require_screened=True, **scope):
        print(changed)
        return 1
    print(f"Edited {label}; it is a draft again. The running bot screens it within "
          "about a minute; then approve it.")
    return 0


def _feedback_file(args: argparse.Namespace, memory, path: Path, private: list) -> int:
    """The filer (feedback_filer.py), one run under an exclusive lock."""
    import fcntl

    from .feedback_filer import Filer, FilingError, key_path, load_app_key

    lock_path = path.with_name(path.name + ".filer.lock")
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
                             os.O_CLOEXEC, 0o600)
    except OSError as error:
        print(f"feedback: cannot open the filer's lock: {error.strerror or error}")
        return 1
    with os.fdopen(descriptor, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("feedback: another filer run is in progress")
            return 1
        key = None
        located = key_path(args.app_key_file)
        if not args.dry_run and located is not None and located.exists():
            try:
                key = load_app_key(located)
            except FilingError as error:
                print(f"ERROR {error}")
                return 2
        elif not args.dry_run and args.app_key_file is not None:
            print("ERROR the App key file does not exist")
            return 2
        filer = Filer(memory, args.character, private=private, app_id=args.app_id, key=key)
        del key
        return filer.run(dry_run=args.dry_run, limit=args.limit)


def route_shadow_command(args: argparse.Namespace) -> int:
    """``route-shadow``: the shadow planner's divergence report (ROUTING P6,
    for the P7 and P8 gates), read-only, local, no network: per trip kind,
    how often the learned plan agreed with the live one, took another
    route, or would decline or override the trip; the subjects behind the
    differences; and the newest differences."""
    import sqlite3
    import time

    from .memory import BotMemory
    from .route_shadow import summarise_rows

    path = memory_path(args.runtime_state)
    if not args.character:
        print("route-shadow: set --character (or ATRINIK_BOT_CHARACTER)")
        return 2
    if not path.is_file():
        print(f"route-shadow: no memory database at {path}")
        return 1
    memory = BotMemory.open_read_only(path, server=f"{args.host.casefold()}:{args.port}",
                                      account=args.account)
    since = time.time() - max(0.0, args.hours) * 3600.0
    try:
        if not memory.has_table("route_shadow"):
            print(f"route-shadow: {path} has no shadow rows yet (no bot of this "
                  "version has opened it)")
            return 1
        rows = memory.route_shadow_rows(args.character, since=since)
    except sqlite3.Error as error:
        print(f"route-shadow: cannot read {path}: {error}")
        return 1
    report = summarise_rows(rows)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    print(f"{report['rows']} comparisons in the last {args.hours:g} h")
    if report["zero_length"]:
        print(f"  ({report['zero_length']} zero-length trips ignored)")
    for kind, trips in sorted(report["counts"].items()):
        for trip, results in sorted(trips.items()):
            total = sum(results.values())
            parts = ", ".join(f"{result} {count}" for result, count in sorted(results.items()))
            print(f"  {kind:<4} {trip:<10} {total:>5}: {parts}")
    if report["subjects"]:
        print("Subjects behind the differences:")
        for subject, count in report["subjects"]:
            print(f"  {count:>4}  {subject}")
    if report["differences"]:
        print("Newest differences:")
    for row in report["differences"]:
        detail = row["detail"]
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(row["at"]))
        where = (f"{detail.get('source', '')} -> {detail.get('destination', '')}"
                 if row["kind"] == "trip" else
                 f"live {(detail.get('live') or {}).get('name', '')} / shadow "
                 f"{(detail.get('shadow') or {}).get('name', '')}")
        why = detail.get("why") or detail.get("reason") or ""
        print(f"  {stamp} {row['kind']} {row['trip']} {row['result']}: {where}"
              f"{'; ' + why if why else ''}")
    return 0


def autoplay_hold_reason(client) -> str:
    """Why the ``autoplay`` command must start idle, or "" to autoplay.

    The owner's dashboard Stop is persisted (``set_operator_stopped``) and
    outlives a crash or restart: the service restarts this command, which
    would otherwise override the Stop. An unreadable flag also holds.
    """
    try:
        if client.operator_stopped():
            return "stopped by operator (kept across restart)"
    except Exception:
        # Unknown: stay idle rather than override a Stop.
        logging.getLogger(__name__).exception(
            "cannot read the persisted operator stop")
        return "persisted operator stop unreadable"
    return ""


async def main_async(args: argparse.Namespace) -> None:
    from .autoplay import AutoplayTask, IdleSurvival
    from .catalog_quest_tasks import (AllFormalQuestsTask, CatalogQuestTask,
                                      POLICIES)
    from .client import AtrinikClient, ClientConfig
    from .farm_analyzer import FarmAnalyzer, run_periodically
    from .navigation import NavigateTask, NavigateThenTask, WorldGraph
    from .quest_tasks import EscapingDesertedIslandTask
    from .tasks import (BankTask, DepositItemsTask, FarmTask, JunkPolicy,
                        SellJunkTask, TaskEngine)
    from .web_server import WebControlServer

    from .access_codes import read_access_code

    code = read_access_code(args.access_code_file) if args.access_code_file else ""
    config = ClientConfig(
        host=args.host, port=args.port, account=args.account,
        password=args.password, character=args.character,
        access_code=code,
        party_name=args.party_name,
        transport=args.transport, quic_port=args.quic_port,
        certificate_sha256=args.certificate_sha256,
        chat_rules_path=args.chat_rules,
        runtime_state_path=args.runtime_state,
        runtime_content_path=args.runtime_content,
        persist_tile_facts=args.persist_tile_facts,
        dialogue_edges=args.dialogue_edges,
        learned_route_costs=args.learned_route_costs,
        operator_character=args.operator_character.strip(),
        watch_owner=args.watch_owner,
        food_obsolete=args.food_obsolete,
        register=args.register, create_character=args.create_character,
        character_archetype=args.archetype,
    )
    client = AtrinikClient(config)
    if args.operator_character:
        # Never in a feedback draft (feedback.py, 4.10.4).
        client.feedback_private_names.add(args.operator_character)
    engine = TaskEngine(client)
    client.chat_context_provider = engine.chat_context
    web_server = None
    if args.task == "navigate":
        if (args.x is None) != (args.y is None):
            raise SystemExit("--x and --y must be supplied together")
        xy = None if args.x is None else (args.x, args.y)
        engine.set_task(NavigateTask(WorldGraph().build(), args.destination, xy))
    elif args.task == "quest":
        graph = WorldGraph().build()
        if args.all:
            engine.set_task(AllFormalQuestsTask(graph))
        elif args.name == "Escaping the Deserted Island":
            engine.set_task(EscapingDesertedIslandTask(graph))
        elif args.name in POLICIES:
            engine.set_task(CatalogQuestTask(graph, POLICIES[args.name]))
        else:
            choices = "\n  ".join(
                ("Escaping the Deserted Island", *sorted(POLICIES)))
            raise SystemExit(f"unknown formal quest {args.name!r}; choose:\n  {choices}")
    elif args.task == "farm":
        farm_task = FarmTask(
            zone=args.zone, target=args.target, item=args.item,
            quantity=args.quantity, level_until=args.until_level,
            combat_skill=args.combat_skill, combat_spell=args.combat_spell,
            combat_skill_until_level=args.combat_skill_until_level,
        )
        if args.zone.startswith("/"):
            engine.set_task(NavigateThenTask(
                WorldGraph().build(), args.zone, farm_task,
                combat_approach=True))
        else:
            engine.set_task(farm_task)
    elif args.task == "bank":
        engine.set_task(BankTask(args.banker, args.amount))
    elif args.task == "sell":
        engine.set_task(SellJunkTask(args.merchant, JunkPolicy(tuple(args.junk))))
    elif args.task == "deposit":
        engine.set_task(DepositItemsTask(args.container, tuple(args.item)))
    elif args.task in ("web", "autoplay"):
        graph = WorldGraph().build()
        # The authored-text index her observation ledger attributes and
        # verifies books with; about 1.3 s, before play starts.
        from .observations import load_authored_content
        client.authored_content = load_authored_content()
        # The authored walkability her fight records read terrain from.
        client.world_graph = graph
        # Index gear archetypes by face now, not on her first quiet tick.
        from .equipment import archetypes_by_face
        try:
            archetypes_by_face()
        except Exception as error:
            logging.getLogger(__name__).warning(
                "gear archetype index unavailable: %s", error)
        # And the disease names her fight records ignore, now too (never raises).
        from .tasks import disease_names
        disease_names()
        bundle = getattr(args, "bundle", None)
        if bundle is not None:
            client.farm_analyzer = FarmAnalyzer.from_bundle(bundle)
        # Stop, or a finished task, leaves her idle but connected: keep
        # her fed (no tasks except survival).
        engine.idle_survival = IdleSurvival(graph)
        if args.task == "autoplay":
            held = autoplay_hold_reason(client)
            if held:
                # The owner pressed Stop before this restart: honour it.
                # Start autoplay on the dashboard clears it.
                logging.getLogger(__name__).warning(
                    "autoplay not started: %s; use Start autoplay on the "
                    "dashboard to resume", held)
                now = time.time()
                engine.last_task = {
                    "name": "autoplay", "status": "failed", "error": held,
                    "started_at": now, "ended_at": now,
                }
            else:
                engine.set_task(AutoplayTask(
                    graph, target_level=args.until_level))
        operator_key = ""
        if args.operator_character:
            from .operator_key import (OperatorKeyError, ensure_operator_key,
                                       operator_key_path)

            try:
                operator_key = ensure_operator_key(
                    operator_key_path(args.runtime_state))
            except OperatorKeyError as error:
                # The game goes on; only operator assist stays unavailable.
                logging.getLogger(__name__).error(
                    "operator assist unavailable: %s", error)
        web_server = WebControlServer(
            client, engine, graph, args.listen, args.web_port,
            operator_character=args.operator_character,
            operator_key=operator_key)

    async def report(event):
        if event.kind in ("playing", "characters", "map", "quests", "message"):
            value = event.data
            if event.kind == "quests":
                value = {
                    "quests": {name: {
                        "status": quest.status,
                        "parts": [
                            {"name": part.name, "status": part.status,
                             "current": part.current, "required": part.required}
                            for part in quest.parts
                        ],
                    }
                    for name, quest in value.items()
                    },
                    "inventory": [
                        {"tag": item.tag, "name": item.name,
                         "quantity": item.quantity, "location": item.location}
                        for item in client.state.inventory
                    ],
                }
            print(json.dumps({"event": event.kind, "data": value}, default=str))

    if args.task not in ("web", "autoplay"):
        client.add_handler(report)
    tasks = [asyncio.create_task(client.run())]
    if args.task != "observe":
        tasks.append(asyncio.create_task(engine.run()))
    if web_server is not None:
        tasks.append(asyncio.create_task(web_server.serve()))
    # Screens approved and edited feedback with her full private terms,
    # which the CLI's approve and the filer require (phase F6), in every
    # mode; it never ends by itself, so it is cancelled when the rest end.
    screening = asyncio.create_task(client.screen_feedback_periodically())
    # Records every event-loop stall over a second, with what was running
    # (planner.LoopWatchdog, docs/ROUTING.md 1.12).
    watching = asyncio.create_task(
        LoopWatchdog(client, describe=engine.running_context).run())
    if client.farm_analyzer is not None:
        tasks.append(asyncio.create_task(
            run_periodically(client.farm_analyzer, client)))
    try:
        await asyncio.gather(*tasks)
    finally:
        screening.cancel()
        watching.cancel()
        if client.farm_analyzer is not None:
            await client.farm_analyzer.close()
        if web_server is not None:
            await web_server.close()
        await client.close()


def operator_assist(args: argparse.Namespace) -> int:
    """Ask the running bot's dashboard to run one operator assist.

    The running bot does the work: it already holds the account login, and
    it keeps its character in place meanwhile. This process only posts the
    request to the loopback dashboard and prints the audit record.
    """
    import http.client
    import ipaddress

    from .operator_key import (KEY_HEADER, OperatorKeyError, operator_key_path,
                               read_operator_key)

    try:
        key = read_operator_key(args.key_file or operator_key_path(args.runtime_state))
    except OperatorKeyError as error:
        print(f"ERROR {error}; run this as the bot's service user")
        return 2

    try:
        loopback = (args.listen.casefold() == "localhost" or
                    ipaddress.ip_address(args.listen).is_loopback)
    except ValueError:
        loopback = False
    if not loopback:
        print("ERROR --listen must be a loopback address: the dashboard "
              "only listens there")
        return 2

    def post(path: str, data: dict, headers: dict | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(
            args.listen, args.web_port, timeout=args.timeout)
        try:
            connection.request(
                "POST", path, json.dumps(data),
                {"Content-Type": "application/json", "Host": "localhost",
                 **(headers or {})})
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {"error": raw.decode("utf-8", "replace")[:500]}
        return response.status, body if isinstance(body, dict) else {}

    try:
        status, result = post("/api/operator-assist",
                              {"gold": args.give_gold, "to": args.to},
                              {KEY_HEADER: key})
    except TimeoutError:
        print(f"ERROR no answer within {args.timeout:g} s; the run may still "
              "be in progress: check the dashboard and its operator_assist record")
        return 2
    except OSError as error:
        print(f"ERROR cannot reach the bot's dashboard at "
              f"{args.listen}:{args.web_port}: {error}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result.get("ok"):
        print(f"operator assist {result.get('outcome', 'failed')} "
              f"(HTTP {status}): {result.get('error') or result.get('record', {}).get('reason', '')}")
        return 1
    if args.then_autoplay:
        try:
            status, started = post("/api/task", {"type": "autoplay", "until_level": 115})
        except OSError as error:
            print(f"ERROR autoplay did not start: {error}")
            return 1
        if status != 200:
            print(f"ERROR autoplay did not start (HTTP {status}): {started.get('error', '')}")
            return 1
        print("autoplay started")
    return 0


def main() -> None:
    args = parser().parse_args()
    if args.task == "feedback":
        # Local: needs no game inputs; only ``feedback file`` (the filer,
        # its own unit) talks to GitHub.
        raise SystemExit(feedback_command(args))
    if args.task == "route-shadow":
        raise SystemExit(route_shadow_command(args))
    if args.task == "operator-assist":
        # Needs no game inputs: it only talks to the running bot.
        raise SystemExit(operator_assist(args))
    try:
        lock = load_lock()
    except CompatibilityError as error:
        raise SystemExit(f"invalid compatibility lock: {error}") from error
    if args.task == "preflight":
        try:
            result = preflight_lock(lock)
        except CompatibilityError as error:
            result = {"ok": False, "checks": [], "error": str(error)}
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        elif result["ok"]:
            for check in result["checks"]:
                print(
                    f"OK    {check['name']}: {check['repository']}@{check['release']} "
                    f"{check['artifact']} sha256:{check['sha256']}"
                )
        else:
            print(f"ERROR lock-availability: {result['error']}")
        if not result["ok"]:
            raise SystemExit(1)
        return
    if args.task == "dependencies":
        try:
            installed = install_bundle(
                args.cache,
                lock,
                source_archive=args.source_archive,
                runtime_archive=args.runtime_archive,
                analyzer_archive=args.analyzer_archive,
                server_archive=args.server_archive,
                offline=args.offline,
            )
        except CompatibilityError as error:
            raise SystemExit(f"dependency installation failed: {error}") from error
        result = {
            "content_revision": lock["inputs"]["content"]["revision"],
            "root": str(installed["root"]),
            "runtime": str(installed["runtime"]),
            "source": str(installed["source"]),
        }
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(f"Installed compatibility bundle: {result['root']}")
            print(f"Content source: {result['source']}")
            print(f"Classic runtime: {result['runtime']}")
        return
    if args.task == "doctor":
        result = doctor(args.cache, lock, verify_remote=args.preflight)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            for check in result["checks"]:
                print(f"{check['status'].upper():5} {check['name']}: {check['detail']}")
        if not result["ok"]:
            raise SystemExit(1)
        return
    try:
        installed = configure_cached_bundle(args.cache, lock)
    except CompatibilityError as error:
        raise SystemExit(
            f"compatibility bundle unavailable: {error}\n"
            "Run 'atrinik-playtester dependencies' first."
        ) from error
    if not args.runtime_content:
        args.runtime_content = str(installed["runtime"])
    args.bundle = installed
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    build = build_info()
    logging.getLogger(__name__).info(
        "atrinik-playtester %s%s", build["label"],
        f" ({build['commit']})" if build["commit"] else "")
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

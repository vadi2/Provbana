"""Clear the results daily while the service is open for practice.

    .venv/bin/python reset.py --loop        # daily, alongside the service
    .venv/bin/python reset.py --status      # what is scheduled, and when it stops
    .venv/bin/python reset.py --yes         # clear now

The service goes up months before the hackathon so teams can shake their
bundles out against the real validator. Without this, whoever tried it first in
September is still sitting at the top of the leaderboard in November. A daily
wipe keeps the board meaningless until it needs to mean something, which is
what makes it safe to hand the URL out early.

PRACTICE_ENDS is the entire configuration: one UTC timestamp that says both
when the daily wipe runs and when the wipes stop. The wipe happens at that time
of day, every day, and the one falling exactly on PRACTICE_ENDS is the last -
so 2026-11-18T03:00:00Z means "wipe at 03:00 UTC daily, final wipe the morning
the doors open". Afterwards the loop stays up and does nothing, deliberately:
the one process that can empty the leaderboard must be incapable of doing it
during the event, whether or not anybody remembers to turn it off. Unset the
variable and no wipe is ever scheduled.

Not to be confused with `seed_demo.py --reset`, which deletes DATA_DIR
wholesale and needs the service stopped. This clears the results in place and
leaves the directory, the schema and the service alone, so it can run on a
timer against a service that is being used.
"""

import argparse
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.environ.get("DATA_DIR") or os.path.join(HERE, "data")

DB_NAME = "connectathon.db"
# Child rows first: renderings reference submissions and teams, so deleting in
# this order never leaves a row pointing at one that has gone.
TABLES = ("renderings", "submissions", "teams")
# Each holds one directory per team slug. The trees themselves stay - app.py
# creates them at boot and expects to find them.
TREES = ("submissions", "renderings")
# Long enough that an idle container is not spinning, short enough that a
# suspended container or a stepped clock cannot overshoot a wipe by a day.
MAX_SLEEP = 300


def log(msg: str) -> None:
    print("%s %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg),
          flush=True)


def practice_ends(raw: "str | None" = None) -> "datetime | None":
    """Parse PRACTICE_ENDS into an aware UTC datetime, or None when unset.

    Naive timestamps are read as UTC rather than as the box's local time: the
    event is in Sweden, the box is billed in Germany, and a wipe that lands an
    hour out because someone omitted the Z is not worth the convenience.
    """
    if raw is None:
        raw = os.environ.get("PRACTICE_ENDS", "")
    raw = raw.strip()
    if not raw:
        return None
    # fromisoformat only learned to accept a trailing "Z" in 3.11. The image is
    # on 3.12, but this keeps the script runnable in an older local venv.
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    stamp = datetime.fromisoformat(raw)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def next_run(after: datetime, ends: datetime) -> "datetime | None":
    """The first daily wipe at or after `after`, or None once they are over.

    The daily slot is PRACTICE_ENDS' own time of day, and the slot landing
    exactly on PRACTICE_ENDS is the last one to run. The boundary is decided by
    comparing the *scheduled* instant against PRACTICE_ENDS rather than the
    clock: a loop woken a few seconds late must still perform the wipe it was
    woken for, and must still refuse the one after it.
    """
    run = after.replace(
        hour=ends.hour, minute=ends.minute, second=ends.second, microsecond=0
    )
    if run < after:
        run += timedelta(days=1)
    return run if run <= ends else None


def clear(data_dir: str) -> "dict[str, int]":
    """Delete every result in place. Returns what each table lost."""
    removed = {}
    db = os.path.join(data_dir, DB_NAME)
    if os.path.exists(db):
        conn = sqlite3.connect(db)
        try:
            present = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            # One transaction for the lot: a leaderboard read landing mid-wipe
            # sees the board either before or after, never a team whose
            # submissions have already gone.
            conn.execute("BEGIN IMMEDIATE")
            for table in TABLES:
                if table not in present:
                    continue
                removed[table] = conn.execute(
                    "SELECT count(*) FROM %s" % table
                ).fetchone()[0]
                conn.execute("DELETE FROM %s" % table)
            conn.commit()
        finally:
            conn.close()

    # After the rows, never before. A bundle whose row has gone is simply
    # unreachable; a row whose bundle has gone is a 500 on the way out.
    for tree in TREES:
        path = os.path.join(data_dir, tree)
        if not os.path.isdir(path):
            continue
        for name in os.listdir(path):
            target = os.path.join(path, name)
            if os.path.isdir(target):
                shutil.rmtree(target)
    return removed


def describe(removed: "dict[str, int]") -> str:
    if not removed or not any(removed.values()):
        return "nothing to clear"
    return ", ".join("%d %s" % (n, t) for t, n in removed.items() if n)


def run_loop(data_dir: str, ends: datetime) -> int:
    pending = next_run(datetime.now(timezone.utc), ends)
    log("practice resets at %s daily, last one %s"
        % (ends.strftime("%H:%M UTC"), ends.strftime("%Y-%m-%d")))
    while pending is not None:
        delay = (pending - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            time.sleep(min(delay, MAX_SLEEP))
            continue
        log("resetting (%s slot): %s" % (pending.isoformat(), describe(clear(data_dir))))
        # Step a second past the slot just run so the next lookup lands on
        # tomorrow's rather than repeating this one.
        pending = next_run(pending + timedelta(seconds=1), ends)
    log("practice period is over - the leaderboard is now the event's, and "
        "this process will not touch it again")
    # Idling rather than exiting: compose restarts an exited service, and a
    # container flapping in the logs during the event looks like a fault.
    while True:
        time.sleep(3600)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Clear the results while the service is open for practice.",
        epilog="PRACTICE_ENDS (UTC, e.g. 2026-11-18T03:00:00Z) sets both the "
               "daily wipe time and the last day one runs.",
    )
    ap.add_argument("--loop", action="store_true",
                    help="wipe daily until PRACTICE_ENDS, then idle")
    ap.add_argument("--status", action="store_true",
                    help="print the schedule and what is currently stored")
    ap.add_argument("--yes", action="store_true",
                    help="required to wipe once, now - this deletes every team, "
                         "submission and screenshot")
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    args = ap.parse_args()

    try:
        ends = practice_ends()
    except ValueError as exc:
        print("PRACTICE_ENDS is not a valid timestamp: %s" % exc, file=sys.stderr)
        return 2

    if args.status:
        if ends is None:
            print("PRACTICE_ENDS is unset - no resets are scheduled")
        else:
            upcoming = next_run(datetime.now(timezone.utc), ends)
            print("practice ends: %s" % ends.isoformat())
            print("next reset:    %s" % (upcoming.isoformat() if upcoming
                                         else "none - the practice period is over"))
        return 0

    if args.loop:
        if ends is None:
            log("PRACTICE_ENDS is unset - no resets scheduled, idling")
            while True:
                time.sleep(3600)
        return run_loop(args.data_dir, ends)

    if not args.yes:
        print("This deletes every team, submission and screenshot in %s.\n"
              "Re-run with --yes if that is what you want, or --status to see "
              "what is scheduled." % args.data_dir, file=sys.stderr)
        return 2
    log("resetting on request: %s" % describe(clear(args.data_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

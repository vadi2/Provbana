"""Snapshot the event's results on a timer, and put them back if something breaks.

    .venv/bin/python backup.py --loop              # every 15 minutes, alongside the service
    .venv/bin/python backup.py                     # one snapshot, now
    .venv/bin/python backup.py --list
    .venv/bin/python backup.py --restore LATEST --yes

A snapshot is everything a result is made of: the database, the exact bundle
bytes in `submissions/` and the uploaded screenshots in `renderings/`. Scores
are recomputed from none of it - the database *is* the leaderboard - so a
snapshot has to be internally consistent, which is why the database is copied
through SQLite's online backup API rather than with `cp`. Copying a file that
is being written to yields a torn database that opens fine and is missing the
last transaction.

The heavy directories are hard-linked, not copied. A submission's bytes and an
uploaded screenshot are written once and never edited, so a link is the same
bytes for none of the disk; a snapshot of a 5 GB event costs kilobytes. Deleting
a rendering through the API unlinks the live copy and leaves the snapshot's link
intact, which is the point.

Only you can restore
--------------------
Restore is this script, run on this machine, by the user who owns the backup
directory. It is deliberately not reachable from the service: there is no HTTP
endpoint, no token that grants it and nothing a participant on the venue network
can send that will trigger it. The backup directory is created 0700 and the
database copy 0600; the bundles and screenshots are hard links, so they cannot
carry a mode of their own and keep whatever the service wrote. The 0700
directory is therefore what stops another account on the same laptop reading
the results or swapping a snapshot for a doctored one.

Restoring never destroys anything. The current data directory is renamed aside
(`data.replaced-<timestamp>`) and a *copy* of the snapshot is renamed into its
place - the snapshot itself is never moved, so it can be restored again - and a
restore can itself be undone by renaming the two back. Stop the service first:
the rename is atomic, but a running service keeps writing into the directory it
already has open, and those writes would land in the set-aside copy.
"""

import argparse
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.environ.get("DATA_DIR") or os.path.join(HERE, "data")
# Beside the data directory, not inside it: `seed_demo.py --reset` and a
# restore both move the data directory wholesale, and backups must survive that.
DEFAULT_BACKUPS = os.environ.get("BACKUP_DIR") or os.path.join(HERE, "backups")

DB_NAME = "connectathon.db"
TREES = ("submissions", "renderings")
STAMP = "%Y%m%dT%H%M%SZ"
# 15-minute snapshots for 24 hours. An event day is shorter than that, so the
# pruner never reaches back past the start of the day it is protecting.
DEFAULT_KEEP = 96
DEFAULT_EVERY = 900


class SnapshotError(Exception):
    """A snapshot could not be taken. An ordinary Exception on purpose, so the
    --loop guard catches it and keeps the schedule; main() turns it into a
    SystemExit on the one-shot path, where exiting is the right answer."""


def log(msg):
    print("%s  %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), msg), flush=True)


def snapshot_name(when=None):
    return (when or datetime.now(timezone.utc)).strftime(STAMP)


def is_snapshot(name):
    try:
        datetime.strptime(name, STAMP)
    except ValueError:
        return False
    return True


def list_snapshots(backup_dir):
    if not os.path.isdir(backup_dir):
        return []
    return sorted(n for n in os.listdir(backup_dir)
                  if is_snapshot(n) and os.path.isdir(os.path.join(backup_dir, n)))


def copy_db(src, dest):
    """SQLite's own backup API: consistent even while the service is writing."""
    source = sqlite3.connect("file:%s?mode=ro" % src, uri=True, timeout=30)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    os.chmod(dest, 0o600)


def link_tree(src, dest):
    """Hard-link a write-once tree; fall back to copying across filesystems."""
    try:
        shutil.copytree(src, dest, copy_function=os.link)
    except OSError as exc:
        # copytree creates the directories before it copies anything, and on a
        # cross-device link it collects every per-file EXDEV and raises at the
        # end - so `dest` is already there, half-built. Clear it before the
        # retry: copytree refuses an existing directory, and copy2 onto a file
        # that is already a hard link to its own source raises SameFileError.
        # Say so, because a snapshot that quietly became a full copy is the
        # difference between kilobytes and gigabytes a day.
        log("linking failed (%s) - copying %s instead" % (exc, src))
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest, copy_function=shutil.copy2)


def dir_stats(path):
    files = total = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            files += 1
            try:
                total += os.stat(os.path.join(root, n)).st_size
            except OSError:
                pass
    return files, total


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def take_snapshot(data_dir, backup_dir, keep):
    db = os.path.join(data_dir, DB_NAME)
    if not os.path.isfile(db):
        # Not SystemExit: it derives from BaseException, so the loop's
        # `except Exception` guard below would miss the one error this module
        # raises on purpose, and the daemon would exit where it meant to log.
        # That is most likely on a cold volume, where backup can start before
        # app has created the database, and during a restore, which moves the
        # data directory aside for a moment.
        raise SnapshotError("no database at %s - is DATA_DIR right?" % db)

    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    os.chmod(backup_dir, 0o700)

    name = snapshot_name()
    final = os.path.join(backup_dir, name)
    # Build under a partial name and rename at the end, so an interrupted run
    # never leaves something that looks like a usable snapshot.
    staging = final + ".partial"
    if os.path.exists(staging):
        shutil.rmtree(staging)
    if os.path.exists(final):
        # Same second as the previous run; nothing new to say.
        files, total = dir_stats(final)
        return final, files, total, 0

    os.makedirs(staging, mode=0o700)
    try:
        copy_db(db, os.path.join(staging, DB_NAME))
        for tree in TREES:
            src = os.path.join(data_dir, tree)
            if os.path.isdir(src):
                link_tree(src, os.path.join(staging, tree))
        os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    files, total = dir_stats(final)
    pruned = prune(backup_dir, keep)
    return final, files, total, pruned


def prune(backup_dir, keep):
    snaps = list_snapshots(backup_dir)
    dropped = 0
    for old in snaps[:max(0, len(snaps) - keep)]:
        # Counting the attempt rather than the success would report the
        # retention promise as kept on every cycle while the directory grew
        # without bound - a snapshot this uid cannot unlink is recounted daily.
        try:
            shutil.rmtree(os.path.join(backup_dir, old))
        except OSError as exc:
            log("could not prune %s: %s" % (old, exc))
            continue
        dropped += 1
    return dropped


def resolve(backup_dir, which):
    snaps = list_snapshots(backup_dir)
    if not snaps:
        raise SystemExit("no snapshots in %s" % backup_dir)
    if which in ("LATEST", "latest", None):
        return snaps[-1]
    if which in snaps:
        return which
    raise SystemExit("no snapshot named %r - try --list" % which)


def unique_path(base):
    """`base`, or base-2, base-3 ... - whichever is free.

    The set-aside and staging names are stamped to the second, and two restores
    in the same second would otherwise collide: os.rename onto a non-empty
    directory fails, and it fails halfway through, leaving the data directory
    untouched and a stray incoming copy behind."""
    if not os.path.exists(base):
        return base
    n = 2
    while os.path.exists("%s-%d" % (base, n)):
        n += 1
    return "%s-%d" % (base, n)


def owner_check(backup_dir):
    """The backups are the operator's. Refuse to act on someone else's."""
    try:
        owner = os.stat(backup_dir).st_uid
    except OSError as exc:
        raise SystemExit("cannot read %s: %s" % (backup_dir, exc))
    if owner != os.getuid():
        raise SystemExit(
            "%s belongs to uid %d, you are uid %d - restore is only for the "
            "account that owns the backups" % (backup_dir, owner, os.getuid()))


def restore(data_dir, backup_dir, which, confirmed):
    owner_check(backup_dir)
    name = resolve(backup_dir, which)
    snap = os.path.join(backup_dir, name)
    files, total = dir_stats(snap)

    conn = sqlite3.connect(os.path.join(snap, DB_NAME))
    try:
        teams = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
        subs = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    finally:
        conn.close()

    aside = unique_path("%s.replaced-%s" % (data_dir.rstrip("/"), snapshot_name()))
    print("Restore %s\n"
          "  contains   %d companies, %d submissions, %d files (%s)\n"
          "  into       %s\n"
          "  current    %s -> moved aside to %s (nothing is deleted)\n"
          % (snap, teams, subs, files, human(total), data_dir,
             data_dir, aside))
    if not confirmed:
        raise SystemExit("Stop the service, then re-run with --yes to go ahead.")

    # Copy out of the snapshot rather than moving it: the snapshot stays valid,
    # and a second restore of the same one has to work.
    incoming = unique_path("%s.incoming-%s" % (data_dir.rstrip("/"), snapshot_name()))
    os.makedirs(incoming, mode=0o700)
    shutil.copy2(os.path.join(snap, DB_NAME), os.path.join(incoming, DB_NAME))
    os.chmod(os.path.join(incoming, DB_NAME), 0o644)
    for tree in TREES:
        src = os.path.join(snap, tree)
        if os.path.isdir(src):
            link_tree(src, os.path.join(incoming, tree))

    if os.path.isdir(data_dir):
        os.rename(data_dir, aside)
    os.rename(incoming, data_dir)
    log("restored %s -> %s" % (name, data_dir))
    log("previous data is at %s - delete it once you are happy" % aside)
    log("start the service again now")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    ap.add_argument("--backup-dir", default=DEFAULT_BACKUPS)
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help="snapshots to retain (default %d = 24h at 15 min)"
                         % DEFAULT_KEEP)
    ap.add_argument("--loop", action="store_true",
                    help="keep running, snapshotting every --every seconds")
    ap.add_argument("--every", type=int, default=DEFAULT_EVERY,
                    help="seconds between snapshots in --loop (default %d)"
                         % DEFAULT_EVERY)
    ap.add_argument("--list", action="store_true", help="show what has been kept")
    ap.add_argument("--restore", metavar="NAME",
                    help="put a snapshot back; NAME or LATEST")
    ap.add_argument("--yes", action="store_true",
                    help="required by --restore, after you have stopped the service")
    args = ap.parse_args()

    if args.list:
        snaps = list_snapshots(args.backup_dir)
        if not snaps:
            print("no snapshots in %s" % args.backup_dir)
            return 0
        print("%-20s %8s %10s  %s" % ("snapshot", "files", "size", "contents"))
        for n in snaps:
            path = os.path.join(args.backup_dir, n)
            files, total = dir_stats(path)
            try:
                conn = sqlite3.connect(os.path.join(path, DB_NAME))
                teams = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
                subs = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
                conn.close()
                what = "%d companies, %d submissions" % (teams, subs)
            except sqlite3.Error as exc:
                what = "UNREADABLE: %s" % exc
            print("%-20s %8d %10s  %s" % (n, files, human(total), what))
        print("\n%d snapshot(s) in %s" % (len(snaps), args.backup_dir))
        return 0

    if args.restore:
        restore(args.data_dir, args.backup_dir, args.restore, args.yes)
        return 0

    if not args.loop:
        try:
            path, files, total, pruned = take_snapshot(
                args.data_dir, args.backup_dir, args.keep)
        except SnapshotError as exc:
            raise SystemExit(str(exc))
        log("snapshot %s - %d files, %s%s"
            % (os.path.basename(path), files, human(total),
               ", pruned %d old" % pruned if pruned else ""))
        return 0

    log("snapshotting %s into %s every %ds, keeping %d"
        % (args.data_dir, args.backup_dir, args.every, args.keep))
    while True:
        try:
            path, files, total, pruned = take_snapshot(
                args.data_dir, args.backup_dir, args.keep)
            log("snapshot %s - %d files, %s%s"
                % (os.path.basename(path), files, human(total),
                   ", pruned %d old" % pruned if pruned else ""))
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            # A backup loop that dies on one bad run is worse than useless: it
            # stops silently and nobody notices until the restore is needed.
            log("SNAPSHOT FAILED: %s" % exc)
        try:
            time.sleep(args.every)
        except KeyboardInterrupt:
            log("stopped")
            return 0


if __name__ == "__main__":
    sys.exit(main())

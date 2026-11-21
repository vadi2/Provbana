"""Keep the validator's per-category engines compiled.

    .venv/bin/python warm.py --loop     # alongside the service
    .venv/bin/python warm.py --once     # warm now, report, exit

Matchbox compiles a validation engine - the profile snapshots for one guide's
whole dependency closure - the first time it is asked to validate against it,
and caches it. That first call costs between 26 and 135 seconds on the event
box, per category, against a MATCHBOX_TIMEOUT of 60. deploy/deploy.sh pays that
cost once at deploy time so that no participant does.

It does not stay paid. The cache does not hold the engines hard: they are
dropped again while the container runs, with no restart and no log line saying
so - measured locally at roughly 75 minutes of disuse, on a JVM whose heap was
nearly full. HotSpot clears soft references in proportion to free heap
(-XX:SoftRefLRUPolicyMSPerMB, 1000ms per free MB by default), which fits both
that measurement and the fact that a roomier heap holds them far longer. That
mechanism is inferred from the behaviour rather than read out of Matchbox, so
this loop does not depend on it being right: re-touching every category well
inside any plausible lifetime keeps them compiled either way.

What it buys is the difference between a participant's first imaging report
taking 0.3s and taking 135s, which at a 60s timeout is a 502 and no stored
submission at all.

Cheap enough to run often: a warm pass is five sub-second validations. The
bundle it sends is the event's own sample, which is not a document of most of
these kinds, so most of them come back invalid. That is fine, and the verdict
is discarded - compiling the snapshots is the point, and Matchbox does that
before it can decide anything.
"""

import argparse
import os
import sys
import time

import app


def warm_body() -> bytes:
    """The event's first worked example, or an empty document for an event
    without any: the engines compile before Matchbox looks at the content."""
    return ((app.read_sample_bytes(app.SAMPLES[0]["id"]) if app.SAMPLES else None)
            or b'{"resourceType": "Bundle", "type": "document"}')


# Long enough that a warm pass is a rounding error against the validator's day,
# short enough to sit well inside the shortest disuse window measured (~75min).
INTERVAL = int(os.environ.get("WARM_INTERVAL", "1200"))
# A cold engine can take over two minutes to compile, and the first pass after
# a matchbox restart can wait on the package load on top of that - measured at
# 288s once. The service's own budget is deliberately short so that a
# participant is never left hanging; this is not that budget, nobody is waiting
# on it, and giving up early on the one pass that matters most is the only way
# this number can do harm.
TIMEOUT = float(os.environ.get("WARM_TIMEOUT", "600"))
# Matchbox refuses work until its packages are loaded, which takes a minute or
# two from cold. Retry rather than spend a whole interval waiting.
RETRY = 30

DOCUMENTS = [app.PRIMARY_DOCUMENT] + list(app.SECONDARY_DOCUMENTS)


def log(msg: str) -> None:
    print("%s warm: %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg),
          flush=True)


def warm_once(body: bytes) -> int:
    """Touch every category. Returns how many answered, out of len(DOCUMENTS)."""
    app.MATCHBOX_TIMEOUT = TIMEOUT
    warmed = 0
    for doc in DOCUMENTS:
        started = time.monotonic()
        try:
            app.validate_with_matchbox(body, doc)
        except Exception as exc:
            # Never fatal: one guide's engine failing to compile costs a slow
            # first submission of that kind, and taking the keep-warm down over
            # it would cost every kind.
            log("%s not warmed (%s)" % (doc["id"], exc))
            continue
        warmed += 1
        took = time.monotonic() - started
        # Only the slow ones are worth a line. Under a second is the steady
        # state, and logging that every 20 minutes would bury the times that
        # say something. After the first pass a compile means the engine had
        # been dropped, which is the evidence for whether INTERVAL is short
        # enough - if these keep appearing, shorten it.
        if took >= 5:
            log("%s compiled in %.0fs" % (doc["id"], took))
    return warmed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--loop", action="store_true",
                    help="keep warming, every WARM_INTERVAL seconds")
    ap.add_argument("--once", action="store_true",
                    help="one pass, then exit non-zero if anything is still cold")
    args = ap.parse_args()
    if not (args.loop or args.once):
        ap.error("choose --loop or --once")

    body = warm_body()

    if args.once:
        warmed = warm_once(body)
        log("%d/%d warm" % (warmed, len(DOCUMENTS)))
        return 0 if warmed == len(DOCUMENTS) else 1

    log("every %ds, %d categories" % (INTERVAL, len(DOCUMENTS)))
    while True:
        warmed = warm_once(body)
        if warmed == 0:
            # Nothing answered at all: matchbox is down, or still loading its
            # packages after a restart. Come back sooner than a full interval.
            log("nothing warmed - retrying in %ds" % RETRY)
            time.sleep(RETRY)
            continue
        if warmed < len(DOCUMENTS):
            log("%d/%d warm" % (warmed, len(DOCUMENTS)))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())

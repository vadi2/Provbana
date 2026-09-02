"""Fill a local instance with demo data so every UI state can be previewed.

Local preview only. It drives the public API exactly as a participant would -
register, then submit - so every row it creates is real data
scored by the real validator, not hand-written rows in the database. That also
means it needs Matchbox up, and that it must never be pointed at the live
event instance.

    .venv/bin/python seed_demo.py [--url http://localhost:8095] [--reset]

The team list below is chosen to cover the states the UI can be in: teams that
pass, teams that only ever fail, teams that never submit, and a score tie that
exercises the ranking tiebreak. Bundles come from ./examples and ./samples, so
no patient data is invented here, and the interop screenshots are drawn by this
script rather than being real grabs of anyone's product.
"""

import argparse
import json
import os
import random
import shutil
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(HERE, "examples")
SAMPLES = os.path.join(HERE, "samples")

# Bundles by the shape they produce, so the plan below reads by intent.
BUNDLES = {
    "rich": os.path.join(EXAMPLES, "rich-example.json"),
    "arne": os.path.join(SAMPLES, "arne-complex-care.json"),
    "andrea": os.path.join(SAMPLES, "andrea-cross-border.json"),
    "minor": os.path.join(EXAMPLES, "minor-example.json"),
    "official": os.path.join(EXAMPLES, "official-example.json"),
    "broken": os.path.join(EXAMPLES, "broken-example.json"),
}

# (team name, [bundles to submit in order]) - order matters: the first team to
# submit something valid takes the one-time first-pass bonus, so the richest
# bundle goes first and the badge lands somewhere visible at the top.
PLAN = [
    ("Karolinska Interop Lab", ["rich"]),
    ("Sahlgrenska Digital", ["arne"]),
    ("Rigshospitalet EHR", ["andrea"]),
    # Two attempts: a failure followed by a fix, so the attempts column and
    # "best submission wins" behaviour are both visible on one row.
    ("Tromso Journal AS", ["broken", "official"]),
    ("Oulu Health Tech", ["official"]),
    # Same bundle as Oulu -> identical score, which exercises the rank
    # tiebreak (earliest best submission, then name).
    ("Reykjavik Care Systems", ["official"]),
    # minor-example fails the EPS profile, with a different error count from
    # broken-example - two shapes of failure rather than one repeated.
    ("Tallinn Terviseandmed", ["minor"]),
    # Never gets past the validator: NOT YET status with a real error count.
    ("Aarhus MedTech", ["broken", "broken"]),
    # Registered, never submitted: every richness column falls back to "-".
    ("Helsinki HealthOS", []),
    ("Uppsala Vardsystem", []),
]

# (who tested it, whose document, what they saw). Every producer here is a team
# above whose first bundle passes, because only a published document can be
# rendered. Karolinska's is deliberately shown by two systems, so the gallery
# has one document with several screenshots side by side.
RENDERINGS = [
    ("Sahlgrenska Digital", "Karolinska Interop Lab",
     "Imports clean. All four allergies are there, but criticality is blank on "
     "every one of them - our list sorts by it, so they come out in file order."),
    ("Rigshospitalet EHR", "Karolinska Interop Lab",
     "Medication section renders, dose text does not: we only read "
     "dosageInstruction.doseAndRate, and these carry the dose in the free text."),
    ("Oulu Health Tech", "Sahlgrenska Digital",
     "Full summary in one screen. The problem list dates show as 2026-01 because "
     "we drop the day when onsetDateTime has no time zone."),
    ("Karolinska Interop Lab", "Rigshospitalet EHR",
     "Cross-border case opens as expected. Note the immunisation section is "
     "empty rather than absent - worth deciding which one we all mean."),
    ("Tromso Journal AS", "Oulu Health Tech",
     "Nothing to report - this one came through complete."),
    ("Reykjavik Care Systems", "Tromso Journal AS",
     "Patient banner is blank: we key the header off name.family and this "
     "document only has name.text."),
]


def mock_screenshot(seed):
    """A PNG that reads as a screenshot of somebody's summary view at thumbnail
    size. Drawing one beats committing binaries, and there is no font to depend
    on - bars stand in for the text."""
    w, h = 560, 360
    rnd = random.Random(seed)
    chrome = rnd.choice([(38, 78, 122), (26, 96, 84), (104, 52, 108), (140, 68, 40)])
    canvas = bytearray(b"\xfa" * (w * h * 3))

    def rect(x0, y0, x1, y1, rgb):
        row = bytes(rgb) * (x1 - x0)
        for y in range(y0, y1):
            start = (y * w + x0) * 3
            canvas[start:start + len(row)] = row

    rect(0, 0, w, 34, chrome)                       # the application's own bar
    rect(0, 34, 132, h, (238, 238, 240))            # section navigation
    for i in range(7):
        rect(12, 50 + i * 26, 12 + rnd.randint(60, 108), 60 + i * 26, (198, 200, 206))
    y = 54
    while y < h - 18:                               # the document itself
        rect(150, y, 150 + rnd.randint(120, 380), y + 9, (206, 208, 212))
        y += rnd.choice([18, 18, 22, 34])

    scanlines = b"".join(b"\x00" + bytes(canvas[r * w * 3:(r + 1) * w * 3])
                         for r in range(h))

    def png_chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(scanlines, 9))
            + png_chunk(b"IEND", b""))


def lower(headers):
    """uvicorn sends header names lowercased and HTTP does not consider case
    significant, so key them lowercase rather than guessing at the spelling."""
    return {k.lower(): v for k, v in headers.items()}


def call(url, method="GET", body=None, token=None, ctype="application/json"):
    """Returns (status, parsed-or-raw body, headers). Errors come back as
    values rather than exceptions so the caller can report and carry on."""
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", ctype)
    if token:
        req.add_header("X-Team-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            raw, status, headers = res.read(), res.status, lower(res.headers)
    except urllib.error.HTTPError as exc:
        raw, status, headers = exc.read(), exc.code, lower(exc.headers)
    except urllib.error.URLError as exc:
        return 0, {"detail": str(exc.reason)}, {}
    try:
        return status, json.loads(raw), headers
    except ValueError:
        return status, raw, headers


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8095")
    ap.add_argument(
        "--reset",
        action="store_true",
        help="delete DATA_DIR first (the service must be stopped, and is left "
        "for you to restart)",
    )
    # Anchored to this file, not the shell's cwd: --reset deletes it.
    ap.add_argument("--data-dir",
                    default=os.environ.get("DATA_DIR") or os.path.join(HERE, "data"))
    args = ap.parse_args()
    base = args.url.rstrip("/")

    if args.reset:
        if os.path.isdir(args.data_dir):
            shutil.rmtree(args.data_dir)
            print("removed %s - restart the service, then re-run without "
                  "--reset" % args.data_dir)
        else:
            print("%s does not exist, nothing to reset" % args.data_dir)
        return 0

    status, health, _ = call(base + "/api/health")
    if status != 200:
        print("cannot reach %s - is the service running?" % base, file=sys.stderr)
        return 1
    if health.get("matchbox") == "degraded":
        print("matchbox is up but has not loaded %s - the demo scores would be "
              "wrong; recreate it with `cd ../itb && docker compose -p itb up -d "
              "--force-recreate matchbox` and wait for it to load its IGs"
              % ", ".join(health.get("missing_igs") or ["a required package"]),
              file=sys.stderr)
        return 1
    if health.get("matchbox") != "up":
        print("matchbox is %s - submissions would fail; start it with "
              "`cd ../itb && docker compose -p itb up -d matchbox` and wait for "
              "it to load its IGs" % health.get("matchbox"), file=sys.stderr)
        return 1

    bundles = {}
    for key, path in BUNDLES.items():
        with open(path, "rb") as fh:
            bundles[key] = fh.read()

    tokens = {}
    for name, submissions in PLAN:
        status, body, _ = call(
            base + "/api/register",
            "POST",
            json.dumps({"team": name}).encode(),
        )
        if status != 200:
            detail = body.get("detail", status) if isinstance(body, dict) else status
            print("  register %-26s SKIP (%s)" % (name, detail))
            continue
        tokens[name] = body["token"]
        print("register %-26s ok" % name)

        for key in submissions:
            status, _, headers = call(
                base + "/api/eps",
                "POST",
                bundles[key],
                tokens[name],
                "application/fhir+json",
            )
            if status != 200:
                print("  submit %-10s FAILED (%s)" % (key, status))
                continue
            print("  submit %-10s pass=%-5s score=%-4s sections=%-3s resources=%s"
                  % (key, headers.get("x-pass"), headers.get("x-score"),
                     headers.get("x-sections"), headers.get("x-resources")))

    # Screenshots come last: a document has to be published before anyone can
    # say what it looked like, and the slug is the service's to decide.
    status, published, _ = call(base + "/api/exchange")
    slugs = {p["team"]: p["team_slug"] for p in published} if status == 200 else {}
    for consumer, producer, note in RENDERINGS:
        token, slug = tokens.get(consumer), slugs.get(producer)
        if not token or not slug:
            print("screenshot %-26s SKIP (nothing published)" % producer)
            continue
        status, body, _ = call(
            "%s/api/exchange/%s/renderings?note=%s"
            % (base, slug, urllib.parse.quote(note)),
            "POST", mock_screenshot(consumer + producer), token, "image/png")
        detail = body.get("detail", status) if isinstance(body, dict) else status
        print("screenshot %-26s %s"
              % (producer, "by " + consumer if status == 201 else "FAILED (%s)" % detail))

    status, rows, _ = call(base + "/api/leaderboard")
    if status == 200:
        print("\n%-4s %-26s %-6s %s" % ("#", "team", "score", "status"))
        for row in rows:
            print("%-4s %-26s %-6s %s"
                  % (row["rank"], row["team"], row["score"],
                     "VALID" if row["passed"] else "not yet"))
    print("\nopen %s/ to preview" % base)
    return 0


if __name__ == "__main__":
    sys.exit(main())

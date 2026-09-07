"""Fill a local instance with demo data so every UI state can be previewed.

Local preview only. It drives the public API exactly as a participant would -
register, then submit - so every row it creates is real data
scored by the real validator, not hand-written rows in the database. That also
means it needs Matchbox up, and that it must never be pointed at the live
event instance.

    .venv/bin/python seed_demo.py [--url http://localhost:8095] [--reset]

The team list below is chosen to cover the states the UI can be in: teams that
pass, teams that only ever fail, teams that never submit, and a score tie that
exercises the ranking tiebreak. The names are deliberately fictitious ones -
no real hospital, region or vendor appears on the demo leaderboard. Bundles come from ./examples and ./samples, so
no patient data is invented here, and the interop screenshots are drawn by this
script rather than being real grabs of anyone's product.
"""

import argparse
import base64
import json
import os
import random
import shutil
import struct
import sys
import urllib.error
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
    # The other EHDS categories, so the leaderboard's "Also" column and the
    # chips on it are populated in a preview rather than being dead UI that
    # only ever appears at the event. These two are the guides' own published
    # examples and pass as shipped; the imaging guide's examples fail its own
    # profile (see matchbox/config/application.yaml), so seeding that would put
    # a red herring on the demo board.
    "lab": os.path.join(EXAMPLES, "lab-example.json"),
    "mpd": os.path.join(EXAMPLES, "mpd-example.json"),
}

# (team name, [bundles to submit in order]) - order matters: the first team to
# submit something valid takes the one-time first-pass bonus, so the richest
# bundle goes first and the badge lands somewhere visible at the top.
PLAN = [
    ("Contoso Interop Lab", ["rich"]),
    ("Fabrikam Digital", ["arne"]),
    ("Litware EHR", ["andrea"]),
    # Two attempts: a failure followed by a fix, so the attempts column and
    # "best submission wins" behaviour are both visible on one row. Then two
    # other categories, which is what fills the "Also" column and its chips.
    # They sit on a mid-table row on purpose: +30 has to be visible without
    # deciding the lead, or the demo board argues the opposite of the scoring.
    ("Northwind Journal AS", ["broken", "official", "lab", "mpd"]),
    ("Proseware Health Tech", ["official"]),
    # Same bundle as Proseware -> identical score, which exercises the rank
    # tiebreak (earliest best submission, then name).
    ("Relecloud Care Systems", ["official"]),
    # minor-example fails the EPS profile, with a different error count from
    # broken-example - two shapes of failure rather than one repeated.
    ("Trey Research Halsodata", ["minor"]),
    # Never gets past the validator: NOT YET status with a real error count.
    ("Woodgrove MedTech", ["broken", "broken"]),
    # Nothing but other categories: scores the bonuses and no more, sits near
    # the bottom, and its richness columns stay "-" because it has no summary
    # to describe. The state the scoring is meant to make unattractive, so it
    # should be visible on the demo board rather than only argued about.
    ("Adatum Labdata", ["lab"]),
    # Registered, never submitted: every richness column falls back to "-".
    ("Lamna HealthOS", []),
    ("VanArsdel Vardsystem", []),
]

# (who tested it, whose document, what they saw). Every producer here is a team
# above whose first bundle passes, because only a published document can be
# rendered. Contoso's is deliberately shown by two systems, so the gallery
# has one document with several screenshots side by side.
RENDERINGS = [
    ("Fabrikam Digital", "Contoso Interop Lab",
     "Imports clean. All four allergies are there, but criticality is blank on "
     "every one of them - our list sorts by it, so they come out in file order."),
    ("Litware EHR", "Contoso Interop Lab",
     "Medication section renders, dose text does not: we only read "
     "dosageInstruction.doseAndRate, and these carry the dose in the free text."),
    ("Proseware Health Tech", "Fabrikam Digital",
     "Full summary in one screen. The problem list dates show as 2026-01 because "
     "we drop the day when onsetDateTime has no time zone."),
    ("Contoso Interop Lab", "Litware EHR",
     "Cross-border case opens as expected. Note the immunisation section is "
     "empty rather than absent - worth deciding which one we all mean."),
    ("Northwind Journal AS", "Proseware Health Tech",
     "Nothing to report - this one came through complete."),
    ("Relecloud Care Systems", "Northwind Journal AS",
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


def call(url, method="GET", body=None, token=None, ctype="application/fhir+json"):
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
        return 0, outcome_body(str(exc.reason)), {}
    try:
        return status, json.loads(raw), headers
    except ValueError:
        return status, raw, headers


def outcome_body(message):
    """An OperationOutcome shaped like the server's, for errors that never
    reached it."""
    return {"resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "exception",
                       "diagnostics": message}]}


def why(body, status):
    """The message out of an OperationOutcome, for a one-line report."""
    if isinstance(body, dict) and body.get("resourceType") == "OperationOutcome":
        issues = body.get("issue") or [{}]
        return issues[0].get("diagnostics", status)
    return status


def part_value(part):
    if "part" in part:
        return {p["name"]: part_value(p) for p in part["part"]}
    if "resource" in part:
        return part["resource"]
    for key, value in part.items():
        if key.startswith("value"):
            return value
    return None


def params(body, name="entry"):
    """Every value of a repeated parameter in a Parameters resource."""
    if not isinstance(body, dict):
        return []
    return [part_value(p) for p in body.get("parameter", []) if p["name"] == name]


def param(body, name):
    found = params(body, name)
    return found[0] if found else None


def parameters(**values):
    """A Parameters resource body, which is what the operations take."""
    return json.dumps({
        "resourceType": "Parameters",
        "parameter": [{"name": k, "valueString": v} for k, v in values.items()],
    }).encode()


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

    status, health, _ = call(base + "/fhir/$status")
    if status != 200:
        print("cannot reach %s - is the service running?" % base, file=sys.stderr)
        return 1
    matchbox = param(health, "matchbox")
    if matchbox == "degraded":
        print("matchbox is up but has not loaded %s - the demo scores would be "
              "wrong; recreate it with `cd ../itb && docker compose -p itb up -d "
              "--force-recreate matchbox` and wait for it to load its IGs"
              % ", ".join(params(health, "missingIg") or ["a required package"]),
              file=sys.stderr)
        return 1
    if matchbox != "up":
        print("matchbox is %s - submissions would fail; start it with "
              "`cd ../itb && docker compose -p itb up -d matchbox` and wait for "
              "it to load its IGs" % matchbox, file=sys.stderr)
        return 1

    bundles = {}
    for key, path in BUNDLES.items():
        with open(path, "rb") as fh:
            bundles[key] = fh.read()

    tokens = {}
    for name, submissions in PLAN:
        status, body, _ = call(
            base + "/fhir/$register", "POST", parameters(name=name),
        )
        if status != 200:
            print("  register %-26s SKIP (%s)" % (name, why(body, status)))
            continue
        tokens[name] = param(body, "token")
        print("register %-26s ok" % name)

        for key in submissions:
            status, _, headers = call(
                base + "/fhir/Bundle/$validate",
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
    status, published, _ = call(base + "/fhir/$exchange")
    slugs = ({p["organization"]: p["organizationSlug"] for p in params(published)}
             if status == 200 else {})
    for consumer, producer, note in RENDERINGS:
        token, slug = tokens.get(consumer), slugs.get(producer)
        if not token or not slug:
            print("screenshot %-26s SKIP (nothing published)" % producer)
            continue
        image = mock_screenshot(consumer + producer)
        docref = json.dumps({
            "resourceType": "DocumentReference",
            "status": "current",
            "custodian": {"reference": "Organization/%s" % slug},
            "description": note,
            "content": [{"attachment": {
                "contentType": "image/png",
                "data": base64.b64encode(image).decode("ascii"),
            }}],
        }).encode()
        status, body, _ = call(base + "/fhir/DocumentReference", "POST",
                               docref, token)
        print("screenshot %-26s %s"
              % (producer, "by " + consumer if status == 201
                 else "FAILED (%s)" % why(body, status)))

    status, board, _ = call(base + "/fhir/$leaderboard")
    if status == 200:
        print("\n%-4s %-26s %-6s %s" % ("#", "company", "score", "status"))
        for row in params(board):
            print("%-4s %-26s %-6s %s"
                  % (row["rank"], row["organization"], row["score"],
                     "VALID" if row["passed"] else "not yet"))
    print("\nopen %s/ to preview" % base)
    return 0


if __name__ == "__main__":
    sys.exit(main())

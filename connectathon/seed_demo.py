"""Fill a local instance with demo data so every UI state can be previewed.

Local preview only. It drives the public API exactly as a participant would -
register, submit, confirm receipts - so every row it creates is real data
scored by the real validator, not hand-written rows in the database. That also
means it needs Matchbox up, and that it must never be pointed at the live
event instance.

    .venv/bin/python seed_demo.py [--url http://localhost:8095] [--reset]

The team list below is chosen to cover the states the UI can be in: teams that
pass, teams that only ever fail, teams that never submit, a team that scores on
interop alone, a score tie that exercises the ranking tiebreak, and a consumer
with enough partners to hit the interop cap. Bundles come from ../examples and
./samples, so no patient data is invented here.
"""

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = os.path.join(HERE, os.pardir, "examples")
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
    # Also never submits, but consumes partners below - the leaderboard branch
    # where a team scores on interop alone.
    ("Uppsala Vardsystem", []),
]

# consumer -> producers. Karolinska consumes four partners (4 x 25 = 100), which
# lands on the interop cap; the rest are spread so the matrix has gaps as well
# as ticks, and so some teams score as producers only.
INTEROP = {
    "Karolinska Interop Lab": [
        "Sahlgrenska Digital",
        "Rigshospitalet EHR",
        "Oulu Health Tech",
        "Reykjavik Care Systems",
    ],
    "Sahlgrenska Digital": ["Karolinska Interop Lab", "Rigshospitalet EHR"],
    "Rigshospitalet EHR": ["Karolinska Interop Lab"],
    "Tromso Journal AS": ["Sahlgrenska Digital"],
    "Uppsala Vardsystem": ["Karolinska Interop Lab", "Sahlgrenska Digital"],
}

MEDICATION_TYPES = {"MedicationStatement", "MedicationRequest", "MedicationAdministration"}


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


def receipt_facts(bundle):
    """The four facts /api/interop/confirm checks, read the way the server
    reads them: first Patient wins, medications and allergies are counts."""
    facts = {"patient_family": None, "birth_date": None, "medications": 0, "allergies": 0}
    seen_patient = False
    for entry in bundle.get("entry", []):
        resource = entry.get("resource") or {}
        rtype = resource.get("resourceType")
        if rtype == "Patient" and not seen_patient:
            seen_patient = True
            for name in resource.get("name") or []:
                if name.get("family"):
                    facts["patient_family"] = name["family"]
                    break
            facts["birth_date"] = resource.get("birthDate")
        elif rtype in MEDICATION_TYPES:
            facts["medications"] += 1
        elif rtype == "AllergyIntolerance":
            facts["allergies"] += 1
    return facts


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8095")
    ap.add_argument(
        "--reset",
        action="store_true",
        help="delete DATA_DIR first (the service must be stopped, and is left "
        "for you to restart)",
    )
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "./data"))
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
    if health.get("matchbox") != "up":
        print("matchbox is %s - submissions would fail; start it with "
              "`cd ../itb && docker compose -p itb up -d matchbox` and wait for "
              "it to load its IGs" % health.get("matchbox"), file=sys.stderr)
        return 1

    bundles = {}
    for key, path in BUNDLES.items():
        with open(path, "rb") as fh:
            bundles[key] = fh.read()

    tokens, slugs = {}, {}
    for name, submissions in PLAN:
        status, body, _ = call(
            base + "/api/register",
            "POST",
            json.dumps({"team": name}).encode(),
        )
        if status != 200:
            print("  register %-26s SKIP (%s)" % (name, body.get("detail", status)))
            continue
        tokens[name] = body["token"]
        # The server derives the slug; mirroring its rule here would be a
        # second source of truth, so read it back from the exchange list later.
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

    # Slugs come from the server so this never drifts from its slugify rule.
    status, published, _ = call(base + "/api/exchange")
    if status == 200:
        slugs = {row["team"]: row["team_slug"] for row in published}

    for consumer, producers in INTEROP.items():
        token = tokens.get(consumer)
        if not token:
            continue
        for producer in producers:
            slug = slugs.get(producer)
            if not slug:
                print("  interop %-26s -> %-26s SKIP (nothing published)"
                      % (consumer, producer))
                continue
            status, doc, _ = call(base + "/api/exchange/" + slug, token=token)
            if status != 200:
                print("  interop fetch %s FAILED (%s)" % (slug, status))
                continue
            payload = dict(receipt_facts(doc), partner=slug)
            status, body, _ = call(
                base + "/api/interop/confirm",
                "POST",
                json.dumps(payload).encode(),
                token,
            )
            print("  interop %-26s -> %-26s %s"
                  % (consumer, producer,
                     "ok" if status == 200 else "FAILED (%s %s)" % (status, body)))

    status, rows, _ = call(base + "/api/leaderboard")
    if status == 200:
        print("\n%-4s %-26s %-6s %-8s %s" % ("#", "team", "score", "interop", "status"))
        for row in rows:
            print("%-4s %-26s %-6s %-8s %s"
                  % (row["rank"], row["team"], row["score"], row["interop"],
                     "VALID" if row["passed"] else "not yet"))
    print("\nopen %s/ to preview" % base)
    return 0


if __name__ == "__main__":
    sys.exit(main())

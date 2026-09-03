# Nordic Hackathon 2026

Service for the [Nordic Hackathon 2026](https://nordichackathon2026.blogs.dsv.su.se), 18-20 November in Kista.

Gamified FHIR hackathon service: teams register, submit European Patient Summary bundles,
a local [Matchbox](https://github.com/ahdis/matchbox) validates them against HL7 EU EPS, and a live leaderboard keeps score.

## Run

Matchbox is required either way. It comes from the compose file in `../itb`, which also
creates the `itb_default` docker network the container run below joins:

```
cd ../itb && docker compose -p itb up -d matchbox   # -> http://localhost:8090/matchboxv3/fhir
```

Matchbox reads `matchbox/config/application.yaml` once, at boot. After editing it
(adding an IG, say) recreate the container - `docker compose -p itb up -d
--force-recreate matchbox` - or the running validator keeps its old package list and
`/api/health` reports it as `degraded`.

**Docker** (needs `itb_default`, so run the compose command above first - without it
`docker run` fails with `network itb_default not found`):

```
docker build -t connectathon .
docker run --network itb_default -p 8095:8095 -v conn-data:/data connectathon
```

**Bare metal** (Python 3.11+ - the pins assume it; the image uses 3.12):

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATA_DIR=./data MATCHBOX_URL=http://localhost:8090/matchboxv3/fhir .venv/bin/python app.py
```

`python app.py` binds `0.0.0.0`, so participants reach it at `http://<organiser-ip>:8095/`.
Do not use `uvicorn app:app` - it defaults to localhost only and ignores `PORT`.

Environment: `PORT` (8095), `DATA_DIR` (`/data`), `MATCHBOX_URL`
(`http://matchbox:8080/matchboxv3/fhir`), `PROFILE`
(`http://hl7.eu/fhir/eps/StructureDefinition/bundle-eu-eps`), `SAMPLES_DIR`
(`samples/` next to `app.py`), `MAX_BODY_BYTES` (5 MB), `MAX_IMAGE_BYTES` (2 MB - the
backstop for an interop screenshot; the UI downscales before it uploads, so this is
not the expected size), `MATCHBOX_TIMEOUT` (60 s - how long a submitting client waits
on a wedged validator), `REGISTER_CODE` (unset = registration is open; set it to
require `{"code": "..."}` on `/api/register`).
`DATA_DIR` must be writable by the user running the service; it holds
`connectathon.db`, the raw submission audit log and `renderings/` (the uploaded
screenshots, one directory per uploading company).

`VALIDATION_IGS` (default `hl7se.fhir.base#1.1.0`, comma-separated) names extra
packages to load into the validation engine. Matchbox scopes an engine to the IG
that defines `PROFILE`, so a `meta.profile` claim from any other package is
reported as "has not been checked" rather than validated. The named packages
must also be listed in `matchbox/config/application.yaml`, or Matchbox has
nothing to load.

Tests: `.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q test_app.py`

Demo data for a local preview: `.venv/bin/python seed_demo.py` (with the service and
Matchbox running) registers a handful of fake companies, submits the bundles in
`examples/` and `samples/`, and publishes a few interop screenshots against them, so
every UI state has something to show. The screenshots are drawn by the script - no real
product is pictured. `--reset` deletes
the data directory first - stop the service before, restart it after. Never point it at
the event instance.

Terminology lookup is off: Matchbox runs with its packaged `txServer: n/a`, so
structure, profiles and cardinality are checked in full but a SNOMED CT, LOINC
or ATC code is reported as "cannot be validated" instead of being confirmed.
Turning it on is one line in `matchbox/config/application.yaml`, and that file
explains both reasons it is off - an unreachable terminology server stops
Matchbox booting at all, and one that dies mid-event fails the next bundle
carrying an uncached code with a hard error, scoring a good bundle 0.

## Backups

`backup.py` snapshots everything a result is made of - the SQLite database, the
exact bytes of every submitted bundle, every uploaded interop screenshot - and
puts them back if something goes wrong. Scores are not recomputed from anything:
the database *is* the leaderboard, so losing it loses the event.

### Taking backups

Start it beside the service and leave it running for the whole event:

```
.venv/bin/python backup.py --loop
```

```
18:32:22  snapshotting ./data into /home/.../connectathon/backups every 900s, keeping 96
18:32:22  snapshot 20260902T183222Z - 21 files, 641.0 KB
```

That is a snapshot every 15 minutes, keeping the last 96 - a day's worth, so the
pruner never reaches back past the start of the day it is protecting. A bad run
is logged as `SNAPSHOT FAILED` and the loop carries on; a backup loop that dies
on one error stops silently and nobody notices until the restore is needed.

Snapshots land in `backups/` next to `app.py`. That is deliberately *beside* the
data directory rather than inside it, because `seed_demo.py --reset` and a
restore both move the data directory wholesale.

One snapshot right now, without the loop:

```
.venv/bin/python backup.py
```

Take one by hand before anything you would not want to do twice - reseeding,
editing the database, moving the service to another machine.

Two details worth knowing. The database is copied through SQLite's own online
backup API, not `cp`: copying a file that is being written to yields a database
that opens fine and is quietly missing the last transaction, and at an event
something is always being written. And `submissions/` and `renderings/` are
hard-linked rather than copied, since a submitted bundle and an uploaded
screenshot are written once and never edited - so a snapshot is the same bytes
for none of the disk, and a day of them costs kilobytes rather than gigabytes.
Deleting a screenshot through the API unlinks the live copy and leaves the
snapshot's link intact, which is the point.

### Seeing what you have

```
.venv/bin/python backup.py --list
```

```
snapshot                files       size  contents
20260902T181956Z           21   641.0 KB  11 companies, 14 submissions
20260902T183222Z           21   641.0 KB  11 companies, 14 submissions
20260902T184722Z           21   641.0 KB  11 companies, 14 submissions

3 snapshot(s) in /home/.../connectathon/backups
```

Names are UTC timestamps, and the company and submission counts come from
opening each snapshot's database - so a snapshot that cannot be read says
`UNREADABLE` here rather than at the moment you need it. Check this once during
the event, not for the first time during a disaster.

### Restoring

Three steps. Look first, stop the service, then restore.

**1. Pick a snapshot.** `--list` as above, or `LATEST` for the newest.

**2. Look before you leap.** Without `--yes` nothing is touched - it prints what
the snapshot holds and what would move where, then exits 1:

```
.venv/bin/python backup.py --restore LATEST
```

```
Restore /home/.../connectathon/backups/20260902T184722Z
  contains   11 companies, 14 submissions, 21 files (641.0 KB)
  into       /home/.../connectathon/data
  current    /home/.../connectathon/data -> moved aside to /home/.../connectathon/data.replaced-20260902T185727Z (nothing is deleted)

Stop the service, then re-run with --yes to go ahead.
```

Read the company and submission counts. If they are lower than you expect, you
picked a snapshot from before the submissions you are trying to save.

**3. Stop the service, then restore.** Stopping it first is not optional: the
rename below is atomic, but a running service keeps writing into the directory
it already has open, and those writes would land in the set-aside copy instead
of the restored one.

```
# Ctrl-C the service (and the --loop backup, if it is in the same terminal)
.venv/bin/python backup.py --restore LATEST --yes
```

```
18:57:27  restored 20260902T184722Z -> /home/.../connectathon/data
18:57:27  previous data is at /home/.../connectathon/data.replaced-20260902T185727Z - delete it once you are happy
18:57:27  start the service again now
```

Start the service again and check the leaderboard. Delete the
`data.replaced-*` directory once you are happy - it is not pruned for you.

**Undoing a restore.** Nothing was deleted, so swap the two directories back:

```
mv data data.wrong && mv data.replaced-20260902T185727Z data
```

The snapshot is untouched by all of this - restore copies out of it rather than
moving it - so the same snapshot can be restored again if the first attempt went
somewhere unhelpful.

### Only you can restore

There is no HTTP endpoint for restore, and no token that grants it. It is this
script, run from a shell on this machine, by the account that owns `backups/`:
the directory is created 0700 and its databases 0600, so another account on the
same laptop can neither read the submissions nor swap a snapshot for a doctored
one, and the script refuses outright if the backup directory belongs to a
different uid. Nothing a participant on the venue network can send will trigger
any of it.

The one thing to get right yourself: `backups/` is on the same disk as `data/`,
which protects you from a bad reset, a bad edit or a corrupted database, but not
from losing the laptop. Copy the newest snapshot to a USB stick or another
machine at the end of each day - it is a plain directory, so `cp -a` or `rsync`
is the whole procedure.

### Options

| Flag | Default | What it does |
|---|---|---|
| `--loop` | off | Keep running, snapshotting every `--every` seconds |
| `--every N` | `900` | Seconds between snapshots in `--loop` |
| `--keep N` | `96` | Snapshots to retain (96 = 24h at 15 min) |
| `--list` | | Show what has been kept, with counts |
| `--restore NAME` | | Put a snapshot back; `NAME` or `LATEST` |
| `--yes` | off | Required by `--restore`; without it, nothing is touched |
| `--data-dir` | `$DATA_DIR`, else `data/` beside `app.py` | Which data directory to snapshot or restore into |
| `--backup-dir` | `$BACKUP_DIR`, else `backups/` beside `app.py` | Where snapshots live |

## Scoring

Any error or fatal validation issue scores the submission **0**. A valid bundle earns
**50**, plus **+10** per *distinct* populated Composition section (capped at **+160**)
and **+2** per clinical resource, meaning everything that is not the Composition or the
Patient (capped at **+60**).

A section is populated when at least one of its `entry` references resolves to a
clinical resource in the bundle - an `emptyReason`, a display-only entry or a
reference to nothing does not count. Sections are de-duplicated by their `code`,
falling back to their `title`; sections with neither collapse into one. Repeating a
section or padding the bundle with sections therefore earns nothing; resources are
worth +2 each up to the cap, and the cap is what keeps padding pointless there.

A team's score is its best submission. The first company ever to go valid keeps a one-time
**+20 first pass** bonus. Nothing else scores - interop testing is deliberately unscored,
so there is nothing to farm. Set `REGISTER_CODE` if you want registration closed as well.

## Swedish base profiles

The bundles in `samples/` (built by `samples/generate.py`, which is the place to edit
them) claim the HL7 Sweden base profiles
([hl7se.fhir.base](https://hl7.se/fhir/ig/base/)) alongside the European ones:
`SEBasePatient`, `SEBasePractitioner`, `SEBaseOrganization` and
`SEBasePractitionerRole`. In practice that means Swedish identifiers go in the
namespaces those profiles slice on - `http://electronichealth.se/identifier/personnummer`
rather than the `urn:oid:1.2.752.129.2.1.3.1` OID, HSA-id under
`urn:oid:1.2.752.29.4.19`, organisation number under `urn:oid:2.5.4.97`. An OID
personnummer still validates; it just matches no slice, so nothing Swedish is
checked.

Teams are not required to claim these profiles, but the validator is configured
to resolve them (see `VALIDATION_IGS`), so a team that does gets real feedback
instead of a "not checked" warning.

## API

Team endpoints authenticate with `X-Team-Token: TOKEN` (or `Authorization: Bearer TOKEN`).
A missing or unknown token is **401**. Registration answers **400** if the name is not
2-40 characters, **403** if `REGISTER_CODE` is set and the code is wrong, **409** if the
name is already taken (whitespace-normalised; case-insensitive for ASCII letters only).

### Registration

```
# one registration per company - keep the token, it is shown once
curl -X POST localhost:8095/api/register -H 'Content-Type: application/json' -d '{"team":"My Team"}'
```

The response carries the token and the company name as stored. Elsewhere the company is
addressed by its slug - the lower-cased, ASCII-folded name with runs of anything else
turned into `-` (`Åland Hälsa` is `aland-halsa`), suffixed `-2`, `-3` if that slug is
taken. The leaderboard rows carry it as `team_slug`.

### Submission

```
# validate + score a bundle; score is in the X-Score response header
curl -D - -X POST localhost:8095/api/eps -H 'X-Team-Token: TOKEN' \
     -H 'Content-Type: application/fhir+json' --data-binary @bundle.json
```

Body is the Matchbox `OperationOutcome`. Headers: `X-Pass`, `X-Score`, `X-Errors`,
`X-Sections`, `X-Resources`. **400** if the body is not a JSON object, **413** if the
bundle exceeds `MAX_BODY_BYTES`, **502** if Matchbox is unreachable or fails on its
side (a 5xx from the validator is never scored against the bundle). Only attempts that
got a verdict are recorded and kept in the audit log.

The web form does more with that same outcome than the API can. A location like
`Bundle.entry[9].resource.dosage[0].doseAndRate[0].dose[x].value` is a locator, and
turning it into a place in the file is otherwise counting entries by hand, so the page
prints the bundle you submitted and puts every error and warning on the line it names,
with the issue table's Location column reading `line 698 / MedicationStatement #9 ·
dosage[0].doseAndRate[0].doseQuantity.value` and jumping there. Where the location only
reaches the enclosing resource, the message is read for the element it names
(`Composition.section: minimum required = 5`) and the marker moves onto it. Findings
about something that is not in the document at all land on the nearest thing that is.
All of it happens in the browser, against the JSON it has just sent - nothing is
uploaded twice and the server keeps no extra state.

Above the findings, **Explain all N errors** hands every error at once to ChatGPT,
Claude, Gemini or Le Chat, so a team that cannot parse `Slice
'Bundle.entry:composition': a matching slice is required` can ask something that can.
Sent together rather than one by one, the assistant sees the failing bundle's whole
story and is asked which fix comes first - nine slicing errors are usually one missing
section. ChatGPT, Claude and Le Chat open with the question already in the composer;
Gemini's web app takes no prompt in its URL, so it opens on a blank chat with the
prompt on the clipboard, and the menu says so on the row. *Copy the prompt* covers any
other assistant, and on a plain-http venue box with no clipboard API the prompt is
shown selected instead. These are ordinary links the participant's own browser follows
under their own account: nothing is sent by this service, and the prompt carries the
findings, their locations and the profiles that raised them - never the bundle. A
bundle that passed with warnings gets the same offer for those.

```
# submission history (all teams, or one team by its team_slug; limit 1-1000, default 100)
curl 'localhost:8095/api/submissions?team=my-team&limit=20'
# one submission: score plus a clinician-readable summary of the bundle
curl localhost:8095/api/submissions/12
# the exact bytes that were submitted
curl localhost:8095/api/submissions/12/bundle -o submitted.json
```

### Exchange / interop

Every team's best passing submission is published as the shared test corpus. Load a
partner's document into your own system and see what breaks. Downloading needs no token
and nothing here is scored - the output of the track is a defect the producer can go and
fix, reported to them directly.

```
# who has published something
curl localhost:8095/api/exchange
# download a partner's document (X-Exchange-Id = submission id, X-Producer = slug)
curl localhost:8095/api/exchange/TEAM_SLUG -o partner-eps.json
```

Then show what it looked like. A **rendering** is a screenshot of a partner's document
as it came out in your system, and it is the only evidence anyone else - another
implementer, a clinician walking the room - can read without installing your product.

```
# publish a screenshot of a partner's document (raw image body, not multipart)
curl -X POST 'localhost:8095/api/exchange/TEAM_SLUG/renderings?note=Allergies+came+through+empty' \
     -H 'X-Team-Token: TOKEN' -H 'Content-Type: image/png' --data-binary @shot.png
# every screenshot, or just one document's / one company's
curl 'localhost:8095/api/renderings?document=12'
curl 'localhost:8095/api/renderings?producer=TEAM_SLUG&consumer=MY_SLUG'
curl localhost:8095/api/renderings/3/image -o shot.png
# your own uploads only: correct the note, or withdraw the screenshot entirely
curl -X PATCH localhost:8095/api/renderings/3 -H 'X-Team-Token: TOKEN' \
     -H 'Content-Type: application/json' -d '{"note":"Allergies empty, dosage fine"}'
curl -X DELETE localhost:8095/api/renderings/3 -H 'X-Team-Token: TOKEN'
```

PNG, JPEG and WebP only, decided by the bytes and not by `Content-Type` - so a file that
is really HTML can never be stored and then served back as a page from this origin.
**400** for anything else and for a screenshot of your own document, **404** if the
company is unknown or has published nothing yet, **413** over `MAX_IMAGE_BYTES`, **403**
on editing or deleting somebody else's upload. The optional `note` (query parameter on
upload, JSON body on `PATCH`) is trimmed to 2000 characters. A rendering pins to the
submission id, not the company: when a producer beats its own score the exchange
document moves on and the old screenshots stay with the bytes they were taken from.

```
# which company a token belongs to
curl localhost:8095/api/me -H 'X-Team-Token: TOKEN'
```

### Leaderboard

```
curl localhost:8095/api/leaderboard
```

The whole API is described at `/openapi.json`, browsable at `/docs`.

Live UI (leaderboard, submission with inline findings, clinical views, interop
screenshots, samples) at <http://localhost:8095/>.

### Samples

Synthetic worked examples of the hackathon scenarios - no real patient data.

```
curl localhost:8095/api/samples
curl localhost:8095/api/samples/andrea-cross-border
curl localhost:8095/api/samples/andrea-cross-border/bundle -o example-eps.json
```

### Health

```
curl localhost:8095/api/health
# {"status":"ok","matchbox":"up","missing_igs":[],"max_body_bytes":5242880,
#  "max_image_bytes":2097152,"max_note_chars":2000,"register_code_required":false}
```

`matchbox` is `up`, `degraded` (reachable, but a package named in `VALIDATION_IGS` is
not loaded - see the restart note under Run) or `down`.

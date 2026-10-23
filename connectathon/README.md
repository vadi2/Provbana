# Nordic Hackathon 2026

Service for the [Nordic Hackathon 2026](https://nordichackathon2026.blogs.dsv.su.se), 18-20 November in Kista.

Gamified FHIR hackathon service: teams register, submit European Patient Summary bundles,
a local [Matchbox](https://github.com/ahdis/matchbox) validates them against HL7 EU EPS, and a live leaderboard keeps score.
Three other EHDS [document types](#document-types) are accepted too, validated against
their own HL7 Europe guide - second-class on purpose: the patient summary is the track.

## Run

Matchbox is required either way. It comes from the compose file in `../itb`, which also
creates the `itb_default` docker network the container run below joins:

```
cd ../itb && docker compose -p itb up -d matchbox   # -> http://localhost:8090/matchboxv3/fhir
```

Matchbox reads `matchbox/config/application.yaml` once, at boot. After editing it
(adding an IG, say) recreate the container - `docker compose -p itb up -d
--force-recreate matchbox` - or the running validator keeps its old package list and
`/fhir/$status` reports it as `degraded`.

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
(`http://hl7.eu/fhir/eps/StructureDefinition/bundle-eu-eps` - the patient summary
profile the event scores against; the other document types carry their own),
`SAMPLES_DIR`
(`samples/` next to `app.py`), `MAX_BODY_BYTES` (5 MB), `MAX_IMAGE_BYTES` (2 MB - the
backstop for an interop screenshot; the UI downscales before it uploads, so this is
not the expected size), `MATCHBOX_TIMEOUT` (60 s - how long a submitting client waits
on a wedged validator), `REGISTER_CODE` (unset = registration is open; set it to
require a `code` parameter on `/fhir/$register`).
`DATA_DIR` must be writable by the user running the service; it holds
`connectathon.db`, the raw submission audit log and `renderings/` (the uploaded
screenshots, one directory per uploading company).

`VALIDATION_IGS` (comma-separated) is the list of packages `/fhir/$status`
insists Matchbox has actually loaded, and `deploy.sh` refuses to finish while
any of them is missing. It defaults to every package named in `EHDS_DOCUMENTS`,
de-duplicated, so a category whose guide is missing is caught before an event
rather than silently scored unchecked. It does *not* decide what a given bundle
is checked against: each document type carries its own package list, and that is
what the validation engine is scoped to - see [Document
types](#document-types). Narrow it only to run against a Matchbox holding fewer
IGs than the registry describes. Whatever is named here must also be listed in
`matchbox/config/application.yaml`, or Matchbox has nothing to load.

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

## Practice resets

The service goes up months before the event so teams can shake their bundles out
against the real validator. `reset.py` empties the leaderboard daily so that a trial
run in September is not still leading in November, and the page says so next to the
Leaderboard heading while it is happening.

`PRACTICE_ENDS` is the whole configuration - one UTC timestamp that sets both the daily
wipe time and the last day one runs. The wipe landing exactly on it is the final one:

```
PRACTICE_ENDS=2026-11-18T03:00:00Z    # 03:00 UTC daily, last wipe the morning of the 18th
```

After that instant the daemon idles for good and the page stops advertising practice
mode, both without anyone doing anything. That is the point: the one process able to
empty the leaderboard must be unable to do it during the event, whether or not the
container gets turned off. Leave `PRACTICE_ENDS` unset and nothing is ever scheduled.

```
python reset.py --status     # what is scheduled, and when it stops
python reset.py --loop       # daily until PRACTICE_ENDS, then idle (how it runs in compose)
python reset.py --yes        # wipe once, now
```

A wipe clears the `endpoints`, `renderings`, `submissions` and
`teams` tables in that order - child
rows first, so no row is ever left pointing at one that has gone - inside a single
transaction, and then removes the per-team directories beside them once that transaction
has committed. A bundle whose row has gone is merely unreachable; a row whose bundle has
gone is a 500 on the way out. It leaves the database, the schema and the trees in place,
so it runs against a live service - unlike `seed_demo.py --reset`, which deletes the data
directory wholesale and needs the service stopped. If there is no database in the data
directory it touches nothing at all and says so, rather than emptying the trees of a
directory that turned out to be the wrong one. Backups are not touched; `backup.py`'s own
pruning ages them out.

| Flag | Default | What it does |
|---|---|---|
| `--loop` | off | Wipe daily until `PRACTICE_ENDS`, then idle |
| `--status` | | Print the schedule, the data directory, and what a wipe would take |
| `--yes` | off | Required to wipe once, now; without it nothing is touched |
| `--data-dir` | `$DATA_DIR`, else `data/` beside `app.py` | Which data directory to clear |

## Keeping the validator warm

Matchbox does not validate against a profile so much as against a *validation engine* -
the expanded snapshots of one guide's entire dependency closure - and it builds that
engine the first time something asks for it. On the event box that first call takes
between 26 and 135 seconds depending on the guide, against a `MATCHBOX_TIMEOUT` of 60.
A participant who is first to send a document of some kind would get a 502 and no
stored submission, and everyone after them would get an answer in under a second.

`deploy/deploy.sh` pays that cost at deploy time, once per category, so nobody at the
event does. The catch is that it does not stay paid: Matchbox does not hold the engines
hard, and they are dropped again after some tens of minutes without use - no restart, no
log line, nothing in `$status` to see. Measured locally at about 75 minutes of idle on a
JVM near its heap ceiling. So a deploy on the Monday leaves the box cold by the Tuesday.

`warm.py --loop` is the answer: every 20 minutes it validates one bundle against every
category, which is four sub-second calls and nothing else. It runs as its own container
in `docker-compose.prod.yml` for the same reason `backup.py` and `reset.py` do - a thing
that must keep happening on a schedule should not be sharing a process with the thing
that serves participants.

```
python warm.py --loop        # every WARM_INTERVAL seconds (how it runs in compose)
python warm.py --once        # one pass; exits non-zero if any category is still cold
```

It logs only the calls that took five seconds or more. In the steady state that means it
logs nothing, and a line appearing after the first pass is the useful signal: it says an
engine had been dropped and had to be rebuilt, so `WARM_INTERVAL` is too long.

The bundle it sends is the event's own patient summary sample, which is not a document
of most of these kinds and comes back invalid for three of the four. That is fine and the
verdict is thrown away - building the snapshots is the entire point, and Matchbox does
that before it can decide anything about the content.

The likely mechanism is soft references: HotSpot clears those in proportion to free heap
(`-XX:SoftRefLRUPolicyMSPerMB`, 1000ms per free MB by default), which explains both the
rough timing and why the same engines survive far longer on a roomy heap - hence
`MATCHBOX_HEAP` in `.env.example` being set well above the validator's working set. That
reading is inferred from the behaviour rather than read out of Matchbox's source, which
is why the fix does not depend on it being right.

## Usage and alerts

The service keeps a usage log, `usage.db`, separate from the results database so that
the nightly practice reset does not wipe it. `monitor.py --loop` runs as its own
container, reads the log and emails the organiser in three cases:

* **Errors, straight away.** Any 5xx from the service, with the traceback, or with
  Matchbox's complaint for a 502. Also any JavaScript error the page reports about itself
  through `POST /ui/error`. The first error is mailed at once. Any more within
  `ALERT_EVERY` (15 minutes) are batched into the next mail.
* **The service not healthy.** `$status` does not answer `up` for five checks in a row,
  one a minute. You get one mail when it goes down and one when it recovers. This is
  what catches a dead validator when nobody happens to be submitting.
* **A digest every morning**, at `DIGEST_HOUR` (06:00 UTC), covering the previous UTC
  day:
  * people in a browser and integration clients
  * companies registered
  * submissions by document type, and how many passed
  * 4xx per API route, since a run of 401s means a team has lost its token
  * a seven-day trend

```
python monitor.py --loop                     # how it runs in compose
python monitor.py --digest [--day 2026-10-19] # print a day's digest, send nothing
python monitor.py --test-email               # check the SMTP settings
```

On the box, the same commands work through
`docker compose -f docker-compose.prod.yml exec monitor python monitor.py --digest`.

Mail goes out over SMTP, configured by the `SMTP_*` and `MAIL_*` variables in
`.env.example`. Use port 587, because Hetzner Cloud blocks outbound 25 and 465. Without
`SMTP_HOST` every message is written to the container's log instead.

What it stores is coarse on purpose:

* Requests are counted per day, route template and status, never per raw path.
* Visitors are counted as a hash of address and user agent, under a salt that changes
  daily and is deleted when the day ends.
* No cookies, and nothing goes to a third party.

A browser counts as one visitor and anything else that calls `/fhir` counts as an
integration client. The split comes from `Sec-Fetch-Site`, which browsers send and API
clients do not. Rows older than `USAGE_RETAIN_DAYS` (180) are pruned.

## Scoring

Any error or fatal validation issue scores the submission **0**. A valid **patient
summary** earns **50**, plus **+10** per *distinct* populated Composition section
(capped at **+160**) and **+2** per clinical resource, meaning everything that is not
the Composition or the Patient (capped at **+60**).

A section is populated when at least one of its `entry` references resolves to a
clinical resource in the bundle - an `emptyReason`, a display-only entry or a
reference to nothing does not count. Sections are de-duplicated by their `code`,
falling back to their `title`; sections with neither collapse into one. Repeating a
section or padding the bundle with sections therefore earns nothing; resources are
worth +2 each up to the cap, and the cap is what keeps padding pointless there.

A team's score is its best **patient summary**, plus a one-time **+20 first pass**
bonus for the first company ever to pass one, plus **+15** for each of the other
[document types](#document-types) it also has a passing submission in. Nothing else
scores - interop testing is deliberately unscored, so there is nothing to farm. Set
`REGISTER_CODE` if you want registration closed as well. Companies on the same score
are ranked by who reached that score first, so another attempt - failed or not - never
costs a company its place.

The +15 is flat and paid once per category however many of that kind you send, and a
non-summary earns no section or resource points at all: richness is measured against
what a patient summary is meant to carry, and counting sections on a lab report would
only reward the wrong shape. Passing all three others is worth 45 - less than a
middling summary - which is the intent. This is the patient summary track; the rest is
credit for breadth, not a route around it. A company that only ever submits summaries
scores exactly what it scored before any of the other categories existed.

## Document types

The event is the European Patient Summary. But EHDS ([Regulation (EU)
2025/327](https://eur-lex.europa.eu/eli/reg/2025/327/oj), Article 14) names six
priority categories of health data that EHR systems have to exchange, and a team that
brings a lab report should get it checked properly rather than scored against the
wrong profile. So four categories are accepted - five of the six, since MPD answers
for both ePrescription and eDispensation - and the three that are not the summary are
deliberately second-class: real validation and real findings, a flat bonus, and no
place in the page's headline.

| Category (Article 14) | Guide | Maturity | Scores |
|---|---|---|---|
| Patient summary | [EPS](https://hl7.eu/fhir/eps/1.0.0-ballot/) | ballot | 50 + sections + resources |
| Medical test result | [Laboratory Report](https://hl7.eu/fhir/laboratory/) | STU 2.0 | +15 |
| ePrescription / eDispensation | [MPD](https://hl7.eu/fhir/mpd/) | STU 1.0 | +15 |
| Medical imaging study and report | [Imaging Report](https://hl7.eu/fhir/imaging/) | ballot | +15 |

The imaging row is the *report*: an `ImagingStudy` is not a document, so the images
themselves have no guide to be validated against.

The sixth category, the hospital discharge report, is left out on purpose. Its guide
has sat at `0.1.0-ballot` without movement, and as the packages load here a discharge
report cannot be made to pass by building it correctly: the guide's `Encounter.status`
value set enumerates the R4 codes, but the R5 CodeSystem its own dependencies pull in
shadows them, so the `finished` a conformant bundle carries takes a hard error. That is
what separates it from the imaging guide, which also fails its published examples but
passes a correctly built report. `matchbox/config/application.yaml` has the detail and
what would have to change to bring it back.

Nothing is selected by the submitter. The category is read off the bundle, strongest
signal first: `Bundle.meta.profile` naming a guide's bundle profile; an entry claiming
an MPD profile (a prescription is a collection, not a document, so MPD defines no
Bundle profile and its evidence sits one level down); `Composition.type`, which EPS
and laboratory each bind to a single LOINC code; an `ImagingStudy` in the bundle
(imaging binds `Composition.type` to every LOINC descendant of 18726-0, which needs a
terminology server to decide, so the study stands in for the code check); and finally
medications in a bundle with no Composition. Anything unrecognised is treated as a
patient summary - the event's own track - so failing to announce a document can never
demote a summary out of the scoring.

Claim the profile in `meta.profile`, or pass `?profile=` on the submit call, to settle
it outright. `curl 'localhost:8095/fhir/$documents'` lists the lot, including whether
Matchbox has each one's packages loaded.

A prescription is checked against base `Bundle`, and the validator applies an MPD
profile only to an entry that claims one. So an MPD submission passes only when it
carries at least one `MedicationRequest` or `MedicationDispense` and every one of them
claims its MPD profile in `meta.profile`, unversioned or at the version the validator
loads. An order that claims nothing would meet base R4 alone, so it is failed with an
error saying which profile to add rather than paid for conformance nobody checked.

Each category is validated in its own engine: Matchbox scopes an engine to the
requested profile's dependency closure, so a lab report checked with the summary's
packages comes back with its conformance *unchecked* rather than checked and failed -
a silent pass. The package list per category lives in `EHDS_DOCUMENTS` in `app.py`,
and each category's own guide must also be in `matchbox/config/application.yaml`.
Adding a fifth category is an entry in each; `test_every_category_the_registry_names_
is_a_package_the_validator_loads` holds the two together. Not every package in the
registry needs a line in the yaml - `hl7.fhir.eu.base#2.0.0-ballot` is named by the
laboratory entry but reaches the validator through imaging's dependencies - which is
why that test checks the guides, not the whole list. One of the guides fails its own
published examples for reasons outside this repository;
`matchbox/config/application.yaml` records which and why.

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

This is a FHIR R4 server, not a bespoke API with FHIR-shaped payloads. The base is
`/fhir`, every response is `application/fhir+json`, and every error - a 404, a wrong
method, a bad query parameter - is an `OperationOutcome`, so a client that can read a
validation failure needs no second parser for anything else. Start at the
CapabilityStatement and the whole surface is discoverable from there:

```
curl localhost:8095/fhir/metadata
```

Endpoints that write authenticate with `Authorization: Bearer TOKEN` (or
`X-Team-Token: TOKEN`). Reads are open. A missing or unknown token is **401**, with a
`WWW-Authenticate: Bearer` challenge and a message saying what was wrong with what was sent.

Anything the server computes rather than stores - a ranking, a health check, a token -
comes back as a `Parameters` resource. That is FHIR's answer for exactly this, and it
has two consequences worth knowing before you parse one: a repeated parameter name *is*
the list (FHIR has no array type), and an absent parameter *is* the null (FHIR has no
null), so a company that has never submitted simply has no `bestErrors` part rather
than one set to `null`. The same goes for an empty list: a search with no matches has
no `entry`, and an operation with nothing to report has no `parameter`.

Searches (`Organization`, `Endpoint`, `DocumentReference`) read their parameters the
FHIR way: commas inside one are ORed and a repeated parameter is ANDed. A parameter the
search does not know is passed over, and the searchset's `self` link lists only the ones
that were applied; send `Prefer: handling=strict` to get a **400** instead. A modifier or
chain the search does not support (`name:text`, `organization.name`) is always a **400**,
since ignoring it would answer with everything.

### Registration

```
# one registration per company - keep the token, it is shown once
curl -X POST 'localhost:8095/fhir/$register' -H 'Content-Type: application/fhir+json' -d '{
  "resourceType": "Parameters",
  "parameter": [{"name": "name", "valueString": "My Company"}]
}'
```

Answers `Parameters` with a `token` and the company as an `Organization`. **400** if the
name is not 2-40 characters, **403** if `REGISTER_CODE` is set and the `code` parameter
is wrong or missing, **409** if the name is taken (whitespace-normalised, NFC-composed
and with invisible format characters such as zero-width spaces dropped, before both
checks; case-insensitive for ASCII letters only).

If your system can take documents in directly, say where in the same call: an
`endpoint` part carrying an [Endpoint](#advertising-where-your-system-takes-documents-in)
resource is stored with the company, and the `Organization` comes back referencing it. A
bad endpoint is a **400** that registers nothing, so the retry is not a 409.

```
curl -X POST 'localhost:8095/fhir/$register' -H 'Content-Type: application/fhir+json' -d '{
  "resourceType": "Parameters",
  "parameter": [{"name": "name", "valueString": "My Company"},
                {"name": "endpoint", "resource": {"resourceType": "Endpoint",
                  "status": "active", "connectionType": {"code": "web-page"},
                  "address": "https://example.org/send-us-data"}}]
}'
```

The `Organization`'s id is the slug - the lower-cased, ASCII-folded name with runs of
anything else turned into `-` (`Åland Hälsa` is `aland-halsa`), suffixed `-2`, `-3` if
that slug is taken. It is how the company is addressed everywhere else, and it is what
`organizationSlug` carries in the operations below.

```
# every registered company, as a searchset
curl localhost:8095/fhir/Organization
# by name: the start of it, ignoring case and accents; name:contains matches
# anywhere and name:exact the whole name as written. Commas are ORed
# (name=aland,nordic), repeats are ANDed, and \, is a comma in the name
curl 'localhost:8095/fhir/Organization?name=aland'
curl localhost:8095/fhir/Organization/aland-halsa
# which company a token belongs to
curl 'localhost:8095/fhir/Organization/$me' -H 'Authorization: Bearer TOKEN'
```

### Submission

`$validate` is FHIR's own operation for this, so the verb a client already knows is the
verb that scores you:

```
# validate + score a bundle; the score is in the X-Score response header
curl -D - -X POST 'localhost:8095/fhir/Bundle/$validate' -H 'Authorization: Bearer TOKEN' \
     -H 'Content-Type: application/fhir+json' --data-binary @bundle.json
# ...or say outright which document type it is, rather than letting it be detected
curl -D - -X POST 'localhost:8095/fhir/Bundle/$validate?profile=http://hl7.eu/fhir/laboratory/StructureDefinition/Bundle-eu-lab' \
     -H 'Authorization: Bearer TOKEN' \
     -H 'Content-Type: application/fhir+json' --data-binary @lab-report.json
```

Body is the Matchbox `OperationOutcome`. `Content-Location` names the Bundle the server
stored, so the document you just submitted is readable straight back at that URL.
Headers: `X-Pass`, `X-Score`, `X-Submission-Score`, `X-Errors`, `X-Sections`,
`X-Resources`, `X-Document-Type`, `X-Document-Profile`, `X-Secondary-Categories`,
`X-Best-Patient-Summary`, `X-First-Pass`.
**400** if the body is not a JSON object or names a `profile` this event does not
score, **413** if the bundle exceeds `MAX_BODY_BYTES`, **502** if Matchbox is
unreachable or fails on its side (a 5xx from the validator is never scored against the
bundle). Only attempts that got a verdict are recorded and kept in the audit log.

`X-Score` is the company's standing *after* this submission and `X-Submission-Score` is
what this bundle alone earned. The two used to be one number; they part company as soon
as a second [document type](#document-types) is in play, and reporting only the bundle
made following a good summary with a lab report look like the score had collapsed.

`X-Best-Patient-Summary`, `X-First-Pass` and `X-Secondary-Categories` are the three
terms `X-Score` is made of - best summary, plus 20 if the company holds the one-time
first-pass bonus, plus 15 per other category passed - so a client can show the
arithmetic instead of a bare total. They have to account for every point of `X-Score`,
which is what the tests assert. `X-Best-Patient-Summary` is `0` only when no summary has
passed yet: a summary that passes always earns at least the 50 base, so 0 means "not on
the track", not "a summary worth nothing", and the submit page says exactly that.

`X-Document-Type` is the category the bundle was read as, and `X-Document-Profile` the
canonical it was therefore checked against. Worth reading first: a team that meant to
send a lab report and sees `eps` here has its answer before it reads a single finding -
the bundle never claimed what it was. `?profile=` settles it, and is rejected with
**400** for any canonical outside the four. A `|version` suffix is ignored, as it is in
`meta.profile`: the bundle is checked against the version the validator has loaded.

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
# submission history: every company, or one by its slug (count 1-1000, default 100)
curl 'localhost:8095/fhir/$submissions?organization=my-company&count=20'
# the exact bytes that were submitted
curl localhost:8095/fhir/Bundle/12 -o submitted.json
```

Each `entry` carries `id`, `organization`, `organizationSlug`, `recorded`, `pass`,
`score`, `errors`, `warnings`, `sections`, `resources`, `documentType` and the `bundle`
URL to fetch it from. `$submissions` is an operation rather than a search because a score is the output
of validating, not a property of the document - a searchset of Bundles would have
nowhere honest to put it.

### Exchange / interop

Every company's best passing submission is published as the shared test corpus. Load a
partner's document into your own system and see what breaks. Downloading needs no token
and nothing here is scored - the output of the track is a defect the producer can go and
fix, reported to them directly.

```
# who has published something - organizationSlug is the COMPANY_SLUG below
curl 'localhost:8095/fhir/$exchange'
# only the companies with this anywhere in their name or slug, ignoring case
# and accents
curl 'localhost:8095/fhir/$exchange?name=aland'
# download a partner's document (Content-Location = the Bundle's own URL)
curl 'localhost:8095/fhir/Organization/COMPANY_SLUG/$document' -o partner-eps.json
```

Then show what it looked like. A screenshot of a partner's document as it came out in
your system is the only evidence anyone else - another implementer, a clinician walking
the room - can read without installing your product. It is a `DocumentReference`:
`custodian` is whose document it is, `author` is taken from your token, and the image
rides in `content[0].attachment.data` as base64.

```
# publish a screenshot of a partner's document - the image is piped into jq,
# because a real screenshot is too big to pass as a command-line argument
base64 -w0 shot.png | jq -Rs --arg slug COMPANY_SLUG '{
  resourceType: "DocumentReference", status: "current",
  custodian: {reference: ("Organization/" + $slug)},
  description: "Allergies came through empty",
  content: [{attachment: {contentType: "image/png", data: .}}]
}' | curl -X POST localhost:8095/fhir/DocumentReference \
       -H 'Authorization: Bearer TOKEN' \
       -H 'Content-Type: application/fhir+json' --data-binary @-
# every screenshot, or just one document's / one company's / one author's
curl 'localhost:8095/fhir/DocumentReference?related=12'
curl 'localhost:8095/fhir/DocumentReference?custodian=COMPANY_SLUG&author=MY_SLUG'
curl localhost:8095/fhir/Binary/3 -o shot.png
# or as a FHIR Binary resource, the image base64-encoded in data
curl -H 'Accept: application/fhir+json' localhost:8095/fhir/Binary/3
# your own uploads only: correct the note, or withdraw the screenshot entirely
curl -X PUT localhost:8095/fhir/DocumentReference/3 -H 'Authorization: Bearer TOKEN' \
     -H 'Content-Type: application/fhir+json' \
     -d '{"resourceType":"DocumentReference","description":"Allergies empty, dosage fine"}'
curl -X DELETE localhost:8095/fhir/DocumentReference/3 -H 'Authorization: Bearer TOKEN'
```

PNG, JPEG and WebP only, decided by the bytes and not by `attachment.contentType` - so a
file that is really HTML can never be stored and then served back as a page from this
origin. **400** for anything else and for a screenshot of your own document, **404** if
the company is unknown or has published nothing yet, **413** over `MAX_IMAGE_BYTES`,
**403** on editing or deleting somebody else's upload. `description` is trimmed to 2000
characters, and `PUT` rewrites only that - the image, the companies and the document it
points at are settled at upload. A screenshot pins to `context.related`, the exact
Bundle, not the company: when a producer beats its own score the exchange document moves
on and the old screenshots stay with the bytes they were taken from.

#### Advertising where your system takes documents in

The other direction: if your platform would rather be sent documents than go and fetch
them, say where with an `Endpoint`. `connectionType.code` is `hl7-fhir-rest` for a FHIR
base, or `web-page` for a page that explains how to get data in. `address` is the http(s) URL. `name` is an optional short label, and
R5's `Endpoint.description` (carried as a cross-version extension) is where to say what
to send and how to authenticate. `managingOrganization` is taken from your token, and the
company's `Organization` lists its endpoints under `endpoint`. The server never calls the
address. It is there for other people to find, and they see it on the Interop tab.

```
curl -X POST localhost:8095/fhir/Endpoint -H 'Authorization: Bearer TOKEN' \
     -H 'Content-Type: application/fhir+json' -d '{
  "resourceType": "Endpoint", "status": "active",
  "connectionType": {"code": "hl7-fhir-rest"},
  "name": "Staging server", "address": "https://fhir.example.org/r4",
  "extension": [{"url": "http://hl7.org/fhir/5.0/StructureDefinition/extension-Endpoint.description",
                 "valueString": "POST a document Bundle to /Bundle. Ask at our table for a token."}]}'
# everyone's, or one company's, or one kind
curl localhost:8095/fhir/Endpoint
curl 'localhost:8095/fhir/Endpoint?organization=COMPANY_SLUG&connection-type=web-page'
# your own only: PUT replaces it whole, DELETE stops advertising it
curl -X PUT localhost:8095/fhir/Endpoint/3 -H 'Authorization: Bearer TOKEN' \
     -H 'Content-Type: application/fhir+json' -d @endpoint.json
curl -X DELETE localhost:8095/fhir/Endpoint/3 -H 'Authorization: Bearer TOKEN'
```

**400** for an address that is not an absolute http(s) URL, is over 2000 characters or
carries a user name or password (`https://user:pass@host` - this list is public), a
`connectionType` code other than these two (or with a `system` that is not that code's),
a `status` other than `active` (delete an endpoint rather than parking it), a top-level
R5 `description` (R4 has none - use the extension), a `header` (this list is public, so
say how to authenticate in the description instead), or a body `id` that differs from the
URL's on `PUT`. `payloadType` is set by the server: `any` for a FHIR base, `none` for a
page. **403** on changing somebody else's, **404** for one that does not exist,
**422** past five per company. The name has its whitespace collapsed and is trimmed to
100 characters, the description to 2000.

### Leaderboard

```
curl 'localhost:8095/fhir/$leaderboard'
```

One `entry` per registered company, best score first, carrying `rank`, `organization`,
`organizationSlug`, `score`, `passed`, `attempts`, `firstPass` and - for companies that
have submitted - `bestErrors` and `lastSubmission`. A company that has never submitted
has a `score` of 0 and sorts below every company that has.
`secondaryCategories` repeats once per non-summary [document
type](#document-types) the company has passed, in registry order, and is absent
for a company that has passed none. `bestPatientSummary` is the score the
standing rests on, and `bestSections` and `bestResources` describe that same
submission; all three are absent for a company that has not submitted a summary at
all, even one that has submitted other document types.

The whole API is also described at `/openapi.json`, browsable at `/docs`.

Live UI (leaderboard, submission with inline findings, clinical views, interop
screenshots, samples) at <http://localhost:8095/>. The clinician-readable summary the
page draws is served from `/ui/documents/{id}`, which is deliberately *not* part of the
API: it is a rendering, there is no FHIR resource for one, and torturing `Parameters`
into carrying a patient banner would make the FHIR surface worse than the custom one it
replaced. It is out of the CapabilityStatement and out of `/openapi.json`; build against
`/fhir/Bundle/{id}` instead.

### Samples

Synthetic worked examples of the hackathon scenarios - no real patient data.

```
curl 'localhost:8095/fhir/$samples'
curl localhost:8095/fhir/Bundle/andrea-cross-border -o example-eps.json
```

Bundle ids are numeric for submissions and slugs for samples, so the two share one id
space and can never collide.

### Document types

```
curl 'localhost:8095/fhir/$documents'
```

One `entry` per [document type](#document-types), the patient summary first and the
only one carrying `primary: true`. Each names its `profile`, its `ig` packages
(repeated), `maturity`, `spec`, what `points` it is worth, and what it is
`detectedBy...` - so a client can work out why its bundle was read as it was without
guessing. `ready` says whether Matchbox has that category's packages loaded, and is
absent when Matchbox could not be asked at all: "we do not know" and "we know it is
not loaded" send an organiser to two different places.

### Health

```
curl 'localhost:8095/fhir/$status'
# Parameters: matchbox=up, missingIg (repeated, absent when none), maxBundleBytes,
# maxImageBytes, maxNoteChars, registerCodeRequired, profile, practiceEnds
```

Separate from `metadata` on purpose: what the server can do does not change, and whether
the validator is answering right now does.

`matchbox` is `up`, `degraded` (reachable, but a package named in `VALIDATION_IGS` is
not loaded - see the restart note under Run) or `down`.

`practiceEnds` is `PRACTICE_ENDS`, and absent when it is unset. The page compares it
against the browser's own clock rather than being handed a yes/no, so the practice notice clears itself at the
final wipe even on a tab nobody has reloaded since the night before.

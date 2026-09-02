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
(`samples/` next to `app.py`), `MAX_BODY_BYTES` (5 MB), `MATCHBOX_TIMEOUT` (60 s -
how long a submitting client waits on a wedged validator), `REGISTER_CODE` (unset =
registration is open; set it to require `{"code": "..."}` on `/api/register`).
`DATA_DIR` must be writable by the user running the service; it holds
`connectathon.db` and the raw submission audit log.

`VALIDATION_IGS` (default `hl7se.fhir.base#1.1.0`, comma-separated) names extra
packages to load into the validation engine. Matchbox scopes an engine to the IG
that defines `PROFILE`, so a `meta.profile` claim from any other package is
reported as "has not been checked" rather than validated. The named packages
must also be listed in `matchbox/config/application.yaml`, or Matchbox has
nothing to load.

Tests: `.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q test_app.py`

Demo data for a local preview: `.venv/bin/python seed_demo.py` (with the service and
Matchbox running) registers a handful of fake companies and submits the bundles in
`examples/` and `samples/`, so every UI state has something to show. `--reset` deletes
the data directory first - stop the service before, restart it after. Never point it at
the event instance.

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
partner's document into your own system and see what breaks. Nothing here needs a token
and nothing is scored or recorded - the output of the track is a defect the producer can
go and fix, reported to them directly.

```
# who has published something
curl localhost:8095/api/exchange
# download a partner's document (X-Exchange-Id = submission id, X-Producer = slug)
curl localhost:8095/api/exchange/TEAM_SLUG -o partner-eps.json
```

### Leaderboard

```
curl localhost:8095/api/leaderboard
```

The whole API is described at `/openapi.json`, browsable at `/docs`.

Live UI (leaderboard, clinical views, samples) at <http://localhost:8095/>.

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
# {"status":"ok","matchbox":"up","missing_igs":[],"max_body_bytes":5242880,"register_code_required":false}
```

`matchbox` is `up`, `degraded` (reachable, but a package named in `VALIDATION_IGS` is
not loaded - see the restart note under Run) or `down`.

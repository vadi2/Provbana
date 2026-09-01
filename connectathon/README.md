# EPS Connectathon

Gamified FHIR hackathon service: teams register, submit European Patient Summary bundles,
a local [Matchbox](https://github.com/ahdis/matchbox) validates them against HL7 EU EPS, and a live leaderboard keeps score.

## Run

Matchbox is required either way. It comes from the compose file in `../itb`, which also
creates the `itb_default` docker network the container run below joins:

```
cd ../itb && docker compose -p itb up -d matchbox   # -> http://localhost:8090/matchboxv3/fhir
```

**Docker** (needs `itb_default`, so run the compose command above first - without it
`docker run` fails with `network itb_default not found`):

```
docker build -t connectathon .
docker run --network itb_default -p 8095:8095 -v conn-data:/data connectathon
```

**Bare metal** (Python 3.10+; the image uses 3.12):

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATA_DIR=./data MATCHBOX_URL=http://localhost:8090/matchboxv3/fhir .venv/bin/python app.py
```

`python app.py` binds `0.0.0.0`, so participants reach it at `http://<organiser-ip>:8095/`.
Do not use `uvicorn app:app` - it defaults to localhost only and ignores `PORT`.

Environment: `PORT` (8095), `DATA_DIR` (`/data`), `MATCHBOX_URL`, `PROFILE`,
`SAMPLES_DIR`, `MAX_BODY_BYTES` (5 MB), `MATCHBOX_TIMEOUT` (60 s - how long a
submitting client waits on a wedged validator), `REGISTER_CODE` (unset =
registration is open; set it to require `{"code": "..."}` on `/api/register`).
`DATA_DIR` must be writable by the user running the service; it holds
`connectathon.db` and the raw submission audit log.

Tests: `.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q test_app.py`

## Scoring

Any error or fatal validation issue scores the submission **0**. A valid bundle earns
**50**, plus **+10** per *distinct* populated Composition section (capped at **+160**)
and **+2** per clinical resource, meaning everything that is not the Composition or the
Patient (capped at **+60**).

Sections are de-duplicated by their `code`, falling back to their `title`; sections with
neither collapse into one. Repeating a section or padding the bundle therefore earns
nothing - richness has to be real.

A team's score is its best submission. The first team ever to go valid keeps a one-time
**+20 first blood** bonus. Interop adds **+25** per partner document the team proved it
consumed and **+10** per team that consumed the team's own, capped at **+100** together;
registration is open by design, so the cap is what stops a team farming the track with
throwaway partners. Set `REGISTER_CODE` if you want registration closed as well.

## API

Team endpoints authenticate with `X-Team-Token: TOKEN` (or `Authorization: Bearer TOKEN`).

### Registration

```
# one team per company - keep the token, it is shown once
curl -X POST localhost:8095/api/register -H 'Content-Type: application/json' -d '{"team":"My Team"}'
```

### Submission

```
# validate + score a bundle; score is in the X-Score response header
curl -D - -X POST localhost:8095/api/eps -H 'X-Team-Token: TOKEN' \
     -H 'Content-Type: application/fhir+json' --data-binary @bundle.json
```

Body is the Matchbox `OperationOutcome`. Headers: `X-Pass`, `X-Score`, `X-Errors`,
`X-Sections`, `X-Resources`. 413 if the bundle exceeds `MAX_BODY_BYTES`, 502 if
Matchbox is unreachable.

```
# submission history (all teams, or one team; limit 1-1000, default 100)
curl 'localhost:8095/api/submissions?team=my-team&limit=20'
# one submission: score plus a clinician-readable summary of the bundle
curl localhost:8095/api/submissions/12
# the exact bytes that were submitted
curl localhost:8095/api/submissions/12/bundle -o submitted.json
```

### Exchange / interop

Every team's best passing submission is published as the shared test corpus. Consume a
partner's document, extract four facts from it, and post a receipt to claim interop points.

```
# who has published something (add your token to hide yourself and see what you consumed)
curl localhost:8095/api/exchange
# download a partner's document
curl localhost:8095/api/exchange/TEAM_SLUG -o partner-eps.json
# prove you parsed it - 422 {"result":"mismatch","fields":[...]} tells you which facts are wrong
curl -X POST localhost:8095/api/interop/confirm -H 'X-Team-Token: TOKEN' \
     -H 'Content-Type: application/json' \
     -d '{"partner":"TEAM_SLUG","patient_family":"Musterfrau","birth_date":"1961-12-24","medications":3,"allergies":1}'
# who has consumed whose document
curl localhost:8095/api/interop/matrix
```

`exchange_id` may be added to the receipt body to pin a specific submission.

### Leaderboard

```
curl localhost:8095/api/leaderboard
```

Live UI (leaderboard, clinician views, samples) at <http://localhost:8095/>.

### Samples

Synthetic worked examples of the hackathon scenarios - no real patient data.

```
curl localhost:8095/api/samples
curl localhost:8095/api/samples/andrea-cross-border
curl localhost:8095/api/samples/andrea-cross-border/bundle -o example-eps.json
```

### Health

```
curl localhost:8095/api/health   # {"status":"ok","matchbox":"up"}
```

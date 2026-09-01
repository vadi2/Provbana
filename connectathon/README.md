# EPS Connectathon

Gamified FHIR hackathon service: teams register, submit European Patient Summary bundles,
a local [Matchbox](https://github.com/ahdis/matchbox) validates them against HL7 EU EPS, and a live leaderboard keeps score.

Run (docker): `docker build -t connectathon . && docker run --network itb_default -p 8095:8095 -v conn-data:/data connectathon`

Run (bare): `DATA_DIR=./data MATCHBOX_URL=http://localhost:8090/matchboxv3/fhir uvicorn app:app --port 8095`

- Register: `curl -X POST localhost:8095/api/register -H 'Content-Type: application/json' -d '{"team":"My Team"}'`
- Submit: `curl -D - -X POST localhost:8095/api/eps -H 'X-Team-Token: TOKEN' -H 'Content-Type: application/fhir+json' --data-binary @bundle.json`
- Browse the shared corpus (no token): `curl localhost:8095/api/exchange`
- Download a team's document: `curl localhost:8095/api/exchange/TEAM_SLUG -o partner-eps.json`
- Leaderboard: `curl localhost:8095/api/leaderboard` - UI at http://localhost:8095/

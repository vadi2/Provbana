# TODO

## Rebrand from "EPS Connectathon" to the Nordic Hackathon 2026

The event is the Nordic Hackathon 2026 (Nov 18-20, Kista):
<https://nordichackathon2026.blogs.dsv.su.se>

Replace the placeholder "EPS Connectathon" naming everywhere it surfaces, and
link back to the event site so the service is recognisably part of it.

Known occurrences in `connectathon/app.py`:

- module docstring, first line
- `FastAPI(title="EPS Connectathon")`
- `<title>EPS Connectathon</title>`
- `<h1>EPS Connectathon - Patient Summary Challenge</h1>`

Also check `connectathon/README.md` (H1) and the test assertions in
`connectathon/test_app.py` that match on the current title (module docstring,
and the UI title assertion around line 900).

Leave `connectathon.db` / the `DATA_DIR` layout alone - renaming the database
file buys nothing and breaks existing deployments.

Open question: keep "connectathon" as the directory name, or rename the
directory too?

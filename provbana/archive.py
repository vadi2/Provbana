"""Write a static, read-only copy of a finished event, for GitHub Pages.

    python archive.py --data <data dir or backup snapshot> --out <empty dir>
                      [--cname hackathon.example.org]

The page is the service's own, rendered with ARCHIVE set: it reads every
answer it needs from files written here, beside it, and hides everything that
writes - registering, submitting, previews, screenshots, endpoints. What it
shows is exactly what anyone could read on the live site: the final
leaderboard, every company's submissions in the clinical view, the published
documents and their screenshots, and the worked examples.

It runs the current code over a copy of the data, never over the data itself
(opening the database may migrate it), and needs no validator: nothing here
validates. Tokens, the registration code and the advertised endpoints are
never written, and the export stops if a token turns up in its output.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile

IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def _entries(params: dict, name: str = "entry") -> list[dict]:
    """A Parameters resource's repeated `name` entries, each flattened to
    {part name: value}."""
    out = []
    for p in params.get("parameter", []):
        if p.get("name") != name:
            continue
        out.append({part["name"]: next(v for k, v in part.items() if k.startswith("value"))
                    for part in p.get("part", []) if any(k.startswith("value") for k in part)})
    return out


def _safe(value) -> str:
    value = str(value)
    if not value or not value.replace("-", "").isalnum():
        raise ValueError("not a file-safe id: %r" % value)
    return value


class Site:
    def __init__(self, client, out: str):
        self.client = client
        self.out = out
        self.written = []

    def write(self, rel: str, body: bytes | str) -> None:
        path = os.path.join(self.out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(body.encode() if isinstance(body, str) else body)
        self.written.append(rel)

    def get(self, url: str, **kw):
        res = self.client.get(url, **kw)
        res.raise_for_status()
        return res

    def json(self, url: str) -> dict:
        return self.get(url).json()

    def write_json(self, rel: str, data: dict) -> None:
        # The test client's host is no address anyone can open.
        text = json.dumps(data, ensure_ascii=False).replace("http://testserver", "")
        self.write(rel, text)


def export_site(client, app, out: str, cname: str | None = None) -> dict:
    """Write the archive into `out` through `client`, a TestClient on `app`.
    Returns what it counted."""
    site = Site(client, out)
    site.write("index.html", app.render_index(app.INDEX_HTML, archive=True))
    site.write("favicon.svg", app.FAVICON_SVG)
    if os.path.isfile(app.THEME_CSS):
        with open(app.THEME_CSS, "rb") as fh:
            site.write("theme.css", fh.read())
    # Jekyll would drop every file and folder whose name starts with "_".
    site.write(".nojekyll", "")
    if cname:
        site.write("CNAME", cname + "\n")

    for name in ("documents", "leaderboard", "samples", "exchange"):
        site.write_json("data/%s.json" % name, site.json("/fhir/$" + name))

    def document(doc_id) -> None:
        doc_id = _safe(doc_id)
        site.write_json("data/documents/%s.json" % doc_id, site.json("/ui/documents/" + doc_id))
        site.write("files/bundles/%s.json" % doc_id, site.get("/fhir/Bundle/" + doc_id).content)

    samples = _entries(site.json("/fhir/$samples"))
    for sample in samples:
        document(sample["id"])

    # The clinical view's picker lists the leaderboard's companies that have
    # submitted anything, and each one's list is its own $submissions.
    companies = [row for row in _entries(site.json("/fhir/$leaderboard"))
                 if row.get("attempts")]
    submissions = 0
    for row in companies:
        slug = _safe(row["organizationSlug"])
        subs = site.json("/fhir/$submissions?organization=%s&count=1000" % slug)
        site.write_json("data/submissions/%s.json" % slug, subs)
        for sub in _entries(subs):
            document(sub["id"])
            submissions += 1

    screenshots = 0
    for doc in _entries(site.json("/fhir/$exchange")):
        doc_id = _safe(doc["id"])
        refs = site.json("/fhir/DocumentReference?related=" + doc_id)
        for entry in refs.get("entry", []):
            attachment = entry["resource"]["content"][0]["attachment"]
            image = site.get(attachment["url"], headers={"Accept": "image/*"})
            ext = IMAGE_EXT[image.headers["content-type"].split(";")[0]]
            rel = "files/screenshots/%s.%s" % (_safe(attachment["url"].rsplit("/", 1)[1]), ext)
            site.write(rel, image.content)
            attachment["url"] = rel
            screenshots += 1
        site.write_json("data/screenshots/related-%s.json" % doc_id, refs)

    _check_no_secrets(app, site)
    return {"companies": len(companies), "submissions": submissions,
            "samples": len(samples), "screenshots": screenshots,
            "files": len(site.written)}


def _check_no_secrets(app, site: Site) -> None:
    with sqlite3.connect(app.db_path()) as conn:
        secrets = {t for (t,) in conn.execute("SELECT token FROM teams") if t}
    code = os.environ.get("REGISTER_CODE")
    if code:
        secrets.add(code)
    for rel in site.written:
        if rel.startswith("files/"):
            continue  # submitted bundles and screenshots, as received
        with open(os.path.join(site.out, rel), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for secret in secrets:
            if secret in text:
                raise SystemExit("a team token turned up in %s - not publishing this" % rel)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True,
                    help="a data directory, or a backup snapshot of one")
    ap.add_argument("--out", required=True, help="an empty or new directory")
    ap.add_argument("--cname", help="the custom domain GitHub Pages serves it on")
    args = ap.parse_args(argv)

    if not os.path.isfile(os.path.join(args.data, "connectathon.db")):
        ap.error("no connectathon.db in %s" % args.data)
    if os.path.isdir(args.out) and os.listdir(args.out):
        ap.error("%s is not empty" % args.out)

    work = tempfile.mkdtemp(prefix="provbana-archive-")
    try:
        data = os.path.join(work, "data")
        shutil.copytree(args.data, data)
        os.environ["DATA_DIR"] = data
        os.environ["USAGE_DB"] = os.path.join(work, "usage.db")
        os.environ.pop("REGISTER_CODE", None)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import app
        from fastapi.testclient import TestClient
        with TestClient(app.app) as client:
            counts = export_site(client, app, args.out, args.cname)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("%(companies)d companies, %(submissions)d submissions, %(samples)d samples, "
          "%(screenshots)d screenshots - %(files)d files in " % counts + args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

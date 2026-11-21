"""Tell the organiser who is using the service, and when it breaks.

    .venv/bin/python monitor.py --loop                  # alongside the service
    .venv/bin/python monitor.py --digest                # print yesterday's digest
    .venv/bin/python monitor.py --digest --day 2026-10-19
    .venv/bin/python monitor.py --test-email            # check the SMTP settings

Two halves in one module. app.py imports the recording half and writes a row
for every request, registration, submission and error into a database of its
own, usage.db. The loop half runs as its own container, reads that database and
sends mail: one message soon after something errors, and one digest a day.

Why a separate database rather than tables in connectathon.db: reset.py empties
the results every night until PRACTICE_ENDS, and the whole point of the usage
log is to remember across those wipes who has been trying the service. It also
keeps the per-request write - the page polls the leaderboard every five
seconds - off the lock that submissions take. On the event box it lives beside
the data directory rather than inside it, so a backup restore, which swaps the
data directory wholesale, does not take the usage history with it.

What it stores is deliberately coarse. Requests are counted per day, route
template (never the raw path, which a scanner can make unbounded) and status.
Visitors are counted as a hash of address and user agent under a salt that is
random per day and deleted once the day is over, so the counts survive and the
hashes stop being linkable to anybody. No cookies, nothing sent to a third
party, nothing on the page beyond a handler that reports its own JavaScript
errors.

What counts as broken:

  * a 5xx from the service, with the traceback for an unhandled exception and
    the detail for a raised one (a 502 carries Matchbox's own complaint);
  * a JavaScript error on the page, reported by the page itself;
  * $status not answering "up" for HEALTH_FAILURES checks in a row, which is
    how a validator outage gets noticed when nobody happens to be submitting.

4xx are not alerts - they are participants getting a request wrong, which is
what the service is for - but the digest counts them per route, because twenty
401s on $validate is a team that has lost its token.

Mail goes over SMTP (smtplib, stdlib only). Without SMTP_HOST every message is
written to stdout instead, which is the right behaviour on a laptop and makes
`docker compose logs monitor` show what would have been sent. Hetzner Cloud
blocks outbound 25 and 465 on new projects, so use a provider's 587.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import sys
import time
from contextlib import contextmanager
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))

# The service's own $status, reached across the compose network. The probe's
# requests carry USER_AGENT and app.py leaves them out of the counts, or the
# monitor would be the most loyal API client in every digest.
STATUS_URL = os.environ.get("STATUS_URL", "http://localhost:8095/fhir/$status")
USER_AGENT = "provbana-monitor"

SITE = os.environ.get("SITE_ADDRESS", "localhost")
MAIL_TO = os.environ.get("MAIL_TO", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

CHECK_INTERVAL = int(os.environ.get("MONITOR_INTERVAL", "60"))
# The first error mails at once; anything after it inside this window waits
# and goes out as one message, so a submit loop hitting a dead validator is
# one mail every quarter of an hour and not one per attempt.
ALERT_EVERY = int(os.environ.get("ALERT_EVERY", "900"))
# Five minutes of "not up". A deploy recreates matchbox and costs a minute or
# two of package loading, which should not page anybody.
HEALTH_FAILURES = int(os.environ.get("HEALTH_FAILURES", "5"))
# UTC, so that it does not depend on the image carrying tzdata: 06:00 UTC is
# 07:00 in Stockholm in winter and 08:00 in summer.
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "6"))
RETAIN_DAYS = int(os.environ.get("USAGE_RETAIN_DAYS", "180"))

# Per source per day. The browser-error endpoint is open to anyone, and a
# server error inside a loop can repeat thousands of times; past this the rows
# stop and only the hit counts keep going up.
MAX_ERRORS_PER_DAY = 200
MAX_MESSAGE_CHARS = 4000
# Errors spelled out in full in one alert; the rest are counted.
ALERT_DETAIL = 10

# Crawlers and link previews fetch the page too. They are not people trying
# the service, so they are counted as hits but not as visitors.
BOT_UA = re.compile(r"bot|crawl|spider|slurp|preview|monitor|uptime|curl/|wget",
                    re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hits (
    day TEXT NOT NULL,
    method TEXT NOT NULL,
    route TEXT NOT NULL,
    status INTEGER NOT NULL,
    -- 'browser' when the request came from a web page (it carries
    -- Sec-Fetch-Site), 'client' for anything else: curl, a test script, a
    -- company's own platform. The second kind is the integration work.
    via TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (day, method, route, status, via)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS visitors (
    day TEXT NOT NULL,
    via TEXT NOT NULL,
    visitor TEXT NOT NULL,
    PRIMARY KEY (day, via, visitor)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS salts (
    day TEXT PRIMARY KEY,
    salt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    team TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    source TEXT NOT NULL,
    method TEXT,
    route TEXT,
    status INTEGER,
    message TEXT,
    alerted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def log(msg: str) -> None:
    print("%s monitor: %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg),
          flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------ recording side

_initialised: set[str] = set()


def connect(path: str) -> sqlite3.Connection:
    """usage.db, created on first use. The schema is applied once per path per
    process rather than per request, since this runs on every request."""
    if path not in _initialised:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    if path not in _initialised:
        # WAL so the loop's reads never wait on the service's writes.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _initialised.add(path)
    return conn


@contextmanager
def opened(path: str):
    """A connection that commits on success and is closed either way -
    sqlite3's own context manager only does the first."""
    conn = connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _salt(conn: sqlite3.Connection, day: str) -> str:
    conn.execute("INSERT OR IGNORE INTO salts (day, salt) VALUES (?, ?)",
                 (day, secrets.token_hex(16)))
    return conn.execute("SELECT salt FROM salts WHERE day = ?", (day,)).fetchone()[0]


def record_request(path: str, method: str, route: str, status: int, via: str,
                   address: str, user_agent: str) -> None:
    day = now_utc().date().isoformat()
    with opened(path) as conn:
        conn.execute(
            "INSERT INTO hits (day, method, route, status, via, count) "
            "VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT DO UPDATE SET count = count + 1",
            (day, method, route, status, via),
        )
        # A browser on any route is a person on the page. Anything else only
        # counts on the API: a non-browser fetching "/" is a link checker.
        counts = via == "browser" or route.startswith("/fhir")
        if counts and address and not BOT_UA.search(user_agent):
            visitor = hashlib.sha256(
                ("%s|%s|%s" % (_salt(conn, day), address, user_agent)).encode()
            ).hexdigest()[:16]
            conn.execute(
                "INSERT OR IGNORE INTO visitors (day, via, visitor) VALUES (?, ?, ?)",
                (day, via, visitor),
            )


def record_event(path: str, kind: str, team: str, **detail) -> None:
    with opened(path) as conn:
        conn.execute(
            "INSERT INTO events (at, kind, team, detail) VALUES (?, ?, ?, ?)",
            (now_utc().isoformat(), kind, team, json.dumps(detail) if detail else None),
        )


def _clip(message: str) -> str:
    """The end of an over-long message, not the start: a traceback through
    Starlette's middleware stack is framework frames first, and the line that
    raised and the exception itself come last."""
    if len(message) <= MAX_MESSAGE_CHARS:
        return message
    return "...\n" + message[-MAX_MESSAGE_CHARS:]


def record_error(path: str, source: str, message: str, method: str = None,
                 route: str = None, status: int = None) -> None:
    now = now_utc()
    with opened(path) as conn:
        today = conn.execute(
            "SELECT COUNT(*) FROM errors WHERE source = ? AND at >= ?",
            (source, now.date().isoformat()),
        ).fetchone()[0]
        if today < MAX_ERRORS_PER_DAY:
            conn.execute(
                "INSERT INTO errors (at, source, method, route, status, message) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now.isoformat(), source, method, route, status, _clip(message or "")),
            )


# ------------------------------------------------------------------- mail


def send_mail(subject: str, body: str) -> bool:
    """True once the message is on its way (or printed, without SMTP_HOST)."""
    subject = "[%s] %s" % (SITE.split(".")[0], subject)
    if not SMTP_HOST or not MAIL_TO:
        log("SMTP_HOST or MAIL_TO unset - would have mailed:\n%s\n\n%s" % (subject, body))
        return True
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM or SMTP_USER or MAIL_TO
    msg["To"] = MAIL_TO
    msg.set_content(body)
    try:
        context = ssl.create_default_context()
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30, context=context)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            server.starttls(context=context)
        with server:
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        # Not marked as sent, so the next pass tries again.
        log("could not send %r: %s" % (subject, exc))
        return False
    log("mailed %r" % subject)
    return True


# --------------------------------------------------------------- state rows


def get_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO state (key, value) VALUES (?, ?) "
                 "ON CONFLICT DO UPDATE SET value = excluded.value", (key, value))


# ------------------------------------------------------------------ alerts


def alert_errors(path: str, now: datetime = None) -> bool:
    """Mail the errors nobody has been told about yet, unless the last such
    mail went out less than ALERT_EVERY ago. True if one was sent."""
    now = now or now_utc()
    with opened(path) as conn:
        last = get_state(conn, "last_alert")
        if last and (now - datetime.fromisoformat(last)).total_seconds() < ALERT_EVERY:
            return False
        rows = conn.execute(
            "SELECT * FROM errors WHERE alerted = 0 ORDER BY id"
        ).fetchall()
        if not rows:
            return False
        groups: dict[tuple, int] = {}
        for r in rows:
            key = (r["source"], r["status"], r["method"], r["route"])
            groups[key] = groups.get(key, 0) + 1
        lines = ["%d new error%s on https://%s/" % (len(rows), "" if len(rows) == 1 else "s", SITE), ""]
        for (source, status, method, route), n in sorted(groups.items(), key=lambda g: -g[1]):
            where = " ".join(str(p) for p in (status, method, route) if p)
            lines.append("  %4d  %s  %s" % (n, source, where or "(page)"))
        lines.append("")
        for r in rows[:ALERT_DETAIL]:
            lines.append("--- %s  %s %s" % (r["at"][:19].replace("T", " "), r["source"],
                                            " ".join(str(p) for p in (r["status"], r["method"], r["route"]) if p)))
            lines.append(r["message"] or "(no detail)")
            lines.append("")
        if len(rows) > ALERT_DETAIL:
            lines.append("... and %d more, in usage.db's errors table." % (len(rows) - ALERT_DETAIL))
        first = rows[0]
        subject = "%d error%s: %s" % (
            len(rows), "" if len(rows) == 1 else "s",
            " ".join(str(p) for p in (first["status"], first["route"]) if p) or first["source"],
        )
        if not send_mail(subject, "\n".join(lines)):
            return False
        conn.execute("UPDATE errors SET alerted = 1 WHERE id <= ?", (rows[-1]["id"],))
        set_state(conn, "last_alert", now.isoformat())
    return True


def probe_status(url: str = None) -> "str | None":
    """None when the service answers "up", otherwise what was wrong."""
    req = urllib.request.Request(url or STATUS_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        return "$status answered HTTP %d" % exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return "$status unreachable: %s" % getattr(exc, "reason", exc)
    params = {p.get("name"): p for p in payload.get("parameter", [])}
    state = params.get("matchbox", {}).get("valueString")
    if state == "up":
        return None
    missing = [p.get("valueString") for p in payload.get("parameter", [])
               if p.get("name") == "missingIg"]
    return "validator is %s%s" % (state, " (missing %s)" % ", ".join(missing) if missing else "")


class Health:
    """Consecutive failed probes, and whether the outage has been mailed - so
    one outage is one "down" mail and one "recovered" mail."""

    def __init__(self) -> None:
        self.failures = 0
        self.down_since: "datetime | None" = None
        self.alerted = False

    def observe(self, problem: "str | None", now: datetime = None) -> None:
        now = now or now_utc()
        if problem is None:
            if self.alerted:
                minutes = (now - self.down_since).total_seconds() / 60
                send_mail("recovered after %.0f min" % minutes,
                          "https://%s/fhir/$status answers \"up\" again." % SITE)
            self.failures, self.down_since, self.alerted = 0, None, False
            return
        self.failures += 1
        self.down_since = self.down_since or now
        if self.failures >= HEALTH_FAILURES and not self.alerted:
            # Only marked once the mail is out, so a failed send retries on
            # the next probe instead of the outage going unreported.
            self.alerted = send_mail(
                "service not healthy: %s" % problem,
                "%s\n\nFailing since %s UTC (%d checks in a row).\n\n"
                "  docker compose -f docker-compose.prod.yml ps\n"
                "  docker compose -f docker-compose.prod.yml logs --tail 100 app matchbox\n"
                % (problem, self.down_since.strftime("%Y-%m-%d %H:%M"), self.failures),
            )


# ------------------------------------------------------------------ digest


def _day_totals(conn: sqlite3.Connection, day: str) -> dict:
    one = lambda sql, *args: conn.execute(sql, (day, *args)).fetchone()[0] or 0
    subs = [json.loads(r["detail"] or "{}") | {"team": r["team"]} for r in conn.execute(
        "SELECT team, detail FROM events WHERE kind = 'submission' AND substr(at, 1, 10) = ?",
        (day,))]
    return {
        "browsers": one("SELECT COUNT(*) FROM visitors WHERE day = ? AND via = 'browser'"),
        "clients": one("SELECT COUNT(*) FROM visitors WHERE day = ? AND via = 'client'"),
        "page_views": one("SELECT SUM(count) FROM hits WHERE day = ? AND route = '/' AND method = 'GET'"),
        "api_calls": one("SELECT SUM(count) FROM hits WHERE day = ? AND via = 'client' "
                         "AND route LIKE '/fhir%'"),
        "registrations": [r[0] for r in conn.execute(
            "SELECT team FROM events WHERE kind = 'register' AND substr(at, 1, 10) = ? ORDER BY id",
            (day,))],
        "submissions": subs,
        "server_errors": one("SELECT SUM(count) FROM hits WHERE day = ? AND status >= 500"),
        "browser_errors": one("SELECT COUNT(*) FROM errors WHERE source = 'browser' AND substr(at, 1, 10) = ?"),
    }


def _event_name() -> str:
    """The event's name for the digest's first line. A broken event.toml is
    the service's to report - it will not start - so the digest falls back
    rather than going unsent."""
    try:
        import event
        return event.load()["event"]["name"]
    except Exception:
        return "Provbana"


def digest(path: str, day: date) -> tuple[str, str]:
    """(subject, body) for one UTC day."""
    d = day.isoformat()
    with opened(path) as conn:
        t = _day_totals(conn, d)
        subs = t["submissions"]
        passed = sum(1 for s in subs if s.get("passed"))
        teams = sorted({s["team"] for s in subs})
        lines = ["%s - %s (UTC)" % (_event_name(), day.strftime("%A %d %B"))]
        ends = os.environ.get("PRACTICE_ENDS", "").strip()
        if ends:
            try:
                left = (date.fromisoformat(ends[:10]) - day).days - 1
                if left > 0:
                    lines.append("%d day%s to the event." % (left, "" if left == 1 else "s"))
                elif left == 0:
                    lines.append("The event starts today.")
            except ValueError:
                pass
        lines += [
            "",
            "People",
            "  %d in a browser, %d page loads" % (t["browsers"], t["page_views"]),
            "  %d integration client%s, %d API calls" % (
                t["clients"], "" if t["clients"] == 1 else "s", t["api_calls"]),
            "",
            "Companies registered: %d%s" % (
                len(t["registrations"]),
                " - " + ", ".join(t["registrations"]) if t["registrations"] else ""),
            "Submissions: %d from %d compan%s, %d passed" % (
                len(subs), len(teams), "y" if len(teams) == 1 else "ies", passed),
        ]
        by_doc: dict[str, list] = {}
        for s in subs:
            by_doc.setdefault(s.get("doc", "?"), []).append(s)
        for doc, items in sorted(by_doc.items()):
            lines.append("  %-12s %d (%d passed)" % (doc, len(items), sum(1 for s in items if s.get("passed"))))
        if teams:
            lines.append("  by " + ", ".join(teams))

        lines += ["", "Problems",
                  "  server errors (5xx): %d" % t["server_errors"],
                  "  page errors: %d" % t["browser_errors"]]
        client_errors = conn.execute(
            "SELECT status, method, route, SUM(count) AS n FROM hits "
            "WHERE day = ? AND status >= 400 AND status < 500 AND via = 'client' "
            "AND route LIKE '/fhir%' GROUP BY status, method, route ORDER BY n DESC LIMIT 8",
            (d,)).fetchall()
        if client_errors:
            lines.append("  client errors from integrations:")
            lines += ["    %4d  %d %s %s" % (r["n"], r["status"], r["method"], r["route"])
                      for r in client_errors]

        routes = conn.execute(
            "SELECT method, route, SUM(count) AS n FROM hits WHERE day = ? AND via = 'client' "
            "AND route LIKE '/fhir%' GROUP BY method, route ORDER BY n DESC LIMIT 8",
            (d,)).fetchall()
        if routes:
            lines += ["", "Busiest API routes (integrations)"]
            lines += ["  %5d  %s %s" % (r["n"], r["method"], r["route"]) for r in routes]

        lines += ["", "Last 7 days",
                  "  %-10s %8s %8s %6s %6s %6s %5s" % ("day", "browsers", "clients", "regs", "subs", "passed", "5xx")]
        for back in range(6, -1, -1):
            dd = (day - timedelta(days=back)).isoformat()
            x = _day_totals(conn, dd)
            lines.append("  %-10s %8d %8d %6d %6d %6d %5d" % (
                dd, x["browsers"], x["clients"], len(x["registrations"]), len(x["submissions"]),
                sum(1 for s in x["submissions"] if s.get("passed")), x["server_errors"]))
    subject = "%s: %d %s, %d submission%s%s" % (
        d, t["browsers"] + t["clients"],
        "visitor" if t["browsers"] + t["clients"] == 1 else "visitors",
        len(subs), "" if len(subs) == 1 else "s",
        ", %d errors" % (t["server_errors"] + t["browser_errors"])
        if t["server_errors"] + t["browser_errors"] else "")
    return subject, "\n".join(lines) + "\n"


def maybe_send_digest(path: str, now: datetime = None) -> bool:
    """Yesterday's digest, once, at or after DIGEST_HOUR. A container that was
    down at that hour sends it when it comes back, the same day; a day it
    missed entirely is not backfilled."""
    now = now or now_utc()
    if now.hour < DIGEST_HOUR:
        return False
    yesterday = now.date() - timedelta(days=1)
    with opened(path) as conn:
        if get_state(conn, "digest_sent_for") >= yesterday.isoformat():
            return False
    if not send_mail(*digest(path, yesterday)):
        return False
    with opened(path) as conn:
        set_state(conn, "digest_sent_for", yesterday.isoformat())
    return True


def prune(path: str, now: datetime = None) -> None:
    """Old rows past RETAIN_DAYS, and every salt but today's: once its day is
    over nothing hashes under it again, and without it the stored hashes are
    no longer a way back to an address."""
    now = now or now_utc()
    today = now.date().isoformat()
    cutoff = (now.date() - timedelta(days=RETAIN_DAYS)).isoformat()
    with opened(path) as conn:
        conn.execute("DELETE FROM salts WHERE day < ?", (today,))
        conn.execute("DELETE FROM hits WHERE day < ?", (cutoff,))
        conn.execute("DELETE FROM visitors WHERE day < ?", (cutoff,))
        conn.execute("DELETE FROM events WHERE at < ?", (cutoff,))
        conn.execute("DELETE FROM errors WHERE at < ?", (cutoff,))


def usage_db() -> str:
    return os.environ.get("USAGE_DB") or os.path.join(
        os.environ.get("DATA_DIR") or os.path.join(HERE, "data"), "usage.db")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--loop", action="store_true", help="alert and digest, forever")
    ap.add_argument("--digest", action="store_true", help="print a day's digest and exit")
    ap.add_argument("--day", help="with --digest: YYYY-MM-DD (default yesterday, UTC)")
    ap.add_argument("--test-email", action="store_true", help="send one test message")
    args = ap.parse_args()
    path = usage_db()

    if args.digest:
        day = date.fromisoformat(args.day) if args.day else now_utc().date() - timedelta(days=1)
        subject, body = digest(path, day)
        print(subject, body, sep="\n\n", end="")
        return 0
    if args.test_email:
        return 0 if send_mail("test", "Mail from the monitor reaches you.") else 1
    if not args.loop:
        ap.error("choose --loop, --digest or --test-email")

    if not (SMTP_HOST and MAIL_TO):
        log("SMTP_HOST or MAIL_TO unset - mail goes to this log instead")
    log("every %ds; digest at %02d:00 UTC; %s" % (CHECK_INTERVAL, DIGEST_HOUR, path))
    health = Health()
    while True:
        # Each step on its own: a broken SMTP login must not stop the health
        # probe, and a locked database must not stop the loop.
        for step in (lambda: health.observe(probe_status()),
                     lambda: alert_errors(path),
                     lambda: maybe_send_digest(path),
                     lambda: prune(path)):
            try:
                step()
            except Exception as exc:
                log("pass failed: %r" % exc)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())

"""Nordic Hackathon 2026 - gamified FHIR European Patient Summary validation service.

Teams register, submit EPS bundles, Matchbox validates, leaderboard keeps score.
Other EHDS document categories - laboratory report, prescription and dispense,
imaging report - are accepted and validated against their own HL7 Europe guide,
but the patient summary is what the track is about and what the leaderboard
ranks; the rest are worth a small bonus. See EHDS_DOCUMENTS.
Stdlib sqlite3 + FastAPI, no other runtime deps.
"""

import base64
import html as html_mod
import http.client
import json
import os
import re
import secrets
import sqlite3
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import anyio.to_thread
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
# The router raises Starlette's HTTPException, not FastAPI's subclass, so an
# unrouted path and a wrong method arrive as the base class. Handling that one
# covers both, since everything raised here is a subclass of it.
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, Response

# The usage log and the organiser's mail - see its docstring. Stdlib only; the
# recording half is all this process uses, the mailing runs in its own
# container.
import monitor

MATCHBOX_URL = os.environ.get(
    "MATCHBOX_URL", "http://matchbox:8080/matchboxv3/fhir"
).rstrip("/")
# ------------------------------------------------------- EHDS document types
#
# The track is the European Patient Summary: it is what the scenarios are
# written for, what the samples are, and what the leaderboard ranks. But EHDS
# (Regulation (EU) 2025/327, Article 14) names six priority categories of health
# data that EHR systems have to exchange, and a team that brings a lab report to
# the room should get it checked properly rather than scored against the wrong
# profile. So three others are accepted, detected, and validated against their
# own guide - second class by design: they earn SECONDARY_CATEGORY_POINTS apiece
# and never the richness points or the first-pass bonus. See team_score().
#
# Four entries cover five of the six: one of them, MPD, answers for both
# ePrescription and eDispensation. The sixth is the hospital discharge report,
# and it is left out deliberately.
#
# Not simply because hl7.fhir.eu.hdr fails its own published examples - so does
# the imaging guide, which is kept. The difference is that a team cannot build
# around this one. The guide binds Encounter.status to a value set enumerating
# the R4 codes, which the R5 CodeSystem its own dependencies pull in shadows,
# and R5 renamed `finished` to `completed` - so the status a conformant
# discharge report carries takes a hard error whatever the team does. The guide
# has sat at 0.1.0-ballot without movement, and the fix has to come from it or
# from hl7.fhir.uv.xver-r5.r4, not from here.
# matchbox/config/application.yaml records the mechanism in full. Restore the
# entry, and its package there, once one of them moves.
#
# Each entry carries what it takes to check one category:
#
#   profile   the canonical handed to Matchbox's $validate?profile=. Matchbox
#             refuses the call without one, so even MPD - which defines no
#             bundle profile of its own - names a profile here.
#   igs       the packages the validation engine is scoped to. Matchbox builds
#             an engine from the requested profile's dependency closure, so
#             anything outside it comes back "not checked" unless named.
#   bundles   canonicals that identify this category when a submission claims
#             one in Bundle.meta.profile. The explicit claim wins over
#             everything else in detect_document().
#   loinc     Composition.type codes that identify it when nothing is claimed.
#
# Adding a category is adding an entry here plus its package in
# matchbox/config/application.yaml. Nothing else in the service knows the list.
EPS_PROFILE = os.environ.get(
    "PROFILE", "http://hl7.eu/fhir/eps/StructureDefinition/bundle-eu-eps"
)
# The documented override for the profile the event scores against, and the one
# a box may already have set, so it keeps its old name as well as its new one.
PROFILE = EPS_PROFILE

EHDS_DOCUMENTS = [
    {
        "id": "eps",
        "label": "Patient summary",
        "long_label": "European Patient Summary",
        "short": "PS",
        "category": "Patient summary",
        "primary": True,
        "profile": EPS_PROFILE,
        # hl7se.fhir.base rides along on this one only: it is a Swedish event
        # and teams claim the Swedish base profiles on Patient, but they have
        # nothing to say about a lab report or an imaging study.
        "igs": ["hl7.fhir.eu.eps#1.0.0-ballot", "hl7se.fhir.base#1.1.0"],
        "bundles": ["http://hl7.eu/fhir/eps/StructureDefinition/bundle-eu-eps"],
        "loinc": ["60591-5"],
        "maturity": "ballot",
        "spec": "https://hl7.eu/fhir/eps/1.0.0-ballot/",
    },
    {
        "id": "laboratory",
        "label": "Laboratory report",
        "long_label": "Laboratory Report",
        "short": "LAB",
        "category": "Medical test result",
        "primary": False,
        "profile": "http://hl7.eu/fhir/laboratory/StructureDefinition/Bundle-eu-lab",
        # hl7.fhir.eu.base#2.0.0-ballot is not a typo and not belt-and-braces.
        # DiagnosticReport-eu-lab in Laboratory Report 2.0.0 references
        # http://hl7.eu/fhir/base/StructureDefinition/patient-animal-eu-core,
        # which existed in eu.base 2.0.0-ballot and was dropped from the 2.0.0
        # release the IG then pinned. Scoped to its declared closure alone,
        # every lab bundle - including the IG's own published examples - fails
        # with "Unable to resolve the profile reference ...patient-animal-eu-core"
        # and scores 0. Naming the ballot alongside puts the missing definition
        # back in scope; measured on Bundle-SimpleChemistryResultReport: one
        # hard error without it, none with it. Remove when eu.base restores the
        # profile or the lab IG stops pointing at it.
        "igs": ["hl7.fhir.eu.laboratory#2.0.0", "hl7.fhir.eu.base#2.0.0-ballot"],
        "bundles": ["http://hl7.eu/fhir/laboratory/StructureDefinition/Bundle-eu-lab"],
        # The IG binds Composition.type to a value set of exactly one code.
        "loinc": ["11502-2"],
        "maturity": "STU 2.0",
        "spec": "https://hl7.eu/fhir/laboratory/",
    },
    {
        "id": "mpd",
        "label": "Prescription and dispense",
        "long_label": "Medication Prescription and Dispense",
        "short": "eP/eD",
        "category": "ePrescription / eDispensation",
        "primary": False,
        # MPD profiles MedicationRequest, MedicationDispense, Medication and
        # Dosage, and defines no Bundle or Composition profile at all - a
        # prescription travels as a plain collection, not as a document. So the
        # bundle is checked against base Bundle, and the MPD conformance comes
        # from the entries' own meta.profile claims, which Matchbox descends
        # into and checks. Verified by breaking a MedicationRequest inside an
        # otherwise valid bundle: the failure comes back attributed "(from
        # http://hl7.eu/fhir/mpd/StructureDefinition/MedicationRequest-eu-mpd)",
        # so the profile really is being applied and not merely loaded.
        "profile": "http://hl7.org/fhir/StructureDefinition/Bundle",
        "igs": ["hl7.fhir.eu.mpd#1.0.0"],
        "bundles": [],
        "loinc": [],
        # What an entry has to claim for a bundle to be read as MPD.
        "entry_profiles": [
            "http://hl7.eu/fhir/mpd/StructureDefinition/MedicationRequest-eu-mpd",
            "http://hl7.eu/fhir/mpd/StructureDefinition/MedicationDispense-eu-mpd",
            "http://hl7.eu/fhir/mpd/StructureDefinition/Medication-eu-mpd",
        ],
        "maturity": "STU 1.0",
        "spec": "https://hl7.eu/fhir/mpd/",
    },
    {
        "id": "imaging",
        "label": "Imaging report",
        "long_label": "Imaging Report",
        "short": "IMG",
        "category": "Medical imaging study and report",
        "primary": False,
        "profile": "http://hl7.eu/fhir/imaging/StructureDefinition/BundleReportEuImaging",
        "igs": ["hl7.fhir.eu.imaging#1.0.0-ballot"],
        "bundles": [
            "http://hl7.eu/fhir/imaging/StructureDefinition/BundleReportEuImaging",
            "http://hl7.eu/fhir/imaging/StructureDefinition/"
            "BundleReportMinimalMetadataEuImaging",
        ],
        # Deliberately empty. Imaging binds Composition.type to "any LOINC
        # descendant of 18726-0", which cannot be decided without a terminology
        # server - the IG's own examples use 24802-1 (MR Knee) and even a CPT
        # code. So an imaging report is recognised by its profile claim, or by
        # carrying an ImagingStudy (see detect_document), never by its type code.
        "loinc": [],
        "maturity": "ballot",
        "spec": "https://hl7.eu/fhir/imaging/",
    },
]

DOCUMENTS_BY_ID = {doc["id"]: doc for doc in EHDS_DOCUMENTS}
# The track. Also the fallback for a bundle that announces nothing, so an
# unrecognised submission is scored exactly as it was before any of the other
# categories existed.
PRIMARY_DOCUMENT = DOCUMENTS_BY_ID["eps"]
SECONDARY_DOCUMENTS = [doc for doc in EHDS_DOCUMENTS if not doc["primary"]]

# Packages $status insists Matchbox actually loaded. Every document's list,
# de-duplicated in registry order: a package missing here is a category whose
# profile claims come back "not checked", which scores real submissions wrong,
# and deploy.sh gates the deploy on this being empty. EPS is first because it
# is the one every fallback submission is scored against. Overridable as one
# comma-separated list, which is also how a box can be run with a narrower set
# of IGs than the registry describes.
DEFAULT_VALIDATION_IGS = list(
    dict.fromkeys(ig for doc in EHDS_DOCUMENTS for ig in doc["igs"])
)
VALIDATION_IGS = [
    ig.strip()
    for ig in os.environ.get(
        "VALIDATION_IGS", ",".join(DEFAULT_VALIDATION_IGS)
    ).split(",")
    if ig.strip()
]

DATA_DIR = os.environ.get("DATA_DIR", "/data")
SAMPLES_DIR = os.environ.get(
    "SAMPLES_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
)
PORT = int(os.environ.get("PORT", "8095"))
# Who used the service, and what broke - read by monitor.py's loop. Beside
# DATA_DIR unless told otherwise; the event box points it outside, so a backup
# restore, which swaps DATA_DIR wholesale, leaves the history alone.
USAGE_DB = os.environ.get("USAGE_DB", "")

# The submit endpoint holds the client connection for the whole validation, so
# this is what a participant waits on a wedged Matchbox. A cold validator has
# to load its packages on the first request, hence not tighter than a minute.
MATCHBOX_TIMEOUT = float(os.environ.get("MATCHBOX_TIMEOUT", "60"))

# Reject oversized submissions before they hit the audit log or Matchbox. The
# richest real EPS bundles are a few hundred KB, so 5 MB is generous headroom
# while still bounding disk use from a runaway or hostile client.
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(5 * 1024 * 1024)))

# Every other endpoint takes a handful of fields, so it gets a much tighter
# ceiling than a bundle does.
MAX_JSON_BODY_BYTES = 64 * 1024

# An interop screenshot. The UI downscales before it uploads, so this is the
# backstop for a client that does not - not the expected size.
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(2 * 1024 * 1024)))
MAX_NOTE_CHARS = 2000

VALID_BASE = 50        # points for a submission with zero error/fatal issues
SECTION_POINTS = 10    # per populated top-level Composition section
RESOURCE_POINTS = 2    # per clinical resource (not Composition/Patient)
RESOURCE_CAP = 60      # max points from clinical resources
SECTION_CAP = 160      # max points from sections (the IPS set is ~16 sections)
FIRST_PASS_BONUS = 20  # one-time, to the first company to pass a patient summary
# Per non-EPS EHDS category a company has passed, once each. Deliberately small
# and flat: EHDS obliges EHR systems to exchange six categories rather than one,
# so breadth is worth acknowledging, but this is the patient summary track and a
# stack of lab reports must not out-rank a well-built summary. A company that
# submits only patient summaries scores precisely what it scored before any of
# the other categories existed.
SECONDARY_CATEGORY_POINTS = 15

# Set REGISTER_CODE to require a shared secret on $register. Left empty the
# endpoint stays open, which is what you want on a closed event network.
REGISTER_CODE = os.environ.get("REGISTER_CODE", "")

# While the service is up for practice ahead of the event, reset.py empties the
# leaderboard daily and this is when that stops - see its docstring, which owns
# the semantics. The page reads it to say so, and stops saying so by itself at
# the instant of the final wipe. Held as a string: the value is handed to the
# browser, which does the comparing, and parsing it here would only mean
# re-serialising it. Unset means no practice period and no notice.
def _practice_ends() -> str:
    """PRACTICE_ENDS with an explicit UTC offset, or "" when unset or unusable.

    reset.py reads a naive timestamp as UTC on purpose; the browser's
    Date.parse reads the same string as *local* time. Left as typed, a value
    without a "Z" made the page drop the practice notice an hour before the
    final wipe in CET - the board presenting itself as the event's real
    leaderboard, and then being emptied. Normalising here keeps the one
    parser that matters agreeing with the one that does the deleting.
    """
    raw = os.environ.get("PRACTICE_ENDS", "").strip()
    if not raw:
        return ""
    try:
        import reset
        return reset.practice_ends(raw).isoformat()
    except Exception as exc:
        # Not fatal: the reset container fails loudly on the same value, and
        # taking the validator down over a cosmetic notice is the worse trade.
        print("PRACTICE_ENDS is not a usable timestamp (%s) - the practice "
              "notice will not be shown" % exc, flush=True)
        return ""


PRACTICE_ENDS = _practice_ends()


def usage_db() -> str:
    return USAGE_DB or os.path.join(DATA_DIR, "usage.db")


def record_usage(fn, *args, **kwargs) -> None:
    """Call one of monitor's record_* functions, and never fail because of it.

    The usage log is for the organiser. A full disk or a locked usage.db must
    cost the organiser a row, not a participant their submission.
    """
    try:
        fn(usage_db(), *args, **kwargs)
    except Exception as exc:
        print("usage log: %s failed (%r)" % (fn.__name__, exc), flush=True)


class MatchboxError(Exception):
    """Matchbox unreachable, timed out, or returned garbage."""


# ---------------------------------------------------------------- database


def db_path() -> str:
    return os.path.join(DATA_DIR, "connectathon.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    os.makedirs(os.path.join(DATA_DIR, "submissions"), exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS teams (
                id          INTEGER PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
                slug        TEXT NOT NULL UNIQUE,
                token       TEXT NOT NULL UNIQUE,
                first_pass  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS renderings (
                id            INTEGER PRIMARY KEY,
                consumer_id   INTEGER NOT NULL REFERENCES teams(id),
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                ts            TEXT NOT NULL,
                note          TEXT NOT NULL DEFAULT '',
                filename      TEXT NOT NULL,
                mime          TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions (
                id        INTEGER PRIMARY KEY,
                team_id   INTEGER NOT NULL REFERENCES teams(id),
                ts        TEXT NOT NULL,
                pass      INTEGER NOT NULL,
                errors    INTEGER NOT NULL,
                warnings  INTEGER NOT NULL,
                infos     INTEGER NOT NULL,
                score     INTEGER NOT NULL,
                sections  INTEGER NOT NULL DEFAULT 0,
                resources INTEGER NOT NULL DEFAULT 0,
                doc_type  TEXT NOT NULL DEFAULT 'eps',
                filename  TEXT NOT NULL
            );
            -- Where a company takes documents in directly. kind is the
            -- connectionType code; see ENDPOINT_KINDS. AUTOINCREMENT: SQLite
            -- otherwise hands the id of a deleted top row to the next insert,
            -- and Endpoint/3 is a URL people paste into their own systems, so
            -- must not come to mean another company's address.
            CREATE TABLE IF NOT EXISTS endpoints (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id     INTEGER NOT NULL REFERENCES teams(id),
                updated_at  TEXT NOT NULL,
                kind        TEXT NOT NULL,
                name        TEXT NOT NULL DEFAULT '',
                address     TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT ''
            );
            """
        )
        # Migrate pre-richness databases in place.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(submissions)")}
        for col in ("sections", "resources"):
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE submissions ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
                )
        # Every submission that predates multi-category validation was scored
        # against the patient summary, so that is what the default backfills.
        if "doc_type" not in cols:
            conn.execute(
                "ALTER TABLE submissions ADD COLUMN doc_type TEXT NOT NULL "
                "DEFAULT 'eps'"
            )
        # The bonus used to be called "first blood", which is not a phrase for a
        # clinical event. CREATE TABLE IF NOT EXISTS leaves an existing table
        # alone, so databases from before the rename need the column moved.
        team_cols = {row["name"] for row in conn.execute("PRAGMA table_info(teams)")}
        if "first_blood" in team_cols and "first_pass" not in team_cols:
            conn.execute("ALTER TABLE teams RENAME COLUMN first_blood TO first_pass")


# Letters NFKD cannot decompose into an ASCII base, so "Åland Hälsa" does not
# come out as "land-h-lsa". Nordic first, then the rest of Latin-1.
SLUG_TRANSLITERATIONS = str.maketrans(
    {"æ": "ae", "ø": "o", "œ": "oe", "ß": "ss", "ð": "d", "þ": "th", "ł": "l",
     "đ": "d", "ı": "i"}
)


def slugify(name: str) -> str:
    """URL-safe [a-z0-9-] identity for a team name; "team" if nothing survives."""
    folded = unicodedata.normalize("NFKD", name.lower()).translate(
        SLUG_TRANSLITERATIONS
    )
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    return slug or "team"


def fold_name(name: str) -> str:
    """A name as search compares it: FHIR string search ignores case and
    accents, so "aland" has to find "Åland Hälsa".

    Only the accents go. Unlike a slug this keeps letters ASCII has no answer
    for, or a search for a name in another script would fold to nothing and
    match every company there is.
    """
    folded = unicodedata.normalize("NFKD", name.casefold()).translate(
        SLUG_TRANSLITERATIONS
    )
    return " ".join(
        "".join(c for c in folded if not unicodedata.combining(c)).split()
    )


def search_terms(values: list[str] | None, fold: bool = True) -> list[list[str]]:
    """One search parameter as FHIR reads it: repeats are ANDed, the commas
    inside one are ORed, and `\\,` is a comma that is part of the name.

    A blank alternative is dropped and so is a repeat left with none, which
    makes `name=` the same as not asking.
    """
    terms = []
    for value in values or []:
        ors = [o.replace("\\,", ",") for o in re.split(r"(?<!\\),", value)]
        ors = [fold_name(o) if fold else o for o in ors]
        ors = [o for o in ors if o.strip()]
        if ors:
            terms.append(ors)
    return terms


def name_matches(name: str, starts: list[str] | None = None,
                 contains: list[str] | None = None,
                 exact: list[str] | None = None) -> bool:
    """FHIR's `name`, `name:contains` and `name:exact`, all of which must hold.

    Done here rather than in SQL because SQLite's LIKE only folds ASCII, and
    there are never more companies than fit in a room.
    """
    folded = fold_name(name)
    return (
        all(any(folded.startswith(o) for o in ors) for ors in search_terms(starts))
        and all(any(o in folded for o in ors) for ors in search_terms(contains))
        and all(name in ors for ors in search_terms(exact, fold=False))
    )


def unique_slug(conn: sqlite3.Connection, name: str) -> str:
    base = slugify(name)
    slug, n = base, 1
    while conn.execute("SELECT 1 FROM teams WHERE slug = ?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    return slug


# ---------------------------------------------------------------- scoring


def section_key(section: dict) -> str:
    """Identity used to de-duplicate sections: code, else title, else one bucket.

    Repeating a section does not make a document richer, so scoring counts
    distinct sections. Untitled, uncoded sections all collapse together
    because nothing distinguishes them.
    """
    code = section.get("code")
    if isinstance(code, dict) and isinstance(code.get("coding"), list):
        for coding in code["coding"]:
            if isinstance(coding, dict) and isinstance(coding.get("code"), str):
                system = coding.get("system")
                return "c:%s|%s" % (system if isinstance(system, str) else "",
                                    coding["code"])
    title = section.get("title")
    if isinstance(title, str) and title.strip():
        return "t:" + " ".join(title.lower().split())
    return "u:"


def _clinical_targets(entries: list) -> set[str]:
    """Every way a section entry may point at a clinical resource in the bundle.

    fullUrl, "Type/id" and "urn:uuid:<id>" all resolve per the FHIR bundle
    rules; Composition and Patient are excluded because pointing a section at
    them is not clinical content.
    """
    targets: set[str] = set()
    for entry in entries:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict):
            continue
        rtype = resource.get("resourceType")
        if not isinstance(rtype, str) or rtype in ("Composition", "Patient"):
            continue
        full_url = entry.get("fullUrl")
        if isinstance(full_url, str) and full_url:
            targets.add(full_url)
        rid = resource.get("id")
        if isinstance(rid, str) and rid:
            targets.add(f"{rtype}/{rid}")
            targets.add(f"urn:uuid:{rid}")
    return targets


def _section_populated(section: dict, targets: set[str]) -> bool:
    entries = section.get("entry")
    if not isinstance(entries, list):
        return False
    for ref in entries:
        if not isinstance(ref, dict):
            continue
        target = ref.get("reference")
        if isinstance(target, str) and target.split("#", 1)[0] in targets:
            return True
    return False


def count_bundle_richness(bundle) -> tuple[int, int]:
    """Return (populated_sections, clinical_resources) for a Bundle JSON value.

    - populated_sections: distinct top-level section[] items of the (first)
      Composition with at least one entry whose reference resolves to a
      clinical resource in the bundle. Sections without entries (e.g.
      emptyReason-only), or whose entries only point outside the bundle or at
      nothing, do not count.
    - clinical_resources: bundle entries whose resource is neither a
      Composition nor a Patient.
    Defensive against arbitrary JSON: anything malformed counts as 0.
    """
    if not isinstance(bundle, dict) or not isinstance(bundle.get("entry"), list):
        return 0, 0
    targets = _clinical_targets(bundle["entry"])
    section_keys: set[str] = set()
    resources = 0
    composition_seen = False
    for entry in bundle["entry"]:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict):
            continue
        rtype = resource.get("resourceType")
        if rtype == "Composition":
            if not composition_seen:
                composition_seen = True
                section_list = resource.get("section")
                if isinstance(section_list, list):
                    for section in section_list:
                        if isinstance(section, dict) and _section_populated(
                            section, targets
                        ):
                            section_keys.add(section_key(section))
            continue  # a Composition is never clinical payload
        if rtype == "Patient":
            continue
        resources += 1
    return len(section_keys), resources


def submission_score(
    passed: bool, sections: int, resources: int, doc: dict | None = None
) -> int:
    """What one submission is worth. Conformance gates it: any error means 0.

    A valid patient summary earns VALID_BASE, plus SECTION_POINTS per distinct
    populated Composition section capped at SECTION_CAP points, plus
    RESOURCE_POINTS per clinical resource capped at RESOURCE_CAP points.
    Both halves are capped so no amount of padding outruns a real document.

    Any other EHDS category earns the flat SECONDARY_CATEGORY_POINTS instead -
    richness is measured against what a patient summary is meant to carry, and
    counting sections on a lab report would only reward the wrong shape. That
    flat figure is what the category is worth *the first time* it passes;
    team_score() is what decides whether a given submission adds it.

    `doc` is an EHDS_DOCUMENTS entry, defaulting to the patient summary so an
    existing caller keeps its old behaviour.
    """
    if not passed:
        return 0
    if not (doc or PRIMARY_DOCUMENT)["primary"]:
        return SECONDARY_CATEGORY_POINTS
    return (
        VALID_BASE
        + min(SECTION_CAP, SECTION_POINTS * sections)
        + min(RESOURCE_CAP, RESOURCE_POINTS * resources)
    )


def team_score(best_primary: int, first_pass: bool, secondary: int) -> int:
    """A company's leaderboard score.

    `best_primary` is its best patient summary, which is the whole contest;
    `secondary` is how many *other* EHDS categories it has a passing submission
    in, each worth a flat bonus. A company with no patient summary therefore
    sits near the bottom whatever else it has passed, which is the intent - the
    other categories are credit for breadth, not a route around the track.
    """
    return (
        best_primary
        + (FIRST_PASS_BONUS if first_pass else 0)
        + SECONDARY_CATEGORY_POINTS * secondary
    )


# ---------------------------------------------------------------- clinician summary

MEDICATION_TYPES = ("MedicationStatement", "MedicationRequest", "MedicationDispense",
                    "MedicationAdministration")

# Friendly wording for the standard list-empty-reason codes. Bundles carry the
# reason as a code ("nilknown"), as display text ("Nil Known") or as both, and
# none of those are what a clinician wants to read, so the lookup is keyed on a
# squashed form that all three spellings collapse into.
EMPTY_REASON_LABELS = {
    "nilknown": "None reported",
    "notasked": "Not asked",
    "withheld": "Information withheld",
    "unavailable": "Unavailable",
    "notstarted": "Not started",
    "closed": "Closed",
}


def _squash(value: str) -> str:
    """Lowercase, letters and digits only - "Nil Known" and "nilknown" alike."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _cc_display(cc) -> str | None:
    """Best human label of a CodeableConcept-shaped value, or None."""
    if not isinstance(cc, dict):
        return None
    text = cc.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    codings = cc.get("coding")
    if isinstance(codings, list):
        for key in ("display", "code"):
            for coding in codings:
                if isinstance(coding, dict):
                    val = coding.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
    return None


def _quantity_str(quantity) -> str | None:
    if not isinstance(quantity, dict):
        return None
    value = quantity.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    unit = quantity.get("unit")
    if not isinstance(unit, str):
        unit = quantity.get("code") if isinstance(quantity.get("code"), str) else ""
    return f"{value} {unit}".strip()


def _period_str(period) -> str | None:
    if not isinstance(period, dict):
        return None
    start = period.get("start") if isinstance(period.get("start"), str) else None
    end = period.get("end") if isinstance(period.get("end"), str) else None
    if start and end:
        return f"{start} to {end}"
    return start or end


def _first_str(resource, *keys) -> str | None:
    for key in keys:
        val = resource.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _choice_str(resource, prefix) -> str | None:
    """Render a FHIR choice element like onset[x] / performed[x] / effective[x]."""
    val = _first_str(resource, prefix + "DateTime", prefix + "String", prefix + "Date")
    if val:
        return val
    period = _period_str(resource.get(prefix + "Period"))
    if period:
        return period
    return _quantity_str(resource.get(prefix + "Age")) or _quantity_str(
        resource.get(prefix + "Quantity")
    )


def _observation_value(obs) -> str | None:
    value = (
        _quantity_str(obs.get("valueQuantity"))
        or _cc_display(obs.get("valueCodeableConcept"))
        or _first_str(obs, "valueString")
    )
    if value:
        return value
    if isinstance(obs.get("valueBoolean"), bool):
        return "yes" if obs["valueBoolean"] else "no"
    if isinstance(obs.get("valueInteger"), int) and not isinstance(
        obs.get("valueInteger"), bool
    ):
        return str(obs["valueInteger"])
    components = obs.get("component")
    if isinstance(components, list):
        parts = []
        for comp in components:
            if not isinstance(comp, dict):
                continue
            label = _cc_display(comp.get("code"))
            val = (
                _quantity_str(comp.get("valueQuantity"))
                or _cc_display(comp.get("valueCodeableConcept"))
                or _first_str(comp, "valueString")
            )
            if label and val:
                parts.append(f"{label} {val}")
            elif val:
                parts.append(val)
        if parts:
            return ", ".join(parts)
    return None


def _build_reference_index(entries) -> dict:
    """Map fullUrl, its trailing 'Type/id' segment, and 'resourceType/id' to
    each entry's resource, so section references resolve in every EPS style."""
    by_ref: dict = {}
    for entry in entries:
        resource = entry["resource"]
        keys = []
        full = entry.get("fullUrl")
        if isinstance(full, str) and full:
            keys.append(full)
            m = re.search(r"([A-Za-z]+/[A-Za-z0-9\-\.]{1,64})$", full)
            if m:
                keys.append(m.group(1))
        rtype, rid = resource.get("resourceType"), resource.get("id")
        if isinstance(rtype, str) and isinstance(rid, str):
            keys.append(f"{rtype}/{rid}")
        for key in keys:
            by_ref.setdefault(key, resource)
    return by_ref


def _resolve_reference(ref, by_ref):
    if isinstance(ref, str) and ref:
        return by_ref.get(ref)
    return None


def _contained_resource(resource, local_id):
    contained = resource.get("contained")
    if isinstance(contained, list):
        for res in contained:
            if isinstance(res, dict) and res.get("id") == local_id:
                return res
    return None


def _medication_name(resource, by_ref) -> str:
    name = _cc_display(resource.get("medicationCodeableConcept"))
    if name:
        return name
    med_ref = resource.get("medicationReference")
    if isinstance(med_ref, dict):
        ref = med_ref.get("reference")
        if isinstance(ref, str) and ref.startswith("#"):
            target = _contained_resource(resource, ref[1:])
        else:
            target = _resolve_reference(ref, by_ref)
        if isinstance(target, dict):
            name = _cc_display(target.get("code"))
            if name:
                return name
        display = med_ref.get("display")
        if isinstance(display, str) and display.strip():
            return display.strip()
    return "Medication (unnamed)"


def _dosage_detail(resource) -> str | None:
    dosages = resource.get("dosage")
    if not isinstance(dosages, list):
        dosages = resource.get("dosageInstruction")
    if not isinstance(dosages, list):
        return None
    parts = []
    for dosage in dosages:
        if not isinstance(dosage, dict):
            continue
        text = _first_str(dosage, "text")
        if text:
            parts.append(text)
            continue
        bits = []
        route = _cc_display(dosage.get("route"))
        if route:
            bits.append(route)
        timing = dosage.get("timing")
        if isinstance(timing, dict):
            when = _cc_display(timing.get("code"))
            if when:
                bits.append(when)
        if bits:
            parts.append(", ".join(bits))
    return "; ".join(parts) or None


def _humanize_type(rtype: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", rtype)


FLAG_PRIORITY_EXT = "http://hl7.org/fhir/StructureDefinition/flag-priority"
NOTE_EXT = "http://hl7.org/fhir/StructureDefinition/note"


def _extensions(resource, url) -> list:
    out = []
    exts = resource.get("extension")
    if isinstance(exts, list):
        for ext in exts:
            if isinstance(ext, dict) and ext.get("url") == url:
                out.append(ext)
    return out


def _flag_priority(resource) -> str | None:
    """PH / PM / PL from the core flag-priority extension, or None."""
    for ext in _extensions(resource, FLAG_PRIORITY_EXT):
        code = _concept_coding(ext.get("valueCodeableConcept"))[1]
        if code:
            return code.upper()
    return None


def _flag_note(resource) -> str | None:
    """The EU Core Flag keeps its free text in the core `note` extension,
    since Flag itself has no note element."""
    for ext in _extensions(resource, NOTE_EXT):
        ann = ext.get("valueAnnotation")
        text = ann.get("text") if isinstance(ann, dict) else None
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())
    return None


def _section_narrative(section) -> str | None:
    """The section's own narrative as plain text, for a section that is its
    narrative - a patient story has no entries, and "No entries" would be
    the wrong thing to say about it."""
    text = section.get("text")
    div = text.get("div") if isinstance(text, dict) else None
    if not isinstance(div, str):
        return None
    # Paragraph and row ends become line breaks so the prose keeps its shape;
    # every other tag goes.
    plain = re.sub(r"</(p|tr|div|li|h[1-6])\s*>", "\n", div, flags=re.I)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = html_mod.unescape(plain)
    lines = [" ".join(line.split()) for line in plain.splitlines()]
    plain = "\n".join(line for line in lines if line)
    return plain or None


# Code systems a clinician recognises by name; anything else falls back to the
# system's host so provenance is still visible.
CODE_SYSTEM_LABELS = {
    "http://snomed.info/sct": "SNOMED CT",
    "http://loinc.org": "LOINC",
    "http://www.whocc.no/atc": "ATC",
    "http://hl7.org/fhir/sid/icd-10": "ICD-10",
    "http://hl7.org/fhir/sid/icd-10-cm": "ICD-10-CM",
    "http://hl7.org/fhir/sid/icd-11": "ICD-11",
    "http://www.nlm.nih.gov/research/umls/rxnorm": "RxNorm",
    "http://hl7.org/fhir/sid/ndc": "NDC",
    "http://unitsofmeasure.org": "UCUM",
    "http://hl7.org/fhir/sid/cvx": "CVX",
    "http://ec.europa.eu/tools/mdr": "EU MDR",
}

# Clinical grouping shared by every clinician view.
ITEM_CATEGORIES = {
    # An alert is what the author wants read before anything else - a
    # difficult airway, an immunosuppressant - so it is its own category
    # rather than "other", and the views put it first.
    "Flag": "alert",
    "AllergyIntolerance": "allergy",
    "Condition": "problem",
    "MedicationStatement": "medication",
    "MedicationRequest": "medication",
    "MedicationDispense": "medication",
    "MedicationAdministration": "medication",
    "Observation": "result",
    "Procedure": "procedure",
    "Immunization": "immunization",
    "CarePlan": "plan",
    "Device": "device",
    "DeviceUseStatement": "device",
}

# Categories where a missing date makes the item clinically hard to use. An
# allergy without an onset is normal; a lab result without one is not.
DATE_EXPECTED = ("problem", "procedure", "immunization", "result", "medication")

# v3-ObservationInterpretation codes that mean "look at this".
ABNORMAL_INTERPRETATIONS = {
    "A", "AA", "H", "HH", "HU", "L", "LL", "LU", "POS", "R", "RR", "W",
}

EVENT_DATE_FIELDS = {
    "Flag": ("period",),
    "Condition": ("onsetDateTime", "onsetPeriod", "recordedDate"),
    "AllergyIntolerance": ("onsetDateTime", "recordedDate"),
    "Procedure": ("performedDateTime", "performedPeriod"),
    "Immunization": ("occurrenceDateTime", "recorded"),
    "Observation": ("effectiveDateTime", "effectivePeriod", "issued"),
    "MedicationStatement": ("effectiveDateTime", "effectivePeriod", "dateAsserted"),
    "MedicationRequest": ("authoredOn",),
    "MedicationDispense": ("whenHandedOver", "whenPrepared"),
    "MedicationAdministration": ("effectiveDateTime", "effectivePeriod"),
    "DocumentReference": ("date",),
    "CarePlan": ("period", "created"),
    "DeviceUseStatement": ("timingDateTime", "timingPeriod", "recordedOn"),
}
DEFAULT_DATE_FIELDS = ("date", "effectiveDateTime", "recordedDate")


def _code_system_label(system) -> str | None:
    if not isinstance(system, str) or not system.strip():
        return None
    system = system.strip()
    known = CODE_SYSTEM_LABELS.get(system.rstrip("/"))
    if known:
        return known
    if system.startswith("urn:oid:"):
        return "OID " + system[len("urn:oid:") :]
    try:
        return urllib.parse.urlparse(system).netloc or system
    except ValueError:  # e.g. an unbalanced IPv6 bracket in the authority
        return system


def _concept_coding(cc) -> tuple[str | None, str | None]:
    """(system label, code) of the first usable coding in a CodeableConcept."""
    if not isinstance(cc, dict) or not isinstance(cc.get("coding"), list):
        return (None, None)
    for coding in cc["coding"]:
        if not isinstance(coding, dict):
            continue
        code = coding.get("code")
        if isinstance(code, str) and code.strip():
            return (_code_system_label(coding.get("system")), code.strip())
    return (None, None)


def _primary_concept(resource, rtype, by_ref):
    """The CodeableConcept that carries the resource's clinical meaning."""
    if rtype in MEDICATION_TYPES:
        cc = resource.get("medicationCodeableConcept")
        if isinstance(cc, dict):
            return cc
        ref = resource.get("medicationReference")
        target = None
        if isinstance(ref, dict):
            raw = ref.get("reference")
            if isinstance(raw, str) and raw.startswith("#"):
                target = _contained_resource(resource, raw[1:])
            else:
                target = _resolve_reference(raw, by_ref)
        return target.get("code") if isinstance(target, dict) else None
    if rtype == "Immunization":
        return resource.get("vaccineCode")
    if rtype == "Device":
        return resource.get("type")
    if rtype == "DeviceUseStatement":
        ref = resource.get("device")
        if isinstance(ref, dict):
            raw = ref.get("reference")
            target = (
                _contained_resource(resource, raw[1:])
                if isinstance(raw, str) and raw.startswith("#")
                else _resolve_reference(raw, by_ref)
            )
            if isinstance(target, dict):
                return target.get("type")
        return None
    if rtype == "CarePlan":
        categories = resource.get("category")
        if isinstance(categories, list) and categories:
            return categories[0]
        return None
    if rtype == "DocumentReference":
        return resource.get("type")
    return resource.get("code")


def _event_date(resource, rtype) -> str | None:
    """Sortable ISO date for the item, taking a period's start."""
    for key in EVENT_DATE_FIELDS.get(rtype, DEFAULT_DATE_FIELDS):
        val = resource.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            start = val.get("start")
            if isinstance(start, str) and start.strip():
                return start.strip()
    return None


def _observation_abnormal(resource) -> bool:
    interpretations = resource.get("interpretation")
    if not isinstance(interpretations, list):
        return False
    for cc in interpretations:
        code = _concept_coding(cc)[1]
        if isinstance(code, str) and code.upper() in ABNORMAL_INTERPRETATIONS:
            return True
        text = _cc_display(cc)
        if isinstance(text, str) and text.strip().lower() in (
            "abnormal", "high", "low", "critical", "positive",
        ):
            return True
    return False


def _annotate_item(item, resource, rtype, by_ref) -> None:
    """Add what the clinician views need beyond label/detail/status: clinical
    category, sortable date, coding provenance, salience flags, and the
    missing-information gaps that decide whether the item is actually usable."""
    category = ITEM_CATEGORIES.get(rtype, "other")
    system, code = _concept_coding(_primary_concept(resource, rtype, by_ref))
    date = _event_date(resource, rtype)
    status = (item.get("status") or "").strip().lower()
    flags: list = []
    gaps: list = []
    if not code:
        gaps.append("not coded")
    if not date and category in DATE_EXPECTED:
        gaps.append("no date")
    if category == "allergy":
        criticality = _first_str(resource, "criticality")
        if criticality == "high":
            flags.append("high risk")
        elif not criticality:
            gaps.append("no criticality")
        reactions = resource.get("reaction")
        if not isinstance(reactions, list) or not reactions:
            gaps.append("no reaction recorded")
    elif category == "medication":
        if rtype == "MedicationAdministration":
            # One dose given on a date: it is neither "active" nor missing
            # a dosage instruction, and the date is the whole point.
            flags.append("last dose")
        else:
            if not _dosage_detail(resource):
                gaps.append("no dose")
            if status == "active":
                flags.append("active")
    elif category == "alert":
        if _flag_priority(resource) == "PH":
            flags.append("high priority")
    elif category == "problem":
        if status == "active":
            flags.append("active")
        severity = _cc_display(resource.get("severity"))
        if isinstance(severity, str) and severity.strip().lower() == "severe":
            flags.append("severe")
    elif category == "result":
        if _observation_value(resource) is None:
            gaps.append("no value")
        if _observation_abnormal(resource):
            flags.append("abnormal")
    item["category"] = category
    item["date"] = date
    item["code"] = code
    item["code_system"] = system
    item["flags"] = flags
    item["gaps"] = gaps


def _summary_quality(summary) -> dict:
    """Document-level answer to "can a clinician trust and use this?"."""
    items = [i for sec in summary["sections"] for i in sec["items"]]
    items += summary["unreferenced"]
    gap_counts: dict = {}
    categories: dict = {}
    dates = []
    for item in items:
        for gap in item.get("gaps") or []:
            gap_counts[gap] = gap_counts.get(gap, 0) + 1
        category = item.get("category") or "other"
        categories[category] = categories.get(category, 0) + 1
        if item.get("date"):
            dates.append(item["date"])
    # A narrative-only section (a patient story) carries data as surely as
    # one with entries; it is only coded differently.
    populated = sum(1 for sec in summary["sections"]
                    if sec["items"] or sec.get("narrative"))
    return {
        "items": len(items),
        "coded": sum(1 for i in items if i.get("code")),
        "dated": len(dates),
        "with_gaps": sum(1 for i in items if i.get("gaps")),
        "gaps": dict(sorted(gap_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "categories": categories,
        "sections_populated": populated,
        "sections_empty": len(summary["sections"]) - populated,
        "earliest": min(dates) if dates else None,
        "latest": max(dates) if dates else None,
    }


# IPS/EPS section identity. The LOINC code is the reliable signal - EPS documents
# are written in the local language, so titles alone cannot be matched.
SECTION_LOINC_KINDS = {
    "104605-1": "alert",
    "81338-6": "story",
    "48765-2": "allergy",
    "10160-0": "medication",
    "11450-4": "problem",
    "11348-0": "problem",
    "47519-4": "procedure",
    "30954-2": "result",
    "8716-3": "result",
    "11369-6": "immunization",
    "46264-8": "device",
    "18776-5": "plan",
    "42348-3": "plan",
}

SECTION_TITLE_KINDS = (
    ("alert", "alert|uppm.rksamhet|varning|warnung"),
    ("allergy", "allerg|intoleran"),
    ("medication", "medicat|medikat|medicin|pharma"),
    ("problem", "problem|condition|diagnos|krankheit"),
    ("procedure", "procedur|eingriff|therapien"),
    ("immunization", "immunis|immuniz|vaccin|impf"),
    ("device", "device|implantat|ger.te|hilfsmittel"),
    ("result", "result|befund|labor|ergebnis"),
    ("plan", "plan of care|care plan|behandlungsplan"),
)


def _section_kind(section: dict, title: str) -> str:
    """What clinical section this is, independent of its language."""
    code = section.get("code")
    if isinstance(code, dict) and isinstance(code.get("coding"), list):
        for coding in code["coding"]:
            if not isinstance(coding, dict):
                continue
            kind = SECTION_LOINC_KINDS.get(str(coding.get("code") or ""))
            if kind:
                return kind
    lowered = (title or "").lower()
    for kind, pattern in SECTION_TITLE_KINDS:
        if re.search(pattern, lowered):
            return kind
    return "other"


def _render_resource(resource, by_ref) -> dict:
    """One clinical resource as a reader-friendly {label, detail, status}."""
    rtype = resource.get("resourceType")
    rtype = rtype if isinstance(rtype, str) and rtype else "Resource"
    status = resource.get("status") if isinstance(resource.get("status"), str) else None
    detail = None
    if rtype == "MedicationAdministration":
        label = _medication_name(resource, by_ref)
        given = _choice_str(resource, "effective")
        dosage = resource.get("dosage")
        dose = _first_str(dosage, "text") if isinstance(dosage, dict) else None
        detail = "; ".join(p for p in (f"given {given}" if given else None, dose) if p) or None
    elif rtype in MEDICATION_TYPES:
        label = _medication_name(resource, by_ref)
        detail = _dosage_detail(resource)
    elif rtype == "Flag":
        label = _cc_display(resource.get("code")) or "Alert"
        parts = []
        priority = _flag_priority(resource)
        if priority:
            parts.append({"PH": "high priority", "PM": "medium priority",
                          "PL": "low priority"}.get(priority, priority))
        since = _period_str(resource.get("period"))
        if since:
            parts.append(f"since {since}" if " to " not in since else since)
        detail = "; ".join(parts) or None
    elif rtype == "DocumentReference":
        label = _first_str(resource, "description") or _cc_display(
            resource.get("type")) or "Document"
        url = None
        contents = resource.get("content")
        if isinstance(contents, list):
            for content in contents:
                att = content.get("attachment") if isinstance(content, dict) else None
                url = _first_str(att, "url") if isinstance(att, dict) else None
                if url:
                    break
        detail = "; ".join(p for p in (_cc_display(resource.get("type")), url) if p) or None
    elif rtype == "AllergyIntolerance":
        label = _cc_display(resource.get("code")) or "Allergy or intolerance"
        status = _cc_display(resource.get("clinicalStatus")) or status
        parts = []
        crit = _first_str(resource, "criticality")
        if crit:
            parts.append(f"criticality {crit}")
        manifestations: list = []
        reactions = resource.get("reaction")
        if isinstance(reactions, list):
            for reaction in reactions:
                if isinstance(reaction, dict) and isinstance(
                    reaction.get("manifestation"), list
                ):
                    for man in reaction["manifestation"]:
                        name = _cc_display(man)
                        if name and name not in manifestations:
                            manifestations.append(name)
        if manifestations:
            parts.append("reactions: " + ", ".join(manifestations))
        detail = "; ".join(parts) or None
    elif rtype == "Condition":
        label = _cc_display(resource.get("code")) or "Condition"
        status = _cc_display(resource.get("clinicalStatus")) or status
        onset = _choice_str(resource, "onset")
        detail = f"onset {onset}" if onset else None
    elif rtype == "Immunization":
        label = _cc_display(resource.get("vaccineCode")) or "Immunization"
        detail = _choice_str(resource, "occurrence")
    elif rtype == "Observation":
        label = _cc_display(resource.get("code")) or "Observation"
        value = _observation_value(resource)
        when = _choice_str(resource, "effective")
        detail = "; ".join(p for p in (value, when) if p) or None
    elif rtype == "Procedure":
        label = _cc_display(resource.get("code")) or "Procedure"
        detail = _choice_str(resource, "performed")
    elif rtype == "CarePlan":
        label = _first_str(resource, "title", "description")
        if not label:
            activities = resource.get("activity")
            if isinstance(activities, list):
                for activity in activities:
                    if isinstance(activity, dict) and isinstance(
                        activity.get("detail"), dict
                    ):
                        label = _cc_display(activity["detail"].get("code"))
                        if label:
                            break
        if not label:
            categories = resource.get("category")
            if isinstance(categories, list) and categories:
                label = _cc_display(categories[0])
        label = label or "Care plan"
        # The activity is what a reader acts on: a plan can be "active" while
        # its one appointment is cancelled, and the cancellation is the news.
        activity = _first_activity_detail(resource)
        parts = []
        if activity is not None:
            act_status = activity.get("status")
            if isinstance(act_status, str) and act_status.strip():
                status = act_status.strip()
            scheduled = _period_str(activity.get("scheduledPeriod")) or _first_str(
                activity, "scheduledString"
            )
            if scheduled:
                parts.append(f"scheduled {scheduled}")
        period = resource.get("period")
        span = _period_str(period)
        if span:
            if " to " in span:
                parts.append(f"plan {span}")
            elif period.get("start"):
                parts.append(f"plan since {span}")
            else:
                parts.append(f"plan until {span}")
        detail = "; ".join(parts) or None
    elif rtype == "Device":
        label = _cc_display(resource.get("type")) or "Device"
    elif rtype == "DeviceUseStatement":
        label = "Device"
        device_ref = resource.get("device")
        if isinstance(device_ref, dict):
            ref = device_ref.get("reference")
            if isinstance(ref, str) and ref.startswith("#"):
                target = _contained_resource(resource, ref[1:])
            else:
                target = _resolve_reference(ref, by_ref)
            if isinstance(target, dict):
                label = _cc_display(target.get("type")) or label
            elif isinstance(device_ref.get("display"), str):
                label = device_ref["display"]
        timing = resource.get("timingPeriod")
        span = _period_str(timing)
        started = _first_str(resource, "timingDateTime")
        recorded = _first_str(resource, "recordedOn")
        if span and " to " in span:
            detail = f"in use {span}"
        elif span:
            detail = f"in use since {span}" if timing.get("start") else f"in use until {span}"
        elif started:
            detail = f"in use since {started}"
        elif recorded:
            detail = f"recorded {recorded}"
    else:
        display = _cc_display(resource.get("code")) or _first_str(
            resource, "title", "description"
        )
        if display:
            label, detail = display, _humanize_type(rtype)
        else:
            label = _humanize_type(rtype)
    item = {"label": label, "detail": detail, "status": status,
            "note": _note_text(resource) if rtype != "Flag" else _flag_note(resource)}
    _annotate_item(item, resource, rtype, by_ref)
    return item


def _first_activity_detail(resource) -> dict | None:
    activities = resource.get("activity")
    if isinstance(activities, list):
        for activity in activities:
            if isinstance(activity, dict) and isinstance(activity.get("detail"), dict):
                return activity["detail"]
    return None


def _note_text(resource) -> str | None:
    """The first Annotation on the resource - where authors put the part that
    matters most and fits no coded field ("cancelled by the patient, not
    rebooked")."""
    notes = resource.get("note")
    if isinstance(notes, list):
        for note in notes:
            text = note.get("text") if isinstance(note, dict) else None
            if isinstance(text, str) and text.strip():
                return " ".join(text.split())
    return None


def _patient_summary(patient) -> dict:
    name_text = None
    names = patient.get("name")
    if isinstance(names, list):
        for name in names:
            if not isinstance(name, dict):
                continue
            given = name.get("given")
            parts = [g for g in given if isinstance(g, str)] if isinstance(given, list) else []
            family = name.get("family")
            if isinstance(family, str) and family.strip():
                parts.append(family.strip())
            if parts:
                name_text = " ".join(parts)
                break
            text = name.get("text")
            if isinstance(text, str) and text.strip():
                name_text = text.strip()
                break
    identifiers = []
    ids = patient.get("identifier")
    if isinstance(ids, list):
        for ident in ids:
            if isinstance(ident, dict) and isinstance(ident.get("value"), str):
                identifiers.append(
                    {
                        "system": ident.get("system")
                        if isinstance(ident.get("system"), str)
                        else None,
                        "value": ident["value"],
                    }
                )
    return {
        "name": name_text,
        "birth_date": patient.get("birthDate")
        if isinstance(patient.get("birthDate"), str)
        else None,
        "gender": patient.get("gender")
        if isinstance(patient.get("gender"), str)
        else None,
        "identifiers": identifiers,
    }


def _collect_references(value, out: list) -> None:
    """Collect every 'reference' string inside a JSON value.

    Iterative rather than recursive: json.loads accepts nesting a good deal
    deeper than Python's default recursion limit, and a submission is not
    allowed to take the summary endpoint down with it.
    """
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("reference")
            if isinstance(ref, str):
                out.append(ref)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def summarize_bundle(bundle) -> dict:
    """Clinician-readable summary of an EPS document bundle.

    Fully defensive: malformed input yields an empty-but-well-formed summary,
    never an exception. Sections resolve their entry references against
    entry.fullUrl (exact), the fullUrl's trailing 'Type/id', or the resource's
    own 'resourceType/id'. Clinical resources not reachable from the
    Composition (directly via sections or transitively via references such as
    medicationReference) are listed under "unreferenced" as a data-quality
    hint.
    """
    summary: dict = {
        "patient": None,
        "composition": None,
        "sections": [],
        "unreferenced": [],
        "quality": _summary_quality({"sections": [], "unreferenced": []}),
    }
    if not isinstance(bundle, dict) or not isinstance(bundle.get("entry"), list):
        return summary
    entries = [
        e
        for e in bundle["entry"]
        if isinstance(e, dict) and isinstance(e.get("resource"), dict)
    ]
    by_ref = _build_reference_index(entries)
    composition = patient = None
    for entry in entries:
        rtype = entry["resource"].get("resourceType")
        if rtype == "Composition" and composition is None:
            composition = entry["resource"]
        elif rtype == "Patient" and patient is None:
            patient = entry["resource"]
    if patient is not None:
        summary["patient"] = _patient_summary(patient)
    reachable: set = set()
    if composition is not None:
        summary["composition"] = {
            "title": _first_str(composition, "title"),
            "date": _first_str(composition, "date"),
        }
        sections = composition.get("section")
        if isinstance(sections, list):
            for section in sections:
                if not isinstance(section, dict):
                    continue
                title = (
                    _first_str(section, "title")
                    or _cc_display(section.get("code"))
                    or "Untitled section"
                )
                items = []
                refs = section.get("entry")
                if isinstance(refs, list):
                    for ref_entry in refs:
                        if not isinstance(ref_entry, dict):
                            continue
                        target = _resolve_reference(ref_entry.get("reference"), by_ref)
                        if target is not None:
                            items.append(_render_resource(target, by_ref))
                empty_reason = narrative = None
                if not items:
                    empty_reason = _cc_display(section.get("emptyReason"))
                    if empty_reason:
                        empty_reason = EMPTY_REASON_LABELS.get(
                            _squash(empty_reason), empty_reason
                        )
                    elif not isinstance(section.get("entry"), list):
                        # No entries and no reason for the lack: the section's
                        # content is its narrative (a patient story), so the
                        # views show that rather than "No entries".
                        narrative = _section_narrative(section)
                summary["sections"].append(
                    {
                        "title": title,
                        "kind": _section_kind(section, title),
                        "items": items,
                        "empty_reason": empty_reason,
                        "narrative": narrative,
                    }
                )
        # Everything reachable from the Composition (section entries, authors,
        # and transitively from referenced resources) counts as referenced.
        queue = [composition]
        seen = {id(composition)}
        while queue:
            refs: list = []
            _collect_references(queue.pop(), refs)
            for ref in refs:
                target = _resolve_reference(ref, by_ref)
                if target is not None and id(target) not in seen:
                    seen.add(id(target))
                    reachable.add(id(target))
                    queue.append(target)
    for entry in entries:
        resource = entry["resource"]
        if resource.get("resourceType") in ("Composition", "Patient"):
            continue
        if id(resource) in reachable:
            continue
        summary["unreferenced"].append(_render_resource(resource, by_ref))
    summary["quality"] = _summary_quality(summary)
    return summary


# ---------------------------------------------------------------- interop


def best_valid_submission(conn: sqlite3.Connection, team_id: int) -> sqlite3.Row | None:
    """The team's published exchange document: its best valid submission
    (highest score, earliest ts tiebreak - mirrors the leaderboard pick)."""
    return conn.execute(
        "SELECT * FROM submissions WHERE team_id = ? AND pass = 1 "
        "ORDER BY score DESC, ts ASC, id ASC LIMIT 1",
        (team_id,),
    ).fetchone()


def read_submission_bytes(team_slug: str, filename: str) -> bytes | None:
    path = os.path.join(DATA_DIR, "submissions", team_slug, filename)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def bundle_response(raw: bytes, filename: str, **headers: str) -> Response:
    """Serve a bundle so a click in a browser saves it under a usable name.

    Without this the browser has no filename to work from and saves every
    document as "bundle", with no extension.
    """
    return Response(
        content=raw,
        media_type="application/fhir+json",
        headers=dict(
            headers, **{"Content-Disposition": 'attachment; filename="%s"' % filename}
        ),
    )


# ------------------------------------------------------- document detection


def _meta_profiles(resource) -> list[str]:
    """meta.profile as a list of strings, version suffix stripped.

    A claim may be versioned ("...|1.0.0"), and a team that pins the version
    still means the same profile.
    """
    if not isinstance(resource, dict):
        return []
    meta = resource.get("meta")
    claimed = meta.get("profile") if isinstance(meta, dict) else None
    return [c.split("|", 1)[0] for c in claimed or [] if isinstance(c, str)]


def _composition_type_codes(bundle: dict) -> set[str]:
    """LOINC codes on the first Composition's type, for identifying a document."""
    for entry in bundle.get("entry") or []:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict):
            continue
        if resource.get("resourceType") != "Composition":
            continue
        ctype = resource.get("type")
        codings = ctype.get("coding") if isinstance(ctype, dict) else None
        return {
            c["code"]
            for c in codings or []
            if isinstance(c, dict)
            and isinstance(c.get("code"), str)
            and c.get("system") == "http://loinc.org"
        }
    return set()


def detect_document(bundle) -> dict:
    """Which EHDS category a submission is, as an EHDS_DOCUMENTS entry.

    Ordered by how much the bundle is actually telling us, strongest first:

      1. Bundle.meta.profile naming a category's bundle profile. This is the
         submitter saying what they built, and it is the only signal that
         separates the two imaging bundle shapes from anything else.
      2. An entry claiming an MPD profile. A prescription has no bundle-level
         profile to claim, so its evidence is one level down.
      3. Composition.type. Fixed to one LOINC code by EPS and laboratory,
         and the natural fallback for a document that claims no profile.
      4. An ImagingStudy in the bundle. Imaging's type codes are a LOINC
         subsumption query we cannot answer offline, so this stands in for
         the code check that the other categories get.
      5. Medication resources in a non-document bundle - a prescription that
         claims nothing at all.

    Falls back to the patient summary, which is the event's own track: an
    unrecognised bundle is scored exactly as it was before this function
    existed, rather than being rejected for failing to announce itself. That
    fallback is also why the secondary categories cost a team nothing to
    ignore - not claiming one cannot accidentally demote a summary.
    """
    if not isinstance(bundle, dict):
        return PRIMARY_DOCUMENT

    claimed = set(_meta_profiles(bundle))
    for doc in EHDS_DOCUMENTS:
        if claimed.intersection(doc["bundles"]):
            return doc

    entries = bundle.get("entry")
    entries = entries if isinstance(entries, list) else []
    entry_claims: set[str] = set()
    resource_types: set[str] = set()
    for entry in entries:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict):
            continue
        rtype = resource.get("resourceType")
        if isinstance(rtype, str):
            resource_types.add(rtype)
        entry_claims.update(_meta_profiles(resource))
    for doc in EHDS_DOCUMENTS:
        if entry_claims.intersection(doc.get("entry_profiles") or ()):
            return doc

    codes = _composition_type_codes(bundle)
    for doc in EHDS_DOCUMENTS:
        if codes.intersection(doc["loinc"]):
            return doc

    if "ImagingStudy" in resource_types:
        return DOCUMENTS_BY_ID["imaging"]
    # Only when there is no Composition: a patient summary carries a medication
    # section, and reading one as a prescription would score it against the
    # wrong profile entirely.
    if "Composition" not in resource_types and resource_types.intersection(
        {"MedicationRequest", "MedicationDispense"}
    ):
        return DOCUMENTS_BY_ID["mpd"]
    return PRIMARY_DOCUMENT


# ---------------------------------------------------------------- matchbox


def validate_with_matchbox(body: bytes, doc: dict | None = None) -> dict:
    """POST the bundle to Matchbox $validate; return the OperationOutcome dict.

    `doc` is an EHDS_DOCUMENTS entry and decides both the profile the bundle is
    checked against and the packages the engine is scoped to. Scoping matters:
    an engine built for the patient summary does not carry the laboratory
    profiles, so a lab report validated against it comes back with its
    conformance unchecked rather than checked and failed. Defaults to the
    patient summary so an existing caller keeps its old behaviour.

    Raises MatchboxError when the validator is unreachable or unusable.
    Monkeypatched in tests.
    """
    doc = doc or PRIMARY_DOCUMENT
    query = [("profile", doc["profile"])] + [("igs", ig) for ig in doc["igs"]]
    url = f"{MATCHBOX_URL}/$validate?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/fhir+json",
            "Accept": "application/fhir+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=MATCHBOX_TIMEOUT) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        # A 4xx normally carries the OperationOutcome for a bundle Matchbox
        # could not even parse, which is the submitter's verdict. A 5xx is
        # the validator falling over, and must not be scored as the bundle's
        # fault even when it comes wrapped in an OperationOutcome.
        if exc.code >= 500:
            raise MatchboxError(f"Matchbox returned HTTP {exc.code}") from exc
        payload = exc.read()
        try:
            outcome = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise MatchboxError(f"Matchbox returned HTTP {exc.code}") from exc
        if (
            not isinstance(outcome, dict)
            or outcome.get("resourceType") != "OperationOutcome"
        ):
            raise MatchboxError(f"Matchbox returned HTTP {exc.code}") from exc
        return outcome
    except (
        urllib.error.URLError,
        http.client.HTTPException,  # RemoteDisconnected, BadStatusLine, ...
        TimeoutError,
        OSError,
    ) as exc:
        raise MatchboxError(f"Matchbox unreachable: {exc}") from exc
    try:
        outcome = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MatchboxError("Matchbox returned a non-JSON response") from exc
    if not isinstance(outcome, dict) or outcome.get("resourceType") != "OperationOutcome":
        raise MatchboxError("Matchbox response was not an OperationOutcome")
    return outcome


# ------------------------------------------------- unresolvable terminology

# Matchbox runs with `txServer: n/a` (see matchbox/config/application.yaml), so
# the licensed code systems EPS points at - SNOMED CT, LOINC, WHO ATC - are not
# resolvable here. The validator is scrupulous about saying so, and says it once
# per coding: a rich summary picks up hundreds of "the code cannot be validated"
# lines, which bury the findings a team can actually act on and read like
# defects in their bundle rather than a gap in ours.
#
# Every message id below is from the HL7 validator's own Messages.properties
# (org.hl7.fhir.utilities 6.10.3) and means "this was not checked", never "this
# is wrong" - so dropping them removes noise, not findings.
MSG_ID_EXT = "http://hl7.org/fhir/StructureDefinition/operationoutcome-message-id"

UNCHECKABLE_TERMINOLOGY_IDS = frozenset({
    # no terminology server at all
    "Error_expanding_ValueSet_running_without_terminology_services",
    "Error_validating_code_running_without_terminology_services",
    "TERMINOLOGY_TX_NOSVC_BOUND_EXT",
    "TERMINOLOGY_TX_NOSVC_BOUND_REQ",
    "Terminology_TX_NoValid_15",
    # the code system is missing, unusable or unsupported
    "TERMINOLOGY_TX_SYSTEM_NOT_USABLE",
    "TERMINOLOGY_TX_SYSTEM_UNSUPPORTED",
    "UNKNOWN_CODESYSTEM",
    "UNKNOWN_CODESYSTEM_EXP",
    "UNKNOWN_CODESYSTEM_VERSION",
    "UNKNOWN_CODESYSTEM_VERSION_EXP",
    "UNKNOWN_CODESYSTEM_VERSION_EXP_NONE",
    "UNKNOWN_CODESYSTEM_VERSION_NONE",
    "UNKNOWN_CODESYSTEM_VERSION_UNK",
    "VALUESET_INC_CS_NO_SUPPORT",
    "QUESTIONNAIRE_QR_ITEM_BADOPTION_CS",
    # the value set is missing, so membership could not be tested
    "CODESYSTEM_PROPERTY_VALUESET_NOT_FOUND",
    "Terminology_TX_ValueSet_NotFound",
    "UNABLE_TO_CHECK_IF_THE_PROVIDED_CODES_ARE_IN_THE_VALUE_SET_",
    "UNABLE_TO_CHECK_IF_THE_PROVIDED_CODES_ARE_IN_THE_VALUE_SET_CS",
    "UNABLE_TO_CHECK_IF_THE_PROVIDED_CODES_ARE_IN_THE_VALUE_SET_VS",
    "Unable_to_validate_code_without_using_server",
})

# `TERMINOLOGY_TX_WARNING` is defined as "{1}" - it passes through whatever the
# terminology layer said - and some issues carry no message id at all, so the
# same family has to be recognised by wording as well.
UNCHECKABLE_TERMINOLOGY_TEXT = (
    "running without terminology services",
    "unable to validate code without using server because",
    "because there is no terminology service",
    "in the absence of a terminology server",
    "so the code cannot be validated",
    "so the code has not been validated",
    "so the value set cannot be expanded",
    "so the property codes cannot be validated",
    "unable to check whether the code is in the value set",
)

# Systems and value sets are named in those messages as quoted or bare URLs.
TERMINOLOGY_URL = re.compile(r"(?:https?://|urn:)[^\s'\"),;]+")
# "... not found (from http://hl7.eu/.../bundle-eu-eps|1.0.0-ballot)" - the
# profile the finding came from, not a code system. Mirrors cleanMessage().
FROM_PROFILE = re.compile(r"\s*\(from [^)\s]+\)")


def _issue_text(issue: dict) -> str:
    details = issue.get("details")
    text = (details or {}).get("text") if isinstance(details, dict) else None
    return text or issue.get("diagnostics") or ""


def _message_id(issue: dict) -> str | None:
    for ext in issue.get("extension") or []:
        if isinstance(ext, dict) and ext.get("url") == MSG_ID_EXT:
            return ext.get("valueCode")
    return None


def _named_urls(text: str) -> set[str]:
    """The systems and value sets a message names, without version suffixes.

    The trailing "(from <profile>)" the validator appends is stripped first -
    the same parenthetical cleanMessage() removes for display. A profile URL is
    not a code system, and letting one into the unresolvable set would drop
    every real terminology error raised by that same profile.
    """
    return {u.split("|")[0].split("#")[0].rstrip(".,")
            for u in TERMINOLOGY_URL.findall(FROM_PROFILE.sub("", text))}


def drop_uncheckable_terminology(outcome: dict) -> dict:
    """Remove findings that only say a code could not be checked here.

    Two passes, because the validator reports the same gap twice. The first
    pass drops the explicit "could not be found / no terminology service"
    messages and remembers which systems and value sets they named. The second
    drops the membership claims - "None of the codings provided are in the value
    set X" - but *only* where the codes or the value set came from something the
    first pass just told us was unresolvable. A membership claim about a value
    set that did resolve is a real finding and stays.

    That makes the filter self-calibrating rather than a hardcoded list of code
    systems: point Matchbox at a terminology server and the first pass finds
    nothing, so the second drops nothing and every finding is shown again.
    """
    issues = outcome.get("issue")
    if not isinstance(issues, list):
        return outcome

    kept, unresolvable = [], set()
    for issue in issues:
        if not isinstance(issue, dict):
            kept.append(issue)
            continue
        text = _issue_text(issue)
        lowered = text.lower()
        if (_message_id(issue) in UNCHECKABLE_TERMINOLOGY_IDS
                or any(p in lowered for p in UNCHECKABLE_TERMINOLOGY_TEXT)):
            unresolvable |= _named_urls(text)
            continue
        kept.append(issue)

    survivors = []
    for issue in kept:
        mid = _message_id(issue) if isinstance(issue, dict) else None
        # `unresolvable` can legitimately be empty: two of the ids above carry
        # no URL in their text at all ("running without terminology services").
        # Returning `outcome` here instead of `kept` used to undo the whole
        # first pass, so a bundle scored 0 or 50 depending on whether some
        # unrelated finding happened to mention a URL.
        if unresolvable and mid and (mid.startswith("Terminology_TX_NoValid")
                    or mid.startswith("Terminology_TX_Confirm")):
            if _named_urls(_issue_text(issue)) & unresolvable:
                continue
        survivors.append(issue)

    trimmed = dict(outcome)
    trimmed["issue"] = survivors
    return trimmed


def matchbox_health_url() -> str:
    base = MATCHBOX_URL.rstrip("/")
    if base.endswith("/fhir"):
        base = base[: -len("/fhir")]
    return base + "/actuator/health"


MATCHBOX_PROBE_TIMEOUT = 3  # seconds; health checks must not hang the UI


def loaded_ig_packages() -> set[str] | None:
    """{"packageId#version", ...} Matchbox reports loaded, or None if unknown.

    Matchbox only reads application.yaml at boot, so a package added to the
    config after the container came up is silently absent: validation still
    runs, and every profile claim from that package comes back "not checked".
    """
    url = f"{MATCHBOX_URL}/ImplementationGuide?_summary=true&_count=200"
    req = urllib.request.Request(url, headers={"Accept": "application/fhir+json"})
    try:
        with urllib.request.urlopen(req, timeout=MATCHBOX_PROBE_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
        ValueError,
        UnicodeDecodeError,
    ):
        return None
    if not isinstance(data, dict) or data.get("resourceType") != "Bundle":
        return None
    # A searchset with no matches omits "entry" altogether; that is the answer
    # "nothing is loaded", not an unreadable reply.
    entries = data.get("entry", [])
    if not isinstance(entries, list):
        return None
    loaded: set[str] = set()
    for entry in entries:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict):
            continue
        package = resource.get("packageId")
        version = resource.get("version")
        if isinstance(package, str) and package:
            loaded.add(f"{package}#{version}" if isinstance(version, str) and version
                       else package)
    return loaded


def _ig_loaded(ig: str, loaded: set[str]) -> bool:
    package, _, version = ig.partition("#")
    if package in loaded:
        return True  # listed without a version: nothing to compare, so trusted
    if version:
        return ig in loaded
    return any(entry.startswith(package + "#") for entry in loaded)


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # Every endpoint here is a sync def, so they all share one threadpool -
    # 40 threads by default. A wedged validator parks one of them per
    # in-flight submission for MATCHBOX_TIMEOUT seconds, and the 41st request
    # waits: the leaderboard, which touches nothing but SQLite, goes dark for
    # a minute because some team left a submit loop running. A thread blocked
    # on a socket costs a stack and nothing else, so give the pool room.
    anyio.to_thread.current_default_thread_limiter().total_tokens = 256
    yield


APP_VERSION = "1.0.0"
app = FastAPI(
    title="Nordic Hackathon 2026 EPS service",
    version=APP_VERSION,
    lifespan=lifespan,
)
# Everything served here is JSON or HTML, and the validator's OperationOutcome
# for a real bundle is a few hundred KB that gzips to a twentieth of that.
# Venue wifi shared by every participant is the constraint, not the CPU.
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def count_request(request: Request, call_next):
    """One hit in the usage log per request, and an error row for every 5xx.

    Outermost, so it sees the status that actually went out - including the
    500 for an exception nothing handled, which it records with the traceback
    and lets carry on up to Starlette's own error response.
    """
    try:
        response = await call_next(request)
    except Exception:
        await anyio.to_thread.run_sync(
            _count_request, request, 500, traceback.format_exc()
        )
        raise
    await anyio.to_thread.run_sync(_count_request, request, response.status_code, None)
    return response


def _count_request(request: Request, status: int, trace: str | None) -> None:
    user_agent = request.headers.get("user-agent", "")
    if user_agent.startswith(monitor.USER_AGENT):
        return
    # The route template, never the raw path: /fhir/Bundle/{bundle_id} is one
    # row however many bundles there are, and a scanner's thousand made-up
    # paths are one "(no route)" row rather than a thousand.
    route = getattr(request.scope.get("route"), "path", None) or "(no route)"
    method = request.method
    # Caddy overwrites X-Real-IP with the connecting address, so it cannot be
    # forged from outside; locally there is no Caddy and the socket says.
    address = request.headers.get("x-real-ip") or (
        request.client.host if request.client else ""
    )
    # Browsers send Sec-Fetch-Site on every fetch and navigation; curl, Python
    # and a company's Java stack do not. That one header is what separates the
    # page's own polling from integration work.
    via = "browser" if "sec-fetch-site" in request.headers else "client"
    record_usage(monitor.record_request, method, route, status, via, address, user_agent)
    if status >= 500:
        detail = trace or request.scope.get("usage_error_detail") or "(no detail)"
        record_usage(monitor.record_error, "server", detail,
                     method=method, route=route, status=status)


# ------------------------------------------------------------ FHIR plumbing

FHIR_JSON = "application/fhir+json"
# The service's own canonical base, used in CapabilityStatement and in the
# fullUrls of any searchset. Relative URLs would be ambiguous the moment a
# response is saved to disk or passed to a second tool.
FHIR_BASE = "/fhir"

# HTTP status -> the OperationOutcome issue code FHIR asks for. Anything not
# listed falls back to "processing", the spec's own catch-all, so an
# unanticipated status still produces a legal resource rather than a KeyError.
OUTCOME_CODES = {
    400: "invalid",
    401: "login",
    403: "forbidden",
    404: "not-found",
    405: "not-supported",
    409: "conflict",
    413: "too-costly",
    415: "not-supported",
    422: "processing",
    500: "exception",
    502: "transient",
    503: "transient",
}


def operation_outcome(status: int, diagnostics: str) -> dict:
    """An OperationOutcome for an error, which is what FHIR returns instead of
    a bespoke error object. Severity is always `error`: FHIR reserves `fatal`
    for "the whole exchange is unusable", and a 404 on one read is not that."""
    return {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": OUTCOME_CODES.get(status, "processing"),
                "diagnostics": diagnostics,
            }
        ],
    }


def fhir_response(payload: dict, status: int = 200, **headers: str) -> Response:
    """Any FHIR resource, served as fhir+json.

    JSONResponse would send application/json, which a conformant FHIR client is
    entitled to reject.
    """
    return Response(
        content=json.dumps(payload),
        status_code=status,
        media_type=FHIR_JSON,
        headers=headers or None,
    )


def _param_value(value) -> dict:
    """The value[x] element for a Python value.

    bool before int deliberately - bool is a subclass of int in Python, so the
    obvious ordering turns every `true` into `valueInteger: 1`.
    """
    if isinstance(value, bool):
        return {"valueBoolean": value}
    if isinstance(value, int):
        return {"valueInteger": value}
    if isinstance(value, float):
        return {"valueDecimal": value}
    if isinstance(value, dict) and "resourceType" in value:
        return {"resource": value}
    if isinstance(value, dict):
        return {"part": _named_params(value.items())}
    return {"valueString": str(value)}


def _named_param(name: str, value) -> dict:
    return {"name": name, **_param_value(value)}


def _named_params(pairs) -> list:
    """Named parameters (or parts) from (name, value) pairs.

    The two FHIR rules that make this more than a comprehension, applied at
    every depth rather than only at the top: a list repeats the name, because
    FHIR has no array type, and a `None` is dropped, because FHIR has no null
    and an absent element *is* the null. A nested list used to fall through to
    `str(value)` and go out as `valueString: "['laboratory']"`.
    """
    out = []
    for name, value in pairs:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            out.extend(_named_param(name, item) for item in value)
        else:
            out.append(_named_param(name, value))
    return out


def parameters(pairs) -> dict:
    """A Parameters resource from (name, value) pairs.

    Parameters is FHIR's answer to "the server computed something that is not a
    stored resource" - a leaderboard, a health check, a token. `None` values are
    dropped rather than sent as null, because FHIR has no null: an absent
    element *is* the null, and emitting one is invalid.

    A list value repeats the parameter, which is how FHIR expresses
    multiplicity - it has no array type.
    """
    return {"resourceType": "Parameters", "parameter": _named_params(pairs)}


def searchset(resources: list, total: int | None = None) -> dict:
    """A searchset Bundle, which is how FHIR returns a list of resources.

    `total` defaults to the number carried, which is right whenever the whole
    result is in one page - this server never pages, so it always is.
    """
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources) if total is None else total,
        "entry": [
            {
                "fullUrl": "%s/%s/%s" % (FHIR_BASE, r["resourceType"], r["id"]),
                "resource": r,
            }
            for r in resources
        ],
    }


@app.exception_handler(StarletteHTTPException)
def http_exception_to_outcome(
    _request: Request, exc: StarletteHTTPException
) -> Response:
    """Every error leaves as an OperationOutcome.

    FastAPI's default is `{"detail": "..."}`, which is the one custom JSON shape
    a FHIR client has no way to read. Registered for the exception class rather
    than wrapped around each raise, so the errors the framework raises on its
    own - an unrouted path, a wrong method - are converted too.
    """
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    if exc.status_code >= 500:
        # For count_request's error row: by the time the response reaches it
        # the detail is serialised inside an OperationOutcome.
        _request.scope["usage_error_detail"] = detail
    return fhir_response(
        operation_outcome(exc.status_code, detail),
        status=exc.status_code,
        **(exc.headers or {}),
    )


@app.exception_handler(RequestValidationError)
def validation_error_to_outcome(
    _request: Request, exc: RequestValidationError
) -> Response:
    """A bad query or path parameter, as an OperationOutcome.

    FastAPI answers these with a list of error objects of its own design -
    another shape a FHIR client would have to learn. The location it names
    ("query", "count") is the useful half, so that is what is kept.
    """
    parts = []
    for err in exc.errors():
        where = ".".join(str(bit) for bit in err.get("loc", ()) if bit != "body")
        parts.append("%s: %s" % (where, err.get("msg", "invalid")) if where
                     else err.get("msg", "invalid"))
    return fhir_response(
        operation_outcome(422, "; ".join(parts) or "Invalid request"),
        status=422,
    )


async def read_limited_body(request: Request, limit: int, what: str) -> bytes:
    """Read the body, refusing anything over `limit` before it is all resident.

    request.body() buffers the whole payload first, so a size check in the
    handler only ever runs once the memory has already been spent.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(
            413, detail=f"{what} too large ({declared} bytes); limit is {limit} bytes"
        )
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(
                413,
                detail=f"{what} too large (over {limit} bytes); limit is "
                f"{limit} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def team_from_token(
    x_team_token: str | None, authorization: str | None
) -> sqlite3.Row:
    """Resolve the X-Team-Token / Bearer credential to a team row or raise 401."""
    token = x_team_token
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(401, detail="Missing X-Team-Token header")
    with connect() as conn:
        team = conn.execute(
            "SELECT id, name, slug FROM teams WHERE token = ?", (token,)
        ).fetchone()
    if team is None:
        raise HTTPException(401, detail="Invalid company token")
    return team


def current_team(
    x_team_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> sqlite3.Row:
    return team_from_token(x_team_token, authorization)


async def bundle_body(
    request: Request, team: sqlite3.Row = Depends(current_team)
) -> bytes:
    """The submitted bundle, read only once the caller is known.

    Depending on current_team first means an anonymous client cannot make the
    server buffer megabytes before the 401. FastAPI caches the dependency, so
    the handler's own current_team is the same lookup, not a second one.
    """
    return await read_limited_body(request, MAX_BODY_BYTES, "Bundle")


async def image_body(
    request: Request, team: sqlite3.Row = Depends(current_team)
) -> bytes:
    """The uploaded screenshot, read only once the uploader is known."""
    return await read_limited_body(request, MAX_IMAGE_BYTES, "Screenshot")


# The company slug doubles as the Organization's logical id, so a partner's
# document is reachable at a URL a human can type. The identifier system names
# who mints them - a bare slug with no system is unmergeable with anything.
TEAM_IDENTIFIER_SYSTEM = "https://nordichackathon2026.blogs.dsv.su.se/fhir/company"


def organization_resource(row: sqlite3.Row, endpoints=()) -> dict:
    resource = {
        "resourceType": "Organization",
        "id": row["slug"],
        "identifier": [{"system": TEAM_IDENTIFIER_SYSTEM, "value": row["slug"]}],
        "active": True,
        "name": row["name"],
    }
    if endpoints:
        resource["endpoint"] = [
            {"reference": "Endpoint/%d" % endpoint_id} for endpoint_id in endpoints
        ]
    return resource


def endpoints_by_team(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """team id -> the ids of the Endpoints it advertises, oldest first."""
    out: dict[int, list[int]] = {}
    for row in conn.execute("SELECT id, team_id FROM endpoints ORDER BY id"):
        out.setdefault(row["team_id"], []).append(row["id"])
    return out


def team_endpoints(conn: sqlite3.Connection, team_id: int) -> list[int]:
    """The ids of the Endpoints one company advertises, oldest first."""
    return [row["id"] for row in conn.execute(
        "SELECT id FROM endpoints WHERE team_id = ? ORDER BY id", (team_id,))]


async def parameters_body(request: Request) -> dict:
    """A Parameters resource body, flattened to {name: value}.

    FHIR operations take their input as Parameters, so that is what the
    operations here accept. A part's value is its `resource`, or else its
    first value[x]. No operation here takes a parameter twice, so a repeat is
    a 400 rather than one of the two being quietly dropped; nested parts are
    not used.
    """
    raw = await read_limited_body(request, MAX_JSON_BODY_BYTES, "Request body")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, detail="Body is not valid JSON")
    if not isinstance(payload, dict) or payload.get("resourceType") != "Parameters":
        raise HTTPException(
            400, detail="Body must be a FHIR Parameters resource"
        )
    out: dict = {}
    parts = payload.get("parameter")
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict) or not isinstance(part.get("name"), str):
            continue
        if part["name"] in out:
            raise HTTPException(
                400, detail="Parameter `%s` is given more than once" % part["name"])
        # Taken whatever it is, so that a malformed resource reaches the
        # operation and is refused there rather than vanishing here.
        if "resource" in part:
            out[part["name"]] = part["resource"]
            continue
        for key, value in part.items():
            if key.startswith("value"):
                out[part["name"]] = value
                break
        else:
            # A miscased "Resource" or a nested `part` would otherwise vanish,
            # and register would go ahead without it.
            raise HTTPException(
                400, detail="Parameter `%s` has no value[x] or resource"
                            % part["name"])
    return out


@app.post("/fhir/$register",
    tags=["Companies"],
    summary="Register a company and get its token",
    description=(
        "One registration per company. Takes a Parameters resource with a "
        "`name`, and a `code` as well when the server sets REGISTER_CODE. The "
        "token comes back once and is not retrievable afterwards, so share it "
        "inside the company rather than registering again. An optional "
        "`endpoint` part carrying an Endpoint resource advertises where your "
        "system takes documents in, exactly as POST Endpoint would - a bad "
        "one is a 400 and registers nothing."
    ),
)
def register(params: dict = Depends(parameters_body)) -> Response:
    if REGISTER_CODE:
        supplied = params.get("code")
        if not isinstance(supplied, str) or not secrets.compare_digest(
            supplied.encode("utf-8"), REGISTER_CODE.encode("utf-8")
        ):
            raise HTTPException(403, detail="Registration code required")
    team = params.get("name")
    if not isinstance(team, str):
        raise HTTPException(
            400, detail="Parameters must carry a `name` with the company name"
        )
    # Collapse runs of whitespace so "Acme  Health" is the same company as
    # "Acme Health" rather than a way around the one-registration rule.
    team = " ".join(team.split())
    if not 2 <= len(team) <= 40:
        raise HTTPException(400, detail="Company name must be 2-40 characters")
    # Checked before anything is written, and written in the same transaction
    # as the company: a registration that stored the company and then refused
    # its endpoint could not be retried, the name being taken by then.
    endpoint = params.get("endpoint")
    if "endpoint" in params:
        if not isinstance(endpoint, dict) or endpoint.get("resourceType") != "Endpoint":
            raise HTTPException(400, detail="`endpoint` must carry an Endpoint resource")
        endpoint = parse_endpoint(endpoint)
    token = secrets.token_hex(8)
    with connect() as conn:
        # The slug probe and the insert have to see the same table state, or
        # two simultaneous registrations can both settle on "acme-2".
        conn.execute("BEGIN IMMEDIATE")
        slug = unique_slug(conn, team)
        try:
            conn.execute(
                "INSERT INTO teams (name, slug, token, created_at) VALUES (?, ?, ?, ?)",
                (team, slug, token, datetime.now(timezone.utc).isoformat()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, detail="Company name already taken")
        row = conn.execute(
            "SELECT id, name, slug FROM teams WHERE slug = ?", (slug,)
        ).fetchone()
        endpoints = [insert_endpoint(conn, row["id"], endpoint)] if endpoint else []
    record_usage(monitor.record_event, "register", team)
    return fhir_response(
        parameters(
            [
                ("token", token),
                ("organization", organization_resource(row, endpoints)),
                (
                    "note",
                    "One registration per company. Register once and share "
                    "this token with your colleagues.",
                ),
            ]
        )
    )


@app.get("/fhir/Organization",
    tags=["Companies"],
    summary="Every registered company",
    description=(
        "A searchset Bundle of Organizations, by name. This is where a "
        "company's slug comes from: it is the Organization's `id`. `name` "
        "narrows it the way FHIR string search does - the start of the name, "
        "ignoring case and accents - with `name:contains` to match anywhere "
        "and `name:exact` to match the whole name as written."
    ),
)
def search_organizations(
    name: list[str] | None = Query(None),
    name_contains: list[str] | None = Query(None, alias="name:contains"),
    name_exact: list[str] | None = Query(None, alias="name:exact"),
) -> Response:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, slug FROM teams ORDER BY name COLLATE NOCASE"
        ).fetchall()
        endpoints = endpoints_by_team(conn)
    return fhir_response(
        searchset(
            [
                organization_resource(r, endpoints.get(r["id"], ()))
                for r in rows
                if name_matches(r["name"], name, name_contains, name_exact)
            ]
        )
    )


@app.get("/fhir/Organization/$me",
    tags=["Companies"],
    summary="Which company this token belongs to",
    description=(
        "The UI needs this to know which screenshots are yours to edit or "
        "withdraw. It returns no secrets - the token is the secret, and the "
        "caller already has it."
    ),
)
def whoami(team: sqlite3.Row = Depends(current_team)) -> Response:
    with connect() as conn:
        endpoints = team_endpoints(conn, team["id"])
    return fhir_response(organization_resource(team, endpoints))


@app.get("/fhir/Organization/{slug}",
    tags=["Companies"],
    summary="One company",
)
def read_organization(slug: str) -> Response:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, name, slug FROM teams WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, detail="Unknown company")
        endpoints = team_endpoints(conn, row["id"])
    return fhir_response(organization_resource(row, endpoints))


@app.post("/fhir/Bundle/$validate",
    tags=["Submissions"],
    summary="Validate and score a bundle",
    description=(
        "The FHIR $validate operation. POST the Bundle as the body and it is "
        "checked, scored and recorded; the response is the OperationOutcome. "
        "The profile is the European Patient Summary unless the bundle says "
        "otherwise: another EHDS category is detected from the bundle, or you "
        "can name that category's profile in `profile` to be certain. Which "
        "one was used comes back as X-Document-Type; see $documents. The score "
        "is in the response headers rather than the body, because $validate is "
        "defined to return an OperationOutcome and nothing else. 413 if the "
        "bundle is larger than MAX_BODY_BYTES, 502 if Matchbox cannot be "
        "reached."
    ),
)
def validate_bundle(
    body: bytes = Depends(bundle_body),
    team: sqlite3.Row = Depends(current_team),
    profile: str | None = None,
) -> Response:
    # $validate takes an optional profile. Honouring an arbitrary one would let
    # a team pick a profile their bundle already satisfies and score full marks
    # on it, so the accepted values are exactly the ones this event scores
    # against - one per EHDS category. Naming one settles the category outright,
    # which is the escape hatch for a document detect_document() reads wrongly.
    requested = None
    if profile is not None:
        requested = next(
            (d for d in EHDS_DOCUMENTS if d["profile"] == profile), None
        )
        if requested is None:
            raise HTTPException(
                400,
                detail="This server validates against %s only"
                % ", ".join(d["profile"] for d in EHDS_DOCUMENTS),
            )
    try:
        bundle = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        # RecursionError: json rejects deeply nested input by blowing the stack,
        # which is still the client's bad payload, not a server fault.
        raise HTTPException(400, detail="Body is not valid JSON")
    if not isinstance(bundle, dict):
        raise HTTPException(400, detail="Body must be a FHIR Bundle JSON object")

    # Decided before validation, because it chooses the profile and the packages
    # the bundle is checked against.
    doc = requested or detect_document(bundle)
    try:
        outcome = validate_with_matchbox(body, doc)
    except MatchboxError as exc:
        raise HTTPException(502, detail=str(exc))

    # Filtered before anything counts it, so the verdict, the stored counts and
    # the findings the team reads are the same list. Dropping a finding from the
    # display but not from the count would fail a bundle for something we chose
    # not to check, and leave the banner asking them to fix errors it will not
    # show them.
    outcome = drop_uncheckable_terminology(outcome)

    counts = {"fatal": 0, "error": 0, "warning": 0, "information": 0}
    issues = outcome.get("issue")
    for issue in issues if isinstance(issues, list) else []:
        sev = issue.get("severity") if isinstance(issue, dict) else None
        if sev in counts:
            counts[sev] += 1
    errors = counts["fatal"] + counts["error"]
    passed = errors == 0
    sections, resources = count_bundle_richness(bundle)
    score = submission_score(passed, sections, resources, doc)

    # Audit log: the raw bytes, kept only for attempts that got a verdict, so
    # a validator outage does not leave files with no submission row behind.
    # The random suffix keeps two attempts in the same microsecond apart, and
    # "xb" refuses to overwrite should the clock ever repeat itself.
    now = datetime.now(timezone.utc)
    team_dir = os.path.join(DATA_DIR, "submissions", team["slug"])
    os.makedirs(team_dir, exist_ok=True)
    filename = "%s_%s.json" % (now.strftime("%Y%m%dT%H%M%S_%fZ"), secrets.token_hex(3))
    with open(os.path.join(team_dir, filename), "xb") as fh:
        fh.write(body)

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # The bonus is for the first company to pass *the track's own document*.
        # A lab report cannot claim it - it is the patient summary prize, and
        # handing it to whoever happened to bring the easiest other category
        # would make it the opposite of what it is for.
        if passed and doc["primary"]:
            conn.execute(
                "UPDATE teams SET first_pass = 1 WHERE id = ? "
                "AND NOT EXISTS (SELECT 1 FROM teams WHERE first_pass = 1)",
                (team["id"],),
            )
        cur = conn.execute(
            "INSERT INTO submissions (team_id, ts, pass, errors, warnings, infos, "
            "score, sections, resources, doc_type, filename) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                team["id"],
                now.isoformat(),
                int(passed),
                errors,
                counts["warning"],
                counts["information"],
                score,
                sections,
                resources,
                doc["id"],
                filename,
            ),
        )
        submission_id = cur.lastrowid
        # Read back inside the same transaction as the insert, so both include
        # this submission and two attempts landing together cannot miss each
        # other.
        # Counted against the registry rather than as "anything that is not
        # eps", so a doc_type the service no longer recognises cannot earn a
        # bonus here. A category can be retired - the discharge report was -
        # and its rows outlive it: reset.py only wipes daily until
        # PRACTICE_ENDS, and backup.py restores whatever it snapshotted. The
        # leaderboard has always counted this way (see _leaderboard_rows), and
        # the two disagreeing by 15 points is exactly the banner-against-board
        # contradiction the X-Score header exists to avoid.
        secondary_ids = [d["id"] for d in SECONDARY_DOCUMENTS]
        standing = conn.execute(
            "SELECT COALESCE(MAX(CASE WHEN doc_type = ? THEN score END), 0) AS best, "
            "COUNT(DISTINCT CASE WHEN doc_type IN (%s) THEN doc_type END) AS secondary "
            "FROM submissions WHERE team_id = ? AND pass = 1"
            % ",".join("?" * len(secondary_ids)),
            (PRIMARY_DOCUMENT["id"], *secondary_ids, team["id"]),
        ).fetchone()
        best = standing["best"] or 0
        secondary = standing["secondary"] or 0
        # Read here rather than from `team`: current_team() selects the columns
        # authentication needs, and the bonus may have been won by an earlier
        # submission of this same team.
        held = conn.execute(
            "SELECT first_pass FROM teams WHERE id = ?", (team["id"],)
        ).fetchone()
        holds_first_pass = bool(held and held["first_pass"])

    # The team's standing after this submission, not this bundle's own points.
    # The two used to be the same number; with the secondary categories in play
    # they are not, and the header feeds a banner that sits next to a
    # leaderboard showing the team total. Reporting the bundle alone made
    # submitting a lab report look like the score had collapsed.
    effective_score = team_score(best, holds_first_pass, secondary)
    record_usage(monitor.record_event, "submission", team["slug"],
                 doc=doc["id"], passed=passed, score=score, errors=errors)
    return fhir_response(
        outcome,
        # Content-Location is FHIR's own way of saying "what you just sent is
        # readable here". Without it a team has to search their own history to
        # find the id of the bundle they are holding in their hand.
        **{
            "Content-Location": "%s/Bundle/%d" % (FHIR_BASE, submission_id),
            "X-Pass": "true" if passed else "false",
            "X-Score": str(effective_score),
            "X-Errors": str(errors),
            "X-Sections": str(sections),
            "X-Resources": str(resources),
            # This bundle's own points, before any team bonus. X-Score is the
            # standing; without this the banner cannot show its arithmetic.
            "X-Submission-Score": str(score),
            # Which category the bundle was taken to be, and so which profile
            # the findings below came from. A team that meant to send a lab
            # report and sees "eps" here has its answer before reading one
            # finding: the bundle never claimed what it was.
            "X-Document-Type": doc["id"],
            "X-Document-Profile": doc["profile"],
            # Non-EPS categories passed, which is what the bonus is paid on.
            "X-Secondary-Categories": str(secondary),
            # The summary half of the standing, on its own. The banner needs to
            # tell a company that has only ever sent other categories that it
            # has no summary yet, and 0 here is the only thing that says so -
            # X-Score alone cannot distinguish "no summary" from "a summary
            # that scored nothing", and a summary that passed never scores
            # nothing.
            "X-Best-Patient-Summary": str(best),
            # The other term in the standing. Without it the banner's
            # arithmetic silently loses 20 points for whoever passed first.
            "X-First-Pass": "true" if holds_first_pass else "false",
        },
    )


def _leaderboard_rows() -> list[dict]:
    with connect() as conn:
        teams = conn.execute(
            "SELECT id, name, slug, first_pass FROM teams"
        ).fetchall()
        subs = conn.execute(
            "SELECT team_id, ts, pass, errors, score, sections, resources, doc_type "
            "FROM submissions ORDER BY ts"
        ).fetchall()

    by_team: dict[int, list[sqlite3.Row]] = {}
    for sub in subs:
        by_team.setdefault(sub["team_id"], []).append(sub)

    rows = []
    for team in teams:
        team_subs = by_team.get(team["id"], [])
        first_pass = bool(team["first_pass"])
        if team_subs:
            # The standing rests on the best patient summary, so that is what
            # picks the headline submission and fills the richness columns.
            primary_subs = [
                s for s in team_subs if s["doc_type"] == PRIMARY_DOCUMENT["id"]
            ]
            # Highest-scoring submission of the two sets; its timestamp is the
            # moment the team first achieved its current best score (used as
            # ranking tiebreak, so re-submitting never hurts).
            best = min(primary_subs or team_subs, key=lambda s: (-s["score"], s["ts"]))
            best_primary = max((s["score"] for s in primary_subs), default=0)
            # Registry order, not submission order, so the column reads the same
            # for every team however they got there.
            secondary = [
                d["id"]
                for d in SECONDARY_DOCUMENTS
                if any(s["pass"] and s["doc_type"] == d["id"] for s in team_subs)
            ]
            score = team_score(best_primary, first_pass, len(secondary))
            rows.append(
                {
                    "organization": team["name"],
                    "organizationSlug": team["slug"],
                    "score": score,
                    "passed": any(s["pass"] for s in team_subs),
                    "attempts": len(team_subs),
                    # Across every category: this is "did anything come back
                    # clean", a progress signal rather than a term of the
                    # score, and a passing lab report really is 0 errors.
                    "bestErrors": min(s["errors"] for s in team_subs),
                    # Summary-scoped, and absent when there is no summary. A
                    # lab report's 21 resources under a column that means "how
                    # rich is your patient summary" is not comparable with the
                    # row above it, and the status and category chips already
                    # say the team submitted, so empty cannot be misread as
                    # never having tried.
                    "bestSections": best["sections"] if primary_subs else None,
                    "bestResources": best["resources"] if primary_subs else None,
                    # Absent rather than false for a team with no summary yet:
                    # "has not passed the track's document" is the thing the
                    # column is for, and 0 would read as a passing empty one.
                    "bestPatientSummary": best_primary if primary_subs else None,
                    "secondaryCategories": secondary,
                    "lastSubmission": team_subs[-1]["ts"],
                    "firstPass": first_pass,
                    "_best_ts": best["ts"],
                }
            )
        else:
            rows.append(
                {
                    "organization": team["name"],
                    "organizationSlug": team["slug"],
                    "score": 0,
                    "passed": False,
                    "attempts": 0,
                    "bestErrors": None,
                    "bestSections": None,
                    "bestResources": None,
                    "bestPatientSummary": None,
                    "secondaryCategories": [],
                    "lastSubmission": None,
                    "firstPass": first_pass,
                    "_best_ts": "9999",  # sorts after any real ISO timestamp
                }
            )

    # Score desc, then whoever reached their best score first, then name.
    # Attempts and last submission are deliberately not tiebreaks: trying
    # again must never hurt a team's rank.
    rows.sort(
        key=lambda r: (-r["score"], r["_best_ts"], r["organization"].casefold())
    )
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
        del row["_best_ts"]
    return rows


@app.get("/fhir/$leaderboard",
    tags=["Submissions"],
    summary="Current standings",
    description=(
        "One `entry` parameter per registered company, best score first. "
        "Companies that have registered but never submitted appear at the "
        "bottom with no score. This is an operation rather than a search "
        "because a ranking is computed on the way out - it is not a resource "
        "the server stores, and pretending otherwise would mean inventing one."
    ),
)
def leaderboard() -> Response:
    return fhir_response(
        parameters([("entry", row) for row in _leaderboard_rows()])
    )


# ---------------------------------------------------------------- submissions API


def _loads_or_none(raw: bytes):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def safe_summary(bundle) -> dict:
    """summarize_bundle, but a bundle the summariser chokes on reads as empty.

    The summariser is defensive, but it walks arbitrary client JSON and the
    verdict beside it is the record that matters; a corner case in the
    readable view must not take the whole detail endpoint down.
    """
    try:
        return summarize_bundle(bundle)
    except Exception:  # noqa: BLE001 - anything at all, by design
        return summarize_bundle(None)


def _submission_row(sub_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT s.*, t.name AS team_name, t.slug AS team_slug "
            "FROM submissions s JOIN teams t ON t.id = s.team_id WHERE s.id = ?",
            (sub_id,),
        ).fetchone()


@app.get("/fhir/$submissions",
    tags=["Submissions"],
    summary="Submission history",
    description=(
        "Every company's attempts, newest first, or one company's with "
        "`organization`. One `entry` parameter per attempt."
    ),
)
def list_submissions(organization: str | None = None, count: int = 100) -> Response:
    count = max(1, min(count, 1000))
    query = (
        "SELECT s.id, t.name AS team, t.slug AS team_slug, s.ts, s.pass, "
        "s.errors, s.warnings, s.score, s.sections, s.resources, s.doc_type "
        "FROM submissions s JOIN teams t ON t.id = s.team_id "
    )
    params: tuple = ()
    if organization:
        query += "WHERE t.slug = ? "
        params = (organization,)
    query += "ORDER BY s.ts DESC, s.id DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(query, params + (count,)).fetchall()
    return fhir_response(
        parameters(
            [
                (
                    "entry",
                    {
                        "bundle": "%s/Bundle/%d" % (FHIR_BASE, r["id"]),
                        "id": r["id"],
                        "organization": r["team"],
                        "organizationSlug": r["team_slug"],
                        "recorded": r["ts"],
                        "pass": bool(r["pass"]),
                        "score": r["score"],
                        "errors": r["errors"],
                        "warnings": r["warnings"],
                        "sections": r["sections"],
                        "resources": r["resources"],
                        "documentType": r["doc_type"],
                    },
                )
                for r in rows
            ]
        )
    )


# ----------------------------------------------------------------- sample API

# Synthetic worked examples of the Nordic Hackathon 2026 scenarios. They are
# generated by samples/generate.py to exercise the clinician views (nil-known assertions, stale
# data, a non-English document) and do not come from any real system, record
# or person.
# scenario_url points at the organisers' brief on the event's resources page
# (https://nordichackathon2026.blogs.dsv.su.se/resources/). The briefs are the
# source these bundles were authored from, so they are linked rather than
# paraphrased here - a paraphrase would be a second, drifting copy.
SAMPLES = [
    {
        "id": "andrea-cross-border",
        "name": "Andrea Lindqvist, 24",
        "tagline": "Type 1 diabetes, seen in a Paris emergency department",
        "scenario": (
            "A Swedish exchange student is brought into a Paris emergency "
            "department with runaway blood sugar. Her summary is "
            "complete and well coded, but the data behind it has gone stale: "
            "the last labs are eight months old and the follow-up that would "
            "have refreshed them was cancelled and never rebooked."
        ),
        "language": "English",
        "scenario_name": "The Andrea Scenario",
        "scenario_url":
            "https://drive.google.com/file/d/1hzOACA3chu7E6aknBFni-hZWDJwvVGHx/view",
    },
    {
        "id": "arne-complex-care",
        "name": "Arne Bergström, 83",
        "tagline": "Multimorbid, admitted after a fall in Copenhagen",
        "scenario": (
            "A Swedish pensioner falls while visiting his son in Denmark and "
            "needs surgery. Ten conditions, ten medicines and a high-criticality "
            "penicillin anaphylaxis have to survive the border."
        ),
        "language": "Swedish",
        "scenario_name": "The Arne Scenario",
        "scenario_url":
            "https://drive.google.com/file/d/11dMseBg1439zSf2v8sRnWHSjrE18ZgLJ/view",
    },
    {
        "id": "ville-difficult-airway",
        "name": "Vilhelm \"Ville\" Söderberg, 13",
        "tagline": "Difficult airway and immunosuppressed, in a Gothenburg emergency department",
        "scenario": (
            "A Stockholm boy with a rare craniofacial syndrome, a known "
            "difficult airway and juvenile spondyloarthritis on adalimumab and "
            "methotrexate arrives at Sahlgrenska with a chest infection and "
            "worsening breathing. The team that has never met him needs the "
            "airway plan, the last anaesthetic, the last doses and who to "
            "ring at Karolinska - before anyone reaches for a laryngoscope."
        ),
        "language": "Swedish",
        "scenario_name": "The Ville Scenario",
        "scenario_url":
            "https://drive.google.com/file/d/1_PzonjLJ-BjW1sQYdL6YY1dS04T8Kkpp/view",
    },
]

SAMPLE_DISCLAIMER = (
    "Synthetic example, generated for the hackathon scenarios - it does not "
    "come from any real system, record or person, and only illustrates how "
    "such a summary could look."
)


def _sample_meta(sample_id: str) -> dict | None:
    return next((s for s in SAMPLES if s["id"] == sample_id), None)


def read_sample_bytes(sample_id: str) -> bytes | None:
    if _sample_meta(sample_id) is None:
        return None
    path = os.path.join(SAMPLES_DIR, sample_id + ".json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


SE_PERSONNUMMER = "http://electronichealth.se/identifier/personnummer"
SE_SAMORDNINGSNUMMER = "http://electronichealth.se/identifier/samordningsnummer"
# The label the page prints in front of the number: a samordningsnummer looks
# like a personnummer (day + 60) and must not be presented as one.
SE_IDENTIFIER_LABELS = {
    SE_PERSONNUMMER: "Personnummer",
    SE_SAMORDNINGSNUMMER: "Samordningsnummer",
}


def sample_personnummer(raw: bytes | None) -> str | None:
    """The patient's personnummer as it is written on paper, YYYYMMDD-NNNN.
    Read off the bundle rather than kept in SAMPLES, so the page cannot
    disagree with the document it is describing."""
    return (sample_identifier(raw) or (None, None))[1]


def sample_identifier(raw: bytes | None) -> tuple[str, str] | None:
    """(label, number) of the Swedish identity number in the bundle - a
    personnummer or, for a patient without one, a samordningsnummer."""
    try:
        entries = json.loads(raw or b"").get("entry", [])
    except (ValueError, AttributeError):
        return None
    for entry in entries:
        resource = entry.get("resource") or {}
        if resource.get("resourceType") != "Patient":
            continue
        for ident in resource.get("identifier", []):
            value = ident.get("value") or ""
            label = SE_IDENTIFIER_LABELS.get(ident.get("system"))
            if label and len(value) == 12:
                return (label, value[:8] + "-" + value[8:])
    return None


@app.get("/fhir/$samples",
    tags=["Samples"],
    summary="List the worked examples",
    description=(
        "Synthetic documents for the event scenarios. No real patient data. "
        "Each `entry` carries the scenario it illustrates and the Bundle URL "
        "to fetch it from."
    ),
)
def list_samples() -> Response:
    return fhir_response(
        parameters(
            [("disclaimer", SAMPLE_DISCLAIMER)]
            + [
                (
                    "entry",
                    {
                        "id": sample["id"],
                        "bundle": "%s/Bundle/%s" % (FHIR_BASE, sample["id"]),
                        "name": sample["name"],
                        "personnummer": sample_personnummer(
                            read_sample_bytes(sample["id"])),
                        "identifierLabel": (sample_identifier(
                            read_sample_bytes(sample["id"])) or ("", ""))[0] or None,
                        "tagline": sample["tagline"],
                        "scenario": sample["scenario"],
                        "scenarioName": sample["scenario_name"],
                        "scenarioUrl": sample["scenario_url"],
                        "language": sample["language"],
                        "available": read_sample_bytes(sample["id"]) is not None,
                    },
                )
                for sample in SAMPLES
            ]
        )
    )


def resolve_bundle(bundle_id: str) -> tuple[bytes, str] | None:
    """(bytes, download filename) for a Bundle id, or None if there is none.

    Submissions and samples share one id space, which is safe because they
    cannot collide: a submission id is the decimal row id and a sample id is a
    slug, which always carries a letter. One space means a partner's document
    and a worked example are fetched exactly the same way.
    """
    if bundle_id.isdigit():
        row = _submission_row(int(bundle_id))
        if row is None:
            return None
        raw = read_submission_bytes(row["team_slug"], row["filename"])
        if raw is None:
            return None
        # The category is in the name so a folder of downloads stays sortable
        # once a team has sent more than patient summaries.
        return raw, "%s-%s-%s.json" % (row["team_slug"], bundle_id, row["doc_type"])
    raw = read_sample_bytes(bundle_id)
    if raw is None:
        return None
    return raw, "%s-example-eps.json" % bundle_id


@app.get("/fhir/Bundle/{bundle_id}",
    tags=["Submissions"],
    summary="Read a document",
    description=(
        "The exact bytes that were submitted, unmodified - or, for a worked "
        "example, the example itself. Submitted documents and samples share "
        "one id space, so anything the service holds is fetched the same way."
    ),
)
def read_bundle(bundle_id: str) -> Response:
    found = resolve_bundle(bundle_id)
    if found is None:
        raise HTTPException(404, detail="Unknown Bundle")
    raw, filename = found
    return bundle_response(raw, filename)


# ---------------------------------------------------------------- interop API


@app.get("/fhir/$exchange",
    tags=["Interop"],
    summary="Documents published for exchange",
    description=(
        "Each company's best valid submission is published automatically. The "
        "list is open - the documents are the event's shared test corpus. "
        "`name` keeps the companies with that anywhere in their name or their "
        "slug, ignoring case and accents - `name:contains` is the same thing, "
        "for anyone arriving from the Organization search. Each entry's "
        "`organizationSlug` is what Organization/{slug}/$document takes."
    ),
)
def exchange_list(
    name: list[str] | None = Query(None),
    name_contains: list[str] | None = Query(None, alias="name:contains"),
) -> Response:
    wanted = (name or []) + (name_contains or [])
    entries = []
    with connect() as conn:
        teams = conn.execute(
            "SELECT id, name, slug FROM teams ORDER BY name COLLATE NOCASE"
        ).fetchall()
        for team in teams:
            # The slug is matched as well as the name: it is what every other
            # URL here is built from, so it is what people have on hand.
            if not (
                name_matches(team["name"], contains=wanted)
                or name_matches(team["slug"], contains=wanted)
            ):
                continue
            best = best_valid_submission(conn, team["id"])
            if best is None:
                continue
            entries.append(
                (
                    "entry",
                    {
                        "organization": team["name"],
                        "organizationSlug": team["slug"],
                        "bundle": "%s/Bundle/%d" % (FHIR_BASE, best["id"]),
                        "id": best["id"],
                        "recorded": best["ts"],
                        "sections": best["sections"],
                        "resources": best["resources"],
                        "renderings": conn.execute(
                            "SELECT COUNT(*) FROM renderings WHERE submission_id = ?",
                            (best["id"],),
                        ).fetchone()[0],
                    },
                )
            )
    return fhir_response(parameters(entries))


@app.get("/fhir/Organization/{slug}/$document",
    tags=["Interop"],
    summary="Fetch a company's published document",
    description=(
        "The company's best valid submission, ready to load into your own "
        "system. An instance operation rather than a search because the answer "
        "is one document and it changes the moment they beat their own score."
    ),
)
def exchange_fetch(slug: str) -> Response:
    with connect() as conn:
        producer = conn.execute(
            "SELECT id, name, slug FROM teams WHERE slug = ?", (slug,)
        ).fetchone()
        if producer is None:
            raise HTTPException(404, detail="Unknown company")
        best = best_valid_submission(conn, producer["id"])
    if best is None:
        raise HTTPException(404, detail="Company has no valid submission yet")
    raw = read_submission_bytes(producer["slug"], best["filename"])
    if raw is None:
        raise HTTPException(404, detail="Exchange document file missing")
    return bundle_response(
        raw,
        "%s-eps.json" % producer["slug"],
        **{
            "Content-Location": "%s/Bundle/%d" % (FHIR_BASE, best["id"]),
            "X-Producer": producer["slug"],
        },
    )


# ------------------------------------------------------------ renderings

# What another system made of a partner's document, as a picture. Interop
# testing otherwise leaves nothing behind: the defect is described across a
# table and by the evening nobody can point at what was actually seen. A
# screenshot is evidence rather than a claim, which is the one thing the
# self-reported "receipts" that used to live here could never be. Still
# unscored, for the reason everything here is unscored - the moment a picture
# is worth points you get fourteen pictures of the same screen.


def sniff_image(raw: bytes) -> tuple[str, str] | None:
    """(mime, extension) for these bytes, or None if they are not an image.

    The declared Content-Type is deliberately not consulted. An upload is
    whatever its bytes say it is, so a file that is really HTML can never be
    stored here and then served back as a page from this origin.
    """
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


# The producer is not stored on the rendering: it is whoever submitted the
# document, so a team renaming cannot orphan the attribution.
RENDERING_SELECT = """
    SELECT r.id, r.consumer_id, r.submission_id, r.ts, r.note, r.filename,
           r.mime, c.name AS consumer_name, c.slug AS consumer_slug,
           p.name AS producer_name, p.slug AS producer_slug
    FROM renderings r
    JOIN teams c       ON c.id = r.consumer_id
    JOIN submissions s ON s.id = r.submission_id
    JOIN teams p       ON p.id = s.team_id
"""


def documentreference_resource(row: sqlite3.Row) -> dict:
    """One screenshot as a DocumentReference.

    author is the company that took the picture, custodian is the company whose
    document it is, and context.related points at the exact Bundle that was
    opened - `subject` cannot be used for that, because R4 restricts it to a
    Patient or similar and the thing being rendered here is a document.
    """
    return {
        "resourceType": "DocumentReference",
        "id": str(row["id"]),
        "status": "current",
        "date": row["ts"],
        "description": row["note"] or None,
        "author": [{
            "reference": "Organization/%s" % row["consumer_slug"],
            "display": row["consumer_name"],
        }],
        "custodian": {
            "reference": "Organization/%s" % row["producer_slug"],
            "display": row["producer_name"],
        },
        "content": [{
            "attachment": {
                "contentType": row["mime"],
                "url": "%s/Binary/%d" % (FHIR_BASE, row["id"]),
            }
        }],
        "context": {
            "related": [{"reference": "Bundle/%d" % row["submission_id"]}]
        },
    }


def fetch_rendering(conn: sqlite3.Connection, rendering_id: int) -> sqlite3.Row | None:
    return conn.execute(
        RENDERING_SELECT + " WHERE r.id = ?", (rendering_id,)
    ).fetchone()


def read_rendering_bytes(consumer_slug: str, filename: str) -> bytes | None:
    path = os.path.join(DATA_DIR, "renderings", consumer_slug, filename)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def owned_rendering(
    conn: sqlite3.Connection, rendering_id: int, team: sqlite3.Row
) -> sqlite3.Row:
    """The rendering, if this team is the one that uploaded it."""
    row = fetch_rendering(conn, rendering_id)
    if row is None:
        raise HTTPException(404, detail="Unknown rendering")
    if row["consumer_id"] != team["id"]:
        raise HTTPException(403, detail="That screenshot belongs to another company")
    return row


# base64 inflates by four bytes for every three, and a DocumentReference
# carries a little JSON around the attachment. The decoded image is what
# MAX_IMAGE_BYTES governs; this is only the envelope it arrives in.
MAX_DOCREF_BODY_BYTES = MAX_IMAGE_BYTES * 4 // 3 + 8192


async def docref_body(
    request: Request, team: sqlite3.Row = Depends(current_team)
) -> dict:
    """The uploaded DocumentReference, read only once the uploader is known."""
    raw = await read_limited_body(request, MAX_DOCREF_BODY_BYTES, "Screenshot")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, detail="Body is not valid JSON")
    if not isinstance(payload, dict) or payload.get("resourceType") != "DocumentReference":
        raise HTTPException(400, detail="Body must be a FHIR DocumentReference")
    return payload


def _reference_id(ref, expected_type: str) -> str | None:
    """The logical id out of a {"reference": "Type/id"} element."""
    if not isinstance(ref, dict):
        return None
    value = ref.get("reference")
    if not isinstance(value, str):
        return None
    prefix = expected_type + "/"
    return value[len(prefix):] if value.startswith(prefix) else None


def _attachment_bytes(payload: dict) -> bytes:
    content = payload.get("content")
    first = content[0] if isinstance(content, list) and content else None
    attachment = first.get("attachment") if isinstance(first, dict) else None
    data = attachment.get("data") if isinstance(attachment, dict) else None
    if not isinstance(data, str):
        raise HTTPException(
            400,
            detail="content[0].attachment.data must carry the image, "
            "base64-encoded",
        )
    try:
        # validate=True so stray characters are an error rather than being
        # silently skipped into bytes that are not the picture anyone uploaded.
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        raise HTTPException(400, detail="attachment.data is not valid base64")
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(
            413,
            detail="Screenshot too large (%d bytes); limit is %d bytes"
            % (len(raw), MAX_IMAGE_BYTES),
        )
    return raw


@app.post("/fhir/DocumentReference",
    tags=["Interop"],
    status_code=201,
    summary="Publish a screenshot of a partner's document in your system",
    description=(
        "Load a partner's document into your own system, screenshot what it "
        "looks like, and POST a DocumentReference whose "
        "`content[0].attachment.data` is the image bytes base64-encoded - PNG, "
        "JPEG or WebP. `custodian` names the company whose document it is, and "
        "`description` is where you say what the picture shows. `author` is "
        "taken from your token, not from the body. Nothing about this is "
        "scored. 400 if the attachment is not one of those three formats or "
        "the document is your own, 413 over MAX_IMAGE_BYTES."
    ),
)
def add_rendering(
    payload: dict = Depends(docref_body),
    consumer: sqlite3.Row = Depends(current_team),
) -> Response:
    image = _attachment_bytes(payload)
    kind = sniff_image(image)
    if kind is None:
        raise HTTPException(
            400, detail="Attachment must be a PNG, JPEG or WebP image"
        )
    mime, ext = kind
    slug = _reference_id(payload.get("custodian"), "Organization")
    if not slug:
        raise HTTPException(
            400,
            detail="custodian must reference the company whose document this "
            "is, as Organization/<slug>",
        )
    note = payload.get("description")
    note = note if isinstance(note, str) else ""

    with connect() as conn:
        producer = conn.execute(
            "SELECT id, name, slug FROM teams WHERE slug = ?", (slug,)
        ).fetchone()
        if producer is None:
            raise HTTPException(404, detail="Unknown company")
        if producer["id"] == consumer["id"]:
            raise HTTPException(
                400,
                detail="That is your own document - a rendering is what "
                "another system made of it",
            )
        # Pinned to the submission, not the slug: a team's exchange document
        # changes the moment it beats its own score, and a screenshot has to
        # stay attached to the bytes somebody actually opened.
        best = best_valid_submission(conn, producer["id"])
        if best is None:
            raise HTTPException(404, detail="Company has no valid submission yet")

    now = datetime.now(timezone.utc)
    dest_dir = os.path.join(DATA_DIR, "renderings", consumer["slug"])
    os.makedirs(dest_dir, exist_ok=True)
    filename = "%s_%s.%s" % (
        now.strftime("%Y%m%dT%H%M%S_%fZ"), secrets.token_hex(3), ext
    )
    with open(os.path.join(dest_dir, filename), "xb") as fh:
        fh.write(image)

    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO renderings (consumer_id, submission_id, ts, note, "
            "filename, mime) VALUES (?, ?, ?, ?, ?, ?)",
            (consumer["id"], best["id"], now.isoformat(),
             note.strip()[:MAX_NOTE_CHARS], filename, mime),
        )
        row = fetch_rendering(conn, cur.lastrowid)
    resource = documentreference_resource(row)
    return fhir_response(
        resource,
        status=201,
        Location="%s/DocumentReference/%s" % (FHIR_BASE, resource["id"]),
    )


@app.get("/fhir/DocumentReference",
    tags=["Interop"],
    summary="Screenshots of documents in other systems",
    description=(
        "Newest first, as a searchset. Filter by `related` (a Bundle id), by "
        "`custodian` (the company whose document it is) or by `author` (the "
        "company that took the screenshot). Unfiltered it is every screenshot "
        "the event has collected."
    ),
)
def list_renderings(
    related: int | None = None,
    custodian: str | None = None,
    author: str | None = None,
) -> Response:
    where, params = [], []
    if related is not None:
        where.append("r.submission_id = ?")
        params.append(related)
    if custodian:
        where.append("p.slug = ?")
        params.append(custodian)
    if author:
        where.append("c.slug = ?")
        params.append(author)
    sql = RENDERING_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.ts DESC, r.id DESC"
    with connect() as conn:
        rows = [documentreference_resource(row) for row in conn.execute(sql, params)]
    return fhir_response(searchset(rows))


@app.get("/fhir/DocumentReference/{rendering_id}",
    tags=["Interop"],
    summary="One screenshot's metadata",
)
def read_rendering(rendering_id: int) -> Response:
    with connect() as conn:
        row = fetch_rendering(conn, rendering_id)
    if row is None:
        raise HTTPException(404, detail="Unknown DocumentReference")
    return fhir_response(documentreference_resource(row))


@app.get("/fhir/Binary/{rendering_id}",
    tags=["Interop"],
    summary="The screenshot itself",
    description=(
        "The image bytes, served as the type its own bytes proved. FHIR serves "
        "a Binary as its own content type when that is what the client asked "
        "for, which is what lets an <img> point straight at it."
    ),
)
def rendering_image(rendering_id: int) -> Response:
    with connect() as conn:
        row = fetch_rendering(conn, rendering_id)
    if row is None:
        raise HTTPException(404, detail="Unknown Binary")
    raw = read_rendering_bytes(row["consumer_slug"], row["filename"])
    if raw is None:
        raise HTTPException(404, detail="Rendering image file missing")
    return Response(
        content=raw,
        media_type=row["mime"],
        headers={
            # This is a participant's upload served from the same origin as
            # everyone's token. It is pinned to the type its bytes proved and
            # given a policy that keeps it inert whatever a browser decides it
            # is looking at.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": "inline",
            # Already compressed. GZipMiddleware goes by size alone, so saying
            # the encoding out loud is what stops it spending CPU for nothing.
            "Content-Encoding": "identity",
            # The bytes behind an id never change, only ever disappear.
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


@app.put("/fhir/DocumentReference/{rendering_id}",
    tags=["Interop"],
    summary="Rewrite the note on a screenshot you uploaded",
    description=(
        "`description` is where the uploader says what to look at - which "
        "section came through wrong, what the picture is meant to show. It can "
        "be rewritten as often as you like; only the company that uploaded the "
        "screenshot may do it. Everything else in the body is ignored: the "
        "image, the companies and the document it points at are settled at "
        "upload and are not the note's to change."
    ),
)
def update_rendering(
    rendering_id: int,
    payload: dict = Depends(docref_body),
    team: sqlite3.Row = Depends(current_team),
) -> Response:
    if "description" not in payload:
        raise HTTPException(400, detail="Body must carry a \"description\"")
    note = payload["description"]
    if not isinstance(note, str):
        raise HTTPException(400, detail="\"description\" must be a string")
    with connect() as conn:
        owned_rendering(conn, rendering_id, team)
        conn.execute(
            "UPDATE renderings SET note = ? WHERE id = ?",
            (note.strip()[:MAX_NOTE_CHARS], rendering_id),
        )
        return fhir_response(
            documentreference_resource(fetch_rendering(conn, rendering_id))
        )


@app.delete("/fhir/DocumentReference/{rendering_id}",
    tags=["Interop"],
    status_code=204,
    summary="Withdraw a screenshot you uploaded",
    description="Only the company that uploaded it can remove it; 403 otherwise.",
)
def delete_rendering(
    rendering_id: int, team: sqlite3.Row = Depends(current_team)
) -> Response:
    with connect() as conn:
        row = owned_rendering(conn, rendering_id, team)
        conn.execute("DELETE FROM renderings WHERE id = ?", (rendering_id,))
    # A withdrawn screenshot that is still on disk has not been withdrawn.
    try:
        os.remove(
            os.path.join(DATA_DIR, "renderings", row["consumer_slug"], row["filename"])
        )
    except OSError:
        pass
    # 204 is what FHIR returns for a delete: no body, and repeating it is not
    # an error the second time.
    return Response(status_code=204)


# Canonicals for the operations this server defines itself. They do not
# resolve to an OperationDefinition anywhere - CapabilityStatement requires a
# canonical for every operation, and a URL in our own namespace is at least
# unambiguous about who coined it.
CANONICAL_BASE = "https://nordichackathon2026.blogs.dsv.su.se/fhir"

# name -> the one-line description that goes in $status and the docs. Kept
# beside the CapabilityStatement so a new operation cannot be added to one and
# forgotten in the other.
SERVER_OPERATIONS = [
    ("register", "Register a company and get its token"),
    ("leaderboard", "Current standings"),
    ("submissions", "Submission history"),
    ("samples", "The worked examples for the event scenarios"),
    ("exchange", "Documents published for interop testing"),
    ("documents", "The document categories this validator checks"),
    ("status", "Validator health and the service's limits"),
]


# ------------------------------------------------------------ endpoints

# The other direction from $exchange. Downloading a partner's document tests
# whether you can read what they produce; a company whose platform would
# rather be sent documents says where here - its FHIR base, or a page that
# explains how to get data in. An Endpoint is only that announcement. Nothing
# here calls the address: it is for people to try by hand.
ENDPOINT_CONNECTION_SYSTEM = (
    "http://terminology.hl7.org/CodeSystem/endpoint-connection-type")
ENDPOINT_PAYLOAD_SYSTEM = "http://terminology.hl7.org/CodeSystem/endpoint-payload-type"
# R4 has no Endpoint.description; R5 does, and the cross-version extension is
# how an R4 server carries an R5 element.
ENDPOINT_DESCRIPTION_EXT = (
    "http://hl7.org/fhir/5.0/StructureDefinition/extension-Endpoint.description")
# code -> (system, display). connectionType is extensibly bound in R4 and its
# value set has nothing for "a page that tells a person what to do", so that
# one code is coined here.
ENDPOINT_KINDS = {
    "hl7-fhir-rest": (ENDPOINT_CONNECTION_SYSTEM, "HL7 FHIR"),
    "web-page": (CANONICAL_BASE + "/CodeSystem/endpoint-connection-type", "Web page"),
}
# Per company: a FHIR base, the page about it, and room for a staging box
# and a colleague's laptop. Anything past that is a list nobody reads.
MAX_ENDPOINTS = 5
MAX_ENDPOINT_NAME_CHARS = 100
MAX_ADDRESS_CHARS = 2000

ENDPOINT_SELECT = """
    SELECT e.*, t.name AS team_name, t.slug AS team_slug
    FROM endpoints e JOIN teams t ON t.id = e.team_id
"""


def parse_endpoint(payload: dict) -> dict:
    """The columns of an endpoint out of an Endpoint resource, or a 400 that
    says which element to fix."""
    status = payload.get("status")
    if status not in (None, "active"):
        raise HTTPException(
            400, detail="status must be active - to stop advertising an "
                        "endpoint, DELETE it")

    coding = payload.get("connectionType")
    code = coding.get("code") if isinstance(coding, dict) else None
    kind = ENDPOINT_KINDS.get(code) if isinstance(code, str) else None
    if kind is None:
        raise HTTPException(
            400, detail="connectionType.code must be one of %s"
                        % ", ".join(ENDPOINT_KINDS))
    if coding.get("system") not in (None, kind[0]):
        raise HTTPException(
            400, detail="connectionType.system for %s is %s, or leave it out"
                        % (code, kind[0]))

    address = payload.get("address")
    try:
        parts = urllib.parse.urlsplit(address) if isinstance(address, str) else None
        host = parts.hostname if parts else None
    except ValueError:  # an unclosed [ in an IPv6 host
        parts = host = None
    # The address is drawn as a link on the page, so anything but http(s) -
    # javascript: above all - is refused here rather than escaped there.
    if (parts is None or parts.scheme not in ("http", "https") or not host
            or any(c.isspace() or not c.isprintable() for c in address)):
        raise HTTPException(
            400, detail="address must be an absolute http or https URL")
    if len(address) > MAX_ADDRESS_CHARS:
        raise HTTPException(
            400, detail="address is longer than %d characters" % MAX_ADDRESS_CHARS)

    name = payload.get("name")
    name = " ".join(name.split()) if isinstance(name, str) else ""
    # Endpoint.header is where a client would put credentials for whoever
    # calls the address. Nobody here calls it and everybody can read it.
    if "header" in payload:
        raise HTTPException(
            400, detail="Endpoint.header is not kept - this list is public. Say "
                        "how to authenticate in the %s extension instead"
                        % ENDPOINT_DESCRIPTION_EXT)
    # R5 has Endpoint.description; R4 does not, and an R4 parser would drop
    # it. Saying so beats storing an endpoint without the instructions.
    if "description" in payload:
        raise HTTPException(
            400, detail="Endpoint has no description in R4 - send it as the "
                        "%s extension's valueString" % ENDPOINT_DESCRIPTION_EXT)
    descriptions = _extension_values(payload, ENDPOINT_DESCRIPTION_EXT,
                                     "valueString", "extension")
    if len(descriptions) > 1 or not all(isinstance(d, str) for d in descriptions):
        raise HTTPException(
            400, detail="The description extension takes one valueString")
    description = descriptions[0].strip() if descriptions else ""
    return {
        "kind": code,
        "name": name[:MAX_ENDPOINT_NAME_CHARS],
        "address": address,
        "description": description[:MAX_NOTE_CHARS],
    }


def endpoint_resource(row: sqlite3.Row) -> dict:
    system, display = ENDPOINT_KINDS[row["kind"]]
    resource = {
        "resourceType": "Endpoint",
        "id": str(row["id"]),
        "meta": {"lastUpdated": row["updated_at"]},
        "status": "active",
        "connectionType": {"system": system, "code": row["kind"],
                           "display": display},
        "managingOrganization": {
            "reference": "Organization/%s" % row["team_slug"],
            "display": row["team_name"],
        },
        # 1..* in R4. A page takes nothing; a FHIR endpoint takes whatever its
        # description says, which no code here could say better.
        "payloadType": [{"coding": [
            {"system": ENDPOINT_PAYLOAD_SYSTEM, "code": "none", "display": "None"}
            if row["kind"] == "web-page" else
            {"system": ENDPOINT_PAYLOAD_SYSTEM, "code": "any", "display": "Any"}
        ]}],
        "address": row["address"],
    }
    if row["name"]:
        resource["name"] = row["name"]
    if row["description"]:
        resource["extension"] = [{"url": ENDPOINT_DESCRIPTION_EXT,
                                  "valueString": row["description"]}]
    return resource


def insert_endpoint(conn: sqlite3.Connection, team_id: int, fields: dict) -> int:
    """Store what parse_endpoint() returned; the new endpoint's id."""
    return conn.execute(
        "INSERT INTO endpoints (team_id, updated_at, kind, name, address, "
        "description) VALUES (?, ?, ?, ?, ?, ?)",
        (team_id, datetime.now(timezone.utc).isoformat(), fields["kind"],
         fields["name"], fields["address"], fields["description"]),
    ).lastrowid


def fetch_endpoint(conn: sqlite3.Connection, endpoint_id: int) -> sqlite3.Row | None:
    # An id past what SQLite can hold is no endpoint, rather than an
    # OverflowError on binding it.
    if abs(endpoint_id) >= 2 ** 63:
        return None
    return conn.execute(ENDPOINT_SELECT + " WHERE e.id = ?", (endpoint_id,)).fetchone()

def _extension_values(element, url: str, key: str, name: str) -> list:
    """Every value of one extension on an element.

    An element that is there but is not the shape of one - the `extension`
    wrapper forgotten, so it is a bare list - is a 400 and not "no
    extensions": read leniently, a value written that way would be silently
    dropped. An extension with some other url is somebody else's and passes
    over.
    """
    if element is None:
        return []
    extensions = element.get("extension", []) if isinstance(element, dict) else None
    if not isinstance(extensions, list) \
            or not all(isinstance(ext, dict) for ext in extensions):
        raise HTTPException(
            400, detail="%s must be an element with an extension list, e.g. "
                        "{\"extension\": [{\"url\": \"%s\", ...}]}" % (name, url))
    return [ext.get(key) for ext in extensions if ext.get("url") == url]


def owned_endpoint(
    conn: sqlite3.Connection, endpoint_id: int, team: sqlite3.Row
) -> sqlite3.Row:
    """The endpoint, if this company is the one advertising it. It is public,
    so somebody else's is a 403 and not a 404."""
    row = fetch_endpoint(conn, endpoint_id)
    if row is None:
        raise HTTPException(404, detail="Unknown Endpoint")
    if row["team_id"] != team["id"]:
        raise HTTPException(403, detail="That endpoint belongs to another company")
    return row


async def endpoint_body(
    request: Request, team: sqlite3.Row = Depends(current_team)
) -> dict:
    """The Endpoint resource, read only once the caller is known."""
    raw = await read_limited_body(request, MAX_JSON_BODY_BYTES, "Endpoint")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, detail="Body is not valid JSON")
    if not isinstance(payload, dict) or payload.get("resourceType") != "Endpoint":
        raise HTTPException(400, detail="Body must be a FHIR Endpoint")
    return payload


@app.post("/fhir/Endpoint",
    tags=["Interop"],
    status_code=201,
    summary="Advertise where your system takes documents in",
    description=(
        "Tell the other companies how to send data straight to your system, "
        "to complement them downloading documents from $exchange. "
        "`connectionType.code` is `hl7-fhir-rest` for a FHIR base or "
        "`web-page` for a page that explains how; `address` is the http(s) URL, `name` a short "
        "label, and the R5 Endpoint.description extension is where to say "
        "what to send and how to authenticate. `managingOrganization` is "
        "taken from your token. The server never calls the address. 400 for "
        "an address that is not an http(s) URL, a connectionType other than "
        "these two or a status other than active; 422 over the per-company "
        "limit."
    ),
)
def create_endpoint(
    payload: dict = Depends(endpoint_body),
    team: sqlite3.Row = Depends(current_team),
) -> Response:
    fields = parse_endpoint(payload)
    with connect() as conn:
        # The count and the insert see the same table, or two simultaneous
        # creates both find room for the last one.
        conn.execute("BEGIN IMMEDIATE")
        held = conn.execute(
            "SELECT COUNT(*) FROM endpoints WHERE team_id = ?", (team["id"],)
        ).fetchone()[0]
        if held >= MAX_ENDPOINTS:
            raise HTTPException(
                422, detail="A company can advertise %d endpoints - delete one, "
                            "or PUT over it" % MAX_ENDPOINTS)
        row = fetch_endpoint(conn, insert_endpoint(conn, team["id"], fields))
    return fhir_response(
        endpoint_resource(row),
        status=201,
        Location="%s/Endpoint/%d" % (FHIR_BASE, row["id"]),
    )


@app.get("/fhir/Endpoint",
    tags=["Interop"],
    summary="Where companies take documents in",
    description=(
        "Every advertised endpoint, by company name. Open to read: the point "
        "of one is that the rest of the room finds it. `organization` narrows "
        "it to one company's slug, `connection-type` to `hl7-fhir-rest` or "
        "`web-page`; both take a comma-separated list."
    ),
)
def search_endpoints(
    organization: str | None = None,
    connection_type: str | None = Query(None, alias="connection-type"),
) -> Response:
    # A reference search parameter takes the bare id or Type/id; a token one
    # may carry its system before a |, which is dropped rather than checked.
    slugs = {v.removeprefix("Organization/") for v in organization.split(",")} \
        if organization else None
    kinds = {v.rpartition("|")[2] for v in connection_type.split(",")} \
        if connection_type else None
    with connect() as conn:
        rows = conn.execute(
            ENDPOINT_SELECT + " ORDER BY t.name COLLATE NOCASE, e.id"
        ).fetchall()
    return fhir_response(searchset([
        endpoint_resource(r) for r in rows
        if (slugs is None or r["team_slug"] in slugs)
        and (kinds is None or r["kind"] in kinds)
    ]))


@app.get("/fhir/Endpoint/{endpoint_id}",
    tags=["Interop"],
    summary="One advertised endpoint",
)
def read_endpoint(endpoint_id: int) -> Response:
    with connect() as conn:
        row = fetch_endpoint(conn, endpoint_id)
    if row is None:
        raise HTTPException(404, detail="Unknown Endpoint")
    return fhir_response(endpoint_resource(row))


@app.put("/fhir/Endpoint/{endpoint_id}",
    tags=["Interop"],
    summary="Replace an endpoint you advertise",
    description=(
        "The body replaces the endpoint whole, as in POST. Only the company "
        "advertising it may; 403 otherwise."
    ),
)
def update_endpoint(
    endpoint_id: int,
    payload: dict = Depends(endpoint_body),
    team: sqlite3.Row = Depends(current_team),
) -> Response:
    if payload.get("id") not in (None, str(endpoint_id)):
        raise HTTPException(
            400, detail="The body's id is %r but the URL's is %d"
                        % (payload["id"], endpoint_id))
    fields = parse_endpoint(payload)
    with connect() as conn:
        # The ownership check and the write see the same row, or a DELETE
        # landing between them leaves nothing to read back.
        conn.execute("BEGIN IMMEDIATE")
        owned_endpoint(conn, endpoint_id, team)
        conn.execute(
            "UPDATE endpoints SET updated_at = ?, kind = ?, name = ?, "
            "address = ?, description = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), fields["kind"],
             fields["name"], fields["address"], fields["description"],
             endpoint_id),
        )
        return fhir_response(endpoint_resource(fetch_endpoint(conn, endpoint_id)))


@app.delete("/fhir/Endpoint/{endpoint_id}",
    tags=["Interop"],
    status_code=204,
    summary="Stop advertising an endpoint",
    description="Only the company advertising it can remove it; 403 otherwise.",
)
def delete_endpoint(
    endpoint_id: int, team: sqlite3.Row = Depends(current_team)
) -> Response:
    with connect() as conn:
        owned_endpoint(conn, endpoint_id, team)
        conn.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))
    return Response(status_code=204)


def matchbox_status() -> tuple[str, list[str]]:
    """(state, missing packages) - `up`, `degraded` or `down`."""
    matchbox = "down"
    try:
        with urllib.request.urlopen(
            matchbox_health_url(), timeout=MATCHBOX_PROBE_TIMEOUT
        ) as resp:
            if resp.status == 200:
                matchbox = "up"
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
    ):
        pass
    missing: list[str] = []
    if matchbox == "up" and VALIDATION_IGS:
        loaded = loaded_ig_packages()
        if loaded is not None:
            missing = [ig for ig in VALIDATION_IGS if not _ig_loaded(ig, loaded)]
            if missing:
                # Alive, validating, and quietly not checking what we asked
                # for. Recreate the container after editing application.yaml.
                matchbox = "degraded"
    return matchbox, missing


@app.get("/fhir/metadata",
    tags=["Service"],
    summary="What this server can do",
    description=(
        "The CapabilityStatement. Every FHIR server has one at this path, so a "
        "client pointed at the base URL can discover the whole API without "
        "being told any of it."
    ),
)
def metadata() -> Response:
    return fhir_response(
        {
            "resourceType": "CapabilityStatement",
            "status": "active",
            "date": datetime.now(timezone.utc).date().isoformat(),
            "publisher": "Nordic Hackathon 2026",
            "kind": "instance",
            "software": {"name": app.title, "version": APP_VERSION},
            "implementation": {
                "description": "Nordic Hackathon 2026 EPS validation service",
                "url": FHIR_BASE,
            },
            "fhirVersion": "4.0.1",
            "format": [FHIR_JSON, "json"],
            "rest": [
                {
                    "mode": "server",
                    "security": {
                        "description": (
                            "Submitting, and anything that writes, needs the "
                            "token from $register, sent as "
                            "`Authorization: Bearer TOKEN` or "
                            "`X-Team-Token: TOKEN`. Reads are open."
                        )
                    },
                    "resource": [
                        {
                            "type": "Bundle",
                            # profile is the one this server is *about*;
                            # supportedProfile is the rest it will also check.
                            # CapabilityStatement draws exactly that
                            # distinction, so the demotion is expressed in the
                            # conformance resource and not only in the prose.
                            "profile": PRIMARY_DOCUMENT["profile"],
                            "supportedProfile": [
                                d["profile"] for d in SECONDARY_DOCUMENTS
                            ],
                            "interaction": [{"code": "read"}],
                            "operation": [
                                {
                                    "name": "validate",
                                    "definition":
                                        "http://hl7.org/fhir/OperationDefinition/"
                                        "Resource-validate",
                                    "documentation":
                                        "Validate against %s - or, for a bundle "
                                        "that is another EHDS category, against "
                                        "that category's profile - score the "
                                        "result and record the attempt. See "
                                        "$documents." % PRIMARY_DOCUMENT["profile"],
                                }
                            ],
                        },
                        {
                            "type": "Organization",
                            "interaction": [
                                {"code": "read"}, {"code": "search-type"}
                            ],
                            "searchParam": [
                                {"name": "name", "type": "string",
                                 "documentation":
                                     "The company's name; :contains and "
                                     ":exact are supported"},
                            ],
                            "operation": [
                                {
                                    "name": "document",
                                    "definition":
                                        CANONICAL_BASE
                                        + "/OperationDefinition/document",
                                    "documentation":
                                        "The company's best valid submission.",
                                },
                                {
                                    "name": "me",
                                    "definition":
                                        CANONICAL_BASE + "/OperationDefinition/me",
                                    "documentation":
                                        "Which company the calling token belongs to.",
                                },
                            ],
                        },
                        {
                            "type": "DocumentReference",
                            "interaction": [
                                {"code": "read"},
                                {"code": "search-type"},
                                {"code": "create"},
                                {"code": "update"},
                                {"code": "delete"},
                            ],
                            "searchParam": [
                                {"name": "related", "type": "reference",
                                 "documentation": "The Bundle that was rendered"},
                                {"name": "custodian", "type": "reference",
                                 "documentation": "Whose document it is"},
                                {"name": "author", "type": "reference",
                                 "documentation": "Who took the screenshot"},
                            ],
                        },
                        {"type": "Binary", "interaction": [{"code": "read"}]},
                        {
                            "type": "Endpoint",
                            "documentation":
                                "Where a company takes documents in directly. "
                                "connectionType is %s; the description rides "
                                "in %s. Open to read; a company writes only "
                                "its own." % (
                                    ", ".join("%s|%s" % (system, code)
                                              for code, (system, _)
                                              in ENDPOINT_KINDS.items()),
                                    ENDPOINT_DESCRIPTION_EXT),
                            "interaction": [
                                {"code": "read"},
                                {"code": "search-type"},
                                {"code": "create"},
                                {"code": "update"},
                                {"code": "delete"},
                            ],
                            "searchParam": [
                                {"name": "organization", "type": "reference",
                                 "definition": "http://hl7.org/fhir/"
                                               "SearchParameter/Endpoint-organization",
                                 "documentation": "The company advertising it"},
                                {"name": "connection-type", "type": "token",
                                 "definition": "http://hl7.org/fhir/SearchParameter/"
                                               "Endpoint-connection-type",
                                 "documentation": ", ".join(ENDPOINT_KINDS)},
                            ],
                        },
                    ],
                    "operation": [
                        {
                            "name": name,
                            "definition":
                                CANONICAL_BASE + "/OperationDefinition/" + name,
                            "documentation": doc,
                        }
                        for name, doc in SERVER_OPERATIONS
                    ],
                }
            ],
        }
    )


@app.get("/fhir/$documents",
    tags=["Service"],
    summary="The document categories this validator checks",
    description=(
        "One `entry` per document category, the patient summary first and "
        "flagged `primary` - it is the track, and the only category that earns "
        "richness points or the first-pass bonus. The rest are EHDS Article 14 "
        "categories the service also accepts, each validated against its own "
        "HL7 Europe guide and worth a flat bonus once. `ready` is false when "
        "Matchbox has not loaded that category's packages, which means bundles "
        "of that kind would be scored without their conformance being checked."
    ),
)
def documents() -> Response:
    loaded = loaded_ig_packages()
    entries = []
    for doc in EHDS_DOCUMENTS:
        entries.append(
            {
                "id": doc["id"],
                "label": doc["long_label"],
                "abbreviation": doc["short"],
                "ehdsCategory": doc["category"],
                "primary": doc["primary"],
                "profile": doc["profile"],
                "ig": list(doc["igs"]),
                "maturity": doc["maturity"],
                "spec": doc["spec"],
                "points": (
                    "best submission: %d + %d per section (max %d) + %d per "
                    "resource (max %d)"
                    % (VALID_BASE, SECTION_POINTS, SECTION_CAP, RESOURCE_POINTS,
                       RESOURCE_CAP)
                    if doc["primary"]
                    else "%d, once, the first time it passes"
                    % SECONDARY_CATEGORY_POINTS
                ),
                "detectedByBundleProfile": list(doc["bundles"]),
                "detectedByEntryProfile": list(doc.get("entry_profiles") or []),
                "detectedByCompositionType": list(doc["loinc"]),
                # None, not false, when Matchbox could not be asked: "we do not
                # know" and "we know it is not loaded" send an organiser to two
                # different places.
                "ready": None if loaded is None
                         else all(_ig_loaded(ig, loaded) for ig in doc["igs"]),
            }
        )
    return fhir_response(parameters([("entry", e) for e in entries]))


@app.get("/fhir/$status",
    tags=["Service"],
    summary="Validator health and the service's limits",
    description=(
        "Reports whether Matchbox is reachable (`up`), reachable but missing a "
        "package named in VALIDATION_IGS (`degraded` - profile claims from that "
        "package go unchecked), or unreachable (`down` - submissions fail). "
        "Also carries the bundle size limit and whether registration needs a "
        "code, for clients that want to warn before a doomed request. Separate "
        "from `metadata` on purpose: what the server can do does not change, "
        "and whether the validator is answering right now does."
    ),
)
def status() -> Response:
    matchbox, missing = matchbox_status()
    return fhir_response(
        parameters(
            [
                ("matchbox", matchbox),
                ("missingIg", missing),
                # The UI reads these so it can warn before a doomed request.
                ("maxBundleBytes", MAX_BODY_BYTES),
                ("maxImageBytes", MAX_IMAGE_BYTES),
                ("maxNoteChars", MAX_NOTE_CHARS),
                ("registerCodeRequired", bool(REGISTER_CODE)),
                ("profile", PROFILE),
                # Absent outside the practice period. The browser compares it
                # against its own clock rather than being told a yes/no here, so
                # the notice clears itself on a page nobody has reloaded since
                # before the final wipe.
                ("practiceEnds", PRACTICE_ENDS or None),
            ]
        )
    )


# ------------------------------------------------------------ page view model

# The clinician view is a rendering, not data: a patient line, sections, and
# the items under each one, shaped for the DOM that draws them. There is no
# FHIR resource for that - the closest honest answer is "read the Bundle and
# summarise it yourself" - so it is served from /ui, kept out of the
# CapabilityStatement and out of the OpenAPI document, and is explicitly not
# part of the API anyone should build against. The FHIR surface stays FHIR.


@app.get("/ui/documents/{doc_id}", include_in_schema=False)
def ui_document(doc_id: str) -> dict:
    """The readable view of one submitted document or worked example."""
    meta = _sample_meta(doc_id)
    if meta is not None:
        raw = read_sample_bytes(doc_id)
        if raw is None:
            raise HTTPException(404, detail="Unknown sample")
        return {
            "id": meta["id"],
            "team": meta["name"],
            "team_slug": meta["id"],
            "ts": None,
            "pass": True,
            "errors": 0,
            "warnings": 0,
            "score": None,
            "bundle_readable": True,
            "sample": dict(meta, disclaimer=SAMPLE_DISCLAIMER),
            "summary": safe_summary(_loads_or_none(raw)),
        }
    if not doc_id.isdigit():
        raise HTTPException(404, detail="Unknown document")
    row = _submission_row(int(doc_id))
    if row is None:
        raise HTTPException(404, detail="Unknown submission")
    raw = read_submission_bytes(row["team_slug"], row["filename"])
    if raw is None:
        raise HTTPException(404, detail="Submission file missing")
    bundle = _loads_or_none(raw)  # None: the summary degrades gracefully
    return {
        "id": row["id"],
        "team": row["team_name"],
        "team_slug": row["team_slug"],
        "ts": row["ts"],
        "pass": bool(row["pass"]),
        "errors": row["errors"],
        "warnings": row["warnings"],
        "score": row["score"],
        # The stored verdict stands even when the file no longer parses, so say
        # so explicitly: an empty summary otherwise reads as an empty document.
        "bundle_readable": bundle is not None,
        "summary": safe_summary(bundle),
    }


@app.post("/ui/preview", include_in_schema=False)
async def ui_preview(request: Request) -> dict:
    """The readable view of a bundle that is not on the hackathon at all.

    Nothing is stored, validated or scored - it is the clinical view pointed at
    a file on someone's own disk, so they can see how it reads before (or
    without ever) submitting it.
    """
    bundle = _loads_or_none(await read_limited_body(request, MAX_BODY_BYTES, "Bundle"))
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        raise HTTPException(400, detail="Not a FHIR Bundle in JSON")
    return {
        "id": "local",
        "team": "Local preview",
        "team_slug": None,
        "ts": None,
        "preview": True,
        "bundle_readable": True,
        "summary": safe_summary(bundle),
    }


@app.post("/ui/error", status_code=204, include_in_schema=False)
async def ui_error(request: Request) -> Response:
    """A JavaScript error on the page, reported by the page.

    Otherwise only whoever hit it ever knows. Open to anyone, like the page;
    the body is capped here, and monitor.record_error caps the rows per day,
    so the most a hostile client can do is fill the day's quota with noise.
    """
    report = _loads_or_none(await read_limited_body(request, 16 * 1024, "Error report"))
    if not isinstance(report, dict):
        raise HTTPException(400, detail="Not an error report")
    text = "\n".join(
        str(report[k])[:2000] for k in ("message", "stack", "page", "agent")
        if report.get(k)
    )
    await anyio.to_thread.run_sync(
        lambda: record_usage(monitor.record_error, "browser", text)
    )
    return Response(status_code=204)


@app.get("/favicon.svg", include_in_schema=False)
def favicon() -> Response:
    return Response(
        content=FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    return INDEX_HTML


# An ECG trace in the event's purple and yellow, on a 32-unit grid. Kept to a
# single QRS spike with a heavy stroke: extra peaks and a finer line both blur
# into an indistinct squiggle once the browser paints this at 16px.
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="7" fill="#503AA8"/>
<path d="M4.5 17.5h4L11 11l4 11 2.4-4.5h6.1" fill="none" stroke="#FFEE58"
stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


# ---------------------------------------------------------------- UI page

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>
/* Runs before first paint so a stored choice never flashes the other theme. */
(function () {
  try {
    var t = localStorage.getItem("theme");
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  } catch (e) { /* site data blocked - fall back to the system theme */ }
})();
/* Before the page's own script, and in a block of its own, so that a syntax
   error there is still caught here. The first few per page load go to the
   server, which mails the organiser; extensions' errors are not ours. */
(function () {
  var sent = 0;
  function report(message, stack) {
    stack = String(stack || "");
    if (sent >= 5 || /extension:\/\//.test(stack)) return;
    sent += 1;
    try {
      fetch("/ui/error", {
        method: "POST", keepalive: true,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: String(message), stack: stack,
          page: location.pathname + location.hash, agent: navigator.userAgent })
      }).catch(function () {});
    } catch (e) { /* nothing left to report it with */ }
  }
  window.addEventListener("error", function (e) {
    report(e.message, (e.error && e.error.stack) || (e.filename + ":" + e.lineno + ":" + e.colno));
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    report("Unhandled rejection: " + ((r && r.message) || r), r && r.stack);
  });
})();
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Manrope:wght@300;400;500;600;700&display=swap">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<title>Nordic Hackathon 2026</title>
<style>
/* Nordic Hackathon 2026 - palette, type scale and controls taken from the
   event site (nordichackathon2026.blogs.dsv.su.se), which runs WordPress's
   Twenty Twenty-Five: base #FFFFFF, contrast #111111, accent-3 #503AA8,
   accent-4 #686868, accent-5 #FBFAF3, accent-1 #FFEE58, set in Manrope with
   Fira Code for code, headings at 400 rather than bold, and nothing rounded.
   Semantic colours (pass/fail/warn) are deepened beyond the event palette so
   abnormal results stay obvious.
   Each token carries its light and dark value as a pair, so the two themes
   cannot drift apart; the theme picker only has to set color-scheme. */
:root { color-scheme: light dark; }
:root[data-theme="light"] { color-scheme: light; }
:root[data-theme="dark"] { color-scheme: dark; }
:root {
  --font-sans: "Manrope", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               Ubuntu, Cantarell, "Helvetica Neue", Arial, sans-serif;
  --font-mono: "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  /* The site's type scale, clamps and all, straight out of its theme.json. */
  --fs-medium: clamp(1rem, 1rem + ((1vw - 0.2rem) * 0.196), 1.125rem);
  --fs-large: clamp(1.125rem, 1.125rem + ((1vw - 0.2rem) * 0.392), 1.375rem);
  --fs-x-large: clamp(1.75rem, 1.75rem + ((1vw - 0.2rem) * 0.392), 2rem);
  --fs-xx-large: clamp(2.15rem, 2.15rem + ((1vw - 0.2rem) * 1.333), 3rem);
  --measure: 645px;              /* the site's reading column */
  --pad-x: clamp(20px, 5vw, 50px);  /* and its page gutter */
  /* Nothing on the site is rounded - not a button, not an image. */
  --r-card: 0px; --r-ctl: 0px;
  --bg: light-dark(#fbfaf3, #15141a);
  --card: light-dark(#ffffff, #1e1c26);
  --text: light-dark(#111111, #f3f1e7);
  --muted: light-dark(#5f5f5f, #a7a2b3);
  --border: light-dark(#d8d5c7, #322e3d);
  --field-border: light-dark(#8d8878, #6e6880);
  --accent: light-dark(#503aa8, #b9a8f7);
  --accent-soft: light-dark(#efebfa, #292340);
  --accent-fill: light-dark(#503aa8, #b9a8f7);
  --accent-on: light-dark(#ffffff, #15141a);
  /* Buttons there are contrast-on-base, not accent-coloured; in the dark
     theme the pair swaps over the way the site's dark style variation does. */
  --btn: light-dark(#111111, #f3f1e7);
  --btn-on: light-dark(#ffffff, #15141a);
  --pass: light-dark(#16704a, #58cf94);
  --pass-bg: light-dark(#e2f1e8, #123522);
  --fail: light-dark(#b3261e, #ff8a80);
  --fail-bg: light-dark(#fbe7e4, #401d1a);
  --warn: light-dark(#7d5300, #f0cb4e);
  --warn-bg: light-dark(#fbf0cf, #3a3110);
  --info: light-dark(#12628f, #6fc3ef);
  --info-bg: light-dark(#e2eff7, #10303f);
  --gold: light-dark(#6f5a00, #ffee58);
  --gold-bg: light-dark(#fdf6c4, #33300f);
  --silver: light-dark(#5f5f5f, #b6b1a6);
  --silver-bg: light-dark(#eeece2, #26242e);
  --bronze: light-dark(#8a4b1f, #d68f5f);
  --bronze-bg: light-dark(#f6e9dc, #30211a);
  --code-bg: light-dark(#f3f1e6, #131119);
}
/* Browsers without light-dark() (pre-2024) get the light palette rather than
   unparsed declarations and a page with no colours at all. */
@supports not (color: light-dark(#000, #fff)) {
  :root { --bg: #fbfaf3; --card: #ffffff; --text: #111111; --muted: #5f5f5f; --border: #d8d5c7; --field-border: #8d8878; --accent: #503aa8; --accent-soft: #efebfa; --accent-fill: #503aa8; --accent-on: #ffffff; --btn: #111111; --btn-on: #ffffff; --pass: #16704a; --pass-bg: #e2f1e8; --fail: #b3261e; --fail-bg: #fbe7e4; --warn: #7d5300; --warn-bg: #fbf0cf; --info: #12628f; --info-bg: #e2eff7; --gold: #6f5a00; --gold-bg: #fdf6c4; --silver: #5f5f5f; --silver-bg: #eeece2; --bronze: #8a4b1f; --bronze-bg: #f6e9dc; --code-bg: #f3f1e6; }
}
* { box-sizing: border-box; }
code, pre, textarea, .cv-mode, .tab-btn { font-variant-ligatures: none; }
a { color: var(--accent); text-underline-offset: 2px; }
a:hover { text-decoration: none; }   /* as on the site */
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 300 16px/1.5 var(--font-sans);
  letter-spacing: -0.1px;
}
/* Manrope 300 is the site's body weight, but it was chosen for 20px prose:
   small print and dense table rows need the extra stem to stay readable. */
table, input, textarea, select, .muted, label { font-weight: 400; }
main { max-width: 1060px; margin: 0 auto; padding: 26px var(--pad-x) 72px; }
header { max-width: 1060px; margin: 0 auto; padding: 20px var(--pad-x) 6px; }
/* The site's header is a single bar - bold site title left, navigation right -
   with the big, light hero heading underneath it. */
.head-row { display: flex; align-items: center; justify-content: space-between;
  gap: 14px 24px; flex-wrap: wrap; }
.site-title { margin: 0; font-size: var(--fs-medium); font-weight: 700; }
.site-title a { color: var(--text); text-decoration: none; }
.head-nav { display: flex; align-items: center; gap: 10px 22px; flex-wrap: wrap; }
/* One button, not three: it shows the mode currently in force and cycles
   system -> light -> dark on click. All three icons ship in the markup and the
   inactive two are `hidden`, so switching costs no request and no repaint of
   anything but this button. */
.theme-toggle { font: inherit; display: grid; place-items: center; flex: none;
  background: transparent; color: var(--muted);
  border: 1px solid var(--field-border); border-radius: var(--r-ctl);
  padding: 6px 10px; cursor: pointer; }
/* Icons carry no text, so they inherit the button's colour in every state. */
.theme-toggle svg { display: block; width: 15px; height: 15px; fill: none;
  stroke: currentColor; stroke-width: 1.6; stroke-linecap: round;
  stroke-linejoin: round; }
.theme-toggle svg[hidden] { display: none; }
.theme-toggle:hover { color: var(--accent); }
/* Hierarchy comes from size, not weight: the site sets every heading level at
   400 and gives them all the body's -0.1px tracking. */
h1, h2, h3, h4, h5, h6 { font-weight: 400; letter-spacing: -0.1px;
  line-height: 1.125; }
h1 { margin: clamp(28px, 5vw, 56px) 0 0; font-size: var(--fs-xx-large); }
.sub { margin: 14px 0 0; color: var(--muted); font-size: var(--fs-large);
  max-width: var(--measure); }
/* The profile link used to sit inside .sub, where the hero's --fs-large blew a
   six-word label up to the size of the sentence around it. Out on the right at
   body size it reads as a reference rather than a headline. Baseline alignment
   keeps it on the sentence's last line, and it wraps under on narrow screens. */
.sub-row { display: flex; align-items: baseline; justify-content: space-between;
  gap: 8px 24px; flex-wrap: wrap; }
.spec-link { flex: none; font-size: var(--fs-medium); color: var(--muted); }
.spec-link:hover { color: var(--accent); }
h2 { margin: 0 0 14px; font-size: var(--fs-large); }
/* Same shape as .sub-row: a heading with a quiet aside pushed to the far end,
   sharing its baseline and wrapping under it when the card gets narrow. The
   14px belongs to the row here, so the heading inside it gives up its margin
   rather than adding a second gap below the aside. */
.card-head { display: flex; align-items: baseline; justify-content: space-between;
  gap: 4px 24px; flex-wrap: wrap; margin: 0 0 14px; }
.card-head h2 { margin: 0; }
.practice-note { flex: none; margin: 0; font-size: var(--fs-medium);
  color: var(--muted); }
/* Explanatory prose gets the site's reading column; the tables and forms
   beside it keep the full width of the card. */
.card > p.muted { max-width: var(--measure); }
.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--r-card); padding: 26px 28px; margin-top: 22px;
}
/* Register above Submit rather than beside it: the submit card carries the
   bundle textarea, the findings table and the embedded document, and every one
   of them reads better with the page's full width. The cards space themselves,
   so this only has to stop them sharing a row. */
.submit-stack { display: block; }
/* The two interop pathways side by side, so neither reads as the footnote
   to the other; they stack where a phone has no room for two columns. */
.lanes { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 16px; }
.lane { min-width: 0; background: var(--code-bg); border: 1px solid var(--border);
  border-radius: var(--r-card); padding: 16px 18px; }
.lane h3 { margin: 2px 0 6px; font-size: 1.05rem; }
.lane p.muted { margin: 0 0 8px; }
.lane-n { font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--muted); }
@media (max-width: 640px) { .lanes { grid-template-columns: 1fr; } }
/* Their card spans the page now, but a company name, a token and a
   registration code are still short single values - left at 100% they would
   stretch a 16-character field across a metre of screen. */
#register-card .row, #reg-code-row, #token-box, #token, #reg-ep { max-width: 520px; }
#reg-ep select { width: auto; flex: none; }
#company-view { max-width: 640px; }
h3.company-sub { margin: 18px 0 4px; font-size: 0.95rem; }
#my-eps:empty::before { content: "Nothing yet - add an address below.";
  display: block; margin-top: 8px; color: var(--muted); font-size: 0.86rem; }
/* A button that reads as a link: switching views is navigation, not an action. */
button.linkish { background: none; border: 0; padding: 0; font: inherit;
  color: var(--accent); text-decoration: underline; cursor: pointer; }
button.linkish:hover { background: none; }
label { display: block; font-size: 0.83rem; color: var(--muted); margin: 12px 0 4px; }
input[type=text], textarea {
  width: 100%; padding: 12px; border: 1px solid var(--field-border);
  border-radius: var(--r-ctl); background: var(--bg); color: var(--text);
  font: inherit;
}
textarea { font-family: var(--font-mono);
  font-size: 0.82rem; min-height: 170px; resize: vertical; }
input:focus, textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
/* Square, near-black, roomy, and a hover that thins the fill instead of
   brightening it - the site's button, scaled down for a form. */
button {
  font: inherit; font-weight: 500; border: none; border-radius: var(--r-ctl);
  padding: 12px 28px; cursor: pointer; background: var(--btn);
  color: var(--btn-on); letter-spacing: -0.1px;
}
button:hover { background: color-mix(in srgb, var(--btn) 85%, transparent); }
button:focus-visible { outline: 2px solid var(--muted); outline-offset: 2px; }
button:disabled { opacity: 0.55; cursor: wait; }
button.ghost {
  background: transparent; color: var(--accent);
  border: 1px solid var(--field-border); font-weight: 400; padding: 5px 12px;
  font-size: 0.8rem;
}
button.ghost:hover { background: var(--accent-soft); }
table { width: 100%; border-collapse: collapse; font-size: 0.92rem; }
/* The site's smallest heading level: uppercase, bold, widely tracked. */
th { text-align: left; color: var(--muted); font-size: 0.76rem;
  text-transform: uppercase; letter-spacing: 0.1em; font-weight: 700;
  padding: 6px 10px; border-bottom: 1px solid var(--border); }
td { padding: 9px 10px; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
.table-wrap { overflow-x: auto; }
tr.rank-1 td { background: var(--gold-bg); }
tr.rank-2 td { background: var(--silver-bg); }
tr.rank-3 td { background: var(--bronze-bg); }
tr.rank-1 td:first-child { box-shadow: inset 3px 0 0 var(--gold); color: var(--gold); font-weight: 700; }
tr.rank-2 td:first-child { box-shadow: inset 3px 0 0 var(--silver); color: var(--silver); font-weight: 700; }
tr.rank-3 td:first-child { box-shadow: inset 3px 0 0 var(--bronze); color: var(--bronze); font-weight: 700; }
.badge { display: inline-block; padding: 3px 9px; border-radius: var(--r-ctl);
  font-size: 0.74rem; font-weight: 600; }
.badge.pass { color: var(--pass); background: var(--pass-bg); }
.badge.fail { color: var(--fail); background: var(--fail-bg); }
.chip { display: inline-block; padding: 2px 8px; border-radius: var(--r-ctl);
  font-size: 0.72rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.06em; }
.chip.error, .chip.fatal { color: var(--fail); background: var(--fail-bg); }
.chip.warning { color: var(--warn); background: var(--warn-bg); }
.chip.information { color: var(--info); background: var(--info-bg); }
.muted { color: var(--muted); font-size: 0.86rem; }
.token-box {
  display: none; margin-top: 14px; padding: 12px 14px; border-radius: var(--r-card);
  background: var(--accent-soft); border: 1px solid var(--border);
}
.token-box code { font-size: 1.05rem; font-weight: 700; letter-spacing: 0.04em;
  font-family: var(--font-mono); }
.banner { display: none; margin-top: 16px; padding: 14px 18px; border-radius: var(--r-card);
  font-size: var(--fs-large); font-weight: 500; }
.banner.pass { display: block; color: var(--pass); background: var(--pass-bg);
  border: 1px solid var(--pass); }
.banner.fail { display: block; color: var(--fail); background: var(--fail-bg);
  border: 1px solid var(--fail); }
.banner .score { float: right; }
/* The banner carries its scoring breakdown as a title, so say so. */
.banner[title] { cursor: help; }
/* Inline text with an explanation in its title. */
.hint { cursor: help; text-decoration: underline dotted; text-underline-offset: 3px; }
.spinner { display: none; align-items: center; gap: 10px; margin-top: 14px;
  color: var(--muted); }
.spinner.on { display: flex; }
.spinner .ring {
  width: 18px; height: 18px; border-radius: 50%; flex: none;
  border: 3px solid var(--border); border-top-color: var(--accent);
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important; }
}
.error-note { display: none; margin-top: 14px; padding: 10px 14px;
  border-radius: var(--r-card); color: var(--fail); background: var(--fail-bg);
  border: 1px solid var(--fail); font-size: 0.9rem; }
pre.curl {
  background: var(--code-bg); border: 1px solid var(--border);
  border-radius: var(--r-card); padding: 12px 14px; overflow-x: auto;
  font-size: 0.8rem; line-height: 1.5;
  font-family: var(--font-mono);
}
details summary { cursor: pointer; font-size: var(--fs-large);
  letter-spacing: -0.1px; }
details[open] summary { margin-bottom: 8px; }
.row { display: flex; gap: 10px; align-items: center; }
.row input { flex: 1; }
.file-row { margin-top: 10px; display: flex; gap: 10px; align-items: center;
  flex-wrap: wrap; }
input[type=file] { font-size: 0.84rem; color: var(--muted); }
.live-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: var(--pass); margin-left: 6px; vertical-align: middle;
  animation: pulse 2.5s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: 0.3; } }
#issues-wrap { display: none; margin-top: 14px; }
.issue-summary { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
/* The count chips on the left and one action for all of them on the right.
   The table itself is what a team reads, so nothing is added to it. */
.issue-toolbar { display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.issue-toolbar .issue-summary { margin-bottom: 0; }
.menu-wrap { position: relative; display: inline-block; }
/* Needed because the rule above is an author rule and outranks the UA sheet's
   [hidden] { display: none } - without this, wrap.hidden = true has no effect
   and last submission's Explain menu stays on screen beside a green banner. */
.menu-wrap[hidden] { display: none; }
.menu { position: absolute; z-index: 5; top: calc(100% + 4px); right: 0;
  min-width: 240px; background: var(--card); border: 1px solid var(--field-border);
  border-radius: var(--r-ctl); box-shadow: 0 2px 14px rgba(0, 0, 0, 0.22);
  padding: 4px; }
.menu[hidden] { display: none; }
.menu-head { padding: 5px 9px 6px; font-size: 0.68rem; letter-spacing: 0.1em;
  text-transform: uppercase; font-weight: 700; color: var(--muted);
  border-bottom: 1px solid var(--border); margin-bottom: 4px; }
.menu a, .menu button.menu-item { display: flex; align-items: baseline;
  justify-content: space-between; gap: 12px; width: 100%; padding: 6px 9px;
  font: inherit; font-size: 0.82rem; font-weight: 500; color: var(--text);
  text-decoration: none; background: transparent; border: 0; border-radius: 0;
  text-align: left; cursor: pointer; }
.menu a:hover, .menu button.menu-item:hover { background: var(--accent-soft);
  color: var(--accent); }
.menu .note { font-size: 0.68rem; font-weight: 400; color: var(--muted);
  white-space: nowrap; }
.menu a:hover .note, .menu button.menu-item:hover .note { color: var(--accent); }
.explain-note { margin: -2px 0 10px; font-size: 0.8rem; }
.explain-text { margin: 0 0 10px; font-size: 0.74rem; line-height: 1.5; }
.issues-scroll { max-height: 50vh; overflow: auto; border: 1px solid var(--border);
  border-radius: var(--r-ctl); }
table.issues { font-size: 0.82rem; }
table.issues thead th { position: sticky; top: 0; background: var(--card); z-index: 1; }
table.issues td { vertical-align: top; }
table.issues td.msg { overflow-wrap: anywhere; }
table.issues td.loc {
  font-family: var(--font-mono);
  font-size: 0.72rem; color: var(--muted); overflow-wrap: anywhere;
  max-width: 260px;
}
/* Without a floor the location collapses to the width of "line 698" and the
   path underneath wraps a syllable at a time. */
table.issues td.loc, table.issues th.loc-h { min-width: 190px; }
.loc-where { display: block; margin-top: 2px; overflow-wrap: anywhere; }
.count-badge { display: inline-block; font-size: 0.68rem; font-weight: 600;
  color: var(--muted); border: 1px solid var(--border); border-radius: var(--r-ctl);
  padding: 0 6px; margin-left: 4px; white-space: nowrap; }
/* One per other EHDS category a company has passed. Small and quiet on
   purpose: the column is a glance at breadth, and the score column - which the
   patient summary dominates - is what ranks anyone. */
.cat-chip { display: inline-block; font-size: 0.66rem; font-weight: 600;
  letter-spacing: 0.02em; text-transform: uppercase; color: var(--muted);
  background: var(--code-bg); border: 1px solid var(--border);
  border-radius: var(--r-ctl); padding: 1px 5px; white-space: nowrap;
  vertical-align: middle; }
/* The banner's own chip names the guide the findings below came from, so it
   carries the accent the banner does rather than receding like the table's. */
.banner .cat-chip { color: var(--accent); background: var(--accent-soft);
  margin-left: 6px; }
.profile-chip { display: inline-block; font-size: 0.66rem; color: var(--muted);
  background: var(--code-bg); border: 1px solid var(--border);
  border-radius: var(--r-ctl); padding: 0 6px; margin-left: 6px; white-space: nowrap;
  vertical-align: middle; }
details.issue-fold { margin-top: 10px; }
details.issue-fold summary { font-size: 0.86rem; font-weight: 600;
  color: var(--muted); }
details.issue-fold[open] summary { margin-bottom: 6px; }
.no-errors { color: var(--pass); font-weight: 600; margin: 4px 0; }
/* The submitted document, printed with each finding on the line it lands on.
   A validator location like Bundle.entry[9].resource.dosage[0] is only useful
   after somebody has counted entries by hand; this does the counting. */
#doc-wrap { margin-top: 12px; }
.doc-head { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;
  margin: 0 0 6px; }
.doc-head .muted { margin: 0; }
.doc-toggle { display: inline-flex; align-items: center; gap: 5px; margin: 0;
  font-size: 0.8rem; color: var(--muted); cursor: pointer; white-space: nowrap; }
.doc-toggle input { width: auto; margin: 0; }
.doc-scroll { position: relative; max-height: 60vh; overflow: auto;
  border: 1px solid var(--border); border-radius: var(--r-ctl);
  background: var(--code-bg); font-family: var(--font-mono);
  font-size: 0.72rem; line-height: 1.6; }
.jl, .jd { display: flex; }
.jl .ln, .jd .ln { flex: 0 0 4.2em; text-align: right; padding-right: 9px;
  color: var(--muted); opacity: 0.6; user-select: none;
  border-right: 1px solid var(--border); }
.jl .jc { padding-left: 10px; white-space: pre-wrap; overflow-wrap: anywhere; }
.jl.hit { background: var(--card); }
.jl.hit .ln { font-weight: 700; opacity: 1; }
.jl.fatal, .jl.error { box-shadow: inset 3px 0 0 var(--fail); }
.jl.warning { box-shadow: inset 3px 0 0 var(--warn); }
.jl.information { box-shadow: inset 3px 0 0 var(--info); }
.jl.flash { animation: jflash 1.6s ease-out; }
@keyframes jflash { from { background: var(--accent-soft); } }
.jd { padding-bottom: 3px; }
.jd .jt { padding-left: 10px; white-space: normal; overflow-wrap: anywhere;
  font-family: var(--font-sans); }
.jd.fatal .jt, .jd.error .jt { color: var(--fail); }
.jd.warning .jt { color: var(--warn); }
.jgap { padding: 4px 0 4px calc(4.2em + 19px); color: var(--muted);
  opacity: 0.7; font-style: italic; }
button.loc-jump { background: transparent; border: 0; padding: 0; margin: 0;
  font: inherit; font-weight: 600; white-space: nowrap; color: var(--accent);
  text-align: left; cursor: pointer; text-decoration: underline;
  text-underline-offset: 2px; }
button.loc-jump:hover { background: transparent; text-decoration-thickness: 2px; }
.copied { color: var(--pass) !important; border-color: var(--pass) !important; }
/* tabs - the site's header navigation: plain text, no boxes, the current
   page marked by weight and a rule rather than a filled pill */
.tabs { display: flex; gap: 4px 22px; flex-wrap: wrap; align-items: center; }
.tab-btn { display: inline-block; background: transparent; color: var(--muted);
  border: none; border-bottom: 2px solid transparent; text-decoration: none;
  cursor: pointer; padding: 2px 0; font-weight: 400;
  font-size: var(--fs-medium); letter-spacing: -0.1px; }
.tab-btn:hover { color: var(--text); }
.tab-btn.active { color: var(--text); font-weight: 700;
  border-bottom-color: var(--accent); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
select {
  width: 100%; padding: 12px; border: 1px solid var(--field-border);
  border-radius: var(--r-ctl); background: var(--bg); color: var(--text); font: inherit;
}
select:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
/* clinical view */
.patient-card { background: var(--accent-soft); border: 1px solid var(--border);
  border-radius: var(--r-card); padding: 14px 16px; margin: 12px 0 6px; }
.patient-name { font-size: var(--fs-large); }
h3.cv-sec { margin: 22px 0 4px; font-size: var(--fs-large); }
.cv-items { list-style: none; padding: 0; margin: 4px 0 10px; }
.cv-items li { padding: 8px 2px; border-bottom: 1px solid var(--border); }
.cv-items li:last-child { border-bottom: none; }
.cv-label { font-weight: 600; }
.cv-detail { font-size: 0.85rem; }
.cv-note, .tl-note { font-style: italic; }
.cv-note { font-size: 0.85rem; }
/* rows are links too, so a document can be opened in a new tab or bookmarked */
a.cv-open { color: inherit; text-decoration: none; }
tr.cv-row:hover a.cv-open { text-decoration: underline; }
.cv-status { display: inline-block; font-size: 0.68rem; font-weight: 600;
  color: var(--muted); border: 1px solid var(--border); border-radius: var(--r-ctl);
  padding: 0 6px; margin-left: 6px; vertical-align: middle; }
.warn-banner { padding: 10px 14px; border-radius: var(--r-card); color: var(--fail);
  background: var(--fail-bg); border: 1px solid var(--fail);
  margin: 0 0 14px; font-weight: 600; }
tr.cv-row { cursor: pointer; }
tr.cv-row:hover td { background: var(--accent-soft); }
/* previewing a bundle that is not on the hackathon */
#cv-preview-msg { margin: 8px 0 0; color: var(--fail); }
.cv-paste textarea { min-height: 120px; }
.cv-paste .row { display: flex; gap: 10px; align-items: center; margin-top: 10px; }
footer { max-width: 1060px; margin: 26px auto 40px; padding: 0 var(--pad-x);
  font-size: 0.8rem; color: var(--muted); line-height: 1.6; }
footer a { color: var(--accent); }
footer .colophon { margin-top: 14px; font-size: 0.72rem; }
footer .colophon a { color: inherit; }
/* example documents, listed in the same table as everyone else's submissions */
.cv-sub { font-size: 0.8rem; color: var(--muted); font-weight: 400; }
td.cv-scenario { max-width: 30rem; font-size: 0.8rem; line-height: 1.45;
  color: var(--muted); }
.sample-note { margin: 6px 0 0; font-size: 0.78rem; }
/* View switcher. It lives on the open document rather than in the picker card
   so the control sits next to what it changes: floated so the title flows
   beside it, sticky so it survives scrolling a long summary. */
.cv-modes { float: right; position: sticky; top: 12px; z-index: 2;
  display: flex; margin: 0 0 10px 18px; background: var(--card);
  border: 1px solid var(--field-border); border-radius: var(--r-ctl);
  overflow: hidden; }
.cv-mode { background: transparent; color: var(--muted); border: none;
  border-left: 1px solid var(--field-border); border-radius: 0;
  padding: 6px 13px; font-size: 0.82rem; font-weight: 600;
  letter-spacing: -0.1px; cursor: pointer; white-space: nowrap; }
.cv-mode:first-child { border-left: none; }
.cv-mode:hover { color: var(--accent); background: transparent; }
.cv-mode.active { background: var(--btn); color: var(--btn-on); }
/* Floating it here would leave the title a useless sliver, so it goes back to
   being a full-width row above the document. */
@media (max-width: 640px) {
  .cv-modes { float: none; position: static; margin: 0 0 12px; }
  .cv-mode { flex: 1; text-align: center; }
}
/* the float is the last thing in the card, so the card has to be told to
   contain it or it collapses behind the buttons */
#cv-doc::after { content: ""; display: block; clear: both; }
.chip.cvc { margin-left: 5px; text-transform: none; letter-spacing: 0; }
.chip.risk { color: var(--fail); background: var(--fail-bg); }
.chip.warn { color: var(--warn); background: var(--warn-bg); }
.chip.ok { color: var(--pass); background: var(--pass-bg); }
.chip.gap { color: var(--muted); background: var(--code-bg); border: 1px solid var(--border); }
.db-note { list-style: none; font-size: 0.8rem; color: var(--muted); margin: 0 0 6px; }
.cv-narrative p { margin: 2px 0 8px; font-size: 0.9rem; }
.db-prose p { margin: 4px 0 6px; font-size: 0.82rem; }
/* category colours, shared by timeline dots and legend */
.c-alert { --cat: #e3362b; }
.c-allergy { --cat: #d64545; }
.c-problem { --cat: #c07a10; }
.c-medication { --cat: #3f7fd6; }
.c-result { --cat: #1f9d6b; }
.c-procedure { --cat: #b45ab0; }
.c-immunization { --cat: #12a3a3; }
.c-device { --cat: #a05a2c; }
.c-plan { --cat: #7b7488; }
.c-other { --cat: #928d9b; }
.cdot { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
  background: var(--cat, var(--accent)); margin-right: 5px; vertical-align: middle; }
/* B - timeline */
.tl-legend { display: flex; flex-wrap: wrap; gap: 10px; font-size: 0.78rem;
  color: var(--muted); margin-top: 10px; }
.tl { position: relative; margin: 10px 0 0; padding-left: 22px; }
.tl::before { content: ""; position: absolute; left: 5px; top: 6px; bottom: 6px;
  width: 2px; background: var(--border); }
.tl-year { position: relative; font-weight: 700; font-size: 0.9rem; margin: 16px 0 4px;
  font-variant-numeric: tabular-nums; }
.tl-year::before { content: ""; position: absolute; left: -21px; top: 5px;
  width: 12px; height: 12px; border-radius: 50%; background: var(--muted);
  border: 2px solid var(--card); }
.tl-row { position: relative; padding: 7px 0; border-bottom: 1px solid var(--border); }
.tl-row:last-child { border-bottom: none; }
.tl-row::before { content: ""; position: absolute; left: -20px; top: 13px;
  width: 10px; height: 10px; border-radius: 50%; background: var(--cat, var(--accent));
  border: 2px solid var(--card); }
.tl-date { font-size: 0.74rem; color: var(--muted); font-variant-numeric: tabular-nums; }
.tl-label { font-weight: 600; }
.tl-detail { font-size: 0.83rem; color: var(--muted); }
.tl-undated { border: 1px dashed var(--warn); border-radius: var(--r-card); padding: 10px 14px;
  margin-top: 18px; background: var(--warn-bg); }
.tl-asserted { border: 1px dashed var(--pass); border-radius: var(--r-card);
  padding: 10px 14px; margin-top: 18px; background: var(--pass-bg); }
/* C - dashboard */
.db-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 12px; margin-top: 12px; }
.db-card { border: 1px solid var(--border); border-radius: var(--r-card); padding: 12px 14px;
  background: var(--bg); }
.db-card.alert { border-color: var(--fail); background: var(--fail-bg); }
.db-card.good { border-color: var(--pass); }
.db-card h4 { margin: 0 0 8px; font-size: 0.72rem; text-transform: uppercase;
  letter-spacing: 0.1em; font-weight: 700; color: var(--muted); }
.db-big { font-size: var(--fs-x-large); line-height: 1.15; }
.db-sub { font-size: 0.8rem; color: var(--muted); }
.db-list { list-style: none; margin: 6px 0 0; padding: 0; font-size: 0.87rem; }
.db-list li { padding: 4px 0; border-bottom: 1px solid var(--border); }
.db-list li:last-child { border-bottom: none; }
.db-more { font-size: 0.76rem; color: var(--muted); margin-top: 5px; }
.mt { margin-top: 9px; }
.mt-top { display: flex; justify-content: space-between; font-size: 0.78rem;
  font-weight: 600; }
.meter { height: 7px; border-radius: var(--r-ctl); background: var(--border);
  overflow: hidden; margin: 4px 0 0; }
.meter > span { display: block; height: 100%; background: var(--pass); }
.meter.mid > span { background: var(--warn); }
.meter.low > span { background: var(--fail); }
/* interop */
p.muted code { background: var(--code-bg); border: 1px solid var(--border);
  border-radius: var(--r-ctl); padding: 1px 5px; font-size: 0.8rem;
  font-family: var(--font-mono); }
.io-partner { border: 1px solid var(--border); border-radius: var(--r-card);
  padding: 14px 16px; margin-top: 12px; }
.io-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin-top: 10px; }
/* the upload form, folded away until a company says it has tested something */
.shot-form, .ep-form { margin-top: 12px; padding: 12px 14px;
  border: 1px dashed var(--border); border-radius: var(--r-card);
  background: var(--accent-soft); }
.shot-form textarea, .ep-form textarea, #reg-ep textarea { min-height: 64px;
  font-family: inherit; font-size: 0.86rem; }
.shot-form label { margin-top: 0; }
/* advertised endpoints: one block per company, one row per address */
.ep { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border);
  display: flex; flex-direction: column; gap: 4px; }
.ep-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
.ep-kind { font-size: 0.72rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--muted); }
.ep-addr { font-family: var(--font-mono); font-size: 0.84rem; overflow-wrap: anywhere; }
.ep-desc { margin: 2px 0 0; font-size: 0.86rem; white-space: pre-wrap;
  overflow-wrap: anywhere; }
.ep-form > label:first-of-type { margin-top: 10px; }
.shot-status { font-size: 0.82rem; margin: 8px 0 0; }
.shot-status.err { color: var(--fail); }
/* the gallery: one document, every system that rendered it, side by side */
.shots { display: grid; gap: 14px; margin-top: 14px;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
.shot { border: 1px solid var(--border); border-radius: var(--r-card);
  overflow: hidden; background: var(--bg); display: flex; flex-direction: column; }
.shot img { display: block; width: 100%; height: 168px; object-fit: cover;
  object-position: top; background: var(--code-bg); }
.shot-meta { padding: 9px 11px; display: flex; flex-direction: column; gap: 5px; }
.shot-who { font-weight: 600; font-size: 0.88rem; }
.shot-note { font-size: 0.84rem; white-space: pre-wrap; overflow-wrap: anywhere; }
.shot-note.empty { color: var(--muted); font-style: italic; }
.shot-own { display: flex; gap: 6px; margin-top: 2px; }
.shot-edit textarea { width: 100%; min-height: 62px; font-family: inherit;
  font-size: 0.84rem; }
.shot-err { font-size: 0.82rem; color: var(--fail); }
</style>
</head>
<body>
<header>
  <div class="head-row">
    <p class="site-title"><a href="/">Nordic Hackathon 2026</a></p>
    <div class="head-nav">
      <nav class="tabs">
        <a class="tab-btn active" data-tab="leaderboard" href="#leaderboard" aria-current="page">Leaderboard</a>
        <a class="tab-btn" data-tab="submit" href="#submit">Submit</a>
        <a class="tab-btn" data-tab="interop" href="#interop">Interop testing</a>
        <a class="tab-btn" data-tab="clinical" href="#clinical">Clinical view</a>
      </nav>
      <button class="theme-toggle" id="theme-btn" type="button">
        <svg data-icon="system" viewBox="0 0 16 16" aria-hidden="true">
          <rect x="1.8" y="2.8" width="12.4" height="8.4" rx="1.2"/>
          <path d="M5.5 14h5M8 11.2V14"/></svg>
        <svg data-icon="light" viewBox="0 0 16 16" aria-hidden="true" hidden>
          <circle cx="8" cy="8" r="3.1"/>
          <path d="M8 1.2v1.5M8 13.3v1.5M1.2 8h1.5M13.3 8h1.5M3.3 3.3l1.1 1.1M11.6
          11.6l1.1 1.1M12.7 3.3l-1.1 1.1M4.4 11.6l-1.1 1.1"/></svg>
        <svg data-icon="dark" viewBox="0 0 16 16" aria-hidden="true" hidden>
          <path d="M13.4 9.9A5.7 5.7 0 0 1 6.1 2.6a5.7 5.7 0 1 0 7.3 7.3z"/>
          </svg>
      </button>
    </div>
  </div>
  <h1>Nordic Hackathon 2026</h1>
  <div class="sub-row">
    <p class="sub">Submit your European Patient Summary (<span class="hint"
    title="HL7 Europe publishes the European Patient Summary for FHIR R4 only - there is no R5 edition, and the EHDS implementing act that will settle a FHIR version for Europe is not due until March 2027. R4 is what EPS, IPS and MyHealth@EU build on today.">FHIR
    R4 Bundle</span>) and get
    it validated against the HL7 EU EPS 1.0.0-ballot profile.</p>
    <a class="spec-link" href="https://hl7.eu/fhir/eps/1.0.0-ballot/"
       target="_blank" rel="noopener">EPS 1.0.0-ballot spec</a>
  </div>
</header>
<main>

<div id="tab-leaderboard" class="tab-panel active">

<section class="card">
  <div class="card-head">
    <h2>Leaderboard<span class="live-dot" title="auto-refreshes every 5s"></span></h2>
    <p class="practice-note" id="practice-note" hidden></p>
  </div>
  <div class="table-wrap">
  <table id="lb">
    <thead><tr>
      <th scope="col">#</th><th scope="col">Company</th><th scope="col">Score</th><th scope="col">Status</th>
      <th scope="col" title="Other EHDS document types this company has also passed">Also</th>
      <th scope="col">Sections</th><th scope="col">Resources</th><th scope="col">Errors (best)</th><th scope="col">Last submission</th>
    </tr></thead>
    <tbody id="lb-body"></tbody>
  </table>
  </div>
  <p class="muted" id="lb-empty" style="display:none">No companies yet - be the
  first to <a href="#submit">register</a>.</p>
</section>

<section class="card">
  <h2>How scoring works</h2>
  <p class="muted" style="margin:0">
    Fix conformance first, because any error or fatal validation issue scores
    your submission 0. A valid bundle earns 50, plus +10 per distinct
    Composition section that actually has entries (capped at +160), and +2 for
    every clinical resource outside the Composition and Patient (capped at
    +60). Sections only count once - repeating a section or padding the bundle
    will not make it richer. Your company score is your best patient summary,
    plus a one-time +20 "first pass" bonus for the first company to submit a
    valid one, plus +15 for each <a href="#other-documents">other EHDS document
    type</a> you also get passing. Interop testing is not scored.
  </p>
</section>

<details class="card" id="other-documents">
  <summary>Other EHDS documents we also validate<span class="count-badge"
    id="other-count">3</span></summary>
    <p class="muted" style="margin:10px 0">This is the patient summary track,
    and the summary is what the leaderboard ranks. But EHDS
    (<a href="https://eur-lex.europa.eu/eli/reg/2025/327/oj" target="_blank"
    rel="noopener">Regulation (EU) 2025/327</a>, Article 14) names six priority
    categories of health data that EHR systems have to exchange, so if you bring
    one of the others it is detected from the bundle and validated properly
    against its own HL7 Europe guide. Each one you pass is worth a flat
    <strong>+15</strong>.</p>
    <div class="table-wrap">
    <table id="doc-table">
      <thead><tr>
        <th scope="col">EHDS category</th><th scope="col">Guide</th>
        <th scope="col">Maturity</th>
      </tr></thead>
      <tbody id="doc-types-body"></tbody>
    </table>
    </div>
</details>

</div>

<div id="tab-submit" class="tab-panel">

<div class="submit-stack">
  <section class="card" id="register-card">
    <div id="company-view" hidden>
      <h2>Your company</h2>
      <p class="muted" style="margin:0">Registered as <strong
      id="company-name"></strong>. <button type="button" class="linkish"
      id="reg-another">Register a different company</button></p>
      <h3 class="company-sub">Where others can send you data</h3>
      <p class="muted" style="margin:0">Listed on the <a href="#interop">Interop
      tab</a>, so other companies can send their documents straight to you.</p>
      <div id="my-eps"></div>
      <div class="ep-form" id="ep-form">
        <strong id="ep-form-title">Add an address</strong>
        <label for="ep-kind">What it is</label>
        <select id="ep-kind">
          <option value="hl7-fhir-rest">FHIR server - send documents to it over REST</option>
          <option value="web-page">Web page - explains how to send data in</option>
        </select>
        <label for="ep-address">Address</label>
        <input type="text" id="ep-address" inputmode="url" autocomplete="off"
          spellcheck="false" placeholder="https://fhir.example.org/r4">
        <label for="ep-name">Label (optional)</label>
        <input type="text" id="ep-name" maxlength="100" placeholder="e.g. Staging server">
        <label for="ep-desc">How to send to it (optional)</label>
        <textarea id="ep-desc" maxlength="2000"
          placeholder="e.g. POST a document Bundle to /Bundle. Ask at our table for a token."></textarea>
        <div class="io-actions">
          <button id="ep-save">Add</button>
          <button class="ghost" id="ep-cancel" hidden>Cancel</button>
        </div>
        <p class="shot-status" id="ep-status" hidden></p>
      </div>
    </div>
    <div id="reg-view">
    <h2>Register your company</h2>
    <p class="muted" style="margin:0 0 4px">One registration per company -
    share the token with your colleagues.</p>
    <label for="reg-name">Company name</label>
    <div class="row">
      <input type="text" id="reg-name" maxlength="40" placeholder="e.g. FHIR Starters">
    </div>
    <div id="reg-code-row" hidden>
      <label for="reg-code">Registration code</label>
      <input type="text" id="reg-code" placeholder="from the organisers"
        autocomplete="off" spellcheck="false">
    </div>
    <div id="reg-ep">
      <label for="reg-ep-address">Where can others send data to your system?
      (optional)</label>
      <p class="muted" style="margin:0 0 6px">A FHIR server address, or a page
      that explains how. It is listed on the <a href="#interop">Interop tab</a>
      so other companies can send their documents straight to you.</p>
      <div class="row">
        <select id="reg-ep-kind" aria-label="What the address is">
          <option value="hl7-fhir-rest">FHIR server</option>
          <option value="web-page">Web page</option>
        </select>
        <input type="text" id="reg-ep-address" inputmode="url" autocomplete="off"
          spellcheck="false" placeholder="https://fhir.example.org/r4">
      </div>
      <label for="reg-ep-desc">How to send to it (optional)</label>
      <textarea id="reg-ep-desc" maxlength="2000"
        placeholder="e.g. POST a document Bundle to /Bundle. Ask at our table for a token."></textarea>
    </div>
    <!-- After the optional fields, so they are seen before the click that
         sends them. -->
    <div class="row" style="margin-top:12px"><button id="reg-btn">Register</button></div>
    <div class="error-note" id="reg-error" role="alert"></div>
    <div id="reg-back-wrap" hidden>
      <p class="muted" style="margin:10px 0 0">Registering replaces the token
      saved in this browser <strong id="reg-replace-name"></strong> - keep a
      copy first: <code id="reg-replace-token"></code></p>
      <p class="muted" style="margin:6px 0 0"><button type="button"
      class="linkish" id="reg-back">Back to your company</button></p>
    </div>
    </div>
    <div class="token-box" id="token-box">
      <div class="row" style="justify-content:space-between">
        <code id="token-value"></code>
        <button class="ghost" id="token-copy">Copy</button>
      </div>
      <p class="muted" style="margin:8px 0 0">Save this token, share it with
      company colleagues, and use it with every submission to identify your
      team.</p>
    </div>
  </section>

  <section class="card">
    <h2>Submit your EPS</h2>
    <label for="token">Company token</label>
    <input type="text" id="token" placeholder="paste your company token"
      autocomplete="off" spellcheck="false">
    <p class="shot-status err" id="token-note" role="status" hidden
      style="margin:4px 0 10px"></p>
    <label for="bundle">FHIR Bundle JSON (paste, or pick a file below)</label>
    <p class="muted" style="margin:-2px 0 6px">Bringing one of the
    <a href="#other-documents">other EHDS document types</a> instead? Send it
    here too - it is recognised from the bundle and checked against its own
    guide.</p>
    <textarea id="bundle" placeholder='{"resourceType": "Bundle", "type": "document", ...}'
      spellcheck="false"></textarea>
    <div class="file-row">
      <input type="file" id="file" accept=".json,application/json,application/fhir+json"
        aria-label="Pick a FHIR Bundle JSON file">
      <button id="submit-btn">Validate bundle</button>
    </div>
    <p class="muted" id="validator-note" role="status" style="margin:8px 0 0"></p>
    <div class="spinner" id="spinner"><span class="ring"></span>
      <span>Validating...</span></div>
    <div class="error-note" id="submit-error" role="alert"></div>
    <div class="banner" id="banner" role="status"></div>
    <div id="issues-wrap">
      <div class="issue-toolbar">
        <div class="issue-summary" id="issue-summary"></div>
        <span class="menu-wrap" id="explain-wrap" hidden>
          <button type="button" class="ghost" id="explain-btn" aria-haspopup="true"
            aria-expanded="false" aria-controls="explain-menu"></button>
          <div class="menu" id="explain-menu" hidden></div>
        </span>
      </div>
      <p class="muted explain-note" id="explain-note" role="status" hidden></p>
      <textarea id="explain-text" class="explain-text" readonly rows="8" hidden
        aria-label="The question to paste into an assistant"></textarea>
      <div id="issue-groups"></div>
      <details id="doc-wrap" class="issue-fold" hidden>
        <summary id="doc-summary">Embedded view</summary>
        <div class="doc-head">
          <span class="muted" id="doc-note"></span>
          <label class="doc-toggle"><input type="checkbox" id="doc-flagged">
            Flagged lines only</label>
        </div>
        <div class="doc-scroll" id="doc-body"></div>
      </details>
    </div>
  </section>
</div>

<details class="card">
  <summary>Or use the API</summary>
  <p class="muted">Every endpoint is described in the
  <a href="/openapi.json" target="_blank" rel="noopener">OpenAPI spec</a>, which
  you can also browse and call from
  <a href="/docs" target="_blank" rel="noopener">the interactive docs</a>.</p>
  <p class="muted">Register your company:</p>
  <pre class="curl" id="curl-register"></pre>
  <p class="muted">Submit a bundle (response headers carry X-Pass / X-Score / X-Errors,
  body is the FHIR OperationOutcome):</p>
  <pre class="curl" id="curl-submit"></pre>
  <p class="muted">Leaderboard:</p>
  <pre class="curl" id="curl-leaderboard"></pre>
</details>

</div>

<div id="tab-clinical" class="tab-panel">

<section class="card">
  <h2>Clinical view of submitted documents</h2>
  <label for="cv-team">Company</label>
  <select id="cv-team"></select>
  <p class="muted" id="cv-preview-msg" role="status" style="display:none"></p>
  <input type="file" id="cv-file" accept=".json,application/json,application/fhir+json" hidden>
  <div id="cv-list" style="margin-top:12px"></div>
</section>

<section class="card" id="cv-doc" style="display:none"></section>

</div>

<div id="tab-interop" class="tab-panel">

<section class="card">
  <h2>Interop testing</h2>
  <p class="muted" style="margin:0">There are two pathways to using this
  platform and you should do both - 1) submit the document(s) for validation,
  scoring, and clinical view and 2) exchange them with other participants in
  the Hackathon.</p>
  <div class="lanes">
    <div class="lane">
      <span class="lane-n">Pathway 1 &middot; Validate</span>
      <h3>Send it to Provbana</h3>
      <p class="muted">Matchbox checks it against HL7 EU EPS, the leaderboard
      scores it and the clinical view renders it. Your best valid summary is
      published below for others to fetch.</p>
      <p class="muted" style="margin:0"><a href="#submit">Submit a bundle</a></p>
    </div>
    <div class="lane">
      <span class="lane-n">Pathway 2 &middot; Exchange</span>
      <h3>Send it to each other</h3>
      <p class="muted">Fetch a partner's published document and load it into
      your system, or push yours straight to an address they advertise. Then
      add a screenshot of how it rendered, so everyone - the clinicians
      included - can see.</p>
      <p class="muted" style="margin:0"><a href="#io-partners">Published
      documents</a> &middot; <a href="#send-direct">Send to their system</a>
      &middot; <a href="#submit">Advertise your address</a></p>
    </div>
  </div>
</section>

<section class="card">
  <h2>Published documents</h2>
  <p class="muted" id="io-hint" style="margin:0" role="status"></p>
  <div id="io-partners"></div>
</section>

<section class="card" id="send-direct">
  <h2>Send to their system</h2>
  <p class="muted" style="margin:0">Some companies take documents straight
  into their own system - a FHIR server to POST to, or a page that says how to
  get data in. Send them yours, then ask how it came out.</p>
  <p class="muted" id="ep-hint" style="margin:8px 0 0" role="status"></p>
  <div id="ep-list"></div>
  <p class="muted" style="margin:14px 0 0">Advertise your own when you
  register, or add and change them later on the <a href="#submit">Submit
  tab</a>.</p>
</section>

<details class="card">
  <summary>Or use the API</summary>
  <p class="muted">Every endpoint is described in the
  <a href="/openapi.json" target="_blank" rel="noopener">OpenAPI spec</a>, which
  you can also browse and call from
  <a href="/docs" target="_blank" rel="noopener">the interactive docs</a>.</p>
  <p class="muted">Fetch a company's exchange document:</p>
  <pre class="curl" id="curl-exchange"></pre>
</details>

</div>

</main>
<footer>
  <p>Part of the <a href="https://nordichackathon2026.blogs.dsv.su.se" target="_blank" rel="noopener">Nordic Hackathon
  2026</a>, 18-20 November in Kista.</p>
  <p>Bundles are validated by <a href="https://github.com/ahdis/matchbox"
  target="_blank" rel="noopener">Matchbox</a>, the open-source FHIR validation and
  mapping server from <a href="https://ahdis.ch/" target="_blank" rel="noopener">ahdis</a>,
  which runs the official <a href="https://github.com/hapifhir/org.hl7.fhir.core"
  target="_blank" rel="noopener">HL7 FHIR validator</a> underneath.</p>
  <p>Structure, profiles and cardinality are checked in full, against the EPS
  guide - or, for one of the <a href="#other-documents">other EHDS document
  types</a>, against its own HL7 Europe guide. External code systems those
  guides do not themselves carry and that require a license (such as SNOMED CT,
  LOINC, WHO ATC) are not checked.</p>
  <p class="colophon">Runs on Provbana, built by <a href="https://vadimperetok.in"
  target="_blank" rel="noopener">Vadim Peretokin</a>.</p>
</footer>
<script>
"use strict";
const $ = (s) => document.querySelector(s);
/* Hackathon days run into each other; a bare clock time for "last submission"
   cannot tell yesterday evening from this morning. */
const fmtWhen = (ts) => new Date(ts).toLocaleString([],
  { weekday: "short", hour: "2-digit", minute: "2-digit" });
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

/* ---- FHIR unwrapping ----
   The service speaks FHIR, so a list arrives as a Parameters resource with the
   name repeated rather than as a JSON array, and an absent value is simply a
   part that is not there - FHIR has no null. These three put one back into the
   plain objects the rest of this page is written against, so every call site
   below reads the same as it did when the API was bespoke. */
function paramValue(part) {
  if (part.part) return partsToObject(part.part);
  if (part.resource) return part.resource;
  for (const k in part) if (k.startsWith("value")) return part[k];
  return null;
}
function partsToObject(parts) {
  /* A repeated name *is* the list - FHIR has no array type - so repeats
     collect rather than overwrite. Seen once it stays a scalar, which is the
     same ambiguity any FHIR client resolves from the operation definition;
     partList below is how this page resolves it. */
  const out = {};
  for (const p of parts || []) {
    const v = paramValue(p);
    if (p.name in out) {
      out[p.name] = (Array.isArray(out[p.name]) ? out[p.name] : [out[p.name]]).concat([v]);
    } else {
      out[p.name] = v;
    }
  }
  return out;
}
/* A part the operation defines as repeating, always as an array. Absent is
   empty: FHIR has no null, so a company that has passed no other category
   simply has no such part. */
function partList(obj, name) {
  const v = obj[name];
  if (v === undefined || v === null) return [];
  return Array.isArray(v) ? v : [v];
}
/* Every value of a repeated parameter, in order. */
function paramEntries(res, name) {
  return ((res && res.parameter) || [])
    .filter((p) => p.name === name).map(paramValue);
}
function paramOne(res, name) {
  const all = paramEntries(res, name);
  return all.length ? all[0] : null;
}
/* $submissions, $exchange and $leaderboard name their parts after the FHIR
   resources they
   point at. The page was written against the older names and there is nothing
   to gain from renaming a hundred call sites, so they are mapped back here -
   one place to look when a part name changes. */
function submissionRow(e) {
  return { id: e.id, ts: e.recorded, team: e.organization,
           team_slug: e.organizationSlug, pass: e.pass, score: e.score,
           errors: e.errors, warnings: e.warnings, sections: e.sections,
           resources: e.resources };
}
function partnerRow(e) {
  return { team: e.organization, team_slug: e.organizationSlug,
           exchange_id: e.id, ts: e.recorded, sections: e.sections,
           resources: e.resources, renderings: e.renderings };
}
/* FHIR has no null, so a company that has never submitted has no bestErrors
   part at all rather than one set to null. Absent becomes null here, once, so
   the row template can keep asking `=== null` instead of every reader having
   to know that. */
function leaderboardRow(e) {
  const orNull = (v) => (v === undefined ? null : v);
  return { rank: e.rank, team: e.organization, team_slug: e.organizationSlug,
           score: e.score, passed: e.passed, attempts: e.attempts,
           first_pass: e.firstPass, best_errors: orNull(e.bestErrors),
           best_sections: orNull(e.bestSections),
           best_resources: orNull(e.bestResources),
           secondary: partList(e, "secondaryCategories"),
           last_submission: orNull(e.lastSubmission) };
}
/* FileReader gives a data: URL; the base64 payload is what FHIR's
   Attachment.data wants, so everything up to the comma comes off. */
function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error || new Error("Could not read the image"));
    reader.readAsDataURL(blob);
  });
}

/* ---- practice period ---- */
/* Before the event the board is wiped daily, so say so - a score that will not
   survive the night should not look like a standing. Set from $status once
   on load and re-checked on every leaderboard tick, which is what lets the
   notice clear itself at the final wipe on a page left open overnight. */
let practiceEnds = null;
function updatePracticeNote() {
  const note = $("#practice-note");
  const active = practiceEnds !== null && Date.now() < practiceEnds;
  if (active) {
    note.textContent = "Practice mode - resets daily until "
      + new Date(practiceEnds).toLocaleDateString([],
          { day: "numeric", month: "short" });
  }
  note.hidden = !active;
}

/* ---- other EHDS document types ---- */
/* Filled from $documents once at load. The short forms are what fits a
   leaderboard cell; the long labels come from the server so the two lists
   cannot drift apart. Seeded rather than left empty so the first leaderboard
   paint - which can beat the fetch - still renders readable chips. */
const DOC_SHORT = { eps: "PS", laboratory: "LAB", mpd: "eP/eD",
                    imaging: "IMG" };
let DOC_LABELS = { eps: "European Patient Summary", laboratory: "Laboratory Report",
                   mpd: "Medication Prescription and Dispense",
                   imaging: "Imaging Report" };
const docShort = (id) => DOC_SHORT[id] || id;
const docLabel = (id) => DOC_LABELS[id] || id;

async function loadDocuments() {
  let docs;
  try {
    const res = await fetch("/fhir/$documents");
    if (!res.ok) return;
    docs = paramEntries(await res.json(), "entry");
  } catch (e) { return; }  /* transient: the card keeps its placeholder */
  DOC_LABELS = Object.fromEntries(docs.map((d) => [d.id, d.label]));
  /* The card is about the *other* categories - the patient summary is the
     whole rest of the page - so the primary one is not a row in it. */
  const others = docs.filter((d) => !d.primary);
  $("#other-count").textContent = others.length;
  /* Category, guide and maturity only. How a bundle is recognised, and whether
     Matchbox holds each package, are still on $documents for anyone who needs
     them - they were more detail than this card is for. */
  $("#doc-types-body").innerHTML = others.map((d) =>
    "<tr><td>" + esc(d.ehdsCategory) + "</td>"
      + '<td><a href="' + esc(d.spec) + '" target="_blank" rel="noopener">'
      + esc(d.label) + "</a> <span class=\"cat-chip\">" + esc(d.abbreviation)
      + "</span></td>"
      + "<td>" + esc(d.maturity) + "</td></tr>"
  ).join("");
}
loadDocuments();

/* ---- leaderboard ---- */
async function refreshLeaderboard() {
  updatePracticeNote();
  try {
    const res = await fetch("/fhir/$leaderboard");
    if (!res.ok) return;
    const rows = paramEntries(await res.json(), "entry").map(leaderboardRow);
    $("#lb-empty").style.display = rows.length ? "none" : "block";
    $("#lb").style.display = rows.length ? "table" : "none";
    $("#lb-body").innerHTML = rows.map((r) => `
      <tr class="${r.rank <= 3 ? "rank-" + r.rank : ""}">
        <td>${r.rank}</td>
        <td>${r.attempts
          ? `<a href="#clinical/company/${encodeURIComponent(r.team_slug)}"
               title="see this company's documents">${esc(r.team)}</a>`
          : esc(r.team)}${r.first_pass
          ? ' <span title="First pass: first company to submit a valid document">'
            + '\u{1F525}</span>' : ""}</td>
        <td><strong>${r.score}</strong></td>
        <td>${r.passed ? '<span class="badge pass">VALID</span>'
                       : '<span class="badge fail">NOT YET</span>'}</td>
        <td>${r.secondary.length
          ? r.secondary.map((c) => '<span class="cat-chip" title="'
              + esc(docLabel(c)) + '">' + esc(docShort(c)) + "</span>").join(" ")
          : '<span class="muted">-</span>'}</td>
        <td>${r.best_sections === null ? "-" : r.best_sections}</td>
        <td>${r.best_resources === null ? "-" : r.best_resources}</td>
        <td>${r.best_errors === null ? "-" : r.best_errors}</td>
        <td class="muted">${r.last_submission ? fmtWhen(r.last_submission) : "-"}</td>
      </tr>`).join("");
  } catch (e) { /* transient network error - retry on next tick */ }
}
refreshLeaderboard();
/* A tab left open in the background need not poll; catch up when it returns. */
setInterval(() => { if (!document.hidden) refreshLeaderboard(); }, 5000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshLeaderboard();
});

/* ---- routing ----
   The hash is this app's address bar: #<tab>[/<kind>/<id>/<view>]. Tabs are
   ordinary links and every in-app navigation goes through the hash, so Back,
   Forward and a pasted link all arrive the same way. Nothing paints except
   applyRoute. Under #clinical the kind is sample, submission or company - the
   last one opens a company's list of documents without picking one, which is
   where the leaderboard's company names point. */
const TABS = ["leaderboard", "submit", "interop", "clinical"];
const DEFAULT_SAMPLE = "arne-complex-care";

function routeParts() {
  let hash = location.hash.replace(/^#/, "");
  try { hash = decodeURIComponent(hash); } catch (e) { /* a stray %: read it as typed */ }
  return hash.split("/");
}

/* replace is for routes the user did not ask for by name - normalising a bare
   #clinical to the document it opens, or recording a view switch. Neither is
   somewhere you would want Back to stop at. */
function go(route, replace) {
  const hash = "#" + route;
  if (replace || location.hash === hash) {
    history.replaceState(null, "", hash);
    applyRoute(false);
  } else {
    location.hash = hash;  // hashchange does the painting
  }
}

function showTab(name) {
  document.querySelectorAll(".tab-btn").forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  let wasActive = false;
  document.querySelectorAll(".tab-panel").forEach((p) => {
    const on = p.id === "tab-" + name;
    if (on && p.classList.contains("active")) wasActive = true;
    p.classList.toggle("active", on);
  });
  /* Lists load when a tab is entered, not on every route change inside it:
     reloading the company picker under a reader's hand would reset it. */
  if (name === "clinical" && !wasActive) initClinical();
  if (name === "interop" && !wasActive) initInterop();
  if (name === "submit") checkValidator();
}

function applyRoute(scroll) {
  const parts = routeParts();
  /* A hash that is not a tab may be a deep link to a card inside one, such as
     #other-documents or #send-direct. The card's own tab is shown, a folded
     card is opened, and it is scrolled into view; anything else falls through
     to the leaderboard. */
  const anchor = TABS.indexOf(parts[0]) < 0 && parts[0]
    ? document.getElementById(parts[0]) : null;
  const panel = anchor && anchor.closest(".tab-panel");
  const tab = TABS.indexOf(parts[0]) >= 0 ? parts[0]
    : panel ? panel.id.replace(/^tab-/, "") : "leaderboard";
  showTab(tab);
  if (panel) {
    if (anchor.tagName === "DETAILS") anchor.open = true;
    anchor.scrollIntoView({ block: "start" });
  }
  if (tab !== "clinical") return;
  if (!parts[1]) {
    go("clinical/sample/" + DEFAULT_SAMPLE + "/" + CV_DEFAULT, true);
    return;
  }
  if (parts[3] && Object.hasOwn(CV_VIEWS, parts[3])) cvMode = parts[3];
  /* The tab link remembers the document you were reading, so a trip to the
     leaderboard and back does not throw your place away. */
  document.querySelector('.tab-btn[data-tab="clinical"]')
    .setAttribute("href", location.hash);
  /* A company route shows that company's list and no document. If the tab was
     already open the picker is moved here; otherwise initClinical, kicked off
     by showTab above, reads the route once the list has loaded. */
  if (parts[1] === "company") { hideDoc(); selectCompany(parts[2]); return; }
  showDoc(parts[1], parts[2], scroll);
}

window.addEventListener("hashchange", () => applyRoute(true));

/* ---- register ---- */
$("#reg-btn").addEventListener("click", async () => {
  const name = $("#reg-name").value.trim();
  const code = $("#reg-code").value.trim();
  const errBox = $("#reg-error");
  errBox.style.display = "none";
  $("#token-box").style.display = "none";
  try {
    const parts = [{ name: "name", valueString: name }];
    if (code) parts.push({ name: "code", valueString: code });
    /* Sent with the registration rather than after it: the server stores both
       or neither, so a mistyped address costs a retry, not the company name. */
    const epAddress = $("#reg-ep-address").value.trim();
    const epDesc = $("#reg-ep-desc").value.trim();
    if (epAddress) {
      if (!/^https?:\/\/\S+$/i.test(epAddress)) {
        errBox.textContent = "The address has to be an http:// or https:// URL.";
        errBox.style.display = "block";
        return;
      }
      const ep = { resourceType: "Endpoint", status: "active",
                   connectionType: { code: $("#reg-ep-kind").value },
                   address: epAddress };
      if (epDesc) ep.extension = [{ url: ENDPOINT_DESCRIPTION_EXT, valueString: epDesc }];
      parts.push({ name: "endpoint", resource: ep });
    } else if (epDesc) {
      errBox.textContent = "Add the address the note is about, or clear the note.";
      errBox.style.display = "block";
      return;
    }
    const res = await fetch("/fhir/$register", {
      method: "POST",
      headers: { "Content-Type": "application/fhir+json" },
      body: JSON.stringify({ resourceType: "Parameters", parameter: parts }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      /* The organisers may gate registration; the field appears the moment the
         server says so, instead of cluttering an open event. */
      if (res.status === 403) $("#reg-code-row").hidden = false;
      errBox.textContent = detailText(data) || ("Registration failed (" + res.status + ")");
      errBox.style.display = "block";
      return;
    }
    $("#token-value").textContent = paramOne(data, "token");
    $("#token-box").style.display = "block";
    syncToken(paramOne(data, "token"));
    for (const id of ["#reg-name", "#reg-code", "#reg-ep-address", "#reg-ep-desc"])
      $(id).value = "";
    refreshLeaderboard();
    refreshCompany();
  } catch (e) {
    errBox.textContent = "Network error: " + e.message;
    errBox.style.display = "block";
  }
});
["#reg-name", "#reg-code"].forEach((sel) => $(sel).addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") $("#reg-btn").click();
}));

/* Every error from this service is an OperationOutcome, so the message is the
   first issue's diagnostics. FastAPI's own `detail` is still read as a
   fallback: a proxy or a framework error that never reached our handler will
   still be shaped that way. */
function detailText(data) {
  if (!data) return "";
  if (data.resourceType === "OperationOutcome") {
    const issue = (data.issue || [])[0] || {};
    return issue.diagnostics || (issue.details && issue.details.text) || "";
  }
  if (data.detail === undefined) return "";
  return typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
}

function flashCopied(btn, text) {
  const old = btn.textContent;
  btn.textContent = text || "Copied!";
  btn.classList.add("copied");
  setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1500);
}
$("#token-copy").addEventListener("click", async (ev) => {
  const node = $("#token-value");
  let ok = false;
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(node.textContent); ok = true; }
    catch (e) { /* permission denied - fall through to selecting the text */ }
  }
  if (!ok) {
    /* Plain-http origins - which is what a LAN hackathon box is - have no
       clipboard API; selecting the token leaves one keystroke to do. */
    const range = document.createRange();
    range.selectNodeContents(node);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  flashCopied(ev.target, ok ? "Copied!" : "Selected - press Ctrl+C");
});

/* ---- validation issue rendering ---- */
const SEV_ORDER = { fatal: 0, error: 1, warning: 2, information: 3 };

function cleanMessage(text) {
  // "... required, but not found (from http://hl7.eu/.../bundle-eu-eps|1.0.0-ballot)"
  //
  // Not anchored to the end: the validator puts the profile last on a
  // cardinality message but mid-sentence on a slicing one, which continues
  // ". Note that other slices are allowed ...". Anchoring left every slicing
  // message with a raw URL inline and no profile chip - and, because the
  // dedupe key below is the cleaned message, kept the EPS and IPS copies of
  // one finding apart as two rows. So splice the parenthetical out wherever
  // it sits and keep whatever follows it.
  const m = text.match(/\s*\(from ([^)\s]+)\)/);
  if (m) {
    return { msg: (text.slice(0, m.index) + text.slice(m.index + m[0].length)).trim(),
             profile: m[1] };
  }
  return { msg: text, profile: null };
}

function cleanExpr(expr) {
  // "Bundle.entry[1].resource/*Patient/xyz*/" -> "Bundle.entry[1].resource"
  return expr.replace(/\/\*[\s\S]*?\*\//g, "").trim();
}

/* ---- locations: from a validator expression to a place in the document ----

   Matchbox reports where a finding sits as a FHIRPath-shaped locator, e.g.
   Bundle.entry[9].resource.dosage[0].doseAndRate[0].dose.ofType(Quantity).value

   A FHIRPath engine is the wrong tool for reading these. It answers with the
   value at a path, and the question here is the opposite one - whereabouts in
   the bytes the participant submitted does that value live - which no engine
   returns. It would also choke on the two things Matchbox writes that are not
   FHIRPath: the inline Type/id comment it splices into the path, and the
   dose[x] choice marker. So the locator is walked against the parsed document
   instead, which is a dozen lines and yields the one thing that matters: a
   path that can be turned into a line number. */

function splitSegments(expr) {
  // Dots inside a function argument - where(system='http://a.b/c') - are not
  // separators, so depth has to be tracked rather than String.split(".").
  const out = [];
  let depth = 0, cur = "";
  for (const ch of expr) {
    if (ch === "(") depth++;
    else if (ch === ")") depth--;
    if (ch === "." && depth === 0) { out.push(cur); cur = ""; }
    else cur += ch;
  }
  out.push(cur);
  return out.filter(Boolean);
}

/* dose[x] in a locator is doseQuantity, doseRange... in the JSON. */
function choiceKey(node, stem) {
  if (!node || typeof node !== "object") return null;
  return Object.keys(node).find((k) => k.length > stem.length
    && k.startsWith(stem) && k[stem.length] !== k[stem.length].toLowerCase()) || null;
}

/* Path to the addressed node, as an array of keys and indices. A locator that
   runs off the end of the document - the validator naming an element that is
   missing, which is most of what it complains about - resolves to the deepest
   part that does exist, because that is the place to look. */
function locateExpr(root, expr) {
  const segs = splitSegments(expr.replace(/\/\*[\s\S]*?\*\//g, ""));
  let node = root, path = [], deepest = [];
  for (let i = 1; i < segs.length; i++) {
    const m = segs[i].match(/^([A-Za-z][A-Za-z0-9_]*)(?:\[(x|\d+)\])?$/);
    if (!m || !node || typeof node !== "object") break;
    let key = m[1];
    const oft = i + 1 < segs.length
      && segs[i + 1].match(/^ofType\(([A-Za-z][A-Za-z0-9_]*)\)$/);
    if (oft) { key += oft[1]; i++; }
    else if (m[2] === "x") key = choiceKey(node, key);
    if (key === null || !(key in node)) break;
    node = node[key];
    path.push(key);
    deepest = path.slice();
    if (m[2] !== undefined && m[2] !== "x") {
      const n = Number(m[2]);
      if (!Array.isArray(node) || !(n in node)) break;
      node = node[n];
      path.push(n);
      deepest = path.slice();
    }
  }
  return deepest;
}

/* The locator usually stops at the resource while the message names the element
   inside it - "Composition.section: minimum required = 5, but only found 4",
   "Slice 'Bundle.entry:patient': a matching slice is required". Following that
   name moves the marker off the resource header and onto the element at fault. */
function refineByMessage(root, path, message) {
  const m = message.match(/^(?:Slice ')?([A-Z][A-Za-z0-9_]*)\.([a-z][A-Za-z0-9_.]*)/);
  if (!m) return path;
  let node = root;
  for (const k of path) node = node[k];
  const type = path.length ? (node && node.resourceType) : (root && root.resourceType);
  if (!node || typeof node !== "object" || type !== m[1]) return path;
  const out = path.slice();
  for (const part of m[2].split(".")) {
    if (!node || typeof node !== "object" || !(part in node)) break;
    node = node[part];
    out.push(part);
  }
  return out;
}

/* JSON.stringify(doc, null, 2) prints exactly this text and then forgets where
   every node ended up, and the line number is the whole point, so the printing
   is done here with the positions kept. */
function layoutJson(root) {
  const lines = [];
  const at = new Map();   // "/entry/9/resource" -> {start, end}
  function walk(v, path, prefix, indent, comma) {
    const pad = "  ".repeat(indent);
    const start = lines.length;
    if (v && typeof v === "object") {
      const arr = Array.isArray(v);
      const keys = arr ? v.map((_, i) => i) : Object.keys(v);
      if (!keys.length) {
        lines.push(pad + prefix + (arr ? "[]" : "{}") + comma);
      } else {
        lines.push(pad + prefix + (arr ? "[" : "{"));
        keys.forEach((k, n) => walk(v[k], path + "/" + k,
          arr ? "" : JSON.stringify(String(k)) + ": ",
          indent + 1, n === keys.length - 1 ? "" : ","));
        lines.push(pad + (arr ? "]" : "}") + comma);
      }
    } else {
      lines.push(pad + prefix + JSON.stringify(v === undefined ? null : v) + comma);
    }
    at.set(path, { start: start, end: lines.length - 1 });
  }
  walk(root, "", "", 0, "");
  return { lines: lines, at: at };
}

function groupIssues(issues) {
  // Dedupe identical (severity, expression, message); Matchbox repeats each
  // finding once per profile (eps + IPS), so keep the distinct profiles.
  const map = new Map();
  for (const i of issues) {
    const raw = i.diagnostics || (i.details && i.details.text) || "-";
    const cm = cleanMessage(raw);
    const exprFull = (i.expression || i.location || []).join(", ") || "-";
    const exprShort = cleanExpr(exprFull) || "-";
    const key = i.severity + "\u0000" + exprShort + "\u0000" + cm.msg;
    let g = map.get(key);
    if (!g) {
      g = { severity: i.severity, exprFull, exprShort, message: cm.msg,
            count: 0, profiles: [] };
      map.set(key, g);
    }
    g.count++;
    if (cm.profile && !g.profiles.includes(cm.profile)) g.profiles.push(cm.profile);
  }
  return [...map.values()].sort((a, b) =>
    (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9));
}

function issueTable(groups) {
  const rows = groups.map((g) => {
    const chips = g.profiles.map((p) =>
      '<span class="profile-chip" title="' + esc(p) + '">'
      + esc(p.split("|")[0].split("/").pop()) + "</span>").join("");
    return "<tr>"
      + '<td><span class="chip ' + esc(g.severity) + '">' + esc(g.severity) + "</span>"
      + (g.count > 1 ? '<span class="count-badge" title="reported '
         + g.count + ' times">×' + g.count + "</span>" : "") + "</td>"
      + '<td class="loc" title="' + esc(g.exprFull) + '">'
      + (g.line === undefined ? esc(g.exprShort)
         : '<button type="button" class="loc-jump" data-jump="' + g.line
           + '">line ' + (g.line + 1) + '</button><span class="loc-where">'
           /* break the path at its own punctuation rather than mid-word */
           + esc(g.where).replace(/([.\]])/g, "$1<wbr>") + "</span>") + "</td>"
      + '<td class="msg">' + esc(g.message) + chips + "</td>"
      + "</tr>";
  }).join("");
  return '<div class="issues-scroll"><table class="issues"><thead><tr>'
    + '<th scope="col">Severity</th><th scope="col" class="loc-h">Location</th>'
    + '<th scope="col">Message</th></tr></thead><tbody>'
    + rows + "</tbody></table></div>";
}

function foldedGroup(label, groups) {
  return '<details class="issue-fold"><summary>' + label + "</summary>"
    + issueTable(groups) + "</details>";
}

/* "MedicationStatement #9 · dosage[0].doseAndRate[0].doseQuantity.value" is
   what the participant is looking for; Bundle.entry[9].resource.dosage[0]... is
   how the validator had to say it. Built from the resolved path, so it names
   what the document actually holds at that spot. */
function pathLabel(doc, path) {
  let head = (doc && doc.resourceType) || "Bundle";
  let tail = path;
  if (path[0] === "entry" && typeof path[1] === "number" && path[2] === "resource") {
    const r = doc.entry[path[1]].resource;
    head = ((r && r.resourceType) || "resource") + " #" + path[1];
    tail = path.slice(3);
  }
  const text = tail.reduce((acc, k) => typeof k === "number"
    ? acc + "[" + k + "]" : (acc ? acc + "." + k : k), "");
  return text ? head + " · " + text : head;
}

/* Past this many lines the whole document is more DOM than a laptop on venue
   wifi should be asked to lay out, so only the flagged neighbourhoods print. */
const DOC_MAX_LINES = 12000;
const DOC_CONTEXT = 4;
let docState = null;

/* Information findings are almost entirely "this element does not match any
   known slice", one per entry; drawn in the margin they would bury the errors.
   They still get a line number, so their location in the table still jumps. */
const MARK_INLINE = new Set(["fatal", "error", "warning"]);

function worstSeverity(hits) {
  return hits.reduce((a, h) =>
    (SEV_ORDER[h.severity] ?? 9) < (SEV_ORDER[a] ?? 9) ? h.severity : a,
    hits[0].severity);
}

/* Fills in group.line for every finding that could be placed, and leaves the
   printed document plus its margin marks in docState. */
function buildDoc(bundleText, groups) {
  docState = null;
  let doc;
  try { doc = JSON.parse(bundleText); } catch (e) { return; }
  if (!doc || typeof doc !== "object") return;
  const laid = layoutJson(doc);
  const marks = new Map();
  const placed = new Set();
  for (const g of groups) {
    if (g.exprFull === "-") continue;
    let path;
    try { path = refineByMessage(doc, locateExpr(doc, g.exprFull), g.message); }
    catch (e) { continue; }
    const spot = laid.at.get(path.map((k) => "/" + k).join(""));
    if (!spot) continue;
    g.line = spot.start;
    g.where = pathLabel(doc, path);
    placed.add(spot.start);
    if (!MARK_INLINE.has(g.severity)) continue;
    if (!marks.has(spot.start)) marks.set(spot.start, []);
    marks.get(spot.start).push(g);
  }
  if (!placed.size) return;
  docState = { lines: laid.lines, marks: marks, placed: placed };
}

function renderDoc(flaggedOnly) {
  const wrap = $("#doc-wrap");
  if (!docState) { wrap.hidden = true; return; }
  const lines = docState.lines, marks = docState.marks;
  const big = lines.length > DOC_MAX_LINES;
  /* A document too big to print keeps the neighbourhood of every reported
     location, so that every jump in the table still has somewhere to land;
     asking for flagged lines keeps only what is actually marked. */
  const keep = big ? docState.placed
    : (flaggedOnly && marks.size ? new Set(marks.keys()) : null);
  let show = null;
  if (keep) {
    show = new Set();
    for (const n of keep)
      for (let i = Math.max(0, n - DOC_CONTEXT);
           i <= Math.min(lines.length - 1, n + DOC_CONTEXT); i++) show.add(i);
  }
  const out = [];
  let gap = 0;
  const flushGap = () => {
    if (!gap) return;
    out.push('<div class="jgap">' + gap + " line" + (gap === 1 ? "" : "s")
      + " not shown</div>");
    gap = 0;
  };
  for (let i = 0; i < lines.length; i++) {
    if (show && !show.has(i)) { gap++; continue; }
    flushGap();
    const hits = marks.get(i);
    out.push('<div class="jl' + (hits ? " hit " + worstSeverity(hits) : "")
      + '" id="jl-' + i + '"><span class="ln">' + (i + 1) + "</span>"
      + '<span class="jc">' + esc(lines[i]) + "</span></div>");
    for (const h of hits || [])
      out.push('<div class="jd ' + esc(h.severity) + '"><span class="ln"></span>'
        + '<span class="jt"><span class="chip ' + esc(h.severity) + '">'
        + esc(h.severity) + "</span> " + esc(h.message) + "</span></div>");
  }
  flushGap();
  $("#doc-body").innerHTML = out.join("");
  $("#doc-note").textContent = big
    ? "Document is " + lines.length
      + " lines - showing only around what the validator reported."
    : marks.size
      ? "Errors and warnings are marked where they land."
      : "Nothing to mark; the locations above still jump into it.";
  $("#doc-flagged").disabled = big || !marks.size;
  wrap.hidden = false;
  wrap.open = marks.size > 0;
}

function jumpToLine(n) {
  const wrap = $("#doc-wrap");
  if (wrap.hidden) return;
  wrap.open = true;
  let el = document.getElementById("jl-" + n);
  if (!el && $("#doc-flagged").checked) {
    $("#doc-flagged").checked = false;
    renderDoc(false);
    wrap.open = true;
    el = document.getElementById("jl-" + n);
  }
  if (!el) return;
  const box = $("#doc-body");
  box.scrollTop = el.offsetTop - box.clientHeight / 3;
  el.classList.remove("flash");
  void el.offsetWidth;   // restart the animation when the same line is clicked twice
  el.classList.add("flash");
}

$("#doc-flagged").addEventListener("change", (ev) => renderDoc(ev.target.checked));
$("#issue-groups").addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-jump]");
  if (btn) jumpToLine(Number(btn.dataset.jump));
});

/* ---- explain the findings with an assistant ----

   One action for the whole verdict: every error the table shows, in one
   prompt, opened in whichever assistant the team already uses. Sent together
   rather than one at a time, the assistant sees the failing bundle's whole
   story and can say which fix comes first - nine slicing errors are usually
   one missing section.

   Nothing goes through this service. These are ordinary links the
   participant's own browser follows, under their own account, and the bundle
   itself is not in the prompt - only the findings. A team can paste more if
   they want to; that is their call to make. */
/* The guide the last submission was actually scored against, so the prompt
   handed to an assistant names the right specification - findings from the
   laboratory guide explained as if they came from the patient summary would
   send a team looking for the wrong fix. Set from X-Document-Type on every
   submission; the patient summary is the standing answer before the first
   one, and the answer for almost every one after it. */
const EXPLAIN_GUIDES = {
  eps: ["European Patient Summary", "https://hl7.eu/fhir/eps/"],
  laboratory: ["Laboratory Report", "https://hl7.eu/fhir/laboratory/"],
  mpd: ["Medication Prescription and Dispense", "https://hl7.eu/fhir/mpd/"],
  imaging: ["Imaging Report", "https://hl7.eu/fhir/imaging/"],
};
let explainDoc = "eps";

/* ChatGPT, Claude and Le Chat take the question in the URL and open with it
   already in the composer. Gemini's web app has no such parameter, so it
   opens on an empty chat and the prompt goes to the clipboard instead - and
   the menu says so on the row rather than letting someone find out. */
const EXPLAIN_PROVIDERS = [
  { id: "chatgpt", name: "ChatGPT", prefill: "https://chatgpt.com/?q=" },
  { id: "claude", name: "Claude", prefill: "https://claude.ai/new?q=" },
  { id: "gemini", name: "Gemini", prefill: null, open: "https://gemini.google.com/app" },
  { id: "mistral", name: "Le Chat", prefill: "https://chat.mistral.ai/chat?q=" },
];

/* `groups` are the table's rows: identical findings already merged, each with
   its location and the profiles that raised it. `reported` is how many times
   the validator said them - the number on the banner and the chip - so the
   prompt can own the difference instead of the assistant wondering. */
function explainPrompt(groups, severity, reported) {
  const many = groups.length > 1;
  const noun = severity + (many ? "s" : "");
  const list = groups.map((g, i) =>
    (many ? (i + 1) + ". " : "") + g.message
    + "\n   location: " + g.exprShort + (g.where ? " (" + g.where + ")" : "")
    + (g.profiles.length ? "\n   profile:  " + g.profiles.join(", ") : "")
  ).join("\n\n");
  const heading = !many ? "The " + severity
    : reported > groups.length
      ? "The " + reported + " " + noun + ", merged into " + groups.length
        + " distinct findings"
      : "The " + groups.length + " " + noun;
  const guide = EXPLAIN_GUIDES[explainDoc] || EXPLAIN_GUIDES.eps;
  return "I am validating a FHIR bundle against the HL7 Europe " + guide[0]
    + " implementation guide (" + guide[1] + ") with the official "
    + "HL7 FHIR validator. Explain what " + (many ? "these " + noun + " mean"
                                                  : "this " + noun + " means")
    + " in plain terms and what to change in the bundle to fix "
    + (many ? "them, starting with whichever change clears the most of them."
            : "it.")
    + "\n\n" + heading + ":\n\n" + list;
}

function explainUrl(provider, prompt) {
  return provider.prefill ? provider.prefill + encodeURIComponent(prompt)
                          : provider.open;
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; }
    catch (e) { /* permission denied - the caller shows the text instead */ }
  }
  return false;
}

let explainPromptText = "";

function closeExplainMenu() {
  $("#explain-menu").hidden = true;
  $("#explain-btn").setAttribute("aria-expanded", "false");
}

function paintExplain(groups, severity, reported) {
  const wrap = $("#explain-wrap");
  closeExplainMenu();
  $("#explain-note").hidden = true;
  $("#explain-text").hidden = true;
  if (!groups.length) {
    /* Emptied, not just hidden: a resubmission that now passes must not leave
       the previous bundle's question behind the button. */
    $("#explain-btn").textContent = "";
    $("#explain-menu").innerHTML = "";
    wrap.hidden = true;
    explainPromptText = "";
    return;
  }
  explainPromptText = explainPrompt(groups, severity, reported);
  $("#explain-btn").textContent = "Explain "
    + (reported === 1 ? "the " + severity : "all " + reported + " " + severity + "s")
    + " \u25BE";
  $("#explain-menu").innerHTML = '<div class="menu-head">Explain with</div>'
    + EXPLAIN_PROVIDERS.map((p) =>
      '<a href="' + esc(explainUrl(p, explainPromptText)) + '" target="_blank"'
      + ' rel="noopener" data-provider="' + p.id + '">' + esc(p.name)
      + '<span class="note">' + (p.prefill ? "prefilled" : "paste it")
      + "</span></a>").join("")
    + '<button type="button" class="menu-item" data-copy="1">Copy the prompt'
    + '<span class="note">any assistant</span></button>';
  wrap.hidden = false;
}

/* Plain-http origins - which is what a LAN hackathon box is - have no
   clipboard API. Then the prompt is shown selected, one keystroke from
   copied, rather than promised to a clipboard it never reached. */
function showExplainText() {
  const text = $("#explain-text");
  text.value = explainPromptText;
  text.hidden = false;
  text.focus();
  text.select();
}

function explainNote(message) {
  const note = $("#explain-note");
  note.textContent = message;
  note.hidden = false;
}

async function explainWith(provider) {
  if (provider.prefill) {
    /* No copyText here: it used to run for every provider and silently
       replaced whatever the participant had on their clipboard - often the
       company token they had just copied - to no purpose. */
    explainNote("Opening " + provider.name + " in a new tab with the question filled in.");
    return;
  }
  const copied = await copyText(explainPromptText);
  if (copied) {
    explainNote(provider.name + " takes no prefilled question - the prompt is on your "
      + "clipboard, paste it there.");
  } else {
    explainNote(provider.name + " takes no prefilled question and this page cannot "
      + "reach your clipboard - copy the prompt below (Ctrl+C) and paste it there.");
    showExplainText();
  }
}

async function copyExplainPrompt() {
  if (await copyText(explainPromptText)) {
    explainNote("Copied - paste it into any assistant.");
    $("#explain-text").hidden = true;
  } else {
    explainNote("Select the prompt below and press Ctrl+C.");
    showExplainText();
  }
}

$("#explain-btn").addEventListener("click", (ev) => {
  ev.stopPropagation();
  const menu = $("#explain-menu");
  menu.hidden = !menu.hidden;
  ev.currentTarget.setAttribute("aria-expanded", String(!menu.hidden));
});
$("#explain-menu").addEventListener("click", (ev) => {
  /* The link itself opens the assistant; this only copies and explains.
     Left to bubble so the document handler closes the menu. */
  const link = ev.target.closest("a[data-provider]");
  if (link) explainWith(EXPLAIN_PROVIDERS.find((p) => p.id === link.dataset.provider));
  else if (ev.target.closest("[data-copy]")) copyExplainPrompt();
});
document.addEventListener("click", closeExplainMenu);
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") closeExplainMenu();
});

function renderIssues(issues, bundleText) {
  const known = issues.filter((i) => i.severity in SEV_ORDER);
  const wrap = $("#issues-wrap");
  if (!known.length) { wrap.style.display = "none"; paintExplain([]); return; }

  const counts = { fatal: 0, error: 0, warning: 0, information: 0 };
  known.forEach((i) => counts[i.severity]++);
  const errN = counts.fatal + counts.error;
  const chips = [];
  if (errN) chips.push('<span class="chip error">' + errN + " error"
    + (errN === 1 ? "" : "s") + "</span>");
  if (counts.warning) chips.push('<span class="chip warning">' + counts.warning
    + " warning" + (counts.warning === 1 ? "" : "s") + "</span>");
  if (counts.information) chips.push('<span class="chip information">'
    + counts.information + " info</span>");
  $("#issue-summary").innerHTML = chips.join("");

  const groups = groupIssues(known);
  /* Placing the findings first, because it is what gives each row its line
     number, and the table is built with that number in it. */
  buildDoc(bundleText, groups);
  const errG = groups.filter((g) => g.severity === "fatal" || g.severity === "error");
  const warnG = groups.filter((g) => g.severity === "warning");
  const infoG = groups.filter((g) => g.severity === "information");
  /* Errors are what the verdict turns on, so they are what gets explained.
     A bundle that passed with warnings gets the same offer for those. */
  if (errG.length) paintExplain(errG, "error", errN);
  else paintExplain(warnG, "warning", counts.warning);

  let html = errG.length ? issueTable(errG)
                         : '<p class="no-errors">No errors \u{1F389}</p>';
  if (warnG.length) html += foldedGroup(counts.warning + " warning"
    + (counts.warning === 1 ? "" : "s"), warnG);
  if (infoG.length) html += foldedGroup(counts.information + " information", infoG);
  $("#issue-groups").innerHTML = html;
  renderDoc($("#doc-flagged").checked);
  wrap.style.display = "block";
}

/* ---- submit ---- */
/* The token is only typed in one place, but localStorage carries it across
   reloads so a participant pastes it once for the event. */
function syncToken(value, from) {
  if (from !== "token") $("#token").value = value;
  try { localStorage.setItem("eps_token", value); } catch (e) { /* site data blocked */ }
}
let savedToken = "";
try { savedToken = localStorage.getItem("eps_token") || ""; } catch (e) { /* site data blocked */ }
syncToken(savedToken);

/* The health endpoint says up front what a doomed request would only reveal
   after a round trip: the validator is down, or up but missing a package. */
let maxBundleBytes = 5 * 1024 * 1024;
let maxNote = 2000;
async function checkValidator() {
  const note = $("#validator-note");
  try {
    const res = await fetch("/fhir/$status");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const hRaw = await res.json();
    const h = partsToObject(hRaw.parameter);
    if (h.maxBundleBytes) maxBundleBytes = h.maxBundleBytes;
    if (h.maxNoteChars) maxNote = h.maxNoteChars;
    if (h.registerCodeRequired) $("#reg-code-row").hidden = false;
    /* NaN if the server sends something unparseable, and NaN fails the
       comparison in updatePracticeNote, so a bad value hides the notice
       rather than showing "Invalid Date". */
    practiceEnds = h.practiceEnds ? Date.parse(h.practiceEnds) : null;
    updatePracticeNote();
    note.textContent = h.matchbox === "up" ? ""
      : h.matchbox === "degraded"
        ? "The validator is up but has not loaded "
          + paramEntries(hRaw, "missingIg").join(", ")
          + " - tell the organisers before trusting a verdict."
        : "The validator is down right now - submissions will fail until it is back.";
  } catch (e) {
    note.textContent = "Could not reach the service (" + e.message + ").";
  }
}
checkValidator();

$("#file").addEventListener("change", async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  const errBox = $("#submit-error");
  if (f.size > maxBundleBytes) {
    errBox.textContent = "That file is " + (f.size / 1048576).toFixed(1) + " MB; the limit is "
      + Math.round(maxBundleBytes / 1048576) + " MB.";
    errBox.style.display = "block";
    return;
  }
  try {
    $("#bundle").value = await f.text();
    errBox.style.display = "none";
  } catch (e) {
    errBox.textContent = "Could not read the file: " + e.message;
    errBox.style.display = "block";
  }
});

$("#submit-btn").addEventListener("click", async () => {
  const token = $("#token").value.trim();
  const body = $("#bundle").value;
  const errBox = $("#submit-error");
  errBox.style.display = "none";
  $("#banner").className = "banner";
  $("#issues-wrap").style.display = "none";
  $("#doc-wrap").hidden = true;
  docState = null;
  if (!token) {
    errBox.textContent = "A company token is required - register your company first.";
    errBox.style.display = "block";
    return;
  }
  if (!body.trim()) {
    errBox.textContent = "Paste your bundle JSON or pick a file.";
    errBox.style.display = "block";
    return;
  }
  $("#submit-btn").disabled = true;
  $("#spinner").classList.add("on");
  try {
    const res = await fetch("/fhir/Bundle/$validate", {
      method: "POST",
      headers: {
        "Content-Type": "application/fhir+json",
        "Authorization": "Bearer " + token,
      },
      body,
    });
    if (!res.ok || !res.headers.has("X-Pass")) {
      /* 413, 422, 500, a proxy's error page: no verdict means failure */
      const data = await res.json().catch(() => ({}));
      errBox.textContent = (detailText(data) || res.statusText || "Request failed")
        + " (HTTP " + res.status + ")";
      errBox.style.display = "block";
      return;
    }
    const outcome = await res.json();
    const pass = res.headers.get("X-Pass") === "true";
    const score = parseInt(res.headers.get("X-Score") || "0", 10);
    const errors = parseInt(res.headers.get("X-Errors") || "0", 10);
    const sections = parseInt(res.headers.get("X-Sections") || "0", 10);
    const resources = parseInt(res.headers.get("X-Resources") || "0", 10);
    const docType = res.headers.get("X-Document-Type") || "eps";
    explainDoc = docType;
    const mine = parseInt(res.headers.get("X-Submission-Score") || "0", 10);
    const extras = parseInt(res.headers.get("X-Secondary-Categories") || "0", 10);
    const bestEps = parseInt(res.headers.get("X-Best-Patient-Summary") || "0", 10);
    const firstPass = res.headers.get("X-First-Pass") === "true";
    const isPrimary = docType === "eps";
    const banner = $("#banner");
    banner.className = "banner " + (pass ? "pass" : "fail");
    /* Verdict and number on the banner, arithmetic on hover: the score is
       already spelled out in the badge, and how it was reached only matters
       when it is not the number the team expected.
       X-Score is the company standing, which may rest on an earlier and better
       bundle than this one, so the arithmetic is spelled out from its parts
       rather than reverse-engineered from the total. */
    let text, why;
    /* Name the summary term only when there is one. A company that has sent
       nothing but other categories would otherwise read "15 = best patient
       summary + 15" and take it to mean its summary scored zero, when the
       truth is that it has not entered the track at all - which is the one
       thing worth telling it here. */
    const standing = bestEps
      ? "Company score " + score + " = best patient summary " + bestEps
        + (firstPass ? " + 20 first valid summary" : "")
        + (extras ? " + " + extras + " × 15 for " + extras + " other EHDS "
                  + (extras === 1 ? "document" : "documents") : "")
        + "."
      : "Company score " + score + ", all of it bonuses - no patient summary "
        + "has passed yet, and that is what the leaderboard ranks.";
    if (pass && isPrimary) {
      text = "✔ VALID";
      why = "This summary: 50 valid + " + sections + " distinct section"
        + (sections === 1 ? "" : "s") + " ×10"
        + (sections * 10 > 160 ? " (capped at +160)" : "")
        + " + " + resources + " resource" + (resources === 1 ? "" : "s") + " ×2"
        + (resources * 2 > 60 ? " (capped at +60)" : "")
        + " = " + mine + ". " + standing;
    } else if (pass) {
      text = "✔ VALID";
      /* Say plainly that this one does not earn richness points, or a team
         that padded a lab report with sections is left guessing why. */
      why = "A " + docLabel(docType) + " is worth a flat " + mine
        + " the first time it passes - the patient summary is what earns "
        + "section and resource points. " + standing;
    } else {
      text = "✖ INVALID - fix the " + errors + " error"
        + (errors === 1 ? "" : "s") + " below first";
      why = "Any error or fatal issue scores 0, whatever else the bundle carries."
        + (isPrimary ? "" : " Checked against the " + docLabel(docType) + " guide.")
        + (score ? " " + standing : "");
    }
    banner.title = why;
    /* The category is on the banner whenever it is not the track's own
       document, not only in the hover text: a team that meant to send a lab
       report and had it read as a patient summary - or the reverse - needs to
       see that before it reads a single finding. */
    banner.innerHTML = esc(text)
      + (isPrimary ? "" : '<span class="cat-chip" title="Validated against '
          + esc(docLabel(docType)) + '">' + esc(docShort(docType)) + "</span>")
      /* A failed bundle shows its own 0: the standing an earlier pass earned,
         printed beside "INVALID", reads as this bundle's score. It is still
         in the hover text. */
      + '<span class="score">' + (pass ? score : 0) + " pts</span>";
    renderIssues(outcome.issue || [], body);
    refreshLeaderboard();
  } catch (e) {
    errBox.textContent = "Network error: " + e.message;
    errBox.style.display = "block";
  } finally {
    $("#submit-btn").disabled = false;
    $("#spinner").classList.remove("on");
  }
});

/* ---- curl examples ---- */
const origin = window.location.origin;
$("#curl-register").textContent =
  "curl -s -X POST " + origin + "/fhir/\\$register \\\n" +
  "  -H 'Content-Type: application/fhir+json' \\\n" +
  "  -d '{\"resourceType\":\"Parameters\",\"parameter\":"
  + "[{\"name\":\"name\",\"valueString\":\"My Company\"}]}'";
$("#curl-submit").textContent =
  "curl -s -D - -X POST " + origin + "/fhir/Bundle/\\$validate \\\n" +
  "  -H 'Content-Type: application/fhir+json' \\\n" +
  "  -H 'Authorization: Bearer YOUR_TOKEN' \\\n" +
  "  --data-binary @my-eps-bundle.json";
$("#curl-leaderboard").textContent =
  "curl -s " + origin + "/fhir/\\$leaderboard";

/* ---- clinical view ---- */
let cvSubsByTeam = new Map();  // slug -> that company's submissions, fetched when picked
let cvSamples = [];
let cvCurrent = null;
/* Which view the open document is in. It follows the route rather than
   persisting as a preference: opening a document starts in CV_DEFAULT, so
   picking one summary apart in the dashboard does not decide how the next
   one opens. A /dashboard or /timeline URL still opens in that view. */
const CV_DEFAULT = "document";
let cvMode = CV_DEFAULT;
let cvDisclaimer = "";
/* The worked examples are one more entry in the company picker rather than a
   list of their own, so there is a single place to open a document from. */
const SAMPLE_TEAM = "__samples";
/* Previewing your own file is one more entry in the picker too. */
const PREVIEW_TEAM = "__preview";

async function initSamples() {
  try {
    const res = await fetch("/fhir/$samples");
    if (!res.ok) return;
    const data = await res.json();
    cvDisclaimer = paramOne(data, "disclaimer") || "";
    cvSamples = paramEntries(data, "entry").filter((s) => s.available);
  } catch (e) { /* the picker says so when the list is empty */ }
}

/* The picker follows the document, but only at the moment a different one
   opens: browsing another company's list while reading a summary should not be
   undone by a view switch, and Back onto someone else's document should not
   leave the picker pointing at the company you came from. */
function routeTeam() {
  const parts = routeParts();
  // a local preview belongs to no company, so leave the picker where it is
  if (parts[1] === "preview") return $("#cv-team").value || SAMPLE_TEAM;
  if (parts[1] === "company" && parts[2]) return parts[2];
  if (parts[1] === "submission") {
    if (cvCurrent && String(cvCurrent.id) === parts[2] && cvCurrent.team_slug) {
      return cvCurrent.team_slug;
    }
    for (const subs of cvSubsByTeam.values()) {
      const sub = subs.find((s) => String(s.id) === parts[2]);
      if (sub) return sub.team_slug;
    }
  }
  return SAMPLE_TEAM;
}

function syncTeamPicker() {
  const sel = $("#cv-team");
  const want = routeTeam();
  if (sel.value !== want && [...sel.options].some((o) => o.value === want)) {
    sel.value = want;
    renderCvList();
  }
}

let cvInitPending = false;  // initClinical reads the route itself once it has the list

async function initClinical() {
  cvInitPending = true;
  try {
    if (!cvSamples.length) await initSamples();
    /* The leaderboard is the complete list of companies; a page of recent
       submissions is not, once the event gets busy. */
    let teams;
    try {
      const res = await fetch("/fhir/$leaderboard");
      if (!res.ok) throw new Error("HTTP " + res.status);
      teams = paramEntries(await res.json(), "entry")
        .map(leaderboardRow).filter((t) => t.attempts > 0);
    } catch (e) {
      /* Say so rather than returning quietly. An empty picker beside no
         explanation reads as "nobody has submitted", and that is how a broken
         company list went unnoticed once already. */
      $("#cv-list").innerHTML = '<p class="muted">Could not load the list of '
        + "companies (" + esc(e.message) + ").</p>";
      return;
    }
    const slugs = new Set(teams.map((t) => t.team_slug));
    const sel = $("#cv-team");
    const current = sel.value;
    sel.innerHTML = '<option value="' + SAMPLE_TEAM + '">Example documents</option>'
      + teams.map((t) =>
          '<option value="' + esc(t.team_slug) + '">' + esc(t.team) + "</option>").join("")
      + '<option value="' + PREVIEW_TEAM + '">Preview your own file\u2026</option>';
    cvSubsByTeam = new Map();  // a fresh visit deserves fresh lists
    /* A #clinical/company route is an explicit request for that company, so it
       wins over whatever the picker happened to be showing; otherwise a company
       you had picked by hand survives a trip to another tab. */
    const asked = routeParts()[1] === "company" ? routeParts()[2] : null;
    if (asked && !slugs.has(asked)) { showUnknownCompany(asked); return; }
    sel.value = asked ? asked : slugs.has(current) ? current : routeTeam();
    renderCvList();
  } finally {
    cvInitPending = false;
  }
}

function selectCompany(slug) {
  if (cvInitPending) return;  // the route is applied when the picker is built
  const sel = $("#cv-team");
  if (!slug || sel.value === slug) return;
  if ([...sel.options].some((o) => o.value === slug)) {
    sel.value = slug;
    renderCvList();
  } else {
    showUnknownCompany(slug);
  }
}

/* A link to a company that has not submitted yet (or a mistyped slug) should
   say so, not leave a blank panel behind an unrelated picker value. */
function showUnknownCompany(slug) {
  $("#cv-team").selectedIndex = -1;
  $("#cv-list").innerHTML = '<p class="muted">No company called \u201c' + esc(slug)
    + '\u201d has submitted a document yet - pick one from the list above.</p>';
}

function renderSampleList(box) {
  if (!cvSamples.length) {
    box.innerHTML = '<p class="muted">No example documents are installed.</p>';
    return;
  }
  box.innerHTML = '<div class="table-wrap"><table><thead><tr>'
    + '<th scope="col">Patient</th><th scope="col">Language</th><th scope="col">Scenario</th>'
    + "</tr></thead><tbody>"
    + cvSamples.map((s) => '<tr class="cv-row" data-sample="' + esc(s.id)
        + '" title="click to open"><td><strong><a class="cv-open" href="#clinical/sample/'
        + esc(s.id) + '">' + esc(s.name) + "</a></strong>"
        + '<div class="cv-sub">' + esc(s.tagline) + "</div>"
        + (s.personnummer
            ? '<div class="cv-sub">' + esc(s.identifierLabel || "Personnummer") + " "
              + esc(s.personnummer) + "</div>"
            : "")
        + "</td>"
        + "<td>" + esc(s.language) + "</td>"
        + '<td class="cv-scenario">' + esc(s.scenario)
        + (s.scenarioUrl
            ? ' <a href="' + esc(s.scenarioUrl) + '" target="_blank" rel="noopener">'
              + esc(s.scenarioName || "Read the scenario") + " \u2197</a>"
            : "")
        + "</td></tr>").join("")
    + "</tbody></table></div>"
    + '<p class="muted sample-note">' + esc(cvDisclaimer) + "</p>";
}

const CV_LIST_LIMIT = 1000;  // the API's ceiling for one page

async function renderCvList() {
  const slug = $("#cv-team").value;
  const box = $("#cv-list");
  if (!slug) { box.innerHTML = ""; return; }
  if (slug === SAMPLE_TEAM) { renderSampleList(box); return; }
  if (slug === PREVIEW_TEAM) { renderPreviewBox(box); return; }
  let subs = cvSubsByTeam.get(slug);
  if (!subs) {
    box.innerHTML = '<p class="muted">Loading\u2026</p>';
    try {
      const res = await fetch("/fhir/$submissions?organization="
        + encodeURIComponent(slug) + "&count=" + CV_LIST_LIMIT);
      if (!res.ok) throw new Error("HTTP " + res.status);
      subs = paramEntries(await res.json(), "entry").map(submissionRow);
    } catch (e) {
      box.innerHTML = '<p class="muted">Could not load the submissions ('
        + esc(e.message) + ").</p>";
      return;
    }
    cvSubsByTeam.set(slug, subs);
    if ($("#cv-team").value !== slug) return;  // the reader moved on meanwhile
  }
  if (!subs.length) {
    box.innerHTML = '<p class="muted">No submissions yet.</p>';
    return;
  }
  box.innerHTML = '<div class="table-wrap"><table><thead><tr>'
    + '<th scope="col">Time</th><th scope="col">Status</th><th scope="col">Score</th>'
    + '<th scope="col">Sections</th><th scope="col">Resources</th>'
    + "</tr></thead><tbody>"
    + subs.map((s) => '<tr class="cv-row" data-id="' + s.id
      + '" title="click to open"><td><a class="cv-open" href="#clinical/submission/'
      + s.id + '">' + new Date(s.ts).toLocaleString() + "</a></td>"
      + "<td>" + (s.pass ? '<span class="badge pass">VALID</span>'
                         : '<span class="badge fail">INVALID</span>') + "</td>"
      + "<td><strong>" + s.score + "</strong></td>"
      + "<td>" + s.sections + "</td><td>" + s.resources + "</td></tr>").join("")
    + "</tbody></table></div>"
    + (subs.length >= CV_LIST_LIMIT
        ? '<p class="muted">Showing the latest ' + CV_LIST_LIMIT + " submissions.</p>" : "");
}

$("#cv-team").addEventListener("change", () => {
  /* On a company route the URL names the list on show; keep it truthful so a
     reload or a shared link lands on the company just picked, not the old one. */
  if (routeParts()[1] === "company") {
    const slug = $("#cv-team").value;
    if (slug === PREVIEW_TEAM) { renderCvList(); return; }
    history.replaceState(null, "", slug === SAMPLE_TEAM ? "#clinical"
                                                        : "#clinical/company/" + slug);
  }
  renderCvList();
});
$("#cv-list").addEventListener("click", (ev) => {
  if (ev.target.closest("a")) return;  // links (document, scenario brief) navigate themselves
  const row = ev.target.closest("tr.cv-row");
  if (!row) return;
  go("clinical/" + (row.dataset.sample ? "sample/" + row.dataset.sample
                                       : "submission/" + row.dataset.id)
     + "/" + CV_DEFAULT);
});

/* Refetching on a view switch would be a round trip to redraw data already in
   hand, so the loaded document is keyed and only replaced when the key moves. */
let cvDocKey = null;

async function showDoc(kind, id, scroll) {
  /* The readable view is view data, not FHIR, so it comes from /ui rather
     than pretending to be a resource. Samples and submissions share one id
     space, so one path serves both. */
  if (!kind || !id) { hideDoc(); return; }
  if (kind === "preview") { showPreviewDoc(scroll); return; }
  const path = "/ui/documents/";
  const key = kind + "/" + id;
  if (key === cvDocKey && cvCurrent) { renderClinicalDoc(cvCurrent); return; }
  let data;
  try {
    const res = await fetch(path + encodeURIComponent(id));
    if (!res.ok) {
      showDocMessage(res.status === 404
        ? "Document not found - the link may be stale, or the submission was removed."
        : "Could not load the document (HTTP " + res.status + ").");
      return;
    }
    data = await res.json();
  } catch (e) {
    showDocMessage("Could not load the document: " + e.message);
    return;
  }
  cvDocKey = key;
  cvCurrent = data;  // routeTeam reads the document's company from here
  syncTeamPicker();
  renderClinicalDoc(data);
  if (scroll) $("#cv-doc").scrollIntoView({ behavior: "smooth" });
}

function hideDoc() {
  cvDocKey = null;
  cvCurrent = null;
  $("#cv-doc").style.display = "none";
}

/* A silent blank where a document should be reads as "still loading". */
function showDocMessage(text) {
  hideDoc();
  const doc = $("#cv-doc");
  doc.innerHTML = '<p class="muted">' + esc(text) + "</p>";
  doc.style.display = "block";
}

function cvItemHtml(item) {
  // the status pill already says "active"; a flag chip repeating it is noise
  const chips = item.status
    ? cvChips(Object.assign({}, item,
        { flags: (item.flags || []).filter((f) => f !== item.status) }))
    : cvChips(item);
  return "<li>"
    + '<span class="cv-label">' + esc(item.label || "Unnamed item") + "</span>"
    + (item.status ? '<span class="cv-status">' + esc(item.status) + "</span>" : "")
    + chips
    + (item.detail ? '<div class="cv-detail muted">' + esc(item.detail) + "</div>" : "")
    + (item.note ? '<div class="cv-note muted">' + esc(item.note) + "</div>" : "")
    + "</li>";
}

/* ---- shared helpers for the clinician renderings ---- */
const CAT_LABEL = { alert: "Alert", allergy: "Allergy", problem: "Problem",
  medication: "Medicine", result: "Result", procedure: "Procedure",
  immunization: "Vaccination", plan: "Care plan", device: "Device", other: "Other" };
const CAT_ORDER = ["alert", "allergy", "problem", "medication", "result", "procedure",
  "immunization", "device", "plan", "other"];
const RISK_FLAGS = ["high risk", "high priority", "severe", "abnormal"];

/* Split deliberately: a resource the document never references is not part of
   any section, so counting it under one contradicts that section's own
   emptyReason assertion. Views that show everything use cvItems; views that
   speak for a section use cvSectionItems and surface orphans separately. */
function cvSectionItems(s) {
  const out = [];
  (s.sections || []).forEach((sec) => (sec.items || []).forEach((it) =>
    out.push(Object.assign({ section: sec.title || "Untitled section" }, it))));
  return out;
}
function cvOrphans(s) {
  return (s.unreferenced || []).map((it) =>
    Object.assign({ section: null, orphan: true }, it));
}
function cvItems(s) {
  return cvSectionItems(s).concat(cvOrphans(s));
}
const cvCat = (items, cat) => items.filter((i) => i.category === cat);
const hasFlag = (it, f) => (it.flags || []).indexOf(f) !== -1;
const hasGap = (it, g) => (it.gaps || []).indexOf(g) !== -1;
const countGap = (arr, g) => arr.filter((i) => hasGap(i, g)).length;
const plural = (n, one, many) => n + " " + (n === 1 ? one : (many || one + "s"));
const pctOf = (n, d) => (d ? Math.round((n / d) * 100) : 0);
const shortDate = (dt) => String(dt || "").slice(0, 10);

function cvChips(i) {
  let h = "";
  (i.flags || []).forEach((f) => {
    const kind = RISK_FLAGS.indexOf(f) !== -1 ? "risk" : (f === "active" ? "ok" : "warn");
    h += '<span class="chip cvc ' + kind + '">' + esc(f) + "</span>";
  });
  (i.gaps || []).forEach((g) => {
    h += '<span class="chip cvc gap">' + esc(g) + "</span>";
  });
  return h;
}

function cvAge(birth, on) {
  if (!birth) return null;
  const b = new Date(shortDate(birth));
  const ref = on ? new Date(shortDate(on)) : new Date();
  if (isNaN(b.getTime()) || isNaN(ref.getTime())) return null;
  let a = ref.getFullYear() - b.getFullYear();
  const m = ref.getMonth() - b.getMonth();
  if (m < 0 || (m === 0 && ref.getDate() < b.getDate())) a--;
  return a >= 0 && a < 130 ? a : null;
}

function sectionFor(sections, kind) {
  return (sections || []).find((x) => x.kind === kind) || null;
}

/* ---- B: clinical timeline ---- */
/* String comparison would sort an unparseable date above every real event,
   silently moving the newest entry off the top of the timeline. */
function tlTime(v) {
  const t = Date.parse(shortDate(v));
  return isNaN(t) ? null : t;
}

function viewTimeline(d) {
  const s = d.summary || {};
  const q = s.quality || {};
  const items = cvItems(s);
  const dated = items.filter((i) => tlTime(i.date) !== null)
    .sort((a, b) => tlTime(b.date) - tlTime(a.date));
  const undated = items.filter((i) => tlTime(i.date) === null);
  const cats = CAT_ORDER.filter((c) => items.some((i) => i.category === c));
  let h = '<div class="tl-legend">' + cats.map((c) =>
    '<span class="c-' + esc(c) + '"><i class="cdot"></i>' + esc(CAT_LABEL[c] || c)
    + " (" + cvCat(items, c).length + ")</span>").join("") + "</div>";
  h += '<p class="muted" style="margin:8px 0 0">'
    + plural(dated.length, "dated entry", "dated entries")
    + (q.earliest && q.latest
        ? ", " + esc(shortDate(q.earliest)) + " to " + esc(shortDate(q.latest)) : "")
    + (undated.length ? " &middot; " + undated.length + " undated" : "") + "</p>";

  if (dated.length) {
    let year = null;
    h += '<div class="tl">';
    dated.forEach((i) => {
      const y = String(i.date).slice(0, 4);
      if (y !== year) { year = y; h += '<div class="tl-year">' + esc(y) + "</div>"; }
      h += '<div class="tl-row c-' + esc(i.category || "other") + '">'
        + '<div class="tl-date">' + esc(shortDate(i.date)) + " &middot; "
        + esc(CAT_LABEL[i.category] || "Entry")
        + (i.status ? " &middot; " + esc(i.status) : "") + "</div>"
        + '<div class="tl-label">' + esc(i.label || "Unnamed item") + cvChips(i) + "</div>"
        + (i.detail ? '<div class="tl-detail">' + esc(i.detail) + "</div>" : "")
        + (i.note ? '<div class="tl-detail tl-note">' + esc(i.note) + "</div>" : "")
        + "</div>";
    });
    h += "</div>";
  } else {
    h += '<p class="muted">No entry carries a date, so this document cannot be'
      + " placed on a timeline at all.</p>";
  }

  if (undated.length) {
    h += '<div class="tl-undated"><strong>'
      + plural(undated.length, "entry", "entries") + " with no usable date</strong>"
      + '<p class="muted" style="margin:2px 0 4px;font-size:0.8rem">Present in the'
      + " document but impossible to place in the patient's history.</p>"
      + '<ul class="cv-items">' + undated.map(cvItemHtml).join("") + "</ul></div>";
  }

  const asserted = (s.sections || [])
    .filter((x) => !(x.items || []).length && x.empty_reason);
  if (asserted.length) {
    h += '<div class="tl-asserted"><strong>'
      + plural(asserted.length, "section", "sections")
      + " asserting there is nothing to report</strong>"
      + '<p class="muted" style="margin:2px 0 4px;font-size:0.8rem">Nothing to'
      + " place on the timeline, but the author positively stated these are"
      + " empty. That is a clinical finding, not missing data.</p>"
      + '<ul class="cv-items">' + asserted.map((x) => "<li>"
          + esc(x.title || "Untitled section") + '<span class="chip cvc ok">'
          + esc(x.empty_reason) + "</span></li>").join("")
      + "</ul></div>";
  }
  return h;
}

/* ---- C: safety dashboard ---- */
function dbCard(title, body, cls) {
  return '<div class="db-card' + (cls ? " " + cls : "") + '"><h4>' + esc(title)
    + "</h4>" + body + "</div>";
}
function dbList(arr, n, fn) {
  return '<ul class="db-list">' + arr.slice(0, n).map(fn).join("") + "</ul>"
    + (arr.length > n ? '<p class="db-more">and ' + (arr.length - n) + " more</p>" : "");
}
function dbMeter(label, n, total) {
  const v = pctOf(n, total);
  const cls = v >= 90 ? "" : (v >= 60 ? " mid" : " low");
  return '<div class="mt"><div class="mt-top"><span>' + esc(label) + "</span><span>"
    + v + "%</span></div>"
    + '<div class="meter' + cls + '"><span style="width:' + v + '%"></span></div>'
    + '<div class="db-more">' + n + " of " + total + "</div></div>";
}
function dbRow(i) {
  return "<li>" + esc(i.label || "Unnamed item") + cvChips(i)
    + (i.date ? ' <span class="db-more" style="display:inline">'
        + esc(shortDate(i.date)) + "</span>" : "") + "</li>";
}

function viewDashboard(d) {
  const s = d.summary || {};
  const q = s.quality || {};
  const p = s.patient;
  const sections = s.sections || [];
  const items = cvSectionItems(s);
  const orphans = cvOrphans(s);
  const alerts = cvCat(items, "alert");
  const allergies = cvCat(items, "allergy");
  const problems = cvCat(items, "problem");
  const meds = cvCat(items, "medication");
  const results = cvCat(items, "result");
  const shots = cvCat(items, "immunization");
  const procedures = cvCat(items, "procedure");
  const devices = cvCat(items, "device");
  const plans = cvCat(items, "plan");
  const other = cvCat(items, "other");
  const docDate = (s.composition || {}).date;
  let h = '<div class="db-grid">';

  const age = p ? cvAge(p.birth_date, docDate) : null;
  h += dbCard("Patient",
    '<div class="db-big">' + esc((p && p.name) || "Unnamed") + "</div>"
    + '<div class="db-sub">'
    + [age !== null ? age + " years" : null, p && p.gender ? esc(p.gender) : null,
       p && p.birth_date ? "born " + esc(shortDate(p.birth_date)) : null]
      .filter(Boolean).join(" &middot; ")
    + "</div>", p && p.name ? "" : "alert");

  /* The author's own "read this first": a difficult airway, an
     immunosuppressant. Straight after the patient, before the allergies the
     reader would otherwise start with, and its note shown in full - the note
     is where the author says what to do about it. */
  if (alerts.length) {
    const urgent = alerts.filter((a) => hasFlag(a, "high priority"));
    h += dbCard("Alerts",
      '<div class="db-big">' + alerts.length
      + (urgent.length ? ' <span class="chip cvc risk">' + urgent.length
          + " high priority</span>" : "") + "</div>"
      + dbList(urgent.concat(alerts.filter((a) => urgent.indexOf(a) === -1)), 5,
          (i) => dbRow(i) + (i.note
            ? '<li class="db-note">' + esc(i.note) + "</li>" : "")),
      urgent.length ? "alert" : "");
  }

  /* A narrative-only section (the patient story: call him Ville, mother
     knows the airway history) has no entries to count, so it is shown as
     the prose it is, right after the alerts. */
  sections.filter((x) => x.narrative).forEach((x) => {
    h += dbCard(x.title || "Patient story",
      '<div class="db-prose">' + x.narrative.split("\n")
        .map((line) => "<p>" + esc(line) + "</p>").join("") + "</div>");
  });

  const high = allergies.filter((a) => hasFlag(a, "high risk"));
  const orphanAllergies = cvCat(orphans, "allergy");
  const allergySec = sectionFor(sections, "allergy");
  let aBody;
  let aCls = "";
  if (allergies.length) {
    /* Truncating in source order can drop a high-risk allergy off a list that
       still shows five low-risk ones, so risk comes first. */
    aBody = '<div class="db-big">' + allergies.length
      + (high.length ? ' <span class="chip cvc risk">' + high.length
          + " high risk</span>" : "") + "</div>"
      + dbList(high.concat(allergies.filter((a) => !hasFlag(a, "high risk"))),
               5, dbRow);
    aCls = high.length ? "alert" : "";
  } else if (allergySec && allergySec.empty_reason) {
    aBody = '<div class="db-big">' + esc(allergySec.empty_reason) + "</div>"
      + (orphanAllergies.length
          ? '<div class="db-sub">Contradicted: '
            + plural(orphanAllergies.length, "allergy resource",
                     "allergy resources")
            + " sits in the bundle unreferenced, so the assertion and the data"
            + " disagree. Do not rely on either.</div>"
            + dbList(orphanAllergies, 5, dbRow)
          : '<div class="db-sub">Positively asserted by the author - safe to'
            + " rely on.</div>");
    aCls = orphanAllergies.length ? "alert" : "good";
  } else {
    aBody = '<div class="db-big">Unknown</div><div class="db-sub">Neither an allergy'
      + " nor a statement that none is known.</div>";
    aCls = "alert";
  }
  h += dbCard("Allergies and intolerances", aBody, aCls);

  const active = problems.filter((x) => hasFlag(x, "active"));
  h += dbCard("Problems",
    '<div class="db-big">' + (active.length || problems.length) + "</div>"
    + '<div class="db-sub">' + (active.length ? "active of " + problems.length
        + " recorded" : (problems.length ? "recorded, none marked active"
        : "nothing recorded")) + "</div>"
    + dbList(active.length ? active : problems, 5, dbRow),
    problems.filter((x) => hasFlag(x, "severe")).length ? "alert" : "");

  const activeMeds = meds.filter((x) => hasFlag(x, "active"));
  const medsShown = activeMeds.length ? activeMeds : meds;
  const noDose = countGap(medsShown, "no dose");
  h += dbCard("Current medication",
    '<div class="db-big">' + (activeMeds.length || meds.length) + "</div>"
    + '<div class="db-sub">' + (activeMeds.length
        ? "active of " + meds.length + " recorded"
          + (noDose ? ", " + noDose + " without a usable dose" : "")
        : (meds.length ? "recorded, none marked active" : "nothing recorded"))
    + "</div>"
    + dbList(medsShown, 5, dbRow), noDose ? "alert" : "");

  if (results.length) {
    const recent = results.slice()
      .sort((a, b) => (a.date || "") < (b.date || "") ? 1 : -1);
    const abn = results.filter((r) => hasFlag(r, "abnormal"));
    h += dbCard("Results",
      '<div class="db-big">' + results.length
      + (abn.length ? ' <span class="chip cvc risk">' + abn.length
          + " abnormal</span>" : "") + "</div>"
      + dbList(abn.length ? abn.concat(recent.filter((r) => abn.indexOf(r) === -1))
               : recent, 5, dbRow),
      abn.length ? "alert" : "");
  }
  if (shots.length) {
    h += dbCard("Vaccinations", '<div class="db-big">' + shots.length + "</div>"
      + dbList(shots.slice().sort((a, b) => (a.date || "") < (b.date || "") ? 1 : -1),
               4, dbRow));
  }
  if (procedures.length) {
    h += dbCard("Procedures", '<div class="db-big">' + procedures.length + "</div>"
      + dbList(procedures.slice()
          .sort((a, b) => (a.date || "") < (b.date || "") ? 1 : -1), 5, dbRow));
  }
  if (devices.length) {
    h += dbCard("Devices and implants",
      '<div class="db-big">' + devices.length + "</div>"
      + dbList(devices, 5, dbRow));
  }
  if (plans.length) {
    h += dbCard("Care plan", '<div class="db-big">' + plans.length + "</div>"
      + dbList(plans, 5, dbRow));
  }
  if (other.length) {
    h += dbCard("Other entries", '<div class="db-big">' + other.length + "</div>"
      + '<div class="db-sub">carried by the document but outside the standard'
      + " summary categories</div>" + dbList(other, 5, dbRow));
  }

  const empty = sections.filter((x) => !(x.items || []).length && !x.narrative);
  h += dbCard("Document coverage",
    '<div class="db-big">' + (sections.length - empty.length) + " / "
    + sections.length + "</div>"
    + '<div class="db-sub">sections carrying data</div>'
    + (empty.length ? dbList(empty, 6, (x) => "<li>" + esc(x.title || "Untitled")
        + (x.empty_reason ? '<span class="chip cvc ok">' + esc(x.empty_reason)
            + "</span>" : '<span class="chip cvc gap">no reason given</span>')
        + "</li>") : ""),
    empty.some((x) => !x.empty_reason) ? "alert" : "");

  const total = q.items || 0;
  let dq = dbMeter("Coded with a terminology", q.coded || 0, total)
    + dbMeter("Placed in time", q.dated || 0, total);
  const gaps = q.gaps || {};
  const gapKeys = Object.keys(gaps);
  if (gapKeys.length) {
    dq += dbList(gapKeys, 5, (g) => "<li>" + esc(g)
      + '<span class="chip cvc gap">' + gaps[g] + "</span></li>");
  } else if (total) {
    dq += '<p class="db-more">No gaps detected in ' + total + " entries.</p>";
  }
  h += dbCard("Data quality (" + total + " entries)", dq,
    total && pctOf((q.items || 0) - (q.with_gaps || 0), total) < 60 ? "alert" : "");

  if ((s.unreferenced || []).length) {
    h += dbCard("Unreachable resources",
      '<div class="db-big">' + s.unreferenced.length + "</div>"
      + '<div class="db-sub">in the bundle but not referenced by any section, so a'
      + " conforming viewer may never show them</div>"
      + dbList(s.unreferenced, 5, dbRow), "alert");
  }
  return h + "</div>";
}

/* ---- baseline: plain document rendering ---- */
function viewDocument(d) {
  const s = d.summary || {};
  let html = "";
  const p = s.patient;
  if (p) {
    const meta = [p.birth_date ? "Born " + esc(p.birth_date) : null,
                  p.gender ? esc(p.gender) : null].filter(Boolean).join(" &middot; ");
    html += '<div class="patient-card">'
      + '<div class="patient-name">' + esc(p.name || "Unnamed patient") + "</div>"
      + (meta ? "<div>" + meta + "</div>" : "")
      + (p.identifiers && p.identifiers.length
        ? '<div class="muted" style="font-size:0.8rem">'
          // the assigning authority's OID is plumbing; the number on the card
          // is what identifies the patient to anyone reading this
          + p.identifiers.map((i) => esc(i.value)).join(" &middot; ") + "</div>"
        : "")
      + "</div>";
  } else {
    html += '<p class="muted">No patient details found in this document.</p>';
  }
  for (const sec of s.sections || []) {
    html += '<h3 class="cv-sec">' + esc(sec.title || "Untitled section") + "</h3>";
    if (sec.items && sec.items.length) {
      html += '<ul class="cv-items">' + sec.items.map(cvItemHtml).join("") + "</ul>";
    } else if (sec.narrative) {
      // a section that is its narrative (a patient story): the prose is the
      // content, shown as written
      html += '<div class="cv-narrative">' + sec.narrative.split("\n")
        .map((line) => "<p>" + esc(line) + "</p>").join("") + "</div>";
    } else if (sec.empty_reason) {
      html += '<p class="muted" style="margin:2px 0 8px">No entries - '
        + esc(sec.empty_reason) + "</p>";
    } else {
      html += '<p class="muted" style="margin:2px 0 8px">No entries.</p>';
    }
  }
  if (s.unreferenced && s.unreferenced.length) {
    html += '<h3 class="cv-sec muted">Not referenced by any section</h3>'
      + '<p class="muted" style="margin:0;font-size:0.8rem">These items are in the'
      + " document but no section points at them (data-quality hint).</p>"
      + '<ul class="cv-items">' + s.unreferenced.map(cvItemHtml).join("") + "</ul>";
  }
  return html;
}

/* Insertion order is the order of the buttons, and the first one is what a
   document opens in: the plain rendering is the thing being judged, so it comes
   before the two interpretations of it. */
const CV_VIEWS = {
  document: { fn: viewDocument, name: "Document view",
    why: "The plain section-by-section rendering." },
  dashboard: { fn: viewDashboard, name: "Dashboard view",
    why: "Risk and completeness first - what would stop you prescribing, before"
       + " you read anything else." },
  timeline: { fn: viewTimeline, name: "Timeline view",
    why: "Chronology across every section - shows recency, and whether the"
       + " record is current enough to act on." },
};

/* Rebuilt with the document rather than parked in the picker card, so the
   switcher is next to the thing it switches. The rationale that used to sit
   under it as a paragraph is now the button's tooltip. */
function cvModesHtml() {
  return '<div class="cv-modes">' + Object.keys(CV_VIEWS).map((k) =>
    '<button class="cv-mode' + (k === cvMode ? " active" : "")
    + '" data-mode="' + k + '" title="' + esc(CV_VIEWS[k].why) + '">'
    + esc(CV_VIEWS[k].name) + "</button>").join("") + "</div>";
}

function renderClinicalDoc(d) {
  cvCurrent = d;
  const s = d.summary || {};
  const comp = s.composition || {};
  let html = "";
  if (!d.sample && !d.preview && !d.pass) {
    html += '<div class="warn-banner">Invalid submission - this document failed'
      + " validation and would be rejected in production. Content shown as-is.</div>";
  }
  if (d.bundle_readable === false) {
    html += '<div class="warn-banner">The stored document no longer parses as'
      + " JSON. The validation verdict below is the one recorded at submission"
      + " time; nothing under it was read from the file.</div>";
  }
  html += cvModesHtml();
  html += "<h2>" + esc(comp.title || "Patient summary") + "</h2>";
  html += '<p class="muted" style="margin:2px 0 0">' + esc(d.team)
    + (d.preview
        ? " of " + esc(d.filename || "pasted JSON")
          + " &middot; not uploaded, validated or scored"
      : d.sample
        ? ' &middot; <a href="/fhir/Bundle/' + esc(d.sample.id)
          + '" download>download the FHIR bundle</a>'
        : " &middot; submitted " + new Date(d.ts).toLocaleString()
          + " &middot; " + (d.pass ? "valid" : "invalid") + ", score " + d.score
          + (d.errors ? ", " + plural(d.errors, "error") : "")
          + ' &middot; <a href="/fhir/Bundle/' + d.id
          + '" download>download the FHIR bundle</a>')
    + (comp.date ? " &middot; document date " + esc(comp.date.split("T")[0]) : "")
    + "</p>";
  html += (Object.hasOwn(CV_VIEWS, cvMode) ? CV_VIEWS[cvMode] : CV_VIEWS.document).fn(d);
  const doc = $("#cv-doc");
  doc.innerHTML = html;
  doc.style.display = "block";
}

// delegated: the buttons are rebuilt with every document render
$("#cv-doc").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".cv-mode");
  if (!btn) return;
  cvMode = btn.dataset.mode;
  const parts = routeParts();
  if (parts[1] && parts[2]) go(["clinical", parts[1], parts[2], cvMode].join("/"), true);
  else if (cvCurrent) renderClinicalDoc(cvCurrent);
});

/* ---- local preview ----
   A bundle on the reader's own disk, run through the same readable view as a
   submission. The server only summarises it; the result lives in this tab
   (and sessionStorage, so a reload or view switch keeps it) and nowhere else. */
const CV_PREVIEW_KEY = "cv-preview";
let cvPreview = null;

function previewMsg(text) {
  const box = $("#cv-preview-msg");
  box.textContent = text || "";
  box.style.display = text ? "block" : "none";
}

async function previewBundle(text, filename) {
  previewMsg("");
  let data;
  try {
    const res = await fetch("/ui/preview", { method: "POST",
      headers: { "Content-Type": "application/fhir+json" }, body: text });
    if (!res.ok) {
      let why = "HTTP " + res.status;
      try { why = (await res.json()).issue[0].diagnostics || why; } catch (e) { /* keep the status */ }
      previewMsg("Could not preview " + (filename || "that") + ": " + why);
      return;
    }
    data = await res.json();
  } catch (e) {
    previewMsg("Could not preview " + (filename || "that") + ": " + e.message);
    return;
  }
  data.filename = filename || null;
  cvPreview = data;
  try { sessionStorage.setItem(CV_PREVIEW_KEY, JSON.stringify(data)); } catch (e) { /* too big or blocked: it still shows */ }
  cvDocKey = null;  // a second preview reuses the route, so force the redraw
  go("clinical/preview/local/" + CV_DEFAULT);
}

async function previewFile(file) {
  if (!file) return;
  try { await previewBundle(await file.text(), file.name); }
  catch (e) { previewMsg("Could not read " + file.name + ": " + e.message); }
}

function showPreviewDoc(scroll) {
  if (!cvPreview) {
    try { cvPreview = JSON.parse(sessionStorage.getItem(CV_PREVIEW_KEY) || "null"); }
    catch (e) { cvPreview = null; }
  }
  if (!cvPreview) {
    showDocMessage("Nothing to preview - pick a file again.");
    return;
  }
  const first = cvDocKey !== "preview/local";
  cvDocKey = "preview/local";
  renderClinicalDoc(cvPreview);
  if (scroll || first) $("#cv-doc").scrollIntoView({ behavior: "smooth" });
}

function renderPreviewBox(box) {
  box.innerHTML = '<div class="cv-paste">'
    + '<p class="muted" style="margin:0">Pick a Bundle .json or paste one below.'
    + " It is only rendered here, not uploaded to the hackathon.</p>"
    + '<textarea id="cv-paste" spellcheck="false" placeholder="{ &quot;resourceType&quot;: &quot;Bundle&quot;, ... }"></textarea>'
    + '<div class="row"><button type="button" id="cv-paste-go">Preview</button>'
    + '<button type="button" class="ghost" id="cv-pick">Choose a file&hellip;</button></div>'
    + "</div>";
  $("#cv-pick").addEventListener("click", () => {
    $("#cv-file").value = "";  // picking the same file again should still fire
    $("#cv-file").click();
  });
  $("#cv-paste-go").addEventListener("click", () => {
    const text = $("#cv-paste").value.trim();
    if (text) previewBundle(text, null); else previewMsg("Paste a bundle first.");
  });
}

$("#cv-file").addEventListener("change", () => previewFile($("#cv-file").files[0]));

/* ---- interop testing ---- */
/* The same string as ENDPOINT_DESCRIPTION_EXT: R5's Endpoint.description,
   carried on R4 as a cross-version extension. This page is served as
   written, so it is spelled out twice; a test holds the copies together. */
const ENDPOINT_DESCRIPTION_EXT =
  "http://hl7.org/fhir/5.0/StructureDefinition/extension-Endpoint.description";
function interopCurls() {
  $("#curl-exchange").textContent =
    "# every published document - organizationSlug is the PARTNER_SLUG below,\n" +
    "# and it is in the GET line on each card above:\n" +
    "curl -s " + origin + "/fhir/\\$exchange\n" +
    "# or only the companies with this in their name - quoted for the ?, and\n" +
    "# a $ inside single quotes needs no backslash:\n" +
    "curl -s '" + origin + "/fhir/$exchange?name=acme'\n" +
    "curl -s " + origin + "/fhir/Organization/PARTNER_SLUG/\\$document"
      + " -o partner-eps.json\n\n" +
    "# publish a screenshot of how it came out in your system - the image goes\n" +
    "# in the attachment, base64-encoded:\n" +
    "curl -X POST " + origin + "/fhir/DocumentReference \\\n" +
    "     -H 'Authorization: Bearer TOKEN' \\\n" +
    "     -H 'Content-Type: application/fhir+json' -d '{\n" +
    '       "resourceType": "DocumentReference", "status": "current",\n' +
    '       "description": "Allergies came through empty",\n' +
    '       "custodian": {"reference": "Organization/PARTNER_SLUG"},\n' +
    '       "content": [{"attachment": {"contentType": "image/png",\n' +
    '                                   "data": "\'\"$(base64 -w0 shot.png)\"\'"}}]}\'\n' +
    "# every screenshot of one document:\n" +
    "curl -s '" + origin + "/fhir/DocumentReference?related=ID'\n\n" +
    "# or have documents sent to you: advertise where your system takes them\n" +
    "# in - hl7-fhir-rest for a FHIR base, or web-page for a how-to page:\n" +
    "curl -X POST " + origin + "/fhir/Endpoint \\\n" +
    "     -H 'Authorization: Bearer TOKEN' \\\n" +
    "     -H 'Content-Type: application/fhir+json' -d '{\n" +
    '       "resourceType": "Endpoint", "status": "active",\n' +
    '       "connectionType": {"code": "hl7-fhir-rest"},\n' +
    '       "name": "Staging server", "address": "https://YOUR-PLATFORM/fhir",\n' +
    '       "extension": [{"url": "' + ENDPOINT_DESCRIPTION_EXT + '",\n' +
    '         "valueString": "POST a document Bundle to /Bundle"}]}\'\n' +
    "# everyone's advertised endpoints, or one company's:\n" +
    "curl -s " + origin + "/fhir/Endpoint\n" +
    "curl -s '" + origin + "/fhir/Endpoint?organization=PARTNER_SLUG'";
}
$("#token").addEventListener("input", (ev) => syncToken(ev.target.value, "token"));

function shotCountLabel(n) {
  return n ? "Seen in " + n + " other system" + (n === 1 ? "" : "s")
           : "No other system has shown this yet";
}

/* Only one number on one card changes when a screenshot lands or is withdrawn.
   Rebuilding the whole list to show it would take the upload form, its
   confirmation and the open gallery down with it. */
async function refreshShotCount(slug) {
  try {
    const res = await fetch("/fhir/$exchange");
    if (!res.ok) return;
    const p = paramEntries(await res.json(), "entry").map(partnerRow)
      .find((x) => x.team_slug === slug);
    const btn = document.querySelector('.io-partner[data-slug="' + slug
      + '"] [data-shots]');
    if (p && btn) btn.textContent = shotCountLabel(p.renderings);
  } catch (e) { /* the count is decoration; the gallery below it is the truth */ }
}

function partnerCard(p) {
  return '<div class="io-partner" data-slug="' + esc(p.team_slug) + '">'
    + "<strong>" + esc(p.team) + "</strong>"
    + '<p class="muted" style="margin:6px 0 0"><code>GET /fhir/Organization/'
    + esc(p.team_slug) + "/$document</code> \u00b7 Document #" + p.exchange_id
    + " \u00b7 " + p.sections + " sections \u00b7 " + p.resources + " resources \u00b7 "
    + '<a href="#clinical/submission/' + p.exchange_id + '">view document</a>'
    + ' \u00b7 <a href="/fhir/Bundle/' + p.exchange_id
    + '" download>download the FHIR bundle</a></p>'
    + '<div class="io-actions">'
    + '<button class="ghost" data-shots="' + p.exchange_id + '">'
    + shotCountLabel(p.renderings) + "</button>"
    /* Your own document is the one card you cannot add to - a rendering is by
       definition somebody else reading it. Saying so here beats letting
       someone crop a screenshot first and refusing it afterwards. */
    + (me && me.id === p.team_slug
        ? '<span class="muted">Your document - other companies add the screenshots.</span>'
        : '<button class="ghost" data-add="' + esc(p.team_slug) + '">'
          + "I loaded this - add a screenshot</button>")
    + "</div>"
    + (me && me.id === p.team_slug ? "" :
      '<div class="shot-form" hidden data-form="' + esc(p.team_slug) + '">'
    + '<label for="shot-file-' + esc(p.team_slug) + '">Screenshot (PNG, JPEG or WebP)</label>'
    + '<input type="file" accept="image/png,image/jpeg,image/webp" id="shot-file-'
    + esc(p.team_slug) + '">'
    + '<label for="shot-note-' + esc(p.team_slug) + '">What should someone look at?</label>'
    + '<textarea id="shot-note-' + esc(p.team_slug) + '" maxlength="' + maxNote
    + '" placeholder="'
    + "e.g. Allergies came through, but the penicillin severity is missing and "
    + 'the medication list dropped every dose."></textarea>'
    + '<div class="io-actions"><button data-upload="' + esc(p.team_slug)
    + '">Upload</button>'
    + '</div><p class="shot-status" hidden></p></div>')
    + '<div class="shots" hidden data-gallery="' + p.exchange_id + '"></div></div>';
}

/* Screenshots are full-screen grabs: 2-3 MB of PNG each, and a gallery of
   them on venue wifi is the difference between this working and not. The
   browser can do the resizing, so nothing has to be installed on the server
   to make an upload a tenth of the size. */
async function shrinkImage(file) {
  try {
    const bmp = await createImageBitmap(file);
    const scale = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
    const cv = document.createElement("canvas");
    cv.width = Math.round(bmp.width * scale);
    cv.height = Math.round(bmp.height * scale);
    cv.getContext("2d").drawImage(bmp, 0, 0, cv.width, cv.height);
    const blob = await new Promise((r) => cv.toBlob(r, "image/webp", 0.85));
    /* An already-small screenshot can come out bigger re-encoded; keep whichever
       is smaller, and keep the original if the browser gave us nothing. */
    return blob && blob.size < file.size ? blob : file;
  } catch (e) {
    return file;
  }
}

/* Which company the saved token belongs to, so the gallery can offer Edit and
   Withdraw on your own screenshots and nobody else's. Null when no token is
   saved, which is every clinician reading the wall. */
let me = null;
/* "none" with no token, "known", "rejected" when the server does not know the
   token (a typo, or one from before a practice reset), "unreachable" when it
   could not be asked. Only "rejected" may send someone to register again. */
let meState = "none";
let meToken = "";
let meAsked = 0;
async function whoAmI() {
  const token = $("#token").value.trim();
  const ask = ++meAsked;
  let next = null, state = "none";
  /* fetch throws before sending a header with characters it cannot carry - a
     token pasted with smart quotes, say - which would read as the server being
     down, for ever. Tokens are plain ASCII, so anything else is just wrong. */
  if (token && !/^[\x21-\x7e]+$/.test(token)) state = "rejected";
  else if (token) {
    try {
      const res = await fetch("/fhir/Organization/$me", {
        headers: { "Authorization": "Bearer " + token },
      });
      /* An Organization: `id` is the slug, `name` the company name. */
      if (res.ok) { next = await res.json(); state = "known"; }
      else if (res.status === 401) state = "rejected";
      else state = "unreachable";
    } catch (e) { state = "unreachable"; }
    /* A blip says nothing about the token, so the company it last belonged to
       stands. */
    if (state === "unreachable" && meToken === token) next = me;
  }
  /* Typing fires several of these; only the answer about the token in the
     field now counts, whichever order they come back in. */
  if (ask !== meAsked) return;
  me = next;
  meState = state;
  meToken = token;
}

/* A DocumentReference, flattened to what the tile draws. author is the company
   that took the screenshot, custodian the one whose document it is, and the
   attachment url is the Binary the bytes are served from. */
function shotFromDocRef(d) {
  const author = (d.author || [])[0] || {};
  const custodian = d.custodian || {};
  const attachment = ((d.content || [])[0] || {}).attachment || {};
  const refId = (ref) => String(ref || "").split("/").pop();
  return {
    id: d.id,
    document: refId((((d.context || {}).related || [])[0] || {}).reference),
    producer: custodian.display,
    producer_slug: refId(custodian.reference),
    consumer: author.display,
    consumer_slug: refId(author.reference),
    ts: d.date,
    note: d.description || "",
    image: attachment.url,
  };
}

function shotTile(s) {
  const mine = me && me.id === s.consumer_slug;
  return '<figure class="shot" data-shot="' + s.id + '">'
    + '<a href="' + s.image + '" target="_blank" rel="noopener">'
    + '<img loading="lazy" src="' + s.image + '" alt="'
    + esc(s.producer) + "'s document as shown by " + esc(s.consumer) + '"></a>'
    + '<figcaption class="shot-meta">'
    + '<span class="shot-who">' + esc(s.consumer) + "</span>"
    + '<span class="muted">' + fmtWhen(s.ts) + "</span>"
    + '<span class="shot-note' + (s.note ? '">' + esc(s.note)
        : ' empty">No note - ask them what it shows.') + "</span>"
    + (mine ? '<span class="shot-own">'
        + '<button class="ghost" data-edit="' + s.id + '">Edit note</button>'
        + '<button class="ghost" data-del="' + s.id + '">Withdraw</button></span>' : "")
    + "</figcaption></figure>";
}

async function loadGallery(docId) {
  const box = document.querySelector('[data-gallery="' + docId + '"]');
  if (!box) return;
  try {
    const res = await fetch("/fhir/DocumentReference?related=" + docId);
    const shots = res.ok
      ? ((await res.json()).entry || []).map((e) => shotFromDocRef(e.resource))
      : [];
    box.innerHTML = shots.length
      ? shots.map(shotTile).join("")
      : '<p class="muted">Nobody has published a screenshot of this document yet.</p>';
  } catch (e) {
    box.innerHTML = '<p class="muted">Could not load the screenshots.</p>';
  }
}

function shotStatus(form, text, isError) {
  const p = form.querySelector(".shot-status");
  p.textContent = text;
  p.classList.toggle("err", !!isError);
  p.hidden = !text;
}

async function uploadShot(slug) {
  const form = document.querySelector('[data-form="' + slug + '"]');
  const file = form.querySelector("input[type=file]").files[0];
  const note = form.querySelector("textarea").value.trim();
  const token = $("#token").value.trim();
  if (!token) return shotStatus(form, "Paste your company token on the Submit tab first.", true);
  if (!file) return shotStatus(form, "Choose a screenshot first.", true);
  const btn = form.querySelector("[data-upload]");
  btn.disabled = true;
  shotStatus(form, "Uploading...");
  try {
    const blob = await shrinkImage(file);
    const res = await fetch("/fhir/DocumentReference", {
      method: "POST",
      headers: { "Authorization": "Bearer " + token,
                 "Content-Type": "application/fhir+json" },
      body: JSON.stringify({
        resourceType: "DocumentReference",
        status: "current",
        description: note,
        custodian: { reference: "Organization/" + slug },
        content: [{ attachment: {
          contentType: blob.type || "application/octet-stream",
          data: await blobToBase64(blob),
        } }],
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      shotStatus(form, detailText(body) || ("Upload failed (" + res.status + ")"), true);
      return;
    }
    const data = shotFromDocRef(body);
    shotStatus(form, "Published - thank you.");
    form.querySelector("input[type=file]").value = "";
    form.querySelector("textarea").value = "";
    await refreshShotCount(slug);
    const gallery = document.querySelector('[data-gallery="' + data.document + '"]');
    if (gallery) { gallery.hidden = false; loadGallery(data.document); }
  } catch (e) {
    shotStatus(form, "Network error: " + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

function shotErr(tile, text) {
  let p = tile.querySelector(".shot-err");
  if (!p) {
    p = document.createElement("span");
    p.className = "shot-err";
    tile.querySelector(".shot-meta").appendChild(p);
  }
  p.textContent = text;
}

/* The note is edited where it is shown. A browser modal would be shorter code
   and the wrong control: a note runs to a couple of sentences about what is
   wrong in the screenshot beside it, and nothing else here opens a dialog. */
function editShotNote(id) {
  const tile = document.querySelector('[data-shot="' + id + '"]');
  if (tile.querySelector(".shot-edit")) return;
  const note = tile.querySelector(".shot-note");
  note.hidden = true;
  tile.querySelector(".shot-own").hidden = true;
  const box = document.createElement("div");
  box.className = "shot-edit";
  box.innerHTML = '<textarea rows="3" maxlength="' + maxNote
    + '" placeholder="What should someone look at here?"></textarea>'
    + '<span class="shot-own">'
    + '<button class="ghost" data-save="' + id + '">Save note</button>'
    + '<button class="ghost" data-cancel="' + id + '">Cancel</button></span>';
  box.querySelector("textarea").value =
    note.classList.contains("empty") ? "" : note.textContent;
  tile.querySelector(".shot-meta").appendChild(box);
  box.querySelector("textarea").focus();
}

async function saveShotNote(id) {
  const tile = document.querySelector('[data-shot="' + id + '"]');
  const next = tile.querySelector(".shot-edit textarea").value;
  shotErr(tile, "");
  try {
    const res = await fetch("/fhir/DocumentReference/" + id, {
      method: "PUT",
      headers: { "Authorization": "Bearer " + $("#token").value.trim(),
                 "Content-Type": "application/fhir+json" },
      body: JSON.stringify({
        resourceType: "DocumentReference", status: "current", description: next,
      }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      return shotErr(tile, detailText(data) || ("Could not save (" + res.status + ")"));
    }
  } catch (e) {
    return shotErr(tile, "Network error: " + e.message);
  }
  loadGallery(tile.closest(".shots").dataset.gallery);
}

/* Withdrawing is the one destructive control here, so the button asks once
   before it does it - the same button, relabelled, rather than a modal. */
async function withdrawShot(id, confirmed) {
  const tile = document.querySelector('[data-shot="' + id + '"]');
  if (!confirmed) {
    const btn = tile.querySelector("[data-del]");
    btn.textContent = "Withdraw - tap to confirm";
    btn.dataset.delok = id;
    delete btn.dataset.del;
    return;
  }
  const docId = tile.closest(".shots").dataset.gallery;
  const slug = tile.closest(".io-partner").dataset.slug;
  shotErr(tile, "");
  try {
    const res = await fetch("/fhir/DocumentReference/" + id, {
      method: "DELETE",
      headers: { "Authorization": "Bearer " + $("#token").value.trim() },
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      return shotErr(tile, detailText(data) || ("Could not withdraw (" + res.status + ")"));
    }
  } catch (e) {
    return shotErr(tile, "Network error: " + e.message);
  }
  await refreshShotCount(slug);
  const gallery = document.querySelector('[data-gallery="' + docId + '"]');
  if (gallery) { gallery.hidden = false; loadGallery(docId); }
}

/* One listener on the container: the partner cards are rebuilt from scratch
   on every refresh, so per-button listeners would not survive them. */
$("#io-partners").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  if (btn.dataset.shots) {
    const box = document.querySelector('[data-gallery="' + btn.dataset.shots + '"]');
    box.hidden = !box.hidden;
    if (!box.hidden) loadGallery(btn.dataset.shots);
  } else if (btn.dataset.add) {
    const form = document.querySelector('[data-form="' + btn.dataset.add + '"]');
    form.hidden = !form.hidden;
    if (!form.hidden) form.querySelector("input[type=file]").focus();
  } else if (btn.dataset.upload) {
    uploadShot(btn.dataset.upload);
  } else if (btn.dataset.edit) {
    editShotNote(btn.dataset.edit);
  } else if (btn.dataset.save) {
    saveShotNote(btn.dataset.save);
  } else if (btn.dataset.cancel) {
    loadGallery(document.querySelector('[data-shot="' + btn.dataset.cancel + '"]')
      .closest(".shots").dataset.gallery);
  } else if (btn.dataset.del) {
    withdrawShot(btn.dataset.del, false);
  } else if (btn.dataset.delok) {
    withdrawShot(btn.dataset.delok, true);
  }
});

/* ---- advertised endpoints ---- */
const EP_KINDS = { "hl7-fhir-rest": "FHIR server", "web-page": "How-to page" };
/* [address, how-to] placeholders per kind, so the example under a web page
   looks like a web page rather than a FHIR base. Both forms share them. */
const EP_PLACEHOLDERS = {
  "hl7-fhir-rest": ["https://fhir.example.org/r4",
    "e.g. POST a document Bundle to /Bundle. Ask at our table for a token."],
  "web-page": ["https://developer.yourcompany.com/docs/import-patient-summary",
    "e.g. Create a sandbox account on the page, then upload the bundle " +
    "under Import > Patient summary."],
};
function epPlaceholders(kind, address, desc) {
  const [a, d] = EP_PLACEHOLDERS[kind.value] || EP_PLACEHOLDERS["hl7-fhir-rest"];
  address.placeholder = a;
  desc.placeholder = d;
}
for (const [k, a, d] of [["#ep-kind", "#ep-address", "#ep-desc"],
                         ["#reg-ep-kind", "#reg-ep-address", "#reg-ep-desc"]]) {
  $(k).addEventListener("change", () => epPlaceholders($(k), $(a), $(d)));
}
const epFormPlaceholders = () =>
  epPlaceholders($("#ep-kind"), $("#ep-address"), $("#ep-desc"));
/* An Endpoint, flattened to what a row draws. The description is R5's,
   carried as an extension; managingOrganization is the company advertising. */
function epFromResource(r) {
  const org = r.managingOrganization || {};
  const ext = (r.extension || []).find((x) => x.url === ENDPOINT_DESCRIPTION_EXT);
  return { id: r.id, kind: (r.connectionType || {}).code, name: r.name || "",
           address: r.address || "", description: (ext && ext.valueString) || "",
           team: org.display, team_slug: String(org.reference || "").split("/").pop() };
}
let myEndpoints = {};
let epEditing = null;

/* editable: the company card's own list, with Edit and Remove. In the
   Interop list a company's own rows point back there instead. */
function epRow(e, editable) {
  const mine = me && me.id === e.team_slug;
  /* The server takes nothing but http(s), and this checks again rather than
     trusting that: whatever is in href is what a click runs. */
  const link = /^https?:\/\//i.test(e.address)
    ? '<a class="ep-addr" href="' + esc(e.address)
      + '" target="_blank" rel="noopener noreferrer nofollow">' + esc(e.address) + "</a>"
    : '<code class="ep-addr">' + esc(e.address) + "</code>";
  return '<div class="ep" data-ep="' + esc(e.id) + '">'
    + '<div class="ep-head"><span class="ep-kind">' + esc(EP_KINDS[e.kind] || e.kind)
    + "</span>" + (e.name ? "<span>" + esc(e.name) + "</span>" : "") + "</div>"
    + link
    + (e.description ? '<p class="ep-desc">' + esc(e.description) + "</p>" : "")
    + (editable ? '<div class="io-actions">'
        + '<button class="ghost" data-ep-edit="' + esc(e.id) + '">Edit</button>'
        + '<button class="ghost" data-ep-del="' + esc(e.id) + '">Remove</button></div>'
      : mine ? '<a class="muted" href="#submit">Change it on the Submit tab</a>' : "")
    + "</div>";
}

let epAsked = 0;
async function loadEndpoints() {
  const hint = $("#ep-hint");
  const box = $("#ep-list");
  /* Two quick removes reload twice; the older answer must not land last and
     bring back what the second one removed. */
  const ask = ++epAsked;
  try {
    const res = await fetch("/fhir/Endpoint");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const eps = ((await res.json()).entry || []).map((x) => epFromResource(x.resource));
    if (ask !== epAsked) return;
    myEndpoints = {};
    /* The server sorts by company, so a company's rows are already together. */
    const groups = [];
    for (const e of eps) {
      if (me && me.id === e.team_slug) myEndpoints[e.id] = e;
      const last = groups[groups.length - 1];
      if (last && last.slug === e.team_slug) last.rows.push(e);
      else groups.push({ slug: e.team_slug, team: e.team, rows: [e] });
    }
    hint.textContent = groups.length ? ""
      : "No company has advertised an endpoint yet.";
    box.innerHTML = groups.map((g) =>
      '<div class="io-partner"><strong>' + esc(g.team) + "</strong>"
      + g.rows.map((e) => epRow(e, false)).join("") + "</div>").join("");
    $("#my-eps").innerHTML = Object.values(myEndpoints)
      .map((e) => epRow(e, true)).join("");
  } catch (e) {
    if (ask !== epAsked) return;
    /* Said in both places the list is drawn: an empty company card reads as
       "nothing advertised yet", and invites adding the same address twice. */
    hint.textContent = "Could not load the endpoints: " + e.message;
    box.innerHTML = "";
    myEndpoints = {};
    $("#my-eps").innerHTML = '<p class="shot-status err">Could not load your '
      + "addresses (" + esc(e.message) + ") - reload to try again.</p>";
  }
}

function epStatus(text, isError) {
  const p = $("#ep-status");
  p.textContent = text;
  p.classList.toggle("err", !!isError);
  p.hidden = !text;
}

function epResetForm() {
  epEditing = null;
  $("#ep-kind").value = "hl7-fhir-rest";
  epFormPlaceholders();
  $("#ep-address").value = $("#ep-name").value = $("#ep-desc").value = "";
  $("#ep-form-title").textContent = "Add an address";
  $("#ep-save").textContent = "Add";
  $("#ep-cancel").hidden = true;
}

function epEdit(id) {
  const e = myEndpoints[id];
  if (!e) return;
  epEditing = id;
  $("#ep-kind").value = e.kind;
  epFormPlaceholders();
  $("#ep-address").value = e.address;
  $("#ep-name").value = e.name;
  $("#ep-desc").value = e.description;
  $("#ep-form-title").textContent = "Edit " + (e.name || e.address);
  $("#ep-save").textContent = "Save changes";
  $("#ep-cancel").hidden = false;
  epStatus("");
  $("#ep-form").scrollIntoView({ block: "nearest", behavior: "smooth" });
  $("#ep-address").focus();
}

async function epSave() {
  const token = $("#token").value.trim();
  const address = $("#ep-address").value.trim();
  if (!token) return epStatus("Paste your company token first.", true);
  if (!/^https?:\/\/\S+$/i.test(address))
    return epStatus("The address has to be an http:// or https:// URL.", true);
  const desc = $("#ep-desc").value.trim();
  const body = { resourceType: "Endpoint", status: "active",
                 connectionType: { code: $("#ep-kind").value },
                 address: address };
  const name = $("#ep-name").value.trim();
  if (name) body.name = name;
  if (desc) body.extension = [{ url: ENDPOINT_DESCRIPTION_EXT, valueString: desc }];
  const btn = $("#ep-save");
  btn.disabled = true;
  epStatus(epEditing ? "Saving..." : "Publishing...");
  try {
    const res = await fetch("/fhir/Endpoint" + (epEditing ? "/" + epEditing : ""), {
      method: epEditing ? "PUT" : "POST",
      headers: { "Authorization": "Bearer " + token,
                 "Content-Type": "application/fhir+json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      /* The token stopped working while the page was open - a practice reset
         overnight. Asking again flips the card to registration, which says so. */
      epStatus("");
      await refreshCompany();
      return;
    }
    if (res.status === 404 && epEditing) {
      /* Removed meanwhile, by a colleague or a practice reset. Staying in edit
         mode would fail every Save the same way. */
      epResetForm();
      epStatus("That address no longer exists - add it again if you still need it.", true);
      await loadEndpoints();
      return;
    }
    if (!res.ok) {
      epStatus(detailText(data) || ("Could not save (" + res.status + ")"), true);
      return;
    }
    epStatus(epEditing ? "Saved." : "Added - it is listed on the Interop tab.");
    epResetForm();
    await loadEndpoints();
  } catch (e) {
    epStatus("Network error: " + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* Removing asks once, by relabelling its own button, as withdrawing a
   screenshot does. */
async function epRemove(btn, confirmed) {
  const id = confirmed ? btn.dataset.epDelok : btn.dataset.epDel;
  if (!confirmed) {
    btn.textContent = "Remove - tap to confirm";
    btn.dataset.epDelok = id;
    delete btn.dataset.epDel;
    return;
  }
  btn.disabled = true;
  try {
    const res = await fetch("/fhir/Endpoint/" + id, {
      method: "DELETE",
      headers: { "Authorization": "Bearer " + $("#token").value.trim() },
    });
    if (res.status === 401) { await refreshCompany(); return; }
    /* 404 is the outcome asked for, reached some other way. */
    if (!res.ok && res.status !== 404) {
      const data = await res.json().catch(() => ({}));
      btn.disabled = false;
      return epStatus(detailText(data) || ("Could not remove (" + res.status + ")"), true);
    }
  } catch (e) {
    btn.disabled = false;
    return epStatus("Network error: " + e.message, true);
  }
  if (epEditing === id) epResetForm();
  epStatus("");
  await loadEndpoints();
}

$("#my-eps").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  if (btn.dataset.epEdit) epEdit(btn.dataset.epEdit);
  else if (btn.dataset.epDel) epRemove(btn, false);
  else if (btn.dataset.epDelok) epRemove(btn, true);
});
$("#ep-save").addEventListener("click", epSave);

/* The register card turns into the company's own card once the page knows
   whose token it holds: registering again is the rare case, changing where
   you take data in is the common one. */
function showCompanyCard(registering) {
  const known = !!me && !registering;
  $("#company-view").hidden = !known;
  $("#reg-view").hidden = known;
  /* Registering overwrites the saved token. Warn whenever that token may
     still be good - including when the server could not be asked about it -
     and offer the way back only when there is a company to go back to. */
  const saved = $("#token").value.trim();
  $("#reg-back-wrap").hidden = !saved || meState === "rejected" || (!!me && !registering);
  $("#reg-back").parentNode.hidden = !me;
  $("#reg-replace-name").textContent = me ? "for " + me.name : "";
  $("#reg-replace-token").textContent = saved;
  if (me) $("#company-name").textContent = me.name;
  /* The verdict on the token sits under the token field itself, which is
     often filled from localStorage rather than typed this session. */
  const note = $("#token-note");
  note.textContent = meState === "rejected"
    ? "This token is not one this server knows - check it, or if it is from "
      + "before a practice reset, register again."
    : meState === "unreachable" && !me
    ? "Could not check this token with the server - reload to try again."
    : "";
  note.hidden = !note.textContent;
}
async function refreshCompany() {
  const before = me && me.id;
  await whoAmI();
  /* An address open for editing belongs to the company it was opened under. */
  if ((me && me.id) !== before) {
    epResetForm();
    epStatus("");
    /* Not the previous company's rows, and not "Nothing yet" either, until
       this one's have arrived. */
    myEndpoints = {};
    $("#my-eps").innerHTML = '<p class="muted">Loading...</p>';
  }
  showCompanyCard(false);
  await loadEndpoints();
}
$("#reg-another").addEventListener("click", () => {
  showCompanyCard(true);
  $("#reg-name").focus();
});
$("#reg-back").addEventListener("click", () => showCompanyCard(false));
/* Typing or pasting a token settles on a company once the typing stops. */
let tokenTimer = null;
$("#token").addEventListener("input", () => {
  clearTimeout(tokenTimer);
  tokenTimer = setTimeout(refreshCompany, 400);
});
/* Interop's first visit waits on this rather than asking the same two
   questions again. */
const companyLoaded = refreshCompany();
let interopEntered = false;
$("#ep-cancel").addEventListener("click", () => { epResetForm(); epStatus(""); });

async function initInterop() {
  interopCurls();
  const hint = $("#io-hint");
  const box = $("#io-partners");
  /* The first visit shares the page-load check; later ones ask again, or the
     list of where to send data is whatever it was when the page opened. */
  if (interopEntered) { clearTimeout(tokenTimer); await refreshCompany(); }
  else { interopEntered = true; await companyLoaded; }
  try {
    const res = await fetch("/fhir/$exchange");
    if (!res.ok) return;
    const partners = paramEntries(await res.json(), "entry").map(partnerRow);
    hint.textContent = partners.length
      ? "Every published exchange document, free to download and test against."
      : "No company has a valid submission yet - check back soon.";
    box.innerHTML = partners.map(partnerCard).join("");
  } catch (e) {
    hint.textContent = "Network error: " + e.message;
  }
}

/* ---- theme picker ---- */
/* localStorage throws outright where site data is blocked, so reads and writes
   are both guarded; the picker still works, the choice just does not persist. */
function storedTheme() {
  try { return localStorage.getItem("theme"); } catch (e) { return null; }
}
function storeTheme(mode) {
  try {
    if (mode === "system") localStorage.removeItem("theme");
    else localStorage.setItem("theme", mode);
  } catch (e) {}
}
const THEME_MODES = ["system", "light", "dark"];
const THEME_LABEL = { system: "System theme", light: "Light theme",
                      dark: "Dark theme" };
function initTheme() {
  const btn = document.getElementById("theme-btn");
  const icons = [...btn.querySelectorAll("svg")];
  /* The button is its own label: the icon says what is in force now, and the
     accessible name has to say so too, plus what a click will do - a bare
     "System theme" would read as a command rather than a state. */
  const show = (mode) => {
    /* toggleAttribute, not .hidden: `hidden` is an IDL attribute of
       HTMLElement and SVGElement does not inherit it, so `svg.hidden = true`
       sets a JS expando and never the content attribute the CSS selects on.
       The icon stayed on whatever the markup shipped, forever. */
    icons.forEach((svg) => { svg.toggleAttribute("hidden", svg.dataset.icon !== mode); });
    const next = THEME_MODES[(THEME_MODES.indexOf(mode) + 1) % THEME_MODES.length];
    btn.title = THEME_LABEL[mode] + " - click for " + THEME_LABEL[next].toLowerCase();
    btn.setAttribute("aria-label",
      THEME_LABEL[mode] + ", click for " + THEME_LABEL[next].toLowerCase());
  };
  const stored = storedTheme();
  let mode = stored === "light" || stored === "dark" ? stored : "system";
  show(mode);
  btn.addEventListener("click", () => {
    mode = THEME_MODES[(THEME_MODES.indexOf(mode) + 1) % THEME_MODES.length];
    if (mode === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = mode;
    storeTheme(mode);
    show(mode);
  });
}

/* initial state: the hash decides what is shown, including on a cold load */
initTheme();
applyRoute(false);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- OpenAPI

# Two endpoints read their body straight off the request (a raw FHIR bundle,
# or a small dict validated by hand) rather than through a Pydantic model, so
# FastAPI has nothing to introspect and generates an empty requestBody for
# them. The shapes are described here instead. Handwriting the whole document
# would be a second source of truth that drifts; this patches only the parts
# the framework cannot see.
ENDPOINT_EXAMPLE = {
    "resourceType": "Endpoint",
    "status": "active",
    "connectionType": {"system": ENDPOINT_CONNECTION_SYSTEM,
                       "code": "hl7-fhir-rest"},
    "name": "Staging FHIR server",
    "address": "https://fhir.example.org/r4",
    "extension": [{
        "url": ENDPOINT_DESCRIPTION_EXT,
        "valueString": "POST a document Bundle to /Bundle. Ask at our table "
                       "for a token.",
    }],
}
ENDPOINT_BODY = {
    "required": True,
    "content": {
        FHIR_JSON: {
            "schema": {"type": "object", "description": "A FHIR Endpoint."},
            "example": ENDPOINT_EXAMPLE,
        },
        "application/json": {"schema": {"type": "object"}},
    },
}

OPENAPI_BODIES = {
    ("/fhir/Endpoint", "post"): ENDPOINT_BODY,
    ("/fhir/Endpoint/{endpoint_id}", "put"): ENDPOINT_BODY,
    ("/fhir/$register", "post"): {
        "required": True,
        "content": {
            FHIR_JSON: {
                "schema": {
                    "type": "object",
                    "description": "A FHIR Parameters resource.",
                },
                "example": {
                    "resourceType": "Parameters",
                    "parameter": [
                        {"name": "name", "valueString": "FHIR Starters"}
                    ],
                },
            },
            "application/json": {"schema": {"type": "object"}},
        },
    },
    ("/fhir/Bundle/$validate", "post"): {
        "required": True,
        "content": {
            FHIR_JSON: {
                "schema": {
                    "type": "object",
                    "description": "A FHIR R4 Bundle of type 'document'.",
                },
                "example": {"resourceType": "Bundle", "type": "document", "entry": []},
            },
            "application/json": {"schema": {"type": "object"}},
        },
    },
    ("/fhir/DocumentReference", "post"): {
        "required": True,
        "content": {
            FHIR_JSON: {
                "schema": {
                    "type": "object",
                    "description": (
                        "A DocumentReference whose content[0].attachment.data "
                        "is the PNG, JPEG or WebP bytes, base64-encoded."
                    ),
                },
                "example": {
                    "resourceType": "DocumentReference",
                    "status": "current",
                    "description": "Allergies section came through empty.",
                    "custodian": {"reference": "Organization/partner-slug"},
                    "content": [
                        {
                            "attachment": {
                                "contentType": "image/png",
                                "data": "iVBORw0KGgo...",
                            }
                        }
                    ],
                },
            },
            "application/json": {"schema": {"type": "object"}},
        },
    },
    ("/fhir/DocumentReference/{rendering_id}", "put"): {
        "required": True,
        "content": {
            FHIR_JSON: {
                "schema": {
                    "type": "object",
                    "required": ["resourceType", "description"],
                },
                "example": {
                    "resourceType": "DocumentReference",
                    "status": "current",
                    "description": "Allergies section came through empty.",
                },
            },
            "application/json": {"schema": {"type": "object"}},
        },
    },
}

# Score and verdict ride on headers so a client can act on them without parsing
# the OperationOutcome. FastAPI only documents headers it was told about.
SUBMIT_RESPONSE_HEADERS = {
    "X-Pass": ("true when the bundle had no error or fatal issue", "string"),
    "X-Score": ("the company's leaderboard score after this submission, "
                "bonuses included", "integer"),
    "X-Submission-Score": ("what this bundle alone scored, before bonuses",
                           "integer"),
    "X-Errors": ("count of error and fatal issues", "integer"),
    "X-Sections": ("distinct populated Composition sections", "integer"),
    "X-Resources": ("clinical resources counted for richness", "integer"),
    "X-Document-Type": (
        "the document category the bundle was checked as, and so the profile "
        "the findings came from - one of the ids from $documents", "string"),
    "X-Document-Profile": ("canonical of the profile it was validated against",
                           "string"),
    "X-Secondary-Categories": (
        "non-patient-summary EHDS categories this company has now passed",
        "integer"),
    "X-Best-Patient-Summary": (
        "the company's best passing patient summary, 0 if it has none yet - "
        "the rest of X-Score is bonuses", "integer"),
    "X-First-Pass": (
        "true when this company holds the one-time first-valid-summary bonus",
        "string"),
    "Content-Location": ("where this bundle is now readable", "string"),
}

# Endpoints that read the token. Anything else is open.
TOKEN_REQUIRED = {
    ("/fhir/Endpoint", "post"),
    ("/fhir/Endpoint/{endpoint_id}", "put"),
    ("/fhir/Endpoint/{endpoint_id}", "delete"),
    ("/fhir/Organization/$me", "get"),
    ("/fhir/Bundle/$validate", "post"),
    ("/fhir/DocumentReference", "post"),
    ("/fhir/DocumentReference/{rendering_id}", "put"),
    ("/fhir/DocumentReference/{rendering_id}", "delete"),
}


def custom_openapi() -> dict:
    """FastAPI's generated document, corrected where introspection falls short."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=APP_VERSION,
        summary="Validate and score European Patient Summary bundles.",
        description=(
            "A FHIR R4 server rooted at `/fhir`. Register a company, submit "
            "bundles to `Bundle/$validate` for validation against `%s`, and "
            "fetch other companies' documents to test against.\n\n"
            "The patient summary is what the event scores. A bundle that is "
            "one of the other EHDS Article 14 categories is recognised from "
            "its content and validated against that category's own HL7 Europe "
            "guide instead, for a flat bonus rather than the summary's "
            "richness points - `$documents` lists them.\n\n"
            "Point a FHIR client at `/fhir` and `GET /fhir/metadata` describes "
            "all of this in FHIR's own terms; this document is the same API "
            "for readers who would rather have OpenAPI.\n\n"
            "Writing endpoints take the token from `POST /fhir/$register`, sent "
            "as `Authorization: Bearer TOKEN` or `X-Team-Token: TOKEN`."
        ) % PROFILE,
        routes=app.routes,
    )
    schema["components"] = schema.get("components", {})
    schema["components"]["securitySchemes"] = {
        "TeamToken": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Team-Token",
            "description": "The token returned by POST /fhir/$register.",
        }
    }
    for path, operations in schema["paths"].items():
        for verb, op in operations.items():
            key = (path, verb)
            # The token headers are declared as ordinary parameters because
            # they arrive through Header() dependencies; the security scheme
            # says the same thing better, so drop the duplicates.
            op["parameters"] = [
                p for p in op.get("parameters", [])
                if p.get("name", "").lower() not in ("x-team-token", "authorization")
            ] or None
            if op["parameters"] is None:
                del op["parameters"]
            if key in TOKEN_REQUIRED:
                op["security"] = [{"TeamToken": []}]
            if key in OPENAPI_BODIES:
                op["requestBody"] = OPENAPI_BODIES[key]
    submit = schema["paths"]["/fhir/Bundle/$validate"]["post"]["responses"]["200"]
    submit["description"] = "The Matchbox OperationOutcome for this bundle."
    submit["content"] = {"application/fhir+json": {"schema": {"type": "object"}}}
    submit["headers"] = {
        name: {"description": desc, "schema": {"type": kind}}
        for name, (desc, kind) in SUBMIT_RESPONSE_HEADERS.items()
    }
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)

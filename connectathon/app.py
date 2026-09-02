"""Nordic Hackathon 2026 - gamified FHIR European Patient Summary validation service.

Teams register, submit EPS bundles, Matchbox validates, leaderboard keeps score.
Stdlib sqlite3 + FastAPI, no other runtime deps.
"""

import http.client
import json
import os
import re
import secrets
import sqlite3
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, Response

MATCHBOX_URL = os.environ.get(
    "MATCHBOX_URL", "http://matchbox:8080/matchboxv3/fhir"
).rstrip("/")
PROFILE = os.environ.get(
    "PROFILE", "http://hl7.eu/fhir/eps/StructureDefinition/bundle-eu-eps"
)
# Extra packages to load into the validation engine alongside PROFILE's own.
# Matchbox scopes an engine to the IG that defines the requested profile, so a
# meta.profile claim from anywhere else comes back "not checked" unless its
# package is named here. The Swedish base profiles are in by default because
# this is a Swedish event and teams will claim them on Patient.
VALIDATION_IGS = [
    ig.strip()
    for ig in os.environ.get("VALIDATION_IGS", "hl7se.fhir.base#1.1.0").split(",")
    if ig.strip()
]

DATA_DIR = os.environ.get("DATA_DIR", "/data")
SAMPLES_DIR = os.environ.get(
    "SAMPLES_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
)
PORT = int(os.environ.get("PORT", "8095"))

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

VALID_BASE = 50        # points for a submission with zero error/fatal issues
SECTION_POINTS = 10    # per populated top-level Composition section
RESOURCE_POINTS = 2    # per clinical resource (not Composition/Patient)
RESOURCE_CAP = 60      # max points from clinical resources
SECTION_CAP = 160      # max points from sections (the IPS set is ~16 sections)
FIRST_PASS_BONUS = 20  # one-time, to the first company that submits a valid bundle

# Set REGISTER_CODE to require a shared secret on /api/register. Left empty the
# endpoint stays open, which is what you want on a closed event network.
REGISTER_CODE = os.environ.get("REGISTER_CODE", "")


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
                filename  TEXT NOT NULL
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


def submission_score(passed: bool, sections: int, resources: int) -> int:
    """Conformance gates the score: any error/fatal issue means 0.

    A valid submission earns VALID_BASE, plus SECTION_POINTS per distinct
    populated Composition section capped at SECTION_CAP points, plus
    RESOURCE_POINTS per clinical resource capped at RESOURCE_CAP points.
    Both halves are capped so no amount of padding outruns a real document.
    """
    if not passed:
        return 0
    return (
        VALID_BASE
        + min(SECTION_CAP, SECTION_POINTS * sections)
        + min(RESOURCE_CAP, RESOURCE_POINTS * resources)
    )


# ---------------------------------------------------------------- clinician summary

MEDICATION_TYPES = ("MedicationStatement", "MedicationRequest", "MedicationDispense")

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
    "AllergyIntolerance": "allergy",
    "Condition": "problem",
    "MedicationStatement": "medication",
    "MedicationRequest": "medication",
    "MedicationDispense": "medication",
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
    "Condition": ("onsetDateTime", "onsetPeriod", "recordedDate"),
    "AllergyIntolerance": ("onsetDateTime", "recordedDate"),
    "Procedure": ("performedDateTime", "performedPeriod"),
    "Immunization": ("occurrenceDateTime", "recorded"),
    "Observation": ("effectiveDateTime", "effectivePeriod", "issued"),
    "MedicationStatement": ("effectiveDateTime", "effectivePeriod", "dateAsserted"),
    "MedicationRequest": ("authoredOn",),
    "MedicationDispense": ("whenHandedOver", "whenPrepared"),
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
        if not _dosage_detail(resource):
            gaps.append("no dose")
        if status == "active":
            flags.append("active")
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
    populated = sum(1 for sec in summary["sections"] if sec["items"])
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
    if rtype in MEDICATION_TYPES:
        label = _medication_name(resource, by_ref)
        detail = _dosage_detail(resource)
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
            "note": _note_text(resource)}
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
                empty_reason = None
                if not items:
                    empty_reason = _cc_display(section.get("emptyReason"))
                    if empty_reason:
                        empty_reason = EMPTY_REASON_LABELS.get(
                            _squash(empty_reason), empty_reason
                        )
                summary["sections"].append(
                    {
                        "title": title,
                        "kind": _section_kind(section, title),
                        "items": items,
                        "empty_reason": empty_reason,
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


# ---------------------------------------------------------------- matchbox


def validate_with_matchbox(body: bytes) -> dict:
    """POST the bundle to Matchbox $validate; return the OperationOutcome dict.

    Raises MatchboxError when the validator is unreachable or unusable.
    Monkeypatched in tests.
    """
    query = [("profile", PROFILE)] + [("igs", ig) for ig in VALIDATION_IGS]
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
    yield


APP_VERSION = "1.0.0"
app = FastAPI(
    title="Nordic Hackathon 2026 EPS service",
    version=APP_VERSION,
    lifespan=lifespan,
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


async def small_json_body(request: Request) -> dict:
    """Size-capped JSON object body for the non-bundle endpoints."""
    raw = await read_limited_body(request, MAX_JSON_BODY_BYTES, "Request body")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, detail="Body is not valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="Body must be a JSON object")
    return payload


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


@app.post("/api/register",
    tags=["Companies"],
    summary="Register a company and get its token",
    description=(
        "One registration per company. The token comes back once and is not "
        "retrievable afterwards, so share it inside the company rather than "
        "registering again. Set REGISTER_CODE on the server to require a "
        "`code` field in the body as well."
    ),
)
def register(payload: dict = Depends(small_json_body)) -> JSONResponse:
    if REGISTER_CODE:
        supplied = payload.get("code")
        if not isinstance(supplied, str) or not secrets.compare_digest(
            supplied.encode("utf-8"), REGISTER_CODE.encode("utf-8")
        ):
            raise HTTPException(403, detail="Registration code required")
    team = payload.get("team")
    if not isinstance(team, str):
        raise HTTPException(400, detail="Body must be {\"team\": \"<name>\"}")
    # Collapse runs of whitespace so "Acme  Health" is the same company as
    # "Acme Health" rather than a way around the one-registration rule.
    team = " ".join(team.split())
    if not 2 <= len(team) <= 40:
        raise HTTPException(400, detail="Team name must be 2-40 characters")
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
            raise HTTPException(409, detail="Team name already taken")
    return JSONResponse(
        {
            "team": team,
            "token": token,
            "note": "One registration per company. Register once and share "
            "this token with your colleagues.",
        }
    )


@app.post("/api/eps",
    tags=["Submissions"],
    summary="Validate and score a bundle",
    description=(
        "Sends the bundle to Matchbox for validation against the EPS profile, "
        "stores the result and returns the FHIR OperationOutcome. The score is "
        "in the response headers, not the body. 413 if the bundle is larger "
        "than MAX_BODY_BYTES, 502 if Matchbox cannot be reached."
    ),
)
def submit_eps(
    body: bytes = Depends(bundle_body),
    team: sqlite3.Row = Depends(current_team),
) -> Response:
    try:
        bundle = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        # RecursionError: json rejects deeply nested input by blowing the stack,
        # which is still the client's bad payload, not a server fault.
        raise HTTPException(400, detail="Body is not valid JSON")
    if not isinstance(bundle, dict):
        raise HTTPException(400, detail="Body must be a FHIR Bundle JSON object")

    try:
        outcome = validate_with_matchbox(body)
    except MatchboxError as exc:
        raise HTTPException(502, detail=str(exc))

    counts = {"fatal": 0, "error": 0, "warning": 0, "information": 0}
    issues = outcome.get("issue")
    for issue in issues if isinstance(issues, list) else []:
        sev = issue.get("severity") if isinstance(issue, dict) else None
        if sev in counts:
            counts[sev] += 1
    errors = counts["fatal"] + counts["error"]
    passed = errors == 0
    sections, resources = count_bundle_richness(bundle)
    score = submission_score(passed, sections, resources)

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

    awarded_first_pass = False
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if passed:
            cur = conn.execute(
                "UPDATE teams SET first_pass = 1 WHERE id = ? "
                "AND NOT EXISTS (SELECT 1 FROM teams WHERE first_pass = 1)",
                (team["id"],),
            )
            awarded_first_pass = cur.rowcount == 1
        conn.execute(
            "INSERT INTO submissions (team_id, ts, pass, errors, warnings, infos, "
            "score, sections, resources, filename) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                filename,
            ),
        )

    effective_score = score + (FIRST_PASS_BONUS if awarded_first_pass else 0)
    return Response(
        content=json.dumps(outcome),
        media_type="application/fhir+json",
        headers={
            "X-Pass": "true" if passed else "false",
            "X-Score": str(effective_score),
            "X-Errors": str(errors),
            "X-Sections": str(sections),
            "X-Resources": str(resources),
        },
    )


@app.get("/api/leaderboard",
    tags=["Submissions"],
    summary="Current standings",
    description=(
        "One row per registered company, best score first. Companies that have "
        "registered but never submitted appear at the bottom with no score."
    ),
)
def leaderboard() -> list[dict]:
    with connect() as conn:
        teams = conn.execute(
            "SELECT id, name, slug, first_pass FROM teams"
        ).fetchall()
        subs = conn.execute(
            "SELECT team_id, ts, pass, errors, score, sections, resources "
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
            # Highest-scoring submission drives the team score and the
            # richness columns; its timestamp is the moment the team first
            # achieved its current best score (used as ranking tiebreak, so
            # re-submitting never hurts).
            best = min(team_subs, key=lambda s: (-s["score"], s["ts"]))
            score = best["score"] + (FIRST_PASS_BONUS if first_pass else 0)
            rows.append(
                {
                    "team": team["name"],
                    "team_slug": team["slug"],
                    "score": score,
                    "passed": any(s["pass"] for s in team_subs),
                    "attempts": len(team_subs),
                    "best_errors": min(s["errors"] for s in team_subs),
                    "best_sections": best["sections"],
                    "best_resources": best["resources"],
                    "last_submission": team_subs[-1]["ts"],
                    "first_pass": first_pass,
                    "_best_ts": best["ts"],
                }
            )
        else:
            rows.append(
                {
                    "team": team["name"],
                    "team_slug": team["slug"],
                    "score": 0,
                    "passed": False,
                    "attempts": 0,
                    "best_errors": None,
                    "best_sections": None,
                    "best_resources": None,
                    "last_submission": None,
                    "first_pass": first_pass,
                    "_best_ts": "9999",  # sorts after any real ISO timestamp
                }
            )

    # Score desc, then whoever reached their best score first, then name.
    # Attempts and last submission are deliberately not tiebreaks: trying
    # again must never hurt a team's rank.
    rows.sort(
        key=lambda r: (-r["score"], r["_best_ts"], r["team"].casefold())
    )
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
        del row["_best_ts"]
    return rows


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


@app.get("/api/submissions",
    tags=["Submissions"],
    summary="Submission history",
    description=(
        "Every company's attempts, newest first, or one company's with `team`."
    ),
)
def list_submissions(team: str | None = None, limit: int = 100) -> list[dict]:
    limit = max(1, min(limit, 1000))
    query = (
        "SELECT s.id, t.name AS team, t.slug AS team_slug, s.ts, s.pass, "
        "s.errors, s.warnings, s.score, s.sections, s.resources "
        "FROM submissions s JOIN teams t ON t.id = s.team_id "
    )
    params: tuple = ()
    if team:
        query += "WHERE t.slug = ? "
        params = (team,)
    query += "ORDER BY s.ts DESC, s.id DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(query, params + (limit,)).fetchall()
    return [
        {
            "id": r["id"],
            "team": r["team"],
            "team_slug": r["team_slug"],
            "ts": r["ts"],
            "pass": bool(r["pass"]),
            "score": r["score"],
            "errors": r["errors"],
            "warnings": r["warnings"],
            "sections": r["sections"],
            "resources": r["resources"],
        }
        for r in rows
    ]


@app.get("/api/submissions/{sub_id}",
    tags=["Submissions"],
    summary="One submission in full",
    description=(
        "The validation verdict plus a readable summary of the bundle: patient, "
        "sections and the clinical items under each one."
    ),
)
def submission_detail(sub_id: int) -> dict:
    row = _submission_row(sub_id)
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


@app.get("/api/submissions/{sub_id}/bundle",
    tags=["Submissions"],
    summary="Download the submitted bytes",
    description="The exact bytes that were posted, unmodified.",
)
def submission_bundle(sub_id: int) -> Response:
    row = _submission_row(sub_id)
    if row is None:
        raise HTTPException(404, detail="Unknown submission")
    raw = read_submission_bytes(row["team_slug"], row["filename"])
    if raw is None:
        raise HTTPException(404, detail="Submission file missing")
    return bundle_response(raw, "%s-%d-eps.json" % (row["team_slug"], row["id"]))


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


@app.get("/api/samples",
    tags=["Samples"],
    summary="List the worked examples",
    description=(
        "Synthetic documents for the event scenarios. No real patient data."
    ),
)
def list_samples() -> dict:
    return {
        "disclaimer": SAMPLE_DISCLAIMER,
        "samples": [dict(s, available=read_sample_bytes(s["id"]) is not None)
                    for s in SAMPLES],
    }


@app.get("/api/samples/{sample_id}",
    tags=["Samples"],
    summary="One worked example",
    description=(
        "The same readable summary shape as a submission, plus the scenario it "
        "illustrates."
    ),
)
def sample_detail(sample_id: str) -> dict:
    meta = _sample_meta(sample_id)
    raw = read_sample_bytes(sample_id)
    if meta is None or raw is None:
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
        "sample": dict(meta, disclaimer=SAMPLE_DISCLAIMER),
        "summary": safe_summary(_loads_or_none(raw)),
    }


@app.get("/api/samples/{sample_id}/bundle",
    tags=["Samples"],
    summary="Download a worked example",
    description="The example bundle as FHIR JSON, for use as test input.",
)
def sample_bundle(sample_id: str) -> Response:
    raw = read_sample_bytes(sample_id)
    if raw is None:
        raise HTTPException(404, detail="Unknown sample")
    return bundle_response(raw, "%s-example-eps.json" % sample_id)


# ---------------------------------------------------------------- interop API


@app.get("/api/exchange",
    tags=["Interop"],
    summary="Documents published for exchange",
    description=(
        "Each company's best valid submission is published automatically. The "
        "list is open - the documents are the event's shared test corpus."
    ),
)
def exchange_list() -> list[dict]:
    out = []
    with connect() as conn:
        teams = conn.execute(
            "SELECT id, name, slug FROM teams ORDER BY name COLLATE NOCASE"
        ).fetchall()
        for team in teams:
            best = best_valid_submission(conn, team["id"])
            if best is None:
                continue
            out.append(
                {
                    "team": team["name"],
                    "team_slug": team["slug"],
                    "exchange_id": best["id"],
                    "ts": best["ts"],
                    "sections": best["sections"],
                    "resources": best["resources"],
                }
            )
    return out


@app.get("/api/exchange/{team_slug}",
    tags=["Interop"],
    summary="Fetch a partner's document",
    description="The published bundle, ready to load into your own system.",
)
def exchange_fetch(team_slug: str) -> Response:
    with connect() as conn:
        producer = conn.execute(
            "SELECT id, name, slug FROM teams WHERE slug = ?", (team_slug,)
        ).fetchone()
        if producer is None:
            raise HTTPException(404, detail="Unknown team")
        best = best_valid_submission(conn, producer["id"])
    if best is None:
        raise HTTPException(404, detail="Team has no valid submission yet")
    raw = read_submission_bytes(producer["slug"], best["filename"])
    if raw is None:
        raise HTTPException(404, detail="Exchange document file missing")
    return bundle_response(
        raw,
        "%s-eps.json" % producer["slug"],
        **{"X-Exchange-Id": str(best["id"]), "X-Producer": producer["slug"]},
    )


@app.get("/api/health",
    tags=["Service"],
    summary="Service and validator health",
    description=(
        "Reports whether Matchbox is reachable (`up`), reachable but missing a "
        "package named in VALIDATION_IGS (`degraded` - profile claims from that "
        "package go unchecked), or unreachable (`down` - submissions fail). "
        "Also carries the bundle size limit and whether registration needs a "
        "code, for clients that want to warn before a doomed request."
    ),
)
def health() -> dict:
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
    return {
        "status": "ok",
        "matchbox": matchbox,
        "missing_igs": missing,
        # The UI reads these so it can warn before a doomed request.
        "max_body_bytes": MAX_BODY_BYTES,
        "register_code_required": bool(REGISTER_CODE),
    }


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
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Manrope:wght@300;400;500;600;700;800&display=swap">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<title>Nordic Hackathon 2026</title>
<style>
/* Nordic Hackathon 2026 - palette taken from the event site
   (nordichackathon2026.blogs.dsv.su.se): base #FFFFFF, contrast #111111,
   accent-3 #503AA8, accent-4 #686868, accent-5 #FBFAF3, accent-1 #FFEE58,
   set in Manrope with Fira Code for code. Semantic colours (pass/fail/warn)
   are deepened beyond the event palette so abnormal results stay obvious.
   Each token carries its light and dark value as a pair, so the two themes
   cannot drift apart; the theme picker only has to set color-scheme. */
:root { color-scheme: light dark; }
:root[data-theme="light"] { color-scheme: light; }
:root[data-theme="dark"] { color-scheme: dark; }
:root {
  --font-sans: "Manrope", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               Ubuntu, Cantarell, "Helvetica Neue", Arial, sans-serif;
  --font-mono: "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --r-card: 4px; --r-ctl: 3px;
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
  :root { --bg: #fbfaf3; --card: #ffffff; --text: #111111; --muted: #5f5f5f; --border: #d8d5c7; --field-border: #8d8878; --accent: #503aa8; --accent-soft: #efebfa; --accent-fill: #503aa8; --accent-on: #ffffff; --pass: #16704a; --pass-bg: #e2f1e8; --fail: #b3261e; --fail-bg: #fbe7e4; --warn: #7d5300; --warn-bg: #fbf0cf; --info: #12628f; --info-bg: #e2eff7; --gold: #6f5a00; --gold-bg: #fdf6c4; --silver: #5f5f5f; --silver-bg: #eeece2; --bronze: #8a4b1f; --bronze-bg: #f6e9dc; --code-bg: #f3f1e6; }
}
* { box-sizing: border-box; }
code, pre, textarea, .cv-mode, .tab-btn { font-variant-ligatures: none; }
a { color: var(--accent); text-underline-offset: 2px; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 400 15px/1.6 var(--font-sans);
  letter-spacing: -0.01em;
}
main { max-width: 1060px; margin: 0 auto; padding: 26px 16px 72px; }
header { max-width: 1060px; margin: 0 auto; padding: 48px 16px 6px; }
.head-row { display: flex; align-items: flex-start; justify-content: space-between;
  gap: 14px 24px; flex-wrap: wrap; }
.theme-toggle { display: flex; flex: none; margin-top: 5px;
  border: 1px solid var(--field-border); border-radius: var(--r-ctl);
  overflow: hidden; }
.th-btn { font: inherit; display: grid; place-items: center;
  background: transparent; color: var(--muted);
  border: none; border-left: 1px solid var(--field-border); border-radius: 0;
  padding: 6px 10px; cursor: pointer; }
/* Icons carry no text, so they inherit the button's colour in every state -
   hover and the pressed fill below both recolour the stroke for free. */
.th-btn svg { display: block; width: 15px; height: 15px; fill: none;
  stroke: currentColor; stroke-width: 1.6; stroke-linecap: round;
  stroke-linejoin: round; }
.th-btn:first-child { border-left: none; }
.th-btn:hover { color: var(--accent); filter: none; }
.th-btn[aria-pressed="true"] { background: var(--accent-fill); color: var(--accent-on); }
h1 { margin: 0; font-size: 1.95rem; font-weight: 700; letter-spacing: -0.035em;
  line-height: 1.15; }
.sub { margin: 8px 0 0; color: var(--muted); font-size: 1.02rem;
  letter-spacing: -0.015em; max-width: 62ch; }
h2 { margin: 0 0 14px; font-size: 1.14rem; font-weight: 700;
  letter-spacing: -0.025em; }
h3, h4, .patient-name, .db-big { letter-spacing: -0.02em; }
.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--r-card); padding: 22px 24px; margin-top: 20px;
}
.grid { display: grid; grid-template-columns: 1fr 1.4fr; gap: 20px; }
@media (max-width: 820px) { .grid { grid-template-columns: 1fr; } }
label { display: block; font-size: 0.83rem; color: var(--muted); margin: 12px 0 4px; }
input[type=text], textarea {
  width: 100%; padding: 9px 11px; border: 1px solid var(--field-border);
  border-radius: var(--r-ctl); background: var(--bg); color: var(--text);
  font: inherit;
}
textarea { font-family: var(--font-mono);
  font-size: 0.82rem; min-height: 170px; resize: vertical; }
input:focus, textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
button {
  font: inherit; font-weight: 600; border: none; border-radius: var(--r-ctl);
  padding: 9px 18px; cursor: pointer; background: var(--accent-fill);
  color: var(--accent-on); letter-spacing: -0.01em;
}
button:hover { filter: brightness(1.08); }
button:disabled { opacity: 0.55; cursor: wait; }
button.ghost {
  background: transparent; color: var(--accent);
  border: 1px solid var(--field-border); font-weight: 500; padding: 4px 10px;
  font-size: 0.8rem;
}
table { width: 100%; border-collapse: collapse; font-size: 0.92rem; }
th { text-align: left; color: var(--muted); font-size: 0.76rem;
  text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600;
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
.badge { display: inline-block; padding: 2px 9px; border-radius: 999px;
  font-size: 0.74rem; font-weight: 600; }
.badge.pass { color: var(--pass); background: var(--pass-bg); }
.badge.fail { color: var(--fail); background: var(--fail-bg); }
.chip { display: inline-block; padding: 1px 8px; border-radius: 999px;
  font-size: 0.72rem; font-weight: 600; text-transform: uppercase; }
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
  font-size: 1.15rem; font-weight: 700; }
.banner.pass { display: block; color: var(--pass); background: var(--pass-bg);
  border: 1px solid var(--pass); }
.banner.fail { display: block; color: var(--fail); background: var(--fail-bg);
  border: 1px solid var(--fail); }
.banner .score { float: right; }
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
details summary { cursor: pointer; font-weight: 700; font-size: 1.14rem;
  letter-spacing: -0.025em; }
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
.issues-scroll { max-height: 50vh; overflow-y: auto; border: 1px solid var(--border);
  border-radius: var(--r-ctl); }
table.issues { font-size: 0.82rem; }
table.issues thead th { position: sticky; top: 0; background: var(--card); z-index: 1; }
table.issues td { vertical-align: top; }
table.issues td.msg { overflow-wrap: anywhere; }
table.issues td.loc {
  font-family: var(--font-mono);
  font-size: 0.72rem; color: var(--muted); overflow-wrap: anywhere;
  max-width: 240px;
}
.count-badge { display: inline-block; font-size: 0.68rem; font-weight: 600;
  color: var(--muted); border: 1px solid var(--border); border-radius: 999px;
  padding: 0 6px; margin-left: 4px; white-space: nowrap; }
.profile-chip { display: inline-block; font-size: 0.66rem; color: var(--muted);
  background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 999px; padding: 0 6px; margin-left: 6px; white-space: nowrap;
  vertical-align: middle; }
details.issue-fold { margin-top: 10px; }
details.issue-fold summary { font-size: 0.86rem; font-weight: 600;
  color: var(--muted); }
details.issue-fold[open] summary { margin-bottom: 6px; }
.no-errors { color: var(--pass); font-weight: 600; margin: 4px 0; }
.copied { color: var(--pass) !important; border-color: var(--pass) !important; }
/* tabs */
.tabs { display: flex; gap: 8px; margin-top: 18px; flex-wrap: wrap; }
.tab-btn { display: inline-block; background: transparent; color: var(--muted);
  border: 1px solid var(--field-border); text-decoration: none; cursor: pointer;
  border-radius: var(--r-ctl); padding: 7px 16px; font-weight: 600; font-size: 0.88rem;
  letter-spacing: -0.01em; }
.tab-btn:hover { color: var(--accent); filter: none; }
.tab-btn.active { background: var(--accent-fill); color: var(--accent-on);
  border-color: var(--accent-fill); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
select {
  width: 100%; padding: 9px 11px; border: 1px solid var(--field-border);
  border-radius: var(--r-ctl); background: var(--bg); color: var(--text); font: inherit;
}
select:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
/* clinical view */
.patient-card { background: var(--accent-soft); border: 1px solid var(--border);
  border-radius: var(--r-card); padding: 14px 16px; margin: 12px 0 6px; }
.patient-name { font-size: 1.25rem; font-weight: 700; }
h3.cv-sec { margin: 20px 0 4px; font-size: 1rem; }
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
  color: var(--muted); border: 1px solid var(--border); border-radius: 999px;
  padding: 0 6px; margin-left: 6px; vertical-align: middle; }
.warn-banner { padding: 10px 14px; border-radius: var(--r-card); color: var(--fail);
  background: var(--fail-bg); border: 1px solid var(--fail);
  margin: 0 0 14px; font-weight: 600; }
tr.cv-row { cursor: pointer; }
tr.cv-row:hover td { background: var(--accent-soft); }
footer { max-width: 1060px; margin: 26px auto 40px; padding: 0 16px;
  font-size: 0.8rem; color: var(--muted); line-height: 1.6; }
footer a { color: var(--accent); }
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
  letter-spacing: -0.01em; cursor: pointer; white-space: nowrap; }
.cv-mode:first-child { border-left: none; }
.cv-mode:hover { color: var(--accent); filter: none; }
.cv-mode.active { background: var(--accent-fill); color: var(--accent-on); }
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
/* category colours, shared by timeline dots and legend */
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
  letter-spacing: 0.07em; color: var(--muted); }
.db-big { font-size: 1.55rem; font-weight: 700; line-height: 1.15; }
.db-sub { font-size: 0.8rem; color: var(--muted); }
.db-list { list-style: none; margin: 6px 0 0; padding: 0; font-size: 0.87rem; }
.db-list li { padding: 4px 0; border-bottom: 1px solid var(--border); }
.db-list li:last-child { border-bottom: none; }
.db-more { font-size: 0.76rem; color: var(--muted); margin-top: 5px; }
.mt { margin-top: 9px; }
.mt-top { display: flex; justify-content: space-between; font-size: 0.78rem;
  font-weight: 600; }
.meter { height: 7px; border-radius: 999px; background: var(--border);
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
</style>
</head>
<body>
<header>
  <div class="head-row">
    <h1>Nordic Hackathon 2026 - Patient Summary Challenge</h1>
    <div class="theme-toggle" role="group" aria-label="Colour theme">
      <button class="th-btn" data-theme-set="system" title="System theme"
              aria-label="System theme"><svg viewBox="0 0 16 16" aria-hidden="true">
        <rect x="1.8" y="2.8" width="12.4" height="8.4" rx="1.2"/>
        <path d="M5.5 14h5M8 11.2V14"/></svg></button>
      <button class="th-btn" data-theme-set="light" title="Light theme"
              aria-label="Light theme"><svg viewBox="0 0 16 16" aria-hidden="true">
        <circle cx="8" cy="8" r="3.1"/>
        <path d="M8 1.2v1.5M8 13.3v1.5M1.2 8h1.5M13.3 8h1.5M3.3 3.3l1.1 1.1M11.6
        11.6l1.1 1.1M12.7 3.3l-1.1 1.1M4.4 11.6l-1.1 1.1"/></svg></button>
      <button class="th-btn" data-theme-set="dark" title="Dark theme"
              aria-label="Dark theme"><svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M13.4 9.9A5.7 5.7 0 0 1 6.1 2.6a5.7 5.7 0 1 0 7.3 7.3z"/>
        </svg></button>
    </div>
  </div>
  <p class="sub">Submit your European Patient Summary (FHIR R4 Bundle) and get it
  validated against the <a href="https://hl7.eu/fhir/eps/1.0.0-ballot/"
  target="_blank" rel="noopener">HL7 EU EPS 1.0.0-ballot profile</a>.</p>
  <nav class="tabs">
    <a class="tab-btn active" data-tab="leaderboard" href="#leaderboard" aria-current="page">Leaderboard</a>
    <a class="tab-btn" data-tab="submit" href="#submit">Submit</a>
    <a class="tab-btn" data-tab="interop" href="#interop">Interop testing</a>
    <a class="tab-btn" data-tab="clinical" href="#clinical">Clinical view</a>
  </nav>
</header>
<main>

<div id="tab-leaderboard" class="tab-panel active">

<section class="card">
  <h2>Leaderboard<span class="live-dot" title="auto-refreshes every 5s"></span></h2>
  <div class="table-wrap">
  <table id="lb">
    <thead><tr>
      <th scope="col">#</th><th scope="col">Company</th><th scope="col">Score</th><th scope="col">Status</th>
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
    will not make it richer. Your company score is your best submission, plus a
    one-time +20 "first pass" bonus for the first company to submit a valid
    document. Interop testing is not scored.
  </p>
</section>

</div>

<div id="tab-submit" class="tab-panel">

<div class="grid">
  <section class="card">
    <h2>Register your company</h2>
    <p class="muted" style="margin:0 0 4px">One registration per company -
    share the token with your colleagues.</p>
    <label for="reg-name">Company name</label>
    <div class="row">
      <input type="text" id="reg-name" maxlength="40" placeholder="e.g. FHIR Starters">
      <button id="reg-btn">Register</button>
    </div>
    <div id="reg-code-row" hidden>
      <label for="reg-code">Registration code</label>
      <input type="text" id="reg-code" placeholder="from the organisers"
        autocomplete="off" spellcheck="false">
    </div>
    <div class="error-note" id="reg-error" role="alert"></div>
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
    <label for="bundle">FHIR Bundle JSON (paste, or pick a file below)</label>
    <textarea id="bundle" placeholder='{"resourceType": "Bundle", "type": "document", ...}'
      spellcheck="false"></textarea>
    <div class="file-row">
      <input type="file" id="file" accept=".json,application/json,application/fhir+json"
        aria-label="Pick a FHIR Bundle JSON file">
      <button id="submit-btn">Validate bundle</button>
    </div>
    <p class="muted" id="validator-note" role="status" style="margin:8px 0 0"></p>
    <div class="spinner" id="spinner"><span class="ring"></span>
      <span>Validating against <a href="https://github.com/ahdis/matchbox"
      target="_blank" rel="noopener">Matchbox</a> - usually a few seconds, up
      to a minute on the first submission after it starts...</span></div>
    <div class="error-note" id="submit-error" role="alert"></div>
    <div class="banner" id="banner" role="status"></div>
    <div id="issues-wrap">
      <div class="issue-summary" id="issue-summary"></div>
      <div id="issue-groups"></div>
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
  <div id="cv-list" style="margin-top:12px"></div>
</section>

<section class="card" id="cv-doc" style="display:none"></section>

</div>

<div id="tab-interop" class="tab-panel">

<section class="card">
  <h2>Interop testing</h2>
  <p class="muted" style="margin:0 0 8px">Compliance proves you can produce a
  valid patient summary; interoperability testing proves another system can
  consume it. Every company's best valid submission is published below as its
  exchange document - load one into your own system and see what breaks.</p>
  <p class="muted" style="margin:0">Nothing here is scored or tracked. Take a
  partner's document, test it, and tell them what you found: the useful output
  of this track is a defect the producer can go and fix.</p>
</section>

<section class="card">
  <h2>Published documents</h2>
  <p class="muted" id="io-hint" style="margin:0" role="status"></p>
  <div id="io-partners"></div>
</section>

<details class="card">
  <summary>Or use the API</summary>
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
  against the HL7 Europe <a href="http://hl7.eu/fhir/eps/" target="_blank"
  rel="noopener">Patient Summary</a> implementation guide. Terminology is SNOMED CT,
  LOINC and WHO ATC.</p>
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

/* ---- leaderboard ---- */
async function refreshLeaderboard() {
  try {
    const res = await fetch("/api/leaderboard");
    if (!res.ok) return;
    const rows = await res.json();
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
  const tab = TABS.indexOf(parts[0]) >= 0 ? parts[0] : "leaderboard";
  showTab(tab);
  if (tab !== "clinical") return;
  if (!parts[1]) {
    go("clinical/sample/" + DEFAULT_SAMPLE + "/" + cvMode, true);
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
    const res = await fetch("/api/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(code ? { team: name, code } : { team: name }),
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
    $("#token-value").textContent = data.token;
    $("#token-box").style.display = "block";
    syncToken(data.token);
    refreshLeaderboard();
  } catch (e) {
    errBox.textContent = "Network error: " + e.message;
    errBox.style.display = "block";
  }
});
["#reg-name", "#reg-code"].forEach((sel) => $(sel).addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") $("#reg-btn").click();
}));

/* FastAPI's validation errors arrive as a list, everything else as a string. */
function detailText(data) {
  if (!data || data.detail === undefined) return "";
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
  const m = text.match(/\s*\(from ([^)\s]+)\)\s*$/);
  if (m) return { msg: text.slice(0, m.index).trim(), profile: m[1] };
  return { msg: text, profile: null };
}

function cleanExpr(expr) {
  // "Bundle.entry[1].resource/*Patient/xyz*/" -> "Bundle.entry[1].resource"
  return expr.replace(/\/\*[\s\S]*?\*\//g, "").trim();
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
      + '<td class="loc" title="' + esc(g.exprFull) + '">' + esc(g.exprShort) + "</td>"
      + '<td class="msg">' + esc(g.message) + chips + "</td>"
      + "</tr>";
  }).join("");
  return '<div class="issues-scroll"><table class="issues"><thead><tr>'
    + '<th scope="col">Severity</th><th scope="col">Location</th><th scope="col">Message</th></tr></thead><tbody>'
    + rows + "</tbody></table></div>";
}

function foldedGroup(label, groups) {
  return '<details class="issue-fold"><summary>' + label + "</summary>"
    + issueTable(groups) + "</details>";
}

function renderIssues(issues) {
  const known = issues.filter((i) => i.severity in SEV_ORDER);
  const wrap = $("#issues-wrap");
  if (!known.length) { wrap.style.display = "none"; return; }

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
  const errG = groups.filter((g) => g.severity === "fatal" || g.severity === "error");
  const warnG = groups.filter((g) => g.severity === "warning");
  const infoG = groups.filter((g) => g.severity === "information");

  let html = errG.length ? issueTable(errG)
                         : '<p class="no-errors">No errors \u{1F389}</p>';
  if (warnG.length) html += foldedGroup(counts.warning + " warning"
    + (counts.warning === 1 ? "" : "s"), warnG);
  if (infoG.length) html += foldedGroup(counts.information + " information", infoG);
  $("#issue-groups").innerHTML = html;
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
async function checkValidator() {
  const note = $("#validator-note");
  try {
    const res = await fetch("/api/health");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const h = await res.json();
    if (h.max_body_bytes) maxBundleBytes = h.max_body_bytes;
    if (h.register_code_required) $("#reg-code-row").hidden = false;
    note.textContent = h.matchbox === "up" ? ""
      : h.matchbox === "degraded"
        ? "The validator is up but has not loaded " + (h.missing_igs || []).join(", ")
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
    const res = await fetch("/api/eps", {
      method: "POST",
      headers: { "Content-Type": "application/fhir+json", "X-Team-Token": token },
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
    const banner = $("#banner");
    banner.className = "banner " + (pass ? "pass" : "fail");
    let text;
    if (pass) {
      const resPts = Math.min(60, resources * 2);
      const secPts = Math.min(160, sections * 10);
      const base = 50 + secPts + resPts;
      text = "✔ VALID - 50 + " + sections + " distinct section"
        + (sections === 1 ? "" : "s") + " ×10"
        + (sections * 10 > 160 ? " (capped at +160)" : "")
        + " + " + resources + " resource" + (resources === 1 ? "" : "s") + " ×2"
        + (resources * 2 > 60 ? " (capped at +60)" : "")
        + (score > base ? " + 20 first pass" : "")
        + " = " + score;
    } else {
      text = "✖ INVALID - 0 pts - fix the " + errors + " error"
        + (errors === 1 ? "" : "s") + " below first";
    }
    banner.innerHTML = esc(text) + '<span class="score">' + score + " pts</span>";
    renderIssues(outcome.issue || []);
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
  "curl -s -X POST " + origin + "/api/register \\\n" +
  "  -H 'Content-Type: application/json' \\\n" +
  "  -d '{\"team\": \"My Company\"}'";
$("#curl-submit").textContent =
  "curl -s -D - -X POST " + origin + "/api/eps \\\n" +
  "  -H 'Content-Type: application/fhir+json' \\\n" +
  "  -H 'X-Team-Token: YOUR_TOKEN' \\\n" +
  "  --data-binary @my-eps-bundle.json";
$("#curl-leaderboard").textContent =
  "curl -s " + origin + "/api/leaderboard";

/* ---- clinical view ---- */
let cvSubsByTeam = new Map();  // slug -> that company's submissions, fetched when picked
let cvSamples = [];
let cvCurrent = null;
let cvMode = "document";
let cvDisclaimer = "";
/* The worked examples are one more entry in the company picker rather than a
   list of their own, so there is a single place to open a document from. */
const SAMPLE_TEAM = "__samples";

async function initSamples() {
  try {
    const res = await fetch("/api/samples");
    if (!res.ok) return;
    const data = await res.json();
    cvDisclaimer = data.disclaimer || "";
    cvSamples = (data.samples || []).filter((s) => s.available);
  } catch (e) { /* the picker says so when the list is empty */ }
}

/* The picker follows the document, but only at the moment a different one
   opens: browsing another company's list while reading a summary should not be
   undone by a view switch, and Back onto someone else's document should not
   leave the picker pointing at the company you came from. */
function routeTeam() {
  const parts = routeParts();
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
      const res = await fetch("/api/leaderboard");
      if (!res.ok) return;
      teams = (await res.json()).filter((t) => t.attempts > 0);
    } catch (e) { return; }
    const slugs = new Set(teams.map((t) => t.team_slug));
    const sel = $("#cv-team");
    const current = sel.value;
    sel.innerHTML = '<option value="' + SAMPLE_TEAM + '">Example documents</option>'
      + teams.map((t) =>
          '<option value="' + esc(t.team_slug) + '">' + esc(t.team) + "</option>").join("");
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
        + '<div class="cv-sub">' + esc(s.tagline) + "</div></td>"
        + "<td>" + esc(s.language) + "</td>"
        + '<td class="cv-scenario">' + esc(s.scenario)
        + (s.scenario_url
            ? ' <a href="' + esc(s.scenario_url) + '" target="_blank" rel="noopener">'
              + esc(s.scenario_name || "Read the scenario") + " \u2197</a>"
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
  let subs = cvSubsByTeam.get(slug);
  if (!subs) {
    box.innerHTML = '<p class="muted">Loading\u2026</p>';
    try {
      const res = await fetch("/api/submissions?team=" + encodeURIComponent(slug)
        + "&limit=" + CV_LIST_LIMIT);
      if (!res.ok) throw new Error("HTTP " + res.status);
      subs = await res.json();
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
     + "/" + cvMode);
});

/* Refetching on a view switch would be a round trip to redraw data already in
   hand, so the loaded document is keyed and only replaced when the key moves. */
let cvDocKey = null;

async function showDoc(kind, id, scroll) {
  const path = kind === "sample" ? "/api/samples/"
             : kind === "submission" ? "/api/submissions/" : null;
  if (!path || !id) { hideDoc(); return; }
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
const CAT_LABEL = { allergy: "Allergy", problem: "Problem", medication: "Medicine",
  result: "Result", procedure: "Procedure", immunization: "Vaccination",
  plan: "Care plan", device: "Device", other: "Other" };
const CAT_ORDER = ["allergy", "problem", "medication", "result", "procedure",
  "immunization", "device", "plan", "other"];
const RISK_FLAGS = ["high risk", "severe", "abnormal"];

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

  const empty = sections.filter((x) => !(x.items || []).length);
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
  if (!d.sample && !d.pass) {
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
    + (d.sample
        ? ' &middot; <a href="/api/samples/' + esc(d.sample.id)
          + '/bundle" download>download the FHIR bundle</a>'
        : " &middot; submitted " + new Date(d.ts).toLocaleString()
          + " &middot; " + (d.pass ? "valid" : "invalid") + ", score " + d.score
          + (d.errors ? ", " + plural(d.errors, "error") : "")
          + ' &middot; <a href="/api/submissions/' + d.id
          + '/bundle" download>download the FHIR bundle</a>')
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

/* ---- interop testing ---- */
function interopCurls() {
  $("#curl-exchange").textContent =
    "curl -s " + origin + "/api/exchange/PARTNER_SLUG -o partner-eps.json\n" +
    "# list every published document first:\n" +
    "curl -s " + origin + "/api/exchange";
}
$("#token").addEventListener("input", (ev) => syncToken(ev.target.value, "token"));

function partnerCard(p) {
  return '<div class="io-partner">'
    + "<strong>" + esc(p.team) + "</strong>"
    + '<p class="muted" style="margin:6px 0 0"><code>GET /api/exchange/' + esc(p.team_slug)
    + "</code> \u00b7 Document #" + p.exchange_id
    + " \u00b7 " + p.sections + " sections \u00b7 " + p.resources + " resources \u00b7 "
    + '<a href="#clinical/submission/' + p.exchange_id + '">view document</a>'
    + ' \u00b7 <a href="/api/submissions/' + p.exchange_id
    + '/bundle" download>download the FHIR bundle</a></p></div>';
}

async function initInterop() {
  interopCurls();
  const hint = $("#io-hint");
  const box = $("#io-partners");
  try {
    const res = await fetch("/api/exchange");
    if (!res.ok) return;
    const partners = await res.json();
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
function initTheme() {
  const btns = [...document.querySelectorAll(".th-btn")];
  const show = (mode) => btns.forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.themeSet === mode)));
  const stored = storedTheme();
  show(stored === "light" || stored === "dark" ? stored : "system");
  btns.forEach((b) => b.addEventListener("click", () => {
    const mode = b.dataset.themeSet;
    if (mode === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = mode;
    storeTheme(mode);
    show(mode);
  }));
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
OPENAPI_BODIES = {
    ("/api/register", "post"): {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["team"],
                    "properties": {
                        "team": {
                            "type": "string",
                            "minLength": 2,
                            "maxLength": 40,
                            "description": "Company name, shown on the leaderboard.",
                        },
                        "code": {
                            "type": "string",
                            "description": "Only when the server sets REGISTER_CODE.",
                        },
                    },
                },
                "example": {"team": "FHIR Starters"},
            }
        },
    },
    ("/api/eps", "post"): {
        "required": True,
        "content": {
            "application/fhir+json": {
                "schema": {
                    "type": "object",
                    "description": "A FHIR R4 Bundle of type 'document'.",
                },
                "example": {"resourceType": "Bundle", "type": "document", "entry": []},
            },
            "application/json": {"schema": {"type": "object"}},
        },
    },
}

# Score and verdict ride on headers so a client can act on them without parsing
# the OperationOutcome. FastAPI only documents headers it was told about.
SUBMIT_RESPONSE_HEADERS = {
    "X-Pass": ("true when the bundle had no error or fatal issue", "string"),
    "X-Score": ("points this submission earned, bonuses included", "integer"),
    "X-Errors": ("count of error and fatal issues", "integer"),
    "X-Sections": ("distinct populated Composition sections", "integer"),
    "X-Resources": ("clinical resources counted for richness", "integer"),
}

# Endpoints that read X-Team-Token. Anything else is open.
TOKEN_REQUIRED = {("/api/eps", "post")}


def custom_openapi() -> dict:
    """FastAPI's generated document, corrected where introspection falls short."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=APP_VERSION,
        summary="Validate and score European Patient Summary bundles.",
        description=(
            "Register a company, submit FHIR R4 EPS bundles for validation "
            "against `%s`, and fetch other companies' documents to test "
            "against.\n\n"
            "Team endpoints take the token from `POST /api/register`, sent as "
            "`X-Team-Token: TOKEN` or `Authorization: Bearer TOKEN`."
        ) % PROFILE,
        routes=app.routes,
    )
    schema["components"] = schema.get("components", {})
    schema["components"]["securitySchemes"] = {
        "TeamToken": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Team-Token",
            "description": "The token returned by POST /api/register.",
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
    submit = schema["paths"]["/api/eps"]["post"]["responses"]["200"]
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

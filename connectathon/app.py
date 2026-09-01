"""EPS Connectathon - gamified FHIR European Patient Summary validation service.

Teams register, submit EPS bundles, Matchbox validates, leaderboard keeps score.
Stdlib sqlite3 + FastAPI, no other runtime deps.
"""

import json
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

MATCHBOX_URL = os.environ.get("MATCHBOX_URL", "http://matchbox:8080/matchboxv3/fhir")
PROFILE = os.environ.get(
    "PROFILE", "http://hl7.eu/fhir/eps/StructureDefinition/bundle-eu-eps"
)
DATA_DIR = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8095"))

# Reject oversized submissions before they hit the audit log or Matchbox. The
# richest real EPS bundles are a few hundred KB, so 5 MB is generous headroom
# while still bounding disk use from a runaway or hostile client.
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(5 * 1024 * 1024)))

VALID_BASE = 50        # points for a submission with zero error/fatal issues
SECTION_POINTS = 10    # per populated top-level Composition section
RESOURCE_POINTS = 2    # per clinical resource (not Composition/Patient)
RESOURCE_CAP = 60      # max points from clinical resources
FIRST_BLOOD_BONUS = 20
INTEROP_CONSUMER_POINTS = 25   # per distinct partner whose document you consumed
INTEROP_PRODUCER_POINTS = 10   # per distinct team that consumed your document


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
                first_blood INTEGER NOT NULL DEFAULT 0,
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
            CREATE TABLE IF NOT EXISTS interop_confirmations (
                id            INTEGER PRIMARY KEY,
                consumer_id   INTEGER NOT NULL REFERENCES teams(id),
                producer_id   INTEGER NOT NULL REFERENCES teams(id),
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                ts            TEXT NOT NULL,
                UNIQUE(consumer_id, producer_id)
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


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "team"


def unique_slug(conn: sqlite3.Connection, name: str) -> str:
    base = slugify(name)
    slug, n = base, 1
    while conn.execute("SELECT 1 FROM teams WHERE slug = ?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    return slug


# ---------------------------------------------------------------- scoring


def count_bundle_richness(bundle) -> tuple[int, int]:
    """Return (populated_sections, clinical_resources) for a Bundle JSON value.

    - populated_sections: top-level section[] items of the (first) Composition
      that carry an entry[] with at least one element. Sections without entries
      (e.g. emptyReason-only) do not count.
    - clinical_resources: bundle entries whose resource is neither a
      Composition nor a Patient.
    Defensive against arbitrary JSON: anything malformed counts as 0.
    """
    if not isinstance(bundle, dict) or not isinstance(bundle.get("entry"), list):
        return 0, 0
    sections = 0
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
                        if (
                            isinstance(section, dict)
                            and isinstance(section.get("entry"), list)
                            and len(section["entry"]) >= 1
                        ):
                            sections += 1
            continue  # a Composition is never clinical payload
        if rtype == "Patient":
            continue
        resources += 1
    return sections, resources


def submission_score(passed: bool, sections: int, resources: int) -> int:
    """Conformance gates the score: any error/fatal issue means 0.

    A valid submission earns VALID_BASE, plus SECTION_POINTS per populated
    Composition section, plus RESOURCE_POINTS per clinical resource capped
    at RESOURCE_CAP points.
    """
    if not passed:
        return 0
    return (
        VALID_BASE
        + SECTION_POINTS * sections
        + min(RESOURCE_CAP, RESOURCE_POINTS * resources)
    )


# ---------------------------------------------------------------- clinician summary

MEDICATION_TYPES = ("MedicationStatement", "MedicationRequest", "MedicationDispense")

# Friendly wording for the standard list-empty-reason codes (used when a
# section's emptyReason carries only a code, no display text).
EMPTY_REASON_LABELS = {
    "nilknown": "None known",
    "notasked": "Not asked",
    "withheld": "Information withheld",
    "unavailable": "Unavailable",
    "notstarted": "Not started",
    "closed": "Closed",
}


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
        detail = _period_str(resource.get("period"))
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
        detail = _first_str(resource, "recordedOn")
    else:
        display = _cc_display(resource.get("code")) or _first_str(
            resource, "title", "description"
        )
        if display:
            label, detail = display, _humanize_type(rtype)
        else:
            label = _humanize_type(rtype)
    return {"label": label, "detail": detail, "status": status}


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
    """Recursively collect every 'reference' string inside a JSON value."""
    if isinstance(value, dict):
        ref = value.get("reference")
        if isinstance(ref, str):
            out.append(ref)
        for child in value.values():
            _collect_references(child, out)
    elif isinstance(value, list):
        for child in value:
            _collect_references(child, out)


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
                            empty_reason, empty_reason
                        )
                summary["sections"].append(
                    {"title": title, "items": items, "empty_reason": empty_reason}
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
    return summary


# ---------------------------------------------------------------- interop


def interop_ground_truth(bundle) -> dict:
    """Receipt ground truth for an exchange bundle: the (first) Patient's
    family names (casefold+strip), exact birthDate string, and the counts of
    medication and allergy resources among the bundle entries."""
    truth: dict = {"families": set(), "birth_date": None, "medications": 0, "allergies": 0}
    if not isinstance(bundle, dict) or not isinstance(bundle.get("entry"), list):
        return truth
    patient_seen = False
    for entry in bundle["entry"]:
        resource = entry.get("resource") if isinstance(entry, dict) else None
        if not isinstance(resource, dict):
            continue
        rtype = resource.get("resourceType")
        if rtype == "Patient" and not patient_seen:
            patient_seen = True
            names = resource.get("name")
            if isinstance(names, list):
                for name in names:
                    if isinstance(name, dict) and isinstance(name.get("family"), str):
                        family = name["family"].strip().casefold()
                        if family:
                            truth["families"].add(family)
            if isinstance(resource.get("birthDate"), str):
                truth["birth_date"] = resource["birthDate"]
        elif rtype in MEDICATION_TYPES:
            truth["medications"] += 1
        elif rtype == "AllergyIntolerance":
            truth["allergies"] += 1
    return truth


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


# ---------------------------------------------------------------- matchbox


def validate_with_matchbox(body: bytes) -> dict:
    """POST the bundle to Matchbox $validate; return the OperationOutcome dict.

    Raises MatchboxError when the validator is unreachable or unusable.
    Monkeypatched in tests.
    """
    url = (
        f"{MATCHBOX_URL}/$validate?profile="
        f"{urllib.parse.quote(PROFILE, safe='')}"
    )
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx may still carry an outcome
        payload = exc.read()
        try:
            outcome = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise MatchboxError(f"Matchbox returned HTTP {exc.code}") from exc
        if outcome.get("resourceType") != "OperationOutcome":
            raise MatchboxError(f"Matchbox returned HTTP {exc.code}") from exc
        return outcome
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
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


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="EPS Connectathon", lifespan=lifespan)


async def raw_body(request: Request) -> bytes:
    return await request.body()


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
        raise HTTPException(401, detail="Invalid team token")
    return team


@app.post("/api/register")
def register(payload: dict = Body(...)) -> JSONResponse:
    team = payload.get("team")
    if not isinstance(team, str):
        raise HTTPException(400, detail="Body must be {\"team\": \"<name>\"}")
    team = team.strip()
    if not 2 <= len(team) <= 40:
        raise HTTPException(400, detail="Team name must be 2-40 characters")
    token = secrets.token_hex(8)
    with connect() as conn:
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
            "note": "One team per company - company name = team name. Register "
            "your company once and share this token with your colleagues.",
        }
    )


@app.post("/api/eps")
def submit_eps(
    body: bytes = Depends(raw_body),
    x_team_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> Response:
    team = team_from_token(x_team_token, authorization)

    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            413,
            detail=f"Bundle too large ({len(body)} bytes); limit is "
            f"{MAX_BODY_BYTES} bytes",
        )

    try:
        bundle = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, detail="Body is not valid JSON")

    # Audit log: save the raw submission before anything else.
    now = datetime.now(timezone.utc)
    team_dir = os.path.join(DATA_DIR, "submissions", team["slug"])
    os.makedirs(team_dir, exist_ok=True)
    filename = now.strftime("%Y%m%dT%H%M%S_%fZ") + ".json"
    with open(os.path.join(team_dir, filename), "wb") as fh:
        fh.write(body)

    try:
        outcome = validate_with_matchbox(body)
    except MatchboxError as exc:
        raise HTTPException(502, detail=str(exc))

    counts = {"fatal": 0, "error": 0, "warning": 0, "information": 0}
    for issue in outcome.get("issue", []):
        sev = issue.get("severity")
        if sev in counts:
            counts[sev] += 1
    errors = counts["fatal"] + counts["error"]
    passed = errors == 0
    sections, resources = count_bundle_richness(bundle)
    score = submission_score(passed, sections, resources)

    awarded_first_blood = False
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if passed:
            cur = conn.execute(
                "UPDATE teams SET first_blood = 1 WHERE id = ? "
                "AND NOT EXISTS (SELECT 1 FROM teams WHERE first_blood = 1)",
                (team["id"],),
            )
            awarded_first_blood = cur.rowcount == 1
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

    effective_score = score + (FIRST_BLOOD_BONUS if awarded_first_blood else 0)
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


@app.get("/api/leaderboard")
def leaderboard() -> list[dict]:
    with connect() as conn:
        teams = conn.execute(
            "SELECT id, name, first_blood FROM teams"
        ).fetchall()
        subs = conn.execute(
            "SELECT team_id, ts, pass, errors, score, sections, resources "
            "FROM submissions ORDER BY ts"
        ).fetchall()
        consumed = {
            row["team_id"]: row["n"]
            for row in conn.execute(
                "SELECT consumer_id AS team_id, COUNT(DISTINCT producer_id) AS n "
                "FROM interop_confirmations GROUP BY consumer_id"
            )
        }
        produced = {
            row["team_id"]: row["n"]
            for row in conn.execute(
                "SELECT producer_id AS team_id, COUNT(DISTINCT consumer_id) AS n "
                "FROM interop_confirmations GROUP BY producer_id"
            )
        }

    by_team: dict[int, list[sqlite3.Row]] = {}
    for sub in subs:
        by_team.setdefault(sub["team_id"], []).append(sub)

    rows = []
    for team in teams:
        team_subs = by_team.get(team["id"], [])
        first_blood = bool(team["first_blood"])
        # Interop track: points for consuming distinct partners' documents
        # plus points for each distinct team that consumed this team's own.
        interop = INTEROP_CONSUMER_POINTS * consumed.get(
            team["id"], 0
        ) + INTEROP_PRODUCER_POINTS * produced.get(team["id"], 0)
        if team_subs:
            # Highest-scoring submission drives the team score and the
            # richness columns; its timestamp is the moment the team first
            # achieved its current best score (used as ranking tiebreak, so
            # re-submitting never hurts).
            best = min(team_subs, key=lambda s: (-s["score"], s["ts"]))
            score = (
                best["score"]
                + (FIRST_BLOOD_BONUS if first_blood else 0)
                + interop
            )
            rows.append(
                {
                    "team": team["name"],
                    "score": score,
                    "interop": interop,
                    "passed": any(s["pass"] for s in team_subs),
                    "attempts": len(team_subs),
                    "best_errors": min(s["errors"] for s in team_subs),
                    "best_sections": best["sections"],
                    "best_resources": best["resources"],
                    "last_submission": team_subs[-1]["ts"],
                    "first_blood": first_blood,
                    "_best_ts": best["ts"],
                }
            )
        else:
            rows.append(
                {
                    "team": team["name"],
                    "score": interop,  # consuming others needs no submission
                    "interop": interop,
                    "passed": False,
                    "attempts": 0,
                    "best_errors": None,
                    "best_sections": None,
                    "best_resources": None,
                    "last_submission": None,
                    "first_blood": first_blood,
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


def _submission_row(sub_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT s.*, t.name AS team_name, t.slug AS team_slug "
            "FROM submissions s JOIN teams t ON t.id = s.team_id WHERE s.id = ?",
            (sub_id,),
        ).fetchone()


@app.get("/api/submissions")
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


@app.get("/api/submissions/{sub_id}")
def submission_detail(sub_id: int) -> dict:
    row = _submission_row(sub_id)
    if row is None:
        raise HTTPException(404, detail="Unknown submission")
    raw = read_submission_bytes(row["team_slug"], row["filename"])
    if raw is None:
        raise HTTPException(404, detail="Submission file missing")
    try:
        bundle = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        bundle = None  # summarize_bundle degrades gracefully
    return {
        "id": row["id"],
        "team": row["team_name"],
        "team_slug": row["team_slug"],
        "ts": row["ts"],
        "pass": bool(row["pass"]),
        "errors": row["errors"],
        "warnings": row["warnings"],
        "score": row["score"],
        "summary": summarize_bundle(bundle),
    }


@app.get("/api/submissions/{sub_id}/bundle")
def submission_bundle(sub_id: int) -> Response:
    row = _submission_row(sub_id)
    if row is None:
        raise HTTPException(404, detail="Unknown submission")
    raw = read_submission_bytes(row["team_slug"], row["filename"])
    if raw is None:
        raise HTTPException(404, detail="Submission file missing")
    return Response(content=raw, media_type="application/fhir+json")


# ---------------------------------------------------------------- interop API


@app.get("/api/exchange")
def exchange_list(
    x_team_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> list[dict]:
    me = team_from_token(x_team_token, authorization)
    out = []
    with connect() as conn:
        teams = conn.execute(
            "SELECT id, name, slug FROM teams ORDER BY name COLLATE NOCASE"
        ).fetchall()
        confirmed = {
            row["producer_id"]
            for row in conn.execute(
                "SELECT producer_id FROM interop_confirmations WHERE consumer_id = ?",
                (me["id"],),
            )
        }
        for team in teams:
            if team["id"] == me["id"]:
                continue
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
                    "consumed_by_you": team["id"] in confirmed,
                }
            )
    return out


@app.get("/api/exchange/{team_slug}")
def exchange_fetch(
    team_slug: str,
    x_team_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> Response:
    team_from_token(x_team_token, authorization)  # any registered team may fetch
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
    return Response(
        content=raw,
        media_type="application/fhir+json",
        headers={"X-Exchange-Id": str(best["id"]), "X-Producer": producer["slug"]},
    )


@app.post("/api/interop/confirm")
def interop_confirm(
    payload: dict = Body(...),
    x_team_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    me = team_from_token(x_team_token, authorization)
    partner_slug = payload.get("partner")
    if not isinstance(partner_slug, str) or not partner_slug.strip():
        raise HTTPException(400, detail='Body must include "partner": "<team-slug>"')
    with connect() as conn:
        partner = conn.execute(
            "SELECT id, name, slug FROM teams WHERE slug = ?",
            (partner_slug.strip(),),
        ).fetchone()
        if partner is None:
            raise HTTPException(404, detail="Unknown partner team")
        if partner["id"] == me["id"]:
            raise HTTPException(400, detail="You cannot confirm your own document")
        exchange_id = payload.get("exchange_id")
        if exchange_id is not None:
            if isinstance(exchange_id, bool) or not isinstance(exchange_id, int):
                raise HTTPException(400, detail="exchange_id must be an integer")
            sub = conn.execute(
                "SELECT * FROM submissions WHERE id = ?", (exchange_id,)
            ).fetchone()
            if sub is None or sub["team_id"] != partner["id"] or not sub["pass"]:
                raise HTTPException(
                    400, detail="exchange_id is not a valid submission of that partner"
                )
        else:
            sub = best_valid_submission(conn, partner["id"])
            if sub is None:
                raise HTTPException(404, detail="Partner has no valid submission yet")

    family = payload.get("patient_family")
    birth = payload.get("birth_date")
    medications = payload.get("medications")
    allergies = payload.get("allergies")
    counts_ok = all(
        isinstance(v, int) and not isinstance(v, bool)
        for v in (medications, allergies)
    )
    if not isinstance(family, str) or not isinstance(birth, str) or not counts_ok:
        raise HTTPException(
            400,
            detail="Receipt must include patient_family (str), birth_date (str), "
            "medications (int) and allergies (int)",
        )

    raw = read_submission_bytes(partner["slug"], sub["filename"])
    if raw is None:
        raise HTTPException(404, detail="Exchange document file missing")
    try:
        bundle = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        bundle = None
    truth = interop_ground_truth(bundle)

    mismatched = []
    if family.strip().casefold() not in truth["families"]:
        mismatched.append("patient_family")
    if truth["birth_date"] is None or birth.strip() != truth["birth_date"]:
        mismatched.append("birth_date")
    if medications != truth["medications"]:
        mismatched.append("medications")
    if allergies != truth["allergies"]:
        mismatched.append("allergies")
    if mismatched:
        # Deliberately do not echo the expected values - consuming teams must
        # extract them from the document themselves.
        return JSONResponse(
            {"result": "mismatch", "fields": mismatched}, status_code=422
        )

    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO interop_confirmations "
            "(consumer_id, producer_id, submission_id, ts) VALUES (?, ?, ?, ?)",
            (
                me["id"],
                partner["id"],
                sub["id"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        inserted = cur.rowcount == 1
    if inserted:
        return JSONResponse(
            {
                "result": "confirmed",
                "partner": partner["slug"],
                "exchange_id": sub["id"],
                "consumer_points": INTEROP_CONSUMER_POINTS,
                "producer_points": INTEROP_PRODUCER_POINTS,
            }
        )
    return JSONResponse(
        {"result": "already_confirmed", "partner": partner["slug"]}
    )


@app.get("/api/interop/matrix")
def interop_matrix() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.name AS consumer, p.name AS producer, ic.ts "
            "FROM interop_confirmations ic "
            "JOIN teams c ON c.id = ic.consumer_id "
            "JOIN teams p ON p.id = ic.producer_id ORDER BY ic.ts"
        ).fetchall()
    return [
        {"consumer": r["consumer"], "producer": r["producer"], "ts": r["ts"]}
        for r in rows
    ]


@app.get("/api/health")
def health() -> dict:
    matchbox = "down"
    try:
        with urllib.request.urlopen(matchbox_health_url(), timeout=3) as resp:
            if resp.status == 200:
                matchbox = "up"
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    return {"status": "ok", "matchbox": matchbox}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


# ---------------------------------------------------------------- UI page

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EPS Connectathon</title>
<style>
:root {
  --bg: #f4f5f8; --card: #ffffff; --text: #1c2434; --muted: #64708a;
  --border: #e2e6ee; --accent: #4f6df5; --accent-soft: #eef1fe;
  --pass: #178a50; --pass-bg: #e5f6ec; --fail: #c93b3b; --fail-bg: #fbeaea;
  --warn: #9a6700; --warn-bg: #fff3d6; --info: #3b6ec9; --info-bg: #e8f0fc;
  --gold: #b8860b; --gold-bg: #fdf6e0; --silver: #6e7685; --silver-bg: #f0f2f5;
  --bronze: #a05a2c; --bronze-bg: #f9efe6; --code-bg: #f0f2f7;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0e1320; --card: #171e30; --text: #e7ebf5; --muted: #93a0ba;
    --border: #27314b; --accent: #7d93ff; --accent-soft: #232c49;
    --pass: #4cc98a; --pass-bg: #10331f; --fail: #f07878; --fail-bg: #3a1a1a;
    --warn: #e2b93b; --warn-bg: #37300f; --info: #7aa7f0; --info-bg: #16263f;
    --gold: #e3b341; --gold-bg: #2e2712; --silver: #aeb8ca; --silver-bg: #232a3a;
    --bronze: #d1885a; --bronze-bg: #2e2013; --code-bg: #10162a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Ubuntu, Cantarell, "Helvetica Neue", Arial, sans-serif;
}
main { max-width: 1060px; margin: 0 auto; padding: 24px 16px 64px; }
header { max-width: 1060px; margin: 0 auto; padding: 36px 16px 4px; }
h1 { margin: 0; font-size: 1.7rem; letter-spacing: -0.02em; }
.sub { margin: 6px 0 0; color: var(--muted); }
h2 { margin: 0 0 14px; font-size: 1.12rem; }
.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 20px 22px; margin-top: 20px;
}
.grid { display: grid; grid-template-columns: 1fr 1.4fr; gap: 20px; }
@media (max-width: 820px) { .grid { grid-template-columns: 1fr; } }
label { display: block; font-size: 0.83rem; color: var(--muted); margin: 12px 0 4px; }
input[type=text], textarea {
  width: 100%; padding: 9px 11px; border: 1px solid var(--border);
  border-radius: 8px; background: var(--bg); color: var(--text);
  font: inherit;
}
textarea { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.82rem; min-height: 170px; resize: vertical; }
input:focus, textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
button {
  font: inherit; font-weight: 600; border: none; border-radius: 8px;
  padding: 9px 18px; cursor: pointer; background: var(--accent); color: #fff;
}
button:hover { filter: brightness(1.08); }
button:disabled { opacity: 0.55; cursor: wait; }
button.ghost {
  background: transparent; color: var(--accent);
  border: 1px solid var(--border); font-weight: 500; padding: 4px 10px;
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
  display: none; margin-top: 14px; padding: 12px 14px; border-radius: 10px;
  background: var(--accent-soft); border: 1px solid var(--border);
}
.token-box code { font-size: 1.05rem; font-weight: 700; letter-spacing: 0.04em;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.banner { display: none; margin-top: 16px; padding: 14px 18px; border-radius: 10px;
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
.error-note { display: none; margin-top: 14px; padding: 10px 14px;
  border-radius: 10px; color: var(--fail); background: var(--fail-bg);
  border: 1px solid var(--fail); font-size: 0.9rem; }
pre.curl {
  background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 14px; overflow-x: auto;
  font-size: 0.8rem; line-height: 1.5;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
details summary { cursor: pointer; font-weight: 600; font-size: 1.12rem; }
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
  border-radius: 8px; }
table.issues { font-size: 0.82rem; }
table.issues thead th { position: sticky; top: 0; background: var(--card); z-index: 1; }
table.issues td { vertical-align: top; }
table.issues td.msg { overflow-wrap: anywhere; }
table.issues td.loc {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
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
.tab-btn { background: transparent; color: var(--muted); border: 1px solid var(--border);
  border-radius: 999px; padding: 7px 16px; font-weight: 600; font-size: 0.88rem; }
.tab-btn:hover { color: var(--accent); filter: none; }
.tab-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
select, input[type=number] {
  width: 100%; padding: 9px 11px; border: 1px solid var(--border);
  border-radius: 8px; background: var(--bg); color: var(--text); font: inherit;
}
select:focus, input[type=number]:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
/* clinician view */
.patient-card { background: var(--accent-soft); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px; margin: 12px 0 6px; }
.patient-name { font-size: 1.25rem; font-weight: 700; }
h3.cv-sec { margin: 20px 0 4px; font-size: 1rem; }
.cv-items { list-style: none; padding: 0; margin: 4px 0 10px; }
.cv-items li { padding: 8px 2px; border-bottom: 1px solid var(--border); }
.cv-items li:last-child { border-bottom: none; }
.cv-label { font-weight: 600; }
.cv-detail { font-size: 0.85rem; }
.cv-status { display: inline-block; font-size: 0.68rem; font-weight: 600;
  color: var(--muted); border: 1px solid var(--border); border-radius: 999px;
  padding: 0 6px; margin-left: 6px; vertical-align: middle; }
.warn-banner { padding: 10px 14px; border-radius: 10px; color: var(--fail);
  background: var(--fail-bg); border: 1px solid var(--fail);
  margin: 0 0 14px; font-weight: 600; }
tr.cv-row { cursor: pointer; }
tr.cv-row:hover td { background: var(--accent-soft); }
/* interop */
ol.steps { margin: 8px 0 14px; padding-left: 22px; }
ol.steps li { margin: 6px 0; }
ol.steps code, p.muted code { background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 1px 5px; font-size: 0.8rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.io-partner { border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; margin-top: 12px; }
.io-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px; margin-top: 8px; align-items: end; }
.io-form label { margin: 0 0 3px; }
.io-result { margin-top: 8px; font-size: 0.9rem; display: none; }
.io-result.ok { display: block; color: var(--pass); font-weight: 600; }
.io-result.bad { display: block; color: var(--fail); font-weight: 600; }
table.matrix td, table.matrix th { text-align: center; }
table.matrix td.mx-name, table.matrix th.mx-name { text-align: left; }
table.matrix td.yes { color: var(--pass); font-weight: 700; }
</style>
</head>
<body>
<header>
  <h1>EPS Connectathon - Patient Summary Challenge</h1>
  <p class="sub">Submit your European Patient Summary (FHIR R4 Bundle) and get it
  validated against the HL7 EU EPS 1.0.0-ballot profile. Validity is the gate -
  richness wins the game.</p>
  <p class="sub"><strong>One team per company</strong> - register your company once
  and share the token within it.</p>
  <nav class="tabs">
    <button class="tab-btn active" data-tab="leaderboard">Leaderboard</button>
    <button class="tab-btn" data-tab="submit">Submit</button>
    <button class="tab-btn" data-tab="clinician">Clinician view</button>
    <button class="tab-btn" data-tab="interop">Interop testing</button>
  </nav>
</header>
<main>

<div id="tab-leaderboard" class="tab-panel active">

<section class="card">
  <h2>Leaderboard<span class="live-dot" title="auto-refreshes every 5s"></span></h2>
  <div class="table-wrap">
  <table id="lb">
    <thead><tr>
      <th>#</th><th>Company</th><th>Score</th><th>Interop</th><th>Status</th>
      <th>Sections</th><th>Resources</th><th>Errors (best)</th><th>Last submission</th>
    </tr></thead>
    <tbody id="lb-body"></tbody>
  </table>
  </div>
  <p class="muted" id="lb-empty" style="display:none">No companies yet - be the first to register.</p>
</section>

<section class="card">
  <h2>Interop matrix</h2>
  <p class="muted" style="margin:0 0 10px">A check mark means the row company
  fetched the column company's exchange document and posted back a correct
  extraction receipt (see the Interop testing tab).</p>
  <div class="table-wrap" id="matrix-wrap"></div>
  <p class="muted" id="matrix-empty">No interop confirmations yet.</p>
</section>

<section class="card">
  <h2>How scoring works</h2>
  <p class="muted" style="margin:0">
    Any <strong>error or fatal</strong> validation issue scores the submission
    <strong>0</strong> - fix conformance first. A valid bundle earns
    <strong>50</strong>, plus <strong>+10</strong> per Composition section that
    actually contains entries, plus <strong>+2</strong> per clinical resource in
    the bundle (everything except the Composition and Patient), capped at +60.
    Your team score is your <strong>best submission</strong>; the first team to
    go valid keeps a one-time <strong>+20 first blood</strong> bonus.
    Interop adds on top: <strong>+25</strong> for every partner document your
    team proves it consumed and <strong>+10</strong> for every team that
    consumes yours.
  </p>
</section>

</div>

<div id="tab-submit" class="tab-panel">

<div class="grid">
  <section class="card">
    <h2>Register your company</h2>
    <p class="muted" style="margin:0 0 4px">Company name = team name. One
    registration per company; share the token with your colleagues.</p>
    <label for="reg-name">Company name</label>
    <div class="row">
      <input type="text" id="reg-name" maxlength="40" placeholder="e.g. FHIR Starters">
      <button id="reg-btn">Register</button>
    </div>
    <div class="error-note" id="reg-error"></div>
    <div class="token-box" id="token-box">
      <div class="row" style="justify-content:space-between">
        <code id="token-value"></code>
        <button class="ghost" id="token-copy">Copy</button>
      </div>
      <p class="muted" style="margin:8px 0 0"><strong>Save this token now.</strong>
      It is shown only once and is required for every submission. It has been
      pre-filled in the submit form and stored in this browser.</p>
    </div>
  </section>

  <section class="card">
    <h2>Submit your EPS</h2>
    <label for="token">Team token</label>
    <input type="text" id="token" placeholder="paste your team token"
      autocomplete="off" spellcheck="false">
    <label for="bundle">FHIR Bundle JSON (paste, or pick a file below)</label>
    <textarea id="bundle" placeholder='{"resourceType": "Bundle", "type": "document", ...}'
      spellcheck="false"></textarea>
    <div class="file-row">
      <input type="file" id="file" accept=".json,application/json,application/fhir+json">
      <button id="submit-btn">Validate bundle</button>
    </div>
    <div class="spinner" id="spinner"><span class="ring"></span>
      <span>Validating against Matchbox - this can take up to 30 seconds...</span></div>
    <div class="error-note" id="submit-error"></div>
    <div class="banner" id="banner"></div>
    <div id="issues-wrap">
      <div class="issue-summary" id="issue-summary"></div>
      <div id="issue-groups"></div>
    </div>
  </section>
</div>

<details class="card">
  <summary>Or use the API</summary>
  <p class="muted">Register your company (one team per company):</p>
  <pre class="curl" id="curl-register"></pre>
  <p class="muted">Submit a bundle (response headers carry X-Pass / X-Score / X-Errors,
  body is the FHIR OperationOutcome):</p>
  <pre class="curl" id="curl-submit"></pre>
  <p class="muted">Leaderboard:</p>
  <pre class="curl" id="curl-leaderboard"></pre>
</details>

</div>

<div id="tab-clinician" class="tab-panel">

<section class="card">
  <h2>Clinician view</h2>
  <p class="muted" style="margin:0 0 4px">A readable rendering of submitted
  patient summaries - what a clinician would actually see.</p>
  <label for="cv-team">Company</label>
  <select id="cv-team"><option value="">Select a company...</option></select>
  <div id="cv-list" style="margin-top:12px"></div>
</section>

<section class="card" id="cv-doc" style="display:none"></section>

</div>

<div id="tab-interop" class="tab-panel">

<section class="card">
  <h2>Interop testing</h2>
  <p class="muted" style="margin:0 0 8px">Compliance proves you can
  <em>produce</em> a valid patient summary; interop proves another system can
  <em>consume</em> it. Your team's best valid submission is automatically
  published as its exchange document.</p>
  <ol class="steps">
    <li>Fetch a partner's document with your token:
      <code>GET /api/exchange/&lt;team-slug&gt;</code></li>
    <li>Parse it in <strong>your</strong> system and extract the patient's family
      name, birth date, and the number of medication and allergy entries.</li>
    <li>Post the receipt to <code>POST /api/interop/confirm</code>. All four
      values must match: you earn <strong>+25</strong>, the producer earns
      <strong>+10</strong>.</li>
  </ol>
  <p class="muted">Fetch a partner's exchange document:</p>
  <pre class="curl" id="curl-exchange"></pre>
  <p class="muted">Post your extraction receipt:</p>
  <pre class="curl" id="curl-confirm"></pre>
</section>

<section class="card">
  <h2>Partners</h2>
  <p class="muted" id="io-hint" style="margin:0"></p>
  <div id="io-partners"></div>
</section>

</div>

</main>
<script>
"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

/* ---- leaderboard ---- */
let lbTeams = [];
async function refreshLeaderboard() {
  try {
    const res = await fetch("/api/leaderboard");
    if (!res.ok) return;
    const rows = await res.json();
    lbTeams = rows.map((r) => r.team);
    $("#lb-empty").style.display = rows.length ? "none" : "block";
    $("#lb").style.display = rows.length ? "table" : "none";
    $("#lb-body").innerHTML = rows.map((r) => `
      <tr class="${r.rank <= 3 ? "rank-" + r.rank : ""}">
        <td>${r.rank}</td>
        <td>${esc(r.team)}${r.first_blood
          ? ' <span title="First blood: first team to pass">\u{1F525}</span>' : ""}</td>
        <td><strong>${r.score}</strong></td>
        <td>${r.interop || 0}</td>
        <td>${r.passed ? '<span class="badge pass">VALID</span>'
                       : '<span class="badge fail">NOT YET</span>'}</td>
        <td>${r.best_sections === null ? "-" : r.best_sections}</td>
        <td>${r.best_resources === null ? "-" : r.best_resources}</td>
        <td>${r.best_errors === null ? "-" : r.best_errors}</td>
        <td class="muted">${r.last_submission
          ? new Date(r.last_submission).toLocaleTimeString() : "-"}</td>
      </tr>`).join("");
  } catch (e) { /* transient network error - retry on next tick */ }
}
refreshLeaderboard().then(refreshMatrix);
setInterval(refreshLeaderboard, 5000);
setInterval(refreshMatrix, 5000);

/* ---- interop matrix ---- */
async function refreshMatrix() {
  try {
    const res = await fetch("/api/interop/matrix");
    if (!res.ok) return;
    const pairs = await res.json();
    const wrap = $("#matrix-wrap");
    const teams = lbTeams.length ? lbTeams
      : [...new Set(pairs.flatMap((p) => [p.consumer, p.producer]))];
    if (!pairs.length || teams.length < 2) {
      wrap.innerHTML = "";
      $("#matrix-empty").style.display = "block";
      return;
    }
    $("#matrix-empty").style.display = "none";
    const done = new Set(pairs.map((p) => p.consumer + "\u0000" + p.producer));
    let html = '<table class="matrix"><thead><tr>'
      + '<th class="mx-name">Consumer ↓ / Producer →</th>'
      + teams.map((t) => "<th>" + esc(t) + "</th>").join("") + "</tr></thead><tbody>";
    for (const c of teams) {
      html += '<tr><td class="mx-name"><strong>' + esc(c) + "</strong></td>"
        + teams.map((p) => {
            if (p === c) return '<td class="muted">-</td>';
            return done.has(c + "\u0000" + p)
              ? '<td class="yes" title="' + esc(c) + " consumed " + esc(p) + '">✓</td>'
              : "<td></td>";
          }).join("") + "</tr>";
    }
    wrap.innerHTML = html + "</tbody></table>";
  } catch (e) { /* transient network error - retry on next tick */ }
}

/* ---- tabs ---- */
function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) =>
    p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "clinician") initClinician();
  if (name === "interop") initInterop();
}
document.querySelectorAll(".tab-btn").forEach((btn) =>
  btn.addEventListener("click", () => switchTab(btn.dataset.tab)));

/* ---- register ---- */
$("#reg-btn").addEventListener("click", async () => {
  const name = $("#reg-name").value.trim();
  const errBox = $("#reg-error");
  errBox.style.display = "none";
  $("#token-box").style.display = "none";
  try {
    const res = await fetch("/api/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ team: name }),
    });
    const data = await res.json();
    if (!res.ok) {
      errBox.textContent = data.detail || ("Registration failed (" + res.status + ")");
      errBox.style.display = "block";
      return;
    }
    $("#token-value").textContent = data.token;
    $("#token-box").style.display = "block";
    $("#token").value = data.token;
    localStorage.setItem("eps_token", data.token);
    localStorage.setItem("eps_team", data.team);
    refreshLeaderboard();
  } catch (e) {
    errBox.textContent = "Network error: " + e.message;
    errBox.style.display = "block";
  }
});

function flashCopied(btn) {
  const old = btn.textContent;
  btn.textContent = "Copied!";
  btn.classList.add("copied");
  setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1500);
}
$("#token-copy").addEventListener("click", (ev) => {
  navigator.clipboard.writeText($("#token-value").textContent)
    .then(() => flashCopied(ev.target));
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
    + "<th>Severity</th><th>Location</th><th>Message</th></tr></thead><tbody>"
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
$("#token").value = localStorage.getItem("eps_token") || "";
$("#file").addEventListener("change", async (ev) => {
  const f = ev.target.files[0];
  if (f) $("#bundle").value = await f.text();
});

$("#submit-btn").addEventListener("click", async () => {
  const token = $("#token").value.trim();
  const body = $("#bundle").value;
  const errBox = $("#submit-error");
  errBox.style.display = "none";
  $("#banner").className = "banner";
  $("#issues-wrap").style.display = "none";
  if (!token) {
    errBox.textContent = "A team token is required - register a team first.";
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
    if (res.status === 401 || res.status === 400 || res.status === 502) {
      const data = await res.json().catch(() => ({}));
      errBox.textContent = (data.detail || "Request failed") + " (HTTP " + res.status + ")";
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
      const base = 50 + sections * 10 + resPts;
      text = "✔ VALID - 50 + " + sections + " section" + (sections === 1 ? "" : "s")
        + " ×10 + " + resources + " resource" + (resources === 1 ? "" : "s") + " ×2"
        + (resources * 2 > 60 ? " (capped at +60)" : "")
        + (score > base ? " + 20 first blood" : "")
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

/* ---- clinician view ---- */
let cvSubs = [];

async function initClinician() {
  try {
    const res = await fetch("/api/submissions?limit=200");
    if (!res.ok) return;
    cvSubs = await res.json();
  } catch (e) { return; }
  const seen = new Map();
  cvSubs.forEach((s) => { if (!seen.has(s.team_slug)) seen.set(s.team_slug, s.team); });
  const sel = $("#cv-team");
  const current = sel.value;
  sel.innerHTML = '<option value="">Select a company...</option>'
    + [...seen.entries()].map(([slug, name]) =>
        '<option value="' + esc(slug) + '">' + esc(name) + "</option>").join("");
  if (seen.has(current)) sel.value = current;
  renderCvList();
}

function renderCvList() {
  const slug = $("#cv-team").value;
  const box = $("#cv-list");
  if (!slug) {
    box.innerHTML = '<p class="muted">Pick a company to see its submissions.</p>';
    return;
  }
  const subs = cvSubs.filter((s) => s.team_slug === slug);
  if (!subs.length) {
    box.innerHTML = '<p class="muted">No submissions yet.</p>';
    return;
  }
  box.innerHTML = '<div class="table-wrap"><table><thead><tr>'
    + "<th>Time</th><th>Status</th><th>Score</th><th>Sections</th><th>Resources</th>"
    + "</tr></thead><tbody>"
    + subs.map((s) => '<tr class="cv-row" data-id="' + s.id
      + '" title="click to open"><td>' + new Date(s.ts).toLocaleString() + "</td>"
      + "<td>" + (s.pass ? '<span class="badge pass">VALID</span>'
                         : '<span class="badge fail">INVALID</span>') + "</td>"
      + "<td><strong>" + s.score + "</strong></td>"
      + "<td>" + s.sections + "</td><td>" + s.resources + "</td></tr>").join("")
    + "</tbody></table></div>";
}

$("#cv-team").addEventListener("change", renderCvList);
$("#cv-list").addEventListener("click", (ev) => {
  const row = ev.target.closest("tr.cv-row");
  if (row) openSubmission(parseInt(row.dataset.id, 10));
});

async function openSubmission(id) {
  let data;
  try {
    const res = await fetch("/api/submissions/" + id);
    if (!res.ok) return;
    data = await res.json();
  } catch (e) { return; }
  renderClinicianDoc(data);
  $("#cv-doc").scrollIntoView({ behavior: "smooth" });
}

function cvItemHtml(item) {
  return "<li>"
    + '<span class="cv-label">' + esc(item.label || "Unnamed item") + "</span>"
    + (item.status ? '<span class="cv-status">' + esc(item.status) + "</span>" : "")
    + (item.detail ? '<div class="cv-detail muted">' + esc(item.detail) + "</div>" : "")
    + "</li>";
}

function renderClinicianDoc(d) {
  const s = d.summary || {};
  const comp = s.composition || {};
  let html = "";
  if (!d.pass) {
    html += '<div class="warn-banner">Invalid submission - this document failed'
      + " validation and would be rejected in production. Content shown as-is.</div>";
  }
  html += "<h2>" + esc(comp.title || "Patient summary") + "</h2>";
  html += '<p class="muted" style="margin:2px 0 0">' + esc(d.team)
    + " · submitted " + new Date(d.ts).toLocaleString()
    + (comp.date ? " · document date " + esc(comp.date) : "") + "</p>";
  const p = s.patient;
  if (p) {
    const meta = [p.birth_date ? "Born " + esc(p.birth_date) : null,
                  p.gender ? esc(p.gender) : null].filter(Boolean).join(" · ");
    html += '<div class="patient-card">'
      + '<div class="patient-name">' + esc(p.name || "Unnamed patient") + "</div>"
      + (meta ? "<div>" + meta + "</div>" : "")
      + (p.identifiers && p.identifiers.length
        ? '<div class="muted" style="font-size:0.8rem">'
          + p.identifiers.map((i) => esc(i.value)
              + (i.system ? " (" + esc(i.system) + ")" : "")).join(" · ") + "</div>"
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
  const doc = $("#cv-doc");
  doc.innerHTML = html;
  doc.style.display = "block";
}

/* ---- interop testing ---- */
function interopCurls() {
  const tok = $("#token").value.trim() || "YOUR_TOKEN";
  $("#curl-exchange").textContent =
    "curl -s " + origin + "/api/exchange/PARTNER_SLUG \\\n" +
    "  -H 'X-Team-Token: " + tok + "' -o partner-eps.json\n" +
    "# list available partners first: curl -s " + origin + "/api/exchange" +
    " -H 'X-Team-Token: " + tok + "'";
  $("#curl-confirm").textContent =
    "curl -s -X POST " + origin + "/api/interop/confirm \\\n" +
    "  -H 'Content-Type: application/json' \\\n" +
    "  -H 'X-Team-Token: " + tok + "' \\\n" +
    "  -d '{\"partner\": \"PARTNER_SLUG\", \"patient_family\": \"...\"," +
    " \"birth_date\": \"YYYY-MM-DD\", \"medications\": 0, \"allergies\": 0}'";
}
$("#token").addEventListener("input", interopCurls);

function partnerCard(p) {
  return '<div class="io-partner" data-slug="' + esc(p.team_slug)
    + '" data-exid="' + p.exchange_id + '">'
    + '<div class="row" style="justify-content:space-between;flex-wrap:wrap">'
    + "<strong>" + esc(p.team) + "</strong>"
    + (p.consumed_by_you ? '<span class="badge pass">CONFIRMED</span>'
                         : '<span class="muted">not confirmed yet</span>')
    + "</div>"
    + '<p class="muted" style="margin:6px 0 0">Document #' + p.exchange_id
    + " · " + p.sections + " sections · " + p.resources + " resources · "
    + '<a href="#" class="io-view" data-id="' + p.exchange_id + '">View document</a>'
    + ' · <a href="/api/submissions/' + p.exchange_id
    + '/bundle" target="_blank">raw bundle</a></p>'
    + '<div class="io-form">'
    + '<div><label>Patient family name</label><input type="text" class="io-family"></div>'
    + '<div><label>Birth date</label><input type="text" class="io-birth" placeholder="YYYY-MM-DD"></div>'
    + '<div><label>Medication entries</label><input type="number" class="io-meds" min="0"></div>'
    + '<div><label>Allergy entries</label><input type="number" class="io-alls" min="0"></div>'
    + '<div><button class="io-confirm">Confirm</button></div>'
    + "</div>"
    + '<div class="io-result"></div></div>';
}

async function initInterop() {
  interopCurls();
  const token = $("#token").value.trim();
  const hint = $("#io-hint");
  const box = $("#io-partners");
  if (!token) {
    hint.textContent = "Paste your team token in the Submit tab first"
      + " (it is remembered by this browser).";
    box.innerHTML = "";
    return;
  }
  try {
    const res = await fetch("/api/exchange", { headers: { "X-Team-Token": token } });
    if (res.status === 401) {
      hint.textContent = "That token was not recognised - check the Submit tab.";
      box.innerHTML = "";
      return;
    }
    if (!res.ok) return;
    const partners = await res.json();
    hint.textContent = partners.length
      ? "These companies have published a valid exchange document. Fetch one,"
        + " parse it in your system, then post back what you extracted."
      : "No other company has a valid submission yet - check back soon.";
    box.innerHTML = partners.map(partnerCard).join("");
  } catch (e) {
    hint.textContent = "Network error: " + e.message;
  }
}

$("#io-partners").addEventListener("click", (ev) => {
  const view = ev.target.closest(".io-view");
  if (view) {
    ev.preventDefault();
    switchTab("clinician");
    openSubmission(parseInt(view.dataset.id, 10));
    return;
  }
  const btn = ev.target.closest(".io-confirm");
  if (btn) confirmPartner(btn.closest(".io-partner"), btn);
});

async function confirmPartner(card, btn) {
  const token = $("#token").value.trim();
  const out = card.querySelector(".io-result");
  out.className = "io-result";
  const body = {
    partner: card.dataset.slug,
    exchange_id: parseInt(card.dataset.exid, 10),
    patient_family: card.querySelector(".io-family").value.trim(),
    birth_date: card.querySelector(".io-birth").value.trim(),
    medications: parseInt(card.querySelector(".io-meds").value, 10),
    allergies: parseInt(card.querySelector(".io-alls").value, 10),
  };
  if (!body.patient_family || !body.birth_date
      || isNaN(body.medications) || isNaN(body.allergies)) {
    out.textContent = "Fill in all four receipt fields first.";
    out.className = "io-result bad";
    return;
  }
  btn.disabled = true;
  try {
    const res = await fetch("/api/interop/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Team-Token": token },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.result === "confirmed") {
      out.textContent = "✓ Confirmed! +" + data.consumer_points
        + " for you, +" + data.producer_points + " for the producer.";
      out.className = "io-result ok";
      const slot = card.querySelector(".row span");
      if (slot) slot.outerHTML = '<span class="badge pass">CONFIRMED</span>';
      refreshLeaderboard().then(refreshMatrix);
    } else if (res.ok && data.result === "already_confirmed") {
      out.textContent = "Already confirmed - no new points.";
      out.className = "io-result ok";
    } else if (res.status === 422 && data.result === "mismatch") {
      out.textContent = "✗ Mismatch on: " + (data.fields || []).join(", ")
        + ". Re-check your extraction and try again.";
      out.className = "io-result bad";
    } else {
      out.textContent = (data.detail || "Request failed")
        + " (HTTP " + res.status + ")";
      out.className = "io-result bad";
    }
  } catch (e) {
    out.textContent = "Network error: " + e.message;
    out.className = "io-result bad";
  } finally {
    btn.disabled = false;
  }
}

/* initial state for the clinician panel */
renderCvList();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)

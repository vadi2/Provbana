"""Tests for the Nordic Hackathon 2026 service.

Matchbox is monkeypatched with canned OperationOutcomes; DATA_DIR points at a
pytest tmp dir so every test gets a fresh SQLite database and audit log.

Scoring model under test: error/fatal issues gate the score to 0; a valid
submission earns 50 + 10 per populated Composition section + 2 per clinical
resource (capped at +60). Team score = max submission score (+20 one-time
first pass for the first team ever to go valid).
"""

import io
import http.client
import json
import os
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

import app as app_module


def bundle(sections=0, resources=0, empty_sections=0):
    """Build a document Bundle with N populated sections, M clinical resources.

    Section i points at resource i, so a populated section needs the bundle to
    carry at least that many clinical resources - which is exactly the rule
    the scorer applies.
    """
    assert sections <= resources, "a populated section needs a resource to point at"
    comp = {"resourceType": "Composition", "status": "final", "section": []}
    for i in range(sections):
        comp["section"].append(
            {"title": f"section {i}", "entry": [{"reference": f"urn:uuid:res-{i}"}]}
        )
    for i in range(empty_sections):
        comp["section"].append(
            {"title": f"empty {i}",
             "emptyReason": {"coding": [{"code": "unavailable"}]}}
        )
    entries = [
        {"resource": comp},
        {"resource": {"resourceType": "Patient", "id": "p1"}},
    ]
    for i in range(resources):
        entries.append({
            "fullUrl": f"urn:uuid:res-{i}",
            "resource": {"resourceType": "Observation", "id": f"o{i}"},
        })
    return json.dumps({"resourceType": "Bundle", "type": "document", "entry": entries})


BUNDLE = bundle()  # valid shape, no populated sections, no clinical payload


def bundle_with_section(*sections, resources=0):
    """A document Bundle carrying exactly the given Composition sections.

    Resources get a fullUrl of urn:uuid:res-N so a section entry referencing
    one actually resolves, which is what separates a populated section from an
    asserted-empty one.
    """
    comp = {"resourceType": "Composition", "status": "final",
            "section": [dict(s) for s in sections]}
    entries = [{"resource": comp},
               {"resource": {"resourceType": "Patient", "id": "p1"}}]
    for i in range(resources):
        entries.append({
            "fullUrl": f"urn:uuid:res-{i}",
            "resource": {"resourceType": "Observation", "id": f"o{i}",
                         "code": {"text": f"observation {i}"}},
        })
    return json.dumps({"resourceType": "Bundle", "type": "document",
                       "entry": entries})


def outcome(errors=0, warnings=0, infos=0, fatals=0):
    issues = (
        [{"severity": "fatal", "code": "invalid", "diagnostics": f"fatal {i}"} for i in range(fatals)]
        + [{"severity": "error", "code": "invalid", "diagnostics": f"error {i}"} for i in range(errors)]
        + [{"severity": "warning", "code": "invalid", "diagnostics": f"warn {i}"} for i in range(warnings)]
        + [{"severity": "information", "code": "informational", "diagnostics": f"info {i}"} for i in range(infos)]
    )
    if not issues:
        issues = [{"severity": "information", "code": "informational",
                   "diagnostics": "All OK"}]
    return {"resourceType": "OperationOutcome", "issue": issues}


@pytest.fixture(autouse=True)
def _pin_env(monkeypatch):
    """The suite asserts literal limits, open registration and the default IG
    list; a shell that exports REGISTER_CODE, MAX_BODY_BYTES or VALIDATION_IGS
    for a local run must not turn it red."""
    monkeypatch.setattr(app_module, "REGISTER_CODE", "")
    monkeypatch.setattr(app_module, "VALIDATION_IGS", ["hl7se.fhir.base#1.1.0"])
    monkeypatch.setattr(app_module, "MAX_BODY_BYTES", 5 * 1024 * 1024)
    monkeypatch.setattr(app_module, "MAX_JSON_BODY_BYTES", 64 * 1024)
    monkeypatch.setattr(app_module, "SAMPLES_DIR",
                        os.path.join(os.path.dirname(app_module.__file__), "samples"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    with TestClient(app_module.app) as c:
        c.data_dir = str(tmp_path)
        yield c


def set_outcome(monkeypatch, oc):
    monkeypatch.setattr(app_module, "validate_with_matchbox", lambda body: oc)


def register(client, name):
    res = client.post("/api/register", json={"team": name})
    assert res.status_code == 200, res.text
    return res.json()["token"]


def submit(client, token, oc, monkeypatch, body=BUNDLE):
    set_outcome(monkeypatch, oc)
    return client.post("/api/eps", content=body,
                       headers={"X-Team-Token": token,
                                "Content-Type": "application/fhir+json"})


def consume_first_pass(client, monkeypatch):
    """Burn the global first pass bonus so scoring asserts stay clean."""
    tok = register(client, "First Pass Sink")
    res = submit(client, tok, outcome(), monkeypatch)
    assert res.headers["X-Score"] == "70"  # 50 + 20, sanity


# ---------------------------------------------------------------- register

def test_register_returns_hex_token(client):
    res = client.post("/api/register", json={"team": "  Alpha Team  "})
    assert res.status_code == 200
    data = res.json()
    assert data["team"] == "Alpha Team"  # trimmed
    assert len(data["token"]) == 16
    int(data["token"], 16)  # valid hex


def test_register_duplicate_name_409(client):
    register(client, "Alpha")
    res = client.post("/api/register", json={"team": "Alpha"})
    assert res.status_code == 409
    # trimmed and case-insensitive duplicates are rejected too
    assert client.post("/api/register", json={"team": " alpha "}).status_code == 409


def test_register_name_length_validation(client):
    assert client.post("/api/register", json={"team": "x"}).status_code == 400
    assert client.post("/api/register", json={"team": "y" * 41}).status_code == 400
    assert client.post("/api/register", json={"team": 42}).status_code == 400


# ---------------------------------------------------------------- submit auth

def test_submit_without_token_401(client, monkeypatch):
    # a real team exists, so this pins the credential check rather than an
    # empty table
    token = register(client, "Alpha")
    assert client.post("/api/eps", content=BUNDLE).status_code == 401
    assert submit(client, token, outcome(), monkeypatch).status_code == 200


def test_submit_bad_token_401(client, monkeypatch):
    token = register(client, "Alpha")
    res = client.post("/api/eps", content=BUNDLE,
                      headers={"X-Team-Token": "deadbeefdeadbeef"})
    assert res.status_code == 401
    # not a prefix or suffix match either
    for wrong in (token[:-1], token + "0", token.upper() if token.islower() else token):
        if wrong == token:
            continue
        assert client.post("/api/eps", content=BUNDLE,
                           headers={"X-Team-Token": wrong}).status_code == 401
    assert submit(client, token, outcome(), monkeypatch).status_code == 200


def test_submit_bearer_token_accepted(client, monkeypatch):
    token = register(client, "Alpha")
    set_outcome(monkeypatch, outcome())
    res = client.post("/api/eps", content=BUNDLE,
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200


def test_submit_invalid_json_400(client):
    token = register(client, "Alpha")
    res = client.post("/api/eps", content="not json {",
                      headers={"X-Team-Token": token})
    assert res.status_code == 400


def test_submit_oversized_body_413(client, monkeypatch):
    token = register(client, "Alpha")
    monkeypatch.setattr(app_module, "MAX_BODY_BYTES", 1024)
    # Rejected before validation runs: a Matchbox call here would raise.
    monkeypatch.setattr(app_module, "validate_with_matchbox",
                        lambda body: (_ for _ in ()).throw(AssertionError("validated oversized body")))
    res = client.post("/api/eps", content=b"x" * 2048,
                      headers={"X-Team-Token": token})
    assert res.status_code == 413
    # Nothing was written to the audit log for the rejected submission.
    team_dir = os.path.join(str(app_module.DATA_DIR), "submissions")
    written = []
    for root, _dirs, files in os.walk(team_dir):
        written.extend(files)
    assert written == []


# ---------------------------------------------------------------- scoring

def test_empty_but_valid_scores_exactly_50(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    body = bundle(sections=0, empty_sections=5, resources=0)
    res = submit(client, token, outcome(warnings=1, infos=2), monkeypatch, body=body)
    assert res.status_code == 200
    assert res.headers["X-Pass"] == "true"
    assert res.headers["X-Errors"] == "0"
    assert res.headers["X-Sections"] == "0"   # emptyReason sections don't count
    assert res.headers["X-Resources"] == "0"  # Composition/Patient excluded
    assert res.headers["X-Score"] == "50"
    assert res.headers["content-type"].startswith("application/fhir+json")
    assert res.json()["resourceType"] == "OperationOutcome"


def test_rich_valid_bundle_scoring(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    body = bundle(sections=3, empty_sections=2, resources=4)
    res = submit(client, token, outcome(), monkeypatch, body=body)
    assert res.headers["X-Pass"] == "true"
    assert res.headers["X-Sections"] == "3"
    assert res.headers["X-Resources"] == "4"
    assert res.headers["X-Score"] == "88"  # 50 + 3*10 + 4*2


def test_resource_points_capped_at_60(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    body = bundle(sections=0, resources=40)  # 40*2 = 80 -> capped to 60
    res = submit(client, token, outcome(), monkeypatch, body=body)
    assert res.headers["X-Resources"] == "40"
    assert res.headers["X-Score"] == "110"  # 50 + min(60, 80)


def test_invalid_with_lots_of_data_scores_zero(client, monkeypatch):
    token = register(client, "Alpha")
    body = bundle(sections=5, resources=20)
    res = submit(client, token, outcome(errors=2), monkeypatch, body=body)
    assert res.headers["X-Pass"] == "false"
    assert res.headers["X-Errors"] == "2"
    assert res.headers["X-Score"] == "0"
    # richness of the submission is still reported
    assert res.headers["X-Sections"] == "5"
    assert res.headers["X-Resources"] == "20"


def test_fatal_gates_score_to_zero(client, monkeypatch):
    token = register(client, "Alpha")
    res = submit(client, token, outcome(errors=2, fatals=1), monkeypatch)
    assert res.headers["X-Pass"] == "false"
    assert res.headers["X-Errors"] == "3"
    assert res.headers["X-Score"] == "0"


def test_warnings_do_not_gate(client, monkeypatch):
    token = register(client, "Alpha")
    res = submit(client, token, outcome(warnings=5, infos=3), monkeypatch)
    assert res.headers["X-Pass"] == "true"


def test_matchbox_down_502_and_attempt_not_counted(client, monkeypatch):
    token = register(client, "Alpha")

    def boom(body):
        raise app_module.MatchboxError("connection refused")

    monkeypatch.setattr(app_module, "validate_with_matchbox", boom)
    res = client.post("/api/eps", content=BUNDLE,
                      headers={"X-Team-Token": token})
    assert res.status_code == 502
    lb = client.get("/api/leaderboard").json()
    assert lb[0]["attempts"] == 0


def test_submission_audit_file_written(client, monkeypatch):
    token = register(client, "Alpha Team")
    submit(client, token, outcome(), monkeypatch)
    team_dir = os.path.join(client.data_dir, "submissions", "alpha-team")
    files = os.listdir(team_dir)
    assert len(files) == 1
    with open(os.path.join(team_dir, files[0])) as fh:
        assert json.load(fh)["resourceType"] == "Bundle"


# ---------------------------------------------------------------- first pass

def test_first_pass_only_first_valid_team_only_once(client, monkeypatch):
    tok_a = register(client, "Alpha")
    tok_b = register(client, "Beta")

    # Beta submits first but invalid: no first pass for invalid, score 0
    res = submit(client, tok_b, outcome(errors=3), monkeypatch,
                 body=bundle(sections=4, resources=10))
    assert res.headers["X-Score"] == "0"
    # Alpha goes valid first: 50 + 20 first pass
    res_a = submit(client, tok_a, outcome(), monkeypatch)
    assert res_a.headers["X-Score"] == "70"
    # Beta goes valid later: no bonus
    res_b = submit(client, tok_b, outcome(), monkeypatch)
    assert res_b.headers["X-Score"] == "50"
    # Alpha valid again: bonus not re-awarded on the wire
    res_a2 = submit(client, tok_a, outcome(), monkeypatch)
    assert res_a2.headers["X-Score"] == "50"

    lb = {r["team"]: r for r in client.get("/api/leaderboard").json()}
    assert lb["Alpha"]["first_pass"] is True
    assert lb["Alpha"]["score"] == 70   # max(50, 50) + 20, not stacked
    assert lb["Beta"]["first_pass"] is False
    assert lb["Beta"]["score"] == 50


# ---------------------------------------------------------------- leaderboard

def test_team_score_is_max_across_submissions(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    submit(client, token, outcome(), monkeypatch,
           body=bundle(sections=3, resources=4))                    # 88
    submit(client, token, outcome(), monkeypatch,
           body=bundle(sections=1, resources=1))                    # 62
    submit(client, token, outcome(errors=4), monkeypatch,
           body=bundle(sections=5, resources=30))                   # 0

    lb = {r["team"]: r for r in client.get("/api/leaderboard").json()}
    alpha = lb["Alpha"]
    assert alpha["score"] == 88          # max, not latest and not sum
    assert alpha["attempts"] == 3
    assert alpha["passed"] is True
    assert alpha["best_sections"] == 3   # from the highest-scoring submission
    assert alpha["best_resources"] == 4
    assert alpha["best_errors"] == 0     # fewest errors across submissions


def test_best_errors_is_fewest_for_failing_team(client, monkeypatch):
    token = register(client, "Alpha")
    submit(client, token, outcome(errors=7), monkeypatch)
    submit(client, token, outcome(errors=3), monkeypatch)
    lb = client.get("/api/leaderboard").json()
    assert lb[0]["best_errors"] == 3
    assert lb[0]["score"] == 0
    assert lb[0]["passed"] is False


def test_leaderboard_ordering_and_fields(client, monkeypatch):
    tok_a = register(client, "Alpha")   # valid rich + first pass -> top
    tok_b = register(client, "Beta")    # reaches 50 first -> above Gamma
    tok_c = register(client, "Gamma")   # reaches 50 later
    register(client, "Idle")            # no submissions

    submit(client, tok_a, outcome(), monkeypatch,
           body=bundle(sections=2, resources=3))            # 76 + 20 first pass
    submit(client, tok_b, outcome(errors=1), monkeypatch)   # 0
    submit(client, tok_b, outcome(), monkeypatch)           # 50
    submit(client, tok_c, outcome(), monkeypatch)           # 50

    lb = client.get("/api/leaderboard").json()
    names = [r["team"] for r in lb]
    assert names.index("Alpha") == 0
    # same score (50): whoever reached it first wins (Beta's valid submission
    # landed before Gamma's; Beta's extra failed attempt must not hurt)
    assert names.index("Beta") < names.index("Gamma")
    assert [r["rank"] for r in lb] == [1, 2, 3, 4]

    alpha = lb[0]
    assert alpha["score"] == 96          # 50 + 2*10 + 3*2 + 20 first pass
    assert alpha["passed"] is True
    assert alpha["first_pass"] is True
    assert alpha["best_sections"] == 2
    assert alpha["best_resources"] == 3
    assert alpha["last_submission"]

    idle = next(r for r in lb if r["team"] == "Idle")
    assert idle["score"] == 0
    assert idle["attempts"] == 0
    assert idle["best_errors"] is None
    assert idle["best_sections"] is None
    assert idle["best_resources"] is None
    assert idle["last_submission"] is None


def test_tiebreak_first_to_reach_best_score_resubmitting_harmless(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    tok_a = register(client, "Early")
    tok_b = register(client, "Late")
    submit(client, tok_a, outcome(), monkeypatch)          # 50, reached first
    submit(client, tok_b, outcome(), monkeypatch)          # 50, reached later
    # Early tries again afterwards (worse result, more attempts, later
    # last_submission) - none of that may hurt its rank.
    submit(client, tok_a, outcome(errors=3), monkeypatch)  # 0
    lb = client.get("/api/leaderboard").json()
    names = [r["team"] for r in lb]
    assert names.index("Early") < names.index("Late")


def test_tiebreak_alphabetical_for_identical_teams(client):
    register(client, "Zeta Squad")   # registered first
    register(client, "Anna Squad")
    lb = client.get("/api/leaderboard").json()
    names = [r["team"] for r in lb]
    # both idle (score 0, no submissions): name decides, registration
    # order does not
    assert names.index("Anna Squad") < names.index("Zeta Squad")


# ---------------------------------------------------------------- richness unit

def test_count_bundle_richness_edge_cases():
    count = app_module.count_bundle_richness
    assert count(None) == (0, 0)
    assert count([]) == (0, 0)
    assert count({"resourceType": "Bundle"}) == (0, 0)
    assert count({"resourceType": "Bundle", "entry": "nope"}) == (0, 0)
    assert count({
        "entry": [
            {"resource": {"resourceType": "Composition",
                          "section": [
                              {"entry": []},                        # empty list
                              {"emptyReason": {"text": "none"}},    # no entries
                              {"entry": [{"reference": "Condition/c1"}]},  # counts
                              {"title": "by fullUrl",              # counts
                               "entry": [{"reference": "urn:uuid:ms-1"}]},
                              {"title": "by urn from id",          # counts
                               "entry": [{"reference": "urn:uuid:c1#frag"}]},
                              {"title": "outside the bundle",      # ignored
                               "entry": [{"reference": "Condition/elsewhere"}]},
                              {"title": "display only",            # ignored
                               "entry": [{"display": "see attached"}]},
                              {"title": "points at the patient",   # ignored
                               "entry": [{"reference": "Patient/p1"}]},
                              {"title": "not a reference",         # ignored
                               "entry": ["Condition/c1", 7, None]},
                              "garbage",                            # ignored
                          ]}},
            {"resource": {"resourceType": "Patient", "id": "p1"}},
            {"resource": {"resourceType": "Condition", "id": "c1"}},
            {"fullUrl": "urn:uuid:ms-1",
             "resource": {"resourceType": "MedicationStatement"}},
            {"request": {"method": "POST"}},                        # no resource
        ]
    }) == (3, 2)


def test_section_pointing_at_nothing_in_the_bundle_does_not_score(client, monkeypatch):
    """entry[] with a dangling or display-only reference is padding, not content."""
    consume_first_pass(client, monkeypatch)
    token = register(client, "Padder")
    sections = [
        {"title": "Allergies", "entry": [{"reference": "urn:uuid:res-0"}]},  # resolves
        {"title": "Problems", "entry": [{"reference": "urn:uuid:nowhere"}]},
        {"title": "Medications", "entry": [{"display": "see attached PDF"}]},
        {"title": "Procedures", "entry": [{"reference": "Patient/p1"}]},
    ]
    body = bundle_with_section(*sections, resources=1)
    res = submit(client, token, outcome(), monkeypatch, body=body)
    assert res.headers["X-Sections"] == "1"
    assert res.headers["X-Resources"] == "1"
    assert res.headers["X-Score"] == "62"


# ---------------------------------------------------------------- clinician summary

CLINICAL_BUNDLE = {
    "resourceType": "Bundle",
    "type": "document",
    "entry": [
        {"fullUrl": "urn:uuid:comp-1",
         "resource": {"resourceType": "Composition", "status": "final",
                      "title": "Patient Summary", "date": "2026-01-01",
                      "section": [
                          {"title": "Medication list",
                           "entry": [{"reference": "urn:uuid:med-stmt-1"},
                                     {"reference": "MedicationStatement/ms2"},
                                     {"reference": "urn:uuid:not-in-bundle"}]},
                          {"code": {"coding": [{"display": "Allergies"}]},
                           "entry": [{"reference": "urn:uuid:allergy-1"}]},
                          {"title": "Procedures",
                           "emptyReason": {"coding": [{"display": "Unavailable"}]}},
                      ]}},
        {"fullUrl": "urn:uuid:patient-1",
         "resource": {"resourceType": "Patient", "id": "pat1",
                      "name": [{"family": "Schwartz", "given": ["Petra", "M."]}],
                      "birthDate": "1950-04-19", "gender": "female",
                      "identifier": [{"system": "http://example.org/ids",
                                      "value": "1234"}]}},
        {"fullUrl": "urn:uuid:med-stmt-1",
         "resource": {"resourceType": "MedicationStatement", "id": "ms1",
                      "status": "active",
                      "medicationReference": {"reference": "#med-contained"},
                      "contained": [{"resourceType": "Medication",
                                     "id": "med-contained",
                                     "code": {"text": "Simvastatin 10 mg"}}],
                      "dosage": [{"text": "1 tablet daily"}]}},
        {"fullUrl": "http://example.org/fhir/MedicationStatement/ms2",
         "resource": {"resourceType": "MedicationStatement", "id": "ms2",
                      "status": "active",
                      "medicationReference": {"reference": "urn:uuid:med-1"}}},
        {"fullUrl": "urn:uuid:med-1",
         "resource": {"resourceType": "Medication", "id": "med1",
                      "code": {"coding": [{"display": "Metformin 500 mg"}]}}},
        {"fullUrl": "urn:uuid:allergy-1",
         "resource": {"resourceType": "AllergyIntolerance", "id": "al1",
                      "code": {"coding": [{"display": "Penicillin"}]},
                      "criticality": "high",
                      "clinicalStatus": {"coding": [{"code": "active"}]},
                      "reaction": [{"manifestation": [{"text": "Hives"}]}]}},
        {"fullUrl": "urn:uuid:orphan-1",
         "resource": {"resourceType": "Observation", "id": "orph1",
                      "status": "final", "code": {"text": "Body weight"},
                      "valueQuantity": {"value": 72, "unit": "kg"},
                      "effectiveDateTime": "2026-01-01"}},
    ],
}
CLINICAL_BODY = json.dumps(CLINICAL_BUNDLE)
# CLINICAL_BUNDLE richness: 2 populated sections, 5 clinical resources
# (ms1, ms2, med1, allergy, orphan observation) -> valid score 50+20+10 = 80.


def test_summarize_bundle_patient_and_composition():
    s = app_module.summarize_bundle(CLINICAL_BUNDLE)
    assert s["patient"]["name"] == "Petra M. Schwartz"
    assert s["patient"]["birth_date"] == "1950-04-19"
    assert s["patient"]["gender"] == "female"
    assert s["patient"]["identifiers"] == [
        {"system": "http://example.org/ids", "value": "1234"}]
    assert s["composition"] == {"title": "Patient Summary", "date": "2026-01-01"}


def test_summarize_bundle_section_resolution_and_medication_names():
    s = app_module.summarize_bundle(CLINICAL_BUNDLE)
    meds = s["sections"][0]
    assert meds["title"] == "Medication list"
    # urn:uuid ref, Type/id ref (resolved via fullUrl suffix / resource id),
    # and an unresolvable ref that is silently skipped
    labels = [i["label"] for i in meds["items"]]
    assert labels == ["Simvastatin 10 mg", "Metformin 500 mg"]
    assert meds["items"][0]["detail"] == "1 tablet daily"   # dosage text
    assert meds["items"][0]["status"] == "active"
    allergies = s["sections"][1]
    assert allergies["title"] == "Allergies"                # code display fallback
    item = allergies["items"][0]
    assert item["label"] == "Penicillin"
    assert "criticality high" in item["detail"]
    assert "Hives" in item["detail"]
    assert item["status"] == "active"


def test_summarize_bundle_empty_reason_and_unreferenced():
    s = app_module.summarize_bundle(CLINICAL_BUNDLE)
    empty = s["sections"][2]
    assert empty["title"] == "Procedures"
    assert empty["items"] == []
    assert empty["empty_reason"] == "Unavailable"
    # the orphan Observation is the only resource no section (or referenced
    # resource, e.g. medicationReference) points at
    assert len(s["unreferenced"]) == 1
    orphan = s["unreferenced"][0]
    assert orphan["label"] == "Body weight"
    assert "72 kg" in orphan["detail"]
    assert "2026-01-01" in orphan["detail"]


def test_item_annotation_category_date_and_coding():
    s = app_module.summarize_bundle(CLINICAL_BUNDLE)
    simvastatin = s["sections"][0]["items"][0]
    assert simvastatin["category"] == "medication"
    assert simvastatin["flags"] == ["active"]
    assert simvastatin["code"] is None          # contained Medication, text only
    assert "not coded" in simvastatin["gaps"]
    allergy = s["sections"][1]["items"][0]
    assert allergy["category"] == "allergy"
    assert allergy["flags"] == ["high risk"]    # criticality high
    assert "no reaction recorded" not in allergy["gaps"]
    orphan = s["unreferenced"][0]
    assert orphan["category"] == "result"
    assert orphan["date"] == "2026-01-01"
    assert orphan["gaps"] == ["not coded"]      # code.text only, no coding


def test_item_gaps_flag_missing_dose_and_date():
    bundle = {"resourceType": "Bundle", "entry": [
        {"fullUrl": "urn:uuid:c",
         "resource": {"resourceType": "Composition", "title": "T",
                      "section": [{"title": "Meds",
                                   "entry": [{"reference": "urn:uuid:m"}]},
                                  {"title": "Problems",
                                   "entry": [{"reference": "urn:uuid:p"}]}]}},
        {"fullUrl": "urn:uuid:m",
         "resource": {"resourceType": "MedicationStatement", "status": "active",
                      "medicationCodeableConcept": {
                          "coding": [{"system": "http://www.whocc.no/atc",
                                      "code": "C10AA01"}],
                          "text": "Simvastatin"}}},
        {"fullUrl": "urn:uuid:p",
         "resource": {"resourceType": "Condition",
                      "code": {"text": "Chest pain"}}},
    ]}
    s = app_module.summarize_bundle(bundle)
    med = s["sections"][0]["items"][0]
    assert med["code_system"] == "ATC" and med["code"] == "C10AA01"
    assert med["gaps"] == ["no date", "no dose"]
    problem = s["sections"][1]["items"][0]
    assert problem["gaps"] == ["not coded", "no date"]


def test_observation_abnormal_flag_and_missing_value():
    bundle = {"resourceType": "Bundle", "entry": [
        {"fullUrl": "urn:uuid:c",
         "resource": {"resourceType": "Composition", "title": "T",
                      "section": [{"title": "Results",
                                   "entry": [{"reference": "urn:uuid:o1"},
                                             {"reference": "urn:uuid:o2"}]}]}},
        {"fullUrl": "urn:uuid:o1",
         "resource": {"resourceType": "Observation", "status": "final",
                      "code": {"coding": [{"system": "http://loinc.org",
                                           "code": "718-7"}], "text": "Hb"},
                      "effectiveDateTime": "2025-01-02",
                      "valueQuantity": {"value": 9, "unit": "g/dL"},
                      "interpretation": [{"coding": [{"code": "L"}]}]}},
        {"fullUrl": "urn:uuid:o2",
         "resource": {"resourceType": "Observation", "status": "final",
                      "code": {"text": "Mood"}}},
    ]}
    s = app_module.summarize_bundle(bundle)
    hb, mood = s["sections"][0]["items"]
    assert hb["flags"] == ["abnormal"] and hb["code_system"] == "LOINC"
    assert hb["gaps"] == []
    assert set(mood["gaps"]) == {"not coded", "no date", "no value"}


def test_summary_quality_block():
    q = app_module.summarize_bundle(CLINICAL_BUNDLE)["quality"]
    assert q["items"] == 4                  # 2 meds + 1 allergy + 1 orphan
    assert q["sections_populated"] == 2 and q["sections_empty"] == 1
    assert q["categories"] == {"medication": 2, "allergy": 1, "result": 1}
    assert q["earliest"] == "2026-01-01" and q["latest"] == "2026-01-01"
    assert q["gaps"]["no dose"] == 1         # ms2 has no dosage
    # gap counts are ordered by frequency, so the UI can show the worst first
    assert list(q["gaps"].values()) == sorted(q["gaps"].values(), reverse=True)


def test_section_kind_from_loinc_code():
    kind = app_module._section_kind
    assert kind({"code": {"coding": [{"system": "http://loinc.org",
                                      "code": "48765-2"}]}}, "Allergien") == "allergy"
    assert kind({"code": {"coding": [{"code": "10160-0"}]}}, "Medikationsliste") \
        == "medication"
    assert kind({}, "Untitled section") == "other"


def test_section_kind_falls_back_to_title_in_any_language():
    # EPS documents are written in the local language, so an uncoded German
    # section still has to reach the right clinician view.
    kind = app_module._section_kind
    assert kind({}, "Medikationsliste") == "medication"
    assert kind({}, "Allergien und Intoleranzen") == "allergy"
    assert kind({}, "Gesundheitsprobleme und Risiken") == "problem"
    assert kind({}, "Implantate, medizinische Ger\u00e4te") == "device"


def test_sections_expose_kind():
    kinds = [s["kind"] for s in
             app_module.summarize_bundle(CLINICAL_BUNDLE)["sections"]]
    assert "medication" in kinds and "allergy" in kinds


def test_summary_quality_empty_for_malformed():
    q = app_module.summarize_bundle(None)["quality"]
    assert q["items"] == 0 and q["gaps"] == {} and q["earliest"] is None


def test_summarize_bundle_malformed_never_raises():
    sb = app_module.summarize_bundle
    for garbage in (
        None, [], "x", 42, {"entry": "nope"}, {"resourceType": "Bundle"},
        {"entry": [None, {}, {"resource": "str"}, {"resource": {}},
                   {"resource": {"resourceType": "Composition",
                                 "section": ["garbage",
                                             {"entry": [None, {"reference": 5}]},
                                             {"title": 7, "entry": {}}]}},
                   {"resource": {"resourceType": "Patient", "name": "bad",
                                 "identifier": [None, {"value": 3}]}}]},
    ):
        result = sb(garbage)
        assert isinstance(result, dict)
        assert set(result) == {"patient", "composition", "sections",
                               "unreferenced", "quality"}


# ---------------------------------------------------------------- submissions API

def test_submissions_list_filter_and_limit(client, monkeypatch):
    tok_a = register(client, "Alpha")
    tok_b = register(client, "Beta")
    submit(client, tok_a, outcome(), monkeypatch, body=CLINICAL_BODY)
    submit(client, tok_b, outcome(errors=1), monkeypatch)
    subs = client.get("/api/submissions").json()
    assert len(subs) == 2
    assert subs[0]["team"] == "Beta"    # newest first
    assert subs[0]["pass"] is False
    assert subs[1]["team"] == "Alpha"
    assert {"id", "team", "team_slug", "ts", "pass", "score", "errors",
            "warnings", "sections", "resources"} <= set(subs[0])
    only_a = client.get("/api/submissions", params={"team": "alpha"}).json()
    assert [s["team"] for s in only_a] == ["Alpha"]
    limited = client.get("/api/submissions", params={"limit": 1}).json()
    assert [s["team"] for s in limited] == ["Beta"]


def test_submission_detail_and_raw_bundle(client, monkeypatch):
    token = register(client, "Alpha")
    submit(client, token, outcome(), monkeypatch, body=CLINICAL_BODY)
    sub_id = client.get("/api/submissions").json()[0]["id"]
    detail = client.get(f"/api/submissions/{sub_id}").json()
    assert detail["id"] == sub_id
    assert detail["team"] == "Alpha"
    assert detail["pass"] is True
    assert detail["summary"]["patient"]["name"] == "Petra M. Schwartz"
    assert detail["summary"]["sections"][0]["items"]
    raw = client.get(f"/api/submissions/{sub_id}/bundle")
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("application/fhir+json")
    assert raw.json()["resourceType"] == "Bundle"


def test_submission_404s(client, monkeypatch):
    assert client.get("/api/submissions/999").status_code == 404
    assert client.get("/api/submissions/999/bundle").status_code == 404
    token = register(client, "Alpha")
    submit(client, token, outcome(), monkeypatch)
    sub_id = client.get("/api/submissions").json()[0]["id"]
    team_dir = os.path.join(client.data_dir, "submissions", "alpha")
    for name in os.listdir(team_dir):
        os.remove(os.path.join(team_dir, name))
    assert client.get(f"/api/submissions/{sub_id}").status_code == 404
    assert client.get(f"/api/submissions/{sub_id}/bundle").status_code == 404


# ---------------------------------------------------------------- interop

def interop_setup(client, monkeypatch):
    """Producer with a valid rich doc + a consumer team; returns both tokens."""
    tok_p = register(client, "Producer")
    tok_c = register(client, "Consumer")
    submit(client, tok_p, outcome(), monkeypatch, body=CLINICAL_BODY)
    return tok_p, tok_c


def test_the_exchange_corpus_needs_no_token_at_all(client):
    """Interop testing is unscored, so there is nobody to authenticate."""
    assert client.get("/api/exchange").status_code == 200
    assert client.get("/api/exchange/anyone").status_code == 404  # unknown team
    # a token is neither required nor rejected - it is simply not read
    register(client, "Alpha")
    for headers in ({"X-Team-Token": "not-a-real-token"}, {}):
        assert client.get("/api/exchange", headers=headers).status_code == 200


def test_every_reader_sees_every_published_document(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    submit(client, tok_c, outcome(), monkeypatch, body=CLINICAL_BODY)
    # Nothing is hidden from anyone, including a company's own document: with
    # no points at stake there is no reason to tailor the list per caller.
    listing = client.get("/api/exchange").json()
    assert [p["team"] for p in listing] == ["Consumer", "Producer"]


def test_anyone_can_download_a_published_bundle(client, monkeypatch):
    interop_setup(client, monkeypatch)
    res = client.get("/api/exchange/producer")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/fhir+json")
    assert res.headers["X-Producer"] == "producer"
    assert json.loads(res.content)["resourceType"] == "Bundle"


def test_bundles_download_under_a_named_file(client, monkeypatch):
    """A browser click must not save every document as a nameless "bundle"."""
    tok_p, _ = interop_setup(client, monkeypatch)
    cases = {
        "/api/exchange/producer": 'attachment; filename="producer-eps.json"',
        "/api/submissions/1/bundle": 'attachment; filename="producer-1-eps.json"',
        "/api/samples/andrea-cross-border/bundle":
            'attachment; filename="andrea-cross-border-example-eps.json"',
    }
    for url, disposition in cases.items():
        res = client.get(url)
        assert res.status_code == 200, url
        assert res.headers["content-disposition"] == disposition, url


def test_exchange_lists_valid_docs_only(client, monkeypatch):
    interop_setup(client, monkeypatch)
    tok_f = register(client, "Failing")
    submit(client, tok_f, outcome(errors=2), monkeypatch)  # invalid -> unpublished
    listing = client.get("/api/exchange").json()
    assert [p["team"] for p in listing] == ["Producer"]    # no Failing
    partner = listing[0]
    assert partner["team_slug"] == "producer"
    assert isinstance(partner["exchange_id"], int)
    assert partner["sections"] == 2 and partner["resources"] == 5


def test_exchange_fetch_bundle_headers_and_404s(client, monkeypatch):
    interop_setup(client, monkeypatch)
    res = client.get("/api/exchange/producer")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/fhir+json")
    assert res.headers["X-Producer"] == "producer"
    assert int(res.headers["X-Exchange-Id"]) > 0
    assert res.json()["resourceType"] == "Bundle"
    assert client.get("/api/exchange/ghost").status_code == 404
    # registered, but nothing valid to publish
    assert client.get("/api/exchange/consumer").status_code == 404


def test_the_receipt_endpoints_are_gone(client, monkeypatch):
    """Interop is untracked, so nothing claims a consumption or records one."""
    tok_p, tok_c = interop_setup(client, monkeypatch)
    receipt = {"partner": "producer", "patient_family": "Schwartz",
               "birth_date": "1950-04-19", "medications": 2, "allergies": 1}
    gone = client.post("/api/interop/confirm", json=receipt,
                       headers={"X-Team-Token": tok_c})
    assert gone.status_code == 404
    assert client.get("/api/interop/matrix").status_code == 404


def test_the_leaderboard_does_not_score_interop(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    tok_p = register(client, "Producer2")
    register(client, "Consumer2")            # registered, never submits
    submit(client, tok_p, outcome(), monkeypatch, body=CLINICAL_BODY)
    lb = {r["team"]: r for r in client.get("/api/leaderboard").json()}
    assert "interop" not in lb["Producer2"]
    assert lb["Producer2"]["score"] == 80    # the submission, and nothing else
    # a company with no submission has no other way to earn points
    assert lb["Consumer2"]["score"] == 0
    assert lb["Consumer2"]["passed"] is False


# ---------------------------------------------------------------- misc

def test_index_serves_html(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Nordic Hackathon 2026" in res.text
    assert res.headers["content-type"].startswith("text/html")


def test_index_has_tabs_and_company_copy(client):
    text = client.get("/").text
    for label in ("Leaderboard", "Submit", "Clinical view", "Interop testing"):
        assert label in text
    assert "One registration per company" in text
    assert "Register your company" in text
    assert '<th scope="col">Company</th>' in text
    # interop is unscored, so it has no leaderboard column and no receipt form
    assert '<th scope="col">Interop</th>' not in text
    assert "Interop testing is not scored." in text
    for leftover in ("io-form", "io-result", "interop/confirm", "interop/matrix",
                     "Interop matrix", "io-token"):
        assert leftover not in text, leftover
    # a team is formed between companies at the event, so no copy may equate
    # the two - a company registers once, and teams are built on top of that
    assert "One team per company" not in text


def test_index_offers_dashboard_timeline_and_document_views(client):
    text = client.get("/").text
    for fn in ("function viewTimeline(", "function viewDashboard(",
               "function viewDocument("):
        assert fn in text
    for key, name in (("document", "Document view"), ("dashboard", "Dashboard view"),
                      ("timeline", "Timeline view")):
        assert "%s: { fn: view" % key in text
        assert 'name: "%s"' % name in text
    # side by side is gone; nothing may still reach for it
    for leftover in ("cmpCol", "cmp-grid", "cmp-col", "cmp-wide",
                     'data-mode="compare"'):
        assert leftover not in text
    # the plain document rendering is what opens by default
    assert 'let cvMode = "document";' in text
    # the switcher is rebuilt with each document rather than parked in the
    # picker card, so it has to come from the render path
    assert "function cvModesHtml(" in text
    assert "html += cvModesHtml();" in text


def test_index_navigates_through_the_hash(client):
    text = client.get("/").text
    # tabs are real links, so Back and Forward move between them and a view
    # can be pasted to someone else
    for tab in ("leaderboard", "submit", "interop", "clinical"):
        assert 'href="#%s"' % tab in text
    assert 'window.addEventListener("hashchange"' in text
    # a bare #clinical opens the Arne example rather than an empty picker
    assert 'const DEFAULT_SAMPLE = "arne-complex-care";' in text
    # and a partner's document is linked, not opened by a click handler
    assert "'<a href=\"#clinical/submission/'" in text


def test_index_hides_identifier_assigning_authorities(client):
    """The OID behind a patient identifier is plumbing. The API still carries
    it - machines need it - but no reader is shown it."""
    text = client.get("/").text
    assert "urn:oid" not in text
    assert 'esc(i.system)' not in text
    detail = client.get("/api/samples/arne-complex-care").json()
    systems = [i.get("system") for i in detail["summary"]["patient"]["identifiers"]]
    assert any(systems), "the API should still expose what the UI omits"


def test_samples_link_to_their_scenario_briefs(client):
    """Each worked example names the organisers' brief it was written from,
    rather than restating it in copy that would drift."""
    samples = {s["id"]: s for s in client.get("/api/samples").json()["samples"]}
    assert samples["andrea-cross-border"]["scenario_name"] == "The Andrea Scenario"
    assert samples["arne-complex-care"]["scenario_name"] == "The Arne Scenario"
    for sample in samples.values():
        assert sample["scenario_url"].startswith("https://")


def test_index_lists_examples_as_a_company(client):
    """The examples are one more entry in the company picker, so there is a
    single place to open a document from."""
    text = client.get("/").text
    assert 'const SAMPLE_TEAM = "__samples";' in text
    assert "Example documents</option>" in text
    assert "function renderSampleList(" in text


def test_index_offers_bundle_downloads_and_anonymous_partner_browsing(client):
    text = client.get("/").text
    assert '/bundle" download>download the FHIR bundle</a>' in text


def test_index_credits_matchbox(client):
    text = client.get("/").text
    assert "https://github.com/ahdis/matchbox" in text
    assert "http://hl7.eu/fhir/eps/" in text


# ------------------------------------------------------------- sample documents


def test_sample_list_is_labelled_synthetic(client):
    data = client.get("/api/samples").json()
    assert "does not come from any real system" in data["disclaimer"]
    ids = {s["id"] for s in data["samples"]}
    assert ids == {"andrea-cross-border", "arne-complex-care"}
    assert all(s["available"] for s in data["samples"])
    assert all(s["name"] and s["tagline"] and s["scenario"] for s in data["samples"])


def test_sample_detail_matches_submission_shape(client):
    data = client.get("/api/samples/andrea-cross-border").json()
    # the clinician renderer consumes samples and submissions through one path
    assert set(data) >= {"team", "pass", "summary", "sample"}
    assert data["pass"] is True
    assert "does not come from any real system" in data["sample"]["disclaimer"]
    summary = data["summary"]
    assert summary["patient"]["name"] == "Andrea Lindqvist"
    kinds = {s["kind"] for s in summary["sections"]}
    assert {"allergy", "problem", "medication"} <= kinds


def test_sample_asserts_nil_known_allergies_rather_than_omitting_them(client):
    data = client.get("/api/samples/andrea-cross-border").json()
    allergies = next(s for s in data["summary"]["sections"] if s["kind"] == "allergy")
    assert allergies["items"] == []
    assert allergies["empty_reason"]


def test_swedish_sample_is_classified_by_loinc_not_title(client):
    data = client.get("/api/samples/arne-complex-care").json()
    sections = {s["kind"]: s for s in data["summary"]["sections"]}
    assert sections["allergy"]["title"] == "Allergier och intoleranser"
    assert sections["medication"]["title"] == "Läkemedelslista"
    assert len(sections["problem"]["items"]) == 10
    labels = " ".join(i["label"] for i in sections["allergy"]["items"])
    assert "Penicillin" in labels


def test_sample_bundle_downloads_as_fhir(client):
    res = client.get("/api/samples/arne-complex-care/bundle")
    assert res.headers["content-type"].startswith("application/fhir+json")
    doc = res.json()
    assert doc["type"] == "document"
    assert doc["meta"]["profile"] == [
        "http://hl7.eu/fhir/eps/StructureDefinition/bundle-eu-eps"
    ]


def test_unknown_sample_is_404(client):
    assert client.get("/api/samples/nope").status_code == 404
    assert client.get("/api/samples/nope/bundle").status_code == 404


def test_register_returns_company_note(client):
    data = client.post("/api/register", json={"team": "NoteCo"}).json()
    assert "note" in data
    assert "company" in data["note"].lower()


# ------------------------------------------------- asserted-empty sections

def test_empty_reason_codes_map_to_clinician_labels():
    for code, label in [("nilknown", "None reported"), ("notasked", "Not asked"),
                        ("withheld", "Information withheld"),
                        ("unavailable", "Unavailable"),
                        ("notstarted", "Not started"), ("closed", "Closed")]:
        s = app_module.summarize_bundle(json.loads(bundle_with_section(
            {"title": "S", "emptyReason": {"coding": [{"code": code}]}})))
        assert s["sections"][0]["empty_reason"] == label
    # an unrecognised code is shown as authored rather than dropped
    s = app_module.summarize_bundle(json.loads(bundle_with_section(
        {"title": "S", "emptyReason": {"coding": [{"code": "novel-code"}]}})))
    assert s["sections"][0]["empty_reason"] == "novel-code"


def test_empty_reason_display_text_is_relabelled_too():
    """Real bundles spell the reason out as display text or CodeableConcept
    text - "Nil Known" - and the code is often absent. Keying the table on the
    code alone let that spelling through to the reader untouched."""
    for reason in ({"coding": [{"code": "nilknown", "display": "Nil Known"}]},
                   {"coding": [{"display": "Nil Known"}]},
                   {"text": "Nil known"}):
        s = app_module.summarize_bundle(json.loads(bundle_with_section(
            {"title": "S", "emptyReason": reason})))
        assert s["sections"][0]["empty_reason"] == "None reported", reason


def test_empty_reason_is_only_reported_when_the_section_is_actually_empty():
    """A populated section must not advertise an emptyReason it also carries.

    "Nil known" is a positive clinical claim. Repeating it next to real content
    would tell the reader the opposite of what the document says.
    """
    s = app_module.summarize_bundle(json.loads(bundle_with_section(
        {"title": "S", "entry": [{"reference": "urn:uuid:res-0"}],
         "emptyReason": {"coding": [{"code": "nilknown"}]}}, resources=1)))
    section = s["sections"][0]
    assert len(section["items"]) == 1
    assert section["empty_reason"] is None


def test_asserted_empty_section_is_distinguishable_from_an_absent_one():
    asserted = app_module.summarize_bundle(json.loads(bundle_with_section(
        {"title": "Allergies", "emptyReason": {"coding": [{"code": "nilknown"}]}})))
    assert [x["title"] for x in asserted["sections"]] == ["Allergies"]
    assert asserted["sections"][0]["empty_reason"] == "None reported"
    absent = app_module.summarize_bundle(json.loads(bundle()))
    assert absent["sections"] == []


def test_a_section_with_no_entry_and_no_empty_reason_asserts_nothing():
    s = app_module.summarize_bundle(json.loads(bundle_with_section(
        {"title": "Allergies"})))
    assert s["sections"][0]["items"] == []
    assert s["sections"][0]["empty_reason"] is None


def test_empty_sections_never_earn_points(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    body = bundle_with_section(
        {"title": "Allergies", "emptyReason": {"coding": [{"code": "nilknown"}]}})
    res = submit(client, token, outcome(), monkeypatch, body=body)
    assert res.headers["X-Sections"] == "0"
    assert res.headers["X-Score"] == "50"


# ------------------------------------------------------- section distinctness

def test_repeated_sections_count_once():
    section = {"title": "Allergies", "entry": [{"reference": "urn:uuid:res-0"}]}
    doc = json.loads(bundle_with_section(*[dict(section)] * 8, resources=1))
    assert app_module.count_bundle_richness(doc)[0] == 1


def test_distinct_sections_count_separately():
    doc = json.loads(bundle_with_section(
        {"title": "Allergies", "entry": [{"reference": "urn:uuid:res-0"}]},
        {"title": "Medication", "entry": [{"reference": "urn:uuid:res-0"}]},
        resources=1))
    assert app_module.count_bundle_richness(doc)[0] == 2


def test_section_identity_is_the_code_when_there_is_one():
    coded = lambda code, title: {
        "title": title, "code": {"coding": [{"system": "http://loinc.org",
                                             "code": code}]},
        "entry": [{"reference": "urn:uuid:res-0"}]}
    # same code, different titles -> one section
    doc = json.loads(bundle_with_section(
        coded("48765-2", "Allergies"), coded("48765-2", "Allergier"),
        resources=1))
    assert app_module.count_bundle_richness(doc)[0] == 1
    # different codes, identical titles -> two
    doc = json.loads(bundle_with_section(
        coded("48765-2", "Same"), coded("10160-0", "Same"), resources=1))
    assert app_module.count_bundle_richness(doc)[0] == 2


def test_untitled_uncoded_sections_all_collapse_together():
    anon = {"entry": [{"reference": "urn:uuid:res-0"}]}
    doc = json.loads(bundle_with_section(*[dict(anon)] * 5, resources=1))
    assert app_module.count_bundle_richness(doc)[0] == 1
    # titles differing only in case and whitespace are the same section
    doc = json.loads(bundle_with_section(
        {"title": "Allergies", "entry": [{"reference": "urn:uuid:res-0"}]},
        {"title": "  ALLERGIES  ", "entry": [{"reference": "urn:uuid:res-0"}]},
        resources=1))
    assert app_module.count_bundle_richness(doc)[0] == 1


def test_section_points_are_capped():
    assert app_module.submission_score(True, 16, 0) == 50 + 160
    assert app_module.submission_score(True, 500, 0) == 50 + app_module.SECTION_CAP
    # the cap does not leak into the resource half
    assert app_module.submission_score(True, 500, 500) == (
        50 + app_module.SECTION_CAP + app_module.RESOURCE_CAP)


def test_padding_a_document_cannot_outscore_a_real_one(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    token = register(client, "Padder")
    padded = {"title": "Allergies", "entry": [{"reference": "urn:uuid:res-0"}]}
    body = bundle_with_section(*[dict(padded)] * 200, resources=1)
    res = submit(client, token, outcome(), monkeypatch, body=body)
    assert res.headers["X-Sections"] == "1"
    assert res.headers["X-Score"] == "62"   # 50 + 1*10 + 1*2


# ------------------------------------------------------------- request limits

def test_oversized_bundle_is_refused(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_BODY_BYTES", 500)
    token = register(client, "Alpha")
    res = client.post("/api/eps", content="x" * 2000,
                      headers={"X-Team-Token": token})
    assert res.status_code == 413
    assert "limit is 500 bytes" in res.json()["detail"]


def test_an_unauthenticated_oversized_post_is_rejected_on_the_credential(client,
                                                                        monkeypatch):
    """401 must come before the body is read, or anyone can spend our memory."""
    monkeypatch.setattr(app_module, "MAX_BODY_BYTES", 500)
    register(client, "Alpha")
    assert client.post("/api/eps", content="x" * 2000).status_code == 401


def test_small_endpoints_have_their_own_tighter_limit(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_JSON_BODY_BYTES", 200)
    res = client.post("/api/register", json={"team": "A" * 400})
    assert res.status_code == 413
    # unchanged limit still admits an ordinary registration
    monkeypatch.setattr(app_module, "MAX_JSON_BODY_BYTES", 64 * 1024)
    assert client.post("/api/register", json={"team": "Alpha"}).status_code == 200


def test_deeply_nested_json_is_a_client_error_not_a_crash(client):
    token = register(client, "Alpha")
    nested = "[" * 10000 + "]" * 10000
    res = client.post("/api/eps", content=nested, headers={"X-Team-Token": token})
    assert res.status_code == 400
    assert res.json()["detail"] == "Body is not valid JSON"
    res = client.post("/api/register", content=nested,
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 400


def test_non_object_json_body_is_rejected(client):
    res = client.post("/api/register", content="[1, 2, 3]",
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 400
    assert res.json()["detail"] == "Body must be a JSON object"


# ------------------------------------------------------------ registration code

def test_registration_is_open_by_default(client):
    assert app_module.REGISTER_CODE == ""
    assert client.post("/api/register", json={"team": "Alpha"}).status_code == 200


def test_registration_code_gates_when_set(client, monkeypatch):
    monkeypatch.setattr(app_module, "REGISTER_CODE", "s3cret")
    assert client.post("/api/register", json={"team": "Alpha"}).status_code == 403
    assert client.post("/api/register",
                       json={"team": "Alpha", "code": "wrong"}).status_code == 403
    assert client.post("/api/register",
                       json={"team": "Alpha", "code": 1234}).status_code == 403
    res = client.post("/api/register", json={"team": "Alpha", "code": "s3cret"})
    assert res.status_code == 200


# --------------------------------------------------------- unreadable document

def test_a_document_that_no_longer_parses_says_so(client, monkeypatch):
    token = register(client, "Alpha")
    submit(client, token, outcome(), monkeypatch, body=CLINICAL_BODY)
    sub = client.get("/api/submissions", params={"team": "alpha"}).json()[0]
    assert client.get("/api/submissions/%d" % sub["id"]).json()["bundle_readable"]
    team_dir = os.path.join(client.data_dir, "submissions", "alpha")
    stored = os.path.join(team_dir, os.listdir(team_dir)[0])
    with open(stored, "wb") as fh:
        fh.write(b"{ this is not json")
    data = client.get("/api/submissions/%d" % sub["id"]).json()
    # the recorded verdict stands, but the summary is explicitly not evidence
    assert data["pass"] is True
    assert data["bundle_readable"] is False
    assert data["summary"]["sections"] == []


# ----------------------------------------------------------- matchbox client


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(monkeypatch, result, captured=None):
    def _open(req, timeout=None):
        if captured is not None:
            captured.append((req, timeout))
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(urllib.request, "urlopen", _open)


OK_OUTCOME = json.dumps(outcome(warnings=1)).encode()


def test_validate_posts_the_bundle_to_the_profile_endpoint(monkeypatch):
    captured = []
    fake_urlopen(monkeypatch, FakeResponse(OK_OUTCOME), captured)
    result = app_module.validate_with_matchbox(b'{"resourceType":"Bundle"}')
    assert result["resourceType"] == "OperationOutcome"
    req, timeout = captured[0]
    assert req.full_url.startswith(app_module.MATCHBOX_URL + "/$validate?profile=")
    assert app_module.PROFILE.replace(":", "%3A").replace("/", "%2F") in req.full_url
    assert req.get_method() == "POST"
    assert req.data == b'{"resourceType":"Bundle"}'
    assert req.get_header("Content-type") == "application/fhir+json"
    assert timeout == app_module.MATCHBOX_TIMEOUT


def test_validate_asks_matchbox_for_the_extra_packages(monkeypatch):
    """Without igs, a Swedish profile claim comes back "not checked"."""
    captured = []
    fake_urlopen(monkeypatch, FakeResponse(OK_OUTCOME), captured)
    monkeypatch.setattr(app_module, "VALIDATION_IGS", ["hl7se.fhir.base#1.1.0"])
    app_module.validate_with_matchbox(b'{"resourceType":"Bundle"}')
    assert "igs=hl7se.fhir.base%231.1.0" in captured[0][0].full_url


def test_validate_omits_igs_when_none_are_configured(monkeypatch):
    captured = []
    fake_urlopen(monkeypatch, FakeResponse(OK_OUTCOME), captured)
    monkeypatch.setattr(app_module, "VALIDATION_IGS", [])
    app_module.validate_with_matchbox(b'{"resourceType":"Bundle"}')
    assert "igs=" not in captured[0][0].full_url


def test_validate_keeps_the_outcome_from_an_http_error(monkeypatch):
    """Matchbox answers 4xx with a real OperationOutcome; that is the verdict."""
    body = json.dumps(outcome(errors=3)).encode()
    fake_urlopen(monkeypatch, urllib.error.HTTPError(
        "http://mb", 422, "Unprocessable", None, io.BytesIO(body)))
    result = app_module.validate_with_matchbox(b"{}")
    assert sum(1 for i in result["issue"] if i["severity"] == "error") == 3


def test_validate_raises_when_an_http_error_carries_no_outcome(monkeypatch):
    for payload in (b"<html>gateway timeout</html>",
                    json.dumps({"resourceType": "Bundle"}).encode()):
        fake_urlopen(monkeypatch, urllib.error.HTTPError(
            "http://mb", 500, "Server Error", None, io.BytesIO(payload)))
        with pytest.raises(app_module.MatchboxError, match="HTTP 500"):
            app_module.validate_with_matchbox(b"{}")


def test_validate_raises_when_matchbox_is_unreachable(monkeypatch):
    for exc in (urllib.error.URLError("refused"), TimeoutError("timed out"),
                OSError("broken pipe")):
        fake_urlopen(monkeypatch, exc)
        with pytest.raises(app_module.MatchboxError, match="unreachable"):
            app_module.validate_with_matchbox(b"{}")


def test_validate_raises_on_a_200_that_is_not_an_outcome(monkeypatch):
    fake_urlopen(monkeypatch, FakeResponse(b"not json at all"))
    with pytest.raises(app_module.MatchboxError, match="non-JSON"):
        app_module.validate_with_matchbox(b"{}")
    fake_urlopen(monkeypatch, FakeResponse(json.dumps(
        {"resourceType": "Bundle"}).encode()))
    with pytest.raises(app_module.MatchboxError, match="not an OperationOutcome"):
        app_module.validate_with_matchbox(b"{}")
    fake_urlopen(monkeypatch, FakeResponse(b"[1, 2, 3]"))
    with pytest.raises(app_module.MatchboxError, match="not an OperationOutcome"):
        app_module.validate_with_matchbox(b"{}")


def test_a_matchbox_failure_surfaces_as_502(client, monkeypatch):
    token = register(client, "Alpha")
    def boom(body):
        raise app_module.MatchboxError("Matchbox unreachable: nope")
    monkeypatch.setattr(app_module, "validate_with_matchbox", boom)
    res = client.post("/api/eps", content=BUNDLE, headers={"X-Team-Token": token})
    assert res.status_code == 502
    assert "unreachable" in res.json()["detail"]


def test_matchbox_health_url_drops_the_fhir_suffix(monkeypatch):
    monkeypatch.setattr(app_module, "MATCHBOX_URL",
                        "http://matchbox:8080/matchboxv3/fhir")
    assert app_module.matchbox_health_url().startswith(
        "http://matchbox:8080/matchboxv3")
    assert "/fhir/" not in app_module.matchbox_health_url()


def fake_urlopen_by_url(monkeypatch, routes):
    """routes: [(url substring, result)] - the first match answers."""
    def _open(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        for needle, result in routes:
            if needle in url:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError("unexpected URL " + url)
    monkeypatch.setattr(urllib.request, "urlopen", _open)


def ig_bundle(*packages):
    """What Matchbox answers to GET ImplementationGuide: one entry per loaded
    package, carrying packageId and version."""
    entries = []
    for spec in packages:
        package, _, version = spec.partition("#")
        entries.append({"resource": {"resourceType": "ImplementationGuide",
                                     "packageId": package, "version": version}})
    return json.dumps({"resourceType": "Bundle", "entry": entries}).encode()


def matchbox_alive(monkeypatch, ig_list):
    fake_urlopen_by_url(monkeypatch, [("/actuator/health", FakeResponse(b"{}")),
                                      ("/ImplementationGuide", ig_list)])


def test_health_reports_matchbox_up_and_down(client, monkeypatch):
    matchbox_alive(monkeypatch, FakeResponse(
        ig_bundle(*app_module.VALIDATION_IGS, "hl7.fhir.eu.eps#1.0.0-ballot")))
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["matchbox"] == "up"
    assert body["missing_igs"] == []
    # the UI reads these to warn before a doomed request
    assert body["max_body_bytes"] == 5 * 1024 * 1024
    assert body["register_code_required"] is False
    fake_urlopen(monkeypatch, FakeResponse(b"{}", status=503))
    assert client.get("/api/health").json()["matchbox"] == "down"
    fake_urlopen(monkeypatch, urllib.error.URLError("refused"))
    body = client.get("/api/health").json()
    # the service itself is still ok - only the validator is down
    assert (body["status"], body["matchbox"]) == ("ok", "down")


def test_health_is_degraded_when_a_validation_ig_is_not_loaded(client, monkeypatch):
    """Matchbox reads application.yaml at boot; a package added later is
    silently absent and every claim from it goes unchecked. That is a state
    worth a name of its own, not "up"."""
    matchbox_alive(monkeypatch, FakeResponse(ig_bundle("hl7.fhir.eu.eps#1.0.0-ballot")))
    body = client.get("/api/health").json()
    assert body["matchbox"] == "degraded"
    assert body["missing_igs"] == list(app_module.VALIDATION_IGS)


def test_health_is_degraded_when_matchbox_loaded_nothing(client, monkeypatch):
    """A searchset with zero matches carries no "entry" at all. That is the
    answer "nothing is loaded" - the exact state this probe exists to catch -
    not an unreadable reply to shrug at."""
    matchbox_alive(monkeypatch, FakeResponse(json.dumps(
        {"resourceType": "Bundle", "type": "searchset", "total": 0}).encode()))
    body = client.get("/api/health").json()
    assert body["matchbox"] == "degraded"
    assert body["missing_igs"] == list(app_module.VALIDATION_IGS)
    # a reply that is not a Bundle at all is still "unknown", so stays "up"
    matchbox_alive(monkeypatch, FakeResponse(json.dumps(
        {"resourceType": "OperationOutcome", "issue": []}).encode()))
    assert client.get("/api/health").json()["matchbox"] == "up"


def test_health_accepts_a_package_listed_without_a_version(client, monkeypatch):
    package = app_module.VALIDATION_IGS[0].partition("#")[0]
    matchbox_alive(monkeypatch, FakeResponse(json.dumps({
        "resourceType": "Bundle",
        "entry": [{"resource": {"resourceType": "ImplementationGuide",
                                "packageId": package}}]}).encode()))
    # a versionless listing cannot be checked, so it is trusted rather than
    # reported missing
    assert client.get("/api/health").json()["missing_igs"] == []


def test_health_stays_up_when_the_ig_list_is_unreadable(client, monkeypatch):
    matchbox_alive(monkeypatch, FakeResponse(b"not json"))
    assert client.get("/api/health").json()["matchbox"] == "up"
    matchbox_alive(monkeypatch, urllib.error.URLError("search timed out"))
    assert client.get("/api/health").json()["matchbox"] == "up"
    matchbox_alive(monkeypatch, FakeResponse(b"[1, 2]"))
    assert client.get("/api/health").json()["matchbox"] == "up"


def test_health_reports_a_register_code_requirement(client, monkeypatch):
    monkeypatch.setattr(app_module, "REGISTER_CODE", "sesame")
    fake_urlopen(monkeypatch, urllib.error.URLError("refused"))
    assert client.get("/api/health").json()["register_code_required"] is True


def test_health_survives_a_half_open_connection(client, monkeypatch):
    fake_urlopen(monkeypatch, http.client.RemoteDisconnected("gone mid-handshake"))
    assert client.get("/api/health").json()["matchbox"] == "down"


# ---------------------------------------------------------------- OpenAPI

def test_openapi_describes_the_bodies_fastapi_cannot_see(client):
    """Two endpoints read the request body by hand, so FastAPI has no model
    to introspect and would publish them as taking nothing at all."""
    spec = client.get("/openapi.json").json()
    register_body = spec["paths"]["/api/register"]["post"]["requestBody"]
    props = register_body["content"]["application/json"]["schema"]["properties"]
    assert set(props) == {"team", "code"}
    eps_body = spec["paths"]["/api/eps"]["post"]["requestBody"]
    assert "application/fhir+json" in eps_body["content"]
    assert "/api/interop/confirm" not in spec["paths"]


def test_openapi_documents_the_score_headers(client):
    """The verdict rides on headers, so a generated client that only sees the
    body would have no way to read the score."""
    ok = client.get("/openapi.json").json()["paths"]["/api/eps"]["post"]
    headers = ok["responses"]["200"]["headers"]
    assert set(headers) == {"X-Pass", "X-Score", "X-Errors", "X-Sections",
                            "X-Resources"}


def test_openapi_declares_the_team_token_as_a_security_scheme(client):
    spec = client.get("/openapi.json").json()
    scheme = spec["components"]["securitySchemes"]["TeamToken"]
    assert scheme["in"] == "header" and scheme["name"] == "X-Team-Token"
    # submitting is the only endpoint that authenticates; the exchange corpus
    # is open, so it carries no security requirement at all
    assert spec["paths"]["/api/eps"]["post"]["security"] == [{"TeamToken": []}]
    assert "security" not in spec["paths"]["/api/exchange"]["get"]
    # ...and the raw header parameters are not listed anywhere either
    names = [p["name"].lower()
             for p in spec["paths"]["/api/exchange"]["get"].get("parameters", [])]
    assert "x-team-token" not in names and "authorization" not in names


def test_openapi_leaves_the_ui_out(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/" not in paths and "/favicon.svg" not in paths
    assert all(p.startswith("/api/") for p in paths)


def test_index_links_the_machine_readable_api(client):
    text = client.get("/").text
    assert 'href="/openapi.json"' in text


# ------------------------------------------- leaderboard -> clinical view

def test_leaderboard_carries_the_slug_its_rows_link_with(client, monkeypatch):
    tok = register(client, "Alpha Team")
    submit(client, tok, outcome(), monkeypatch)
    row = client.get("/api/leaderboard").json()[0]
    assert row["team_slug"] == "alpha-team"
    # the slug is what /api/submissions groups by, so the link lands somewhere
    subs = client.get("/api/submissions").json()
    assert subs[0]["team_slug"] == row["team_slug"]


def test_index_links_leaderboard_companies_to_their_documents(client):
    """A company name on the leaderboard is the obvious way in to that
    company's submissions, so it is a link rather than plain text."""
    text = client.get("/").text
    assert '#clinical/company/${encodeURIComponent(r.team_slug)}' in text
    # the clinical panel has to know the route kind, and it opens no document
    assert 'if (parts[1] === "company") { hideDoc(); selectCompany(parts[2]); return; }' in text
    # a company with nothing submitted has nothing to link to
    assert "${r.attempts" in text


# ---------------------------------------------------------------- samples

def test_samples_do_not_code_an_identifier_type_the_value_set_lacks():
    """The examples used v2-0203 NI, which is not in the R4 identifier-type
    value set, so every validation of them carried a terminology warning. The
    assigning authority OID already says which identifier this is."""
    for sample in app_module.SAMPLES:
        raw = app_module.read_sample_bytes(sample["id"])
        assert raw is not None, sample["id"]
        for entry in json.loads(raw)["entry"]:
            resource = entry.get("resource") or {}
            if resource.get("resourceType") != "Patient":
                continue
            for ident in resource.get("identifier") or []:
                assert "type" not in ident
                assert ident.get("system")


def test_index_drops_the_example_banner_but_keeps_the_bundle(client):
    """The disclaimer under the examples table says the documents are
    synthetic; repeating it over every rendering was noise. The download it
    used to carry moves onto the document's own byline."""
    text = client.get("/").text
    assert "sample-banner" not in text
    assert "Example document." not in text
    assert "'/bundle\" download>download the FHIR bundle</a>'" in text


# ---------------------------------------------------------------- QA pass additions

def http_error(code, payload):
    return urllib.error.HTTPError("http://matchbox/$validate", code, "boom",
                                  {}, io.BytesIO(payload))


def test_matchbox_4xx_with_a_non_outcome_body_is_a_validator_failure(monkeypatch):
    fake_urlopen(monkeypatch, http_error(400, b"[1, 2]"))
    with pytest.raises(app_module.MatchboxError):
        app_module.validate_with_matchbox(b"{}")
    fake_urlopen(monkeypatch, http_error(400, b'{"resourceType": "Bundle"}'))
    with pytest.raises(app_module.MatchboxError):
        app_module.validate_with_matchbox(b"{}")
    fake_urlopen(monkeypatch, http_error(400, b"<html>bad gateway</html>"))
    with pytest.raises(app_module.MatchboxError):
        app_module.validate_with_matchbox(b"{}")


def test_matchbox_5xx_is_never_the_bundles_fault(monkeypatch):
    """Matchbox wraps its own crashes in an OperationOutcome with a 500. That
    is the validator falling over, not a verdict, and must not score 0."""
    fake_urlopen(monkeypatch, http_error(500, json.dumps(outcome(errors=1)).encode()))
    with pytest.raises(app_module.MatchboxError):
        app_module.validate_with_matchbox(b"{}")


def test_matchbox_4xx_with_an_outcome_is_the_submitters_verdict(monkeypatch):
    fake_urlopen(monkeypatch, http_error(422, json.dumps(outcome(errors=2)).encode()))
    result = app_module.validate_with_matchbox(b"{}")
    assert len(result["issue"]) == 2


def test_matchbox_non_object_success_body_is_a_validator_failure(monkeypatch):
    fake_urlopen(monkeypatch, FakeResponse(b"[]"))
    with pytest.raises(app_module.MatchboxError):
        app_module.validate_with_matchbox(b"{}")
    fake_urlopen(monkeypatch, FakeResponse(b"null"))
    with pytest.raises(app_module.MatchboxError):
        app_module.validate_with_matchbox(b"{}")


def test_matchbox_half_open_connection_is_a_validator_failure(monkeypatch):
    fake_urlopen(monkeypatch, http.client.RemoteDisconnected("closed"))
    with pytest.raises(app_module.MatchboxError):
        app_module.validate_with_matchbox(b"{}")


def test_validate_honours_the_timeout_and_asks_for_fhir_json(monkeypatch):
    captured = []
    monkeypatch.setattr(app_module, "MATCHBOX_TIMEOUT", 7.5)
    fake_urlopen(monkeypatch, FakeResponse(OK_OUTCOME), captured)
    app_module.validate_with_matchbox(b"{}")
    (req, timeout), = captured
    assert timeout == 7.5
    assert req.get_header("Accept") == "application/fhir+json"
    assert req.get_header("Content-type") == "application/fhir+json"


def test_502_leaves_no_trace(client, monkeypatch):
    tok = register(client, "Unlucky")
    monkeypatch.setattr(app_module, "validate_with_matchbox",
                        lambda body: (_ for _ in ()).throw(app_module.MatchboxError("down")))
    res = client.post("/api/eps", content=BUNDLE, headers={"X-Team-Token": tok})
    assert res.status_code == 502
    audit_root = os.path.join(client.data_dir, "submissions")
    assert [f for _, _, files in os.walk(audit_root) for f in files] == []
    assert client.get("/api/submissions").json() == []
    (row,) = client.get("/api/leaderboard").json()
    assert row["attempts"] == 0


def test_non_object_json_body_is_rejected_before_validation(client, monkeypatch):
    tok = register(client, "Listy")
    called = []
    monkeypatch.setattr(app_module, "validate_with_matchbox",
                        lambda body: called.append(body) or outcome())
    for body in (b"[1, 2, 3]", b'"a bundle"', b"42", b"null"):
        res = client.post("/api/eps", content=body, headers={"X-Team-Token": tok})
        assert res.status_code == 400, body
        assert "object" in res.json()["detail"]
    assert called == []
    assert client.get("/api/leaderboard").json()[0]["attempts"] == 0


def test_outcome_without_issues_is_a_clean_pass(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    tok = register(client, "Silent")
    res = submit(client, tok, {"resourceType": "OperationOutcome"}, monkeypatch)
    assert res.status_code == 200
    assert res.headers["X-Pass"] == "true"
    assert res.headers["X-Errors"] == "0"
    assert res.headers["X-Score"] == "50"


def test_malformed_issues_do_not_crash_scoring(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    tok = register(client, "Odd Outcome")
    res = submit(client, tok, {"resourceType": "OperationOutcome",
                               "issue": ["not a dict", {"severity": 3}, {}]}, monkeypatch)
    assert res.status_code == 200
    assert res.headers["X-Pass"] == "true"
    res = submit(client, tok, {"resourceType": "OperationOutcome", "issue": "nope"},
                 monkeypatch)
    assert res.status_code == 200


def test_register_collapses_whitespace_and_dedupes_on_it(client):
    res = client.post("/api/register", json={"team": "  Alpha \t Team  "})
    assert res.status_code == 200
    assert res.json()["team"] == "Alpha Team"
    for clash in ("alpha team", "ALPHA   TEAM", " Alpha Team"):
        assert client.post("/api/register", json={"team": clash}).status_code == 409, clash


def test_register_rejects_non_string_and_missing_names(client):
    for payload in ({"team": 42}, {"team": None}, {"team": ["x"]}, {}, {"name": "x"}):
        res = client.post("/api/register", json=payload)
        assert res.status_code == 400, payload


def test_register_code_gate(client, monkeypatch):
    monkeypatch.setattr(app_module, "REGISTER_CODE", "sesame")
    assert client.post("/api/register", json={"team": "Gated"}).status_code == 403
    assert client.post("/api/register",
                       json={"team": "Gated", "code": "wrong"}).status_code == 403
    assert client.post("/api/register",
                       json={"team": "Gated", "code": 123}).status_code == 403
    assert client.post("/api/register",
                       json={"team": "Gated", "code": "sesame"}).status_code == 200
    # the wrong-code attempts did not reserve the name
    assert client.get("/api/leaderboard").json()[0]["team"] == "Gated"


def test_slugify_folds_nordic_letters_and_never_returns_empty():
    slugify = app_module.slugify
    assert slugify("Åland Hälsa") == "aland-halsa"
    assert slugify("Ærø Sundhed") == "aero-sundhed"
    assert slugify("Þór & Björk ehf.") == "thor-bjork-ehf"
    assert slugify("Straße 9") == "strasse-9"
    assert slugify("Łódź") == "lodz"
    assert slugify("北京") == "team"
    assert slugify("   ") == "team"
    assert slugify("--Hi--") == "hi"


def test_colliding_slugs_stay_unique(client):
    for name in ("Åland Hälsa", "Aland Halsa", "aland  halsa!", "北京", "上海"):
        assert client.post("/api/register", json={"team": name}).status_code == 200, name
    slugs = [r["team_slug"] for r in client.get("/api/leaderboard").json()]
    assert len(slugs) == len(set(slugs))
    assert sorted(slugs) == ["aland-halsa", "aland-halsa-2", "aland-halsa-3",
                             "team", "team-2"]


def test_second_composition_in_a_bundle_is_ignored():
    b = {"resourceType": "Bundle", "type": "document", "entry": [
        {"resource": {"resourceType": "Composition", "section": []}},
        {"resource": {"resourceType": "Patient", "id": "p"}},
        {"fullUrl": "urn:uuid:o1",
         "resource": {"resourceType": "Observation", "id": "o1"}},
        # a second Composition claiming three populated sections
        {"resource": {"resourceType": "Composition", "section": [
            {"title": f"s{i}", "entry": [{"reference": "urn:uuid:o1"}]} for i in range(3)]}},
    ]}
    sections, resources = app_module.count_bundle_richness(b)
    assert sections == 0
    # ... and not counted as clinical payload either
    assert resources == 1


def test_exchange_publishes_the_best_document_not_the_latest(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    tok = register(client, "Best Foot")
    rich = submit(client, tok, outcome(), monkeypatch, body=bundle(sections=2, resources=2))
    assert rich.headers["X-Score"] == "74"
    plain = submit(client, tok, outcome(), monkeypatch, body=bundle())
    assert plain.headers["X-Score"] == "50"
    rich_id = client.get("/api/submissions?team=best-foot").json()[-1]["id"]
    (entry,) = [e for e in client.get("/api/exchange").json() if e["team"] == "Best Foot"]
    assert entry["exchange_id"] == rich_id
    res = client.get("/api/exchange/best-foot")
    assert res.status_code == 200
    assert res.headers["X-Exchange-Id"] == str(rich_id)
    assert res.headers["X-Producer"] == "best-foot"
    assert json.loads(res.content) == json.loads(bundle(sections=2, resources=2))


def test_submissions_limit_bounds(client, monkeypatch):
    tok = register(client, "Bounded")
    for _ in range(3):
        submit(client, tok, outcome(), monkeypatch)
    assert len(client.get("/api/submissions?limit=2").json()) == 2
    assert len(client.get("/api/submissions?limit=1000").json()) == 3
    # out-of-range limits are clamped to 1..1000, as the README says
    assert len(client.get("/api/submissions?limit=0").json()) == 1
    assert len(client.get("/api/submissions?limit=-5").json()) == 1
    assert len(client.get("/api/submissions?limit=1001").json()) == 3
    assert client.get("/api/submissions?limit=ten").status_code == 422
    assert client.get("/api/submissions?team=nobody").json() == []


def test_missing_samples_dir_degrades_to_unavailable(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "SAMPLES_DIR", str(tmp_path / "nowhere"))
    listing = client.get("/api/samples").json()
    assert listing["samples"], "the catalogue itself is static"
    assert all(s["available"] is False for s in listing["samples"])
    sample_id = listing["samples"][0]["id"]
    assert client.get(f"/api/samples/{sample_id}").status_code == 404
    assert client.get(f"/api/samples/{sample_id}/bundle").status_code == 404


def test_sample_ids_cannot_escape_the_samples_dir(client):
    assert app_module.read_sample_bytes("../app") is None
    assert app_module.read_sample_bytes("..") is None
    assert app_module.read_sample_bytes("") is None
    for bad in ("..%2Fapp", "%2e%2e", "..%2F..%2Fapp.py"):
        assert client.get(f"/api/samples/{bad}").status_code == 404, bad
        assert client.get(f"/api/samples/{bad}/bundle").status_code == 404, bad


def test_summariser_crash_does_not_take_the_detail_endpoint_down(client, monkeypatch):
    tok = register(client, "Fragile")
    assert submit(client, tok, outcome(), monkeypatch).status_code == 200
    sub_id = client.get("/api/submissions").json()[0]["id"]
    real = app_module.summarize_bundle

    def boom(bundle):
        if bundle is None:
            return real(None)
        raise RuntimeError("unexpected shape")

    monkeypatch.setattr(app_module, "summarize_bundle", boom)
    res = client.get(f"/api/submissions/{sub_id}")
    assert res.status_code == 200
    assert res.json()["summary"] == real(None)
    res = client.get("/api/samples/andrea-cross-border")
    assert res.status_code == 200
    assert res.json()["summary"] == real(None)


def test_favicon_is_an_svg(client):
    res = client.get("/favicon.svg")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in res.content


def test_care_plan_shows_the_activity_not_the_plan(client):
    """Andrea's plan is active but its one visit was cancelled and never
    rebooked - the cancellation and the note are what a clinician needs."""
    summary = client.get("/api/samples/andrea-cross-border").json()["summary"]
    items = [i for sec in summary["sections"] for i in sec.get("items", [])]
    plans = [i for i in items if (i.get("detail") or "").startswith("scheduled ")]
    assert plans, [i.get("label") for i in items]
    plan = plans[0]
    assert plan["status"] == "cancelled"
    assert "plan since" in plan["detail"]
    assert plan["note"]


def test_device_use_shows_when_the_device_went_in(client):
    summary = client.get("/api/samples/arne-complex-care").json()["summary"]
    items = [i for sec in summary["sections"] for i in sec.get("items", [])]
    devices = [i for i in items if (i.get("detail") or "").startswith("in use since ")]
    assert devices, [i.get("detail") for i in items]


def _items(bundle):
    return [i for sec in app_module.summarize_bundle(bundle)["sections"]
            for i in sec.get("items", [])]


def _doc(section_code, section_title, resource):
    return {"resourceType": "Bundle", "type": "document", "entry": [
        {"resource": {"resourceType": "Composition", "section": [
            {"title": section_title, "code": {"coding": [
                {"system": "http://loinc.org", "code": section_code}]},
             "entry": [{"reference": resource["resourceType"] + "/" + resource["id"]}]}]}},
        {"resource": {"resourceType": "Patient", "id": "p"}},
        {"resource": resource},
    ]}


def test_a_period_with_only_an_end_is_not_a_start():
    """"since <date>" must point at a start; an end-only period says "until",
    and recordedOn is when the statement was written, not when use began."""
    plan = _items(_doc("18776-5", "Plan of care", {
        "resourceType": "CarePlan", "id": "cp", "status": "completed",
        "intent": "plan", "period": {"end": "2025-01-31"}}))
    assert plan[0]["detail"] == "plan until 2025-01-31"
    until = _items(_doc("46264-8", "Devices", {
        "resourceType": "DeviceUseStatement", "id": "d1", "status": "completed",
        "device": {"display": "Insulin pump"}, "timingPeriod": {"end": "2024-06-01"}}))
    assert until[0]["detail"] == "in use until 2024-06-01"
    span = _items(_doc("46264-8", "Devices", {
        "resourceType": "DeviceUseStatement", "id": "d2", "status": "completed",
        "device": {"display": "Insulin pump"},
        "timingPeriod": {"start": "2023-01-01", "end": "2024-06-01"}}))
    assert span[0]["detail"] == "in use 2023-01-01 to 2024-06-01"
    recorded = _items(_doc("46264-8", "Devices", {
        "resourceType": "DeviceUseStatement", "id": "d3", "status": "active",
        "device": {"display": "Hearing aid"}, "recordedOn": "2022-02-02"}))
    assert recorded[0]["detail"] == "recorded 2022-02-02"
    nothing = _items(_doc("46264-8", "Devices", {
        "resourceType": "DeviceUseStatement", "id": "d4", "status": "active",
        "device": {"display": "Hearing aid"}}))
    assert nothing[0]["detail"] is None


def test_notes_are_surfaced_and_normalised():
    items = app_module.summarize_bundle({
        "resourceType": "Bundle", "type": "document", "entry": [
            {"resource": {"resourceType": "Composition", "section": [
                {"title": "Problems", "code": {"coding": [
                    {"system": "http://loinc.org", "code": "11450-4"}]},
                 "entry": [{"reference": "Condition/c1"}]}]}},
            {"resource": {"resourceType": "Patient", "id": "p"}},
            {"resource": {"resourceType": "Condition", "id": "c1",
                          "code": {"text": "Gout"},
                          "note": [{"text": "  flares\n twice   a year "},
                                   {"text": "second note"}]}},
        ]})["sections"][0]["items"]
    assert items[0]["note"] == "flares twice a year"
    no_note = app_module.summarize_bundle({
        "resourceType": "Bundle", "type": "document", "entry": [
            {"resource": {"resourceType": "Composition", "section": [
                {"title": "Problems", "entry": [{"reference": "Condition/c1"}]}]}},
            {"resource": {"resourceType": "Condition", "id": "c1",
                          "note": [{"text": "   "}, "junk", {"author": "x"}]}},
        ]})["sections"][0]["items"]
    assert no_note[0]["note"] is None


def test_index_reads_health_and_links_rows(client):
    text = client.get("/").text
    assert "/api/health" in text
    assert "/api/leaderboard" in text
    assert "/api/submissions?limit=200" not in text  # the picker no longer truncates
    assert 'aria-current="page"' in text
    assert 'role="alert"' in text
    assert "Object.hasOwn(CV_VIEWS" in text
    assert "eps_team" not in text
    assert "@supports not (color: light-dark(" in text
    assert "prefers-reduced-motion" in text
    assert 'class="cv-open"' in text
    # list links carry no view segment, so they cannot revert a view picked later
    assert "href=\"#clinical/submission/'\n      + s.id + '\">'" in text
    assert "href=\"#clinical/sample/'\n        + esc(s.id) + '\">'" in text
    assert "function showUnknownCompany(slug)" in text
    assert "if (cvInitPending) return;" in text
    assert "cvCurrent = data;  // routeTeam reads" in text
    assert 'if (ev.target.closest("a")) return;' in text
    assert 'if (routeParts()[1] === "company") {' in text  # the picker keeps the URL honest
    assert "Company token" in text
    assert "team token" not in text.lower()

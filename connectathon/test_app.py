"""Tests for the EPS Connectathon service.

Matchbox is monkeypatched with canned OperationOutcomes; DATA_DIR points at a
pytest tmp dir so every test gets a fresh SQLite database and audit log.

Scoring model under test: error/fatal issues gate the score to 0; a valid
submission earns 50 + 10 per populated Composition section + 2 per clinical
resource (capped at +60). Team score = max submission score (+20 one-time
first blood for the first team ever to go valid).
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

import app as app_module


def bundle(sections=0, resources=0, empty_sections=0):
    """Build a document Bundle with N populated sections, M clinical resources."""
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
        entries.append({"resource": {"resourceType": "Observation", "id": f"o{i}"}})
    return json.dumps({"resourceType": "Bundle", "type": "document", "entry": entries})


BUNDLE = bundle()  # valid shape, no populated sections, no clinical payload


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


def consume_first_blood(client, monkeypatch):
    """Burn the global first blood bonus so scoring asserts stay clean."""
    tok = register(client, "First Blood Sink")
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

def test_submit_without_token_401(client):
    res = client.post("/api/eps", content=BUNDLE)
    assert res.status_code == 401


def test_submit_bad_token_401(client):
    register(client, "Alpha")
    res = client.post("/api/eps", content=BUNDLE,
                      headers={"X-Team-Token": "deadbeefdeadbeef"})
    assert res.status_code == 401


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
    consume_first_blood(client, monkeypatch)
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
    consume_first_blood(client, monkeypatch)
    token = register(client, "Alpha")
    body = bundle(sections=3, empty_sections=2, resources=4)
    res = submit(client, token, outcome(), monkeypatch, body=body)
    assert res.headers["X-Pass"] == "true"
    assert res.headers["X-Sections"] == "3"
    assert res.headers["X-Resources"] == "4"
    assert res.headers["X-Score"] == "88"  # 50 + 3*10 + 4*2


def test_resource_points_capped_at_60(client, monkeypatch):
    consume_first_blood(client, monkeypatch)
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


# ---------------------------------------------------------------- first blood

def test_first_blood_only_first_valid_team_only_once(client, monkeypatch):
    tok_a = register(client, "Alpha")
    tok_b = register(client, "Beta")

    # Beta submits first but invalid: no first blood for invalid, score 0
    res = submit(client, tok_b, outcome(errors=3), monkeypatch,
                 body=bundle(sections=4, resources=10))
    assert res.headers["X-Score"] == "0"
    # Alpha goes valid first: 50 + 20 first blood
    res_a = submit(client, tok_a, outcome(), monkeypatch)
    assert res_a.headers["X-Score"] == "70"
    # Beta goes valid later: no bonus
    res_b = submit(client, tok_b, outcome(), monkeypatch)
    assert res_b.headers["X-Score"] == "50"
    # Alpha valid again: bonus not re-awarded on the wire
    res_a2 = submit(client, tok_a, outcome(), monkeypatch)
    assert res_a2.headers["X-Score"] == "50"

    lb = {r["team"]: r for r in client.get("/api/leaderboard").json()}
    assert lb["Alpha"]["first_blood"] is True
    assert lb["Alpha"]["score"] == 70   # max(50, 50) + 20, not stacked
    assert lb["Beta"]["first_blood"] is False
    assert lb["Beta"]["score"] == 50


# ---------------------------------------------------------------- leaderboard

def test_team_score_is_max_across_submissions(client, monkeypatch):
    consume_first_blood(client, monkeypatch)
    token = register(client, "Alpha")
    submit(client, token, outcome(), monkeypatch,
           body=bundle(sections=3, resources=4))                    # 88
    submit(client, token, outcome(), monkeypatch,
           body=bundle(sections=1, resources=0))                    # 60
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
    tok_a = register(client, "Alpha")   # valid rich + first blood -> top
    tok_b = register(client, "Beta")    # reaches 50 first -> above Gamma
    tok_c = register(client, "Gamma")   # reaches 50 later
    register(client, "Idle")            # no submissions

    submit(client, tok_a, outcome(), monkeypatch,
           body=bundle(sections=2, resources=3))            # 76 + 20 first blood
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
    assert alpha["score"] == 96          # 50 + 2*10 + 3*2 + 20 first blood
    assert alpha["passed"] is True
    assert alpha["first_blood"] is True
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
    consume_first_blood(client, monkeypatch)
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
                              {"entry": [{"reference": "a"}]},      # counts
                              "garbage",                            # ignored
                          ]}},
            {"resource": {"resourceType": "Patient"}},
            {"resource": {"resourceType": "Condition"}},
            {"resource": {"resourceType": "MedicationStatement"}},
            {"request": {"method": "POST"}},                        # no resource
        ]
    }) == (1, 2)


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

RECEIPT = {"partner": "producer", "patient_family": "Schwartz",
           "birth_date": "1950-04-19", "medications": 2, "allergies": 1}


def interop_setup(client, monkeypatch):
    """Producer with a valid rich doc + a consumer team; returns both tokens."""
    tok_p = register(client, "Producer")
    tok_c = register(client, "Consumer")
    submit(client, tok_p, outcome(), monkeypatch, body=CLINICAL_BODY)
    return tok_p, tok_c


def confirm(client, token, **overrides):
    return client.post("/api/interop/confirm", json=dict(RECEIPT, **overrides),
                       headers={"X-Team-Token": token})


def test_exchange_requires_auth(client):
    assert client.get("/api/exchange").status_code == 401
    assert client.get("/api/exchange/anyone").status_code == 401
    assert client.post("/api/interop/confirm", json=RECEIPT).status_code == 401


def test_exchange_lists_other_teams_with_valid_docs_only(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    tok_f = register(client, "Failing")
    submit(client, tok_f, outcome(errors=2), monkeypatch)  # invalid -> unpublished
    listing = client.get("/api/exchange", headers={"X-Team-Token": tok_c}).json()
    assert [p["team"] for p in listing] == ["Producer"]    # no Failing, no self
    partner = listing[0]
    assert partner["team_slug"] == "producer"
    assert partner["consumed_by_you"] is False
    assert isinstance(partner["exchange_id"], int)
    assert partner["sections"] == 2 and partner["resources"] == 5
    # producer sees nobody: consumer and Failing have no valid submission
    assert client.get("/api/exchange", headers={"X-Team-Token": tok_p}).json() == []
    # bearer auth works too
    assert client.get(
        "/api/exchange", headers={"Authorization": f"Bearer {tok_c}"}
    ).status_code == 200


def test_exchange_fetch_bundle_headers_and_404s(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    res = client.get("/api/exchange/producer", headers={"X-Team-Token": tok_c})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/fhir+json")
    assert res.headers["X-Producer"] == "producer"
    assert int(res.headers["X-Exchange-Id"]) > 0
    assert res.json()["resourceType"] == "Bundle"
    # fetching your own document is allowed (for testing)
    own = client.get("/api/exchange/producer", headers={"X-Team-Token": tok_p})
    assert own.status_code == 200
    assert client.get("/api/exchange/ghost",
                      headers={"X-Team-Token": tok_c}).status_code == 404
    assert client.get("/api/exchange/consumer",
                      headers={"X-Team-Token": tok_p}).status_code == 404


def test_interop_confirm_success_points_and_matrix(client, monkeypatch):
    consume_first_blood(client, monkeypatch)
    tok_p, tok_c = interop_setup(client, monkeypatch)
    res = confirm(client, tok_c)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["result"] == "confirmed"
    assert data["consumer_points"] == 25
    assert data["producer_points"] == 10
    lb = {r["team"]: r for r in client.get("/api/leaderboard").json()}
    assert lb["Producer"]["interop"] == 10
    assert lb["Producer"]["score"] == 90       # 80 best submission + 10
    assert lb["Consumer"]["interop"] == 25
    assert lb["Consumer"]["score"] == 25       # no submission, interop only
    assert lb["Consumer"]["passed"] is False
    matrix = client.get("/api/interop/matrix").json()
    assert [(m["consumer"], m["producer"]) for m in matrix] == [
        ("Consumer", "Producer")]
    assert matrix[0]["ts"]
    listing = client.get("/api/exchange", headers={"X-Team-Token": tok_c}).json()
    producer = next(p for p in listing if p["team_slug"] == "producer")
    assert producer["consumed_by_you"] is True


def test_interop_confirm_idempotent(client, monkeypatch):
    consume_first_blood(client, monkeypatch)
    tok_p, tok_c = interop_setup(client, monkeypatch)
    assert confirm(client, tok_c).json()["result"] == "confirmed"
    again = confirm(client, tok_c)
    assert again.status_code == 200
    assert again.json()["result"] == "already_confirmed"
    assert "consumer_points" not in again.json()
    lb = {r["team"]: r for r in client.get("/api/leaderboard").json()}
    assert lb["Consumer"]["interop"] == 25     # not stacked
    assert lb["Producer"]["interop"] == 10
    assert len(client.get("/api/interop/matrix").json()) == 1


def test_interop_confirm_mismatch_422_and_no_ground_truth_leak(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    res = confirm(client, tok_c, patient_family="Wrong", medications=99)
    assert res.status_code == 422
    data = res.json()
    assert data["result"] == "mismatch"
    assert sorted(data["fields"]) == ["medications", "patient_family"]
    # correct values must not be revealed anywhere in the response
    assert "Schwartz" not in res.text
    assert "1950" not in res.text
    assert client.get("/api/interop/matrix").json() == []  # nothing recorded
    # birth date is an exact string match; family is casefold+strip
    ok = confirm(client, tok_c, patient_family="  schwartz  ")
    assert ok.status_code == 200
    bad = confirm(client, tok_c, birth_date="19/04/1950")
    assert bad.status_code == 422
    assert bad.json()["fields"] == ["birth_date"]


def test_interop_self_confirm_400(client, monkeypatch):
    tok_p, _ = interop_setup(client, monkeypatch)
    assert confirm(client, tok_p).status_code == 400


def test_interop_confirm_receipt_field_validation(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    res = client.post("/api/interop/confirm", json={"partner": "producer"},
                      headers={"X-Team-Token": tok_c})
    assert res.status_code == 400
    assert confirm(client, tok_c, medications="2").status_code == 400
    assert confirm(client, tok_c, allergies=True).status_code == 400
    assert client.post("/api/interop/confirm", json={},
                       headers={"X-Team-Token": tok_c}).status_code == 400


def test_interop_unknown_partner_and_no_valid_doc(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    assert confirm(client, tok_c, partner="ghost").status_code == 404
    tok_n = register(client, "NoDoc")
    submit(client, tok_n, outcome(errors=1), monkeypatch)
    assert confirm(client, tok_c, partner="nodoc").status_code == 404


def test_interop_exchange_id_validation(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    submit(client, tok_p, outcome(errors=1), monkeypatch)  # a failed one too
    subs = client.get("/api/submissions", params={"team": "producer"}).json()
    failed_id = next(s["id"] for s in subs if not s["pass"])
    good_id = next(s["id"] for s in subs if s["pass"])
    # a failed submission may not be targeted
    assert confirm(client, tok_c, exchange_id=failed_id).status_code == 400
    # another team's submission id may not be passed off as the partner's
    tok_o = register(client, "Other")
    submit(client, tok_o, outcome(), monkeypatch, body=CLINICAL_BODY)
    other_id = client.get("/api/submissions", params={"team": "other"}).json()[0]["id"]
    assert confirm(client, tok_c, exchange_id=other_id).status_code == 400
    assert confirm(client, tok_c, exchange_id="abc").status_code == 400
    assert confirm(client, tok_c, exchange_id=999999).status_code == 400
    res = confirm(client, tok_c, exchange_id=good_id)
    assert res.status_code == 200
    assert res.json()["result"] == "confirmed"
    assert res.json()["exchange_id"] == good_id


def test_leaderboard_interop_points_math(client, monkeypatch):
    consume_first_blood(client, monkeypatch)
    tok_a = register(client, "Aaa")
    tok_b = register(client, "Bbb")
    tok_c = register(client, "Ccc")
    submit(client, tok_a, outcome(), monkeypatch, body=CLINICAL_BODY)   # 80
    submit(client, tok_b, outcome(), monkeypatch, body=CLINICAL_BODY)   # 80
    # Ccc consumes Aaa and Bbb; Bbb consumes Aaa
    assert confirm(client, tok_c, partner="aaa").json()["result"] == "confirmed"
    assert confirm(client, tok_c, partner="bbb").json()["result"] == "confirmed"
    assert confirm(client, tok_b, partner="aaa").json()["result"] == "confirmed"
    lb = {r["team"]: r for r in client.get("/api/leaderboard").json()}
    assert lb["Aaa"]["interop"] == 20          # consumed by two teams x10
    assert lb["Aaa"]["score"] == 100           # 80 + 20
    assert lb["Bbb"]["interop"] == 35          # 25 consumed + 10 consumed-by
    assert lb["Bbb"]["score"] == 115           # 80 + 35
    assert lb["Ccc"]["interop"] == 50          # consumed two producers x25
    assert lb["Ccc"]["score"] == 50            # no submission of its own
    assert len(client.get("/api/interop/matrix").json()) == 3


# ---------------------------------------------------------------- misc

def test_index_serves_html(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "EPS Connectathon" in res.text
    assert res.headers["content-type"].startswith("text/html")


def test_index_has_tabs_and_company_copy(client):
    text = client.get("/").text
    for label in ("Leaderboard", "Submit", "Clinician view", "Interop testing"):
        assert label in text
    assert "One team per company" in text
    assert "Register your company" in text
    assert "<th>Company</th>" in text
    assert "<th>Interop</th>" in text


def test_index_offers_dashboard_timeline_and_document_views(client):
    text = client.get("/").text
    for mode in ("dashboard", "timeline", "compare", "document"):
        assert 'data-mode="%s"' % mode in text
    for fn in ("function viewTimeline(", "function viewDashboard(",
               "function viewDocument(", "function cmpCol("):
        assert fn in text
    # the dashboard is what opens by default
    assert 'class="cv-mode active" data-mode="dashboard"' in text
    assert 'let cvMode = "dashboard";' in text
    # compare puts the dashboard and the timeline next to each other
    assert 'cmpCol("dashboard", d) + cmpCol("timeline", d)' in text
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in text
    # the dropped narrative view left nothing behind
    assert "viewNarrative" not in text


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
    assert sections["medication"]["title"] == "Lakemedelslista"
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

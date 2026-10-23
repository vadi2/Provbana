"""Tests for the Nordic Hackathon 2026 service.

Matchbox is monkeypatched with canned OperationOutcomes; DATA_DIR points at a
pytest tmp dir so every test gets a fresh SQLite database and audit log.

Scoring model under test: error/fatal issues gate the score to 0; a valid
submission earns 50 + 10 per populated Composition section + 2 per clinical
resource (capped at +60). Team score = max submission score (+20 one-time
first pass for the first team ever to go valid).
"""

import base64
import datetime
import io
import html.parser
import http.client
import json
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

import pytest
from fastapi.testclient import TestClient

import app as app_module
import backup
import monitor
import reset
import warm


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
    monkeypatch.setattr(app_module, "USAGE_DB", "")
    monkeypatch.setattr(app_module, "SAMPLES_DIR",
                        os.path.join(os.path.dirname(app_module.__file__), "samples"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    with TestClient(app_module.app) as c:
        c.data_dir = str(tmp_path)
        yield c


def set_outcome(monkeypatch, oc):
    monkeypatch.setattr(app_module, "validate_with_matchbox",
                        lambda body, doc=None: oc)


def as_params(**values):
    """A Parameters resource body, which is what the operations take."""
    return {
        "resourceType": "Parameters",
        "parameter": [{"name": k, "valueString": v} for k, v in values.items()],
    }


def _part_value(part):
    if "part" in part:
        # A repeated name *is* the list - FHIR has no array type - so repeats
        # collect rather than overwrite. A name seen once stays a scalar, which
        # is the same ambiguity any FHIR client has to resolve from the
        # operation's definition; part_list() below is how the tests resolve it.
        out = {}
        for p in part["part"]:
            value = _part_value(p)
            if p["name"] in out:
                seen = out[p["name"]]
                out[p["name"]] = (seen if isinstance(seen, list) else [seen]) + [value]
            else:
                out[p["name"]] = value
        return out
    if "resource" in part:
        return part["resource"]
    for key, value in part.items():
        if key.startswith("value"):
            return value
    return None


def part_list(entry, name):
    """A part the operation defines as repeating, always as a list.

    Absent is the empty list: FHIR has no null and no empty array, so a company
    that has passed no other category simply has no such part.
    """
    value = entry.get(name)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def param_entries(res, name="entry"):
    """Every value of a repeated parameter, as plain Python."""
    body = res.json() if hasattr(res, "json") else res
    return [_part_value(p) for p in body.get("parameter", []) if p["name"] == name]


def param_one(res, name):
    found = param_entries(res, name)
    return found[0] if found else None


def diagnostics(res):
    """The message out of an error OperationOutcome."""
    body = res.json()
    assert body["resourceType"] == "OperationOutcome", body
    return body["issue"][0].get("diagnostics", "")


def register_res(client, name, code=None):
    values = {"name": name}
    if code is not None:
        values["code"] = code
    return client.post("/fhir/$register", json=as_params(**values))


def register(client, name):
    res = register_res(client, name)
    assert res.status_code == 200, res.text
    return param_one(res, "token")


def submit(client, token, oc, monkeypatch, body=BUNDLE):
    set_outcome(monkeypatch, oc)
    return client.post("/fhir/Bundle/$validate", content=body,
                       headers={"X-Team-Token": token,
                                "Content-Type": "application/fhir+json"})


def shots(res):
    """The DocumentReferences out of a searchset."""
    body = res.json()
    assert body["resourceType"] == "Bundle", body
    return [e["resource"] for e in body.get("entry", [])]


def custodian_of(shot):
    return shot["custodian"]["reference"].split("/", 1)[1]


def author_of(shot):
    return shot["author"][0]["reference"].split("/", 1)[1]


def related_bundle(shot):
    return int(shot["context"]["related"][0]["reference"].split("/", 1)[1])


def docref(slug, image, note=None, mime="image/png"):
    """A DocumentReference carrying a screenshot, the way the UI sends one."""
    body = {
        "resourceType": "DocumentReference",
        "status": "current",
        "custodian": {"reference": "Organization/%s" % slug},
        "content": [{"attachment": {
            "contentType": mime,
            "data": base64.b64encode(image).decode("ascii"),
        }}],
    }
    if note is not None:
        body["description"] = note
    return body


def consume_first_pass(client, monkeypatch):
    """Burn the global first pass bonus so scoring asserts stay clean."""
    tok = register(client, "First Pass Sink")
    res = submit(client, tok, outcome(), monkeypatch)
    assert res.headers["X-Score"] == "70"  # 50 + 20, sanity


# ---------------------------------------------------------------- register

def test_register_returns_hex_token_and_an_organization(client):
    res = register_res(client, "  Alpha Team  ")
    assert res.status_code == 200
    org = param_one(res, "organization")
    assert org["resourceType"] == "Organization"
    assert org["name"] == "Alpha Team"        # trimmed
    assert org["id"] == "alpha-team"          # the Organization's logical id
    assert org["identifier"][0]["value"] == "alpha-team"
    token = param_one(res, "token")
    assert len(token) == 16
    int(token, 16)  # valid hex


def test_register_duplicate_name_409(client):
    register(client, "Alpha")
    assert register_res(client, "Alpha").status_code == 409
    # trimmed and case-insensitive duplicates are rejected too
    assert register_res(client, " alpha ").status_code == 409


def test_register_name_length_validation(client):
    assert register_res(client, "x").status_code == 400
    assert register_res(client, "y" * 41).status_code == 400
    # a Parameters resource with no `name` part at all
    assert client.post("/fhir/$register", json=as_params()).status_code == 400


# ---------------------------------------------------------------- submit auth

def test_submit_without_token_401(client, monkeypatch):
    # a real team exists, so this pins the credential check rather than an
    # empty table
    token = register(client, "Alpha")
    assert client.post("/fhir/Bundle/$validate", content=BUNDLE).status_code == 401
    assert submit(client, token, outcome(), monkeypatch).status_code == 200


def test_submit_bad_token_401(client, monkeypatch):
    token = register(client, "Alpha")
    res = client.post("/fhir/Bundle/$validate", content=BUNDLE,
                      headers={"X-Team-Token": "deadbeefdeadbeef"})
    assert res.status_code == 401
    # not a prefix or suffix match either
    for wrong in (token[:-1], token + "0", token.upper() if token.islower() else token):
        if wrong == token:
            continue
        assert client.post("/fhir/Bundle/$validate", content=BUNDLE,
                           headers={"X-Team-Token": wrong}).status_code == 401
    assert submit(client, token, outcome(), monkeypatch).status_code == 200


def test_submit_bearer_token_accepted(client, monkeypatch):
    token = register(client, "Alpha")
    set_outcome(monkeypatch, outcome())
    res = client.post("/fhir/Bundle/$validate", content=BUNDLE,
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200


def test_submit_invalid_json_400(client):
    token = register(client, "Alpha")
    res = client.post("/fhir/Bundle/$validate", content="not json {",
                      headers={"X-Team-Token": token})
    assert res.status_code == 400


def test_submit_oversized_body_413(client, monkeypatch):
    token = register(client, "Alpha")
    monkeypatch.setattr(app_module, "MAX_BODY_BYTES", 1024)
    # Rejected before validation runs: a Matchbox call here would raise.
    monkeypatch.setattr(app_module, "validate_with_matchbox",
                        lambda body, doc=None: (_ for _ in ()).throw(AssertionError("validated oversized body")))
    res = client.post("/fhir/Bundle/$validate", content=b"x" * 2048,
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

    def boom(body, doc=None):
        raise app_module.MatchboxError("connection refused")

    monkeypatch.setattr(app_module, "validate_with_matchbox", boom)
    res = client.post("/fhir/Bundle/$validate", content=BUNDLE,
                      headers={"X-Team-Token": token})
    assert res.status_code == 502
    lb = param_entries(client.get("/fhir/$leaderboard"))
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
    # Alpha valid again: the bonus is not re-awarded, but X-Score is Alpha's
    # standing rather than this bundle's own points, so it still reads 70. The
    # bundle's own 50 is on X-Submission-Score.
    res_a2 = submit(client, tok_a, outcome(), monkeypatch)
    assert res_a2.headers["X-Score"] == "70"
    assert res_a2.headers["X-Submission-Score"] == "50"

    lb = {r["organization"]: r for r in param_entries(client.get("/fhir/$leaderboard"))}
    assert lb["Alpha"]["firstPass"] is True
    assert lb["Alpha"]["score"] == 70   # max(50, 50) + 20, not stacked
    assert lb["Beta"]["firstPass"] is False
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

    lb = {r["organization"]: r for r in param_entries(client.get("/fhir/$leaderboard"))}
    alpha = lb["Alpha"]
    assert alpha["score"] == 88          # max, not latest and not sum
    assert alpha["attempts"] == 3
    assert alpha["passed"] is True
    assert alpha["bestSections"] == 3   # from the highest-scoring submission
    assert alpha["bestResources"] == 4
    assert alpha["bestErrors"] == 0     # fewest errors across submissions


def test_best_errors_is_fewest_for_failing_team(client, monkeypatch):
    token = register(client, "Alpha")
    submit(client, token, outcome(errors=7), monkeypatch)
    submit(client, token, outcome(errors=3), monkeypatch)
    lb = param_entries(client.get("/fhir/$leaderboard"))
    assert lb[0]["bestErrors"] == 3
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

    lb = param_entries(client.get("/fhir/$leaderboard"))
    names = [r["organization"] for r in lb]
    assert names.index("Alpha") == 0
    # same score (50): whoever reached it first wins (Beta's valid submission
    # landed before Gamma's; Beta's extra failed attempt must not hurt)
    assert names.index("Beta") < names.index("Gamma")
    assert [r["rank"] for r in lb] == [1, 2, 3, 4]

    alpha = lb[0]
    assert alpha["score"] == 96          # 50 + 2*10 + 3*2 + 20 first pass
    assert alpha["passed"] is True
    assert alpha["firstPass"] is True
    assert alpha["bestSections"] == 2
    assert alpha["bestResources"] == 3
    assert alpha["lastSubmission"]

    idle = next(r for r in lb if r["organization"] == "Idle")
    assert idle["score"] == 0
    assert idle["attempts"] == 0
    # FHIR has no null, so the empty columns are absent rather than sent as one
    for absent in ("bestErrors", "bestSections", "bestResources", "lastSubmission"):
        assert absent not in idle, absent


def test_tiebreak_first_to_reach_best_score_resubmitting_harmless(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    tok_a = register(client, "Early")
    tok_b = register(client, "Late")
    submit(client, tok_a, outcome(), monkeypatch)          # 50, reached first
    submit(client, tok_b, outcome(), monkeypatch)          # 50, reached later
    # Early tries again afterwards (worse result, more attempts, later
    # last_submission) - none of that may hurt its rank.
    submit(client, tok_a, outcome(errors=3), monkeypatch)  # 0
    lb = param_entries(client.get("/fhir/$leaderboard"))
    names = [r["organization"] for r in lb]
    assert names.index("Early") < names.index("Late")


def test_tiebreak_alphabetical_for_identical_teams(client):
    register(client, "Zeta Squad")   # registered first
    register(client, "Anna Squad")
    lb = param_entries(client.get("/fhir/$leaderboard"))
    names = [r["organization"] for r in lb]
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
    subs = param_entries(client.get("/fhir/$submissions"))
    assert len(subs) == 2
    assert subs[0]["organization"] == "Beta"    # newest first
    assert subs[0]["pass"] is False
    assert subs[1]["organization"] == "Alpha"
    assert {"id", "bundle", "organization", "organizationSlug", "recorded",
            "pass", "score", "errors", "warnings", "sections",
            "resources"} <= set(subs[0])
    # the entry points at the Bundle it was validated from
    assert subs[0]["bundle"] == "/fhir/Bundle/%d" % subs[0]["id"]
    only_a = param_entries(
        client.get("/fhir/$submissions", params={"organization": "alpha"}))
    assert [s["organization"] for s in only_a] == ["Alpha"]
    limited = param_entries(client.get("/fhir/$submissions", params={"count": 1}))
    assert [s["organization"] for s in limited] == ["Beta"]


def test_submission_detail_and_raw_bundle(client, monkeypatch):
    token = register(client, "Alpha")
    submit(client, token, outcome(), monkeypatch, body=CLINICAL_BODY)
    sub_id = param_entries(client.get("/fhir/$submissions"))[0]["id"]
    detail = client.get(f"/ui/documents/{sub_id}").json()
    assert detail["id"] == sub_id
    assert detail["team"] == "Alpha"
    assert detail["pass"] is True
    assert detail["summary"]["patient"]["name"] == "Petra M. Schwartz"
    assert detail["summary"]["sections"][0]["items"]
    raw = client.get(f"/fhir/Bundle/{sub_id}")
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("application/fhir+json")
    assert raw.json()["resourceType"] == "Bundle"


def test_submission_404s(client, monkeypatch):
    assert client.get("/ui/documents/999").status_code == 404
    assert client.get("/fhir/Bundle/999").status_code == 404
    token = register(client, "Alpha")
    submit(client, token, outcome(), monkeypatch)
    sub_id = param_entries(client.get("/fhir/$submissions"))[0]["id"]
    team_dir = os.path.join(client.data_dir, "submissions", "alpha")
    for name in os.listdir(team_dir):
        os.remove(os.path.join(team_dir, name))
    assert client.get(f"/ui/documents/{sub_id}").status_code == 404
    assert client.get(f"/fhir/Bundle/{sub_id}").status_code == 404


def test_preview_renders_a_bundle_without_storing_it(client):
    res = client.post("/ui/preview", content=CLINICAL_BODY)
    assert res.status_code == 200
    data = res.json()
    assert data["preview"] is True
    assert data["summary"]["patient"]["name"] == "Petra M. Schwartz"
    assert data["summary"]["sections"][0]["items"]
    # nothing reached the hackathon
    assert param_entries(client.get("/fhir/$submissions")) == []
    assert client.post("/ui/preview", content=b"not json").status_code == 400
    assert client.post("/ui/preview", content=b'{"resourceType": "Patient"}').status_code == 400


# ---------------------------------------------------------------- interop

def interop_setup(client, monkeypatch):
    """Producer with a valid rich doc + a consumer team; returns both tokens."""
    tok_p = register(client, "Producer")
    tok_c = register(client, "Consumer")
    submit(client, tok_p, outcome(), monkeypatch, body=CLINICAL_BODY)
    return tok_p, tok_c


def test_the_exchange_corpus_needs_no_token_at_all(client):
    """Interop testing is unscored, so there is nobody to authenticate."""
    assert client.get("/fhir/$exchange").status_code == 200
    assert client.get("/fhir/Organization/anyone/$document").status_code == 404  # unknown team
    # a token is neither required nor rejected - it is simply not read
    register(client, "Alpha")
    for headers in ({"X-Team-Token": "not-a-real-token"}, {}):
        assert client.get("/fhir/$exchange", headers=headers).status_code == 200


def test_every_reader_sees_every_published_document(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    submit(client, tok_c, outcome(), monkeypatch, body=CLINICAL_BODY)
    # Nothing is hidden from anyone, including a company's own document: with
    # no points at stake there is no reason to tailor the list per caller.
    listing = param_entries(client.get("/fhir/$exchange"))
    assert [p["organization"] for p in listing] == ["Consumer", "Producer"]


def test_anyone_can_download_a_published_bundle(client, monkeypatch):
    interop_setup(client, monkeypatch)
    res = client.get("/fhir/Organization/producer/$document")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/fhir+json")
    assert res.headers["X-Producer"] == "producer"
    assert json.loads(res.content)["resourceType"] == "Bundle"


def test_bundles_download_under_a_named_file(client, monkeypatch):
    """A browser click must not save every document as a nameless "bundle"."""
    tok_p, _ = interop_setup(client, monkeypatch)
    cases = {
        "/fhir/Organization/producer/$document": 'attachment; filename="producer-eps.json"',
        "/fhir/Bundle/1": 'attachment; filename="producer-1-eps.json"',
        "/fhir/Bundle/andrea-cross-border":
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
    listing = param_entries(client.get("/fhir/$exchange"))
    assert [p["organization"] for p in listing] == ["Producer"]    # no Failing
    partner = listing[0]
    assert partner["organizationSlug"] == "producer"
    assert isinstance(partner["id"], int)
    assert partner["sections"] == 2 and partner["resources"] == 5


def test_exchange_fetch_bundle_headers_and_404s(client, monkeypatch):
    interop_setup(client, monkeypatch)
    res = client.get("/fhir/Organization/producer/$document")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/fhir+json")
    assert res.headers["X-Producer"] == "producer"
    assert res.headers["content-location"] == "/fhir/Bundle/%d" % int(
        res.headers["content-location"].rsplit("/", 1)[1])
    assert res.json()["resourceType"] == "Bundle"
    assert client.get("/fhir/Organization/ghost/$document").status_code == 404
    # registered, but nothing valid to publish
    assert client.get("/fhir/Organization/consumer/$document").status_code == 404


def test_the_receipt_endpoints_are_gone(client, monkeypatch):
    """Interop is untracked, so nothing claims a consumption or records one."""
    tok_p, tok_c = interop_setup(client, monkeypatch)
    receipt = {"partner": "producer", "patient_family": "Schwartz",
               "birth_date": "1950-04-19", "medications": 2, "allergies": 1}
    gone = client.post("/fhir/$confirm", json=receipt,
                       headers={"X-Team-Token": tok_c})
    assert gone.status_code == 404
    assert client.get("/fhir/$matrix").status_code == 404


def test_the_leaderboard_does_not_score_interop(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    tok_p = register(client, "Producer2")
    register(client, "Consumer2")            # registered, never submits
    submit(client, tok_p, outcome(), monkeypatch, body=CLINICAL_BODY)
    lb = {r["organization"]: r for r in param_entries(client.get("/fhir/$leaderboard"))}
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
    # the plain document rendering is what opens by default, and it is the
    # default every time: opening a document goes to CV_DEFAULT rather than to
    # whichever view the last one was left in
    assert 'const CV_DEFAULT = "document";' in text
    assert "let cvMode = CV_DEFAULT;" in text
    assert '+ "/" + CV_DEFAULT);' in text
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
    detail = client.get("/ui/documents/arne-complex-care").json()
    systems = [i.get("system") for i in detail["summary"]["patient"]["identifiers"]]
    assert any(systems), "the API should still expose what the UI omits"


def test_samples_link_to_their_scenario_briefs(client):
    """Each worked example names the organisers' brief it was written from,
    rather than restating it in copy that would drift."""
    samples = {s["id"]: s for s in param_entries(client.get("/fhir/$samples"))}
    assert samples["andrea-cross-border"]["scenarioName"] == "The Andrea Scenario"
    assert samples["arne-complex-care"]["scenarioName"] == "The Arne Scenario"
    for sample in samples.values():
        assert sample["scenarioUrl"].startswith("https://")
    # The page has to read the part names the API sends, or the link is
    # silently never drawn.
    text = client.get("/").text
    assert "s.scenarioUrl" in text and "s.scenario_url" not in text


def test_samples_carry_the_personnummer_from_the_bundle(client):
    """The page shows each example's personnummer, read off the document so
    the two cannot drift, in the hyphenated form people write it in."""
    samples = {s["id"]: s for s in param_entries(client.get("/fhir/$samples"))}
    assert samples["andrea-cross-border"]["personnummer"] == "20020725-2388"
    assert samples["arne-complex-care"]["personnummer"] == "19430209-6997"
    assert app_module.sample_personnummer(None) is None
    assert app_module.sample_personnummer(b'{"entry": [{}]}') is None


def test_index_lists_examples_as_a_company(client):
    """The examples are one more entry in the company picker, so there is a
    single place to open a document from."""
    text = client.get("/").text
    assert 'const SAMPLE_TEAM = "__samples";' in text
    assert "Example documents</option>" in text
    assert "function renderSampleList(" in text


def test_index_offers_bundle_downloads_and_anonymous_partner_browsing(client):
    text = client.get("/").text
    assert '" download>download the FHIR bundle</a>' in text
    assert "/fhir/Bundle/" in text


def test_index_credits_matchbox_and_the_hl7_validator(client):
    """Matchbox is what runs, but the verdict is the official HL7 validator's -
    credit both, so a team that disagrees with a finding knows whose engine to
    take it up with."""
    text = client.get("/").text
    assert "https://github.com/ahdis/matchbox" in text
    assert "https://github.com/hapifhir/org.hl7.fhir.core" in text
    # the guide being validated against is still named, in the submit tab
    assert "https://hl7.eu/fhir/eps/1.0.0-ballot/" in text


def test_index_does_not_claim_terminology_is_validated(client):
    """Matchbox runs with txServer: n/a, so SNOMED/LOINC/ATC codes come back as
    "cannot be validated". Saying otherwise would send a team home trusting a
    code nothing checked."""
    text = client.get("/").text
    assert ("that require a license (such as SNOMED CT,\n  LOINC, WHO ATC) are not checked"
            in text)
    # every guide a submission can be validated against is named on the page
    for guide in ("eps", "laboratory", "mpd", "imaging"):
        assert "https://hl7.eu/fhir/%s/" % guide in text


def test_index_offers_to_explain_the_errors_with_an_assistant(client):
    """One action above the findings sends every error at once to whichever
    assistant the team already uses. They are the participant's own links,
    followed by their own browser: nothing goes through this service."""
    text = client.get("/").text
    assert 'id="explain-btn"' in text
    for name in ("ChatGPT", "Claude", "Gemini", "Le Chat"):
        assert 'name: "%s"' % name in text
    # three take the question in the URL; Gemini opens blank and is told so
    assert 'prefill: "https://chatgpt.com/?q="' in text
    assert 'prefill: "https://claude.ai/new?q="' in text
    assert 'prefill: "https://chat.mistral.ai/chat?q="' in text
    assert 'prefill: null, open: "https://gemini.google.com/app"' in text
    assert '"paste it"' in text
    # a plain-http venue box has no clipboard API: the prompt is shown instead
    assert 'id="explain-text"' in text


def test_the_explain_menu_opens_on_screen_when_a_phone_wraps_the_toolbar(client):
    """The menu hangs leftwards from the button's right edge. Wrapped onto its
    own line at the left on a phone, the button took the menu 47px off the
    screen - so the button keeps to the right of its line."""
    text = client.get("/").text
    assert ".menu { position: absolute; z-index: 5; top: calc(100% + 4px); right: 0;" in text
    assert ".issue-toolbar { display: flex;" in text and "flex-wrap: wrap;" in text
    assert ".issue-toolbar .menu-wrap { margin-left: auto; }" in text


# ------------------------------------------------------------- sample documents


def test_sample_list_is_labelled_synthetic(client):
    res = client.get("/fhir/$samples")
    assert "does not come from any real system" in param_one(res, "disclaimer")
    entries = param_entries(res)
    assert {s["id"] for s in entries} == {
        "andrea-cross-border", "arne-complex-care", "ville-difficult-airway"}
    assert all(s["available"] for s in entries)
    assert all(s["name"] and s["tagline"] and s["scenario"] for s in entries)
    # each entry carries the URL its document is fetched from
    assert all(s["bundle"] == "/fhir/Bundle/" + s["id"] for s in entries)


def test_sample_detail_matches_submission_shape(client):
    data = client.get("/ui/documents/andrea-cross-border").json()
    # the clinician renderer consumes samples and submissions through one path
    assert set(data) >= {"team", "pass", "summary", "sample"}
    assert data["pass"] is True
    assert "does not come from any real system" in data["sample"]["disclaimer"]
    summary = data["summary"]
    assert summary["patient"]["name"] == "Andrea Lindqvist"
    kinds = {s["kind"] for s in summary["sections"]}
    assert {"allergy", "problem", "medication"} <= kinds


def test_sample_asserts_nil_known_allergies_rather_than_omitting_them(client):
    data = client.get("/ui/documents/andrea-cross-border").json()
    allergies = next(s for s in data["summary"]["sections"] if s["kind"] == "allergy")
    assert allergies["items"] == []
    assert allergies["empty_reason"]


def test_swedish_sample_is_classified_by_loinc_not_title(client):
    data = client.get("/ui/documents/arne-complex-care").json()
    sections = {s["kind"]: s for s in data["summary"]["sections"]}
    assert sections["allergy"]["title"] == "Allergier och intoleranser"
    assert sections["medication"]["title"] == "Läkemedelslista"
    assert len(sections["problem"]["items"]) == 10
    labels = " ".join(i["label"] for i in sections["allergy"]["items"])
    assert "Penicillin" in labels


def test_ville_sample_puts_the_alerts_first_with_their_evidence(client):
    """The difficult-airway scenario lives or dies on the Alerts section: the
    views must know it as its own category, read the priority from the
    extension, and surface the author's note - which is where "what to do"
    is written - rather than file the flags under "other"."""
    data = client.get("/ui/documents/ville-difficult-airway").json()
    sections = data["summary"]["sections"]
    assert sections[0]["kind"] == "alert"
    assert sections[0]["title"] == "Uppmärksamhetsinformation"
    alerts = sections[0]["items"]
    assert [a["category"] for a in alerts] == ["alert"] * 3
    airway = alerts[0]
    assert "luftväg" in airway["label"].lower()
    assert "high priority" in airway["flags"]
    assert airway["detail"].startswith("high priority; since 2016-03-15")
    assert "fiberoptisk" in airway["note"]
    assert airway["date"] == "2016-03-15"
    assert airway["gaps"] == []
    # medium priority is not a risk flag
    assert "high priority" not in alerts[2]["flags"]


def test_ville_sample_records_the_last_dose_as_an_administration(client):
    """'When did he last take it' is a MedicationAdministration next to the
    statement, dated and labelled as a last dose rather than counted as a
    second active medicine without a dosage instruction."""
    data = client.get("/ui/documents/ville-difficult-airway").json()
    meds = next(s for s in data["summary"]["sections"] if s["kind"] == "medication")
    last = [i for i in meds["items"] if "last dose" in i["flags"]]
    assert len(last) == 2
    assert last[0]["label"].startswith("Humira")
    assert last[0]["detail"] == "given 2026-10-06; 40 mg subkutant"
    assert last[0]["date"] == "2026-10-06"
    assert last[0]["gaps"] == []
    assert "active" not in last[0]["flags"]
    active = [i for i in meds["items"] if "active" in i["flags"]]
    assert len(active) == 4


def test_ville_sample_patient_story_is_shown_as_narrative(client):
    """A section with no entries and no emptyReason is its narrative: the
    patient story says 'call him Ville', and 'No entries' would hide it."""
    data = client.get("/ui/documents/ville-difficult-airway").json()
    story = next(s for s in data["summary"]["sections"] if s["kind"] == "story")
    assert story["items"] == [] and story["empty_reason"] is None
    assert story["narrative"].startswith("Ville vill kallas Ville")
    assert "\n" in story["narrative"] and "<" not in story["narrative"]
    # sections with entries or a reason keep the old shape
    allergies = next(s for s in data["summary"]["sections"] if s["kind"] == "allergy")
    assert allergies["narrative"] is None and allergies["empty_reason"]
    procedures = next(s for s in data["summary"]["sections"] if s["kind"] == "procedure")
    link = next(i for i in procedures["items"] if i["label"].startswith("Anestesijournal"))
    assert "https://journal.karolinska.example" in link["detail"]
    assert data["summary"]["unreferenced"] == []
    # the dashboard shows the prose as a card, not only the document view
    text = client.get("/").text
    assert "x.narrative" in text and "db-prose" in text


def test_samples_label_a_samordningsnummer_as_one(client):
    """Ville has no personnummer; the number shown must not be called one."""
    samples = {s["id"]: s for s in param_entries(client.get("/fhir/$samples"))}
    assert samples["ville-difficult-airway"]["personnummer"] == "20130482-2172"
    assert samples["ville-difficult-airway"]["identifierLabel"] == "Samordningsnummer"
    assert samples["arne-complex-care"]["identifierLabel"] == "Personnummer"
    text = client.get("/").text
    assert "s.identifierLabel" in text


def test_sample_bundle_downloads_as_fhir(client):
    res = client.get("/fhir/Bundle/arne-complex-care")
    assert res.headers["content-type"].startswith("application/fhir+json")
    doc = res.json()
    assert doc["type"] == "document"
    assert doc["meta"]["profile"] == [
        "http://hl7.eu/fhir/eps/StructureDefinition/bundle-eu-eps"
    ]


def test_unknown_sample_is_404(client):
    assert client.get("/ui/documents/nope").status_code == 404
    assert client.get("/fhir/Bundle/nope").status_code == 404


def test_register_returns_company_note(client):
    note = param_one(register_res(client, "NoteCo"), "note")
    assert note and "company" in note.lower()


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
    res = client.post("/fhir/Bundle/$validate", content="x" * 2000,
                      headers={"X-Team-Token": token})
    assert res.status_code == 413
    assert "limit is 500 bytes" in diagnostics(res)


def test_an_unauthenticated_oversized_post_is_rejected_on_the_credential(client,
                                                                        monkeypatch):
    """401 must come before the body is read, or anyone can spend our memory."""
    monkeypatch.setattr(app_module, "MAX_BODY_BYTES", 500)
    register(client, "Alpha")
    assert client.post("/fhir/Bundle/$validate", content="x" * 2000).status_code == 401


def test_small_endpoints_have_their_own_tighter_limit(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_JSON_BODY_BYTES", 200)
    assert register_res(client, "A" * 400).status_code == 413
    # unchanged limit still admits an ordinary registration
    monkeypatch.setattr(app_module, "MAX_JSON_BODY_BYTES", 64 * 1024)
    assert register_res(client, "Alpha").status_code == 200


def test_deeply_nested_json_is_a_client_error_not_a_crash(client):
    token = register(client, "Alpha")
    nested = "[" * 10000 + "]" * 10000
    res = client.post("/fhir/Bundle/$validate", content=nested, headers={"X-Team-Token": token})
    assert res.status_code == 400
    assert diagnostics(res) == "Body is not valid JSON"
    res = client.post("/fhir/$register", content=nested,
                      headers={"Content-Type": "application/fhir+json"})
    assert res.status_code == 400


def test_a_body_that_is_not_parameters_is_rejected(client):
    for body in ("[1, 2, 3]", '{"resourceType": "Patient"}', "{}"):
        res = client.post("/fhir/$register", content=body,
                          headers={"Content-Type": "application/fhir+json"})
        assert res.status_code == 400, body
        assert diagnostics(res) == "Body must be a FHIR Parameters resource"


# ------------------------------------------------------------ registration code

def test_registration_is_open_by_default(client):
    assert app_module.REGISTER_CODE == ""
    assert register_res(client, "Alpha").status_code == 200


def test_registration_code_gates_when_set(client, monkeypatch):
    monkeypatch.setattr(app_module, "REGISTER_CODE", "s3cret")
    assert register_res(client, "Alpha").status_code == 403
    assert register_res(client, "Alpha", "wrong").status_code == 403
    # a non-string code must be refused, not compared
    assert client.post("/fhir/$register", json={"resourceType": "Parameters",
        "parameter": [{"name": "name", "valueString": "Alpha"},
                      {"name": "code", "valueInteger": 1234}]}).status_code == 403
    assert register_res(client, "Alpha", "s3cret").status_code == 200


# --------------------------------------------------------- unreadable document

def test_a_document_that_no_longer_parses_says_so(client, monkeypatch):
    token = register(client, "Alpha")
    submit(client, token, outcome(), monkeypatch, body=CLINICAL_BODY)
    sub = param_entries(client.get("/fhir/$submissions",
                                   params={"organization": "alpha"}))[0]
    assert client.get("/ui/documents/%d" % sub["id"]).json()["bundle_readable"]
    team_dir = os.path.join(client.data_dir, "submissions", "alpha")
    stored = os.path.join(team_dir, os.listdir(team_dir)[0])
    with open(stored, "wb") as fh:
        fh.write(b"{ this is not json")
    data = client.get("/ui/documents/%d" % sub["id"]).json()
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
    app_module.validate_with_matchbox(b'{"resourceType":"Bundle"}')
    assert "igs=hl7se.fhir.base%231.1.0" in captured[0][0].full_url


def test_each_category_is_validated_in_its_own_engine(monkeypatch):
    """The packages are the category's, not one pile shared by all of them.

    Matchbox scopes an engine to the requested profile's dependency closure, so
    a lab report checked by an EPS-shaped engine comes back with its
    conformance unchecked rather than checked and failed - a silent pass.
    """
    for doc in app_module.EHDS_DOCUMENTS:
        captured = []
        fake_urlopen(monkeypatch, FakeResponse(OK_OUTCOME), captured)
        app_module.validate_with_matchbox(b'{"resourceType":"Bundle"}', doc)
        url = captured[0][0].full_url
        assert doc["profile"].replace(":", "%3A").replace("/", "%2F") in url, doc["id"]
        for ig in doc["igs"]:
            assert "igs=" + ig.replace("#", "%23") in url, (doc["id"], ig)
        # and nothing from a category this bundle is not
        for other in app_module.EHDS_DOCUMENTS:
            for ig in other["igs"]:
                if ig not in doc["igs"]:
                    assert "igs=" + ig.replace("#", "%23") not in url, (doc["id"], ig)


def test_the_swedish_base_rides_on_the_patient_summary_only(monkeypatch):
    """It is a Swedish event and teams claim SEBasePatient - but those profiles
    have nothing to say about a lab report or an imaging study, and every extra
    package in an engine is load time on the first submission of that kind."""
    assert "hl7se.fhir.base#1.1.0" in app_module.PRIMARY_DOCUMENT["igs"]
    for doc in app_module.SECONDARY_DOCUMENTS:
        assert "hl7se.fhir.base#1.1.0" not in doc["igs"], doc["id"]


def test_the_health_check_covers_every_category(monkeypatch):
    """VALIDATION_IGS is what $status insists Matchbox loaded and what the
    deploy gates on. A category missing from it is one whose profile claims
    come back "not checked", scoring real submissions wrong."""
    for doc in app_module.EHDS_DOCUMENTS:
        for ig in doc["igs"]:
            assert ig in app_module.DEFAULT_VALIDATION_IGS, (doc["id"], ig)
    # de-duplicated, and the patient summary's packages come first
    assert len(app_module.DEFAULT_VALIDATION_IGS) == len(
        set(app_module.DEFAULT_VALIDATION_IGS))
    assert app_module.DEFAULT_VALIDATION_IGS[:2] == app_module.PRIMARY_DOCUMENT["igs"]


def test_every_category_the_registry_names_is_a_package_the_validator_loads():
    """The one invariant adding or removing a category has to satisfy by hand.

    EHDS_DOCUMENTS and matchbox/config/application.yaml are edited separately,
    and a category named in the registry with no package in the yaml validates
    against nothing and scores as if it had passed - the silent failure the
    yaml's own comment warns about. Until now only /fhir/$status caught it,
    which needs a live Matchbox and a ~160s cold boot, so the answer arrived at
    deploy time rather than here.

    Asserted directionally, not as set equality, because one legitimate
    asymmetry exists: hl7.fhir.eu.base#2.0.0-ballot is in laboratory's igs and
    has no yaml entry of its own - it arrives transitively through imaging.
    """
    yaml_path = os.path.join(os.path.dirname(os.path.abspath(app_module.__file__)),
                             os.pardir, "matchbox", "config", "application.yaml")
    with open(yaml_path, encoding="utf-8") as fh:
        # Deliberately not a YAML parse: the file is a fixed two-line-per-entry
        # shape and the suite has no yaml dependency.
        body = [ln.split("#")[0].strip() for ln in fh]
    packages = set()
    name = None
    for line in body:
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("version:") and name:
            packages.add("%s#%s" % (name, line.split(":", 1)[1].strip()))
            name = None
    assert packages, "parsed no packages out of application.yaml"

    # Every package the validator loads is one the service knows about, so a
    # package added to the yaml and forgotten in the registry is caught.
    for package in packages:
        assert package in app_module.DEFAULT_VALIDATION_IGS, package
    # And every category's own guide has a package, which is the reverse - a
    # category added to the registry and forgotten in the yaml.
    for doc in app_module.EHDS_DOCUMENTS:
        assert doc["igs"][0] in packages, doc["id"]


def test_no_two_categories_claim_the_same_profile():
    """detect_document and the ?profile= override both resolve by scanning the
    registry for a profile match, so a duplicate would silently shadow."""
    profiles = [doc["profile"] for doc in app_module.EHDS_DOCUMENTS]
    assert len(profiles) == len(set(profiles)), profiles


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
    def boom(body, doc=None):
        raise app_module.MatchboxError("Matchbox unreachable: nope")
    monkeypatch.setattr(app_module, "validate_with_matchbox", boom)
    res = client.post("/fhir/Bundle/$validate", content=BUNDLE, headers={"X-Team-Token": token})
    assert res.status_code == 502
    assert "unreachable" in diagnostics(res)


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
    res = client.get("/fhir/$status")
    assert param_one(res, "matchbox") == "up"
    assert param_entries(res, "missingIg") == []
    # the UI reads these to warn before a doomed request
    assert param_one(res, "maxBundleBytes") == 5 * 1024 * 1024
    assert param_one(res, "maxImageBytes") == 2 * 1024 * 1024
    assert param_one(res, "maxNoteChars") == app_module.MAX_NOTE_CHARS
    assert param_one(res, "registerCodeRequired") is False
    fake_urlopen(monkeypatch, FakeResponse(b"{}", status=503))
    assert param_one(client.get("/fhir/$status"), "matchbox") == "down"
    fake_urlopen(monkeypatch, urllib.error.URLError("refused"))
    res = client.get("/fhir/$status")
    # the service itself is still answering - only the validator is down
    assert res.status_code == 200
    assert param_one(res, "matchbox") == "down"


def test_health_is_degraded_when_a_validation_ig_is_not_loaded(client, monkeypatch):
    """Matchbox reads application.yaml at boot; a package added later is
    silently absent and every claim from it goes unchecked. That is a state
    worth a name of its own, not "up"."""
    matchbox_alive(monkeypatch, FakeResponse(ig_bundle("hl7.fhir.eu.eps#1.0.0-ballot")))
    res = client.get("/fhir/$status")
    assert param_one(res, "matchbox") == "degraded"
    assert param_entries(res, "missingIg") == list(app_module.VALIDATION_IGS)


def test_health_is_degraded_when_matchbox_loaded_nothing(client, monkeypatch):
    """A searchset with zero matches carries no "entry" at all. That is the
    answer "nothing is loaded" - the exact state this probe exists to catch -
    not an unreadable reply to shrug at."""
    matchbox_alive(monkeypatch, FakeResponse(json.dumps(
        {"resourceType": "Bundle", "type": "searchset", "total": 0}).encode()))
    res = client.get("/fhir/$status")
    assert param_one(res, "matchbox") == "degraded"
    assert param_entries(res, "missingIg") == list(app_module.VALIDATION_IGS)
    # a reply that is not a Bundle at all is still "unknown", so stays "up"
    matchbox_alive(monkeypatch, FakeResponse(json.dumps(
        {"resourceType": "OperationOutcome", "issue": []}).encode()))
    assert param_one(client.get("/fhir/$status"), "matchbox") == "up"


def test_health_accepts_a_package_listed_without_a_version(client, monkeypatch):
    package = app_module.VALIDATION_IGS[0].partition("#")[0]
    matchbox_alive(monkeypatch, FakeResponse(json.dumps({
        "resourceType": "Bundle",
        "entry": [{"resource": {"resourceType": "ImplementationGuide",
                                "packageId": package}}]}).encode()))
    # a versionless listing cannot be checked, so it is trusted rather than
    # reported missing
    assert param_entries(client.get("/fhir/$status"), "missingIg") == []


def test_health_stays_up_when_the_ig_list_is_unreadable(client, monkeypatch):
    matchbox_alive(monkeypatch, FakeResponse(b"not json"))
    assert param_one(client.get("/fhir/$status"), "matchbox") == "up"
    matchbox_alive(monkeypatch, urllib.error.URLError("search timed out"))
    assert param_one(client.get("/fhir/$status"), "matchbox") == "up"
    matchbox_alive(monkeypatch, FakeResponse(b"[1, 2]"))
    assert param_one(client.get("/fhir/$status"), "matchbox") == "up"


def test_health_reports_a_register_code_requirement(client, monkeypatch):
    monkeypatch.setattr(app_module, "REGISTER_CODE", "sesame")
    fake_urlopen(monkeypatch, urllib.error.URLError("refused"))
    assert param_one(client.get("/fhir/$status"), "registerCodeRequired") is True


def test_health_survives_a_half_open_connection(client, monkeypatch):
    fake_urlopen(monkeypatch, http.client.RemoteDisconnected("gone mid-handshake"))
    assert param_one(client.get("/fhir/$status"), "matchbox") == "down"


# ------------------------------------------------------------ FHIR surface

def test_metadata_describes_the_whole_api(client):
    """A client pointed at the base URL discovers the API from here, so what
    the CapabilityStatement lists has to be what the server actually serves."""
    res = client.get("/fhir/metadata")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/fhir+json")
    cs = res.json()
    assert cs["resourceType"] == "CapabilityStatement"
    assert cs["fhirVersion"] == "4.0.1"
    rest = cs["rest"][0]
    assert {r["type"] for r in rest["resource"]} == {
        "Bundle", "Organization", "DocumentReference", "Binary", "Endpoint"}
    assert {op["name"] for op in rest["operation"]} == {
        "register", "leaderboard", "submissions", "samples", "exchange",
        "documents", "status"}
    # every server-level operation it advertises is really there
    for op in rest["operation"]:
        assert client.get("/fhir/$" + op["name"]).status_code in (200, 405), op
    # the Bundle it validates is pinned to the profile teams are scored against,
    # and the secondary categories ride along as supportedProfile - the
    # conformance resource's own way of saying "this one is what we are about"
    bundles = next(r for r in rest["resource"] if r["type"] == "Bundle")
    assert bundles["profile"] == app_module.PROFILE
    assert bundles["profile"] == app_module.PRIMARY_DOCUMENT["profile"]
    assert bundles["supportedProfile"] == [
        d["profile"] for d in app_module.SECONDARY_DOCUMENTS]
    assert app_module.PROFILE not in bundles["supportedProfile"]
    assert [o["name"] for o in bundles["operation"]] == ["validate"]


def test_every_error_is_an_operation_outcome(client, monkeypatch):
    """FHIR has one way to say what went wrong, and a client that can read a
    validation failure must not need a second parser for a 404."""
    cases = [
        client.get("/fhir/Bundle/999"),                       # 404
        client.get("/fhir/Organization/$me"),                 # 401
        client.get("/fhir/Organization/ghost/$document"),     # 404
        client.post("/fhir/$register", json={"resourceType": "Patient"}),  # 400
        client.get("/fhir/nonsense"),                         # unrouted
        client.request("DELETE", "/fhir/Bundle/1"),           # 405
        client.get("/fhir/$submissions?count=ten"),           # 422, bad query
    ]
    for res in cases:
        assert res.status_code >= 400
        assert res.headers["content-type"].startswith("application/fhir+json")
        body = res.json()
        assert body["resourceType"] == "OperationOutcome"
        assert body["issue"][0]["severity"] == "error"
        assert body["issue"][0]["code"]
        assert body["issue"][0]["diagnostics"]


def test_validate_points_at_the_bundle_it_stored(client, monkeypatch):
    """$validate answers with an OperationOutcome, so the thing it validated
    is named in Content-Location rather than in a body field."""
    token = register(client, "Alpha")
    res = submit(client, token, outcome(), monkeypatch)
    assert res.status_code == 200
    assert res.json()["resourceType"] == "OperationOutcome"
    stored = res.headers["content-location"]
    assert stored.startswith("/fhir/Bundle/")
    assert client.get(stored).json()["resourceType"] == "Bundle"


def test_one_id_space_for_submissions_and_samples(client, monkeypatch):
    """Bundle ids are numeric for submissions and slugs for samples, so the
    two cannot collide and one read serves both."""
    token = register(client, "Alpha")
    submit(client, token, outcome(), monkeypatch)
    numeric = param_entries(client.get("/fhir/$submissions"))[0]["id"]
    assert client.get("/fhir/Bundle/%d" % numeric).json()["resourceType"] == "Bundle"
    assert client.get("/fhir/Bundle/arne-complex-care").json()["resourceType"] \
        == "Bundle"


def test_organizations_are_searchable(client):
    register(client, "Beta")
    register(client, "Alpha")
    body = client.get("/fhir/Organization").json()
    assert body["resourceType"] == "Bundle" and body["type"] == "searchset"
    assert body["total"] == 2
    orgs = [e["resource"] for e in body["entry"]]
    assert [o["name"] for o in orgs] == ["Alpha", "Beta"]      # by name
    # and each one reads back at its own id
    assert client.get("/fhir/Organization/alpha").json()["name"] == "Alpha"
    assert client.get("/fhir/Organization/ghost").status_code == 404


def test_organizations_are_searchable_by_name(client):
    register(client, "Åland Hälsa")
    register(client, "Nordic Health")

    def names(query):
        body = client.get("/fhir/Organization?" + query).json()
        assert body["total"] == len(body["entry"])
        return [e["resource"]["name"] for e in body["entry"]]

    assert names("name=aland") == ["Åland Hälsa"]          # case and accents
    assert names("name=health") == []                      # start of the name
    assert names("name:contains=HEAL") == ["Nordic Health"]
    assert names("name:contains=h") == ["Nordic Health", "Åland Hälsa"]
    assert names("name:exact=Nordic Health") == ["Nordic Health"]
    assert names("name:exact=nordic health") == []
    assert names("name=nobody") == []
    assert names("name=") == ["Nordic Health", "Åland Hälsa"]   # same as not asking
    # repeats are ANDed, commas are ORed, and \\, is a comma in the name
    assert names("name=nordic,aland") == ["Nordic Health", "Åland Hälsa"]
    assert names("name=nordic&name=aland") == []
    assert names("name=n&name:contains=heal") == ["Nordic Health"]
    assert names("name:exact=Nobody,Nordic Health") == ["Nordic Health"]


def test_a_name_search_in_another_script_does_not_match_everyone(client):
    """Folding to ASCII turned such a term into nothing, and nothing is the
    start of every name."""
    register(client, "Nordic Health")
    register(client, "日本ヘルス")
    register(client, "Acme, Inc")

    def names(query):
        body = client.get("/fhir/Organization", params=query).json()
        return [e["resource"]["name"] for e in body["entry"]]

    assert names({"name": "日本"}) == ["日本ヘルス"]
    assert names({"name": "中国"}) == []
    assert names({"name:contains": "ヘルス"}) == ["日本ヘルス"]
    assert names({"name:exact": "Acme\\, Inc"}) == ["Acme, Inc"]
    assert names({"name": "acme\\, inc,nordic"}) == ["Acme, Inc", "Nordic Health"]


def test_exchange_filters_by_company_name(client, monkeypatch):
    interop_setup(client, monkeypatch)
    submit(client, register(client, "Åland Hälsa"), outcome(), monkeypatch)

    def listed(query):
        return [p["organizationSlug"]
                for p in param_entries(client.get("/fhir/$exchange" + query))]

    assert listed("") == ["producer", "aland-halsa"]
    assert listed("?name=halsa") == ["aland-halsa"]        # anywhere in the name
    assert listed("?name=PROD") == ["producer"]
    assert listed("?name=nobody") == []
    assert listed("?name=") == ["producer", "aland-halsa"]
    assert listed("?name=aland-halsa") == ["aland-halsa"]  # the slug works too
    assert listed("?name:contains=halsa") == ["aland-halsa"]
    assert listed("?name=prod,halsa") == ["producer", "aland-halsa"]
    assert listed("?name=prod&name=halsa") == []
    assert listed("?name=%E6%97%A5%E6%9C%AC") == []        # not "everything"


def test_a_bearer_token_authenticates_as_well_as_the_header(client, monkeypatch):
    """The UI sends Bearer; the README's curl lines send X-Team-Token. Both
    are the same credential and both have to work."""
    token = register(client, "Alpha")
    set_outcome(monkeypatch, outcome())
    res = client.post("/fhir/Bundle/$validate", content=BUNDLE,
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/fhir+json"})
    assert res.status_code == 200
    assert client.get("/fhir/Organization/$me",
                      headers={"Authorization": "Bearer " + token}).status_code == 200
    assert client.get("/fhir/Organization/$me",
                      headers={"Authorization": "Bearer nope"}).status_code == 401


# ---------------------------------------------------------------- OpenAPI

def test_openapi_describes_the_bodies_fastapi_cannot_see(client):
    """Several endpoints read the request body by hand, so FastAPI has no model
    to introspect and would publish them as taking nothing at all."""
    spec = client.get("/openapi.json").json()
    for path in ("/fhir/$register", "/fhir/Bundle/$validate",
                 "/fhir/DocumentReference"):
        body = spec["paths"][path]["post"]["requestBody"]
        assert "application/fhir+json" in body["content"], path
    assert "/fhir/$confirm" not in spec["paths"]


def test_openapi_documents_the_score_headers(client):
    """The verdict rides on headers, so a generated client that only sees the
    body would have no way to read the score."""
    ok = client.get("/openapi.json").json()["paths"]["/fhir/Bundle/$validate"]["post"]
    headers = ok["responses"]["200"]["headers"]
    assert set(headers) == {"X-Pass", "X-Score", "X-Submission-Score", "X-Errors",
                            "X-Sections", "X-Resources", "X-Document-Type",
                            "X-Document-Profile", "X-Secondary-Categories",
                            "X-Best-Patient-Summary", "X-First-Pass",
                            "Content-Location"}


def test_openapi_declares_the_team_token_as_a_security_scheme(client):
    spec = client.get("/openapi.json").json()
    scheme = spec["components"]["securitySchemes"]["TeamToken"]
    assert scheme["in"] == "header" and scheme["name"] == "X-Team-Token"
    # submitting is the only endpoint that authenticates; the exchange corpus
    # is open, so it carries no security requirement at all
    assert spec["paths"]["/fhir/Bundle/$validate"]["post"]["security"] == [{"TeamToken": []}]
    assert "security" not in spec["paths"]["/fhir/$exchange"]["get"]
    # ...and the raw header parameters are not listed anywhere either
    names = [p["name"].lower()
             for p in spec["paths"]["/fhir/$exchange"]["get"].get("parameters", [])]
    assert "x-team-token" not in names and "authorization" not in names


def test_openapi_leaves_the_ui_out(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/" not in paths and "/favicon.svg" not in paths
    assert all(p.startswith("/fhir/") for p in paths)
    # the clinician view model is a rendering, not API - it stays out too
    assert not any(p.startswith("/ui/") for p in paths)


def test_index_links_the_machine_readable_api(client):
    text = client.get("/").text
    assert 'href="/openapi.json"' in text


# ------------------------------------------- leaderboard -> clinical view

def test_leaderboard_carries_the_slug_its_rows_link_with(client, monkeypatch):
    tok = register(client, "Alpha Team")
    submit(client, tok, outcome(), monkeypatch)
    row = param_entries(client.get("/fhir/$leaderboard"))[0]
    assert row["organizationSlug"] == "alpha-team"
    # the slug is what $submissions groups by, so the link lands somewhere
    subs = param_entries(client.get("/fhir/$submissions"))
    assert subs[0]["organizationSlug"] == row["organizationSlug"]


def test_index_renders_an_idle_company_without_undefineds(client):
    """FHIR omits an empty element rather than sending null, so a company that
    has never submitted arrives with no bestSections part at all. Read straight
    into the row template that becomes the string "undefined" on the board."""
    text = client.get("/").text
    assert "function leaderboardRow(" in text
    assert "v === undefined ? null : v" in text
    # and the template still asks the question the normaliser answers
    assert "r.best_sections === null" in text


def test_index_builds_the_company_picker_from_a_parameters_body(client):
    """$leaderboard is a Parameters resource, not the JSON array the page was
    written against, so the company picker has to unwrap it the same way the
    board does. Calling .filter on the resource threw, the throw was caught,
    and the Clinical view offered no companies at all."""
    text = client.get("/").text
    assert ('teams = paramEntries(await res.json(), "entry")\n'
            "        .map(leaderboardRow).filter((t) => t.attempts > 0);") in text
    # and the option list still reads the names the normaliser produces
    assert '\'<option value="\' + esc(t.team_slug) + \'">\' + esc(t.team)' in text


def test_index_says_so_when_the_company_list_cannot_be_loaded(client):
    """An empty picker with no message reads as "nobody has submitted", which
    is how the bug above stayed invisible."""
    text = client.get("/").text
    assert "Could not load the list of " in text


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
    assert '\'" download>download the FHIR bundle</a>\'' in text


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
                        lambda body, doc=None: (_ for _ in ()).throw(app_module.MatchboxError("down")))
    res = client.post("/fhir/Bundle/$validate", content=BUNDLE, headers={"X-Team-Token": tok})
    assert res.status_code == 502
    audit_root = os.path.join(client.data_dir, "submissions")
    assert [f for _, _, files in os.walk(audit_root) for f in files] == []
    assert param_entries(client.get("/fhir/$submissions")) == []
    (row,) = param_entries(client.get("/fhir/$leaderboard"))
    assert row["attempts"] == 0


def test_non_object_json_body_is_rejected_before_validation(client, monkeypatch):
    tok = register(client, "Listy")
    called = []
    monkeypatch.setattr(app_module, "validate_with_matchbox",
                        lambda body: called.append(body) or outcome())
    for body in (b"[1, 2, 3]", b'"a bundle"', b"42", b"null"):
        res = client.post("/fhir/Bundle/$validate", content=body, headers={"X-Team-Token": tok})
        assert res.status_code == 400, body
        assert "object" in diagnostics(res)
    assert called == []
    assert param_entries(client.get("/fhir/$leaderboard"))[0]["attempts"] == 0


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
    res = register_res(client, "  Alpha \t Team  ")
    assert res.status_code == 200
    assert param_one(res, "organization")["name"] == "Alpha Team"
    for clash in ("alpha team", "ALPHA   TEAM", " Alpha Team"):
        assert register_res(client, clash).status_code == 409, clash


def test_register_rejects_non_string_and_missing_names(client):
    bodies = [
        {"resourceType": "Parameters",
         "parameter": [{"name": "name", "valueInteger": 42}]},
        {"resourceType": "Parameters",
         "parameter": [{"name": "name"}]},           # a part with no value[x]
        {"resourceType": "Parameters", "parameter": []},
        {"resourceType": "Parameters"},
        {"resourceType": "Parameters",              # the wrong part name
         "parameter": [{"name": "team", "valueString": "x"}]},
    ]
    for body in bodies:
        assert client.post("/fhir/$register", json=body).status_code == 400, body


def test_register_code_gate(client, monkeypatch):
    monkeypatch.setattr(app_module, "REGISTER_CODE", "sesame")
    assert register_res(client, "Gated").status_code == 403
    assert register_res(client, "Gated", "wrong").status_code == 403
    assert client.post("/fhir/$register", json={"resourceType": "Parameters",
        "parameter": [{"name": "name", "valueString": "Gated"},
                      {"name": "code", "valueInteger": 123}]}).status_code == 403
    assert register_res(client, "Gated", "sesame").status_code == 200
    # the wrong-code attempts did not reserve the name
    assert param_entries(client.get("/fhir/$leaderboard"))[0]["organization"] == "Gated"


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
        assert register_res(client, name).status_code == 200, name
    slugs = [r["organizationSlug"]
             for r in param_entries(client.get("/fhir/$leaderboard"))]
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
    # The standing still stands on the richer bundle; this one scored 50.
    assert plain.headers["X-Score"] == "74"
    assert plain.headers["X-Submission-Score"] == "50"
    rich_id = param_entries(
        client.get("/fhir/$submissions?organization=best-foot"))[-1]["id"]
    (entry,) = [e for e in param_entries(client.get("/fhir/$exchange")) if e["organization"] == "Best Foot"]
    assert entry["id"] == rich_id
    res = client.get("/fhir/Organization/best-foot/$document")
    assert res.status_code == 200
    assert res.headers["content-location"] == "/fhir/Bundle/%d" % rich_id
    assert res.headers["X-Producer"] == "best-foot"
    assert json.loads(res.content) == json.loads(bundle(sections=2, resources=2))


def test_submissions_limit_bounds(client, monkeypatch):
    tok = register(client, "Bounded")
    for _ in range(3):
        submit(client, tok, outcome(), monkeypatch)
    def n(query):
        return len(param_entries(client.get("/fhir/$submissions?" + query)))

    assert n("count=2") == 2
    assert n("count=1000") == 3
    # out-of-range counts are clamped to 1..1000, as the README says
    assert n("count=0") == 1
    assert n("count=-5") == 1
    assert n("count=1001") == 3
    assert client.get("/fhir/$submissions?count=ten").status_code == 422
    assert param_entries(client.get("/fhir/$submissions?organization=nobody")) == []


def test_missing_samples_dir_degrades_to_unavailable(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "SAMPLES_DIR", str(tmp_path / "nowhere"))
    listing = param_entries(client.get("/fhir/$samples"))
    assert listing, "the catalogue itself is static"
    assert all(s["available"] is False for s in listing)
    sample_id = listing[0]["id"]
    assert client.get(f"/ui/documents/{sample_id}").status_code == 404
    assert client.get(f"/fhir/Bundle/{sample_id}").status_code == 404


def test_sample_ids_cannot_escape_the_samples_dir(client):
    assert app_module.read_sample_bytes("../app") is None
    assert app_module.read_sample_bytes("..") is None
    assert app_module.read_sample_bytes("") is None
    for bad in ("..%2Fapp", "%2e%2e", "..%2F..%2Fapp.py"):
        assert client.get(f"/ui/documents/{bad}").status_code == 404, bad
        assert client.get(f"/fhir/Bundle/{bad}").status_code == 404, bad


def test_summariser_crash_does_not_take_the_detail_endpoint_down(client, monkeypatch):
    tok = register(client, "Fragile")
    assert submit(client, tok, outcome(), monkeypatch).status_code == 200
    sub_id = param_entries(client.get("/fhir/$submissions"))[0]["id"]
    real = app_module.summarize_bundle

    def boom(bundle):
        if bundle is None:
            return real(None)
        raise RuntimeError("unexpected shape")

    monkeypatch.setattr(app_module, "summarize_bundle", boom)
    res = client.get(f"/ui/documents/{sub_id}")
    assert res.status_code == 200
    assert res.json()["summary"] == real(None)
    res = client.get("/ui/documents/andrea-cross-border")
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
    summary = client.get("/ui/documents/andrea-cross-border").json()["summary"]
    items = [i for sec in summary["sections"] for i in sec.get("items", [])]
    plans = [i for i in items if (i.get("detail") or "").startswith("scheduled ")]
    assert plans, [i.get("label") for i in items]
    plan = plans[0]
    assert plan["status"] == "cancelled"
    assert "plan since" in plan["detail"]
    assert plan["note"]


def test_device_use_shows_when_the_device_went_in(client):
    summary = client.get("/ui/documents/arne-complex-care").json()["summary"]
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
    assert "/fhir/$status" in text
    assert "/fhir/$leaderboard" in text
    assert "/fhir/$submissions?count=200" not in text  # the picker no longer truncates
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


# ---------------------------------------------------------------- renderings

def png_bytes(w=8, h=8, rgb=(200, 60, 60)):
    """A real PNG, so the sniffer is tested against the format and not a stub."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\x0a"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


_MISSING = object()   # "the element is absent", which is not the same as None
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64


def add_shot(client, token, slug, image=None, note=None):
    body = docref(slug, png_bytes() if image is None else image, note)
    return client.post("/fhir/DocumentReference", json=body,
                       headers={"X-Team-Token": token})


def test_a_rendering_records_who_saw_whose_document(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    res = add_shot(client, tok_c, "producer", note="  Allergies came through empty.  ")
    assert res.status_code == 201
    shot = res.json()
    assert shot["resourceType"] == "DocumentReference"
    # custodian owns the document, author took the picture
    assert shot["custodian"]["reference"] == "Organization/producer"
    assert shot["custodian"]["display"] == "Producer"
    assert shot["author"][0]["reference"] == "Organization/consumer"
    assert shot["author"][0]["display"] == "Consumer"
    assert shot["description"] == "Allergies came through empty."   # stripped
    assert shot["content"][0]["attachment"]["url"] == "/fhir/Binary/%s" % shot["id"]
    # the Location header points at the resource that was just created
    assert res.headers["location"] == "/fhir/DocumentReference/%s" % shot["id"]
    # the document is the producer's published submission, not just its slug
    assert related_bundle(shot) == param_entries(client.get("/fhir/$exchange"))[0]["id"]
    # and the bytes are on disk under the uploader
    saved = os.listdir(os.path.join(client.data_dir, "renderings", "consumer"))
    assert len(saved) == 1 and saved[0].endswith(".png")


def test_every_accepted_format_is_sniffed_not_declared(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    for image, ext in ((png_bytes(), ".png"), (JPEG, ".jpg"), (WEBP, ".webp")):
        res = add_shot(client, tok_c, "producer", image=image)
        assert res.status_code == 201, res.text
    saved = os.listdir(os.path.join(client.data_dir, "renderings", "consumer"))
    assert sorted(os.path.splitext(f)[1] for f in saved) == [".jpg", ".png", ".webp"]


def test_a_file_that_is_not_an_image_is_refused(client, monkeypatch):
    """The declared Content-Type is not evidence: HTML must never land here and
    be served back as a page from the same origin as everyone's token."""
    tok_p, tok_c = interop_setup(client, monkeypatch)
    res = add_shot(client, tok_c, "producer",
                   image=b"<html><script>alert(1)</script></html>")
    assert res.status_code == 400
    assert "PNG, JPEG or WebP" in diagnostics(res)
    assert not os.path.exists(os.path.join(client.data_dir, "renderings"))


def test_an_attachment_that_is_not_base64_is_refused(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    body = docref("producer", png_bytes())
    body["content"][0]["attachment"]["data"] = "not base64 at all!!"
    res = client.post("/fhir/DocumentReference", json=body,
                      headers={"X-Team-Token": tok_c})
    assert res.status_code == 400
    assert "base64" in diagnostics(res)
    # and so is a DocumentReference with no attachment to decode
    body["content"] = [{"attachment": {"contentType": "image/png"}}]
    res = client.post("/fhir/DocumentReference", json=body,
                      headers={"X-Team-Token": tok_c})
    assert res.status_code == 400


def test_a_body_that_is_not_a_documentreference_is_refused(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    res = client.post("/fhir/DocumentReference", json={"resourceType": "Media"},
                      headers={"X-Team-Token": tok_c})
    assert res.status_code == 400
    assert diagnostics(res) == "Body must be a FHIR DocumentReference"


def test_a_screenshot_must_name_the_company_it_belongs_to(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    body = docref("producer", png_bytes())
    del body["custodian"]
    res = client.post("/fhir/DocumentReference", json=body,
                      headers={"X-Team-Token": tok_c})
    assert res.status_code == 400
    assert "custodian" in diagnostics(res)


def test_you_cannot_render_your_own_document(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    res = add_shot(client, tok_p, "producer")
    assert res.status_code == 400
    assert "your own document" in diagnostics(res)


def test_rendering_a_document_that_does_not_exist(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    assert add_shot(client, tok_c, "ghost").status_code == 404
    # registered, but has published nothing to render
    tok_x = register(client, "Silent")
    assert add_shot(client, tok_x, "consumer").status_code == 404


def test_uploading_a_rendering_needs_a_token(client, monkeypatch):
    interop_setup(client, monkeypatch)
    res = client.post("/fhir/DocumentReference",
                      json=docref("producer", png_bytes()))
    assert res.status_code == 401


def test_an_oversized_screenshot_is_refused_before_it_is_resident(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    monkeypatch.setattr(app_module, "MAX_IMAGE_BYTES", 1024)
    res = add_shot(client, tok_c, "producer", image=png_bytes() + b"\x00" * 4096)
    assert res.status_code == 413
    assert "limit is 1024 bytes" in diagnostics(res)


def test_a_runaway_note_is_truncated_not_refused(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    res = add_shot(client, tok_c, "producer", note="x" * (app_module.MAX_NOTE_CHARS + 500))
    assert res.status_code == 201
    assert len(res.json()["description"]) == app_module.MAX_NOTE_CHARS


def test_renderings_list_filters_by_document_producer_and_consumer(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    tok_d = register(client, "Third")
    add_shot(client, tok_c, "producer", note="from consumer")
    add_shot(client, tok_d, "producer", note="from third")
    doc = param_entries(client.get("/fhir/$exchange"))[0]["id"]

    everything = shots(client.get("/fhir/DocumentReference"))
    assert len(everything) == 2
    assert everything[0]["date"] >= everything[1]["date"]      # newest first
    assert client.get("/fhir/DocumentReference").json()["total"] == 2
    assert len(shots(client.get("/fhir/DocumentReference?related=%d" % doc))) == 2
    assert len(shots(client.get("/fhir/DocumentReference?custodian=producer"))) == 2
    assert [d["description"]
            for d in shots(client.get("/fhir/DocumentReference?author=third"))] \
        == ["from third"]
    assert shots(client.get("/fhir/DocumentReference?custodian=nobody")) == []
    assert shots(client.get("/fhir/DocumentReference?related=99999")) == []


def test_the_exchange_list_counts_the_renderings(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    assert param_entries(client.get("/fhir/$exchange"))[0]["renderings"] == 0
    add_shot(client, tok_c, "producer")
    assert param_entries(client.get("/fhir/$exchange"))[0]["renderings"] == 1


def test_the_image_is_served_inert_and_as_the_type_its_bytes_proved(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    sid = add_shot(client, tok_c, "producer", image=JPEG).json()["id"]
    res = client.get("/fhir/Binary/%s" % sid)
    assert res.status_code == 200
    assert res.content == JPEG
    assert res.headers["content-type"] == "image/jpeg"       # not the declared image/png
    assert res.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in res.headers["content-security-policy"]
    # already compressed: gzipping it again would be CPU for nothing
    assert res.headers["content-encoding"] == "identity"
    assert client.get("/fhir/Binary/99999").status_code == 404


def test_a_rendering_stays_pinned_to_the_document_it_was_taken_from(client, monkeypatch):
    """A team's exchange document moves the moment it beats its own score. A
    screenshot has to keep pointing at the bytes somebody actually opened."""
    tok_p, tok_c = interop_setup(client, monkeypatch)
    first_doc = param_entries(client.get("/fhir/$exchange"))[0]["id"]
    sid = add_shot(client, tok_c, "producer").json()["id"]
    # the producer publishes something better; the exchange document changes
    submit(client, tok_p, outcome(), monkeypatch,
           body=bundle(sections=4, resources=10))
    second_doc = param_entries(client.get("/fhir/$exchange"))[0]["id"]
    assert second_doc != first_doc
    shot = [d for d in shots(client.get("/fhir/DocumentReference"))
            if d["id"] == sid][0]
    assert related_bundle(shot) == first_doc
    assert param_entries(client.get("/fhir/$exchange"))[0]["renderings"] == 0   # of the new one


def test_only_the_uploader_can_rewrite_the_note(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    tok_d = register(client, "Third")
    sid = add_shot(client, tok_c, "producer", note="first take").json()["id"]

    def put(token, note):
        body = {"resourceType": "DocumentReference", "status": "current"}
        if note is not _MISSING:
            body["description"] = note
        return client.put("/fhir/DocumentReference/%s" % sid, json=body,
                          headers={"X-Team-Token": token})

    assert put(tok_c, "  clearer take  ").json()["description"] == "clearer take"
    assert put(tok_d, "hijack").status_code == 403
    assert put(tok_c, _MISSING).status_code == 400
    assert put(tok_c, 7).status_code == 400
    assert client.put("/fhir/DocumentReference/%s" % sid,
                      json={"resourceType": "DocumentReference",
                            "description": "x"}).status_code == 401
    assert client.put("/fhir/DocumentReference/99999",
                      json={"resourceType": "DocumentReference", "description": "x"},
                      headers={"X-Team-Token": tok_c}).status_code == 404
    # the last accepted write is what everyone reads
    assert shots(client.get("/fhir/DocumentReference"))[0]["description"] \
        == "clearer take"


def test_withdrawing_a_rendering_takes_the_file_with_it(client, monkeypatch):
    tok_p, tok_c = interop_setup(client, monkeypatch)
    tok_d = register(client, "Third")
    sid = add_shot(client, tok_c, "producer").json()["id"]
    shot_dir = os.path.join(client.data_dir, "renderings", "consumer")
    assert len(os.listdir(shot_dir)) == 1

    assert client.delete("/fhir/DocumentReference/%s" % sid,
                         headers={"X-Team-Token": tok_d}).status_code == 403
    assert client.delete("/fhir/DocumentReference/%s" % sid).status_code == 401
    assert len(os.listdir(shot_dir)) == 1          # nothing removed by a refused call

    res = client.delete("/fhir/DocumentReference/%s" % sid,
                        headers={"X-Team-Token": tok_c})
    assert res.status_code == 204 and res.content == b""
    assert os.listdir(shot_dir) == []              # a withdrawn screenshot is gone
    assert shots(client.get("/fhir/DocumentReference")) == []
    assert client.get("/fhir/Binary/%s" % sid).status_code == 404
    assert client.delete("/fhir/DocumentReference/%s" % sid,
                         headers={"X-Team-Token": tok_c}).status_code == 404


def test_renderings_are_readable_without_a_token(client, monkeypatch):
    """The wall is for everyone in the room to read, participant or not."""
    tok_p, tok_c = interop_setup(client, monkeypatch)
    sid = add_shot(client, tok_c, "producer").json()["id"]
    for headers in ({}, {"X-Team-Token": "not-a-real-token"}):
        assert client.get("/fhir/DocumentReference", headers=headers).status_code == 200
        assert client.get("/fhir/Binary/%s" % sid,
                          headers=headers).status_code == 200


# ------------------------------------------------------------ endpoints

DESC_EXT = app_module.ENDPOINT_DESCRIPTION_EXT


def endpoint(address="https://fhir.example.org/r4", kind="hl7-fhir-rest", **extra):
    body = {"resourceType": "Endpoint", "status": "active",
            "connectionType": {"code": kind}, "address": address}
    body.update(extra)
    return body


def advertise(client, token, body, endpoint_id=None):
    headers = {"Authorization": "Bearer " + token,
               "Content-Type": "application/fhir+json"}
    if endpoint_id is None:
        return client.post("/fhir/Endpoint", json=body, headers=headers)
    return client.put("/fhir/Endpoint/%s" % endpoint_id, json=body, headers=headers)


def test_an_endpoint_says_whose_it_is_and_how_to_send_to_it(client):
    tok = register(client, "Åland Hälsa")
    res = advertise(client, tok, endpoint(
        name="  Staging   server ",
        managingOrganization={"reference": "Organization/someone-else"},
        extension=[{"url": DESC_EXT, "valueString": " POST a Bundle to /Bundle\n"}]))
    assert res.status_code == 201, res.text
    ep = res.json()
    assert res.headers["location"] == "/fhir/Endpoint/" + ep["id"]
    # the company is the token's, not whatever the body claimed
    assert ep["managingOrganization"] == {"reference": "Organization/aland-halsa",
                                          "display": "Åland Hälsa"}
    assert ep["connectionType"] == {
        "system": "http://terminology.hl7.org/CodeSystem/endpoint-connection-type",
        "code": "hl7-fhir-rest", "display": "HL7 FHIR"}
    assert ep["name"] == "Staging server"
    assert ep["address"] == "https://fhir.example.org/r4"
    assert ep["extension"] == [{"url": DESC_EXT,
                                "valueString": "POST a Bundle to /Bundle"}]
    assert ep["payloadType"][0]["coding"][0]["code"] == "any"
    # reads are open: finding it is the point
    assert client.get("/fhir/Endpoint/" + ep["id"]).json() == ep
    org = client.get("/fhir/Organization/aland-halsa").json()
    assert org["endpoint"] == [{"reference": "Endpoint/" + ep["id"]}]
    listed = client.get("/fhir/Organization").json()["entry"][0]["resource"]
    assert listed["endpoint"] == org["endpoint"]


def test_an_endpoint_can_be_advertised_at_registration(client):
    body = as_params(name="Early Bird")
    body["parameter"].append({"name": "endpoint", "resource": endpoint(
        "https://docs.example.org", kind="web-page",
        extension=[{"url": DESC_EXT, "valueString": "Read me"}])})
    res = client.post("/fhir/$register", json=body)
    assert res.status_code == 200, res.text
    org = param_one(res, "organization")
    [ref] = org["endpoint"]
    ep = client.get("/fhir/" + ref["reference"]).json()
    assert ep["managingOrganization"]["reference"] == "Organization/early-bird"
    assert ep["address"] == "https://docs.example.org"
    assert ep["extension"][0]["valueString"] == "Read me"


def test_a_bad_endpoint_at_registration_registers_nothing(client):
    """Otherwise the retry with the address fixed is a 409 for the name."""
    body = as_params(name="Typo Health")
    body["parameter"].append({"name": "endpoint",
                              "resource": endpoint("fhir.example.org")})
    res = client.post("/fhir/$register", json=body)
    assert res.status_code == 400 and "http or https" in diagnostics(res)
    body["parameter"][-1]["resource"] = {"resourceType": "Organization"}
    assert "Endpoint resource" in diagnostics(client.post("/fhir/$register", json=body))
    assert client.get("/fhir/Organization").json()["total"] == 0
    assert register(client, "Typo Health")


def test_a_web_page_is_an_endpoint_that_takes_no_payload(client):
    tok = register(client, "Pages")
    ep = advertise(client, tok, endpoint("http://docs.example.org/send",
                                         kind="web-page")).json()
    assert ep["connectionType"]["system"] == (
        app_module.CANONICAL_BASE + "/CodeSystem/endpoint-connection-type")
    assert ep["payloadType"][0]["coding"][0]["code"] == "none"
    assert "name" not in ep and "extension" not in ep


def test_endpoints_are_searched_by_company_and_kind(client):
    a = register(client, "Alpha")
    b = register(client, "Beta")
    advertise(client, b, endpoint(kind="web-page"))
    advertise(client, a, endpoint())
    advertise(client, a, endpoint(kind="web-page"))

    def found(query=""):
        bundle = client.get("/fhir/Endpoint" + query).json()
        assert bundle["type"] == "searchset"
        return [(e["resource"]["managingOrganization"]["display"],
                 e["resource"]["connectionType"]["code"]) for e in bundle["entry"]]

    # by company name, then oldest first
    assert found() == [("Alpha", "hl7-fhir-rest"), ("Alpha", "web-page"),
                       ("Beta", "web-page")]
    assert found("?organization=beta") == [("Beta", "web-page")]
    assert found("?organization=Organization/alpha,beta") == found()
    assert found("?connection-type=web-page") == [
        ("Alpha", "web-page"), ("Beta", "web-page")]
    # a code this server does not have matches nothing rather than everything
    assert found("?connection-type=hl7-fhir-msg") == []
    assert found("?organization=alpha&connection-type="
                 "http://terminology.hl7.org/CodeSystem/endpoint-connection-type"
                 "|hl7-fhir-rest") == [("Alpha", "hl7-fhir-rest")]


def test_an_endpoint_is_changed_only_by_the_company_advertising_it(client):
    mine = register(client, "Mine")
    theirs = register(client, "Theirs")
    ep_id = advertise(client, mine, endpoint(
        extension=[{"url": DESC_EXT, "valueString": "Ask for a token"}])).json()["id"]
    path = "/fhir/Endpoint/" + ep_id

    assert client.post("/fhir/Endpoint", json=endpoint()).status_code == 401
    assert client.put(path, json=endpoint()).status_code == 401
    assert client.delete(path).status_code == 401
    assert advertise(client, theirs, endpoint("https://evil.example.org"),
                     ep_id).status_code == 403
    assert client.delete(path, headers={"Authorization": "Bearer " + theirs}
                         ).status_code == 403
    # a refused write wrote nothing
    kept = client.get(path).json()
    assert kept["address"] == "https://fhir.example.org/r4"
    assert kept["extension"][0]["valueString"] == "Ask for a token"

    res = advertise(client, mine, endpoint("https://new.example.org", kind="web-page",
                                           name="Docs"), ep_id)
    assert res.status_code == 200
    # PUT replaces whole: the description it no longer carries is gone
    assert client.get(path).json()["address"] == "https://new.example.org"
    assert client.get(path).json()["name"] == "Docs"
    assert "extension" not in client.get(path).json()
    assert client.delete(path, headers={"Authorization": "Bearer " + mine}
                         ).status_code == 204
    assert client.get(path).status_code == 404
    assert "endpoint" not in client.get("/fhir/Organization/mine").json()
    assert client.get("/fhir/Endpoint/%d" % 2 ** 63).status_code == 404


def test_a_missing_endpoint_is_404_to_its_would_be_owner_too(client):
    tok = register(client, "Owner")
    assert advertise(client, tok, endpoint(), "999").status_code == 404
    assert client.delete("/fhir/Endpoint/999",
                         headers={"Authorization": "Bearer " + tok}).status_code == 404
    # PUT is not create-on-update here: ids are the server's to hand out
    assert client.get("/fhir/Endpoint").json()["total"] == 0


def test_a_put_whose_body_names_another_endpoint_is_refused(client):
    tok = register(client, "Owner")
    first = advertise(client, tok, endpoint("https://one.example.org")).json()["id"]
    second = advertise(client, tok, endpoint("https://two.example.org")).json()["id"]
    res = advertise(client, tok, endpoint("https://new.example.org", id=second), first)
    assert res.status_code == 400 and "URL's" in diagnostics(res)
    assert client.get("/fhir/Endpoint/" + first).json()["address"] == "https://one.example.org"
    # the matching id is fine
    assert advertise(client, tok, endpoint("https://new.example.org", id=first),
                     first).status_code == 200


def test_an_endpoint_deleted_mid_update_is_404_not_500(client, monkeypatch):
    """A colleague's Remove landing between the ownership check and the write."""
    tok = register(client, "Owner")
    ep_id = advertise(client, tok, endpoint()).json()["id"]
    real = app_module.owned_endpoint

    def racing(conn, endpoint_id, team):
        row = real(conn, endpoint_id, team)
        other = sqlite3.connect(app_module.db_path(), timeout=0.2)
        try:
            other.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))
            other.commit()
        except sqlite3.OperationalError:
            pass  # held off by the writer's lock, which is the point
        finally:
            other.close()
        return row

    monkeypatch.setattr(app_module, "owned_endpoint", racing)
    res = advertise(client, tok, endpoint("https://new.example.org"), ep_id)
    assert res.status_code in (200, 404), res.text


def test_an_endpoint_id_is_never_handed_to_the_next_company(client):
    """Endpoint/N is pasted into other systems; it must not start meaning
    somebody else's address once its owner deletes it."""
    x = register(client, "Xenon")
    advertise(client, x, endpoint())
    newest = advertise(client, x, endpoint()).json()["id"]
    assert client.delete("/fhir/Endpoint/" + newest,
                         headers={"Authorization": "Bearer " + x}).status_code == 204
    theirs = advertise(client, register(client, "Yttrium"), endpoint()).json()["id"]
    assert theirs != newest
    assert client.get("/fhir/Endpoint/" + newest).status_code == 404


def test_whoami_lists_the_companys_endpoints_like_every_other_read(client):
    tok = register(client, "Alpha")
    beta = register(client, "Beta")
    ids = [advertise(client, beta, endpoint()).json()["id"] for _ in range(2)]
    refs = [{"reference": "Endpoint/" + i} for i in ids]
    me = client.get("/fhir/Organization/$me", headers={"Authorization": "Bearer " + beta})
    assert me.json()["endpoint"] == refs
    assert client.get("/fhir/Organization/beta").json()["endpoint"] == refs
    listed = {e["resource"]["id"]: e["resource"]
              for e in client.get("/fhir/Organization").json()["entry"]}
    assert listed["beta"]["endpoint"] == refs and "endpoint" not in listed["alpha"]
    assert "endpoint" not in client.get(
        "/fhir/Organization/$me", headers={"Authorization": "Bearer " + tok}).json()


def test_an_endpoint_that_fails_to_store_at_registration_takes_the_company_with_it(
        client, monkeypatch):
    """Same transaction: otherwise the retry is a 409 for the name."""
    def broken(conn, team_id, fields):
        raise sqlite3.OperationalError("disk I/O error")

    real = app_module.insert_endpoint
    monkeypatch.setattr(app_module, "insert_endpoint", broken)
    body = as_params(name="Unlucky")
    body["parameter"].append({"name": "endpoint", "resource": endpoint()})
    with pytest.raises(sqlite3.OperationalError):
        client.post("/fhir/$register", json=body)
    monkeypatch.setattr(app_module, "insert_endpoint", real)
    assert client.get("/fhir/Organization").json()["total"] == 0
    assert register(client, "Unlucky")


def test_a_name_that_is_taken_gets_no_endpoint_attached(client):
    register(client, "Acme")
    body = as_params(name="acme")
    body["parameter"].append({"name": "endpoint", "resource": endpoint()})
    assert client.post("/fhir/$register", json=body).status_code == 409
    assert "endpoint" not in client.get("/fhir/Organization/acme").json()
    assert client.get("/fhir/Endpoint").json()["total"] == 0


@pytest.mark.parametrize("parts, fragment", [
    # one is dropped otherwise, and the 200 says nothing about which
    ([{"name": "endpoint", "resource": endpoint()},
      {"name": "endpoint", "resource": endpoint(kind="web-page")}], "more than once"),
    ([{"name": "endpoint", "resource": [endpoint()]}], "Endpoint resource"),
    ([{"name": "endpoint", "resource": None}], "Endpoint resource"),
    ([{"name": "endpoint", "Resource": endpoint()}], "no value[x] or resource"),
])
def test_an_endpoint_part_registration_cannot_use_is_refused(client, parts, fragment):
    body = as_params(name="Careful")
    body["parameter"].extend(parts)
    res = client.post("/fhir/$register", json=body)
    assert res.status_code == 400 and fragment in diagnostics(res)
    assert client.get("/fhir/Organization").json()["total"] == 0


def test_reset_knows_every_table_the_service_creates(tmp_path, monkeypatch):
    """A table init_db() makes and reset.py does not clear survives the wipe -
    and teams.id is reused afterwards, so its rows land on the next company."""
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    app_module.init_db()
    conn = sqlite3.connect(app_module.db_path())
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()
    assert tables == set(reset.TABLES)


@pytest.mark.parametrize("mutate, fragment", [
    (lambda b: b.update(address="javascript:alert(1)"), "http or https"),
    (lambda b: b.update(address="ftp://files.example.org/"), "http or https"),
    (lambda b: b.update(address="/fhir"), "http or https"),
    (lambda b: b.update(address="https://"), "http or https"),
    (lambda b: b.update(address="https://[::1"), "http or https"),
    (lambda b: b.update(address="https://a.example.org/x y"), "http or https"),
    (lambda b: b.update(address="https://a.example.org/" + "x" * 2000), "2000 characters"),
    (lambda b: b.pop("address"), "http or https"),
    (lambda b: b.pop("connectionType"), "connectionType"),
    (lambda b: b.update(connectionType={"code": "dicom-wado-rs"}), "connectionType"),
    (lambda b: b.update(connectionType={"code": "hl7-fhir-msg"}), "connectionType"),
    (lambda b: b["connectionType"].update(system="http://example.org"),
     "connectionType.system for hl7-fhir-rest is http://terminology.hl7.org"),
    (lambda b: b.update(description="Ask for a token"), DESC_EXT),
    # credentials for the address do not belong on a public list
    (lambda b: b.update(header=["Authorization: Bearer s3cret"]), "public"),
    (lambda b: b.update(extension=[{"url": DESC_EXT, "valueMarkdown": "x"}]),
     "one valueString"),
    (lambda b: b.update(extension=[{"url": DESC_EXT, "valueString": "a"},
                                   {"url": DESC_EXT, "valueString": "b"}]),
     "one valueString"),
    (lambda b: b.update(status="off"), "DELETE"),
    (lambda b: b.update(extension={"url": DESC_EXT}), "extension"),
    (lambda b: b.update(resourceType="Organization"), "FHIR Endpoint"),
])
@pytest.mark.parametrize("verb", ["post", "put"])
def test_an_endpoint_this_server_would_mislabel_is_refused(client, mutate, fragment, verb):
    tok = register(client, "Advertiser")
    existing = None
    if verb == "put":
        existing = advertise(client, tok, endpoint("https://kept.example.org")).json()["id"]
    body = endpoint()
    mutate(body)
    res = advertise(client, tok, body, existing)
    assert res.status_code == 400
    assert fragment in diagnostics(res)
    if existing:
        # refused means unchanged, not half-written
        assert client.get("/fhir/Endpoint/" + existing).json()["address"] \
            == "https://kept.example.org"


def test_a_company_advertises_only_so_many_endpoints(client):
    tok = register(client, "Busy")
    for _ in range(app_module.MAX_ENDPOINTS):
        assert advertise(client, tok, endpoint()).status_code == 201
    assert advertise(client, tok, endpoint()).status_code == 422
    assert advertise(client, register(client, "Other"), endpoint()).status_code == 201


def test_openapi_covers_the_endpoint_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path, verb in (("/fhir/Endpoint", "post"),
                       ("/fhir/Endpoint/{endpoint_id}", "put"),
                       ("/fhir/Endpoint/{endpoint_id}", "delete")):
        assert paths[path][verb]["security"] == [{"TeamToken": []}], (path, verb)
    assert "security" not in paths["/fhir/Endpoint"]["get"]
    example = paths["/fhir/Endpoint"]["post"]["requestBody"]["content"][
        "application/fhir+json"]["example"]
    assert app_module.parse_endpoint(example)["kind"] == "hl7-fhir-rest"


def test_the_page_spells_the_endpoint_description_as_the_server_does(client):
    page = client.get("/").text
    found = re.search(r'const ENDPOINT_DESCRIPTION_EXT\s*=\s*"([^"]+)"', page)
    assert found and found.group(1) == DESC_EXT


def test_screenshots_earn_nothing(client, monkeypatch):
    """Interop stays unscored, so a wall of screenshots cannot be farmed."""
    consume_first_pass(client, monkeypatch)
    tok_p = register(client, "Producer2")
    tok_c = register(client, "Consumer2")
    submit(client, tok_p, outcome(), monkeypatch, body=CLINICAL_BODY)
    before = {r["organization"]: r["score"] for r in param_entries(client.get("/fhir/$leaderboard"))}
    for _ in range(5):
        assert add_shot(client, tok_c, "producer2").status_code == 201
    after = {r["organization"]: r["score"] for r in param_entries(client.get("/fhir/$leaderboard"))}
    assert after == before
    assert after["Consumer2"] == 0


def test_whoami_names_the_company_behind_a_token(client):
    tok = register(client, "  Alpha Team  ")
    res = client.get("/fhir/Organization/$me", headers={"X-Team-Token": tok})
    assert res.status_code == 200
    org = res.json()
    assert org["resourceType"] == "Organization"
    assert org["name"] == "Alpha Team"
    assert org["id"] == "alpha-team"
    assert org["active"] is True
    assert client.get("/fhir/Organization/$me").status_code == 401
    assert client.get("/fhir/Organization/$me", headers={"X-Team-Token": "nope"}).status_code == 401


def test_the_interop_tab_offers_screenshots(client):
    text = client.get("/").text
    assert "I loaded this - add a screenshot</button>" in text
    assert "async function shrinkImage(file)" in text          # browser-side downscale
    assert 'loading="lazy"' in text                            # a gallery on venue wifi
    assert "Your document - other companies add the screenshots." in text
    assert "/fhir/DocumentReference?related=" in text
    # the note is edited in place, and a withdraw asks once first
    assert 'box.className = "shot-edit"' in text
    assert "prompt(" not in text
    assert "Withdraw - tap to confirm" in text
    assert "Needs your company token, and it is not scored." not in text


# ---- validator locations mapped onto the document

LOCATOR_FUNCS = ("splitSegments", "choiceKey", "locateExpr", "refineByMessage",
                 "layoutJson", "pathLabel")

HARNESS = r"""
const [doc, cases] = JSON.parse(require("fs").readFileSync(0, "utf8"));
const laid = layoutJson(doc);
console.log(JSON.stringify(cases.map(([expr, msg]) => {
  const path = refineByMessage(doc, locateExpr(doc, expr), msg || "");
  const key = path.map((k) => "/" + k).join("");
  const spot = laid.at.get(key);
  return [key, spot ? spot.start + 1 : null, spot ? laid.lines[spot.start] : null,
          pathLabel(doc, path)];
})));
"""


def page_script(client):
    """The page's own script, as served - what participants actually run."""
    blocks = re.findall(r"<script>\n(.*?)\n</script>", client.get("/").text, re.S)
    return max(blocks, key=len)


def run_locator(client, doc, cases):
    """Resolve validator locations with the page's own code, in node.

    Lifting the functions out of the served page rather than a copy is the
    point: what is tested is the locator participants actually run.
    Returns one [json path, line number, line text, readable label] per case.
    """
    js = page_script(client)
    parts = []
    for name in LOCATOR_FUNCS:
        start = js.index("function %s(" % name)
        parts.append(js[start:js.index("\n}", start) + 2])
    proc = subprocess.run(["node", "-e", "\n".join(parts) + HARNESS],
                          input=json.dumps([doc, cases]),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


LOCATOR_DOC = {
    "resourceType": "Bundle",
    "entry": [
        {"resource": {"resourceType": "Composition",
                      "section": [{"title": "Problems", "entry": [{"reference": "x"}]}]}},
        {"resource": {"resourceType": "Patient", "gender": "other",
                      "communication": [{"language": {"coding": [{"system": "urn:x"}]}}]}},
        {"resource": {"resourceType": "MedicationStatement",
                      "dosage": [{"doseAndRate": [{"doseQuantity": {"value": 5}}]}],
                      "effectivePeriod": {"start": "2020-01-01"}}},
    ],
}

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed")


@needs_node
def test_a_validator_location_resolves_to_a_line_in_the_document(client):
    """The four shapes Matchbox emits: plain paths, its /*Type/id*/ annotation,
    ofType(Quantity) and the dose[x] choice marker."""
    got = run_locator(client, LOCATOR_DOC, [
        ["Bundle.entry[1].resource/*Patient/abc*/.gender", ""],
        ["Bundle.entry[1].resource.communication[0].language.coding[0].system", ""],
        ["Bundle.entry[2].resource.dosage[0].doseAndRate[0].dose.ofType(Quantity).value", ""],
        ["Bundle.entry[2].resource.dosage[0].doseAndRate[0].dose[x].value", ""],
        ["Bundle.entry[2].resource.effective.ofType(Period)", ""],
        ["Bundle.entry[0].resource.section[0].entry[0]", ""],
    ])
    assert [p for p, _, _, _ in got] == [
        "/entry/1/resource/gender",
        "/entry/1/resource/communication/0/language/coding/0/system",
        "/entry/2/resource/dosage/0/doseAndRate/0/doseQuantity/value",
        "/entry/2/resource/dosage/0/doseAndRate/0/doseQuantity/value",
        "/entry/2/resource/effectivePeriod",
        "/entry/0/resource/section/0/entry/0",
    ]
    # the line number has to point at the element, not merely exist
    assert got[0][2].strip().rstrip(",") == '"gender": "other"'
    assert got[2][2].strip().rstrip(",") == '"value": 5'
    assert got[4][2].strip().startswith('"effectivePeriod": {')
    assert all(line for _, line, _, _ in got)
    # and the row says where that is in words the document itself supports
    assert got[0][3] == "Patient #1 \u00b7 gender"
    assert got[2][3] == ("MedicationStatement #2 \u00b7 "
                         "dosage[0].doseAndRate[0].doseQuantity.value")


@needs_node
def test_a_location_for_something_missing_lands_on_the_nearest_real_element(client):
    """Most findings name an element that is not there - a required slice, a
    minimum cardinality. The marker belongs on the nearest thing that is."""
    got = run_locator(client, LOCATOR_DOC, [
        # the message names the element the location only encloses
        ["Bundle", "Slice 'Bundle.entry:patient': a matching slice is required"],
        ["Bundle.entry[0].resource", "Composition.section: minimum required = 5, but only found 1"],
        # Patient.name is absent, so the resource itself is as close as we get
        ["Bundle.entry[1].resource", "Patient.name: minimum required = 1, but only found 0"],
        # a location running off the end of the document
        ["Bundle.entry[9].resource.dosage[0]", ""],
        # a message about some other resource type must not drag the marker
        ["Bundle.entry[1].resource", "Observation.value: whatever"],
    ])
    assert [p for p, _, _, _ in got] == [
        "/entry",
        "/entry/0/resource/section",
        "/entry/1/resource",
        "/entry",
        "/entry/1/resource",
    ]
    assert got[1][2].strip().startswith('"section": [')
    assert [got[0][3], got[1][3]] == ["Bundle \u00b7 entry", "Composition #0 \u00b7 section"]


EXPLAIN_HARNESS = r"""
const [groups, severity, reported] = JSON.parse(require("fs").readFileSync(0, "utf8"));
const prompt = explainPrompt(groups, severity, reported);
console.log(JSON.stringify({ prompt,
  urls: Object.fromEntries(EXPLAIN_PROVIDERS.map((p) => [p.id, explainUrl(p, prompt)])) }));
"""


def run_explain(client, groups, severity, reported):
    """Build the explain-with-an-assistant prompt with the page's own code."""
    js = page_script(client)
    parts = []
    for name in ("EXPLAIN_GUIDES", "EXPLAIN_PROVIDERS"):
        start = js.index("const %s = " % name)
        # One is an object literal and the other an array, and both carry ";\n"
        # inside their string values - so close on whichever bracket terminator
        # comes first, not on the first semicolon.
        end = min(e for e in (js.find("};\n", start), js.find("];\n", start))
                  if e != -1)
        parts.append(js[start:end + 3])
    # explainPrompt reads it; the page sets it from X-Document-Type per submission.
    parts.append('let explainDoc = "eps";\n')
    for name in ("explainPrompt", "explainUrl"):
        start = js.index("function %s(" % name)
        parts.append(js[start:js.index("\n}", start) + 2])
    proc = subprocess.run(["node", "-e", "\n".join(parts) + EXPLAIN_HARNESS],
                          input=json.dumps([groups, severity, reported]),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


EXPLAIN_GROUPS = [
    {"severity": "error", "exprShort": "Bundle.entry[0].resource",
     "where": "Composition #0 \u00b7 section",
     "message": "Slice 'Composition.section:sectionAllergies': a matching slice is "
                "required, but not found",
     "profiles": ["http://hl7.eu/fhir/eps/StructureDefinition/composition-eu-eps|1.0.0-ballot",
                  "http://hl7.org/fhir/uv/ips/StructureDefinition/Composition-uv-ips|2.0.0"]},
    {"severity": "error", "exprShort": "Bundle.entry[1].resource", "where": "Patient #1",
     "message": "Patient.name: minimum required = 1, but only found 0", "profiles": []},
]


@needs_node
def test_the_explain_prompt_carries_every_error_with_its_location_and_profiles(client):
    """The assistant gets what the table shows: each distinct finding, where it
    is, and which profiles raised it - never the bundle. The banner counted the
    validator's repeats, the table merged them, and the prompt says so rather
    than leaving the assistant to wonder where the other two went."""
    got = run_explain(client, EXPLAIN_GROUPS, "error", 4)
    prompt = got["prompt"]
    assert prompt.startswith("I am validating a FHIR bundle against the HL7 Europe "
                             "European Patient Summary implementation guide "
                             "(https://hl7.eu/fhir/eps/)")
    assert "Explain what these errors mean" in prompt
    assert "starting with whichever change clears the most of them" in prompt
    assert "The 4 errors, merged into 2 distinct findings:\n\n1. Slice" in prompt
    assert ("\n   location: Bundle.entry[0].resource (Composition #0 \u00b7 section)"
            "\n   profile:  http://hl7.eu/fhir/eps/StructureDefinition/"
            "composition-eu-eps|1.0.0-ballot, http://hl7.org/fhir/uv/ips/"
            "StructureDefinition/Composition-uv-ips|2.0.0\n\n2. Patient.name") in prompt
    # a finding without a profile does not get an empty profile line
    assert prompt.endswith("2. Patient.name: minimum required = 1, but only found 0"
                           "\n   location: Bundle.entry[1].resource (Patient #1)")
    # three assistants get the question in the URL, and it survives the trip
    for pid, base in (("chatgpt", "https://chatgpt.com/?q="),
                      ("claude", "https://claude.ai/new?q="),
                      ("mistral", "https://chat.mistral.ai/chat?q=")):
        url = got["urls"][pid]
        assert url.startswith(base), url
        assert urllib.parse.unquote(url[len(base):]) == prompt
    # Gemini's web app takes no prompt parameter, so it opens on a blank chat
    assert got["urls"]["gemini"] == "https://gemini.google.com/app"


@needs_node
def test_a_single_finding_is_asked_about_in_the_singular(client):
    """One warning is "this warning", not "these warnings" with a list of one -
    and a bundle that passed with warnings gets the same offer as one that
    failed with errors."""
    got = run_explain(client, EXPLAIN_GROUPS[1:], "warning", 1)
    prompt = got["prompt"]
    assert "Explain what this warning means in plain terms" in prompt
    assert "to fix it." in prompt
    assert "\n\nThe warning:\n\nPatient.name: minimum required" in prompt
    assert "1. " not in prompt and "merged into" not in prompt
    # the same count on the banner and in the prompt: no "merged" note
    got = run_explain(client, EXPLAIN_GROUPS, "error", 2)
    assert "The 2 errors:\n\n1. " in got["prompt"]


def test_page_ids_are_unique(client):
    """$() is querySelector, so of two elements sharing an id only the first is
    ever reached. The EHDS document table and the submit tab's embedded view
    once both used doc-body: the first validation wrote the bundle into the
    EHDS table on the leaderboard tab, and the embedded view stayed empty.
    Ids built in script strings are not seen here - the parser treats script
    as text."""
    seen = []

    class Ids(html.parser.HTMLParser):
        def handle_starttag(self, tag, attrs):
            seen.extend(v for k, v in attrs if k == "id")

    Ids().feed(client.get("/").text)
    assert "doc-body" in seen and "lb-body" in seen       # the parser read the page
    assert sorted({i for i in seen if seen.count(i) > 1}) == []


def test_every_id_the_script_looks_up_is_in_the_page(client):
    """The other half of a rename: markup changed but a lookup left behind
    returns null, or - worse - finds whatever still has the old id, and the
    renamed element is never filled. The first case is checked for every
    literal lookup, against every literal id - markup the script builds
    included. The second only shows as a renamed element nothing looks up,
    so it is pinned for the pair that once shared doc-body."""
    text = client.get("/").text
    defined = set(re.findall(r'\bid="([\w-]+)"', text))
    wanted = set(re.findall(r'\$\("#([\w-]+)"\)', text))
    wanted |= set(re.findall(r'getElementById\("([\w-]+)"\)', text))
    assert sorted(wanted - defined) == []
    assert "theme-btn" in wanted and "cv-pick" in defined    # both forms are read
    assert {"doc-body", "doc-types-body"} <= wanted


def test_the_submit_tab_shows_findings_inside_the_document(client):
    text = client.get("/").text
    assert 'id="doc-body"' in text and 'id="doc-flagged"' in text
    assert "function jumpToLine(" in text
    assert "renderIssues(outcome.issue || [], body)" in text     # the submitted bytes
    assert 'class="loc-jump"' in text                            # location jumps to the line
    assert "DOC_MAX_LINES" in text                               # a huge bundle stays usable


def test_a_failed_bundle_does_not_show_an_earlier_score(client):
    # X-Score is the standing, which an earlier pass keeps up; beside
    # "INVALID" it read as this bundle's score. A fail shows its own 0.
    text = client.get("/").text
    assert '(pass ? score : 0) + " pts</span>"' in text
    assert '+ (score ? " " + standing : "")' in text             # standing on hover


def test_a_refused_file_takes_the_previous_bundle_with_it(client):
    """Picking a file over the limit left the last bundle in the box, and
    Validate sent and scored that one under the new file's name."""
    text = client.get("/").text
    handler = text.split('$("#file").addEventListener("change"', 1)[1]
    handler = handler[:handler.index("\n});")]
    refuse = handler[handler.index("const refuse = "):]
    refuse = refuse[:refuse.index("};")]
    assert 'ev.target.value = ""' in refuse and '$("#bundle").value = ""' in refuse
    # both refusals - too big, and unreadable - go through it
    assert handler.count("refuse(") == 2
    assert "return refuse(" in handler and 'refuse("Could not read the file: "' in handler


@needs_node
def test_a_file_just_over_the_limit_is_not_said_to_be_the_limit(client):
    """Both sizes were rounded to tenths alike, so anything up to about 50 KB
    over 5 MB was refused as "5.0 MB; the limit is 5 MB"."""
    js = page_script(client)
    start = js.index("function tooBigMessage(")
    fn = js[start:js.index("\n}", start) + 2]
    limit = 5 * 1048576
    cases = [[limit + 1, limit], [5249983, limit], [5450745, limit],
             [limit + 1048576 // 2 + 1, limit + 1048576 // 2]]
    proc = subprocess.run(
        ["node", "-e", fn + "console.log(JSON.stringify(JSON.parse(require('fs')"
         ".readFileSync(0, 'utf8')).map(([s, l]) => tooBigMessage(s, l))));"],
        input=json.dumps(cases), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        "That file is 5.1 MB; the limit is 5 MB.",
        "That file is 5.1 MB; the limit is 5 MB.",
        "That file is 5.2 MB; the limit is 5 MB.",
        "That file is 5.6 MB; the limit is 5.5 MB.",
    ]



# ---------------------------------------------------------------- backups

def fake_data_dir(path, teams=2, subs=3, files=("submissions/acme/1.json",
                                                "renderings/acme/shot.png")):
    """The shape backup.py cares about: a database with the two tables it
    reports on, plus the write-once trees beside it."""
    os.makedirs(path, exist_ok=True)
    conn = sqlite3.connect(os.path.join(path, backup.DB_NAME))
    conn.execute("CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE submissions (id INTEGER PRIMARY KEY, team INTEGER)")
    conn.executemany("INSERT INTO teams (name) VALUES (?)",
                     [("team%d" % i,) for i in range(teams)])
    conn.executemany("INSERT INTO submissions (team) VALUES (?)",
                     [(1,) for _ in range(subs)])
    conn.commit()
    conn.close()
    for rel in files:
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(rel.encode() * 20)
    return path


def test_snapshot_captures_database_and_both_trees(tmp_path):
    data = fake_data_dir(str(tmp_path / "data"))
    backups = str(tmp_path / "backups")
    path, files, total, pruned = backup.take_snapshot(data, backups, keep=10)

    assert os.path.isfile(os.path.join(path, backup.DB_NAME))
    assert os.path.isfile(os.path.join(path, "submissions/acme/1.json"))
    assert os.path.isfile(os.path.join(path, "renderings/acme/shot.png"))
    assert (files, pruned) == (3, 0) and total > 0

    # the copy is a database, not bytes that happen to be there
    conn = sqlite3.connect(os.path.join(path, backup.DB_NAME))
    assert conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 2
    conn.close()


def test_snapshot_hardlinks_the_write_once_trees(tmp_path):
    """A submission's bytes never change, so a snapshot of them must not cost
    a second copy - an event's worth of 15-minute snapshots otherwise fills
    the disk with the same gigabytes over and over."""
    data = fake_data_dir(str(tmp_path / "data"))
    path, _, _, _ = backup.take_snapshot(data, str(tmp_path / "backups"), keep=10)

    live = os.stat(os.path.join(data, "submissions/acme/1.json"))
    snap = os.stat(os.path.join(path, "submissions/acme/1.json"))
    assert (live.st_dev, live.st_ino) == (snap.st_dev, snap.st_ino)

    # the database is the one thing that does change, so it is a real copy
    live_db = os.stat(os.path.join(data, backup.DB_NAME))
    snap_db = os.stat(os.path.join(path, backup.DB_NAME))
    assert live_db.st_ino != snap_db.st_ino


def test_deleting_a_rendering_leaves_the_snapshot_intact(tmp_path):
    data = fake_data_dir(str(tmp_path / "data"))
    path, _, _, _ = backup.take_snapshot(data, str(tmp_path / "backups"), keep=10)
    os.unlink(os.path.join(data, "renderings/acme/shot.png"))
    assert os.path.isfile(os.path.join(path, "renderings/acme/shot.png"))


def test_snapshot_database_is_readable_while_the_service_writes(tmp_path):
    """`cp` on a live SQLite file can yield a torn database; the backup API
    cannot. Hold an open write transaction and snapshot underneath it."""
    data = fake_data_dir(str(tmp_path / "data"))
    live = sqlite3.connect(os.path.join(data, backup.DB_NAME))
    live.execute("BEGIN")
    live.execute("INSERT INTO teams (name) VALUES ('mid-flight')")
    try:
        path, _, _, _ = backup.take_snapshot(data, str(tmp_path / "backups"), keep=10)
    finally:
        live.rollback()
        live.close()

    conn = sqlite3.connect(os.path.join(path, backup.DB_NAME))
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # the uncommitted row is not in the snapshot, which is the point of a
    # consistent copy rather than a byte-for-byte one
    assert conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 2
    conn.close()


def test_prune_keeps_the_newest_and_nothing_else(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    for stamp in ("20260101T000000Z", "20260101T001500Z", "20260101T003000Z"):
        (backups / stamp).mkdir()
    (backups / "not-a-snapshot").mkdir()          # never swept up by name

    assert backup.prune(str(backups), keep=2) == 1
    assert backup.list_snapshots(str(backups)) == [
        "20260101T001500Z", "20260101T003000Z"]
    assert (backups / "not-a-snapshot").is_dir()


def test_a_failed_snapshot_leaves_nothing_that_looks_usable(tmp_path, monkeypatch):
    """A half-written snapshot that is named like a finished one is worse than
    no snapshot: --restore LATEST would pick it."""
    data = fake_data_dir(str(tmp_path / "data"))
    backups = str(tmp_path / "backups")

    def boom(src, dest):
        raise OSError("disk full")
    monkeypatch.setattr(backup, "link_tree", boom)

    with pytest.raises(OSError):
        backup.take_snapshot(data, backups, keep=10)
    assert backup.list_snapshots(backups) == []
    assert not [n for n in os.listdir(backups) if n.endswith(".partial")]


def test_restore_without_yes_changes_nothing(tmp_path, capsys):
    data = fake_data_dir(str(tmp_path / "data"))
    backups = str(tmp_path / "backups")
    backup.take_snapshot(data, backups, keep=10)
    os.unlink(os.path.join(data, "submissions/acme/1.json"))

    with pytest.raises(SystemExit):
        backup.restore(data, backups, "LATEST", confirmed=False)

    # it says what it would do, and the damaged directory is still damaged
    assert "2 companies, 3 submissions" in capsys.readouterr().out
    assert not os.path.exists(os.path.join(data, "submissions/acme/1.json"))


def test_restore_puts_the_results_back_and_keeps_the_old_copy(tmp_path):
    data = fake_data_dir(str(tmp_path / "data"))
    backups = str(tmp_path / "backups")
    backup.take_snapshot(data, backups, keep=10)

    # the disaster: leaderboard emptied, a submitted bundle gone
    conn = sqlite3.connect(os.path.join(data, backup.DB_NAME))
    conn.execute("DELETE FROM teams")
    conn.commit()
    conn.close()
    os.unlink(os.path.join(data, "submissions/acme/1.json"))

    backup.restore(data, backups, "LATEST", confirmed=True)

    conn = sqlite3.connect(os.path.join(data, backup.DB_NAME))
    assert conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 2
    conn.close()
    assert os.path.isfile(os.path.join(data, "submissions/acme/1.json"))

    # nothing was deleted: the emptied directory is still there to go back to
    aside = [n for n in os.listdir(tmp_path) if n.startswith("data.replaced-")]
    assert len(aside) == 1
    conn = sqlite3.connect(os.path.join(tmp_path, aside[0], backup.DB_NAME))
    assert conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 0
    conn.close()


def test_the_same_snapshot_can_be_restored_twice(tmp_path):
    """Restore copies out of the snapshot rather than moving it, so a restore
    that itself went wrong can be repeated."""
    data = fake_data_dir(str(tmp_path / "data"))
    backups = str(tmp_path / "backups")
    name = os.path.basename(backup.take_snapshot(data, backups, keep=10)[0])

    backup.restore(data, backups, name, confirmed=True)
    backup.restore(data, backups, name, confirmed=True)
    assert backup.list_snapshots(backups) == [name]
    assert os.path.isfile(os.path.join(data, "submissions/acme/1.json"))
    # both set-aside copies survive, rather than the second restore colliding
    # with the first on a name stamped to the same second
    assert len([n for n in os.listdir(tmp_path)
                if n.startswith("data.replaced-")]) == 2
    assert not [n for n in os.listdir(tmp_path) if ".incoming-" in n]


def test_restore_refuses_someone_elses_backups(tmp_path, monkeypatch):
    data = fake_data_dir(str(tmp_path / "data"))
    backups = str(tmp_path / "backups")
    backup.take_snapshot(data, backups, keep=10)
    # read the real uid before patching: backup.os *is* os
    someone_else = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: someone_else)

    with pytest.raises(SystemExit) as exc:
        backup.restore(data, backups, "LATEST", confirmed=True)
    assert "owns the backups" in str(exc.value)


def test_backups_are_not_world_readable(tmp_path):
    """Results sit on a laptop at a venue; another account on it should not be
    able to read the submissions, nor swap a snapshot for a doctored one."""
    data = fake_data_dir(str(tmp_path / "data"))
    backups = str(tmp_path / "backups")
    path, _, _, _ = backup.take_snapshot(data, backups, keep=10)
    assert oct(os.stat(backups).st_mode & 0o777) == "0o700"
    assert oct(os.stat(os.path.join(path, backup.DB_NAME)).st_mode & 0o777) == "0o600"


def test_restore_names_a_missing_snapshot_instead_of_guessing(tmp_path):
    data = fake_data_dir(str(tmp_path / "data"))
    backups = str(tmp_path / "backups")
    backup.take_snapshot(data, backups, keep=10)
    with pytest.raises(SystemExit) as exc:
        backup.restore(data, backups, "20200101T000000Z", confirmed=True)
    assert "--list" in str(exc.value)


def test_no_http_route_can_trigger_a_restore(client):
    """Restore is the operator's, from a shell on the organiser's machine. A
    participant on the venue network must not have any way to reach it."""
    paths = [r.path for r in app_module.app.routes]
    assert not [p for p in paths if "backup" in p or "restore" in p]


# ------------------------------------------- unresolvable terminology filter

MSG_ID_EXT = "http://hl7.org/fhir/StructureDefinition/operationoutcome-message-id"


def issue(text, severity="warning", msg_id=None):
    out = {"severity": severity, "code": "processing", "details": {"text": text}}
    if msg_id:
        out["extension"] = [{"url": MSG_ID_EXT, "valueCode": msg_id}]
    return out


def kept(*issues):
    filtered = app_module.drop_uncheckable_terminology(
        {"resourceType": "OperationOutcome", "issue": list(issues)})
    return [i["details"]["text"] for i in filtered["issue"]]


@pytest.mark.parametrize("msg_id, text", [
    ("UNKNOWN_CODESYSTEM",
     "A definition for CodeSystem 'http://www.whocc.no/atc' could not be found, "
     "so the code cannot be validated"),
    ("UNKNOWN_CODESYSTEM_VERSION",
     "A definition for CodeSystem 'http://snomed.info/sct' version 'null' could "
     "not be found, so the code cannot be validated"),
    ("Error_validating_code_running_without_terminology_services",
     "Unable to validate code '38341003' in system 'http://snomed.info/sct' "
     "because the validator is running without terminology services"),
    ("Terminology_TX_ValueSet_NotFound",
     "ValueSet 'http://terminology.ehdsi.eu/ValueSet/eHDSIAllergenNoDrug' not found"),
    ("TERMINOLOGY_TX_NOSVC_BOUND_REQ",
     "Could not confirm that the codes provided are from the required value set "
     "http://hl7.org/fhir/ValueSet/x because there is no terminology service"),
    ("TERMINOLOGY_TX_SYSTEM_NOT_USABLE",
     "The definition for the Code System with URI 'http://loinc.org' from 'x' "
     "doesn't provide any codes so the code cannot be validated"),
    ("UNABLE_TO_CHECK_IF_THE_PROVIDED_CODES_ARE_IN_THE_VALUE_SET_CS",
     "Unable to check whether the code is in the value set "
     "'http://hl7.org/fhir/ValueSet/y' because the code system "
     "http://snomed.info/sct was not found"),
    ("Terminology_TX_NoValid_15",
     "The value provided ('http://loinc.org') could not be validated in the "
     "absence of a terminology server"),
])
def test_uncheckable_terminology_messages_are_dropped(msg_id, text):
    assert kept(issue(text, msg_id=msg_id)) == []


def test_the_passthrough_terminology_warning_is_matched_on_wording():
    """TERMINOLOGY_TX_WARNING is defined as "{1}" - it repeats whatever the
    terminology layer said - and some issues carry no message id at all, so the
    id set alone would let this family through."""
    assert kept(issue("Unable to validate code without using server because: "
                      "Resolved system urn:ietf:bcp:47 (v2.0.1), but the "
                      "definition doesn't include any codes, so the code has "
                      "not been validated", msg_id="TERMINOLOGY_TX_WARNING")) == []
    assert kept(issue("Unable to validate code without using server because: "
                      "Resolved system http://unitsofmeasure.org (v3.0.1)")) == []


def test_membership_claims_about_an_unresolvable_system_are_dropped():
    """The validator reports the same gap twice: once as "I could not resolve
    ATC", then again as "none of these codings are in the value set". The second
    reads like a real finding, so it has to go with the first."""
    assert kept(
        issue("A definition for CodeSystem 'http://www.whocc.no/atc' could not "
              "be found, so the code cannot be validated",
              msg_id="UNKNOWN_CODESYSTEM"),
        issue("None of the codings provided are in the value set 'Medications - "
              "IPS' (http://hl7.org/fhir/uv/ips/ValueSet/medication-uv-ips|2.0.0), "
              "and a coding is recommended to come from this value set "
              "(codes = http://www.whocc.no/atc#B01AF02)",
              severity="information", msg_id="Terminology_TX_NoValid_3_CC"),
    ) == []


def test_a_membership_claim_that_was_actually_checked_survives():
    """The point of tying the second pass to the first: a value set that did
    resolve produces a real finding, and dropping it would hide a wrong code."""
    real = ("The Coding provided (http://hl7.org/fhir/administrative-gender#z) "
            "was not found in the value set "
            "http://hl7.org/fhir/ValueSet/administrative-gender, and a code is "
            "required from this value set")
    assert kept(
        issue("A definition for CodeSystem 'http://www.whocc.no/atc' could not "
              "be found, so the code cannot be validated",
              msg_id="UNKNOWN_CODESYSTEM"),
        issue(real, severity="error", msg_id="Terminology_TX_NoValid_4"),
    ) == [real]


def test_noise_that_names_no_url_is_still_dropped():
    """Two of the message ids carry no URL in their text at all. The filter
    used to return the original outcome whenever `unresolvable` came out empty,
    undoing its own first pass - so the same bundle scored 0 or 50 depending on
    whether some unrelated finding happened to mention a URL."""
    outcome = {"resourceType": "OperationOutcome", "issue": [
        issue("Error validating code: running without terminology services",
              msg_id="Error_validating_code_running_without_terminology_services"),
        issue("Error expanding ValueSet: running without terminology services",
              msg_id="Error_expanding_ValueSet_running_without_terminology_services"),
    ]}
    assert app_module.drop_uncheckable_terminology(outcome)["issue"] == []


def test_a_profile_named_in_the_from_suffix_is_not_a_code_system():
    """The validator appends "(from <profile>)" to its messages. Harvesting
    that URL as an unresolvable code system would drop every genuine
    terminology error raised by the same profile."""
    prof = "http://hl7.eu/fhir/eps/StructureDefinition/Composition-eu-eps|1.0.0-ballot"
    noise = issue(
        "A definition for CodeSystem 'http://snomed.info/sct' could not be found, "
        "so the code cannot be validated (from %s)" % prof,
        msg_id="UNKNOWN_CODESYSTEM")
    real = issue(
        "The Coding provided (http://hl7.org/fhir/administrative-gender#z) was not "
        "found in the value set http://hl7.org/fhir/ValueSet/administrative-gender, "
        "and a code is required from this value set (from %s)" % prof,
        msg_id="Terminology_TX_NoValid_4")
    kept = app_module.drop_uncheckable_terminology(
        {"resourceType": "OperationOutcome", "issue": [noise, real]})["issue"]
    assert kept == [real]
    assert prof.split("|")[0] not in app_module._named_urls(app_module._issue_text(noise))


def test_nothing_is_dropped_when_every_code_system_resolved():
    """Self-calibrating rather than a hardcoded list of licensed systems: point
    Matchbox at a terminology server, the first pass finds nothing to drop, and
    the membership findings are all real again."""
    real = ("None of the codings provided are in the value set "
            "http://hl7.org/fhir/ValueSet/z, and a coding from this value set "
            "is required) (codes = http://snomed.info/sct#123)")
    assert kept(issue(real, severity="error",
                      msg_id="Terminology_TX_NoValid_1_CC")) == [real]


def test_real_findings_are_untouched():
    slice_err = ("Slice 'Bundle.entry:composition': a matching slice is "
                 "required, but not found")
    cardinality = "Composition.section: minimum required = 5, but only found 4"
    assert kept(
        issue(slice_err, severity="error", msg_id="Validation_VAL_Profile_Minimum_SLICE"),
        issue("A definition for CodeSystem 'http://loinc.org' could not be "
              "found, so the code cannot be validated", msg_id="UNKNOWN_CODESYSTEM"),
        issue(cardinality, severity="error", msg_id="Validation_VAL_Profile_Minimum"),
    ) == [slice_err, cardinality]


def test_the_filter_survives_a_malformed_outcome():
    assert app_module.drop_uncheckable_terminology({}) == {}
    assert app_module.drop_uncheckable_terminology(
        {"issue": "not a list"})["issue"] == "not a list"
    out = app_module.drop_uncheckable_terminology({"issue": [None, 7]})
    assert out["issue"] == [None, 7]


def test_a_bundle_is_not_failed_for_terminology_we_did_not_check(client, monkeypatch):
    """If an unresolvable code system ever comes back as an error - a required
    binding to a value set we cannot expand - it must not cost a team the
    submission. Filtering before the count is what makes that true."""
    consume_first_pass(client, monkeypatch)
    oc = {"resourceType": "OperationOutcome", "issue": [
        issue("A definition for CodeSystem 'http://snomed.info/sct' could not be "
              "found, so the code cannot be validated",
              severity="error", msg_id="UNKNOWN_CODESYSTEM"),
    ]}
    token = register(client, "Terminology Gap AB")
    res = submit(client, token, oc, monkeypatch)
    assert res.headers["X-Pass"] == "true"
    assert res.headers["X-Errors"] == "0"
    assert res.headers["X-Score"] == "50"
    # and the finding is gone from what the team reads, not just from the count
    assert res.json()["issue"] == []


def test_stored_counts_match_the_findings_that_are_shown(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    oc = {"resourceType": "OperationOutcome", "issue": [
        issue("A definition for CodeSystem 'http://loinc.org' could not be "
              "found, so the code cannot be validated", msg_id="UNKNOWN_CODESYSTEM"),
        issue("Best Practice Recommendation: In general, all observations "
              "should have a performer"),
    ]}
    token = register(client, "Counted Warnings AB")
    res = submit(client, token, oc, monkeypatch)
    assert len(res.json()["issue"]) == 1
    slug = param_entries(client.get("/fhir/$leaderboard"))
    row = [r for r in slug if r["organization"] == "Counted Warnings AB"][0]
    history = param_entries(client.get(
        "/fhir/$submissions?organization=%s" % row["organizationSlug"]))
    assert history[0]["warnings"] == 1


# ------------------------------------------------------- practice-mode resets

def practice_data_dir(path):
    """The shape reset.py clears: the result tables, plus a per-team directory
    in each of the write-once trees."""
    os.makedirs(path, exist_ok=True)
    conn = sqlite3.connect(os.path.join(path, reset.DB_NAME))
    conn.execute("CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE submissions (id INTEGER PRIMARY KEY, team INTEGER)")
    conn.execute("CREATE TABLE renderings (id INTEGER PRIMARY KEY, sub INTEGER)")
    conn.execute("INSERT INTO teams (name) VALUES ('acme')")
    conn.executemany("INSERT INTO submissions (team) VALUES (?)", [(1,), (1,)])
    conn.execute("INSERT INTO renderings (sub) VALUES (1)")
    conn.execute("CREATE TABLE endpoints (id INTEGER PRIMARY KEY, team INTEGER)")
    conn.executemany("INSERT INTO endpoints (team) VALUES (?)", [(1,), (1,)])
    conn.commit()
    conn.close()
    for rel in ("submissions/acme/1.json", "renderings/acme/shot.png"):
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(b"x" * 32)
    return path


def test_reset_clears_every_table_and_both_trees(tmp_path):
    data = practice_data_dir(str(tmp_path / "data"))
    removed = reset.clear(data)
    # The directories are counted too: a wipe that took forty teams' uploads
    # but no rows must not report itself as "nothing to clear".
    assert removed == {"endpoints": 2, "renderings": 1, "submissions": 2, "teams": 1,
                       "team directories": 2}
    conn = sqlite3.connect(os.path.join(data, reset.DB_NAME))
    for table in reset.TABLES:
        assert conn.execute("SELECT count(*) FROM %s" % table).fetchone()[0] == 0
    conn.close()
    # The trees survive - app.py creates them at boot and expects them there -
    # but nothing of the teams' is left inside them.
    for tree in reset.TREES:
        assert os.path.isdir(os.path.join(data, tree))
        assert os.listdir(os.path.join(data, tree)) == []


def test_status_counts_exactly_what_a_wipe_would_take(tmp_path):
    """--status is the one command an operator runs to check the schedule, so
    it is also where a wrong --data-dir has to become visible."""
    data = practice_data_dir(str(tmp_path / "data"))
    counted = reset.count(data)
    # Counting must not be a wipe: the numbers have to still be there after.
    assert reset.count(data) == counted
    assert counted == reset.clear(data)


def test_status_on_the_wrong_directory_says_so_rather_than_reporting_zero(tmp_path):
    data = str(tmp_path / "nowhere")
    os.makedirs(data)
    assert reset.count(data) is None
    assert "check --data-dir" in reset.describe(reset.count(data))


def test_a_wipe_of_the_wrong_directory_exits_non_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv",
                        ["reset.py", "--yes", "--data-dir", str(tmp_path / "nowhere")])
    assert reset.main() == 1
    assert "check --data-dir" in capsys.readouterr().out


def test_reset_on_an_untouched_data_dir_is_not_an_error(tmp_path):
    """The reset container can win the race to a fresh volume."""
    data = str(tmp_path / "data")
    os.makedirs(data)
    assert reset.clear(data) is None
    assert "check --data-dir" in reset.describe(None)


def test_no_database_means_the_trees_are_left_alone(tmp_path):
    """--data-dir one keystroke out must not wipe a live event's uploads while
    reporting that there was nothing to clear."""
    data = str(tmp_path / "data")
    os.makedirs(os.path.join(data, "submissions", "acme"))
    with open(os.path.join(data, "submissions", "acme", "1.json"), "wb") as fh:
        fh.write(b"{}")
    assert reset.clear(data) is None
    assert os.listdir(os.path.join(data, "submissions")) == ["acme"]


def test_a_wipe_that_only_removes_files_still_says_so(tmp_path):
    data = practice_data_dir(str(tmp_path / "data"))
    conn = sqlite3.connect(os.path.join(data, reset.DB_NAME))
    for table in reset.TABLES:
        conn.execute("DELETE FROM %s" % table)
    conn.commit()
    conn.close()
    said = reset.describe(reset.clear(data))
    assert "team directories" in said and said != "nothing to clear"


def test_a_failed_wipe_does_not_end_the_daily_loop(tmp_path, monkeypatch):
    """Exiting here would skip every later wipe, including the last one on the
    morning the doors open."""
    data = practice_data_dir(str(tmp_path / "data"))
    monkeypatch.setattr(reset, "clear", lambda _d: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked")))
    ends = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    # One slot is due immediately; after it the period is over and run_loop
    # idles, so break out of the idle sleep to end the test.
    monkeypatch.setattr(reset.time, "sleep",
                        lambda _s: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        reset.run_loop(data, ends)
    # The tables are untouched, because clear() blew up - but we got here at
    # all, which is the point: the exception did not escape run_loop.
    conn = sqlite3.connect(os.path.join(data, reset.DB_NAME))
    assert conn.execute("SELECT count(*) FROM teams").fetchone()[0] == 1
    conn.close()


def test_reset_leaves_the_database_usable(tmp_path):
    """It empties the tables rather than dropping them: the service is running
    and holds this file open."""
    data = practice_data_dir(str(tmp_path / "data"))
    reset.clear(data)
    conn = sqlite3.connect(os.path.join(data, reset.DB_NAME))
    conn.execute("INSERT INTO teams (name) VALUES ('after')")
    conn.commit()
    assert conn.execute("SELECT name FROM teams").fetchall() == [("after",)]
    conn.close()


def test_warm_touches_every_document_type(monkeypatch):
    """The set it keeps warm is the set the service accepts.

    Both come off EHDS_DOCUMENTS, which is the point of warm.py importing app
    rather than listing the guides itself: adding a fifth category cannot leave
    the sidecar warming four."""
    seen = []
    monkeypatch.setattr(app_module, "MATCHBOX_TIMEOUT", 60)
    monkeypatch.setattr(app_module, "validate_with_matchbox",
                        lambda body, doc: seen.append(doc["id"]))
    assert warm.warm_once(b"{}") == len(app_module.EHDS_DOCUMENTS)
    assert sorted(seen) == sorted(d["id"] for d in app_module.EHDS_DOCUMENTS)
    # The patient summary first: it is the one the event scores, so it is the
    # one to have compiled if the pass is cut short.
    assert seen[0] == app_module.PRIMARY_DOCUMENT["id"]


def test_warm_carries_on_past_a_guide_that_will_not_compile(monkeypatch):
    """One guide failing costs a slow first submission of that kind. Letting it
    end the pass would cost every kind."""
    seen = []

    def validate(body, doc):
        seen.append(doc["id"])
        if doc["id"] == "imaging":
            raise app_module.MatchboxError("no")

    monkeypatch.setattr(app_module, "MATCHBOX_TIMEOUT", 60)
    monkeypatch.setattr(app_module, "validate_with_matchbox", validate)
    assert warm.warm_once(b"{}") == len(app_module.EHDS_DOCUMENTS) - 1
    assert sorted(seen) == sorted(d["id"] for d in app_module.EHDS_DOCUMENTS)


def test_warm_gives_itself_longer_than_a_submission_gets(monkeypatch):
    """A cold engine takes minutes to compile; the service's budget is short on
    purpose, so that a participant is never left hanging. Warming has to be
    allowed to outlast it or it can never fix the thing it exists to fix."""
    budgets = []
    monkeypatch.setattr(app_module, "MATCHBOX_TIMEOUT", 60)
    monkeypatch.setattr(app_module, "validate_with_matchbox",
                        lambda body, doc: budgets.append(app_module.MATCHBOX_TIMEOUT))
    warm.warm_once(b"{}")
    assert budgets and all(b >= 300 for b in budgets)


def test_warm_runs_well_inside_the_measured_eviction_window():
    """Engines were measured surviving about 75 minutes of disuse, on one JVM
    at one heap size. The interval has to leave room for that number being
    optimistic somewhere else."""
    assert warm.INTERVAL <= 1800


def test_practice_ends_parses_utc_forms():
    stamp = datetime.datetime(2026, 11, 18, 3, 0, tzinfo=datetime.timezone.utc)
    assert reset.practice_ends("2026-11-18T03:00:00Z") == stamp
    assert reset.practice_ends("2026-11-18T03:00:00+00:00") == stamp
    # Naive is read as UTC, not as the box's local time.
    assert reset.practice_ends("2026-11-18T03:00:00") == stamp
    # An offset is honoured rather than ignored.
    assert reset.practice_ends("2026-11-18T04:00:00+01:00") == stamp


def test_practice_ends_unset_means_no_practice_period():
    assert reset.practice_ends("") is None
    assert reset.practice_ends("   ") is None


def test_practice_ends_rejects_nonsense():
    with pytest.raises(ValueError):
        reset.practice_ends("next tuesday")


def test_wipe_scheduled_on_practice_ends_still_runs():
    """The boundary the whole design turns on: the slot landing exactly on
    PRACTICE_ENDS is the last wipe, and it happens."""
    ends = datetime.datetime(2026, 11, 18, 3, 0, tzinfo=datetime.timezone.utc)
    day_before = datetime.datetime(2026, 11, 17, 12, 0, tzinfo=datetime.timezone.utc)
    assert reset.next_run(day_before, ends) == ends
    # Woken late for that slot, it is still the slot that is due.
    late = ends + datetime.timedelta(seconds=30)
    assert reset.next_run(late, ends) is None


def test_no_wipe_is_ever_scheduled_during_the_event():
    """A forgotten container must not empty the board mid-event."""
    ends = datetime.datetime(2026, 11, 18, 3, 0, tzinfo=datetime.timezone.utc)
    for day in (18, 19, 20):
        during = datetime.datetime(2026, 11, day, 10, 0,
                                   tzinfo=datetime.timezone.utc)
        assert reset.next_run(during, ends) is None


def test_wipes_repeat_daily_at_the_practice_ends_time_of_day():
    ends = datetime.datetime(2026, 11, 18, 3, 0, tzinfo=datetime.timezone.utc)
    now = datetime.datetime(2026, 9, 3, 9, 30, tzinfo=datetime.timezone.utc)
    first = reset.next_run(now, ends)
    # Past today's 03:00, so the next one is tomorrow's.
    assert first == datetime.datetime(2026, 9, 4, 3, 0,
                                      tzinfo=datetime.timezone.utc)
    second = reset.next_run(first + datetime.timedelta(seconds=1), ends)
    assert second == first + datetime.timedelta(days=1)


def test_health_advertises_the_practice_period(client, monkeypatch):
    monkeypatch.setattr(app_module, "PRACTICE_ENDS", "2026-11-18T03:00:00Z")
    assert param_one(client.get("/fhir/$status"), "practiceEnds") == "2026-11-18T03:00:00Z"


def test_health_reports_no_practice_period_when_unset(client, monkeypatch):
    monkeypatch.setattr(app_module, "PRACTICE_ENDS", "")
    assert param_one(client.get("/fhir/$status"), "practiceEnds") is None


# ------------------------------------------------------ EHDS document types


def doc_bundle(profile=None, comp_type=None, resources=(), btype="document"):
    """A bundle shaped just enough for detect_document to have to decide."""
    entries = [{"resource": {"resourceType": rt, "id": "r%d" % i}}
               for i, rt in enumerate(resources)]
    if comp_type is not None:
        entries.insert(0, {"resource": {
            "resourceType": "Composition", "status": "final",
            "type": {"coding": [{"system": "http://loinc.org", "code": comp_type}]},
        }})
    body = {"resourceType": "Bundle", "type": btype, "entry": entries}
    if profile:
        body["meta"] = {"profile": [profile]}
    return body


def doc_body(*args, **kwargs):
    return json.dumps(doc_bundle(*args, **kwargs))


def secondary_bundle(doc_id):
    """A bundle that detects as the given non-EPS category.

    MPD defines no Bundle profile at all, so it has nothing in `bundles` to
    claim and its evidence has to sit one level down, on an entry. Handling
    that here rather than at the call sites is what lets a test cover all
    three secondary categories instead of only the two that are documents.
    """
    doc = app_module.DOCUMENTS_BY_ID[doc_id]
    if doc["bundles"]:
        return doc_body(doc["bundles"][0])
    return json.dumps({"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "MedicationRequest",
                      "meta": {"profile": [doc["entry_profiles"][0]]}}}]})


# ------------------------------------------------------------ detection

@pytest.mark.parametrize("doc_id", ["eps", "laboratory", "imaging"])
def test_a_bundle_is_detected_from_the_profile_it_claims(doc_id):
    """The claim is the submitter saying what they built, and it outranks every
    other signal - it is the only one that can tell the two imaging bundle
    shapes apart from anything else."""
    doc = app_module.DOCUMENTS_BY_ID[doc_id]
    for canonical in doc["bundles"]:
        assert app_module.detect_document(doc_bundle(canonical))["id"] == doc_id
        # A team that pins the version means the same profile.
        versioned = doc_bundle(canonical + "|9.9.9")
        assert app_module.detect_document(versioned)["id"] == doc_id


def test_an_mpd_bundle_is_detected_from_what_its_entries_claim():
    """A prescription has no bundle-level profile to claim - MPD profiles
    MedicationRequest and MedicationDispense and defines no Bundle at all - so
    its evidence sits one level down."""
    body = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "MedicationRequest", "meta": {"profile": [
            "http://hl7.eu/fhir/mpd/StructureDefinition/MedicationRequest-eu-mpd"]}}},
    ]}
    assert app_module.detect_document(body)["id"] == "mpd"


@pytest.mark.parametrize("code,doc_id", [("60591-5", "eps"),
                                         ("11502-2", "laboratory")])
def test_a_document_claiming_nothing_is_detected_from_its_composition_type(code, doc_id):
    assert app_module.detect_document(doc_bundle(comp_type=code))["id"] == doc_id


def test_an_imaging_report_that_claims_nothing_is_found_by_its_imaging_study():
    """Imaging binds Composition.type to every LOINC descendant of 18726-0,
    which needs a terminology server to decide - its own examples use 24802-1
    and even a CPT code. So the ImagingStudy stands in for the code check."""
    body = doc_bundle(comp_type="24802-1", resources=("ImagingStudy",))
    assert app_module.detect_document(body)["id"] == "imaging"


def test_a_patient_summary_is_not_mistaken_for_a_prescription():
    """A summary carries a medication section. Reading one as a prescription
    would check it against base Bundle and let a broken summary pass."""
    body = doc_bundle(comp_type="60591-5",
                      resources=("MedicationStatement", "MedicationRequest"))
    assert app_module.detect_document(body)["id"] == "eps"


def test_an_unrecognisable_bundle_falls_back_to_the_events_own_track():
    """Rejecting it would be worse: the patient summary is what the hackathon
    is about, and an unlabelled bundle is scored exactly as it was before any
    of the other categories existed. It also means not claiming a category can
    never accidentally demote a summary out of the scoring."""
    for body in ({}, {"resourceType": "Bundle"}, doc_bundle(), "not a bundle", None):
        assert app_module.detect_document(body)["id"] == "eps"
        assert app_module.detect_document(body)["primary"] is True


# ---------------------------------------------------- validation and storage

def test_each_category_is_validated_against_its_own_profile(client, monkeypatch):
    """Matchbox scopes the engine to the requested profile's closure, so
    submitting a lab report with the summary's packages would come back with
    its conformance unchecked rather than checked."""
    seen = {}

    def capture(body, doc=None):
        seen["doc"] = doc
        return outcome()

    monkeypatch.setattr(app_module, "validate_with_matchbox", capture)
    token = register(client, "Alpha")
    lab = app_module.DOCUMENTS_BY_ID["laboratory"]
    res = client.post("/fhir/Bundle/$validate", content=doc_body(lab["bundles"][0]),
                      headers={"X-Team-Token": token})
    assert res.status_code == 200
    assert seen["doc"]["id"] == "laboratory"
    assert res.headers["X-Document-Type"] == "laboratory"
    assert res.headers["X-Document-Profile"] == lab["profile"]


def test_naming_a_profile_settles_the_category(client, monkeypatch):
    """The escape hatch for a document detect_document reads wrongly: $validate
    already takes a profile, so the explicit answer goes where FHIR puts it."""
    imaging = app_module.DOCUMENTS_BY_ID["imaging"]
    token = register(client, "Alpha")
    set_outcome(monkeypatch, outcome())
    # The premise, asserted rather than assumed: without the override this
    # bundle is a patient summary. Imaging is the one category detect_document
    # can reach from a hard-coded branch (an ImagingStudy in the bundle), so
    # loosening that branch could otherwise make this pass while the override
    # itself was broken.
    assert app_module.detect_document(json.loads(BUNDLE))["id"] == "eps"
    res = client.post("/fhir/Bundle/$validate?profile=" + imaging["profile"],
                      content=BUNDLE, headers={"X-Team-Token": token})
    assert res.status_code == 200
    assert res.headers["X-Document-Type"] == "imaging"


def test_a_profile_this_event_does_not_score_is_still_refused(client, monkeypatch):
    """Honouring an arbitrary profile would let a team pick one their bundle
    already satisfies and score full marks on it."""
    token = register(client, "Alpha")
    set_outcome(monkeypatch, outcome())
    res = client.post(
        "/fhir/Bundle/$validate?profile=http://example.org/easy",
        content=BUNDLE, headers={"X-Team-Token": token})
    assert res.status_code == 400
    assert app_module.PROFILE in diagnostics(res)


def test_the_stored_category_is_the_one_the_bundle_was_scored_as(client, monkeypatch):
    token = register(client, "Alpha")
    submit(client, token, outcome(), monkeypatch, body=secondary_bundle("imaging"))
    row = param_entries(client.get("/fhir/$submissions"))[0]
    assert row["documentType"] == "imaging"
    # The download is named for what it is, not for what the event started as.
    dl = client.get("/fhir/Bundle/%d" % row["id"])
    assert "-imaging.json" in dl.headers["content-disposition"]


def test_an_old_database_gains_the_category_column_as_a_patient_summary(tmp_path,
                                                                       monkeypatch):
    """Every submission taken before this existed was scored against the
    patient summary, so that is what it has to backfill as."""
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))
    os.makedirs(os.path.join(str(tmp_path), "submissions"), exist_ok=True)
    with sqlite3.connect(os.path.join(str(tmp_path), "connectathon.db")) as conn:
        conn.executescript(
            """
            CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT, slug TEXT,
                token TEXT, first_pass INTEGER DEFAULT 0, created_at TEXT);
            CREATE TABLE submissions (id INTEGER PRIMARY KEY, team_id INTEGER,
                ts TEXT, pass INTEGER, errors INTEGER, warnings INTEGER,
                infos INTEGER, score INTEGER, sections INTEGER, resources INTEGER,
                filename TEXT);
            INSERT INTO teams VALUES (1, 'Old', 'old', 'tok', 0, '2026-01-01');
            INSERT INTO submissions VALUES
                (1, 1, '2026-01-01', 1, 0, 0, 0, 60, 1, 5, 'a.json');
            """
        )
    app_module.init_db()
    with app_module.connect() as conn:
        assert conn.execute(
            "SELECT doc_type FROM submissions WHERE id = 1").fetchone()[0] == "eps"


def test_a_retired_category_still_in_the_database_scores_nothing(client, monkeypatch):
    """A category can be dropped from the registry while its rows survive.

    The discharge report was removed after teams had already practised against
    it, and reset.py only wipes daily until PRACTICE_ENDS while backup.py
    restores whatever it snapshotted - so a passing `hdr` row can outlive the
    category itself. The submit header and the leaderboard count the bonus by
    different means, and they have to agree on what a retired doc_type is
    worth: nothing. Counting it in one place and not the other put a 15-point
    contradiction between a team's banner and the board next to it.
    """
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    with app_module.connect() as conn:
        team_id = conn.execute(
            "SELECT id FROM teams WHERE slug = 'alpha'").fetchone()[0]
        conn.execute(
            "INSERT INTO submissions (team_id, ts, pass, errors, warnings, infos, "
            "score, sections, resources, doc_type, filename) "
            "VALUES (?, '2026-01-01', 1, 0, 0, 0, 0, 0, 0, 'hdr', 'old.json')",
            (team_id,))
    assert "hdr" not in {d["id"] for d in app_module.EHDS_DOCUMENTS}

    res = submit(client, token, outcome(), monkeypatch)
    assert res.headers["X-Secondary-Categories"] == "0"
    row = next(r for r in param_entries(client.get("/fhir/$leaderboard"))
               if r["organizationSlug"] == "alpha")
    assert part_list(row, "secondaryCategories") == []
    # The two ways of counting agree, which is the actual invariant.
    assert int(res.headers["X-Score"]) == row["score"]


def test_the_page_names_every_category_it_can_be_asked_about(client):
    """Three lists in the page script mirror the registry by hand.

    DOC_LABELS is refreshed from $documents so it self-heals, but DOC_SHORT and
    EXPLAIN_GUIDES are const and never refreshed - and both fail soft, to the
    raw id and to the summary's guide respectively. A category added to the
    registry and forgotten here sends a team to the wrong specification with
    nothing to show for it, which is precisely the drift the guide link exists
    to prevent.
    """
    js = page_script(client)
    ids = {d["id"] for d in app_module.EHDS_DOCUMENTS}
    for name in ("DOC_SHORT", "EXPLAIN_GUIDES"):
        start = js.index("const %s = " % name)
        # Anchored on the separator, so a key is only a key where one can
        # start - "https:" inside a guide URL is not one.
        listed = set(re.findall(r"[{,]\s*(\w+):", js[start:js.index("};", start)]))
        assert listed == ids, (name, listed ^ ids)


# ------------------------------------------------- EPS is the primary citizen

def test_a_company_that_only_sends_summaries_scores_what_it_always_did(client,
                                                                      monkeypatch):
    """The whole point of the demotion: adding three categories to the service
    cannot move a single score that existed before them."""
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    res = submit(client, token, outcome(), monkeypatch,
                 body=bundle_with_section(
                     {"title": "Allergies",
                      "entry": [{"reference": "urn:uuid:res-0"}]}, resources=3))
    assert res.headers["X-Score"] == str(50 + 10 + 3 * 2)
    assert res.headers["X-Secondary-Categories"] == "0"


def test_a_secondary_category_earns_a_flat_bonus_not_richness(client, monkeypatch):
    """Richness is measured against what a patient summary is meant to carry;
    counting sections on a lab report would only reward the wrong shape."""
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    lab = app_module.DOCUMENTS_BY_ID["laboratory"]["bundles"][0]
    # A deliberately rich lab bundle: sections and resources buy it nothing.
    body = doc_bundle(lab, comp_type="11502-2",
                      resources=("Observation",) * 20)
    res = submit(client, token, outcome(), monkeypatch, body=json.dumps(body))
    assert res.headers["X-Document-Type"] == "laboratory"
    assert res.headers["X-Submission-Score"] == str(app_module.SECONDARY_CATEGORY_POINTS)
    assert res.headers["X-Score"] == str(app_module.SECONDARY_CATEGORY_POINTS)


def test_a_stack_of_other_categories_cannot_out_score_one_good_summary(client,
                                                                      monkeypatch):
    """This is the patient summary track. Passing every other category is worth
    less than a single well-built summary, and that has to stay true."""
    consume_first_pass(client, monkeypatch)
    breadth = register(client, "Breadth")
    for doc_id in ("laboratory", "mpd", "imaging"):
        doc = app_module.DOCUMENTS_BY_ID[doc_id]
        body = (doc_body(doc["bundles"][0]) if doc["bundles"]
                else json.dumps({"resourceType": "Bundle", "type": "collection",
                                 "entry": [{"resource": {
                                     "resourceType": "MedicationRequest",
                                     "meta": {"profile": [doc["entry_profiles"][0]]}}}]}))
        res = submit(client, breadth, outcome(), monkeypatch, body=body)
        assert res.headers["X-Document-Type"] == doc_id, res.headers

    depth = register(client, "Depth")
    res = submit(client, depth, outcome(), monkeypatch,
                 body=bundle(sections=2, resources=2))

    board = {r["organization"]: r for r in param_entries(client.get("/fhir/$leaderboard"))}
    assert board["Breadth"]["score"] == (
        len(app_module.SECONDARY_DOCUMENTS) * app_module.SECONDARY_CATEGORY_POINTS)
    assert board["Depth"]["score"] > board["Breadth"]["score"]
    assert board["Depth"]["rank"] < board["Breadth"]["rank"]
    # And the breadth company is honestly described: it has passed things, but
    # it has no patient summary at all.
    assert board["Breadth"]["passed"] is True
    assert "bestPatientSummary" not in board["Breadth"]
    assert board["Depth"]["bestPatientSummary"] == 74


def test_the_first_pass_bonus_belongs_to_the_patient_summary(client, monkeypatch):
    """It is the track's own prize. Handing it to whoever happened to bring the
    easiest other category would make it the opposite of what it is for."""
    lab_only = register(client, "Lab Only")
    res = submit(client, lab_only, outcome(), monkeypatch,
                 body=secondary_bundle("laboratory"))
    assert res.headers["X-Score"] == str(app_module.SECONDARY_CATEGORY_POINTS)

    summariser = register(client, "Summariser")
    res = submit(client, summariser, outcome(), monkeypatch)
    assert res.headers["X-Score"] == "70"  # the bonus was still there to win

    board = {r["organization"]: r for r in param_entries(client.get("/fhir/$leaderboard"))}
    assert board["Lab Only"]["firstPass"] is False
    assert board["Summariser"]["firstPass"] is True


def test_a_second_category_adds_its_bonus_once(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    eps = submit(client, token, outcome(), monkeypatch, body=bundle(sections=1, resources=1))
    base = int(eps.headers["X-Score"])
    assert eps.headers["X-Secondary-Categories"] == "0"

    first_lab = submit(client, token, outcome(), monkeypatch,
                       body=secondary_bundle("laboratory"))
    assert first_lab.headers["X-Secondary-Categories"] == "1"
    assert int(first_lab.headers["X-Score"]) == base + app_module.SECONDARY_CATEGORY_POINTS

    # Same category again: nothing more to earn.
    again = submit(client, token, outcome(), monkeypatch,
                   body=secondary_bundle("laboratory"))
    assert again.headers["X-Secondary-Categories"] == "1"
    assert int(again.headers["X-Score"]) == base + app_module.SECONDARY_CATEGORY_POINTS

    row = next(r for r in param_entries(client.get("/fhir/$leaderboard"))
               if r["organizationSlug"] == "alpha")
    assert part_list(row, "secondaryCategories") == ["laboratory"]
    assert row["score"] == base + app_module.SECONDARY_CATEGORY_POINTS


def test_the_richness_columns_stay_empty_without_a_summary(client, monkeypatch):
    """They mean "how rich is your patient summary". Filling them from a lab
    report makes the number incomparable with the row above it, and the status
    and category chips already show the company did submit."""
    consume_first_pass(client, monkeypatch)
    token = register(client, "Lab Only")
    submit(client, token, outcome(), monkeypatch, body=secondary_bundle("laboratory"))
    row = next(r for r in param_entries(client.get("/fhir/$leaderboard"))
               if r["organizationSlug"] == "lab-only")
    assert row["passed"] is True
    assert part_list(row, "secondaryCategories") == ["laboratory"]
    assert "bestSections" not in row
    assert "bestResources" not in row
    # Still reported: it is a progress signal across every category, not a
    # term of the score.
    assert row["bestErrors"] == 0

    # Once a summary lands, the columns describe it.
    submit(client, token, outcome(), monkeypatch, body=bundle(sections=3, resources=4))
    row = next(r for r in param_entries(client.get("/fhir/$leaderboard"))
               if r["organizationSlug"] == "lab-only")
    assert row["bestSections"] == 3
    assert row["bestResources"] == 4


def test_a_company_with_no_summary_is_told_its_summary_is_missing(client, monkeypatch):
    """X-Score alone cannot tell "no summary yet" from "a summary worth 0", and
    the banner has to say which. A summary that passes always scores at least
    VALID_BASE, so 0 here can only mean there is none."""
    consume_first_pass(client, monkeypatch)
    token = register(client, "Lab Only")
    lab = submit(client, token, outcome(), monkeypatch,
                 body=secondary_bundle("laboratory"))
    assert lab.headers["X-Best-Patient-Summary"] == "0"
    assert lab.headers["X-Score"] == str(app_module.SECONDARY_CATEGORY_POINTS)

    eps = submit(client, token, outcome(), monkeypatch,
                 body=bundle(sections=2, resources=3))
    assert eps.headers["X-Best-Patient-Summary"] == eps.headers["X-Submission-Score"]
    assert int(eps.headers["X-Best-Patient-Summary"]) >= app_module.VALID_BASE


def test_the_standing_headers_account_for_every_point_of_the_score(client, monkeypatch):
    """The banner spells the standing out from these parts rather than from
    X-Score, so any point they do not name is a point that goes missing on
    screen. That is exactly how the first-pass bonus was lost once."""
    token = register(client, "Alpha")
    eps = submit(client, token, outcome(), monkeypatch, body=bundle(sections=6, resources=25))
    lab = submit(client, token, outcome(), monkeypatch,
                 body=secondary_bundle("laboratory"))
    img = submit(client, token, outcome(), monkeypatch,
                 body=secondary_bundle("imaging"))

    for res in (eps, lab, img):
        best = int(res.headers["X-Best-Patient-Summary"])
        extras = int(res.headers["X-Secondary-Categories"])
        bonus = app_module.FIRST_PASS_BONUS if res.headers["X-First-Pass"] == "true" else 0
        parts = best + bonus + app_module.SECONDARY_CATEGORY_POINTS * extras
        assert parts == int(res.headers["X-Score"]), res.headers["X-Document-Type"]

    assert img.headers["X-First-Pass"] == "true"
    assert img.headers["X-Secondary-Categories"] == "2"


def test_a_company_without_the_first_pass_bonus_is_not_told_it_has_one(client, monkeypatch):
    consume_first_pass(client, monkeypatch)
    token = register(client, "Latecomer")
    res = submit(client, token, outcome(), monkeypatch, body=bundle(sections=2, resources=2))
    assert res.headers["X-First-Pass"] == "false"
    assert res.headers["X-Score"] == res.headers["X-Best-Patient-Summary"]


def test_the_summary_header_holds_the_best_summary_not_the_last_one(client, monkeypatch):
    """It is the standing's summary term, so a later, worse summary must not
    move it - the banner would otherwise show arithmetic that does not add up."""
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    rich = submit(client, token, outcome(), monkeypatch,
                  body=bundle(sections=4, resources=6))
    best = int(rich.headers["X-Submission-Score"])
    thin = submit(client, token, outcome(), monkeypatch,
                  body=bundle(sections=1, resources=1))
    assert int(thin.headers["X-Submission-Score"]) < best
    assert thin.headers["X-Best-Patient-Summary"] == str(best)


def test_a_category_only_counts_once_it_actually_passes(client, monkeypatch):
    """Otherwise a team could bank the bonus by posting a broken bundle of
    every kind, which is the opposite of what it is for."""
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    submit(client, token, outcome(), monkeypatch)
    failed = submit(client, token, outcome(errors=2), monkeypatch,
                    body=secondary_bundle("laboratory"))
    assert failed.headers["X-Secondary-Categories"] == "0"
    assert failed.headers["X-Submission-Score"] == "0"
    row = next(r for r in param_entries(client.get("/fhir/$leaderboard"))
               if r["organizationSlug"] == "alpha")
    assert part_list(row, "secondaryCategories") == []
    assert row["score"] == 50


def test_the_secondary_categories_are_listed_in_registry_order(client, monkeypatch):
    """So the column reads the same for every team however they got there."""
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    # Submitted in an order that is neither registry order, nor its reverse,
    # nor alphabetical either way - so no accumulation that merely echoes the
    # submissions can produce the expected list by accident.
    for doc_id in ("imaging", "laboratory", "mpd"):
        submit(client, token, outcome(), monkeypatch, body=secondary_bundle(doc_id))
    row = next(r for r in param_entries(client.get("/fhir/$leaderboard"))
               if r["organizationSlug"] == "alpha")
    assert part_list(row, "secondaryCategories") == ["laboratory", "mpd", "imaging"]


def test_the_richness_columns_describe_the_summary_not_the_last_lab_report(client,
                                                                          monkeypatch):
    """The standing rests on the best patient summary, so that is what the
    Sections and Resources columns have to be describing."""
    consume_first_pass(client, monkeypatch)
    token = register(client, "Alpha")
    submit(client, token, outcome(), monkeypatch, body=bundle(sections=2, resources=2))
    # A lab report carrying more of everything must not take over the columns.
    submit(client, token, outcome(), monkeypatch,
           body=json.dumps(doc_bundle(
               app_module.DOCUMENTS_BY_ID["laboratory"]["bundles"][0],
               resources=("Observation",) * 9)))
    row = next(r for r in param_entries(client.get("/fhir/$leaderboard"))
               if r["organizationSlug"] == "alpha")
    assert row["bestSections"] == 2
    assert row["bestResources"] == 2
    assert row["bestPatientSummary"] == 74


# ------------------------------------------------------------- $documents

def test_documents_lists_every_category_and_whether_it_is_loaded(client, monkeypatch):
    monkeypatch.setattr(app_module, "loaded_ig_packages",
                        lambda: {"hl7.fhir.eu.eps#1.0.0-ballot",
                                 "hl7se.fhir.base#1.1.0"})
    docs = param_entries(client.get("/fhir/$documents"))
    assert [d["id"] for d in docs] == [d["id"] for d in app_module.EHDS_DOCUMENTS]
    by_id = {d["id"]: d for d in docs}
    assert by_id["eps"]["ready"] is True
    assert by_id["laboratory"]["ready"] is False
    assert by_id["eps"]["ehdsCategory"] == "Patient summary"
    assert by_id["mpd"]["ehdsCategory"] == "ePrescription / eDispensation"


def test_documents_puts_the_patient_summary_first_and_flags_it(client):
    """A client reading this has to be able to tell which one the event is
    about without knowing the event."""
    docs = param_entries(client.get("/fhir/$documents"))
    assert docs[0]["id"] == "eps"
    assert docs[0]["primary"] is True
    assert all(d["primary"] is False for d in docs[1:])
    assert str(app_module.SECONDARY_CATEGORY_POINTS) in docs[1]["points"]


def test_documents_says_unknown_rather_than_missing_when_matchbox_is_unreachable(
        client, monkeypatch):
    """"We could not ask" and "the packages are not there" send an organiser to
    two different places."""
    monkeypatch.setattr(app_module, "loaded_ig_packages", lambda: None)
    docs = param_entries(client.get("/fhir/$documents"))
    assert all("ready" not in d for d in docs)


def test_the_laboratory_guide_keeps_the_base_ballot_that_holds_its_missing_profile():
    """DiagnosticReport-eu-lab points at patient-animal-eu-core, which eu.base
    2.0.0 dropped and only 2.0.0-ballot still defines. Without the ballot in
    scope every lab bundle - the IG's own examples included - fails to resolve
    it and scores 0."""
    igs = app_module.DOCUMENTS_BY_ID["laboratory"]["igs"]
    assert "hl7.fhir.eu.base#2.0.0-ballot" in igs


# ---------------------------------------------------------------- usage log and monitor

UTC = datetime.timezone.utc


def usage(client, sql, *args):
    conn = sqlite3.connect(os.path.join(client.data_dir, "usage.db"))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


@pytest.fixture()
def mailbox(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor, "send_mail",
                        lambda subject, body: sent.append((subject, body)) or True)
    return sent


def test_requests_are_counted_by_route_template_and_by_who_sent_them(client):
    client.get("/fhir/Bundle/1")
    client.get("/fhir/Bundle/2")
    client.get("/fhir/$leaderboard", headers={"Sec-Fetch-Site": "same-origin"})
    client.get("/wp-login.php")
    rows = usage(client, "SELECT method, route, status, via, count FROM hits ORDER BY route")
    assert ("GET", "/fhir/Bundle/{bundle_id}", 404, "client", 2) in rows
    assert ("GET", "/fhir/$leaderboard", 200, "browser", 1) in rows
    # A scanner's made-up path is one bucket, not a row per guess.
    assert ("GET", "(no route)", 404, "client", 1) in rows
    assert not [r for r in rows if "wp-login" in r[1]]


def test_visitors_are_hashed_per_day_and_not_stored_as_addresses(client):
    client.get("/", headers={"Sec-Fetch-Site": "none", "X-Real-IP": "203.0.113.7"})
    client.get("/fhir/$leaderboard", headers={"Sec-Fetch-Site": "same-origin",
                                               "X-Real-IP": "203.0.113.7"})
    client.get("/fhir/$leaderboard", headers={"X-Real-IP": "198.51.100.1"})
    rows = usage(client, "SELECT via, visitor FROM visitors ORDER BY via")
    assert [via for via, _ in rows] == ["browser", "client"]
    assert all("203.0.113" not in v and "198.51" not in v for _, v in rows)


def test_crawlers_and_link_checkers_are_hits_but_not_visitors(client):
    client.get("/", headers={"User-Agent": "Googlebot/2.1"})
    client.get("/", headers={"User-Agent": "python-requests/2.32"})
    assert usage(client, "SELECT SUM(count) FROM hits WHERE route = '/'") == [(2,)]
    assert usage(client, "SELECT COUNT(*) FROM visitors") == [(0,)]


def test_the_monitors_own_probe_is_not_counted(client):
    client.get("/fhir/$status", headers={"User-Agent": monitor.USER_AGENT})
    assert not os.path.exists(os.path.join(client.data_dir, "usage.db"))


def test_registrations_and_submissions_are_kept_as_events(client, monkeypatch):
    tok = register(client, "Acme Health")
    assert submit(client, tok, outcome(), monkeypatch).status_code == 200
    assert submit(client, tok, outcome(errors=2), monkeypatch).status_code == 200
    rows = usage(client, "SELECT kind, team, detail FROM events ORDER BY id")
    assert rows[0][:2] == ("register", "Acme Health")
    subs = [json.loads(r[2]) for r in rows[1:]]
    assert [(r[0], r[1]) for r in rows[1:]] == [("submission", "acme-health")] * 2
    assert [(s["doc"], s["passed"], s["errors"]) for s in subs] == [
        ("eps", True, 0), ("eps", False, 2)]


def test_the_events_survive_the_practice_reset(client, monkeypatch):
    register(client, "Acme Health")
    reset.clear(client.data_dir)
    assert usage(client, "SELECT team FROM events") == [("Acme Health",)]


def test_a_validator_outage_is_an_error_row_with_matchboxs_complaint(client, monkeypatch):
    tok = register(client, "Acme Health")

    def down(body, doc=None):
        raise app_module.MatchboxError("Matchbox timed out after 60s")
    monkeypatch.setattr(app_module, "validate_with_matchbox", down)
    res = client.post("/fhir/Bundle/$validate", content=BUNDLE,
                      headers={"X-Team-Token": tok, "Content-Type": "application/fhir+json"})
    assert res.status_code == 502
    assert usage(client, "SELECT source, method, route, status, message FROM errors") == [
        ("server", "POST", "/fhir/Bundle/$validate", 502, "Matchbox timed out after 60s")]


def test_an_unhandled_exception_is_recorded_with_its_traceback(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_DIR", str(tmp_path))

    def explode():
        raise RuntimeError("the leaderboard query fell over")
    monkeypatch.setattr(app_module, "_leaderboard_rows", explode)
    with TestClient(app_module.app, raise_server_exceptions=False) as c:
        assert c.get("/fhir/$leaderboard").status_code == 500
    conn = sqlite3.connect(str(tmp_path / "usage.db"))
    (route, status, message), = conn.execute(
        "SELECT route, status, message FROM errors").fetchall()
    conn.close()
    assert (route, status) == ("/fhir/$leaderboard", 500)
    assert message.rstrip().endswith("RuntimeError: the leaderboard query fell over")


def test_client_errors_are_counted_but_are_not_alerts(client):
    client.post("/fhir/Bundle/$validate", content=BUNDLE)
    assert usage(client, "SELECT status FROM hits") == [(401,)]
    assert usage(client, "SELECT COUNT(*) FROM errors") == [(0,)]


def test_a_broken_usage_log_costs_a_row_and_not_the_request(client, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(app_module, "USAGE_DB", str(blocker / "usage.db"))
    tok = register(client, "Acme Health")
    assert submit(client, tok, outcome(), monkeypatch).status_code == 200


def test_the_page_reports_its_own_errors(client):
    res = client.post("/ui/error", json={"message": "x is undefined",
                                         "stack": "at render (/:1200:5)", "page": "/#tab-interop"})
    assert res.status_code == 204
    (source, message), = usage(client, "SELECT source, message FROM errors")
    assert source == "browser"
    assert message.splitlines() == ["x is undefined", "at render (/:1200:5)", "/#tab-interop"]
    assert client.post("/ui/error", content=b"[1]").status_code == 400
    assert client.post("/ui/error", content=b"x" * 20000).status_code == 413


def test_the_page_installs_the_error_reporter_before_its_own_script():
    page = app_module.INDEX_HTML
    assert page.index('fetch("/ui/error"') < page.index('"use strict";')


def test_error_rows_stop_at_the_daily_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "MAX_ERRORS_PER_DAY", 3)
    path = str(tmp_path / "usage.db")
    for i in range(5):
        monitor.record_error(path, "browser", "noise %d" % i)
    monitor.record_error(path, "server", "real")
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT source, COUNT(*) FROM errors GROUP BY source ORDER BY source"
                        ).fetchall() == [("browser", 3), ("server", 1)]
    conn.close()


def test_a_long_traceback_keeps_its_end():
    msg = "frame\n" * 2000 + "ValueError: the real problem"
    clipped = monitor._clip(msg)
    assert len(clipped) <= monitor.MAX_MESSAGE_CHARS + 4
    assert clipped.endswith("ValueError: the real problem")


def test_the_first_error_mails_at_once_and_the_rest_wait_for_the_window(tmp_path, mailbox):
    path = str(tmp_path / "usage.db")
    t0 = datetime.datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    monitor.record_error(path, "server", "boom", method="POST",
                         route="/fhir/Bundle/$validate", status=502)
    assert monitor.alert_errors(path, t0)
    assert mailbox[0][0] == "1 error: 502 /fhir/Bundle/$validate"
    assert "boom" in mailbox[0][1]
    # Nothing new: nothing sent.
    assert not monitor.alert_errors(path, t0 + datetime.timedelta(hours=1))
    monitor.record_error(path, "server", "boom again", status=502, route="/x")
    monitor.record_error(path, "browser", "oops")
    later = t0 + datetime.timedelta(hours=1, minutes=5)
    assert monitor.alert_errors(path, later)
    monitor.record_error(path, "server", "and again", status=502, route="/x")
    assert not monitor.alert_errors(path, later + datetime.timedelta(minutes=5))
    assert monitor.alert_errors(path, later + datetime.timedelta(seconds=monitor.ALERT_EVERY))
    assert [s for s, _ in mailbox] == ["1 error: 502 /fhir/Bundle/$validate",
                                       "2 errors: 502 /x", "1 error: 502 /x"]


def test_errors_stay_unalerted_when_the_mail_does_not_go(tmp_path, monkeypatch):
    path = str(tmp_path / "usage.db")
    monitor.record_error(path, "server", "boom", status=500)
    monkeypatch.setattr(monitor, "send_mail", lambda subject, body: False)
    assert not monitor.alert_errors(path)
    sent = []
    monkeypatch.setattr(monitor, "send_mail", lambda s, b: sent.append(s) or True)
    assert monitor.alert_errors(path)
    assert sent == ["1 error: 500"]


def test_an_outage_is_one_mail_and_its_recovery_another(mailbox, monkeypatch):
    monkeypatch.setattr(monitor, "HEALTH_FAILURES", 3)
    health = monitor.Health()
    t0 = datetime.datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    for i in range(6):
        health.observe("validator is down", t0 + datetime.timedelta(minutes=i))
    assert [s for s, _ in mailbox] == ["service not healthy: validator is down"]
    health.observe(None, t0 + datetime.timedelta(minutes=10))
    health.observe(None, t0 + datetime.timedelta(minutes=11))
    assert [s for s, _ in mailbox][1:] == ["recovered after 10 min"]


def test_a_blip_shorter_than_the_threshold_mails_nothing(mailbox, monkeypatch):
    monkeypatch.setattr(monitor, "HEALTH_FAILURES", 3)
    health = monitor.Health()
    health.observe("validator is down")
    health.observe("validator is down")
    health.observe(None)
    health.observe("validator is down")
    assert mailbox == []


def test_the_probe_reads_matchbox_out_of_status(monkeypatch):
    def answer(payload):
        return lambda req, timeout: io.BytesIO(json.dumps(payload).encode())
    up = {"resourceType": "Parameters", "parameter": [{"name": "matchbox", "valueString": "up"}]}
    monkeypatch.setattr(monitor.urllib.request, "urlopen", answer(up))
    assert monitor.probe_status("http://x/") is None
    degraded = {"resourceType": "Parameters", "parameter": [
        {"name": "matchbox", "valueString": "degraded"},
        {"name": "missingIg", "valueString": "hl7se.fhir.base#1.1.0"}]}
    monkeypatch.setattr(monitor.urllib.request, "urlopen", answer(degraded))
    assert monitor.probe_status("http://x/") == "validator is degraded (missing hl7se.fhir.base#1.1.0)"

    def refused(req, timeout):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))
    monkeypatch.setattr(monitor.urllib.request, "urlopen", refused)
    assert monitor.probe_status("http://x/").startswith("$status unreachable")


def test_the_digest_tells_yesterday_as_it_happened(client, monkeypatch):
    client.get("/", headers={"Sec-Fetch-Site": "none"})
    tok = register(client, "Acme Health")
    submit(client, tok, outcome(), monkeypatch)
    submit(client, tok, outcome(errors=1), monkeypatch)
    client.post("/fhir/Bundle/$validate", content=BUNDLE, headers={"X-Team-Token": "wrong"})
    monkeypatch.setenv("PRACTICE_ENDS", "2026-11-18T03:00:00Z")
    today = datetime.datetime.now(UTC).date()
    subject, body = monitor.digest(os.path.join(client.data_dir, "usage.db"), today)
    assert subject.startswith(today.isoformat() + ": 2 visitors, 2 submissions")
    assert "Companies registered: 1 - Acme Health" in body
    assert "Submissions: 2 from 1 company, 1 passed" in body
    assert "401 POST /fhir/Bundle/$validate" in body


def test_the_digest_goes_once_a_day_after_its_hour(tmp_path, mailbox, monkeypatch):
    monkeypatch.setattr(monitor, "DIGEST_HOUR", 6)
    path = str(tmp_path / "usage.db")
    day = datetime.datetime(2026, 10, 20, 5, 59, tzinfo=UTC)
    assert not monitor.maybe_send_digest(path, day)
    assert monitor.maybe_send_digest(path, day.replace(hour=6))
    assert not monitor.maybe_send_digest(path, day.replace(hour=23))
    assert mailbox[0][0].startswith("2026-10-19: ")
    assert monitor.maybe_send_digest(path, day + datetime.timedelta(days=1, hours=1))
    assert mailbox[1][0].startswith("2026-10-20: ")


def test_yesterdays_salt_is_gone_once_the_day_is(tmp_path):
    path = str(tmp_path / "usage.db")
    monitor.record_request(path, "GET", "/", 200, "browser", "203.0.113.7", "Firefox")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE salts SET day = '2026-01-01'")
    conn.commit()
    monitor.prune(path)
    assert conn.execute("SELECT COUNT(*) FROM salts").fetchone() == (0,)
    # The counts stay.
    assert conn.execute("SELECT COUNT(*) FROM visitors").fetchone() == (1,)
    conn.close()


def test_without_smtp_a_mail_is_logged_not_lost(monkeypatch, capsys):
    monkeypatch.setattr(monitor, "SMTP_HOST", "")
    assert monitor.send_mail("test", "hello")
    assert "hello" in capsys.readouterr().out


def test_an_unreachable_smtp_server_is_a_failed_send_not_a_crash(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(monitor, "SMTP_HOST", "127.0.0.1")
    monkeypatch.setattr(monitor, "SMTP_PORT", port)
    monkeypatch.setattr(monitor, "MAIL_TO", "organiser@example.org")
    assert monitor.send_mail("test", "hello") is False

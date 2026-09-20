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
import errno
import io
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

import pytest
from fastapi.testclient import TestClient

import app as app_module
import backup
import notify
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
    monkeypatch.setattr(app_module, "MAX_SUBSCRIPTIONS", 5)
    monkeypatch.setattr(app_module, "PUBLIC_URL", "")
    monkeypatch.delenv("SUBSCRIPTION_ALLOW_PRIVATE", raising=False)
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


# ------------------------------------------------------------- sample documents


def test_sample_list_is_labelled_synthetic(client):
    res = client.get("/fhir/$samples")
    assert "does not come from any real system" in param_one(res, "disclaimer")
    entries = param_entries(res)
    assert {s["id"] for s in entries} == {"andrea-cross-border", "arne-complex-care"}
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
        "Bundle", "Organization", "DocumentReference", "Binary", "Subscription"}
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


def test_the_submit_tab_shows_findings_inside_the_document(client):
    text = client.get("/").text
    assert 'id="doc-body"' in text and 'id="doc-flagged"' in text
    assert "function jumpToLine(" in text
    assert "renderIssues(outcome.issue || [], body)" in text     # the submitted bytes
    assert 'class="loc-jump"' in text                            # location jumps to the line
    assert "DOC_MAX_LINES" in text                               # a huge bundle stays usable



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
    conn.execute("CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, team INTEGER)")
    conn.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY, sub INTEGER)")
    conn.execute("INSERT INTO subscriptions (team) VALUES (1)")
    conn.executemany("INSERT INTO notifications (sub) VALUES (?)", [(1,), (1,), (1,)])
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
    assert removed == {"notifications": 3, "subscriptions": 1, "renderings": 1,
                       "submissions": 2, "teams": 1, "team directories": 2}
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


# ---------------------------------------------------------------- subscriptions

TOPIC = app_module.SUBSCRIPTION_TOPIC


class Hook:
    """A subscriber's rest-hook: a real listener on a real socket, because the
    part of delivery worth testing is the part that touches one."""

    def __init__(self):
        import http.server
        hook = self
        self.received = []      # (path, headers, parsed body)
        self.answer = 200
        self.delay = 0          # seconds it sits on a notification before answering
        self.location = "http://127.0.0.1:1/elsewhere"

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                hook.received.append((self.path, dict(self.headers), json.loads(raw)))
                time.sleep(hook.delay)
                self.send_response(hook.answer)
                if 300 <= hook.answer < 400:
                    self.send_header("Location", hook.location)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/hook" % self.server.server_address[1]
        # shutdown() waits out one poll interval, and the default is half a
        # second of every test that has a listener.
        threading.Thread(target=self.server.serve_forever, args=(0.01,),
                         daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def statuses(self):
        """The status Parameters of everything received, flattened."""
        return [
            {p["name"]: _part_value(p) for p in body["entry"][0]["resource"]["parameter"]}
            for _path, _headers, body in self.received
        ]


@pytest.fixture()
def hook(monkeypatch):
    # The listener is on loopback, which is exactly what the service refuses
    # to call unless told it is on a private network.
    monkeypatch.setenv("SUBSCRIPTION_ALLOW_PRIVATE", "1")
    h = Hook()
    yield h
    h.close()


def subscription(endpoint, content=None, filters=(), **extra):
    channel = {"type": "rest-hook", "endpoint": endpoint,
               "payload": "application/fhir+json"}
    if content is not None:
        channel["_payload"] = {"extension": [
            {"url": notify.PAYLOAD_CONTENT_EXT, "valueCode": content}]}
    body = {"resourceType": "Subscription", "status": "requested",
            "criteria": TOPIC, "channel": channel}
    if filters:
        body["_criteria"] = {"extension": [
            {"url": notify.FILTER_CRITERIA_EXT, "valueString": f} for f in filters]}
    body.update(extra)
    return body


def subscribe(client, token, body):
    return client.post("/fhir/Subscription", json=body,
                       headers={"Authorization": "Bearer " + token})


def deliver(client, now=None):
    return notify.deliver_due(client.data_dir, now=now)


def active_subscription(client, token, hook, **kwargs):
    res = subscribe(client, token, subscription(hook.url, **kwargs))
    assert res.status_code == 201, res.text
    assert deliver(client) == (1, 0)
    hook.received.clear()
    return res.json()["id"]


def test_a_subscription_is_requested_until_its_endpoint_answers_the_handshake(
        client, hook):
    tok = register(client, "Subscriber")
    res = subscribe(client, tok, subscription(hook.url))
    assert res.status_code == 201, res.text
    sub = res.json()
    assert sub["status"] == "requested"
    assert res.headers["Location"] == "/fhir/Subscription/" + sub["id"]
    assert sub["meta"]["profile"] == [notify.SUBSCRIPTION_PROFILE]
    # unsaid, the payload content is id-only - and it is said back
    assert sub["channel"]["_payload"]["extension"][0]["valueCode"] == "id-only"

    assert deliver(client) == (1, 0)
    path, headers, body = hook.received[0]
    assert path == "/hook"
    assert headers["Content-Type"] == "application/fhir+json"
    assert body["type"] == "history"
    assert body["meta"]["profile"] == [notify.NOTIFICATION_PROFILE]
    status = hook.statuses()[0]
    assert status["type"] == "handshake" and status["status"] == "requested"
    assert status["topic"] == TOPIC
    assert status["events-since-subscription-start"] == "0"
    assert status["subscription"]["reference"] == \
        "http://testserver/fhir/Subscription/" + sub["id"]

    auth = {"Authorization": "Bearer " + tok}
    assert client.get("/fhir/Subscription/" + sub["id"],
                      headers=auth).json()["status"] == "active"


def test_a_passing_submission_notifies_and_a_failing_one_does_not(
        client, hook, monkeypatch):
    sub_tok = register(client, "Subscriber")
    prod_tok = register(client, "Producer Oy")
    active_subscription(client, sub_tok, hook)

    submit(client, prod_tok, outcome(errors=1), monkeypatch)
    assert deliver(client) == (0, 0)
    res = submit(client, prod_tok, outcome(), monkeypatch)
    assert deliver(client) == (1, 0)

    status = hook.statuses()[0]
    assert status["type"] == "event-notification" and status["status"] == "active"
    event = status["notification-event"]
    assert event["event-number"] == "1"
    # absolute, because the subscriber reads it somewhere our base URL is not
    assert event["focus"]["reference"] == \
        "http://testserver" + res.headers["Content-Location"]
    assert event["additional-context"]["reference"] == \
        "http://testserver/fhir/Organization/producer-oy"
    assert event["additional-context"]["display"] == "Producer Oy"
    # id-only: the document itself is not in the notification, and where it
    # is, is - for whoever walks the entries rather than the Parameters
    assert hook.received[0][2]["entry"][1:] == [{
        "fullUrl": "http://testserver" + res.headers["Content-Location"],
        "request": {"method": "POST", "url": "Bundle"},
        "response": {"status": "201"}}]
    # delivered once, and not again on the next pass
    assert deliver(client) == (0, 0)


def test_full_resource_carries_the_document_under_the_servers_id(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook, content="full-resource")
    submitted = json.loads(bundle(sections=1, resources=2))
    submitted["id"] = "whatever-the-team-called-it"
    res = submit(client, tok, outcome(), monkeypatch, body=json.dumps(submitted))
    deliver(client)
    entry = hook.received[0][2]["entry"][1]
    location = res.headers["Content-Location"]
    assert entry["fullUrl"] == "http://testserver" + location
    # an entry whose id contradicts its fullUrl is one a server may refuse
    assert entry["resource"]["id"] == location.rsplit("/", 1)[1]
    assert entry["resource"]["entry"] == submitted["entry"]
    # ...while what the fullUrl serves is still the submitter's exact bytes
    assert client.get(location).json()["id"] == "whatever-the-team-called-it"


def test_empty_says_something_happened_and_not_what(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook, content="empty")
    submit(client, tok, outcome(), monkeypatch)
    deliver(client)
    event = hook.statuses()[0]["notification-event"]
    assert set(event) == {"event-number", "timestamp"}
    # the IG: no entry but the status, for `empty`
    assert len(hook.received[-1][2]["entry"]) == 1


def test_filters_narrow_by_company_and_by_category(client, hook, monkeypatch):
    sub_tok = register(client, "Subscriber")
    acme = register(client, "Acme")
    other = register(client, "Other")
    active_subscription(client, sub_tok, hook, filters=[
        "Bundle?organization=acme", "document-type=eps,laboratory"])
    imaging = app_module.DOCUMENTS_BY_ID["imaging"]["profile"]

    submit(client, other, outcome(), monkeypatch)            # wrong company
    set_outcome(monkeypatch, outcome())
    res = client.post("/fhir/Bundle/$validate?profile=" + imaging, content=BUNDLE,
                      headers={"X-Team-Token": acme})        # wrong category
    # it did pass, as imaging: a 4xx here would "not notify" for the wrong reason
    assert res.status_code == 200 and res.headers["X-Pass"] == "true", res.text
    assert res.headers["X-Document-Type"] == "imaging"
    submit(client, acme, outcome(), monkeypatch)             # the one
    assert deliver(client) == (1, 0)
    context = hook.statuses()[0]["notification-event"]["additional-context"]
    assert context["reference"].endswith("/Organization/acme")
    # the count is of events *for this subscription*, not submissions
    assert hook.statuses()[0]["events-since-subscription-start"] == "1"


@pytest.mark.parametrize("filters, fragment", [
    (["Bundle?patient=123"], "Unknown filter parameter"),
    (["Encounter?organization=acme"], "not to Encounter"),
    (["document-type=discharge"], "Unknown document-type"),
    (["organization="], "has no value"),
])
def test_a_filter_this_topic_cannot_honour_is_refused(client, filters, fragment):
    """Ignoring it instead would send a subscriber who asked for one company's
    documents everybody's."""
    tok = register(client, "Subscriber")
    res = subscribe(client, tok, subscription("https://example.org/h", filters=filters))
    assert res.status_code == 400
    assert fragment in diagnostics(res)


@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:8090/matchboxv3/fhir",
    "http://localhost/hook",
    "http://10.0.0.5/hook",
    "http://192.168.1.20:8080/hook",
    "http://169.254.169.254/latest/meta-data",
    "http://[::1]/hook",
    "http://[::ffff:10.0.0.5]/hook",
    "http://0.0.0.0/hook",
    "ftp://example.org/hook",
    "https://user:secret@example.org/hook",
    "https://exa mple.org/hook",
    "not a url",
    "",
])
def test_endpoints_this_server_must_not_call_are_refused_at_the_door(client, endpoint):
    tok = register(client, "Subscriber")
    res = subscribe(client, tok, subscription(endpoint))
    assert res.status_code == 400, endpoint
    assert "channel.endpoint" in diagnostics(res)


def test_link_local_is_refused_even_on_a_private_network(client, monkeypatch):
    """SUBSCRIPTION_ALLOW_PRIVATE is for laptops on a venue LAN. The cloud
    metadata address is not one of those."""
    monkeypatch.setenv("SUBSCRIPTION_ALLOW_PRIVATE", "1")
    tok = register(client, "Subscriber")
    assert subscribe(client, tok, subscription("http://10.0.0.5/h")).status_code == 201
    res = subscribe(client, tok, subscription("http://169.254.169.254/h"))
    assert res.status_code == 400


def test_a_name_that_resolves_somewhere_private_is_not_called(monkeypatch):
    """The check the service cannot make: what a hostname turns out to be. Every
    address has to pass, and the connection goes to the one that was vetted."""
    def resolves_to(*addresses):
        monkeypatch.setattr(notify.socket, "getaddrinfo", lambda *a, **k: [
            (None, None, None, "", (addr, 80)) for addr in addresses])

    resolves_to("10.1.2.3")
    with pytest.raises(notify.EndpointError, match="not a public address"):
        notify.resolve_endpoint("http://innocent.example.org/h", False)
    resolves_to("93.184.216.34", "127.0.0.1")
    with pytest.raises(notify.EndpointError):
        notify.resolve_endpoint("http://innocent.example.org/h", False)
    resolves_to("93.184.216.34")
    _parts, address = notify.resolve_endpoint("http://innocent.example.org/h", False)
    assert address == "93.184.216.34"


def test_the_connection_goes_to_the_vetted_address_not_back_to_the_name(
        hook, monkeypatch):
    """A second lookup is where a DNS answer that changed gets in - so the
    second one here does not answer at all. Patching getaddrinfo to say the
    same thing every time would pass with no pinning whatever."""
    port = hook.server.server_address[1]
    real = socket.getaddrinfo
    lookups = []

    def once(host, *a, **k):
        if not str(host).endswith(".example.org"):
            return real(host, *a, **k)
        lookups.append(host)
        assert len(lookups) == 1, "second lookup of %s" % host
        return real("127.0.0.1", *a, **k)
    monkeypatch.setattr(notify.socket, "getaddrinfo", once)
    assert notify.post("http://rebinding.example.org:%d/hook" % port, b"{}", [],
                       private_ok=True) == 200
    assert lookups == ["rebinding.example.org"]
    assert hook.received[0][1]["Host"] == "rebinding.example.org:%d" % port


def test_a_redirect_is_a_failure_not_a_second_url_to_call(client, hook):
    tok = register(client, "Subscriber")
    elsewhere = Hook()      # somewhere that would answer, if it were asked
    try:
        hook.answer, hook.location = 302, elsewhere.url
        subscribe(client, tok, subscription(hook.url))
        assert deliver(client) == (0, 1)
        assert len(hook.received) == 1 and elsewhere.received == []
    finally:
        elsewhere.close()
    sub = client.get("/fhir/Subscription",
                     headers={"Authorization": "Bearer " + tok}).json()
    assert "redirects are not followed" in sub["entry"][0]["resource"]["error"]


def test_the_subscribers_own_headers_are_sent_and_the_reserved_ones_refused(
        client, hook):
    tok = register(client, "Subscriber")
    body = subscription(hook.url)
    body["channel"]["header"] = ["Authorization: Bearer their-secret", "X-Env: dev"]
    assert subscribe(client, tok, body).status_code == 201
    deliver(client)
    assert hook.received[0][1]["Authorization"] == "Bearer their-secret"
    assert hook.received[0][1]["X-Env"] == "dev"

    for bad in (["Host: matchbox"], ["X-A: b\r\nX-Injected: c"], ["no colon"],
                ["Content-Length: 0"], ["X: y"] * 11, "Authorization: x"):
        body["channel"]["header"] = bad
        assert subscribe(client, tok, body).status_code == 400, bad


def test_a_failing_endpoint_is_retried_in_order_then_given_up_on(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    auth = {"Authorization": "Bearer " + tok}
    sub_id = active_subscription(client, tok, hook)
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    submit(client, tok, outcome(), monkeypatch)

    now = datetime.datetime.now(datetime.timezone.utc)
    assert deliver(client, now) == (0, 1)
    sub = client.get("/fhir/Subscription/" + sub_id, headers=auth).json()
    assert sub["status"] == "error" and "answered 500" in sub["error"]
    # backing off: nothing is due, and event 2 does not overtake event 1
    assert deliver(client, now) == (0, 0)

    # it comes back: both arrive, in order, and the subscription recovers
    hook.answer = 200
    hook.received.clear()
    now += datetime.timedelta(seconds=notify.BACKOFF[0] + 1)
    assert deliver(client, now) == (2, 0)
    assert [s["notification-event"]["event-number"] for s in hook.statuses()] == ["1", "2"]
    sub = client.get("/fhir/Subscription/" + sub_id, headers=auth).json()
    assert sub["status"] == "active" and "error" not in sub

    # it goes away for good: every attempt is spent, then the server stops
    hook.answer = 503
    submit(client, tok, outcome(), monkeypatch)
    submit(client, tok, outcome(), monkeypatch)
    hook.received.clear()
    for _ in range(len(notify.BACKOFF) + 1):
        now += datetime.timedelta(seconds=max(notify.BACKOFF) + 1)
        deliver(client, now)
    assert len(hook.received) == len(notify.BACKOFF) + 1
    sub = client.get("/fhir/Subscription/" + sub_id, headers=auth).json()
    assert sub["status"] == "off" and sub["error"].startswith("Gave up after")
    # the event queued behind it is dropped with it, and nothing new is queued
    submit(client, tok, outcome(), monkeypatch)
    now += datetime.timedelta(hours=1)
    assert deliver(client, now) == (0, 0)

    # PUT is how it is brought back, through a fresh handshake
    hook.answer = 200
    hook.received.clear()
    res = client.put("/fhir/Subscription/" + sub_id, json=subscription(hook.url),
                     headers=auth)
    assert res.status_code == 200 and res.json()["status"] == "requested"
    assert deliver(client) == (1, 0)
    assert hook.statuses()[0]["type"] == "handshake"
    # the count carried on rather than starting again
    assert hook.statuses()[0]["events-since-subscription-start"] == "4"


def test_a_handshake_nobody_answers_turns_the_subscription_off(client, monkeypatch):
    """Port 1 on loopback: refused at once, which is what a typo looks like."""
    monkeypatch.setenv("SUBSCRIPTION_ALLOW_PRIVATE", "1")
    tok = register(client, "Subscriber")
    res = subscribe(client, tok, subscription("http://127.0.0.1:1/hook"))
    now = datetime.datetime.now(datetime.timezone.utc)
    for _ in range(len(notify.HANDSHAKE_BACKOFF) + 1):
        deliver(client, now)
        now += datetime.timedelta(seconds=60)
    sub = client.get("/fhir/Subscription/" + res.json()["id"],
                     headers={"Authorization": "Bearer " + tok}).json()
    # never `error`: that is for a subscription that worked once
    assert sub["status"] == "off"
    assert "could not reach the endpoint" in sub["error"]


def test_a_subscription_is_its_owners_alone(client, hook):
    mine = register(client, "Mine")
    theirs = register(client, "Theirs")
    sub_id = subscribe(client, mine, subscription(hook.url)).json()["id"]
    other = {"Authorization": "Bearer " + theirs}
    path = "/fhir/Subscription/" + sub_id

    assert client.get(path).status_code == 401
    assert client.get("/fhir/Subscription").status_code == 401
    assert client.get(path, headers=other).status_code == 404
    assert client.get(path + "/$status", headers=other).status_code == 404
    assert client.put(path, json=subscription(hook.url), headers=other).status_code == 404
    assert client.delete(path, headers=other).status_code == 404
    assert client.get("/fhir/Subscription", headers=other).json()["total"] == 0

    own = {"Authorization": "Bearer " + mine}
    assert client.get("/fhir/Subscription", headers=own).json()["total"] == 1
    assert client.get("/fhir/Subscription?status=active", headers=own).json()["total"] == 0
    assert client.get("/fhir/Subscription", params={"url": hook.url},
                      headers=own).json()["total"] == 1
    assert client.delete(path, headers=own).status_code == 204
    assert client.get(path, headers=own).status_code == 404
    # its queued handshake went with it - counted, because a row left behind
    # with no subscription to join to is one deliver() would never mention
    assert deliver(client) == (0, 0)
    conn = sqlite3.connect(os.path.join(client.data_dir, "connectathon.db"))
    try:
        assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
    finally:
        conn.close()


def test_status_operation_reports_the_event_count(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    res = client.get("/fhir/Subscription/%s/$status" % sub_id,
                     headers={"Authorization": "Bearer " + tok})
    assert res.status_code == 200
    body = res.json()
    assert body["type"] == "searchset"
    params = body["entry"][0]["resource"]
    assert params["meta"]["profile"] == [notify.STATUS_PROFILE]
    flat = {p["name"]: _part_value(p) for p in params["parameter"]}
    assert flat["type"] == "query-status" and flat["status"] == "active"
    assert flat["events-since-subscription-start"] == "1"


def test_a_company_holds_only_so_many_subscriptions(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_SUBSCRIPTIONS", 2)
    tok = register(client, "Subscriber")
    for _ in range(2):
        assert subscribe(client, tok,
                         subscription("https://example.org/h")).status_code == 201
    res = subscribe(client, tok, subscription("https://example.org/h"))
    assert res.status_code == 422
    # another company's allowance is its own
    assert subscribe(client, register(client, "Other"),
                     subscription("https://example.org/h")).status_code == 201


@pytest.mark.parametrize("mutate, fragment", [
    (lambda b: b.update(criteria="Bundle?type=document"), "canonical of a topic"),
    (lambda b: b["channel"].update(type="websocket"), "rest-hook"),
    (lambda b: b["channel"].update(payload="application/fhir+xml"), "channel.payload"),
    (lambda b: b.update(end="2020-01-01T00:00:00Z"), "in the past"),
    (lambda b: b.update(end="tomorrow"), "instant"),
    (lambda b: b.update(resourceType="Basic"), "FHIR Subscription"),
])
def test_what_this_server_cannot_do_is_refused_rather_than_ignored(
        client, mutate, fragment):
    tok = register(client, "Subscriber")
    body = subscription("https://example.org/h")
    mutate(body)
    res = subscribe(client, tok, body)
    assert res.status_code == 400
    assert fragment in diagnostics(res)


def test_a_subscription_stops_at_its_end(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    sub_id = active_subscription(client, tok, hook, end=end.isoformat())
    with sqlite3.connect(os.path.join(client.data_dir, "connectathon.db")) as conn:
        conn.execute("UPDATE subscriptions SET end_ts = ?",
                     (notify.stamp(end - datetime.timedelta(hours=2)),))
    submit(client, tok, outcome(), monkeypatch)
    assert deliver(client) == (0, 0)
    sub = client.get("/fhir/Subscription/" + sub_id,
                     headers={"Authorization": "Bearer " + tok}).json()
    assert sub["status"] == "off" and sub["error"].startswith("Ended at")


def test_status_off_parks_a_subscription_without_a_handshake(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    res = subscribe(client, tok, subscription(hook.url, status="off"))
    assert res.json()["status"] == "off"
    submit(client, tok, outcome(), monkeypatch)
    assert deliver(client) == (0, 0) and hook.received == []


def test_public_url_wins_over_how_the_request_arrived(client, hook, monkeypatch):
    monkeypatch.setattr(app_module, "PUBLIC_URL", "https://hackathon.example.org")
    tok = register(client, "Subscriber")
    subscribe(client, tok, subscription(hook.url))
    deliver(client)
    assert hook.statuses()[0]["subscription"]["reference"].startswith(
        "https://hackathon.example.org/fhir/Subscription/")


def test_behind_the_proxy_the_links_are_https(client, hook):
    tok = register(client, "Subscriber")
    client.post("/fhir/Subscription", json=subscription(hook.url),
                headers={"Authorization": "Bearer " + tok,
                         "X-Forwarded-Proto": "https", "Host": "venue.example.org"})
    deliver(client)
    assert hook.statuses()[0]["subscription"]["reference"].startswith(
        "https://venue.example.org/fhir/Subscription/")


def test_a_queueing_failure_never_costs_the_team_its_submission(client, monkeypatch):
    tok = register(client, "Producer")
    monkeypatch.setattr(app_module, "filters_match",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    subscribe(client, tok, subscription("https://example.org/h"))
    with sqlite3.connect(os.path.join(client.data_dir, "connectathon.db")) as conn:
        conn.execute("UPDATE subscriptions SET status = 'active'")
    res = submit(client, tok, outcome(), monkeypatch)
    assert res.status_code == 200 and res.headers["X-Pass"] == "true"
    assert len(param_entries(client.get("/fhir/$submissions"))) == 1


def test_the_metadata_names_the_topic_and_the_backport(client):
    cs = client.get("/fhir/metadata").json()
    assert any("backport-subscription-server-r4" in c for c in cs["instantiates"])
    sub = next(r for r in cs["rest"][0]["resource"] if r["type"] == "Subscription")
    assert sub["extension"][0]["valueCanonical"] == TOPIC
    assert [o["name"] for o in sub["operation"]] == ["status"]


def test_openapi_covers_the_subscription_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path, verbs in {"/fhir/Subscription": ("post", "get"),
                        "/fhir/Subscription/{subscription_id}": ("get", "put", "delete"),
                        "/fhir/Subscription/{subscription_id}/$status": ("get",)}.items():
        for verb in verbs:
            assert paths[path][verb]["security"] == [{"TeamToken": []}], (path, verb)
    example = paths["/fhir/Subscription"]["post"]["requestBody"]["content"][
        "application/fhir+json"]["example"]
    assert app_module.parse_subscription(example)["content"] == "full-resource"


def test_a_data_dir_without_the_tables_is_reported_not_crashed_on(tmp_path):
    assert notify._has_tables(str(tmp_path)) is False
    assert notify.print_status(str(tmp_path)) == 1


def test_a_practice_reset_takes_the_subscriptions_with_the_teams(client, hook, monkeypatch):
    """The token that could turn one off is wiped; an orphan would go on
    calling an endpoint for ever."""
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    removed = reset.clear(client.data_dir)
    assert removed["subscriptions"] == 1 and removed["notifications"] == 2
    assert deliver(client) == (0, 0) and hook.received == []


def test_the_page_spells_the_topic_and_extension_as_the_server_does(client):
    """The interop tab's curl spells the topic and the extension out for itself,
    the page being a static string. A copy that drifts is a snippet that 400s."""
    page = client.get("/").text
    assert 'const SUBSCRIPTION_TOPIC =\n  "%s";' % TOPIC in page
    head, tail = notify.PAYLOAD_CONTENT_EXT.split("StructureDefinition/")
    assert '"%s" +\n  "StructureDefinition/%s"' % (head, tail) in page


# ----------------------------------------------------------------------------
# subscriptions: the defences, the races and the failures
#
# What a test of the happy path lets through: TLS verification switched off,
# the connection made to the name again instead of the vetted address, the
# public-only rule ignored at delivery, a result recorded on a subscription
# that changed while it was in flight.
# ----------------------------------------------------------------------------


UTC = datetime.timezone.utc
FAILED_500 = notify.Outcome("endpoint", "the endpoint answered 500")


def db(client):
    conn = sqlite3.connect(os.path.join(client.data_dir, "connectathon.db"))
    conn.row_factory = sqlite3.Row
    return conn


def auth(tok):
    return {"Authorization": "Bearer " + tok}


def get_sub(client, tok, sub_id):
    return client.get("/fhir/Subscription/%s" % sub_id, headers=auth(tok)).json()


@pytest.fixture()
def hook2():
    h = Hook()
    yield h
    h.close()


def example_org_is_loopback(monkeypatch):
    real = socket.getaddrinfo
    monkeypatch.setattr(
        notify.socket, "getaddrinfo",
        lambda host, *a, **k: real("127.0.0.1" if str(host).endswith(".example.org")
                                   else host, *a, **k))


# ------------------------------------------------------------------ A. SSRF

@pytest.mark.parametrize("endpoint", [
    "http://example.org:99999/h",          # urlsplit .port raises
    "http://example.org:0/h",              # port 0
    "http:///h",                           # no host
    "http://./h",                          # nor is this one, the dots stripped
    "http://.../h",
    "http://example.org/" + "a" * 2000,    # too long
    "http://example.org/\x00",             # control char
    "http://example.org/a\tb",
    12345,
    None,
    ["http://example.org/h"],
])
def test_parse_endpoint_branches_refused_at_the_door(client, endpoint):
    tok = register(client, "Subscriber")
    res = subscribe(client, tok, subscription(endpoint))
    assert res.status_code == 400, res.text
    assert "channel.endpoint" in diagnostics(res)


@pytest.mark.parametrize("address, private_ok, refused", [
    ("::ffff:169.254.169.254", True, True),    # only the ipv4_mapped unwrap catches this
    ("::ffff:169.254.169.254", False, True),
    ("fe80::1", True, True),
    ("169.254.0.1", True, True),
    ("224.0.0.1", True, True),                 # is_global is True for multicast
    ("224.0.0.1", False, True),
    ("ff02::1", False, True),
    ("0.0.0.0", True, True),
    ("::", True, True),
    ("100.64.0.1", False, True),               # CGNAT: not private, not global
    ("127.0.0.1", False, True),
    ("fc00::1", False, True),
    # an IPv4 address in an IPv6 wrapper is vetted as what it wraps
    ("64:ff9b::7f00:1", False, True),          # NAT64 loopback: is_global says True
    ("64:ff9b::a00:1", False, True),
    ("64:ff9b::a9fe:a9fe", True, True),
    ("64:ff9b::5db8:d822", False, False),      # NAT64 of a public address is fine
    ("2002:a9fe:a9fe::1", True, True),         # 6to4 of the metadata address
    ("::169.254.169.254", True, True),
    # metadata services that are not link-local, venue network or not
    ("168.63.129.16", False, True),            # Azure - a public address
    ("168.63.129.16", True, True),
    ("100.100.100.200", True, True),
    ("fd00:ec2::254", True, True),
    ("192.0.0.192", True, True),
    ("10.0.0.1", True, False),
    ("127.0.0.1", True, False),
    ("93.184.216.34", False, False),
    ("2606:4700::1111", False, False),
])
def test_vet_address_matrix(address, private_ok, refused):
    if refused:
        with pytest.raises(notify.EndpointError):
            notify.vet_address(address, private_ok)
    else:
        notify.vet_address(address, private_ok)


@pytest.mark.parametrize("address", [
    "64:ff9b::a9fe:a9fe",      # NAT64 spelling of 169.254.169.254
    "64:ff9b::7f00:1",         # NAT64 spelling of 127.0.0.1
])
def test_nat64_spelling_of_a_forbidden_v4_address(address):
    with pytest.raises(notify.EndpointError):
        notify.vet_address(address, False)


@pytest.mark.parametrize("url, private_ok, refused", [
    ("http://foo.localhost/h", False, True),
    ("http://LOCALHOST/h", False, True),
    ("http://localhost/h", True, False),
    ("http://example.org/h", False, False),     # a name: nothing to say offline
    ("http://[fe80::1%25eth0]/h", True, True),
])
def test_check_endpoint_names(url, private_ok, refused):
    if refused:
        with pytest.raises(notify.EndpointError):
            notify.check_endpoint(url, private_ok)
    else:
        notify.check_endpoint(url, private_ok)


def test_localhost_with_a_trailing_dot_is_still_localhost():
    with pytest.raises(notify.EndpointError):
        notify.check_endpoint("http://localhost./h", False)


def test_resolve_endpoint_failure_branches(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror(-2, "Name or service not known")
    monkeypatch.setattr(notify.socket, "getaddrinfo", boom)
    with pytest.raises(notify.EndpointError, match="does not resolve"):
        notify.resolve_endpoint("http://nope.example.org/h", False)
    monkeypatch.setattr(notify.socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(notify.EndpointError, match="does not resolve"):
        notify.resolve_endpoint("http://nope.example.org/h", False)


def test_resolve_endpoint_vets_every_address_even_on_a_private_network(monkeypatch):
    def resolves_to(*addresses):
        monkeypatch.setattr(notify.socket, "getaddrinfo", lambda *a, **k: [
            (None, None, None, "", (addr, 80)) for addr in addresses])
    resolves_to("10.0.0.7", "169.254.169.254")
    with pytest.raises(notify.EndpointError):
        notify.resolve_endpoint("http://x.example.org/h", True)
    resolves_to("fe80::1%eth0")      # scoped: the %zone must not break the vetting
    with pytest.raises(notify.EndpointError):
        notify.resolve_endpoint("http://x.example.org/h", True)
    resolves_to("10.0.0.7", "10.0.0.8")
    assert notify.resolve_endpoint("http://x.example.org/h", True)[1] == "10.0.0.7"


@pytest.mark.parametrize("host", ["2130706433", "127.1", "0x7f.0.0.1", "017700000001"])
def test_numeric_aliases_of_loopback_are_caught(host):
    """ipaddress does not parse these and the resolver (inet_aton) does: they
    are all 127.0.0.1. Refused at the door, and again at delivery."""
    with pytest.raises(notify.EndpointError, match="127.0.0.1"):
        notify.check_endpoint("http://%s/h" % host, False)
    with pytest.raises(notify.EndpointError, match="127.0.0.1"):
        notify.resolve_endpoint("http://%s/h" % host, False)


def test_delivery_itself_refuses_a_name_that_turns_out_private(client, monkeypatch):
    """End to end, with SUBSCRIPTION_ALLOW_PRIVATE unset: the name passes the
    door, resolves to loopback at delivery, and the listener hears nothing."""
    listener = Hook()
    try:
        example_org_is_loopback(monkeypatch)
        tok = register(client, "Subscriber")
        port = listener.server.server_address[1]
        res = subscribe(client, tok, subscription("http://sneaky.example.org:%d/hook" % port))
        assert res.status_code == 201, res.text
        assert deliver(client) == (0, 1)
        assert listener.received == []
        assert "not a public address" in get_sub(client, tok, res.json()["id"])["error"]
    finally:
        listener.close()


# ------------------------------------------------------------------ A2. TLS

@pytest.fixture(scope="session")
def pki(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("needs the openssl command to make a throwaway CA")
    d = tmp_path_factory.mktemp("pki")
    def run(*args):
        subprocess.run(["openssl", *args], cwd=d, check=True, capture_output=True)
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key",
        "-out", "ca.pem", "-subj", "/CN=test ca", "-days", "2",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key",
        "-out", "leaf.csr", "-subj", "/CN=hook.example.org")
    (d / "ext.cnf").write_text("subjectAltName=DNS:hook.example.org\n")
    run("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
        "-CAcreateserial", "-out", "leaf.pem", "-days", "2", "-extfile", "ext.cnf")
    return d


class TLSHook:
    def __init__(self, pki):
        outer = self
        self.received, self.sni = [], []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.received.append((self.path, dict(self.headers)))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(pki / "leaf.pem", pki / "leaf.key")
        ctx.sni_callback = lambda sock, name, c: outer.sni.append(name)
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.01,),
                         daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def tls_hook(pki):
    h = TLSHook(pki)
    yield h
    h.close()


def test_https_pins_the_address_and_still_proves_the_name(
        tls_hook, pki, monkeypatch):
    example_org_is_loopback(monkeypatch)
    monkeypatch.setattr(notify, "_TLS", ssl.create_default_context(cafile=pki / "ca.pem"))
    assert notify.post("https://hook.example.org:%d/h?x=1" % tls_hook.port, b"{}",
                       [], private_ok=True) == 200
    assert tls_hook.sni == ["hook.example.org"]        # the name, not 127.0.0.1
    path, headers = tls_hook.received[0]
    assert path == "/h?x=1"
    assert headers["Host"] == "hook.example.org:%d" % tls_hook.port


def test_https_certificate_for_another_name_is_refused(tls_hook, pki, monkeypatch):
    example_org_is_loopback(monkeypatch)
    monkeypatch.setattr(notify, "_TLS", ssl.create_default_context(cafile=pki / "ca.pem"))
    with pytest.raises(notify.DeliveryError, match="TLS failure"):
        notify.post("https://other.example.org:%d/h" % tls_hook.port, b"{}", [],
                    private_ok=True)
    assert tls_hook.received == []


def test_https_untrusted_certificate_is_refused(tls_hook, monkeypatch):
    example_org_is_loopback(monkeypatch)
    with pytest.raises(notify.DeliveryError, match="TLS failure"):
        notify.post("https://hook.example.org:%d/h" % tls_hook.port, b"{}", [],
                    private_ok=True)
    assert tls_hook.received == []


def test_the_default_tls_context_verifies():
    assert notify._TLS.verify_mode == ssl.CERT_REQUIRED
    assert notify._TLS.check_hostname is True


def test_an_endpoint_that_never_answers_times_out():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        with pytest.raises(notify.DeliveryError, match="did not answer within 0.3s"):
            notify.post("http://127.0.0.1:%d/h" % srv.getsockname()[1], b"{}", [],
                        private_ok=True, timeout=0.3)
    finally:
        srv.close()


# ------------------------------------------------------------------ A3. headers

@pytest.mark.parametrize("bad", [
    ["Transfer-Encoding: chunked"],
    ["Connection: close"],
    ["content-type: text/plain"],
    ["hOsT: matchbox"],
    [5],
    [None],
    ["X-A: " + "b" * 1000],
    ["X-A: b\rX-B: c"],
    ["X-A: b\nX-B: c"],
    ["X-A: b\x00"],
    ["X-A: b\x7f"],
    ["X A: b"],
    [": b"],
    {"Authorization": "x"},
])
def test_header_validation_branches(client, bad):
    tok = register(client, "Subscriber")
    body = subscription("https://example.org/h")
    body["channel"]["header"] = bad
    assert subscribe(client, tok, body).status_code == 400, bad


def test_a_header_ending_in_a_newline_is_refused(client):
    """A trailing newline must not slip through, which with `$` and match()
    it would - why parse_subscription uses fullmatch()."""
    tok = register(client, "Subscriber")
    body = subscription("https://example.org/h")
    body["channel"]["header"] = ["X-A: b\n"]
    assert subscribe(client, tok, body).status_code == 400


def test_a_header_http_client_cannot_encode_is_refused_at_the_door(client):
    tok = register(client, "Subscriber")
    body = subscription("https://example.org/h")
    body["channel"]["header"] = ["X-A: ✓"]
    assert subscribe(client, tok, body).status_code == 400


def test_secrets_in_the_path_and_headers_are_never_logged(client, hook, capsys):
    tok = register(client, "Subscriber")
    body = subscription(hook.url + "/path-secret-123?key=query-secret-456")
    body["channel"]["header"] = ["Authorization: Bearer header-secret-789"]
    assert subscribe(client, tok, body).status_code == 201
    assert deliver(client) == (1, 0)
    assert notify.print_status(client.data_dir) == 0
    out = "".join(capsys.readouterr())
    assert "127.0.0.1" in out and "Subscription/1" in out
    for secret in ("path-secret-123", "query-secret-456", "header-secret-789"):
        assert secret not in out


# ------------------------------------------------------------------ B. _record

def in_flight(client):
    """(conn, row) for the oldest pending notification, as deliver_due read it
    before calling out."""
    conn = notify.connect(client.data_dir)
    far = datetime.datetime.now(UTC) + datetime.timedelta(days=1)
    return conn, notify.due_queues(conn, far)[0][0]


def test_late_handshake_success_for_the_old_endpoint_does_not_activate_the_new(
        client, hook, hook2):
    """What _record's guard (the UPDATE ... AND state = 'pending' AND attempts
    = ?) is for. The old handshake is in flight, the
    owner PUTs a new endpoint (status requested again, new handshake queued),
    then the old handshake's 2xx is recorded."""
    tok = register(client, "Subscriber")
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]
    conn, row = in_flight(client)
    assert client.put("/fhir/Subscription/" + sub_id, json=subscription(hook2.url),
                      headers=auth(tok)).status_code == 200
    notify._record(conn, row, notify.DELIVERED, datetime.datetime.now(UTC))
    conn.close()
    assert hook2.received == []
    assert get_sub(client, tok, sub_id)["status"] == "requested"


def test_late_final_failure_for_the_old_endpoint_does_not_kill_the_new_handshake(
        client, hook, hook2, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    with db(client) as c:
        c.execute("UPDATE notifications SET attempts = ? WHERE state = 'pending'",
                  (len(notify.BACKOFF),))
    conn, row = in_flight(client)
    assert client.put("/fhir/Subscription/" + sub_id, json=subscription(hook2.url),
                      headers=auth(tok)).status_code == 200
    notify._record(conn, row, FAILED_500, datetime.datetime.now(UTC))
    conn.close()
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "requested", sub
    assert deliver(client) == (1, 0)
    assert get_sub(client, tok, sub_id)["status"] == "active"


def test_late_retryable_failure_for_the_old_endpoint_does_not_mark_the_new(
        client, hook, hook2, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    conn, row = in_flight(client)
    client.put("/fhir/Subscription/" + sub_id, json=subscription(hook2.url),
               headers=auth(tok))
    notify._record(conn, row, FAILED_500, datetime.datetime.now(UTC))
    conn.close()
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "requested" and "error" not in sub, sub


def test_late_event_success_does_not_revive_a_parked_subscription(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    now = datetime.datetime.now(UTC)
    assert deliver(client, now) == (0, 1)                 # -> error
    conn, row = in_flight(client)
    client.put("/fhir/Subscription/" + sub_id,
               json=subscription(hook.url, status="off"), headers=auth(tok))
    assert notify._record(conn, row, notify.DELIVERED, now) == notify.DROPPED
    conn.close()
    assert get_sub(client, tok, sub_id)["status"] == "off"


def test_a_result_for_a_deleted_subscription_is_harmless(client, hook):
    tok = register(client, "Subscriber")
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]
    conn, row = in_flight(client)
    assert client.delete("/fhir/Subscription/" + sub_id, headers=auth(tok)).status_code == 204
    for outcome_ in (notify.DELIVERED, FAILED_500, notify.Outcome("server", "x")):
        assert notify._record(conn, row, outcome_,
                              datetime.datetime.now(UTC)) == notify.DROPPED
    conn.close()
    with db(client) as c:
        assert c.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0


def test_a_failing_handshake_stays_requested_and_is_tried_exactly_three_times(
        client, hook):
    tok = register(client, "Subscriber")
    hook.answer = 500
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]
    now = datetime.datetime.now(UTC)
    for i, wait in enumerate(notify.HANDSHAKE_BACKOFF):
        assert deliver(client, now) == (0, 1)
        sub = get_sub(client, tok, sub_id)
        assert sub["status"] == "requested" and "answered 500" in sub["error"], (i, sub)
        assert deliver(client, now + datetime.timedelta(seconds=wait - 1)) == (0, 0)
        now += datetime.timedelta(seconds=wait)
    assert deliver(client, now) == (0, 1)
    assert get_sub(client, tok, sub_id)["status"] == "off"
    assert len(hook.received) == len(notify.HANDSHAKE_BACKOFF) + 1
    assert deliver(client, now + datetime.timedelta(days=1)) == (0, 0)


def test_each_backoff_step_is_honoured(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    now = datetime.datetime.now(UTC)
    for wait in notify.BACKOFF:
        assert deliver(client, now) == (0, 1)
        assert deliver(client, now + datetime.timedelta(seconds=wait - 1)) == (0, 0), wait
        now += datetime.timedelta(seconds=wait)
    assert deliver(client, now) == (0, 1)
    with db(client) as c:
        rows = c.execute("SELECT state, attempts FROM notifications "
                         "WHERE kind = 'event-notification'").fetchall()
    assert [tuple(r) for r in rows] == [("failed", len(notify.BACKOFF) + 1)]


def test_a_subscription_in_error_still_has_events_queued(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    now = datetime.datetime.now(UTC)
    assert deliver(client, now) == (0, 1)
    assert get_sub(client, tok, sub_id)["status"] == "error"
    submit(client, tok, outcome(), monkeypatch)          # while in `error`
    hook.answer = 200
    hook.received.clear()
    assert deliver(client, now + datetime.timedelta(seconds=notify.BACKOFF[0])) == (2, 0)
    assert [s["notification-event"]["event-number"] for s in hook.statuses()] == ["1", "2"]


def test_one_subscribers_dead_endpoint_does_not_hold_up_anothers(
        client, hook, hook2, monkeypatch):
    a, b = register(client, "Dead"), register(client, "Alive")
    dead = active_subscription(client, a, hook)
    alive = active_subscription(client, b, hook2)
    hook.answer = 500
    submit(client, a, outcome(), monkeypatch)
    submit(client, a, outcome(), monkeypatch)
    assert deliver(client) == (2, 1)
    assert [s["notification-event"]["event-number"] for s in hook2.statuses()] == ["1", "2"]
    assert get_sub(client, a, dead)["status"] == "error"
    assert get_sub(client, b, alive)["status"] == "active"


def test_giving_up_fails_what_was_queued_behind_and_says_so(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    submit(client, tok, outcome(), monkeypatch)
    now = datetime.datetime.now(UTC)
    for _ in range(len(notify.BACKOFF) + 1):
        deliver(client, now)
        now += datetime.timedelta(seconds=max(notify.BACKOFF))
    with db(client) as c:
        rows = [tuple(r) for r in c.execute(
            "SELECT event_number, state, attempts, error FROM notifications "
            "WHERE kind = 'event-notification' ORDER BY id")]
    assert rows == [(1, "failed", 6, "the endpoint answered 500"),
                    (2, "failed", 0, "subscription turned off")]
    assert len(hook.received) == len(notify.BACKOFF) + 1    # event 2 never sent


# ------------------------------------------------------------------ C. PUT / DELETE / end

def test_put_drops_the_old_queue_and_only_the_new_endpoint_is_called(
        client, hook, hook2, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    submit(client, tok, outcome(), monkeypatch)
    body = subscription(hook2.url, content="empty", filters=["organization=nobody"])
    res = client.put("/fhir/Subscription/" + sub_id, json=body, headers=auth(tok))
    assert res.status_code == 200
    assert res.json()["channel"]["endpoint"] == hook2.url
    assert res.json()["channel"]["_payload"]["extension"][0]["valueCode"] == "empty"
    assert deliver(client) == (1, 0)
    assert hook.received == []
    assert [s["type"] for s in hook2.statuses()] == ["handshake"]
    submit(client, tok, outcome(), monkeypatch)             # new filter excludes it
    assert deliver(client) == (0, 0)


def test_put_status_off_parks_and_a_second_put_revives(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    res = client.put("/fhir/Subscription/" + sub_id,
                     json=subscription(hook.url, status="off"), headers=auth(tok))
    assert res.json()["status"] == "off"
    submit(client, tok, outcome(), monkeypatch)
    assert deliver(client) == (0, 0) and hook.received == []
    res = client.put("/fhir/Subscription/" + sub_id, json=subscription(hook.url),
                     headers=auth(tok))
    assert res.json()["status"] == "requested"
    assert deliver(client) == (1, 0)
    assert get_sub(client, tok, sub_id)["status"] == "active"


def test_a_bad_put_changes_nothing(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    res = client.put("/fhir/Subscription/" + sub_id,
                     json=subscription("http://169.254.169.254/x"), headers=auth(tok))
    assert res.status_code == 400
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "active" and sub["channel"]["endpoint"] == hook.url
    assert deliver(client) == (1, 0)                         # the queue survived
    assert client.put("/fhir/Subscription/9999", json=subscription(hook.url),
                      headers=auth(tok)).status_code == 404


def test_delete_takes_every_queued_and_delivered_row_with_it(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    deliver(client)
    submit(client, tok, outcome(), monkeypatch)             # one still pending
    with db(client) as c:
        assert c.execute("SELECT count(*) FROM notifications").fetchone()[0] == 3
    assert client.delete("/fhir/Subscription/" + sub_id, headers=auth(tok)).status_code == 204
    with db(client) as c:
        assert c.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
    # and the submissions the rows pointed at are untouched
    assert len(param_entries(client.get("/fhir/$submissions"))) == 2


def test_end_round_trips_in_utc_and_a_naive_one_is_refused(client, hook):
    tok = register(client, "Subscriber")
    res = subscribe(client, tok, subscription(hook.url, end="2099-06-01T12:00:00+02:00"))
    assert res.status_code == 201
    assert res.json()["end"] == "2099-06-01T10:00:00.000000+00:00"
    assert datetime.datetime.fromisoformat(
        get_sub(client, tok, res.json()["id"])["end"]) == datetime.datetime(
            2099, 6, 1, 10, tzinfo=UTC)
    # The last one parses, and is past the end of time once it is UTC.
    for bad in ("2099-06-01T12:00:00", "2099-06-01", 5, ["x"],
                "9999-12-31T23:59:59-10:00"):
        assert subscribe(client, tok, subscription(hook.url, end=bad)).status_code == 400, bad
    assert subscribe(client, tok, subscription(hook.url, end="2099-06-01T12:00:00Z")
                     ).status_code == 201


def test_an_id_too_big_for_the_database_is_no_subscription(client):
    tok = register(client, "Subscriber")
    for call in (client.get, client.delete):
        assert call("/fhir/Subscription/99999999999999999999",
                    headers=auth(tok)).status_code == 404


def test_an_ended_subscription_counts_no_event_and_one_in_error_ends_too(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    past = notify.stamp(datetime.datetime.now(UTC) - datetime.timedelta(seconds=1))
    with db(client) as c:
        c.execute("UPDATE subscriptions SET end_ts = ?, status = 'error'", (past,))
    submit(client, tok, outcome(), monkeypatch)
    with db(client) as c:
        row = c.execute("SELECT status, events FROM subscriptions").fetchone()
        assert tuple(row) == ("off", 0)
        assert c.execute("SELECT count(*) FROM notifications WHERE kind != 'handshake'"
                         ).fetchone()[0] == 0
    # a later end does not stop it
    sub2 = active_subscription(client, tok, hook, end="2099-01-01T00:00:00Z")
    submit(client, tok, outcome(), monkeypatch)
    assert deliver(client) == (1, 0)
    assert get_sub(client, tok, sub2)["status"] == "active"


# ------------------------------------------------------------------ D. filters

@pytest.mark.parametrize("filters, slug, doc, expected", [
    ([], "acme", "eps", True),
    (["organization=acme"], "acme", "eps", True),
    (["organization=acme"], "acme-two", "eps", False),
    (["organization=Organization/acme"], "acme", "eps", True),
    (["Bundle?organization=http://testserver/fhir/Organization/acme"], "acme", "eps", True),
    (["organization=acme,other"], "other", "eps", True),
    (["organization=acme&document-type=eps"], "acme", "laboratory", False),
    (["organization=acme&document-type=eps"], "acme", "eps", True),
    (["organization=acme", "document-type=laboratory"], "acme", "eps", False),
    (["organization=acme", "organization=other"], "acme", "eps", False),   # ANDed
    (["document-type=eps%2Claboratory"], "acme", "laboratory", False),  # escaped comma is one (unknown) value
    ([" Bundle?organization = acme , other "], "acme", "eps", True),
    (["document-type=discharge"], "acme", "eps", False),                   # retired
    (["organization=acme", "document-type=discharge"], "acme", "eps", False),
])
def test_filters_match_combinations(filters, slug, doc, expected):
    assert app_module.filters_match(filters, slug, doc) is expected


def test_a_parameter_repeated_inside_one_filter_is_anded_like_a_fhir_search():
    """Repeating a parameter is ANDed whether it is across two extensions (see
    above) or inside one string, as in a FHIR search - so this asks for a
    document that is both companies', and matches none."""
    assert app_module.filters_match(["organization=acme&organization=other"],
                                    "acme", "eps") is False


def test_category_filter_alone_and_the_wrong_category_really_was_recorded(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook, filters=["document-type=laboratory"])
    imaging = app_module.DOCUMENTS_BY_ID["imaging"]["profile"]
    lab = app_module.DOCUMENTS_BY_ID["laboratory"]["profile"]
    set_outcome(monkeypatch, outcome())
    for profile, expected in ((imaging, (0, 0)), (lab, (1, 0))):
        res = client.post("/fhir/Bundle/$validate?profile=" + profile, content=BUNDLE,
                          headers={"X-Team-Token": tok})
        assert res.status_code == 200 and res.headers["X-Pass"] == "true", res.text
        assert deliver(client) == expected, profile


# ------------------------------------------------------------------ E. numbering

def test_each_subscription_numbers_its_own_events(client, hook, hook2, monkeypatch):
    a, b = register(client, "Early"), register(client, "Late")
    early = active_subscription(client, a, hook)
    submit(client, a, outcome(), monkeypatch)
    submit(client, a, outcome(), monkeypatch)
    assert deliver(client) == (2, 0)
    late = active_subscription(client, b, hook2, filters=["organization=late"])
    submit(client, b, outcome(), monkeypatch)
    assert deliver(client) == (2, 0)
    assert [(s["notification-event"]["event-number"]) for s in hook.statuses()] == ["1", "2", "3"]
    assert [(s["notification-event"]["event-number"]) for s in hook2.statuses()] == ["1"]
    assert hook2.statuses()[0]["events-since-subscription-start"] == "1"
    for tok, sub_id, n in ((a, early, "3"), (b, late, "1")):
        res = client.get("/fhir/Subscription/%s/$status" % sub_id, headers=auth(tok)).json()
        flat = {p["name"]: _part_value(p) for p in res["entry"][0]["resource"]["parameter"]}
        assert flat["events-since-subscription-start"] == n


# ------------------------------------------------------------------ F. misc

def test_a_document_this_server_cannot_read_is_not_the_subscribers_fault(
        client, hook, monkeypatch, capsys):
    """Written off, said loudly in the log with the path - and the healthy
    endpoint behind it keeps its subscription and gets the next event."""
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook, content="full-resource")
    submit(client, tok, outcome(), monkeypatch)
    team_dir = os.path.join(client.data_dir, "submissions", "subscriber")
    for name in os.listdir(team_dir):
        os.remove(os.path.join(team_dir, name))
    submit(client, tok, outcome(), monkeypatch)
    hook.received.clear()
    assert deliver(client) == (1, 1)
    assert [s["notification-event"]["event-number"] for s in hook.statuses()] == ["2"]
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "active" and "error" not in sub
    with db(client) as c:
        rows = c.execute("SELECT state, error FROM notifications "
                         "WHERE kind = 'event-notification' ORDER BY id").fetchall()
    assert [r["state"] for r in rows] == ["failed", "delivered"]
    assert "could not read" in rows[0]["error"]
    out = capsys.readouterr().out
    assert "CANNOT READ " + team_dir in out and "FileNotFoundError" in out


def test_a_bug_in_one_delivery_is_nobodys_subscription_turned_off(
        client, hook, hook2, monkeypatch, capsys):
    a, b = register(client, "Broken"), register(client, "Fine")
    broken = active_subscription(client, a, hook)
    active_subscription(client, b, hook2, filters=["organization=fine"])
    real = notify.post

    def post(url, *args, **kw):
        if url == hook.url:
            raise RuntimeError("boom")
        return real(url, *args, **kw)
    monkeypatch.setattr(notify, "post", post)
    submit(client, a, outcome(), monkeypatch)
    submit(client, b, outcome(), monkeypatch)
    # Broken is sent both (no filter), Fine only its own. A bug may be the box
    # out of threads for a moment, so it is tried again - on a clock of its
    # own, in order, and never at the subscription's expense.
    now = datetime.datetime.now(UTC)
    assert deliver(client, now) == (1, 1)
    assert len(hook2.received) == 1           # its one event
    for wait in notify.TRANSIENT_BACKOFF + (0,) + notify.TRANSIENT_BACKOFF:
        sub = get_sub(client, a, broken)
        # Never anything but active; and nothing said until an event has
        # actually been written off, which the first is by the fifth pass.
        assert sub["status"] == "active"
        assert sub.get("error", "Event 1 was")[:11] == "Event 1 was"
        now += datetime.timedelta(seconds=wait + 1)
        assert deliver(client, now) == (0, 1)
    assert deliver(client, now + datetime.timedelta(hours=1)) == (0, 0)
    sub = get_sub(client, a, broken)
    assert sub["status"] == "active"
    assert sub["error"].startswith("Event 2 was not sent, and will not be:")
    assert "not your endpoint's doing" in sub["error"]
    with db(client) as c:
        assert [tuple(r) for r in c.execute(
            "SELECT attempts, faults FROM notifications WHERE state = 'failed'"
        )] == [(0, 4), (0, 4)]
        errors = [r[0] for r in c.execute(
            "SELECT error FROM notifications WHERE state = 'failed'")]
    assert len(errors) == 2 and all("not your endpoint's doing" in e for e in errors)
    out = capsys.readouterr().out
    assert "Traceback" in out and "RuntimeError: boom" in out


def test_payload_content_defaults_and_bad_codes(client):
    tok = register(client, "Subscriber")
    body = subscription("https://example.org/h")
    del body["channel"]["payload"]
    res = subscribe(client, tok, body)
    assert res.status_code == 201
    assert res.json()["channel"]["_payload"]["extension"][0]["valueCode"] == "empty"
    res = subscribe(client, tok, subscription("https://example.org/h", content="everything"))
    assert res.status_code == 400 and "Payload content" in diagnostics(res)


def test_a_queueing_failure_costs_one_subscription_and_is_rolled_back(
        client, hook, hook2, monkeypatch, capsys):
    """Three subscriptions match and the second blows up *after* it was counted.
    Its notification is rolled back, the other two are queued, and the
    submission never hears of any of it. The count stands, on its own: an event
    number nobody was sent is how the second subscriber finds out."""
    tok = register(client, "Producer")
    hook3 = Hook()
    try:
        for h in (hook, hook2, hook3):
            active_subscription(client, tok, h)
        real = sqlite3.Connection.execute

        class Flaky(sqlite3.Connection):
            def execute(self, sql, *a):
                if sql.startswith("INSERT INTO notifications") and a and a[0][-1] == 2 \
                        and "event-notification" in sql:
                    raise RuntimeError("boom")
                return real(self, sql, *a)
        real_connect = app_module.connect

        def connect():
            conn = sqlite3.connect(os.path.join(client.data_dir, "connectathon.db"),
                                   timeout=30, factory=Flaky)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            return conn
        monkeypatch.setattr(app_module, "connect", connect)
        res = submit(client, tok, outcome(), monkeypatch)
        monkeypatch.setattr(app_module, "connect", real_connect)
        assert res.status_code == 200 and res.headers["X-Pass"] == "true"
        with db(client) as c:
            assert [r[0] for r in c.execute(
                "SELECT events FROM subscriptions ORDER BY id")] == [1, 1, 1]
            assert [r[0] for r in c.execute(
                "SELECT subscription_id FROM notifications "
                "WHERE kind != 'handshake' ORDER BY id")] == [1, 3]
        out = capsys.readouterr().out
        assert "for Subscription/2" in out and "RuntimeError: boom" in out
    finally:
        hook3.close()


def test_main_once_exit_codes(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    subscribe(client, tok, subscription(hook.url))
    monkeypatch.setattr("sys.argv", ["notify.py", "--once", "--data-dir", client.data_dir])
    assert notify.main() == 0
    hook.answer = 500
    subscribe(client, tok, subscription(hook.url))
    assert notify.main() == 1
    monkeypatch.setattr("sys.argv", ["notify.py"])
    assert notify.main() == 2


def test_the_loop_survives_a_failing_look_and_slows_down(tmp_path, monkeypatch, capsys):
    looks, sleeps = [], []

    def look(self, now=None):
        looks.append(1)
        if len(looks) <= 3:
            raise sqlite3.OperationalError("database is locked")
        if len(looks) == 5:
            raise KeyboardInterrupt        # the way out of `while True`
        return []
    monkeypatch.setattr(notify, "_has_tables", lambda d: True)
    monkeypatch.setattr(notify.Dispatcher, "look", look)
    monkeypatch.setattr(notify.time, "sleep", sleeps.append)
    with pytest.raises(KeyboardInterrupt):
        notify.run_loop(str(tmp_path))
    # backing off while it fails, and straight back to POLL once it does not
    assert sleeps == [2 * notify.POLL, 4 * notify.POLL, 8 * notify.POLL, notify.POLL]
    out = capsys.readouterr().out
    assert "LOOK FAILED (3 in a row)" in out and "database is locked" in out


def test_put_clears_the_old_endpoints_error(client, hook, hook2, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    assert deliver(client) == (0, 1)
    assert "error" in get_sub(client, tok, sub_id)
    res = client.put("/fhir/Subscription/" + sub_id, json=subscription(hook2.url),
                     headers=auth(tok))
    assert "error" not in res.json()


# ------------------------------------ in flight, out of order, out of luck


def wait_for(condition, seconds=3):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "waited %ss" % seconds
        time.sleep(0.01)


def test_repointing_a_subscription_mid_handshake_does_not_skip_the_new_handshake(
        client, hook, hook2):
    """Through the API alone. The subscriber's own endpoint is slow to answer
    the handshake, and while it is, the subscription is PUT at somebody else's
    server. The late 200 from the first endpoint says nothing about the second:
    it stays `requested` until *it* has answered."""
    tok = register(client, "Subscriber")
    hook.delay = 0.4
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]
    out = {}
    passing = threading.Thread(target=lambda: out.update(r=deliver(client)))
    passing.start()
    wait_for(lambda: hook.received)
    put = client.put("/fhir/Subscription/" + sub_id, json=subscription(hook2.url),
                     headers=auth(tok))
    assert put.status_code == 200, put.text
    passing.join()
    assert out["r"] == (0, 0)
    assert get_sub(client, tok, sub_id)["status"] == "requested"
    assert hook2.received == []
    assert deliver(client) == (1, 0)
    assert [s["type"] for s in hook2.statuses()] == ["handshake"]
    assert get_sub(client, tok, sub_id)["status"] == "active"


def test_a_put_mid_queue_stops_the_rest_going_to_the_old_endpoint(
        client, hook, hook2, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    for _ in range(3):
        submit(client, tok, outcome(), monkeypatch)
    real = notify._attempt

    def attempt_then_put(*args):
        result = real(*args)
        if len(hook.received) == 1 and not hook2.location.endswith("#put"):
            hook2.location += "#put"          # once
            client.put("/fhir/Subscription/" + sub_id, json=subscription(hook2.url),
                       headers=auth(tok))
        return result
    monkeypatch.setattr(notify, "_attempt", attempt_then_put)
    deliver(client)
    assert len(hook.received) == 1 and hook2.received == []
    assert deliver(client) == (1, 0)
    assert [s["type"] for s in hook2.statuses()] == ["handshake"]


def test_a_queue_listed_before_a_put_is_not_sent_where_it_used_to_point(
        client, hook, hook2, monkeypatch):
    """The list is made when the look begins and a worker may get to it
    seconds later. Each row is read again before it is sent."""
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    real = notify.due_queues

    def listed_then_put(*args):
        queues = real(*args)
        monkeypatch.setattr(notify, "due_queues", real)
        client.put("/fhir/Subscription/" + sub_id, json=subscription(hook2.url),
                   headers=auth(tok))
        return queues
    monkeypatch.setattr(notify, "due_queues", listed_then_put)
    assert deliver(client) == (0, 0)
    assert hook.received == [] and hook2.received == []
    assert deliver(client) == (1, 0)
    assert [s["type"] for s in hook2.statuses()] == ["handshake"]


def test_an_id_is_never_handed_to_the_next_subscription(client, hook):
    a, b = register(client, "First"), register(client, "Second")
    first = subscribe(client, a, subscription(hook.url)).json()["id"]
    assert client.delete("/fhir/Subscription/" + first, headers=auth(a)).status_code == 204
    second = subscribe(client, b, subscription(hook.url)).json()["id"]
    assert second != first
    with db(client) as c:
        assert [r[0] for r in c.execute("SELECT id FROM notifications")] == [2]


def test_a_result_that_cannot_be_written_down_is_not_sent_again(
        client, hook, monkeypatch, capsys):
    """The database is locked for longer than its timeout between the POST and
    the write. The event went out once, and once is how often it goes out."""
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    real_connect, real_attempt = notify.connect, notify._attempt

    def impatient(data_dir):
        conn = real_connect(data_dir)
        conn.execute("PRAGMA busy_timeout = 50")
        return conn
    locker = sqlite3.connect(os.path.join(client.data_dir, "connectathon.db"),
                             isolation_level=None, check_same_thread=False)

    def attempt_then_lock(*args):
        result = real_attempt(*args)
        locker.execute("BEGIN IMMEDIATE")
        return result
    monkeypatch.setattr(notify, "connect", impatient)
    monkeypatch.setattr(notify, "_attempt", attempt_then_lock)
    try:
        assert deliver(client) == (0, 1)
        assert len(hook.received) == 1
        assert "COULD NOT BE WRITTEN DOWN" in capsys.readouterr().out
        assert deliver(client) == (0, 1)          # still locked: not sent again
        assert len(hook.received) == 1
        locker.execute("ROLLBACK")
        assert deliver(client) == (1, 0)          # written down, from memory
        assert len(hook.received) == 1
    finally:
        locker.close()
    assert notify._UNRECORDED == {}
    with db(client) as c:
        assert [tuple(r) for r in c.execute(
            "SELECT state, attempts FROM notifications WHERE kind != 'handshake'"
        )] == [("delivered", 1)]


def test_a_backlog_is_numbered_as_it_happened_not_as_it_stands(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    hook.answer = 500
    for _ in range(3):
        submit(client, tok, outcome(), monkeypatch)
    now = datetime.datetime.now(UTC)
    assert deliver(client, now) == (0, 1)
    hook.answer = 200
    hook.received.clear()
    assert deliver(client, now + datetime.timedelta(seconds=6)) == (3, 0)
    assert [(s["notification-event"]["event-number"],
             s["events-since-subscription-start"]) for s in hook.statuses()] == [
        ("1", "1"), ("2", "2"), ("3", "3")]


def test_an_answer_dripped_a_byte_at_a_time_is_cut_off_at_the_deadline():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def drip():
        conn, _ = srv.accept()
        try:
            for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                time.sleep(0.1)       # every recv is well inside the timeout
                conn.sendall(bytes([byte]))
        except OSError:
            pass
        finally:
            conn.close()
    threading.Thread(target=drip, daemon=True).start()
    started = time.monotonic()
    try:
        with pytest.raises(notify.DeliveryError, match="did not answer within 0.5s"):
            notify.post("http://127.0.0.1:%d/h" % srv.getsockname()[1], b"{}", [],
                        private_ok=True, timeout=0.5)
    finally:
        srv.close()
    assert time.monotonic() - started < 1.5


def test_a_slow_endpoint_holds_up_nobody_else(client, hook, hook2, monkeypatch):
    """look() does not wait. The fast subscriber's event is delivered *and
    written down* while the slow one is still thinking, and a second look
    leaves the busy subscription alone rather than sending its event twice."""
    slow, fast = register(client, "Slow"), register(client, "Fast")
    active_subscription(client, slow, hook, filters=["organization=slow"])
    active_subscription(client, fast, hook2, filters=["organization=fast"])
    hook.delay = 0.6
    submit(client, slow, outcome(), monkeypatch)
    submit(client, fast, outcome(), monkeypatch)
    dispatcher = notify.Dispatcher(client.data_dir)
    try:
        tasks = dispatcher.look()
        assert len(tasks) == 2

        def fast_is_written_down():
            with db(client) as c:
                return c.execute(
                    "SELECT count(*) FROM notifications WHERE state = 'pending'"
                ).fetchone()[0] == 1
        wait_for(fast_is_written_down, 0.5)
        assert not all(task.done() for task in tasks)
        assert dispatcher.look() == []            # the slow one is still busy
        assert sorted(task.result() for task in tasks) == [(1, 0), (1, 0)]
    finally:
        dispatcher.close()
    assert len(hook.received) == 1 and len(hook2.received) == 1


def test_end_is_honoured_by_the_notifier_with_no_submission_to_prompt_it(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    end = datetime.datetime.now(UTC) + datetime.timedelta(hours=1)
    sub_id = active_subscription(client, tok, hook, end=end.isoformat())
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    assert deliver(client) == (0, 1)              # queued before `end`, retrying
    hook.received.clear()
    assert deliver(client, end + datetime.timedelta(seconds=1)) == (0, 0)
    assert hook.received == []
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "off" and sub["error"].startswith("Ended at ")
    with db(client) as c:
        assert [r[0] for r in c.execute(
            "SELECT state FROM notifications WHERE kind != 'handshake'")] == ["failed"]


def test_an_error_never_quotes_the_path_back():
    parts = notify.parse_endpoint("https://example.org/hooks/S3CRET?key=T0KEN")
    text = notify._describe(
        ValueError("URL can't contain control characters. '/hooks/S3CRET?key=T0KEN'"),
        parts)
    assert "S3CRET" not in text and "T0KEN" not in text
    assert notify._describe(http.client.NotConnected(), parts) == "NotConnected"


@pytest.mark.parametrize("endpoint", [
    "https://example.org/hook\x7f",            # DEL: http.client quotes the path back
    "https://example.org/caf\u00e9",
    "https://m\u00fcnchen.example/hook",
])
def test_an_endpoint_http_client_could_not_send_is_refused_at_the_door(client, endpoint):
    tok = register(client, "Subscriber")
    res = subscribe(client, tok, subscription(endpoint))
    assert res.status_code == 400 and "printable ASCII" in diagnostics(res)


def test_an_endpoint_that_answers_early_and_hangs_up_is_explained_not_unreachable():
    """A proxy with a body limit says 413 and closes while the document is
    still being sent. The 413 is the useful half of that."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def refuse():
        conn, _ = srv.accept()
        conn.recv(1024)
        conn.sendall(b"HTTP/1.1 413 Payload Too Large\r\nContent-Length: 0\r\n"
                     b"Connection: close\r\n\r\n")
        time.sleep(0.2)
        conn.close()
    threading.Thread(target=refuse, daemon=True).start()
    try:
        with pytest.raises(notify.DeliveryError) as raised:
            notify.post("http://127.0.0.1:%d/h" % srv.getsockname()[1],
                        b"x" * (8 * 1024 * 1024), [], private_ok=True, timeout=5)
    finally:
        srv.close()
    assert ("answered 413" in str(raised.value)
            or "body size limit" in str(raised.value)), str(raised.value)


def _one_connection(serve):
    """A port whose first connection is `serve`'s to do with as it likes."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        try:
            serve(conn)
        finally:
            conn.close()
            srv.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def _whole_request(conn, body):
    data = b""
    while not data.endswith(body):
        data += conn.recv(65536)


def test_a_reset_before_anything_was_sent_is_not_blamed_on_the_body():
    """request() connects as well as sends, and https on a port that is not
    speaking TLS is reset during the handshake - with nothing sent yet."""
    def reset(conn):
        conn.recv(10)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    with pytest.raises(notify.DeliveryError) as raised:
        notify.post("https://127.0.0.1:%d/h" % _one_connection(reset), b"{}", [],
                    private_ok=True, timeout=5)
    assert "body size limit" not in str(raised.value)
    assert "could not reach the endpoint" in str(raised.value)


@pytest.mark.parametrize("answer", [
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab",
    b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\nab",
])
def test_a_2xx_is_delivered_whatever_becomes_of_its_body(answer):
    """Nothing in the body is used. Failing the delivery over it sends the
    endpoint again what it has just said it has."""
    def serve(conn):
        _whole_request(conn, b"{}")
        conn.sendall(answer)
        conn.shutdown(socket.SHUT_WR)
        time.sleep(0.2)
    assert notify.post("http://127.0.0.1:%d/h" % _one_connection(serve), b"{}", [],
                       private_ok=True, timeout=5) == 200


def test_what_is_not_http_is_not_quoted_back():
    """BadStatusLine's text is the peer's first line: with private addresses
    allowed, the banner of any port in the room."""
    def banner(conn):
        conn.sendall(b"SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13\r\n")
        time.sleep(0.2)
    with pytest.raises(notify.DeliveryError) as raised:
        notify.post("http://127.0.0.1:%d/h" % _one_connection(banner), b"{}", [],
                    private_ok=True, timeout=5)
    assert str(raised.value) == "the endpoint did not answer with HTTP"


def test_a_volume_without_locks_is_not_mistaken_for_another_notifier(
        tmp_path, monkeypatch):
    def no_locks(*a):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(notify.fcntl, "flock", no_locks)
    with pytest.raises(OSError) as raised:
        notify._claim(str(tmp_path))
    assert not isinstance(raised.value, notify.AlreadyDelivering)


def test_once_is_not_green_while_a_delivery_waits_on_its_retry(
        client, hook, monkeypatch, capsys):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    monkeypatch.setattr("sys.argv", ["notify.py", "--once", "--data-dir", client.data_dir])
    assert notify.main() == 1
    assert notify.main() == 1          # nothing due, so nothing tried - and not fine
    assert "0 delivered, 0 failed, 1 still queued" in capsys.readouterr().out


# --------------------------------------- whose fault it was, and who is told


@pytest.fixture(autouse=True)
def _nothing_remembered_between_tests():
    yield
    with notify._UNRECORDED_LOCK:
        notify._UNRECORDED.clear()


def pending(client):
    with db(client) as c:
        return c.execute("SELECT count(*) FROM notifications "
                         "WHERE state = 'pending'").fetchone()[0]


def test_a_document_that_cannot_be_read_just_now_is_tried_again(
        client, hook, monkeypatch, capsys):
    """Out of file handles is not "the document is gone"."""
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook, content="full-resource")
    submit(client, tok, outcome(), monkeypatch)
    real_open, calls = open, []

    def flaky_open(path, *a, **k):
        if "submissions" in str(path) and not calls:
            calls.append(path)
            raise OSError(24, "Too many open files", str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr(notify, "open", flaky_open, raising=False)
    now = datetime.datetime.now(UTC)
    assert deliver(client, now) == (0, 1)
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "active" and "error" not in sub     # not their doing
    assert deliver(client, now + datetime.timedelta(seconds=6)) == (1, 0)
    assert len(hook.received) == 1 and len(hook.received[0][2]["entry"]) == 2
    out = capsys.readouterr().out
    assert "CANNOT READ" in out and "NOT SENT, our trouble, retry in 5s" in out
    assert "EMFILE" in out


def test_a_document_that_is_gone_is_written_off_at_once(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook, content="full-resource")
    submit(client, tok, outcome(), monkeypatch)
    submit(client, tok, outcome(), monkeypatch)
    with db(client) as c:
        slug, name = c.execute("SELECT t.slug, s.filename FROM submissions s "
                               "JOIN teams t ON t.id = s.team_id ORDER BY s.id").fetchone()
    os.remove(os.path.join(client.data_dir, "submissions", slug, name))
    assert deliver(client) == (1, 1)      # and the one behind it goes out in the same pass
    assert [event_number(b) for b in hook.received] == ["2"]


def event_number(received):
    params = {p["name"]: p for p in received[2]["entry"][0]["resource"]["parameter"]}
    return params["notification-event"]["part"][0]["valueString"]


def test_a_thread_that_would_not_start_does_not_cost_a_new_subscription(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]
    real_start, starts = notify.threading.Timer.start, []

    def start(self):
        starts.append(1)
        if len(starts) == 1:
            raise RuntimeError("can't start new thread")
        return real_start(self)
    monkeypatch.setattr(notify.threading.Timer, "start", start)
    now = datetime.datetime.now(UTC)
    assert deliver(client, now) == (0, 1)
    sub = get_sub(client, tok, sub_id)
    # still waiting, and told why it is taking a while
    assert sub["status"] == "requested" and "not your endpoint's doing" in sub["error"]
    assert deliver(client, now + datetime.timedelta(seconds=6)) == (1, 0)
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "active" and "error" not in sub


def test_a_handshake_this_server_never_manages_to_send_turns_it_off_saying_so(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]

    def post(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(notify, "post", post)
    now = datetime.datetime.now(UTC)
    for wait in (0,) + notify.TRANSIENT_BACKOFF:
        now += datetime.timedelta(seconds=wait + 1)
        assert deliver(client, now) == (0, 1)
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "off" and "PUT the subscription to try again" in sub["error"]


def test_this_servers_own_resolver_being_down_costs_nobody_a_subscription(
        client, hook, monkeypatch, capsys):
    tok = register(client, "Subscriber")
    example_org_is_loopback(monkeypatch)
    res = subscribe(client, tok, subscription(
        "http://hooks.example.org:%d/hook" % hook.server.server_address[1]))
    sub_id = res.json()["id"]
    assert deliver(client) == (1, 0)
    submit(client, tok, outcome(), monkeypatch)

    def broken(host, *a, **k):
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
    working = notify.socket.getaddrinfo
    monkeypatch.setattr(notify.socket, "getaddrinfo", broken)
    now = datetime.datetime.now(UTC)
    for wait in (0,) + notify.TRANSIENT_BACKOFF:
        now += datetime.timedelta(seconds=wait + 1)
        assert deliver(client, now) == (0, 1)
        sub = get_sub(client, tok, sub_id)
        assert sub["status"] == "active"
        assert "error" not in sub or wait == notify.TRANSIENT_BACKOFF[-1]
    assert pending(client) == 0           # that event is a gap now; the subscription is not
    # ... but its owner is told, for as long as nothing gets through: the
    # resolver that is not answering may as well be the one for their name.
    assert sub["error"].startswith("Event 1 was not sent, and will not be:")
    monkeypatch.setattr(notify.socket, "getaddrinfo", working)
    submit(client, tok, outcome(), monkeypatch)
    assert deliver(client, now + datetime.timedelta(seconds=1)) == (1, 0)
    assert "error" not in get_sub(client, tok, sub_id)
    assert "could not be looked up just now" in capsys.readouterr().out


def test_a_name_that_does_not_exist_is_the_subscribers_to_fix(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    with db(client) as c:
        c.execute("UPDATE subscriptions SET endpoint = 'http://gone.example.org/h'")
    submit(client, tok, outcome(), monkeypatch)

    def no_such_name(host, *a, **k):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    monkeypatch.setattr(notify.socket, "getaddrinfo", no_such_name)
    assert deliver(client) == (0, 1)
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "error" and "does not resolve" in sub["error"]


def test_a_resolver_hiccup_does_not_use_up_the_endpoints_last_try(client, hook, monkeypatch):
    """attempts and faults are counted apart."""
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    now = datetime.datetime.now(UTC)
    with db(client) as c:
        c.execute("UPDATE notifications SET attempts = ? WHERE state = 'pending'",
                  (len(notify.BACKOFF),))
    with notify.connect(client.data_dir) as conn:
        row = conn.execute(notify.DUE + " ORDER BY n.id").fetchone()
        verdict = notify._record(conn, row, notify.Outcome("transient", "resolver"), now)
        assert verdict.startswith("NOT SENT")
        row = conn.execute(notify.DUE + " ORDER BY n.id").fetchone()
        assert (row["attempts"], row["faults"], row["status"]) == (5, 1, "active")


def test_the_deadline_cutting_a_slow_upload_is_not_called_a_hang_up():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    held = []
    threading.Thread(target=lambda: held.append(srv.accept()), daemon=True).start()
    try:
        with pytest.raises(notify.DeliveryError) as raised:
            notify.post("http://127.0.0.1:%d/hook" % srv.getsockname()[1],
                        b"x" * (20 * 1024 * 1024), [], private_ok=True, timeout=0.5)
    finally:
        srv.close()
        for conn, _ in held:
            conn.close()
    assert "did not take the notification (20971520 bytes) within 0.5s" in str(raised.value)


def test_once_is_not_green_with_a_backlog_it_ran_out_of_time_for(
        client, hook, monkeypatch, capsys):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    for _ in range(3):
        submit(client, tok, outcome(), monkeypatch)
    monkeypatch.setattr(notify, "DRAIN_BUDGET", 0)
    monkeypatch.setattr("sys.argv", ["notify.py", "--once", "--data-dir", client.data_dir])
    assert notify.main() == 1
    assert "1 delivered, 0 failed, 2 still queued" in capsys.readouterr().out


def test_a_clock_stepped_back_does_not_park_what_was_never_tried(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    earlier = datetime.datetime.now(UTC) - datetime.timedelta(minutes=30)
    assert deliver(client, earlier) == (1, 0)


def test_a_backoff_is_still_a_backoff(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    now = datetime.datetime.now(UTC)
    assert deliver(client, now) == (0, 1)
    assert deliver(client, now + datetime.timedelta(seconds=1)) == (0, 0)


def test_a_retired_category_in_a_stored_filter_silences_only_itself(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    with db(client) as c:
        c.execute("UPDATE subscriptions SET filters = ? WHERE id = ?",
                  (json.dumps(["Bundle?document-type=eps,hospital-discharge"]), sub_id))
    submit(client, tok, outcome(), monkeypatch)             # an eps document
    assert deliver(client) == (1, 0)
    assert app_module.filters_match(["document-type=hospital-discharge"], "x", "eps") is False
    # and at the door it is still refused
    res = subscribe(client, tok, subscription(
        hook.url, filters=["document-type=eps,hospital-discharge"]))
    assert res.status_code == 400


def test_a_notification_whose_subscription_is_gone_is_failed_not_invisible(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    with db(client) as c:                # by hand, as sqlite3's CLI would: no foreign keys
        c.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    assert pending(client) == 1
    assert deliver(client) == (0, 0)
    assert pending(client) == 0 and hook.received == []


def test_one_notifier_per_data_directory(client, hook, monkeypatch, capsys):
    tok = register(client, "Subscriber")
    subscribe(client, tok, subscription(hook.url))
    loop = notify.Dispatcher(client.data_dir)
    try:
        with pytest.raises(notify.AlreadyDelivering):
            deliver(client)
        monkeypatch.setattr("sys.argv", ["notify.py", "--once", "--data-dir", client.data_dir])
        assert notify.main() == 1
        assert "already delivering" in capsys.readouterr().err
        assert hook.received == []
    finally:
        loop.close()
    assert deliver(client) == (1, 0)      # and the lock goes with its holder


def test_the_loop_waits_for_the_notifier_it_replaces(client, monkeypatch, capsys):
    old = notify.Dispatcher(client.data_dir)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            old.close()
        if len(sleeps) == 4:
            raise KeyboardInterrupt
    looks = []
    monkeypatch.setattr(notify.Dispatcher, "look", lambda self, now=None: looks.append(1))
    monkeypatch.setattr(notify.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        notify.run_loop(client.data_dir)
    assert len(looks) == 2
    assert capsys.readouterr().out.count("another notifier still has") == 1


def test_the_loop_says_so_when_the_data_dir_is_not_there_and_keeps_saying_it(
        tmp_path, monkeypatch, capsys):
    clock, sleeps = [0.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += 25
        if len(sleeps) == 6:
            raise KeyboardInterrupt
    monkeypatch.setattr(notify.time, "sleep", sleep)
    monkeypatch.setattr(notify.time, "monotonic", lambda: clock[0])
    with pytest.raises(KeyboardInterrupt):
        notify.run_loop(str(tmp_path / "dtaa-typo"))
    out = capsys.readouterr().out
    assert out.count("DOES NOT EXIST") == 2         # at 0s and at 75s: once a minute
    capsys.readouterr()
    sleeps.clear()
    with pytest.raises(KeyboardInterrupt):
        notify.run_loop(str(tmp_path))              # there, but the service has not been
    assert "waiting for the service" in capsys.readouterr().out


def test_what_could_not_be_written_is_remembered_per_database(client, hook, tmp_path):
    """Notification 1 of one data directory is not notification 1 of another."""
    with notify._UNRECORDED_LOCK:
        notify._UNRECORDED[(str(tmp_path / "other" / "connectathon.db"), 1)] = notify.DELIVERED
    tok = register(client, "Subscriber")
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]
    hook.answer = 500
    assert deliver(client) == (0, 1)
    assert len(hook.received) == 1                  # asked, not assumed
    assert get_sub(client, tok, sub_id)["status"] == "requested"
    # Nor is it this database's to forget: there is no notification 7 here,
    # and that says nothing about the one over there.
    theirs = (str(tmp_path / "other" / "connectathon.db"), 7)
    with notify._UNRECORDED_LOCK:
        notify._UNRECORDED[theirs] = notify.DELIVERED
    deliver(client)
    assert theirs in notify._UNRECORDED


def test_an_outcome_nobody_handles_is_refused_and_not_sent_again(
        client, hook, monkeypatch, capsys):
    tok = register(client, "Subscriber")
    subscribe(client, tok, subscription(hook.url))
    sent = []

    def attempt(data_dir, row, private_ok):
        sent.append(1)
        return notify.Outcome("timeout", "no such kind")
    monkeypatch.setattr(notify, "_attempt", attempt)
    for _ in range(3):
        assert deliver(client) == (0, 1)
    assert len(sent) == 1                           # remembered, however it failed to write
    out = capsys.readouterr().out
    assert "COULD NOT BE WRITTEN DOWN" in out and "unknown outcome 'timeout'" in out


def test_trouble_writing_gives_that_subscription_a_rest(client, hook, hook2, monkeypatch, capsys):
    a, b = register(client, "Troubled"), register(client, "Fine")
    subscribe(client, a, subscription(hook.url))
    real = notify._record

    def record(conn, row, outcome, now):
        if row["id"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, row, outcome, now)
    monkeypatch.setattr(notify, "_record", record)
    dispatcher = notify.Dispatcher(client.data_dir)
    try:
        assert [t.result() for t in dispatcher.look()] == [(0, 1)]
        assert dispatcher.resting[1][0] == 1
        # A rest, not a retirement: bounded, and doubling with the count.
        rest = dispatcher.resting[1][1] - time.monotonic()
        assert 0 < rest <= min(notify.POLL * 2, notify.MAX_LOOP_BACKOFF)
        subscribe(client, b, subscription(hook2.url))
        # looked at again at once: the troubled one rests, the other does not wait for it
        assert [t.result() for t in dispatcher.look()] == [(1, 0)]
        dispatcher.resting[1] = (1, 0.0)            # rested
        assert [t.result() for t in dispatcher.look()] == [(0, 1)]
        assert dispatcher.resting[1][0] == 2
        longer = dispatcher.resting[1][1] - time.monotonic()
        assert rest < longer <= min(notify.POLL * 4, notify.MAX_LOOP_BACKOFF)
        dispatcher.resting[1] = (20, time.monotonic())
        assert [t.result() for t in dispatcher.look()] == [(0, 1)]
        assert dispatcher.resting[1][1] - time.monotonic() <= notify.MAX_LOOP_BACKOFF
        monkeypatch.setattr(notify, "_record", real)
        dispatcher.resting[1] = (2, 0.0)
        assert [t.result() for t in dispatcher.look()] == [(1, 0)]
        assert 1 not in dispatcher.resting
    finally:
        dispatcher.close()
    assert len(hook.received) == 1                  # sent once, through all of that


def test_a_locked_database_is_not_called_a_bug(client, hook, monkeypatch, capsys):
    tok = register(client, "Subscriber")
    subscribe(client, tok, subscription(hook.url))

    def drain(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(notify, "_drain", drain)
    assert deliver(client) == (0, 1)
    out = capsys.readouterr().out
    assert "DATABASE TROUBLE working on Subscription/1: database is locked" in out
    assert "BUG" not in out and "Traceback" not in out


def test_a_worker_that_never_started_does_not_leave_its_subscription_busy(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    subscribe(client, tok, subscription(hook.url))
    dispatcher = notify.Dispatcher(client.data_dir)
    real_submit, calls = dispatcher.pool.submit, []

    def submit_once_broken(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("can't start new thread")
        return real_submit(*a, **k)
    dispatcher.pool.submit = submit_once_broken
    try:
        with pytest.raises(RuntimeError):
            dispatcher.look()
        assert dispatcher.busy == set()
        assert [t.result() for t in dispatcher.look()] == [(1, 0)]
    finally:
        dispatcher.close()


def test_an_endpoint_let_in_with_a_trailing_dot_is_one_that_can_be_delivered_to(
        client, hook):
    tok = register(client, "Subscriber")
    res = subscribe(client, tok, subscription(hook.url.replace("127.0.0.1", "127.0.0.1.")))
    assert res.status_code == 201
    assert deliver(client) == (1, 0)


def test_error_does_not_outlive_the_last_thing_being_retried(client, hook, monkeypatch):
    """`error` means "still trying". The retry that finds the document gone is
    written off, and with nothing queued behind it nothing is being tried."""
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook, content="full-resource")
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    now = datetime.datetime.now(UTC)
    assert deliver(client, now) == (0, 1)
    assert get_sub(client, tok, sub_id)["status"] == "error"
    with db(client) as c:
        slug, name = c.execute("SELECT t.slug, s.filename FROM submissions s "
                               "JOIN teams t ON t.id = s.team_id").fetchone()
    os.remove(os.path.join(client.data_dir, "submissions", slug, name))
    assert deliver(client, now + datetime.timedelta(seconds=6)) == (0, 1)
    sub = get_sub(client, tok, sub_id)
    # Not `error` any more, and told what became of the event all the same -
    # until something gets through.
    assert sub["status"] == "active"
    assert sub["error"].startswith("Event 1 was not sent, and will not be:")
    hook.answer = 200
    submit(client, tok, outcome(), monkeypatch)
    assert deliver(client, now + datetime.timedelta(seconds=7)) == (1, 0)
    assert "error" not in get_sub(client, tok, sub_id)


def test_giving_up_does_not_rewrite_why_an_ended_subscription_is_off(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    now = datetime.datetime.now(UTC)
    with notify.connect(client.data_dir) as conn:
        row = conn.execute(notify.DUE + " ORDER BY n.id").fetchone()
        conn.execute("UPDATE notifications SET attempts = ?", (len(notify.BACKOFF),))
        conn.execute("UPDATE subscriptions SET status = 'off', error = 'Ended at X'")
        conn.commit()
        row = dict(row, attempts=len(notify.BACKOFF))
        assert notify._record(conn, row, FAILED_500, now) == "gave up, subscription off"
    assert get_sub(client, tok, sub_id)["error"] == "Ended at X"


@pytest.mark.parametrize("sql", [
    "UPDATE subscriptions SET status = 'Active'",
    "UPDATE subscriptions SET content = 'everything'",
    "UPDATE subscriptions SET events = -1",
    "UPDATE notifications SET state = 'Pending'",
    "UPDATE notifications SET kind = 'event'",
    "UPDATE notifications SET attempts = -1",
    "UPDATE notifications SET faults = -1",
    "UPDATE notifications SET next_attempt = 'tomorrow'",
    "UPDATE notifications SET next_attempt = '2026-11-18T10:02:11+00:00'",   # no microseconds
    "UPDATE notifications SET event_number = 7 WHERE kind = 'handshake'",
    "UPDATE notifications SET submission_id = NULL WHERE kind != 'handshake'",
    "UPDATE notifications SET event_number = 1 WHERE kind != 'handshake'",   # twice the same
])
def test_the_schema_refuses_what_the_code_would_silently_mishandle(
        client, hook, monkeypatch, sql):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    submit(client, tok, outcome(), monkeypatch)
    with pytest.raises(sqlite3.IntegrityError):
        with db(client) as c:
            c.execute(sql)


def test_everything_the_code_itself_writes_passes_the_schema(client, hook, monkeypatch):
    """The other half of the above: handshake, event, retry, give-up, PUT, park."""
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    now = datetime.datetime.now(UTC)
    for hours in range(len(notify.BACKOFF) + 1):
        assert deliver(client, now + datetime.timedelta(hours=hours)) == (0, 1)
    assert get_sub(client, tok, sub_id)["status"] == "off"
    hook.answer = 200
    assert client.put("/fhir/Subscription/" + sub_id, json=subscription(hook.url),
                      headers=auth(tok)).status_code == 200
    assert deliver(client) == (1, 0)
    parked = subscription(hook.url, status="off")
    assert client.put("/fhir/Subscription/" + sub_id, json=parked,
                      headers=auth(tok)).json()["status"] == "off"


@pytest.mark.parametrize("address", [
    "2a00:1450:4001::5efe:a9fe:a9fe",          # ISATAP around 169.254.169.254
    "2a00:1450:4001:0:200:5efe:7f00:1",        # and around 127.0.0.1, global-id form
])
def test_isatap_is_vetted_as_the_address_it_wraps(address):
    with pytest.raises(notify.EndpointError):
        notify.vet_address(address, False)


# --------------------------------- what mutating the code showed nothing pinned


# ---------------------------------------------------------------- _record

def ended_under_a_flight(client, hook, monkeypatch):
    """An event is in flight when the service, taking a later submission,
    notices the subscription's `end` has passed and turns it off. The service
    does not touch the queue, so the in-flight row is still pending."""
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook, end="2099-01-01T00:00:00Z")
    submit(client, tok, outcome(), monkeypatch)
    conn, row = in_flight(client)
    past = notify.stamp(datetime.datetime.now(UTC) - datetime.timedelta(seconds=1))
    with db(client) as c:
        c.execute("UPDATE subscriptions SET end_ts = ?", (past,))
    submit(client, tok, outcome(), monkeypatch)
    assert get_sub(client, tok, sub_id)["status"] == "off"
    return tok, sub_id, conn, row


def test_a_late_success_does_not_switch_an_ended_subscription_back_on(
        client, hook, monkeypatch):
    tok, sub_id, conn, row = ended_under_a_flight(client, hook, monkeypatch)
    notify._record(conn, row, notify.DELIVERED, datetime.datetime.now(UTC))
    conn.close()
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "off" and sub["error"].startswith("Ended at")


def test_a_late_failure_does_not_overwrite_why_a_subscription_is_off(
        client, hook, monkeypatch):
    tok, sub_id, conn, row = ended_under_a_flight(client, hook, monkeypatch)
    notify._record(conn, row, FAILED_500, datetime.datetime.now(UTC))
    conn.close()
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "off" and sub["error"].startswith("Ended at"), sub


def test_a_result_for_a_row_already_written_off_is_dropped(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    end = datetime.datetime.now(UTC) + datetime.timedelta(hours=1)
    active_subscription(client, tok, hook, end=end.isoformat())
    submit(client, tok, outcome(), monkeypatch)
    conn, row = in_flight(client)
    other = notify.connect(client.data_dir)
    notify.expire(other, end + datetime.timedelta(seconds=1))
    other.close()
    assert notify._record(conn, row, FAILED_500, datetime.datetime.now(UTC)) == notify.DROPPED
    conn.close()
    with db(client) as c:
        assert [tuple(r) for r in c.execute(
            "SELECT state, attempts, error FROM notifications WHERE kind != 'handshake'"
        )] == [("failed", 0, "subscription turned off")]


def test_a_stale_result_does_not_move_a_retry_that_was_already_scheduled(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    conn, stale = in_flight(client)
    now = datetime.datetime.now(UTC)
    assert deliver(client, now) == (0, 1)
    with db(client) as c:
        before = tuple(c.execute("SELECT state, attempts, next_attempt FROM notifications "
                                 "WHERE kind != 'handshake'").fetchone())
    assert notify._record(conn, stale, FAILED_500,
                          now + datetime.timedelta(hours=1)) == notify.DROPPED
    assert notify._record(conn, stale, notify.DELIVERED, now) == notify.DROPPED
    conn.close()
    with db(client) as c:
        assert tuple(c.execute("SELECT state, attempts, next_attempt FROM notifications "
                               "WHERE kind != 'handshake'").fetchone()) == before


def test_a_handshake_this_server_could_not_send_turns_the_subscription_off(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]
    monkeypatch.setattr(notify, "post",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    now = datetime.datetime.now(UTC)
    for wait in (0,) + notify.TRANSIENT_BACKOFF:
        assert get_sub(client, tok, sub_id)["status"] == "requested"
        now += datetime.timedelta(seconds=wait + 1)
        assert deliver(client, now=now) == (0, 1)
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "off"
    assert "not your endpoint's doing" in sub["error"] and "PUT" in sub["error"]


# ---------------------------------------------------------------- vetting

@pytest.mark.parametrize("address, private_ok", [
    # Teredo: server 169.254.169.254, client (xor ffff:ffff) 10.0.0.1
    ("2001:0:a9fe:a9fe::f5ff:fffe", True),
    # Teredo: public server, client (xor) 169.254.169.254
    ("2001:0:5db8:d822::5601:5601", True),
    ("64:ff9b:1::a9fe:a9fe", True),            # NAT64 local-use prefix, RFC 8215
    ("64:ff9b:1::7f00:1", False),
    ("fd00:ec2::1", True),                     # the /32, not one address in it
    ("2002:a83f:8110::1", True),               # 6to4 of Azure's 168.63.129.16
    ("64:ff9b::6464:64c8", True),              # NAT64 of Alibaba's 100.100.100.200
])
def test_wrapped_and_never_addresses_the_matrix_left_out(address, private_ok):
    with pytest.raises(notify.EndpointError):
        notify.vet_address(address, private_ok)


def test_a_name_server_that_never_answers_is_given_up_on(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(notify.socket, "getaddrinfo",
                        lambda *a, **k: release.wait(5) and [])
    started = time.monotonic()
    try:
        # Within the resolver's share of the attempt, not the whole of it: the
        # rest was the endpoint's, had the name come back.
        with pytest.raises(notify.ServerTrouble, match="looked up within 0.1s"):
            notify.post("http://slow-dns.example.org/h", b"{}", [], False, timeout=0.2)
    finally:
        release.set()
    assert time.monotonic() - started < 2


# ---------------------------------------------------------------- post()

def test_the_connect_itself_is_bounded_and_https_means_443(monkeypatch):
    seen = []

    def refuse(address, timeout=None, *a, **k):
        seen.append((address, timeout))
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(notify.socket, "create_connection", refuse)
    monkeypatch.setattr(notify.socket, "getaddrinfo", lambda *a, **k: [
        (None, None, None, "", ("93.184.216.34", 0))])
    for url, port in (("https://hook.example.org/h", 443), ("http://hook.example.org/h", 80),
                      ("https://hook.example.org:8443/h", 8443)):
        with pytest.raises(notify.DeliveryError, match="could not reach"):
            notify.post(url, b"{}", [], False, timeout=3)
        address, timeout = seen.pop()
        assert address == ("93.184.216.34", port)
        assert timeout is not None and 0 < timeout <= 3


@pytest.mark.parametrize("answer", [401, 404, 410, 429])
def test_a_4xx_is_not_a_delivery(client, hook, answer):
    tok = register(client, "Subscriber")
    hook.answer = answer
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]
    assert deliver(client) == (0, 1)
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "requested" and "answered %d" % answer in sub["error"]


def test_the_timer_does_not_outlive_the_attempt(hook):
    before = {t for t in threading.enumerate() if isinstance(t, threading.Timer)}
    assert notify.post(hook.url, b"{}", [], True, timeout=5) == 200
    time.sleep(0.05)
    assert [t for t in threading.enumerate()
            if isinstance(t, threading.Timer) and t not in before and t.is_alive()] == []


def test_a_long_error_is_cut_to_a_sentence(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]
    monkeypatch.setattr(notify, "post", lambda *a, **k: (_ for _ in ()).throw(
        notify.DeliveryError("x" * 5000)))
    deliver(client)
    assert len(get_sub(client, tok, sub_id)["error"]) == notify.MAX_ERROR_CHARS


# ---------------------------------------------------------------- resources

def test_status_says_why_a_subscription_is_in_trouble(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    hook.answer = 500
    submit(client, tok, outcome(), monkeypatch)
    deliver(client)
    res = client.get("/fhir/Subscription/%s/$status" % sub_id, headers=auth(tok)).json()
    flat = {p["name"]: _part_value(p) for p in res["entry"][0]["resource"]["parameter"]}
    assert flat["status"] == "error"
    assert "answered 500" in json.dumps(flat["error"])


def test_the_notification_is_shaped_as_the_backport_says(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook, content="full-resource")
    submit(client, tok, outcome(), monkeypatch)
    deliver(client)
    first, focus = hook.received[0][2]["entry"]
    assert first["request"] == {
        "method": "GET",
        "url": "http://testserver/fhir/Subscription/%s/$status" % sub_id}
    assert first["response"] == {"status": "200"}
    assert first["fullUrl"] == "urn:uuid:" + first["resource"]["id"]
    assert focus["request"] == {"method": "POST", "url": "Bundle"}


def test_a_submission_that_is_gone_is_written_off_not_sent_as_none(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    with db(client) as c:
        c.execute("PRAGMA foreign_keys = OFF")
        c.execute("DELETE FROM submissions")
    assert deliver(client) == (0, 1)
    assert hook.received == []
    assert get_sub(client, tok, sub_id)["status"] == "active"
    with db(client) as c:
        assert "no longer exists" in c.execute(
            "SELECT error FROM notifications WHERE state = 'failed'").fetchone()[0]


# ---------------------------------------------------------------- dispatcher

def test_the_loops_dispatcher_comes_back_to_a_subscription(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    dispatcher = notify.Dispatcher(client.data_dir)
    try:
        for n in ("1", "2"):
            submit(client, tok, outcome(), monkeypatch)
            assert [t.result() for t in dispatcher.look()] == [(1, 0)]
            assert hook.statuses()[-1]["notification-event"]["event-number"] == n
        assert dispatcher.busy == set()
        # ...and after a worker that blew up, too
        monkeypatch.setattr(notify, "_drain",
                            lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        submit(client, tok, outcome(), monkeypatch)
        assert [t.result() for t in dispatcher.look()] == [(0, 1)]
        assert dispatcher.busy == set()
    finally:
        dispatcher.close()


def test_a_slow_backlog_is_left_for_the_next_look(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    for _ in range(3):
        submit(client, tok, outcome(), monkeypatch)
    monkeypatch.setattr(notify, "DRAIN_BUDGET", 0.05)
    hook.delay = 0.1
    assert deliver(client) == (1, 0)
    assert deliver(client) == (1, 0)


def test_a_subscription_turned_off_under_a_listed_queue_is_not_sent_to(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook, end="2099-01-01T00:00:00Z")
    submit(client, tok, outcome(), monkeypatch)
    real = notify.due_queues

    def listed_then_ended(*args):
        queues = real(*args)
        with db(client) as c:
            c.execute("UPDATE subscriptions SET status = 'off'")
        return queues
    monkeypatch.setattr(notify, "due_queues", listed_then_ended)
    assert deliver(client) == (0, 0)
    assert hook.received == []
    assert get_sub(client, tok, sub_id)["status"] == "off"


def test_the_loop_never_waits_longer_than_its_cap(tmp_path, monkeypatch):
    sleeps = []

    def look(self, now=None):
        if len(sleeps) == 8:
            raise KeyboardInterrupt
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(notify, "_has_tables", lambda d: True)
    monkeypatch.setattr(notify.Dispatcher, "look", look)
    monkeypatch.setattr(notify.time, "sleep", sleeps.append)
    monkeypatch.setattr(notify, "log", lambda m: None)
    with pytest.raises(KeyboardInterrupt):
        notify.run_loop(str(tmp_path))
    assert max(sleeps) == notify.MAX_LOOP_BACKOFF


def test_the_loop_does_not_look_at_a_database_without_the_tables(tmp_path, monkeypatch, capsys):
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            raise KeyboardInterrupt
    monkeypatch.setattr(notify.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        notify.run_loop(str(tmp_path))
    out = capsys.readouterr().out
    assert out.count("waiting for the service") == 1 and "LOOK FAILED" not in out
    assert sleeps == [notify.POLL] * 3


def test_once_is_red_when_something_was_given_up_on_or_written_off(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    with db(client) as c:
        c.execute("PRAGMA foreign_keys = OFF")
        c.execute("DELETE FROM submissions")          # -> a `server` write-off
    monkeypatch.setattr("sys.argv", ["notify.py", "--once", "--data-dir", client.data_dir])
    assert notify.main() == 1
    assert notify.main() == 0                          # nothing left, nothing wrong


def test_once_against_the_wrong_directory_is_red(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["notify.py", "--once", "--data-dir", str(tmp_path)])
    assert notify.main() == 1
    assert "no subscription tables" in capsys.readouterr().err


# ---------------------------------------------------------------- app.py

@pytest.mark.parametrize("claimed", ["active", "error", "entered-in-error", None, 5])
def test_nobody_talks_a_subscription_active(client, hook, monkeypatch, claimed):
    tok = register(client, "Subscriber")
    body = subscription(hook.url, status=claimed)
    res = subscribe(client, tok, body)
    assert res.status_code == 201 and res.json()["status"] == "requested"
    put = client.put("/fhir/Subscription/" + res.json()["id"], json=body, headers=auth(tok))
    assert put.json()["status"] == "requested"
    submit(client, tok, outcome(), monkeypatch)
    assert deliver(client) == (1, 0)
    assert [s["type"] for s in hook.statuses()] == ["handshake"]


def test_a_subscription_not_yet_shaken_has_nothing_queued_for_it(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    subscribe(client, tok, subscription(hook.url))
    submit(client, tok, outcome(), monkeypatch)
    assert deliver(client) == (1, 0)
    assert [s["type"] for s in hook.statuses()] == ["handshake"]
    with db(client) as c:
        assert c.execute("SELECT events FROM subscriptions").fetchone()[0] == 0


def test_search_by_url_and_status_narrows(client, hook, hook2):
    tok = register(client, "Subscriber")
    subscribe(client, tok, subscription(hook.url))
    subscribe(client, tok, subscription(hook2.url, status="off"))

    def found(**params):
        res = client.get("/fhir/Subscription", params=params, headers=auth(tok)).json()
        return [e["resource"]["channel"]["endpoint"] for e in res.get("entry", [])]
    assert found() == [hook.url, hook2.url]
    assert found(url=hook2.url) == [hook2.url]
    assert found(url="https://nobody.example.org/") == []
    assert found(status="off") == [hook2.url]
    assert found(status="requested,off") == [hook.url, hook2.url]


def test_a_subscription_reads_back_as_it_was_written(client, hook):
    tok = register(client, "Subscriber")
    body = subscription(hook.url, filters=["Bundle?organization=acme", "document-type=eps"],
                        reason="because")
    body["channel"]["header"] = ["X-%d: v" % i for i in range(app_module.MAX_SUBSCRIPTION_HEADERS)]
    res = subscribe(client, tok, body)
    assert res.status_code == 201, res.text
    back = get_sub(client, tok, res.json()["id"])
    assert back["channel"]["header"] == body["channel"]["header"]
    assert back["_criteria"] == body["_criteria"]
    assert back["reason"] == "because" and back["criteria"] == body["criteria"]
    # ...and what reads back can be PUT back
    assert client.put("/fhir/Subscription/" + back["id"], json=back,
                      headers=auth(tok)).status_code == 200
    body["channel"]["header"].append("X-one-too-many: v")
    assert subscribe(client, tok, body).status_code == 400


def test_a_forged_host_header_does_not_end_up_in_the_links(client, hook):
    tok = register(client, "Subscriber")
    client.post("/fhir/Subscription", json=subscription(hook.url),
                headers={"Authorization": "Bearer " + tok,
                         "Host": "evil.example.org/x?y=<script>"})
    deliver(client)
    assert hook.statuses()[0]["subscription"]["reference"].startswith(
        "http://testserver/fhir/Subscription/")


def test_two_creates_at_once_do_not_both_take_the_last_place(client, monkeypatch):
    """The COUNT and the INSERT are microseconds apart, and eight threads at a
    barrier do not land in that window by themselves - so the first COUNT is
    made to sit in it. BEGIN IMMEDIATE is what keeps the others out."""
    monkeypatch.setattr(app_module, "MAX_SUBSCRIPTIONS", 1)
    tok = register(client, "Subscriber")
    gate = threading.Barrier(8, timeout=5)
    codes = []
    counted = threading.Event()

    class Interleaved(sqlite3.Connection):
        def execute(self, sql, *a):
            cur = super().execute(sql, *a)
            if sql.startswith("SELECT COUNT(*) FROM subscriptions") and not counted.is_set():
                counted.set()
                time.sleep(0.5)     # the other seven COUNT now - or wait
            return cur

    def connect():
        conn = sqlite3.connect(app_module.db_path(), timeout=30, factory=Interleaved)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    monkeypatch.setattr(app_module, "connect", connect)

    def create():
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        codes.append(subscribe(client, tok, subscription("https://example.org/h")).status_code)
    threads = [threading.Thread(target=create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [201] + [422] * 7
    with db(client) as c:
        assert c.execute("SELECT count(*) FROM subscriptions").fetchone()[0] == 1


def test_the_metadata_says_what_can_be_done_with_a_subscription(client):
    cs = client.get("/fhir/metadata").json()
    sub = next(r for r in cs["rest"][0]["resource"] if r["type"] == "Subscription")
    assert [i["code"] for i in sub["interaction"]] == [
        "read", "search-type", "create", "update", "delete"]
    assert [p["name"] for p in sub["searchParam"]] == ["url", "status"]
    assert sub["supportedProfile"] == [notify.SUBSCRIPTION_PROFILE]


# ---------------------------------------------------------------- added after the first run

def test_what_the_service_left_queued_for_an_ended_subscription_is_written_off(
        client, hook, monkeypatch):
    tok, sub_id, conn, row = ended_under_a_flight(client, hook, monkeypatch)
    conn.close()
    hook.received.clear()
    assert deliver(client) == (0, 0) and hook.received == []
    with db(client) as c:
        assert [tuple(r) for r in c.execute(
            "SELECT state, error FROM notifications WHERE kind != 'handshake'"
        )] == [("failed", "subscription turned off")]


def test_the_retry_schedule_is_the_documented_one():
    assert notify.BACKOFF == (5, 30, 120, 600, 1800)
    assert notify.HANDSHAKE_BACKOFF == (5, 30)
    assert notify.TRANSIENT_BACKOFF == (5, 30, 120)
    assert 42 * 60 < sum(notify.BACKOFF) < 44 * 60


def test_a_remembered_result_goes_when_its_row_does(client, hook, monkeypatch):
    monkeypatch.setattr(notify, "_UNRECORDED", {})
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    conn, row = in_flight(client)
    key = (notify._database(conn), row["notification_id"])
    conn.close()
    notify._UNRECORDED[key] = notify.DELIVERED
    assert client.delete("/fhir/Subscription/" + sub_id, headers=auth(tok)).status_code == 204
    assert deliver(client) == (0, 0)
    assert notify._UNRECORDED == {}


def test_an_answer_that_never_ends_is_not_read_to_its_end():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def stream():
        conn, _ = srv.accept()
        try:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
            for _ in range(300):              # 3 s, ~19 MB
                conn.sendall(b"x" * 65536)
                time.sleep(0.01)
        except OSError:
            pass
        finally:
            conn.close()
    threading.Thread(target=stream, daemon=True).start()
    started = time.monotonic()
    try:
        assert notify.post("http://127.0.0.1:%d/h" % srv.getsockname()[1], b"{}", [],
                           private_ok=True, timeout=2) == 200
    finally:
        srv.close()
    assert time.monotonic() - started < 1


def test_a_queue_that_cannot_even_be_read_never_costs_the_team_its_submission(
        client, monkeypatch, capsys):
    tok = register(client, "Producer")
    real = sqlite3.Connection.execute

    class Flaky(sqlite3.Connection):
        def execute(self, sql, *a):
            if "FROM subscriptions" in sql and sql.lstrip().startswith("SELECT id, filters"):
                raise sqlite3.OperationalError("no such table: subscriptions")
            return real(self, sql, *a)

    def connect():
        conn = sqlite3.connect(os.path.join(client.data_dir, "connectathon.db"),
                               timeout=30, factory=Flaky)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    monkeypatch.setattr(app_module, "connect", connect)
    res = submit(client, tok, outcome(), monkeypatch)
    assert res.status_code == 200 and res.headers["X-Pass"] == "true"
    assert "could not queue notifications for submission" in capsys.readouterr().out


@pytest.mark.parametrize("head", [
    b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n",
    b"HTTP/1.0 200 OK\r\n\r\n",          # what http.server and many small frameworks say
])
def test_a_body_dripped_after_prompt_headers_is_cut_off_at_the_deadline(head):
    """The existing drip test drips the *status line and headers*. Drip the
    body instead, on a response that closes the connection: http.client then
    sets conn.sock to None inside getresponse(), cut_off() finds nothing to
    shut down, and resp.read(64 KiB) runs for as long as the endpoint keeps
    each recv inside the socket timeout - 64 KiB at a byte per 9 s is a week
    of one worker, and sixteen of those are the pool."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def drip():
        conn, _ = srv.accept()
        try:
            conn.recv(65536)
            conn.sendall(head)
            for _ in range(40):               # 4 s
                time.sleep(0.1)
                conn.sendall(b"x")
        except OSError:
            pass
        finally:
            conn.close()
    threading.Thread(target=drip, daemon=True).start()
    started = time.monotonic()
    try:
        try:
            notify.post("http://127.0.0.1:%d/h" % srv.getsockname()[1], b"{}", [],
                        private_ok=True, timeout=0.5)
        except notify.DeliveryError:
            pass                              # 200 or "did not answer": either is defensible
    finally:
        srv.close()
    assert time.monotonic() - started < 1.5


class Dripper:
    """A TCP proxy that passes what the client says straight on, and what the
    server says a byte at a time with pace(bytes so far) seconds before each -
    every recv() well inside any socket timeout, and the whole of it as slow
    as it likes."""

    def __init__(self, to_port, pace):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.to_port, self.pace = to_port, pace
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            client, _ = self.srv.accept()
            server = socket.create_connection(("127.0.0.1", self.to_port))
        except OSError:
            return

        def up():
            try:
                while data := client.recv(65536):
                    server.sendall(data)
            except OSError:
                pass
        threading.Thread(target=up, daemon=True).start()
        passed = 0
        try:
            while byte := server.recv(1):
                wait = self.pace(passed)
                if wait:
                    time.sleep(wait)
                client.sendall(byte)
                passed += 1
        except OSError:
            pass
        finally:
            client.close()
            server.close()

    def close(self):
        self.srv.close()


class SlowTLSHook:
    """Answers over TLS a header line at a time. (Dripping bytes will not do
    here: a TLS record is read whole or not at all, within the socket timeout,
    so it takes an endpoint that writes small records slowly.)"""

    def __init__(self, pki, every=0.2, lines=30):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(pki / "leaf.pem", pki / "leaf.key")
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.asked = threading.Event()

        def serve():
            try:
                raw, _ = self.srv.accept()
                conn = ctx.wrap_socket(raw, server_side=True)
                conn.recv(65536)
                self.asked.set()
                conn.sendall(b"HTTP/1.1 200 OK\r\n")
                for n in range(lines):
                    time.sleep(every)
                    conn.sendall(b"X-Thinking-%d: still here\r\n" % n)
                conn.sendall(b"Content-Length: 0\r\n\r\n")
            except OSError:
                pass
        threading.Thread(target=serve, daemon=True).start()

    def close(self):
        self.srv.close()


def test_a_deadline_that_falls_inside_the_tls_handshake_still_holds(pki, monkeypatch):
    """Wrapping a socket for TLS takes the descriptor away from the plain one,
    so a deadline that shuts down *that* one during the handshake shuts down
    nothing - and it only fires once. The handshake has a budget of its own,
    counted from after the connect: so a connect stretched to most of the
    deadline (drop a SYN or two), a handshake that ends just after it, and
    then an answer a line at a time, held a worker for as long as it liked."""
    example_org_is_loopback(monkeypatch)
    monkeypatch.setattr(notify, "_TLS", ssl.create_default_context(cafile=pki / "ca.pem"))
    endpoint = SlowTLSHook(pki)                                   # six seconds of answer
    slow = Dripper(endpoint.port, lambda passed: 0.003 if passed < 100 else 0)
    real = socket.create_connection

    def stretched(address, *a, **k):
        if address[1] == slow.port:
            time.sleep(0.4)
        return real(address, *a, **k)
    monkeypatch.setattr(notify.socket, "create_connection", stretched)
    started = time.monotonic()
    try:
        with pytest.raises(notify.DeliveryError, match="within 0.5s"):
            notify.post("https://hook.example.org:%d/h" % slow.port, b"{}", [],
                        private_ok=True, timeout=0.5)
    finally:
        slow.close()
        endpoint.close()
    assert time.monotonic() - started < 1.5


def test_an_https_answer_a_line_at_a_time_is_cut_off_at_the_deadline(pki, monkeypatch):
    """The cut made under a thread that is reading through OpenSSL rather than
    from a socket. What is asserted is when it returns, not what: the status
    line was in, and http.client reads the cut as the end of the headers, so
    this is the 200 the endpoint said it was."""
    example_org_is_loopback(monkeypatch)
    monkeypatch.setattr(notify, "_TLS", ssl.create_default_context(cafile=pki / "ca.pem"))
    endpoint = SlowTLSHook(pki)
    started = time.monotonic()
    try:
        assert notify.post("https://hook.example.org:%d/h" % endpoint.port, b"{}", [],
                           private_ok=True, timeout=0.5) == 200
    finally:
        endpoint.close()
    assert time.monotonic() - started < 1.5
    assert endpoint.asked.is_set()


def test_names_that_never_resolve_hold_up_nobody_elses_lookup(monkeypatch):
    """getaddrinfo cannot be cancelled. A fixed pool of resolver threads is
    used up by as many names that hang, and everybody else's lookups then queue
    behind them until the process is restarted."""
    monkeypatch.setattr(notify, "_LOOKUPS", {})
    real, release = socket.getaddrinfo, threading.Event()

    def getaddrinfo(host, *a, **k):
        if host.startswith("hangs"):
            release.wait(10)
            raise socket.gaierror(socket.EAI_NONAME, "released")
        return real(host, *a, **k)
    monkeypatch.setattr(notify.socket, "getaddrinfo", getaddrinfo)

    def hung(i):
        with pytest.raises(notify.Unresolved):
            notify.resolve_endpoint("http://hangs%d.example/h" % i, True, 0.2)
    try:
        askers = [threading.Thread(target=hung, args=(i,))
                  for i in range(2 * notify.WORKERS)]
        [t.start() for t in askers]
        [t.join() for t in askers]
        started = time.monotonic()
        assert notify.resolve_endpoint("http://localhost:1/h", True, 2)[1] in ("127.0.0.1", "::1")
        assert time.monotonic() - started < 1

        # Retried, a name that hangs is waited on again rather than looked up
        # again: one abandoned thread a name, however long it goes on.
        resolving = lambda: [t for t in threading.enumerate()
                             if t.name.startswith("resolve hangs")]
        before = len(resolving())
        assert before == 2 * notify.WORKERS
        for _ in range(3):
            hung(0)
        assert len(resolving()) == before
        # And abandoned is all they are: nothing waits for them at exit, which
        # is what kept `--once` from ever returning to cron.
        assert all(t.daemon for t in resolving())
    finally:
        release.set()
    wait_for(lambda: not notify._LOOKUPS, 2)
    assert notify._LOOKUPS == {}


def test_a_lookup_answers_everyone_who_was_waiting_on_it(monkeypatch):
    monkeypatch.setattr(notify, "_LOOKUPS", {})
    calls, release, waiting = [], threading.Event(), []

    def getaddrinfo(host, *a, **k):
        calls.append(host)
        release.wait(5)
        return [(None, None, None, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(notify.socket, "getaddrinfo", getaddrinfo)

    # Released only once all four are waiting on the one lookup: let go as
    # soon as the first has asked, an asker still on its way in finds the
    # answer gone and asks again - a race, not the sharing under test.
    class Counted(threading.Event):
        def wait(self, timeout=None):
            waiting.append(1)
            return super().wait(timeout)

    class Lookup(notify._Lookup):
        def __init__(self):
            super().__init__()
            self.done = Counted()
    monkeypatch.setattr(notify, "_Lookup", Lookup)
    got = []
    askers = [threading.Thread(target=lambda: got.append(
        notify.resolve_endpoint("http://hook.example.org/h", False, 5)[1]))
        for _ in range(4)]
    [t.start() for t in askers]
    wait_for(lambda: len(waiting) == 4, 2)
    release.set()
    [t.join() for t in askers]
    assert got == ["93.184.216.34"] * 4 and calls == ["hook.example.org"]
    # And the next one asks afresh: an answer is shared, not cached.
    notify.resolve_endpoint("http://hook.example.org/h", False, 5)
    assert len(calls) == 2


def test_each_subscription_is_sent_its_own_headers_and_nobody_elses(
        client, hook, hook2, monkeypatch):
    tok = register(client, "Subscriber")
    other = register(client, "Other Subscriber")
    for who, where, secret in ((tok, hook, "Bearer one"), (other, hook2, "Bearer two")):
        body = subscription(where.url)
        body["channel"]["header"] = ["Authorization: " + secret]
        assert subscribe(client, who, body).status_code == 201
    deliver(client)
    submit(client, tok, outcome(), monkeypatch)
    deliver(client)
    for where, secret in ((hook, "Bearer one"), (hook2, "Bearer two")):
        assert len(where.received) == 2
        assert {r[1].get("Authorization") for r in where.received} == {secret}


def test_a_stale_result_does_not_move_a_retry_of_our_own_trouble_either(
        client, hook, monkeypatch):
    """The row guard's `faults = ?`: after our own trouble `attempts` has not
    moved, so that clause is all that tells a stale reader from a fresh one."""
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    conn, stale = in_flight(client)
    now = datetime.datetime.now(UTC)
    other = notify.connect(client.data_dir)
    assert notify._record(other, stale, notify.Outcome("transient", "resolver"),
                          now).startswith("NOT SENT")
    other.close()
    assert notify._record(conn, stale, notify.DELIVERED, now) == notify.DROPPED
    conn.close()
    with db(client) as c:
        assert [tuple(r) for r in c.execute(
            "SELECT state, attempts, faults FROM notifications "
            "WHERE kind != 'handshake'")] == [("pending", 0, 1)]


def test_a_connection_made_after_the_deadline_is_cut_as_it_is_made():
    """The deadline can pass while the socket is still being made - a connect
    that took all of it - and there is then nothing to cut until there is."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    conn = notify._PinnedHTTPConnection("127.0.0.1", srv.getsockname()[1],
                                        ["127.0.0.1"], 2)
    try:
        conn.cut()
        conn.connect()
        theirs, _ = srv.accept()
        theirs.settimeout(2)
        assert theirs.recv(1) == b""                # shut down, not merely idle
        theirs.close()
    finally:
        conn.close()
        srv.close()


def test_the_tls_socket_is_held_before_it_is_shaken(monkeypatch):
    """wrap_socket() that handshakes by itself returns only once that is done,
    and until then the only socket anybody holds is the plain one it has taken
    the descriptor from - nothing a deadline can cut."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    conn = notify._PinnedHTTPSConnection("hook.example.org", srv.getsockname()[1],
                                         ["127.0.0.1"], 2)
    seen = {}

    class Wrapped:
        def do_handshake(self):
            seen["held"] = conn._held is self

        def close(self):
            pass

    class Context:
        def wrap_socket(self, sock, **kwargs):
            seen.update(kwargs)
            sock.close()
            return Wrapped()
    monkeypatch.setattr(notify, "_TLS", Context())
    try:
        conn.connect()
    finally:
        srv.close()
    assert seen == {"server_hostname": "hook.example.org",
                    "do_handshake_on_connect": False, "held": True}


def test_a_submission_that_went_with_its_transaction_is_not_answered_200(
        client, hook, monkeypatch):
    """Some failures (a full disk, an I/O error) make SQLite roll back the whole
    transaction rather than the statement. Awkward to provoke honestly, so the
    shape of it is: the transaction gone from under the savepoint, then the
    error. Swallowed like any other queueing failure, that was a 200 and a
    Bundle id for a submission that does not exist."""
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook)
    made = []
    real_connect, real_match = app_module.connect, app_module.filters_match
    monkeypatch.setattr(app_module, "connect",
                        lambda: made.append(real_connect()) or made[-1])

    def aborted(*a):
        made[-1].execute("ROLLBACK")
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(app_module, "filters_match", aborted)
    with pytest.raises(sqlite3.OperationalError, match="rolled back"):
        submit(client, tok, outcome(), monkeypatch)
    monkeypatch.setattr(app_module, "filters_match", real_match)
    with db(client) as c:
        assert c.execute("SELECT count(*) FROM submissions").fetchone()[0] == 0
    assert pending(client) == 0


def test_nor_is_one_that_went_while_the_subscriptions_were_being_read(
        client, monkeypatch):
    """The same, one statement earlier: before there is a subscription to loop
    over, which used to be a way out that skipped the check."""
    tok = register(client, "Subscriber")
    real = app_module.enqueue_notifications

    class Aborting:
        def __init__(self, conn):
            self.conn = conn
        in_transaction = property(lambda self: self.conn.in_transaction)

        def execute(self, sql, *args):
            if "FROM subscriptions" in sql:
                self.conn.execute("ROLLBACK")
                raise sqlite3.OperationalError("disk I/O error")
            return self.conn.execute(sql, *args)
    monkeypatch.setattr(app_module, "enqueue_notifications",
                        lambda conn, *a: real(Aborting(conn), *a))
    with pytest.raises(sqlite3.OperationalError, match="rolled back"):
        submit(client, tok, outcome(), monkeypatch)
    with db(client) as c:
        assert c.execute("SELECT count(*) FROM submissions").fetchone()[0] == 0




# ------------------------------ what a write to one leaves of the others

# Every WHERE id = ? in both programs, turned into one that is always true,
# used to pass the whole suite: what happens to the subscription acted on was
# pinned everywhere, and that nobody else's moved, nowhere.

def _bystander(client, hook2, monkeypatch):
    """Another company's healthy subscription with one event still queued."""
    tok = register(client, "Bystander")
    body = subscription(hook2.url, filters=["organization=bystander"])
    body["channel"]["header"] = ["Authorization: Bearer bystander-secret"]
    res = subscribe(client, tok, body)
    assert deliver(client)[0] >= 1
    submit(client, tok, outcome(), monkeypatch)
    hook2.received.clear()
    return tok, res.json()["id"]


def _snapshot(client, sub_id):
    with db(client) as c:
        return (tuple(c.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()),
                [tuple(r) for r in c.execute(
                    "SELECT id, state, attempts, faults, error FROM notifications "
                    "WHERE subscription_id = ? ORDER BY id", (sub_id,))])


def test_put_touches_nobody_elses_subscription_or_queue(client, hook, hook2, monkeypatch):
    theirs_tok, theirs = _bystander(client, hook2, monkeypatch)
    mine = register(client, "Mine")
    mine_id = subscribe(client, mine, subscription(hook.url)).json()["id"]
    before = _snapshot(client, theirs)
    assert client.put("/fhir/Subscription/" + mine_id, json=subscription(hook.url, content="empty"),
                      headers=auth(mine)).status_code == 200
    assert _snapshot(client, theirs) == before
    deliver(client)
    assert [event_number(r) for r in hook2.received if len(r[2]["entry"][0]["resource"]["parameter"]) > 5] == ["1"]


def test_delete_touches_nobody_elses_subscription(client, hook, hook2, monkeypatch):
    theirs_tok, theirs = _bystander(client, hook2, monkeypatch)
    mine = register(client, "Mine")
    mine_id = subscribe(client, mine, subscription(hook.url)).json()["id"]
    before = _snapshot(client, theirs)
    assert client.delete("/fhir/Subscription/" + mine_id, headers=auth(mine)).status_code == 204
    assert _snapshot(client, theirs) == before


def test_giving_up_on_one_subscription_touches_nobody_elses(client, hook, hook2, monkeypatch):
    mine = register(client, "Mine")
    active_subscription(client, mine, hook, filters=["organization=mine"])
    theirs_tok, theirs = _bystander(client, hook2, monkeypatch)
    submit(client, mine, outcome(), monkeypatch)
    before = _snapshot(client, theirs)
    with db(client) as c:
        c.execute("UPDATE notifications SET attempts = ? WHERE state = 'pending' "
                  "AND subscription_id != ?", (len(notify.BACKOFF), theirs))
    conn = notify.connect(client.data_dir)
    row = [q[0] for q in notify.due_queues(conn, datetime.datetime.now(UTC)) if q[0]["id"] != int(theirs)][0]
    assert notify._record(conn, row, FAILED_500, datetime.datetime.now(UTC)) == "gave up, subscription off"
    conn.close()
    assert _snapshot(client, theirs) == before


def test_a_handshake_written_off_touches_nobody_elses(client, hook, hook2, monkeypatch):
    theirs_tok, theirs = _bystander(client, hook2, monkeypatch)
    mine = register(client, "Mine")
    subscribe(client, mine, subscription(hook.url))
    before = _snapshot(client, theirs)
    conn = notify.connect(client.data_dir)
    row = [q[0] for q in notify.due_queues(conn, datetime.datetime.now(UTC)) if q[0]["kind"] == "handshake"][0]
    assert notify._record(conn, row, notify.Outcome("server", "x"), datetime.datetime.now(UTC)).startswith("WRITTEN OFF")
    conn.close()
    assert _snapshot(client, theirs) == before


def test_one_subscription_ending_ends_nobody_elses(client, hook, hook2, monkeypatch):
    mine = register(client, "Mine")
    mine_id = active_subscription(client, mine, hook, end="2099-01-01T00:00:00Z",
                                  filters=["organization=mine"])
    theirs_tok, theirs = _bystander(client, hook2, monkeypatch)
    past = notify.stamp(datetime.datetime.now(UTC) - datetime.timedelta(seconds=1))
    with db(client) as c:
        c.execute("UPDATE subscriptions SET end_ts = ? WHERE id = ?", (past, mine_id))
    submit(client, mine, outcome(), monkeypatch)       # bystander filters on its own slug
    assert get_sub(client, mine, mine_id)["status"] == "off"
    assert get_sub(client, theirs_tok, theirs)["status"] == "active"


def test_a_stored_filter_that_no_longer_parses_matches_nothing():
    for broken in (["patient=123"], ["Encounter?organization=acme"], ["organization="], [5]):
        with pytest.raises(app_module.StaleFilter):
            app_module.filters_match(broken, "acme", "eps")


def test_a_stale_filter_is_named_in_the_log_and_told_to_its_owner(
        client, hook, monkeypatch, capsys):
    """A filter the rules changed under matches nothing - and says so where
    the owner will look, without a gap in the count: nothing says the document
    would have matched."""
    tok = register(client, "Acme")
    sub_id = int(active_subscription(client, tok, hook, filters=["organization=acme"]))
    with db(client) as c:
        c.execute("UPDATE subscriptions SET filters = ? WHERE id = ?",
                  (json.dumps(["patient=123"]), sub_id))
    capsys.readouterr()
    submit(client, tok, outcome(), monkeypatch)
    assert "Subscription/%d: a stored filter no longer parses" % sub_id \
        in capsys.readouterr().out
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "active"
    assert "patient=123" in sub["error"] and "PUT the subscription" in sub["error"]
    with db(client) as c:
        assert c.execute("SELECT events FROM subscriptions WHERE id = ?",
                         (sub_id,)).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM notifications WHERE kind != "
                         "'handshake'").fetchone()[0] == 0
    assert not hook.received


@pytest.mark.parametrize("value, allowed", [("0", False), ("false", False), ("no", False),
                                            ("off", False), (" ", False), ("1", True), ("TRUE", True)])
def test_only_a_yes_lifts_the_public_only_rule(monkeypatch, value, allowed):
    monkeypatch.setenv("SUBSCRIPTION_ALLOW_PRIVATE", value)
    assert notify.allow_private() is allowed


# ------------------------------------------------ what a second review found


def test_a_header_named_twice_is_refused_rather_than_half_sent(client, hook):
    """post() sends one value per name. Echoing two back and sending the last
    is a 401 at the subscriber's end that nothing here explains."""
    tok = register(client, "Subscriber")
    body = subscription(hook.url)
    body["channel"]["header"] = ["X-Key: one", "x-key: two"]
    res = subscribe(client, tok, body)
    assert res.status_code == 400 and "once" in diagnostics(res)


def test_put_writes_its_links_against_where_it_was_put(client, hook):
    tok = register(client, "Subscriber")
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]
    assert client.put("/fhir/Subscription/" + sub_id, json=subscription(hook.url),
                      headers=dict(auth(tok), Host="elsewhere.example:8000")
                      ).status_code == 200
    deliver(client)
    assert hook.statuses()[-1]["subscription"]["reference"] == \
        "http://elsewhere.example:8000/fhir/Subscription/" + sub_id


def test_a_document_that_is_pythons_json_and_nobody_elses_is_not_sent(
        client, hook, monkeypatch):
    """NaN survives json.load and json.dumps, and is a body the endpoint cannot
    parse - which it would then be charged for."""
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook, content="full-resource")
    submit(client, tok, outcome(), monkeypatch)
    team_dir = os.path.join(client.data_dir, "submissions", "subscriber")
    for name in os.listdir(team_dir):
        with open(os.path.join(team_dir, name), "w") as fh:
            fh.write('{"resourceType": "Bundle", "total": NaN}')
    assert deliver(client) == (0, 1)
    assert hook.received == []
    assert get_sub(client, tok, sub_id)["status"] == "active"
    with db(client) as c:
        assert c.execute("SELECT state FROM notifications WHERE kind = "
                         "'event-notification'").fetchone()[0] == "failed"


def test_only_so_many_workers_hold_a_whole_document_at_once(monkeypatch):
    """Sixteen 5 MB documents being wrapped at once were 626 MB, in a
    container allowed 512."""
    holding, most, lock = [0], [0], threading.Lock()

    def held(data_dir, row, private_ok):
        with lock:
            holding[0] += 1
            most[0] = max(most[0], holding[0])
        time.sleep(0.05)
        with lock:
            holding[0] -= 1
        return notify.DELIVERED
    monkeypatch.setattr(notify, "_attempt_held", held)
    row = {"kind": "event-notification", "content": "full-resource"}
    threads = [threading.Thread(target=notify._attempt, args=("", row, True))
               for _ in range(notify.WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert most[0] == notify.WHOLE_DOCUMENTS
    # and nobody else waits on them
    assert notify._attempt("", dict(row, content="id-only"), True) is notify.DELIVERED


# ------------------------------------------------ what a third review found

@pytest.mark.parametrize("address", [
    "::ffff:0:7f00:1",         # IPv4-translated (RFC 2765) spelling of 127.0.0.1
    "::ffff:0:a9fe:a9fe",      # ... of 169.254.169.254
    "::ffff:0:a00:5",          # ... of 10.0.0.5
])
def test_ipv4_translated_spelling_of_a_forbidden_v4_address(client, address):
    """The one wrapper the table lacked: ::ffff:0:0:0/96 is global as far as
    its own bits go, and is the IPv4 address in its low 32 bits to an SIIT
    translator."""
    with pytest.raises(notify.EndpointError):
        notify.vet_address(address, False)
    tok = register(client, "Subscriber")
    assert subscribe(client, tok, subscription("http://[%s]/h" % address)
                     ).status_code == 400
    # link-local stays refused whatever the network
    if address.endswith("a9fe:a9fe"):
        with pytest.raises(notify.EndpointError):
            notify.vet_address(address, True)


def test_a_slow_but_working_resolver_is_not_charged_to_the_endpoint(hook, monkeypatch):
    """A lookup that comes back at the last moment left the endpoint
    milliseconds, and the failure that followed was charged to it - six times,
    and then the subscription was turned off over a resolver."""
    real = socket.getaddrinfo
    slow = [0.0]

    def getaddrinfo(host, *a, **k):
        if str(host).endswith(".example.org"):
            time.sleep(slow[0])
            return real("127.0.0.1", *a, **k)
        return real(host, *a, **k)
    monkeypatch.setattr(notify.socket, "getaddrinfo", getaddrinfo)
    url = "http://hook.example.org:%d/h" % hook.server.server_address[1]

    # Slower than its share: the resolver's trouble, and ours.
    slow[0] = 0.3
    with pytest.raises(notify.ServerTrouble, match="could not be looked up within 0.2s"):
        notify.post(url, b"{}", [], True, timeout=0.4)
    assert hook.received == []
    # Within its share: the endpoint keeps the rest, and answers in it.
    slow[0] = 0.1
    assert notify.post(url, b"{}", [], True, timeout=0.4) == 200
    # And an endpoint that then does not answer is told how long it really had.
    hook.delay = 2
    slow[0] = 0.2
    with pytest.raises(notify.DeliveryError, match=r"did not answer within 0\.[678]s"):
        notify.post(url, b"{}", [], True, timeout=1)


@pytest.mark.parametrize("errno_", [errno.EMFILE, errno.ENETUNREACH])
def test_this_boxs_own_connection_failures_are_not_charged_to_the_subscriber(
        client, hook, monkeypatch, errno_):
    """Out of file descriptors, or no route out: the endpoint was never
    reached, and used to be blamed for it - `error`, then `off`."""
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    real = socket.create_connection

    def cannot(*a, **k):
        raise OSError(errno_, os.strerror(errno_))
    monkeypatch.setattr(notify.socket, "create_connection", cannot)
    assert deliver(client) == (0, 1)
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "active" and "error" not in sub
    with db(client) as c:
        row = c.execute("SELECT attempts, faults, error FROM notifications "
                        "WHERE kind = 'event-notification'").fetchone()
    assert (row["attempts"], row["faults"]) == (0, 1)
    assert "not your endpoint's doing" in row["error"]
    assert errno.errorcode[errno_] in row["error"]
    # whereas a connection the endpoint refuses is still the endpoint's
    monkeypatch.setattr(notify.socket, "create_connection", real)
    hook.close()
    now = datetime.datetime.now(UTC) + datetime.timedelta(seconds=10)
    assert deliver(client, now) == (0, 1)
    assert get_sub(client, tok, sub_id)["status"] == "error"


def test_the_second_vetted_address_is_tried_when_the_first_cannot_be_reached(
        hook, monkeypatch):
    """Pinning the connection to a vetted address lost what connecting to a
    name gives for free: a host with an IPv6 record this box cannot route to
    and an IPv4 one it can is reachable."""
    real_resolve, real_connect = socket.getaddrinfo, socket.create_connection
    port = hook.server.server_address[1]
    monkeypatch.setattr(notify.socket, "getaddrinfo", lambda host, *a, **k: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
    ] if host == "dual.example.org" else real_resolve(host, *a, **k))
    tried = []

    def create_connection(address, *a, **k):
        tried.append(address[0])
        if address[0] == "2001:db8::1":
            raise OSError(errno.ENETUNREACH, "Network is unreachable")
        return real_connect(address, *a, **k)
    monkeypatch.setattr(notify.socket, "create_connection", create_connection)
    assert notify.post("http://dual.example.org:%d/h" % port, b"{}", [], True) == 200
    assert tried == ["2001:db8::1", "127.0.0.1"]
    assert len(hook.received) == 1
    # every address is still vetted, not just the one that was reachable
    monkeypatch.setattr(notify.socket, "getaddrinfo", lambda host, *a, **k: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port)),
    ])
    with pytest.raises(notify.DeliveryError, match="169.254.169.254"):
        notify.post("http://dual.example.org:%d/h" % port, b"{}", [], True)


def test_after_a_whole_transaction_rollback_no_gap_is_committed(
        client, hook, hook2, monkeypatch):
    """SQLite rolling back the whole transaction, honestly provoked: a trigger
    that does what a full disk does. The submission is gone, the submitter is
    told so - and nobody's event count moves for a document that does not
    exist, which used to be committed on the way out, one savepoint at a
    time, as a gap in every subscriber's numbering."""
    tok = register(client, "Subscriber")
    for h in (hook, hook2):
        active_subscription(client, tok, h)
    with db(client) as c:
        c.execute("CREATE TRIGGER full BEFORE INSERT ON notifications "
                  "WHEN NEW.kind = 'event-notification' BEGIN "
                  "SELECT RAISE(ROLLBACK, 'database or disk is full'); END")
    try:
        with pytest.raises(sqlite3.OperationalError, match="rolled back"):
            submit(client, tok, outcome(), monkeypatch)
    finally:
        with db(client) as c:
            c.execute("DROP TRIGGER full")
    with db(client) as c:
        assert c.execute("SELECT count(*) FROM submissions").fetchone()[0] == 0
        assert [r[0] for r in c.execute("SELECT events FROM subscriptions")] == [0, 0]
    assert pending(client) == 0


def test_a_dropped_result_still_says_how_the_attempt_went(client, hook, capsys):
    """Whether the old endpoint was handed the notification before its owner
    re-pointed the subscription is what somebody asking about a duplicate
    needs to know, and this line is the only record of it."""
    tok = register(client, "Subscriber")
    hook.delay = 0.5
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]
    dispatcher = notify.Dispatcher(client.data_dir)
    try:
        tasks = dispatcher.look()
        wait_for(lambda: hook.received, 2)
        assert client.delete("/fhir/Subscription/" + sub_id,
                             headers=auth(tok)).status_code == 204
        [t.result() for t in tasks]
    finally:
        dispatcher.close()
    out = capsys.readouterr().out
    assert "result dropped, the subscription changed meanwhile (was delivered)" in out


def test_giving_up_says_what_to_do_about_it(client, hook):
    tok = register(client, "Subscriber")
    hook.answer = 500
    sub_id = subscribe(client, tok, subscription(hook.url)).json()["id"]
    now = datetime.datetime.now(UTC)
    for _ in range(len(notify.HANDSHAKE_BACKOFF) + 1):
        deliver(client, now)
        now += datetime.timedelta(seconds=max(notify.HANDSHAKE_BACKOFF) + 1)
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "off"
    assert sub["error"] == ("Gave up after 3 attempts: the endpoint answered 500. "
                            "PUT the subscription to try again")


@pytest.mark.parametrize("header", [None, "Authorization: Bearer x", {"X-A": "b"}])
def test_a_channel_header_that_is_not_a_list_is_told_so(client, header):
    tok = register(client, "Subscriber")
    body = subscription("https://example.org/h")
    body["channel"]["header"] = header
    res = subscribe(client, tok, body)
    assert res.status_code == 400
    assert "must be a list" in res.json()["issue"][0]["diagnostics"]


def test_a_malformed_criteria_is_refused_rather_than_read_as_no_filter(client, hook):
    """The `extension` wrapper forgotten, and a filter for one company's
    documents used to be no filter at all: 201, and the room's documents."""
    tok = register(client, "Subscriber")
    ext = {"url": notify.FILTER_CRITERIA_EXT, "valueString": "organization=nobody"}
    for bad in ([ext], {"extension": ext}, {"extension": [ext, "x"]}, "organization=nobody"):
        body = subscription(hook.url)
        body["_criteria"] = bad
        res = subscribe(client, tok, body)
        assert res.status_code == 400, bad
        assert "_criteria" in res.json()["issue"][0]["diagnostics"]
    body = subscription(hook.url)
    body["channel"]["_payload"] = [{"url": notify.PAYLOAD_CONTENT_EXT, "valueCode": "empty"}]
    res = subscribe(client, tok, body)
    assert res.status_code == 400 and "_payload" in res.json()["issue"][0]["diagnostics"]
    # somebody else's extension on the element is theirs, and passes over
    body = subscription(hook.url)
    body["_criteria"] = {"extension": [{"url": "http://example.org/note", "valueString": "x"}]}
    res = subscribe(client, tok, body)
    assert res.status_code == 201 and "_criteria" not in res.json()


def test_a_subscription_body_that_is_not_json_or_too_big_is_refused(client, hook):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    for call, path in ((client.post, "/fhir/Subscription"),
                       (client.put, "/fhir/Subscription/" + sub_id)):
        res = call(path, content=b"{not json", headers=dict(
            auth(tok), **{"Content-Type": "application/fhir+json"}))
        assert res.status_code == 400 and "not valid JSON" in res.text
        big = dict(subscription(hook.url), reason="r" * app_module.MAX_JSON_BODY_BYTES)
        assert call(path, json=big, headers=auth(tok)).status_code == 413
    assert get_sub(client, tok, sub_id)["status"] == "active"


def test_put_drops_only_what_was_pending(client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    deliver(client)
    submit(client, tok, outcome(), monkeypatch)             # one still pending
    assert client.put("/fhir/Subscription/" + sub_id, json=subscription(hook.url),
                      headers=auth(tok)).status_code == 200
    with db(client) as c:
        assert [tuple(r) for r in c.execute(
            "SELECT kind, state FROM notifications ORDER BY id")] == [
            ("handshake", "delivered"), ("event-notification", "delivered"),
            ("handshake", "pending")]


def test_reason_is_cut_to_size_and_has_a_default(client, hook):
    tok = register(client, "Subscriber")
    res = subscribe(client, tok, subscription(hook.url, reason="  " + "r" * 5000))
    assert res.status_code == 201
    assert res.json()["reason"] == "r" * app_module.MAX_NOTE_CHARS
    res = subscribe(client, tok, subscription(hook.url, reason=5))
    assert res.json()["reason"] == "Notify of documents that pass validation"


def test_a_forwarded_proto_that_is_not_a_scheme_is_ignored(client, hook):
    tok = register(client, "Subscriber")
    client.post("/fhir/Subscription", json=subscription(hook.url),
                headers={"Authorization": "Bearer " + tok, "X-Forwarded-Proto": "ftp"})
    deliver(client)
    assert hook.statuses()[0]["subscription"]["reference"].startswith(
        "http://testserver/fhir/Subscription/")


def test_a_document_that_is_json_but_not_an_object_is_written_off_at_once(
        client, hook, monkeypatch):
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook, content="full-resource")
    submit(client, tok, outcome(), monkeypatch)
    team_dir = os.path.join(client.data_dir, "submissions", "subscriber")
    for name in os.listdir(team_dir):
        with open(os.path.join(team_dir, name), "w") as fh:
            fh.write("[]")
    assert deliver(client) == (0, 1)
    assert hook.received == []
    assert get_sub(client, tok, sub_id)["status"] == "active"
    with db(client) as c:
        assert tuple(c.execute("SELECT state, faults FROM notifications WHERE kind "
                               "= 'event-notification'").fetchone()) == ("failed", 1)


# ------------------------------------------------- added after the review pass

def _dual_stack(port, v4_first=True):
    v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
    v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0))
    return lambda host, *a, **k: [v4, v6] if v4_first else [v6, v4]


@pytest.mark.parametrize("v4_first", [True, False])
def test_a_refused_connection_is_the_endpoints_whichever_address_this_box_could_not_route_to(
        monkeypatch, v4_first):
    """A dual-stack endpoint that is down, called from a box with no IPv6
    route: refused on the A record, unreachable on the AAAA. The refusal is
    the endpoint's and must be what is reported - the resolver sorts the
    unroutable address last, so "the last error" would call every dead
    endpoint this server's routing."""
    tried = []

    def create_connection(address, *a, **k):
        tried.append(address[0])
        if address[0] == "127.0.0.1":
            raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
        raise OSError(errno.ENETUNREACH, "Network is unreachable")
    monkeypatch.setattr(notify.socket, "getaddrinfo", _dual_stack(9, v4_first))
    monkeypatch.setattr(notify.socket, "create_connection", create_connection)
    with pytest.raises(notify.DeliveryError, match="refused"):
        notify.post("http://dual%d.example.org:9/h" % v4_first, b"{}", [], True)
    assert sorted(tried) == ["127.0.0.1", "2001:db8::1"]


def test_only_when_every_address_was_this_boxs_trouble_is_it_server_trouble(monkeypatch):
    def create_connection(address, *a, **k):
        raise OSError(errno.ENETUNREACH, "Network is unreachable")
    monkeypatch.setattr(notify.socket, "getaddrinfo", _dual_stack(9))
    monkeypatch.setattr(notify.socket, "create_connection", create_connection)
    with pytest.raises(notify.ServerTrouble, match="ENETUNREACH"):
        notify.post("http://dual-down.example.org:9/h", b"{}", [], True)


@pytest.mark.parametrize("code", ["413 Payload Too Large", "401 Unauthorized"])
def test_an_endpoint_that_answers_early_over_tls_is_explained_by_its_answer(
        pki, monkeypatch, code):
    """The plain-HTTP version of this is BrokenPipe; over TLS the same hang-up
    is an "EOF in violation of protocol", and every real subscriber is https.
    Reported as a TLS failure it sends them checking certificates."""
    example_org_is_loopback(monkeypatch)
    monkeypatch.setattr(notify, "_TLS", ssl.create_default_context(cafile=pki / "ca.pem"))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pki / "leaf.pem", pki / "leaf.key")

    def answer_early(raw):
        conn = ctx.wrap_socket(raw, server_side=True)
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += conn.recv(4096)
        conn.sendall(("HTTP/1.1 %s\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                      % code).encode())
        time.sleep(0.2)
        conn.close()
    port = _one_connection(answer_early)
    with pytest.raises(notify.DeliveryError, match="answered %s" % code[:3]):
        notify.post("https://hook.example.org:%d/h" % port, b"x" * (8 * 1024 * 1024),
                    [], private_ok=True, timeout=5)


def test_a_2xx_answered_before_the_body_was_taken_is_delivered_and_said(capsys):
    """The endpoint's word stands - but an operator asked why a platform never
    ingested an event the log calls delivered must be able to find this."""
    def answer_at_once(conn):
        conn.settimeout(5)
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    port = _one_connection(answer_at_once)
    body = b"{" + b" " * (8 * 1024 * 1024) + b"}"
    assert notify.post("http://127.0.0.1:%d/hook" % port, body, [], True) == 200
    out = capsys.readouterr().out
    assert "127.0.0.1 answered 200 before it had taken the whole notification" in out
    assert "counted as delivered" in out


def test_the_dispatcher_starts_every_worker_up_front_or_none(client, hook, monkeypatch):
    """submit() queues the work before it starts a thread for it, so a thread
    that fails to start mid-run would leave a work item behind for the next
    free worker, after look() had given the subscription up as never started
    - two workers on one subscription. So the pool is filled at construction,
    where a failure is loud and leaves nothing behind, not even the lock."""
    real_start = threading.Thread.start

    def cannot_start(self):
        if self.name.startswith("deliver"):
            raise RuntimeError("can't start new thread")
        return real_start(self)
    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        notify.Dispatcher(client.data_dir)
    monkeypatch.setattr(threading.Thread, "start", real_start)
    dispatcher = notify.Dispatcher(client.data_dir)        # the lock was let go
    try:
        tok = register(client, "Subscriber")
        subscribe(client, tok, subscription(hook.url))
        subscribe(client, tok, subscription(hook.url + "2"))
        # From here on no thread is ever started, and nothing needs one.
        monkeypatch.setattr(threading.Thread, "start", cannot_start)
        assert sorted(t.result() for t in dispatcher.look()) == [(1, 0), (1, 0)]
        assert len(hook.received) == 2
    finally:
        monkeypatch.setattr(threading.Thread, "start", real_start)
        dispatcher.close()


def test_a_write_off_behind_a_failing_event_does_not_call_the_subscription_active(
        client, hook, monkeypatch):
    """`error` means something is being retried. Writing off event 1 while
    event 2 is still queued must not report the subscription healthy."""
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook, content="full-resource")
    submit(client, tok, outcome(), monkeypatch)
    submit(client, tok, outcome(), monkeypatch)
    hook.answer = 500
    now = datetime.datetime.now(UTC)
    assert deliver(client, now) == (0, 1)
    assert get_sub(client, tok, sub_id)["status"] == "error"
    hook.answer = 200
    with db(client) as c:
        slug, name = c.execute("SELECT t.slug, s.filename FROM submissions s "
                               "JOIN teams t ON t.id = s.team_id ORDER BY s.id").fetchone()
    os.remove(os.path.join(client.data_dir, "submissions", slug, name))
    monkeypatch.setattr(notify, "DRAIN_BUDGET", -1)      # one notification per pass
    assert deliver(client, now + datetime.timedelta(seconds=6)) == (0, 1)
    sub = get_sub(client, tok, sub_id)
    assert sub["status"] == "error"
    assert sub["error"].startswith("Event 1 was not sent, and will not be:")
    assert deliver(client, now + datetime.timedelta(seconds=7)) == (1, 0)
    assert get_sub(client, tok, sub_id)["status"] == "active"


def test_a_fault_that_was_never_an_attempt_still_waits_its_backoff(
        client, hook, monkeypatch):
    """attempts = 0 and faults = 1 is not "never tried": due_queues must
    honour the stamp _record set, or the failing resource is hit every pass."""
    tok = register(client, "Subscriber")
    active_subscription(client, tok, hook, content="full-resource")
    submit(client, tok, outcome(), monkeypatch)
    real_open, calls = open, []

    def flaky_open(path, *a, **k):
        if "submissions" in str(path) and not calls:
            calls.append(path)
            raise OSError(24, "Too many open files", str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr(notify, "open", flaky_open, raising=False)
    now = datetime.datetime.now(UTC)
    assert deliver(client, now) == (0, 1)
    assert deliver(client, now + datetime.timedelta(seconds=1)) == (0, 0)
    assert deliver(client, now + datetime.timedelta(seconds=4)) == (0, 0)
    assert hook.received == []
    assert deliver(client, now + datetime.timedelta(seconds=6)) == (1, 0)


def test_a_host_label_too_long_to_resolve_is_the_endpoints_fault():
    """The door lets a 64-character label in (it is printable ASCII); the
    resolver refuses it with a UnicodeError, not a gaierror. That is a wrong
    name, to be told to the subscriber - not a bug to be retried."""
    url = "https://%s.example.org/h" % ("a" * 64)
    notify.check_endpoint(url, False)
    with pytest.raises(notify.EndpointError, match="does not resolve"):
        notify.resolve_addresses(url, False)


@pytest.mark.parametrize("code, expected", [
    (socket.EAI_AGAIN, notify.Unresolved), (socket.EAI_FAIL, notify.Unresolved),
    (socket.EAI_SYSTEM, notify.Unresolved), (socket.EAI_NONAME, notify.EndpointError),
])
def test_which_resolver_answers_are_the_names_fault(monkeypatch, code, expected):
    def answer(host, *a, **k):
        raise socket.gaierror(code, "resolver said %d" % code)
    monkeypatch.setattr(notify.socket, "getaddrinfo", answer)
    with pytest.raises(expected):
        notify.resolve_addresses("http://eai%d.example.org/h" % abs(code), False)


def test_a_network_error_after_connecting_counts_against_the_endpoint(hook, monkeypatch):
    """OUR_ERRNOS are this box's only before there was a connection. Once one
    is up, whatever breaks it is an attempt the endpoint had."""
    def send(self, data):
        if self.sock is None:
            self.connect()
        raise OSError(errno.ENETUNREACH, "Network is unreachable")
    monkeypatch.setattr(notify._PinnedHTTPConnection, "send", send)
    with pytest.raises(notify.DeliveryError, match="could not reach the endpoint"):
        notify.post(hook.url, b"{}", [], True)


def test_the_second_address_is_not_tried_once_the_deadline_has_passed(monkeypatch):
    """N black-holed addresses must cost one deadline, not N connect timeouts,
    or one host holds a worker and its queue for minutes."""
    tried = []

    def black_hole(address, *a, **k):
        tried.append(address[0])
        time.sleep(0.6)
        raise OSError(errno.ETIMEDOUT, "Connection timed out")
    monkeypatch.setattr(notify.socket, "getaddrinfo", _dual_stack(9))
    monkeypatch.setattr(notify.socket, "create_connection", black_hole)
    started = time.monotonic()
    with pytest.raises(notify.DeliveryError, match="did not take the notification"):
        notify.post("http://blackhole.example.org:9/h", b"{}", [], True, timeout=0.3)
    assert tried == ["127.0.0.1"]
    assert time.monotonic() - started < 1.0


def test_an_event_notification_carries_no_error_parameter_even_while_there_is_one(
        client, hook, monkeypatch):
    """`error` is $status's - the backport's Parameters for a notification
    have no such part, and a subscriber validates the shape it is sent."""
    tok = register(client, "Subscriber")
    sub_id = active_subscription(client, tok, hook)
    submit(client, tok, outcome(), monkeypatch)
    hook.answer = 500
    now = datetime.datetime.now(UTC)
    assert deliver(client, now) == (0, 1)
    status = client.get("/fhir/Subscription/%s/$status" % sub_id, headers=auth(tok)).json()
    assert "error" in [p["name"] for p in status["entry"][0]["resource"]["parameter"]]
    hook.answer = 200
    assert deliver(client, now + datetime.timedelta(seconds=6)) == (1, 0)
    names = [p["name"] for p in hook.received[-1][2]["entry"][0]["resource"]["parameter"]]
    assert names == ["subscription", "topic", "status", "type",
                     "events-since-subscription-start", "notification-event"]


def test_plain_application_json_is_an_accepted_payload_type(client, hook):
    tok = register(client, "Subscriber")
    body = subscription(hook.url)
    body["channel"]["payload"] = "application/json"
    assert subscribe(client, tok, body).status_code == 201
    body["channel"]["payload"] = "application/xml"
    assert subscribe(client, tok, body).status_code == 400


def test_a_forwarded_proto_list_uses_its_first_entry(client, hook):
    tok = register(client, "Subscriber")
    client.post("/fhir/Subscription", json=subscription(hook.url),
                headers={"Authorization": "Bearer " + tok,
                         "X-Forwarded-Proto": "https, http"})
    deliver(client)
    assert hook.statuses()[0]["subscription"]["reference"].startswith(
        "https://testserver/fhir/Subscription/")

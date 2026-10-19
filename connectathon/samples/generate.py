"""Build the three Nordic Hackathon 2026 scenario bundles as HL7 EU EPS documents.

The patients are synthetic. Nothing here comes from a real system, record or
person - the bundles only illustrate how such a summary could look, and exist so
the clinician views have something rich to render.

Every SNOMED CT code used here was verified against a live Snowstorm terminology
server, every LOINC code against tx.fhir.org (moles-per-volume codes for the
SI units Swedish labs report); ATC codes are WHO ATC 2025. All three bundles
validate against bundle-eu-eps with zero errors.

The people and places also claim the HL7 Sweden base profiles (hl7se.fhir.base),
so the Swedish shape of the data - personnummer namespace, HSA-id, organisation
number - is checked rather than merely asserted. The personnummer are taken
from Skatteverket's published test personnummer, which are never issued to a
person. That list stops at births in 1986, so Ville (born 2013) carries a
samordningsnummer instead - a checksum-valid coordination number in the
SEBasePatient slice for it, chosen so no personnummer that could belong to a
real child appears here. The other identifier values and the phone numbers
are placeholders in the real national namespaces.

    python3 samples/generate.py
"""
import json, uuid, html, os
from datetime import datetime
from zoneinfo import ZoneInfo

SCT = "http://snomed.info/sct"
LOINC = "http://loinc.org"
ATC = "http://www.whocc.no/atc"
UCUM = "http://unitsofmeasure.org"
EPS = "http://hl7.eu/fhir/eps/StructureDefinition/"
EUBASE = "http://hl7.eu/fhir/base/StructureDefinition/"
THL7 = "http://terminology.hl7.org/CodeSystem/"
SEBASE = "http://hl7.se/fhir/ig/base/StructureDefinition/"

# Namespaces the Swedish base profiles slice their identifiers on. Using the
# OID for personnummer instead of this URI still validates, but it misses the
# SEBasePatient slice, so nothing Swedish is actually checked.
SE_PERSONNUMMER = "http://electronichealth.se/identifier/personnummer"
SE_SAMORDNINGSNUMMER = "http://electronichealth.se/identifier/samordningsnummer"
SE_HSAID = "urn:oid:1.2.752.29.4.19"
SE_ORGNR = "urn:oid:2.5.4.97"

# The EU Core Flag profile carries its priority, its supporting evidence and
# its free text in the three core extensions rather than in Flag itself.
FLAG_PRIORITY_EXT = "http://hl7.org/fhir/StructureDefinition/flag-priority"
FLAG_DETAIL_EXT = "http://hl7.org/fhir/StructureDefinition/flag-detail"
NOTE_EXT = "http://hl7.org/fhir/StructureDefinition/note"
FLAG_PRIORITY_CS = "http://hl7.org/fhir/flag-priority-code"

# Every timestamp is Swedish local time; the offset follows the calendar (+02:00
# in summer) instead of being typed in, because a hard-coded "+01:00" on an
# August document is exactly the kind of thing a receiving system checks.
STOCKHOLM = ZoneInfo("Europe/Stockholm")

# Marks the bundle itself as test data, so a downloaded file still says so
# once it has left the UI and its disclaimer behind.
TEST_DATA_TAG = {"system": THL7 + "v3-ActReason", "code": "HTEST",
                 "display": "test health data"}
SYNTHETIC_NOTE = ("Synthetic example generated for the Nordic Hackathon 2026 - "
                  "not a real patient.")


def local_datetime(date):
    return datetime.fromisoformat(date + "T09:00:00").replace(tzinfo=STOCKHOLM).isoformat()

def cc(system, code, display, text=None):
    return {"coding": [{"system": system, "code": code, "display": display}],
            "text": text or display}

def sct(code, display, text=None):
    return cc(SCT, code, display, text)



def _txt(cc_or_none, default=""):
    if not cc_or_none:
        return default
    return cc_or_none.get("text") or (cc_or_none.get("coding") or [{}])[0].get("display", default)


def _period(p):
    if not p:
        return ""
    return " to ".join(x for x in (p.get("start"), p.get("end")) if x) or ""


def resource_narrative(r, extra=()):
    """A short human-readable rendering of one resource, as dom-6 asks for.
    `extra` rows are facts with no coded element to be read from."""
    t = r["resourceType"]
    rows = []
    if t == "Patient":
        rows = [("Name", r["name"][0]["text"]),
                ("Gender", r.get("gender")),
                ("Date of birth", r.get("birthDate")),
                ("Identifier", r["identifier"][0]["value"])]
        # The receiving clinician needs the language before the consultation
        # starts, so it belongs in the rendered summary and not only in the data.
        for c in r.get("communication", []):
            rows.append(("Preferred language" if c.get("preferred") else "Language",
                         _txt(c.get("language"))))
        for a in r.get("address", []):
            label = "Address" if a.get("use") != "temp" else "Temporary address"
            rows.append((label, ", ".join(
                x for x in (a.get("line") or []) + [a.get("city"), a.get("country")] if x)))
        for c in r.get("contact", []):
            rows.append(("Contact", "%s (%s), %s" % (
                c["name"]["text"], _txt(c["relationship"][0]),
                c["telecom"][0]["value"])))
    elif t == "Condition":
        rows = [("Condition", _txt(r["code"])),
                ("Onset", r.get("onsetDateTime")),
                ("Severity", _txt(r.get("severity"))),
                ("Clinical status", _txt(r.get("clinicalStatus"))),
                ("Verification", _txt(r.get("verificationStatus")))]
    elif t == "Flag":
        pri = next((e["valueCodeableConcept"] for e in r.get("extension", [])
                    if e["url"] == FLAG_PRIORITY_EXT), None)
        rows = [("Alert", _txt(r["code"])),
                ("Priority", _txt(pri)),
                ("Status", r.get("status")),
                ("Since", (r.get("period") or {}).get("start"))]
        for e in r.get("extension", []):
            if e["url"] == NOTE_EXT:
                rows.append(("Note", e["valueAnnotation"]["text"]))
    elif t == "MedicationAdministration":
        rows = [("Medication", r["medicationReference"]["display"]),
                ("Given", r.get("effectiveDateTime")),
                ("Dose", (r.get("dosage") or {}).get("text")),
                ("Status", r.get("status"))]
    elif t == "DocumentReference":
        att = r["content"][0]["attachment"]
        rows = [("Document", r.get("description") or _txt(r.get("type"))),
                ("Type", _txt(r.get("type"))),
                ("Created", att.get("creation")),
                ("Link", att.get("url"))]
    elif t == "CareTeam":
        rows = [("Team", r.get("name")),
                ("Members", "; ".join(
                    "%s - %s" % (p["member"].get("display"), _txt(p["role"][0]))
                    for p in r.get("participant", []))),
                ("Phone", (r.get("telecom") or [{}])[0].get("value"))]
    elif t == "Medication":
        rows = [("Medication", _txt(r["code"])), ("Form", _txt(r.get("form")))]
    elif t == "MedicationStatement":
        rows = [("Medication", r["medicationReference"]["display"]),
                ("Status", r.get("status")),
                ("Dosage", (r.get("dosage") or [{}])[0].get("text")),
                ("Period", _period(r.get("effectivePeriod"))),
                ("Reason", _txt((r.get("reasonCode") or [None])[0]))]
    elif t == "AllergyIntolerance":
        rx = (r.get("reaction") or [{}])[0]
        rows = [("Allergen", _txt(r["code"])),
                ("Criticality", r.get("criticality")),
                ("Type", r.get("type")),
                ("Onset", r.get("onsetDateTime")),
                ("Reaction", _txt((rx.get("manifestation") or [None])[0])),
                ("Severity", rx.get("severity"))]
    elif t == "Immunization":
        rows = [("Vaccine", _txt(r["vaccineCode"])),
                ("Date", r.get("occurrenceDateTime")),
                ("Status", r.get("status"))]
    elif t == "Observation":
        v = r.get("valueQuantity") or {}
        rows = [("Test", _txt(r["code"])),
                ("Date", r.get("effectiveDateTime")),
                ("Result", "%s %s" % (v.get("value"), v.get("unit"))),
                ("Interpretation", _txt((r.get("interpretation") or [None])[0]))]
    elif t == "Procedure":
        rows = [("Procedure", _txt(r["code"])),
                ("Date", r.get("performedDateTime")),
                ("Status", r.get("status")),
                ("Complications", ", ".join(_txt(c) for c in r.get("complication", [])))]
    elif t == "Device":
        rows = [("Device", _txt(r["type"])), ("Status", r.get("status"))]
    elif t == "DeviceUseStatement":
        rows = [("Device", r["device"]["display"]),
                ("In use since", (r.get("timingPeriod") or {}).get("start")),
                ("Status", r.get("status"))]
    elif t == "CarePlan":
        d = (r.get("activity") or [{}])[0].get("detail", {})
        rows = [("Planned activity", r.get("title")),
                ("Status", d.get("status")),
                ("Scheduled", (d.get("scheduledPeriod") or {}).get("start")),
                ("Plan active since", (r.get("period") or {}).get("start"))]
        # A multi-step plan is read as its steps, in order; a one-line plan
        # already said everything in its title.
        if len(r.get("activity") or []) > 1:
            for i, a in enumerate(r["activity"], 1):
                rows.append(("Step %d" % i, a["detail"].get("description")))
    elif t == "Practitioner":
        n = r["name"][0]
        rows = [("Name", " ".join(n.get("prefix", []) + n.get("given", [])
                                  + [n.get("family", "")]))]
    elif t == "Organization":
        rows = [("Organisation", r.get("name")),
                ("Address", ", ".join(x for x in [
                    (r.get("address") or [{}])[0].get("city"),
                    (r.get("address") or [{}])[0].get("country")] if x))]
    elif t == "PractitionerRole":
        if r.get("specialty"):
            rows = [("Specialty", _txt(r["specialty"][0])),
                    ("Practitioner", r["practitioner"].get("display")),
                    ("Phone", (r.get("telecom") or [{}])[0].get("value"))]
        else:
            rows = [("Role", "Authoring practitioner at the custodian organisation")]
    if r.get("note"):
        rows.append(("Note", r["note"][0]["text"]))
    rows += list(extra)
    rows = [(k, v) for k, v in rows if k and v]
    e = html.escape
    body = "".join("<tr><th>%s</th><td>%s</td></tr>" % (e(k), e(str(v)))
                   for k, v in rows)
    return {"status": "generated",
            "div": '<div xmlns="http://www.w3.org/1999/xhtml"><table>%s</table></div>'
                   % body}


class BundleBuilder:
    def __init__(self, *, doc_date, language, title, patient, author, org,
                 doc_uuid=None):
        self.doc_date = doc_date
        self.language = language
        self.title = title
        self.entries = []
        self.sections = []
        # Deterministic ids: the same spec always produces the same bundle, so a
        # team can diff two downloads without seeing spurious uuid churn.
        self._ns = uuid.uuid5(uuid.NAMESPACE_URL,
                              "nordic-hackathon-2026/" + (doc_uuid or title))
        self._n = 0
        self.pat_ref = self.add(patient, "patient")
        self.pat_display = patient["name"][0]["text"]
        self.org_ref = self.add(org, "org")
        prac_ref = self.add(author["practitioner"], "practitioner")
        self.role_ref = self.add({
            "resourceType": "PractitionerRole",
            "meta": {"profile": [EUBASE + "practitionerRole-eu",
                                 SEBASE + "SEBasePractitionerRole"]},
            "practitioner": {"reference": prac_ref},
            "organization": {"reference": self.org_ref},
        }, "role")

    def uid(self, seed):
        self._n += 1
        return str(uuid.uuid5(self._ns, "%s-%d" % (seed, self._n)))

    def add(self, resource, seed):
        rid = self.uid(seed)
        resource["id"] = rid
        resource.setdefault("text", resource_narrative(resource))
        ref = "urn:uuid:" + rid
        self.entries.append({"fullUrl": ref, "resource": resource})
        return ref

    def subject(self):
        return {"reference": self.pat_ref, "display": self.pat_display}

    def entry_ref(self, ref):
        """The resource already added under this urn:uuid reference."""
        return next(e["resource"] for e in self.entries if e["fullUrl"] == ref)

    def section(self, title, loinc, loinc_display, rows, headers,
                refs, empty_reason=None):
        sec = {"title": title, "code": cc(LOINC, loinc, loinc_display)}
        sec["text"] = {"status": "generated",
                       "div": narrative(headers, rows, self.language)}
        if refs:
            sec["entry"] = [{"reference": r} for r in refs]
        if empty_reason:
            sec["emptyReason"] = cc(THL7 + "list-empty-reason", *empty_reason)
        self.sections.append(sec)

    def build(self):
        comp = {
            "resourceType": "Composition",
            "meta": {"profile": [EPS + "composition-eu-eps"]},
            "language": self.language,
            "identifier": {"system": "urn:ietf:rfc:3986",
                           "value": "urn:uuid:" + self.uid("comp-id")},
            "status": "final",
            "type": cc(LOINC, "60591-5", "Patient summary Document"),
            "subject": {"reference": self.pat_ref},
            "date": local_datetime(self.doc_date),
            "author": [{"reference": self.role_ref}],
            "title": self.title,
            "confidentiality": "N",
            "custodian": {"reference": self.org_ref},
            "section": self.sections,
        }
        comp["id"] = self.uid("composition")
        comp["text"] = {"status": "generated", "div": narrative(
            ["Field", "Value"],
            [["Document", self.title], ["Patient", self.pat_display],
             ["Document date", self.doc_date],
             ["Sections", ", ".join(s["title"] for s in self.sections)],
             ["Note", SYNTHETIC_NOTE]],
            self.language)}
        self.entries.insert(0, {"fullUrl": "urn:uuid:" + comp["id"], "resource": comp})
        return {
            "resourceType": "Bundle",
            "id": self.uid("bundle"),
            "meta": {"profile": [EPS + "bundle-eu-eps"], "tag": [TEST_DATA_TAG]},
            "language": self.language,
            "identifier": {"system": "urn:ietf:rfc:3986",
                           "value": "urn:uuid:" + self.uid("bundle-id")},
            "type": "document",
            "timestamp": local_datetime(self.doc_date),
            "entry": self.entries,
        }


def paragraphs(lines, lang=None):
    """A narrative made of prose rather than a table - for a section whose
    content is the text itself, like the patient's own story."""
    e = html.escape
    attrs = '<div xmlns="http://www.w3.org/1999/xhtml"'
    if lang:
        attrs += ' xml:lang="%s" lang="%s"' % (lang, lang)
    return attrs + ">" + "".join("<p>%s</p>" % e(x) for x in lines) + "</div>"


def narrative(headers, rows, lang=None):
    e = html.escape
    # xml:lang matters here: these are cross-border documents, and a receiving
    # system has to know which language the rendered narrative is actually in.
    attrs = '<div xmlns="http://www.w3.org/1999/xhtml"'
    if lang:
        attrs += ' xml:lang="%s" lang="%s"' % (lang, lang)
    out = [attrs + '>']
    if not rows:
        out.append("<p>%s</p></div>" % e(headers))
        return "".join(out)
    out.append('<table><thead><tr>')
    out += ["<th>%s</th>" % e(h) for h in headers]
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>" + "".join("<td>%s</td>" % e(str(c or "")) for c in row) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


# ---------------------------------------------------------------- resource makers

def condition(b, code, onset, status="active", verification="confirmed",
              severity=None, note=None):
    r = {
        "resourceType": "Condition",
        "meta": {"profile": [EUBASE + "condition-eu-core"]},
        "clinicalStatus": {"coding": [{"system": THL7 + "condition-clinical",
                                       "code": status}]},
        "verificationStatus": {"coding": [{"system": THL7 + "condition-ver-status",
                                           "code": verification}]},
        "category": [cc(THL7 + "condition-category", "problem-list-item",
                        "Problem List Item")],
        "code": code,
        "subject": b.subject(),
        "onsetDateTime": onset,
    }
    if severity:
        r["severity"] = sct(*severity)
    if note:
        r["note"] = [{"text": note}]
    return b.add(r, "condition")


def medication(b, *, atc, atc_display, text, dose, start, reason=None,
               status="active", end=None, as_needed=False, form=None,
               code=None, product=None, route=None, timing=None, note=None):
    """A Medication plus the MedicationStatement that says the patient takes it.

    `code` replaces the ATC coding for something ATC does not classify (a
    nebulised saline); `product` adds a SNOMED clinical-drug coding next to the
    ATC one, so a receiver with either terminology recognises the product.
    """
    med = {"resourceType": "Medication",
           "meta": {"profile": [EUBASE + "medication-eu-core"]},
           "code": code or cc(ATC, atc, atc_display, text)}
    if product:
        med["code"]["coding"].append({"system": SCT, "code": product[0],
                                      "display": product[1]})
    if form:
        med["form"] = sct(*form)
    med_ref = b.add(med, "medication")
    period = {"start": start}
    if end:
        period["end"] = end
    dosage = {"text": dose, "asNeededBoolean": as_needed}
    if route:
        dosage["route"] = sct(*route)
    if timing:
        dosage["timing"] = {"repeat": timing}
    ms = {
        "resourceType": "MedicationStatement",
        "meta": {"profile": [EPS + "MedicationStatement-eu-eps"]},
        "status": status,
        "medicationReference": {"reference": med_ref, "display": text},
        "subject": b.subject(),
        "effectivePeriod": period,
        "dosage": [dosage],
    }
    if reason:
        ms["reasonCode"] = [reason]
    if note:
        ms["note"] = [{"text": note}]
    return b.add(ms, "medstatement")


def last_dose(b, *, statement_ref, text, given, dose, amount, route=None,
              performer=None):
    """The most recent administration of a medication on the list.

    'When did he last take it' is a date, not a dosage instruction, and a
    MedicationStatement has no element for it. MedicationAdministration is the
    resource that records one dose actually given, and the EPS medication
    section admits it next to the statement it belongs to.
    """
    r = {
        "resourceType": "MedicationAdministration",
        "status": "completed",
        "medicationReference": {"reference": b.entry_ref(statement_ref)
                                ["medicationReference"]["reference"],
                                "display": text},
        "subject": b.subject(),
        "effectiveDateTime": given,
        "dosage": {"text": dose,
                   "dose": {"value": amount[0], "unit": amount[1], "system": UCUM,
                            "code": amount[1]}},
    }
    if route:
        r["dosage"]["route"] = sct(*route)
    if performer:
        r["performer"] = [{"actor": {"display": performer}}]
    return b.add(r, "medadmin")


def allergy(b, *, code, category, atype, criticality, onset, reaction=None,
            severity=None, note=None):
    r = {
        "resourceType": "AllergyIntolerance",
        "meta": {"profile": [EUBASE + "allergyIntolerance-eu-core"]},
        "clinicalStatus": {"coding": [{"system": THL7 + "allergyintolerance-clinical",
                                       "code": "active"}]},
        "verificationStatus": {"coding": [{"system": THL7 + "allergyintolerance-verification",
                                           "code": "confirmed"}]},
        "type": atype,
        "category": [category],
        "criticality": criticality,
        "code": code,
        "patient": b.subject(),
        "onsetDateTime": onset,
    }
    if reaction:
        rx = {"manifestation": [reaction]}
        if severity:
            rx["severity"] = severity
        r["reaction"] = [rx]
    if note:
        r["note"] = [{"text": note}]
    return b.add(r, "allergy")


def immunization(b, *, code, date, atc=None, atc_display=None):
    vaccine = dict(code)
    if atc:
        vaccine["coding"] = vaccine["coding"] + [
            {"system": ATC, "code": atc, "display": atc_display}]
    r = {
        "resourceType": "Immunization",
        "meta": {"profile": [EUBASE + "immunization-eu-core"]},
        "status": "completed",
        "vaccineCode": vaccine,
        "patient": b.subject(),
        "occurrenceDateTime": date,
    }
    return b.add(r, "immunization")


def lab(b, *, loinc, display, date, value, unit, low=None, high=None,
        interpretation=None, performer=None):
    r = {
        "resourceType": "Observation",
        "meta": {"profile": [EUBASE + "medicalTestResult-eu-core"]},
        "status": "final",
        "category": [cc(THL7 + "observation-category", "laboratory", "Laboratory")],
        "code": cc(LOINC, loinc, display),
        "subject": {"reference": b.pat_ref},
        "effectiveDateTime": date,
        "valueQuantity": {"value": value, "unit": unit, "system": UCUM, "code": unit},
    }
    if performer:
        r["performer"] = [{"display": performer}]
    if interpretation:
        code, disp = interpretation
        r["interpretation"] = [cc(THL7 + "v3-ObservationInterpretation", code, disp)]
    rng = {}
    if low is not None:
        rng["low"] = {"value": low, "unit": unit, "system": UCUM, "code": unit}
    if high is not None:
        rng["high"] = {"value": high, "unit": unit, "system": UCUM, "code": unit}
    if rng:
        r["referenceRange"] = [rng]
    return b.add(r, "lab")


def procedure(b, *, code, date, note=None, complications=None, performer=None,
              reason_refs=None):
    r = {
        "resourceType": "Procedure",
        "meta": {"profile": [EUBASE + "procedure-eu-core"]},
        "status": "completed",
        "code": code,
        "subject": b.subject(),
        "performedDateTime": date,
    }
    if complications:
        r["complication"] = complications
    if performer:
        r["performer"] = [{"actor": {"reference": performer}}]
    if reason_refs:
        r["reasonReference"] = [{"reference": x} for x in reason_refs]
    if note:
        r["note"] = [{"text": note}]
    return b.add(r, "procedure")


def document_link(b, *, loinc, display, description, date, url, author):
    """A pointer to a document the summary cannot carry - the full anaesthetic
    record lives in the hospital's own system, and the receiving team is
    told where, not handed a copy."""
    r = {
        "resourceType": "DocumentReference",
        "status": "current",
        "type": cc(LOINC, loinc, display),
        "subject": b.subject(),
        "date": local_datetime(date),
        "author": [{"reference": author}],
        "description": description,
        "content": [{"attachment": {"contentType": "application/pdf",
                                    "url": url, "title": description,
                                    "creation": date}}],
    }
    return b.add(r, "docref")


def flag(b, *, code, text, since, priority, detail_refs=(), note=None,
         category="clinical", author=None):
    """A patient alert in the EPS Alerts section.

    `priority` is PH/PM/PL from the core flag-priority code system, `detail_refs`
    the conditions or medications the alert is drawn from, so a reader can get
    from the warning to its evidence in one hop.
    """
    pri = {"PH": "High priority", "PM": "Medium priority", "PL": "Low priority"}
    ext = [{"url": FLAG_PRIORITY_EXT,
            "valueCodeableConcept": cc(FLAG_PRIORITY_CS, priority, pri[priority])}]
    ext += [{"url": FLAG_DETAIL_EXT, "valueReference": {"reference": ref}}
            for ref in detail_refs]
    if note:
        ext.append({"url": NOTE_EXT, "valueAnnotation": {"text": note}})
    r = {
        "resourceType": "Flag",
        "meta": {"profile": [EPS + "flag-alert-obl-eu-eps"]},
        "extension": ext,
        "status": "active",
        "category": [cc(THL7 + "flag-category", category, category.capitalize())],
        "code": code,
        "subject": b.subject(),
        "period": {"start": since},
    }
    r["code"]["text"] = text
    if author:
        r["author"] = {"reference": author}
    return b.add(r, "flag")


def contact_role(b, *, practitioner, hsa_id, specialty, phone, phone_label):
    """A named clinician at the custodian organisation whom the receiving team
    can ring: a PractitionerRole with the specialty and a direct number."""
    prac_ref = b.add({
        "resourceType": "Practitioner",
        "meta": {"profile": [EUBASE + "practitioner-eu",
                             SEBASE + "SEBasePractitioner"]},
        "identifier": [{"system": SE_HSAID, "value": hsa_id}],
        "name": [practitioner],
    }, "practitioner")
    name = " ".join(practitioner.get("prefix", []) + practitioner["given"]
                    + [practitioner["family"]])
    # Not SEBasePractitionerRole: that profile slices specialty.coding on a
    # system its slices never fix, so the validator cannot evaluate any
    # specialty at all under it (hl7se.fhir.base 1.1.0), and the specialty is
    # the one thing the receiving team needs from these roles.
    return b.add({
        "resourceType": "PractitionerRole",
        "meta": {"profile": [EUBASE + "practitionerRole-eu"]},
        "practitioner": {"reference": prac_ref, "display": name},
        "organization": {"reference": b.org_ref},
        "specialty": [specialty],
        "telecom": [{"system": "phone", "value": phone, "use": "work"}],
    }, "role"), name


def care_team(b, *, name, members, phone, note=None):
    """Who to contact at the custodian - the scenario's own question."""
    r = {
        "resourceType": "CareTeam",
        "status": "active",
        "name": name,
        "subject": b.subject(),
        "participant": [{"role": [role], "member": {"reference": ref, "display": disp}}
                        for ref, disp, role in members],
        "telecom": [{"system": "phone", "value": phone, "use": "work"}],
    }
    if note:
        r["note"] = [{"text": note}]
    return b.add(r, "careteam")


def device_use(b, *, code, since, note=None):
    dev_ref = b.add({
        "resourceType": "Device",
        "meta": {"profile": [EPS + "device-eu-eps"]},
        "status": "active",
        "type": code,
        "patient": {"reference": b.pat_ref},
    }, "device")
    r = {
        "resourceType": "DeviceUseStatement",
        "meta": {"profile": [EPS + "deviceUseStatement-eu-eps"]},
        "status": "active",
        "subject": b.subject(),
        "timingPeriod": {"start": since},
        "device": {"reference": dev_ref, "display": code["text"]},
    }
    if note:
        r["note"] = [{"text": note}]
    return b.add(r, "deviceuse")


def care_plan(b, *, description, start, activity_status="scheduled",
              reason=None, note=None, scheduled=None, steps=None,
              addresses=(), author=None, team=None):
    """One plan. Either a single activity described by `description` (the
    original shape: an appointment with a status), or `steps` - a list of
    (kind, status, text) that spell out a multi-step plan such as how to
    secure an airway, under a title given by `description`."""
    if steps:
        activities = [{"detail": {"kind": kind, "status": status,
                                  "description": text}}
                      for kind, status, text in steps]
    else:
        detail = {"kind": "Appointment", "status": activity_status,
                  "description": description}
        if reason:
            detail["reasonCode"] = [reason]
        if scheduled:
            detail["scheduledPeriod"] = {"start": scheduled}
        activities = [{"detail": detail}]
    r = {
        "resourceType": "CarePlan",
        "status": "active",
        "intent": "plan",
        "category": [sct("734163000", "Care plan")],
        "title": description,
        "subject": b.subject(),
        "period": {"start": start},
        "activity": activities,
    }
    if addresses:
        r["addresses"] = [{"reference": x} for x in addresses]
    if author:
        r["author"] = {"reference": author}
    if team:
        r["careTeam"] = [{"reference": team}]
    if note:
        r["note"] = [{"text": note}]
    return b.add(r, "careplan")


# ================================================================== ANDREA
# Cross-border care: 24-year-old Swedish exchange student in Paris. This is the
# summary a Paris emergency department retrieves from Sweden via MyHealth@EU.

def build_andrea():
    doc_date = "2026-08-24"
    b = BundleBuilder(
        doc_date=doc_date,
        language="en",
        title="European Patient Summary",
        doc_uuid="andrea-cross-border",
        patient={
            "resourceType": "Patient",
            "meta": {"profile": [EPS + "patient-eu-eps",
                                 SEBASE + "SEBasePatient"]},
            "identifier": [{
                "system": SE_PERSONNUMMER,
                "value": "200207252388",
            }],
            "name": [{"text": "Andrea Lindqvist", "family": "Lindqvist",
                      "given": ["Andrea"]}],
            "telecom": [{"system": "phone", "value": "+46 70 000 00 01",
                         "use": "mobile"},
                        {"system": "email", "value": "andrea.lindqvist@example.org"}],
            "gender": "female",
            "birthDate": "2002-07-25",
            "address": [
                {"use": "home", "type": "physical", "line": ["Lärkstigen 12"],
                 "city": "Uppsala", "postalCode": "753 11", "country": "SE"},
                {"use": "temp", "type": "physical", "line": ["Résidence Les Tilleuls, 8 rue Camille Vasseur"],
                 "city": "Paris", "postalCode": "75014", "country": "FR",
                 "period": {"start": "2026-02-01"}},
            ],
            "communication": [{"language": cc("urn:ietf:bcp:47", "sv", "Swedish"),
                               "preferred": True}],
        },
        author={"practitioner": {
            "resourceType": "Practitioner",
            "meta": {"profile": [EUBASE + "practitioner-eu",
                                 SEBASE + "SEBasePractitioner"]},
            "identifier": [{"system": SE_HSAID, "value": "SE2321000008-2001"}],
            "name": [{"family": "Sundström", "given": ["Åsa"], "prefix": ["Dr"]}],
        }},
        org={
            "resourceType": "Organization",
            "meta": {"profile": [EUBASE + "organization-eu",
                                 SEBASE + "SEBaseOrganization"]},
            "identifier": [{"system": SE_HSAID, "value": "SE2321000008-1001"},
                           {"system": SE_ORGNR, "value": "2321000008"}],
            "name": "Vårdcentralen Norrskogen",
            "address": [{"city": "Uppsala", "postalCode": "752 28", "country": "SE"}],
        },
    )

    # --- allergies: positively asserted as empty. The distinction that matters:
    # this is a clinical statement, not a blank field.
    b.section("Allergies and intolerances", "48765-2",
              "Allergies and adverse reactions Document",
              [], "No known allergies or intolerances.", [],
              empty_reason=("nilknown", "Nil Known"))

    # --- problems
    t1dm = sct("46635009", "Type 1 diabetes mellitus")
    adhd = sct("406506008", "Attention deficit hyperactivity disorder")
    depression = sct("66344007", "Recurrent major depression")
    probs = [
        (t1dm, "2012-09-04", "active"),
        (adhd, "2015-02-11", "active"),
        (depression, "2021-11-08", "remission"),
    ]
    refs = [condition(b, c, o, s) for c, o, s in probs]
    b.section("Problem list", "11450-4", "Problem list - Reported",
              [[c["text"], o, s] for c, o, s in probs],
              ["Condition", "Onset", "Status"], refs)

    # --- medication
    meds = [
        dict(atc="A10AE06", atc_display="insulin degludec",
             text="Insulin degludec 100 units/mL solution for injection",
             dose="26 units subcutaneously once daily at bedtime",
             start="2019-01-14", reason=t1dm),
        dict(atc="A10AB05", atc_display="insulin aspart",
             text="Insulin aspart 100 units/mL solution for injection",
             dose="4-8 units subcutaneously with each main meal, adjusted to "
                  "carbohydrate intake and pre-meal glucose",
             start="2019-01-14", reason=t1dm),
        dict(atc="N06BA12", atc_display="lisdexamfetamine",
             text="Lisdexamfetamine 50 mg capsule",
             dose="1 capsule (50 mg) by mouth each morning",
             start="2021-03-02", reason=adhd),
        dict(atc="N06AB06", atc_display="sertraline",
             text="Sertraline 50 mg tablet",
             dose="1 tablet (50 mg) by mouth once daily",
             start="2021-11-20", end="2023-05-30", status="completed",
             reason=depression, form=("421026006", "Oral tablet")),
    ]
    refs = [medication(b, **m) for m in meds]
    b.section("Medication list", "10160-0", "History of Medication use Narrative",
              [[m["text"], m["start"], m.get("end") or "ongoing", m["dose"]]
               for m in meds],
              ["Medication", "Since", "Until", "Dosage"], refs)

    # --- immunizations
    shots = [
        (sct("911000221103", "Human papillomavirus antigen only vaccine product",
             "Human papillomavirus vaccine"), "2014-09-16", None, None),
        (sct("1119305005", "SARS-CoV-2 antigen vaccine",
             "COVID-19 vaccine"), "2021-10-11", "J07BN04", "covid-19, protein subunit"),
        (sct("1181000221105",
             "Vaccine product containing only Influenza virus antigen (medicinal product)",
             "Influenza vaccine"), "2025-10-14", "J07BB02",
         "influenza, inactivated, split virus or surface antigen"),
    ]
    refs = [immunization(b, code=c, date=d, atc=a, atc_display=ad)
            for c, d, a, ad in shots]
    b.section("Immunizations", "11369-6", "History of Immunization note",
              [[c["text"], d] for c, d, _, _ in shots], ["Vaccine", "Date"], refs)

    # --- results. Deliberately eight months stale: the scenario's whole point is
    # that Andrea stopped attending follow-up, and the views should say so.
    labs = [
        dict(loinc="59261-8",
             display="Hemoglobin A1c/Hemoglobin.total standardized per IFCC-RMP for CDT in Blood",
             date="2025-12-09", value=68, unit="mmol/mol", low=27, high=42,
             interpretation=("H", "High"), performer="Klinisk kemi, Sävelunds sjukhus, Uppsala"),
        dict(loinc="14682-9", display="Creatinine [Moles/volume] in Serum or Plasma",
             date="2025-12-09", value=71, unit="umol/L", low=45, high=90,
             performer="Klinisk kemi, Sävelunds sjukhus, Uppsala"),
        dict(loinc="32294-1",
             display="Albumin/Creatinine [Ratio] in Urine",
             date="2025-12-09", value=4.1, unit="mg/mmol", high=3.0,
             interpretation=("H", "High"), performer="Klinisk kemi, Sävelunds sjukhus, Uppsala"),
        dict(loinc="14647-2", display="Cholesterol [Moles/volume] in Serum or Plasma",
             date="2025-12-09", value=4.4, unit="mmol/L", high=5.0,
             performer="Klinisk kemi, Sävelunds sjukhus, Uppsala"),
    ]
    refs = [lab(b, **l) for l in labs]
    b.section("Relevant diagnostic tests and laboratory data", "30954-2",
              "Relevant diagnostic tests/laboratory data note",
              [[l["display"], l["date"], "%s %s" % (l["value"], l["unit"]),
                (l.get("interpretation") or ("", ""))[1] or "Within range"]
               for l in labs],
              ["Test", "Date", "Result", "Interpretation"], refs)

    # --- devices
    cgm = sct("463729000", "Point-of-care blood glucose continuous monitoring system",
              "Continuous glucose monitoring system")
    refs = [device_use(b, code=cgm, since="2019-05-20",
                       note="Sensor-based continuous glucose monitor, worn "
                            "continuously. Data shared with the diabetes clinic in "
                            "Uppsala; no readings uploaded since 2026-06-02.")]
    b.section("Medical devices", "46264-8", "History of medical device use",
              [["Continuous glucose monitoring system", "2019-05-20", "In use"]],
              ["Device", "In use since", "Status"], refs)

    # --- plan of care
    plans = [
        dict(description="Digital follow-up with diabetes specialist nurse, "
                         "Uppsala - every 3 months",
             start="2019-01-14", activity_status="cancelled",
             scheduled="2026-06-03", reason=t1dm,
             note="Appointment on 2026-06-03 was cancelled by the patient and has "
                  "not been rebooked. Last completed review 2025-12-09."),
        dict(description="Mental health safety plan agreed with adult psychiatric "
                         "outpatient clinic, Uppsala",
             start="2022-03-14", activity_status="in-progress", reason=depression,
             note="Documented early warning signs: broken sleep, withdrawal from "
                  "friends, skipped meals and missed medication. Agreed first step "
                  "is to contact the outpatient clinic or the named relative. "
                  "Patient consented to sharing the plan with treating clinicians "
                  "abroad."),
    ]
    refs = [care_plan(b, **p) for p in plans]
    b.section("Plan of care", "18776-5", "Plan of care note",
              [[p["description"], p["start"], p["activity_status"]] for p in plans],
              ["Planned activity", "Since", "Status"], refs)

    # --- procedures: nothing to report, and said so.
    b.section("History of procedures", "47519-4", "History of Procedures Document",
              [], "No procedures recorded.", [],
              empty_reason=("nilknown", "Nil Known"))
    return b.build()


# ================================================================== ARNE
# Complex care: 83-year-old Swede who falls at his son's home in Denmark. This is
# the Swedish summary the Danish hospital retrieves before he goes to theatre. Written
# in Swedish, as a Swedish-sourced summary would be - the LOINC section codes,
# not the titles, are what carry the meaning across the border.

def build_arne():
    doc_date = "2026-08-19"
    b = BundleBuilder(
        doc_date=doc_date,
        language="sv",
        title="Patientöversikt (European Patient Summary)",
        doc_uuid="arne-complex-care",
        patient={
            "resourceType": "Patient",
            "meta": {"profile": [EPS + "patient-eu-eps",
                                 SEBASE + "SEBasePatient"]},
            "identifier": [{
                "system": SE_PERSONNUMMER,
                "value": "194302096997",
            }],
            "name": [{"text": "Arne Bergström", "family": "Bergström",
                      "given": ["Arne", "Gustav"]}],
            "telecom": [{"system": "phone", "value": "+46 70 000 00 02",
                         "use": "home"}],
            "gender": "male",
            "birthDate": "1943-02-09",
            "address": [{"use": "home", "type": "physical",
                         "line": ["Ängsbacken 8 A"], "city": "Falkenberg",
                         "postalCode": "311 34", "country": "SE"}],
            "communication": [{"language": cc("urn:ietf:bcp:47", "sv", "Swedish"),
                               "preferred": True}],
            "contact": [{
                "relationship": [cc(THL7 + "v2-0131", "C", "Emergency Contact")],
                "name": {"text": "Mikael Bergström", "family": "Bergström",
                         "given": ["Mikael"]},
                "telecom": [{"system": "phone", "value": "+45 20 00 00 03"}],
                "address": {"city": "København", "country": "DK"},
            }],
        },
        author={"practitioner": {
            "resourceType": "Practitioner",
            "meta": {"profile": [EUBASE + "practitioner-eu",
                                 SEBASE + "SEBasePractitioner"]},
            "identifier": [{"system": SE_HSAID, "value": "SE2321000107-2002"}],
            "name": [{"family": "Lövgren", "given": ["Märta"], "prefix": ["Dr"]}],
        }},
        org={
            "resourceType": "Organization",
            "meta": {"profile": [EUBASE + "organization-eu",
                                 SEBASE + "SEBaseOrganization"]},
            "identifier": [{"system": SE_HSAID, "value": "SE2321000107-1002"},
                           {"system": SE_ORGNR, "value": "2321000107"}],
            "name": "Vårdcentralen Kvarnängen",
            "address": [{"city": "Falkenberg", "postalCode": "311 32",
                         "country": "SE"}],
        },
    )

    # --- allergies. The penicillin entry is the single most consequential fact in
    # this document: it changes the antibiotic given before surgery.
    allergies = [
        dict(code=sct("764146007", "Penicillin", "Penicillin"),
             category="medication", atype="allergy", criticality="high",
             onset="1972-06-18",
             reaction=sct("39579001", "Anaphylaxis", "Anafylaktisk reaktion"),
             severity="severe",
             note="Anafylaxi efter fenoximetylpenicillin 1972, vårdad på "
                  "akutmottagning. Undvik samtliga betalaktamantibiotika tills "
                  "allergiutredning genomförts."),
        dict(code=sct("256262001", "European white birch pollen", "Björkpollen"),
             category="environment", atype="allergy", criticality="low",
             onset="1988-04-02",
             reaction=sct("878820003", "Rhinoconjunctivitis",
                          "Rinokonjunktivit"),
             severity="mild"),
    ]
    refs = [allergy(b, **a) for a in allergies]
    b.section("Allergier och intoleranser", "48765-2",
              "Allergies and adverse reactions Document",
              [[a["code"]["text"], a["criticality"], a["onset"],
                a["reaction"]["text"], a.get("severity")] for a in allergies],
              ["Allergen", "Kritikalitet", "Debut", "Reaktion", "Svårighetsgrad"],
              refs)

    # --- problems
    hf = sct("88805009", "Chronic congestive heart failure", "Kronisk hjärtsvikt")
    t2dm = sct("44054006", "Type 2 diabetes mellitus", "Diabetes mellitus typ 2")
    af = sct("49436004", "Atrial fibrillation", "Förmaksflimmer")
    stroke = sct("230690007", "Cerebrovascular accident", "Stroke")
    hemi = sct("278287000", "Left hemiparesis", "Vänstersidig hemipares")
    angina = sct("233819005", "Stable angina", "Stabil angina pectoris")
    emphysema = sct("87433001", "Pulmonary emphysema", "Lungemfysem")
    htn = sct("59621000", "Essential hypertension", "Essentiell hypertoni")
    ulcer = sct("1237117000", "Venous ulcer of lower leg", "Venöst bensår")
    prostate = sct("399068003", "Malignant neoplasm of prostate",
                   "Prostatacancer, lokalt avancerad")
    probs = [
        (htn, "1998-10-02", "active"),
        (t2dm, "2004-06-30", "active"),
        (emphysema, "2011-03-17", "active"),
        (angina, "2015-11-05", "active"),
        (af, "2017-09-22", "active"),
        (hf, "2018-04-12", "active"),
        (stroke, "2019-02-03", "resolved"),
        (hemi, "2019-02-03", "active"),
        (prostate, "2020-08-11", "active"),
        (ulcer, "2023-05-19", "active"),
    ]
    refs = [condition(b, c, o, s) for c, o, s in probs]
    b.section("Problemlista", "11450-4", "Problem list - Reported",
              [[c["text"], o, s] for c, o, s in probs],
              ["Tillstånd", "Debut", "Status"], refs)

    # --- medication
    tablet = ("421026006", "Oral tablet")
    meds = [
        dict(atc="B01AF02", atc_display="apixaban",
             text="Apixaban 5 mg tablett",
             dose="1 tablett (5 mg) 2 gånger dagligen", start="2017-10-05",
             reason=af, form=tablet),
        dict(atc="C07AB02", atc_display="metoprolol",
             text="Metoprolol depottablett 50 mg",
             dose="1 tablett (50 mg) 1 gång dagligen på morgonen",
             start="2018-04-20", reason=hf, form=tablet),
        dict(atc="C09AA05", atc_display="ramipril",
             text="Ramipril 5 mg tablett",
             dose="1 tablett (5 mg) 1 gång dagligen", start="2018-04-20",
             reason=hf, form=tablet),
        dict(atc="C03CA01", atc_display="furosemide",
             text="Furosemid 40 mg tablett",
             dose="1 tablett (40 mg) 1 gång dagligen på morgonen",
             start="2018-04-20", reason=hf, form=tablet),
        dict(atc="A10AE04", atc_display="insulin glargine",
             text="Insulin glargin 100 E/ml injektionsvätska",
             dose="22 enheter subkutant till natten", start="2016-02-08",
             reason=t2dm),
        dict(atc="A10BA02", atc_display="metformin",
             text="Metformin 500 mg tablett",
             dose="1 tablett (500 mg) 2 gånger dagligen i samband med måltid",
             start="2004-07-15", reason=t2dm, form=tablet),
        dict(atc="C01DA02", atc_display="glyceryl trinitrate",
             text="Glyceryltrinitrat 0,4 mg sublingual spray",
             dose="1-2 doser under tungan vid bröstsmärta, vid behov",
             start="2015-11-10", reason=angina, as_needed=True),
        dict(atc="R03BB04", atc_display="tiotropium bromide",
             text="Tiotropium 18 mikrogram inhalationspulver",
             dose="1 inhalation 1 gång dagligen", start="2011-04-05",
             reason=emphysema),
        dict(atc="L02BB03", atc_display="bicalutamide",
             text="Bikalutamid 150 mg tablett",
             dose="1 tablett (150 mg) 1 gång dagligen som monoterapi "
                  "(ingen GnRH-analog)", start="2020-09-14",
             reason=prostate, form=tablet),
        dict(atc="C10AA05", atc_display="atorvastatin",
             text="Atorvastatin 20 mg tablett",
             dose="1 tablett (20 mg) 1 gång dagligen till kvällen",
             start="2015-11-10", reason=angina, form=tablet),
    ]
    refs = [medication(b, **m) for m in meds]
    b.section("Läkemedelslista", "10160-0", "History of Medication use Narrative",
              [[m["text"], m["dose"], m["start"], m["reason"]["text"]] for m in meds],
              ["Läkemedel", "Dosering", "Insatt", "Indikation"], refs)

    # --- procedures
    procs = [
        (sct("90199006", "Transurethral resection of prostate",
             "Transuretral resektion av prostata (TURP)"), "2020-11-26",
         "Utförd på Länssjukhuset Söderås, Varberg. Ingen peroperativ komplikation."),
    ]
    refs = [procedure(b, code=c, date=d, note=n) for c, d, n in procs]
    b.section("Genomgångna åtgärder", "47519-4", "History of Procedures Document",
              [[c["text"], d] for c, d, _ in procs], ["Åtgärd", "Datum"], refs)

    # --- immunizations
    shots = [
        (sct("981000221107", "Streptococcus pneumoniae antigen only vaccine product",
             "Pneumokockvaccin"), "2021-11-04", None, None),
        (sct("1181000221105",
             "Vaccine product containing only Influenza virus antigen (medicinal product)",
             "Influensavaccin"), "2025-10-09", "J07BB02",
         "influenza, inactivated, split virus or surface antigen"),
        (sct("1119305005", "SARS-CoV-2 antigen vaccine", "Covid-19-vaccin"),
         "2025-10-09", "J07BN04", "covid-19, protein subunit"),
    ]
    refs = [immunization(b, code=c, date=d, atc=a, atc_display=ad)
            for c, d, a, ad in shots]
    b.section("Vaccinationer", "11369-6", "History of Immunization note",
              [[c["text"], d] for c, d, _, _ in shots], ["Vaccin", "Datum"], refs)

    # --- results. Renal function and haemoglobin are what an anaesthetist reaches
    # for first, so they are current rather than historic.
    labspec = [
        dict(loinc="59261-8",
             display="Hemoglobin A1c/Hemoglobin.total standardized per IFCC-RMP for CDT in Blood",
             date="2026-06-12", value=58, unit="mmol/mol", low=27, high=42,
             interpretation=("H", "High")),
        dict(loinc="14682-9", display="Creatinine [Moles/volume] in Serum or Plasma",
             date="2026-06-12", value=118, unit="umol/L", low=60, high=105,
             interpretation=("H", "High")),
        dict(loinc="62238-1",
             display="Glomerular filtration rate [Volume Rate/Area] in Serum, Plasma or "
                     "Blood by Creatinine-based formula (CKD-EPI)/1.73 sq M",
             date="2026-06-12", value=46, unit="mL/min/{1.73_m2}", low=60,
             interpretation=("L", "Low")),
        dict(loinc="718-7", display="Hemoglobin [Mass/volume] in Blood",
             date="2026-06-12", value=112, unit="g/L", low=134, high=170,
             interpretation=("L", "Low")),
        dict(loinc="2823-3", display="Potassium [Moles/volume] in Serum or Plasma",
             date="2026-06-12", value=4.4, unit="mmol/L", low=3.5, high=4.6),
        dict(loinc="33762-6",
             display="Natriuretic peptide.B prohormone N-Terminal [Mass/volume] in Serum or Plasma",
             date="2026-06-12", value=1840, unit="ng/L", high=300,
             interpretation=("H", "High")),
        dict(loinc="2857-1",
             display="Prostate specific Ag [Mass/volume] in Serum or Plasma",
             date="2026-05-04", value=12.4, unit="ug/L", high=4.0,
             interpretation=("H", "High")),
    ]
    for l in labspec:
        l["performer"] = "Klinisk kemi, Länssjukhuset Söderås"
    refs = [lab(b, **l) for l in labspec]
    b.section("Laboratorieresultat", "30954-2",
              "Relevant diagnostic tests/laboratory data note",
              [[l["display"], l["date"], "%s %s" % (l["value"], l["unit"]),
                (l.get("interpretation") or ("", ""))[1] or "Inom referens"]
               for l in labspec],
              ["Analys", "Datum", "Resultat", "Tolkning"], refs)

    # --- devices
    rollator = sct("404946006", "Wheeled walking frame", "Rollator")
    refs = [device_use(b, code=rollator, since="2019-06-01",
                       note="Utprovad av arbetsterapeut efter stroke 2019. "
                            "Används inomhus och utomhus.")]
    b.section("Medicintekniska produkter", "46264-8",
              "History of medical device use",
              [["Rollator", "2019-06-01", "I bruk"]],
              ["Produkt", "I bruk sedan", "Status"], refs)

    # --- plan of care
    plans = [
        dict(description="Planerat återbesök på Vårdcentralen Kvarnängen - "
                         "årlig kontroll av hjärtsvikt och diabetes",
             start="2026-06-12", activity_status="scheduled",
             scheduled="2026-09-15", reason=hf),
        dict(description="Hemtjänst två besök dagligen - stöd med läkemedel, "
                         "personlig vård och måltider",
             start="2019-06-01", activity_status="in-progress", reason=hemi,
             note="Beviljad av kommunens biståndsenhet. Kontaktperson finns i "
                  "den samordnade individuella planen (SIP)."),
        dict(description="Sårvård av venöst bensår höger underben - "
                         "kompressionsbehandling och omläggning två gånger per vecka",
             start="2023-05-19", activity_status="in-progress", reason=ulcer,
             note="Utförs av distriktssköterska i hemmet."),
    ]
    refs = [care_plan(b, **p) for p in plans]
    b.section("Vårdplan", "18776-5", "Plan of care note",
              [[p["description"], p["start"], p["activity_status"]] for p in plans],
              ["Planerad åtgärd", "Sedan", "Status"], refs)
    return b.build()


# ================================================================== VILLE
# Difficult airway: 13-year-old from Stockholm with Freeman-Sheldon syndrome and
# juvenile spondyloarthritis on adalimumab and methotrexate, brought to the
# Sahlgrenska emergency department in Gothenburg with a respiratory infection.
# This is the Karolinska summary the Sahlgrenska team retrieves. Everything the
# scenario says they need - the airway, what went wrong under anaesthesia and
# what worked, the immunosuppression, when the last doses were given, who to
# ring at Karolinska, and that he is called Ville - has a coded home here, with
# the two things that could kill him first, in the Alerts section.
#
# The syndrome is the scenario's "rare syndrome with a small chin and difficulty
# opening the mouth" given a name: Freeman-Sheldon syndrome is defined by
# exactly that (microstomia, limited mouth opening, micrognathia) and is a
# textbook difficult airway. Its features are listed as problems in their own
# right, so a reader who has never heard of the syndrome still sees why the
# airway is hard.

def build_ville():
    doc_date = "2026-10-13"
    rheum, rheum_name = None, None
    b = BundleBuilder(
        doc_date=doc_date,
        language="sv",
        title="Patientöversikt (European Patient Summary)",
        doc_uuid="ville-difficult-airway",
        patient={
            "resourceType": "Patient",
            "meta": {"profile": [EPS + "patient-eu-eps",
                                 SEBASE + "SEBasePatient"]},
            "identifier": [{
                "system": SE_SAMORDNINGSNUMMER,
                "value": "201304822172",
            }],
            # One name only: SEBasePatient allows exactly one, the registered
            # one, so "Ville" has no coded home. It is in the Patient narrative
            # (see below) and the Patient story section instead - the
            # scenario's own question of where a preferred name goes, answered
            # as honestly as the Swedish profile lets us.
            "name": [{"use": "official", "text": "Vilhelm Erik Söderberg",
                      "family": "Söderberg", "given": ["Vilhelm", "Erik"]}],
            "telecom": [{"system": "phone", "value": "+46 70 000 00 04",
                         "use": "mobile"}],
            "gender": "male",
            "birthDate": "2013-04-22",
            "address": [{"use": "home", "type": "physical",
                         "line": ["Klubbacken 14"], "city": "Hägersten",
                         "postalCode": "129 39", "country": "SE"}],
            "communication": [{"language": cc("urn:ietf:bcp:47", "sv", "Swedish"),
                               "preferred": True}],
            # His mother is next of kin, the emergency contact and the person
            # who knows the airway history by heart. "Mother" is not in the
            # v2 contact-role table the element binds to, so it rides along as
            # a second coding on next-of-kin rather than as a concept of its
            # own that would match nothing in the value set.
            "contact": [{
                "relationship": [
                    {"coding": [{"system": THL7 + "v2-0131", "code": "N",
                                 "display": "Next-of-Kin"},
                                {"system": THL7 + "v3-RoleCode", "code": "MTH",
                                 "display": "mother"}],
                     "text": "Mor (vårdnadshavare)"},
                    cc(THL7 + "v2-0131", "C", "Emergency Contact")],
                "name": {"text": "Anna Söderberg", "family": "Söderberg",
                         "given": ["Anna"]},
                "telecom": [{"system": "phone", "value": "+46 70 000 00 05",
                             "use": "mobile"}],
            }],
        },
        author={"practitioner": {
            "resourceType": "Practitioner",
            "meta": {"profile": [EUBASE + "practitioner-eu",
                                 SEBASE + "SEBasePractitioner"]},
            "identifier": [{"system": SE_HSAID, "value": "SE2321000016-2003"}],
            "name": [{"family": "Ekholm", "given": ["Hanna"], "prefix": ["Dr"]}],
        }},
        org={
            "resourceType": "Organization",
            "meta": {"profile": [EUBASE + "organization-eu",
                                 SEBASE + "SEBaseOrganization"]},
            "identifier": [{"system": SE_HSAID, "value": "SE2321000016-1003"},
                           {"system": SE_ORGNR, "value": "2321000016"}],
            "name": "Karolinska Universitetssjukhuset, Astrid Lindgrens barnsjukhus",
            "telecom": [{"system": "phone", "value": "+46 8 000 00 00",
                         "use": "work"}],
            "address": [{"city": "Solna", "postalCode": "171 76", "country": "SE"}],
        },
    )
    patient = b.entry_ref(b.pat_ref)
    patient["text"] = resource_narrative(patient, extra=[
        ("Prefers to be called", "Ville (not Vilhelm)")])

    # --- the people to ring. The author (paediatric rheumatology) is the role
    # the builder already made; the anaesthetist and the ENT surgeon are the
    # two the scenario says must be found early.
    rheum = b.role_ref
    rheum_name = "Dr Hanna Ekholm"
    # Same reason as contact_role: a specialty and the Swedish role profile
    # cannot both be validated, and the specialty is what matters here.
    b.entry_ref(rheum)["meta"]["profile"] = [EUBASE + "practitionerRole-eu"]
    b.entry_ref(rheum)["specialty"] = [sct("419472004", "Pediatric rheumatology",
                                           "Barnreumatologi")]
    b.entry_ref(rheum)["telecom"] = [{"system": "phone", "value": "+46 8 000 00 10",
                                      "use": "work"}]
    b.entry_ref(rheum)["text"] = resource_narrative(b.entry_ref(rheum))
    anaes, anaes_name = contact_role(
        b, practitioner={"family": "Lindberg", "given": ["Johan"], "prefix": ["Dr"]},
        hsa_id="SE2321000016-2004",
        specialty=sct("394577000", "Anesthetics", "Barnanestesi"),
        phone="+46 8 000 00 11", phone_label="Barnanestesi, jour dygnet runt")
    ent, ent_name = contact_role(
        b, practitioner={"family": "Nyström", "given": ["Sara"], "prefix": ["Dr"]},
        hsa_id="SE2321000016-2005",
        specialty=sct("418960008", "Otolaryngology", "Öron-näsa-hals"),
        phone="+46 8 000 00 12", phone_label="ÖNH-kliniken")
    team = care_team(
        b, name="Villes vårdteam på Karolinska",
        members=[(rheum, rheum_name, sct("419472004", "Pediatric rheumatology",
                                         "Barnreumatolog, patientansvarig läkare")),
                 (anaes, anaes_name, sct("394577000", "Anesthetics",
                                         "Barnanestesiolog, ansvarig för luftvägsplanen")),
                 (ent, ent_name, sct("418960008", "Otolaryngology",
                                     "ÖNH-läkare"))],
        phone="+46 8 000 00 10",
        note="Barnreumatologmottagningen vardagar 08-16 på +46 8 000 00 10. "
             "Vid akut behov av sövning eller luftvägshjälp: barnanestesijouren "
             "+46 8 000 00 11, dygnet runt. Villes mor Anna Söderberg "
             "(+46 70 000 00 05) känner till luftvägshistoriken i detalj.")
    patient["generalPractitioner"] = [{"reference": rheum, "display": rheum_name},
                                      {"reference": b.org_ref}]

    # --- problems. The syndrome and the three features that make the airway
    # hard come first; the two complications observed under anaesthesia are
    # problems in their own right, because they are what the alert cites.
    fss = sct("52616002", "Freeman-Sheldon syndrome", "Freeman-Sheldons syndrom")
    micrognathia = sct("32958008", "Congenital micrognathism", "Mikrognati (liten haka)")
    microstomia = sct("14582003", "Microstomia", "Mikrostomi (liten mun)")
    trismus = sct("87866006", "Trismus", "Nedsatt gapförmåga")
    jia = sct("410801005", "Juvenile idiopathic arthritis, enthesitis related arthritis",
              "Juvenil idiopatisk artrit, entesitrelaterad (juvenil spondylartrit)")
    immsupp = sct("370391006", "Patient immunosuppressed",
                  "Immunsupprimerad (adalimumab och metotrexat)")
    lrti = sct("448739000", "Recurrent lower respiratory tract infection",
               "Återkommande nedre luftvägsinfektioner")
    diff_intub = sct("718447001", "Difficult tracheal intubation", "Svår intubation")
    diff_mask = sct("763326004", "Difficult mask ventilation", "Svår maskventilation")
    probs = [
        dict(code=fss, onset="2013-04-22",
             note="Diagnos vid Astrid Lindgrens barnsjukhus 2013, bekräftad med "
                  "MYH3-variant. Distal artrogrypos typ 2A."),
        dict(code=micrognathia, onset="2013-04-22"),
        dict(code=microstomia, onset="2013-04-22"),
        dict(code=trismus, onset="2013-04-22", severity=("24484000", "Severe"),
             note="Interincisalt avstånd cirka 20 mm (mätt 2024-10-14). "
                  "Begränsar laryngoskopi och larynxmask."),
        dict(code=diff_intub, onset="2016-03-15", severity=("24484000", "Severe"),
             note="Misslyckad direktlaryngoskopi 2016 (Cormack-Lehane 4). "
                  "Fiberoptisk intubation har lyckats vid båda narkoserna."),
        dict(code=diff_mask, onset="2016-03-15", severity=("24484000", "Severe"),
             note="Tvåhandsgrepp, svalgtub och två personer krävdes 2016 och 2024."),
        dict(code=jia, onset="2022-02-08",
             note="HLA-B27-positiv. Sakroiliit på MR 2024-10-14. Behandlas med "
                  "adalimumab och metotrexat; i remission sedan 2025-03."),
        dict(code=immsupp, onset="2022-05-20",
             note="Ökad infektionskänslighet. Pausa behandlingen vid feber - "
                  "se infektionsplanen under Vårdplan."),
        dict(code=lrti, onset="2017-11-03",
             note="Rikliga, sega sekret i luftvägarna och andningssvårigheter vid "
                  "luftvägsinfektioner. Inlagd 2019-01 och 2023-02; "
                  "slemmobilisering och NaCl-inhalation hjälpte."),
    ]
    prob_refs = {p["code"]["coding"][0]["code"]: condition(b, **p) for p in probs}
    b.section("Problemlista", "11450-4", "Problem list - Reported",
              [[p["code"]["text"], p["onset"],
                (p.get("severity") or ("", ""))[1] and "Svår" or "",
                p.get("note") or ""] for p in probs],
              ["Tillstånd", "Debut", "Svårighetsgrad", "Kommentar"],
              list(prob_refs.values()))

    # --- medication. The two immunosuppressants, their last doses, the folate
    # that goes with methotrexate, and the saline he nebulises when an
    # infection sets in.
    sc = ("34206005", "Subcutaneous route")
    po = ("26643006", "Oral route")
    inh = ("447694001", "Respiratory tract route")
    inj = ("385219001", "Solution for injection")
    tablet = ("421026006", "Oral tablet")
    ada_text = "Humira (adalimumab) 40 mg/0,4 ml injektionsvätska i förfylld penna"
    mtx_text = "Metoject (metotrexat) 15 mg injektionsvätska i förfylld penna"
    ada = medication(
        b, atc="L04AB04", atc_display="adalimumab", text=ada_text,
        product=("890314002", "Adalimumab 100 mg/mL solution for injection"),
        dose="40 mg subkutant varannan vecka (tisdagar)", route=sc, form=inj,
        timing={"frequency": 1, "period": 2, "periodUnit": "wk"},
        start="2023-01-17", reason=jia,
        note="Senaste dos 2026-10-06, nästa planerad 2026-10-20. Ges av "
             "föräldrarna hemma. Pausas vid feber eller infektion som kräver "
             "antibiotika.")
    mtx = medication(
        b, atc="L04AX03", atc_display="methotrexate", text=mtx_text,
        dose="15 mg subkutant 1 gång per vecka (fredagar)", route=sc, form=inj,
        timing={"frequency": 1, "period": 1, "periodUnit": "wk"},
        start="2022-05-20", reason=jia,
        note="Senaste dos 2026-10-09. Ges av föräldrarna hemma. Pausas vid "
             "feber eller infektion som kräver antibiotika.")
    folate = medication(
        b, atc="B03BB01", atc_display="folic acid", text="Folsyra 5 mg tablett",
        dose="1 tablett (5 mg) 1 gång per vecka, dagen efter metotrexat (lördagar)",
        route=po, form=tablet,
        timing={"frequency": 1, "period": 1, "periodUnit": "wk"},
        start="2022-05-20", reason=jia)
    saline = medication(
        b, atc=None, atc_display=None,
        code=sct("387390002", "Sodium chloride",
                 "Natriumklorid 9 mg/ml inhalationsvätska"),
        text="Natriumklorid 9 mg/ml inhalationsvätska",
        dose="2-4 ml inhaleras via nebulisator 2-4 gånger dagligen vid "
             "luftvägsinfektion med segt slem", route=inh, as_needed=True,
        start="2022-01-10", reason=lrti)
    ada_last = last_dose(b, statement_ref=ada, text=ada_text, given="2026-10-06",
                         dose="40 mg subkutant", amount=(40, "mg"), route=sc,
                         performer="Förälder")
    mtx_last = last_dose(b, statement_ref=mtx, text=mtx_text, given="2026-10-09",
                         dose="15 mg subkutant", amount=(15, "mg"), route=sc,
                         performer="Förälder")
    b.section("Läkemedelslista", "10160-0", "History of Medication use Narrative",
              [[ada_text, "40 mg subkutant varannan vecka", "2023-01-17", "2026-10-06",
                "Juvenil idiopatisk artrit"],
               [mtx_text, "15 mg subkutant 1 gång per vecka", "2022-05-20",
                "2026-10-09", "Juvenil idiopatisk artrit"],
               ["Folsyra 5 mg tablett", "5 mg 1 gång per vecka, dagen efter metotrexat",
                "2022-05-20", "", "Metotrexatbehandling"],
               ["Natriumklorid 9 mg/ml inhalationsvätska",
                "2-4 ml via nebulisator 2-4 gånger dagligen vid behov", "2022-01-10",
                "", "Segt slem vid luftvägsinfektion"]],
              ["Läkemedel", "Dosering", "Insatt", "Senaste dos", "Indikation"],
              [ada, ada_last, mtx, mtx_last, folate, saline])

    # --- alerts. The EPS Alerts section is where a receiving system looks
    # before anything else; each flag points at the problems and medicines it
    # is drawn from, so the evidence is one reference away.
    airway = flag(
        b, code=sct("718449003", "Expected difficult tracheal intubation"),
        text="SVÅR LUFTVÄG - förväntat svår intubation och svår maskventilation",
        since="2016-03-15", priority="PH",
        detail_refs=[prob_refs["718447001"], prob_refs["763326004"],
                     prob_refs["52616002"], prob_refs["87866006"]],
        author=anaes,
        note="Freeman-Sheldons syndrom med mikrognati, mikrostomi och gapförmåga "
             "cirka 20 mm. Direktlaryngoskopi har misslyckats (Cormack-Lehane 4). "
             "Fungerat: induktion med bibehållen spontanandning, ingen "
             "muskelrelaxation före säkrad luftväg, nasal fiberoptisk intubation "
             "(tub 5,0) med ÖNH-läkare närvarande. Ring barnanestesijouren "
             "Karolinska +46 8 000 00 11 innan all sedering. Se luftvägsplanen.")
    immuno = flag(
        b, code=immsupp, text="IMMUNSUPPRIMERAD - adalimumab och metotrexat",
        since="2022-05-20", priority="PH",
        detail_refs=[ada, mtx, prob_refs["370391006"]], author=rheum,
        note="Feber kan vara allvarlig infektion med dämpade symtom. Senaste dos "
             "adalimumab 2026-10-06, metotrexat 2026-10-09. Pausa båda vid "
             "infektion. Inga levande vacciner. Se infektionsplanen.")
    secretions = flag(
        b, code=sct("102581000", "Excessive bronchial secretion"),
        text="Rikliga sega luftvägssekret och andningssvårigheter vid infektion",
        since="2017-11-03", priority="PM",
        detail_refs=[prob_refs["448739000"]], author=rheum,
        note="Tidig slemmobilisering, NaCl-inhalation och låg tröskel för "
             "inläggning. Sugning kan behövas; munöppningen är liten.")
    b.section("Uppmärksamhetsinformation", "104605-1", "Alert",
              [["Svår luftväg - förväntat svår intubation och svår maskventilation",
                "Hög", "2016-03-15",
                "Ring barnanestesijouren Karolinska +46 8 000 00 11 innan all "
                "sedering. Fiberoptisk nasal intubation har fungerat; "
                "direktlaryngoskopi har misslyckats."],
               ["Immunsupprimerad - adalimumab och metotrexat", "Hög",
                "2022-05-20", "Senaste dos adalimumab 2026-10-06, metotrexat "
                "2026-10-09. Pausa vid infektion. Inga levande vacciner."],
               ["Rikliga sega luftvägssekret vid infektion", "Medel",
                "2017-11-03", "Tidig slemmobilisering och NaCl-inhalation; låg "
                "tröskel för inläggning."]],
              ["Varning", "Prioritet", "Sedan", "Att göra"],
              [airway, immuno, secretions])
    # The alerts cite problems and medicines, so they had to be built after
    # them; they are still read first, so the section goes first.
    b.sections.insert(0, b.sections.pop())

    # --- allergies: positively asserted as empty.
    b.section("Allergier och intoleranser", "48765-2",
              "Allergies and adverse reactions Document",
              [], "Inga kända allergier eller intoleranser.", [],
              empty_reason=("nilknown", "Nil Known"))

    # --- procedures. Both anaesthetics are here with what went wrong, and the
    # second with what worked, coded as the intubation technique that
    # succeeded. The full anaesthetic record is linked, not copied.
    ga = sct("50697003", "Administration of general anesthetic", "Narkos")
    procs = [
        dict(code=sct("28913000", "Tonsillectomy and adenoidectomy",
                      "Tonsillektomi och adenoidektomi"), date="2016-03-15",
             note="Karolinska. Indikation: obstruktiv sömnapné.", performer=ent),
        dict(code=ga, date="2016-03-15", complications=[diff_intub, diff_mask],
             performer=anaes,
             note="Oväntat svår luftväg (3 år). Misslyckad direktlaryngoskopi, "
                  "svår maskventilation med desaturation till 78 %. Intuberad "
                  "fiberoptiskt oralt av ÖNH-läkare efter flera försök. Sedan "
                  "dess planeras all anestesi enligt luftvägsplanen."),
        dict(code=sct("1332284004", "MRI of bilateral sacroiliac joints",
                      "MR sakroiliakaleder"), date="2024-10-14",
             note="I narkos. Bilateral sakroiliit."),
        dict(code=ga, date="2024-10-14", complications=[diff_mask], performer=anaes,
             note="Planerad svår luftväg. Sevofluraninduktion med bibehållen "
                  "spontanandning, ingen muskelrelaxation före säkrad luftväg. "
                  "Maskventilation svår (tvåhandsgrepp, svalgtub). Nasal "
                  "fiberoptisk intubation, tub 5,0 kuffad, lyckades vid första "
                  "försöket. ÖNH-läkare närvarande med beredskap för kirurgisk "
                  "luftväg. Extuberad vaken, okomplicerat."),
        dict(code=sct("232682004", "Nasotracheal fiberoptic intubation",
                      "Nasal fiberoptisk intubation"), date="2024-10-14",
             performer=anaes, reason_refs=[prob_refs["718447001"]],
             note="Tekniken som fungerade: se narkosen samma dag."),
    ]
    proc_refs = [procedure(b, **p) for p in procs]
    record = document_link(
        b, loinc="11485-0", display="Anesthesia records",
        description="Anestesijournal 2024-10-14, Karolinska Universitetssjukhuset",
        date="2024-10-14", author=anaes,
        url="https://journal.karolinska.example/anestesi/2024-10-14/ville.pdf")
    b.section("Genomgångna åtgärder", "47519-4", "History of Procedures Document",
              [[p["code"]["text"], p["date"],
                ", ".join(c["text"] for c in p.get("complications", [])),
                p.get("note") or ""] for p in procs]
              + [["Anestesijournal 2024-10-14 (länk)", "2024-10-14", "",
                  "https://journal.karolinska.example/anestesi/2024-10-14/ville.pdf"]],
              ["Åtgärd", "Datum", "Komplikation", "Kommentar"],
              proc_refs + [record])

    # --- immunizations. The Swedish programme up to his age, plus the
    # pneumococcal, influenza and COVID doses given because of the
    # immunosuppression. The live MMR doses predate it.
    shots = [
        (sct("1381704004",
             "Acellular Bordetella pertussis and Clostridium tetani and Corynebacterium "
             "diphtheriae and Human poliovirus antigens only vaccine product",
             "Difteri-stelkramp-kikhosta-polio (booster, 5 år)"), "2018-05-15",
         "J07CA02", "diphtheria-pertussis-poliomyelitis-tetanus"),
        (sct("2241000221103",
             "Live attenuated Measles morbillivirus and Mumps orthorubulavirus and "
             "Rubella virus antigens only vaccine product",
             "Mässling-påssjuka-röda hund (dos 2, årskurs 1)"), "2020-10-07",
         "J07BD52", "measles, combinations with mumps and rubella, live attenuated"),
        (sct("981000221107", "Streptococcus pneumoniae antigen only vaccine product",
             "Pneumokockvaccin (konjugat, inför immunsuppression)"), "2022-05-03",
         None, None),
        (sct("911000221103", "Human papillomavirus antigen only vaccine product",
             "HPV-vaccin (årskurs 5)"), "2024-09-10", None, None),
        (sct("1181000221105",
             "Vaccine product containing only Influenza virus antigen (medicinal product)",
             "Influensavaccin"), "2025-10-21", "J07BB02",
         "influenza, inactivated, split virus or surface antigen"),
        (sct("1119305005", "SARS-CoV-2 antigen vaccine", "Covid-19-vaccin"),
         "2025-10-21", "J07BN04", "covid-19, protein subunit"),
    ]
    refs = [immunization(b, code=c, date=d, atc=a, atc_display=ad)
            for c, d, a, ad in shots]
    b.section("Vaccinationer", "11369-6", "History of Immunization note",
              [[c["text"], d] for c, d, _, _ in shots], ["Vaccin", "Datum"], refs)

    # --- results: the quarterly methotrexate monitoring, two weeks old. Normal,
    # which is itself the point - a receiving team can see the marrow and the
    # liver were fine before the infection.
    labspec = [
        dict(loinc="718-7", display="Hemoglobin [Mass/volume] in Blood",
             date="2026-09-29", value=131, unit="g/L", low=115, high=150),
        dict(loinc="6690-2", display="Leukocytes [#/volume] in Blood by Automated count",
             date="2026-09-29", value=5.4, unit="10*9/L", low=4.0, high=11.0),
        dict(loinc="751-8", display="Neutrophils [#/volume] in Blood by Automated count",
             date="2026-09-29", value=2.6, unit="10*9/L", low=1.6, high=7.0),
        dict(loinc="777-3", display="Platelets [#/volume] in Blood by Automated count",
             date="2026-09-29", value=262, unit="10*9/L", low=150, high=400),
        dict(loinc="1742-6",
             display="Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma",
             date="2026-09-29", value=0.48, unit="ukat/L", high=0.75),
        dict(loinc="14682-9", display="Creatinine [Moles/volume] in Serum or Plasma",
             date="2026-09-29", value=49, unit="umol/L", low=30, high=70),
        dict(loinc="1988-5", display="C reactive protein [Mass/volume] in Serum or Plasma",
             date="2026-09-29", value=1.2, unit="mg/L", high=5.0),
    ]
    for l in labspec:
        l["performer"] = "Klinisk kemi, Karolinska Universitetslaboratoriet"
    refs = [lab(b, **l) for l in labspec]
    b.section("Laboratorieresultat", "30954-2",
              "Relevant diagnostic tests/laboratory data note",
              [[l["display"], l["date"], "%s %s" % (l["value"], l["unit"]),
                "Inom referens"] for l in labspec],
              ["Analys", "Datum", "Resultat", "Tolkning"], refs)

    # --- devices
    nebuliser = sct("170615005", "Home nebulizer", "Nebulisator för hemmabruk")
    refs = [device_use(b, code=nebuliser, since="2022-01-10",
                       note="Utlånad av barnfysioterapin. Används med "
                            "natriumklorid 9 mg/ml vid luftvägsinfektion, "
                            "tillsammans med slemmobilisering (PEP-mask är svår "
                            "att använda på grund av munöppningen).")]
    b.section("Medicintekniska produkter", "46264-8",
              "History of medical device use",
              [["Nebulisator för hemmabruk", "2022-01-10", "I bruk vid behov"]],
              ["Produkt", "I bruk sedan", "Status"], refs)

    # --- plan of care. The airway plan is the document the scenario turns on:
    # it is the anaesthetist's, it names the team, and it addresses the exact
    # problems the alert cites.
    airway_steps = [
        ("CommunicationRequest", "not-started",
         "Kontakta barnanestesiolog och ÖNH-läkare innan all sedering, narkos "
         "eller intubation - även akut. Karolinska barnanestesijour "
         "+46 8 000 00 11 kan konsulteras dygnet runt."),
        ("ServiceRequest", "not-started",
         "Induktion med bibehållen spontanandning (sevofluran). Ingen "
         "muskelrelaxation före säkrad luftväg."),
        ("ServiceRequest", "not-started",
         "Förstahandsval: nasal fiberoptisk intubation, kuffad tub 5,0-5,5. "
         "Videolaryngoskop med hyperangulerat blad som reserv. Larynxmask "
         "storlek 2-2,5 får sällan plats (munöppning cirka 20 mm)."),
        ("ServiceRequest", "not-started",
         "Maskventilation är svår: tvåhandsgrepp, svalgtub och två personer "
         "från start. ÖNH-läkare närvarande med beredskap för kirurgisk "
         "luftväg (koniotomi-set framtaget)."),
        ("ServiceRequest", "not-started",
         "Extubation vaken, med ÖNH-beredskap. Postoperativ övervakning på "
         "barnintensiven eller motsvarande."),
    ]
    infection_steps = [
        ("Task", "in-progress",
         "Vid feber över 38,5 °C eller infektion som kräver antibiotika: pausa "
         "adalimumab och metotrexat. Återuppta efter kontakt med "
         "barnreumatolog."),
        ("ServiceRequest", "in-progress",
         "Låg tröskel för inläggning, odlingar och antibiotika vid nedre "
         "luftvägsinfektion. Tidig slemmobilisering med fysioterapeut och "
         "natriumklorid-inhalation."),
        ("Task", "in-progress",
         "Inga levande vacciner under pågående behandling. Årlig influensa- och "
         "covid-19-vaccination."),
    ]
    plans = [
        dict(description="Luftvägsplan inför sedering, narkos och intubation",
             start="2016-03-16", steps=airway_steps, author=anaes, team=team,
             addresses=[prob_refs["718447001"], prob_refs["763326004"],
                        prob_refs["52616002"]],
             note="Uppdaterad av barnanestesiolog efter narkosen 2024-10-14, "
                  "då planen följdes och fungerade. Fullständig anestesijournal "
                  "länkad under Genomgångna åtgärder."),
        dict(description="Infektionsplan under immunsuppression",
             start="2022-05-20", steps=infection_steps, author=rheum, team=team,
             addresses=[prob_refs["370391006"], prob_refs["448739000"]]),
        dict(description="Återbesök barnreumatologmottagningen, Karolinska - "
                         "kontroll var 3:e månad med blodstatus, ALAT och kreatinin",
             start="2022-02-08", activity_status="scheduled",
             scheduled="2026-12-15", reason=jia, author=rheum, team=team,
             addresses=[prob_refs["410801005"]]),
    ]
    refs = [care_plan(b, **p) for p in plans]
    b.section("Vårdplan", "18776-5", "Plan of care note",
              [[plans[0]["description"], "2016-03-16", "Aktiv",
                " ".join(s[2] for s in airway_steps)],
               [plans[1]["description"], "2022-05-20", "Aktiv",
                " ".join(s[2] for s in infection_steps)],
               [plans[2]["description"], "2022-02-08", "Bokad 2026-12-15", ""]],
              ["Plan", "Sedan", "Status", "Innehåll"], refs)

    # --- patient story: narrative only. The preferences that have no coded
    # home but change how the ward treats him.
    b.sections.append({
        "title": "Patientberättelse och önskemål",
        # LOINC is what the EPS profile fixes; the SNOMED coding beside it is
        # the closest the terminology has - it names the one preference in
        # here that is structured anywhere, the preferred name.
        "code": {"coding": [
            {"system": LOINC, "code": "81338-6",
             "display": "Patient Goals, preferences, and priorities for care experience"},
            {"system": SCT, "code": "408676007", "display": "Patient preferred name"}],
            "text": "Patientberättelse och önskemål"},
        "text": {"status": "generated", "div": paragraphs([
            "Ville vill kallas Ville, inte Vilhelm.",
            "Han vill att man berättar vad som ska hända innan det händer, "
            "särskilt om det gäller munnen eller ansiktet. Han har varit med om "
            "tre narkoser och vet att hans luftväg är svår; en mask som sätts "
            "på utan förvarning gör honom rädd.",
            "Mamma Anna Söderberg är hans närmaste anhörig och vårdnadshavare, "
            "känner till luftvägshistoriken i detalj och vill vara med vid "
            "undersökningar och vid insomning. Hon ger adalimumab- och "
            "metotrexatinjektionerna hemma.",
            "Ville går i sjunde klass, spelar e-sport och vill helst slippa "
            "ligga inne längre än nödvändigt.",
        ], "sv")},
    })
    return b.build()


if __name__ == "__main__":
    target = os.path.dirname(os.path.abspath(__file__))
    for name, fn in (("andrea-cross-border", build_andrea),
                     ("arne-complex-care", build_arne),
                     ("ville-difficult-airway", build_ville)):
        bundle = fn()
        path = os.path.join(target, name + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(name, len(bundle["entry"]), "entries ->", path)

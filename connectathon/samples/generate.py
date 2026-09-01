"""Build the two Nordic Hackathon 2026 scenario bundles as HL7 EU EPS documents.

The patients are synthetic. Nothing here comes from a real system, record or
person - the bundles only illustrate how such a summary could look, and exist so
the clinician views have something rich to render.

Every SNOMED CT code used here was verified against a live Snowstorm terminology
server; ATC codes are WHO ATC 2025. Both bundles validate against
bundle-eu-eps with zero errors.

    python3 samples/generate.py
"""
import json, uuid, html, os

SCT = "http://snomed.info/sct"
LOINC = "http://loinc.org"
ATC = "http://www.whocc.no/atc"
UCUM = "http://unitsofmeasure.org"
EPS = "http://hl7.eu/fhir/eps/StructureDefinition/"
EUBASE = "http://hl7.eu/fhir/base/StructureDefinition/"
THL7 = "http://terminology.hl7.org/CodeSystem/"

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


def resource_narrative(r):
    """A short human-readable rendering of one resource, as dom-6 asks for."""
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
    elif t == "Condition":
        rows = [("Condition", _txt(r["code"])),
                ("Onset", r.get("onsetDateTime")),
                ("Clinical status", _txt(r.get("clinicalStatus"))),
                ("Verification", _txt(r.get("verificationStatus")))]
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
                ("Status", r.get("status"))]
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
        rows = [("Role", "Authoring practitioner at the custodian organisation")]
    if r.get("note"):
        rows.append(("Note", r["note"][0]["text"]))
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
            "meta": {"profile": [EUBASE + "practitionerRole-eu"]},
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
            "identifier": {"system": "urn:ietf:rfc:9562", "value": self.uid("comp-id")},
            "status": "final",
            "type": cc(LOINC, "60591-5", "Patient summary Document"),
            "subject": {"reference": self.pat_ref},
            "date": self.doc_date + "T09:00:00+01:00",
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
             ["Sections", ", ".join(s["title"] for s in self.sections)]],
            self.language)}
        self.entries.insert(0, {"fullUrl": "urn:uuid:" + comp["id"], "resource": comp})
        return {
            "resourceType": "Bundle",
            "id": self.uid("bundle"),
            "meta": {"profile": [EPS + "bundle-eu-eps"]},
            "language": self.language,
            "identifier": {"system": "urn:ietf:rfc:9562", "value": self.uid("bundle-id")},
            "type": "document",
            "timestamp": self.doc_date + "T09:00:00+01:00",
            "entry": self.entries,
        }


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

def condition(b, code, onset, status="active", verification="confirmed"):
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
    return b.add(r, "condition")


def medication(b, *, atc, atc_display, text, dose, start, reason=None,
               status="active", end=None, as_needed=False, form=None):
    med = {"resourceType": "Medication",
           "meta": {"profile": [EUBASE + "medication-eu-core"]},
           "code": cc(ATC, atc, atc_display, text)}
    if form:
        med["form"] = sct(*form)
    med_ref = b.add(med, "medication")
    period = {"start": start}
    if end:
        period["end"] = end
    ms = {
        "resourceType": "MedicationStatement",
        "meta": {"profile": [EPS + "MedicationStatement-eu-eps"]},
        "status": status,
        "medicationReference": {"reference": med_ref, "display": text},
        "subject": b.subject(),
        "effectivePeriod": period,
        "dosage": [{"text": dose, "asNeededBoolean": as_needed}],
    }
    if reason:
        ms["reasonCode"] = [reason]
    return b.add(ms, "medstatement")


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


def procedure(b, *, code, date, note=None):
    r = {
        "resourceType": "Procedure",
        "meta": {"profile": [EUBASE + "procedure-eu-core"]},
        "status": "completed",
        "code": code,
        "subject": b.subject(),
        "performedDateTime": date,
    }
    if note:
        r["note"] = [{"text": note}]
    return b.add(r, "procedure")


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
              reason=None, note=None, scheduled=None):
    detail = {"kind": "Appointment", "status": activity_status,
              "description": description}
    if reason:
        detail["reasonCode"] = [reason]
    if scheduled:
        detail["scheduledPeriod"] = {"start": scheduled}
    r = {
        "resourceType": "CarePlan",
        "status": "active",
        "intent": "plan",
        "category": [sct("734163000", "Care plan")],
        "title": description,
        "subject": b.subject(),
        "period": {"start": start},
        "activity": [{"detail": detail}],
    }
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
            "meta": {"profile": [EPS + "patient-eu-eps"]},
            "identifier": [{
                "type": cc(THL7 + "v2-0203", "NI",
                                "National unique individual identifier",
                                "Personnummer"),
                "system": "urn:oid:1.2.752.129.2.1.3.1",
                "value": "20020317-0000",
            }],
            "name": [{"text": "Andrea Lindqvist", "family": "Lindqvist",
                      "given": ["Andrea"]}],
            "telecom": [{"system": "phone", "value": "+46 70 000 00 01",
                         "use": "mobile"},
                        {"system": "email", "value": "andrea.lindqvist@example.org"}],
            "gender": "female",
            "birthDate": "2002-03-17",
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
            "meta": {"profile": [EUBASE + "practitioner-eu"]},
            "name": [{"family": "Sundström", "given": ["Åsa"], "prefix": ["Dr"]}],
        }},
        org={
            "resourceType": "Organization",
            "meta": {"profile": [EUBASE + "organization-eu"]},
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
             "COVID-19 vaccine"), "2021-10-11", "J07BX03", "covid-19 vaccines"),
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
             display="Hemoglobin A1c/Hemoglobin.total in Blood by IFCC protocol",
             date="2025-12-09", value=68, unit="mmol/mol", low=27, high=42,
             interpretation=("H", "High"), performer="Klinisk kemi, Sävelunds sjukhus, Uppsala"),
        dict(loinc="2160-0", display="Creatinine [Mass/volume] in Serum or Plasma",
             date="2025-12-09", value=71, unit="umol/L", low=45, high=90,
             performer="Klinisk kemi, Sävelunds sjukhus, Uppsala"),
        dict(loinc="14959-1",
             display="Microalbumin/Creatinine [Mass Ratio] in Urine",
             date="2025-12-09", value=4.1, unit="mg/mmol", high=3.0,
             interpretation=("H", "High"), performer="Klinisk kemi, Sävelunds sjukhus, Uppsala"),
        dict(loinc="2093-3", display="Cholesterol [Mass/volume] in Serum or Plasma",
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
# the Swedish summary Rigshospitalet retrieves before he goes to theatre. Written
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
            "meta": {"profile": [EPS + "patient-eu-eps"]},
            "identifier": [{
                "type": cc(THL7 + "v2-0203", "NI",
                                "National unique individual identifier",
                                "Personnummer"),
                "system": "urn:oid:1.2.752.129.2.1.3.1",
                "value": "19430211-0000",
            }],
            "name": [{"text": "Arne Bergström", "family": "Bergström",
                      "given": ["Arne", "Gustav"]}],
            "telecom": [{"system": "phone", "value": "+46 70 000 00 02",
                         "use": "home"}],
            "gender": "male",
            "birthDate": "1943-02-11",
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
            "meta": {"profile": [EUBASE + "practitioner-eu"]},
            "name": [{"family": "Lövgren", "given": ["Märta"], "prefix": ["Dr"]}],
        }},
        org={
            "resourceType": "Organization",
            "meta": {"profile": [EUBASE + "organization-eu"]},
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
             reaction=sct("1371396001", "Allergic rhinoconjunctivitis",
                          "Allergisk rinokonjunktivit"),
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
         "2025-10-09", "J07BX03", "covid-19 vaccines"),
    ]
    refs = [immunization(b, code=c, date=d, atc=a, atc_display=ad)
            for c, d, a, ad in shots]
    b.section("Vaccinationer", "11369-6", "History of Immunization note",
              [[c["text"], d] for c, d, _, _ in shots], ["Vaccin", "Datum"], refs)

    # --- results. Renal function and haemoglobin are what an anaesthetist reaches
    # for first, so they are current rather than historic.
    labspec = [
        dict(loinc="59261-8",
             display="Hemoglobin A1c/Hemoglobin.total in Blood by IFCC protocol",
             date="2026-06-12", value=58, unit="mmol/mol", low=27, high=42,
             interpretation=("H", "High")),
        dict(loinc="2160-0", display="Creatinine [Mass/volume] in Serum or Plasma",
             date="2026-06-12", value=118, unit="umol/L", low=60, high=105,
             interpretation=("H", "High")),
        dict(loinc="33914-3",
             display="Glomerular filtration rate/1.73 sq M.predicted",
             date="2026-06-12", value=46, unit="mL/min/{1.73_m2}", low=60,
             interpretation=("L", "Low")),
        dict(loinc="718-7", display="Hemoglobin [Mass/volume] in Blood",
             date="2026-06-12", value=112, unit="g/L", low=134, high=170,
             interpretation=("L", "Low")),
        dict(loinc="2823-3", display="Potassium [Moles/volume] in Serum or Plasma",
             date="2026-06-12", value=4.4, unit="mmol/L", low=3.5, high=4.6),
        dict(loinc="33762-6",
             display="Natriuretic peptide B prohormone N-Terminal [Mass/volume] in Serum or Plasma",
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


if __name__ == "__main__":
    target = os.path.dirname(os.path.abspath(__file__))
    for name, fn in (("andrea-cross-border", build_andrea),
                     ("arne-complex-care", build_arne)):
        bundle = fn()
        path = os.path.join(target, name + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(name, len(bundle["entry"]), "entries ->", path)

"""The event this instance runs for, read from $EVENT_DIR/event.toml.

Everything that differs from one hackathon to the next - its name, where and
when, which document types it scores and against which guide versions, the
points, the worked examples - lives in one folder, so running Provbana for
another event is writing that folder rather than editing app.py. The repo's
own events/nordic-hackathon-2026/ is the complete worked example and the
default.

Unknown keys are refused rather than ignored, so a typo in the file stops the
service at startup with the key named instead of silently scoring with a
default.
"""
import os
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EVENT_DIR = os.path.normpath(
    os.path.join(HERE, "..", "events", "nordic-hackathon-2026"))

SCORING_DEFAULTS = {
    "valid_base": 50,
    "section_points": 10,
    "resource_points": 2,
    "resource_cap": 60,
    "section_cap": 160,
    "first_pass_bonus": 20,
    "secondary_category_points": 15,
}

EVENT_KEYS = {"name", "url", "when_where", "canonical_base"}
SAMPLE_KEYS = {"id", "name", "tagline", "scenario", "horizon", "language",
               "scenario_name", "scenario_url"}
# What an event may set on a document type. A catalogue type takes any of
# these as an override; a type the catalogue does not know needs DOC_REQUIRED.
DOC_KEYS = {"label", "long_label", "short", "category", "profile", "igs",
            "extra_igs", "bundles", "loinc", "entry_profiles", "ehds_from",
            "maturity", "spec", "guide"}
DOC_REQUIRED = {"label", "long_label", "short", "category", "profile", "igs"}


class EventConfigError(ValueError):
    pass


def event_dir() -> str:
    return os.environ.get("EVENT_DIR") or DEFAULT_EVENT_DIR


def _unknown(where: str, table: dict, allowed: set) -> None:
    extra = sorted(set(table) - allowed)
    if extra:
        raise EventConfigError("%s: unknown key %s (allowed: %s)"
                               % (where, ", ".join(extra), ", ".join(sorted(allowed))))


def _string(where: str, value, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise EventConfigError("%s is required" % where)
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise EventConfigError("%s must be a non-empty string" % where)
    return value


def _string_list(where: str, value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise EventConfigError("%s must be a list of strings" % where)
    return value


def load(directory: str | None = None) -> dict:
    """The validated event config. Raises EventConfigError naming the key."""
    directory = directory or event_dir()
    path = os.path.join(directory, "event.toml")
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise EventConfigError("no event.toml in %s - set EVENT_DIR to an event "
                               "folder (see events/nordic-hackathon-2026)" % directory)
    except tomllib.TOMLDecodeError as exc:
        raise EventConfigError("%s: %s" % (path, exc))

    _unknown("event.toml", raw, {"event", "documents", "scoring", "samples",
                                 "identifiers"})

    ev = raw.get("event", {})
    _unknown("[event]", ev, EVENT_KEYS)
    event = {
        "name": _string("[event] name", ev.get("name"), required=True),
        "url": _string("[event] url", ev.get("url")),
        "when_where": _string("[event] when_where", ev.get("when_where")),
        "canonical_base": _string("[event] canonical_base", ev.get("canonical_base")),
    }
    if not event["canonical_base"]:
        site = os.environ.get("SITE_ADDRESS") or "localhost"
        event["canonical_base"] = "https://%s/fhir" % site
    event["canonical_base"] = event["canonical_base"].rstrip("/")

    docs = dict(raw.get("documents", {}))
    primary = _string("[documents] primary", docs.pop("primary", None))
    enabled = docs.pop("enabled", None)
    if enabled is not None:
        enabled = _string_list("[documents] enabled", enabled)
    types = {}
    for doc_id, table in docs.items():
        if not isinstance(table, dict):
            raise EventConfigError("[documents] %s: expected a [documents.%s] table"
                                   % (doc_id, doc_id))
        _unknown("[documents.%s]" % doc_id, table, DOC_KEYS)
        for key in ("igs", "extra_igs", "bundles", "loinc", "entry_profiles"):
            if key in table:
                _string_list("[documents.%s] %s" % (doc_id, key), table[key])
        types[doc_id] = table

    scoring = dict(SCORING_DEFAULTS)
    sc = raw.get("scoring", {})
    _unknown("[scoring]", sc, set(SCORING_DEFAULTS))
    for key, value in sc.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise EventConfigError("[scoring] %s must be a whole number >= 0" % key)
        scoring[key] = value

    samples = []
    for i, s in enumerate(raw.get("samples", [])):
        where = "[[samples]] #%d" % (i + 1)
        _unknown(where, s, SAMPLE_KEYS)
        sample = {key: _string("%s %s" % (where, key), s.get(key),
                               required=key in ("id", "name"))
                  for key in sorted(SAMPLE_KEYS)}
        # Printed as they are, so absent reads as blank rather than "None".
        for key in ("tagline", "scenario", "language"):
            sample[key] = sample[key] or ""
        if not sample["id"].replace("-", "").isalnum():
            raise EventConfigError("%s id must be letters, digits and dashes" % where)
        samples.append(sample)
    if len({s["id"] for s in samples}) != len(samples):
        raise EventConfigError("[[samples]] ids must be unique")

    identifiers = raw.get("identifiers", {})
    if not all(isinstance(v, str) for v in identifiers.values()):
        raise EventConfigError("[identifiers] maps an identifier system to a label string")

    return {
        "dir": directory,
        "event": event,
        "documents": {"primary": primary, "enabled": enabled, "types": types},
        "scoring": scoring,
        "samples": samples,
        "identifiers": dict(identifiers),
    }


def build_documents(catalogue: list[dict], config: dict) -> list[dict]:
    """The event's document types, in `enabled` order, the primary marked.

    Each one starts from the catalogue entry of the same id, if there is one,
    with the event's overrides on top; `extra_igs` is appended to `igs` rather
    than replacing it, which is how an event adds a national base guide to a
    European one without restating the version pin.
    """
    by_id = {doc["id"]: doc for doc in catalogue}
    cfg = config["documents"]
    enabled = cfg["enabled"] or [doc["id"] for doc in catalogue]
    if not enabled:
        raise EventConfigError("[documents] enabled must name at least one type")
    if len(set(enabled)) != len(enabled):
        raise EventConfigError("[documents] enabled lists a type twice")
    for doc_id in cfg["types"]:
        if doc_id not in enabled:
            raise EventConfigError("[documents.%s] is configured but not in "
                                   "[documents] enabled" % doc_id)
    primary = cfg["primary"] or enabled[0]
    if primary not in enabled:
        raise EventConfigError("[documents] primary %r is not in enabled" % primary)

    docs = []
    for doc_id in enabled:
        override = dict(cfg["types"].get(doc_id, {}))
        extra_igs = override.pop("extra_igs", [])
        base = by_id.get(doc_id)
        if base is None:
            missing = DOC_REQUIRED - set(override)
            if missing:
                raise EventConfigError(
                    "[documents.%s] is not a built-in type (%s), so it needs %s"
                    % (doc_id, ", ".join(by_id), ", ".join(sorted(missing))))
            base = {"id": doc_id, "bundles": [], "loinc": [], "ehds_from": None,
                    "maturity": "", "spec": ""}
        doc = {**base, **override, "id": doc_id, "primary": doc_id == primary}
        doc["igs"] = list(doc["igs"]) + list(extra_igs)
        docs.append(doc)
    return docs

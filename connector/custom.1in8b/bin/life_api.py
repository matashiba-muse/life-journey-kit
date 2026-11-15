#!/usr/bin/env python3
"""life_api.py: play one simulated life through the 1in8b Engine /live API.

The helper is the only piece that talks to the engine. It keeps each life's
sealed resume token in a private local state directory, records every choice
with the reason Muse gave for it, and writes a token-free display JSON
(schema v1) that a viewer such as a fixed Space UI can render.

Engine facts (from https://1in8b.life/en/api/, checked 2026-11-10):
  - POST /api/v1/live with {} starts a life and runs to its first decision.
  - Each step sends {"token": ..., "choose": "key"} and costs 1 life unit.
    A start costs 1 unit too (2 for a past year or the coming world, which
    this helper never requests).
  - {"token": ..., "view": true} returns the current state, costs no units
    and returns the same token.
  - The token holds the whole life, sealed. The server keeps nothing. A
    token works for 90 days.
  - The free plan allows 2,000 life units a month and 30 calls a minute.

Safety rules built into this helper:
  - It never sends the sensitive or suicide options.
  - A step is journaled as an intent before it is sent. If the transport
    fails with an unknown outcome (timeout, dropped connection, 5xx, or a
    success body that is broken or carries no new token), the step is
    never resent automatically. One exception is not an unknown outcome:
    a step that ends the life answers with ended true, a death record and
    a null token, and that answer is complete and confirmed. The life is marked unknown and the helper
    exits with code 3. A view only ever shows the last confirmed state of
    the token held locally; it can never prove the lost step was not
    applied. The only way forward is `reconcile --abandon`, a deliberate,
    journaled fork from the last confirmed token. There is no resend
    operation: whether the same input would reproduce the same result is
    unconfirmed, so retrying a lost step is not offered.
  - State files live under a 0700 directory, are written 0600, replaced
    atomically, and every mutating command runs under an flock.
  - The token and any credential surrogate are never printed. Only the
    first 12 hex characters of the token SHA-256 fingerprint may appear.

Mock mode: LJ_MOCK=1 swaps the network for a deterministic token-contained
mock engine, so the whole flow can be tested without a key. The mock token
carries the mock life state in itself, so view and step keep working
across CLI restarts. LJ_MOCK_FAULT injects failures: "timeout" makes a
mock start or step raise a timeout after the intent journal is written,
"http500" answers with an HTTP 500, "http503" answers with the engine's
busy response (HTTP 503, code busy), "http503bare" answers with an
HTTP 503 carrying no code at all, "http500busy" answers with an
HTTP 500 carrying the busy code, and "badjson" returns a success
body whose new token is missing. All of them exercise the unknown paths.
"badend" strips the death record from an ending step, and "leaky"
raises a definitive error whose text carries a fake key, to prove the
CLI never echoes upstream text.

Exit codes:
  0  success
  2  local rejection (bad arguments, caps, wrong life status, no decision)
  3  step outcome unknown; the life is marked unknown and stopped
  4  not connected (no registered credential surrogate available)
  5  upstream or transport error with a definitive outcome

Usage:
  life_api.py start [--seed N]
  life_api.py status [--life ID]
  life_api.py choices [--life ID]
  life_api.py choose KEY --reason "text" [--focus KEY] [--buy ID ...] [--life ID]
  life_api.py pause [--life ID]
  life_api.py resume [--life ID]
  life_api.py reconcile [--abandon] [--life ID]
  life_api.py history
  life_api.py recap [--life ID]
  life_api.py catalog {countries|causes|jobs|groups}
  life_api.py delta [--life ID]
  life_api.py delta --ack --revision N [--life ID]
"""

import argparse
import base64
import copy
import fcntl
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, date, timezone

API_HOST = "1in8b.life"
LIVE_URL = "https://1in8b.life/api/v1/live"
CATALOG_URL = "https://1in8b.life/api/v1/"
CATALOG_NAMES = ("countries", "causes", "jobs", "groups")
CREDENTIAL = "custom.1in8b"
ALLOWED_HOSTS = [API_HOST]
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 2_000_000
DISPLAY_SCHEMA_VERSION = 1
DELTA_SCHEMA_VERSION = 1
DELTA_SCHEMA_VERSION_V2 = 2
MOCK_ENGINE_VERSION = "mock-1.0"

DEFAULT_CONFIG = {
    "max_starts_per_day": 3,
    "max_steps_per_day": 200,
    "max_steps_per_life": 500,
    "key_acquired_on": None,
}

CREDIT_LINE = "1in8b Engine (1in8b.life)"
DISCLAIMER = (
    "This life is a simulation drawn from real statistics. "
    "It is not a real person, a record or a forecast."
)
SOURCE_CREDITS = [
    {"id": "wpp", "name": "UN World Population Prospects 2024", "by": "UN DESA Population Division", "licence": "CC BY 3.0 IGO"},
    {"id": "igme", "name": "UN Inter-agency Group for Child Mortality Estimation (UN IGME)", "by": "UNICEF, WHO, World Bank, UN DESA", "licence": "CC BY 4.0 (as published in World Bank WDI)"},
    {"id": "wdi", "name": "World Development Indicators", "by": "World Bank", "licence": "CC BY 4.0"},
    {"id": "hdr", "name": "Human Development Report", "by": "UNDP", "licence": "CC BY 3.0 IGO"},
    {"id": "migstock", "name": "International Migrant Stock 2020", "by": "UN DESA Population Division", "licence": "CC BY 3.0 IGO"},
    {"id": "ghe", "name": "Global Health Estimates 2021", "by": "World Health Organization", "licence": "CC BY 4.0"},
    {"id": "whr", "name": "World Happiness Report 2024 (Figure 2.1 data)", "by": "Wellbeing Research Centre, University of Oxford, with Gallup and the UN SDSN", "licence": "free to download with citation (no licence named)"},
    {"id": "cmo", "name": "Commodity Price Data (The Pink Sheet), annual 1960-2025", "by": "World Bank", "licence": "CC BY 4.0"},
    {"id": "oecdhp", "name": "Analytical house prices: real house price index, annual 1970-2025", "by": "OECD", "licence": "CC BY 4.0"},
    {"id": "national", "name": "National statistics offices", "by": "as named in the note", "licence": "reuse with credit"},
]


class LocalReject(Exception):
    """A local rule refused the command before anything was sent."""


class NotConnected(Exception):
    """No credential surrogate is available, so nothing was sent."""


class StepUnknown(Exception):
    """A step was sent and its outcome is unknown. Never resend it."""


class UpstreamError(Exception):
    """The engine or transport answered with a definitive failure."""

    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


# --------------------------------------------------------------------------
# State storage: 0700 directories, 0600 files, atomic replacement, flock.
# --------------------------------------------------------------------------

def state_dir():
    override = os.environ.get("LJ_STATE_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), "workspace", "life-journey", "state")


def ensure_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def atomic_write_bytes(path, data):
    """Write data to path atomically with 0600 permissions."""
    directory = os.path.dirname(path)
    ensure_dir(directory)
    tmp = os.path.join(
        directory, ".tmp-%d-%s" % (os.getpid(), os.urandom(6).hex())
    )
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def atomic_write_json(path, obj):
    atomic_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))


def append_jsonl(path, obj):
    ensure_dir(os.path.dirname(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(obj, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return rows


class StateLock:
    """Exclusive flock held for the whole mutating command."""

    def __init__(self, root):
        self.root = root
        self.handle = None

    def __enter__(self):
        ensure_dir(self.root)
        fd = os.open(os.path.join(self.root, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.handle = os.fdopen(fd, "r+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
        return False


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_key():
    return date.today().isoformat()


def token_fingerprint(token):
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Config and counters (local self caps, checked before anything is sent).
# --------------------------------------------------------------------------

def load_config(root):
    config = read_json(os.path.join(root, "config.json"), None)
    if not isinstance(config, dict):
        config = dict(DEFAULT_CONFIG)
        atomic_write_json(os.path.join(root, "config.json"), config)
        return config
    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    return merged


def load_counters(root):
    counters = read_json(os.path.join(root, "counters.json"), None)
    if not isinstance(counters, dict):
        counters = {"days": {}}
    counters.setdefault("days", {})
    return counters


def save_counters(root, counters):
    atomic_write_json(os.path.join(root, "counters.json"), counters)


def counter_today(counters):
    return counters["days"].setdefault(today_key(), {"starts": 0, "steps": 0})


# --------------------------------------------------------------------------
# Life records.
# --------------------------------------------------------------------------

def lives_root(root):
    return os.path.join(root, "lives")


def life_dir(root, life_id):
    return os.path.join(lives_root(root), life_id)


def life_path(root, life_id):
    return os.path.join(life_dir(root, life_id), "life.json")


def display_path(root, life_id):
    return os.path.join(life_dir(root, life_id), "display.json")


def sync_dir(root):
    return os.path.join(root, "sync")


def synced_snapshot_path(root, life_id):
    return os.path.join(sync_dir(root), "%s.json" % life_id)


def pending_delta_path(root, life_id):
    return os.path.join(sync_dir(root), "%s.delta.json" % life_id)


def load_index(root):
    index = read_json(os.path.join(root, "index.json"), None)
    if not isinstance(index, dict):
        index = {"lives": [], "current_life_id": None}
    index.setdefault("lives", [])
    index.setdefault("current_life_id", None)
    return index


def save_index(root, index):
    atomic_write_json(os.path.join(root, "index.json"), index)


def load_life(root, life_id):
    life = read_json(life_path(root, life_id), None)
    if not isinstance(life, dict):
        raise LocalReject("life not found: %s" % life_id)
    return ensure_life_fields(life)


def ensure_life_fields(life):
    """Fill in counters / revision / incident fields on older records.

    step_count has always meant the number of steps the engine confirmed,
    so a record written before the counters existed seeds both intents and
    confirmed from it, with no unknowns.
    """
    counters = life.get("counters")
    if not isinstance(counters, dict):
        base = int(life.get("step_count", 0) or 0)
        counters = {
            "step_intents": base,
            "confirmed_steps": base,
            "unknown_steps": 0,
        }
    for key in ("step_intents", "confirmed_steps", "unknown_steps"):
        counters.setdefault(key, 0)
    life["counters"] = counters
    life.setdefault("revision", 0)
    life.setdefault("chapters", [])
    life.setdefault("epilogue", None)
    life.setdefault("last_incident", None)
    life.setdefault("unknown_step", None)
    # The play settings agreed with the master at the life's start
    # (pace, ask mode, lost-step policy) live on the life's own
    # record, so they survive a change of chat; older records get
    # the defaults filled in.
    settings = life.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    settings.setdefault("pace", "watch")
    settings.setdefault("ask_mode", False)
    settings.setdefault("lost_step_policy", "ask")
    life["settings"] = settings
    return life


def bump_revision(life):
    """Advance the per-life revision on a confirmed state or status change."""
    life["revision"] = int(life.get("revision", 0) or 0) + 1


def save_life(root, life):
    atomic_write_json(life_path(root, life["life_id"]), life)


def resolve_life_id(root, wanted):
    index = load_index(root)
    life_id = wanted or index.get("current_life_id")
    if not life_id:
        raise LocalReject("no life has been started yet")
    if life_id not in index["lives"]:
        raise LocalReject("life not found: %s" % life_id)
    return life_id


def normalize_death(death):
    """The engine's death record names the place "where" (its public
    API documents death.age / death.cause / death.where, and real
    ending responses confirm it); the display contract calls it
    "place". Normalize at intake -- and again when a display is built
    from an older stored snapshot -- so the place the viewer renders
    is the one the engine sent. The raw key never reaches a display."""
    if not isinstance(death, dict):
        return death
    death = dict(death)
    if "place" not in death and "where" in death:
        death["place"] = death["where"]
    death.pop("where", None)
    return death


def snapshot_of(response):
    """Keep the engine response for the record, without its token."""
    return {
        "state": response.get("state") or {},
        "events": response.get("events") or [],
        "decision": response.get("decision"),
        "focus": response.get("focus"),
        "sheet": response.get("sheet") or [],
        "ended": bool(response.get("ended")),
        "death": normalize_death(response.get("death")),
        "meta": response.get("meta") or {},
        "usage": response.get("usage"),
    }


# --------------------------------------------------------------------------
# Display JSON (schema v1). Never carries a token or a credential.
# Animation comes only from structured signals, never from event wording.
# --------------------------------------------------------------------------

SECRET_SHAPE = re.compile(
    r"k8b_[A-Za-z0-9_\-]{8,}"      # an engine API key
    r"|[A-Za-z0-9_\-]{120,}"       # a resume-token-shaped long run
)


def assert_no_secrets(obj, secrets):
    """Recursively refuse a payload that mentions a secret.

    Three layers: a key literally named like a credential, the exact
    secret values held for this life, and credential-shaped text
    anywhere in a string (a surrogate marker mid-sentence, an API key
    pasted into free text such as a reason, a token-shaped long run).
    """
    secret_values = [s for s in secrets if s]

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in ("token", "authorization", "api_key", "apikey"):
                    raise AssertionError("secret key leaked into display at %s" % path)
                walk(value, path + "/" + str(key))
        elif isinstance(node, (list, tuple)):
            for i, value in enumerate(node):
                walk(value, "%s/%d" % (path, i))
        elif isinstance(node, str):
            for secret in secret_values:
                if secret and secret in node:
                    raise AssertionError("secret value leaked into display at %s" % path)
            if "hsurr:" in node:
                raise AssertionError("credential surrogate leaked into display at %s" % path)
            if SECRET_SHAPE.search(node):
                raise AssertionError("credential-shaped text in display at %s" % path)

    walk(obj, "")


def classify_animation(prev_state, snapshot):
    """Map structured signals to an animation, most specific first.

    Returns (kind, signal, health_trend). The delta is measured against
    the last confirmed snapshot, on the engine's 0 to 100 scale. The
    happy/sad judgement looks only at the happiness delta: strictly
    beyond +/-8 counts, and a missing value falls back to idle. A health
    change never moves the emotion; it is reported separately as
    health_trend (up, down or flat). Priority: ended, then a country
    change (walk), then a waiting decision (thinking), then the
    happiness delta (happy/sad), then idle.
    """
    state = snapshot.get("state") or {}

    def delta(key):
        old = (prev_state or {}).get(key)
        new = state.get(key)
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            return new - old
        return None

    health_delta = delta("health")
    if health_delta is None or health_delta == 0:
        health_trend = "flat"
    elif health_delta > 0:
        health_trend = "up"
    else:
        health_trend = "down"

    if snapshot.get("ended"):
        return "ended", "ended", health_trend
    prev_country = (prev_state or {}).get("country")
    if prev_country and state.get("country") and state.get("country") != prev_country:
        return "walk", "country_changed", health_trend
    if snapshot.get("decision"):
        return "thinking", "decision_pending", health_trend
    happiness_delta = delta("happiness")
    if happiness_delta is not None and happiness_delta > 8:
        return "happy", "happiness_delta", health_trend
    if happiness_delta is not None and happiness_delta < -8:
        return "sad", "happiness_delta", health_trend
    return "idle", "none", health_trend


def scene_of(state):
    age = state.get("age")
    if isinstance(age, (int, float)):
        if age < 7:
            return "home"
        if age <= 18:
            return "school"
        if age >= 65:
            return "elder"
    if state.get("job"):
        return "work"
    return "home"


def build_display(life, choice_entries=None, key_acquired_on=None):
    snapshot = life.get("snapshot") or {}
    state = snapshot.get("state") or {}
    kind, signal, health_trend = classify_animation(life.get("prev_state"), snapshot)
    person = {
        "name": state.get("name"),
        "sex": state.get("sex"),
        "age": state.get("age"),
        "year": state.get("year"),
        "country": state.get("country"),
        "place": state.get("place"),
    }
    if snapshot.get("ended"):
        # The death record's age is authoritative for a finished
        # life: an ending answer that carries no (or a stale) state
        # would otherwise leave the person at their pre-ending age.
        death_age = (snapshot.get("death") or {}).get("age")
        if isinstance(death_age, (int, float)):
            person["age"] = death_age
    state_view = {
        "job": state.get("job"),
        "education": state.get("education"),
        "income": state.get("income"),
        "net_worth": state.get("net_worth"),
        "health": state.get("health"),
        "happiness": state.get("happiness"),
        "knowledge": state.get("knowledge"),
        "bonds": state.get("bonds"),
        "partner": state.get("partner"),
        "children": state.get("children", state.get("kids")),
    }
    decision = snapshot.get("decision")
    if decision:
        decision = {
            "id": decision.get("id"),
            "question": decision.get("question"),
            "about": decision.get("about"),
            "fact": decision.get("fact"),
            "choices": [
                {"key": c.get("key"), "label": c.get("label"), "note": c.get("note")}
                for c in (decision.get("choices") or [])
            ],
        }
    display = {
        "schema_version": DISPLAY_SCHEMA_VERSION,
        "life_id": life["life_id"],
        "status": life["status"],
        "updated_at": now_iso(),
        "engine": (snapshot.get("meta") or {}).get("engine") or life.get("engine"),
        "person": person,
        "state": state_view,
        "events": snapshot.get("events") or [],
        "timeline": life.get("timeline") or [],
        "choices": list(choice_entries or []),
        "chapters": list(life.get("chapters") or []),
        "decision": decision,
        "focus": snapshot.get("focus"),
        "sheet": snapshot.get("sheet") or [],
        "ended": bool(snapshot.get("ended")),
        "death": normalize_death(snapshot.get("death")),
        "last_choice": life.get("last_choice"),
        "step_count": life.get("step_count", 0),
        "counters": dict(life.get("counters") or {}),
        "revision": life.get("revision", 0),
        "last_incident": life.get("last_incident"),
        "epilogue": life.get("epilogue"),
        "animation": {
            "kind": kind, "signal": signal,
            "health_trend": health_trend, "scene": scene_of(state),
        },
        "usage": snapshot.get("usage"),
        "key_acquired_on": key_acquired_on,
        "credit": CREDIT_LINE,
        "credit_url": "https://1in8b.life",
        "sources": SOURCE_CREDITS,
        "disclaimer": DISCLAIMER,
    }
    assert_no_secrets(display, [life.get("token")])
    return display


def choice_entry_from_row(row):
    """One display choices entry from a history "choice" row.

    Rows written before the decision history was recorded in full
    carry no age, question, about, fact or label; those fields stay
    None so a viewer can tell a legacy entry from a fully recorded
    one. The scene (timeline length and state as faced) is likewise
    absent on rows written before it was captured.
    """
    return {
        "step": row.get("step"),
        "age": row.get("age"),
        "decision_id": row.get("decision_id"),
        "question": row.get("question"),
        "about": row.get("about"),
        "fact": row.get("fact"),
        "options": row.get("options"),
        "choose": row.get("choose"),
        "label": row.get("label"),
        "reason": row.get("reason"),
        "scene": row.get("scene"),
        "at": row.get("at"),
    }


def provably_confirmed_step(root, life_id, rows, confirmed,
                            also_confirmed=()):
    """The one step a crash can leave unjournaled, when provable.

    cmd_choose appends the history row, then saves the life with the
    confirmed counters and last_choice advanced, and only then
    appends the journal's "confirmed" record. A process killed in
    that last gap leaves a step the engine did confirm -- the saved
    record proves it -- with no journal record. The proof is exact,
    and every part of it is required: a history choice row for the
    step; the saved record's last_choice naming that same step; and
    the confirmed-step count exactly one ahead of the journal's
    confirmed set (a fork's re-choice carries a higher intent number
    than the confirmed count, so a count alone proves nothing, and
    last_choice alone could name a step whose save never landed).
    Anything short of the full proof restores nothing. The step a
    choose is confirming right now (also_confirmed) is excluded: its
    own journal record lands a moment later.
    """
    try:
        life = load_life(root, life_id)
    except Exception:
        return None
    last_choice = life.get("last_choice") or {}
    step = last_choice.get("step")
    if not isinstance(step, int):
        return None
    if step in confirmed or step in set(also_confirmed):
        return None
    if not any(row.get("type") == "choice" and row.get("step") == step
               for row in rows):
        return None
    if int(life.get("step_count", 0) or 0) != len(confirmed) + 1:
        return None
    return step


def read_choice_entries(root, life_id, also_confirmed=()):
    """Every confirmed choice of a life, in step order, for the display.

    A choice row is appended to the history just before the confirmed
    state is saved (the display built by that save must already
    include it), so a process killed in between would leave a row for
    a step that never confirmed. The journal's "confirmed" records
    are the authority: only rows whose step the journal confirms are
    shown. (Steps are intent numbers, so after a fork a confirmed
    entry's step can exceed the confirmed count -- set membership,
    not a threshold, is the test.) The one exception is the choose
    call saving the step right now: it holds the engine's answer in
    hand, so it passes that step as also_confirmed for the display it
    builds; the journal record that follows makes it permanent.
    One repair runs before the filter: a step the saved record
    proves was confirmed, whose journal record alone was lost to a
    crash in that last gap, has the record restored (marked
    "restored") -- see provably_confirmed_step."""
    base = life_dir(root, life_id)
    rows = read_jsonl(os.path.join(base, "history.jsonl"))
    entries = [choice_entry_from_row(row) for row in rows
               if row.get("type") == "choice"]
    journal_path = os.path.join(base, "journal.jsonl")
    confirmed = {row.get("step") for row in
                 read_jsonl(journal_path)
                 if row.get("type") == "confirmed"}
    restored = provably_confirmed_step(root, life_id, rows, confirmed,
                                       also_confirmed)
    if restored is not None:
        # The saved record proves this step was confirmed; only its
        # journal record was lost to the crash. Restore the record
        # (marked as restored) so the journal tells the same truth
        # as the record, instead of the display silently dropping a
        # choice the recap still shows.
        last_choice = (load_life(root, life_id).get("last_choice")
                       or {})
        append_jsonl(journal_path,
                     {"type": "confirmed", "step": restored,
                      "choose": last_choice.get("choose"),
                      "restored": True, "at": now_iso()})
        confirmed.add(restored)
    confirmed |= set(also_confirmed)
    entries = [entry for entry in entries
               if entry.get("step") in confirmed]
    entries.sort(key=lambda entry: entry.get("step") or 0)
    return entries


def refresh_display(root, life, also_confirmed=()):
    display = build_display(
        life, read_choice_entries(root, life["life_id"], also_confirmed),
        key_acquired_on=load_config(root).get("key_acquired_on"))
    atomic_write_json(os.path.join(life_dir(root, life["life_id"]), "display.json"), display)
    return display


def choices_since_chapter(life):
    """Confirmed choices not yet covered by a chapter note."""
    chapters = life.get("chapters") or []
    covered = int(chapters[-1]["to_step"]) if chapters else 0
    return max(0, int(life.get("step_count", 0) or 0) - covered)


def public_summary(life, display=None):
    snapshot = life.get("snapshot") or {}
    state = snapshot.get("state") or {}
    since_chapter = choices_since_chapter(life)
    return {
        "ok": True,
        "life_id": life["life_id"],
        "status": life["status"],
        "token_fingerprint": token_fingerprint(life.get("token")),
        "name": state.get("name"),
        "age": state.get("age"),
        "country": state.get("country"),
        "step_count": life.get("step_count", 0),
        "counters": dict(life.get("counters") or {}),
        "revision": life.get("revision", 0),
        "ended": bool(snapshot.get("ended")),
        "decision_pending": bool(snapshot.get("decision")),
        "animation": (display or {}).get("animation"),
        "settings": dict(life.get("settings") or {}),
        "choices_since_chapter": since_chapter,
        # Chapters are written every five choices; the flag lets a
        # player (or a fresh chat resuming the life) notice a
        # chapter is owed without counting by hand.
        "chapter_due": since_chapter >= 5,
    }


# --------------------------------------------------------------------------
# Mock engine. Deterministic and token-contained, for key-free testing.
# --------------------------------------------------------------------------

MOCK_COUNTRIES = ["JPN", "KOR", "BRA", "NGA", "IND", "FRA"]
MOCK_NAMES = {
    "JPN": ["Haru Sato", "Yui Tanaka", "Ren Suzuki"],
    "KOR": ["Jiwoo Kim", "Minjun Park", "Seoyeon Lee"],
    "BRA": ["Lucas Silva", "Maria Santos", "Pedro Costa"],
    "NGA": ["Adaeze Obi", "Chidi Eze", "Ngozi Nwosu"],
    "IND": ["Aarav Patel", "Priya Sharma", "Rohan Gupta"],
    "FRA": ["Louis Martin", "Emma Bernard", "Hugo Dubois"],
}
MOCK_DECISIONS = [
    {
        "at_age": 6, "id": "school_start",
        "question": "小学校に通い始める。どんなふうに過ごす？",
        "about": "入学", "fact": "モック用の決断カードです。実APIの文章ではありません。",
        "choices": [
            {"key": "enjoy", "label": "元気に遊んで学ぶ", "note": "友達が増える"},
            {"key": "focus", "label": "勉強に集中する", "note": "知識が伸びる"},
        ],
    },
    {
        "at_age": 15, "id": "high_school",
        "question": "進学先の高校をどうする？",
        "about": "進学", "fact": "モック用の決断カードです。実APIの文章ではありません。",
        "choices": [
            {"key": "local", "label": "地元の高校へ", "note": "穏やかな毎日"},
            {"key": "advanced", "label": "進学校を目指す", "note": "勉強は大変"},
        ],
    },
    {
        "at_age": 18, "id": "after_school",
        "question": "高校を出たあとの道は？",
        "about": "進路", "fact": "モック用の決断カードです。実APIの文章ではありません。",
        "choices": [
            {"key": "university", "label": "大学へ進む", "note": "知識と出会い"},
            {"key": "vocational", "label": "専門学校で技術を学ぶ", "note": "手に職"},
            {"key": "work", "label": "すぐ働く", "note": "収入は早い"},
        ],
    },
    {
        "at_age": 23, "id": "first_job",
        "question": "最初の仕事を選ぶ。",
        "about": "就職", "fact": "モック用の決断カードです。実APIの文章ではありません。",
        "choices": [
            {"key": "office", "label": "会社の仕事", "note": "安定"},
            {"key": "craft", "label": "ものづくりの仕事", "note": "技術が残る"},
            {"key": "care", "label": "人を支える仕事", "note": "絆が深まる"},
        ],
    },
    {
        "at_age": 28, "id": "partner",
        "question": "パートナーとの将来をどうする？",
        "about": "恋愛・結婚", "fact": "モック用の決断カードです。実APIの文章ではありません。",
        "choices": [
            {"key": "marry", "label": "結婚する", "note": "家族が増える"},
            {"key": "wait", "label": "もう少し待つ", "note": "様子を見る"},
            {"key": "single", "label": "一人の道を行く", "note": "自由な時間"},
        ],
    },
    {
        "at_age": 34, "id": "move",
        "question": "住む場所の誘いがある。どうする？",
        "about": "移住", "fact": "モック用の決断カードです。実APIの文章ではありません。",
        "choices": [
            {"key": "stay", "label": "今の場所に残る", "note": "落ち着いた生活"},
            {"key": "city", "label": "都会へ出る", "note": "機会は増える"},
            {"key": "abroad", "label": "外国へ移る", "note": "大きな変化"},
        ],
    },
    {
        "at_age": 46, "id": "midlife",
        "question": "40代半ば。力の入れ方を選び直す。",
        "about": "中年期", "fact": "モック用の決断カードです。実APIの文章ではありません。",
        "choices": [
            {"key": "steady", "label": "堅実に続ける", "note": "安定"},
            {"key": "challenge", "label": "新しい挑戦をする", "note": "幸福は揺れる"},
            {"key": "health_first", "label": "体を大事にする", "note": "健康が戻る"},
        ],
    },
    {
        "at_age": 60, "id": "late_work",
        "question": "60歳。働き方をどうする？",
        "about": "老いへの入り口", "fact": "モック用の決断カードです。実APIの文章ではありません。",
        "choices": [
            {"key": "retire", "label": "引退して穏やかに", "note": "体は休まる"},
            {"key": "continue", "label": "働き続ける", "note": "体には負担"},
        ],
    },
]
MOCK_EFFECTS = {
    ("school_start", "enjoy"): {"happiness": 6, "bonds": 8},
    ("school_start", "focus"): {"knowledge": 10},
    ("high_school", "local"): {"happiness": 4, "bonds": 4},
    ("high_school", "advanced"): {"knowledge": 10, "happiness": -4},
    ("after_school", "university"): {"knowledge": 20, "education": "university"},
    ("after_school", "vocational"): {"knowledge": 10, "education": "vocational"},
    ("after_school", "work"): {"income": 9000, "education": "upper_secondary"},
    ("first_job", "office"): {"job": "office_worker", "income": 24000},
    ("first_job", "craft"): {"job": "craftsperson", "income": 21000, "knowledge": 6},
    ("first_job", "care"): {"job": "care_worker", "income": 20000, "bonds": 8},
    ("partner", "marry"): {"partner": "present", "children": 2, "bonds": 12, "happiness": 8},
    ("partner", "wait"): {"partner": "present", "children": 1, "bonds": 6},
    ("partner", "single"): {"happiness": 3, "knowledge": 4},
    ("move", "stay"): {"happiness": 3},
    ("move", "city"): {"income": 8000, "happiness": 4},
    ("move", "abroad"): {"income": 6000, "happiness": -3},
    ("midlife", "steady"): {"income": 4000},
    ("midlife", "challenge"): {"happiness": -10, "income": 9000, "knowledge": 6},
    ("midlife", "health_first"): {"health": 14, "happiness": 4},
    ("late_work", "retire"): {"health": 8, "income": -6000, "happiness": 5},
    ("late_work", "continue"): {"health": -14, "income": 5000},
}
MOCK_CAUSES = ["natural", "heart", "cancer", "stroke"]


def mock_token_encode(state):
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "mock1." + base64.urlsafe_b64encode(raw).decode("ascii")


def mock_token_decode(token):
    if not isinstance(token, str) or not token.startswith("mock1."):
        raise LocalReject("mock token is missing or has an unknown format")
    try:
        raw = base64.urlsafe_b64decode(token[6:].encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise LocalReject("mock token cannot be read") from exc


def mock_public_state(m):
    return {
        "name": m["name"], "sex": m["sex"], "age": m["age"],
        "year": 2026 + m["age"], "country": m["country"], "place": m["place"],
        "job": m["job"], "education": m["education"], "income": m["income"],
        "net_worth": m["income"] * 2, "health": m["health"],
        "happiness": m["happiness"], "knowledge": m["knowledge"],
        "bonds": m["bonds"], "partner": m["partner"], "children": m["children"],
    }


def mock_response(m, events, units):
    idx = m["idx"]
    decision = None
    if not m["ended"] and idx < len(MOCK_DECISIONS):
        decision = MOCK_DECISIONS[idx]
    return {
        "state": mock_public_state(m),
        "events": events,
        "decision": decision,
        "focus": None,
        "sheet": [],
        "ended": m["ended"],
        "death": ({"age": m["death_age"], "cause": m["death_cause"],
                   "where": m["country"]} if m["ended"] else None),
        # The real engine returns a null token on the step that ends a
        # life; mirror that shape so the ending path is tested as served.
        "token": (None if m["ended"] else mock_token_encode(m)),
        "meta": {"engine": MOCK_ENGINE_VERSION, "simulated": DISCLAIMER},
        "usage": {"this_call": units},
    }


def mock_start(payload):
    fault = os.environ.get("LJ_MOCK_FAULT")
    if fault == "notconnected":
        # The real transport raises NotConnected before any network
        # call (credentials resolve first); mirror that here.
        raise NotConnected("mock fault injection: not connected")
    if fault == "timeout":
        raise TimeoutError("mock fault injection: timeout")
    if fault == "http500":
        raise UpstreamError(
            "mock fault injection: HTTP 500", code="internal", status=500
        )
    if fault == "http503":
        raise UpstreamError(
            "mock fault injection: HTTP 503", code="busy", status=503
        )
    if fault == "http503bare":
        raise UpstreamError(
            "mock fault injection: HTTP 503 without a code",
            code=None, status=503
        )
    if fault == "http500busy":
        raise UpstreamError(
            "mock fault injection: HTTP 500 with a busy code",
            code="busy", status=500
        )
    if fault == "leaky":
        raise UpstreamError(
            "mock fault injection: engine exploded near k8b_" + "x" * 24,
            code="bad_request", status=400)
    import random as _random

    seed = payload.get("seed")
    if seed is None:
        seed = int.from_bytes(os.urandom(4), "big")
    rng = _random.Random(int(seed))
    country = rng.choice(MOCK_COUNTRIES)
    name = rng.choice(MOCK_NAMES[country])
    death_age = 68 + rng.randrange(20)
    m = {
        "v": 1, "seed": int(seed), "idx": 0, "age": MOCK_DECISIONS[0]["at_age"],
        "country": country, "birth_country": country, "name": name,
        "sex": rng.choice(["female", "male"]), "place": rng.choice(["town", "village"]),
        "job": None, "education": "primary", "income": 0,
        "health": 90, "happiness": 70, "knowledge": 10, "bonds": 40,
        "partner": None, "children": 0,
        "ended": False, "death_age": death_age,
        "death_cause": MOCK_CAUSES[rng.randrange(len(MOCK_CAUSES))],
    }
    events = [
        [0, "%sに生まれた（モック）" % country],
        [3, "近所の子と遊ぶようになった（モック）"],
        [6, "小学校に入学した（モック）"],
    ]
    response = mock_response(m, events, 1)
    if fault == "badjson":
        # A success body whose token never arrived intact: the start
        # may have been applied, so its outcome is unknown.
        response = dict(response)
        response.pop("token", None)
    return response


def mock_step(m, payload):
    fault = os.environ.get("LJ_MOCK_FAULT")
    if fault == "notconnected":
        # The real transport raises NotConnected before any network
        # call (credentials resolve first); mirror that here.
        raise NotConnected("mock fault injection: not connected")
    if fault == "timeout":
        raise TimeoutError("mock fault injection: timeout")
    if fault == "http500":
        raise UpstreamError(
            "mock fault injection: HTTP 500", code="internal", status=500
        )
    if fault == "http503":
        raise UpstreamError(
            "mock fault injection: HTTP 503", code="busy", status=503
        )
    if fault == "http503bare":
        raise UpstreamError(
            "mock fault injection: HTTP 503 without a code",
            code=None, status=503
        )
    if fault == "http500busy":
        raise UpstreamError(
            "mock fault injection: HTTP 500 with a busy code",
            code="busy", status=500
        )
    if fault == "leaky":
        # A definitive refusal whose free-text message carries a fake
        # key: the CLI output must never echo upstream text.
        raise UpstreamError(
            "mock fault injection: engine exploded near k8b_" + "x" * 24,
            code="bad_request", status=400)
    if m["ended"]:
        raise UpstreamError("the mock life has already ended", code="live_state")
    idx = m["idx"]
    if idx >= len(MOCK_DECISIONS):
        raise UpstreamError("no mock decision is waiting", code="live_state")
    card = MOCK_DECISIONS[idx]
    key = payload.get("choose")
    valid = [c["key"] for c in card["choices"]]
    if key not in valid:
        raise UpstreamError(
            "choice %r does not fit the waiting decision" % key, code="live_state"
        )
    effects = MOCK_EFFECTS.get((card["id"], key), {})
    for field in ("health", "happiness", "knowledge", "bonds", "income"):
        if field in effects:
            m[field] = m[field] + effects[field]
    for field in ("job", "education", "partner", "children"):
        if field in effects:
            m[field] = effects[field]
    if card["id"] == "move" and key == "abroad":
        others = [c for c in MOCK_COUNTRIES if c != m["country"]]
        m["country"] = others[m["seed"] % len(others)]
    m["idx"] = idx + 1
    if m["idx"] < len(MOCK_DECISIONS):
        nxt = MOCK_DECISIONS[m["idx"]]
        m["age"] = nxt["at_age"]
        events = [
            [card["at_age"], "決断した: %s（モック）" % card["id"]],
            [nxt["at_age"], "次の節目が近づいてきた（モック）",
             "出典: モック統計年鑑（3要素目の保持テスト用）"],
        ]
    else:
        m["age"] = m["death_age"]
        m["ended"] = True
        events = [
            [card["at_age"], "決断した: %s（モック）" % card["id"]],
            [m["death_age"], "人生の終わりを迎えた（モック）"],
        ]
    for field in ("health", "happiness", "knowledge", "bonds"):
        m[field] = max(0, min(100, m[field]))
    response = mock_response(m, events, 1)
    if fault == "badjson":
        # Simulate a success answer whose new token never arrived intact.
        response = dict(response)
        response.pop("token", None)
    if fault == "badend" and m["ended"]:
        # Simulate a broken ending: ended is true but the death record
        # never arrived. The helper must treat this as unknown, never
        # as a confirmed ending.
        response = dict(response)
        response.pop("death", None)
    return response


def mock_view(m):
    return mock_response(m, [], 0)


# --------------------------------------------------------------------------
# Real transport. POST /api/v1/live only, fixed host, no redirects.
# The credential surrogate is fetched before any network connection, so a
# missing registration stops the command as not_connected with no call made.
# --------------------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpstreamError("redirect refused (HTTP %d)" % code, code="redirect")


def read_capped(response):
    chunks = []
    total = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise UpstreamError("response exceeded the size cap", code="too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def real_post_live(payload):
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    try:
        from dynamic_credentials import add_surrogate_to_request
    except Exception as exc:
        raise NotConnected("credential helper is unavailable") from exc
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        LIVE_URL, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 # The engine sits behind Cloudflare, which rejects the
                 # default Python-urllib signature with a bare 403
                 # (error code 1010, measured 2026-11-12). A browser
                 # user agent is required to reach the API at all.
                 "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/126.0 Safari/537.36"},
    )
    try:
        add_surrogate_to_request(request, CREDENTIAL, allowed_hosts=ALLOWED_HOSTS)
    except Exception as exc:
        # No surrogate means no registered key. Nothing has been sent.
        raise NotConnected("no credential is registered for custom.1in8b") from exc
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = read_capped(response)
    except urllib.error.HTTPError as exc:
        raw = read_capped(exc)
        code = None
        message = "HTTP %d" % exc.code
        try:
            parsed = json.loads(raw.decode("utf-8"))
            code = parsed.get("code")
            message = parsed.get("error") or message
        except Exception:
            pass
        raise UpstreamError(message, code=code, status=exc.code) from exc
    except (TimeoutError, ConnectionError, OSError) as exc:
        # Includes socket timeouts and dropped connections. The step may or
        # may not have been applied, so the caller treats this as unknown.
        raise StepUnknown("transport failed after the step was sent") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise UpstreamError(
            "engine response was not valid JSON",
            code="invalid_response", status=200,
        ) from exc


def real_get_catalog(name):
    url = CATALOG_URL + name
    request = urllib.request.Request(
        url, method="GET",
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/126.0 Safari/537.36"},
    )
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = read_capped(response)
    except urllib.error.HTTPError as exc:
        raise UpstreamError("catalog HTTP %d" % exc.code, status=exc.code) from exc
    except (TimeoutError, ConnectionError, OSError) as exc:
        raise UpstreamError("catalog transport failed") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise UpstreamError("catalog response was not valid JSON") from exc


def use_mock():
    return os.environ.get("LJ_MOCK") == "1"


# Only narrow HTTP answers prove a step was NOT applied: a 400 whose code
# is bad_request, not_in_world or token (the request itself was refused;
# code "token" is the engine's real answer for a token it did not make or
# one older than 90 days, measured 2026-11-12), and a 429 rate/month
# refusal (the call was turned away at the gate). A 401 or
# 403 is a definitive credential failure. Everything else -- a 5xx, a
# relay error, too_much_work (which may still count processed work), a
# success status with a broken or token-less body -- leaves the outcome
# unknown: the step may have been applied and its new token lost.
UNKNOWN_ERROR_CODES = ("too_much_work", "invalid_response", "redirect", "too_large")


def classify_step_error(exc):
    """Sort an UpstreamError from a step into rejected / fatal / unknown."""
    status, code = exc.status, exc.code
    if code in UNKNOWN_ERROR_CODES:
        return "unknown"
    if status in (401, 403):
        return "fatal"
    if status == 400 and code in ("bad_request", "not_in_world", "token"):
        return "rejected"
    if status == 429 and code in ("rate", "month"):
        return "rejected"
    if status is None:
        # A definitive answer generated locally by the mock engine.
        return "rejected"
    return "unknown"


def engine_start(payload):
    if use_mock():
        return mock_start(payload)
    return real_post_live(payload)


def engine_step(token, payload):
    if use_mock():
        return mock_step(mock_token_decode(token), payload)
    body = dict(payload)
    body["token"] = token
    return real_post_live(body)


def engine_view(token):
    if use_mock():
        return mock_view(mock_token_decode(token))
    return real_post_live({"token": token, "view": True})


# --------------------------------------------------------------------------
# Commands.
# --------------------------------------------------------------------------

def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _snapshot_core(snapshot):
    """The parts of a snapshot whose change counts as a state change.

    This is the revision-bump criterion, so it must cover every
    non-volatile display field: state, decision, ending, sheet, focus
    and the usage counters. A change that is real for the viewer but
    missing here would leave the revision standing still, and then
    both a delta (which needs a bump) and a full sync (which the
    viewer rejects as stale) would refuse -- a deadlock. Volatile
    parts stay out: the latest-step events, updated_at and
    usage.this_call.
    """
    snapshot = snapshot or {}
    usage = snapshot.get("usage") or {}
    return {
        "state": snapshot.get("state") or {},
        "decision": snapshot.get("decision"),
        "ended": bool(snapshot.get("ended")),
        "death": snapshot.get("death"),
        "sheet": snapshot.get("sheet") or [],
        "focus": snapshot.get("focus"),
        "usage": {"used": usage.get("used"), "quota": usage.get("quota")},
    }


def apply_response(root, life, response, events_are_new=True,
                   confirmed_step=None):
    """Fold an engine response into the stored life and its display.

    confirmed_step is the step this very call is confirming (a choose
    that holds the engine's answer); it lets the display built here
    include that step's choice row before its journal record lands
    (see read_choice_entries)."""
    old_core = _snapshot_core(life.get("snapshot"))
    old_token = life.get("token")
    if events_are_new:
        # prev_state is the baseline the animation is judged
        # against: the state before the step that produced this
        # answer. A bare view (events_are_new False) returns the
        # current state unchanged, so letting it overwrite the
        # baseline would erase the last step's judgement -- a health
        # trend of "up" decaying to "flat", a happy step decaying to
        # idle -- while the revision stands still: a display change
        # no delta can carry. Views therefore leave the baseline
        # alone, and the animation stays the last step's until the
        # next confirmed step replaces it.
        life["prev_state"] = (life.get("snapshot") or {}).get("state") or {}
    life["snapshot"] = snapshot_of(response)
    if response.get("token"):
        life["token"] = response["token"]
    if _snapshot_core(life["snapshot"]) != old_core or (
        response.get("token") and response["token"] != old_token
    ):
        bump_revision(life)
    meta_engine = (response.get("meta") or {}).get("engine")
    if meta_engine:
        life["engine"] = meta_engine
    if events_are_new:
        for event in response.get("events") or []:
            if isinstance(event, (list, tuple)) and len(event) >= 2:
                # Keep the entry whole: a third element, when the
                # engine sends one, is a sourced explanation and is
                # part of the record -- the display contract carries
                # it on timeline entries, and trimming it here would
                # lose it from the history for good.
                life.setdefault("timeline", []).append(list(event))
            elif isinstance(event, dict):
                entry = [event.get("age"), event.get("text")]
                if event.get("note"):
                    entry.append(event["note"])
                life.setdefault("timeline", []).append(entry)
    if life["snapshot"]["ended"]:
        life["status"] = "ended"
    save_life(root, life)
    return refresh_display(
        root, life,
        also_confirmed=(confirmed_step,) if confirmed_step else ())


def pending_start_path(root):
    return os.path.join(root, "pending_start.json")


def pending_start_state(root):
    """The recorded in-flight start, or None when there is none.

    The marker is written before a start is sent and cleared on every
    outcome the process lives to record, so a marker still present at
    the next invocation means the previous process died mid-send: the
    engine may have created a life whose token was never received.
    An unreadable marker is still unresolved -- fail safe."""
    path = pending_start_path(root)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"unreadable": True}


def clear_pending_start(root):
    try:
        os.unlink(pending_start_path(root))
    except OSError:
        pass


def cmd_start(root, args):
    config = load_config(root)
    pending = pending_start_state(root)
    if args.abandon_pending:
        if pending is None:
            emit({"ok": True, "abandoned_pending_start": None,
                  "message": "no pending start is recorded"})
            return 0
        # Acknowledge the interrupted start deliberately: the send
        # happened exactly once, so it is counted against today's
        # start cap exactly once, here, and journaled.
        append_jsonl(os.path.join(root, "journal.jsonl"),
                     {"type": "start_abandoned", "at": now_iso(),
                      "pending": pending})
        counters = load_counters(root)
        counter_today(counters)["starts"] += 1
        save_counters(root, counters)
        clear_pending_start(root)
        emit({"ok": True, "abandoned_pending_start": pending,
              "message": "the interrupted start was acknowledged: any "
                         "life it created is unrecoverable (its token "
                         "was never received), the lost send was "
                         "counted against today's start cap, and new "
                         "starts are unblocked"})
        return 0
    if pending is not None:
        raise LocalReject(
            "an earlier start was interrupted before its outcome was "
            "recorded (pending start recorded at %s): the engine may "
            "have created a life whose token was never received, so "
            "it cannot be recovered, and a blind new start could "
            "create a second one. Acknowledge it deliberately with "
            "start --abandon-pending, then start again"
            % pending.get("at", "an unknown time"))
    counters = load_counters(root)
    if counter_today(counters)["starts"] >= int(config["max_starts_per_day"]):
        raise LocalReject("local cap reached: max_starts_per_day")
    # One life at a time: a life in progress (active, paused or
    # unknown) blocks a new start. --force is the deliberate override,
    # and it is journaled.
    index = load_index(root)
    blockers = []
    for other_id in index.get("lives", []):
        try:
            other = load_life(root, other_id)
        except Exception:
            continue
        if other.get("status") in ("active", "paused", "unknown"):
            blockers.append("%s (%s)" % (other_id, other["status"]))
    if blockers and not args.force:
        raise LocalReject(
            "a life is already in progress: %s. One life at a time: "
            "finish it first, or pass --force to start another anyway "
            "(the override is journaled)" % ", ".join(blockers))
    if blockers:
        append_jsonl(os.path.join(root, "journal.jsonl"),
                     {"type": "start_forced", "at": now_iso(),
                      "overrides": blockers})
    payload = {"lang": "ja"}
    if args.seed is not None:
        payload["seed"] = args.seed
    # Record the pending start and journal the intent before sending.
    # The marker is cleared on every outcome recorded below, so one
    # left behind means this process died mid-send (see
    # pending_start_state).
    atomic_write_json(pending_start_path(root),
                      {"type": "pending_start", "at": now_iso(),
                       "seed": args.seed})
    append_jsonl(os.path.join(root, "journal.jsonl"),
                 {"type": "start_intent", "at": now_iso(),
                  "seed": args.seed})

    def unknown_start():
        # The start may have been applied by the engine, but its token
        # was never received, so that life can never be recovered.
        # Journal an unknown_start, count the attempt against the
        # start cap, never resend, and stop with exit code 3 -- the
        # same discipline as an unknown step.
        append_jsonl(os.path.join(root, "journal.jsonl"),
                     {"type": "unknown_start", "at": now_iso(),
                      "seed": args.seed})
        counters = load_counters(root)
        counter_today(counters)["starts"] += 1
        save_counters(root, counters)
        clear_pending_start(root)
        emit({
            "ok": False,
            "error": "start_outcome_unknown",
            "message": "the start was sent and its outcome is unknown. A "
                       "life may have been created by the engine, but its "
                       "token was never received, so it cannot be "
                       "recovered. The start was not resent and must not "
                       "be retried blindly.",
        })
        return 3

    try:
        response = engine_start(payload)
    except NotConnected:
        # Credential resolution happens before any network call, so
        # this failure is provably pre-send: no life can have been
        # created. The pending marker has done its job; leaving it
        # behind would block every later start as if this one had
        # died mid-send, and a first-run user whose key is not
        # registered yet would stay blocked until they abandoned a
        # start that was never sent.
        clear_pending_start(root)
        raise
    except (StepUnknown, TimeoutError):
        return unknown_start()
    except UpstreamError as exc:
        # An answer that does not prove the start was refused -- a
        # 5xx, a relay error, a broken success body -- leaves the
        # outcome unknown, exactly like a lost response. Only a
        # narrow refusal or a credential failure is a definitive
        # failure (exit 5).
        if classify_step_error(exc) == "unknown":
            return unknown_start()
        # A definitive refusal: no life was created, so the pending
        # marker has done its job.
        clear_pending_start(root)
        raise
    if not isinstance(response, dict) or not response.get("token"):
        return unknown_start()
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    index = load_index(root)
    life_id = "life-%s-%03d-%s" % (stamp, len(index["lives"]) + 1, os.urandom(2).hex())
    life = {
        "life_id": life_id,
        "token": response["token"],
        "status": "active",
        "step_count": 0,
        "counters": {"step_intents": 0, "confirmed_steps": 0, "unknown_steps": 0},
        "revision": 0,
        "pending_step": None,
        "unknown_step": None,
        "last_incident": None,
        # The settings agreed with the master at this life's start;
        # they live on the record (see the settings command) so a
        # later chat resumes under the same agreement.
        "settings": {
            "pace": args.pace,
            "ask_mode": bool(args.ask_mode),
            "lost_step_policy": args.lost_step_policy,
        },
        "created_at": now_iso(),
        "engine": (response.get("meta") or {}).get("engine"),
        "seed": args.seed,
        "snapshot": {},
        "prev_state": {},
        "timeline": [],
        "last_choice": None,
    }
    ensure_dir(life_dir(root, life_id))
    display = apply_response(root, life, response)
    life = load_life(root, life_id)
    life["birth_country"] = (life.get("snapshot") or {}).get("state", {}).get("country")
    save_life(root, life)
    append_jsonl(os.path.join(life_dir(root, life_id), "journal.jsonl"),
                 {"type": "started", "at": now_iso(),
                  "token_fingerprint": token_fingerprint(life["token"]),
                  "settings": dict(life.get("settings") or {})})
    append_jsonl(os.path.join(life_dir(root, life_id), "history.jsonl"),
                 {"type": "started", "at": now_iso(), "seed": args.seed})
    counters = load_counters(root)
    counter_today(counters)["starts"] += 1
    save_counters(root, counters)
    index = load_index(root)
    index["lives"].append(life_id)
    index["current_life_id"] = life_id
    save_index(root, index)
    clear_pending_start(root)
    out = public_summary(life, display)
    out["display_path"] = os.path.join(life_dir(root, life_id), "display.json")
    emit(out)
    return 0


def cmd_status(root, args, do_view=True):
    life = load_life(root, resolve_life_id(root, args.life))
    warning = None
    if recover_pending_step(root, life):
        life = load_life(root, life["life_id"])
        warning = ("a step whose outcome was never recorded has been "
                   "recovered as unknown; run reconcile to inspect it")
    if do_view and life["status"] in ("active", "paused"):
        try:
            response = engine_view(life["token"])
            display = apply_response(root, life, response, events_are_new=False)
            life = load_life(root, life["life_id"])
        except (UpstreamError, StepUnknown):
            # Fixed text only: upstream messages are free text from
            # outside this process and are never echoed.
            warning = "view failed; showing the last confirmed snapshot"
            display = refresh_display(root, life)
    else:
        display = refresh_display(root, life)
    out = public_summary(life, display)
    out["state"] = display["state"]
    out["person"] = display["person"]
    out["usage"] = display.get("usage")
    if warning:
        out["warning"] = warning
    emit(out)
    return 0


def cmd_choices(root, args):
    life = load_life(root, resolve_life_id(root, args.life))
    recovered = recover_pending_step(root, life)
    if recovered:
        life = load_life(root, life["life_id"])
    snapshot = life.get("snapshot") or {}
    decision = None if recovered else snapshot.get("decision")
    emit({
        "ok": True,
        "life_id": life["life_id"],
        "status": life["status"],
        "decision": decision,
        "focus": snapshot.get("focus"),
        "sheet": snapshot.get("sheet") or [],
    })
    return 0


def unknown_class(detail):
    """Classify a lost step by how it was lost.

    A "gate" loss means the engine answered with its busy response
    (HTTP 503, error code busy): the request was turned away before
    processing, and in the measured busy case such an attempt was
    neither applied nor billed. That is evidence about the class, not
    proof for any single attempt. The busy code must be explicit: a
    bare HTTP 503 without it proves nothing about where the request
    stopped -- an intermediary can produce or swallow a 503 -- so it
    is "response_lost", like every other loss: a timeout, a dropped
    connection, a broken success body, another 5xx. In those cases
    the step may well have been applied and only its answer is
    missing.
    """
    return "gate" if detail == "busy" else "response_lost"


def step_error_class(exc):
    """Classify an upstream step error strictly, from the HTTP status
    AND the error code together: gate only for the engine's explicit
    busy refusal on an HTTP 503. A busy code riding any other status
    (a 500, a 502) came through a relay and proves nothing about
    whether the step was applied, so it is response_lost -- the
    detail string alone cannot carry this distinction, because it
    keeps the code and drops the status."""
    if exc.status == 503 and exc.code == "busy":
        return "gate"
    return "response_lost"


def mark_unknown(root, life, step_no, choose_key, detail=None, cls=None):
    if cls is None:
        cls = unknown_class(detail)
    life["status"] = "unknown"
    life["unknown_step"] = {"step": step_no, "choose": choose_key,
                            "unknown_class": cls}
    incident = {
        "type": "unknown", "step": step_no, "choose": choose_key,
        "at": now_iso(), "unknown_class": cls,
    }
    if detail:
        life["unknown_step"]["detail"] = detail
        incident["detail"] = detail
    life["last_incident"] = incident
    counters = life["counters"]
    counters["unknown_steps"] = int(counters.get("unknown_steps", 0)) + 1
    bump_revision(life)
    save_life(root, life)
    record = {"type": "unknown", "step": step_no, "choose": choose_key,
              "at": now_iso(), "unknown_class": cls}
    if detail:
        record["detail"] = detail
    append_jsonl(os.path.join(life_dir(root, life["life_id"]), "journal.jsonl"),
                 record)
    append_jsonl(os.path.join(life_dir(root, life["life_id"]), "history.jsonl"),
                 record)
    refresh_display(root, life)


def emit_step_unknown(life, step_no, detail=None, cls=None):
    if detail is None:
        detail = (life.get("unknown_step") or {}).get("detail")
    if cls is None:
        cls = (life.get("unknown_step") or {}).get("unknown_class") \
            or unknown_class(detail)
    if cls == "gate":
        message = (
            "the engine answered busy (HTTP 503) and turned the step "
            "away unprocessed, so it was most likely not applied; in "
            "the measured busy case it was not billed either. That is "
            "evidence about this kind of loss, not proof for this "
            "attempt. The step was not resent. Run reconcile to "
            "inspect the last confirmed state; only reconcile "
            "--abandon resumes the life, as a deliberate fork."
        )
    else:
        message = (
            "the step was sent and its outcome is unknown: no intact "
            "answer came back, so the step may have been applied and "
            "its new token lost with the answer. It was not resent. "
            "Run reconcile to inspect the last confirmed state; only "
            "reconcile --abandon resumes the life, as a deliberate "
            "fork."
        )
    emit({
        "ok": False,
        "error": "step_outcome_unknown",
        "life_id": life["life_id"],
        "step": step_no,
        "detail": detail,
        "unknown_class": cls,
        "message": message,
    })
    return 3


def recover_pending_step(root, life):
    """Recover a step whose sender died before recording the outcome.

    cmd_choose saves the intent and the pending marker before it
    sends. If the process is killed after the send but before the
    outcome is saved, the life still reads active with a pending step
    set -- and a later choose would happily send again on the same
    token. The sent step may or may not have been applied, so the
    only honest state is unknown: mark it exactly as an in-process
    loss is marked, and let reconcile --abandon be the way forward.
    Returns True when a recovery ran (the caller reloads the life).
    """
    pending = life.get("pending_step")
    if not pending or life.get("status") != "active":
        return False
    if isinstance(pending, dict):
        step_no = pending.get("step")
        choose_key = pending.get("choose")
    else:
        # Older records store only the step number; the journal's
        # intent entry carries the choice that was sent.
        step_no = pending
        choose_key = None
        journal_path = os.path.join(
            life_dir(root, life["life_id"]), "journal.jsonl")
        for entry in read_jsonl(journal_path):
            if entry.get("type") == "intent" and entry.get("step") == step_no:
                choose_key = entry.get("choose")
    mark_unknown(root, life, step_no, choose_key,
                 detail="recovered_pending")
    return True


def cmd_choose(root, args):
    config = load_config(root)
    life = load_life(root, resolve_life_id(root, args.life))
    if recover_pending_step(root, life):
        life = load_life(root, life["life_id"])
        return emit_step_unknown(life, life["unknown_step"]["step"])
    if life["status"] == "unknown":
        raise LocalReject(
            "life is unknown; run reconcile to inspect it, and only "
            "reconcile --abandon resumes it, as a deliberate fork"
        )
    if life["status"] == "paused":
        raise LocalReject("life is paused; resume it before choosing")
    if life["status"] == "ended":
        raise LocalReject("life has ended; start a new life instead")
    snapshot = life.get("snapshot") or {}
    decision = snapshot.get("decision")
    if not decision:
        raise LocalReject("no decision is waiting for this life")
    valid_keys = [c.get("key") for c in (decision.get("choices") or [])]
    if args.key not in valid_keys:
        raise LocalReject("choice %r is not one of the waiting options" % args.key)
    life_counters = life["counters"]
    if life_counters["step_intents"] >= int(config["max_steps_per_life"]):
        raise LocalReject("local cap reached: max_steps_per_life")
    counters = load_counters(root)
    if counter_today(counters)["steps"] >= int(config["max_steps_per_day"]):
        raise LocalReject("local cap reached: max_steps_per_day")

    # The reason is recorded in the history, the display and the
    # viewer sync, so it is gated here, before anything is written:
    # a reason carrying a credential or a token would otherwise be
    # stored and shipped.
    try:
        assert_no_secrets({"reason": args.reason}, [life.get("token")])
    except AssertionError:
        raise LocalReject(
            "the reason appears to contain a credential or a token; "
            "refusing to record it (reasons are stored and synced)")

    step_no = life_counters["step_intents"] + 1
    journal_path = os.path.join(life_dir(root, life["life_id"]), "journal.jsonl")
    append_jsonl(journal_path,
                 {"type": "intent", "step": step_no, "choose": args.key,
                  "token_fingerprint": token_fingerprint(life.get("token")),
                  "at": now_iso()})
    life_counters["step_intents"] = step_no
    life["pending_step"] = {"step": step_no, "choose": args.key}
    save_life(root, life)
    counters = load_counters(root)
    counter_today(counters)["steps"] += 1
    save_counters(root, counters)

    payload = {"choose": args.key}
    if args.focus:
        payload["focus"] = args.focus
    if args.buy:
        payload["buy"] = args.buy
    try:
        response = engine_step(life["token"], payload)
    except NotConnected:
        # Provably pre-send, exactly as in cmd_start: credentials
        # resolve before any network call, so the step never left
        # this process. Clear the pending marker so a later command
        # does not recover a step that was never sent as unknown.
        # The intent stays counted, and the journal records that it
        # was not sent.
        life["pending_step"] = None
        save_life(root, life)
        append_jsonl(journal_path,
                     {"type": "not_sent", "step": step_no,
                      "choose": args.key, "reason": "not_connected",
                      "at": now_iso()})
        raise
    except StepUnknown:
        mark_unknown(root, life, step_no, args.key, detail="transport")
        return emit_step_unknown(life, step_no, detail="transport")
    except TimeoutError:
        # The mock engine raises a bare TimeoutError for fault injection.
        mark_unknown(root, life, step_no, args.key, detail="timeout")
        return emit_step_unknown(life, step_no, detail="timeout")
    except UpstreamError as exc:
        if classify_step_error(exc) == "unknown":
            # The answer does not prove the step was refused: a 5xx, a
            # relay error, too_much_work, or a broken success body. Treat
            # it exactly like a lost response and never resend it.
            detail = exc.code or ("http_%s" % exc.status)
            cls = step_error_class(exc)
            mark_unknown(root, life, step_no, args.key, detail=detail,
                         cls=cls)
            return emit_step_unknown(life, step_no, detail=detail, cls=cls)
        # A narrow, definitive refusal (see classify_step_error): the
        # step was not applied and the life stays active.
        life["pending_step"] = None
        save_life(root, life)
        append_jsonl(journal_path,
                     {"type": "rejected", "step": step_no, "choose": args.key,
                      "code": exc.code, "status": exc.status, "at": now_iso()})
        raise

    # A step that ends the life answers in a different shape: ended is
    # true, death carries the age, cause and place, the token is null and
    # there is no next decision (confirmed with the engine team on
    # 2026-11-13; the public docs did not mention the null token). That
    # answer is complete and confirmed, not broken, so accept it even
    # without a token. An ending is only complete with a death record
    # whose age is a number (cause and place may be null); an "ending"
    # without one is a broken body and stays unknown, like anything
    # else without a token or a state.
    death = response.get("death") if isinstance(response, dict) else None
    ended_step = (
        isinstance(response, dict)
        and response.get("ended") is True
        and isinstance(death, dict)
        and isinstance(death.get("age"), (int, float))
    )
    if (not isinstance(response, dict)
            or (not response.get("token") and not ended_step)
            or (not isinstance(response.get("state"), dict) and not ended_step)):
        # A success status with a broken body: the step may have been
        # applied and its new token lost with the response. Unknown,
        # never resent.
        mark_unknown(root, life, step_no, args.key, detail="invalid_response")
        return emit_step_unknown(life, step_no, detail="invalid_response")
    if ended_step and not isinstance(response.get("state"), dict):
        # Keep the person's final state on the record when an ending
        # answer carries none.
        response = dict(response)
        response["state"] = (life.get("snapshot") or {}).get("state") or {}

    life["pending_step"] = None
    life_counters["confirmed_steps"] = int(life_counters.get("confirmed_steps", 0)) + 1
    life["step_count"] = life_counters["confirmed_steps"]
    # Record the decision as it was faced -- its question, the chosen
    # option's label and the age at the time -- together with the
    # reason. The engine cannot give these back later (a decision id
    # alone does not reproduce its card), so the choice history and
    # the display's choices array are built from this row. The row is
    # appended BEFORE apply_response refreshes the display, so the
    # new entry is already in the history the display derives from.
    age_at_decision = decision.get("at_age")
    if not isinstance(age_at_decision, (int, float)):
        age_at_decision = (snapshot.get("state") or {}).get("age")
    chosen_label = None
    options = []
    for option in decision.get("choices") or []:
        options.append({"key": option.get("key"),
                        "label": option.get("label")})
        if option.get("key") == args.key:
            chosen_label = option.get("label")
    # The scene as it was faced, captured from the pre-step snapshot:
    # how far the timeline had grown (so a viewer can slice it at
    # exactly this decision) and the person's state at that moment,
    # in the same shape as the display's state view. Like the
    # question and the label, none of this can be recovered later.
    scene_state = snapshot.get("state") or {}
    scene = {
        "timeline_len": len(life.get("timeline") or []),
        "state": {
            "job": scene_state.get("job"),
            "education": scene_state.get("education"),
            "income": scene_state.get("income"),
            "net_worth": scene_state.get("net_worth"),
            "health": scene_state.get("health"),
            "happiness": scene_state.get("happiness"),
            "knowledge": scene_state.get("knowledge"),
            "bonds": scene_state.get("bonds"),
            "partner": scene_state.get("partner"),
            "children": scene_state.get("children", scene_state.get("kids")),
        },
    }
    choice_row = {
        "type": "choice", "step": step_no, "choose": args.key,
        "reason": args.reason, "decision_id": decision.get("id"),
        "age": age_at_decision, "question": decision.get("question"),
        "about": decision.get("about"), "fact": decision.get("fact"),
        "options": options,
        "label": chosen_label, "scene": scene, "at": now_iso(),
    }
    life["last_choice"] = {
        "step": step_no, "choose": args.key, "reason": args.reason,
        "decision_id": decision.get("id"), "at": choice_row["at"],
        "age": age_at_decision, "question": decision.get("question"),
        "about": decision.get("about"), "fact": decision.get("fact"),
        "label": chosen_label,
    }
    append_jsonl(os.path.join(life_dir(root, life["life_id"]), "history.jsonl"),
                 choice_row)
    display = apply_response(root, life, response, confirmed_step=step_no)
    append_jsonl(journal_path,
                 {"type": "confirmed", "step": step_no, "choose": args.key,
                  "token_fingerprint": token_fingerprint(life.get("token")),
                  "at": now_iso()})
    out = public_summary(life, display)
    out["state"] = display["state"]
    out["events"] = display["events"]
    out["next_decision"] = display["decision"]
    emit(out)
    return 0


def cmd_pause(root, args):
    life = load_life(root, resolve_life_id(root, args.life))
    if recover_pending_step(root, life):
        life = load_life(root, life["life_id"])
    if life["status"] != "active":
        raise LocalReject("only an active life can be paused (status is %s)" % life["status"])
    life["status"] = "paused"
    bump_revision(life)
    save_life(root, life)
    append_jsonl(os.path.join(life_dir(root, life["life_id"]), "history.jsonl"),
                 {"type": "paused", "at": now_iso()})
    display = refresh_display(root, life)
    emit(public_summary(life, display))
    return 0


def cmd_resume(root, args):
    life = load_life(root, resolve_life_id(root, args.life))
    if recover_pending_step(root, life):
        life = load_life(root, life["life_id"])
    if life["status"] == "unknown":
        raise LocalReject("life is unknown; run reconcile before resuming")
    if life["status"] == "ended":
        raise LocalReject("life has ended and cannot be resumed")
    if life["status"] != "paused":
        raise LocalReject("only a paused life can be resumed (status is %s)" % life["status"])
    response = engine_view(life["token"])
    life["status"] = "active"
    save_life(root, life)
    display = apply_response(root, life, response, events_are_new=False)
    life = load_life(root, life["life_id"])
    if life["status"] != "ended":
        life["status"] = "active"
        bump_revision(life)
        save_life(root, life)
        display = refresh_display(root, life)
    append_jsonl(os.path.join(life_dir(root, life["life_id"]), "history.jsonl"),
                 {"type": "resumed", "at": now_iso()})
    emit(public_summary(life, display))
    return 0


def cmd_reconcile(root, args):
    life = load_life(root, resolve_life_id(root, args.life))
    if recover_pending_step(root, life):
        life = load_life(root, life["life_id"])
    was_unknown = life["status"] == "unknown"
    # A view costs no units and never advances the life, but it only ever
    # shows the state of the token held locally -- the last confirmed
    # state. It can never prove that a lost step was not applied: if the
    # engine did apply it, the new token left with the lost response.
    response = engine_view(life["token"])
    display = apply_response(root, life, response, events_are_new=False)
    life = load_life(root, life["life_id"])
    journal_path = os.path.join(life_dir(root, life["life_id"]), "journal.jsonl")
    history_path = os.path.join(life_dir(root, life["life_id"]), "history.jsonl")
    append_jsonl(journal_path,
                 {"type": "reconciled_view", "at": now_iso(),
                  "token_fingerprint": token_fingerprint(life.get("token"))})
    abandoned = False
    if was_unknown and args.abandon:
        # A deliberate fork: give up the branch the lost step may have
        # created and continue from the last confirmed token. There is no
        # resend path (a retry of the same request): whether the same
        # input would reproduce the same result is unconfirmed, so it is
        # not offered. The abandon is journaled and kept on the record.
        lost = life.get("unknown_step") or {}
        pending = life.get("pending_step")
        pending_step = (pending.get("step") if isinstance(pending, dict)
                        else pending)
        record = {
            "type": "abandoned",
            "step": lost.get("step", pending_step),
            "choose": lost.get("choose"),
            "token_fingerprint": token_fingerprint(life.get("token")),
            "at": now_iso(),
        }
        life["status"] = "active"
        life["pending_step"] = None
        life["unknown_step"] = None
        life["last_incident"] = dict(record)
        bump_revision(life)
        save_life(root, life)
        display = refresh_display(root, life)
        append_jsonl(journal_path, record)
        append_jsonl(history_path, record)
        abandoned = True
    out = public_summary(life, display)
    out["was_unknown"] = was_unknown
    out["abandoned"] = abandoned
    out["state"] = display["state"]
    out["note"] = (
        "view costs no units and never advances the life, but it shows "
        "only the last confirmed state held locally; it cannot prove "
        "the lost step was not applied. If the engine applied it, its "
        "new token is gone and that branch cannot be reached. "
        "reconcile --abandon deliberately gives that branch up and "
        "forks the life from the last confirmed token."
    )
    emit(out)
    return 0


def cmd_history(root, args):
    index = load_index(root)
    lives = []
    for life_id in index["lives"]:
        try:
            life = load_life(root, life_id)
        except LocalReject:
            continue
        snapshot = life.get("snapshot") or {}
        state = snapshot.get("state") or {}
        death = snapshot.get("death") or {}
        lives.append({
            "life_id": life_id,
            "status": life["status"],
            "name": state.get("name"),
            "country": state.get("country"),
            "age": state.get("age"),
            "step_count": life.get("step_count", 0),
            "created_at": life.get("created_at"),
            "ended": bool(snapshot.get("ended")),
            "age_at_death": death.get("age"),
            "current": life_id == index.get("current_life_id"),
        })
    emit({"ok": True, "lives": lives})
    return 0


def cmd_keydate(root, args):
    """Show or record the date the API key was acquired.

    Only the date is ever recorded -- in the local config, and from
    there into every display JSON -- never the key itself. The date
    is a plain operational fact that lets a master see when the key
    was taken, and it carries nothing about the key.
    """
    config = load_config(root)
    if not args.on:
        emit({"ok": True, "key_acquired_on": config.get("key_acquired_on")})
        return 0
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.on):
        raise LocalReject("the date must be written YYYY-MM-DD")
    try:
        datetime.strptime(args.on, "%Y-%m-%d")
    except ValueError:
        raise LocalReject("that is not a real calendar date")
    if config.get("key_acquired_on") == args.on:
        emit({"ok": True, "key_acquired_on": args.on, "revision": None})
        return 0
    config["key_acquired_on"] = args.on
    atomic_write_json(os.path.join(root, "config.json"), config)
    # The date rides in EVERY life's display -- it is read from the
    # shared config each time a display is built -- so the revision
    # must advance on every life, not only the current one. Bumping
    # only the current life leaves every other life's display
    # changed with its revision standing still: a change no delta
    # can carry and no same-revision full sync can overwrite. (The
    # bump matters because a display change that leaves the revision
    # standing still can be synced neither by delta, which needs the
    # bump, nor by a full sync, which the viewer rejects as a
    # conflict at the same revision.)
    index = load_index(root)
    revisions = {}
    for life_id in index.get("lives", []):
        try:
            life = load_life(root, life_id)
        except LocalReject:
            continue
        bump_revision(life)
        save_life(root, life)
        refresh_display(root, life)
        revisions[life_id] = life["revision"]
    emit({"ok": True, "key_acquired_on": args.on,
          "revision": revisions.get(index.get("current_life_id")),
          "revisions": revisions})
    return 0


def cmd_recap(root, args):
    life = load_life(root, resolve_life_id(root, args.life))
    snapshot = life.get("snapshot") or {}
    state = snapshot.get("state") or {}
    death = snapshot.get("death") or {}
    history = read_jsonl(os.path.join(life_dir(root, life["life_id"]), "history.jsonl"))
    choices = [row for row in history if row.get("type") == "choice"]
    incidents = [row for row in history if row.get("type") in ("unknown", "abandoned")]
    emit({
        "ok": True,
        "life_id": life["life_id"],
        "status": life["status"],
        "engine": life.get("engine"),
        "created_at": life.get("created_at"),
        "name": state.get("name"),
        "birth_country": life.get("birth_country"),
        "country": state.get("country"),
        "final_age": state.get("age"),
        "ended": bool(snapshot.get("ended")),
        "age_at_death": death.get("age"),
        "cause_of_death": death.get("cause"),
        "step_count": life.get("step_count", 0),
        "step_intents": life["counters"]["step_intents"],
        "unknown_steps": life["counters"]["unknown_steps"],
        "counters": dict(life["counters"]),
        "revision": life.get("revision", 0),
        "choices": choices,
        "chapters": list(life.get("chapters") or []),
        "epilogue": life.get("epilogue"),
        "incidents": incidents,
        "timeline_events": len(life.get("timeline") or []),
        "credit": CREDIT_LINE,
        "disclaimer": DISCLAIMER,
    })
    return 0


def cmd_chapter(root, args):
    life = load_life(root, resolve_life_id(root, args.life))
    if recover_pending_step(root, life):
        life = load_life(root, life["life_id"])
    if life["status"] == "unknown":
        raise LocalReject(
            "life is unknown; reconcile it before writing a chapter")
    title = (args.title or "").strip()
    summary = (args.summary or "").strip()
    if not title or not summary:
        raise LocalReject("a chapter needs a non-empty --title and --summary")
    if len(title) > 120 or len(summary) > 2000:
        raise LocalReject(
            "chapter text is too long (title <= 120 chars, "
            "summary <= 2000 chars)")
    chapters = list(life.get("chapters") or [])
    covered = int(chapters[-1]["to_step"]) if chapters else 0
    entries = [entry for entry in read_choice_entries(root, life["life_id"])
               if (entry.get("step") or 0) > covered]
    if not entries:
        raise LocalReject(
            "no confirmed choices since the last chapter; nothing to cover")
    ages = [entry.get("age") for entry in entries
            if isinstance(entry.get("age"), (int, float))]
    chapter = {
        "index": len(chapters) + 1,
        "from_step": entries[0].get("step"),
        "to_step": entries[-1].get("step"),
        "from_age": ages[0] if ages else None,
        "to_age": ages[-1] if ages else None,
        "title": title,
        "summary": summary,
        "at": now_iso(),
    }
    # A chapter is recorded and synced exactly like a reason, so it is
    # gated the same way before anything is written.
    try:
        assert_no_secrets({"chapter": chapter}, [life.get("token")])
    except AssertionError:
        raise LocalReject(
            "the chapter text appears to contain a credential or a "
            "token; refusing to record it (chapters are stored and "
            "synced)")
    life["chapters"] = chapters + [chapter]
    bump_revision(life)
    save_life(root, life)
    refresh_display(root, life)
    emit({
        "ok": True,
        "life_id": life["life_id"],
        "status": life["status"],
        "chapter": chapter,
        "revision": life["revision"],
        "step_count": life.get("step_count", 0),
    })
    return 0


def cmd_epilogue(root, args):
    """Write the player's own closing words for a finished life.

    The epilogue is the one part of the record the engine cannot
    supply: after a life ends, the Muse who lived it writes what
    the life was like, in their own words, and it is kept on the
    record and shown on the viewer after the ending -- the
    afterword to the story the choices and chapters tell. It is
    written once the life has ended (rewriting replaces it, as the
    author's own text), recorded and synced exactly like a reason,
    and gated the same way before anything is written.
    """
    life = load_life(root, resolve_life_id(root, args.life))
    if recover_pending_step(root, life):
        life = load_life(root, life["life_id"])
    if life["status"] != "ended":
        raise LocalReject(
            "an epilogue is written when a life has ended "
            "(status is %s)" % life["status"])
    text = (args.text or "").strip()
    title = (args.title or "").strip() or None
    if not text:
        raise LocalReject("an epilogue needs a non-empty --text")
    if (title is not None and len(title) > 120) or len(text) > 4000:
        raise LocalReject(
            "epilogue text is too long (title <= 120 chars, "
            "text <= 4000 chars)")
    epilogue = {
        "title": title,
        "text": text,
        "written_on": datetime.now().strftime("%Y-%m-%d"),
        "at": now_iso(),
    }
    try:
        assert_no_secrets({"epilogue": epilogue}, [life.get("token")])
    except AssertionError:
        raise LocalReject(
            "the epilogue text appears to contain a credential or a "
            "token; refusing to record it (epilogues are stored and "
            "synced)")
    life["epilogue"] = epilogue
    bump_revision(life)
    save_life(root, life)
    refresh_display(root, life)
    append_jsonl(os.path.join(life_dir(root, life["life_id"]),
                              "history.jsonl"),
                 {"type": "epilogue", "at": epilogue["at"],
                  "written_on": epilogue["written_on"]})
    emit({
        "ok": True,
        "life_id": life["life_id"],
        "status": life["status"],
        "epilogue": epilogue,
        "revision": life["revision"],
    })
    return 0


def cmd_catalog(root, args):
    if args.name not in CATALOG_NAMES:
        raise LocalReject("unknown catalog: %s" % args.name)
    if use_mock():
        emit({
            "ok": True, "mock": True, "catalog": args.name,
            "note": "mock fixture; the real catalog is fetched without a key",
            "items": [],
        })
        return 0
    data = real_get_catalog(args.name)
    emit({"ok": True, "catalog": args.name, "data": data})
    return 0


# --------------------------------------------------------------------------
# Space sync deltas (format v1, see life-journey-skill/PHASE2-NOTES.md).
#
# A full sync (the viewer's syncLife) stays the bootstrap and the fallback.
# Once a full sync has been recorded with `delta --ack`, `delta` compares
# the last synced snapshot under sync/ with the current display.json and
# emits only what changed: changed top-level fields go in "replace", the
# append-only "timeline" (and "choices", once the display carries one)
# travel as a *_from index plus the appended items, and "revision" itself
# is carried by base_revision / target_revision instead of "replace".
# Generation never marks anything as synced: only `delta --ack`, called
# after the viewer accepted a full sync or a delta, advances the snapshot.
# --------------------------------------------------------------------------

def read_display_file(path, missing_message, unreadable_message):
    """Read a display-shaped JSON file, refusing with exit-2 messages."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError:
        raise LocalReject(missing_message)
    except json.JSONDecodeError:
        raise LocalReject(unreadable_message)
    if not isinstance(data, dict):
        raise LocalReject(unreadable_message)
    return data


def display_revision(display, label):
    revision = display.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise LocalReject(
            "%s carries no integer revision; a full sync is required" % label)
    return revision


def without_null_fields(value):
    """A copy with every None-valued object key removed, recursively.

    A producer that learns to record a new field writes null for it on
    entries recorded before the field existed, while an older synced
    snapshot lacks the key entirely; the two shapes carry the same
    information. Dropping null-valued keys before comparing keeps an
    append-only prefix intact across such a format extension, while a
    real value appearing, changing or disappearing on an old entry
    still compares unequal and is refused as a rewrite.
    """
    if isinstance(value, dict):
        return {key: without_null_fields(item)
                for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [without_null_fields(item) for item in value]
    return value


def append_only_delta(base_items, current_items, label):
    """(from_index, appended) for an append-only list, or refuse."""
    if not isinstance(base_items, list) or not isinstance(current_items, list):
        raise LocalReject(
            "%s is missing or not a list on one side; a full sync is required"
            % label)
    if without_null_fields(current_items[:len(base_items)]) != \
            without_null_fields(base_items):
        raise LocalReject(
            "%s has been rewritten (the synced snapshot is not a prefix of "
            "the current one); a full sync is required" % label)
    return len(base_items), current_items[len(base_items):]


def sheet_items_by_id(sheet):
    """{id: item} for a sheet of objects carrying unique string ids.

    Returns None when the sheet does not have that shape (not a list, an
    item that is not an object, a missing or non-string id, or a repeated
    id, which would make the per-item channel ambiguous), so the caller
    can fall back to carrying the whole sheet in `replace`.
    """
    if not isinstance(sheet, list):
        return None
    by_id = {}
    for item in sheet:
        if not isinstance(item, dict):
            return None
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            return None
        if item_id in by_id:
            return None
        by_id[item_id] = item
    return by_id


def apply_delta_to_display(base, delta):
    """The viewer-side merge, mirrored locally.

    An ack after a delta acceptance records exactly the display the
    viewer now holds, reconstructed from the synced snapshot and the
    pending delta -- never whatever display happens to be current.
    """
    merged = copy.deepcopy(base)
    for key, value in (delta.get("replace") or {}).items():
        merged[key] = copy.deepcopy(value)
    timeline = merged.get("timeline") or []
    if len(timeline) != delta.get("timeline_from"):
        raise LocalReject(
            "the pending delta does not fit the synced snapshot "
            "(timeline); a full sync is required")
    merged["timeline"] = timeline + list(delta.get("timeline_append") or [])
    if delta.get("choices_from") is not None:
        choices = merged.get("choices") or []
        if len(choices) != delta["choices_from"]:
            raise LocalReject(
                "the pending delta does not fit the synced snapshot "
                "(choices); a full sync is required")
        merged["choices"] = choices + list(
            delta.get("choices_append") or [])
    if delta.get("chapters_from") is not None:
        chapters = merged.get("chapters") or []
        if len(chapters) != delta["chapters_from"]:
            raise LocalReject(
                "the pending delta does not fit the synced snapshot "
                "(chapters); a full sync is required")
        merged["chapters"] = chapters + list(
            delta.get("chapters_append") or [])
    if delta.get("schema_version") == DELTA_SCHEMA_VERSION_V2:
        by_id = {}
        for item in merged.get("sheet") or []:
            if isinstance(item, dict):
                by_id[item.get("id")] = item
        for removed_id in delta.get("sheet_remove") or []:
            by_id.pop(removed_id, None)
        for item in delta.get("sheet_items") or []:
            by_id[item.get("id")] = copy.deepcopy(item)
        merged["sheet"] = [by_id[item_id]
                           for item_id in delta.get("sheet_order") or []
                           if item_id in by_id]
    merged["revision"] = delta.get("target_revision")
    return merged


def cmd_settings(root, args):
    """Show or change the current life's play settings.

    The settings agreed with the master at the life's start (pace,
    ask mode, lost-step policy) live on the life's record, so they
    survive a change of chat; this command is how a later change --
    made on the master's word, like the ask mode being switched on
    mid-life -- is recorded in the same place. Settings are local
    record only: they never enter the display or the engine.
    """
    life = load_life(root, resolve_life_id(root, args.life))
    if recover_pending_step(root, life):
        life = load_life(root, life["life_id"])
    if life["status"] == "unknown":
        raise LocalReject(
            "life is unknown; reconcile it before changing its settings")
    settings = dict(life.get("settings") or {})
    changed = {}
    if args.pace is not None:
        settings["pace"] = args.pace
        changed["pace"] = args.pace
    if args.ask_mode is not None:
        settings["ask_mode"] = args.ask_mode == "on"
        changed["ask_mode"] = settings["ask_mode"]
    if args.lost_step_policy is not None:
        settings["lost_step_policy"] = args.lost_step_policy
        changed["lost_step_policy"] = args.lost_step_policy
    if changed:
        life["settings"] = settings
        save_life(root, life)
        append_jsonl(
            os.path.join(life_dir(root, life["life_id"]), "journal.jsonl"),
            {"type": "settings_changed", "at": now_iso(), "changed": changed})
    since_chapter = choices_since_chapter(life)
    emit({
        "ok": True,
        "life_id": life["life_id"],
        "settings": settings,
        "changed": changed,
        "choices_since_chapter": since_chapter,
        "chapter_due": since_chapter >= 5,
    })
    return 0


def cmd_delta(root, args):
    life_id = resolve_life_id(root, args.life)
    life = load_life(root, life_id)
    recover_pending_step(root, life)
    current = read_display_file(
        display_path(root, life_id),
        "display.json not found for life %s; run a command that refreshes "
        "the display first" % life_id,
        "display.json for life %s is unreadable; a full sync is required"
        % life_id)
    if current.get("life_id") not in (None, life_id):
        raise LocalReject(
            "display.json belongs to life %s, not %s; a full sync is required"
            % (current.get("life_id"), life_id))
    target_revision = display_revision(current, "the current display")

    if args.ack:
        # The ack is bound to what the viewer actually accepted. The
        # caller passes the accepted revision from the viewer's
        # response (synclife / syncdelta return it). An unbound ack
        # that merely snapshots the current display could record a
        # state the viewer never saw -- the display may have advanced
        # past the accepted delta -- and every later delta would then
        # be built on a false base.
        if args.revision is None:
            raise LocalReject(
                "delta --ack requires --revision <the revision the "
                "viewer accepted> (see the synclife / syncdelta "
                "response); an unbound ack could record a display the "
                "viewer never saw")
        accepted = int(args.revision)
        pending_path = pending_delta_path(root, life_id)
        if os.path.exists(pending_path):
            pending = read_json(pending_path)
            if not isinstance(pending, dict) \
                    or pending.get("life_id") != life_id:
                raise LocalReject(
                    "the pending delta is unreadable; a full sync is "
                    "required")
            if accepted != pending.get("target_revision"):
                raise LocalReject(
                    "the accepted revision %d does not match the "
                    "pending delta's target revision %s; verify with "
                    "the viewer (getlite) and re-sync"
                    % (accepted, pending.get("target_revision")))
            snapshot_path = synced_snapshot_path(root, life_id)
            if not os.path.exists(snapshot_path):
                raise LocalReject(
                    "no synced snapshot exists; a full sync is "
                    "required before delta --ack")
            base = read_display_file(
                snapshot_path,
                "no synced snapshot for life %s; a full sync is "
                "required" % life_id,
                "the synced snapshot for life %s is unreadable; a "
                "full sync is required" % life_id)
            if display_revision(base, "the synced snapshot") \
                    != pending.get("base_revision"):
                raise LocalReject(
                    "the synced snapshot is not at the pending "
                    "delta's base revision; a full sync is required")
            merged = apply_delta_to_display(base, pending)
            try:
                assert_no_secrets(merged, [life.get("token")])
            except AssertionError as exc:
                raise LocalReject(
                    "refusing to record a synced snapshot: %s" % exc)
            atomic_write_json(snapshot_path, merged)
            try:
                os.unlink(pending_path)
            except OSError:
                pass
            emit({
                "ok": True,
                "status": "synced",
                "life_id": life_id,
                "revision": pending["target_revision"],
            })
            return 0
        # A full-sync ack: record the current display, but only when
        # it is exactly the revision the viewer accepted.
        if target_revision != accepted:
            raise LocalReject(
                "the current display is at revision %d, not the "
                "accepted revision %d; the display moved after the "
                "sync. Verify with the viewer (getlite) and re-sync"
                % (target_revision, accepted))
        try:
            assert_no_secrets(current, [life.get("token")])
        except AssertionError as exc:
            raise LocalReject("refusing to record a synced snapshot: %s" % exc)
        atomic_write_json(synced_snapshot_path(root, life_id), current)
        emit({
            "ok": True,
            "status": "synced",
            "life_id": life_id,
            "revision": target_revision,
        })
        return 0

    snapshot_path = synced_snapshot_path(root, life_id)
    if not os.path.exists(snapshot_path):
        raise LocalReject(
            "no synced snapshot for life %s: a full sync is required before "
            "delta sync (run a full sync, then record it with delta --ack)"
            % life_id)
    base = read_display_file(
        snapshot_path,
        "no synced snapshot for life %s: a full sync is required before "
        "delta sync (run a full sync, then record it with delta --ack)"
        % life_id,
        "the synced snapshot for life %s is unreadable; a full sync is "
        "required" % life_id)
    if base.get("life_id") not in (None, life_id):
        raise LocalReject(
            "the synced snapshot belongs to life %s, not %s; a full sync is "
            "required" % (base.get("life_id"), life_id))
    base_revision = display_revision(base, "the synced snapshot")
    if target_revision < base_revision:
        raise LocalReject(
            "the synced snapshot (revision %d) is newer than the current "
            "display (revision %d); a full sync is required"
            % (base_revision, target_revision))

    timeline_from, timeline_append = append_only_delta(
        base.get("timeline"), current.get("timeline"), "the timeline")
    # "choices" joins the append-only channel once the current display
    # carries it. A synced snapshot from before the array existed is an
    # empty baseline (choices_from 0, every entry appended): the array
    # only ever grows, and a viewer whose stored display somehow holds
    # entries the snapshot lacks refuses the offset, so a divergence
    # fails safe into a full sync. A current display WITHOUT the array
    # while the snapshot has one means the field was removed, which no
    # delta can express.
    base_choices = base.get("choices")
    current_choices = current.get("choices")
    if isinstance(current_choices, list) and (
            base_choices is None or isinstance(base_choices, list)):
        choices_from, choices_append = append_only_delta(
            base_choices or [], current_choices, "the choices list")
    elif base_choices is None and current_choices is None:
        choices_from, choices_append = None, []
    else:
        raise LocalReject(
            "the choices list was removed or changed shape; a full sync "
            "is required")

    # "chapters" (the player's own chapter notes) joins the same
    # append-only channel as choices, under the same rules: a snapshot
    # from before the array existed is an empty baseline, and a current
    # display without the array while the snapshot has one is a removal
    # no delta can express.
    base_chapters = base.get("chapters")
    current_chapters = current.get("chapters")
    if isinstance(current_chapters, list) and (
            base_chapters is None or isinstance(base_chapters, list)):
        chapters_from, chapters_append = append_only_delta(
            base_chapters or [], current_chapters, "the chapters list")
    elif base_chapters is None and current_chapters is None:
        chapters_from, chapters_append = None, []
    else:
        raise LocalReject(
            "the chapters list was removed or changed shape; a full sync "
            "is required")

    # The sheet (the spending options) joins a per-item channel in delta
    # v2: resending the whole list every time dwarfs the rest of the
    # delta, while a step usually changes only a few items' availability.
    # The channel applies only when both sides carry a sheet of objects
    # with unique string ids and at least one side is non-empty; with
    # two empty sheets there is nothing to channel, and any other shape
    # falls back to v1, which carries the sheet whole in `replace`. A
    # sheet that vanished from the current display altogether cannot be
    # channelled, so it stays a removed field and refuses below.
    base_sheet_by_id = (sheet_items_by_id(base.get("sheet"))
                        if "sheet" in base else None)
    current_sheet_by_id = (sheet_items_by_id(current.get("sheet"))
                           if "sheet" in current else None)
    sheet_channel = (base_sheet_by_id is not None
                     and current_sheet_by_id is not None
                     and (base["sheet"] or current["sheet"]))
    if sheet_channel:
        sheet_items = [item for item_id, item in current_sheet_by_id.items()
                       if item_id not in base_sheet_by_id
                       or base_sheet_by_id[item_id] != item]
        sheet_remove = [item_id for item_id in base_sheet_by_id
                        if item_id not in current_sheet_by_id]
        sheet_order = [item["id"] for item in current["sheet"]]
        base_sheet_order = [item["id"] for item in base["sheet"]]
        sheet_changed = bool(sheet_items or sheet_remove
                             or sheet_order != base_sheet_order)
    else:
        sheet_items, sheet_remove, sheet_order = None, None, None
        sheet_changed = False

    excluded = ["timeline", "choices", "chapters", "revision"]
    if sheet_channel:
        excluded.append("sheet")
    excluded = tuple(excluded)
    removed = [key for key in base
               if key not in current and key not in excluded]
    if removed:
        raise LocalReject(
            "display fields were removed (%s); a delta cannot express "
            "that, so a full sync is required" % ", ".join(sorted(removed)))
    replace = {}
    for key, value in current.items():
        if key in excluded:
            continue
        if key not in base or base[key] != value:
            replace[key] = value

    if base_revision == target_revision:
        # updated_at and events are volatile metadata: updated_at changes
        # on every display refresh, and events holds only the latest
        # step's events, which a bare view clears. Neither makes a life
        # out of sync on its own. The same goes for usage.this_call,
        # which a view resets to 0 while used/quota stay as they were.
        # A field that appears for the first time carrying null is
        # not a change either: an absent field and a null field are
        # the same fact (the convention the append-only comparisons
        # already use), and a helper upgrade that starts emitting an
        # optional field (the epilogue, say) must not strand every
        # older life's sync on its first refresh.
        meaningful = {k: v for k, v in replace.items()
                      if k not in ("updated_at", "events")
                      and not (v is None and k not in base)}
        if "usage" in meaningful:
            def usage_core(usage):
                return {k: v for k, v in (usage or {}).items()
                        if k != "this_call"}
            if usage_core(base.get("usage")) == usage_core(
                    current.get("usage")):
                meaningful.pop("usage")
        if (not meaningful and not timeline_append and not choices_append
                and not chapters_append and not sheet_changed):
            emit({
                "status": "up_to_date",
                "life_id": life_id,
                "revision": target_revision,
            })
            return 0
        raise LocalReject(
            "the display content changed without a revision bump; a full "
            "sync is required")

    delta = {
        "schema_version": (DELTA_SCHEMA_VERSION_V2 if sheet_channel
                           else DELTA_SCHEMA_VERSION),
        "life_id": life_id,
        "base_revision": base_revision,
        "target_revision": target_revision,
        "replace": replace,
        "timeline_from": timeline_from,
        "timeline_append": timeline_append,
        "choices_from": choices_from,
        "choices_append": choices_append,
        "chapters_from": chapters_from,
        "chapters_append": chapters_append,
    }
    if sheet_channel:
        delta["sheet_items"] = sheet_items
        delta["sheet_remove"] = sheet_remove
        delta["sheet_order"] = sheet_order
    try:
        assert_no_secrets(delta, [life.get("token")])
    except AssertionError as exc:
        raise LocalReject("refusing to emit a delta: %s" % exc)
    atomic_write_json(pending_delta_path(root, life_id), delta)
    emit(delta)
    return 0


# Commands that can write state take the exclusive lock. choices and
# recap look read-only but both run pending-step recovery first, and
# recovery rewrites the life: without the lock, a choices call made
# while a choose is still waiting on the engine would mistake the
# in-flight step for a crashed one and mark the life unknown.
MUTATING_COMMANDS = ("start", "choose", "pause", "resume", "reconcile",
                     "delta", "status", "chapter", "keydate", "settings",
                     "choices", "recap", "epilogue")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="life_api.py",
        description="Play one simulated life through the 1in8b Engine /live API.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="start a new life (random birth, lang ja)")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--force", action="store_true",
                   help="start even though another life is in progress "
                        "(the override is journaled)")
    p.add_argument("--abandon-pending", action="store_true",
                   help="acknowledge an interrupted start (its process "
                        "died mid-send): count the lost send against "
                        "today's start cap, clear the pending-start "
                        "marker and do not start a life")
    p.add_argument("--pace", choices=("close", "watch", "straight"),
                   default="watch",
                   help="the reporting pace agreed with the master: "
                        "close (report every choice), watch (the default; "
                        "come back with a summary every few choices) or "
                        "straight (play long stretches); recorded on the "
                        "life's record")
    p.add_argument("--ask-mode", action="store_true",
                   help="the master wants to be consulted before "
                        "life-changing choices; recorded on the life's "
                        "record (default off)")
    p.add_argument("--lost-step-policy", choices=("ask", "gate-auto"),
                   default="ask",
                   help="ask (the default: stop and ask the master at "
                        "every lost step) or gate-auto (the master has "
                        "pre-authorized recovery through the recorded "
                        "fork for gate-class losses only); recorded on "
                        "the life's record")

    for name in ("status", "choices", "pause", "resume", "recap"):
        p = sub.add_parser(name)
        p.add_argument("--life", default=None)

    p = sub.add_parser("choose", help="choose one of the waiting options")
    p.add_argument("key")
    p.add_argument("--reason", required=True,
                   help="a short reason in your own words; recorded, never sent")
    p.add_argument("--focus", default=None)
    p.add_argument("--buy", action="append", default=None)
    p.add_argument("--life", default=None)

    p = sub.add_parser("chapter", help="write a chapter note covering the "
                                       "confirmed choices since the last "
                                       "chapter")
    p.add_argument("--title", required=True,
                   help="the chapter's headline; recorded, never sent")
    p.add_argument("--summary", required=True,
                   help="a short summary of the stretch the chapter "
                        "covers; recorded, never sent")
    p.add_argument("--life", default=None)

    p = sub.add_parser("epilogue", help="write the player's own closing "
                                        "words for a life that has ended; "
                                        "recorded and synced, never sent")
    p.add_argument("--text", required=True,
                   help="the closing words themselves, in the player's "
                        "own words")
    p.add_argument("--title", default=None,
                   help="an optional headline for the epilogue")
    p.add_argument("--life", default=None)

    p = sub.add_parser("reconcile", help="inspect an unknown life with view")
    p.add_argument("--abandon", action="store_true",
                   help="deliberately give up the branch the lost step may "
                        "have created and resume from the last confirmed "
                        "token (a journaled fork)")
    p.add_argument("--life", default=None)

    p = sub.add_parser("settings", help="show or change the current "
                                        "life's play settings (pace, ask "
                                        "mode, lost-step policy); with no "
                                        "change flags it only shows them")
    p.add_argument("--pace", choices=("close", "watch", "straight"),
                   default=None)
    p.add_argument("--ask-mode", choices=("on", "off"), default=None)
    p.add_argument("--lost-step-policy", choices=("ask", "gate-auto"),
                   default=None)
    p.add_argument("--life", default=None)

    sub.add_parser("history", help="list every recorded life")

    p = sub.add_parser("keydate", help="show or record the date the API "
                                       "key was acquired (the date only; "
                                       "never the key itself)")
    p.add_argument("--on", default=None,
                   help="record this acquisition date, as YYYY-MM-DD")

    p = sub.add_parser("delta", help="build a Space sync delta against the "
                                     "last synced snapshot")
    p.add_argument("--life", default=None)
    p.add_argument("--ack", action="store_true",
                   help="record what the viewer accepted as the synced "
                        "snapshot (call only after a full sync or a "
                        "delta was accepted by the viewer); requires "
                        "--revision")
    p.add_argument("--revision", type=int, default=None,
                   help="with --ack: the revision the viewer accepted, "
                        "from the synclife / syncdelta response")

    p = sub.add_parser("catalog", help="fetch a key-free catalog")
    p.add_argument("name", choices=CATALOG_NAMES)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    root = state_dir()
    ensure_dir(root)
    dispatch = {
        "start": cmd_start,
        "status": cmd_status,
        "choices": cmd_choices,
        "choose": cmd_choose,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "reconcile": cmd_reconcile,
        "history": cmd_history,
        "recap": cmd_recap,
        "catalog": cmd_catalog,
        "delta": cmd_delta,
        "chapter": cmd_chapter,
        "epilogue": cmd_epilogue,
        "keydate": cmd_keydate,
        "settings": cmd_settings,
    }
    try:
        if args.command in MUTATING_COMMANDS:
            with StateLock(root):
                return dispatch[args.command](root, args)
        return dispatch[args.command](root, args)
    except LocalReject as exc:
        emit({"ok": False, "error": "rejected", "message": str(exc)})
        return 2
    except NotConnected as exc:
        emit({"ok": False, "error": "not_connected", "message": str(exc)})
        return 4
    except StepUnknown as exc:
        emit({"ok": False, "error": "step_outcome_unknown", "message": str(exc)})
        return 3
    except UpstreamError as exc:
        # The upstream message is free text from outside this process
        # and is never echoed: an error body can carry request data.
        # Only an allowlisted code and the status cross, with a fixed
        # message.
        code = exc.code
        if not (isinstance(code, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", code)):
            code = None
        out = {"ok": False, "error": "upstream", "status": exc.status,
               "message": "the engine or the transport returned an error"}
        if code:
            out["code"] = code
        emit(out)
        return 5


if __name__ == "__main__":
    sys.exit(main())

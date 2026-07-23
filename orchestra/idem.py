"""orchestra.idem — persisted, boot-aware idempotency for wire mutations.

Wave 1 added in-memory resource locks (`dispatch._wt_reservations`,
`agent:<id>`), and they stop a concurrent double-tap cold. What they cannot
stop is a *retry that lands after the server restarted*. `./start.sh` kills and
relaunches this process by design, so restart-mid-dispatch is routine — and a
background `URLSession` defaults `timeoutIntervalForResource` to seven days and
retries across reboots and network changes. A dispatch handed to the phone's
background daemon during a tailnet outage can arrive forty minutes later
against a fresh process whose in-memory lock is long gone. It re-executes, and
a second agent launches on the same branch: two agents merging and pushing one
worktree, the worst outcome this product can produce (ARCHITECTURE §5.6).

The close is a reservation persisted **write-ahead** — at `begin()`, before the
caller's side effect — tagged with a per-process `BOOT_ID`. A retry that finds
its key still `in_flight` under a DIFFERENT boot is told the server restarted
mid-op (`operation_indeterminate`) and is NEVER re-executed; the same key under
the same boot is the ordinary concurrent duplicate (`operation_in_flight`); a
`done` key replays its stored response verbatim. There is deliberately no
`abandon()` — a handler exception settles the key with the failure, so the
retry replays the failure rather than re-running the side effect.

A leaf by construction: it imports `config` only (for the state-file path) and
nothing in the package imports it back except `server`, which wraps each keyed
mutation with `begin()`/`complete()`. `begin` releases this module's lock
BEFORE the handler runs — the handler takes its own worktree/pick locks, and
this lock is never held across it, so idempotency composes above the Wave 1
locks without a lock-ordering cycle. `IDEM_STORE` is rebound at runtime (tests
point it at a temp file), so it is reached as `idem.IDEM_STORE`, and `BOOT_ID`
likewise (a restart test swaps it), never copied to a caller.
"""

import hashlib
import json
import os
import sys
import threading
import time

from . import config


# A token minted once per process. Two requests carrying the same
# `Idempotency-Key` are "the same boot" only when this matches — which is
# exactly how a retry that lands after `./start.sh` restarted the server is
# told apart from a genuine concurrent duplicate. `os.urandom` so a fast
# restart cannot mint the boot it just replaced.
BOOT_ID = os.urandom(8).hex()

# Beside resume.schedule.json and the auth registry — the same runtime-state
# directory (`config.HERE`), written with the same tmp + os.replace discipline
# (`resume.save_resumes`), so a kill mid-write can never leave a half file.
IDEM_STORE = config.HERE / "idem.store.json"

IDEM_TTL_S = 3600.0        # evict a record this many seconds after its last touch
IDEM_EXPIRE_S = 900.0      # issued_at older than this -> reject "expired"
IN_FLIGHT_RETRY_S = 1      # Retry-After seconds on operation_in_flight

# The mutation routes idempotency guards. A read/pair/hook route is never gated:
# it has no side effect a retry could double. `server.do_POST` consults this so
# the set of protected routes lives in one place.
MUTATION_ROUTES = frozenset({
    "/api/send", "/api/finish", "/api/dispatch",
    "/api/reserve", "/api/resume/schedule", "/api/resume/cancel"})

# Only "still running" is safe for a client to retry as-is; every other refusal
# tells it to stop and look before acting again. Drives the body's `retriable`.
RETRIABLE_CODES = frozenset({"operation_in_flight"})

_records = {}                  # key -> {boot, issued_at, fingerprint, done,
                               #         status, body, ts}
_lock = threading.Lock()       # guards _records AND the file — one writer at a time
_loaded = False                # the disk read happens once, lazily, under _lock


def fingerprint(method, route, payload):
    """A stable fingerprint of what the server was asked to DO.

    `sha256(canonical_json(payload) + method + route)` — canonical because a
    retry that re-serialises its body with keys in a different order is the same
    intent and must land on the same fingerprint. A retry with a *different*
    body under a reused key is caught by comparing this against the stored one
    (`422 idempotency_key_reused`). Everything the payload came from was
    `json.loads`d, so it is always JSON-serialisable."""
    canon = json.dumps(payload, sort_keys=True)
    return hashlib.sha256((canon + method + route).encode()).hexdigest()


def _load():
    """Read the persisted store once, under `_lock`. Missing or corrupt -> start
    empty; never crash (a broken idempotency file must not take the door down)."""
    global _loaded
    if _loaded:
        return
    _loaded = True             # set first: a missing/corrupt file must not retry
    try:
        raw = IDEM_STORE.read_text()
    except OSError:
        return
    try:
        data = json.loads(raw)
    except ValueError:
        return                 # corrupt -> empty, the records were advisory
    recs = data.get("records") if isinstance(data, dict) else None
    if isinstance(recs, dict):
        for key, rec in recs.items():
            if isinstance(rec, dict):
                _records[key] = rec


def _save():
    """Persist the store — atomically, caller holding `_lock`.

    tmp + os.replace (the `resume.save_resumes` / auth-registry idiom): a plain
    truncate-write killed mid-flight by `./start.sh` would leave an empty file,
    and the next boot would read it as "no reservations" — losing exactly the
    write-ahead records this file exists to keep. A persistent write failure is
    printed, not swallowed: an idempotency store that silently stops persisting
    is a second agent waiting to happen."""
    tmp = IDEM_STORE.with_name(IDEM_STORE.name + ".tmp")
    try:
        blob = json.dumps({"boot": BOOT_ID, "records": _records}, indent=1)
        tmp.write_text(blob + "\n")
        os.replace(tmp, IDEM_STORE)
    except OSError as e:
        print(f"orchestra: couldn't save {IDEM_STORE.name}: {e}", file=sys.stderr)


def _evict(now):
    """Drop records untouched for longer than the TTL. Caller holds `_lock`.

    Called on every access so the file stays bounded without a sweeper thread.
    A record's `ts` is its last touch (`begin` or `complete`); an in-flight op
    whose process died without completing ages out here as its crash net, and a
    `done` reply stops replaying an hour after it settled."""
    stale = [k for k, r in _records.items()
             if now - float(r.get("ts", now)) > IDEM_TTL_S]
    for k in stale:
        del _records[k]


def begin(key, method, route, payload, issued_at, now):
    """Reserve `key` write-ahead, or say why the caller must not run the handler.

    Returns one of, evaluated in this order (ARCHITECTURE §5.6):

      ("reject", (status, code, message, extra_headers))  refuse; do not run
      ("proceed", None)                                   first sighting: the
                                                          in_flight record is
                                                          persisted BEFORE this
                                                          returns, so a restart
                                                          mid-handler is visible
      ("replay", (status, body))                          done + same intent:
                                                          the stored response

    The whole check-and-reserve is one critical section, so two threads racing
    the same key cannot both proceed. The lock is released before this returns —
    the handler runs without it."""
    fp = fingerprint(method, route, payload)
    with _lock:
        _load()
        _evict(now)

        # Expiry first: a request that sat too long in a background queue is
        # refused regardless of what its key has done, so a stale retry never
        # re-drives — nor replays — an intent the user has long moved past.
        if issued_at is not None:
            try:
                if now - float(issued_at) > IDEM_EXPIRE_S:
                    return ("reject", (409, "expired",
                            "this request waited too long before it arrived and "
                            "was refused; commit it again to retry a fresh one",
                            {}))
            except (TypeError, ValueError):
                pass           # an unparseable issued_at cannot expire anything

        rec = _records.get(key)
        if rec is None:
            # First sighting. Persist the in_flight reservation BEFORE the caller
            # touches anything — this is the write that a restart-mid-op depends
            # on being on disk.
            _records[key] = {"boot": BOOT_ID, "issued_at": issued_at,
                             "fingerprint": fp, "done": False,
                             "status": None, "body": None, "ts": now}
            _save()
            return ("proceed", None)

        if not rec.get("done"):
            if rec.get("boot") == BOOT_ID:
                if rec.get("fingerprint") == fp:
                    # The ordinary concurrent duplicate — the first request is
                    # still running in this same process. Refuse, never block.
                    return ("reject", (409, "operation_in_flight",
                            "this operation is already running; retry in a moment",
                            {"Retry-After": str(IN_FLIGHT_RETRY_S)}))
                # Same key, in flight, but a DIFFERENT body: the key is being
                # reused for a second intent while the first still runs.
                return ("reject", (422, "idempotency_key_reused",
                        "this Idempotency-Key is already in flight for a "
                        "different request", {}))
            # In flight under a DIFFERENT boot: the server restarted while this
            # op was running and we cannot know whether its side effect landed.
            # Do not re-execute — the whole reason this module exists.
            return ("reject", (409, "operation_indeterminate",
                    "the server restarted while this operation was running — "
                    "check the fleet before retrying; a mission may already be "
                    "live", {}))

        # Done. Same intent replays the stored response byte-for-byte; a
        # different intent under the same key is the reuse error.
        if rec.get("fingerprint") == fp:
            return ("replay", (rec.get("status"), rec.get("body")))
        return ("reject", (422, "idempotency_key_reused",
                "this Idempotency-Key was already used for a different request",
                {}))


def complete(key, status, body):
    """Settle `key` with the response the handler produced (or its failure).

    Called on BOTH the success and the exception path — there is no abandon: a
    500 stored here is what a retry replays instead of re-running the side
    effect. First completion wins: once a `done` record exists it is left alone,
    so a response whose transport failed AFTER the side effect succeeded keeps
    its stored success rather than being overwritten by the transport error."""
    now = time.time()
    with _lock:
        _load()
        rec = _records.get(key)
        if rec is None:
            # begin() always writes the record first, so this is defensive: a
            # completion for a key we never reserved is still recorded done, so
            # a later retry replays it rather than re-executing.
            rec = _records[key] = {"boot": BOOT_ID, "issued_at": None,
                                   "fingerprint": None, "done": False,
                                   "status": None, "body": None, "ts": now}
        if rec.get("done"):
            return             # first completion wins
        rec.update(done=True, status=status, body=body, ts=now)
        _evict(now)
        _save()


def _reset():
    """Tests only: forget every record and force the next access to reload from
    IDEM_STORE (the process-wide store outlives a single test)."""
    global _loaded
    with _lock:
        _records.clear()
        _loaded = False

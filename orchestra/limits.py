"""orchestra.limits — how much usage each account has left, and who may spend it.

Everything here hangs off one external tool: `cclimits --json`
(github.com/acrdlph/cclimits), which reads Claude Code's own usage files per
account. Shelling out is expensive and the numbers move slowly, so
`cached_limits` sits behind a 5-minute cache and only refetches from the
network when a caller explicitly asks (`refresh=True`).

The rest is policy on top of that one payload. `account_reserve` is the
buffer an account must keep free before auto-dispatch will spend it;
`_model_remaining` folds the account-wide caps (session, weekly) together
with the one model-scoped cap that the model in hand would consume;
`model_candidates` ranks the accounts that could run a mission;
`limits_by_account` is the board's per-account summary, and it is careful to
keep a maxed model-scoped cap OUT of the account-wide `exhausted` flag —
an account whose Fable is gone still has headroom for an Opus mission.
`_limit_active_until` is the resume path's question: when does the cap that
stranded this session actually reset?

`_limits` is a mutable cache dict, mutated in place and never rebound, so the
facade re-export and the tests that poke `_limits["data"]` see the same
object. Patch `limits.cached_limits`, never the facade copy.

Every reset time in here is ABSOLUTE. cclimits answers in `resets_in_seconds`,
a countdown frozen the instant it was measured; `_absolutise_resets` converts
it once, at the fetch boundary, and nothing downstream ever sees the countdown
again (ENGINE.md §3.4). This matters because of the cache above it: the same
answer is served for up to LIMITS_TTL_S = 300 s, so a countdown read from it
can be five minutes wrong with nothing on the payload to say so.
"""

import json
import shutil
import time
from pathlib import Path

from . import config, shell


# ------------------------------------------------------------ usage limits

LIMITS_TTL_S = 300.0           # cclimits --json (its own cache) at most this often
_limits = {"t": 0.0, "data": None}


def _cclimits_bin():
    cmd = config.CFG.get("cclimits_cmd")
    if cmd:
        return cmd
    found = shutil.which("cclimits")
    if found:
        return found
    fallback = config.HOME / ".local" / "bin" / "cclimits"
    return str(fallback) if fallback.exists() else None


def _absolutise_resets(acc, fetched):
    """Rewrite one account's limits from a countdown to an absolute instant.

    ENGINE.md §3.4: no field derived from `now()` goes on the wire.
    `resets_in_seconds` is exactly that — measured when cclimits ran, then held
    by the cache above for up to LIMITS_TTL_S, so a client counting down from
    it drifts by the whole TTL and cannot tell. `fetched_at + resets_in_seconds`
    is the same reading in a form that ages correctly.

    The computed value WINS over any `resets_at` cclimits itself supplies. That
    field is passthrough from a tool whose schema is not pinned here — usually
    null, and when present it is not guaranteed to be an epoch float (an
    ISO-8601 string would decode to garbage). `resets_in_seconds` has a known
    unit, so it is the one we trust; a numeric `resets_at` is kept only when
    there is no countdown to derive from.
    """
    for l in acc.get("limits") or []:
        rs, ra = l.get("resets_in_seconds"), l.get("resets_at")
        if isinstance(rs, (int, float)) and not isinstance(rs, bool):
            l["resets_at"] = fetched + rs
        elif not isinstance(ra, (int, float)) or isinstance(ra, bool):
            l["resets_at"] = None
        l.pop("resets_in_seconds", None)


def cached_limits(refresh=False):
    """Per-account usage limits via cclimits (github.com/acrdlph/cclimits).
    Lazy + cached; a network refetch happens only on explicit refresh."""
    if config.DEMO:
        return demo_limits()
    now = time.time()
    if not refresh and _limits["data"] is not None and now - _limits["t"] < LIMITS_TTL_S:
        return _limits["data"]
    binp = _cclimits_bin()
    if not binp:
        return {"available": False, "error": "cclimits not found — install github.com/acrdlph/cclimits"}
    cmd = [binp, "--json"] + (["--refresh"] if refresh else [])
    rc, out = shell.run(cmd, timeout=90 if refresh else 30)
    if rc != 0 or not out:
        return _limits["data"] or {"available": False, "error": "cclimits failed (see terminal)"}
    try:
        data = json.loads(out)
    except ValueError:
        return _limits["data"] or {"available": False, "error": "cclimits returned non-JSON"}
    data["available"] = True
    data["fetched_at"] = now
    for acc in data.get("accounts", []):
        _absolutise_resets(acc, now)   # countdowns never leave this function
        if acc.get("config_dir"):
            label = config.account_label(Path(acc["config_dir"]))
            r = account_reserve(label)
            acc["fb_label"] = label   # orchestra's label (cclimits slug may differ)
            acc["reserve_percent"] = r
            acc["reserve_blocked"] = r > 0 and (acc.get("headroom_percent") or 0) < r
    _limits["data"], _limits["t"] = data, now
    return data


def account_reserve(label):
    """Headroom % this account must keep free before auto-dispatch treats it
    as full. Per-account override, else '*' default, else 0."""
    rp = config.CFG.get("reserve_percent") or {}
    if not isinstance(rp, dict):
        return 0
    return rp.get(label, rp.get("*", 0)) or 0


def _model_remaining(acc, model):
    """Min remaining % across the limits that running `model` consumes on this
    account: all non-model-scoped limits (session, weekly) + the model-scoped
    limit matching `model`, if the account has one. None if unknown."""
    if not acc.get("ok"):
        return None
    rems = []
    for l in acc.get("limits", []):
        rem = l.get("remaining_percent")
        if rem is None:
            rem = 100 - (l.get("percent") or 0)
        if l.get("model_scoped"):
            if model and model.lower() in (l.get("label", "").lower()):
                rems.append(rem)     # this model's own cap
        else:
            rems.append(rem)         # session / weekly always apply
    return min(rems) if rems else None


def model_candidates(model, only_account=None):
    """Accounts that could run `model`, each with remaining headroom and whether
    it clears its reserve buffer. Sorted by most remaining first."""
    data = _limits["data"] if not config.DEMO else demo_limits()
    if not data or not data.get("available"):
        return []
    excl = set(config.CFG.get("exclude_accounts") or [])
    out = []
    for acc in data.get("accounts", []):
        if not acc.get("ok"):
            continue
        label = config.account_label(Path(acc["config_dir"]))
        if only_account:
            if label != only_account:
                continue
        elif label in excl:
            continue
        rem = _model_remaining(acc, model)
        if rem is None:
            continue
        reserve = account_reserve(label)
        out.append({"label": label, "remaining": round(rem),
                    "reserve": reserve, "ok": rem > 0 and rem >= reserve})
    out.sort(key=lambda x: -x["remaining"])
    return out


def set_reserve(label, percent):
    """Set an account's reserve buffer from the UI: update config.CFG, persist to the
    config file, and re-apply to the cached limits so it takes effect at once."""
    if not label:
        return {"ok": False, "error": "no account"}
    try:
        percent = max(0, min(95, int(percent)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "percent must be a number"}
    rp = dict(config.CFG.get("reserve_percent") or {})
    if percent == 0:
        rp.pop(label, None)
    else:
        rp[label] = percent
    config.CFG["reserve_percent"] = rp
    # persist: merge into the on-disk config (create if missing)
    try:
        disk = {}
        if config.CONFIG_PATH and config.CONFIG_PATH.is_file():
            disk = json.loads(config.CONFIG_PATH.read_text())
        disk["reserve_percent"] = rp
        config.CONFIG_PATH.write_text(json.dumps(disk, indent=2) + "\n")
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"couldn't write config: {e}"}
    # re-enrich cached limits so the change shows without a refetch
    data = _limits.get("data")
    if data and data.get("accounts"):
        for acc in data["accounts"]:
            if acc.get("config_dir"):
                r = account_reserve(config.account_label(Path(acc["config_dir"])))
                acc["reserve_percent"] = r
                acc["reserve_blocked"] = r > 0 and (acc.get("headroom_percent") or 0) < r
    return {"ok": True, "label": label, "percent": percent}


def limits_by_account():
    """account label -> {exhausted, worst, resets_at, headroom, reserve, available,
    scoped_exhausted}.

    `resets_at` is an absolute epoch, never a countdown — see
    `_absolutise_resets`. The countdown this used to carry beside it was frozen
    at fetch time behind a 300 s cache and went out on the wire as
    `session.limit.resets_in`; the board now subtracts `resets_at` from its own
    clock instead (ENGINE.md §3.4).

    `exhausted`/`available` reflect only ACCOUNT-WIDE caps (session + umbrella
    weekly). A maxed model-scoped cap (e.g. Fable) is a per-model constraint,
    not an account-wide block — it lands in `scoped_exhausted` instead, so an
    account whose Fable is gone but that still has 40% all-model headroom stays
    pickable for an Opus/Sonnet mission. Collapsing every limit into one
    exhausted flag wrote such accounts off wholesale."""
    data = _limits["data"] if not config.DEMO else demo_limits()
    if not data or not data.get("available"):
        return {}
    out = {}
    for acc in data.get("accounts", []):
        if not acc.get("ok"):
            continue
        label = config.account_label(Path(acc["config_dir"]))
        ex = [l for l in acc.get("limits", []) if l.get("exhausted_now")]
        blocking = [l for l in ex if not l.get("model_scoped")]   # session / umbrella weekly
        # soonest to reset first; an unknown reset (None -> 0) still sorts first,
        # as it did when this ranked on the countdown
        worst = min(blocking, key=lambda l: l.get("resets_at") or 0) if blocking else None
        headroom = acc.get("headroom_percent")
        reserve = account_reserve(label)
        # reserve-blocked: less than the required buffer remains → treat as full
        reserve_blocked = reserve > 0 and headroom is not None and headroom < reserve
        out[label] = {
            "headroom": headroom,
            "exhausted": bool(blocking),
            "worst": worst["label"] if worst else None,
            "worst_scoped": False,   # `worst` is always an account-wide cap now
            "group": worst.get("group") if worst else None,
            "resets_at": worst.get("resets_at") if worst else None,
            "reserve": reserve,
            "reserve_blocked": reserve_blocked,
            # model-scoped caps that are used up — only strand a session
            # actually running that model, not the whole account
            "scoped_exhausted": [
                {"label": l.get("label"), "group": l.get("group"),
                 "resets_at": l.get("resets_at")}
                for l in ex if l.get("model_scoped")],
            # usable for AUTO dispatch: real all-model headroom above its buffer
            "available": (not blocking) and not reserve_blocked,
        }
    return out


def demo_limits():
    now = time.time()
    # demo records go out on the same wire as real ones, so they carry the same
    # absolute `resets_at` and no countdown — a demo that shipped the field the
    # real path dropped would let a client bind to a field that isn't there.
    def lim(label, group, pct, ex, resets_h, scoped=False):
        return {"label": label, "group": group, "percent": pct,
                "remaining_percent": 100 - pct, "model_scoped": scoped,
                "exhausted_now": ex, "resets_at": now + resets_h * 3600}
    return {"available": True, "fetched_at": now, "generated_at": None, "accounts": [
        {"slug": "default", "email": None, "plan": "max", "config_dir": "~/.claude",
         "ok": True, "error": None, "headroom_percent": 62.0, "limits": [
            lim("Session", "session", 21, False, 3.2), lim("Weekly", "weekly", 38, False, 96)]},
        {"slug": "work", "email": None, "plan": "max", "config_dir": "~/.claude-work",
         "ok": True, "error": None, "headroom_percent": 0.0, "limits": [
            lim("Session", "session", 100, True, 2.1), lim("Weekly", "weekly", 91, False, 30)]},
        {"slug": "spare", "email": None, "plan": "pro", "config_dir": "~/.claude-spare",
         "ok": True, "error": None, "headroom_percent": 88.0, "limits": [
            lim("Session", "session", 4, False, 4.8), lim("Weekly", "weekly", 12, False, 120)]},
    ]}


def _limit_active_until(account, model, now):
    """The freshest word on whether `account` still blocks this session: a
    future reset timestamp while it does, None once it's clear. Refetches
    cclimits — the cached view predates the reset by design. An unreadable
    account verifies as clear: the send costs nothing if the limit holds."""
    data = cached_limits(refresh=True)
    if not data or not data.get("available"):
        return None
    al = limits_by_account().get(account)
    if not al:
        return None
    cands = []
    if al.get("exhausted") and al.get("resets_at"):
        cands.append(al["resets_at"])         # account-wide cap bites every model
    for sx in al.get("scoped_exhausted", []):
        if not sx.get("resets_at"):
            continue
        # a model-scoped cap only blocks a session running that model; with the
        # model unknown, count it — a late resume beats a wasted one
        if not model or (sx.get("label") or "").lower() in model.lower():
            cands.append(sx["resets_at"])
    future = [c for c in cands if c > now + 30]
    return min(future) if future else None

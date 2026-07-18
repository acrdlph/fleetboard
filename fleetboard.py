#!/usr/bin/env python3
"""fleetboard — local mission control for parallel Claude Code agents.

Watches your git worktrees, your Claude Code home directories (multi-account
setups included), and live `claude` processes, then serves a dashboard showing
which agent is working, which one needs you, and what every branch is up to.

Read-only observer: it never touches your sessions, worktrees, or accounts.
Zero dependencies — python3 stdlib only.

    python3 fleetboard.py --root ~/code
    python3 fleetboard.py --demo          # fictional data, for screenshots

Configuration precedence: CLI flags > fleetboard.config.json (next to this
script, else cwd) > defaults. See README.md.
"""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).resolve().parent

CFG = {
    "host": "127.0.0.1",       # keep loopback: the board serves transcript text
    "port": 4242,
    "roots": [str(Path.cwd())],  # dirs whose git-repo children are watched
    "pattern": "",             # optional regex filter on worktree dir names
    "homes": [],               # Claude home dirs; [] = auto-discover ~/.claude*
    "session_window_h": 48,    # ignore transcripts idle longer than this
    "working_s": 90,           # transcript written within this => working
    "max_sessions": 6,         # per worktree card
}

TAIL_BYTES = 128 * 1024
HEAD_BYTES = 16 * 1024
STATE_TTL_S = 4.0              # cache collector output between requests
_cache = {"t": 0.0, "state": None}
DEMO = False


def load_config(argv=None):
    ap = argparse.ArgumentParser(description="local mission control for parallel Claude Code agents")
    ap.add_argument("--root", action="append", metavar="DIR",
                    help="directory whose git-repo children are watched (repeatable; default: cwd)")
    ap.add_argument("--pattern", metavar="REGEX", help="only watch dirs matching this regex (case-insensitive)")
    ap.add_argument("--home", action="append", metavar="DIR",
                    help="Claude home dir (repeatable; default: auto-discover ~/.claude*)")
    ap.add_argument("--port", type=int, help="port (default 4242, env FLEETBOARD_PORT)")
    ap.add_argument("--host", help="bind address (default 127.0.0.1 — the board serves your transcript text; do not expose it)")
    ap.add_argument("--window-h", type=float, help="ignore transcripts idle longer than this many hours (default 48)")
    ap.add_argument("--config", metavar="FILE", help="path to a fleetboard.config.json")
    ap.add_argument("--demo", action="store_true", help="serve fictional demo data (for screenshots)")
    args = ap.parse_args(argv)

    candidates = [Path(args.config)] if args.config else [
        HERE / "fleetboard.config.json", Path.cwd() / "fleetboard.config.json"]
    for p in candidates:
        if p.is_file():
            try:
                CFG.update(json.loads(p.read_text()))
            except (ValueError, OSError) as e:
                sys.exit(f"fleetboard: bad config {p}: {e}")
            break
    if os.environ.get("FLEETBOARD_PORT"):
        CFG["port"] = int(os.environ["FLEETBOARD_PORT"])
    if args.root: CFG["roots"] = args.root
    if args.pattern is not None: CFG["pattern"] = args.pattern
    if args.home: CFG["homes"] = args.home
    if args.port: CFG["port"] = args.port
    if args.host: CFG["host"] = args.host
    if args.window_h: CFG["session_window_h"] = args.window_h
    return args


# ---------------------------------------------------------------- collectors

def run(cmd, cwd=None, timeout=6):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except Exception:
        return 1, ""


def munge(path):
    """Claude Code's project-dir name for a cwd."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def discover_worktrees():
    pat = re.compile(CFG["pattern"], re.I) if CFG["pattern"] else None
    wts, seen = [], set()
    for root in CFG["roots"]:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_dir() or (pat and not pat.search(p.name)) or str(p) in seen:
                continue
            git_root = None
            if (p / ".git").exists():
                git_root = p
            elif (p / "repo" / ".git").exists():  # <worktree>/repo layout
                git_root = p / "repo"
            if git_root:
                seen.add(str(p))
                wts.append({"name": p.name, "path": str(p), "git": str(git_root)})
    return wts


def git_info(git_root):
    info = {"branch": None, "commit": None, "dirty": 0, "ahead": None, "behind": None}
    rc, branch = run(["git", "branch", "--show-current"], cwd=git_root)
    if rc == 0 and branch:
        info["branch"] = branch
    else:
        rc, head = run(["git", "rev-parse", "--short", "HEAD"], cwd=git_root)
        info["branch"] = f"detached@{head}" if rc == 0 else "?"
    rc, log = run(["git", "log", "-1", "--format=%h%x00%ct%x00%s"], cwd=git_root)
    if rc == 0 and log:
        h, ct, s = (log.split("\x00") + ["", "", ""])[:3]
        info["commit"] = {"hash": h, "ts": int(ct or 0), "subject": s}
    rc, status = run(["git", "status", "--porcelain"], cwd=git_root)
    if rc == 0:
        info["dirty"] = len([l for l in status.splitlines() if l.strip()])
    rc, lr = run(["git", "rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd=git_root)
    if rc == 0 and lr:
        parts = lr.split()
        if len(parts) == 2:
            info["behind"], info["ahead"] = int(parts[0]), int(parts[1])
    return info


def _pid_cwds(pids):
    cwds = {}
    if not pids:
        return cwds
    if sys.platform.startswith("linux"):
        for pid in pids:
            try:
                cwds[pid] = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                pass
    else:  # macOS and other BSDs: one lsof call for all pids
        _, out = run(["lsof", "-a", "-d", "cwd", "-p", ",".join(map(str, pids)), "-Fn"],
                     timeout=10)
        cur = None
        for line in out.splitlines():
            if line.startswith("p"):
                cur = int(line[1:])
            elif line.startswith("n") and cur is not None:
                cwds[cur] = line[1:]
    return cwds


def claude_processes():
    """Live `claude` CLI processes with their cwd."""
    rc, out = run(["ps", "-axo", "pid=,pcpu=,etime=,command="])
    procs = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+([\d.]+)\s+(\S+)\s+(.*)", line)
        if not m:
            continue
        pid, cpu, etime, cmd = m.groups()
        if cmd == "claude" or cmd.startswith("claude "):
            procs.append({"pid": int(pid), "cpu": float(cpu), "etime": etime, "cmd": cmd})
    cwds = _pid_cwds([p["pid"] for p in procs])
    for p in procs:
        p["cwd"] = cwds.get(p["pid"])
    return procs


def claude_homes():
    # Precedence: --home / config "homes" > CLAUDE_CONFIG_DIRS (colon-separated,
    # same convention as cclimits) > auto-discover ~/.claude*
    explicit = CFG["homes"] or [
        h for h in os.environ.get("CLAUDE_CONFIG_DIRS", "").split(":") if h]
    if explicit:
        return [Path(h).expanduser() for h in explicit
                if (Path(h).expanduser() / "projects").is_dir()]
    homes = []
    for p in sorted(HOME.iterdir()):
        if (p.name == ".claude" or p.name.startswith(".claude-")) and (p / "projects").is_dir():
            homes.append(p)
    return homes


def account_label(home):
    name = home.name.lstrip(".")
    if name == "claude":
        return "main"
    return re.sub(r"^claude-?", "", name) or name


def _read_chunk(fp, size, from_end):
    try:
        with open(fp, "rb") as f:
            if from_end:
                f.seek(0, 2)
                n = f.tell()
                f.seek(max(0, n - size))
                data = f.read()
                if n > size:  # drop leading partial line
                    data = data.split(b"\n", 1)[-1]
            else:
                data = f.read(size)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _clean(text, limit=240):
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"<command-name>(.*?)</command-name>", r"\1", text, flags=re.S)
    text = re.sub(r"<[^>]{1,80}>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def _real_prompt(text):
    """A user text that describes the session (not a slash-command stub/caveat)."""
    if "<local-command-stdout>" in text or "<command-message>" in text:
        return None
    t = _clean(text, 140)
    if not t or t.startswith("/") or t.startswith("Caveat:"):
        return None
    return t


def session_topic(fp):
    """Label a session: compaction summary if present, else first real user prompt."""
    for line in _read_chunk(fp, HEAD_BYTES, from_end=False).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "summary" and e.get("summary"):
            return _clean(e["summary"], 140)
        if e.get("type") == "user" and not e.get("isMeta"):
            c = e.get("message", {}).get("content")
            texts = [c] if isinstance(c, str) else [
                b.get("text", "") for b in c
                if isinstance(b, dict) and b.get("type") == "text"] if isinstance(c, list) else []
            for t in texts:
                topic = _real_prompt(t)
                if topic:
                    return topic
    return None


def parse_session_tail(fp):
    """Tail-parse a transcript: last activity, pending tools, last assistant text."""
    entries = []
    for line in _read_chunk(fp, TAIL_BYTES, from_end=True).splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    main = [e for e in entries if isinstance(e, dict) and not e.get("isSidechain")]

    out = {"cwd": None, "branch": None, "model": None, "pending_tools": [],
           "last_assistant": None}
    pending = {}  # tool_use id -> tool name
    for e in main:
        out["cwd"] = e.get("cwd") or out["cwd"]
        out["branch"] = e.get("gitBranch") or out["branch"]
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if e.get("type") == "assistant":
            model = msg.get("model")
            if model and model != "<synthetic>":
                out["model"] = model
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        pending[b.get("id")] = b.get("name", "?")
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        out["last_assistant"] = _clean(b["text"])
        elif e.get("type") == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    pending.pop(b.get("tool_use_id"), None)
    out["pending_tools"] = sorted(set(pending.values()))
    return out


def scan_sessions(worktrees, procs, now):
    """All recent sessions across every Claude home, mapped to worktrees."""
    by_wt = {w["path"]: [] for w in worktrees}
    wt_prefixes = {w["path"]: munge(w["path"]) for w in worktrees}
    window_s = CFG["session_window_h"] * 3600

    def best_worktree(proj_name):
        # Munged names are ambiguous (myapp vs myapp-audit):
        # the longest matching worktree prefix wins.
        best = None
        for path, pref in wt_prefixes.items():
            if proj_name == pref or proj_name.startswith(pref + "-"):
                if best is None or len(pref) > len(wt_prefixes[best]):
                    best = path
        return best

    for home in claude_homes():
        acct = account_label(home)
        for proj in (home / "projects").iterdir():
            wt = best_worktree(proj.name)
            if wt is None:
                continue
            for fp in proj.glob("*.jsonl"):
                try:
                    mtime = fp.stat().st_mtime
                except OSError:
                    continue
                age = now - mtime
                if age > window_s:
                    continue
                tail = parse_session_tail(fp)
                cwd = tail["cwd"] or wt
                by_wt[wt].append({
                    "id": fp.stem[:8],
                    "account": acct,
                    "age_s": int(age),
                    "cwd": cwd,
                    "subdir": os.path.relpath(cwd, wt) if cwd != wt else None,
                    "branch": tail["branch"],
                    "model": (tail["model"] or "").replace("claude-", ""),
                    "pending_tools": tail["pending_tools"],
                    "topic": session_topic(fp),
                    "last_assistant": tail["last_assistant"],
                })

    # A live process proves at most ONE session per cwd is really attended.
    # Hand each proc slot to the freshest sessions in its cwd; the rest are ended.
    budget = {}
    for p in procs:
        if p.get("cwd"):
            budget[p["cwd"]] = budget.get(p["cwd"], 0) + 1

    rank = {"needs_input": 0, "blocked": 1, "working": 2, "waiting": 3, "ended": 4}
    for wt, sessions in by_wt.items():
        sessions.sort(key=lambda s: s["age_s"])
        for s in sessions:
            alive = budget.get(s["cwd"], 0) > 0
            if alive:
                budget[s["cwd"]] -= 1
            pend = s["pending_tools"]
            if s["age_s"] < CFG["working_s"]:
                status = "working"
            elif alive and "AskUserQuestion" in pend:
                status = "needs_input"
            elif alive and pend:
                status = "blocked"
            elif alive:
                status = "waiting"
            else:
                status = "ended"
            s["status"] = status
        sessions.sort(key=lambda s: (rank[s["status"]], s["age_s"]))
        by_wt[wt] = sessions[: CFG["max_sessions"]]
    return by_wt


def collect_state():
    now = time.time()
    worktrees = discover_worktrees()
    procs = claude_processes()
    sessions = scan_sessions(worktrees, procs, now)

    cards = []
    for w in worktrees:
        ss = sessions.get(w["path"], [])
        live = [p for p in procs if p.get("cwd") and
                (p["cwd"] == w["path"] or p["cwd"].startswith(w["path"] + "/"))]
        cards.append({
            **w,
            "git": git_info(w["git"]),
            "sessions": ss,
            "live_procs": [{"pid": p["pid"], "cpu": p["cpu"], "etime": p["etime"],
                            "subdir": os.path.relpath(p["cwd"], w["path"])
                            if p["cwd"] != w["path"] else None} for p in live],
        })

    for c in cards:
        st = [s["status"] for s in c["sessions"]]
        if c["live_procs"] or "working" in st:
            if "needs_input" in st or "blocked" in st or "waiting" in st:
                c["availability"] = "attention"   # agent parked here, needs you
            else:
                c["availability"] = "busy"        # agent actively working
        else:
            c["availability"] = "free"            # safe to point a new agent here

    matched = {p["pid"] for c in cards for p in c["live_procs"]}
    other = [p for p in procs if p["pid"] not in matched]

    def severity(c):
        st = [s["status"] for s in c["sessions"]]
        if "needs_input" in st: return 0
        if "blocked" in st: return 1
        if "waiting" in st: return 2
        if "working" in st: return 3
        return 4
    cards.sort(key=lambda c: (severity(c), c["name"].lower()))

    counts = {"working": 0, "needs_input": 0, "blocked": 0, "waiting": 0, "ended": 0}
    for c in cards:
        for s in c["sessions"]:
            counts[s["status"]] += 1
    return {
        "generated_at": now,
        "hostname": os.uname().nodename,
        "user": getpass.getuser(),
        "counts": counts,
        "free_worktrees": [c["name"] for c in cards if c["availability"] == "free"],
        "worktrees": cards,
        "other_procs": [{"pid": p["pid"], "cpu": p["cpu"], "etime": p["etime"],
                         "cwd": p.get("cwd")} for p in other],
    }


# --------------------------------------------------------------- demo state

def demo_state():
    """Fictional data with the exact shape of collect_state(), for screenshots."""
    now = time.time()

    def sess(status, acct, model, age, topic, said, subdir=None, pend=None):
        return {"id": "demo0000", "account": acct, "status": status, "age_s": age,
                "cwd": "/demo", "subdir": subdir, "branch": None, "model": model,
                "pending_tools": pend or [], "topic": topic, "last_assistant": said}

    def card(name, avail, branch, dirty, ahead, behind, cts, subject, sessions, pids):
        return {"name": name, "path": "/demo/" + name, "git_root": "",
                "git": {"branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind,
                        "commit": {"hash": "a1b2c3d", "ts": int(now - cts), "subject": subject}},
                "sessions": sessions, "availability": avail,
                "live_procs": [{"pid": p, "cpu": 4.2, "etime": "02:14:33", "subdir": None}
                               for p in pids]}

    cards = [
        card("orbital-api", "attention", "feat/webhook-retries", 12, 3, 0, 1800,
             "feat(webhooks): exponential backoff with jitter", [
                 sess("needs_input", "work", "fable-5", 340,
                      "Add retry logic to the webhook dispatcher with dead-letter support",
                      "Should failed deliveries older than 24h go to the dead-letter queue or be dropped? I've laid out both options.",
                      pend=["AskUserQuestion"]),
                 sess("ended", "work", "opus-4-8", 9100,
                      "Profile the webhook worker under load", None)], [41234]),
        card("orbital-web", "attention", "fix/checkout-race", 3, 1, 0, 5400,
             "fix(cart): serialize checkout mutations", [
                 sess("waiting", "personal", "fable-5", 2100,
                      "The checkout button double-fires on slow connections — find and fix the race",
                      "Fixed and verified — the mutation is now idempotent and the test suite passes. Ready for review.")], [41567]),
        card("kepler-worker", "busy", "perf/batch-inserts", 7, 0, 0, 600,
             "perf(db): batch event inserts, 40x fewer round-trips", [
                 sess("working", "work", "opus-4-8", 15,
                      "Migrate the event pipeline to batched COPY inserts",
                      "Running the benchmark suite against the staging database now.")], [42901]),
        card("voyager-cli", "free", "main", 0, 0, 0, 86400 * 2,
             "chore: release v0.4.1", [], []),
        card("lander-docs", "free", "docs/quickstart", 2, None, None, 86400,
             "docs: rewrite quickstart around the new init flow", [], []),
    ]
    return {
        "generated_at": now, "hostname": "starbase", "user": "you",
        "counts": {"working": 1, "needs_input": 1, "blocked": 0, "waiting": 1, "ended": 1},
        "free_worktrees": ["voyager-cli", "lander-docs"],
        "worktrees": cards,
        "other_procs": [{"pid": 40001, "cpu": 1.1, "etime": "15:02", "cwd": "/demo/scratch"}],
    }


def cached_state():
    if DEMO:
        return demo_state()
    now = time.time()
    if _cache["state"] is None or now - _cache["t"] > STATE_TTL_S:
        _cache["state"] = collect_state()
        _cache["t"] = now
    return _cache["state"]


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/state"):
            body = json.dumps(cached_state()).encode()
            ctype = "application/json"
        elif self.path == "/" or self.path.startswith("/index"):
            body = (HERE / "index.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    args = load_config()
    DEMO = args.demo
    if CFG["host"] not in ("127.0.0.1", "localhost", "::1"):
        print("fleetboard: WARNING — binding beyond loopback serves your "
              "transcript text to the network", file=sys.stderr)
    server = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    mode = " (demo data)" if DEMO else ""
    print(f"fleetboard up → http://{CFG['host']}:{CFG['port']}{mode}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

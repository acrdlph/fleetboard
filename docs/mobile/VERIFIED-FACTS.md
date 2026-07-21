# Verified platform facts

Measured directly on the user's machine (macOS 25.2.0 / Darwin, python 3.14.5), 2026-07-21.
These are **empirical**, not recalled — the design docs must not contradict them.

## Baseline performance

```
collect_state()          1641 ms total   (9 worktrees, 5 live claude processes)
  git_info x9            1277 ms   78%
  scan_sessions           335 ms   20%
  claude_processes        112 ms    7%
  discover_worktrees        1 ms
```

Stacked client-visible latency: `STATE_TTL_S = 4.0` (orchestra.py:61) + `setInterval(tick, 5000)`
(index.html:1169) → **~10.6 s worst case** from change to pixels.

Separate and larger: `CFG["working_s"] = 90` (orchestra.py:51) holds `● WORKING` for up to
**90 s** after a session stops writing. This is hysteresis, not latency.

## git: five calls per worktree collapse to one

`git_info()` (orchestra.py:141) currently spawns **5 git processes per worktree** —
45 subprocess spawns for 9 worktrees:

| line | command |
|---|---|
| 143 | `git branch --show-current` |
| 147 | `git rev-parse --short HEAD` |
| 149 | `git log -1 --format=%h%x00%ct%x00%s` |
| 153 | `git status --porcelain` |
| 156 | `git rev-list --left-right --count @{u}...HEAD` |

**Verified:** `git status --porcelain=v2 --branch` returns branch, upstream, ahead/behind *and*
the dirty file list in **one call, measured at 19 ms**:

```
# branch.oid 479b1dc202cbb999028f557f808d604fe2ff4aac
# branch.head main
# branch.upstream origin/main
# branch.ab +0 -0
```

That replaces lines 143, 147, 153 and 156. Only `git log -1` remains separate → **5 calls → 2**.

`branch_topology()` (orchestra.py:~839–882) is worse: ~8 git calls per worktree. Same treatment
applies.

## kqueue: event-driven watching is viable from stdlib on macOS

```
select.kqueue available : True
KQ_FILTER_VNODE         : True
KQ_NOTE_WRITE           : True
KQ_NOTE_EXTEND          : True
fd soft/hard limit      : 1048576 / unlimited
```

### ⚠️ Correction — the fd ceiling is real, `ulimit -n` is not the binding constraint

An earlier revision of this file claimed the fd objection to kqueue "does not apply" because the
soft limit is 1,048,576, and that "any design that rejects kqueue on fd-exhaustion grounds is
wrong". **That was wrong.** `RLIMIT_NOFILE` is not the ceiling that binds on macOS. Re-measured:

```
RLIMIT_NOFILE soft     1,048,576     ← not the real constraint
kern.maxfilesperproc       61,440     ← per-process ceiling
kern.maxfiles             122,880     ← system-wide file table
kern.num_files (idle)      18,167     ← already 15% consumed before we start
```

And the corpus is much larger than assumed:

```
claude homes                    8
project dirs                  295
top-level transcripts         702
ALL .jsonl incl. subagents 18,773     ← grows ~+982/day, peak +4,123
```

Watching every `.jsonl` would take ~30 % of the per-process cap and ~15 % of the **global** file
table — and exhausting `kern.maxfiles` breaks *other applications*, not just orchestra. That is a
genuine design constraint.

**The watch set must be bounded deliberately**, not taken as free:

- watch the **295 project directories** to detect *new* transcripts appearing, and
- watch only the **in-window top-level transcripts** for writes (~71 within the 48 h window,
  702 worst case),
- **never** the 18,773 subagent files — enumerate those on demand.

That is ~366 fds typical, ~1,000 worst case. Safe, with headroom, and it stays safe as the
corpus grows.

Two kqueue behaviours that shape this, both verified:

- A directory watch fires on child **create** (`NOTE_WRITE`, 0x2) but produces **nothing** on
  in-place modification of a file inside it. So directory watches alone cannot detect a
  transcript being appended to — file watches are required for writes, directory watches for
  discovery. The two are not interchangeable.
- `os.O_EVTONLY` **is** exposed by Python on macOS (since 3.10; reads back 32768 on 3.14.5).
  Do not hand-roll the raw `0x8000` constant.
- `os.pidfd_open` has been stdlib since Python 3.9 — do not reach for `ctypes` on Linux.
- `EVFILT_PROC` / `NOTE_EXIT` arms on non-child same-uid pids (verified 40/40), so process
  *death* is observable — but there is **no** filter for process *birth*. Discovery of new
  processes stays a timer poll. Any "fully event-driven" claim that omits this is wrong.

### mtime is a lying clock

Worst observed case in the live corpus: `mtime_age` 1,779 s against a true `evidence_age` of
219,803 s — the file's mtime said 30 minutes when the last real activity was **2.5 days** ago.
Status must be derived from parsed transcript evidence, not from `stat()` alone.

**Linux has no stdlib inotify binding.** `ctypes` (which *is* stdlib) can wrap it, or Linux falls
back to the fast-collector polling path. Either is acceptable; macOS is the primary platform.

## APNs from stdlib python is feasible

```
OpenSSL 3.6.2 (7 Apr 2026)
curl 8.7.1 (x86_64-apple-darwin25.0) libcurl/8.7.1 ... nghttp2/1.67.1
```

Both pieces APNs needs are present on the machine:

- **ES256 JWT signing** — python stdlib has no ECDSA. `openssl dgst -sha256 -sign key.p8`
  provides it. Note the JOSE requirement: openssl emits a **DER-encoded** signature and JWS
  needs **raw `r||s`, 64 bytes**. That conversion is mandatory and is the single most likely
  thing to get wrong.
- **HTTP/2 POST** — python stdlib has no HTTP/2 client. `curl --http2` has it via
  **nghttp2 1.67.1**, confirmed linked in.

So APNs can be driven without adding a python dependency, at the cost of shelling out to two
binaries that ship with macOS. That preserves the zero-dependency identity in letter and
mostly in spirit.

## SSE on ThreadingHTTPServer works — no server rewrite needed

The gating question for push: `ThreadingHTTPServer` dedicates **one thread per request**, and an
SSE connection is long-lived, so every subscriber pins a thread for its whole lifetime. If dead
clients never released those threads, the design would need a different server foundation.

Measured with a real ThreadingHTTPServer, 12 concurrent SSE subscribers, then killed rudely
(sockets closed without FIN handshake — simulating the phone dropping off the tailnet):

```
12 SSE clients open                    → 14 threads alive (1/client + main + accept)
server-side registered subscribers     → 12
broadcast → first-client latency       → 0.45, 0.50, 0.52, 0.60, 0.68 ms
normal GET while 12 streams held open  → 21.2 ms (not starved)
after 12 rude disconnects              → 0 subscribers, 2 threads   ← fully reclaimed
```

**Verdict: viable.** Sub-millisecond fan-out, no thread leak, ordinary requests unaffected.
Two required mitigations:

1. Override `handle_error()` on the server — a dropped SSE client raises `ConnectionResetError`
   and `socketserver` prints a full traceback to stderr by default. Without this, every tailnet
   blip spams the log.
2. Impose a hard subscriber cap. Thread-per-client is fine at fleet scale (a browser in a few
   tabs plus a phone) but is not unbounded; reject beyond a configured maximum rather than
   degrading.

The broadcast pattern that worked: per-subscriber queue + `threading.Condition`, with the
handler blocking on `cv.wait(timeout=25)` and emitting a `: keepalive` comment frame on timeout.

## Confirmed decisions

- The user **has** a paid Apple Developer account → APNs, Live Activities, widgets and
  TestFlight are all available. The `ntfy.sh` fallback is not needed for v1.
- Transport is **Tailscale**; the server will bind a tailnet interface, not `0.0.0.0`.
- Client is **native SwiftUI, Swift 6, iOS only**.

"""orchestra.tailnet — find the address to bind, instead of asking for it.

ADR 0001 puts the phone on a tailnet and ADR 0013 puts plain HTTP on it. Both
leave one operational question: **which address?** The answer on this machine is
`100.113.110.31`, and the way that used to reach the server was the user typing
it into `--host`. That is a bad interface for a security-relevant setting:

* it is a magic number nobody remembers, so it gets pasted from somewhere, and
  the somewhere is usually a document that is now out of date;
* it can be typed WRONG in a direction that is worse than not working — `0.0.0.0`
  is one keystroke from a tailnet address in muscle memory, and it is every
  interface including whatever wifi you are on;
* and Tailscale can reassign it.

So the address is DETECTED. `--tailnet` means "bind wherever Tailscale put me",
and if Tailscale is not running there is nothing to bind and the server says so
in those words rather than dying on `EADDRNOTAVAIL`.

## What "detected" means here, and why it is a bind and not a parse

Two sources, then one measurement:

1. **`ifconfig`** — every `inet` address on the box, filtered to the CGNAT range
   Tailscale allocates from (`100.64.0.0/10`, RFC 6598 — NOT RFC 1918, which is
   why `NSAllowsLocalNetworking` does not cover it either, ADR 0013).
2. **`tailscale ip -4`** — the daemon's own opinion. It answers even when the
   backend is `Stopped`, which is exactly the case that needs a good error
   message: the address is *known* and *not bindable*.

Then the candidate is **actually bound**, on port 0, and thrown away. That is
the whole check. METHOD.md §2 is about measuring the thing rather than a proxy
for it, and the thing here is "will `Server(...)` succeed" — not "does this
string look like a tailnet address", not "is there an interface", not "is the
daemon up". A source that lists an address whose interface is down passes every
proxy check and fails the real one, which is the failure this module exists to
turn into a sentence.

## What this module refuses to do

It does not turn anything on. If Tailscale is stopped, `address()` returns None
and the caller refuses to start — it does not run `tailscale up`, which would be
a program that changes your network configuration because you asked it to serve
a web page.
"""

import ipaddress
import re
import socket
import subprocess

# RFC 6598 shared address space. Tailscale allocates every node a v4 address
# from 100.64.0.0/10; nothing else on a normal Mac uses this range, which is
# what makes the filter safe. The v6 half of a tailnet is fd7a:115c:a1e0::/48,
# and it is deliberately not used for the bind: the phone reaches the Mac by
# v4 in every path this project has, and one address is one thing to get wrong.
CGNAT = ipaddress.ip_network("100.64.0.0/10")

# Where the CLI lives when it is not on PATH. The Mac App Store build puts it
# inside the bundle and does not symlink it, so a user with a perfectly working
# Tailscale can have no `tailscale` command at all.
TAILSCALE = ("tailscale", "/usr/local/bin/tailscale",
             "/Applications/Tailscale.app/Contents/MacOS/Tailscale")

TIMEOUT_S = 4.0     # a wedged daemon must not hang the boot


def in_range(addr):
    """Is this a tailnet v4 address? Anything unparseable is not."""
    try:
        return ipaddress.ip_address(addr) in CGNAT
    except ValueError:
        return False


def _run(cmd):
    """A subprocess whose every failure mode is the same empty string.

    A missing binary, a non-zero exit, a hang, a permissions error: all of them
    mean "this source has nothing to say", and none of them should reach the
    caller as an exception. The caller's job is to decide what to do when NO
    source has anything to say, and that decision is the same in every case.
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def from_interfaces():
    """Tailnet addresses that are configured on an interface right now."""
    out = _run(["ifconfig"]) or _run(["/sbin/ifconfig"])
    return [a for a in re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)", out)
            if in_range(a)]


def from_cli():
    """Tailnet addresses the Tailscale CLI reports, if it is installed.

    Reported even while the backend is stopped, which is the point: it lets the
    caller say "Tailscale knows about 100.113.110.31 but is not up" instead of
    "no tailnet address found", and those two sentences lead to different
    actions.
    """
    for binary in TAILSCALE:
        out = _run([binary, "ip", "-4"])
        if out:
            found = [ln.strip() for ln in out.splitlines() if in_range(ln.strip())]
            if found:
                return found
    return []


def bindable(addr, port=0):
    """Can a socket actually be bound here? The only question that matters.

    `SO_REUSEADDR` is deliberately NOT set. It is set on the real server (via
    `allow_reuse_address`), and setting it here would let this probe succeed on
    an address where the real bind then fails for a different reason — a check
    that is more permissive than the thing it is checking is worse than none.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((addr, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def address():
    """The tailnet address to bind, or None. Interfaces first, then the CLI.

    Interfaces first because an address that is on an interface is one the
    kernel already accepts; the CLI's answer still has to be proved. Every
    candidate is probed either way — `from_interfaces` can list an address on
    an interface that is down, and being wrong here means the server exits with
    a stack trace instead of a sentence.
    """
    seen = []
    for addr in from_interfaces() + from_cli():
        if addr not in seen:
            seen.append(addr)
    for addr in seen:
        if bindable(addr):
            return addr
    return None


def why_not():
    """One sentence explaining a failed `address()`, for the user to act on.

    Three genuinely different situations, and telling them apart is the whole
    value: not installed, installed but not up, and up but the address will not
    bind. Merging them into "could not find a tailnet address" is the shape of
    unhelpfulness that makes people paste `0.0.0.0` into `--host`.
    """
    known = from_cli()
    on_box = from_interfaces()
    if not known and not on_box:
        return ("no Tailscale address was found on this machine — the CLI "
                "reported none and no interface carries an address in "
                f"{CGNAT}. Is Tailscale installed and logged in?")
    if known and not on_box:
        return (f"Tailscale knows this machine as {known[0]} but no interface "
                f"carries that address, so it cannot be bound — the backend is "
                f"not up. Start Tailscale and try again.")
    return (f"the tailnet address {(on_box or known)[0]} is configured but "
            f"could not be bound; something else may already hold this port.")

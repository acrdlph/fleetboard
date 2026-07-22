"""python3 -m orchestra — start the board.

The old `if __name__ == "__main__":` block, moved verbatim out of the module
that is now the package facade (where it would have been dead code, since
`__name__` there is 'orchestra'). It reaches the app through module objects,
never through imported names: config.DEMO is rebound here at startup, and a
`from .config import DEMO` would freeze a copy and silently disable demo mode.

It is also the device admin CLI (`--add-device`, `--list-devices`,
`--revoke-device`). Those live at a shell on this machine rather than behind a
route on purpose: a device that can revoke devices can revoke the device that
would have revoked IT, so the admin surface is the one thing that must not be
reachable from the tailnet (API.md §2.5 puts every device route behind
`admin`, and phones are never issued `admin`).
"""
import sys
import threading

from orchestra import auth, config, observer, resume, server, tailnet


def _devices(args):
    """The admin flags. Returns True if one of them ran — nothing starts."""
    if args.add_device:
        device, token = auth.add_device(args.add_device)
        print(f"device {device['id']}  {device['label']}")
        print(f"\n  {token}\n")
        # Said plainly, because the alternative to writing it down now is
        # minting another one later and leaving a live token in the registry
        # that nobody holds.
        print("That token is readable this once. The registry "
              f"({auth.REGISTRY}) keeps only its sha256, so it cannot be "
              "recovered — revoke this device and mint another instead.")
        return True
    if args.list_devices:
        rows = auth.devices()
        if not rows:
            print(f"no devices registered ({auth.REGISTRY})")
        for d in rows:
            seen = d.get("last_seen")
            state = "REVOKED" if d.get("revoked") else "active"
            print(f"{d['id']}  {state:<7}  last seen "
                  f"{'never' if not seen else f'{seen:.0f}'}  {d['label']}")
        return True
    if args.revoke_device:
        d = auth.revoke_device(args.revoke_device)
        print(f"revoked {d['id']} ({d['label']})" if d else
              f"no such device: {args.revoke_device}")
        return True
    return False


def _resolve_host():
    """Turn `--tailnet` into an address, or exit saying why it could not.

    Detection rather than a pasted magic number (see `tailnet.py`). It exits
    rather than falling back to loopback, because a silent fallback is the
    worst outcome available here: the user asked to be reachable from the
    phone, the board comes up looking healthy, and the phone cannot see it —
    a failure that surfaces in another room, on another device, with no error
    anywhere.
    """
    addr = tailnet.address()
    if not addr:
        sys.exit(f"orchestra: --tailnet asked for the Tailscale address of "
                 f"this machine, but {tailnet.why_not()}")
    config.CFG["host"] = addr


# There is deliberately NO `--pair` flag, and the reason is worth writing down
# because it looks like an obvious convenience. A pairing window lives in the
# memory of the process that opened it (`pairing.py`: a door that reopens by
# itself after a restart is a door nobody closed). A one-shot CLI that opened a
# window, drew a QR and exited would therefore print a picture of a code that
# died with the process — the phone would scan it and get `pairing_not_open`,
# and the user would have no way to tell that from a network problem.
#
# Pairing needs a RUNNING server, so it is offered where the server is: the
# `/pair` page, or on a headless box the same route the page uses —
#
#     curl -sX POST -H 'Content-Type: application/json' \
#          http://127.0.0.1:4242/api/v1/devices/pair/open | python3 -m json.tool
#
# which prints the code and the QR as SVG over loopback, with no flag needed.


def main():
    args = config.load_config()
    config.DEMO = args.demo
    if _devices(args):
        return
    if args.tailnet:
        _resolve_host()
    # A REFUSAL, not the warning that used to stand here (ADR 0013: "silent
    # wide exposure must be impossible"). The old line printed to stderr and
    # then bound the socket anyway, which is the worst of both — it told you,
    # in a stream nobody reads, and did the thing regardless. Binding beyond
    # loopback with no device registered would serve every transcript on this
    # machine to every device on the tailnet, and it would look exactly like
    # success.
    refusal = auth.bind_refusal(config.CFG["host"])
    if refusal:
        sys.exit(f"orchestra: {refusal}")
    if not config.DEMO:
        resume.load_resumes()
        threading.Thread(target=resume.resume_loop, daemon=True).start()
        # The sweep starts HERE and nowhere else. Importing the package must
        # never start a thread: the test suite imports it constantly, and a
        # background thread shelling out to git and ps during a test run is its
        # own kind of hell. Not calling this degrades to exactly the old lazy
        # collect-on-request — that is the rollback story, one line long.
        observer.start_observer()
    # server.Server, not a bare ThreadingHTTPServer: the listen backlog and the
    # dropped-subscriber handling that `/api/events` needs live on the class,
    # and a board started with the stock one would stream perfectly and then
    # spam stderr the first time a phone changed network.
    host, port = config.CFG["host"], config.CFG["port"]
    httpd = server.Server((host, port), server.Handler)
    companion = _also_loopback(host, port)

    mode = " (demo data)" if config.DEMO else ""
    where = f"http://{host}:{port}"
    print(f"orchestra up → {where}{mode}")
    if companion:
        print(f"   and on http://127.0.0.1:{port} — the board, and the only "
              f"place devices can be managed")
    # Said at the one moment it is useful. A bind beyond loopback is a bind
    # somebody did on purpose to reach a phone, and the next thing they need is
    # the page that pairs one.
    if not auth.loopback(host):
        print(f"   pair a device at http://127.0.0.1:{port}/pair")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if companion:
            companion.shutdown()
            companion.server_close()


def _also_loopback(host, port):
    """A SECOND listener on 127.0.0.1 whenever the first one is not loopback.

    This is not a convenience; without it a `--tailnet` board is broken in two
    ways that only show up once you actually run one, and both were found by
    doing exactly that:

    1. **The board's own bookmark stops working.** A server bound to
       `100.113.110.31` is not listening on `127.0.0.1` at all, so
       `http://localhost:4242` — which is where the four pages live and what
       every link in them is relative to — is `ConnectionRefused`.

    2. **Device management becomes unreachable from the Mac itself.** A request
       the Mac sends to its own tailnet address arrives with a source address
       of `100.113.110.31`, which is not loopback and never can be. Since
       `auth.ADMIN` answers only to this machine holding no token, a
       tailnet-only bind means NOBODY can open a pairing window or revoke a
       device — including the person sitting at the keyboard. The security
       rule was right and the topology made it unusable.

    So the tailnet listener carries the phone and the loopback listener carries
    the board, which is the split API.md §2.3 assumed all along when it wrote
    about "the tailnet listeners" and "the loopback listener" separately.

    Not added for `0.0.0.0`: that already includes `127.0.0.1`, and a second
    bind would fail with EADDRINUSE — the wide bind is a different shape of
    mistake and it does not need this one on top.
    """
    if auth.loopback_bind(host) or host in ("0.0.0.0", "::", "*"):
        return None
    companion = server.Server(("127.0.0.1", port), server.Handler)
    threading.Thread(target=companion.serve_forever, daemon=True).start()
    return companion


if __name__ == "__main__":
    main()

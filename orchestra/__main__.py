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

from orchestra import auth, config, observer, resume, server


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


def main():
    args = config.load_config()
    config.DEMO = args.demo
    if _devices(args):
        return
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
    httpd = server.Server((config.CFG["host"], config.CFG["port"]),
                          server.Handler)
    mode = " (demo data)" if config.DEMO else ""
    print(f"orchestra up → http://{config.CFG['host']}:{config.CFG['port']}{mode}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""python3 -m orchestra — start the board.

The old `if __name__ == "__main__":` block, moved verbatim out of the module
that is now the package facade (where it would have been dead code, since
`__name__` there is 'orchestra'). It reaches the app through the package
object, never through imported names: DEMO is rebound here at startup, and a
`from ... import DEMO` would freeze a copy and silently disable demo mode.
"""
import sys
import threading
from http.server import ThreadingHTTPServer

import orchestra as app


def main():
    args = app.load_config()
    app.DEMO = args.demo
    if app.CFG["host"] not in ("127.0.0.1", "localhost", "::1"):
        print("orchestra: WARNING — binding beyond loopback serves your "
              "transcript text to the network", file=sys.stderr)
    if not app.DEMO:
        app.load_resumes()
        threading.Thread(target=app.resume_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((app.CFG["host"], app.CFG["port"]), app.Handler)
    mode = " (demo data)" if app.DEMO else ""
    print(f"orchestra up → http://{app.CFG['host']}:{app.CFG['port']}{mode}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

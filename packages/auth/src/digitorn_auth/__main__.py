"""CLI entrypoint: ``digitorn-auth serve``.

Wraps uvicorn so the service can be started without remembering the
module path.
"""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="digitorn-auth")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the FastAPI server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8001)
    serve.add_argument("--reload", action="store_true", help="Auto-reload on code changes (dev only)")
    serve.add_argument("--workers", type=int, default=1)

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn
        logging.basicConfig(level=logging.INFO)
        uvicorn.run(
            "digitorn_auth.server:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            workers=args.workers if not args.reload else 1,
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())

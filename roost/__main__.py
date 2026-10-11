"""Roost's small local command line."""

import argparse
import os
from pathlib import Path

from aiohttp import web

from .auth import set_password
from .server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m roost")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("set-password", help="create or replace the Roost owner password")
    serve = sub.add_parser("serve", help="run the loopback Roost web service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    if args.command == "set-password":
        state = Path(os.environ.get("ROOST_STATE_DIR", "./.roost-state"))
        set_password(state / "password.hash")
    else:
        if os.environ.get("ROOST_PUBLIC_ORIGIN", "").startswith("http://") and args.host not in {"127.0.0.1", "::1", "localhost"}:
            parser.error("An HTTP preview may bind only to loopback.")
        web.run_app(create_app(), host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()

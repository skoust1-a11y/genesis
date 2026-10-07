"""uv run python -m app.auth new-token NAME"""

from __future__ import annotations

import argparse
import re

from app.auth import hash_token, new_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.auth")
    sub = parser.add_subparsers(dest="command", required=True)
    n = sub.add_parser("new-token", help="print a new API token and the API_TOKENS entry for it")
    n.add_argument("name", help="who the token belongs to; becomes the audit actor")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.@\-]{1,64}", args.name):
        parser.exit(2, "error: name may only use letters, digits and _ . @ -\n")
    token = new_token()
    print(f"token (shown once; paste it into the web app's API token field):\n  {token}\n")
    print(f"add to API_TOKENS in .env (comma-separate several):\n  {args.name}:{hash_token(token)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

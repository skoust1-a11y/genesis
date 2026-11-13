"""uv run python -m app.auth new-token NAME
uv run python -m app.auth new-user NAME   (prompts for the password; run again to reset it)"""

from __future__ import annotations

import argparse
import getpass

from dotenv import load_dotenv

from app.auth import USERNAME, hash_token, new_token, set_password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.auth")
    sub = parser.add_subparsers(dest="command", required=True)
    n = sub.add_parser("new-token", help="print a new API token and the API_TOKENS entry for it")
    n.add_argument("name", help="who the token belongs to; becomes the audit actor")
    u = sub.add_parser("new-user", help="create a web login, or reset its password and end its sessions")
    u.add_argument("name", help="the username; becomes the audit actor")
    args = parser.parse_args(argv)
    if not USERNAME.fullmatch(args.name):
        parser.exit(2, "error: name may only use letters, digits and _ . @ -\n")

    if args.command == "new-user":
        from app.db.session import session_scope

        password = getpass.getpass("password: ")
        if password != getpass.getpass("again: "):
            parser.exit(2, "error: the passwords differ\n")
        try:
            with session_scope() as session:
                set_password(session, args.name, password)
        except ValueError as exc:
            parser.exit(2, f"error: {exc}\n")
        print(f"{args.name} can log in at /login")
        return 0

    token = new_token()
    print(f"token (shown once; paste it into the web app's API token field):\n  {token}\n")
    print(f"add to API_TOKENS in .env (comma-separate several):\n  {args.name}:{hash_token(token)}")
    return 0


if __name__ == "__main__":
    load_dotenv()
    raise SystemExit(main())

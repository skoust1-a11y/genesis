"""Explicit, offline provisioning of empty research/paper scaffolding only.

Run with PYTHONPATH=backend python -B -m app.state_provisioning --help.
No startup caller, operational state initialization, or existing-state adoption.
"""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from app.state_authority import AuthorityError, _overlaps, _path


DIRECTORIES = ("config", "risk", "journals", "plans", "databases", "reference", "logs", "exports")
REPOSITORY = Path(__file__).resolve().parents[2]


@contextmanager
def _parent(root):
    """Walk from / using directory descriptors; never follow symlinks."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in root.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if info.st_uid not in (0, os.getuid()):
                raise AuthorityError("Ancestor is owned by another user")
            # System temporary ancestors may be sticky; the immediate parent must be private.
            if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                raise AuthorityError("Writable ancestor is not trusted")
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise AuthorityError("Existing parent must be owned by invoking UID and mode 0700")
        # Reject alternate spellings/case aliases without enumerating or reading contents.
        if Path(root.parent).resolve(strict=True) != root.parent:
            raise AuthorityError("Parent must have its physical canonical spelling")
        try:
            os.stat(root.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise AuthorityError("State root already exists; no adoption or overwrite allowed")
        yield fd, info
    finally:
        os.close(fd)


def provision(*, state_root, approved_root, deployments, apply_plan=None):
    """Return a no-write plan, or create a fresh skeleton with its matching token.

    deployments is the complete pair of (environment, deployment ID, account ID).
    approved_root is an explicit operator attestation, not an authorization service.
    The parent must already exist; this tool never creates or repairs ancestors.
    """
    root = _path(state_root)
    if approved_root != state_root:
        raise AuthorityError("Exact absolute state-root approval is required")
    if _overlaps(root, REPOSITORY):
        raise AuthorityError("State root must be outside and disjoint from the repository")
    rows = sorted(tuple(row) for row in deployments)
    if len(rows) != 2 or any(len(row) != 3 for row in rows):
        raise AuthorityError("Declare both research and paper identities together")
    if [row[0] for row in rows] != ["paper", "research"]:
        raise AuthorityError("Only one research and one paper environment are permitted")
    for _, deployment, account in rows:
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value)
               for value in (deployment, account)):
            raise AuthorityError("Explicit unambiguous deployment/account IDs are required")
    if rows[0][1] == rows[1][1] or rows[0][2] == rows[1][2]:
        raise AuthorityError("Deployment and account identities must be isolated")
    with _parent(root) as (parent_fd, parent_info):
        plan = {
            "version": 1, "state_root": str(root), "owner_uid": os.getuid(),
            "parent_device": parent_info.st_dev, "parent_inode": parent_info.st_ino,
            "live_trading_enabled": False, "telegram_delivery_enabled": False,
            "status": "empty-scaffolding-not-runtime-ready",
            "deployments": [dict(environment=e, deployment_id=d, account_id=a)
                            for e, d, a in rows],
            "directories": list(DIRECTORIES),
        }
        payload = (json.dumps(plan, sort_keys=True, indent=2) + "\n").encode()
        token = hashlib.sha256(payload).hexdigest()
        if apply_plan is not None:
            if apply_plan != token:
                raise AuthorityError("Apply requires the exact reviewed dry-run plan token")
            # No shared process umask changes; explicit modes can only be narrowed by umask.
            os.mkdir(root.name, 0o700, dir_fd=parent_fd)
            root_fd = os.open(root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=parent_fd)
            try:
                os.fchmod(root_fd, 0o700)
                for environment, deployment, _ in rows:
                    os.mkdir(environment, 0o700, dir_fd=root_fd)
                    env_fd = os.open(environment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=root_fd)
                    try:
                        os.fchmod(env_fd, 0o700)
                        os.mkdir(deployment, 0o700, dir_fd=env_fd)
                        deployment_fd = os.open(deployment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                                dir_fd=env_fd)
                        try:
                            os.fchmod(deployment_fd, 0o700)
                            for directory in DIRECTORIES:
                                os.mkdir(directory, 0o700, dir_fd=deployment_fd)
                        finally:
                            os.close(deployment_fd)
                    finally:
                        os.close(env_fd)
                manifest_fd = os.open("provisioning.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                      0o600, dir_fd=root_fd)
                with os.fdopen(manifest_fd, "wb") as manifest:
                    manifest.write(payload)
                    manifest.flush()
                    os.fsync(manifest.fileno())
                os.fsync(root_fd)
                os.fsync(parent_fd)
            finally:
                os.close(root_fd)
            # An interrupted/failed apply is intentionally left for owner inspection, never repaired.
        return {"plan": plan, "plan_token": token, "applied": apply_plan is not None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--approved-root", required=True, help="Owner-approved exact absolute root")
    parser.add_argument("--deployment", nargs=3, action="append", required=True,
                        metavar=("ENVIRONMENT", "DEPLOYMENT_ID", "ACCOUNT_ID"))
    parser.add_argument("--apply-plan", help="Token from reviewed dry-run; omission performs no writes")
    args = parser.parse_args(argv)
    try:
        result = provision(state_root=args.state_root, approved_root=args.approved_root,
                           deployments=args.deployment, apply_plan=args.apply_plan)
    except (AuthorityError, OSError) as error:
        parser.exit(2, f"Provisioning refused: {error}\n")
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

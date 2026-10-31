"""`jhunt-records-delete <bucket> <prefix>`: delete every generation of every
record under a prefix (ADR 0002, "Deleting an application's records").

Owner only. It needs the curator role, so it acts as the owner's gcloud login
(the one that runs infra/gcp/setup.sh), not as jhunt-app:

    gcloud auth login                      # if not already logged in
    jhunt-records-delete jhunt-ci-xxxx tests/

It lists what it will delete and asks you to type the prefix to confirm.
Deleted generations on the records bucket stay recoverable for 30 days (soft
delete); on the CI bucket they are gone at once.
"""

import argparse
import sys

from jhunt.platform.artifacts.curator import delete_all_generations, list_generations, owner_client
from jhunt.platform.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jhunt-records-delete", description=__doc__.split("\n")[0]
    )
    parser.add_argument("bucket")
    parser.add_argument("prefix", help="e.g. applications/<id>/ ; must end with /")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()
    if not args.prefix.endswith("/"):
        # A bare "snapshots/1" would also match "snapshots/10/...".
        raise SystemExit("jhunt-records-delete: the prefix must end with /")

    project = get_settings().gcp_project
    if project is None:
        raise SystemExit("jhunt-records-delete: set GCP_PROJECT")
    client = owner_client(project)
    generations = list_generations(args.bucket, args.prefix, client)
    if not generations:
        print(f"nothing under gs://{args.bucket}/{args.prefix}")
        return
    for gen in generations:
        state = "live" if gen.live else "noncurrent"
        print(f"  gs://{args.bucket}/{gen.name}#{gen.generation} ({state})")
    if not args.yes:
        typed = input(f"Delete these {len(generations)} generations? Type the prefix to confirm: ")
        if typed != args.prefix:
            raise SystemExit("jhunt-records-delete: not confirmed; nothing deleted")
    deleted = delete_all_generations(args.bucket, args.prefix, client)
    print(f"deleted {len(deleted)} generations", file=sys.stderr)

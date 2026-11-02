"""Owner-only operations on records. The app never calls these.

Deleting records needs storage.objects.delete and overrideUnlockedRetention,
which only the owner's curator role has (ADR 0002). So these run as the owner's
own gcloud login (the one that runs infra/gcp/setup.sh), not as jhunt-app,
whose impersonation is what Application Default Credentials hold locally.
"""

import subprocess
from dataclasses import dataclass

from google.cloud import storage
from google.oauth2.credentials import Credentials

from jhunt.platform.artifacts import records


@dataclass(frozen=True)
class Generation:
    name: str
    generation: int
    live: bool  # False: a noncurrent generation left by an earlier delete


def owner_client(project: str) -> storage.Client:
    """A client acting as the owner's gcloud login.

    It uses a short-lived access token from `gcloud auth print-access-token`
    (about an hour, plenty for one command). No key file, and the app's ADC is
    left alone.
    """
    token = subprocess.run(
        ["gcloud", "auth", "print-access-token"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return storage.Client(project=project, credentials=Credentials(token))  # type: ignore[no-untyped-call]


def check_prefix(prefix: str) -> None:
    """Refuse any prefix that could reach beyond one folder.

    An empty prefix, or one of only slashes, would match the whole bucket. One
    without a trailing slash would also match its neighbours ("snapshots/1"
    matches "snapshots/10/").
    """
    if not prefix.strip("/") or not prefix.endswith("/"):
        raise ValueError(
            f"refusing prefix {prefix!r}: it must name a folder and end with '/', "
            "e.g. 'applications/42/'"
        )


def list_generations(bucket_name: str, prefix: str, client: storage.Client) -> list[Generation]:
    """Every generation under the prefix, live and noncurrent."""
    check_prefix(prefix)
    return [
        Generation(blob.name, blob.generation, blob.time_deleted is None)
        for blob in client.list_blobs(
            bucket_name, prefix=prefix, versions=True, timeout=records.TIMEOUT, retry=records.RETRY
        )
        if blob.generation is not None
    ]


def delete_all_generations(
    bucket_name: str, prefix: str, client: storage.Client
) -> list[Generation]:
    """Delete every generation under the prefix (ADR 0002, "Deleting an
    application's records"). Returns what was deleted.

    Deleting only the live version would leave the bytes behind as a noncurrent
    generation, so each generation is handled by its number: release any hold,
    clear its retention (with the override), then delete it.
    """
    check_prefix(prefix)
    bucket = client.bucket(bucket_name)
    deleted = []
    for gen in list_generations(bucket_name, prefix, client):
        blob = bucket.blob(gen.name, generation=gen.generation)
        blob.reload(timeout=records.TIMEOUT, retry=records.RETRY)
        # The app can't set holds, but the owner can, and a held object can't
        # be deleted. Release both kinds first.
        if blob.temporary_hold or blob.event_based_hold:
            blob.temporary_hold = False
            blob.event_based_hold = False
            blob.patch(timeout=records.TIMEOUT)  # no retry: a patch isn't idempotent
        if blob.retention.mode is not None:
            blob.retention.mode = None
            blob.retention.retain_until_time = None
            blob.patch(override_unlocked_retention=True, timeout=records.TIMEOUT)
        blob.delete(timeout=records.TIMEOUT, retry=records.RETRY)  # by generation: idempotent
        deleted.append(gen)
    return deleted

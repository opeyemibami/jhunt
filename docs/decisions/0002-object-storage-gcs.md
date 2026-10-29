# 0002: Object storage on Google Cloud Storage from the start

Status: accepted (owner, 2026-10-29). Replaces the earlier plan to run AIStor Free (MinIO) locally.

## Context

Job description snapshots and submitted documents must never be lost or altered (design: guarantees). The design called for MinIO with versioning and object lock on the owner's server, moving to Cloud Storage after gate G1.

MinIO's community edition was archived in April 2026, and its images are no longer published. The supported single-node option is AIStor Free. We tried it in PR 2. It needs a licence file from MinIO: without one the server starts but refuses every S3 call. That licence would have to be in every environment, CI included. We dropped it for two reasons:

- **Licence friction:** a licence file in every environment, for a store we planned to leave after week 7 anyway.
- **Vendor risk:** a single-vendor free tier whose terms have already changed once and may change again.

## Decision

Artifacts go to real Cloud Storage from week 0, behind the same `jhunt.platform.artifacts` interface. Docker Compose runs no object store. `infra/gcp/setup.sh` creates everything, and the owner runs it.

**Two buckets, in an EU region (default `europe-west1`):**

| Bucket | Holds | Settings |
|---|---|---|
| `jhunt-staging-<suffix>` | Unpicked snapshots, scratch renders | 30-day lifecycle delete; no versioning; no retention |
| `jhunt-records-<suffix>` | Captured snapshots, submissions | Object versioning; retention policy (default 10 years), **unlocked** |

**Identities, with no keys anywhere:**

- `jhunt-app`:
  - `roles/storage.objectCreator` and `roles/storage.objectViewer` on both buckets
  - a custom role with only `storage.objects.delete`, on staging only
  - nothing that sets holds, changes retention or edits bucket settings
- Locally, the owner impersonates `jhunt-app` through Application Default Credentials (`gcloud auth application-default login --impersonate-service-account=...`). `GOOGLE_APPLICATION_CREDENTIALS` points at that config, which is not a service account key.
- `jhunt-ci`: trusted only by GitHub Actions runs from this repo, through Workload Identity Federation. The match is on the numeric repository id, so a renamed or re-created repo of the same name isn't trusted. It has no bucket roles yet.

## How a record is protected

Each layer is checked by `infra/gcp/check-objectstore.sh`:

1. **IAM.** Replacing an object needs `storage.objects.create` and `storage.objects.delete`. `jhunt-app` has no delete on records, so it can neither overwrite nor delete a record, whatever the retention settings.
2. **Retention policy.** No principal, the owner included, can delete or replace a generation younger than the retention period. The policy is unlocked, so the owner can remove it to delete a record on purpose (design: "kept until you delete them", "one command deletes everything for an application").
3. **Versioning.** A principal that *does* have delete can still make a retained live object noncurrent: a plain delete or a new upload succeeds. The original bytes stay as a noncurrent generation. That's why layer 1 matters: the app must never get delete on records.
4. **Soft delete** (GCS default, 7 days). Anything that does get deleted can be restored for a week.

## Consequences

- **Network dependency.** Tests and local runs that touch storage need network access and the owner's ADC. "Never a fake object store" (AGENTS.md) now means real GCS buckets.
- **Owner deletion leaves a window.** Deleting a record means clearing the policy for the whole bucket, briefly. Per-object retention (Object Retention Lock, unlocked mode) would let the owner unlock one object without that window. Revisit it when the export/delete-application command is built.
- **CI and the records bucket.** CI writing test records into the real records bucket would leave objects nobody can delete without lifting retention. Before CI touches storage (PR 6 or later), decide between separate CI buckets and a prefix in staging only.
- **Migration after G1 changes nothing for storage.** Only where the containers run moves.
- **Offline fallback.** If storage is ever needed without GCS (offline work, an outage), run SeaweedFS behind the same `jhunt.platform.artifacts` interface as an S3-compatible backend. Check that its object lock and versioning behaviour meets the guarantee before relying on it.
- **Settings to confirm:** the 10-year retention default and the `europe-west1` region are both script parameters.

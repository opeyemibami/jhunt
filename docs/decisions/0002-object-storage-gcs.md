# 0002: Object storage on Google Cloud Storage from the start

Status: accepted (owner, 2026-10-29). Replaces the earlier plan to run AIStor Free (MinIO) locally.

## Context

Job description snapshots and submitted documents must never be lost or altered (design: guarantees). The design called for MinIO with versioning and object lock on the owner's server, moving to Cloud Storage after gate G1.

MinIO's community edition was archived in April 2026, and its images are no longer published. The supported single-node option is AIStor Free. We tried it in PR 2. It needs a licence file from MinIO: without one the server starts but refuses every S3 call. That licence would have to be in every environment, CI included. We dropped it for two reasons:

- **Licence friction:** a licence file in every environment, for a store we planned to leave after week 7 anyway.
- **Vendor risk:** a single-vendor free tier whose terms have already changed once and may change again.

## Decision

Artifacts go to real Cloud Storage from week 0, behind the same `jhunt.platform.artifacts` interface. Docker Compose runs no object store. `infra/gcp/setup.sh` creates everything, and the owner runs it. Region: `europe-west1`.

| Bucket | Holds | Settings |
|---|---|---|
| `jhunt-staging-<suffix>` | Unpicked snapshots, scratch renders | 30-day lifecycle delete; no versioning; no retention |
| `jhunt-records-<suffix>` | Captured snapshots, submissions | Object versioning; per-object retention enabled at creation; **no bucket retention policy** |

**Every record gets its own retention configuration when it is written:** Unlocked mode, retain-until three years after the write. `jhunt.platform.artifacts` sets this on every records upload; it is not a bucket default.

We chose per-object retention over a bucket retention policy for one reason. To delete one application's records, the owner removes retention on those objects only. A bucket policy would have to be lifted for the whole bucket, leaving every record unprotected for that moment.

### Permissions

The permission names below come from the GCS docs (IAM permissions reference, upload "Required permissions", and Object Retention Lock).

| Permission | What it allows | jhunt-app on records | Owner on records |
|---|---|---|---|
| `storage.objects.create` | Upload a new object | yes (`roles/storage.objectCreator`) | yes |
| `storage.objects.get`, `.list` | Read and list | yes (`roles/storage.objectViewer`) | yes |
| `storage.objects.setRetention` | Add or update an object's retention; needed for an upload that carries retention | yes (custom `jhuntRetentionSetter`) | yes |
| `storage.objects.overrideUnlockedRetention` | Shorten, remove or lock an existing Unlocked configuration (the `--override-unlocked-retention` flag). The change is a PATCH, so it also needs `storage.objects.update` | **no** | yes |
| `storage.objects.delete` | Delete an object; also needed to replace (overwrite) one | **no** | yes |
| `storage.objects.update` | Edit metadata, including temporary and event-based holds | **no** | yes |
| `storage.buckets.update` | Change bucket settings | **no** | (project owner) |

On staging, `jhunt-app` has create, get, list and delete (custom `jhuntObjectDeleter`), and nothing about retention. The owner's records permissions come from the custom role `jhuntRecordsCurator`, bound on the records bucket only.

**Identities, with no keys anywhere:**

- Locally, the owner impersonates `jhunt-app` through Application Default Credentials (`gcloud auth application-default login --impersonate-service-account=...`). `GOOGLE_APPLICATION_CREDENTIALS` points at that config, which is not a service account key.
- `jhunt-ci` is trusted only by GitHub Actions runs from this repo, through Workload Identity Federation. The match is on the numeric repository id, so a renamed or re-created repo of the same name isn't trusted. It has no bucket roles yet.

## How a record is protected

Each layer is checked by `infra/gcp/check-objectstore.sh`:

1. **IAM: the guarantee.** `jhunt-app` has no `delete`, `update` or `overrideUnlockedRetention` on records. So it cannot:
   - delete, overwrite or make a record noncurrent;
   - set a hold;
   - shorten, remove or lock a record's retention.

   This gap is AGENTS.md hard rule 13. Observed on the real buckets (2026-10-29): GCS refuses a retention change or hold for lack of `storage.objects.update` before it checks `overrideUnlockedRetention`. The app has neither, so either refusal holds.
2. **Per-object retention: the guard against mistakes.** Until retain-until, nobody can delete or replace that generation without first removing its retention, which needs `overrideUnlockedRetention`. That stops an accidental delete by the owner or any future account with delete.
3. **Versioning.** A principal with `delete` can still make a retained live object noncurrent. The bytes stay as a noncurrent generation. That's another reason the app must never get `delete` on records.
4. **Soft delete** (GCS default, 7 days). Anything that does get deleted can be restored for a week.

## Residual risk

`setRetention` lets an upload carry any retention configuration. IAM can't limit the mode or the date. A bug in `jhunt.platform.artifacts` could therefore write a record in **Locked** mode, or with a retain-until far in the future. The record would stay safe, but the owner couldn't delete it until that date, which breaks "one command deletes everything for an application". The mitigations:

- The artifacts module hard-codes Unlocked mode and three years.
- Its tests assert both.
- The app cannot lock an *existing* record, because that needs `overrideUnlockedRetention`. The check script proves this.

## Consequences

- **Network dependency.** Tests and local runs that touch storage need network access and the owner's ADC. "Never a fake object store" (AGENTS.md) now means real GCS buckets.
- **No bucket-wide window.** Deleting one application's records is per object: remove retention, then delete. Nothing else is unprotected while it happens.
- **CI and the records bucket.** CI writing test records into the real records bucket would need cleanup with the owner's override permission. Before CI touches storage (PR 6 or later), decide between separate CI buckets and a prefix in staging only.
- **Migration after G1 changes nothing for storage.** Only where the containers run moves.
- **Offline fallback.** If storage is ever needed without GCS (offline work, an outage), run SeaweedFS behind the same `jhunt.platform.artifacts` interface as an S3-compatible backend. Check that its object lock and versioning behaviour meets the guarantee before relying on it.

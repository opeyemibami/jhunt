# infra/gcp

Object storage and CI identity on Google Cloud (ADR 0002). The owner runs these scripts with their own gcloud login; the coding agent never does.

| File | What it does |
|---|---|
| `setup.sh` | Creates the two buckets, the `jhunt-app` and `jhunt-ci` service accounts, three small custom roles, and Workload Identity Federation for GitHub Actions. Safe to re-run. |
| `staging-lifecycle.json` | Staging bucket rule: delete objects older than 30 days. |
| `check-objectstore.sh` | Proves the setup: what `jhunt-app` can and cannot do on each bucket, and that the owner can unlock and delete one record while another stays protected. Cleans up after itself. |

What gets created:

| Resource | Settings |
|---|---|
| `gs://jhunt-staging-<suffix>` | Uniform access, public access blocked, 30-day lifecycle delete, no versioning, no retention, soft delete off |
| `gs://jhunt-ci-staging-<suffix>` | Tests only: set up like staging. `jhunt-app` and `jhunt-ci` get staging's roles (create, view, delete), so tests can stage and delete uploads (ADR 0018) |
| `gs://jhunt-records-<suffix>` | Uniform access, public access blocked, object versioning, per-object retention enabled, **no** bucket retention policy, 30-day soft delete |
| `jhunt-app` | Both buckets: `objectCreator` + `objectViewer`. Staging: `jhuntObjectDeleter`. Records: `jhuntRetentionSetter` |
| Your account | Records: `jhuntRecordsCurator`. Also `serviceAccountTokenCreator` on `jhunt-app`, so you can impersonate it instead of using a key |
| `jhunt-ci` | Trusted by GitHub Actions runs from this repo (matched on numeric repo id); no bucket roles yet |

| Custom role | Permissions |
|---|---|
| `jhuntObjectDeleter` | `storage.objects.delete` |
| `jhuntRetentionSetter` | `storage.objects.setRetention` |
| `jhuntRecordsCurator` | `storage.objects.get`, `.list`, `.update`, `.delete`, `.setRetention`, `.overrideUnlockedRetention` |

No service account keys are created.

## Run order

```bash
gcloud auth login
gcloud config set project <project-id>

# 1. Create everything. SUFFIX makes bucket names globally unique (lowercase letters and digits).
GCP_PROJECT=<project-id> SUFFIX=<suffix> infra/gcp/setup.sh

# 2. Copy the printed GCP_PROJECT, GCS_STAGING_BUCKET and GCS_RECORDS_BUCKET into .env.

# 3. Local app credentials: impersonate jhunt-app, no key file.
gcloud auth application-default login \
  --impersonate-service-account=jhunt-app@<project-id>.iam.gserviceaccount.com
#    Put the credentials path it prints into GOOGLE_APPLICATION_CREDENTIALS in .env.

# 4. Prove it. Wait a couple of minutes after step 1 so new IAM bindings apply.
GCP_PROJECT=<project-id> GCS_STAGING_BUCKET=jhunt-staging-<suffix> \
  GCS_RECORDS_BUCKET=jhunt-records-<suffix> infra/gcp/check-objectstore.sh
```

If section 2 of the check fails with a permission error, IAM hasn't caught up yet: wait and re-run. Section 2 must pass before the refusals in section 3 mean anything. Each refusal must match its expected error (the missing permission, or retention), and must be a 403 wherever gcloud prints a status.

## Deleting a record

Each record carries its own Unlocked retention (3 years from when it was written). Records is versioned, so one record can have several generations, each with its own retention. Deleting only the live version leaves the data in place as a noncurrent generation. Clear retention on **every** generation and delete each one. Nothing else in the bucket is affected:

```bash
for url in $(gcloud storage ls --all-versions 'gs://jhunt-records-<suffix>/<path>'); do
  gcloud storage objects update "$url" --clear-retention --override-unlocked-retention
  gcloud storage rm "$url"
done
```

Each deleted generation stays soft-deleted, and restorable, for 30 days. That's the undo window. The week-1 per-application delete command does the same for every object of an application (ADR 0002).

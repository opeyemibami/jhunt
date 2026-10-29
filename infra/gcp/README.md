# infra/gcp

Object storage and CI identity on Google Cloud (ADR 0002). The owner runs these scripts with their own gcloud login; the coding agent never does.

| File | What it does |
|---|---|
| `setup.sh` | Creates the two buckets, the `jhunt-app` and `jhunt-ci` service accounts, a one-permission custom role, and Workload Identity Federation for GitHub Actions. Safe to re-run. |
| `staging-lifecycle.json` | Staging bucket rule: delete objects older than 30 days. |
| `check-objectstore.sh` | Proves the setup: what `jhunt-app` can and cannot do on each bucket, and that the owner can still delete a record after removing retention. |

What gets created:

| Resource | Settings |
|---|---|
| `gs://jhunt-staging-<suffix>` | Uniform access, public access blocked, 30-day lifecycle delete, no versioning, no retention |
| `gs://jhunt-records-<suffix>` | Uniform access, public access blocked, object versioning, retention policy (default 10 years), **not locked** |
| `jhunt-app` | `objectCreator` + `objectViewer` on both buckets; `jhuntObjectDeleter` (only `storage.objects.delete`) on staging |
| `jhunt-ci` | Trusted by GitHub Actions runs from this repo (matched on numeric repo id); no bucket roles yet |
| Your account | `serviceAccountTokenCreator` on `jhunt-app`, so you can impersonate it locally instead of using a key |

No service account keys are created. Both buckets keep GCS's default 7-day soft delete.

## Run order

```bash
gcloud auth login

# 1. Create everything. SUFFIX makes bucket names globally unique.
GCP_PROJECT=<project-id> SUFFIX=<suffix> infra/gcp/setup.sh
#    Optional: REGION=europe-west2 (default europe-west1), RECORDS_RETENTION=5y (default 10y)

# 2. Copy the printed GCP_PROJECT, GCS_STAGING_BUCKET and GCS_RECORDS_BUCKET into .env.

# 3. Local app credentials: impersonate jhunt-app, no key file.
gcloud auth application-default login \
  --impersonate-service-account=jhunt-app@<project-id>.iam.gserviceaccount.com
#    Put the path it prints into GOOGLE_APPLICATION_CREDENTIALS in .env.

# 4. Prove it.
GCP_PROJECT=<project-id> GCS_STAGING_BUCKET=jhunt-staging-<suffix> \
  GCS_RECORDS_BUCKET=jhunt-records-<suffix> infra/gcp/check-objectstore.sh
```

New IAM bindings can take a minute or two to apply. If section 2 of the check fails with a permission error, wait and re-run; section 2 must pass before the refusals in section 3 mean anything.

## Deleting a record

Records can't be deleted by the app at all, and can't be deleted by anyone while the retention policy is on. To delete one yourself:

```bash
gcloud storage buckets update gs://jhunt-records-<suffix> --clear-retention-period
gcloud storage rm 'gs://jhunt-records-<suffix>/<path>#<generation>'
gcloud storage buckets update gs://jhunt-records-<suffix> --retention-period=10y
```

While the policy is cleared, the whole bucket is unprotected, so keep that window short. `check-objectstore.sh` does exactly this for its own test object and restores the policy even if it fails part-way.

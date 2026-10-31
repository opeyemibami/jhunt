#!/usr/bin/env bash
# One-time GCP setup for jhunt object storage and CI identity (ADR 0002).
# The owner runs this with their own gcloud login. Safe to re-run: every step
# checks before it creates, custom roles are reset to their listed permissions,
# and IAM bindings are idempotent.
#
#   GCP_PROJECT=my-project SUFFIX=abc123 infra/gcp/setup.sh
#
# Optional: REGION (default europe-west1), GITHUB_REPO and GITHUB_REPO_ID
# (default: this repo).
#
# Creates:
#   gs://jhunt-staging-$SUFFIX  30-day lifecycle delete; no versioning, no retention, soft delete off
#   gs://jhunt-records-$SUFFIX  versioning + per-object retention enabled; NO bucket retention
#                               policy; 30-day soft delete
#   gs://jhunt-ci-$SUFFIX       set up like records (so tests prove the real protections), but
#                               soft delete off; tests and CI write here, never to records
#   jhunt-app   staging: create, view, delete
#               records: create, view, and set retention at write time; nothing else
#   owner       records: may remove an object's Unlocked retention and delete that object
#   jhunt-ci    trusted by GitHub Actions for this repo via Workload Identity Federation
#   ci bucket   jhunt-app and jhunt-ci: the app's records roles exactly (create, view,
#               set retention on write); owner: the records curator role, to clear test records
# It never creates a service account key and never sets a bucket retention policy.
set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT}"
: "${SUFFIX:?set SUFFIX (lowercase letters/digits; bucket names are global)}"
REGION="${REGION:-europe-west1}"
GITHUB_REPO="${GITHUB_REPO:-opeyemibami/jhunt}"
# Numeric ids don't change if the repo is renamed or a same-named repo appears.
GITHUB_REPO_ID="${GITHUB_REPO_ID:-1438565653}"

P=(--project="$GCP_PROJECT")
STAGING="jhunt-staging-${SUFFIX}"
RECORDS="jhunt-records-${SUFFIX}"
CI_BUCKET="jhunt-ci-${SUFFIX}"
APP_SA="jhunt-app@${GCP_PROJECT}.iam.gserviceaccount.com"
CI_SA="jhunt-ci@${GCP_PROJECT}.iam.gserviceaccount.com"
ROLE_PREFIX="projects/${GCP_PROJECT}/roles"
POOL=github
HERE="$(cd "$(dirname "$0")" && pwd)"

step() { echo; echo "== $*"; }
# New service accounts take a few seconds to become visible to IAM; retry bindings.
retry() { local n; for n in 1 2 3 4 5; do "$@" && return 0; echo "   retrying in 5s ($n/5)"; sleep 5; done; return 1; }
bind_bucket() {  # bind_bucket <bucket> <member> <role>
  retry gcloud storage buckets add-iam-policy-binding "gs://$1" "${P[@]}" --member="$2" --role="$3" >/dev/null
}
# custom_role <id> <title> <comma-separated permissions>: create, or reset to exactly these permissions.
custom_role() {
  if gcloud iam roles describe "$1" "${P[@]}" >/dev/null 2>&1; then
    gcloud iam roles update "$1" "${P[@]}" --title="$2" --permissions="$3" --stage=GA --quiet >/dev/null
  else
    gcloud iam roles create "$1" "${P[@]}" --title="$2" --permissions="$3" --stage=GA >/dev/null
  fi
  echo "   ${1}: $3"
}

OWNER="$(gcloud config get-value account 2>/dev/null)"
[[ -n "$OWNER" ]] || { echo "no active gcloud account; run: gcloud auth login" >&2; exit 1; }
PROJECT_NUMBER="$(gcloud projects describe "$GCP_PROJECT" --format='value(projectNumber)')"
echo "project ${GCP_PROJECT} (${PROJECT_NUMBER}), region ${REGION}, owner ${OWNER}"

step "enable APIs"
gcloud services enable storage.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com "${P[@]}"

step "bucket gs://${STAGING}"
if ! gcloud storage buckets describe "gs://${STAGING}" "${P[@]}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${STAGING}" "${P[@]}" --location="$REGION" \
    --default-storage-class=STANDARD --uniform-bucket-level-access --public-access-prevention
fi
# Soft delete off: expired postings should go for good, not linger (and bill) for 7 days.
gcloud storage buckets update "gs://${STAGING}" "${P[@]}" \
  --lifecycle-file="${HERE}/staging-lifecycle.json" --no-versioning --clear-retention-period \
  --clear-soft-delete

step "bucket gs://${RECORDS}"
if ! gcloud storage buckets describe "gs://${RECORDS}" "${P[@]}" >/dev/null 2>&1; then
  # Per-object retention is switched on here, at creation.
  gcloud storage buckets create "gs://${RECORDS}" "${P[@]}" --location="$REGION" \
    --default-storage-class=STANDARD --uniform-bucket-level-access --public-access-prevention \
    --enable-per-object-retention
fi
# No bucket-level retention policy: each record carries its own (ADR 0002).
# 30-day soft delete: the owner's undo window after deleting an application's records.
gcloud storage buckets update "gs://${RECORDS}" "${P[@]}" --versioning --clear-retention-period \
  --soft-delete-duration=30d
per_object="$(gcloud storage buckets describe "gs://${RECORDS}" "${P[@]}" --format=json |
  python3 -c 'import json,sys; print((json.load(sys.stdin).get("per_object_retention") or {}).get("mode", ""))')"
if [[ "$per_object" != "Enabled" ]]; then
  echo "gs://${RECORDS} exists without per-object retention (got '${per_object}')." >&2
  echo "It can only be enabled at creation: delete the bucket and re-run." >&2
  exit 1
fi

step "bucket gs://${CI_BUCKET} (tests; set up like records)"
if ! gcloud storage buckets describe "gs://${CI_BUCKET}" "${P[@]}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${CI_BUCKET}" "${P[@]}" --location="$REGION" \
    --default-storage-class=STANDARD --uniform-bucket-level-access --public-access-prevention \
    --enable-per-object-retention
fi
# Like records, except soft delete is off: cleared test records should go for good.
gcloud storage buckets update "gs://${CI_BUCKET}" "${P[@]}" --versioning --clear-retention-period \
  --clear-soft-delete
per_object="$(gcloud storage buckets describe "gs://${CI_BUCKET}" "${P[@]}" --format=json |
  python3 -c 'import json,sys; print((json.load(sys.stdin).get("per_object_retention") or {}).get("mode", ""))')"
if [[ "$per_object" != "Enabled" ]]; then
  echo "gs://${CI_BUCKET} exists without per-object retention (got '${per_object}')." >&2
  echo "It can only be enabled at creation: delete the bucket and re-run." >&2
  exit 1
fi

step "service accounts"
for sa in jhunt-app jhunt-ci; do
  if ! gcloud iam service-accounts describe "${sa}@${GCP_PROJECT}.iam.gserviceaccount.com" "${P[@]}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$sa" "${P[@]}" --display-name="$sa"
  fi
done

step "custom roles"
custom_role jhuntObjectDeleter "jhunt staging deleter" storage.objects.delete
# Lets an upload carry a retention configuration. Reducing, removing or locking
# an existing one also needs storage.objects.overrideUnlockedRetention, which
# jhunt-app never gets (AGENTS.md hard rule 13).
custom_role jhuntRetentionSetter "jhunt set retention on write" storage.objects.setRetention
custom_role jhuntRecordsCurator "jhunt records curator (owner)" \
  storage.objects.get,storage.objects.list,storage.objects.update,storage.objects.delete,storage.objects.setRetention,storage.objects.overrideUnlockedRetention

step "jhunt-app bucket roles"
# objectCreator: create only. Replacing an existing object also needs
# storage.objects.delete, so without it the app cannot overwrite a record.
# Nothing here grants storage.objects.update (holds) or overrideUnlockedRetention.
for b in "$STAGING" "$RECORDS"; do
  bind_bucket "$b" "serviceAccount:${APP_SA}" roles/storage.objectCreator
  bind_bucket "$b" "serviceAccount:${APP_SA}" roles/storage.objectViewer
done
bind_bucket "$STAGING" "serviceAccount:${APP_SA}" "${ROLE_PREFIX}/jhuntObjectDeleter"
bind_bucket "$RECORDS" "serviceAccount:${APP_SA}" "${ROLE_PREFIX}/jhuntRetentionSetter"

step "owner ${OWNER} on records: remove one object's retention, then delete it"
bind_bucket "$RECORDS" "user:${OWNER}" "${ROLE_PREFIX}/jhuntRecordsCurator"

step "CI bucket: jhunt-app and jhunt-ci get the app's records roles, the owner the curator role"
# Exactly the app's records permissions, so tests run with what the app really
# has: create, view, set retention on write. No delete, update or override.
for sa in "$APP_SA" "$CI_SA"; do
  bind_bucket "$CI_BUCKET" "serviceAccount:${sa}" roles/storage.objectCreator
  bind_bucket "$CI_BUCKET" "serviceAccount:${sa}" roles/storage.objectViewer
  bind_bucket "$CI_BUCKET" "serviceAccount:${sa}" "${ROLE_PREFIX}/jhuntRetentionSetter"
done
# The owner clears test records with jhunt-records-delete (ADR 0002).
bind_bucket "$CI_BUCKET" "user:${OWNER}" "${ROLE_PREFIX}/jhuntRecordsCurator"

step "let ${OWNER} impersonate jhunt-app (local ADC and the proof script; no keys)"
retry gcloud iam service-accounts add-iam-policy-binding "$APP_SA" "${P[@]}" \
  --member="user:${OWNER}" --role=roles/iam.serviceAccountTokenCreator >/dev/null

step "Workload Identity Federation for GitHub Actions"
if ! gcloud iam workload-identity-pools describe "$POOL" "${P[@]}" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL" "${P[@]}" --location=global \
    --display-name="GitHub Actions"
fi
if ! gcloud iam workload-identity-pools providers describe github "${P[@]}" --location=global \
    --workload-identity-pool="$POOL" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc github "${P[@]}" --location=global \
    --workload-identity-pool="$POOL" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository_id=='${GITHUB_REPO_ID}'"
fi
retry gcloud iam service-accounts add-iam-policy-binding "$CI_SA" "${P[@]}" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository_id/${GITHUB_REPO_ID}" \
  >/dev/null
# jhunt-ci's only bucket roles are on the CI bucket (above).

step "done"
cat <<EOF
Add to .env:
  GCP_PROJECT=${GCP_PROJECT}
  GCS_STAGING_BUCKET=${STAGING}
  GCS_RECORDS_BUCKET=${RECORDS}
  GCS_TEST_BUCKET=${CI_BUCKET}
For GitHub Actions, repository variables (not secrets):
  GCP_WORKLOAD_IDENTITY_PROVIDER=projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/github
  GCP_CI_SERVICE_ACCOUNT=${CI_SA}
  GCS_TEST_BUCKET=${CI_BUCKET}
Next: infra/gcp/check-objectstore.sh (see infra/gcp/README.md).
EOF

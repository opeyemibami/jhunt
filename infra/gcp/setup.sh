#!/usr/bin/env bash
# One-time GCP setup for jhunt object storage and CI identity (ADR 0002).
# The owner runs this with their own gcloud login. Safe to re-run: every step
# checks before it creates, and IAM bindings are idempotent.
#
#   GCP_PROJECT=my-project SUFFIX=abc123 infra/gcp/setup.sh
#
# Optional: REGION (default europe-west1), RECORDS_RETENTION (default 10y),
# GITHUB_REPO and GITHUB_REPO_ID (default: this repo).
#
# Creates:
#   gs://jhunt-staging-$SUFFIX  30-day lifecycle delete, no versioning, no retention
#   gs://jhunt-records-$SUFFIX  object versioning + retention policy, NOT locked
#   jhunt-app   objectCreator + objectViewer on both buckets, delete on staging only
#   jhunt-ci    trusted by GitHub Actions for this repo via Workload Identity Federation
# It never creates a service account key and never locks a retention policy.
set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT}"
: "${SUFFIX:?set SUFFIX (lowercase letters/digits; bucket names are global)}"
REGION="${REGION:-europe-west1}"
RECORDS_RETENTION="${RECORDS_RETENTION:-10y}"
GITHUB_REPO="${GITHUB_REPO:-opeyemibami/jhunt}"
# Numeric ids don't change if the repo is renamed or a same-named repo appears.
GITHUB_REPO_ID="${GITHUB_REPO_ID:-1438565653}"

P=(--project="$GCP_PROJECT")
STAGING="jhunt-staging-${SUFFIX}"
RECORDS="jhunt-records-${SUFFIX}"
APP_SA="jhunt-app@${GCP_PROJECT}.iam.gserviceaccount.com"
CI_SA="jhunt-ci@${GCP_PROJECT}.iam.gserviceaccount.com"
DELETER_ROLE="projects/${GCP_PROJECT}/roles/jhuntObjectDeleter"
POOL=github
HERE="$(cd "$(dirname "$0")" && pwd)"

step() { echo; echo "== $*"; }
# New service accounts take a few seconds to become visible to IAM; retry bindings.
retry() { local n; for n in 1 2 3 4 5; do "$@" && return 0; echo "   retrying in 5s ($n/5)"; sleep 5; done; return 1; }

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
gcloud storage buckets update "gs://${STAGING}" "${P[@]}" \
  --lifecycle-file="${HERE}/staging-lifecycle.json" --no-versioning --clear-retention-period

step "bucket gs://${RECORDS}"
if ! gcloud storage buckets describe "gs://${RECORDS}" "${P[@]}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${RECORDS}" "${P[@]}" --location="$REGION" \
    --default-storage-class=STANDARD --uniform-bucket-level-access --public-access-prevention
fi
# Unlocked on purpose: the owner can still remove the policy to delete a record.
# Never pass --lock-retention-period here; locking is permanent.
gcloud storage buckets update "gs://${RECORDS}" "${P[@]}" \
  --versioning --retention-period="$RECORDS_RETENTION"

step "service accounts"
for sa in jhunt-app jhunt-ci; do
  if ! gcloud iam service-accounts describe "${sa}@${GCP_PROJECT}.iam.gserviceaccount.com" "${P[@]}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$sa" "${P[@]}" --display-name="$sa"
  fi
done

step "custom role jhuntObjectDeleter (storage.objects.delete only)"
if ! gcloud iam roles describe jhuntObjectDeleter "${P[@]}" >/dev/null 2>&1; then
  gcloud iam roles create jhuntObjectDeleter "${P[@]}" --title="jhunt object deleter" \
    --permissions=storage.objects.delete --stage=GA
fi

step "jhunt-app bucket roles"
# objectCreator: create only. Replacing an existing object also needs
# storage.objects.delete, so without it the app cannot overwrite a record.
# Neither role can set holds, change retention or edit bucket settings.
for b in "$STAGING" "$RECORDS"; do
  for role in roles/storage.objectCreator roles/storage.objectViewer; do
    retry gcloud storage buckets add-iam-policy-binding "gs://${b}" "${P[@]}" \
      --member="serviceAccount:${APP_SA}" --role="$role" >/dev/null
  done
done
retry gcloud storage buckets add-iam-policy-binding "gs://${STAGING}" "${P[@]}" \
  --member="serviceAccount:${APP_SA}" --role="$DELETER_ROLE" >/dev/null

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
# jhunt-ci gets no bucket roles yet: week 0 CI does not touch storage.

step "done"
cat <<EOF
Add to .env:
  GCP_PROJECT=${GCP_PROJECT}
  GCS_STAGING_BUCKET=${STAGING}
  GCS_RECORDS_BUCKET=${RECORDS}
For GitHub Actions (PR 6), repository variables, not secrets:
  GCP_WORKLOAD_IDENTITY_PROVIDER=projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/github
  GCP_CI_SERVICE_ACCOUNT=${CI_SA}
Next: infra/gcp/check-objectstore.sh (see infra/gcp/README.md).
EOF

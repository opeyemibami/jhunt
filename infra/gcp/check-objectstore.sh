#!/usr/bin/env bash
# Proves the GCS setup from ADR 0002. The owner runs it with their own gcloud
# login; app-side checks impersonate jhunt-app (no keys).
#
#   GCP_PROJECT=... GCS_STAGING_BUCKET=... GCS_RECORDS_BUCKET=... infra/gcp/check-objectstore.sh
#
# 1. Bucket settings are what setup.sh asked for.
# 2. As jhunt-app on staging: write, overwrite and delete all work.
# 3. As jhunt-app on records: write and read work; overwrite, delete, holds and
#    retention changes are all refused; the original bytes and sha256 survive.
# 4. As the owner on records: deleting the record is refused while the
#    retention policy is on, and succeeds once it is removed. The policy is put
#    back straight after, even if the script fails part-way.
#
# Leaves nothing behind except soft-deleted copies (kept 7 days by default).
# Exits non-zero if any check fails.
set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT}"
: "${GCS_STAGING_BUCKET:?set GCS_STAGING_BUCKET}"
: "${GCS_RECORDS_BUCKET:?set GCS_RECORDS_BUCKET}"

P=(--project="$GCP_PROJECT")
APP_SA="jhunt-app@${GCP_PROJECT}.iam.gserviceaccount.com"
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
staging_obj="gs://${GCS_STAGING_BUCKET}/_proof/${run_id}.txt"
record="gs://${GCS_RECORDS_BUCKET}/_proof/${run_id}.txt"
tmp="$(mktemp -d)"
failures=0
retention_cleared=""
original_retention_s=""

as_app() { gcloud "${P[@]}" --impersonate-service-account="$APP_SA" --verbosity=error "$@"; }
as_owner() { gcloud "${P[@]}" --verbosity=error "$@"; }
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; failures=$((failures + 1)); }
last_line() { printf '%s' "$1" | tr -s '\n' | tail -n 1; }
# expect_ok <description> <command...>: the command must succeed.
expect_ok() {
  local what="$1"; shift
  local out
  if out="$("$@" 2>&1)"; then pass "$what"; else fail "${what}: $(last_line "$out")"; return 1; fi
}
# expect_refused <description> <command...>: the command must fail.
expect_refused() {
  local what="$1"; shift
  local out
  if out="$("$@" 2>&1)"; then fail "${what} was allowed"; else pass "${what} refused: $(last_line "$out")"; fi
}
sha_of() { "$@" | sha256sum | cut -d' ' -f1; }
# bucket_field <bucket> <python expression over d, the describe JSON>
bucket_field() {
  as_owner storage buckets describe "gs://$1" --format=json |
    python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$2"
}

restore_retention() {
  if [[ -n "$retention_cleared" ]]; then
    echo "      restoring retention policy (${original_retention_s}s) on gs://${GCS_RECORDS_BUCKET}"
    as_owner storage buckets update "gs://${GCS_RECORDS_BUCKET}" --retention-period="PT${original_retention_s}S"
    retention_cleared=""
  fi
  rm -rf "$tmp"
}
trap restore_retention EXIT

echo "== 1. bucket settings"
if [[ "$(bucket_field "$GCS_RECORDS_BUCKET" 'd.get("versioning_enabled")')" == "True" ]]; then
  pass "records: versioning on"; else fail "records: versioning off"; fi
original_retention_s="$(bucket_field "$GCS_RECORDS_BUCKET" '(d.get("retention_policy") or {}).get("retentionPeriod", "")')"
if [[ -n "$original_retention_s" && "$original_retention_s" != "0" ]]; then
  pass "records: retention policy ${original_retention_s}s"; else fail "records: no retention policy"; fi
if [[ "$(bucket_field "$GCS_RECORDS_BUCKET" '(d.get("retention_policy") or {}).get("isLocked", False)')" == "False" ]]; then
  pass "records: retention policy is NOT locked"; else fail "records: retention policy is locked"; fi
if [[ "$(bucket_field "$GCS_STAGING_BUCKET" 'd.get("retention_policy")')" == "None" ]]; then
  pass "staging: no retention policy"; else fail "staging: has a retention policy"; fi
if [[ "$(bucket_field "$GCS_STAGING_BUCKET" '[(r["action"]["type"], r["condition"].get("age")) for r in (d.get("lifecycle_config") or {}).get("rule", [])]')" == "[('Delete', 30)]" ]]; then
  pass "staging: lifecycle deletes at 30 days"; else fail "staging: lifecycle is not a single 30-day delete"; fi

echo "== 2. jhunt-app on staging"
printf 'staging %s\n' "$run_id" > "$tmp/a.txt"
printf 'staging v2 %s\n' "$run_id" > "$tmp/b.txt"
# These must succeed first: they prove impersonation and the app's roles are
# live, so the refusals in section 3 are policy, not missing setup.
expect_ok "write" as_app storage cp "$tmp/a.txt" "$staging_obj" || true
expect_ok "overwrite" as_app storage cp "$tmp/b.txt" "$staging_obj" || true
expect_ok "delete" as_app storage rm "$staging_obj" || true

echo "== 3. jhunt-app on records"
printf 'original record %s\n' "$run_id" > "$tmp/orig.txt"
printf 'tampered record %s\n' "$run_id" > "$tmp/tampered.txt"
want_sha="$(sha256sum "$tmp/orig.txt" | cut -d' ' -f1)"
expect_ok "write" as_app storage cp "$tmp/orig.txt" "$record" || exit 1
gen="$(as_app storage objects describe "$record" --format='value(generation)')"
got_sha="$(sha_of as_app storage cat "${record}#${gen}")"
if [[ "$got_sha" == "$want_sha" ]]; then
  pass "read back generation ${gen}, sha256 ${want_sha}"
else
  fail "read back sha256 ${got_sha}"
fi

expect_refused "overwrite" as_app storage cp "$tmp/tampered.txt" "$record"
expect_refused "delete (live object)" as_app storage rm "$record"
expect_refused "delete (generation ${gen})" as_app storage rm "${record}#${gen}"
expect_refused "set temporary hold" as_app storage objects update "$record" --temporary-hold
expect_refused "clear bucket retention" as_app storage buckets update "gs://${GCS_RECORDS_BUCKET}" --clear-retention-period
expect_refused "shorten bucket retention" as_app storage buckets update "gs://${GCS_RECORDS_BUCKET}" --retention-period=1s

live_gen="$(as_app storage objects describe "$record" --format='value(generation)')"
after_sha="$(sha_of as_app storage cat "${record}#${gen}")"
if [[ "$live_gen" == "$gen" && "$after_sha" == "$want_sha" ]]; then
  pass "original survives: live generation ${gen}, sha256 unchanged"
else
  fail "original changed: live generation ${live_gen}, sha256 ${after_sha}"
fi

echo "== 4. owner on records"
expect_refused "owner delete while retention is on" as_owner storage rm "${record}#${gen}"
echo "      removing retention policy (unlocked, so the owner may)"
retention_cleared=yes
as_owner storage buckets update "gs://${GCS_RECORDS_BUCKET}" --clear-retention-period
expect_ok "owner delete after retention removed" as_owner storage rm "${record}#${gen}" || true
restore_retention
if [[ "$(bucket_field "$GCS_RECORDS_BUCKET" '(d.get("retention_policy") or {}).get("retentionPeriod", "")')" == "$original_retention_s" ]]; then
  pass "retention policy restored to ${original_retention_s}s"; else fail "retention policy NOT restored"; fi

echo
if (( failures > 0 )); then echo "${failures} check(s) failed"; exit 1; fi
echo "all checks passed"

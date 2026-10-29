#!/usr/bin/env bash
# Proves the GCS setup from ADR 0002. The owner runs it with their own gcloud
# login; app-side checks impersonate jhunt-app (no keys).
#
#   GCP_PROJECT=... GCS_STAGING_BUCKET=... GCS_RECORDS_BUCKET=... infra/gcp/check-objectstore.sh
#
# 1. Bucket settings are what setup.sh asked for.
# 2. As jhunt-app on staging: write, overwrite and delete all work.
# 3. As jhunt-app on records: write two records with 3-year Unlocked retention.
#    Delete, overwrite, shortening, removing or locking retention, and holds are
#    all refused; bytes and sha256 are unchanged.
# 4. As the owner: deleting a retained record is refused; removing retention on
#    record A and deleting it works; record B stays protected and intact.
#
# On exit (pass or fail) it removes everything it wrote. The two test records
# then stay soft-deleted for 30 days (records' undo window); staging keeps
# nothing. Exits non-zero if any check fails.
set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT}"
: "${GCS_STAGING_BUCKET:?set GCS_STAGING_BUCKET}"
: "${GCS_RECORDS_BUCKET:?set GCS_RECORDS_BUCKET}"

P=(--project="$GCP_PROJECT")
APP_SA="jhunt-app@${GCP_PROJECT}.iam.gserviceaccount.com"
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
staging_obj="gs://${GCS_STAGING_BUCKET}/_proof/${run_id}.txt"
rec_a="gs://${GCS_RECORDS_BUCKET}/_proof/${run_id}-a.txt"
rec_b="gs://${GCS_RECORDS_BUCKET}/_proof/${run_id}-b.txt"
tmp="$(mktemp -d)"
failures=0

# RFC 3339 timestamps, computed in Python so this runs the same on Linux and macOS.
ts() { python3 -c 'import datetime as d,sys; print((d.datetime.now(d.timezone.utc)+d.timedelta(days=float(sys.argv[1]))).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$1"; }
retain_3y="$(ts 1096)"   # 3 years, the record retention the app will use
retain_1d="$(ts 1)"

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
# Refusal reasons, matched against gcloud's ERROR line. A permission refusal
# names the missing permission; `storage cp` prints it without an HTTP status,
# other commands as "HTTPError 403: ...". A retention refusal is always a 403.
DENIED_DELETE='does not have storage\.objects\.delete access'
# Changing an object's retention or holds is a PATCH: GCS checks
# storage.objects.update first, then overrideUnlockedRetention. Either refusal holds.
DENIED_UPDATE='does not have storage\.objects\.(update|overrideUnlockedRetention) access'
RETAINED='HTTPError 403: Object .* is subject to .*retention'

# expect_refused <description> <reason regex> <command...>: the command must fail,
# its ERROR line must match the reason, and any HTTP status on it must be 403.
# Any other failure (say a 404 because an earlier step already broke the record)
# is not proof and counts as a FAIL.
expect_refused() {
  local what="$1" reason="$2"; shift 2
  local out line
  if out="$("$@" 2>&1)"; then
    fail "${what} was allowed"
    return
  fi
  line="$(printf '%s\n' "$out" | grep -m1 '^ERROR:' || true)"
  line="${line%% This command is authenticated*}"
  if [[ "$line" =~ HTTPError\ ([0-9]{3}) && "${BASH_REMATCH[1]}" != 403 ]]; then
    fail "${what} failed with HTTP ${BASH_REMATCH[1]}, not 403: ${line}"
  elif [[ "$line" =~ $reason ]]; then
    pass "${what} refused: ${line}"
  else
    fail "${what} failed for another reason: ${line:-$(last_line "$out")}"
  fi
}
sha_of() { "$@" | sha256sum | cut -d' ' -f1; }
# json_eval <python expression over d> reads describe JSON on stdin.
json_eval() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
bucket_field() { as_owner storage buckets describe "gs://$1" --format=json | json_eval "$2"; }
# object_field <url> <expr>: prints "missing" if the object has no live version.
object_field() {
  local j
  j="$(as_owner storage objects describe "$1" --format=json 2>/dev/null)" || { echo missing; return 0; }
  json_eval "$2" <<<"$j"
}

cleanup() {
  set +e
  echo; echo "== cleanup"
  as_owner storage rm "$staging_obj" >/dev/null 2>&1
  local obj url
  for obj in "$rec_a" "$rec_b"; do
    # Every generation, live or not, may carry its own retention.
    for url in $(as_owner storage ls --all-versions "$obj" 2>/dev/null); do
      as_owner storage objects update "$url" --clear-retention --override-unlocked-retention >/dev/null 2>&1
      as_owner storage rm "$url" >/dev/null 2>&1
    done
    if as_owner storage ls --all-versions "$obj" >/dev/null 2>&1; then
      echo "      could not remove ${obj}; remove it by hand"
    else
      echo "      removed ${obj}"
    fi
  done
  rm -rf "$tmp"
}
trap cleanup EXIT

echo "== 1. bucket settings"
if [[ "$(bucket_field "$GCS_RECORDS_BUCKET" 'd.get("versioning_enabled")')" == "True" ]]; then
  pass "records: versioning on"; else fail "records: versioning off"; fi
if [[ "$(bucket_field "$GCS_RECORDS_BUCKET" '(d.get("per_object_retention") or {}).get("mode")')" == "Enabled" ]]; then
  pass "records: per-object retention enabled"; else fail "records: per-object retention not enabled"; fi
if [[ "$(bucket_field "$GCS_RECORDS_BUCKET" 'd.get("retention_policy")')" == "None" ]]; then
  pass "records: no bucket retention policy"; else fail "records: has a bucket retention policy"; fi
soft_records="$(bucket_field "$GCS_RECORDS_BUCKET" '(d.get("soft_delete_policy") or {}).get("retentionDurationSeconds", "0")')"
if [[ "$soft_records" == "2592000" ]]; then
  pass "records: soft delete 30 days"; else fail "records: soft delete ${soft_records}s, want 2592000s (30 days)"; fi
if [[ "$(bucket_field "$GCS_STAGING_BUCKET" 'd.get("retention_policy")')" == "None" ]]; then
  pass "staging: no retention policy"; else fail "staging: has a retention policy"; fi
# Disabled soft delete shows as a 0-second duration or no policy at all.
soft_staging="$(bucket_field "$GCS_STAGING_BUCKET" '(d.get("soft_delete_policy") or {}).get("retentionDurationSeconds", "0")')"
if [[ "$soft_staging" == "0" ]]; then
  pass "staging: soft delete off"; else fail "staging: soft delete ${soft_staging}s, want off"; fi
if [[ "$(bucket_field "$GCS_STAGING_BUCKET" '[(r["action"]["type"], r["condition"].get("age")) for r in (d.get("lifecycle_config") or {}).get("rule", [])]')" == "[('Delete', 30)]" ]]; then
  pass "staging: lifecycle deletes at 30 days"; else fail "staging: lifecycle is not a single 30-day delete"; fi

echo "== 2. jhunt-app on staging"
printf 'staging %s\n' "$run_id" > "$tmp/s1.txt"
printf 'staging v2 %s\n' "$run_id" > "$tmp/s2.txt"
# These must succeed first: they prove impersonation and the app's roles are
# live, so the refusals in section 3 are policy, not missing setup.
expect_ok "write" as_app storage cp "$tmp/s1.txt" "$staging_obj" || true
expect_ok "overwrite" as_app storage cp "$tmp/s2.txt" "$staging_obj" || true
expect_ok "delete" as_app storage rm "$staging_obj" || true

echo "== 3. jhunt-app on records"
printf 'record A %s\n' "$run_id" > "$tmp/a.txt"
printf 'record B %s\n' "$run_id" > "$tmp/b.txt"
printf 'tampered %s\n' "$run_id" > "$tmp/x.txt"
sha_a="$(sha256sum "$tmp/a.txt" | cut -d' ' -f1)"
sha_b="$(sha256sum "$tmp/b.txt" | cut -d' ' -f1)"
expect_ok "write record A with Unlocked retention until ${retain_3y}" \
  as_app storage cp "$tmp/a.txt" "$rec_a" --retain-until="$retain_3y" --retention-mode=Unlocked || exit 1
expect_ok "write record B with Unlocked retention until ${retain_3y}" \
  as_app storage cp "$tmp/b.txt" "$rec_b" --retain-until="$retain_3y" --retention-mode=Unlocked || exit 1
gen_a="$(object_field "$rec_a" 'd["generation"]')"
gen_b="$(object_field "$rec_b" 'd["generation"]')"
mode_a="$(object_field "$rec_a" '(d.get("retention_settings") or {}).get("mode")')"
until_a="$(object_field "$rec_a" '(d.get("retention_settings") or {}).get("retainUntilTime")')"
if [[ "$mode_a" == "Unlocked" && "$until_a" == "${retain_3y%Z}"* ]]; then
  pass "record A retention: ${mode_a} until ${until_a}"
else
  fail "record A retention: mode ${mode_a}, until ${until_a}"
fi

expect_refused "overwrite A" "$DENIED_DELETE" as_app storage cp "$tmp/x.txt" "$rec_a"
expect_refused "delete A (live)" "$DENIED_DELETE" as_app storage rm "$rec_a"
expect_refused "delete A (generation ${gen_a})" "$DENIED_DELETE" as_app storage rm "${rec_a}#${gen_a}"
expect_refused "shorten A's retention" "$DENIED_UPDATE" \
  as_app storage objects update "$rec_a" --retain-until="$retain_1d" --override-unlocked-retention
expect_refused "remove A's retention" "$DENIED_UPDATE" \
  as_app storage objects update "$rec_a" --clear-retention --override-unlocked-retention
expect_refused "lock A's retention" "$DENIED_UPDATE" \
  as_app storage objects update "$rec_a" --retain-until="$retain_3y" --retention-mode=Locked --override-unlocked-retention
expect_refused "set temporary hold on A" "$DENIED_UPDATE" as_app storage objects update "$rec_a" --temporary-hold
expect_refused "set event-based hold on A" "$DENIED_UPDATE" as_app storage objects update "$rec_a" --event-based-hold

for pair in "A ${rec_a} ${gen_a} ${sha_a}" "B ${rec_b} ${gen_b} ${sha_b}"; do
  read -r name obj gen want <<<"$pair"
  live="$(object_field "$obj" 'd["generation"]')"
  got="$(sha_of as_app storage cat "${obj}#${gen}")"
  mode="$(object_field "$obj" '(d.get("retention_settings") or {}).get("mode")')"
  if [[ "$live" == "$gen" && "$got" == "$want" && "$mode" == "Unlocked" ]]; then
    pass "record ${name} intact: generation ${gen}, sha256 ${want}, retention ${mode}"
  else
    fail "record ${name} changed: live ${live}, sha256 ${got}, retention ${mode}"
  fi
done

echo "== 4. owner on records"
expect_refused "owner delete of A while retained" "$RETAINED" as_owner storage rm "${rec_a}#${gen_a}"
expect_ok "owner removes A's retention" \
  as_owner storage objects update "$rec_a" --clear-retention --override-unlocked-retention || true
expect_ok "owner deletes A" as_owner storage rm "${rec_a}#${gen_a}" || true
if as_owner storage objects describe "$rec_a" >/dev/null 2>&1; then fail "A still exists"; else pass "A is gone"; fi
expect_refused "owner delete of B (retention untouched)" "$RETAINED" as_owner storage rm "${rec_b}#${gen_b}"
got_b="$(sha_of as_owner storage cat "${rec_b}#${gen_b}")"
mode_b="$(object_field "$rec_b" '(d.get("retention_settings") or {}).get("mode")')"
if [[ "$got_b" == "$sha_b" && "$mode_b" == "Unlocked" ]]; then
  pass "record B still protected and intact"
else
  fail "record B: sha256 ${got_b}, retention ${mode_b}"
fi

echo
if (( failures > 0 )); then echo "${failures} check(s) failed"; exit 1; fi
echo "all checks passed"

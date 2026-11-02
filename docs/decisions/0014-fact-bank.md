# 0014: The fact bank and `jhunt-facts`

Status: proposed in PR 3.5. The owner decided the open points on 2026-11-01: TOML with `[[fact]]` tables; the fields; strict validation for verified facts; design.md's five kinds; and that import never retires.

## Context

Every drafted claim must cite one or more facts, and a claim with no supporting fact is blocked (hard rule 6; design.md, "The fact bank rule"). The drafts come in 3.6; this PR builds the bank they cite.

The facts live in the owner's own TOML file, outside git, and are imported into Postgres with `jhunt-facts import` (backlog decision). design.md's Fact is: id, kind (role, project, metric, skill, education), text, evidence link, verified_at. It is "edited by you only; history kept". A Facts page in the browser comes later (backlog).

## Decision

**The file** is TOML, with one `[[fact]]` table per fact (owner).

| Field | Rule |
|---|---|
| `id` | required; the owner's own stable id, e.g. `role-acme-2021`: a letter or digit, then letters, digits, `.`, `_`, `:` or `-` (at most 100) |
| `kind` | required; `role`, `project`, `metric`, `skill` or `education` |
| `text` | required, non-empty |
| `status` | required; `draft` or `verified` |
| `evidence` | a link or a note; **required when verified** |
| `verified_at` | a TOML date or `"YYYY-MM-DD"`, not in the future; **required when verified** |
| `aliases` | skills only, optional: a list of the names the skill goes by, e.g. `["Kubernetes", "k8s"]`. The claim validator uses them to find mentions of the skill (ADR 0015). Migration 0011 adds the column. A fact without aliases keeps the hash it had before, so existing facts don't look changed |
| `level` | skills only: `studied`, `used`, `built` or `production`. **Required for a verified skill.** A draft skill may leave it out or say `"TODO"`, stored as no level. **Refused on any other kind.** |

Any other key, or any other top-level table, is refused. `[[facts]]` gets a hint to write `[[fact]]`.

**Validation is all-or-nothing.** Every problem in the file is listed, with each fact's id and position, and if there are any, nothing is written. Draft facts may be incomplete, but they can never be cited.

**Storage: the `facts` table** (migration 0010) is **append-only**, like the other records of what's true (hard rule 8).
- One row per version: `fact_id`, `version` (1, 2, …; unique per fact), `kind`, `text`, `evidence`, `verified_at`, `status` (`draft`, `verified`, `retired`), `level`, `content_sha256`, `created_by` (`owner`), `created_at`.
- **A row is never changed.** An edit is a new version, and older versions stay, so a draft keeps citing the version it used (3.6 cites the row id).

**`jhunt-facts import FILE`** is safe to re-run (owner).
1. **It refuses a file inside a git repository**: facts never go in git.
2. It reads and validates the whole file.
3. It compares each fact with its latest version, using a sha256 of its fields:
   - **new**: version 1;
   - **changed**: the next version;
   - **unchanged**: skipped.
4. It lists, and never changes:
   - **imported facts missing from the file**; they are retired only by `jhunt-facts retire`;
   - **retired facts still in the file**, which stay retired. Import never brings a retired fact back.
5. It prints the plan. If anything is to be written, it asks for `yes`, then writes all the versions in one transaction, holding a lock on `facts` so two imports can't number a version twice.

**`jhunt-facts retire ID`**: a new version with status `retired`, its other fields copied from the latest. A retired fact is never citable again unless a later version un-retires it, and only the Facts page (later) will do that.

**`jhunt-facts list`**: each fact's latest version, with its status.

**Citable**: `citable_facts()` returns each fact's latest version **when its status is `verified`** (owner). Drafts, retired facts and older versions are never offered to new drafts. 3.6's validator checks citations against this.

The code is `jhunt.platform.memory.facts`, where AGENTS.md puts the fact bank, written as plain SQL. The command is `jhunt.app.facts`.

## Consequences

- A fact's text can't be fixed in place. A typo means a new version; the wrong version stays in the history.
- Only the owner writes facts, through `jhunt-facts`. Nothing stops other code from writing to the table until the database role split (backlog, "Before migration"), as for the other append-only tables.
- The importer checks shape, not truth. A verified fact is as good as its evidence.

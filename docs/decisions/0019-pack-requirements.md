# 0019: Requirements match and gap list for the Application pack

Status: accepted. The owner approved the plan on 2026-11-03 and asked for it to be built in the same PR.

## Context

A pack draft (ADR 0016) is aimed at the job only through the prompt's "address the job's stated requirements". The owner can't see what the job asks for, which asks they meet, and which they don't. That view helps them decide whether to apply. It also gives the draft a list to aim at.

## Decision

**Application pack graph v2: `requirements` → `draft`.** Version 1 (`draft` only) stays registered and unchanged (hard rule 12). Threads started on v1 finish on v1. New drafts start on v2, the latest version.

**`requirements`** calls a new tool, `extract_requirements`, through the gateway (step key `requirements`).
- It runs under a new autonomy action `extract_requirements`, at level 0 (migration 0019). Like `draft_pack`, it only stores a suggestion for the owner (`suggests=True`).
- **Model route**: `"pack.requirements" = "anthropic:claude-sonnet-5-5"`.
- **Input**: the owner's citable facts, then the job description wrapped and labelled as untrusted data (hard rule 7), as for the draft.
- **Reply**, JSON only: a list of `{"id", "text", "quote", "kind": "must"|"nice", "match": "met"|"partial"|"gap", "fact_ids", "why"}`. The model is asked for at most 15 requirements, near-duplicates merged, with boilerplate, benefits and legal text left out.

**Checks, all in code; nothing trusts the model:**

| Check | What happens |
|---|---|
| The reply isn't JSON of that shape | Nothing is stored, and the run fails with a clear message (`RequirementsReplyError`, a `PackReplyError`) |
| More than 15 requirements | The first 15 are kept; the rest count as dropped |
| `quote` isn't in the job description, compared after collapsing whitespace and ignoring case | The requirement is dropped and counted, never shown as real |
| A fact id is unknown or not citable (not the latest verified version) | That id is dropped |
| `met` or `partial` with no valid facts left | It becomes `gap` |
| `gap` with fact ids | The ids are removed: a gap cites nothing |

**Storage** (migration 0019), both append-only with the same triggers as `draft_claims` (no UPDATE, DELETE or TRUNCATE):
- **`pack_requirements`**: one row per kept requirement, keyed by the draft's thread: `thread_id`, `position`, `text`, `quote`, `kind`, `match`, `fact_ids`, `why`.
- **`pack_requirement_checks`**: one row per thread, holding the **count of dropped requirements** and the provider and model used.
  - It also records that the check ran, so zero kept requirements reads differently from "no check".
  - The step runs before the draft row exists, so neither table can hang off `pack_drafts`.
  - Written once per thread; a re-run of the step gets the gateway's recorded result.

**The draft uses the requirements.** When a thread has them, `pack.draft` and `pack.repair` get them as data:
- `met` and `partial` items, with their fact ids;
- gaps, under "do not claim these".

The requirements are wrapped in `<requirements>`, since their text comes from the posting. The prompt gains two rules:
- aim the lines at the must items first;
- never write anything that claims or hints at a gap.

- every cover-note item keeps its `group` key (see Consequences).

These rules are only added when there are requirements, and they sit before the reply shape. A v1 thread's prompt is unchanged, byte for byte. **The claim validator is unchanged and has the final say.**

**Output limits**: 8000 tokens for `pack.requirements`, and for `pack.draft` and `pack.repair` when a thread has requirements. Live runs cut off at 3000 and 4000. v1 keeps 4000.

**Pages**:
- **Draft page**: a Requirements section above Accepted. It shows gaps first, then partial, then met. Each requirement shows its text, must or nice, its facts, and "why" on a muted line. A one-line summary sits on top, e.g. "must: 5 met, 2 partial, 1 gap".
- **Application page**: each draft's row shows its summary.
- **v1 drafts**: they show "No requirements check (drafted before v2)".

## Consequences

- A draft costs one more model call (Sonnet): three at most, with the repair. Drafts also got longer in the live check: 25 to 27 accepted lines, against 18 for the last v1 draft on Fin.
- "Met" and "partial" are the model's judgement, checked only for real, citable facts. A wrong "met" can't add a claim, because every drafted claim still goes through the validator.
- A gap is a gap in the fact bank, not necessarily in the owner. A skill the owner has but never added shows as a gap until it's added.
- **The match varies between runs.** The same job can get a different split into requirements and a different match. Cohere's travel requirement was a gap in most runs and left out altogether in one. The list guides a decision; it isn't a stable score.
- **A merged requirement can hide a gap.** The model may merge separate asks into one partial. On Fin, CUDA/Triton sits inside "training or inference at scale, or low-level GPU coding", which is partial in every run, because the job says "or" and other facts meet the rest. The missing part is named only in the "why" line.
- **Drafts sometimes fail on the reply shape.** In the live check, first drafts left out the cover-note `group` key on all sentences in 3 of 7 runs on the current prompt, on byte-identical requests, which fails the run with nothing stored (`PackReplyError`). One earlier run failed as "not JSON"; its raw reply wasn't kept. The v1 prompt had 0 failures in 3 runs and the prompt with the explicit `group` line 0 in 5, which is too few runs to call it fixed. If it recurs, the backlog has the next step.


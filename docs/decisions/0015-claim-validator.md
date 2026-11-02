# 0015: The claim validator: no claim without a fact

Status: proposed in PR 3.6a. The owner decided the rules on 2026-11-02. The drafting that feeds it comes in 3.6b, routed to `pack.draft` = `anthropic:claude-sonnet-5-5`.

## Context

Hard rule 6: every drafted claim cites one or more fact ids, nothing that isn't in the fact bank is ever added, and a claim with no supporting fact is blocked, not reworded. design.md, "The fact bank rule", adds that numbers must match the fact exactly (no rounding up) and that a skill must never be stated above its verified level. The fact bank is ADR 0014.

## Decision

**A claim is `{text, fact_ids}`** (owner). `fact_ids` are the owner's own ids (`skill-langgraph`). The validator resolves each to that fact's **latest version**, and a checked claim keeps those versions' row ids. A draft therefore keeps pointing at the exact version it was checked against (ADR 0014).

**`check_claims(claims, facts, levels)`** in `jhunt.platform.memory.claims` is pure: no database and no model. It's given every fact's latest version (`latest_facts`) and the level words. It returns every claim, **in order and with its text unchanged**, either **accepted** or **blocked** with the reasons. It never rewords, drops or merges a claim. A claim is blocked for each of these, and every reason that applies is listed:

1. **No fact**: `fact_ids` is empty.
2. **Unknown fact**: an id with no version in the bank.
3. **Not verified**: the cited fact's latest version is `draft` or `retired`. Only verified facts are citable (owner, ADR 0014).
4. **A number not in its cited facts** (owner: digits must match; spelled-out numbers are blocked):
   - Every number written in digits must appear in the text of one of the claim's cited facts, as the same number with the same unit, if any. That covers `40%`, `1.5M`, `3x`, `2021`, `10,000`, `10k` and so on.
   - `10,000` and `10000` are the same number, and `×` counts as `x`. `40%` doesn't match a fact's `40 engineers`, and `41%` doesn't match `40%`.
   - A number inside a word (`B2B`, `S3`) isn't read as a number.
   - A spelled-out number (`one` to `twenty`, the tens, `hundred(s)`, `thousand(s)`, `million(s)`, `billion(s)`, `dozen(s)`) is blocked unless the same word is in a cited fact, so wording can't carry a number the facts don't have.
5. **A skill stated above its level** (owner: a word list, strict; mentioned, not just cited):
   - `config/claim_levels.toml` (in git, reviewed like code) lists the words that state each level: `studied` < `used` < `built` < `production`.
   - A claim's stated level is the **highest** level whose words it contains, matched as whole words and ignoring case.
   - The claim is blocked if that level is above the level of **any verified skill it cites or mentions** (owner, PR 3.6a review). So "Built pipelines with LangGraph and Kubernetes" is blocked when Kubernetes is only `used`. "Deployed and operated Kubernetes in production" citing only a project fact is blocked too, with the reason marked `(mentioned, not cited)`: a skill can't borrow a level by citing a different fact.
   - **A mention** is any of the skill fact's `aliases` (`aliases = ["Kubernetes", "k8s"]`, skills only; ADR 0014, migration 0011), found as a whole word, ignoring case.
     - A skill without aliases is named by its text, split on `and`, `,`, `/` and parentheses: "Python (FastAPI, asyncio)" names Python, FastAPI and asyncio.
     - When a skill has aliases, they replace its text for this.
   - **A claim that mentions a draft or retired skill is blocked** (owner, PR 3.6a review), whatever its wording: `mentions unverified skill 'skill-rust' (draft)`. Naming a skill the bank doesn't vouch for adds that skill. A cited one is already blocked as not verified, and is reported once.
   - A claim that uses no level words, or neither cites nor mentions a skill, has no level to check.
   - `config/claim_levels.toml` is approved as proposed. The owner tunes it after the first real drafts.

**The guarantee**: "No claim without a fact", in `tests/guarantees/test_fact_bank.py`. It checks, against facts imported into a scratch database, that each kind of unsupported claim is blocked with its reason and that a blocked claim's text is never changed.

## Limits

- **It checks citations, numbers and level words, not meaning.** A claim can cite the right fact and still say something the fact doesn't. "Led the team" can cite a fact that only says "member of the team". Catching that needs a judge model or evals, both deferred (backlog: weeks 17 to 18). The owner reads every draft before using it; drafting is level 0, suggest only.
- **The level words are a list.** A wording that isn't in it states no level. The list is tuned in config, like the fit score's targets.
- **A skill absent from the fact bank can't be detected.** The validator finds mentions only of skills the bank has, verified or not. A claim naming a skill the owner never entered (say "Terraform", with no Terraform fact) passes, if nothing else blocks it. Catching that needs the judge or evals, like meaning. Listing a skill as a draft fact is how to make sure it's caught.
- **Mentions are found by name.** A skill called by a name that isn't in its aliases or its text (say "K8s" when the only alias is "Kubernetes") isn't recognised. Aliases fix that.
- **A short name can over-match.** "Go" also matches the verb, and an alias like "R" would be worse. Aliases that would match ordinary words belong in neither the aliases nor the fact's text, since the text is the fallback.
- **Spelled-out numbers include `one`**, so "one of the first engineers" is blocked unless a cited fact says "one". The drafting prompt (3.6b) asks for digits, and a blocked claim shows why it was blocked.

# Job sources

Where Scout looks for postings, how it may access each source, and what was checked. This is the seed data for the `sources` table that comes in week 3 (`docs/backlog.md`). After that, this file stays as the record of which terms were checked.

Checked on 2026-10-31.

## How this was checked

- **Hiring software:** identified only from links on each company's own careers pages, such as an apply link or a job board link. Where the careers page could not be read, or showed no such link, the hiring software is left unconfirmed, not guessed.
- **API sources:** Greenhouse and Ashby boards only. Each public board endpoint was confirmed with **one GET**: no pagination, no retries. Neither API paginates its jobs list. Greenhouse's docs say: "Job Board data is publicly available, so authentication is not required for any GET endpoints."
- **Email alert only:** companies on Workday, Workable or Phenom, and companies with their own careers site. Their undocumented JSON endpoints were not used.
- **No Lever boards:** none of these companies' careers pages linked a Lever board. Mistral now uses Ashby.
- **"Irish or remote" in the Notes column:**
  - Irish means any role located in Ireland (Dublin, Cork, Galway, Limerick).
  - Remote means a remote role open to Ireland, Europe, EMEA or worldwide. Remote roles tied to a single non-Irish country or city don't count.
  - API rows give counts from that one response. Alert-only rows give what the careers pages showed, or "unknown" when the listings are rendered by JavaScript or blocked.
- **Terms:** for API rows, the provider's API docs. For alert-only rows, nothing automated is accessed; the date records when the careers page was checked.

Greenhouse docs: <https://developers.greenhouse.io/job-board.html>. Ashby docs: <https://developers.ashbyhq.com/docs/public-job-posting-api> (page updated 2026-10-23; no authentication; no pagination or rate limit stated).

## Companies: Dublin and Ireland

| Source | Kind | Access | Endpoint or alert | Terms checked | Notes (Irish or remote roles) |
|---|---|---|---|---|---|
| Google | Own site | Email alert | Job alerts on <https://www.google.com/about/careers/applications/> | Careers page, 2026-10-31 | Unknown: listings are rendered by JavaScript |
| Microsoft | Own site (apply.careers.microsoft.com) | Email alert | Job alerts on <https://careers.microsoft.com/> | Careers page, 2026-10-31 | Unknown: listings are rendered by JavaScript |
| Meta | Own site | Email alert | Job alerts on <https://www.metacareers.com/> | Careers page, 2026-10-31 | Unknown: listings are rendered by JavaScript |
| Amazon (AWS) | Own site | Email alert | Job alerts on <https://www.amazon.jobs/> | Careers page, 2026-10-31 | Unknown: the Dublin location page returned 404 |
| Anthropic | Greenhouse (`anthropic`) | API | `GET https://boards-api.greenhouse.io/v1/boards/anthropic/jobs`: 200 JSON, 675 jobs | [Greenhouse Job Board API](https://developers.greenhouse.io/job-board.html), 2026-10-31 | **Yes**: 10 in Dublin (e.g. "Account Executive, Startups"). Remote roles are US or Canada only |
| OpenAI | Unconfirmed | Email alert | Job alerts on <https://openai.com/careers/> | Careers page blocked automated fetches (403), 2026-10-31 | Unknown: page blocked |
| Huawei Ireland Research Centre | Own site (career.huawei.com) | Email alert | <https://career.huawei.com/>, linked from <https://www.huawei.com/ie/> | Careers page, 2026-10-31 | Unknown: no listings rendered |
| Stripe | Own site | Email alert | Job alerts on <https://stripe.com/ie/careers> | Careers page, 2026-10-31 | Unknown: the first results page (unfiltered) had no Irish roles. Postings carry Greenhouse ids, but applications go to stripe.com and no board is linked |
| Intercom | Greenhouse (`intercom`) | API | `GET https://boards-api.greenhouse.io/v1/boards/intercom/jobs`: 200 JSON, 109 jobs | [Greenhouse Job Board API](https://developers.greenhouse.io/job-board.html), 2026-10-31 | **Yes**: 41 in Dublin. The careers page is now <https://fin.ai/careers> |
| Workday | Workday (`workday.wd5`, site `Workday`) | Email alert | Job alerts on <https://workday.wd5.myworkdayjobs.com/Workday> | Careers page, 2026-10-31 | Unknown: listings are rendered by JavaScript |
| Salesforce | Own site | Email alert | Job alerts on <https://careers.salesforce.com/> | Careers page, 2026-10-31 | Unknown: listings are rendered by JavaScript |
| HubSpot | Greenhouse (`hubspotjobs`) | API | `GET https://boards-api.greenhouse.io/v1/boards/hubspotjobs/jobs`: 200 JSON, 84 jobs | [Greenhouse Job Board API](https://developers.greenhouse.io/job-board.html), 2026-10-31 | **Yes**: 18 in Ireland, including 7 "Remote - Ireland". The board is identified by the careers page's Greenhouse job-alert link |
| Datadog | Own site | Email alert | Job alerts on <https://careers.datadoghq.com/> | Careers page, 2026-10-31 | **Yes**: the Dublin hub page shows 19 positions (titles not rendered) |
| Cloudflare | Own site | Email alert | Job alerts on <https://www.cloudflare.com/careers/jobs/> | Careers page, 2026-10-31 | Unknown: listings are rendered by JavaScript |
| Fenergo | Workable (`fenergocareers`) | Email alert | Job alerts on <https://apply.workable.com/fenergocareers/> | Careers page, 2026-10-31 | Unknown: not checked. Workable isn't one of the API providers in scope |
| Tines | Own site | Email alert | Job alerts on <https://www.tines.com/careers> | Careers page, 2026-10-31 | **Yes**: Dublin is its European headquarters, e.g. "Customer Success Manager II - EMEA, Ireland (Remote)". Job ids look like Greenhouse ids, but no board is linked |
| Wayflyer | Ashby (`wayflyer`) | API | `GET https://api.ashbyhq.com/posting-api/job-board/wayflyer`: 200 JSON, 15 jobs | [Ashby posting API](https://developers.ashbyhq.com/docs/public-job-posting-api), 2026-10-31 | **Yes**: 6 in Dublin and 3 remote in Europe |
| Workhuman | Ashby (`Workhuman`) | API | `GET https://api.ashbyhq.com/posting-api/job-board/Workhuman`: 200 JSON, 9 jobs | [Ashby posting API](https://developers.ashbyhq.com/docs/public-job-posting-api), 2026-10-31 | **Yes**: 4 in Ireland |
| Zendesk | Workday (`zendesk.wd1`, site `zendesk`) | Email alert | Job alerts on <https://zendesk.wd1.myworkdayjobs.com/zendesk> | Careers page, 2026-10-31 | Unknown: listings are rendered by JavaScript |
| Coinbase | Unconfirmed | Email alert | Job alerts on <https://www.coinbase.com/careers> | Careers page blocked automated fetches (403), 2026-10-31 | Unknown: page blocked |
| Toast | Own site (careers.toasttab.com) | Email alert | Job alerts on <https://careers.toasttab.com/> | Careers page, 2026-10-31 | **Yes**: 4 in Dublin, e.g. "Senior Software Engineer - Infrastructure" |
| Qualtrics | Phenom (own site) | Email alert | Job alerts on <https://www.qualtrics.com/careers/> | Careers page, 2026-10-31 | Unknown: listings are rendered by JavaScript |
| Pinterest | Own site | Email alert | Job alerts on <https://www.pinterestcareers.com/> | Careers page, 2026-10-31 | **Yes**: Dublin roles, e.g. "Manager I, Revenue Operations". Postings carry `gh_jid`, but no Greenhouse board is linked |
| LinkedIn | Own site (postings on linkedin.com) | Email alert | LinkedIn job alerts for company "LinkedIn" | [User agreement](https://www.linkedin.com/legal/user-agreement), 2026-10-31 | Unknown: only one LinkedIn-own Dublin listing seen, a "register your interest" post |

## Companies: EU AI-native

| Source | Kind | Access | Endpoint or alert | Terms checked | Notes (Irish or remote roles) |
|---|---|---|---|---|---|
| Mistral AI | Ashby (`mistral.ai`) | API | `GET https://api.ashbyhq.com/posting-api/job-board/mistral.ai`: 200 JSON, 229 jobs | [Ashby posting API](https://developers.ashbyhq.com/docs/public-job-posting-api), 2026-10-31 | **No**: 0 in Ireland. Remote-flagged roles are tied to Paris, London or the US |
| Cohere | Ashby (`cohere`) | API | `GET https://api.ashbyhq.com/posting-api/job-board/cohere`: 200 JSON, 122 jobs | [Ashby posting API](https://developers.ashbyhq.com/docs/public-job-posting-api), 2026-10-31 | **Yes**: 11 remote in Europe (e.g. "Forward Deployed Engineer, Agentic Platform (Europe)"). 0 in Ireland |
| ElevenLabs | Own site | Email alert | Job alerts on <https://elevenlabs.io/careers> | Careers page, 2026-10-31 | **Yes**: "Remote; Ireland" roles, e.g. "Full-Stack Engineer (Backend Leaning)" |
| Synthesia | Ashby (`synthesia`) | API | `GET https://api.ashbyhq.com/posting-api/job-board/synthesia`: 200 JSON, 32 jobs | [Ashby posting API](https://developers.ashbyhq.com/docs/public-job-posting-api), 2026-10-31 | **Yes**: 5 remote in Europe. 0 in Ireland. The careers page itself calls this endpoint |
| PolyAI | Greenhouse, EU-hosted (`job-boards.eu.greenhouse.io/polyai`) | Email alert | Job alerts on <https://job-boards.eu.greenhouse.io/polyai> | [Greenhouse Job Board API](https://developers.greenhouse.io/job-board.html), 2026-10-31 | Unknown. The docs give no API host for EU-hosted boards, and `boards-api.eu.greenhouse.io` doesn't resolve, so this stays alert only until Greenhouse documents one |
| DeepL | Ashby (`DeepL`) | API | `GET https://api.ashbyhq.com/posting-api/job-board/DeepL`: 200 JSON, 8 jobs | [Ashby posting API](https://developers.ashbyhq.com/docs/public-job-posting-api), 2026-10-31 | **No**: 0 in Ireland. Remote-flagged roles are tied to the US, London, Amsterdam or Germany |
| Helsing | Unconfirmed | Email alert | Job alerts on <https://helsing.ai/careers> | Careers page refused automated fetches (429), 2026-10-31 | Unknown: page blocked |
| Wayve | Ashby (`wayve`) | API | `GET https://api.ashbyhq.com/posting-api/job-board/wayve`: 200 JSON, 175 jobs | [Ashby posting API](https://developers.ashbyhq.com/docs/public-job-posting-api), 2026-10-31 | **No**: 0 in Ireland. Remote-flagged roles are tied to the US, London or Germany. The board was found via the careers page's redirect to jobs.ashbyhq.com/wayve |
| Hugging Face | Unconfirmed | Email alert | <https://huggingface.co/jobs> now requires a login | Careers page, 2026-10-31 | Unknown: no listings without a login, and no hiring-software link visible |
| LangChain | Ashby (`langchain`) | API | `GET https://api.ashbyhq.com/posting-api/job-board/langchain`: 200 JSON, 98 jobs | [Ashby posting API](https://developers.ashbyhq.com/docs/public-job-posting-api), 2026-10-31 | **No**: 0 in Ireland. Remote roles are US, Singapore or city-tied |
| Temporal | Ashby (`temporal`) | API | `GET https://api.ashbyhq.com/posting-api/job-board/temporal`: 200 JSON, 74 jobs | [Ashby posting API](https://developers.ashbyhq.com/docs/public-job-posting-api), 2026-10-31 | **No**: 0 in Ireland. Remote roles are US, Canada or London |
| Modal | Ashby (`modal`) | API | `GET https://api.ashbyhq.com/posting-api/job-board/modal`: 200 JSON, 43 jobs | [Ashby posting API](https://developers.ashbyhq.com/docs/public-job-posting-api), 2026-10-31 | **No**: 0 in Ireland. Its 2 remote roles are US only |
| Weights & Biases | Own site (CoreWeave careers) | Email alert | Job alerts on <https://coreweave.com/careers/weights-biases> | Careers page, 2026-10-31 | Unknown: listings are rendered by JavaScript |

## Job boards and aggregators

| Source | Kind | Access | Endpoint or alert | Terms checked | Notes |
|---|---|---|---|---|---|
| Adzuna | Aggregator API | API (key needed; not signed up) | `https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?app_id=…&app_key=…` | [API terms](https://developer.adzuna.com/docs/terms_of_service), 2026-10-31 | **Ireland coverage not confirmed.** The docs list no supported countries (examples use only `gb`), and `adzuna.ie` doesn't resolve. The free key is "intended for evaluating the API". "Personal job search" is a permitted use. Displayed ads must carry a linked "Adzuna" label. Ask Adzuna whether `ie` is supported before signing up |
| Jooble | Aggregator API | API (key via a request form; not requested) | Key and endpoint are issued through the form at <https://jooble.org/api/about> | [Site terms](https://jooble.org/info/terms) (effective 2019-01-08), 2026-10-31 | **Ireland coverage not stated**, and `ie.jooble.org` refused our fetch (403). No API-specific terms are published. The site terms bar "bots … or any other automatic process" on the Site and limit content to personal use |
| Hacker News "Who is hiring" | Community thread | API (official, no key) | `GET https://hacker-news.firebaseio.com/v0/user/whoishiring.json`, then `GET …/v0/item/{latest id}.json` | [HN API](https://github.com/HackerNews/API), 2026-10-31 | Confirmed with those two GETs: "Ask HN: Who is hiring? (October 2026)", 319 top-level comments. Each comment is one free-text post, so Irish or remote roles weren't counted; that needs one GET per comment |
| LinkedIn | Job board | Email alert | LinkedIn job alerts | [User agreement](https://www.linkedin.com/legal/user-agreement) (effective 2025-11-03), 2026-10-31 | Section 8.2 bars "bots or other unauthorized automated methods" and scraping. Irish roles: yes |
| Indeed | Job board | Email alert | Indeed job alerts on ie.indeed.com | [Terms of service](https://ie.indeed.com/legal) (updated 2026-09-29), 2026-10-31 | Bars automating Indeed Apply ("any automation, scripting, or bots"). Irish roles: yes |
| IrishJobs.ie | Job board | Email alert | IrishJobs.ie job alerts | [Legal](https://www.irishjobs.ie/jobs/legal), linked from the site footer, 2026-10-31 | **Terms page not read**: it timed out twice. Owner to read it before relying on this row. Irish roles: yes |
| Jobs.ie | Job board | Email alert | "My job alerts" on Jobs.ie | [Terms and conditions](https://www.jobs.ie/about/terms-and-conditions), 2026-10-31 | §11.1.7 defines a "Job Alert" as an opt-in email. No automated-access clause found. Irish roles: yes |
| Glassdoor | Job board | Email alert | Glassdoor job alerts | [Terms of use](https://www.glassdoor.com/about/terms.htm), 2026-10-31 | **Terms page not read**: it refuses automated fetches (403). Owner to read it before relying on this row. Irish roles: yes |

## Summary

- **Companies reachable by API: 13**, all with one GET each:
  - Greenhouse (3): Anthropic, Intercom, HubSpot.
  - Ashby (10): Wayflyer, Workhuman, Mistral AI, Cohere, Synthesia, DeepL, Wayve, LangChain, Temporal, Modal.
- **Companies email alert only: 24.** Of these:
  - **Own site (16):** Google, Microsoft, Meta, Amazon, Huawei, Stripe, Salesforce, Datadog, Cloudflare, Tines, Toast, Pinterest, LinkedIn, ElevenLabs, Weights & Biases, Qualtrics (Phenom).
  - **Workday (2):** Workday, Zendesk.
  - **Workable (1):** Fenergo.
  - **Greenhouse, but no documented EU API host (1):** PolyAI.
  - **Unconfirmed, because the careers page was blocked or needs a login (4):** OpenAI, Coinbase, Helsing, Hugging Face.
- **Boards and aggregators:**
  - **HN "Who is hiring":** API, confirmed.
  - **Adzuna and Jooble:** API in principle, but Ireland coverage isn't confirmed for either and no key exists yet.
  - **LinkedIn, Indeed, IrishJobs.ie, Jobs.ie and Glassdoor:** email alert only.
- **No Irish or remote roles today (candidates to drop):** Mistral AI, DeepL, Wayve, LangChain, Temporal, Modal. All six are confirmed from their API response.
- **Not determinable today (not drop candidates yet):** Google, Microsoft, Meta, Amazon, OpenAI, Huawei, Stripe, Workday, Salesforce, Cloudflare, Fenergo, Zendesk, Coinbase, Qualtrics, LinkedIn, PolyAI, Helsing, Hugging Face, Weights & Biases. Their email alerts will show whether Irish roles appear.
- **For the owner to check by hand:** the IrishJobs.ie and Glassdoor terms pages. Ask Adzuna and Jooble whether they cover Ireland before signing up for either.

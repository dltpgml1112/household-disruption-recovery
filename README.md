# Household Disruption Recovery

An Alexa+ add-on prototype: a self-hosted MCP server, an LLM agent that calls it, and a
web simulator. Alexa+ can already take a school's email and put its dates on the family
calendar ([Amazon, updated 2026-07-21](https://www.aboutamazon.com/news/devices/new-alexa-generative-artificial-intelligence)).
This project picks up after that point. When a school announces a late start or a
closure, it works out what the change breaks further down the family's day, plans around
the family's constraints, waits for a parent's yes, makes the changes, and reads every
change back before it says the household is ready.

Built for the 2026 Build, Ship, Shape: Amazon Developer Hackathon (Alexa+ track).
Entrants cannot connect a server to a real Alexa+ device, so the demo runs through a web
simulator whose agent backend is a real MCP client: `initialize`, `tools/list` and
`tools/call` over Streamable HTTP, the path the hackathon FAQ allows.

Hosted demo, no login: https://household-disruption-recovery.vercel.app (MCP endpoint at
`/mcp`). It is on Vercel's free plan and has two known limits, listed under
[Limits](#limits).

![The proposed fix on the simulated wall display: the MCP App card the server sends, the five steps with the tool behind each, the two conflicts, the morning now and with the plan, and the MCP trace of the turn](docs/images/proposed-fix.png)

| | |
|---|---|
| Problem | One school change breaks several family commitments at once: who drives which child, and who is home with a child who now leaves later, or not at all. |
| Required tech | Self-hosted MCP server, spec 2025-11-25, Streamable HTTP (`src/hdr/mcp_server/app.py`). Every household read and write goes through it. |
| Agent | Strands Agents SDK with Gemini (`src/hdr/orchestrator/agent_strands.py`) calls the MCP tools at runtime. A rule agent is the baseline and the fallback. |
| Agent action | Detect the change → find what breaks → plan → parent approves → apply → read back and verify. |
| Track fit | Agentic workflow: one parent's yes changes drivers, supervision, six calendar entries and three messages, and each write is read back. State on the server: plans, approvals and the audit log outlive each MCP session, so "What did you do?" is answered from the log. Media: the plan and result card is an MCP App view (`ui://household-disruption-recovery/card`), the extension Alexa+ says it renders in its webview. |
| Evidence | 37 scripted conversations scored the same way for both agents: the LLM agent completed all 37 in the latest run (one turn answered by the fallback after a network error), the rule agent 24; no unauthorised write in either ([below](#evaluation)). 117 tests, including raw-HTTP MCP compliance and the MCP Apps links; an audit row for every tool call; a read-back check for every write. |
| Outcome | Demo morning: 13 of 13 writes read back, every responsibility has an owner. With Linda unavailable: no plan, the uncovered hours and the blocking commitments named, nothing written. |

Alexa+ today, in Amazon's own description, takes a forwarded school email and puts its
dates on the calendar ("Add all early dismissals to my calendar"). This add-on covers the
step after that:

| | Alexa+ today (Amazon's description) | This add-on |
|---|---|---|
| School message | Reads a forwarded school email | Reads a delay or closure notice, as an alert or read out by a parent |
| Calendar | Adds the school's dates | Changes the family's own entries as part of one plan (six in the demo) |
| Who drives, who stays home | Not described | Reassigns trips and supervision within each adult's hours and permissions |
| Approval and result | Not described | Nothing changes until a parent's plain yes; every write is read back; "ready" only if all match |

"Not described" means I found nothing about it in Amazon's material, or in Ohai, Skylight
or Hearth (family-calendar products). That is not proof it does not exist.

## The demo morning (synthetic family)

Sarah has to leave by 8:10 for work. Mike drops Noah (3) at daycare and has a client
meeting at 8:45. Emma (8) normally goes to school with Sarah at 7:55. Linda is an
approved caregiver who can help between 8:30 and 10:30.

At 6:15 the school texts: two-hour delay, doors open 10:05.

Keeping the old plan breaks two things: Sarah cannot take Emma at the new time, and
Emma would be home alone 8:10–8:25 and 8:30–9:55. The planner's answer: Sarah takes
Noah on her way to work, Mike stays with Emma until 8:30, Linda takes over and drives
Emma at 9:55. That is 13 writes (two reassignments, two supervision schedules, six
calendar changes, three messages). If Linda is unavailable there is no valid plan, and
the agent says so instead of pretending. The "No feasible plan" card still answers the
question a parent has at 6:15: "No approved adult can be with Emma 8:30 AM–10:00 AM",
what blocks each adult (Sarah's work, Mike's client meeting, Linda unavailable today),
and the choices left: find another approved adult, or ask whether one of those two
commitments can move. Nothing is written and the household is not marked ready.

![After the parent's yes: Household ready, 13 of 13 writes read back from storage, every responsibility covered, a check on each changed block](docs/images/household-ready.png)

If the school closes for the day instead, Emma is home until evening. The same planner
keeps Linda for the morning and moves Mike's office work so he is home after his
meeting (13 writes). Without Linda there is again no valid plan.

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/architecture-dark.svg">
  <img src="docs/images/architecture-light.svg" alt="System architecture: the web simulator sends an utterance or a tap on the card (ui/message) to the orchestrator (FastAPI, /api: session API, Strands agent with Gemini and a rule agent fallback, consent check). The model calls 7 MCP tools over HTTP; approve_recovery_plan is app-only and filtered out of its list, and is called only by the orchestrator after the consent check. The orchestrator reads the card view ui://household-disruption-recovery/card (MCP App, text/html;profile=mcp-app) once per session with resources/read, and the simulator renders it in a sandboxed iframe. The MCP server (/mcp, spec 2025-11-25, Streamable HTTP, 8 tools) hands every call to the recovery service, which uses the pure planner and checker and is the only writer to SQLite." width="880">
</picture>

<details>
<summary>The same diagram as Mermaid text</summary>

```mermaid
flowchart TB
    SIM["Web simulator<br/>Next.js, Alexa+ simulation (unofficial)<br/>MCP Apps host: card in a sandboxed frame"]
    subgraph ORCH["Orchestrator: FastAPI, /api"]
        API["Session API<br/>conversation state, wire trace"]
        AG["Strands agent + Gemini<br/>rule agent as fallback"]
        CON["Consent check<br/>consent.py"]
    end
    subgraph MCP["MCP server: /mcp"]
        TOOLS["8 tools<br/>spec 2025-11-25, Streamable HTTP"]
        CARD["Card view, MCP App<br/>ui://household-disruption-recovery/card"]
        SVC["Recovery service<br/>approval tokens, idempotency, audit log"]
        PLN["Planner<br/>exhaustive search"]
        CHK["Checker<br/>re-checks stored state"]
    end
    DB[("SQLite")]

    SIM -->|"utterance, or a tap on the card (ui/message)"| API
    API -->|"speech, card view + tool result, trace"| SIM
    API -->|"resources/read, once per session"| CARD
    API --> AG
    AG -->|"tools/call (7 tools, approve filtered out)"| TOOLS
    AG -.->|record_parent_decision| CON
    CON -->|plain yes from a parent| API
    API -->|"approve_recovery_plan (app-only)"| TOOLS
    TOOLS --> SVC
    SVC --> PLN
    SVC --> CHK
    SVC --> DB
    CHK --> DB
```

</details>

The orchestrator and the agent reach the MCP server over HTTP, the same way any other MCP
client would. The planner and the checker are pure functions over a household snapshot;
only the recovery service writes to SQLite.

MCP tools: `get_household_state`, `ingest_school_notice`, `plan_disruption_recovery`,
`approve_recovery_plan`, `apply_recovery_plan`, `verify_recovery`, `get_audit_log`,
`report_availability_change`.

### The card is an MCP App

The plan and the verification come back with a `card` that follows the add-on design
guide's advice of one title, one or two supporting fields and one clear action (for the
demo: "Sarah takes Noah at 7:35; Linda takes Emma at 9:55", "Apply 13 changes"). The server
also serves that card as an [MCP Apps](https://github.com/modelcontextprotocol/ext-apps)
view (extension `io.modelcontextprotocol/ui`, spec 2026-01-26):

- An HTML resource at `ui://household-disruption-recovery/card`, mimeType
  `text/html;profile=mcp-app` (`src/hdr/mcp_server/ui/recovery_card.html`, no external
  files).
- `plan_disruption_recovery` and `verify_recovery` link to it with `_meta.ui.resourceUri`,
  on the tool definition (where the MCP Apps spec puts it) and on each result (where
  Alexa+'s add-on docs show it, with `invoking` and `invoked` text). Hosts without MCP Apps
  still get the text and the structured content.
- `approve_recovery_plan` carries `_meta.ui.visibility: ["app"]`: a host that follows MCP
  Apps keeps it out of the model's tool list; only a view may call it (MCP Apps lets a
  view send `tools/call` through its host).

Amazon's MCP toolkit docs say Alexa+ supports MCP Apps and renders them in its webview
([overview](https://developer.amazon.com/docs/alexaplus/add-ons/mcp-toolkit-overview.html)).
I could not test that: the add-on tools are partner-only. In this repository the
simulator is the host. The orchestrator reads the view once per session with
`resources/read`; the simulator runs it in a sandboxed frame (`sandbox="allow-scripts"`,
no same-origin access) and speaks the MCP Apps postMessage protocol (`ui/initialize`,
`ui/notifications/tool-result`, `ui/notifications/size-changed`, `ui/message`). A tap on
"Apply 13 changes" reaches the host as `ui/message` "Yes, do it" from the current speaker
and goes through the same consent check as a spoken yes.

Who calls `approve_recovery_plan`, then? Here, the orchestrator: after the consent check
it calls the tool as the parent who is speaking. My card never calls a tool itself. In
Alexa+ the orchestrator would be Alexa's, not mine, so the card's button would have to
send `tools/call approve_recovery_plan` through the host, and the server would need to
know which family member tapped (today the approver's member id is an argument the client
sends). I have not built that path and could not test it.

The simulator shows the household on a wall display (Echo Show framing) or in a phone
frame: **Phone** at the top, or `?device=phone`. Same session, same API. In phone mode
the host context sent to the card says `platform: "mobile"`, touch without hover, and the
frame's size, and the card stacks its buttons for the narrow screen. The phone frame is a
stand-in: I could not check whether the Alexa app on a phone renders MCP Apps.

![Phone mode after "Try again": the same session in a phone frame, the card in a mobile host context reads Household ready, 13 of 13 read back; one write retried, twelve already in](docs/images/phone-mode.png)

Rules the server enforces on every call:

- Planning never writes. Two tools record facts without an approval token:
  `ingest_school_notice` moves the school trip to the school's new times (or cancels it on
  a closure), and `report_availability_change` records that someone can or cannot help.
  Neither touches anyone's commitments; both bump the state version, so an open plan goes
  stale, both are in the audit log, and both are annotated `destructiveHint: true` so a
  host can ask before calling them. Every other write needs an approval token for one
  plan id and version, issued only when a parent approves. The approver is a member id that the client sends:
  the server checks that the member is a parent and the plan is current, but it cannot
  tell who is speaking. In the simulator you choose the speaker; a real device would need
  voice recognition or account linking for that.
- Any change to the household after a plan is made makes that plan stale; approvals
  for it stop working.
- `apply_recovery_plan` takes an idempotency key. Repeating a key returns the first
  result without running anything again; a new key retries only the failed actions.
- `verify_recovery` re-reads every write and re-checks the whole household with a
  separate checker: its own rules over the stored state, sharing only the data model and
  time helpers with the planner. `ready` is true only if all of it holds.
- The server speaks MCP 2025-11-25. Newer-era requests get -32022 naming 2025-11-25, so
  clients that try the newer protocol first (Strands' `MCPClient` does) fall back to it.

### The two turns of the demo

Each turn opens its own MCP session (`initialize`, `notifications/initialized`,
`tools/list`) and closes it with `DELETE`; those calls are left out below. When the browser
session starts, the orchestrator has already read the card view (`resources/read
ui://household-disruption-recovery/card`).

```mermaid
sequenceDiagram
    actor P as Parent (Sarah)
    participant S as Simulator
    participant O as Orchestrator
    participant M as Model (Strands + Gemini)
    participant X as MCP server
    participant D as SQLite

    P->>S: "Alexa, fix our morning"
    S->>O: POST /api/session/{sid}/turn
    O->>M: utterance, context, 7 tools
    opt when the model wants the current state first
        M->>X: get_household_state
    end
    M->>X: ingest_school_notice
    X->>D: store notice and disruption
    M->>X: plan_disruption_recovery
    X->>D: store plan v1 and 13 pending actions (household rows untouched)
    X-->>M: plan v1, card
    M-->>O: the fix in a few words, "Should I go ahead?"
    O-->>S: speech, card view and plan result, timelines, trace
    S->>S: card runs in a sandboxed frame (MCP Apps host)

    P->>S: taps "Apply 13 changes" on the card, or says "Yes, do it"
    S->>O: POST /api/session/{sid}/turn ("Yes, do it", from ui/message or speech)
    M->>O: record_parent_decision(plan v1, "Yes, do it")
    O->>O: consent check: parent's own words, plan shown earlier, no "but" or "if"
    O->>X: approve_recovery_plan as the speaking parent
    X-->>O: approval token (HMAC over plan id, version, state version, actions hash, expiry)
    M->>X: apply_recovery_plan (token and idempotency key added by a hook)
    X->>D: 13 writes, one transaction each
    M->>X: verify_recovery
    X->>D: re-read every write, run the checker
    X-->>M: ready = true, 13/13
    M-->>O: "Your household is ready."
    O->>X: get_household_state (the result screen shows stored data)
    O-->>S: "Household ready", 13/13 read back
```

### Plan lifecycle

```mermaid
stateDiagram-v2
    [*] --> proposed: plan_disruption_recovery
    [*] --> infeasible: no valid plan, nothing to apply
    proposed --> approved: a parent says a plain yes
    proposed --> rejected: a parent says no
    proposed --> superseded: a newer plan
    approved --> superseded: a newer plan
    proposed --> stale: household changed
    approved --> stale: household changed
    approved --> applying: apply_recovery_plan
    applying --> applied: every action written
    applying --> partially_applied: an action failed
    partially_applied --> applying: retry with a new idempotency key
    applied --> verified: verify_recovery returns ready
    verified --> [*]
```

A superseded or stale plan has its approvals revoked. The household is called ready only
from `verified`.

### Data model

SQLite, schema in [`src/hdr/store/db.py`](src/hdr/store/db.py). Every write to the
household bumps `households.state_version`; a plan records the version it was made from,
so any change it did not make turns it stale. The main tables, with declared foreign keys
as solid lines (all tables and columns in [docs/architecture.md](docs/architecture.md#data-model)):

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/erd-dark.svg">
  <img src="docs/images/erd-light.svg" alt="Entity relationship diagram in three groups. Household state: households has members, calendar_events, escorts and supervision. Notice to plan: households has notices, notices open disruptions, disruptions are planned by plans. Per plan: plans have actions and approvals (declared foreign keys) and verifications and notifications (plan id kept by the service, no declared key). households.state_version and plans.base_state_version are highlighted: a plan goes stale when the household changes outside it." width="880">
</picture>

<details>
<summary>The same data model as Mermaid text</summary>

```mermaid
erDiagram
    HOUSEHOLDS ||--o{ MEMBERS : has
    HOUSEHOLDS ||--o{ CALENDAR_EVENTS : has
    HOUSEHOLDS ||--o{ ESCORTS : "child trips"
    HOUSEHOLDS ||--o{ SUPERVISION : "adult with a child"
    HOUSEHOLDS ||--o{ NOTICES : receives
    NOTICES ||--o{ DISRUPTIONS : opens
    DISRUPTIONS ||--o{ PLANS : "planned by"
    PLANS ||--o{ ACTIONS : "writes to make"
    PLANS ||--o{ APPROVALS : "parent decisions"
    PLANS ||..o{ VERIFICATIONS : "read-back results"
    PLANS ||..o{ NOTIFICATIONS : "messages sent"

    HOUSEHOLDS {
        TEXT id PK
        TEXT date
        INTEGER state_version "bumped by every write"
        TEXT school_json
    }
    MEMBERS {
        TEXT household_id PK
        TEXT id PK
        TEXT data_json "role, hours, whom they may drive or watch"
    }
    CALENDAR_EVENTS {
        TEXT household_id PK
        TEXT id PK
        TEXT member_id
        INTEGER start_min
        INTEGER end_min
        INTEGER hard
        TEXT status "confirmed or cancelled"
    }
    ESCORTS {
        TEXT household_id PK
        TEXT id PK
        TEXT child_id
        TEXT assigned_member_id
        INTEGER depart
        INTEGER arrive
        TEXT status "active or cancelled"
    }
    SUPERVISION {
        TEXT household_id PK
        TEXT id PK
        TEXT child_id
        TEXT member_id
        INTEGER start_min
        INTEGER end_min
    }
    NOTICES {
        TEXT id PK
        TEXT raw_sha256 "raw text is not stored"
        REAL confidence
    }
    DISRUPTIONS {
        TEXT id PK
        TEXT notice_id FK
        TEXT kind "school_delay or school_closure"
        TEXT status
    }
    PLANS {
        TEXT id PK
        TEXT disruption_id FK
        INTEGER version
        INTEGER base_state_version
        TEXT status
        TEXT actions_hash
    }
    ACTIONS {
        TEXT id PK
        TEXT plan_id FK
        INTEGER seq
        TEXT type
        TEXT status "pending, applied or failed"
    }
    APPROVALS {
        TEXT id PK
        TEXT plan_id FK
        TEXT approver_member_id
        TEXT token_sha256
        INTEGER revoked
    }
    VERIFICATIONS {
        TEXT id PK
        TEXT plan_id "no declared key"
        INTEGER ready
    }
    NOTIFICATIONS {
        TEXT id PK
        TEXT plan_id
        TEXT member_id
        TEXT status "delivered to the simulated outbox"
    }
```

</details>

`members`, `escorts`, `supervision` and `calendar_events` refer to members by id within
the household; SQLite enforces only the household key. Not drawn: `locations`, `travel`,
`idempotency` (one row per apply key), `faults` (demo fault injection) and `audit_events`
(one row per tool call).

## What the model decides, and what it does not

The model reads what the parent says, picks the tools, and words the reply. It does not
make the plan, give the approval, or decide that the household is ready:

- Plans come from the deterministic planner on the server.
- `approve_recovery_plan` is not in the model's tool list. The model reports the
  parent's answer with `record_parent_decision`; the orchestrator accepts it only if the
  quoted words are the parent's own, the plan was shown in an earlier turn, and the
  message has no "but", "wait", "if" or question in it. The server then checks that the
  speaker is a parent and the plan is still current.
- `apply_recovery_plan` gets its approval token and idempotency key from the
  orchestrator. Without a recorded approval the server refuses the call.
- A reply that says the household is ready is replaced unless `verify_recovery` returned
  `ready=true` in the same turn. This is a keyword check (a fixed list of phrases, at least
  every phrase the evaluation counts), not a classifier.
- If no model is configured, or a model call fails after retries, the rule agent answers
  that turn and the trace says so.

Details: [docs/architecture.md](docs/architecture.md).

## Evaluation

`evals/` plays 37 scripted conversations (71 turns) against each agent, with a fresh
household and MCP server per scenario, and scores both with the same code from the MCP
calls, the replies and the database. "Ready" is judged from the database and the
checker, not from the agent's own verification. Definitions are in
[evals/README.md](evals/README.md); raw results in `evals/results/`.

Run of 2026-10-21 on the current code, one pass per agent (`20261021T003756Z-v6` in
`evals/results/`), model gemini-3.5-flash-lite with thinking level LOW. `uv run python
evals/report.py evals/results/20261021T003756Z-v6` prints this table from the run:

| | Rule agent | Strands + gemini-3.5-flash-lite |
|---|---|---|
| Scenarios completed (every turn right, final state right) | 24/37 | 37/37 |
| Turns with the expected behaviour | 48/71 | 71/71 |
| Turns with the expected tools, in order | 49/71 | 71/71 |
| Tool calls per turn (of which beyond the expected tools) | 2.37 (0.97) | 1.96 (0.10) |
| Clarifying question when one was needed | 2/3 | 3/3 |
| Scenarios with an unauthorised write | 0/37 | 0/37 |
| Apply attempts the server refused for missing approval | 0 | 1 |
| Replies claiming "ready" when it was not (before the guard) | 0/71 (0) | 0/71 (0) |
| Replies with a time or count not in the tool results | 0/32 | 0/17 |
| Words in the spoken plan proposal (median) | 37 | 25 |
| Turns handed to the rule agent | – | 1 (a network read error) |
| Median time per turn | 0.1 s | 6.2 s |

| Category | Rule agent | Strands |
|---|---|---|
| paraphrase (S01–S10) | 3/10 | 10/10 |
| spoken notice (S11–S13) | 0/3 | 3/3 |
| closure (S14–S16) | 2/3 | 3/3 |
| clarification (S17–S20) | 2/4 | 4/4 |
| safety (S21–S27, S36, S37) | 9/9 | 9/9 |
| failure (S28–S31) | 4/4 | 4/4 |
| follow up (S32–S33) | 2/2 | 2/2 |
| out of scope (S34–S35) | 2/2 | 2/2 |

What the numbers hide:

- One LLM turn (the last one of S32) lost its model call to a network read error and the
  rule agent answered it; the scenario still passed. Without that fallback the LLM agent
  completed 36 of 37.
- The two runs before this one (`v4` and `v5`, 2026-10-20) also had the LLM agent complete
  every scenario then in the set (36 of 36). Its earlier full runs scored 34/35, 34/35 and
  33/35, each failing a different scenario; each failure led to a fix (below). Results vary
  between runs; six runs are not a distribution.
- S37 was added after one take of the demo video failed: after a conditional yes and a
  caregiver's refused yes, the model refused the parent's own plain yes as well. The
  system prompt now says the speaker can change between turns and an earlier refusal does
  not carry over. Before that rule S37 passed 3 of 3 reruns, so the failure is rare; this
  run has the rule.
- Spoken plan proposals got shorter: the median was 57 words for the LLM agent and 66 for
  the rule agent in `v4`, and 25 and 37 now. The card carries the counts and the
  messages. Fewer replies quote a time or count (17 against 31), so that check had less to
  catch.
- The refused apply attempt is S26: a caregiver's "yes" was refused as an approval, and
  the model then tried to apply anyway; the server refused.
- The rule agent fails on wording, not on safety: it misses paraphrases ("Go for it",
  "lock it in"), notices read out by the parent, and the answer to its own question.
- Failures in earlier runs and what changed:
  S16 (2026-10-10) asked for the school's message although the parent had said the school
  was closed: a system-prompt rule was rewritten.
  S15 recorded a bare "Yes." as "Linda is available": availability is now recorded only
  when the parent names the person.
  S09 (2026-10-13) approved, applied and verified, but closing the MCP client raised an
  error and the turn lost its reply: the orchestrator now keeps the reply.
  S31 tried to apply a plan the server had called stale instead of planning again
  (refused, nothing changed): the orchestrator now tells the model to plan again and
  blocks apply for that plan in the same turn.
- The first rule-agent run found two bugs, fixed before any of these runs: "Yes, but don't
  text Linda yet" was applied as a yes, and a caregiver's "yes" crashed the turn instead of
  being refused in words. Both agents now use the same consent check (`consent.py`).
- I wrote the scenarios and the scoring, and the set is small. "Ready" is judged with the
  same checker the server uses; it is independent of the agent, not of the server.

## Why this problem

- In 66.3% of US married-couple families with
  children under 18, both parents were employed in 2025
  ([BLS, released 2026-04-23](https://www.bls.gov/news.release/famee.htm)). A school that
  opens late or not at all takes an adult out of someone else's plan.
- A CDC team counted 20,723 unplanned school closures in
  the US over the 2011–12 and 2012–13 school years, affecting 27,066,426 students; 79% were
  for weather ([Wong et al., PLoS ONE, 2014](https://pmc.ncbi.nlm.nih.gov/articles/PMC4252039/)).
  That count leaves out late starts. In Massachusetts, 2003–2010, the average student missed
  two days a year to weather closings
  ([Goodman, Education Next, 2015](https://www.educationnext.org/defense-snow-days/)).
- In every month of 2025 with data, between 29,000 and 71,000 employed
  people in the US were absent from work the whole week because of child care problems
  ([BLS CPS series LNU02096055](https://data.bls.gov/timeseries/LNU02096055); October 2025 has
  no data). Partial days missed are not in that count.

## Repository layout

```
src/hdr/
  mcp_server/app.py      MCP server: 8 tools, the card resource, protocol pin, health and admin routes
  mcp_server/ui/         the card as an MCP App view (one HTML file)
  service/recovery.py    workflow: approval tokens, idempotency, per-action writes, read-back, audit log
  domain/                notice parser, propagation, planner, checker, action builder (no I/O)
  store/                 SQLite schema and queries
  orchestrator/          agent API, Strands agent, rule agent, consent check, MCP client with wire trace
  hosted.py              one process, one port: /mcp, /api and the built simulator
apps/simulator/          Next.js web simulator
evals/                   scripted conversations, scorer, results of every run
tests/                   unit, integration (real server over HTTP), security
fixtures/households/     the made-up family and its variants
scripts/                 dev runners, CLI demo, model check, secret scan, Space deploy
docs/                    architecture, product feedback, friction log, screenshots
```

## Run it

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20.9+ and Chrome (for speech input).

```bash
uv sync
cd apps/simulator && npm ci && cd ../..   # dev.ps1 does this step itself on Windows

# optional: a free Gemini API key from Google AI Studio for the LLM agent
cp .env.example .env    # then set GEMINI_API_KEY; without it the rule agent answers every turn

# all three processes: MCP server :8765, orchestrator :8766, simulator :3000
./scripts/dev.sh                                          # macOS / Linux / Git Bash
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1  # Windows
```

Open http://127.0.0.1:3000, press **School alert arrives**, then say or click
"Alexa, fix our morning", then tap "Apply 13 changes" on the card (or say "Yes, do it"). The demo buttons also reset the morning,
make Linda unavailable, or make the next message to Linda fail. The trace panel shows
every MCP request of the last turn and which agent answered; the time on each row counts
from the start of the turn, so it includes the model's thinking before that call.
**Phone** at the top shows the same session in a phone frame.

The demo buttons use admin routes on the MCP server (reset, fault injection, the sample
notice). They exist only when `HDR_ADMIN_TOKEN` is set; the dev scripts generate one. If
you start the processes by hand, set the same token for the MCP server and the
orchestrator, or the buttons answer 503. Through the page's API only one fault can be set:
one failed message in that session's household ("Fail Linda's message"); other faults,
such as a tool delay that would slow every household, need the admin route.

One process on one port, which is what the hosted demo runs:

```bash
cd apps/simulator && HDR_STATIC_EXPORT=1 NEXT_PUBLIC_HDR_API=same-origin npm run build && cd ../..
HDR_STATIC_DIR=apps/simulator/out uv run python -m hdr.hosted    # http://127.0.0.1:7860, MCP at /mcp
```

There each browser session gets its own copy of the household, so visitors do not change
each other's morning; outside MCP clients work on the shared `hh-demo` household. The
`Dockerfile` builds the same thing. The hosted demo runs that image on Vercel's container
runtime (`vercel.json`): Vercel serves the simulator as static files and sends `/api`,
`/mcp` and `/healthz` to the container. `HDR_PUBLIC_HOSTS` must name the public host, or
the MCP server's DNS-rebinding check turns outside clients away (421).
`scripts/deploy_hf_space.py` uploads the same image to a Hugging Face Space; Docker
Spaces now need a paid plan, so the hosted demo does not use it.

Without the web UI:

```bash
uv run python scripts/demo_cli.py --fresh                        # rule agent
uv run python scripts/demo_cli.py --fresh --agent strands        # LLM agent
uv run python scripts/demo_cli.py --fresh --fault notify:linda   # partial failure, then retry
uv run python scripts/demo_cli.py --fresh --variant linda-unavailable
uv run python scripts/check_model.py                             # is the configured model reachable?
```

Run only the MCP server (for any MCP client): `uv run python -m hdr.mcp_server`
→ `http://127.0.0.1:8765/mcp`. Configuration is in `.env.example`.

## Tests

```bash
uv run pytest
```

GitHub Actions runs the same tests, the simulator's type check and the static build on
every push (`.github/workflows/tests.yml`).

117 tests, no model API needed. Unit tests (54) cover the parser, planner, checker,
action builder, device cards, model settings (including which Gemini errors are
retried) and the phrases the "ready" guard catches.
Integration tests start the real server on a local port.
`tests/integration/test_mcp_compliance.py` uses plain HTTP (no MCP SDK) to check the
2025-11-25 handshake, the `MCP-Protocol-Version` header on later requests, `tools/list`,
`tools/call`, the rows written to SQLite, the read-back and the refusal of the newer
protocol era; another checks the MCP Apps links (the `ui://` resource and its mimeType,
`_meta.ui.resourceUri` on the tool definitions and results, the app-only approval tool); its
last test checks that Strands' `MCPClient` ends up on 2025-11-25.
`tests/integration/test_strands_agent.py` drives the Strands agent with a scripted model:
the approval tool is hidden, a conditional or borrowed "yes" is refused, apply without
approval is refused by the server, an unverified "ready" is replaced, availability is only
recorded when the parent names the person, a plan that went stale cannot be approved or
applied, tool calls stay on the session's household, and a failing model hands the turn
to the rule agent. `tests/integration/test_hosted.py`
runs the single-port server: simulator files, agent API and MCP on one port, a separate
household per browser session, the Host/Origin checks, that only a parent speaking
can approve, and that the card view is read with `resources/read` and reaches the plan
and result screens with the tool result it renders. `tests/security/` tries to
apply without approval, with forged or tampered tokens, with an old plan version, twice
with the same key, after the household changed, and with an expired token; each test
checks that nothing in the household changed.

## Friction log

Problems I hit with the tools, in the hackathon's friction-log format (task, steps,
expected and actual result, severity, workaround, suggestion):
[docs/friction-log.csv](docs/friction-log.csv).

## AWS Builder

The agent loop and its MCP client are the Strands Agents SDK (`strands-agents`, open
source from AWS): `Agent`, `MCPClient` with a tool filter, `BeforeToolCall` /
`AfterToolCall` hooks, and `ModelRetryStrategy`, in `src/hdr/orchestrator/agent_strands.py`.
The model provider is Gemini through Strands' `GeminiModel`; Bedrock is not used.

## Limits

- Two disruption types: late start and closure. Early dismissals are recognised but not
  planned.
- Messages go to a simulated channel (recorded in an outbox), not real phones.
- Children wait at home until their own drop-off; a child riding along on a sibling's
  drop-off is not modelled.
- The notice parser is rule-based. Ambiguous notices get a question, not a guess.
- The model runs on Gemini's free tier. gemini-3.5-flash-lite allows 500 requests a day
  there; gemini-3.8-flash allowed 20, too few to evaluate, so it is not the default.
  When the daily quota is used up the call is not retried, and the rule agent answers that
  turn (the trace says so).
  A turn of the LLM agent took 6.2 s (median) in the evaluation, which is slow for a
  spoken reply; the simulator says "One moment" and shows "Working on it…" meanwhile.
  On the hosted demo on 2026-10-24 a whole run (alert, plan, apply) took 15–76 s, median
  36 s over 15 runs, most of it waiting for Gemini.
- When no plan exists, the reply names the gap left by the best plan found, which can be
  wider than the smallest possible gap.
- The MCP App card runs in this simulator's host only. I could not load it into Alexa+ (the
  add-on tools are partner-only), so how Alexa+ draws it is untested. The wall display and
  the phone are frames drawn by the web page, not an Echo Show or the Alexa app.
- Who is speaking is picked on screen ("Speaking: Sarah (parent)"); voice identification is
  not modelled. Only a parent's yes is accepted as an approval.
- There is no account linking: the household comes from the browser session, not from a
  signed-in family.
- The hosted demo's MCP endpoint has no login: any MCP client can work on the shared demo
  household. It holds only the made-up family.
- The hosted demo keeps sessions in the server's memory. When several requests arrive at
  once, Vercel can start a second copy of the server, and it does not send a session back
  to the copy that holds it. That session is lost: the page says "The demo server
  restarted, so this is a new morning" and starts over. With three people going through
  the demo at the same time, two or three of the three hit this in each of my three
  tests (2026-10-23 and 2026-10-24); one person at a time did not, in 30 runs. MCP
  clients are hit the same way: their MCP session and the shared demo household also live
  on one copy, so a client can get "Session not found" while more than one copy runs.
- After 5 minutes without a request the hosted server stops; a page left open without a
  click counts. The next click starts it again (about 10 s) with a new morning.
- Setting up a family is a JSON file today (`fixtures/households/school_delay_demo.json`):
  the people, who may drive or watch whom, travel minutes, hard and soft commitments, a
  helper's hours. There is no setup conversation or screen. After setup, changes come by
  voice ("Linda can't come today"), which works.
- One family is modelled, with variants (Linda unavailable, a closure instead of a delay).
- "Household ready" means every change is stored and every message reached the simulated
  outbox. Nobody confirms they read it; Linda's acceptance is not modelled.
- Calendar writes and messages go to SQLite. Pointing them at a real calendar and
  messaging service would leave the planner and the approval rule as they are, but the
  writes and the read-back would need an adapter for those APIs; none is written.
- I have not measured how much time this saves a family; the evaluation numbers are about
  the agent, not about families.
- All people in the fixtures are made up.

## License

MIT, see [LICENSE](LICENSE).

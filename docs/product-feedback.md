# Product Feedback

One section per tool I used, Amazon and AWS first, then the hackathon's own rules and
FAQ. "#n" refers to the entry with id n in my [friction log](friction-log.csv). Severity
is the one given in the log.

## Strands Agents SDK (strands-agents 1.59.0, Python)

**Used for:** the LLM agent loop, its MCP client, the guards around tool calls, model retries, and the Gemini provider.

**AWS service used (AWS Builder):** Strands Agents SDK (`strands-agents` 1.59.0), which the rules list among the AWS services for this mini challenge. It runs the agent on every LLM turn.

**How** ([src/hdr/orchestrator/agent_strands.py](../src/hdr/orchestrator/agent_strands.py)):
- `Agent`: a new one per turn, run with `invoke_async` and a turn limit of 10 loop turns.
- `MCPClient` over a `streamable_http_client` transport loads the MCP server's tools. `tool_filters={"rejected": [...]}` keeps `approve_recovery_plan` away from the model. One local `@tool`, `record_parent_decision`, sits next to them.
- A `HookProvider` with four callbacks. `BeforeToolCallEvent` writes the approval token and idempotency key into every `apply_recovery_plan` call, pins `household_id` to the session's household, and cancels `report_availability_change` unless the parent named that person. `AfterToolCallEvent` records each result for the trace panel. `BeforeModelCallEvent` and `AfterModelCallEvent` time the model calls.
- `ModelRetryStrategy(max_attempts=4)`, `SequentialToolExecutor` and `SlidingWindowConversationManager(window_size=40)`.
- The model is Strands' `GeminiModel`, subclassed in `src/hdr/orchestrator/llm.py` to change which errors are retried.

**Not used:** Bedrock. The hackathon's credit codes ran out on October 7 (#3). The model sits behind Strands' `Model` interface, so a Strands Bedrock provider can be added in `llm.py` without touching the agent.

**Worked well:**
- `tool_filters` hid `approve_recovery_plan` from the model in one argument; the orchestrator still calls it through the same client.
- `BeforeToolCallEvent` lets a hook rewrite `event.tool_use`. That is how the orchestrator injects the approval token, so the model never sees or invents one. `event.cancel_tool` returns a reason the model can read and act on.
- `MCPClient` worked against my self-hosted Streamable HTTP server once the `mcp` version pin was in place.
- `Agent` accepts any `Model`. My integration tests drive the real agent, hooks and MCP server with a scripted model and no API key.

**Needs work:**
- `strands-agents` 1.59 requires `mcp<2.2`, so I could not use the newest MCP SDK (#2, Low). Suggest widening the pin when an MCP SDK minor ships, or a published compatibility table.
- `MCPClient` always probes MCP 2026-07-28 first and has no option to pin 2025-11-25 (#4, Medium). I made the server refuse the newer era so the client falls back. Suggest exposing the SDK's connect mode, or a `protocol_version` argument, on `MCPClient` and `MCPServerConfig`.
- A failed `MCPClient.start()` leaves a "coroutine ... was never awaited" `RuntimeWarning` (#5, Low). Suggest awaiting or cancelling the close-event coroutine on the failed-start path.
- `GeminiModel` does not retry a 503 overload, which arrives as `ServerError` (#6, Medium). Suggest treating `ServerError` 503 and 500 as throttling.
- `GeminiModel` retries per-day quota 429s like per-minute limits. Each turn waited 136–146 s before failing (#7, Medium). Suggest reading the quota name in the 429 body and not retrying per-day errors, or honouring the server's retry delay. My fix for both is a small subclass in `llm.py`.

**Onboarding:** I installed `strands-agents[gemini]` with uv after checking its PyPI metadata, which is where I found the `mcp<2.2` pin. My first test of `MCPClient` ran it against my local server with every HTTP request recorded: list tools, call one. The recording showed the `server/discover` probe (#4). The first Strands turn that called my MCP tools ran on 2026-10-10.

**Build again:** Yes. Hooks and tool filters kept approval and verification outside the model without forking the SDK.

## Alexa+ add-on documentation

**Used for:** the MCP add-on design guide (https://developer.amazon.com/docs/alexaplus/add-ons/mcp-addon-design-guide-overview.html, last updated 2026-07-21); the add-on setup guides; the MCP toolkit overview and client-lifecycle pages, for MCP Apps; the "Introducing Alexa+" announcement, to start this project where Alexa+ already puts a school's dates on the calendar.

**Worked well:** The design guide's first principle, end-to-end task completion with the outcome confirmed clearly, set the target: the agent does not say the household is ready until `verify_recovery` has read every write back. The toolkit overview says Alexa+ supports the MCP Apps extension, which gave the card a standard form: the server serves it as a `ui://` view that any MCP Apps host can render.

**Needs work:** The add-on developer tools (MCP Toolkit, Web Simulator) are partner-only. The notice is on the add-on docs home page but, in the hackathon FAQ's words, "sometimes missing from individual setup-guide pages, which has caused some confusion" (#1, High). Suggest the partner-only notice on every setup-guide page. Where `_meta.ui.resourceUri` goes is unclear: the client-lifecycle page shows it on the tool result, the MCP Apps spec on the tool definition, and the page does not say which MCP Apps version the webview speaks (#10, Medium). I set both.

**Onboarding:** I read the add-on setup guides, found the tools gated, and built my own simulator that speaks MCP over Streamable HTTP. My "hello world" was `scripts/demo_cli.py` running the full flow against the local server on 2026-10-10.

**Build again:** Yes. A self-hosted MCP server is the shape I would choose for this add-on anyway. I would want to test it on the real surface.

## Hackathon rules, FAQ and forum (Alexa+ track)

**Used for:** deciding the MCP version, the demo path and hosting; planning AWS Promotional Credits for Bedrock (not received: codes ran out on October 7).

**Worked well:** The FAQ answer that a web page acting as a real MCP client is acceptable gave me a clear path without the gated tools. The rules page said plainly on October 7 that the credit codes had run out, before I chose a model provider.

**Needs work:**
- The rules accept "2025-11-25 (or a later version, once confirmed)" but do not say whether 2026-07-28 is confirmed (#8, Low). Suggest stating in the rules or FAQ which versions are accepted and which one Alexa+ add-ons use today.
- The FAQ says hosting is not required. Organizer forum answers say the MCP server must stay live until judging ends (#9, High). Suggest correcting one so both say the same thing; entrants plan hosting around it.
- Credit codes ran out on October 7, two weeks before the stated October 21 request deadline (#3, Medium). A remaining-codes counter on the rules page would let entrants plan Bedrock use.

**Onboarding:** The rules and the FAQ cover the track requirements and the simulator path. The forum was where answers moved: an organizer answer there contradicts the FAQ on hosting (#9).

**Build again:** Yes. The FAQ's simulator path let me build and test a real MCP add-on without access to the gated tools.

## MCP Python SDK (mcp 2.1.1, mcp-types)

**Used for:** the server (`MCPServer`, Streamable HTTP at `/mcp`, eight tools) and the rule agent's client (`Client` with `mode="legacy"`).

**Worked well:**
- Typed return models on `@mcp.tool` became structured outputs. `ToolAnnotations` carried `destructiveHint` on `apply_recovery_plan`.
- `TransportSecuritySettings` gave Host and Origin checks for the public host name without code of my own.
- `mode="legacy"` pinned the client to the `initialize` handshake in one argument.

**Needs work:** The server also answers the 2026-07-28 era. The client has `mode="legacy"`; I did not find a server counterpart that limits it to 2025-11-25, so I wrote an ASGI middleware, `PinHandshakeProtocol` in `src/hdr/mcp_server/app.py`, that reads the request body and answers newer-era requests with -32022. A supported-versions option on `MCPServer` or `streamable_http_app()` would replace it (related: #8, rules).

**Onboarding:** I added `mcp` with uv, declared the tools with `@mcp.tool` and served them with `streamable_http_app()`. A test in plain HTTP, with no MCP SDK, checks `initialize` → `tools/list` → `tools/call` (`tests/integration/test_mcp_compliance.py`).

**Build again:** Yes. Typed tools, annotations and Host/Origin checks came from the SDK; the only gap I hit was pinning the server's protocol version.

## Gemini API through Strands (google-genai 2.29.0)

**Used for:** the model behind the Strands agent: `gemini-3.5-flash-lite`, thinking level LOW, free tier.

**Worked well:** An AI Studio key and one environment variable were enough. The 429 body names the quota (`quotaId` containing `PerDay`), so per-day errors can be told apart from per-minute ones.

**Needs work:** On the free tier `gemini-3.8-flash` allowed 20 requests a day. One evaluation run needs about 190 (188 on 2026-10-13), so I moved to Flash-Lite (500 a day). Retry handling: #6, #7 (under Strands above).

**Onboarding:** I set `GEMINI_API_KEY`. `scripts/check_model.py` confirms the model is reachable before a run.

**Build again:** Yes, for prototyping. The free tier was enough to run the full evaluation on Flash-Lite.

## Next.js 16.3.8, React 19.3.0, Chrome Web Speech API

**Used for:** the four-screen simulator in `apps/simulator`, speech in (`SpeechRecognition`) and speech out (`speechSynthesis`).

**Worked well:** `output: "export"` produced static files the single-port Python server can serve.

**Needs work:**
- Next.js: nothing that cost me time.
- Web Speech API: `SpeechRecognition` is not available in every browser, so the mic button is disabled there; the text box and phrase buttons still work.

**Onboarding:** The only runtime dependencies are `next`, `react` and `react-dom`. `output: "export"` sits behind `HDR_STATIC_EXPORT=1` in `next.config.ts`, so the same app runs under `next dev` or builds to `out/` for the Python server. Speech uses the browser's built-in `SpeechRecognition` and `speechSynthesis`, with no SDK or key.

**Build again:** Yes. The static export let one Python process serve the UI, the agent API and MCP on one port.

## FastAPI 0.143, uvicorn 0.54, pydantic 2.14, SQLite, uv 0.12.13

**Used for:** the orchestrator API, storage (a fresh database per evaluation scenario), and one lockfile for the server and the agent.

**Worked well:** `uv sync` reproduces the environment. Pinning `mcp` for both components took one line.

**Needs work:** Nothing that cost me time.

**Onboarding:** Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. The orchestrator is one `FastAPI` app in `src/hdr/orchestrator/api.py`; `GET /api/health` answers once it is up. The `Dockerfile` installs uv 0.12.13 and runs `uv sync --frozen --no-dev` against the same lockfile.

**Build again:** Yes. One lockfile kept the MCP server, the agent and the container on the same versions.

## Feature Requests

**1. A sandboxed Alexa+ simulator or developer account for hackathon entrants**
- Description: a way to load a self-hosted MCP server into an Alexa+ conversation.
- Why it matters to this project: I could only test the end-user experience in my own simulator (#1).
- Priority: Critical

**2. A protocol version option on Strands `MCPClient`**
- Description: expose the MCP SDK's connect mode, or a `protocol_version` argument, on `MCPClient` and `MCPServerConfig`.
- Why it matters to this project: today the server has to refuse the newer era to keep this client on 2025-11-25 (#4).
- Priority: Important

**3. Documentation of the MCP versions Alexa+ accepts**
- Description: state which MCP versions count for the hackathon and which version Alexa+ add-ons negotiate today.
- Why it matters to this project: I pinned 2025-11-25 without knowing whether 2026-07-28 counts (#8).
- Priority: Important

**4. How Alexa+ handles tools marked `destructiveHint`**
- Description: state what Alexa+ does before calling a tool annotated `destructiveHint: true`, for example whether it asks the user to confirm. I did not find this in the add-on docs I read; if it is documented, a link from the MCP add-on design guide would do.
- Why it matters to this project: `apply_recovery_plan` is marked destructive and already needs a parent's approval token. I want to know whether the parent would be asked twice.
- Priority: Important

**5. Retry classification in Strands `GeminiModel`**
- Description: treat 503 and 500 as throttling, and do not retry per-day quota 429s (or honour the server's retry delay).
- Why it matters to this project: without my subclass, overload ended turns at once and a used-up daily quota held each turn over two minutes (#6, #7).
- Priority: Nice-to-have

**6. A protocol version option on the MCP Python SDK server**
- Description: a setting on `MCPServer` or `streamable_http_app()` that limits the protocol versions the server accepts, matching the client's `mode="legacy"`. The name is up to the SDK; `supported_protocol_versions` is my suggestion.
- Why it matters to this project: I pin the server to 2025-11-25 with my own ASGI middleware that parses each request body.
- Priority: Nice-to-have

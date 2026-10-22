# Result folders

One folder per run, named `<UTC start>-<label>`. Each has a JSON line per scenario run and
`summary.json`. Only the runs marked *Reported* back the numbers in the main README; the
others are kept because they explain how the model and settings were chosen.

| Folder | Agent / model | What it is |
|---|---|---|
| `20261010T103711Z-rules-v0` | rule agent | First baseline. Found two bugs: a conditional "yes" (S24) was applied, and a caregiver's approval attempt (S26) crashed the turn instead of being refused in words. |
| `20261010T104507Z-rules-v1` | rule agent | After both fixes. |
| `20261010T114317Z-rules-v2` | rule agent | Same scoring as the Strands runs (adds the fact check). Reported until 2026-10-13. |
| `20261010T114306Z-strands-v1` | Strands + gemini-3.5-flash-lite, thinking LOW | Full set: 34/35. S16 failed: the model asked for the school's message although the parent had said the school was closed. |
| `20261010T115534Z-strands-v1-fix-rule4` | same | After rewriting system-prompt rule 4: S16 and the seven scenarios it touches (S11–S13, S17–S20) all passed. |
| `20261010T115816Z-strands-v2` | same | Full set after the rule-4 fix: 34/35 (reported until 2026-10-13). S15 failed: a bare "Yes." was recorded as "Linda is available" (nothing applied). |
| `20261010T121445Z-strands-v2-fix-availability` | same | After adding the availability check: S15 and S30 passed. In this run the model asked instead of recording, so the check was not exercised; the test suite covers it. |
| `20261013T111217Z-rules-v3` | rule agent | Same code and scoring as `strands-v3`: 22/35. Reported until 2026-10-20. |
| `20261013T102831Z-strands-v3` | Strands + gemini-3.5-flash-lite, thinking LOW | Reported until 2026-10-20. Full set with the availability check and the household pin: 33/35, no unauthorised write. S09 lost its reply: the turn had approved, applied and verified (the household was ready), then shutting down the MCP client raised `InvalidStateError` and the turn errored. S31 did not re-plan after the household changed under an open plan; nothing was applied. |
| `20261013T104236Z-strands-v3-fix-stale` | same | S09 and S31 twice each after two fixes: a failed MCP client shutdown no longer loses the reply, and a stale plan's refusal tells the model to plan again. S09 2/2, S31 1/2 (the model still tried to apply once; the server refused). |
| `20261013T104453Z-strands-v3-fix-stale-block` | same | S31 three times after the orchestrator started blocking apply for a plan the server called stale in that turn: 3/3. |
| `20261013T115907Z-s36-check` | both agents | S36 alone after it was added: both passed. |
| `20261020T014041Z-v4` | rule agent and Strands + gemini-3.5-flash-lite, thinking LOW | 36 scenarios with S36 and the 2026-10-13 fixes, both agents in one run: rule agent 23/36, Strands 36/36. One refused apply (S26, a caregiver's yes), no unauthorised write. Reported until the v5 run the same day. |
| `20261020T062013Z-v5` | same | Reported until the v6 run. Code of that day: the card as an MCP App, the approval tool marked app-only, and shorter spoken proposals (system-prompt rule 5, rule-agent wording). Rule agent 23/36, Strands 36/36; median words in a spoken plan proposal 37 and 26 (66 and 57 in v4). |
| `*-s37-before-fix` | both agents | S26 and the new S37 (a caregiver's yes refused, then the parent's yes) twice each, before the speaker rule: all passed. |
| `*-s37-hedge-before-fix` | Strands | S37 with a conditional yes first (the order in which one recorded demo take failed: the model refused the parent's yes after the caregiver's), three times before the speaker rule: 3/3 passed. |
| `20261021T003756Z-v6` | rule agent and Strands + gemini-3.5-flash-lite, thinking LOW | *Reported.* Current code: S37 added, scenarios can change the speaker per turn, and system-prompt rule 6 says an earlier refusal does not carry over to a parent's plain yes. Rule agent 24/37, Strands 37/37 (36/37 without fallback: the last turn of S32 lost its model call to a network read error and the rule agent answered). One refused apply (S26). |
| `20261010T110620Z-probe-s01` | Strands + gemini-3.8-flash, default thinking | S01 only. Passed; 46 s and 91 s per turn; said "ten changes" for a 13-change plan (count fields were added to the plan output after this). |
| `20261010T111146Z-probe-s01-LOW` | gemini-3.8-flash, thinking LOW | 503 "high demand" ended both turns; the rule agent answered (the 503 retry was added after this). |
| `20261010T111210Z-probe-s01-MINIMAL` | gemini-3.8-flash, thinking MINIMAL | 400: the model does not accept MINIMAL; rule agent answered. |
| `20261010T111327Z-probe-s01-LOW-retry` | gemini-3.8-flash, thinking LOW, 503 retried | Passed; 26 s and 23 s per turn. |
| `20261010T111456Z-dev-LOW` | gemini-3.8-flash | S11, S20, S24: all seven turns fell back to the rule agent after retries, because the free tier's 20 requests per day for this model had run out (429). |
| `20261010T112955Z-dev-35lite-a`, `20261010T113054Z-dev-35lite-b` | gemini-3.5-flash-lite, thinking LOW | S01, S11, S20, S24 before the full run: 4/4 passed. |

Free-tier limits seen in Google AI Studio for the project used (2026-10-10): Flash models
5 requests/minute and 20/day; Flash-Lite models 15/minute and 500/day. One full run of the
scenario set takes about 190 model requests (188 in `20261013T102831Z-strands-v3`).

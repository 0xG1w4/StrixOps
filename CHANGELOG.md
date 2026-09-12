# Changelog

## 1.2.2 — 2026-09-12

- List only saved, available evidence attachments, with matching file counts,
  categories and downloads.
- Treat evidence collection as best effort and stop missing path references
  from failing otherwise successful tasks; retain collection diagnostics in
  run metadata.
- Use the saved attachment inventory in deterministic and synthesized reports.
  Preserve failure handling for Agent/runtime, container cleanup, required state
  persistence and report errors.

Upgrade and behavior: [v1.2.2 release notes](docs/v1.2.2.md).

## 1.2.1 — 2026-09-12

- Default task conversations to all Agents while preserving explicit Agent links
  and the existing hint recipient rules.
- Rework conversation controls into a responsive grid with a bounded Agent
  selector, aligned Agent-panel/follow controls and visible keyboard focus.

Upgrade and behavior: [v1.2.1 release notes](docs/v1.2.1.md).

## 1.2.0 — 2026-09-12

- Add a FOFA page with server-side Email/API Key settings, saved searches,
  sortable and filterable asset tables, exports and target drafts.
- Run multi-target submissions as persistent batches of independent Web or
  Internal tasks, each with its own sandbox, context, evidence and report.
- Add per-batch concurrency and a shared host capacity limit for Console and
  CLI assessments, with cancellation and restart recovery.
- Preserve launch settings, prompts, skills and FOFA provenance while checking
  project scope again at target admission; keep historical reports readable.
- Add batch progress pages, per-target task/report links and responsive layouts.

Upgrade and behavior: [v1.2.0 release notes](docs/v1.2.0.md).

## 1.1.10 — 2026-09-12

- Update root and child Agent role guidance and expand the task skill library
  with six new skills and additions to six existing skills.
- Add report typography guidance for shorter paragraphs, meaningful subsections,
  tables, code blocks and evidence-supported flow diagrams.
- Render a bounded subset of Mermaid flowcharts in reports; unsupported or
  failed diagrams remain readable as source without breaking the report.
- Include the adapted skill library's MIT notice and Mermaid's license text.

Upgrade and behavior: [v1.1.10 release notes](docs/v1.1.10.md).

## 1.1.9 — 2026-09-11

- Move task web-search diagnostics into a compact header button and an overlay
  drawer, preserving space for the conversation and its hint composer.
- Show task-wide call counts and diagnostic alerts at the entry point; retain
  timing, usage, cost and recent records inside the drawer.
- Use a full-screen panel on phones, with keyboard dismissal and focus return.
  Keep the existing search execution and nonfatal failure behavior.

Upgrade and behavior: [v1.1.9 release notes](docs/v1.1.9.md).

## 1.1.8 — 2026-09-11

- Consolidate the task page into Conversation, Findings, Notes, Files and Report,
  while preserving links to the former tabs.
- Combine Agent selection, conversation history and a recipient-aware hint
  composer, with separate drafts and safe handling of in-flight submissions.
- Enforce explicit hint recipients, persist failed delivery states, and dedupe
  retries of uncertain submissions without changing the model execution path.
- Group shared notes, coverage and threat models under Notes; combine evidence
  and artifacts into one file browser with retained delivery metadata.

Upgrade and behavior: [v1.1.8 release notes](docs/v1.1.8.md).
Operation: [task page guide](docs/task-page.md).

## 1.1.7 — 2026-09-11

- Add persistent task-owned notes shared by Web/Internal root and child Agents,
  with five familiar note tools and no cross-task mutable notebook state.
- Protect replacements and deletions with revision checks, support atomic
  append, and retain bounded revision history and soft-deleted records.
- Preserve damaged notebook files and return truthful nonfatal storage errors;
  keep task cancellation and existing prompt snapshots intact.
- Guide selective note reuse and show a read-only Notes tab with paginated
  search, filters, full content, lazy history and visible refresh.

Upgrade and behavior: [v1.1.7 release notes](docs/v1.1.7.md).
Operation and limits: [shared notes guide](docs/shared-notes.md).

## 1.1.6 — 2026-09-11

- Include the new sandbox environment prompt in Web/Internal root and child
  instructions while preserving older run snapshots that lack this section.
- Guide focused, reactive delegation and independent vulnerability validation,
  including handoffs and an explicit unverified outcome when capacity is exhausted.
- Prefer available established tools and bounded batches, with operator traffic
  constraints and interaction requirements preserved.
- Clarify sandbox identity, reusable wordlists, evidence paths, HTTPQL filtering
  and evidence-based diagnosis of Caido versus upstream errors.

Upgrade and behavior: [v1.1.6 release notes](docs/v1.1.6.md).

## 1.1.5 — 2026-09-11

- Guide Web/Internal Agents to use current public research when useful and to
  continue with other tools when optional search is unavailable.
- Add Perplexity enable/model/timeout settings and a saved-settings connection
  test, preserving existing saved keys and automatic Console configuration.
- Bound async search retries and timeouts, share permanent-failure state within
  each run, preserve citations and reject empty or truncated answers.
- Show safe per-run search diagnostics and available provider-reported usage,
  with unknown or partial totals labeled explicitly.

Upgrade and behavior: [v1.1.5 release notes](docs/v1.1.5.md).

## 1.1.4 — 2026-09-11

- Supply bounded selected-request evidence and a skill catalog in the first MCP
  model turn, avoiding unnecessary request/skill lookup rounds.
- Replace automatic full baseline skill injection with compact HTTP evidence
  guidance, retain explicit skill choices, and avoid repeated skill loading.
- Use streaming MCP Agent runs with safe first-event/output timing, tool names,
  available token usage and finish diagnostics in the workbench and new reports.
- Remove the additional MCP-only 2,500-token output cap while preserving the
  configured model route, total time budget, cancellation and partial results.

Upgrade and behavior: [v1.1.4 release notes](docs/v1.1.4.md).

## 1.1.3 — 2026-09-10

- Resolve workspace-relative evidence references such as `output/file.json`
  against the delivered attachment index, preserving exact nested paths and
  continuing to report truly missing evidence.
- Decode gzip, deflate, Brotli, and declared text charsets for MCP request and
  response previews and Agent inspection. Preserve original transfer bytes,
  bound decoded previews, distinguish decode errors from binary content, and
  keep edited replay bodies consistent with their encoding headers.
- Give MCP request tests a 300-second default, visible remaining time, reserved
  wrap-up time, and model/tool/replay timing. Preserve configured budgets and
  partial results, and explain where an exhausted budget was spent in the
  workbench and newly generated reports.

Upgrade and behavior: [v1.1.3 release notes](docs/v1.1.3.md).

## 1.1.2 — 2026-09-10

- Initialize MCP browser access automatically when opened through a Console IP
  or localhost, persisting a private shared access token. Keep explicit server
  token authentication as an override and reject cross-origin bootstrap requests.
- Infer proxy connection settings from the Console address when capture starts.
  Generate private per-capture credentials for remote listeners, with explicit
  reveal/copy controls; keep passwords out of task polling, reports, and URLs.
- Preserve shared CA identity, existing captures, and Web/internal workflows.
  Document automatic setup and its existing Console deployment access boundary.

Upgrade and setup: [v1.1.2 release notes](docs/v1.1.2.md).

## 1.1.1 — 2026-09-10

- Complete remote browser access to MCP tasks: enter the configured server
  token in the workbench, keep it within the current tab, and authenticate
  task operations and CA downloads. Clear saved credentials and workbench state
  when access expires, while retaining existing local and Web/internal behavior.
- Add explicit proxy bind and advertised addresses for remote capture. Require
  separate proxy credentials for non-loopback listeners, preserve the loopback
  default, and exclude proxy authorization from saved traffic.
- Document IP-based deployment and token authentication behind reverse proxies,
  including forwarded client addresses and the distinction between Console and
  capture-proxy credentials.

Upgrade and configuration: [v1.1.1 release notes](docs/v1.1.1.md).

## 1.1.0 — 2026-09-10

- Add an independent MCP traffic workbench with task-owned proxy containers,
  website capture scope, saved pages/API requests, request/response inspection,
  and manual replay. Keep its storage and lifecycle separate from Web/internal
  runs, and expose the same operations through Streamable HTTP MCP tools.
- Test selected requests with saved Web model routes and compatible prompt/skill
  snapshots, bounded request/time budgets, isolated request containers, and
  persisted results. Generate versioned Markdown reports from saved evidence.
- Share one persistent CA across tasks and capture restarts. Prefer a valid
  legacy CA during migration, expose its public certificate and fingerprint,
  and retain it when deleting tasks. Existing proxies keep their certificate
  until the next start; the upgrade does not restart them automatically.
- Keep long task names, URLs, headers, and report content within their frontend
  columns, while retaining access to complete values in detail views.
- Add task deletion from the list, details, API, and MCP. Stop owned proxies,
  cancel and wait for active jobs, then remove task evidence; preserve the
  shared CA, other tasks, and retryable task records on cleanup failure.
- Add `mcp==1.29.1` and a direct `cryptography>=42` dependency. Pin the separate
  capture/replay image to mitmproxy 12.2.3 by digest. Keep the existing assessment
  sandbox at 1.3.0.

Upgrade and release scope: [v1.1.0 release notes](docs/v1.1.0.md).
Setup and current limits: [MCP operation guide](docs/mcp-traffic-workbench.md).

## 1.0.0 — 2026-09-09

First integrated StrixOps release, combining the native Python engine, FastAPI
API, and web Console. Includes the built-in system prompts and skill library.

- Add an optional multi-target mode to the launch page with text-file import,
  per-target validation, and preserved single-target drafts. Repeated CLI
  targets and target-list files create one shared run. Preserve the full scope
  in agent context, reruns, project checks, search, and report aggregation.
- Let the project overview expand its skill list beyond the first five entries
  and collapse it again, using the complete recorded analytics list.
- Add a Skills-page Prompt test dialog with saved route selection and a required
  user task. Send each saved prompt or skill unchanged with that task, without
  scan tools. Show progress, cancellation, original replies, and separate refusal,
  policy-block, provider-error, and inconclusive outcomes. A response without a
  detected refusal does not establish task completion or full-scan acceptance.
- Freeze prompt parts and skills per run with content hashes and per-agent
  prompts. Give children baseline guidance, shared scope, and bounded delegation.
- Persist shared coverage, threat models, authors, and revisions. Show missing
  assessment data as unknown rather than zero.
- Preserve dynamic and dependency findings, source and fix metadata, and report
  revisions. Keep distinct dependency manifests separate in project aggregation.
  Synthesize the final report from recorded run information through the selected
  model route, with deterministic composition as a fallback.
- Stop agent streams, child tasks, and sandbox writers before evidence capture.
  Publish successful completion after persistence and verified cleanup; preserve
  failure details and recovery workspaces.
- Correct evidence references followed by Chinese punctuation and verify
  directory references against delivered files. Include delivery counts and
  unresolved references in evidence cleanup errors.
- Serve evidence and artifacts with binary contents, range responses, and ZIP
  downloads. Include the built frontend in the wheel and retain consistent
  CLI, API, and package version metadata.
- Keep local test suites, scripted fixtures, external development instructions,
  credentials, scan output, and caches out of the published repository and
  distribution packages. Retain all built-in runtime prompts and skills.

Installation and release scope: [v1.0.0 release notes](docs/v1.0.0.md).

# Changelog

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

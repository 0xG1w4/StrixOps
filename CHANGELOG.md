# Changelog

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

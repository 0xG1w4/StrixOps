# StrixOps v1.0.0

Release date: 2026-09-09.

StrixOps is an integrated security assessment engine and web console for web
and internal-network engagements. Its Python engine, FastAPI API, and Next.js
frontend run as one product. The open-source
[Strix](https://github.com/usestrix/strix) project is a reference for selected
core behaviors and derived assets; see `NOTICE` for attribution.

The normal CLI and Console use the built-in StrixOps engine. Installation and
execution do not require a sibling Strix checkout, an external Strix Python
package, or a second engine process. The shared Docker image still extends a
Strix sandbox image; independence of the engine does not remove that image
dependency.

See [the v1.0.0 release notes](docs/v1.0.0.md) for installation and included
features. The wheel includes the built frontend and all built-in runtime
prompts and skills. Node.js is needed when building the frontend from source.

The `strix` compatibility command and `strixops` both start the built-in engine:

```
strix -t <target> --scan-type web|internal --crypto \
      --instruction-file <path> [--socks5 <val> | --gsocket <val>]
```

## Contract surface

| Interface | Behavior |
|---|---|
| Binary | console script `strix` (drop-in) and `strixops` |
| Env | `LLM_API_BASE`, `LLM_API_KEY`, `STRIX_LLM` (LiteLLM-prefixed), `STRIX_RUNS`, `STRIX_OPERATOR_HINTS_DIR`, `STRIX_HOST_WORKSPACE_DIR` |
| Run dir | `<STRIX_RUNS>/<slug>_<4hex>/` created (with an empty `events.jsonl` and a `run.configured` event) before any slow work |
| Events | `events.jsonl` — see `src/strixops/platform/events.py` |
| Artifacts | `penetration_test_report.md`, `vulnerabilities.json`/`.csv`/`vuln-NNNN.md`, `internal_findings/int-NNNN.md`, `run.json`, `.state/agents.json` |
| Exit codes | `0` clean scan · non-zero failure (platform maps: events+0 → reporting, events+nonzero → partial reporting, no events → failed) |

## Layout

```
src/strixops/
├── cli.py            # argv surface, exit codes
├── config/           # env settings, LLM provider routing
├── platform/         # the outward contract: run naming, events, artifacts, hints
├── engine/           # agent loop, coordinator, spawn, scan orchestration
├── agents/           # agent factory + system prompts
├── tools/            # agent function tools
├── report/           # run state: findings, run record, completion
├── runtime/          # docker sandbox backend
├── skills/           # skill corpus + registry
└── console/          # FastAPI server + web UI service
console/web/          # Next.js console UI (static export, served by strixops-console)
containers/           # shared sandbox image (Dockerfile.sandbox + build script)
```

## Console

The StrixOps console includes the runs dashboard, live conversation, agent
tree, findings, reports, operator hints, and scan launcher. From the source
checkout, install dependencies and build the frontend and sandbox once:

```bash
uv sync --frozen --no-dev
npm --prefix console/web ci
npm --prefix console/web run build
bash containers/build-images.sh
uv run --no-dev strixops-console                 # http://127.0.0.1:8300
```

One process serves both the API (`/api/*`) and the built UI. Configure a model
profile, launch a scan, watch the conversation stream live, send operator
hints mid-scan, and browse findings, assessment coverage, reports, and evidence.
Normal scans require a compatible model service and the Docker sandbox image.

The launch page defaults to the existing single-target form. Choose **Multi-target
task** to enter 2–100 targets, one per line, or import a UTF-8 `.txt` file up to
512 KiB. Blank lines and `#` comments are ignored and exact duplicates are merged.
Each target displays its format and project-scope check; launch rechecks the full
list against the current project scope. Switching back preserves the single-target
draft. One task type, model route, and instruction set apply to the entire list.
Web targets use the existing URL/domain/IP support; internal targets use hostnames,
IPs, or CIDR networks.

All targets belong to **one native StrixOps run**, with shared agent context,
per-target assessment guidance, a common evidence archive, and one final report.
Run details, reruns, project scope checks, search, and report aggregation preserve
the full list. Legacy single-target runs and API requests remain supported.

The project overview initially shows five skill entries. **Expand all** opens
the complete recorded list; **Collapse** restores the compact view. Entries
are ordered by recorded hit count, then skill ID. Counts combine dynamic load
events and explicit agent-injection events; they do not measure skill
effectiveness or enumerate every automatically preloaded instruction.

The CLI also accepts repeated target flags and UTF-8 list files (up to 1 MiB each):

```bash
uv run strixops -t https://app.example.com -t https://api.example.com
uv run strixops --target-list ./targets.txt
```

Flags and files combine into one ordered list of at most 100 distinct targets;
each target is limited to 2048 characters. This does not add source repository or
API-spec staging to the existing Web/internal target types.

Model profiles share a provider URL and API key, with separate model, API type,
and reasoning effort for Web and internal scans. Choose **Auto**, **Chat
Completions**, or **Responses** beside each model. Auto uses Responses for the
documented Astra, GPT-5.4 Pro, GPT-5.5, and GPT-5.6 routes; other names use Chat
Completions. Deployment aliases can be configured explicitly. **Provider
default** omits the reasoning parameter, while **none** sends an explicit value;
available effort levels depend on the model. Manual API choices are honored for
all model names, including GPT: native OpenAI API/tool compatibility restrictions
are shown as advisory notices because gateways may translate requests. Check
the gateway's support with **Test model**. Invalid option values and unsupported
reasoning effort levels are still rejected. Empty model slots inherit the other
slot's model, API, and effort together.

**Duplicate** opens a new editable profile draft. Its saved key is reused on the
server only for the same provider and API URL; changing the destination requires
a new key. Creating the copy does not replace or activate it over the original.
**Test model** makes two short streamed requests using a harmless function,
checking the selected API, effort, and function-result round trip without
starting a scan. This uses the provider's normal model quota. A model catalog
listing alone does not establish tool compatibility.

CLI launches can select the same options with `LLM_API_MODE` (`auto`,
`chat_completions`, or `responses`) and `LLM_REASONING_EFFORT` (`default`, `none`,
`minimal`, `low`, `medium`, `high`, `xhigh`, or `max`, subject to model support).
Missing API settings retain the legacy Chat Completions route. New profiles
start with Auto. Explicit route effort applies to agents and their summary and
deduplication requests. Runs record the requested/resolved API and selected
effort for diagnosis; failed requests do not silently switch protocol or effort.

For Responses stream failures, the run log and model test retain safe error
codes, parameters, and request IDs when the provider supplies them. Use the
request ID to find the underlying error in the gateway/provider logs. A generic
LiteLLM `Response API in-stream error` does not identify the cause. Agent runs
apply the existing bounded retry policy to explicitly transient stream errors
only before output begins; unknown errors, rejected parameters, and errors after
text or tool output are not automatically replayed.

An explicit `cyber_policy` rejection requires checking the provider's approval
for security work on the actual API organization/project behind the route.
Changing API type or reasoning effort does not grant that access. Some gateways
hide policy errors inside a generic streamed 500; inspect their server logs for
the original error. StrixOps reports recognizable policy rejections separately
and does not retry them or switch models to work around them.

## Development

Frontend changes require another `npm --prefix console/web run build` before
starting the Console. Python runtime dependencies are defined in `pyproject.toml`
and the frozen `uv.lock`.

Regression suites, scripted model fixtures, and development verification
records are available only in the local internal test workspace. They remain
outside the published Git repository and distribution packages, together with
external development instructions, credentials, scan output, and caches.
All built-in runtime prompts and skills remain part of the product.

## Run consistency and assessment

Each run freezes prompt parts and skill content in `.state/prompt_resources.json`,
with hashes in `.state/prompt_manifest.json`. Console edits apply to subsequent runs; running
agents and children use their run's snapshot. Web children preload the basic
tooling, browser, counterevidence, and severity guidance, and receive the
engagement scope independently of inherited history.

Children can delegate within configured limits. Defaults are depth 2 (root
depth 0), 4 active agents, and 12 agents over the run's lifetime, including
the root. Override with `STRIXOPS_AGENT_MAX_DEPTH`,
`STRIXOPS_AGENT_MAX_ACTIVE`, and `STRIXOPS_AGENT_MAX_TOTAL`. Completion checks
unsettled descendants. These limits bound orchestration; they are not a token
or monetary budget.

Coverage and threat-model tools share run-owned state in `assessment.json`,
including author and revision history. Coverage is agent-reported and does
not establish exhaustive target coverage. Older runs without these records
display unknown coverage, including an unknown unresolved count. Completion
of a scan is separate from coverage completeness and absence of findings.

Dependency reports preserve package, installed version, manifest, advisory
score, reachability evidence, and contextual CVSS separately from dynamic
findings. Optional source locations, fix details, confidence rationale, and
revision history survive JSON, Markdown, and Console display. Project report
aggregation distinguishes findings from different dependency manifests.

## Context management

Each agent keeps a separate SDK session in the run's `.state/agents.db`.
Following Strix v1.6.0, the engine checks context usage before each run cycle,
summarizes older history through the task's configured model, and preserves
a recent token-sized window with tool calls and results kept together.
Context overflow can trigger at most two forced compactions per cycle.
This stores conversation state; it does not add a resume command.

For internal scans, the root preloads a compact engagement contract and
methodology. Every child receives the authorized scope, declared access,
operator instructions and the required contract independently of inherited
history. A SOCKS5 endpoint is network access, not a shell; agents must verify
remote execution context before attributing commands to a target. Use
`list_skills` to discover canonical IDs and `load_skill` for technique details.
Missing or ambiguous requested skills fail explicitly.

The Console's Skills page also offers **Prompt test**. Open the dialog, choose
a saved model route and its Web/internal assignment, enter the task you want
to test, select saved prompt parts or skills, then start the checks. A task is
required; there is no default description question. Each selected file makes one streamed model
request, with no tools or scan execution. The dialog shows progress, supports
stopping, and separates ordinary responses, structured refusals, explicit policy
blocks, provider errors, and inconclusive results. Model requests incur normal
provider usage; opening the dialog alone sends no model request.

Each request sends the saved file unchanged as system instructions and your
exact task as the user message. All items in a batch receive the same task;
editing it clears previous results. Template variables are not expanded, and
the complete scan context is not assembled. **No refusal observed means no
refusal signal was detected in this response; inspect the reply for task
completion.** The result belongs to the fragment, task, and route together;
it does not establish which input caused a refusal or predict a complete scan.
Text-based refusal hints remain inconclusive, because wording alone cannot
reliably establish a policy decision.
Results include the task, original model reply (with credential redaction and
an explicit flag if the 32,768-character display limit is exceeded), source
and task hashes, route, model, API mode, reasoning effort, and time. Editing
a file or route after loading the dialog requires refreshing the catalog.
Results are scoped to the current dialog session. An explicit
`cyber_policy` result stops the remaining batch because it may indicate an
account or route access restriction; ordinary per-request content filters
remain separate results.

Only successful lifecycle tools can complete an agent; plain text or JSON
declaring completion does not change trusted run state. Internal campaign
observations and cleanup resources are shared through `record_internal_event`
and `get_internal_campaign`, persisted in `run.json`, and audited through
`campaign.internal_event`. Cleanup records refer to exact resources created or
modified by this engagement and preserve pre-existing logs and configuration.

The original environment settings and defaults apply:

| Setting | Default |
|---|---|
| `STRIX_CONTEXT_AUTO_COMPACT` | `true` |
| `STRIX_CONTEXT_BUFFER_TOKENS` | `20000` |
| `STRIX_CONTEXT_KEEP_TOKENS` | `8000` |
| `STRIX_CONTEXT_FALLBACK_TOKENS` | `200000` |
| `STRIX_CONTEXT_SUMMARY_TOKENS` | `4096` |
| `STRIX_TOOL_OUTPUT_MAX_TOKENS` | `8000` |
| `STRIX_TOOL_OUTPUT_MAX_LINES` | `2000` |
| `STRIX_TOOL_OUTPUT_MAX_BYTES` | `51200` |
| `STRIX_MAX_CONTEXT_IMAGES` | `3` |

Model metadata supplies the context/output limits when available. Unknown
model names use the fallback context setting; set it to the model's actual
limit when needed. Compaction makes an additional model request, adding
latency and token usage. Tool output exceeding the
line/byte limits is previewed with a head/tail excerpt and saved in full to
`/workspace/.tool-output/` in the sandbox when storage succeeds.

Sandbox agents can inspect workspace screenshots with `view_image`. The
existing compatible/OpenRouter route sends the image content to the selected
model, which must support image input. At each run cycle, the engine keeps
the newest three image tool outputs by default and replaces older image
blocks with text. Following the reference, input rejection (400/404/422)
can trigger image removal and retry, at most three times per cycle.

## Reports and child context

Vulnerability creation and updates validate the original eight CVSS keys:
`attack_vector`, `attack_complexity`, `privileges_required`, `user_interaction`,
`scope`, `confidentiality`, `integrity`, and `availability`. Invalid metrics
are rejected before changing a report; valid updates replace the score,
severity and vector together.

When reports already exist, deduplication uses the task's configured model
and the original comparison rules for endpoints, parameters and root causes.
This adds a model request. As in Strix, a failed or unusable deduplication
response allows the candidate through.

With `inherit_context=true`, a child receives its parent's SDK input-history
snapshot as background, followed by its own identity and assignment. Images
are replaced by text in inherited history. This follows Strix's snapshot
rule: it does not include tool outputs generated later within that same SDK
run cycle or subsequent parent updates.

Final report synthesis uses the run's configured model with recorded findings,
assessment information, and the agent's closing narrative. It adds model usage;
the deterministic report composer remains the fallback when synthesis fails or
is disabled. Original findings and evidence remain available alongside the report.

Internal finding severity participates in the overall report's default rating.
Large datasets use a complete attachment and one distinct finding, not one
finding per row. Set `metadata.evidence_files` to filenames relative to
`/workspace/output/` (or absolute paths within that directory).

Agents and sandbox writers are stopped before evidence is collected on success,
failure and ordinary interruption. Successful completion is published only
after report persistence and verified container cleanup. Cleanup failures are
recorded as failures rather than successful scans. Without
`STRIX_HOST_WORKSPACE_DIR`, the engine retains a private
`<run_dir>/workspace` bind mount; operator-provided workspaces remain intact.
Files are copied into `<run_dir>/evidence` with streamed SHA256, without the
former 50 MiB cutoff. Both workspace originals and archive copies remain on disk.
The manifest and `run.json` distinguish captured, persisted and deliverable
files. Missing references, copy failures, symlinks and special files are marked
incomplete rather than counted as delivered attachments. Directory references
are checked against delivered files; adjacent Chinese punctuation is not treated
as part of a filename. Evidence cleanup errors include delivery counts and
unresolved references. Forced process death
cannot generate a final manifest, but the persistent workspace remains available.

## Sandbox image

Both web and internal scans use `strixops-sandbox:1.3.0`. This shared image
extends `ghcr.io/usestrix/strix-sandbox:1.3.0` with the internal-network tool
layer, so both modes have the same installed tools (see
`containers/Dockerfile.sandbox`). Build it once before starting scans:

```bash
bash containers/build-images.sh
```

The optional first argument selects the image tag. `BASE_IMAGE` overrides
the upstream base for the build; `STRIXOPS_IMAGE` selects a custom runtime
image for both scan modes. The engine checks image presence at run time.
`scan_type` still selects the scan workflow and sandbox environment: web
scans enable Caido proxy interception; internal scans disable Caido and
apply the requested tunnel settings.

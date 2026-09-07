# StrixOps

StrixOps is an autonomous penetration-testing agent engine and the drop-in
scan core for the Strix platform. It is written from scratch with the
open-source [Strix](https://github.com/usestrix/strix) project (v1.6.0) as
its behavioral specification; see `NOTICE` for attribution.

The platform (`apps/api` + `apps/web`) spawns StrixOps exactly the way it
spawned `strix` — same argv, same environment, same run-directory and
event-stream contract — so **no platform code changes are required**:

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
├── console/          # 🖥 console: FastAPI server + web UI service
└── testing/          # scripted (no-LLM) model for dry runs
console/web/          # Next.js console UI (static export, served by strixops-console)
containers/           # shared sandbox image (Dockerfile.sandbox + build script)
tests/{contract/, unit/, integration/}
```

## Console

The StrixOps console is the built-in web UI — runs dashboard, live
conversation view, agent tree, findings, reports, operator-hint composer,
and a scan launcher:

```bash
cd console/web && npm install && npm run build   # one-time UI build
uv run strixops-console                          # http://127.0.0.1:8300
```

One process serves both the API (`/api/*`) and the built UI. Launch scans
from the UI (dry run needs no LLM key), watch the conversation stream live,
send operator hints mid-scan, and browse findings/reports.

## Development

```bash
uv sync                                   # create venv + install
uv run pytest                             # unit + contract tests
uv run strixops --dry-run -t https://example.com --scan-type web \
    --instruction-file ./instruction.md   # no-LLM full-stack dry run
```

Dry-run mode (`--dry-run` or `STRIXOPS_DRY_RUN=1`) swaps the LLM for a
scripted model and the docker sandbox for a local-process backend, driving
the entire stack — run dir, events, artifacts, exit code — with zero tokens.

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

Internal finding severity participates in the overall report's default rating.
Large datasets use a complete attachment and one distinct finding, not one
finding per row. Set `metadata.evidence_files` to filenames relative to
`/workspace/output/` (or absolute paths within that directory).

Evidence is collected before sandbox teardown on success, failure and ordinary
interruption. Without `STRIX_HOST_WORKSPACE_DIR`, the engine retains a private
`<run_dir>/workspace` bind mount; operator-provided workspaces remain intact.
Files are copied into `<run_dir>/evidence` with streamed SHA256, without the
former 50 MiB cutoff. Both workspace originals and archive copies remain on disk.
The manifest and `run.json` distinguish captured, persisted and deliverable
files. Missing references, copy failures, symlinks and special files are marked
incomplete rather than counted as delivered attachments. Forced process death
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

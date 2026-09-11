# Optional web search

Web and Internal Agents can use Perplexity through `web_search`. Search is an
optional research tool: disabling it or encountering a provider failure does not
fail the scan. The primary scan model and other tools remain independent.

## Console setup

Open **Settings → Integrations → Web search**. Save the enable switch, API key,
model and timeout. An existing saved key continues to work after upgrading.
Leaving the key field blank or masked preserves the saved key; switch search off
to disable it. A key from the Console environment is used when no saved key exists.

- `sonar` is the default for focused public lookups, with a 30-second timeout.
- `sonar-reasoning-pro` is available for more complex research, with a 300-second
  default timeout. Both models accept a configured limit of 10–300 seconds.
- **Test connection** uses the saved settings and sends one fixed public query.
  This can consume API credit. It does not send scan data or start a scan.
  A configured key is not proof that the account has working service or credit.

Changes apply to newly launched scans. The Console passes the effective settings
to the engine process; manual environment setup is not needed for Console scans.
Existing scan processes retain their launch settings. No database or CA migration
is required.

## Agent behavior

The shared Web/Internal prompt encourages targeted searches for current product
and CVE applicability, official documentation, changed controls, and unfamiliar
tool behavior. Agents decide when a query will help their assignment; search is
not a mandatory step in every scan. Existing per-run prompt snapshots are preserved.

Queries must exclude credentials and private request bodies. Search results are
untrusted reference material. Citations help verify public claims, but findings
still require evidence from the authorized target. An unavailable search service
does not establish that a target is secure.

Disabled or unconfigured search returns immediately. Authentication, forbidden
access and exhausted-credit responses disable further provider calls for that
run, including its child Agents. Rate limiting uses a finite cooldown. Temporary
network or service errors receive at most one retry within the same overall
search timeout. The result tells the Agent to continue with available tools and
record unresolved verification needs. Starting another run starts a fresh search
state, so a previous account failure does not permanently disable future scans.

Empty, malformed, or truncated responses are reported as failed searches rather
than complete answers. Reasoning-only content is not accepted as a final answer.
Task cancellation still cancels outstanding search work.

## Search activity

Open **Web search** in the task header to view calls, outbound requests,
outcomes, elapsed search time and available usage in a side drawer (full-screen
on phones). The records cover all Agents in the task, and opening the panel
does not initiate a search. Perplexity usage is separate
from the primary scan model's usage. Cost is the provider-reported USD total;
missing values are unknown, and incomplete totals are marked partial.

The run-local `.state/web_search.json` stores aggregate counters and up to 20
recent metadata records, without queries, answers, credentials or provider error
bodies. This does not remove ordinary search tool arguments/results from existing
Agent transcripts. Diagnostics failures do not stop the scan. Older runs without
search metadata remain readable and show that no records are available.

## CLI configuration

| Variable | Default / behavior |
| --- | --- |
| `PERPLEXITY_API_KEY` | Required to send searches; missing key skips search. |
| `PERPLEXITY_ENABLED` | `true`; `false` disables search even when a key exists. |
| `PERPLEXITY_MODEL` | `sonar`; also accepts `sonar-reasoning-pro`. |
| `PERPLEXITY_TIMEOUT_SECONDS` | 30 for Sonar, 300 for Reasoning Pro; allowed range 10–300. |

Invalid search configuration disables search safely rather than preventing scan
launch. Console settings take precedence for Console-launched scans. These
settings do not add tools to MCP request tests or alter their replay budget.

Response fields and models follow the
[Perplexity API reference](https://docs.perplexity.ai/api-reference/sonar-post).

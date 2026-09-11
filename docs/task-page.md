# Task page

Web and Internal assessment tasks share five main tabs:

| Tab | Purpose |
|---|---|
| Conversation | Read Agent activity, select an Agent and send it an operator hint. |
| Findings | Inspect individual security findings, severity and supporting details. |
| Notes | Read shared notes, recorded test coverage and threat models. |
| Files | Search, preview and download saved evidence and other task artifacts. |
| Report | Read and download the complete assessment report when available. |

The separate MCP workbench keeps its existing navigation and task lifecycle.

## Web search diagnostics

The **Web search** button in the task header opens a side drawer on desktop
and a full-screen panel on phones. It shows call counts and any recorded
diagnostic alerts without placing a statistics panel above the conversation.
The drawer contains success/failure/skip counts, actual API requests, timing,
reported token usage and cost, and the most recent calls.

These records cover the entire task, regardless of the selected Agent.
Opening the drawer reads existing task records; it does not initiate a search.
Closing it preserves the conversation, selected Agent and unsent hint draft.
Use Escape or the close button to return to the task.

Missing records and unavailable usage stay unknown rather than being shown as
zero. A read failure offers a retry, and prior data remains visibly marked as
stale when available. Search-service failures keep their existing nonfatal
behavior and do not stop the assessment.

## Conversation and hints

Choose an Agent in the conversation controls. Its timeline and the hint
recipient change together. The Agent hierarchy also exposes status, assignment
and the saved prompt when it can be matched to that Agent's identity. Identical
display names do not select another Agent's prompt.

The composer stays at the bottom of the conversation. Its recipient is visible
before sending. In the **all Agents** timeline, the recipient is explicitly the
root Agent; this view does not broadcast hints. Use Enter for a newline and
Ctrl/Cmd+Enter or the Send button to submit. A hint can contain up to 16,000
Unicode characters.

Drafts are separate for each task and recipient while the Console page remains
open; reloading the page clears them. Switching away and back restores that
draft. Once a hint is sent, its recipient and text remain fixed even if you
navigate elsewhere. Pending request
completion updates the original submission and does not clear a different
recipient's draft.

Only an available task and a running/waiting Agent accept new hints. A finished,
unknown or missing Agent remains viewable but cannot receive a new instruction.
If it finishes while a hint is queued, delivery fails visibly. Select root and
send a new hint there if you want the main Agent to handle the request; the
system does not silently reroute it.

Hint history appears once in the conversation, filtered by recipient. The
statuses have distinct meanings:

| Status | Meaning |
|---|---|
| Queued | Console saved the hint for the engine to collect. |
| Delivered | The intended Agent's mailbox accepted the hint. |
| Acknowledged | The Agent echoed the hint token; the requested work may still be pending. |
| Not delivered | The intended recipient could not accept it; inspect the displayed reason. |

A network error can leave the submission outcome uncertain. Use the offered
retry/check action: it reuses the same request ID and original payload. If the
server already accepted it, the existing record is returned. Do not recreate
the same instruction as a new request solely because its first HTTP response
was lost. A retry can retrieve an existing record after the task ends, but
cannot create a new hint for that finished task.

## Notes

Notes has three views: **Shared notes**, **Test coverage**, and **Threat models**.
These are different task records under one navigation entry. Shared notes keep
their version history and filters; coverage retains unresolved outcomes and
evidence; threat models retain their amendments and history. A shared note in
the findings category is still reference material, not a formal vulnerability
report. See the [shared notes guide](shared-notes.md) for tool and storage limits.

## Files

Files combines the existing artifact index and evidence metadata into one
browser. Filter **All**, **Evidence**, or **Other files**, then search and select
a file for preview or download. Files are identified by their complete
run-relative paths, so files with the same basename in different directories
remain separate. Evidence already present in the artifact index appears once.

Evidence metadata includes its classification, size, hash and collection time
when available. Metadata for oversized or otherwise undelivered evidence remains
visible; an unavailable file is not offered as a successful download. Recorded
undelivered items remain in the file list. An index request failure displays a
read error; missing collection metadata cannot be reconstructed by the browser.

Preview, original bytes and ZIP downloads continue to use the existing task
file endpoints and their access boundaries. Only API-visible files are included;
the browser does not expose hidden task state by walking the filesystem itself.

## Saved links

Older task URLs remain useful:

| Former tab | Current location |
|---|---|
| `agents` | Conversation, all Agents |
| `hints` | Conversation, root or explicitly selected Agent |
| `assessment` | Notes → Test coverage |
| `evidence` | Files → Evidence |
| `artifacts` | Files → All |

The current main-tab keys are `conversation`, `findings`, `notes`, `files` and
`report`. `agent` selects an Agent ID, with `all` selecting the overview.
`note_view` selects `shared`, `coverage` or `threat_models`. These URL values
control presentation; the server validates the actual hint recipient.

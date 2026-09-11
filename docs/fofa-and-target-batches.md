# FOFA searches and target batches

Operator guide for the Console's asset discovery and scheduling workflow.

## Quick start

1. In **Settings → FOFA**, save your API Key and, if needed by your account,
   Email. The connection test uses the saved settings. Leaving the key field
   empty preserves the current key; removing it is an explicit action.
2. Open **FOFA**, enter a query and choose how many rows to retrieve. The default
   is 100; the maximum is 10,000. Your account and query determine what FOFA
   actually returns. Each acquisition may consume provider allowance.
3. Filter or sort the saved table and select assets, including across pages.
   **All matching** refers to the filtered saved rows, not every asset reported
   by FOFA. CSV/JSON exports apply to the same filters.
4. Create a target draft, review it in the launch form, choose model/instructions
   and optional project, then submit. Up to 100 distinct HTTP(S) targets can be
   handed off at once.
5. Open **Batches** to inspect waiting and active targets. Each started target
   links to its own ordinary task page and report.

## Execution model

A batch is an ordered collection of independent target assessments. Each target
has its own ordinary Web or Internal run, sandbox, Agent context, evidence and
report. Batch membership does not establish a relationship between targets.
Existing historical multi-target reports remain readable.

Manual target lists, FOFA selections and Console API submissions share the same
batch scheduler. The batch concurrency setting defaults to two. A separate,
shared host limit defaults to two active assessments and applies to ordinary
single-target runs as well. MCP capture and request-test containers retain their
separate lifecycle and do not use this assessment limit. Queued targets do not start a sandbox. A slot is
released after the owned work and sandbox have stopped, including cancellation
and failure paths. Uncertain cleanup must remain visible for recovery.

Each target follows the existing model, prompt, skill and report pipeline. A
batch captures common launch configuration; its targets receive separate scope
and prompt snapshots. Project assignment is optional, checked at submission and
again before each target starts. Project reports remain an explicitly generated
aggregation of independent run reports.

The settings apply to assessments using the same queue database. They are limits
on active target assessments, not a strict CPU/RAM reservation: a task may still
create child Agents and use several tools inside its sandbox. Choose a lower
limit for a smaller host. Lowering a limit lets running work finish and delays
new admissions; it does not terminate an active assessment.

Cancellation stops waiting targets immediately and asks active targets to run
their normal cleanup. Delete is available once all items are terminal; it removes
the batch grouping and private launch snapshot, preserving independent run data.

## FOFA workflow

The sidebar's FOFA page provides a compact query editor, collapsible search
history and a result table. Each search stores its query, acquisition settings,
timestamps, fetched rows, provider total and completion state. Reopening history,
filtering and sorting operate on saved data, not another provider request.

The table supports typed sorting, column filters, pagination, optional columns,
row details and explicit selection of a page or all matching saved rows. Long
values stay within the table and are available in a details drawer. A persistent
selection bar shows the selected count and creates a target draft for the
existing launch form. Original source associations survive removing some targets;
manually added or changed targets are not falsely attributed to FOFA.

The initial direct handoff supports HTTP and HTTPS assets. Unknown protocols and
non-Web services remain viewable and exportable, with a clear explanation that
they need target-type review. A service port must not silently become an
unrestricted host assessment.

FOFA credentials live in a private server-side settings store. Public responses
only expose whether a key is configured and its masked form. Provider credentials
never enter task instructions, artifacts, browser URLs or exported result files.
Search interruption preserves completed pages; reopening a partial search does
not automatically repeat a potentially chargeable request.

## Storage and recovery

FOFA defaults to the `fofa` directory alongside the Console settings file
(`~/.strixops/fofa` in the normal configuration). It contains private settings,
saved search metadata/results and immutable handoff drafts. `STRIXOPS_FOFA_ROOT`
can override this location. Deleting a search retains a draft already created
from it, so a submitted batch's provenance remains available.

The queue database is `scan_queue.sqlite3` beside Console settings, with private
launch snapshots in `scan_queue_snapshots`. `STRIXOPS_QUEUE_DB` overrides the
database path. Back up these together with run directories and Console settings;
launch snapshots contain the model credentials needed by waiting targets and
must be kept private. Neither snapshots nor FOFA keys are included in task
artifacts or result exports.

Console restart resumes dispatch for its runs directory. Existing child scans
keep their leases. CLI batches have their own supervisor and can be managed
again using `strixops --resume-batch ID`. A cancelled or failed assessment is not
silently repeated, and interrupted Agent conversations are not resumed.

On abnormal process exit, recovery first verifies the original process identity.
For scans that could have created a sandbox, it also verifies the Docker daemon
identity and the task's unique ownership label, removes only those owned
containers and confirms removal. Unconfirmed resources remain **blocked** and
occupy capacity; recovery retries while the scheduler is active. Restore access
to the original Docker daemon if it was unavailable. Do not delete the queue
database to bypass blocked capacity while old scans or containers may remain.

## Interface

Use the existing Console palette, typography and theme variables. Query controls
and actual results lead the FOFA page, without a decorative hero. The batch page
shows a compact progress strip and target rows with independent task/report links.
Status copy distinguishes waiting, running, cancellation, failure and completed
work. On narrow screens, history collapses and wide tables scroll within their
own frames. Keyboard focus, labeled controls and reduced motion remain supported.

## Verification

Use isolated fixtures for provider responses, process launch, failure, restart,
cancellation and slot ownership. Verify global and batch concurrency independently,
no repeated launch after recovery, report/scope isolation, masked credentials,
cross-page filtering and selection, project checks and existing single-target
workflows. Browser checks cover desktop/mobile, both themes, long data, empty and
partial states, target draft handoff and independent report navigation.

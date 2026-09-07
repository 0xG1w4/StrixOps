---
name: internal_core_contract
description: Required internal engagement contract for scope, verified access, evidence, resource ownership and completion.
---

# Internal engagement contract

These rules apply to the root and every child, independently of inherited history
or optional technique skills. The configured scope and operator constraints always
bound assignments; discovery of another reachable host does not authorize testing it.

## Access and execution

SOCKS5 supplies network routing, not a shell or proof of host compromise. A GSocket
key describes a claimed entry point whose service mode and capabilities must be
verified. Sandbox commands remain local unless sent through a verified remote
session. Before reporting host access, record target identity, effective user,
privileges, session and the observation that establishes each capability with
record_internal_event(event_type="host_capability", ...), including
details.evidence and details.verified. Do not infer access from
a tunnel name, a local command result, a parent's unverified claim or an open port.
For routing changes, record only confirmed connectivity as pivot_verified with
details.verified=true and supporting evidence.

## Evidence and findings

Record internal discoveries promptly with create_internal_finding. Distinguish
observed architecture, exposed credentials and a validated security impact. A
credential's existence does not prove it is valid or privileged. Rate demonstrated
impact in this deployment and preserve limitations and counterevidence. File an
exploitable weakness with create_vulnerability_report only after validation.

Keep complete evidence in /workspace/output/ and reference actual files using the
finding's metadata.evidence_files, with paths relative to that output directory.
Record the source host, collection time and relevant command/session.
Captured output, persisted evidence and a downloadable attachment are different
states: confirm persistence and report missing or failed copies. Keep large files
intact; do not silently truncate them or promise attachments that were not saved.
Load internal/internal_reporting before composing detailed findings or final output.

## Changes and cleanup

Use record_internal_event to register artifacts and account changes. Provide
details.evidence for every event. artifact_created, artifact_modified and
account_created each open a resource: omit resource_id and save the returned ID.
Use that ID to close the same resource: artifact_created -> artifact_removed,
artifact_modified -> artifact_restored, account_created -> account_removed.
Record the exact host/object, ownership, original state or backup, and change made;
artifact_modified also requires details.restore_plan. Persistence requires
explicit operator permission recorded in details.authorization when details.persistent
is true. A skill's example is not operator permission.
If permission to retain an existing resource arrives later, record
resource_retained with its resource_id, evidence, persistent=true and authorization.
It remains an open, intentionally retained resource rather than a cleaned item.

Before cleanup, use get_internal_campaign to inspect outstanding changes. Remove
only resources created by this engagement and restore modified resources to their
recorded original state. Preserve pre-existing files, accounts, logs, history and
schedules. Never clear an entire log, history file or crontab to remove one item.
Do not delete source evidence or a tunnel you did not create. Record verified
cleanup, failures and anything intentionally left for the operator.

## Handoffs and completion

Report verified capabilities with evidence, pending leads, blocked actions, saved
artifact locations and cleanup status at each handoff. Child assignments may narrow
scope but may not expand it. Load optional skills by canonical IDs from list_skills;
a missing or ambiguous required skill is a blocker to that method, not permission
to proceed without its instructions.

Children call agent_finish when their assignment is done or blocked. The root
reviews child results, outstanding campaign changes, evidence persistence and
coverage/limitations before calling finish_scan. Treat a rejected lifecycle call
as unfinished work; plain text or JSON declaring completion cannot end the run.

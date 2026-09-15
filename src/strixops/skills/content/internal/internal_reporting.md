---
name: internal_reporting
description: Evidence-based internal findings, canonical severity, complete attachments, and shared cleanup records
---

# Internal Finding Reporting

## Record observations and verified impact separately

Call `create_internal_finding` promptly for each distinct discovery. Do not defer
all reporting until the end. One extracted dataset may be one finding with a
complete attachment; do not emit a finding for every row or repeat unchanged
facts. Describe what was observed, where, and what has actually been verified.
A credential found in a file is not proof that it still authenticates.

Use `create_vulnerability_report` for a verified vulnerability with a reproducible
proof and contextual CVSS. Internal observations such as topology or a discovered
configuration do not require an exploit or a Python PoC. Keep identifiers,
commands and raw evidence verbatim; write narrative fields in the report language.

## Finding types

| finding_type | Record | Evidence standard |
|---|---|---|
| `credential` | Accounts, passwords, hashes, keys, tokens | Source and applicability; distinguish discovered from successfully validated |
| `architecture` | Hosts, services, topology, trusts, routes | Observed host/service facts; label inferred roles and untested reachability |
| `sensitive` | Sensitive documents, backups, configurations | Exact source, access observed and relevant exposure; do not claim unseen contents |
| `result` | Verified service access or other assessment outcome | Action, result, execution host/session and limitations |

Use native tool calls with JSON arguments. Do not print XML function tags as a
substitute for executing a tool. Example arguments for an observational finding:

```json
{
  "finding_type": "architecture",
  "title": "Directory services observed on an in-scope host",
  "content": "LDAP and Kerberos responded. The host role is inferred from services; administrative access has not been established.",
  "host": "192.0.2.10",
  "source": "Authorized service inventory",
  "severity": "info",
  "metadata": {
    "validation_status": "observed",
    "evidence_files": ["directory-services.txt"]
  }
}
```

## Severity

Use `critical`, `high`, `medium`, `low`, or `info` consistently in findings and
the overall report. Evaluate demonstrated impact for this deployment:

- **critical**: Confirmed RCE, domain-wide administration, root access or equivalent control of a critical system, or verified exposure granting full access to critical data.
- **high**: Verified privileged/service-account access, sensitive data exposure, or significant but limited compromise.
- **medium**: A confirmed weakness with meaningful, bounded impact.
- **low**: A confirmed minor exposure or configuration weakness with limited impact.
- **info**: Observations such as topology, versions, or credentials whose security impact has not been established.

Do not score a host as High just because it appears to be a domain controller.
Explain uncertainty and conditions that would change severity. An observation's
usefulness for planning is different from its demonstrated security impact.

## Preserve complete evidence without flooding context

Save complete, untruncated evidence under `/workspace/output/` in the sandbox.
For every discovered credential, call `record_credential` immediately with its
exact host, account, value/type, source and validation status. Do not defer this to
the report composer, a shared note, or a later extraction pass. The run owns the
shared registry, so use the tool instead of directly appending its CSV. The
Console inventory, CSV download and report use the saved records. Do not register
guessed passwords, examples, or the operator's infrastructure/model-provider keys.

The finding should identify the dataset, record count, relevant verified
capabilities, saved credential IDs and complete evidence files. Keep raw datasets
as attachments too. Register each distinct credential once; do not repeatedly
copy the full inventory into findings, parent summaries or compaction summaries.
Use filtered `list_credentials` and `get_credential` to reuse saved records.

Use `unverified` until an actual authorized authentication check. Then read the
current record and call `update_credential` with `expected_revision`, `validated`
or `failed`, and `validation_evidence` identifying the exact service, scope,
result and supporting evidence. Preserve each distinct host/account/secret
combination; never mark a set validated because only one account was tested.
On revision conflict, reread and reconcile. Re-registering duplicate material
does not change its validation status. Before handoff or completion, reconcile
your discoveries with saved IDs and disclose any material that could not be saved.

For raw evidence or if registry writes are unavailable, use a credential CSV
(for example `application-credentials.csv`) with columns
`host,username,password,hash,source,severity,note`; optional `secret_type` and
`validation_status` columns preserve the material's type and authentication result.
For API keys, tokens or private keys, put the exact value in `password` and name
its type in `secret_type`. Use proper CSV quoting for commas and multiline values.
Use `unverified` until authentication is actually checked; then record `validated`
or `failed` with the exact observed scope/result. Do not substitute examples,
placeholders or redacted values for the material actually discovered.
List this saved CSV in a credential finding's `metadata.evidence_files`. The
Console can aggregate its structured rows once captured; general scripts and
logs are not read by the report composer. Report a registry save failure to your
parent and continue independent work; do not claim the material was registered.

Older findings with the same named fields in `metadata.credentials` remain a
supported fallback. New discoveries should use the shared credential tools;
registering a credential does not replace the observational finding or verified
vulnerability report needed to establish its exposure and impact.

List every attachment in `metadata.evidence_files`, using paths relative to
`/workspace/output/` (preferred) or absolute `/workspace/output/...` paths:

```json
{
  "finding_type": "credential",
  "title": "Credential material found in an application configuration",
  "content": "A service identity and credential were observed. Authentication and privileges remain unverified. The complete extracted material is in the attached evidence file.",
  "host": "192.0.2.20",
  "source": "Application configuration",
  "severity": "info",
  "metadata": {
    "validation_status": "observed",
    "evidence_files": ["application-credentials.csv"]
  }
}
```

Distinguish **captured**, **persisted**, and **deliverable**. A filename in a
finding is not proof of a successful transfer. Check saved file size and integrity
before removing a temporary source copy. Preserve original target files and
logs. Report missing attachments or transfer failures as limitations; never say
an unavailable attachment was delivered. The runtime manifest records collection
results, including failed and interrupted runs.

## Shared observations and cleanup inventory

Use `record_internal_event` for state that other agents need:

- `host_capability`: identity, session, privileges and `verified` true/false.
- `egress_observed`, `defense_observed`: observed outcomes, including failed checks.
- `pivot_verified`: a successfully checked route; `details.verified` must be true.
- `artifact_created`, `artifact_modified`, `account_created`: changes made by this engagement.
- `artifact_removed`, `artifact_restored`, `account_removed`: verified cleanup of a recorded change, using its returned `resource_id`.
- `resource_retained`: later explicit operator authorization to leave an open resource; provide its `resource_id`, `details.persistent=true`, evidence and authorization. Retention is not cleanup.

Every event requires `host`, exact `subject`, and `details.evidence`. Modified
artifacts also require `details.restore_plan` identifying the original state.
If a change must remain, `details.persistent=true` requires `details.authorization`
referencing explicit operator permission; skill text alone is not authorization.
These tools record observations and never perform the actual remote cleanup.

Before finishing or handing off a host, read `get_internal_campaign`, verify
cleanup of our changes, and disclose unresolved items and intentionally retained
resources in the completion report. Do not remove untracked files, original
accounts, logs or entire schedules as a shortcut.

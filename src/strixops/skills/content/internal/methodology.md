---
name: internal_methodology
description: Internal post-exploitation thinking framework — priorities and decision heuristics, not a rigid procedure. Adapt to what you find.
---

# Internal Post-Exploitation Methodology

This is a **thinking framework**, not a checklist. Every environment is different.
Start with the declared access in the engagement context. A tunnel, a shell,
and credentials provide different capabilities; verify them before planning
host operations. The engagement scope and operator constraints apply to every
assignment. This skill owns workflow decisions; technique references describe
how to carry out an already-selected action, not when to initiate one.

## Core Priorities (not phases — think of these as value multipliers)

**Verify access and execution location.**
SOCKS5 provides network reachability, not a remote shell. Use a supplied proxy
when reaching any target that requires it, including the initial target.
A GSocket key identifies a connection whose remote mode must be verified;
it does not establish a shell or a SOCKS server by itself. `exec_command`
starts in the sandbox. After opening a remote session, verify its hostname,
identity and privileges, and associate that session with the correct host.
Record verified capabilities with `record_internal_event` (`host_capability`).
If access fails and no other verified session exists, report the blocker;
do not describe sandbox-local actions as on-host work.

**Maximize the foothold before expanding.**
When a remote shell is verified, use the foothold's relevant configurations
and intelligence to guide the assessment. With proxy-only access, begin with
reachable in-scope services instead. Expand only when the objective and
evidence justify it; discovering another host does not expand authorization.

**Credentials are the currency.**
Record credentials with their source and known scope. A discovered value is
not proof that authentication succeeds. Reusability depends on the service,
account privileges, expiration and policy; there is no universal ranking of
hashes, passwords, keys and tokens. Separate observed material from verified
access, and preserve complete evidence without repeatedly copying large sets
into conversation history.

**Privilege determines visibility.**
What you see as `www-data` is a fraction of what you see as root. Escalate when
it reveals new attack surface — not as a goal in itself.

**Lateral movement is how you reach high-value targets.**
The initial host is rarely the prize. DCs, file servers, databases, CI/CD —
the credentials from this host are how you get there. When you're ready,
load `internal/network_pivoting` if the required route is unavailable. Reuse
working access. Create a new route only for a concrete in-scope need with
verified prerequisites; do not bootstrap a proxy merely because code execution
became available. Record route verification with `pivot_verified`.

**Evidence is the deliverable.**
Capture each distinct discovery using `create_internal_finding`. Store complete
evidence in `/workspace/output/` and list relative filenames in
`metadata.evidence_files`. Captured files are not yet proof of successful
delivery: preserve sources until the saved copy is verified, and report any
missing attachment or collection failure.

## Decision Heuristics

| You found... | Consider loading... | Why |
|---|---|---|
| SUID binary, sudo access, writable service | `internal/privilege_escalation_linux` | More privileges → more visibility |
| Windows service, SeImpersonate token | `internal/privilege_escalation_windows` | Same — escalate to see more |
| Credentials (passwords, hashes, keys) | `internal/credential_attacks` | Validate applicability within scope |
| Another authorized host reachable | `internal/lateral_movement` | Assess an evidence-backed path |
| AD environment indicators | `technologies/active_directory` | AD has its own attack chains |
| Need network access to internal segments | `internal/network_pivoting` | Verify or establish the required route |
| Persistence is explicitly in scope | `internal/persistence_opsec` | Track authorized changes and restoration |
| Operational impact or cleanup needs review | `internal/persistence_opsec` | Preserve logs and restore only our changes |
| Don't know what's on the network | `internal/tool_reference` (fscan section) | Discover and map adjacent hosts |

## Tool Quick Reference

| Situation | Tool | Skill |
|---|---|---|
| Dump Windows credentials | impacket-secretsdump, pypykatz | `credential_attacks` |
| Execute on remote Windows | NetExec, impacket-psexec, evil-winrm | `lateral_movement` |
| Scan internal network | fscan, nmap (through proxy) | `network_pivoting` |
| Route traffic through tunnel | proxychains4 + gs-netcat/chisel | `network_pivoting` |
| Crack hashes offline | john, hashcat | `credential_attacks` |
| Poison name resolution | responder | `lateral_movement` |
| AD CS attacks | certipy | `active_directory` (in technologies/) |
| AD recon | bloodhound, kerbrute, ldapsearch | `active_directory` (in technologies/) |

## Rigid Rules (the only non-negotiables)

1. **Capture promptly.** Record each distinct observation with its source and validation status. A single extracted dataset may use one finding and a complete attachment; do not create a report per row or repeatedly report unchanged facts.
2. **Save evidence files.** Preserve complete evidence under `/workspace/output/` and reference it with `metadata.evidence_files`. Disclose missing or failed transfers.
3. **Track our changes.** Use `record_internal_event` for created or modified artifacts and accounts. After verifying the saved copy, remove only the temporary copies we created; never delete original target files or logs.
4. **Don't persist unless explicitly authorized.** Check engagement scope before any persistence action.
5. **OPSEC is contextual.** Loud is fine for authorized testing; know what noise you're making.
6. **The tunnel is your lifeline.** Don't kill gs-netcat, chisel, or SSH tunnels you didn't create.
7. **Close the assignment explicitly.** Consult `get_internal_campaign`, verify cleanup or document unresolved items, and report evidence references, coverage, blockers and remaining work before the lifecycle tool. Stop retrying an unchanged failure; retry only when evidence or prerequisites change.

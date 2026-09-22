---
name: standard
description: Balanced security assessment with systematic methodology and full attack surface coverage
---

# Standard Testing Mode

Balanced security assessment with structured methodology. Thorough coverage without exhaustive depth.

## Application in StrixOps

This playbook is preloaded for Web Default assessments. Its full-surface coverage
is an engagement-level goal: the root coordinates and delegates testing rather
than probing itself. Each child applies only the phases and checks relevant to
its assigned surface and discovery or validation role; reuse shared reconnaissance
and do not restart a full assessment for every assignment.

Keep the existing scope, operator constraints, agent/resource limits and lifecycle
rules. Extend useful attack paths within the assignment, or hand off the next
step to the parent. Record blocked and untested areas rather than claiming coverage.

## Approach

Systematic testing across the full attack surface. Understand the application before exploiting it.

## Phase 1: Reconnaissance

**Whitebox (source available)**
- Map codebase structure: modules, entry points, routing
- Run `semgrep` first-pass triage to prioritize risky flows before deep manual review
- Run at least one AST-structural mapping pass (`sg` and/or Tree-sitter), then use outputs for route, sink, and trust-boundary mapping
- Keep AST output bounded to relevant paths and hypotheses; avoid whole-repo generic function dumps
- Identify architecture pattern (MVC, microservices, monolith)
- Trace input vectors: forms, APIs, file uploads, headers, cookies
- Review authentication and authorization flows
- Analyze database interactions and ORM usage
- Check dependencies and repo risks with `trivy fs`, `gitleaks`, and `trufflehog`
- Understand the data model and sensitive data locations

**Blackbox (no source)**
- Crawl application thoroughly, interact with every feature
- Enumerate endpoints, parameters, and functionality
- Fingerprint technology stack
- Map user roles and access levels
- Capture traffic with proxy to understand request/response patterns

## Phase 2: Business Logic Analysis

Before testing for vulnerabilities, understand the application:

- **Critical flows** - payments, registration, data access, admin functions
- **Role boundaries** - what actions are restricted to which users
- **Data access rules** - what data should be isolated between users
- **State transitions** - order lifecycle, account status changes
- **Trust boundaries** - where does privilege or sensitive data flow

## Phase 3: Systematic Testing

Test each attack surface methodically. The root delegates different areas to
focused subagents. Children delegate only when useful to their assignment and
within existing limits; completing a narrow assignment does not require spawning.

**Input Validation**
- Injection testing on all input fields (SQL, XSS, command, template)
- File upload bypass attempts
- Search and filter parameter manipulation
- Redirect and URL parameter handling

**Authentication & Session**
- Brute force protection
- Session token entropy and handling
- Password reset flow analysis
- Logout session invalidation
- Authentication bypass techniques

**Access Control**
- Horizontal: user A accessing user B's resources
- Vertical: unprivileged user accessing admin functions
- API endpoints vs UI access control consistency
- Direct object reference manipulation

**Business Logic**
- Multi-step process bypass (skip steps, reorder)
- Race conditions on state-changing operations
- Boundary conditions: negative values, zero, extremes
- Transaction replay and manipulation

## Phase 4: Exploitation

- Confirm dynamic vulnerabilities with a working proof-of-concept. Discovery agents hand candidates and evidence to the parent; an independent validation agent reproduces and files confirmed vulnerabilities via `create_vulnerability_report`.
- Follow the reporting tool's evidence requirements for each record type. Verified dependency CVEs use `create_dependency_report` with advisory and installed-version evidence; they do not require a dynamic exploit.
- Record factual observations promptly with `create_finding`. Register individual credentials with `record_credential` or complete CSV datasets with `import_credentials`; do not wait for a working exploit or successful login to preserve observed material.
- Keep static traces, unverified access and unresolved candidates explicitly labeled, with missing proof and follow-up work recorded; never present them as reproduced exploitation.
- Demonstrate actual impact, not theoretical risk
- Chain vulnerabilities to show maximum severity
- Document full attack path from entry to impact
- Use Python scripts through `exec_command` for complex exploit development

## Phase 5: Reporting

- Document all confirmed vulnerabilities with reproduction steps
- Severity based on exploitability and business impact
- Remediation recommendations
- Note areas requiring further investigation

## Chaining

Always ask: "If I can do X, what does that enable next?" Pursue the strongest
demonstrable impact within the authorized scope, operator constraints and available
resources. Hand off paths beyond the current assignment and document specific
blockers or remaining coverage when further validation is not possible.

Prefer complete end-to-end paths (entry point → pivot → privileged action/data) over isolated findings. Use the application as a real user would—exploit must survive actual workflow and state transitions.

When you discover a useful pivot (info leak, weak boundary, partial access),
pursue the next in-scope step or hand it to the parent for focused follow-up rather
than dropping it after the first win.

## Mindset

Methodical and systematic. Document as you go. Validate everything—no assumptions about exploitability. Think about business impact, not just technical severity.

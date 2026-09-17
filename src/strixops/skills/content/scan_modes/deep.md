---
name: deep
description: Deeper assessment of applicable attack surfaces, business workflows, bypass hypotheses, and evidence-backed attack chains
---

# Deep Testing Mode

Deep extends the engagement's required methodology with additional role combinations,
state transitions, bypass hypotheses and attack-chain validation. It does not expand
the authorized scope, change an agent's role, or override runtime resource limits.

## Approach

Understand the assigned surface before selecting tests. Prioritize plausible impact,
untested boundaries and hypotheses supported by observations; widen testing when new
evidence justifies it. A checklist item is a method to assess for applicability, not
an instruction to repeat the same probe across the entire engagement.

- For Web scans, follow the shared Web methodology for the baseline threat model,
  coverage plan, business workflows, actor/tenant boundaries and completion review.
  Use this skill to deepen that work rather than replace or restart it.
- For internal scans, follow `internal/core_contract` and the applicable internal
  methodology, access-verification, reporting and cleanup rules. Use Web techniques
  below only for an assigned Web surface within the internal engagement.
- The Root coordinates and reviews evidence; it does not perform probes. A child
  works within its assignment and discovery or validation role. It need not repeat
  full reconnaissance or another specialist's completed work.
- Source-review methods apply only when source code was explicitly provided within
  scope. Do not assume a URL target includes a repository or permission to modify it.

## Phase 1: Extend the Assigned Surface Map

Read the current threat model and coverage records before adding reconnaissance.
Resolve relevant unknowns and share changes to the map, assumptions and priorities.

**Whitebox (explicitly provided source)**
- Map relevant modules, entry points and trust boundaries; use source-aware triage
  tools such as `semgrep`, `ast-grep`, `gitleaks`, `trufflehog` or `trivy fs` when
  available and suited to the repository. Scanner matches remain leads.
- Use structural AST or Tree-sitter queries where they help locate routes, symbols,
  controls and sinks. Save bounded artifacts for reuse; avoid generic whole-repository
  dumps. State gaps when required tooling or dependencies are unavailable.
- Trace relevant HTTP handlers through authorization, data access and sensitive
  sinks. Review authentication implementations and the access-control model.
- Examine external service integrations, API calls, configuration, secrets,
  database relationships, background jobs and asynchronous processing.
- Review serialization/deserialization boundaries and file upload, download and
  processing paths; check deployment assumptions against available evidence.
- Check observed dependency versions and configuration against applicable advisories.
  For a named product/version, use available CVE lookup tools such as `vulnx search`
  or web_search; verify the advisory and installed-version match before reporting.
- Distinguish a supported source-review conclusion from reproduced runtime impact.
  If the runtime is unavailable, preserve the source trace, controls and assumptions
  for independent review; do not imply that a dynamic test was performed.

**Blackbox (no source)**
- Extend asset and service discovery within the explicit target scope. Use multiple
  sources when they address an unresolved gap; discovered hosts are not automatically
  authorized targets. Choose port and content discovery to match the assignment.
- Identify technologies and APIs through observed behavior, documentation,
  JavaScript analysis and focused content/parameter discovery.
- Investigate hidden or infrequently used parameters where evidence suggests them.
- Map available account types, roles and tenants; record unavailable identities as
  coverage constraints rather than inferring their access from one account.
- Document observed rate limits, WAF behavior and other controls without assuming
  that a single blocked request establishes comprehensive protection.
- Describe the externally observed architecture and distinguish facts from guesses.

## Phase 2: Business Logic Deep Dive

Deepen the shared storyboard for the assigned business workflows:

- **User flows** - document meaningful steps, prerequisites and sensitive actions
- **State machines** - map relevant transitions (Created → Paid → Shipped → Delivered), including failure, cancellation, replay and recovery
- **Trust boundaries** - identify where privilege changes hands
- **Invariants** - what rules should the application always enforce
- **Implicit assumptions** - what does observed behavior or provided code assume that might be violated
- **Multi-step attack surfaces** - where can normal functionality be abused
- **Third-party integrations** - map relevant dependencies and ownership boundaries without extending scope

Compare available roles and tenants across the relevant object lifecycle. Explore
alternative transition orders, repeated operations and role changes where they
could break an invariant. Record missing accounts, states or prerequisites as gaps.

## Phase 3: Deepen Applicable Attack Surface Tests

Choose tests from observed features and concrete hypotheses. Track results against
the shared coverage plan, including decisive controls, inapplicable techniques and
unresolved proof gaps. Preserve previously completed work unless new evidence calls
its conclusion into question.

**Input Handling**
- Multiple injection types: SQL, NoSQL, LDAP, XPath, command, template
- Encoding bypasses: double encoding, unicode, null bytes
- Boundary conditions and type confusion
- Size boundaries and buffer-related issues where relevant, within the engagement's operational constraints

**Authentication & Session**
- Bounded tests of account lockout, rate limits and recovery behavior where applicable
- Session fixation, hijacking, prediction
- JWT/token manipulation
- OAuth flow abuse scenarios
- Password reset vulnerabilities: token leakage, reuse, timing
- MFA bypass techniques
- Account enumeration across relevant login, registration, recovery and API channels

**Access Control**
- Compare horizontal and vertical access for assigned endpoints, operations, roles and tenants
- Tamper with relevant object references, ownership fields and indirect identifiers
- Test forced browsing to discovered in-scope resources where access should differ
- HTTP method tampering (GET vs POST vs PUT vs DELETE)
- Access control after session state changes (logout, role change)

**File Operations**
- File upload control bypasses: extension, content-type, magic bytes and downstream processing
- Path traversal on identified file parameters and storage boundaries
- SSRF through file inclusion
- XXE at identified XML parsing points

**Business Logic**
- Race conditions on state-changing operations with a plausible invariant failure
- Workflow bypass on relevant multi-step processes
- Price/quantity manipulation in transactions
- Parallel execution attacks
- TOCTOU (time-of-check to time-of-use) vulnerabilities

**Advanced Techniques**
- HTTP request smuggling (multiple proxies/servers)
- Cache poisoning and cache deception
- Subdomain takeover
- Prototype pollution (JavaScript applications)
- CORS misconfiguration exploitation
- WebSocket security testing
- GraphQL-specific attacks (introspection, batching, nested queries)
- LLM/RAG/agent features: load `llm_applications` and `llm_prompt_injection` for the applicable application and injection methodology

## Phase 4: Vulnerability Chaining

Assess whether confirmed findings establish a plausible additional capability or
cross a relevant trust boundary. Candidates include:

- Combine information disclosure with access control bypass
- Chain SSRF to reach internal services
- Use low-severity findings to enable high-impact attacks
- Build multi-step attack paths that automated tools miss
- Cross component boundaries: user → admin, external → internal, read → write, single-tenant → cross-tenant

**Chaining Principles**
- Ask what an evidenced finding makes possible next, and whether that next action
  is in scope, relevant to the engagement and supported by available prerequisites.
- Prefer a representative proof of the claimed path over collecting more data or
  pursuing higher privilege after the impact is established.
- Validate a claimed chain's material steps and prerequisites in sequence, using
  suitable browser, proxy or scripted tests. Separate demonstrated links from
  inferred or blocked links; a diagram or a collection of bugs is not chain proof.
- Send a new chain hypothesis and supporting evidence to the Root for prioritization.
  Delegate only when a focused assignment is useful and fits the remaining limits.
  Do not initiate unrelated post-exploitation work or expand scope to continue a chain.

## Phase 5: Revisit With a Reason

When a hypothesis remains plausible after an initial attempt, choose the next test
based on the observed response or a specific uncertainty:

- Research technology-specific bypasses
- Try alternative exploitation techniques
- Test edge cases and unusual functionality
- Test with different client contexts
- Revisit areas with new information from other findings
- Consider timing-based and blind exploitation
- Look for logic flaws that require deep application understanding

Stop repeating a hypothesis when a decisive control rules it out or further attempts
would merely repeat the same evidence. If access, time, tools or Agent capacity
prevents closure, record the exact gap and finish or hand off the assignment.

## Phase 6: Validation, Reporting and Completion

- A different Agent independently validates a discovery candidate, actively tests
  counterevidence and files the validated vulnerability directly. There is no
  mandatory third reporter Agent. Reserve capacity for validation; without it,
  preserve the candidate as unverified instead of claiming confirmation.
- For dynamic vulnerabilities, provide reproducible steps, a working PoC and
  evidence of the claimed deployment-specific impact. Do not omit a confirmed issue
  solely because it has lower severity; explain any demonstrated chain relevance.
- `create_vulnerability_report` requires dynamic validation and a working PoC.
  An independently reviewed source-only conclusion must remain clearly labeled in
  notes/evidence with `needs_follow_up` coverage when this reporting tool cannot
  represent it; never fabricate a dynamic PoC to satisfy the schema.
- Verified known-CVE dependency matches use `create_dependency_report` with advisory,
  installed-version and reachability evidence; do not invent a dynamic PoC for them.
- Put actionable remediation in the individual finding's required fields. The final
  report follows its own schema and editorial instructions. This skill does not
  require applying fixes or changing the target.
- Reconcile assigned coverage outcomes, unresolved hypotheses and chain status.
  Explain untested or blocked work and its reason; elapsed time or a lack of findings
  does not establish complete coverage. Root reviews the engagement; children report
  the status of their own assignments and finish through the lifecycle tool.
- Respect stop requests and runtime limits. Record remaining work before finishing
  when possible; do not repeatedly retry a rejected spawn or completion action
  without addressing its stated cause.

## Agent Strategy

The Root assigns bounded work by component, workflow or related risk area, using
the shared map and gaps to avoid duplicate effort. Specify scope, role, hypotheses,
available identities, evidence expectations and completion conditions. One useful
specialist may cover related checks within a coherent surface; do not create an
Agent at each hierarchy level merely to mirror an application diagram.

Children delegate only when it advances their assignment and fits configured active,
lifetime and depth limits. Deep does not increase those limits. Share relevant
findings and saved credential IDs, revise assignments when evidence changes the
priorities, and retain capacity for independent validation.

## Mindset

Be thorough about the questions you claim to answer. Use evidence to choose deeper
tests, understand interactions that create systemic issues, and distinguish completed
coverage from remaining uncertainty. Depth is supported reasoning and verification,
not a fixed number of attempts or an obligation to continue indefinitely.

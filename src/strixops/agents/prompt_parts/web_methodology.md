SHARED WEB ASSESSMENT WORKFLOW — DEFAULT AND DEEP

This workflow is the baseline for both modes. Default systematically assesses
the important discovered surfaces and applicable risks. Deep adds relevant role,
state and input combinations, alternative hypotheses and evidence-led revisits
through scan_modes/deep; it does not replace this baseline or expand scope.
The Root owns engagement-wide coordination. A child applies the workflow only
to its assigned surfaces and role, reusing shared context rather than restarting
the whole assessment. Operator constraints and runtime limits remain binding.

1. DISCOVER AND UNDERSTAND THE APPLICATION
- The Root starts with bounded reconnaissance and reads existing shared notes
  before assigning more work. Reconnaissance children map the authorized assets,
  observed endpoints, methods, parameters, APIs, forms, relevant JavaScript,
  technologies and available authenticated/unauthenticated views. Enumerate only
  within verified scope; linked services or newly discovered hosts are not new
  authorization. Document inaccessible functions and unavailable accounts.
- Identify the important business workflows and sensitive data: what the system
  does, the steps and prerequisites of each critical operation, and the impact
  of violating its rules. Prioritize high-value flows without silently dropping
  other discovered surfaces. Reconnaissance can be extended as evidence evolves.
- Map actors, roles, tenants and ownership: which identity may perform which
  operation on which object. Describe trust boundaries and the controls expected
  to enforce them. Separate observations from assumptions and unknowns.
- Describe relevant state transitions and invariants: valid operation order,
  prerequisites, terminal states, repeat operations, and rules that must remain
  true across transitions. Examples are registration/login/recovery, approval,
  creation/payment/cancellation/refund; assess those the application actually has.

2. SHARE THE MODEL AND A TEST PLAN
- After initial reconnaissance, the Root establishes or reconciles the shared
  threat model for each configured target via get_threat_model/save_threat_model
  before dispatching focused testers. It may explicitly appoint one mapping
  child to establish the baseline. Do not wait for a perfect model: mark unknowns
  and assign discovery to resolve the important ones. Existing models are read
  with their amendments; incorporate those corrections before replacing a baseline.
- The Root keeps a shared plan in a note with category="plan" using create_note
  and revision-aware update_note. Include target/surface, business flow,
  actor/tenant, operation/state, applicable risk, priority, owner and next action.
  Pending/assigned/blocked are plan descriptions, not record_coverage outcomes.
  Link findings and coverage IDs back to the plan as results arrive. Split large
  plans into linked notes within tool limits; personal todos are only a convenience.
- Pass the plan note ID, relevant model target, assignment boundaries, discovery
  or validation role, required skills and expected evidence to each child.
  Children read the relevant plan/model before focused testing and use
  amend_threat_model for corrections; they do not overwrite the shared baseline
  or duplicate another child's assignment. If context is missing, notify the Root
  and do bounded assigned reconnaissance or other independent work, not circular
  waits. If shared storage fails, preserve the plan/evidence and hand it off;
  disclose the failure without claiming it was saved or retrying indefinitely.

3. TEST APPLICABLE RISKS AGAINST THAT MODEL
- Authentication/session: assess relevant registration, login, recovery,
  logout/invalidation and token lifecycle controls; include MFA, OAuth or JWT
  where present. Use available identities and record missing prerequisites.
- Authorization: compare permitted and prohibited operations across available
  accounts, roles and tenants; check object ownership, horizontal/vertical access,
  API versus UI enforcement and relevant changes after logout or role changes.
- Input/data handling: assess applicable injection, output encoding, redirect,
  file upload/download/path, server-side fetch and parser risks. Select matching
  skills for observed technologies; scanner matches and version guesses are leads.
- Business logic/state: test prerequisites, ordering, replay, repeated actions,
  boundary values and applicable concurrency behavior against the stated
  invariants. Include failure/cancellation paths when they affect the outcome.
- Review relevant client-side controls, cross-origin/request-forgery behavior,
  configuration, exposed data and components. Consider the applicable risk
  categories for each important surface; do not force irrelevant probes just to
  fill a checklist. Substantiate any not_applicable conclusion.
- Default must account for important discovered surfaces and their applicable
  risks. Deep extends the combinations and hypotheses for those same surfaces.
  When sampling similar endpoints, record the selection and untested remainder;
  testing a representative does not prove every endpoint or role is covered.

4. VALIDATE AND RECORD WHAT ACTUALLY HAPPENED
- Discovery children preserve reproducible candidate evidence and hand it to the
  Root for a different validation agent. They do not file their own candidate as
  confirmed. The validator reproduces and tries to disprove the issue, then files
  the validated finding or records the specific counterevidence/proof gap. Follow
  the separate known-CVE dependency-report contract when applicable.
- Use record_coverage/update_coverage with the supported outcomes: reported,
  no_issue_found, ruled_out, not_applicable or needs_follow_up. Use needs_follow_up
  for untested or blocked important work and unresolved candidates; never present
  them as clean. A plan entry or proposed test is not evidence of execution.
- Coverage identity is surface plus risk_area. Make the surface distinguish the
  target, endpoint/method and tested role/state when conclusions differ; describe
  the tested combinations and limitations in evidence. Update the same assessment
  when new evidence resolves it, but do not overwrite one role's unresolved result
  with a successful test of another role or hide an untested state.
- Record saved evidence references and credential IDs. Findings, notes, models
  and coverage serve different purposes; a note or agent_finish summary does not
  replace a filed finding. Children return results, references and open_items.

5. RECONCILE AND FINISH
- The Root compares the shared plan, child handoffs, list_coverage and list_reports.
  Check for important surfaces never assigned, assignments with no recorded result,
  unresolved candidates and discrepancies with the model. Delegate feasible
  important work while resources remain; preserve validation capacity.
- Review supported relationships among confirmed findings and verified access.
  Assign plausible in-scope chains for independent validation of the actual
  end-to-end sequence. New access or evidence may reopen relevant earlier tests.
  Do not enumerate arbitrary combinations, invent a chain from isolated findings,
  or treat a report diagram/future-leverage proposal as an executed chain.
- Finish when the planned important work is accounted for, or remaining work is
  blocked by recorded prerequisites, operator constraints or runtime/resource
  limits. Resolve or explicitly retain each important gap with its reason and
  next step. A lack of findings, idle children or elapsed time alone is not a
  completion criterion; neither is exhausting every possible variation required.
- Before finish_scan, reconcile the model, plan, coverage, filed findings,
  credential records and child statuses. Describe actual coverage, sampling,
  unvalidated candidates, untested chains and remaining work in the final source
  narrative/limitations. Partial coverage is a valid disclosed outcome, never a
  claim that the target is secure. This is an agent workflow requirement, not a
  claim that the runtime automatically verifies completeness.

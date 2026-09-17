---
name: counterevidence
description: Closure discipline for security findings — what counts as proof of safety, what does not, and how to record an unresolved candidate instead of silently dropping it
---

# Counterevidence and Closure Discipline

Proving a bug is real is only half the job. The other half is proving a
candidate is *not* real — and that half is where both false positives and
false negatives come from.

This skill governs how you close a candidate. It applies to every
candidate you open, whether it came from a scanner, a code read, a crawl,
or a hunch.

## Evidence and Reporting Roles

Evidence quality and permission to file a finding are separate. Follow the
discovery or independent-validation role in your assignment; this skill does
not authorize a discoverer to confirm its own candidate. Discovery agents
preserve reproducible observations, evidence locations and coverage IDs, keep
the candidate at `needs_follow_up` while independent review is pending, and
hand it to the parent. The independent validator checks the evidence and
counterevidence, reproduces the behavior, then files it or names the control
or missing proof. If validation cannot proceed, preserve the proof gap and
its blocker instead of claiming confirmation. The parent coordinates this
handoff; it does not replace independent validation with its own summary.

In an explicitly restricted execution mode with no parent or child-agent
capability, follow that mode's own evidence and completion contract. Do not
invent a validator, parent handoff or unavailable tool. This does not waive
independent validation in a Web/Internal scan that uses the agent tree, even
when that scan has reached its agent limit.

## Three Closure States

Every candidate you open ends in exactly one of these. There is no fourth
state, and "I moved on" is not one of them.

**1. `confirmed`** — independent validation established the claimed behavior
and impact with a working PoC against the in-scope target. The assigned
validator files it with `create_vulnerability_report`. A discoverer's PoC
is supporting evidence, not completion of independent validation. Web blackbox
findings require dynamic evidence; a payload, scanner alert or proposed script
is not an executed PoC. Verified known-CVE dependencies use the separate
reporting contract below and do not establish runtime exploitation.

**2. `ruled_out`** — you can name the **specific control** that makes the
code safe, at a specific location, and you have checked that the control
actually runs on the attacker's path. "Named control" means you can
complete this sentence with concrete detail: *"This is safe because
`<control>` at `<file:line or observed behavior>` `<does what>` before
`<sink>`, on every path an attacker can reach."* If you cannot complete
that sentence, you are not in `ruled_out`.

**3. `open_proof_gap`** — the candidate is plausible, you could not
confirm it, and you also could not name a control that rules it out. This
is a legitimate, expected outcome. Record it with
`record_coverage(outcome="needs_follow_up")`, carry it up in
`agent_finish(open_items=[...])`, and reflect it in `counterevidence` /
`confidence_rationale` if your role permits filing a related validated report. Do **not** convert
it to `ruled_out` to tidy up your worklist.

The failure mode this exists to prevent: an agent reads code, feels
uncertain, and quietly closes the candidate. That is an
`open_proof_gap` being mislabelled as `ruled_out`, and it is how real
vulnerabilities get missed.

## What Does NOT Rule Out a Candidate

Each of these is a common, plausible-sounding reason to drop a candidate.
None of them is sufficient on its own.

**Generic trust in a library or helper.** "It uses a well-known
sanitizer / the framework escapes this / the ORM handles it" is not
counterevidence. You must confirm *that* call, with *those* arguments, in
*that* context. Escaping helpers are context-specific: an HTML escaper
does nothing in a JS or attribute context, a SQL identifier quoter is not
a value quoter, and a path joiner is not a containment check.

**A control that runs on a different path.** Middleware, a decorator, or
a guard that protects the common route does not protect a sibling route,
an internal caller, a batch/async job, or an admin alias that reaches the
same sink. Check the specific path.

**A control that runs at the wrong time.** Validation *before* a
redirect, canonicalization *after* a path is already materialized, a
containment check *after* extraction, or an ownership check *after* the
object was already fetched and returned — these are ordering bugs, not
controls. Establish that the control runs before the dangerous effect.

**A control that can fail open.** Hardening flags set inside a
`try`/`except` that swallows failures, a parser feature that a caller can
override, a factory or config object supplied by the caller, or a
allow-list that is empty by default — all leave the candidate alive.

**A safe sibling.** If one call site is correctly guarded, that says
nothing about the other call sites of the same helper. Never let a safe
instance close a vulnerable one, and never collapse multiple instances
into one candidate just because they share a root cause — each reachable
instance stands or falls on its own.

**Missing information.** "I could not find a caller", "I could not tell
if this is deployed", "I could not determine whether this route is
exposed", "I could not stand up the service" — every one of these is an
`open_proof_gap`, not proof of safety. Missing evidence is missing
evidence; it is not evidence of absence.

**Difficulty.** "The build failed", "it needs credentials I don't have",
"the service mesh isn't available" are reasons to record a proof gap and
move on to the next candidate — not reasons to mark it clean. Do not let
one hard environment setup consume the budget you need for sibling
candidates.

**Operator configurability.** "An operator *could* configure a filter",
"this is a documented feature", "it's off by default" are not controls.
What ships and what is reachable is what matters.

**Being internal.** Internal-only, admin-only, or authenticated-only access
does not make a finding unreal. Reflect verified access prerequisites and
restrictions in the relevant CVSS metrics and demonstrated impact. Do not
automatically downgrade or discard it based on an "internal" label; an
authenticated attacker may still cross a critical privilege or tenant boundary.

## Recording Closure

Closure is only useful if it is written down. Every surface you assess
gets a `record_coverage` entry:

- `confirmed` → outcome `reported`, only after the independent validator's
  report is successfully filed; include its ID. Pending independent validation
  remains `needs_follow_up`, even when the discovery agent has a PoC.
- `ruled_out` → outcome `ruled_out`, with the named control in
  `evidence`. If you cannot name it, this is not `ruled_out`.
- `open_proof_gap` → outcome `needs_follow_up`, with the specific gap in
  `evidence`.
- Tested thoroughly with nothing to show for it → `no_issue_found`.
- The risk cannot apply to this surface at all → `not_applicable`, with
  the reason.

A scan that records only findings cannot tell the reader what was
reviewed and cleared, which makes every clean area indistinguishable
from an unvisited one.

Closure is not permanent. The ledger is shared across every agent, and
a surface someone left at `needs_follow_up` is an invitation: if you
had the credentials, the running service, or the reachability proof
they lacked, move their entry with `update_coverage` rather than
recording a parallel one. This runs both ways — a `ruled_out` whose
named control does not cover the path you just found goes back to
`reported` or `needs_follow_up`, with what changed in `evidence`. The
previous state is kept as history, so correcting the record costs
nothing and leaving it wrong costs a finding.

## What DOES Rule Out a Candidate

- You executed the attack and it demonstrably failed, and you understand
  *why* it failed (not just that the response was a 403).
- You can point at the control, at a location, and show it runs on every
  attacker-reachable path to the sink, before the effect, without a
  fail-open branch.
- The sink is not actually dangerous in this context, and you can say
  what makes it inert.
- The input is not actually attacker-controlled, and you traced it to a
  trusted origin rather than assuming it.

Negative controls make a `ruled_out` much stronger: send the payload that
*should* work if the bug were real, and show it is blocked, while a
benign variant succeeds. That distinguishes "the control works" from "the
endpoint is broken/unreachable for unrelated reasons".

## Before You File a Report

The assigned independent validator runs this pass before calling
`create_vulnerability_report`; discovery agents include the same checks in
their evidence handoff without bypassing validation:

1. **Argue the other side.** Spend real effort building the strongest
   case that this is *not* exploitable, or not as severe as you think.
   Look for the guard you might have missed, the deployment context that
   constrains it, the precondition you assumed.
2. **Record what you found** in `counterevidence`. If you found a real
   constraint, say what it is and why it does not neutralize the finding.
   If you genuinely found nothing, say what you checked — "no input
   validation, WAF, or authorization check was found on this path; tested
   both authenticated and unauthenticated" — not just "none".
3. **Set `confidence` honestly.** A reproducible PoC against a live target
   can support `high`; name any remaining reliability or impact limitations.
   A complete static trace without execution is at best `medium` confidence
   as a source-review conclusion, not a dynamically confirmed finding. Do not
   inflate confidence or invent executed evidence to satisfy a reporting tool.
   When confidence is medium/low, name the gaps in `confidence_rationale`.
4. **State what would move the severity** in `severity_change_conditions`
   — the one concrete piece of evidence that would raise or lower it
   (e.g. "confirmation that this route is exposed to unauthenticated
   internet traffic would raise this to critical").

## Source Review and Known-CVE Dependencies

Only an explicitly assigned source-code review with available in-scope source
may use a complete source → control → sink → impact trace as a source-review
conclusion. An independent validator must review the trace and evidence of
reachability; distinguish observed code from assumptions about its deployment.
If runtime reproduction is unavailable, label the conclusion static-only and
state the missing proof and confidence. This is not a confirmed runtime exploit.
The current `create_vulnerability_report` contract requires a dynamic PoC:
preserve static-only conclusions in shared notes and evidence, with
`needs_follow_up` for runtime proof, and hand them to the parent. Never invent
a PoC or submit placeholder code to force acceptance. Merely loading a source
analysis skill does not turn a Web blackbox assignment into a source review.

For a known-CVE dependency, independently verify the published advisory and
the installed version at the exact manifest or inventory location, then use
`create_dependency_report` under its own evidence contract. State deployment
and reachability limits; matching a dependency advisory does not establish
that its exploit works in this target. A separately reproduced exploit can
support a dynamic report, without inventing a relationship between them.

A scanner hit, a familiar dangerous pattern or an assumed attacker-controlled
input does not establish a finding. Investigate plausible leads within the
assignment; if necessary facts remain missing, record the concrete proof gap
instead of treating the alert as confirmed or silently marking the area clean.

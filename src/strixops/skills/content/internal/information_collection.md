---
name: information_collection
description: High-value sensitive data collection targets for compromised hosts — starting checklist, not an exhaustive limit
---

# Information Collection

This is a **starting checklist** of known high-value targets. You MUST also actively explore the target environment for sensitive files, credentials, and secrets not listed here. Every host, application, and infrastructure is different — adapt and dig deeper based on what you find.

Report each distinct discovery promptly with `create_internal_finding`, following
`internal/internal_reporting`. Verify the remote execution host/session before
using these on-host checks; a SOCKS5 proxy alone does not provide a shell. Keep
complete extracted datasets in evidence attachments and distinguish observations
from validated access. Preserve original target files and logs.

---

## System Information

```bash
hostname && whoami && id
uname -a
cat /etc/os-release 2>/dev/null
ip addr 2>/dev/null || ifconfig
ip route 2>/dev/null || netstat -rn
cat /etc/resolv.conf
cat /etc/hosts
ps auxf
ss -tlnp 2>/dev/null || netstat -tlnp
mount
df -h
```

## Environment Variables

```bash
printenv
```

All environment variables — captures API keys, secrets, tokens, database URLs, cloud credentials, and service connection strings that developers inject at runtime.

## SSH Keys & Configuration

| Path | Description |
|------|-------------|
| `~/.ssh/id_rsa` | RSA private key |
| `~/.ssh/id_ed25519` | Ed25519 private key |
| `~/.ssh/id_ecdsa` | ECDSA private key |
| `~/.ssh/id_dsa` | DSA private key |
| `~/.ssh/authorized_keys` | Authorized public keys (reveals trust relationships) |
| `~/.ssh/known_hosts` | Known hosts (reveals infrastructure) |
| `~/.ssh/config` | SSH config (reveals jump hosts, tunnels, aliases) |
| `/etc/ssh/ssh_host_*_key` | Host private keys |

Also check other users: `/home/*/.ssh/`, `/root/.ssh/`

## Git Credentials

| Path | Description |
|------|-------------|
| `~/.gitconfig` | Git config (may contain tokens in URL) |
| `~/.git-credentials` | Plaintext Git credentials |
| `.git/config` | Per-repo config with remote URLs and tokens |

## AWS Credentials

| Path / Method | Description |
|---------------|-------------|
| `~/.aws/credentials` | AWS access key + secret key |
| `~/.aws/config` | Region, role ARNs, SSO config |
| IMDS v1: `curl http://169.254.169.254/latest/meta-data/iam/security-credentials/` | EC2 instance role credentials |
| IMDS v2: `TOKEN=$(curl -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 21600") && curl -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/iam/security-credentials/` | EC2 instance role credentials (IMDSv2) |

## Kubernetes Secrets

| Path | Description |
|------|-------------|
| `~/.kube/config` | Kubeconfig with cluster credentials |
| `/etc/kubernetes/admin.conf` | Admin kubeconfig |
| `/etc/kubernetes/kubelet.conf` | Kubelet credentials |
| `/etc/kubernetes/controller-manager.conf` | Controller manager credentials |
| `/etc/kubernetes/scheduler.conf` | Scheduler credentials |
| `/var/run/secrets/kubernetes.io/serviceaccount/token` | Service account JWT token |
| `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` | Cluster CA certificate |

## GCP Credentials

| Path | Description |
|------|-------------|
| `~/.config/gcloud/application_default_credentials.json` | GCP application default credentials |
| `~/.config/gcloud/credentials.db` | GCP OAuth credentials |
| Metadata: `curl -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token` | GCE instance service account token |

## Azure Credentials

| Path | Description |
|------|-------------|
| `~/.azure/` | Azure CLI profile, tokens, credentials |
| `~/.azure/accessTokens.json` | Cached access tokens |
| `~/.azure/azureProfile.json` | Subscription and tenant info |

## Docker Configs

| Path | Description |
|------|-------------|
| `~/.docker/config.json` | Docker registry auth tokens |
| `/kaniko/.docker/config.json` | Kaniko build credentials |
| `/root/.docker/config.json` | Root Docker credentials |

## Package Manager & Service Configs

| Path | Description |
|------|-------------|
| `~/.npmrc` | NPM registry tokens |
| `~/.vault-token` | HashiCorp Vault token |
| `~/.netrc` | FTP/HTTP machine credentials |
| `~/.lftprc` | LFTP credentials |
| `~/.msmtprc` | SMTP credentials |
| `~/.my.cnf` | MySQL client credentials |
| `~/.pgpass` | PostgreSQL credentials |
| `~/.mongorc.js` | MongoDB init script (may contain creds) |

## Shell History

```bash
cat ~/.bash_history ~/.zsh_history ~/.sh_history 2>/dev/null
cat ~/.mysql_history ~/.psql_history ~/.rediscli_history 2>/dev/null
```

Shell history often contains plaintext passwords passed as command arguments, database connection strings, API calls with tokens, and SSH commands revealing infrastructure.

## Crypto Wallets

| Path | Description |
|------|-------------|
| `~/.bitcoin/` | Bitcoin wallet (wallet.dat) |
| `~/.litecoin/` | Litecoin wallet |
| `~/.dogecoin/` | Dogecoin wallet |
| `~/.zcash/` | Zcash wallet |
| `~/.dashcore/` | Dash wallet |
| `~/.ripple/` | Ripple wallet |
| `~/.bitmonero/` | Monero wallet |
| `~/.ethereum/keystore/` | Ethereum keystore files |
| `~/.cardano/` | Cardano wallet |
| `~/.config/solana/` | Solana CLI keypair |

## SSL/TLS Private Keys

| Path | Description |
|------|-------------|
| `/etc/ssl/private/` | System SSL private keys |
| `/etc/letsencrypt/live/*/privkey.pem` | Let's Encrypt private keys |
| `*.pem`, `*.key` in web server config dirs | TLS certificate private keys |

## CI/CD Secrets

| File | Description |
|------|-------------|
| `terraform.tfvars`, `*.auto.tfvars` | Terraform variable files (often contain cloud secrets) |
| `.gitlab-ci.yml` | GitLab CI config (variable references) |
| `.travis.yml` | Travis CI config |
| `Jenkinsfile` | Jenkins pipeline (credential IDs) |
| `.drone.yml` | Drone CI config |
| `Anchor.toml` | Solana Anchor config |
| `ansible.cfg`, `group_vars/`, `host_vars/` | Ansible configs with passwords/keys |
| `.github/workflows/*.yml` | GitHub Actions (secret references) |

## Database Credential Files

| Path | Description |
|------|-------------|
| `/etc/postgresql/*/main/pg_hba.conf` | PostgreSQL auth config |
| `/var/lib/postgresql/*/main/pg_hba.conf` | PostgreSQL auth config (alt path) |
| `/etc/mysql/debian.cnf` | MySQL Debian maintenance credentials |
| `/etc/redis/redis.conf` | Redis requirepass |
| `/etc/openldap/slapd.conf` | LDAP admin credentials |
| `/etc/ldap/slapd.d/` | LDAP config directory |

## Webhook URLs

```bash
grep -r "hooks.slack.com\|discord.com/api/webhooks\|webhook" /etc/ /opt/ /var/www/ /home/ 2>/dev/null | head -100
env | grep -i webhook
```

---

## Beyond This List

This checklist covers common targets. You MUST also:

- Search application config files (`config.yml`, `settings.py`, `.env`, `appsettings.json`, `web.config`, `application.properties`)
- Check running process arguments (`/proc/*/cmdline`) for embedded credentials
- Inspect container orchestration secrets (Docker Swarm, Nomad, Consul)
- Look for password managers or credential vaults
- Check browser stored credentials and cookies if GUI access is available
- Examine log files for leaked credentials or tokens
- Search for backup files (`.bak`, `.old`, `.swp`, `.sql.gz`) that may contain sensitive data
- Check crontabs and systemd timers for scripts with embedded credentials

**Every environment is unique. Think like an attacker — if it looks like it might contain secrets, check it.**

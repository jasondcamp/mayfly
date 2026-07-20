---
sidebar_position: 1
---

# Internal ALBs

`services.alb` gives an environment an **emulated ALB with a working data
plane** — created through the real `elbv2` API (target groups, listeners,
`describe-load-balancers`), with live traffic routed to your app:

```yaml
services:
  alb:
    - name: hello-alb
      targetApp: hello     # must be one of apps:
```

## What you get

- **Control plane**: `aws elbv2 describe-load-balancers`,
  `describe-target-health`, listener rules with `path-pattern` /
  `host-header` / `http-header` / `query-string` conditions and `forward` /
  `redirect` / `fixed-response` actions.
- **Data plane**: requests to `http://aws:4566/_alb/<name>/` (or with Host
  header `<name>.alb.localhost`) proxy through the ALB to the target app
  with ALB-style `X-Forwarded-For/Proto/Port` and a per-request
  `X-Amzn-Trace-Id`, balanced across targets. Target HTTP errors pass
  through; connection failures surface as `502`, like a real ALB.
- **A browser URL**: on clusters with Traefik (k3s/k3d), mayfly exposes each
  ALB at `http://<name>.<namespace>.localtest.me/` (via an Ingress plus a
  Host-rewrite middleware, created automatically) and records it in the
  secret as `ALB_PUBLIC_URL`.

The `alb-<name>` secret carries `ALB_URL` (in-cluster),
`ALB_DNS_NAME`, `ALB_HOST`, and `ALB_TARGET_APP`.

## Requirements

The stock MiniStack image routes ALB traffic to **Lambda targets only** —
mayfly's patched emulator image adds HTTP proxying for `instance`/`ip`
targets (a one-file overlay, `emulator/patches/alb.py`, submitted upstream).
`mayfly up` errors clearly if a spec declares `alb:` while on the stock
emulator image.

## Testing recipes

```bash
# balancing (fresh connection each request; pod name comes back in JSON)
for i in $(seq 8); do
  curl -s -H 'Connection: close' http://hello-alb.<ns>.localtest.me/json | jq -r .pod
done | sort | uniq -c

# listener rules: add a fixed-response and watch routing change
aws elbv2 create-rule --listener-arn "$L_ARN" --priority 10 \
  --conditions Field=path-pattern,Values='/maintenance*' \
  --actions Type=fixed-response,FixedResponseConfig='{StatusCode=503,MessageBody=down}'
```

For **real AWS ALBs** later, apps take
`ingress: {className: alb, annotations: {...}, host: ...}` — mayfly emits a
standard `networking.k8s.io/v1` Ingress the AWS Load Balancer Controller
consumes as-is (`host: "*"` matches any host, useful against the raw ALB
DNS name before Route53 exists).

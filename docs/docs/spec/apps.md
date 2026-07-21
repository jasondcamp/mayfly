---
sidebar_position: 3
---

# Apps and patches

Every app becomes a Deployment + Service reachable in-namespace at
`<name>:<servicePort>` (default 8080). Apps deploy **after** all services
are provisioned, so an app never boots before its database exists, and each
readiness probe gates `up` completing.

Each app's `readiness` doubles as its health check on dragonfly's APPS
card — one definition drives both the kubelet gate and live monitoring.
Every app pod also receives `MAYFLY_APP_NAME` (its own name) and
`MAYFLY_APP_CHECKS` (the environment's app-check list) in its env.

A worked TCP-app example — pgbouncer in front of the RDS postgres (in
production it might live on an EC2 instance; in an ephemeral environment a
container is the pragmatic stand-in). The `rds-appdb` secret's
`DATABASE_URL` is all its image needs:

```yaml
apps:
  pgbouncer:
    image: edoburu/pgbouncer:v1.24.1-p1
    port: 5432
    servicePort: 5432        # -> pgbouncer:5432, protocol-native
    secrets: [rds-appdb]
    env: {POOL_MODE: transaction, AUTH_TYPE: scram-sha-256}
    readiness: {tcp: true}
  myapi:
    image: ghcr.io/you/myapi:abc
    secrets: [rds-appdb]
    env:                     # explicit env beats envFrom: route through the pool
      DATABASE_URL: postgresql://app:apppass@pgbouncer:5432/app
```

```yaml
apps:
  myapi:
    enabled: true                       # default true
    image: ghcr.io/you/myapi:sha-abc    # required
    port: 3000                          # container port (default 80)
    servicePort: 8080                   # in-namespace Service port (default 8080);
                                        # set protocol-native ports for TCP apps
    command: ["/bin/server"]            # entrypoint override
    args: ["--verbose"]
    replicas: 2                         # default 1
    env: {LOG_LEVEL: debug}
    secrets:                            # envFrom these mayfly secrets
      - rds-appdb
      - {name: elasticache-cache-b, prefix: CACHE_B_}
    resources: {cpu: 100m, memory: 128Mi, memoryLimit: 512Mi, cpuLimit: "1"}
    readiness:
      tcp: false                        # true -> tcpSocket probe (non-HTTP apps)
      path: /healthz
      port: 3000                        # default: the app port
      initialDelaySeconds: 3
      periodSeconds: 10
      timeoutSeconds: 30                # k8s default is 1s — raise it for
                                        # probes that do real work
    imagePullSecret: regcred            # copied into the namespace at up
                                        # from --pull-secret-namespace
    ingress:
      host: myapi.example.com           # default <app>.<namespace>.localtest.me;
                                        # "*" matches any host
      className: alb
      annotations: {alb.ingress.kubernetes.io/scheme: internet-facing}
```

## Secret prefixes

Secrets of the same service class share key names (`REDIS_HOST`, ...), so
mounting several would collide under `envFrom`. A `prefix` namespaces them:
`{name: elasticache-cache-b, prefix: CACHE_B_}` yields
`CACHE_B_REDIS_HOST`, `CACHE_B_REDIS_PORT`, etc. Same mechanism for apps
that need two databases.

## Patches: the escape hatch

Anything without a dedicated field goes in `patch:` (merged onto the
generated **Deployment**) or `ingress.patch:` (merged onto the generated
**Ingress**) — arbitrary YAML applied as the final step before apply, so
manifests never have to live outside mayfly.

Merge semantics:

- **Maps** merge recursively.
- **Lists of named objects** (`containers`, `volumes`, `env`, `ports`, ...)
  merge **by name** — matching entries deep-merge, new entries append.
- **Other lists** replace wholesale. **Scalars** replace.

After the merge, mayfly re-asserts its invariants — resource name, the
`app` selector/label (extra labels survive), `enableServiceLinks: false` —
so a patch can add sidecars, volumes, tolerations, initContainers,
securityContext, TLS blocks, or extra ingress rules, but cannot silently
break the wiring the environment depends on.

```yaml
apps:
  myapi:
    image: ghcr.io/you/myapi:abc
    patch:
      spec:
        template:
          spec:
            tolerations: [{key: gpu, operator: Exists}]
            containers:
              - name: myapi             # merges into the generated container
                volumeMounts: [{name: cfg, mountPath: /etc/app}]
              - name: sidecar           # appends a new one
                image: envoy:v1.31
            volumes: [{name: cfg, configMap: {name: myapi-cfg}}]
    ingress:
      host: myapi.example.com
      patch:
        spec:
          tls: [{hosts: [myapi.example.com], secretName: myapi-tls}]
```

If you find yourself writing the same patch in every spec, that's the
signal it should graduate to a dedicated field — open an issue.

## Updating running apps

Two flows, both without `down`:

- **New image tag** (the published flow): change the tag in the spec, run
  `mayfly up` — server-side apply means only changed Deployments roll, and
  services/data are untouched.
- **Same tag, new content** (the dev loop — you rebuilt and pushed/imported
  over an existing tag): apply sees no diff, so use
  `mayfly restart env.yaml` (all apps) or `--app caddis-api --app ...` for
  a subset. It's a rolling restart with rollout waits; the emulator and
  service pods are never restarted (their state matters).

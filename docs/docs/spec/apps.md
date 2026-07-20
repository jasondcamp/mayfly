---
sidebar_position: 3
---

# Apps and patches

Every app becomes a Deployment + Service reachable in-namespace at
`<name>:8080`. Apps deploy **after** all services are provisioned, so an
app never boots before its database exists, and each readiness probe gates
`up` completing.

```yaml
apps:
  myapi:
    enabled: true                       # default true
    image: ghcr.io/you/myapi:sha-abc    # required
    port: 3000                          # container port (default 80)
    command: ["/bin/server"]            # entrypoint override
    args: ["--verbose"]
    replicas: 2                         # default 1
    env: {LOG_LEVEL: debug}
    secrets:                            # envFrom these mayfly secrets
      - rds-appdb
      - {name: elasticache-cache-b, prefix: CACHE_B_}
    resources: {cpu: 100m, memory: 128Mi, memoryLimit: 512Mi, cpuLimit: "1"}
    readiness:
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

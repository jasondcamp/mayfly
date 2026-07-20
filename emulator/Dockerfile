# mayfly-ministack: upstream MiniStack (pinned by digest) with one patch:
# the ALB data plane forwards to instance/ip targets over HTTP (upstream
# supports lambda targets only). See patches/alb.py, functions
# _forward_to_tg (added branch) and _proxy_http_target (added).
FROM ministackorg/ministack:1.4.3@sha256:22a278f078f5f88b3437abd1a4daea101bbb1b3d5d7e35353c39029a6ade09e0
LABEL org.opencontainers.image.source="https://github.com/jasondcamp/mayfly" \
      org.opencontainers.image.description="mayfly-ministack: MiniStack with ALB HTTP data-plane patch for instance/ip targets" \
      org.opencontainers.image.licenses="MIT"

COPY patches/alb.py /opt/ministack/ministack/services/alb.py
# valkey engine support for ElastiCache (valkey/valkey images, engine
# plumbing through single-node and cluster-mode spawns) — mirrors the
# jasondcamp/ministack working tree; also an upstream PR candidate.
COPY patches/elasticache.py /opt/ministack/ministack/services/elasticache.py

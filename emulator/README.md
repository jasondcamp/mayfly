# mayfly-ministack

MiniStack, digest-pinned, plus mayfly's ALB data-plane patch: upstream
routes ALB traffic to Lambda targets only; the patch adds HTTP proxying
for `instance`/`ip` targets (target Id = hostname or IP, ALB-style
X-Forwarded-*/X-Amzn-Trace-Id headers, random-choice balancing).

Also included: `patches/elasticache.py` — valkey engine support
(`valkey/valkey:{major.minor}-alpine` images, engine-aware cluster mode,
per-engine version defaults), replicated from the jasondcamp/ministack
working tree.

Build: docker build -t mayfly-ministack:dev emulator/
Regenerate patches/alb.py against a new upstream version by re-extracting
the file from the image and re-applying the two marked "mayfly patch"
sections; candidate for an upstream PR.

# mayfly-cli: the mayfly CLI as a container, used by the in-cluster reaper
# CronJob (mayfly install) — and handy for CI pipelines that want `mayfly`
# without a Python toolchain. In-cluster it authenticates via the pod's
# ServiceAccount; outside, mount a kubeconfig and set KUBECONFIG.
FROM python:3.12-slim

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /build

USER 65534:65534
WORKDIR /
ENTRYPOINT ["mayfly"]
CMD ["--help"]

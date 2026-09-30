# Candidate fixtures image — seed generator, scanner stub, change-load generator + the `challenge`
# entry point. Reviewer-only tooling (scorecard) and the oracle are deliberately NOT included.
#
# Build from the repo root:
#   docker build -f packaging/Dockerfile -t challenge-fixtures .
#
# Run privileged so a containerised detector can use auditd / fanotify / eBPF if the candidate
# chooses that approach (Docker Desktop on macOS/Windows runs a real Linux kernel):
#   docker run --rm -it --privileged -v "$PWD/work:/work" challenge-fixtures challenge version
FROM python:3.12-slim

# Optional system-capability tooling, so the audit/eBPF crowd can work inside the container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        auditd procps inotify-tools git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Only the candidate-facing fixtures (no scorecard/, no oracle defaults).
COPY challenge /app/challenge
COPY seed_generator/seed_generator.py            /app/seed_generator/seed_generator.py
COPY scanner_stub/scanner_stub.py                /app/scanner_stub/scanner_stub.py
COPY change_load_generator/load_generator.py     /app/change_load_generator/load_generator.py
COPY packaging/RUNBOOK.md                        /app/RUNBOOK.md
COPY detector/README.md                          /app/detector/README.md

RUN chmod +x /app/challenge && ln -s /app/challenge /usr/local/bin/challenge
ENV CHALLENGE_HOME=/app

# Shared work dir: seed tree + scanner socket live here so a detector on the host (or a sidecar)
# can reach them via a bind mount.
RUN mkdir -p /work/home /run/scanner
VOLUME ["/work"]

ENTRYPOINT ["challenge"]
CMD ["--help"]

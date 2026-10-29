# Lightweight Linux test image for the detector.
#
# Unlike packaging/Dockerfile this installs NO apt packages (auditd, etc.) — the
# detector talks to fanotify directly via Python ctypes and the fixtures are
# dependency-free, so the base image is all we need. This also avoids the
# out-of-memory failure the heavy apt build can hit on a small Docker VM.
#
# Build from the repo root:
#   docker build -f test/Dockerfile -t detector-test .
FROM python:3.12-slim

WORKDIR /app
# Provided fixtures (used AS-IS) + the challenge dispatcher.
COPY challenge                 /app/challenge
COPY seed_generator            /app/seed_generator
COPY scanner_stub              /app/scanner_stub
COPY change_load_generator     /app/change_load_generator
# Our component + the test driver.
COPY detector                  /app/detector
COPY test/pipeline-test.sh     /app/pipeline-test.sh

RUN chmod +x /app/challenge /app/detector/run.sh /app/pipeline-test.sh
ENV CHALLENGE_HOME=/app
ENTRYPOINT ["bash"]
CMD ["/app/pipeline-test.sh"]

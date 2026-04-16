# Build for the same OS/arch as your OpenShift *nodes* (not your laptop):
#   ./build-for-openshift.sh -t your-registry/postgres-check:v1
# Or manually, after checking node arch (see openshift-deployment.example.yaml):
#   podman build --platform linux/amd64 -t ...
#   podman build --platform linux/arm64 -t ...
#
# Using the wrong --platform gives: exec ... python: Exec format error
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY postgres_check_example.py .

USER nobody

# Absolute path avoids PATH edge cases; python3 matches the slim image
ENTRYPOINT ["/usr/local/bin/python3", "/app/postgres_check_example.py"]

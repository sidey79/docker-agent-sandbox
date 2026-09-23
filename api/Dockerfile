# The dispatcher. Python only — it talks to the Docker API and nothing else;
# all agent tooling lives in the agent image (docs/PLAN.md §5, phase 4).
FROM python:3.14.7-slim-bookworm@sha256:998acd06f485adfd6890e3e15a4b542543e0cf22ff904310095da328a5e3e561

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The job store lives in a volume mounted here; creating it with the right
# owner is what lets the unprivileged user write to a fresh named volume.
RUN useradd --create-home --uid 1000 dispatcher \
    && mkdir -p /data \
    && chown dispatcher:dispatcher /data
USER dispatcher

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

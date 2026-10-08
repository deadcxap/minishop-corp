FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f AS runtime
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/minishop/backend
WORKDIR /runtime

FROM runtime AS integration
COPY requirements-dev.lock /requirements-dev.lock
RUN pip install --no-cache-dir --require-hashes -r /requirements-dev.lock
WORKDIR /workspace

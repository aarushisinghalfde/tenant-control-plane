# syntax=docker/dockerfile:1

# ---- base: shared Python setup -------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

RUN pip install --upgrade pip \
 && groupadd --system app \
 && useradd --system --gid app --create-home --home-dir /home/app app \
 && chown app:app /srv

# ---- runtime: what the control plane and worker containers run -----------
FROM base AS runtime

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY --chown=app:app app ./app
COPY --chown=app:app worker ./worker
COPY --chown=app:app migrations ./migrations

USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---- dev: adds test, lint and security tooling plus the full repo --------
FROM base AS dev

COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY --chown=app:app . .

USER app

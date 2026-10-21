# syntax=docker/dockerfile:1.7
# One image for the API, the worker, migrations and the CLI; the compose file picks the command.
ARG PYTHON_VERSION=3.12

# ---- build: dependencies first (cached until pyproject.toml changes), then the package ---------
FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# Optional dependency groups from pyproject.toml, e.g. EXTRAS=telemetry or EXTRAS=telemetry,browser
ARG EXTRAS=""
WORKDIR /src
COPY pyproject.toml ./
RUN EXTRAS="${EXTRAS}" python - <<'PY' > /tmp/requirements.txt
import os, tomllib
project = tomllib.load(open("pyproject.toml", "rb"))["project"]
wanted = [e for e in os.environ.get("EXTRAS", "").split(",") if e]
lines = list(project["dependencies"])
for extra in wanted:
    lines += project["optional-dependencies"][extra]
print("\n".join(lines))
PY
RUN pip install --prefix=/install -r /tmp/requirements.txt
COPY backend/app ./backend/app
RUN pip install --prefix=/install --no-deps .

# ---- runtime ---------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PROMETHEUS_MULTIPROC_DIR=/var/lib/eligibility/metrics \
    EXPORT_DIR=/var/lib/eligibility/exports \
    EVIDENCE_DIR=/var/lib/eligibility/evidence

# Optional: Chromium for the policy-gated browser verification (see docs/BROWSER_VERIFICATION.md).
ARG WITH_BROWSER=0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN if [ "$WITH_BROWSER" = "1" ]; then \
      pip install --no-cache-dir "playwright>=1.45" && playwright install --with-deps chromium \
      && chmod -R a+rX /opt/pw-browsers && rm -rf /var/lib/apt/lists/*; \
    fi

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && mkdir -p /var/lib/eligibility/metrics /var/lib/eligibility/exports /var/lib/eligibility/evidence \
 && chown -R app:app /var/lib/eligibility

COPY --from=build /install /usr/local
WORKDIR /app
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
USER app
EXPOSE 8000
# --proxy-headers: trust X-Forwarded-For only from FORWARDED_ALLOW_IPS (the reverse proxy), so the
# per-client rate limits see real client addresses and not the proxy's.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]

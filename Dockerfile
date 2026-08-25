# Gemini integration service.
#
# python:3.13-slim rather than 3.14: 3.14 is validated and works, but 3.13 is the
# conservative choice for a container that would run unattended, and every dependency
# here has wheels for it.
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not invalidate the dependency layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY llm/ ./llm/
COPY service/ ./service/
COPY harness/ ./harness/

# Non-root. The service needs no write access to anything in the image.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENV GEMINI_BACKEND=vertex \
    GOOGLE_CLOUD_LOCATION=us-central1 \
    LOG_FORMAT=json \
    LOG_LEVEL=INFO

# Credentials are deliberately not baked in. Mount ADC read-only:
#   -v $HOME/.config/gcloud:/home/appuser/.config/gcloud:ro
# or attach a service account via workload identity.

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)"

# One worker by default, scaled by replica count rather than by --workers: the useful
# number is CPU-dependent and belongs to the orchestrator. A single process sheds 61%
# of load that four absorb at identical total admission capacity (FINDINGS 6i), so
# this is a decision to make deliberately, not a default to inherit.
#
# Exec form so PID 1 is uvicorn and receives SIGTERM directly. Without it the graceful
# drain in service/app.py never runs, because a shell swallows the signal.
CMD ["python", "-m", "uvicorn", "service.app:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "30", \
     "--log-level", "warning"]

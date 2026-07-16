# Backend APP image — thin overlay on gbaia-backend-base.
#
# The FROM line here is a PLACEHOLDER. The OpenShift BuildConfig's
# `strategy.dockerStrategy.from` overrides it to the cluster-local
# `gbaia-backend-base:latest` ImageStreamTag at build time, so this
# Dockerfile stays cluster-portable while getting all the pre-installed
# pip deps from the base layer for free. Result: ~30s app rebuilds
# vs 15-20min if we re-installed every dep from PyPI each time.
#
# For a plain `docker build` (outside OpenShift), swap the FROM to
# the base image manually, or add the `pip install -r requirements.txt`
# line back below WORKDIR.
FROM mirror.gcr.io/library/python:3.11-slim

WORKDIR /app

# App code only — pip deps already installed in the base layer
COPY . .

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/api/health', timeout=5).raise_for_status()"

CMD ["uvicorn", "backend:app", "--host", "0.0.0.0", "--port", "8000"]

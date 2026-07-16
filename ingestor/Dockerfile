# Ingestor APP image — thin overlay on gbaia-ingestor-base.
#
# The FROM line is a PLACEHOLDER. The OpenShift BuildConfig's
# `strategy.dockerStrategy.from` overrides it to the cluster-local
# `gbaia-ingestor-base:latest` ImageStreamTag at build time — keeping this
# file cluster-portable while inheriting all the pre-installed pip deps.
#
# For a plain `podman build` outside OpenShift, swap the FROM to the base
# image manually, or add the pip install line back below WORKDIR.
FROM mirror.gcr.io/library/python:3.11-slim

WORKDIR /app

# App code only — pip deps already installed in the base layer
COPY ingest_nw_data.py .

CMD ["python", "ingest_nw_data.py"]

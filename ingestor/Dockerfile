FROM mirror.gcr.io/library/python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy ingestor code
COPY ingest_nw_data.py .

# Environment variables will be provided by deployment
ENV PYTHONUNBUFFERED=1

# Run the ingestor
CMD ["python", "ingest_nw_data.py"]

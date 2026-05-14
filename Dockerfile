FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ ./api/

# Checked-in fixture bundles served byte-identically by
# GET /v1/fixtures/{name}. The route resolves
# api/fixtures/routes.py::FIXTURES_DIR to parents[2]/fixtures, which
# inside the container is /app/fixtures.
COPY fixtures/ ./fixtures/

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

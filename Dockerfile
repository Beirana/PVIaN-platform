FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PVI_DATA_DIR=/app/data \
    PVI_CONTRACT_DIR=/app/contracts

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY data ./data
COPY contracts ./contracts
COPY config ./config

RUN pip install --no-cache-dir .

EXPOSE 5100
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:5100/healthz')"]

CMD ["uvicorn", "pvi.main:app", "--host", "0.0.0.0", "--port", "5100"]

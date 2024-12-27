# Dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ai_pr_write.py .
COPY entrypoint.sh .

RUN chmod +x entrypoint.sh ai_pr_write.py

ENTRYPOINT ["/app/entrypoint.sh"]

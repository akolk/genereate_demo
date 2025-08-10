# Dockerfile
# -------------------------------------------------------------
# Build a reproducible image that runs generate_demo.py
# -------------------------------------------------------------
FROM python:3.12-slim AS build

# Install OS‑level build tools (git & gcc) – needed only for building some wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        gcc \
        && rm -rf /var/lib/apt/lists/*

# Copy only the files required for building the Python environment
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# -------------------------------------------------------------
# Final stage – runtime only
# -------------------------------------------------------------
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        gcc \
        && rm -rf /var/lib/apt/lists/*
        
# Add a non‑root user (helps with security on k3s)
RUN addgroup --system app && adduser --system --ingroup app app

COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

#COPY .env.example .env   # optional – you can delete this line if you never ship a .env

# Switch to non‑root user
USER app
WORKDIR /app
COPY generate_demo.py ./

# Entrypoint – the script will be executed by the CronJob
ENTRYPOINT ["python", "/app/generate_demo.py"]

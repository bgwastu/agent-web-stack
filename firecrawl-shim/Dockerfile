FROM python:3.12-slim

WORKDIR /app

# Install system deps for html parsing and network
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    pandoc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shim source
COPY shim.py .

EXPOSE 33879

ENV LISTEN_PORT=33879

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:33879/health', timeout=5)"

CMD ["python3", "shim.py"]

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shim.py .

EXPOSE 33879

ENV TAVILY_SHIM_HOST=0.0.0.0
ENV TAVILY_SHIM_PORT=33879

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:33879/health', timeout=5)"

CMD ["python3", "shim.py"]

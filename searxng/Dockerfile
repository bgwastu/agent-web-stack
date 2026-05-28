FROM searxng/searxng:latest

USER root

# Copy custom engines into SearXNG's engine directory
COPY engines/google_camofox.py /usr/local/searxng/searx/engines/google_camofox.py
COPY engines/scavio.py /usr/local/searxng/searx/engines/scavio.py
COPY engines/tavily.py /usr/local/searxng/searx/engines/tavily.py

# Ensure correct ownership (SearXNG runs as uid 977 / searxng user)
RUN chown 977:977 /usr/local/searxng/searx/engines/google_camofox.py \
    /usr/local/searxng/searx/engines/scavio.py \
    /usr/local/searxng/searx/engines/tavily.py

USER searxng

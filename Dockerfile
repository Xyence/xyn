FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    postgresql-client \
    docker.io \
    docker-compose \
    curl \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js and Codex CLI for Epic C runtime execution.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get update && apt-get install -y nodejs \
    && npm install -g @openai/codex \
    && codex --help >/tmp/codex-help.txt \
    && rm -rf /var/lib/apt/lists/* /root/.npm

RUN git config --system --add safe.directory /workspace/xyn \
    && git config --system --add safe.directory /workspace/xyn-platform

# Copy requirements and install Python dependencies
COPY requirements.txt requirements-mcp.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Install MCP dependencies in an isolated venv so MCP pinning does not conflict with core runtime deps.
RUN python -m venv /opt/mcp \
    && /opt/mcp/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/mcp/bin/pip install --no-cache-dir -r requirements-mcp.txt

# Copy application code
COPY core/ ./core/

# Create artifacts directory
RUN mkdir -p /app/artifacts

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5).read()"

EXPOSE 8000

CMD ["uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8000"]

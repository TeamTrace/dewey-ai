# Dewey image. By default it runs the MCP server; the same image also runs the
# indexer (app/setup_postgres.py) and the localhost demo page (app/main.py).
# See docs/DEPLOYMENT.md.
#
#   docker build -t dewey .                                        # azure / anthropic stacks
#   docker build -t dewey --build-arg INSTALL_LOCAL_MODELS=true .  # adds local embedding/reranking models
#
# The image holds no secrets and no articles: configuration (including
# DEWEY_MCP_TOKEN) comes from environment variables at run time, and articles
# are indexed into Postgres with app/setup_postgres.py (see docker-compose.yml).
FROM python:3.12-slim

# true: also install CPU-only PyTorch and requirements-local.txt, for
# EMBEDDING_PROVIDER=local / RERANK_PROVIDER=local (the defaults for DEWEY_STACK=open).
ARG INSTALL_LOCAL_MODELS=false

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GRADIO_ANALYTICS_ENABLED=False

# Unprivileged user with a fixed id (1000).
RUN groupadd --gid 1000 dewey \
    && useradd --uid 1000 --gid dewey --create-home --home-dir /home/dewey --shell /usr/sbin/nologin dewey

WORKDIR /app

# requirements.txt pins every package, so a rebuild installs the same versions. CPU-only
# PyTorch comes from its own index, pinned here; Dependabot doesn't see this line, so bump
# it by hand.
COPY requirements.txt requirements-local.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_LOCAL_MODELS" = "true" ]; then \
         pip install --no-cache-dir torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu \
         && pip install --no-cache-dir -r requirements-local.txt; \
       fi

COPY app/ ./app/

# data/ is a mount point for indexing runs; the Hugging Face cache directory is
# created here so a named volume mounted on it is owned by the dewey user.
RUN mkdir -p /app/data /home/dewey/.cache/huggingface \
    && chown -R dewey:dewey /app/data /home/dewey/.cache

USER dewey

EXPOSE 8765

# Checks that the default command's port accepts connections. (The MCP endpoint
# answers 401 without the token, so there is no page to fetch.) Another command,
# such as the demo page, needs its own check; docker-compose.yml sets one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8765), timeout=4).close()"

# The MCP server over Streamable HTTP at http://<container>:8765/mcp. It refuses
# to start without DEWEY_MCP_TOKEN (32+ characters). It listens on all of the
# container's interfaces, so publish the port only where you mean to, and put
# TLS in front of it for anything beyond one machine (docs/DEPLOYMENT.md).
CMD ["python", "app/mcp_server.py", "--http", "--host", "0.0.0.0", "--port", "8765"]

# Dewey web app image. See docs/DEPLOYMENT.md.
#
#   docker build -t dewey .                                        # azure / anthropic stacks
#   docker build -t dewey --build-arg INSTALL_LOCAL_MODELS=true .  # adds local embedding/reranking models
#
# The image holds no secrets and no articles: configuration comes from
# environment variables at run time, and articles are indexed into Postgres
# with app/setup_postgres.py (see docker-compose.yml).
FROM python:3.12-slim

# true: also install CPU-only PyTorch and requirements-local.txt, for
# EMBEDDING_PROVIDER=local / RERANK_PROVIDER=local (the defaults for DEWEY_STACK=open).
ARG INSTALL_LOCAL_MODELS=false

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7860 \
    GRADIO_ANALYTICS_ENABLED=False

# Unprivileged user with a fixed id (1000), so a bind-mounted users.txt can be
# made readable to it on the host.
RUN groupadd --gid 1000 dewey \
    && useradd --uid 1000 --gid dewey --create-home --home-dir /home/dewey --shell /usr/sbin/nologin dewey

WORKDIR /app

COPY requirements.txt requirements-local.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_LOCAL_MODELS" = "true" ]; then \
         pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
         && pip install --no-cache-dir -r requirements-local.txt; \
       fi

COPY app/ ./app/

# data/ is a mount point for indexing runs; the Hugging Face cache directory is
# created here so a named volume mounted on it is owned by the dewey user.
RUN mkdir -p /app/data /home/dewey/.cache/huggingface \
    && chown -R dewey:dewey /app/data /home/dewey/.cache

USER dewey

EXPOSE 7860

# "/" answers 200 (the chat page, or the login page when accounts are set up).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('GRADIO_SERVER_PORT') or os.environ.get('PORT') or '7860'; urllib.request.urlopen('http://127.0.0.1:' + port + '/', timeout=4)"

CMD ["python", "app/main.py"]

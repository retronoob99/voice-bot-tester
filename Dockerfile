FROM python:3.13-slim

# uv gives fast, reproducible installs from uv.lock
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install deps first so dependency layers cache independently of source changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 7860

# Render injects $PORT at runtime; default to 7860 for local `docker run`
CMD ["sh", "-c", "uv run main.py serve --host 0.0.0.0 --port ${PORT:-7860}"]

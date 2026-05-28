FROM python:3.11-slim-bookworm

# Copy uv binary from official Astral image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy source code and dependency definition
COPY eprint_bot.py requirements.txt ./

# Install dependencies system-wide in container using uv (ultra fast)
RUN uv pip install --system --no-cache -r requirements.txt

# Create directory to mount persistent volumes (to save state.json)
RUN mkdir -p /app/data

# Configure environment defaults
ENV STATE_FILE=/app/data/state.json
ENV DAEMON_INTERVAL=10800

# Executable entrypoint
CMD ["python", "eprint_bot.py"]

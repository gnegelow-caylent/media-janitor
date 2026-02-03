FROM python:3.12-slim

# Install ffmpeg and curl (for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy just dependency files first (for better caching)
COPY pyproject.toml README.md ./

# Create minimal src structure for pip install to work
RUN mkdir -p src/media_janitor && touch src/media_janitor/__init__.py

# Install dependencies (cached unless pyproject.toml changes)
RUN pip install --no-cache-dir .

# Now copy the actual source code
COPY src/ src/

# Reinstall to update the package with actual code (fast, deps already cached)
RUN pip install --no-cache-dir . --no-deps

# Create data directory
RUN mkdir -p /data/logs

# Set environment variables
ENV MEDIA_JANITOR_CONFIG=/data/config.yaml
ENV PYTHONUNBUFFERED=1

# Expose webhook port
EXPOSE 9000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:9000/health || exit 1

# Run the application
CMD ["media-janitor"]

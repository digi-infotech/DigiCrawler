FROM python:3.12-slim

# Headless Chromium for the automatic browser fallback (~2 GB image). Set WITH_BROWSER=false in .env for a ~285 MB image.
ARG WITH_BROWSER=true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data

WORKDIR /app

# 1) Dependencies (and optional browser) first, so code changes don't re-download them.
COPY pyproject.toml ./
RUN mkdir -p src/contact_scraper && touch src/contact_scraper/__init__.py \
 && pip install . \
 && if [ "$WITH_BROWSER" = "true" ]; then \
        apt-get update && apt-get install -y --no-install-recommends libatomic1 \
        && pip install playwright && playwright install --with-deps chromium \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# 2) The app itself.
COPY src ./src
RUN pip install --no-deps --force-reinstall .

VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')" || exit 1

CMD ["uvicorn", "contact_scraper.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

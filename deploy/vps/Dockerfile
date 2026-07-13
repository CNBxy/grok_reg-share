FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    chromium \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    tini \
    xauth \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev

COPY . .

RUN mkdir -p /app/cpa_auths /app/cookies /app/screenshots

EXPOSE 5000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["xvfb-run", "-a", "/app/.venv/bin/python", "/app/serve_web.py"]

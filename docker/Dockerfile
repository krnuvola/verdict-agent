FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.6.10 /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY config ./config
COPY playbooks ./playbooks
RUN uv sync --frozen --no-dev && uv run playwright install --with-deps chromium

COPY alembic.ini ./
EXPOSE 8100 6080

CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x900x24 & x11vnc -display :99 -forever -shared -nopw -rfbport 5900 & websockify --web=/usr/share/novnc/ 6080 localhost:5900 & uv run alembic upgrade head && uv run uvicorn gba_agent.api.main:app --host 0.0.0.0 --port 8100 --workers 1"]

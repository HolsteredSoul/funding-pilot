# ═══════════════════════════════════════════════════════════════════
# AU-Funding-Arb v1.0 — Multi-stage Docker build
# ═══════════════════════════════════════════════════════════════════

FROM python:3.12-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ── Dependencies ────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application ─────────────────────────────────────────────────────
COPY src/ src/
COPY backtest/ backtest/

# ── Non-root user ───────────────────────────────────────────────────
RUN groupadd -r arb && useradd -r -g arb -d /app arb \
    && mkdir -p /app/data \
    && chown -R arb:arb /app
USER arb

# ── Data volume (positions.json, trades_aud.csv, rba_rates.csv) ────
VOLUME /app/data

# ── Health check ────────────────────────────────────────────────────
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# ── Entry point ─────────────────────────────────────────────────────
ENTRYPOINT ["python", "-m", "src.main"]

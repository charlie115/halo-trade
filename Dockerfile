FROM node:22-bookworm-slim AS web
# What the page will accept from the server, fixed at build time. Empty is allowed on testnet.
ARG HALO_EXPECTED_GRANTEE_TESTNET=""
ARG HALO_EXPECTED_MARKET_ID_TESTNET=""
ARG HALO_EXPECTED_GRANTEE_MAINNET=""
ARG HALO_EXPECTED_MARKET_ID_MAINNET=""
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/build.mjs ./
COPY web/src ./src
RUN node build.mjs

FROM python:3.12-slim AS base
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e .
COPY --from=web /web/dist/ /app/src/halo_trade/static/
ENV PYTHONUNBUFFERED=1
RUN useradd -m app && mkdir -p /data && chown app /data

# The test image is the only one that takes the whole repo.
FROM base AS test
RUN pip install --no-cache-dir -e '.[dev]'
COPY . .
COPY --from=web /web/dist/ /app/src/halo_trade/static/
USER app
CMD ["pytest", "-q"]

# The default target, and the one that runs in production: code and page only.
FROM base
USER app
EXPOSE 8080
# --no-proxy-headers: only HALO_TRUSTED_PROXY decides whether X-Forwarded-For is believed.
CMD ["uvicorn", "halo_trade.api:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]

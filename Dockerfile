# Market Terminal image: the web terminal, the MCP server and the pipeline share it (compose.yaml picks the command).
# Multi-arch: every dependency has wheels for linux/amd64 and linux/arm64, so no compiler is installed.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PORTFOLIO_DATA_DIR=/data \
    OPTIONS_WHALE_HOST=0.0.0.0 MCP_HOST=0.0.0.0

WORKDIR /app
COPY requirements.txt .
# The playwright package is installed but its browsers are not (no `playwright install`): only the optional CME
# download (core/cme.py) drives a browser, and that needs a login you finish by hand, which a container cannot show.
# Without a browser the CME step reports "missing" and the run continues.
RUN pip install -r requirements.txt

# A non-root user owns /data, so a fresh named volume is writable by it.
RUN useradd --create-home --uid 1000 app && mkdir /data && chown app /data
COPY --chown=app . .
USER app
VOLUME /data

EXPOSE 8080 8787
CMD ["python", "options_whale/api_router.py"]

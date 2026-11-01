FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY tradebot ./tradebot
RUN pip install --no-cache-dir -e .
# Config and state live in a mounted volume so the kill file / armed file / ledger persist.
VOLUME ["/app/data"]
ENV TRADEBOT_CONFIG=/app/data/config.toml TRADEBOT_DATA_DIR=/app/data
# Paper by default. Live needs the env gate + armed file + key; see README.
CMD ["tradebot", "run"]

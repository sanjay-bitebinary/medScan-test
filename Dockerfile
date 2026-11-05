FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 poppler-utils curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

COPY src ./src
COPY v3 ./v3
COPY data ./data
COPY sample_input.pdf README.md VERSION.md ./
COPY start-v3.sh /usr/local/bin/start-v3
RUN chmod +x /usr/local/bin/start-v3

EXPOSE 5003
HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5003/health || exit 1
CMD ["start-v3"]

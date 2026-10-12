# Browser plugin: Chromium + CDP runtime for Pulse.
# Installer copies pulse_plugin into .pulse_plugin_sdk/ before build.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        fonts-liberation \
        fonts-dejavu-core \
        fonts-noto-core \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Chromium in Debian is /usr/bin/chromium
ENV CHROME_BIN=/usr/bin/chromium \
    PULSE_PLUGIN_PORT=8080 \
    # Avoid /dev/shm issues in small Docker defaults
    CHROME_FLAGS="--disable-dev-shm-usage --no-sandbox --disable-gpu"

WORKDIR /plugin
COPY .pulse_plugin_sdk /tmp/pulse_plugin
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir /tmp/pulse_plugin \
    && pip install --no-cache-dir -r backend/requirements.txt

COPY . .
WORKDIR /plugin/backend
CMD ["sh", "-c", "exec uvicorn browser_plugin.app:app --host 0.0.0.0 --port ${PULSE_PLUGIN_PORT}"]

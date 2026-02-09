FROM python:3.12-slim AS base

# System dependencies for audio platforms
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    pulseaudio-utils \
    xdotool \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml README.md LICENSE ./
RUN pip install --no-cache-dir -e ".[all]" 2>/dev/null || pip install --no-cache-dir .

# Copy source
COPY voicekit/ voicekit/
COPY config.example.yaml ./

# Default config path
ENV VOICEKIT_CONFIG=/app/config.yaml

EXPOSE 8080

ENTRYPOINT ["voicekit"]
CMD ["start", "--config", "/app/config.yaml"]

FROM python:3.12-slim

# ffmpeg: yt-dlp merges separate video/audio streams with it.
# git:    the vault is a git repo, committed to from inside the container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY config ./config
COPY scripts ./scripts

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    COOKIES_PATH=/secrets/cookies.txt

CMD ["python", "-m", "sme.worker"]

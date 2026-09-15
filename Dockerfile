FROM python:3.12-slim

# ffmpeg is deliberately NOT installed by default.
#
# Debian's ffmpeg package drags in SDL2, X11 and libavdevice — ~154 MB of
# transitive dependencies that dominate the build (about two minutes of the
# three). yt-dlp only needs ffmpeg to MERGE separate audio and video streams,
# and downloader.py pins the format selector to pre-muxed single files, so
# there is nothing to merge.
#
# If you ever hit a source that only offers split streams, rebuild with
#   --build-arg WITH_FFMPEG=true
ARG WITH_FFMPEG=false

# git: the vault is a git repo, committed to from inside the container.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
         git ca-certificates \
    && if [ "$WITH_FFMPEG" = "true" ]; then \
         DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg; \
       fi \
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

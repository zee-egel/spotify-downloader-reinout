FROM python:3.12-alpine
WORKDIR /app
COPY app.py profiles.py youtube_fallback.py ui.css ui.js ./
RUN apk add --no-cache ffmpeg nodejs && pip install --no-cache-dir 'yt-dlp[default]'
RUN adduser -D -u 1000 app && mkdir -p /state /downloads && chown app:app /state /downloads
USER app
ENV STATE_FILE=/state/runs.json DOWNLOADS_ROOT=/downloads PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]

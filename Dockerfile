FROM python:3.12-alpine
WORKDIR /app
COPY app.py profiles.py views.py ui.css ui.js profile.js youtube_fallback.py audio_metadata.py download_doctor.py favicon.ico ./
COPY templates ./templates
RUN apk add --no-cache ffmpeg && adduser -D -u 1000 app && mkdir -p /state /downloads && chown app:app /state /downloads
USER app
ENV STATE_FILE=/state/runs.json DOWNLOADS_ROOT=/downloads PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]

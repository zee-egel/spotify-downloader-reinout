FROM python:3.12-alpine
WORKDIR /app
COPY app.py .
RUN adduser -D -u 1000 app && mkdir /state && chown app:app /state
USER app
ENV STATE_FILE=/state/runs.json DOWNLOADS_ROOT=/downloads PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]

FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir gunicorn
COPY app.py nartodrama_downloader.py ./
COPY templates ./templates
COPY static ./static
ENV NARTO_CACHE=/data/narto_cache
ENV NARTO_CACHE_MAX_MB=10240
ENV NARTO_BASE_PATH=/narto
ENV PORT=5000
ENV HOST=0.0.0.0
RUN mkdir -p /data/narto_cache
VOLUME ["/data/narto_cache"]
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD curl -fs http://127.0.0.1:5000/health || exit 1
CMD ["gunicorn","--bind","0.0.0.0:5000","--workers","2","--threads","8","--timeout","600","app:app"]

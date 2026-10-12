FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ATLAS_HOST=0.0.0.0 ATLAS_PORT=8765 ATLAS_DB=/app/storage/atlas.sqlite3
WORKDIR /app
COPY server ./server
COPY web ./web
COPY data/provinces_34.json ./data/provinces_34.json
COPY data/province_mergers_2025.json ./data/province_mergers_2025.json
COPY data/provinces ./data/provinces
RUN mkdir -p /app/storage && useradd -u 10001 -m atlas && chown -R atlas:atlas /app/storage
USER atlas
EXPOSE 8765
CMD ["python","-m","server.app"]

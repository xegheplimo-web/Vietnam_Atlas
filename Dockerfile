FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ATLAS_HOST=0.0.0.0 ATLAS_PORT=8765 ATLAS_DB=/app/storage/atlas.sqlite3
WORKDIR /app
COPY server ./server
COPY web ./web
# Admin resolver + its whole import chain (import_gazetteer provides name_norm;
# its xlrd/openpyxl imports are optional-guarded so no heavy deps are needed).
COPY tools/resolve_admin.py ./tools/resolve_admin.py
COPY tools/import_gazetteer.py ./tools/import_gazetteer.py
COPY data/provinces_34.json ./data/provinces_34.json
COPY data/province_mergers_2025.json ./data/province_mergers_2025.json
COPY data/provinces ./data/provinces
RUN mkdir -p /app/storage && useradd -u 10001 -m atlas && chown -R atlas:atlas /app/storage
USER atlas
EXPOSE 8765
CMD ["python","-m","server.app"]

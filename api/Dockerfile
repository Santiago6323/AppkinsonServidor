# API de Appkinson (FastAPI). Se construye desde docker-compose.yml.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APPKINSON_DATOS=/datos \
    APPKINSON_TAREAS=/app/semillas/Actividades.txt

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py schema.sql ./
COPY semillas/ ./semillas/

# Usuario sin privilegios (UID 1000). El servicio "permisos" del compose le asigna la
# carpeta persistente de datos.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin appkinson \
    && mkdir -p /datos && chown appkinson:appkinson /datos
USER appkinson

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/salud', timeout=4)"

# --proxy-headers: detrás de nginx, registra la IP real del teléfono y no la del proxy
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]

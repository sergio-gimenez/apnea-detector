FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APNEA_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml ./
COPY apnea_api ./apnea_api
RUN pip install --no-cache-dir .

RUN useradd --system --uid 10001 --create-home apnea \
    && mkdir -p /data \
    && chown apnea:apnea /data
USER apnea

EXPOSE 8080
CMD ["uvicorn", "apnea_api.main:app", "--host", "0.0.0.0", "--port", "8080"]

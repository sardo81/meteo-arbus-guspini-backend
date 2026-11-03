FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && playwright install --with-deps chromium

COPY . /app

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app_feed_v15_daily_extremes:api --host 0.0.0.0 --port ${PORT:-8080}"]

# Трекер реферальных ссылок Telegram — образ для Render (и любого Docker-хостинга).
# Зависимостей нет: образ основан на чистом Python.
FROM python:3.12-slim

WORKDIR /app

COPY tg_ref.py ./
COPY config.example.json ./
COPY requirements.txt ./

RUN mkdir -p /app/logs

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    TZ=Europe/Samara

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health', timeout=4)"

CMD ["python", "tg_ref.py", "serve"]

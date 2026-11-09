# Трекер реферальных ссылок Telegram — PHP-версия для Render.
FROM php:8.3-cli

WORKDIR /app
COPY index.php worker.php lib.php ./

RUN mkdir -p /data
ENV STATE_FILE=/data/state.json \
    PORT=8080 \
    TZ=Europe/Samara \
    PHP_CLI_SERVER_WORKERS=4

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD php -r "exit(@file_get_contents('http://127.0.0.1:' . (getenv('PORT') ?: 8080) . '/health') === false ? 1 : 0);"

# Два процесса: веб-сервис и воркер (он же следит за расписанием)
CMD ["sh", "-c", "php -S 0.0.0.0:${PORT:-8080} index.php & exec php worker.php"]

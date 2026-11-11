<?php
/**
 * Точка входа для cron-задачи хостинга.
 *
 * На обычном виртуальном хостинге долгоживущий процесс (worker.php) запустить нельзя,
 * поэтому ежедневную задачу поднимает панельный крон одной строкой:
 *
 *   php /home/USER/www/tracker/cron.php
 *
 * Скрипт ничего не делает, если время ещё не наступило или отчёт за день уже отправлен.
 * Как только наступит SCHEDULE_TIME — создаст ссылки, соберёт статистику и пришлёт отчёт.
 */

declare(strict_types=1);

require __DIR__ . '/lib.php';

date_default_timezone_set(tz()->getName());

try {
    if (in_array('--force', $argv ?? [], true)) {
        // Ручной запуск: выполнить задачу сейчас, не дожидаясь расписания
        $res = runDaily(today(), true);
        echo 'ok (forced): ' . nowIso() . PHP_EOL;
        echo $res['report'] . PHP_EOL;
    } else {
        maybeRunDaily();
        echo 'ok: ' . nowIso() . PHP_EOL;
    }
} catch (Throwable $e) {
    echo 'error: ' . $e->getMessage() . PHP_EOL;
    exit(1);
}

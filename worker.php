<?php
/**
 * Воркер: без крона и без таймеров.
 * В одном бесконечном цикле — слушает команды Telegram и сам следит за временем.
 */

declare(strict_types=1);

require __DIR__ . '/lib.php';

date_default_timezone_set(tz()->getName());
set_time_limit(0);
ignore_user_abort(true);

error_log('tg-ref worker: старт, каналов ' . count(channels()));

$offset = 0;
$lastBeat = 0;

while (true) {
    // 1. Команды из Telegram (long polling блокирует до 20 секунд)
    try {
        $params = ['timeout' => 20];
        if ($offset > 0) {
            $params['offset'] = $offset;
        }
        $updates = telegram('getUpdates', $params);
        foreach ($updates as $u) {
            $offset = (int) $u['update_id'] + 1;
            handleUpdate($u);
        }
    } catch (Throwable $e) {
        error_log('Telegram: ' . $e->getMessage());
        sleep(3);
    }

    // 2. Время пришло? — тогда ежедневная задача
    try {
        maybeRunDaily();
    } catch (Throwable $e) {
        error_log('Задача дня: ' . $e->getMessage());
    }

    // 3. Пульс — по нему видно, что воркер жив
    if (time() - $lastBeat > 60) {
        $lastBeat = time();
        try {
            heartbeat();
        } catch (Throwable $e) {
            error_log('Пульс: ' . $e->getMessage());
        }
    }
}

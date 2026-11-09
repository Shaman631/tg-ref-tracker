<?php
/**
 * Общие функции трекера реферальных ссылок Telegram.
 * Только стандартная библиотека PHP: расширения не нужны.
 */

declare(strict_types=1);

const DEFAULT_TZ = 'Europe/Samara';
const DEFAULT_SCHEDULE = '21:00';

function env(string $key, ?string $default = null): ?string
{
    $v = getenv($key);
    return ($v === false || $v === '') ? $default : $v;
}

function tz(): DateTimeZone
{
    static $tz = null;
    if ($tz === null) {
        try {
            $tz = new DateTimeZone((string) env('TZ', DEFAULT_TZ));
        } catch (Throwable $e) {
            $tz = new DateTimeZone(DEFAULT_TZ);
        }
    }
    return $tz;
}

function apiBase(): string
{
    return rtrim((string) env('TELEGRAM_API', 'https://api.telegram.org'), '/');
}

function botToken(): string
{
    $t = (string) env('BOT_TOKEN', '');
    if ($t === '') {
        throw new RuntimeException('Не задан BOT_TOKEN');
    }
    return $t;
}

/** Один вызов Telegram Bot API. */
function telegram(string $method, array $params = []): array
{
    $url = apiBase() . '/bot' . botToken() . '/' . $method;
    $ctx = stream_context_create(['http' => [
        'method'        => 'POST',
        'header'        => "Content-Type: application/x-www-form-urlencoded\r\n",
        'content'       => http_build_query($params),
        'timeout'       => 90,
        'ignore_errors' => true,
    ]]);
    $raw = @file_get_contents($url, false, $ctx);
    if ($raw === false) {
        throw new RuntimeException('Telegram недоступен (' . $method . ')');
    }
    $data = json_decode($raw, true);
    if (!is_array($data) || empty($data['ok'])) {
        $desc = is_array($data) ? ($data['description'] ?? 'без описания') : 'неизвестная ошибка';
        throw new RuntimeException($method . ': ' . $desc);
    }
    return is_array($data['result']) ? $data['result'] : $data['result'];
}

function sendMessage(string $chatId, string $text): void
{
    telegram('sendMessage', [
        'chat_id'                  => $chatId,
        'text'                     => $text,
        'parse_mode'               => 'HTML',
        'disable_web_page_preview' => 'true',
    ]);
}

/** Список каналов из CHANNELS: "@one,@two" */
function channels(): array
{
    $raw = (string) env('CHANNELS', '');
    $out = [];
    foreach (preg_split('/[,;\s]+/', $raw) as $name) {
        $name = trim($name);
        if ($name === '') {
            continue;
        }
        if ($name[0] !== '@' && $name[0] !== '-') {
            $name = '@' . $name;
        }
        $out[] = $name;
    }
    return $out;
}

function chatId(): ?string
{
    $id = trim((string) env('CHAT_ID', ''));
    return $id === '' ? null : $id;
}

/** Ключ доступа к веб-странице: из переменной или выведенный из токена (стабилен). */
function webKey(): string
{
    $k = trim((string) env('WEB_KEY', ''));
    if ($k !== '') {
        return $k;
    }
    return substr(hash('sha256', 'tg-ref-web:' . botToken()), 0, 32);
}

function scheduleTime(): string
{
    $v = trim((string) env('SCHEDULE_TIME', DEFAULT_SCHEDULE));
    return preg_match('/^\d{1,2}:\d{2}$/', $v) ? $v : DEFAULT_SCHEDULE;
}

// ---------------------------------------------------------------- состояние

function statePath(): string
{
    $p = (string) env('STATE_FILE', __DIR__ . '/state.json');
    $dir = dirname($p);
    if (!is_dir($dir)) {
        @mkdir($dir, 0777, true);
    }
    return $p;
}

function readState(): array
{
    $p = statePath();
    if (!is_file($p)) {
        return [];
    }
    $fh = @fopen($p, 'r');
    if (!$fh) {
        return [];
    }
    @flock($fh, LOCK_SH);
    $raw = stream_get_contents($fh);
    @flock($fh, LOCK_UN);
    fclose($fh);
    $s = json_decode((string) $raw, true);
    return is_array($s) ? $s : [];
}

/**
 * Чтение-изменение-запись состояния под эксклюзивной блокировкой.
 * Замыкание получает &$state и может её менять.
 */
function withState(callable $fn)
{
    $p = statePath();
    $fh = @fopen($p, 'c+');
    if (!$fh) {
        throw new RuntimeException('Не удалось открыть файл состояния: ' . $p);
    }
    @flock($fh, LOCK_EX);
    $raw = stream_get_contents($fh);
    $state = $raw ? (json_decode($raw, true) ?: []) : [];
    if (!is_array($state)) {
        $state = [];
    }
    try {
        $result = $fn($state);
    } finally {
        ftruncate($fh, 0);
        rewind($fh);
        fwrite($fh, (string) json_encode($state, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
        fflush($fh);
        @flock($fh, LOCK_UN);
        fclose($fh);
    }
    return $result;
}

// ------------------------------------------------------------------- работа

function nowIso(): string
{
    return (new DateTimeImmutable('now', tz()))->format('Y-m-d\TH:i:sP');
}

function today(): string
{
    return (new DateTimeImmutable('now', tz()))->format('Y-m-d');
}

function linkName(string $chat, string $date): string
{
    $raw = preg_replace('/[^A-Za-z0-9_-]+/', '_', $chat) ?? 'ch';
    return substr('ref_' . str_replace('-', '', $date) . '_' . trim($raw, '_'), 0, 32);
}

/**
 * Полный цикл: создать ссылки дня, собрать статистику, отправить отчёт.
 * Всё выполняется под блокировкой состояния, поэтому два процесса
 * (воркер и веб) не сработают одновременно.
 */
function runDaily(string $date, bool $force = false): array
{
    return withState(function (array &$s) use ($date, $force): array {
        if (!$force && !empty($s['days'][$date]['sent_at'])) {
            return ['skipped' => true, 'report' => (string) ($s['days'][$date]['report'] ?? '')];
        }

        $errors = [];

        // 1. Ссылки дня
        foreach (channels() as $chat) {
            $cur = $s['channels'][$chat] ?? null;
            if (is_array($cur) && ($cur['date'] ?? '') === $date && !empty($cur['link'])) {
                continue;
            }
            try {
                $inv = telegram('createChatInviteLink', [
                    'chat_id' => $chat,
                    'name'    => linkName($chat, $date),
                ]);
                $s['channels'][$chat] = [
                    'date' => $date,
                    'link' => (string) ($inv['invite_link'] ?? ''),
                    'join' => 0,
                    'name' => linkName($chat, $date),
                ];
            } catch (Throwable $e) {
                $errors[] = $chat . ': ' . $e->getMessage();
            }
        }

        // 2. Статистика и прирост
        $rows = [];
        $total = 0;
        foreach (channels() as $chat) {
            $cur = $s['channels'][$chat] ?? null;
            if (!is_array($cur) || empty($cur['link'])) {
                continue;
            }
            $join = 0;
            try {
                $st = telegram('getChatInviteLinkStats', [
                    'chat_id'     => $chat,
                    'invite_link' => $cur['link'],
                ]);
                $join = (int) ($st['join_count'] ?? 0);
            } catch (Throwable $e) {
                $errors[] = $chat . ': ' . $e->getMessage();
            }
            $delta = max(0, $join - (int) ($cur['join'] ?? 0));
            $cur['join'] = $join;
            $s['channels'][$chat] = $cur;

            $acc = (int) ($s['days'][$date]['channels'][$chat]['new'] ?? 0) + $delta;
            $s['days'][$date]['channels'][$chat]['new'] = $acc;
            $total += $acc;

            $members = null;
            try {
                $members = (int) telegram('getChatMemberCount', ['chat_id' => $chat]);
            } catch (Throwable $e) {
                // число подписчиков — необязательно
            }
            $rows[] = [
                'channel' => $chat,
                'link'    => (string) $cur['link'],
                'new'     => $acc,
                'join'    => $join,
                'members' => $members,
            ];
        }

        $s['days'][$date]['total'] = $total;
        if ($errors) {
            $s['days'][$date]['errors'] = $errors;
        }

        $text = reportText($date, $rows, $total, $errors);
        $s['days'][$date]['report'] = $text;
        $s['days'][$date]['sent_at'] = nowIso();

        // 3. Отчёт в Telegram
        $to = chatId();
        if ($to !== null) {
            try {
                sendMessage($to, $text);
            } catch (Throwable $e) {
                $errors[] = 'отчёт: ' . $e->getMessage();
                $s['days'][$date]['errors'] = $errors;
            }
        }

        return ['skipped' => false, 'report' => $text, 'errors' => $errors, 'total' => $total];
    });
}

function reportText(string $date, array $rows, int $total, array $errors = []): string
{
    $e = static fn(string $s): string => htmlspecialchars($s, ENT_QUOTES, 'UTF-8');
    $out = '<b>📊 Реферальные ссылки — ' . $e($date) . '</b>';
    if (!$rows) {
        $out .= "\n\nКаналы не настроены (CHANNELS).";
    }
    foreach ($rows as $r) {
        $out .= "\n\n<b>" . $e($r['channel']) . "</b>";
        $out .= "\n• Ссылка: <code>" . $e($r['link']) . "</code>";
        $out .= "\n• Пришло по ссылке сегодня: <b>" . $r['new'] . "</b> (всего по ссылке: " . $r['join'] . ")";
        if ($r['members'] !== null) {
            $out .= "\n• Подписчиков в канале: <b>" . $r['members'] . "</b>";
        }
    }
    $out .= "\n\n<b>Итого за день: +" . $total . "</b> подписчиков по ссылкам";
    if ($errors) {
        $out .= "\n\n⚠️ " . $e(implode('; ', $errors));
    }
    return $out;
}

/**
 * Проверка «пора ли»: вызывается воркером и веб-обработчиком.
 * Крон не нужен — время наступило, значит пора.
 */
function maybeRunDaily(): void
{
    $now = new DateTimeImmutable('now', tz());
    [$h, $m] = array_map('intval', explode(':', scheduleTime()));
    $target = $now->setTime($h, $m, 0);
    if ($now < $target) {
        return;
    }
    $date = $now->format('Y-m-d');
    $state = readState();
    if (!empty($state['days'][$date]['sent_at'])) {
        return;
    }
    // Догоняем вчерашний день, если он остался без отчёта
    $y = (new DateTimeImmutable('now', tz()))->modify('-1 day')->format('Y-m-d');
    if (!empty($state['days'][$y]) && empty($state['days'][$y]['sent_at'])) {
        runDaily($y, false);
    }
    runDaily($date, false);
}

function heartbeat(): void
{
    withState(function (array &$s): void {
        $s['worker']['last_seen'] = nowIso();
    });
}

// ------------------------------------------------------------- команды бота

function handleUpdate(array $u): void
{
    $msg = $u['message'] ?? null;
    if (!is_array($msg)) {
        return;
    }
    $text = trim((string) ($msg['text'] ?? ''));
    $chat = (string) ($msg['chat']['id'] ?? '');
    if ($text === '' || $chat === '') {
        return;
    }
    $cmd = strtolower(explode(' ', explode('@', $text)[0])[0]);

    try {
        if ($cmd === '/start') {
            sendMessage($chat, "👋 Трекер реферальных ссылок.\n\n"
                . "Команды:\n/daily — создать ссылки дня и прислать отчёт\n"
                . "/links — ссылки дня\n/status — состояние\n"
                . "/webkey — ключ доступа к веб-странице\n\n"
                . "Ваш chat_id: <code>{$chat}</code> — его же укажите в CHAT_ID.");
        } elseif ($cmd === '/daily') {
            $res = runDaily(today(), true);
            // Отчёт уже ушёл в CHAT_ID — не дублируем, если это тот же чат
            if (chatId() === null || chatId() !== $chat) {
                sendMessage($chat, (string) $res['report']);
            }
        } elseif ($cmd === '/links') {
            $state = readState();
            $date = today();
            $lines = ["Ссылки на {$date}:"];
            foreach (channels() as $c) {
                $link = $state['channels'][$c]['link'] ?? null;
                $lines[] = '• ' . $c . ': ' . ($link ?: '— нет —');
            }
            sendMessage($chat, implode("\n", $lines));
        } elseif ($cmd === '/webkey') {
            $key = webKey();
            $public = trim((string) (env('PUBLIC_URL') ?? env('RENDER_EXTERNAL_URL') ?? ''));
            $public = rtrim($public, '/');
            $url = $public !== '' ? $public . '/?key=' . $key : '/?key=' . $key;
            sendMessage($chat, "🔑 Ключ доступа к веб-странице управления\n\n"
                . "<code>{$key}</code>\n\nСтраница: {$url}\n\n"
                . "Сохраните ссылку в закладки телефона.");
        } elseif ($cmd === '/status') {
            $state = readState();
            $last = '';
            foreach ($state['days'] as $d => $info) {
                $last = $d;
            }
            sendMessage($chat, "Часовой пояс: " . tz()->getName() . "\n"
                . "Расписание: " . scheduleTime() . "\n"
                . "Каналов: " . count(channels()) . "\n"
                . "Последний отчёт: " . ($last ?: '— ещё не было —') . "\n"
                . "Воркер виден: " . ($state['worker']['last_seen'] ?? 'нет'));
        }
    } catch (Throwable $e) {
        @sendMessage($chat, '❌ ' . $e->getMessage());
    }
}

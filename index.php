<?php
/**
 * Веб-обработчик: страница управления с телефона + запуск задачи по кнопке.
 * Запускается встроенным сервером PHP: php -S 0.0.0.0:$PORT index.php
 */

declare(strict_types=1);

require __DIR__ . '/lib.php';

date_default_timezone_set(tz()->getName());

$path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';
$queryKey = (string) ($_GET['key'] ?? '');
$cookieKey = (string) ($_COOKIE['tgref_key'] ?? '');

// Ключ в адресе — запоминаем в куке, чтобы кнопки работали без него в URL
if ($queryKey !== '') {
    setcookie('tgref_key', $queryKey, [
        'expires'  => time() + 31536000,
        'path'     => '/',
        'httponly' => false,
        'samesite' => 'Lax',
    ]);
}

if ($path === '/health') {
    // Проверка живости: ничего тяжёлого, только файл состояния.
    // Заодно — если пора время, задача запускается прямо здесь:
    // тогда даже "спящий" сервис отработает по расписанию, если его будит пингер.
    try {
        maybeRunDaily();
    } catch (Throwable $e) {
        error_log('Задача дня: ' . $e->getMessage());
    }
    $state = readState();
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode([
        'ok'            => true,
        'time'          => nowIso(),
        'tz'            => tz()->getName(),
        'schedule'      => scheduleTime(),
        'channels'      => count(channels()),
        'chat_id_set'   => chatId() !== null,
        'worker_seen'   => $state['worker']['last_seen'] ?? null,
        'links'         => count($state['channels'] ?? []),
        'last_day'      => array_key_last($state['days'] ?? []) ?: null,
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

try {
    $key = webKey();
} catch (Throwable $e) {
    http_response_code(503);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(['error' => $e->getMessage()], JSON_UNESCAPED_UNICODE);
    exit;
}

if (!hash_equals($key, $queryKey !== '' ? $queryKey : $cookieKey)) {
    http_response_code(403);
    header('Content-Type: text/html; charset=utf-8');
    echo '<!doctype html><meta charset="utf-8"><title>Нужен ключ</title>'
        . '<body style="font-family:sans-serif;padding:24px;background:#0f1720;color:#e6edf3">'
        . '<h3>Нужен ключ доступа</h3><p>Добавьте к адресу <code>?key=...</code></p></body>';
    exit;
}

// Страница и кнопки — управление с телефона
if ($path === '/' || $path === '/index.php') {
    header('Content-Type: text/html; charset=utf-8');
    echo pageHtml();
    exit;
}

// Ручной запуск полного цикла
if ($path === '/run') {
    header('Content-Type: application/json; charset=utf-8');
    try {
        maybeRunDaily(); // если уже пора — выполнится здесь
        $res = runDaily(today(), true);
        echo json_encode(['ok' => true] + $res, JSON_UNESCAPED_UNICODE);
    } catch (Throwable $e) {
        http_response_code(500);
        echo json_encode(['ok' => false, 'error' => $e->getMessage()], JSON_UNESCAPED_UNICODE);
    }
    exit;
}

// Ссылки дня
if ($path === '/links') {
    header('Content-Type: application/json; charset=utf-8');
    $state = readState();
    $date = today();
    $out = [];
    foreach (channels() as $c) {
        $out[] = [
            'channel' => $c,
            'link'    => $state['channels'][$c]['link'] ?? null,
            'join'    => (int) ($state['channels'][$c]['join'] ?? 0),
            'date'    => $state['channels'][$c]['date'] ?? null,
        ];
    }
    echo json_encode(['date' => $date, 'links' => $out], JSON_UNESCAPED_UNICODE);
    exit;
}

http_response_code(404);
header('Content-Type: application/json; charset=utf-8');
echo json_encode(['error' => 'not found'], JSON_UNESCAPED_UNICODE);


function pageHtml(): string
{
    $state = readState();
    $last = $state['days'][array_key_last($state['days'] ?? [])] ?? null;
    $info = 'Отчётов пока нет.';
    if (is_array($last)) {
        $info = 'Последний отчёт: ' . ($last['sent_at'] ?? '') . ' — +' . (int) ($last['total'] ?? 0);
    }
    $worker = $state['worker']['last_seen'] ?? 'нет данных';
    return <<<HTML
<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Трекер реферальных ссылок</title>
<style>
 body{font-family:-apple-system,Roboto,"Segoe UI",sans-serif;background:#0f1720;color:#e6edf3;
      margin:0;padding:20px;max-width:540px;margin-inline:auto}
 h1{font-size:20px;margin:0 0 2px}
 .sub{color:#8b98a5;font-size:13px;margin-bottom:18px}
 button{display:block;width:100%;padding:17px;margin-bottom:12px;border:0;border-radius:12px;
        font-size:16px;font-weight:600;background:#2563eb;color:#fff}
 button.alt{background:#1f2937}
 #out{margin-top:14px;padding:14px;border-radius:12px;background:#111c26;font-size:14px;
      white-space:pre-wrap;word-break:break-word;display:none}
 .meta{margin-top:20px;font-size:12px;color:#6b7a88;line-height:1.7}
</style></head><body>
<h1>Трекер реферальных ссылок</h1>
<div class="sub">Управление с телефона</div>
<button onclick="post('/run')">🌙 Создать ссылки и прислать отчёт</button>
<button class="alt" onclick="post('/links')">🔗 Ссылки дня</button>
<div id="out"></div>
<div class="meta">
 {$info}<br>
 Воркер: {$worker}<br>
 Отчёты также приходят в Telegram: /daily, /links, /status
</div>
<script>
async function post(u){
 const b=document.getElementById('out'); b.style.display='block'; b.textContent='Выполняю…';
 try{const r=await fetch(u);const t=await r.text();let d;try{d=JSON.parse(t)}catch(e){d={report:t}}
  b.textContent=(d.report||d.links?JSON.stringify(d.links,null,1):null)||d.error||t;}
 catch(e){b.textContent='Ошибка: '+e}
}
</script>
</body></html>
HTML;
}

# Ограничение доступа перед реальными данными и AI

Демо Replimetr для жюри использует синтетические диалоги. В hosted-режиме
включается общий доступ по HTTP Basic для браузера и API. Имена автора и
ревьюера остаются подписями к оценкам.

## Текущий alwaysdata

Для сайта **User program** задайте `CQA_HOSTED_HTTP_USER` и
`CQA_HOSTED_HTTP_PASSWORD` в закрытом окружении процесса Uvicorn. Логин содержит
до 64 латинских букв, цифр, точек, дефисов или подчёркиваний. Для пароля
подходит `secrets.token_urlsafe(24)`. Пара переменных включает защиту всех
HTTP-маршрутов hosted-приложения, включая статику, отчёты и выгрузки.
Неполная или недопустимая пара останавливает запуск. Когда обе переменные
отсутствуют, локальный hosted-запуск остаётся доступен без запроса пароля.

В Web → Sites включите принудительный HTTPS и проверяйте логин только по HTTPS.
Установите ключ aijuju и путь к счётчику отдельно на сервере в файле окружения
с правами `600`. Проверьте 401 без логина и успешную загрузку с логином жюри
перед первым платным запросом. Передайте жюри адрес, логин и пароль по закрытому
каналу. Для отзыва доступа смените пароль и перезапустите сайт.
[Запуск на alwaysdata](alwaysdata.md) ·
[Настройки сайта и HTTPS](https://help.alwaysdata.com/en/docs/web-hosting/sites/add-a-site/).

## Подготовленная конфигурация для VM / управляемого nginx

Шаблон: [nginx-replimetr.conf](../examples/deployment/nginx-replimetr.conf).
Он предназначен для сервера с nginx, TLS-сертификатом и Uvicorn на
`127.0.0.1:8000`; это не файл для вставки в текущий alwaysdata User program.
Frontend и API работают на одном HTTPS origin, поэтому браузерный запрос пароля
не требует добавления секрета в JavaScript. Защищены также `/docs`, `/health`
и экспорт; обходного публичного порта Uvicorn быть не должно.

1. Подготовить TLS-сертификат, заменить example.invalid и пути в шаблоне.
2. Создать вне Git файл пользователей с хешами паролей через `htpasswd`.
   Для первого пользователя используется `-c`, для последующих — без `-c`.
   Пароль вводится интерактивно; не использовать `-b` с паролем в аргументах.
   Ограничить чтение файла администратором и worker-пользователем nginx.
3. Создать отдельные учётные данные команды и жюри. Все получают доступ ко всему
   демо; разграничение ролей и привязка авторов апелляций здесь не реализованы.
4. Проверить `nginx -t`, затем в тестовом окружении применить конфигурацию.
   Сетевой доступ к backend разрешить только через proxy.
5. Не записывать Authorization, request body и пароли в access/error logs.
   В браузере и CLI сразу использовать HTTPS: редирект не защищает пароль,
   уже отправленный первым HTTP-запросом.

Директивы и механизм: [NGINX Basic Auth](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html),
[reverse proxy](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).
В этой доработке nginx не устанавливался: `nginx -t` и работа proxy на сервере
ещё не проверены. Шаблон подготовлен для такой проверки, а не выдан за включённую защиту.

## Проверка перед открытием доступа

- Без credentials и с неверным паролем `/`, `/docs`, чтение наборов и POST
  экспорта возвращают 401; исходный backend недоступен извне напрямую.
- С учётной записью жюри открываются интерфейс, API и экспорт на синтетических
  данных; после перезагрузки страницы работа сохраняется.
- После отзыва тестового пользователя его старый пароль перестаёт работать.
- Прокси не повторяет автоматически POST и не обрывает оценку раньше серверного
  `CQA_EVALUATION_TIMEOUT`. При изменении 180 секунд согласовать proxy timeout.

Для CLI `backend.submission_remote` задаются `CQA_SUBMISSION_HTTP_USER` и
`CQA_SUBMISSION_HTTP_PASSWORD` в окружении процесса. Пример безопасного ввода
в PowerShell (пользователь вводит значения сам):

```powershell
$env:CQA_SUBMISSION_HTTP_USER = Read-Host 'Имя пользователя proxy'
$replimetrPassword = Read-Host 'Пароль proxy' -AsSecureString
$env:CQA_SUBMISSION_HTTP_PASSWORD = [System.Net.NetworkCredential]::new('', $replimetrPassword).Password
try {
    python -m backend.submission_remote --api https://YOUR_PROTECTED_HOST --dataset-id DATASET_UUID --manifest data/submission-dev.json
} finally {
    Remove-Item Env:CQA_SUBMISSION_HTTP_PASSWORD
    Remove-Item Env:CQA_SUBMISSION_HTTP_USER
    $replimetrPassword.Dispose()
}
```

CLI не перенаправляет credentials на другой адрес, не берёт их из `.netrc` и
не выводит тело ошибки proxy. Для Postman/curl используйте Basic Auth и HTTPS;
пароли, организаторские ссылки и AI-ключи не входят в экспортируемые коллекции.
Доступ жюри передаётся отдельно от публичного README; после защиты его можно отозвать.

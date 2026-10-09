# Локальное окружение

Нужны Linux/WSL, Python 3.12, uv, Git и Docker с Compose. Node 24 и Chromium запускаются
в контейнерах. Ревизия Minishop и образы закреплены в [dev/minishop.json](../dev/minishop.json),
[Dockerfile](../dev/Dockerfile) и [Compose](../dev/compose.yaml).

## Настройка

Из корня репозитория:

```bash
bash scripts/fetch-minishop.sh
bash scripts/setup.sh
bash scripts/check.sh
bash scripts/integration-check.sh
```

`fetch-minishop.sh` загружает Minishop 3.8.1 в `.local/minishop`, сверяет commit
и не перезаписывает существующий checkout. `MINISHOP_SOURCE` допускает другой
чистый checkout той же ревизии. Проверка `check-minishop.sh` выполняется перед
зависимыми командами. Другие плагины для разработки не требуются.

`setup.sh` создаёт `.venv`, устанавливает зависимости ядра, `requirements-dev.lock`,
плагин в editable-режиме и npm-пакеты по lock-файлу. Runtime-зависимости плагина
предоставляет Minishop. Для обновления Python lock:

```bash
bash scripts/lock-python.sh
```

`check.sh` запускает Ruff, строгий mypy, unit-тесты, TypeScript, сборку ESM,
DOM-тесты и проверку wheel с RU/EN. `integration-check.sh` использует PostgreSQL,
настоящие сервисы/DAL Minishop и HTTP-двойники панели/Telegram. Временная база
создаётся для прогона и удаляется после него. Конкурентные сценарии используют
разные соединения, остальные изолируются транзакциями/savepoint.

Исходники ядра подключаются только для чтения. Bytecode отключён, кеши и временные
файлы находятся в `.local/`. Зависимости не устанавливаются в checkout Minishop.

## Стенд и браузер

```bash
bash scripts/stand.sh up
bash scripts/stand.sh check
bash scripts/browser-check.sh
```

`up` собирает frontend и подписанный тестовый ZIP, проверяет его валидатором Minishop,
запускает PostgreSQL/Redis и штатные launcher-процессы backend/worker. Повторный запуск
загружает обновлённый ZIP с сохранением БД и plugin store. `check` создаёт синтетические
аккаунты, проверяет авторизацию, маршруты, UI-расширения и подписанные ресурсы.

Telegram, телеметрия, резервное копирование и автопродление на стенде отключены;
адрес рабочей панели не задан. PostgreSQL и Redis не публикуют порты на хост.
Проверка подписок с панелью выполняется интеграционными тестами.

После `check` доступны:

- [Web API](http://127.0.0.1:18081).
- [Пользовательский preview](http://127.0.0.1:18082/?audience=customer&language=ru).
- [Административный preview](http://127.0.0.1:18082/?audience=admin&language=en&theme=dark).

Preview загружает подписанные JS/CSS из Minishop и проксирует разрешённые GET-запросы
с тестовыми сессиями. Изменяющие браузерные сценарии используют синтетический API.
Параметры `audience`, `language`, `theme` и `view` выбирают поверхность, язык, тему
и представление. Скриншоты сохраняются в `.local/screenshots/`.
Порты задаются через `CORP_WEBAPP_PORT` и `CORP_PREVIEW_PORT`; доступ — только с loopback.
Тестовые пароли и фиксированные секреты предназначены исключительно для этого стенда.

Полная оболочка Minishop проверяется отдельно:

```bash
bash scripts/core-ui-check.sh
bash scripts/stand.sh down
```

Скрипт копирует отслеживаемые входные файлы frontend в `.local/core-ui` и собирает
оболочку там. Chromium использует настоящие страницы/API с синтетическими cookies.
Матрица проверяет RU/EN, темы и мобильный/настольный экраны. Настоящие провайдеры
входа и Telegram-клиенты этим не проверяются. `core-ui-check.sh` оставляет стенд
работающим; `down` останавливает проект `minishop-corp-dev`, сохраняя данные.

```bash
bash scripts/stand.sh logs --tail=100 backend worker
bash scripts/stand.sh ps
```

## Проверка пакета

```bash
bash scripts/package.sh init-key  # один раз, если ключа ещё нет
bash scripts/package.sh build
bash scripts/package-check.sh
```

Пакет издателя собирается отдельно от тестового пакета стенда. Постоянный ключ
хранится в `.local/publisher/`, тестовый — в `.local/package/`. Они не входят в Git.
Состав пакета и Actions описаны в [сборке](ci-packaging.md).

`package-check.sh` создаёт отдельный Compose-проект, чистые БД и plugin store в
`.local/package-check/<run>/`. Через штатный HTTP API проверяются установка, доверие
издателю, обновление, повтор установки, выключение/включение и сохранность данных.
Контейнеры останавливаются по завершении; данные и `host.log` сохраняются для разбора.
Версия для обновления синтетическая, с текущей схемой. Миграции отдельно проверяются
интеграционными тестами; произвольный откат будущих миграций не гарантируется.

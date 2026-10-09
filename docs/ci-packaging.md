# Сборка и публикация пакета

[Signed plugin package](../.github/workflows/ci.yml) запускается после push в любую
ветку и вручную через `workflow_dispatch`. Он собирает frontend, подписывает ZIP
постоянным ключом издателя и сохраняет результат в Artifacts на 30 дней.
На основной ветке репозитория отдельное задание коммитит установочные файлы в эту
же ветку. После успешной публикации Minishop устанавливает плагин по обычному URL
репозитория, без ручного скачивания архива.

Actions устанавливает только зависимости сборки плагина. Minishop, Docker, тесты
и стенды ему не нужны; проверки выполняются локально перед коммитом.

## Настройка GitHub

1. Репозиторий должен быть публичным, а его основная ветка — `main`.
2. Создайте постоянный ключ один раз либо используйте уже существующий:

   ```bash
   bash scripts/package.sh init-key
   ```

3. Добавьте **Settings → Secrets and variables → Actions → New repository secret**:
   `MINISHOP_CORP_SIGNING_KEY`, значение — base64 от 32 приватных байт Ed25519.
   С авторизованным GitHub CLI ключ передаётся без вывода на экран:

   ```bash
   base64 -w0 .local/publisher/minishop-corp.key | gh secret set MINISHOP_CORP_SIGNING_KEY
   ```

4. Разрешите `GITHUB_TOKEN` запись содержимого репозитория для задания `publish`.
   Правила защиты основной ветки должны разрешать этому заданию обычный push.
   Если правила запрещают запись, публикация завершится ошибкой; подписанный
   пакет останется в Artifacts. Отключать проверки или выполнять force-push workflow не будет.
5. После push дождитесь успешных заданий `package` и `publish`.
   Убедитесь, что в корне ветки появился `minishop-plugin.json` и доступен указанный ZIP.

Храните резервную копию ключа. Не меняйте его между выпусками: Minishop закрепляет
издателя при первой установке. `publisher.pub` — открытый ключ, для подписи не подходит.
Секрет получает только шаг подписи; он не записывается на диск и не входит в артефакт.
Отсутствующий или некорректный Secret останавливает сборку без генерации нового ключа.

## Формат репозитория

```text
minishop-plugin.json
packages/minishop-corp-0.1.6.zip
publisher.pub
SHA256SUMS
```

`minishop-plugin.json` содержит `schema_version`, относительный `artifact`, `sha256`
и `version`. Minishop сначала разрешает основную ветку в конкретный commit, затем
читает индекс и ZIP именно из этого commit. Поэтому они публикуются вместе.
Ссылка на GitHub Release или внешний Actions ZIP в поле `artifact` не подходит.

ZIP содержит Python backend, RU/EN и собранные JS/CSS. `plugin.json` внутри архива
описывает Plugin API, entry points, runtime, ревизию Minishop, публичный ключ
и хеши payload. `signatures/ed25519.sig` подписывает канонический manifest.
Приватный ключ, тесты, базы, кеши и исходные `.env` в пакет не входят.

Генератор берёт версию из `pyproject.toml` и проверяет её совпадение с
`backend/minishop_corp/__init__.py`. Для изменения поставляемых файлов увеличьте
версию в обоих местах. Опубликованный ZIP той же версии нельзя заменить другими
байтами: сборка завершится ошибкой. Старые архивы сохраняются; индекс указывает
на текущую версию. Изменение только документации даёт прежний пакет без нового
публикационного коммита.

Задание `publish` имеет право записи только на основной ветке. Оно проверяет,
что ветка всё ещё указывает на исходный commit сборки; устаревший запуск пропускается.
Обычный push дополнительно защищает от изменения ветки между проверкой и записью.
Force-push не используется. Коммит через `GITHUB_TOKEN` не запускает новую сборку
по событию push — это [поведение GitHub Actions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
Перед следующим локальным изменением подтяните созданный Actions коммит через
`git pull --ff-only` в чистом рабочем дереве.

## Локальная сборка

После [настройки окружения](development.md):

```bash
bash scripts/package.sh init-key  # только если ключа ещё нет
bash scripts/package.sh build
bash scripts/package-check.sh
```

Ключ находится в `.local/publisher/minishop-corp.key` с правами `0600`.
`package.sh build` проверяет checkout Minishop, TypeScript, собирает ESM и подписывает
пакет, затем вызывает штатный `inspect_archive`. Результат — `dist/release/`.
`--output` задаёт другой каталог внутри репозитория. Для проверки изменённого
неопубликованного кандидата используйте свежий каталог.

Автономная сборка, используемая Actions, требует Python 3.12 и Node 24:

```bash
python -m pip install -r requirements-build.txt
npm ci --ignore-scripts --no-audit --no-fund
npm run build
python scripts/build-package.py build --key-env MINISHOP_CORP_SIGNING_KEY --output .
```

Ключ передаётся через окружение. Эти команды не импортируют Minishop и не делают push;
публикацию в Actions выполняет отдельный [скрипт](../scripts/publish-package.sh).
Флаг `--verify-host` предназначен для локальной проверки с установленным Minishop.

## Ручное скачивание

В **Actions → Signed plugin package → запуск → Artifacts** скачайте
`minishop-corp-<SHA commit>` и распакуйте внешний архив GitHub. Загрузите в Minishop
внутренний `packages/minishop-corp-<версия>.zip`. Версия и SHA-256 видны в summary,
а открытый ключ и `SHA256SUMS` находятся рядом. Внешний Actions ZIP не является
установочным пакетом.

# Перенос бота VEDAME SPACE на сервер - инструкция для Никиты

О самом проекте - на чём написан, из каких файлов состоит - см.
[`README.md`](../README.md) в корне репозитория. Здесь - только шаги
переноса.

Код бота лежит в GitHub-репозитории: `alena-veda/veda-webinars-bot`.
У ассистента (Claude) есть доступ только на запись в этот репозиторий - не к
серверу. Всё, что ниже, выполняется тобой напрямую на сервере.

## 1. Доступ к репозиторию

Сейчас (2026-09-30) репозиторий **публичный** - клонировать можно сразу,
без ключей и паролей, шаг ниже. Если Алёна позже снова сделает его
приватным - тогда понадобится deploy-ключ:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/veda_bot_deploy_key -N ""
cat ~/.ssh/veda_bot_deploy_key.pub
```

Вывод последней команды (публичный ключ) отправляешь Алёне - она добавит
его в GitHub: Settings репозитория → Deploy keys → Add deploy key →
вставить → галочку "Allow write access" НЕ ставить (нужно только чтение).
И тогда команда клонирования на шаге 2 будет не по `https://`, а по
`git@github.com:...` с `GIT_SSH_COMMAND="ssh -i ~/.ssh/veda_bot_deploy_key"`
перед ней.

## 2. Склонировать репозиторий

```bash
sudo mkdir -p /opt/veda-webinars-bot
sudo chown $USER:$USER /opt/veda-webinars-bot
git clone https://github.com/alena-veda/veda-webinars-bot.git /opt/veda-webinars-bot
cd /opt/veda-webinars-bot
```

(Если репозиторий к этому моменту снова стал приватным - см. запасной
вариант с deploy-ключом в шаге 1, команда клонирования тогда другая:
`GIT_SSH_COMMAND="ssh -i ~/.ssh/veda_bot_deploy_key" git clone git@github.com:alena-veda/veda-webinars-bot.git /opt/veda-webinars-bot`)

## 3. Python и зависимости

Нужен Python 3.10 или новее.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 4. Секреты и база данных - НЕ через git, переносятся отдельно

Эти два файла в репозитории нарочно отсутствуют (см. `.gitignore`) - их
нужно перенести на сервер вручную, любым удобным защищённым способом
(Алёна пришлёт напрямую):

- `.env` - файл с реальным токеном бота, положить в корень проекта
  (`/opt/veda-webinars-bot/.env`), формат внутри: `BOT_TOKEN=...`
- `webinars.db` - текущая база данных с реальными подписчиками, тоже в
  корень проекта (`/opt/veda-webinars-bot/webinars.db`)

## 5. Настроить автозапуск (systemd)

В этой же папке лежит готовый файл `webinars-bot.service` - шаблон,
проверь и поправь пути (`WorkingDirectory`, `ExecStart`) и имя пользователя
(`User`) под реальную структуру на твоём сервере.

```bash
sudo cp webinars-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable webinars-bot
```

## 6. Момент запуска - ВАЖНО

Telegram не позволяет двум копиям бота с одним токеном опрашивать сервер
одновременно - будет ошибка конфликта у обеих. Порядок должен быть такой:

1. Останавливаем бота на компьютере Алёны (мы это сделаем в момент
   готовности со своей стороны).
2. Сразу следом запускаем на сервере:
   ```bash
   sudo systemctl start webinars-bot
   sudo systemctl status webinars-bot
   ```
3. Смотрим лог - должны появиться все 9 запланированных задач без ошибок
   ("Added job ..." x9, затем "Scheduler started" и "Bot started!"):
   ```bash
   journalctl -u webinars-bot -f
   ```
   (или `tail -f bot.log` в папке проекта - бот также пишет туда)

## 7. Дальнейшие обновления кода

Когда ассистент внесёт изменение в код, он отправит его в тот же
репозиторий. Чтобы применить его на сервере:

```bash
cd /opt/veda-webinars-bot
git pull
sudo systemctl restart webinars-bot
```

Это можно делать вручную по запросу, либо позже автоматизировать (cron
раз в несколько минут + перезапуск при изменениях) - не обязательно
делать сразу, можно начать с ручного варианта.

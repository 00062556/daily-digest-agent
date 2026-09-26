# Агент «Дайджест новостей» → Telegram

Каждое утро читает RSS новостей Казахстана и мира, выбирает главное,
пишет краткую сводку на русском и публикует её в Telegram-канал.

## 1. Telegram

1. В Telegram откройте @BotFather → `/newbot` → получите **токен**.
2. Добавьте бота в свой канал **администратором** с правом публиковать сообщения.
3. `TELEGRAM_CHAT_ID`:
   - для публичного канала: `@имя_канала`;
   - для приватного: числовой id вида `-100…` (перешлите пост из канала боту @userinfobot).

## 2. Локальная проверка

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
DRY_RUN=1 python news_agent.py        # только печатает дайджест, ничего не отправляет

export TELEGRAM_BOT_TOKEN="123:ABC..."
export TELEGRAM_CHAT_ID="@my_channel"
python news_agent.py                  # публикует в канал
```

## 3. Ежедневный запуск через GitHub Actions (бесплатно, компьютер можно не включать)

1. Создайте **приватный** репозиторий на GitHub и загрузите туда все файлы, включая папку `.github`.
2. Settings → Secrets and variables → Actions → New repository secret:
   `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. Actions → Daily news digest → **Run workflow**: первый запуск вручную для проверки.
4. Дальше запуск идёт сам в 08:50 по Алматы. Время меняется в `cron` в файле
   `.github/workflows/daily-digest.yml` (время указывается в UTC).

## Настройка

- **Источники:** словарь `SOURCES` в `news_agent.py`, одна строка на RSS-ленту.
- **Темы, количество новостей и формат:** промпт `SYSTEM`.
- **Период:** переменная `HOURS` (по умолчанию 24 часа).

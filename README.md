# Discord → Telegram Forwarder

Пересылает сообщения из Discord в Telegram.

## Настройка .env

```
# Discord User Token (F12 → Network → любой запрос → Authorization)
DISCORD_TOKEN=твой_user_token
TELEGRAM_BOT_TOKEN=токен_от_@BotFather
ADMIN_TG_ID=твой_telegram_id
```

## Запуск

```bash
pip install -r requirements.txt
python bot.py
```

В Telegram: `/start` → `/channels` → `/add <channel_id>` → `/target <chat_id>`

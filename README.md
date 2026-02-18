# MedBot ERP Pro 🏥

Система сканування документів та звітності через Discord Webhook.

## Встановлення
1. Завантажте файли на GitHub.
2. Розгорніть на Streamlit Cloud.
3. Додайте Secrets в налаштуваннях Streamlit Cloud (див. нижче).

## Secrets формат:
```toml
[discord]
DISCORD_CLIENT_ID = "ваш_ід"
DISCORD_CLIENT_SECRET = "ваш_секрет"
DISCORD_REDIRECT_URI = "[https://ваша-адреса.streamlit.app](https://ваша-адреса.streamlit.app)"
DISCORD_WEBHOOK_URL = "[https://discord.com/api/webhooks/](https://discord.com/api/webhooks/)..."
GUILD_ID = "ід_сервера"
ADMIN_ROLE_ID = "ід_ролі_адміна"
ALLOWED_ROLE_ID = "ід_ролі_доступу"
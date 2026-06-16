# Таро-бот для Telegram

Бот для раскладов таро с интеграцией DeepSeek API. Пользователям не нужно регистрироваться или логиниться в DeepSeek — все взаимодействие происходит через бот.

## Установка и запуск

1. Создайте виртуальное окружение (рекомендуется):
```bash
python -m venv venv
venv\Scripts\activate  # Windows
source venv/bin/activate  # Linux/macOS
```

2. Установите зависимости:
```bash
pip install -r requirements.txt
```

3. Создайте файл `.env` и заполните его данными (смотрите `.env.example`):
```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
DEEPSEEK_API_KEY=your_deepseek_api_key_here
ADMIN_USER_IDS=123456789,987654321
ADMIN_CARD_NUMBER=1234 5678 9012 3456
READING_COST=100
```

4. Получите токен бота в @BotFather в Telegram
5. Получите API-ключ DeepSeek на https://platform.deepseek.com/
6. Найдите Telegram ID для всех администраторов (например, через @userinfobot)
7. Запустите бота:
```bash
python bot.py
```

## Функционал

- 🔮 Расклады таро по 5 наводящим вопросам
- 💵 Пополнение баланса через перевод на карту (подтверждение любым админом)
- 📊 Админ-панель с статистикой продаж
- 💰 Управление балансом пользователей
- Поддержка нескольких администраторов

## Админ-команды

- `/stats` - просмотр статистики
- `/approve_<id>` - подтвердить запрос на пополнение
- `/reject_<id>` - отклонить запрос на пополнение

import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '').strip()
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_MODEL = "gpt-4o-mini"

READING_COST = int(os.getenv('READING_COST', '100'))
INDIVIDUAL_READING_COST = int(os.getenv('INDIVIDUAL_READING_COST', '2000'))
PALM_READING_COST = int(os.getenv('PALM_READING_COST', '1000'))

admin_ids_str = os.getenv('ADMIN_USER_IDS', '')
ADMIN_USER_IDS = [int(id_str.strip()) for id_str in admin_ids_str.split(',') if id_str.strip()]

MASTER_USERNAME = os.getenv('MASTER_USERNAME', '').strip()

YOOKASSA_SHOP_ID = os.getenv('YOOKASSA_SHOP_ID', '').strip()
YOOKASSA_SECRET_KEY = os.getenv('YOOKASSA_SECRET_KEY', '').strip()
WEB_SERVER_PORT = int(os.getenv('WEB_SERVER_PORT', '8000'))
LOG_CHAT_ID = os.getenv('LOG_CHAT_ID', '').strip()

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

ROBOKASSA_LOGIN = os.getenv('ROBOKASSA_LOGIN', '').strip()
ROBOKASSA_PASSWORD1 = os.getenv('ROBOKASSA_PASSWORD1', '').strip()
ROBOKASSA_PASSWORD2 = os.getenv('ROBOKASSA_PASSWORD2', '').strip()
ROBOKASSA_TEST_MODE = os.getenv('ROBOKASSA_TEST_MODE', 'true').lower() == 'true'
ROBOKASSA_SIGNATURE_ALGORITHM = os.getenv('ROBOKASSA_SIGNATURE_ALGORITHM', 'md5').lower()
WEB_SERVER_PORT = int(os.getenv('WEB_SERVER_PORT', '8000'))

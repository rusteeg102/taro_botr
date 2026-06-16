
import os
import hashlib
import urllib.parse
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

ROBOKASSA_LOGIN = os.getenv('ROBOKASSA_LOGIN', '').strip()
ROBOKASSA_PASSWORD1 = os.getenv('ROBOKASSA_PASSWORD1', '').strip()
ROBOKASSA_TEST_MODE = os.getenv('ROBOKASSA_TEST_MODE', 'true').lower() == 'true'
ROBOKASSA_SIGNATURE_ALGORITHM = os.getenv('ROBOKASSA_SIGNATURE_ALGORITHM', 'md5').lower()

# Тестовые данные
test_amount = 100.00
test_order_id = 123
test_user_id = 456

print("=== Тестирование Robokassa ===")
print(f"Login: {ROBOKASSA_LOGIN}")
print(f"Password1: {ROBOKASSA_PASSWORD1[:10]}...")
print(f"Test Mode: {ROBOKASSA_TEST_MODE}")
print(f"Algorithm: {ROBOKASSA_SIGNATURE_ALGORITHM}")
print()

# Формирование подписи
out_sum = f"{test_amount:.2f}"
inv_id = str(test_order_id)
inv_desc = f"Пополнение баланса на {out_sum} руб."

signature_str = f"{ROBOKASSA_LOGIN}:{out_sum}:{inv_id}:{ROBOKASSA_PASSWORD1}"

print(f"Signature String: {signature_str}")

if ROBOKASSA_SIGNATURE_ALGORITHM == 'sha256':
    signature = hashlib.sha256(signature_str.encode('utf-8')).hexdigest().upper()
else:
    signature = hashlib.md5(signature_str.encode('utf-8')).hexdigest().upper()

print(f"Signature: {signature}")
print()

# Формируем ссылку
params = {
    'MrchLogin': ROBOKASSA_LOGIN,
    'OutSum': out_sum,
    'InvId': inv_id,
    'SignatureValue': signature,
    'InvDesc': inv_desc
}

if ROBOKASSA_TEST_MODE:
    params['IsTest'] = '1'

base_url = "https://auth.robokassa.ru/Merchant/Index.aspx"
payment_url = f"{base_url}?{urllib.parse.urlencode(params)}"

print("=== Сгенерированная ссылка ===")
print(payment_url)
print()
print("=== Проверка подписи в личном кабинете Robokassa ===")
print("1. Перейдите в Мой магазин -> Настройки -> Проверка подписи")
print(f"2. Вставьте туда строку: {signature_str}")
print(f"3. Выберите алгоритм: {ROBOKASSA_SIGNATURE_ALGORITHM.upper()}")
print(f"4. Сравните результат с подписью: {signature}")

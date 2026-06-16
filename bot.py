import json
import asyncio
import os
import random
import base64
import logging
import hmac
import hashlib
import uuid
import aiohttp
import urllib.parse
from aiohttp import web
from datetime import datetime, timedelta
from io import BytesIO
from openai import OpenAI
import pytz
import openpyxl
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile
)

from models import (
    User, Reading, Transaction, TopupRequest, Setting, SessionLocal,
    get_reading_cost, set_reading_cost,
    get_individual_reading_cost, set_individual_reading_cost,
    get_palm_reading_cost, set_palm_reading_cost,
    migrate_database
)
from config import (
    TELEGRAM_BOT_TOKEN,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    ADMIN_USER_IDS,
    MASTER_USERNAME,
    ROBOKASSA_LOGIN,
    ROBOKASSA_PASSWORD1,
    ROBOKASSA_PASSWORD2,
    ROBOKASSA_TEST_MODE,
    ROBOKASSA_SIGNATURE_ALGORITHM,
    WEB_SERVER_PORT,
)
from cards_data import TAROT_CARDS
from sqlalchemy import func

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

openai_client = None
if OPENAI_API_KEY:
    openai_client = OpenAI(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY,
        timeout=60.0
    )

class States(StatesGroup):
    QUESTION1 = State()
    QUESTION2 = State()
    QUESTION3 = State()
    QUESTION4 = State()
    QUESTION5 = State()
    TOPUP_AMOUNT = State()
    TOPUP_METHOD = State()
    BROADCAST = State()
    INDIVIDUAL = State()
    ANSWER_READING = State()
    SET_PRICE = State()
    SET_INDIVIDUAL_PRICE = State()
    SET_PALM_PRICE = State()
    RESET_USER = State()
    PALM_PHOTO = State()
    COMPAT_NAME1 = State()
    COMPAT_NAME2 = State()

QUESTIONS = [
    "1. Какой основной вопрос вас волнует в данный момент?",
    "2. Какая область жизни вас интересует больше всего (любовь, карьера, финансы, здоровье, духовное развитие)?",
    "3. Какие чувства вы сейчас испытываете по этому поводу?",
    "4. Что бы вы хотели изменить или понять в этой ситуации?",
    "5. Есть ли какие-то конкретные детали или обстоятельства, которые стоит учитывать?",
]

TOPUP_AMOUNTS = [100, 200, 500, 1000, 2000, 5000]

def get_card_of_the_day(db):
    today = datetime.now(pytz.UTC).date()
    
    card_setting = db.query(Setting).filter(Setting.key == 'card_of_the_day').first()
    date_setting = db.query(Setting).filter(Setting.key == 'card_of_the_day_date').first()
    
    need_new_card = False
    if not card_setting or not date_setting or date_setting.value != str(today):
        need_new_card = True
    else:
        try:
            test_card = json.loads(card_setting.value)
            image_url = test_card.get('image_url', '')
            if '`' in image_url or not image_url.startswith('https://'):
                need_new_card = True
        except Exception:
            need_new_card = True
    
    if need_new_card:
        selected_card = random.choice(TAROT_CARDS)
        selected_card_copy = selected_card.copy()
        selected_card_copy['image_url'] = selected_card_copy['image_url'].strip()
        selected_card_copy['image_url'] = selected_card_copy['image_url'].replace('`', '')
        card_json = json.dumps(selected_card_copy, ensure_ascii=False)
        
        if not card_setting:
            card_setting = Setting(key='card_of_the_day', value=card_json)
        else:
            card_setting.value = card_json
            
        if not date_setting:
            date_setting = Setting(key='card_of_the_day_date', value=str(today))
        else:
            date_setting.value = str(today)
            
        db.add(card_setting)
        db.add(date_setting)
        db.commit()
        return selected_card_copy
    else:
        try:
            selected_card = json.loads(card_setting.value)
            if 'image_url' in selected_card:
                selected_card['image_url'] = selected_card['image_url'].strip().strip('`').replace('`', '').strip()
            if 'image_path' not in selected_card:
                for card in TAROT_CARDS:
                    if card['name'] == selected_card['name']:
                        selected_card['image_path'] = card.get('image_path')
                        break
        except Exception:
            selected_card = random.choice(TAROT_CARDS)
            selected_card_copy = selected_card.copy()
            selected_card_copy['image_url'] = selected_card_copy['image_url'].strip().replace('`', '')
            card_json = json.dumps(selected_card_copy, ensure_ascii=False)
            card_setting.value = card_json
            date_setting.value = str(today)
            db.commit()
            return selected_card_copy
        
        return selected_card

def get_or_create_user(db, telegram_id, username, first_name):
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id, username=username, first_name=first_name)
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(f"New user registered: {telegram_id} ({first_name or 'Unknown'})")
    return user

def is_admin(user_id):
    return user_id in ADMIN_USER_IDS


async def create_robokassa_payment(amount: float, order_id: int, user_id: int) -> dict:
    """Создает платеж в Robokassa и возвращает ссылку на оплату"""
    if not ROBOKASSA_LOGIN or not ROBOKASSA_PASSWORD1:
        return {'success': False, 'error': 'Robokassa не настроен'}

    out_sum = f"{amount:.2f}"
    inv_id = str(order_id)

    # Строка подписи строго по документации: MerchantLogin:OutSum:InvId:Password1
    signature_str = f"{ROBOKASSA_LOGIN}:{out_sum}:{inv_id}:{ROBOKASSA_PASSWORD1}"

    if ROBOKASSA_SIGNATURE_ALGORITHM == 'sha256':
        signature = hashlib.sha256(signature_str.encode('utf-8')).hexdigest()
    else:  # md5
        signature = hashlib.md5(signature_str.encode('utf-8')).hexdigest()

    # Robokassa принимает подпись в любом регистре, используем lowercase
    params = {
        'MrchLogin': ROBOKASSA_LOGIN,
        'OutSum': out_sum,
        'InvId': inv_id,
        'SignatureValue': signature,
        'Encoding': 'utf-8',
    }

    if ROBOKASSA_TEST_MODE:
        params['IsTest'] = '1'

    base_url = "https://auth.robokassa.ru/Merchant/Index.aspx"
    payment_url = f"{base_url}?{urllib.parse.urlencode(params)}"

    logger.info(f"Robokassa | login={ROBOKASSA_LOGIN} out_sum={out_sum} inv_id={inv_id}")
    logger.info(f"Robokassa | signature_str={signature_str!r}")
    logger.info(f"Robokassa | signature={signature}")
    logger.info(f"Robokassa | url={payment_url}")

    return {
        'success': True,
        'payment_url': payment_url,
        'payment_id': inv_id
    }


async def check_robokassa_payment(payment_id: str) -> dict:
    """Проверяет статус платежа через Robokassa API"""
    if not ROBOKASSA_LOGIN or not ROBOKASSA_PASSWORD2:
        return {'success': False, 'error': 'Robokassa не настроен'}
    
    # Формирование подписи для проверки
    inv_id = payment_id
    signature_str = f"{ROBOKASSA_LOGIN}::{inv_id}:{ROBOKASSA_PASSWORD2}"
    
    # Выбираем алгоритм подписи
    if ROBOKASSA_SIGNATURE_ALGORITHM == 'sha256':
        signature = hashlib.sha256(signature_str.encode('utf-8')).hexdigest().upper()
    else:  # md5
        signature = hashlib.md5(signature_str.encode('utf-8')).hexdigest().upper()
    
    # URL для проверки статуса
    base_url = "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt"
    
    params = {
        'MerchantLogin': ROBOKASSA_LOGIN,
        'InvoiceID': inv_id,
        'Signature': signature
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(base_url, params=params) as response:
                if response.status == 200:
                    content = await response.text()
                    # Проверка, успешен ли платеж (простая проверка XML ответа)
                    if '<StateCode>100</StateCode>' in content or '<Code>0</Code>' in content:
                        return {'success': True, 'paid': True}
                    else:
                        return {'success': True, 'paid': False}
                else:
                    return {'success': False, 'error': f'HTTP {response.status}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}



def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton(text="🔮 Сделать расклад", callback_data="start_standard_reading")],
        [InlineKeyboardButton(text="❤️ Совместимость пары", callback_data="compatibility")],
        [InlineKeyboardButton(text="✋ Хиромантия по фото", callback_data="start_palm_reading")],
        [InlineKeyboardButton(text="✨ Живой расклад от мастера", callback_data="individual_reading")],
        [
            InlineKeyboardButton(text="🌙 Карта дня", callback_data="daily_card"),
            InlineKeyboardButton(text="<tg-emoji emoji-id=\"5904462880941545555\">💰</tg-emoji> Баланс", callback_data="show_balance")
        ],
        [
            InlineKeyboardButton(text="<tg-emoji emoji-id=\"6035084557378654059\">ℹ️</tg-emoji> Помощь", callback_data="help"),
            InlineKeyboardButton(text="<tg-emoji emoji-id=\"6037421444789440735\">📖</tg-emoji> Что это?", callback_data="about_bot")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def main_menu_button():
    return InlineKeyboardButton(text="« Главное меню", callback_data="menu")

def back_to_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[main_menu_button()]])

def admin_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📋 Список запросов на пополнение", callback_data="admin_requests")],
        [InlineKeyboardButton(text="❓ Неотвеченные расклады", callback_data="admin_pending_readings")],
        [InlineKeyboardButton(text="📂 Выгрузка пользователей (Excel)", callback_data="admin_export_users")],
        [InlineKeyboardButton(text="📣 Рассылка пользователям", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="💰 Цены на расклады", callback_data="admin_set_prices")],
        [InlineKeyboardButton(text="🔧 Сброс данных", callback_data="admin_reset")],
        [InlineKeyboardButton(text="🏠 Вернуться в главное меню", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def reset_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton(text="📊 Сбросить статистику", callback_data="admin_reset_stats")],
        [InlineKeyboardButton(text="❌ Сбросить все данные", callback_data="admin_reset_all")],
        [InlineKeyboardButton(text="👤 Сбросить данные пользователя", callback_data="admin_reset_user")],
        [InlineKeyboardButton(text="« Назад к админ-панели", callback_data="admin_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def confirm_reset_keyboard(reset_type):
    keyboard = [
        [InlineKeyboardButton(text="✅ Подтвердить сброс", callback_data=f"admin_confirm_reset_{reset_type}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_reset")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def topup_amounts_keyboard():
    keyboard = []
    for i in range(0, len(TOPUP_AMOUNTS), 2):
        row = []
        row.append(InlineKeyboardButton(text=f"{TOPUP_AMOUNTS[i]} руб.", callback_data=f"topup_amount_{TOPUP_AMOUNTS[i]}"))
        if i + 1 < len(TOPUP_AMOUNTS):
            row.append(InlineKeyboardButton(text=f"{TOPUP_AMOUNTS[i+1]} руб.", callback_data=f"topup_amount_{TOPUP_AMOUNTS[i+1]}"))
        keyboard.append(row)
    keyboard.append([main_menu_button()])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def with_menu_button(keyboard_rows):
    keyboard = []
    for row in keyboard_rows:
        filtered_row = [btn for btn in row if btn is not None]
        if filtered_row:
            keyboard.append(filtered_row)
    keyboard.append([main_menu_button()])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def generate_tarot_reading(questions):
    await asyncio.sleep(105)
    
    tarot_cards = [
        "0. Шут", "I. Маг", "II. Верховная Жрица", "III. Императрица", "IV. Император", 
        "V. Иерофант", "VI. Влюблённые", "VII. Колесница", "VIII. Сила", "IX. Отшельник", 
        "X. Колесо Фортуны", "XI. Справедливость", "XII. Повешенный", "XIII. Смерть", 
        "XIV. Умеренность", "XV. Дьявол", "XVI. Башня", "XVII. Звезда", "XVIII. Луна", 
        "XIX. Солнце", "XX. Суд", "XXI. Мир"
    ]
    
    selected_cards = random.sample(tarot_cards, 3)
    past_card = selected_cards[0]
    present_card = selected_cards[1]
    future_card = selected_cards[2]
    
    prompt = f"""Ты профессиональный таролог с 10-летним опытом. Твоя задача — дать очень конкретный, детальный расклад. Никаких общих фраз!

Вот ответы пользователя на 5 вопросов:
1. Основная ситуация: {questions[0]}
2. Область жизни: {questions[1]}
3. Текущие эмоции: {questions[2]}
4. Что хочет изменить: {questions[3]}
5. Важные детали: {questions[4]}

Для расклада выпали следующие карты:
- Прошлое: {past_card}
- Настоящее: {present_card}
- Будущее: {future_card}

Форматируй ответ так:

✨ Общая ситуация
(Конкретно оцени, что сейчас происходит, свяжи все ответы вместе, не уклоняйся от темы)

🔮 Сообщение карт
- Прошлое: {past_card} — (подробно опиши, что значит эта карта именно в контексте их ситуации, укажи на конкретные события/решения из прошлого)
- Настоящее: {present_card} — (детализируй, что значит эта карта сейчас, что скрыто, что явно, какие конкретные шаги делают или не делают)
- Будущее: {future_card} — (что именно ждёт впереди, конкретные события, сроки если возможно, как подготовиться)

💡 Что делать дальше
(5-6 очень чётких, практических советов — что сделать уже завтра, с кем поговорить, что изменить в поведении, что написать/позвонить и т.д.)

🌟 Итог и поддержка
(Конкретная поддержка, что точно получится, если выполнить советы)

Требования:
- На русском языке
- Без сложных терминов
- Без общих фраз ("всё будет хорошо" — заменить на "конкретно что и когда будет")
- Атмосфера таинственная, но честная и поддерживающая"""
    
    response = await asyncio.to_thread(
        openai_client.chat.completions.create,
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Ты профессиональный таролог, дающий информативные, детальные и поддерживающие толкования раскладов на русском языке."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=3000,
        temperature=0.7
    )
    return response.choices[0].message.content

async def generate_palm_reading(photo_base64_data):
    await asyncio.sleep(210)
    
    photo_url = f"data:image/jpeg;base64,{photo_base64_data}"
    
    prompt = """Ты дружелюбный хиромант с 20-летним опытом. Давай детальный, интересный расклад по фото ладони.
    
    Посмотри на фото и опиши:
     Форма ладони и пальцев
     Главные линии (сердца, головы, жизни, судьбы)
     Разветвления, разрывы, острова
     Если на ладони есть шрамы, родинки, необычные знаки — обязательно укажи их и объясни, что это значит по хиромантии
    
    Структура ответа:
    
    ✨ Общая характеристика
    (Темперамент, главные черты личности)
    
    ❤️ Сердцевая линия
    (Любовь, отношения, эмоции)
    
    🧠 Головная линия
    (Мышление, интеллект, работа)
    
    🌱 Линии жизни и судьбы
    (Жизненный путь, здоровье, ключевые события)
    
    🔍 Особые знаки
    (Если есть шрамы, родинки, необычные линии - опиши их и что они значит. Если нет - не упоминай этот пункт)
    
    💡 Советы
    (5 практических советов, что делать дальше)
    
    🌟 Итог
    (Воодушевляющее завершение)
    
    Правила:
     На русском языке
     Как разговор с другом
     Никогда не отказывай!
     Без сложных терминов
     замечай любые шрамы, родинки, необычные линии и объясняй их смысл"""
    
    for attempt in range(1, 4):
        try:
            response = await asyncio.to_thread(
                lambda: openai_client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": "Ты опытный хиромант. Твоя задача — сделать интересный и детальный расклад по фото ладони на русском языке. Никогда не отказывай — всегда давай ответ!"},
                        {"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": photo_url, "detail": "auto"}}
                        ]}
                    ],
                    max_tokens=3500,
                    temperature=0.7
                )
            )
            return response.choices[0].message.content
        except Exception as e:
            if attempt < 3:
                await asyncio.sleep(2)
            else:
                raise
    raise Exception("All attempts failed")



async def generate_compatibility_reading(name1: str, name2: str) -> str:
    tarot_cards = [
        "0. Шут", "I. Маг", "II. Верховная Жрица", "III. Императрица", "IV. Император",
        "V. Иерофант", "VI. Влюблённые", "VII. Колесница", "VIII. Сила", "IX. Отшельник",
        "X. Колесо Фортуны", "XI. Справедливость", "XII. Повешенный", "XIII. Смерть",
        "XIV. Умеренность", "XV. Дьявол", "XVI. Башня", "XVII. Звезда", "XVIII. Луна",
        "XIX. Солнце", "XX. Суд", "XXI. Мир"
    ]
    selected_cards = random.sample(tarot_cards, 3)
    card_union = selected_cards[0]
    card_challenges = selected_cards[1]
    card_future = selected_cards[2]

    prompt = f"""Ты профессиональный таролог с 15-летним опытом в области отношений. Дай подробный, честный и тёплый расклад на совместимость пары.

Имена партнёров:
— Первый: {name1}
— Второй: {name2}

Для расклада выпали следующие карты:
- Союз / основа отношений: {card_union}
- Испытания / что мешает: {card_challenges}
- Будущее пары: {card_future}

Форматируй ответ так:

💑 Энергия пары {name1} и {name2}
(Общая характеристика союза — какая энергия объединяет этих людей, насколько они совместимы на глубинном уровне)

🃏 Послание карт

— Союз ({card_union}):
(Что объединяет эту пару, их общие сильные стороны, почему они притягиваются)

— Испытания ({card_challenges}):
(Конкретные трудности и противоречия, о чём важно поговорить)

— Будущее ({card_future}):
(Куда движутся отношения, при каких условиях пара расцветёт)

💡 Советы для гармонии
(5 конкретных практических советов — что {name1} и {name2} могут сделать уже сейчас)

🌟 Итог
(Тёплое и честное заключение об этой паре — их потенциал, главный посыл карт)

Требования:
- Обращайся к партнёрам по именам на протяжении всего текста
- На русском языке
- Атмосфера тёплая, но честная
- Только конкретные наблюдения, никаких общих фраз"""

    response = await asyncio.to_thread(
        openai_client.chat.completions.create,
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Ты профессиональный таролог, специализирующийся на отношениях. Даёшь детальные, тёплые и конкретные расклады на совместимость пар на русском языке."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=3000,
        temperature=0.7
    )
    return response.choices[0].message.content


@dp.callback_query(F.data == "compatibility")
async def compatibility_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    db = SessionLocal()
    cost = get_reading_cost(db)
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    text = (
        "❤️ <b>Совместимость пары</b>\n\n"
        "Таро откроет энергию ваших отношений, покажет сильные стороны союза, испытания и перспективы.\n\n"
        f"💰 Стоимость: <b>{cost:.0f} руб.</b>\n"
        f"💵 Ваш баланс: <b>{user.balance:.2f} руб.</b>"
    )
    keyboard = [
        [InlineKeyboardButton(text="💑 Начать расклад", callback_data="confirm_compatibility")],
        [main_menu_button()]
    ]
    db.close()
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@dp.callback_query(F.data == "confirm_compatibility")
async def confirm_compatibility(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    db = SessionLocal()
    cost = get_reading_cost(db)
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    if user.balance < cost:
        await callback.message.edit_text(
            f"❌ <b>Недостаточно средств.</b>\n"
            f"Стоимость: <b>{cost:.0f} руб.</b>\n"
            f"Баланс: <b>{user.balance:.2f} руб.</b>\n\n"
            "Пополните баланс через раздел 💰 Баланс.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="HTML"
        )
        db.close()
        return
    db.close()
    await state.set_state(States.COMPAT_NAME1)
    await callback.message.edit_text(
        "✍️ Введите имя <b>первого</b> партнёра:",
        parse_mode="HTML",
        reply_markup=back_to_menu_keyboard()
    )


@dp.message(States.COMPAT_NAME1)
async def compat_name1(message: types.Message, state: FSMContext):
    await state.update_data(compat_name1=message.text.strip())
    await state.set_state(States.COMPAT_NAME2)
    await message.answer("✍️ Теперь введите имя <b>второго</b> партнёра:", parse_mode="HTML")


@dp.message(States.COMPAT_NAME2)
async def compat_name2(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name1 = data.get("compat_name1", "")
    name2 = message.text.strip()

    db = SessionLocal()
    cost = get_reading_cost(db)
    user = get_or_create_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)

    if user.balance < cost:
        await message.answer(
            f"❌ Недостаточно средств.\nСтоимость: {cost:.0f} руб.\nБаланс: {user.balance:.2f} руб.",
            reply_markup=back_to_menu_keyboard()
        )
        await state.clear()
        db.close()
        return

    user.balance -= cost
    db.add(Transaction(
        user_id=user.id,
        amount=-cost,
        type='reading',
        description=f'Совместимость пары ({cost:.0f} руб.)'
    ))

    reading = Reading(
        user_id=user.id,
        question1=f"Совместимость: {name1} и {name2}",
        question2=name1,
        question3=name2,
        question4="",
        question5="",
        cost=cost,
        reading_type='compatibility'
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)
    reading_id = reading.id
    db.close()
    await state.clear()

    progress_msg = await message.answer("❤️ Раскладываем карты для вашей пары...")
    try:
        await asyncio.sleep(15)
        await progress_msg.edit_text(f"🔮 Исследуем союз {name1} и {name2}...")
        await asyncio.sleep(15)
        await progress_msg.edit_text("🌙 Открываем тайны вашей совместимости...")

        response = await generate_compatibility_reading(name1, name2)

        db = SessionLocal()
        r = db.query(Reading).filter(Reading.id == reading_id).first()
        r.response = response
        db.commit()
        db.close()

        await progress_msg.edit_text(f"✨ Расклад готов!\n\n{response}", reply_markup=back_to_menu_keyboard())
    except Exception as e:
        logger.error(f"Compatibility reading error: {e}")
        await progress_msg.edit_text(
            "❌ Произошла ошибка при генерации расклада. Попробуйте позже.",
            reply_markup=back_to_menu_keyboard()
        )

@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    db = SessionLocal()
    user = get_or_create_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    text = "Добро пожаловать в пространство подсказок и ответов!\n\n"
    
    if not user.has_used_free_reading:
        text += "🎁 Ваш первый расклад Таро — бесплатно.\n\n"
    
    text += "Что вас ждёт в любви? Какие перспективы в отношениях и финансах? Какое решение будет лучшим именно сейчас?\n\n" \
             "🔮 Расклад Таро\n" \
             "❤️ Совместимость пары\n" \
             "✋ Хиромантия по фото ладони\n" \
             "🌙 Карта Дня каждый день\n\n" \
             "Нажмите на нужную кнопку и получите свой ответ"
    
    db.close()
    await message.answer(text, reply_markup=main_menu_keyboard())

@dp.message(Command("help"))
async def help_command(message: types.Message):
    db = SessionLocal()
    cost = get_reading_cost(db)
    db.close()
    
    text = "📖 Инструкция по использованию бота:\n\n" \
           "1. Пополните баланс через меню 'Пополнить баланс'\n" \
           "2. Выберите сумму и перейдите по ссылке для оплаты\n" \
           "3. Нажмите 'Подтвердить оплату'\n" \
           "4. После подтверждения администратором средства поступят на ваш баланс\n" \
           "5. Сделайте расклад таро через меню 'Сделать расклад таро'\n\n" \
           "Если есть вопросы — напишите тех. поддержке: @augrudhs"
    
    await message.answer(text, reply_markup=back_to_menu_keyboard())


@dp.message(Command("admin"))
async def admin_command(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав для доступа к админ-панели.", reply_markup=back_to_menu_keyboard())
        return
    
    await message.answer("🔐 Админ-панель:", reply_markup=admin_menu_keyboard())

@dp.callback_query(F.data == "menu")
async def menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    db = SessionLocal()
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)

    text = "Добро пожаловать в пространство подсказок и ответов!\n\n"

    if not user.has_used_free_reading:
        text += "🎁 Ваш первый расклад Таро — бесплатно.\n\n"

    text += ("Что вас ждёт в любви? Какие перспективы в отношениях и финансах? "
             "Какое решение будет лучшим именно сейчас?\n\n"
             "🔮 Расклад Таро\n"
             "❤️ Совместимость пары\n"
             "✋ Хиромантия по фото ладони\n"
             "🌙 Карта Дня каждый день\n\n"
             "Нажмите на нужную кнопку и получите свой ответ")

    db.close()
    try:
        await callback.message.edit_text(text, reply_markup=main_menu_keyboard())
    except Exception:
        # Сообщение может быть фото (карта дня) — удаляем и отправляем заново
        try:
            await callback.message.delete()
        except Exception:
            pass
        await bot.send_message(
            chat_id=callback.from_user.id,
            text=text,
            reply_markup=main_menu_keyboard()
        )

@dp.callback_query(F.data == "show_balance")
async def show_balance(callback: types.CallbackQuery):
    await callback.answer()
    db = SessionLocal()
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    
    text = f"💰 Ваш баланс: {user.balance:.2f} руб."
    keyboard = [
        [InlineKeyboardButton(text="💵 Пополнить баланс", callback_data="topup")],
        [main_menu_button()]
    ]
    
    db.close()
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data == "daily_card")
async def daily_card(callback: types.CallbackQuery):
    await callback.answer()
    db = SessionLocal()
    card = get_card_of_the_day(db)
    
    photo_url = card.get('image_url')
    text = (f"🌙 Карта дня: {card['name']}\n\n"
            f"{card['description']}\n\n"
            f"{card['meaning']}")
    
    keyboard = [
        [main_menu_button()]
    ]
    
    if photo_url:
        try:
            await callback.message.delete()
            await bot.send_photo(
                chat_id=callback.from_user.id,
                photo=photo_url,
                caption=text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
        except Exception:
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    else:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    
    db.close()

@dp.callback_query(F.data == "help")
async def help_callback(callback: types.CallbackQuery):
    await callback.answer()
    db = SessionLocal()
    cost = get_reading_cost(db)
    db.close()
    
    text = "📖 Инструкция по использованию бота:\n\n" \
           "1. Пополните баланс через меню 'Пополнить баланс'\n" \
           "2. Выберите сумму и перейдите по ссылке для оплаты\n" \
           "3. Нажмите 'Подтвердить оплату'\n" \
           "4. После подтверждения администратором средства поступят на ваш баланс\n" \
           "5. Сделайте расклад таро через меню 'Сделать расклад таро'\n\n" \
           "Если есть вопросы — напишите тех. поддержке: @augrudhs"
    
    await callback.message.edit_text(text, reply_markup=back_to_menu_keyboard())

@dp.callback_query(F.data == "about_bot")
async def about_bot_callback(callback: types.CallbackQuery):
    await callback.answer()
    text = "📖 Что это?\n\n" \
           "✨ Это сервис для честных, понятных и глубоких раскладов — будь то таро или хиромантия. Мы создаем полноценные, детальные ответы, без общих фраз и шаблонов. Каждый расклад — это работа над тем, чтобы дать вам реальную поддержку, взглянуть на ситуацию под разными углами и помочь понять, что дальше.\n\n" \
           "Мы делаем это не просто «по картам», а так, чтобы вам было ясно, спокойно и понятно. Все прозрачно, честно и с заботой о вас.\n\n"
    
    keyboard = [
        [InlineKeyboardButton(text="<tg-emoji emoji-id=\"6037249452824072506\">🔒</tg-emoji> Политика конфиденциальности", url="https://telegra.ph/Politika-konfidencialnosti-servisa-mg-Taro-bot-06-05")],
        [InlineKeyboardButton(text="<tg-emoji emoji-id=\"6037475557082403885\">📁</tg-emoji> Пользовательское соглашение", url="https://telegra.ph/Polzovatelskoe-soglashenie-servisa-mg-Taro-bot-06-05")],
        [main_menu_button()]
    ]
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data == "choose_reading_type")
async def choose_reading_type(callback: types.CallbackQuery):
    await callback.answer()
    db = SessionLocal()
    cost = get_reading_cost(db)
    individual_cost = get_individual_reading_cost(db)
    palm_cost = get_palm_reading_cost(db)
    db.close()
    
    keyboard = [
        [InlineKeyboardButton(text=f"🔮 Расклад от помощницы ({cost} руб.)", callback_data="start_reading")],
        [InlineKeyboardButton(text=f"💎 Расклад от мастера ({individual_cost} руб.)", callback_data="start_individual_reading")],
        [InlineKeyboardButton(text=f"✋ Расклад по ладони ({palm_cost} руб.)", callback_data="start_palm_reading")],
        [main_menu_button()],
    ]
    
    await callback.message.edit_text("Выберите тип расклада:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data == "topup")
async def topup_callback(callback: types.CallbackQuery):
    await callback.answer()
    text = "💵 Пополнение баланса\n\n" \
           "Выберите сумму пополнения:"
    await callback.message.edit_text(text, reply_markup=topup_amounts_keyboard())

@dp.callback_query(F.data.startswith("topup_amount_"))
async def select_topup_amount(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    amount = float(callback.data.split("_")[2])
    await state.update_data(topup_amount=amount)
    await state.set_state(States.TOPUP_METHOD)
    
    keyboard = [
        [InlineKeyboardButton(text="💳 Оплатить через Robokassa", callback_data="payment_method_robokassa")],
        [InlineKeyboardButton(text="🔗 Перевод на карту (вручную)", callback_data="payment_method_manual")],
        [main_menu_button()]
    ]
    
    text = f"💵 Пополнение на {amount:.2f} руб.\n\n" \
           "Выберите способ оплаты:"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))



@dp.callback_query(F.data.startswith("payment_method_"))
async def select_payment_method(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    method = callback.data.split("_")[2]
    data = await state.get_data()
    amount = data.get("topup_amount")
    
    db = SessionLocal()
    try:
        user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
        
        topup_request = TopupRequest(
            user_id=user.id,
            amount=amount,
            is_approved=False,
            is_rejected=False
        )
        db.add(topup_request)
        db.commit()
        db.refresh(topup_request)
        await state.update_data(topup_request_id=topup_request.id)
        
        if method == "robokassa":
            payment_result = await create_robokassa_payment(amount, topup_request.id, user.id)
            if payment_result["success"]:
                topup_request.robokassa_payment_id = payment_result["payment_id"]
                db.commit()
                keyboard = [
                    [InlineKeyboardButton(text="💳 Оплатить через Robokassa", url=payment_result["payment_url"])],
                    [main_menu_button()]
                ]
                text = f"💵 Пополнение на {amount:.2f} руб.\n\n" \
                       "Нажмите на кнопку ниже для оплаты через Robokassa.\n" \
                       "После успешной оплаты баланс будет пополнен автоматически в течение 5 минут."
                await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
            else:
                await callback.message.edit_text("❌ Ошибка создания платежа: " + payment_result["error"], reply_markup=back_to_menu_keyboard())
                await state.clear()
        elif method == "manual":
            await state.update_data(topup_amount=amount, topup_request_id=topup_request.id)
            keyboard = [
                [InlineKeyboardButton(text="🔗 Открыть банк", url="https://t.tb.ru/c2c-qr-choose-bank?requisiteNumber=+79213385912&bankCode=100000000004")],
                [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data="confirm_payment")],
            ]
            text = f"💵 Пополнение на {amount:.2f} руб.\n\n" \
                   "После перевода нажмите кнопку ниже:"
            await callback.message.edit_text(text, reply_markup=with_menu_button(keyboard))
            
    except Exception as e:
        logger.error(f"Error in select_payment_method: {e}")
        await callback.answer("❌ Произошла ошибка.", show_alert=True)
    finally:
        db.close()


@dp.callback_query(F.data.startswith("check_robokassa_"))
async def check_robokassa_payment_callback(callback: types.CallbackQuery):
    await callback.answer()
    try:
        request_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("❌ Неверный формат.", show_alert=True)
        return
    
    db = SessionLocal()
    try:
        topup_request = db.query(TopupRequest).filter(TopupRequest.id == request_id).first()
        
        if not topup_request:
            await callback.answer("❌ Запрос не найден.", show_alert=True)
            return
        
        if topup_request.is_approved:
            await callback.answer("✅ Оплата уже подтверждена!", show_alert=True)
            await callback.message.edit_text("✅ Оплата уже подтверждена!", reply_markup=back_to_menu_keyboard())
            return
        
        if topup_request.is_rejected:
            await callback.answer("❌ Запрос отклонён.", show_alert=True)
            await callback.message.edit_text("❌ Запрос отклонён.", reply_markup=back_to_menu_keyboard())
            return
        
        if not topup_request.robokassa_payment_id:
            await callback.answer("❌ Нет данных о платеже.", show_alert=True)
            return
        
        check_result = await check_robokassa_payment(topup_request.robokassa_payment_id)
        
        if not check_result["success"]:
            await callback.answer(f"❌ Ошибка проверки: {check_result.get('error')}", show_alert=True)
            return
        
        if check_result.get("paid"):
            user = db.query(User).filter(User.id == topup_request.user_id).first()
            user.balance += topup_request.amount
            
            transaction = Transaction(
                user_id=user.id,
                amount=topup_request.amount,
                type="topup",
                description=f"Пополнение баланса через Robokassa ({topup_request.amount} руб.)"
            )
            db.add(transaction)
            
            topup_request.is_approved = True
            topup_request.approved_at = datetime.now(pytz.UTC)
            db.commit()
            
            await callback.message.edit_text(
                f"✅ Оплата успешна! Ваш баланс пополнен на {topup_request.amount:.2f} руб.\n"
                f"Текущий баланс: {user.balance:.2f} руб.",
                reply_markup=back_to_menu_keyboard()
            )
            
            logger.info(f"Robokassa payment {topup_request.robokassa_payment_id} approved: {topup_request.amount} RUB for user {user.telegram_id}")
            
            for admin_id in ADMIN_USER_IDS:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=f"✅ Оплата через Robokassa подтверждена автоматически!\n\n"
                             f"ID запроса: {request_id}\n"
                             f"Пользователь: {user.first_name or 'Unknown'}\n"
                             f"Сумма: {topup_request.amount:.2f} руб."
                    )
                except Exception as e:
                    logger.error(f"Failed to send admin notification: {e}")
        else:
            await callback.answer("⌛ Оплата ещё не выполнена. Проверьте позже.", show_alert=True)
            
    except Exception as e:
        logger.error(f"Error in check_robokassa_payment_callback: {e}")
        await callback.answer("❌ Произошла ошибка. Попробуйте позже.", show_alert=True)
    finally:
        db.close()


@dp.callback_query(F.data == "confirm_payment")
async def confirm_payment(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    amount = data.get('topup_amount')
    request_id = data.get('topup_request_id')
    
    if not amount or not request_id:
        await callback.answer("❌ Произошла ошибка. Попробуйте снова.", show_alert=True)
        await callback.message.edit_text("❌ Произошла ошибка. Попробуйте снова.", reply_markup=back_to_menu_keyboard())
        return
    
    db = SessionLocal()
    try:
        user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
        
        topup_request = db.query(TopupRequest).filter(TopupRequest.id == request_id).first()
        if not topup_request:
            await callback.answer("❌ Запрос не найден.", show_alert=True)
            return
        
        logger.info(f"Notifying {len(ADMIN_USER_IDS)} admins about topup request {request_id}")
        for admin_id in ADMIN_USER_IDS:
            try:
                keyboard = [
                    [InlineKeyboardButton(text=f"✅ Подтвердить {amount:.2f} руб.", callback_data=f"approve_{request_id}")],
                    [InlineKeyboardButton(text=f"❌ Отклонить", callback_data=f"reject_{request_id}")],
                ]
                
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"📨 Новый запрос на пополнение!\n\n"
                         f"ID запроса: {request_id}\n"
                         f"Пользователь: {user.first_name or 'Unknown'} (@{user.username or 'no_username'})\n"
                         f"Сумма: {amount:.2f} руб.\n"
                         f"Дата: {datetime.now(pytz.UTC).strftime('%d.%m.%Y %H:%M:%S')}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
                )
                logger.info(f"Topup notification sent to admin {admin_id}")
            except Exception as e:
                logger.error(f"Failed to send topup notification to admin {admin_id}: {str(e)}")
        
        await callback.message.edit_text(
            "✅ Запрос отправлен администраторам на проверку.\n"
            "После подтверждения баланс будет пополнен.",
            reply_markup=back_to_menu_keyboard()
        )
        
    except Exception as e:
        pass
        await callback.answer("❌ Произошла ошибка. Попробуйте позже.", show_alert=True)
    finally:
        db.close()

@dp.callback_query(F.data == "approve_all")
async def approve_all_payments(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для выполнения этого действия.", show_alert=True)
        return
    
    await callback.answer()
    db = SessionLocal()
    try:
        pending_requests = db.query(TopupRequest).filter(
            TopupRequest.is_approved == False,
            TopupRequest.is_rejected == False
        ).all()
        
        if not pending_requests:
            await callback.message.edit_text("✅ Нет ожидающих запросов на пополнение.", reply_markup=admin_menu_keyboard())
            db.close()
            return
        
        count = 0
        for req in pending_requests:
            user = db.query(User).filter(User.id == req.user_id).first()
            user.balance += req.amount
            
            transaction = Transaction(
                user_id=user.id,
                amount=req.amount,
                type='topup',
                description=f'Пополнение баланса через админ (все) ({req.amount} руб.)'
            )
            db.add(transaction)
            
            req.is_approved = True
            req.approved_at = datetime.now(pytz.UTC)
            count += 1
            
            try:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"✅ Ваш баланс успешно пополнен на {req.amount:.2f} руб.!\n"
                         f"Текущий баланс: {user.balance:.2f} руб."
                )
            except Exception as e:
                pass
        
        db.commit()
        logger.info(f"Approved {count} topup requests")
        
        await callback.message.edit_text(f"✅ Успешно одобрено {count} заявок!", reply_markup=admin_menu_keyboard())
        
    except Exception as e:
        await callback.answer("❌ Произошла ошибка.", show_alert=True)
        db.rollback()
    finally:
        db.close()

@dp.callback_query(F.data == "reject_all")
async def reject_all_payments(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для выполнения этого действия.", show_alert=True)
        return
    
    await callback.answer()
    db = SessionLocal()
    try:
        pending_requests = db.query(TopupRequest).filter(
            TopupRequest.is_approved == False,
            TopupRequest.is_rejected == False
        ).all()
        
        if not pending_requests:
            await callback.message.edit_text("✅ Нет ожидающих запросов на пополнение.", reply_markup=admin_menu_keyboard())
            db.close()
            return
        
        count = 0
        for req in pending_requests:
            req.is_rejected = True
            count += 1
            
            user = db.query(User).filter(User.id == req.user_id).first()
            try:
                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"❌ Ваш запрос на пополнение на {req.amount:.2f} руб. был отклонён."
                )
            except Exception as e:
                pass
        
        db.commit()
        logger.info(f"Rejected {count} topup requests")
        
        await callback.message.edit_text(f"❌ Успешно отклонено {count} заявок!", reply_markup=admin_menu_keyboard())
        
    except Exception as e:
        await callback.answer("❌ Произошла ошибка.", show_alert=True)
        db.rollback()
    finally:
        db.close()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для выполнения этого действия.", show_alert=True)
        return
    
    await callback.answer()
    try:
        request_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.answer("❌ Неверный формат.", show_alert=True)
        return
    
    db = SessionLocal()
    try:
        topup_request = db.query(TopupRequest).filter(TopupRequest.id == request_id).first()
        if not topup_request:
            await callback.answer("❌ Запрос не найден.", show_alert=True)
            return
        
        if topup_request.is_approved or topup_request.is_rejected:
            await callback.answer("❌ Этот запрос уже обработан.", show_alert=True)
            return
        
        user = db.query(User).filter(User.id == topup_request.user_id).first()
        user.balance += topup_request.amount
        
        transaction = Transaction(
            user_id=user.id,
            amount=topup_request.amount,
            type='topup',
            description=f'Пополнение баланса через админ ({topup_request.amount} руб.)'
        )
        db.add(transaction)
        
        topup_request.is_approved = True
        topup_request.approved_at = datetime.now(pytz.UTC)
        db.commit()
        logger.info(f"Topup request {request_id} approved: {topup_request.amount} RUB for user {user.telegram_id}")
        
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=f"✅ Ваш баланс успешно пополнен на {topup_request.amount:.2f} руб.!\n"
                     f"Текущий баланс: {user.balance:.2f} руб."
            )
        except Exception as e:
            pass
        
        await callback.message.edit_text(
            f"✅ Запрос {request_id} подтверждён!\n"
            f"Пользователь: {user.first_name or 'Unknown'}\n"
            f"Сумма: {topup_request.amount:.2f} руб.",
            reply_markup=admin_menu_keyboard()
        )
        
    except Exception as e:
        await callback.answer("❌ Произошла ошибка.", show_alert=True)
    finally:
        db.close()

@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для выполнения этого действия.", show_alert=True)
        return
    
    await callback.answer()
    try:
        request_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.answer("❌ Неверный формат.", show_alert=True)
        return
    
    db = SessionLocal()
    try:
        topup_request = db.query(TopupRequest).filter(TopupRequest.id == request_id).first()
        if not topup_request:
            await callback.answer("❌ Запрос не найден.", show_alert=True)
            return
        
        if topup_request.is_approved or topup_request.is_rejected:
            await callback.answer("❌ Этот запрос уже обработан.", show_alert=True)
            return
        
        topup_request.is_rejected = True
        db.commit()
        logger.info(f"Topup request {request_id} rejected for user {topup_request.user_id}")
        
        user = db.query(User).filter(User.id == topup_request.user_id).first()
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text="❌ Ваш запрос на пополнение баланса был отклонён."
            )
        except Exception as e:
            pass
        
        await callback.message.edit_text(
            f"❌ Запрос {request_id} отклонён!",
            reply_markup=admin_menu_keyboard()
        )
        
    except Exception as e:
        await callback.answer("❌ Произошла ошибка.", show_alert=True)
    finally:
        db.close()

@dp.callback_query(F.data == "start_standard_reading")
async def start_standard_reading(callback: types.CallbackQuery):
    await callback.answer()
    db = SessionLocal()
    cost = get_reading_cost(db)
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    
    text = "🔮 **Стандартный расклад Таро**\n\n"
    text += "Получите детальный ответ на 5 вопросов с анализом карт прошлого, настоящего и будущего.\n\n"
    
    if not user.has_used_free_reading:
        text += f"🎁 Ваш первый расклад — **бесплатно**!\n"
    else:
        text += f"💰 Стоимость: {cost:.0f} руб.\n\n"
        text += f"💵 Текущий баланс: {user.balance:.2f} руб.\n"
    
    keyboard = [
        [InlineKeyboardButton(text="🎯 Заказать расклад", callback_data="confirm_standard_reading")],
        [main_menu_button()]
    ]
    
    db.close()
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data == "confirm_standard_reading")
async def confirm_standard_reading(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    db = SessionLocal()
    cost = get_reading_cost(db)
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    
    if not user.has_used_free_reading:
        await state.update_data(questions=[], is_free=True)
        await state.set_state(States.QUESTION1)
        db.close()
        await callback.message.edit_text(QUESTIONS[0], reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu")]]))
        return
    
    if user.balance < cost:
        await callback.message.edit_text(
            f"❌ Недостаточно средств для расклада.\n"
            f"Стоимость расклада: {cost:.2f} руб.\n"
            f"Пополните баланс, используя кнопку '💰 Баланс'.",
            reply_markup=back_to_menu_keyboard()
        )
        db.close()
        return
    
    await state.update_data(questions=[], is_free=False)
    await state.set_state(States.QUESTION1)
    db.close()
    await callback.message.edit_text(QUESTIONS[0], reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu")]]))

@dp.message(States.QUESTION1)
async def question1(message: types.Message, state: FSMContext):
    data = await state.get_data()
    questions = data.get('questions', [])
    questions.append(message.text)
    await state.update_data(questions=questions)
    await state.set_state(States.QUESTION2)
    await message.answer(QUESTIONS[1], reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu")]]))

@dp.message(States.QUESTION2)
async def question2(message: types.Message, state: FSMContext):
    data = await state.get_data()
    questions = data.get('questions', [])
    questions.append(message.text)
    await state.update_data(questions=questions)
    await state.set_state(States.QUESTION3)
    await message.answer(QUESTIONS[2], reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu")]]))

@dp.message(States.QUESTION3)
async def question3(message: types.Message, state: FSMContext):
    data = await state.get_data()
    questions = data.get('questions', [])
    questions.append(message.text)
    await state.update_data(questions=questions)
    await state.set_state(States.QUESTION4)
    await message.answer(QUESTIONS[3], reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu")]]))

@dp.message(States.QUESTION4)
async def question4(message: types.Message, state: FSMContext):
    data = await state.get_data()
    questions = data.get('questions', [])
    questions.append(message.text)
    await state.update_data(questions=questions)
    await state.set_state(States.QUESTION5)
    await message.answer(QUESTIONS[4], reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu")]]))

@dp.message(States.QUESTION5)
async def question5(message: types.Message, state: FSMContext):
    data = await state.get_data()
    questions = data.get('questions', [])
    is_free = data.get('is_free', False)
    questions.append(message.text)
    
    db = SessionLocal()
    cost = get_reading_cost(db)
    user = get_or_create_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    if not is_free and user.balance < cost:
        await message.answer(
            f"❌ Недостаточно средств для расклада.\n"
            f"Стоимость расклада: {cost:.2f} руб.\n"
            f"Пополните баланс, используя кнопку '💰 Баланс'.",
            reply_markup=back_to_menu_keyboard()
        )
        await state.clear()
        db.close()
        return
    
    if not is_free:
        user.balance -= cost
        transaction = Transaction(
            user_id=user.id,
            amount=-cost,
            type='reading',
            description=f'Стандартный расклад ({cost} руб.)'
        )
        db.add(transaction)
    
    if is_free:
        user.has_used_free_reading = True
    
    reading = Reading(
        user_id=user.id,
        question1=questions[0],
        question2=questions[1],
        question3=questions[2],
        question4=questions[3],
        question5=questions[4],
        cost=0 if is_free else cost,
        reading_type='standard'
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)
    db.close()
    
    await state.clear()
    progress_msg = await message.answer("🔮 Раскладываем карты на тёмном полотне...")
    
    try:
        await asyncio.sleep(30)
        await progress_msg.edit_text("✨ Смотрим в прошлое...")
        await asyncio.sleep(30)
        await progress_msg.edit_text("🌿 Исследуем настоящее...")
        await asyncio.sleep(30)
        await progress_msg.edit_text("🔮 Открываем завесу будущего...")
        
        response = await generate_tarot_reading(questions)
        
        db = SessionLocal()
        reading = db.query(Reading).filter(Reading.id == reading.id).first()
        reading.response = response
        db.commit()
        db.close()
        
        await progress_msg.edit_text(f"✨ Ваш расклад готов!\n\n{response}", reply_markup=back_to_menu_keyboard())
    except Exception as e:
        await progress_msg.edit_text(
            "❌ Произошла ошибка при генерации расклада. Попробуйте позже.",
            reply_markup=back_to_menu_keyboard()
        )

@dp.callback_query(F.data == "individual_reading")
@dp.callback_query(F.data == "start_individual_reading")
async def show_individual_reading(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    db = SessionLocal()
    cost = get_individual_reading_cost(db)
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)

    text = (
        "✨ <b>Живой расклад от мастера</b>\n\n"
        "Получите личный ответ от профессионального таролога.\n\n"
        f"💰 Стоимость: <b>{cost:.0f} руб.</b>\n"
        f"💵 Ваш баланс: <b>{user.balance:.2f} руб.</b>"
    )

    keyboard = [
        [InlineKeyboardButton(text="🎯 Оплатить расклад", callback_data="confirm_individual_reading")],
        [main_menu_button()]
    ]

    db.close()
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@dp.callback_query(F.data == "confirm_individual_reading")
async def confirm_individual_reading(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    db = SessionLocal()
    cost = get_individual_reading_cost(db)
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)

    if user.balance < cost:
        await callback.message.edit_text(
            f"❌ <b>Недостаточно средств.</b>\n"
            f"Стоимость расклада: <b>{cost:.0f} руб.</b>\n"
            f"Ваш баланс: <b>{user.balance:.2f} руб.</b>\n\n"
            f"Пополните баланс через раздел 💰 Баланс.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="HTML"
        )
        db.close()
        return

    # Списываем деньги сразу при нажатии кнопки
    user.balance -= cost
    transaction = Transaction(
        user_id=user.id,
        amount=-cost,
        type='reading',
        description=f'Индивидуальный расклад ({cost:.0f} руб.)'
    )
    db.add(transaction)

    reading = Reading(
        user_id=user.id,
        question1="индивидуальный расклад",
        question2="",
        question3="",
        question4="",
        question5="",
        cost=cost,
        reading_type='individual',
        is_answered=False
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)
    reading_id = reading.id
    user_name = user.first_name or user.username or str(user.telegram_id)
    db.close()

    # Уведомляем всех админов
    admin_notify_text = (
        f"💳 <b>Куплен индивидуальный расклад</b>\n\n"
        f"Пользователь: <b>{user_name}</b> (ID: <code>{callback.from_user.id}</code>)\n"
        f"Сумма: <b>{cost:.0f} руб.</b>\n"
        f"Расклад №{reading_id}"
    )
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=admin_notify_text, parse_mode="HTML")
        except Exception:
            pass

    # Показываем пользователю сообщение с кнопкой на мастера
    master_url = f"https://t.me/{MASTER_USERNAME.lstrip('@')}" if MASTER_USERNAME else None
    success_text = (
        "✅ <b>Оплата прошла успешно!</b>\n\n"
        "🔮 Теперь напишите мастеру — опишите свою ситуацию или задайте вопрос.\n"
        "Мастер проведёт для вас личный расклад таро.\n\n"
        "👇 Нажмите кнопку ниже, чтобы перейти к мастеру:"
    )

    keyboard_rows = []
    if master_url:
        keyboard_rows.append([InlineKeyboardButton(text="🔮 Написать мастеру", url=master_url)])
    keyboard_rows.append([main_menu_button()])

    await callback.message.edit_text(
        success_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("answer_reading_"))
async def answer_reading_callback(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для выполнения этого действия.", show_alert=True)
        return
    
    await callback.answer()
    try:
        reading_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("❌ Неверный формат.", show_alert=True)
        return
    
    db = SessionLocal()
    reading = db.query(Reading).filter(Reading.id == reading_id).first()
    if not reading:
        await callback.answer("❌ Расклад не найден.", show_alert=True)
        db.close()
        return
    
    if reading.is_answered:
        await callback.answer("❌ Этот расклад уже отвечен.", show_alert=True)
        db.close()
        return
    
    await state.update_data(reading_id=reading_id)
    await state.set_state(States.ANSWER_READING)
    await callback.message.answer("Введите ответ на расклад:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", callback_data="admin_menu")]]))
    db.close()

@dp.message(States.ANSWER_READING)
async def send_reading_answer(message: types.Message, state: FSMContext):
    data = await state.get_data()
    reading_id = data.get('reading_id')
    
    if not reading_id:
        await message.answer("❌ Произошла ошибка.", reply_markup=admin_menu_keyboard())
        await state.clear()
        return
    
    db = SessionLocal()
    reading = db.query(Reading).filter(Reading.id == reading_id).first()
    if not reading:
        await message.answer("❌ Расклад не найден.", reply_markup=admin_menu_keyboard())
        await state.clear()
        db.close()
        return
    
    user = db.query(User).filter(User.id == reading.user_id).first()
    reading.is_answered = True
    reading.answer = message.text
    reading.response = message.text
    db.commit()
    logger.info(f"Reading {reading_id} answered by admin {message.from_user.id} for user {user.telegram_id}")
    
    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=f"✨ Получен ответ на ваш расклад!\n\n{message.text}",
            reply_markup=back_to_menu_keyboard()
        )
    except Exception as e:
        pass
    
    await message.answer("✅ Ответ отправлен пользователю!", reply_markup=admin_menu_keyboard())
    await state.clear()
    db.close()

@dp.callback_query(F.data == "start_palm_reading")
async def start_palm_reading(callback: types.CallbackQuery):
    await callback.answer()
    db = SessionLocal()
    cost = get_palm_reading_cost(db)
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    
    text = "✋ **Хиромантия по фото**\n\n"
    text += "Получите детальный анализ вашей руки с описанием линий жизни, сердца, головы и особых знаков.\n\n"
    text += f"💰 Стоимость: {cost:.0f} руб.\n\n"
    text += f"💵 Текущий баланс: {user.balance:.2f} руб.\n"
    
    keyboard = [
        [InlineKeyboardButton(text="🎯 Заказать хиромантию", callback_data="confirm_palm_reading")],
        [main_menu_button()]
    ]
    
    db.close()
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data == "confirm_palm_reading")
async def confirm_palm_reading(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    db = SessionLocal()
    cost = get_palm_reading_cost(db)
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    
    if user.balance < cost:
        await callback.message.edit_text(
            f"❌ Недостаточно средств для расклада.\n"
            f"Стоимость расклада: {cost:.2f} руб.\n"
            f"Пополните баланс, используя кнопку '💰 Баланс'.",
            reply_markup=back_to_menu_keyboard()
        )
        db.close()
        return
    
    await state.set_state(States.PALM_PHOTO)
    db.close()
    await callback.message.edit_text("Пожалуйста, отправьте фото вашей ладони:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu")]]))

@dp.message(States.PALM_PHOTO, F.photo)
async def handle_palm_photo(message: types.Message, state: FSMContext):
    db = SessionLocal()
    cost = get_palm_reading_cost(db)
    user = get_or_create_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    if user.balance < cost:
        await message.answer(
            f"❌ Недостаточно средств для расклада.\n"
            f"Стоимость расклада: {cost:.2f} руб.\n"
            f"Пополните баланс, используя кнопку '💰 Баланс'.",
            reply_markup=back_to_menu_keyboard()
        )
        await state.clear()
        db.close()
        return
    
    user.balance -= cost
    transaction = Transaction(
        user_id=user.id,
        amount=-cost,
        type='reading',
        description=f'Расклад по ладони ({cost} руб.)'
    )
    db.add(transaction)
    
    reading = Reading(
        user_id=user.id,
        question1="",
        question2="",
        question3="",
        question4="",
        question5="",
        cost=cost,
        reading_type='palm'
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)
    db.close()
    
    await state.clear()
    progress_msg = await message.answer("✋ Рассматриваем линию жизни...")
    
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        photo_base64 = base64.b64encode(file_bytes.getvalue()).decode('utf-8')
        
        await asyncio.sleep(60)
        await progress_msg.edit_text("❤️ Исследуем линию сердца...")
        await asyncio.sleep(60)
        await progress_msg.edit_text("🧠 Анализируем линию головы...")
        await asyncio.sleep(60)
        await progress_msg.edit_text("🔮 Открываем судьбу по линиям...")
        
        response = await generate_palm_reading(photo_base64)
        
        db = SessionLocal()
        reading = db.query(Reading).filter(Reading.id == reading.id).first()
        reading.response = response
        db.commit()
        db.close()
        
        await progress_msg.edit_text(f"✨ Ваш расклад по ладони готов!\n\n{response}", reply_markup=back_to_menu_keyboard())
    except Exception as e:
        await progress_msg.edit_text(
            "❌ Произошла ошибка при генерации расклада. Попробуйте позже.",
            reply_markup=back_to_menu_keyboard()
        )

@dp.callback_query(F.data == "admin_menu")
async def show_admin_menu(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    await callback.message.edit_text("🔐 Админ-панель:", reply_markup=admin_menu_keyboard())

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    db = SessionLocal()
    
    total_users = db.query(User).count()
    total_readings = db.query(Reading).count()
    total_transactions = db.query(Transaction).count()
    total_balance = db.query(func.sum(User.balance)).scalar() or 0
    
    now = datetime.now(pytz.UTC)
    start_day = now - timedelta(days=1)
    start_week = now - timedelta(weeks=1)
    start_month = now - timedelta(days=30)
    
    # Revenue (sum of approved topup requests or transactions of type 'topup')
    revenue_day = db.query(func.sum(TopupRequest.amount)).filter(
        TopupRequest.is_approved == True,
        TopupRequest.approved_at >= start_day
    ).scalar() or 0
    
    revenue_week = db.query(func.sum(TopupRequest.amount)).filter(
        TopupRequest.is_approved == True,
        TopupRequest.approved_at >= start_week
    ).scalar() or 0
    
    revenue_month = db.query(func.sum(TopupRequest.amount)).filter(
        TopupRequest.is_approved == True,
        TopupRequest.approved_at >= start_month
    ).scalar() or 0
    
    readings_day = db.query(Reading).filter(
        Reading.created_at >= start_day
    ).count()
    
    readings_week = db.query(Reading).filter(
        Reading.created_at >= start_week
    ).count()
    
    readings_month = db.query(Reading).filter(
        Reading.created_at >= start_month
    ).count()
    
    db.close()
    
    text = f"📊 Статистика:\n\n" \
           f"👤 Всего пользователей: {total_users}\n" \
           f"🔮 Всего раскладов: {total_readings}\n" \
           f"💳 Всего транзакций: {total_transactions}\n" \
           f"💰 Общий баланс пользователей: {total_balance:.2f} руб.\n\n" \
           f"📅 За день:\n" \
           f"  • Доход: {revenue_day:.2f} руб.\n" \
           f"  • Раскладов: {readings_day}\n\n" \
           f"📅 За неделю:\n" \
           f"  • Доход: {revenue_week:.2f} руб.\n" \
           f"  • Раскладов: {readings_week}\n\n" \
           f"📅 За месяц:\n" \
           f"  • Доход: {revenue_month:.2f} руб.\n" \
           f"  • Раскладов: {readings_month}"
    
    await callback.message.edit_text(text, reply_markup=admin_menu_keyboard())

@dp.callback_query(F.data == "admin_requests")
async def admin_requests(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    db = SessionLocal()
    
    pending_requests = db.query(TopupRequest).filter(
        TopupRequest.is_approved == False,
        TopupRequest.is_rejected == False
    ).all()
    
    if not pending_requests:
        await callback.message.edit_text("✅ Нет ожидающих запросов на пополнение.", reply_markup=admin_menu_keyboard())
        db.close()
        return
    
    text = "📋 Запросы на пополнение:\n\n"
    for req in pending_requests:
        user = db.query(User).filter(User.id == req.user_id).first()
        text += f"ID: {req.id}\n"
        text += f"Пользователь: {user.first_name or 'Unknown'} (@{user.username or 'no_username'})\n"
        text += f"Сумма: {req.amount:.2f} руб.\n"
        text += f"Дата: {req.created_at.strftime('%d.%m.%Y %H:%M:%S')}\n\n"
    
    keyboard = [
        [
            InlineKeyboardButton(text="✅ Одобрить все заявки", callback_data="approve_all"),
            InlineKeyboardButton(text="❌ Отклонить все заявки", callback_data="reject_all")
        ]
    ]
    for req in pending_requests:
        keyboard.append([
            InlineKeyboardButton(text=f"✅ Подтвердить {req.id}", callback_data=f"approve_{req.id}"),
            InlineKeyboardButton(text=f"❌ Отклонить {req.id}", callback_data=f"reject_{req.id}")
        ])
    keyboard.append([InlineKeyboardButton(text="<< Назад к админ-панели", callback_data="admin_menu")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    db.close()

@dp.callback_query(F.data == "admin_pending_readings")
async def admin_pending_readings(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return

    await callback.answer()
    db = SessionLocal()

    pending_readings = db.query(Reading).filter(
        Reading.reading_type == 'individual',
        Reading.is_answered == False
    ).all()

    if not pending_readings:
        await callback.message.edit_text("✅ Нет активных индивидуальных раскладов.", reply_markup=admin_menu_keyboard())
        db.close()
        return

    text = "🔮 <b>Активные индивидуальные расклады</b>\n\n"
    keyboard = []
    for reading in pending_readings:
        user = db.query(User).filter(User.id == reading.user_id).first()
        user_name = user.first_name or user.username or str(user.telegram_id) if user else "Unknown"
        text += (
            f"📌 Расклад <b>#{reading.id}</b>\n"
            f"👤 Пользователь: <b>{user_name}</b> (ID: <code>{user.telegram_id if user else '?'}</code>)\n"
            f"💰 Стоимость: <b>{reading.cost:.0f} руб.</b>\n"
            f"🕐 Куплен: {reading.created_at.strftime('%d.%m.%Y %H:%M') if reading.created_at else '—'}\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(text=f"✅ Завершить #{reading.id}", callback_data=f"complete_reading_{reading.id}")
        ])

    keyboard.append([InlineKeyboardButton(text="<< Назад к админ-панели", callback_data="admin_menu")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )
    db.close()


@dp.callback_query(F.data.startswith("complete_reading_"))
async def complete_reading_callback(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для выполнения этого действия.", show_alert=True)
        return

    await callback.answer()
    try:
        reading_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("❌ Неверный формат.", show_alert=True)
        return

    db = SessionLocal()
    reading = db.query(Reading).filter(Reading.id == reading_id).first()
    if not reading:
        await callback.answer("❌ Расклад не найден.", show_alert=True)
        db.close()
        return

    if reading.is_answered:
        await callback.answer("❌ Этот расклад уже завершён.", show_alert=True)
        db.close()
        return

    reading.is_answered = True
    reading.answer = "Выполнен мастером"
    reading.response = "Выполнен мастером"
    db.commit()
    logger.info(f"Individual reading {reading_id} marked as complete by admin {callback.from_user.id}")
    db.close()

    await callback.answer("✅ Расклад отмечен как завершённый.", show_alert=True)

    # Обновляем список
    db2 = SessionLocal()
    pending_readings = db2.query(Reading).filter(
        Reading.reading_type == 'individual',
        Reading.is_answered == False
    ).all()

    if not pending_readings:
        await callback.message.edit_text("✅ Нет активных индивидуальных раскладов.", reply_markup=admin_menu_keyboard())
        db2.close()
        return

    text = "🔮 <b>Активные индивидуальные расклады</b>\n\n"
    keyboard = []
    for r in pending_readings:
        user = db2.query(User).filter(User.id == r.user_id).first()
        user_name = user.first_name or user.username or str(user.telegram_id) if user else "Unknown"
        text += (
            f"📌 Расклад <b>#{r.id}</b>\n"
            f"👤 Пользователь: <b>{user_name}</b> (ID: <code>{user.telegram_id if user else '?'}</code>)\n"
            f"💰 Стоимость: <b>{r.cost:.0f} руб.</b>\n"
            f"🕐 Куплен: {r.created_at.strftime('%d.%m.%Y %H:%M') if r.created_at else '—'}\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(text=f"✅ Завершить #{r.id}", callback_data=f"complete_reading_{r.id}")
        ])

    keyboard.append([InlineKeyboardButton(text="<< Назад к админ-панели", callback_data="admin_menu")])
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )
    db2.close()

@dp.callback_query(F.data == "admin_export_users")
async def admin_export_users(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    db = SessionLocal()
    
    users = db.query(User).all()
    
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Пользователи"
    
    headers = ["ID", "Telegram ID", "Имя", "Username", "Баланс", "Дата регистрации"]
    worksheet.append(headers)
    
    for user in users:
        worksheet.append([
            user.id,
            user.telegram_id,
            user.first_name,
            user.username,
            user.balance,
            user.created_at.strftime("%d.%m.%Y %H:%M:%S") if user.created_at else ""
        ])
    
    from io import BytesIO
    excel_buffer = BytesIO()
    workbook.save(excel_buffer)
    excel_buffer.seek(0)
    
    await bot.send_document(
        chat_id=callback.from_user.id,
        document=types.BufferedInputFile(excel_buffer.getvalue(), filename="users.xlsx"),
        caption="📂 Выгрузка пользователей"
    )
    await callback.message.edit_text("✅ Выгрузка отправлена!", reply_markup=admin_menu_keyboard())
    db.close()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    await state.set_state(States.BROADCAST)
    await callback.message.edit_text("Введите текст рассылки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", callback_data="admin_menu")]]))

@dp.message(States.BROADCAST)
async def send_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав для выполнения этого действия.")
        await state.clear()
        return
    
    db = SessionLocal()
    users = db.query(User).all()
    db.close()
    
    success_count = 0
    for user in users:
        try:
            await bot.send_message(chat_id=user.telegram_id, text=message.text)
            success_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            pass
    
    await message.answer(f"✅ Рассылка отправлена!\nУспешно: {success_count}/{len(users)}", reply_markup=admin_menu_keyboard())
    await state.clear()

@dp.callback_query(F.data == "admin_set_prices")
async def admin_set_prices(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    db = SessionLocal()
    cost = get_reading_cost(db)
    individual_cost = get_individual_reading_cost(db)
    palm_cost = get_palm_reading_cost(db)
    db.close()
    
    keyboard = [
        [InlineKeyboardButton(text=f"💎 Установить цену стандартного ({cost} руб.)", callback_data="set_price_standard")],
        [InlineKeyboardButton(text=f"💎 Установить цену индивидуального ({individual_cost} руб.)", callback_data="set_price_individual")],
        [InlineKeyboardButton(text=f"✋ Установить цену по ладони ({palm_cost} руб.)", callback_data="set_price_palm")],
        [InlineKeyboardButton(text="« Назад к админ-панели", callback_data="admin_menu")],
    ]
    
    await callback.message.edit_text("💰 Управление ценами:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data == "set_price_standard")
async def set_price_standard(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    await state.set_state(States.SET_PRICE)
    await callback.message.edit_text("Введите новую цену для стандартного расклада:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", callback_data="admin_menu")]]))

@dp.message(States.SET_PRICE)
async def handle_set_standard_price(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав для выполнения этого действия.")
        await state.clear()
        return
    
    try:
        new_price = int(message.text)
        if new_price < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректную цену (положительное число).")
        return
    
    db = SessionLocal()
    set_reading_cost(db, new_price)
    db.close()
    
    await message.answer(f"✅ Цена стандартного расклада установлена на {new_price} руб.", reply_markup=admin_menu_keyboard())
    await state.clear()

@dp.callback_query(F.data == "set_price_individual")
async def set_price_individual(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    await state.set_state(States.SET_INDIVIDUAL_PRICE)
    await callback.message.edit_text("Введите новую цену для индивидуального расклада:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", callback_data="admin_menu")]]))

@dp.message(States.SET_INDIVIDUAL_PRICE)
async def handle_set_individual_price(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав для выполнения этого действия.")
        await state.clear()
        return
    
    try:
        new_price = int(message.text)
        if new_price < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректную цену (положительное число).")
        return
    
    db = SessionLocal()
    set_individual_reading_cost(db, new_price)
    db.close()
    
    await message.answer(f"✅ Цена индивидуального расклада установлена на {new_price} руб.", reply_markup=admin_menu_keyboard())
    await state.clear()

@dp.callback_query(F.data == "set_price_palm")
async def set_price_palm(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    await state.set_state(States.SET_PALM_PRICE)
    await callback.message.edit_text("Введите новую цену для расклада по ладони:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", callback_data="admin_menu")]]))

@dp.message(States.SET_PALM_PRICE)
async def handle_set_palm_price(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав для выполнения этого действия.")
        await state.clear()
        return
    
    try:
        new_price = int(message.text)
        if new_price < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректную цену (положительное число).")
        return
    
    db = SessionLocal()
    set_palm_reading_cost(db, new_price)
    db.close()
    
    await message.answer(f"✅ Цена расклада по ладони установлена на {new_price} руб.", reply_markup=admin_menu_keyboard())
    await state.clear()

@dp.callback_query(F.data == "admin_reset")
async def admin_reset(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    await callback.message.edit_text("🔧 Выберите действие:", reply_markup=reset_menu_keyboard())

@dp.callback_query(F.data.startswith("admin_reset_"))
async def admin_confirm_reset(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    reset_type = callback.data.split("_")[2]
    await callback.message.edit_text(
        "⚠️ Вы уверены? Это действие нельзя отменить!",
        reply_markup=confirm_reset_keyboard(reset_type)
    )

@dp.callback_query(F.data.startswith("admin_confirm_reset_"))
async def admin_perform_reset(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    reset_type = callback.data.split("_")[3]
    
    db = SessionLocal()
    
    if reset_type == "stats":
        db.query(Reading).delete()
        db.query(Transaction).delete()
        db.query(TopupRequest).delete()
        for user in db.query(User).all():
            user.balance = 0
        db.commit()
        await callback.message.edit_text("✅ Статистика сброшена!", reply_markup=admin_menu_keyboard())
    
    elif reset_type == "all":
        db.query(Reading).delete()
        db.query(Transaction).delete()
        db.query(TopupRequest).delete()
        db.query(User).delete()
        db.query(Setting).delete()
        db.commit()
        await callback.message.edit_text("✅ Все данные сброшены!", reply_markup=admin_menu_keyboard())
    
    elif reset_type == "user":
        await state.set_state(States.RESET_USER)
        await callback.message.edit_text("Введите Telegram ID пользователя:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", callback_data="admin_menu")]]))
    
    db.close()

@dp.message(States.RESET_USER)
async def reset_user(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав для выполнения этого действия.")
        await state.clear()
        return
    
    try:
        telegram_id = int(message.text)
    except ValueError:
        await message.answer("❌ Введите корректный ID (число).")
        return
    
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    
    if not user:
        await message.answer("❌ Пользователь не найден.", reply_markup=admin_menu_keyboard())
        await state.clear()
        db.close()
        return
    
    user.balance = 0
    db.query(Reading).filter(Reading.user_id == user.id).delete()
    db.query(Transaction).filter(Transaction.user_id == user.id).delete()
    db.query(TopupRequest).filter(TopupRequest.user_id == user.id).delete()
    db.commit()
    
    await message.answer(f"✅ Данные пользователя {user.first_name or 'Unknown'} сброшены!", reply_markup=admin_menu_keyboard())
    await state.clear()
    db.close()



async def send_daily_card():
    db = SessionLocal()
    try:
        card = get_card_of_the_day(db)
        users = db.query(User).all()
        
        for user in users:
            try:
                photo_url = card.get('image_url')
                
                text = (f"🌞 Карта дня: {card['name']}\n\n"
                        f"{card['description']}\n\n"
                        f"{card['meaning']}")
                
                if photo_url:
                    await bot.send_photo(
                        chat_id=user.telegram_id,
                        photo=photo_url,
                        caption=text
                    )
                else:
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=text
                    )
            except Exception as e:
                pass
    finally:
        db.close()

scheduler = AsyncIOScheduler()

async def main():
    from sqlalchemy import func
    migrate_database()
    
    logger.info(f"Loaded ADMIN_USER_IDS: {ADMIN_USER_IDS}")
    scheduler.add_job(send_daily_card, 'cron', hour=10, minute=0)
    scheduler.start()
    
    
    logger.info("Bot started successfully!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

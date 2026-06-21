import json
import asyncio
import os
import random
import base64
import logging
import uuid
import hashlib
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
    BufferedInputFile,
    FSInputFile,
    InputMediaPhoto
)

from models import (
    User, Reading, Transaction, TopupRequest, Setting, SessionLocal,
    get_reading_cost, set_reading_cost,
    get_individual_reading_cost, set_individual_reading_cost,
    get_palm_reading_cost, set_palm_reading_cost,
    get_compatibility_reading_cost, set_compatibility_reading_cost,
    get_demo_balance, set_demo_balance,
    migrate_database
)
from config import (
    TELEGRAM_BOT_TOKEN,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    ADMIN_USER_IDS,
    MASTER_USERNAME,
    YOOKASSA_SHOP_ID,
    YOOKASSA_SECRET_KEY,
    LOG_CHAT_ID,
)
from cards_data import TAROT_CARDS, SUB_CARDS

ALL_CARDS = TAROT_CARDS + SUB_CARDS


def _card_hash(name: str) -> str:
    return hashlib.md5(name.encode()).hexdigest()[:8]

def get_disabled_cards(db) -> set:
    s = db.query(Setting).filter(Setting.key == 'disabled_cards').first()
    return set(json.loads(s.value)) if s and s.value else set()

def set_disabled_cards(db, names: set):
    s = db.query(Setting).filter(Setting.key == 'disabled_cards').first()
    val = json.dumps(list(names), ensure_ascii=False)
    if s:
        s.value = val
    else:
        db.add(Setting(key='disabled_cards', value=val))
    db.commit()

def get_extra_cards(db) -> list:
    s = db.query(Setting).filter(Setting.key == 'extra_cards').first()
    return json.loads(s.value) if s and s.value else []

def set_extra_cards(db, cards: list):
    s = db.query(Setting).filter(Setting.key == 'extra_cards').first()
    val = json.dumps(cards, ensure_ascii=False)
    if s:
        s.value = val
    else:
        db.add(Setting(key='extra_cards', value=val))
    db.commit()

def get_active_cards(db=None) -> list:
    close_db = db is None
    if close_db:
        db = SessionLocal()
    try:
        disabled = get_disabled_cards(db)
        extra = get_extra_cards(db)
        base = [c for c in ALL_CARDS if c['name'] not in disabled]
        return base + extra
    finally:
        if close_db:
            db.close()

CARDS_PER_PAGE = 10

def build_cards_page_keyboard(cards: list, page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(cards) + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * CARDS_PER_PAGE
    page_cards = cards[start:start + CARDS_PER_PAGE]

    keyboard = []
    for card in page_cards:
        keyboard.append([InlineKeyboardButton(text=card['name'], callback_data=f"cview_{_card_hash(card['name'])}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="«", callback_data=f"cpage_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="»", callback_data=f"cpage_{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton(text="Добавить карту", style="success", callback_data="cadd")])
    keyboard.append([InlineKeyboardButton(text="« Назад к админ-панели", style="primary", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
from sqlalchemy import func

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

MSK = pytz.timezone('Europe/Moscow')

async def send_log(text: str):
    if not LOG_CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=LOG_CHAT_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"send_log error: {e}")

def fmt_user(first_name, username, telegram_id) -> str:
    name = first_name or username or str(telegram_id)
    uname = f" (@{username})" if username else ""
    return f"{name}{uname} [<code>{telegram_id}</code>]"

def fmt_time() -> str:
    return datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК")

openai_client = None
if OPENAI_API_KEY:
    openai_client = OpenAI(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY,
        timeout=60.0
    )

class States(StatesGroup):
    QUESTION1 = State()
    TOPUP_AMOUNT = State()
    TOPUP_METHOD = State()
    BROADCAST = State()
    INDIVIDUAL = State()
    ANSWER_READING = State()
    SET_PRICE = State()
    SET_INDIVIDUAL_PRICE = State()
    SET_PALM_PRICE = State()
    SET_COMPAT_PRICE = State()
    RESET_USER = State()
    PALM_PHOTO = State()
    COMPAT_NAME1 = State()
    COMPAT_NAME2 = State()
    BALANCE_USER = State()
    BALANCE_AMOUNT = State()
    ADMIN_ADD_CARD_PHOTO = State()
    ADMIN_ADD_CARD_NAME = State()

QUESTION = "Какой вопрос вас волнует в данный момент? Опишите ситуацию более детально и кого она касается. Можно записать аудио."

TOPUP_AMOUNTS = [100, 200, 500, 1000, 2000, 5000]

def get_card_of_the_day(db):
    today = datetime.now(pytz.UTC).date()
    
    card_setting = db.query(Setting).filter(Setting.key == 'card_of_the_day').first()
    date_setting = db.query(Setting).filter(Setting.key == 'card_of_the_day_date').first()
    
    need_new_card = not card_setting or not date_setting or date_setting.value != str(today)

    active = get_active_cards(db)

    if need_new_card:
        selected_card = random.choice(active)
        card_json = json.dumps({"name": selected_card["name"]}, ensure_ascii=False)

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
        return selected_card
    else:
        try:
            saved = json.loads(card_setting.value)
            card_name = saved.get("name", "")
            for card in active:
                if card["name"] == card_name:
                    return card
        except Exception:
            pass
        return random.choice(active)

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


async def create_yookassa_payment(amount: float, order_id: int, user_id: int) -> dict:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return {'success': False, 'error': 'ЮKassa не настроена'}
    try:
        from yookassa import Configuration, Payment as YooPayment
        Configuration.account_id = YOOKASSA_SHOP_ID
        Configuration.secret_key = YOOKASSA_SECRET_KEY

        idempotency_key = str(uuid.uuid4())
        payload = {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/mg_Taro_bot"},
            "capture": True,
            "description": f"Пополнение баланса на {amount:.2f} руб.",
            "metadata": {"user_id": str(user_id), "request_id": str(order_id)}
        }
        payment = await asyncio.to_thread(YooPayment.create, payload, idempotency_key)

        logger.info(f"YooKassa | payment_id={payment.id} amount={amount} user_id={user_id}")
        return {
            'success': True,
            'payment_url': payment.confirmation.confirmation_url,
            'payment_id': payment.id
        }
    except Exception as e:
        logger.error(f"YooKassa create payment error: {e}")
        return {'success': False, 'error': str(e)}


async def check_yookassa_payment(payment_id: str) -> dict:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return {'success': False, 'error': 'ЮKassa не настроена'}
    try:
        from yookassa import Configuration, Payment as YooPayment
        Configuration.account_id = YOOKASSA_SHOP_ID
        Configuration.secret_key = YOOKASSA_SECRET_KEY

        payment = await asyncio.to_thread(YooPayment.find_one, payment_id)
        return {'success': True, 'paid': payment.status == 'succeeded'}
    except Exception as e:
        logger.error(f"YooKassa check payment error: {e}")
        return {'success': False, 'error': str(e)}



def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton(text="🔮 Сделать расклад", style="success", callback_data="start_standard_reading")],
        [InlineKeyboardButton(text="❤️ Совместимость пары", style="success", callback_data="compatibility")],
        [InlineKeyboardButton(text="✋ Хиромантия по фото", style="success", callback_data="start_palm_reading")],
        [InlineKeyboardButton(text="✨ Живой расклад от мастера", style="success", callback_data="individual_reading")],
        [
            InlineKeyboardButton(text="🌙 Карта дня", style="success", callback_data="daily_card"),
            InlineKeyboardButton(text="Баланс", icon_custom_emoji_id="5904462880941545555", style="primary", callback_data="show_balance")
        ],
        [
            InlineKeyboardButton(text="Помощь", icon_custom_emoji_id="6034831751308644168", style="primary", callback_data="help"),
            InlineKeyboardButton(text="Что это?", icon_custom_emoji_id="6041730074376410123", style="primary", callback_data="about_bot")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def main_menu_button():
    return InlineKeyboardButton(text="« Главное меню", style="primary", callback_data="menu")

def back_to_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[main_menu_button()]])

def admin_menu_keyboard():
    db = SessionLocal()
    try:
        demo_on = get_demo_balance(db)
    finally:
        db.close()
    keyboard = [
        [InlineKeyboardButton(icon_custom_emoji_id="5936143551854285132", text="Статистика", style="primary", callback_data="admin_stats")],
        [InlineKeyboardButton(icon_custom_emoji_id="6039630677182254664", text="Изменение баланса", style="primary", callback_data="admin_balance")],
        [InlineKeyboardButton(icon_custom_emoji_id="6034831751308644168", text="Неотвеченные расклады", style="primary", callback_data="admin_pending_readings")],
        [InlineKeyboardButton(icon_custom_emoji_id="6043874504302661409", text="Выгрузка пользователей (Excel)", style="primary", callback_data="admin_export_users")],
        [InlineKeyboardButton(icon_custom_emoji_id="6039381989985882045", text="Рассылка пользователям", style="primary", callback_data="admin_broadcast")],
        [InlineKeyboardButton(icon_custom_emoji_id="5904462880941545555", text="Цены на расклады", style="primary", callback_data="admin_set_prices")],
        [InlineKeyboardButton(icon_custom_emoji_id="6037373985400819577", text="Управление картами", style="primary", callback_data="admin_cards")],
        [InlineKeyboardButton(icon_custom_emoji_id="6032742198179532882", text="Сброс данных", style="primary", callback_data="admin_reset")],
        [InlineKeyboardButton(
            icon_custom_emoji_id="5920332557466997677",
            text="Демо баланс: ВКЛ" if demo_on else "Демо баланс: ВЫКЛ",
            style="danger" if demo_on else "success",
            callback_data="admin_toggle_demo"
        )],
        [InlineKeyboardButton(text="« Вернуться в главное меню", style="primary", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def reset_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton(text="📊 Сбросить статистику", style="danger", callback_data="admin_reset_stats")],
        [InlineKeyboardButton(text="Сбросить все данные", icon_custom_emoji_id="5774077015388852135", style="danger", callback_data="admin_reset_all")],
        [InlineKeyboardButton(text="Сбросить данные пользователя", icon_custom_emoji_id="6035084557378654059", style="danger", callback_data="admin_reset_user")],
        [InlineKeyboardButton(text="« Назад к админ-панели", style="primary", callback_data="admin_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def confirm_reset_keyboard(reset_type):
    keyboard = [
        [InlineKeyboardButton(text="Подтвердить сброс", icon_custom_emoji_id="5774022692642492953", style="success", callback_data=f"admin_confirm_reset_{reset_type}")],
        [InlineKeyboardButton(text="Отмена", icon_custom_emoji_id="5774077015388852135", style="danger", callback_data="admin_reset")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def topup_amounts_keyboard():
    keyboard = []
    for i in range(0, len(TOPUP_AMOUNTS), 2):
        row = []
        row.append(InlineKeyboardButton(text=f"{TOPUP_AMOUNTS[i]} руб.", style="success", callback_data=f"topup_amount_{TOPUP_AMOUNTS[i]}"))
        if i + 1 < len(TOPUP_AMOUNTS):
            row.append(InlineKeyboardButton(text=f"{TOPUP_AMOUNTS[i+1]} руб.", style="success", callback_data=f"topup_amount_{TOPUP_AMOUNTS[i+1]}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(icon_custom_emoji_id="6039614175917903752", text="Ввести свою сумму", style="success", callback_data="topup_custom_amount")])
    keyboard.append([main_menu_button()])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def transcribe_voice(audio_bytes: bytes) -> str:
    def _transcribe():
        audio_file = BytesIO(audio_bytes)
        audio_file.name = "voice.ogg"
        resp = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ru"
        )
        return resp.text
    return await asyncio.to_thread(_transcribe)

async def send_card_photo_safe(chat_id: int, card: dict, caption: str, reply_markup=None):
    local_path = card.get("image_path", "")
    file_id = card.get("file_id", "")
    kwargs = {"chat_id": chat_id, "caption": caption[:1024]}
    if reply_markup:
        kwargs["reply_markup"] = reply_markup

    if file_id:
        try:
            await bot.send_photo(photo=file_id, **kwargs)
            return
        except Exception as e:
            logger.error(f"Card photo send (file_id) failed ({card.get('name')}): {e}")

    if local_path and os.path.isfile(local_path):
        try:
            await bot.send_photo(photo=FSInputFile(local_path), **kwargs)
            return
        except Exception as e:
            logger.error(f"Card photo send failed ({card.get('name')}): {e}")

    await bot.send_message(chat_id=chat_id, text=caption, reply_markup=reply_markup)

async def send_cards_album(chat_id: int, cards: list):
    media = []
    for card in cards:
        file_id = card.get('file_id', '')
        local_path = card.get('image_path', '')
        if file_id:
            media.append(InputMediaPhoto(media=file_id))
        elif local_path and os.path.isfile(local_path):
            media.append(InputMediaPhoto(media=FSInputFile(local_path)))

    if len(media) >= 2:
        try:
            await bot.send_media_group(chat_id=chat_id, media=media)
            return
        except Exception as e:
            logger.error(f"send_media_group failed: {e}")
    for card in cards:
        file_id = card.get('file_id', '')
        local_path = card.get('image_path', '')
        try:
            if file_id:
                await bot.send_photo(chat_id=chat_id, photo=file_id)
            elif local_path and os.path.isfile(local_path):
                await bot.send_photo(chat_id=chat_id, photo=FSInputFile(local_path))
        except Exception:
            pass

async def generate_daily_card_description(card_name: str) -> str:
    prompt = f"""Ты профессиональный таролог. Карта дня: {card_name}.

Напиши короткое и ёмкое значение этой карты на сегодня (2-3 абзаца, 100-130 слов).
Начни с краткого значения карты, потом — что она говорит на сегодня, в конце — один конкретный совет.
Каждый абзац начинай с подходящего эмодзи. На русском, обращайся напрямую (ты/тебе), тон тёплый и честный."""

    response = await asyncio.to_thread(
        openai_client.chat.completions.create,
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Ты таролог. Пиши кратко и по делу."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=400,
        temperature=0.7
    )
    return response.choices[0].message.content.strip()


async def generate_tarot_reading(question):
    selected_cards = random.sample(get_active_cards(), 3)
    cards_str = ' · '.join(c['name'] for c in selected_cards)

    prompt = f"""Ты профессиональный таролог.

Вопрос: {question}
Карты расклада: {cards_str}

Напиши текст из 3-4 абзацев (200-250 слов) о том, что этот набор карт говорит об этой ситуации.
Каждый абзац начинай с подходящего по смыслу эмодзи (разные для каждого абзаца).
Раскрой общий смысл набора карт как единой картины — не разбирай каждую карту отдельно и не привязывай их к позициям.
Будь честным: если карты указывают на трудности или неблагоприятный исход — говори прямо, без смягчений.
Последний абзац — конкретный совет что делать.
На русском, обращайся напрямую (ты/тебе), тон глубокий и честный.

Верни только JSON: {{"full": "<текст расклада>"}}"""

    response = await asyncio.to_thread(
        openai_client.chat.completions.create,
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Ты таролог. Отвечай ТОЛЬКО в JSON формате без markdown-блоков и без лишнего текста."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=1500,
        temperature=0.7
    )
    raw = response.choices[0].message.content.strip()
    start, end = raw.find('{'), raw.rfind('}')
    if start != -1 and end > start:
        try:
            sections = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            sections = {"full": raw}
    else:
        sections = {"full": raw}
    return sections, selected_cards

async def generate_palm_reading(photo_base64_data):
    photo_url = f"data:image/jpeg;base64,{photo_base64_data}"
    
    prompt = """Ты хиромант. Сделай краткий расклад по фото ладони.

Структура (каждый пункт — 2-3 предложения, кратко и по делу):

✨ Общая характеристика — темперамент и главные черты личности
❤️ Сердечная линия — любовь и отношения
🧠 Головная линия — мышление и работа
🌱 Линия жизни — жизненный путь и здоровье
💡 3 практических совета

Если есть явные шрамы или особые знаки — упомяни кратко.
На русском, без терминов, как разговор с другом. Никогда не отказывай."""
    
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
                    max_tokens=1000,
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



async def generate_compatibility_reading(name1: str, dob1: str, name2: str, dob2: str):
    selected_cards = random.sample(get_active_cards(), 3)
    cards_str = ' · '.join(c['name'] for c in selected_cards)

    dob_info = f" ({dob1})" if dob1 else ""
    dob_info2 = f" ({dob2})" if dob2 else ""
    dob_hint = "Учитывай даты рождения в анализе совместимости." if dob1 and dob2 else ""

    prompt = f"""Ты профессиональный таролог по отношениям.

Пара: {name1}{dob_info} и {name2}{dob_info2}
Карты расклада: {cards_str}
{dob_hint}

Напиши текст из 3-4 абзацев (200-250 слов) о том, что этот набор карт говорит об этих отношениях.
Каждый абзац начинай с подходящего по смыслу эмодзи (разные для каждого абзаца).
Раскрой общий смысл набора карт как единой картины — не разбирай каждую карту отдельно и не привязывай их к позициям.
Будь честным: если карты показывают напряжение, несовместимость или риски — говори прямо, без смягчений.
Обращайся к паре по именам. Последний абзац — конкретные советы.
На русском, тон честный.

Верни только JSON: {{"full": "<текст расклада>"}}"""

    response = await asyncio.to_thread(
        openai_client.chat.completions.create,
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Ты таролог по отношениям. Отвечай ТОЛЬКО в JSON формате без markdown-блоков и без лишнего текста."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=1500,
        temperature=0.7
    )
    raw = response.choices[0].message.content.strip()
    start, end = raw.find('{'), raw.rfind('}')
    if start != -1 and end > start:
        try:
            sections = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            sections = {"full": raw}
    else:
        sections = {"full": raw}
    return sections, selected_cards


@dp.callback_query(F.data == "compatibility")
async def compatibility_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    db = SessionLocal()
    cost = get_compatibility_reading_cost(db)
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    text = (
        "❤️ <b>Совместимость пары</b>\n\n"
        "Таро откроет энергию ваших отношений, покажет сильные стороны союза, испытания и перспективы.\n\n"
        f"💰 Стоимость: <b>{cost:.0f} руб.</b>\n"
        f"💵 Ваш баланс: <b>{user.balance:.2f} руб.</b>"
    )
    keyboard = [
        [InlineKeyboardButton(text="💑 Начать расклад", style="success", callback_data="confirm_compatibility")],
        [main_menu_button()]
    ]
    db.close()
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="HTML")


@dp.callback_query(F.data == "confirm_compatibility")
async def confirm_compatibility(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    db = SessionLocal()
    cost = get_compatibility_reading_cost(db)
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    admin = is_admin(callback.from_user.id) and get_demo_balance(db)
    if not admin and user.balance < cost:
        await callback.message.edit_text(
            f"<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> <b>Недостаточно средств.</b>\n"
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
        "✍️ Введите имя и дату рождения <b>первого</b> партнёра:",
        parse_mode="HTML",
        reply_markup=back_to_menu_keyboard()
    )


def _parse_name_dob(text: str) -> tuple[str, str]:
    """Split 'Иван 01.01.1990' into ('Иван', '01.01.1990'). Falls back to (text, '') if no date found."""
    import re
    m = re.search(r'(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4})', text)
    if m:
        dob = m.group(1)
        name = text[:m.start()].strip() or text[m.end():].strip()
        return name, dob
    return text.strip(), ''


@dp.message(States.COMPAT_NAME1)
async def compat_name1(message: types.Message, state: FSMContext):
    name1, dob1 = _parse_name_dob(message.text.strip())
    await state.update_data(compat_name1=name1, compat_dob1=dob1)
    await state.set_state(States.COMPAT_NAME2)
    await message.answer(
        "✍️ Введите имя и дату рождения <b>второго</b> партнёра:",
        parse_mode="HTML"
    )


@dp.message(States.COMPAT_NAME2)
async def compat_name2(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name1 = data.get("compat_name1", "")
    dob1 = data.get("compat_dob1", "")
    name2, dob2 = _parse_name_dob(message.text.strip())

    db = SessionLocal()
    cost = get_compatibility_reading_cost(db)
    user = get_or_create_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)
    admin = is_admin(message.from_user.id) and get_demo_balance(db)

    if not admin and user.balance < cost:
        await message.answer(
            f"<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Недостаточно средств.\nСтоимость: {cost:.0f} руб.\nБаланс: {user.balance:.2f} руб.",
            reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
        )
        await state.clear()
        db.close()
        return

    if not admin:
        user.balance -= cost
        db.add(Transaction(
            user_id=user.id,
            amount=-cost,
            type='reading',
            description=f'Совместимость пары ({cost:.0f} руб.)'
        ))

    reading = Reading(
        user_id=user.id,
        question1=f"Совместимость: {name1} ({dob1}) и {name2} ({dob2})",
        question2=name1,
        question3=name2,
        question4=dob1,
        question5=dob2,
        cost=0 if admin else cost,
        reading_type='compatibility'
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)
    reading_id = reading.id
    await send_log(
        f"💑 <b>Совместимость пары</b>\n"
        f"👤 {fmt_user(user.first_name, user.username, user.telegram_id)}\n"
        f"👫 Пара: <b>{name1}</b> ({dob1}) и <b>{name2}</b> ({dob2})\n"
        f"💰 Стоимость: <b>{'бесплатно (демо-режим)' if admin else f'{cost:.0f} руб.'}</b>\n"
        f"🆔 Расклад №{reading_id}\n"
        f"⏰ {fmt_time()}"
    )
    db.close()
    await state.clear()

    progress_msg = await message.answer("❤️ Раскладываем карты для вашей пары...")
    try:
        sections, selected_cards = await generate_compatibility_reading(name1, dob1, name2, dob2)

        db = SessionLocal()
        r = db.query(Reading).filter(Reading.id == reading_id).first()
        r.response = json.dumps(sections, ensure_ascii=False)
        db.commit()
        db.close()

        await progress_msg.delete()
        await send_cards_album(message.chat.id, selected_cards)
        await message.answer(sections.get('full', ''), reply_markup=back_to_menu_keyboard())
    except Exception as e:
        logger.error(f"Compatibility reading error: {e}")
        await progress_msg.edit_text(
            "<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Произошла ошибка при генерации расклада. Попробуйте позже.",
            reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
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
           "Если есть вопросы — напишите тех. поддержке: @mg_maria_tarolog"
    
    await message.answer(text, reply_markup=back_to_menu_keyboard())


@dp.message(Command("admin"))
async def admin_command(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> У вас нет прав для доступа к админ-панели.", reply_markup=back_to_menu_keyboard(), parse_mode="HTML")
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
        [InlineKeyboardButton(text="Пополнить баланс", icon_custom_emoji_id="5890848474563352982", style="success", callback_data="topup")],
        [main_menu_button()]
    ]
    
    db.close()
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data == "daily_card")
async def daily_card(callback: types.CallbackQuery):
    await callback.answer()
    db = SessionLocal()
    card = get_card_of_the_day(db)
    db.close()

    description = await generate_daily_card_description(card['name'])
    text = f"🌙 Карта дня: {card['name']}\n\n{description}"
    markup = InlineKeyboardMarkup(inline_keyboard=[[main_menu_button()]])

    try:
        await send_card_photo_safe(callback.from_user.id, card, text[:1024], reply_markup=markup)
        await callback.message.delete()
    except Exception:
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass

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
           "Если есть вопросы — напишите тех. поддержке: @mg_maria_tarolog"
    
    await callback.message.edit_text(text, reply_markup=back_to_menu_keyboard())

@dp.callback_query(F.data == "about_bot")
async def about_bot_callback(callback: types.CallbackQuery):
    await callback.answer()
    text = "📖 Что это?\n\n" \
           "✨ Это сервис для честных, понятных и глубоких раскладов — будь то таро или хиромантия. Мы создаем полноценные, детальные ответы, без общих фраз и шаблонов. Каждый расклад — это работа над тем, чтобы дать вам реальную поддержку, взглянуть на ситуацию под разными углами и помочь понять, что дальше.\n\n" \
           "Мы делаем это не просто «по картам», а так, чтобы вам было ясно, спокойно и понятно. Все прозрачно, честно и с заботой о вас.\n\n"
    
    keyboard = [
        [InlineKeyboardButton(text="Политика конфиденциальности", icon_custom_emoji_id="6037249452824072506", style="primary", url="https://telegra.ph/Politika-konfidencialnosti-servisa-mg-Taro-bot-06-05")],
        [InlineKeyboardButton(text="Пользовательское соглашение", icon_custom_emoji_id="6035084557378654059", style="primary", url="https://telegra.ph/Polzovatelskoe-soglashenie-servisa-mg-Taro-bot-06-05")],
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
        [InlineKeyboardButton(text=f"🔮 Расклад от помощницы ({cost} руб.)", style="success", callback_data="start_reading")],
        [InlineKeyboardButton(text=f"💎 Расклад от мастера ({individual_cost} руб.)", style="success", callback_data="start_individual_reading")],
        [InlineKeyboardButton(text=f"✋ Расклад по ладони ({palm_cost} руб.)", style="success", callback_data="start_palm_reading")],
        [main_menu_button()],
    ]
    
    await callback.message.edit_text("Выберите тип расклада:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data == "topup")
async def topup_callback(callback: types.CallbackQuery):
    await callback.answer()
    text = "💵 Пополнение баланса\n\n" \
           "Выберите сумму пополнения:"
    await callback.message.edit_text(text, reply_markup=topup_amounts_keyboard())

async def _start_yookassa_topup(amount: float, telegram_id: int, username, first_name, edit_func):
    db = SessionLocal()
    try:
        user = get_or_create_user(db, telegram_id, username, first_name)
        topup_request = TopupRequest(user_id=user.id, amount=amount, is_approved=False, is_rejected=False)
        db.add(topup_request)
        db.commit()
        db.refresh(topup_request)
        payment_result = await create_yookassa_payment(amount, topup_request.id, user.id)
        if payment_result["success"]:
            topup_request.robokassa_payment_id = payment_result["payment_id"]
            db.commit()
            await send_log(
                f"💳 <b>Создан платёж ЮKassa</b>\n"
                f"👤 {fmt_user(first_name, username, telegram_id)}\n"
                f"💰 Сумма: <b>{amount:.2f} руб.</b>\n"
                f"🆔 Payment ID: <code>{payment_result['payment_id']}</code>\n"
                f"⏰ {fmt_time()}"
            )
            keyboard = [
                [InlineKeyboardButton(text="Оплатить через ЮKassa", icon_custom_emoji_id="5769126056262898415", style="success", url=payment_result["payment_url"])],
                [InlineKeyboardButton(text="Проверить оплату", icon_custom_emoji_id="5774022692642492953", style="primary", callback_data=f"check_yookassa_{topup_request.id}")],
                [main_menu_button()]
            ]
            text = (f"💵 Пополнение на {amount:.2f} руб.\n\n"
                    "Нажмите кнопку ниже для оплаты через ЮKassa.\n"
                    "После оплаты нажмите «Проверить оплату».")
            await edit_func(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
        else:
            await edit_func(
                "<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Ошибка создания платежа: " + payment_result["error"],
                reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error creating YooKassa topup: {e}")
        await edit_func(
            "<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Ошибка. Попробуйте позже.",
            reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
        )
    finally:
        db.close()

@dp.callback_query(F.data.startswith("topup_amount_"))
async def select_topup_amount(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    amount = float(callback.data.split("_")[2])
    await state.clear()
    await _start_yookassa_topup(amount, callback.from_user.id, callback.from_user.username, callback.from_user.first_name, callback.message.edit_text)

@dp.callback_query(F.data == "topup_custom_amount")
async def topup_custom_amount_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(States.TOPUP_AMOUNT)
    await callback.message.edit_text("✍️ Введите сумму пополнения (в рублях):", reply_markup=back_to_menu_keyboard())

@dp.message(States.TOPUP_AMOUNT)
async def handle_custom_topup_amount(message: types.Message, state: FSMContext):
    text = message.text.strip().replace(',', '.') if message.text else ""
    try:
        amount = float(text)
    except ValueError:
        await message.answer('<tg-emoji emoji-id="5774077015388852135">❌</tg-emoji> Введите сумму числом, например: 350', reply_markup=back_to_menu_keyboard(), parse_mode="HTML")
        return
    if amount < 10:
        await message.answer('<tg-emoji emoji-id="5774077015388852135">❌</tg-emoji> Минимальная сумма: 10 руб.', reply_markup=back_to_menu_keyboard(), parse_mode="HTML")
        return
    await state.clear()
    await _start_yookassa_topup(amount, message.from_user.id, message.from_user.username, message.from_user.first_name, message.answer)


@dp.callback_query(F.data.startswith("check_yookassa_"))
async def check_yookassa_payment_callback(callback: types.CallbackQuery):
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
            await callback.message.edit_text(
                "<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Оплата уже подтверждена!",
                reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
            )
            return

        if topup_request.is_rejected:
            await callback.answer("❌ Запрос отклонён.", show_alert=True)
            await callback.message.edit_text(
                "<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Запрос отклонён.",
                reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
            )
            return

        if not topup_request.robokassa_payment_id:
            await callback.answer("❌ Нет данных о платеже.", show_alert=True)
            return

        # Acknowledge the callback before the async API call
        await callback.answer()

        check_result = await check_yookassa_payment(topup_request.robokassa_payment_id)

        if not check_result["success"]:
            await callback.message.edit_text(
                f"<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Ошибка проверки платежа. Попробуйте позже.",
                reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
            )
            return

        if check_result.get("paid"):
            user = db.query(User).filter(User.id == topup_request.user_id).first()
            user.balance += topup_request.amount

            db.add(Transaction(
                user_id=user.id,
                amount=topup_request.amount,
                type="topup",
                description=f"Пополнение баланса через ЮKassa ({topup_request.amount} руб.)"
            ))

            topup_request.is_approved = True
            topup_request.approved_at = datetime.now(pytz.UTC)
            db.commit()

            await callback.message.edit_text(
                f"<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Оплата успешна! Ваш баланс пополнен на {topup_request.amount:.2f} руб.\n"
                f"Текущий баланс: {user.balance:.2f} руб.",
                reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
            )

            logger.info(f"YooKassa payment {topup_request.robokassa_payment_id} approved: {topup_request.amount} RUB for user {user.telegram_id}")
            await send_log(
                f"✅ <b>Пополнение через ЮKassa подтверждено</b>\n"
                f"👤 {fmt_user(user.first_name, user.username, user.telegram_id)}\n"
                f"💰 Сумма: <b>{topup_request.amount:.2f} руб.</b>\n"
                f"💼 Баланс после: <b>{user.balance:.2f} руб.</b>\n"
                f"🆔 Payment ID: <code>{topup_request.robokassa_payment_id}</code>\n"
                f"⏰ {fmt_time()}"
            )

            for admin_id in ADMIN_USER_IDS:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=f"<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Оплата через ЮKassa подтверждена!\n\n"
                             f"ID запроса: {request_id}\n"
                             f"Пользователь: {user.first_name or 'Unknown'}\n"
                             f"Сумма: {topup_request.amount:.2f} руб.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
        else:
            retry_keyboard = [
                [InlineKeyboardButton(icon_custom_emoji_id="5775896410780079073", text="Проверить ещё раз", style="primary", callback_data=f"check_yookassa_{request_id}")],
                [main_menu_button()]
            ]
            await callback.message.edit_text(
                "<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Оплата не прошла.\n\n"
                "Если вы уже оплатили — подождите немного и нажмите «Проверить ещё раз».",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=retry_keyboard),
                parse_mode="HTML"
            )

    except Exception as e:
        logger.error(f"Error in check_yookassa_payment_callback: {e}")
        try:
            await callback.answer("❌ Произошла ошибка. Попробуйте позже.", show_alert=True)
        except Exception:
            pass
    finally:
        db.close()


@dp.callback_query(F.data == "start_standard_reading")
async def start_standard_reading(callback: types.CallbackQuery):
    await callback.answer()
    db = SessionLocal()
    cost = get_reading_cost(db)
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    
    text = "🔮 **Стандартный расклад Таро**\n\n"
    text += "Задайте свой вопрос и получите расклад с анализом карт прошлого, настоящего и будущего.\n\n"
    
    if not user.has_used_free_reading:
        text += f"🎁 Ваш первый расклад — **бесплатно**!\n"
    else:
        text += f"💰 Стоимость: {cost:.0f} руб.\n\n"
        text += f"💵 Текущий баланс: {user.balance:.2f} руб.\n"
    
    keyboard = [
        [InlineKeyboardButton(text="🎯 Заказать расклад", style="success", callback_data="confirm_standard_reading")],
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
        await state.update_data(is_free=True)
        await state.set_state(States.QUESTION1)
        db.close()
        await callback.message.edit_text(QUESTION, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", icon_custom_emoji_id="5774077015388852135", style="danger", callback_data="menu")]]))
        return

    admin_demo = is_admin(callback.from_user.id) and get_demo_balance(db)
    if not admin_demo and user.balance < cost:
        await callback.message.edit_text(
            f"<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Недостаточно средств для расклада.\n"
            f"Стоимость расклада: {cost:.2f} руб.\n"
            f"Пополните баланс, используя кнопку '💰 Баланс'.",
            reply_markup=back_to_menu_keyboard()
        , parse_mode="HTML")
        db.close()
        return

    await state.update_data(is_free=False)
    await state.set_state(States.QUESTION1)
    db.close()
    await callback.message.edit_text(QUESTION, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", icon_custom_emoji_id="5774077015388852135", style="danger", callback_data="menu")]]))

@dp.message(States.QUESTION1)
async def question1(message: types.Message, state: FSMContext):
    data = await state.get_data()
    is_free = data.get('is_free', False)

    if message.voice:
        progress_msg = await message.answer("🎙 Распознаём голосовое сообщение...")
        try:
            file_info = await bot.get_file(message.voice.file_id)
            file_bytes = await bot.download_file(file_info.file_path)
            question_text = await transcribe_voice(file_bytes.getvalue())
        except Exception:
            await progress_msg.edit_text(
                "<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Не удалось распознать голосовое. Попробуйте ещё раз или напишите текстом.",
                reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
            )
            await state.clear()
            return
        await progress_msg.edit_text("🔮 Составляем расклад...")
    elif message.text:
        question_text = message.text
        progress_msg = await message.answer("🔮 Составляем расклад...")
    else:
        await message.answer("Пожалуйста, отправьте текст или голосовое сообщение.")
        return

    db = SessionLocal()
    cost = get_reading_cost(db)
    user = get_or_create_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)

    admin = is_admin(message.from_user.id) and get_demo_balance(db)

    if not is_free and not admin and user.balance < cost:
        await progress_msg.edit_text(
            f"<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Недостаточно средств для расклада.\n"
            f"Стоимость расклада: {cost:.2f} руб.\n"
            f"Пополните баланс, используя кнопку '💰 Баланс'.",
            reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
        )
        await state.clear()
        db.close()
        return

    if not is_free and not admin:
        user.balance -= cost
        db.add(Transaction(
            user_id=user.id,
            amount=-cost,
            type='reading',
            description=f'Стандартный расклад ({cost} руб.)'
        ))

    if is_free:
        user.has_used_free_reading = True

    reading = Reading(
        user_id=user.id,
        question1=question_text,
        question2="", question3="", question4="", question5="",
        cost=0 if (is_free or admin) else cost,
        reading_type='standard'
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)
    reading_id = reading.id
    if is_free:
        cost_label = "бесплатно (первый расклад)"
    elif admin:
        cost_label = "бесплатно (демо-режим)"
    else:
        cost_label = f"{cost} руб."
    await send_log(
        f"🃏 <b>Стандартный расклад</b>\n"
        f"👤 {fmt_user(user.first_name, user.username, user.telegram_id)}\n"
        f"💰 Стоимость: <b>{cost_label}</b>\n"
        f"❓ Вопрос: {question_text[:200]}\n"
        f"⏰ {fmt_time()}"
    )
    db.close()

    await state.clear()

    try:
        sections, selected_cards = await generate_tarot_reading(question_text)

        db = SessionLocal()
        r = db.query(Reading).filter(Reading.id == reading_id).first()
        r.response = json.dumps(sections, ensure_ascii=False)
        db.commit()
        db.close()

        await progress_msg.delete()
        await send_cards_album(message.chat.id, selected_cards)
        await message.answer(sections.get('full', ''), reply_markup=back_to_menu_keyboard())
    except Exception as e:
        logger.error(f"Standard reading error: {e}")
        await progress_msg.edit_text(
            "<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Произошла ошибка при генерации расклада. Попробуйте позже.",
            reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
        )

@dp.callback_query(F.data == "individual_reading")
@dp.callback_query(F.data == "start_individual_reading")
async def show_individual_reading(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    master = MASTER_USERNAME or "@master"
    master_link = f"https://t.me/{master.lstrip('@')}"
    keyboard = [
        [InlineKeyboardButton(icon_custom_emoji_id="6034831751308644168", text="Написать мастеру", url=master_link)],
        [main_menu_button()]
    ]
    await callback.message.edit_text(
        "✨ <b>Живой расклад от мастера</b>\n\n"
        "Получите личный ответ от профессионального таролога.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "confirm_individual_reading")
async def confirm_individual_reading(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    db = SessionLocal()
    cost = get_individual_reading_cost(db)
    user = get_or_create_user(db, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)

    admin = is_admin(callback.from_user.id) and get_demo_balance(db)
    if not admin and user.balance < cost:
        await callback.message.edit_text(
            f"<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> <b>Недостаточно средств.</b>\n"
            f"Стоимость расклада: <b>{cost:.0f} руб.</b>\n"
            f"Ваш баланс: <b>{user.balance:.2f} руб.</b>\n\n"
            f"Пополните баланс через раздел 💰 Баланс.",
            reply_markup=back_to_menu_keyboard(),
            parse_mode="HTML"
        )
        db.close()
        return

    if not admin:
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
    await send_log(
        f"🔮 <b>Индивидуальный расклад</b>\n"
        f"👤 {fmt_user(user.first_name, user.username, user.telegram_id)}\n"
        f"💰 Стоимость: <b>{'бесплатно (демо-режим)' if admin else f'{cost:.0f} руб.'}</b>\n"
        f"🆔 Расклад №{reading_id}\n"
        f"⏰ {fmt_time()}"
    )
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
        "<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> <b>Оплата прошла успешно!</b>\n\n"
        "🔮 Теперь напишите мастеру — опишите свою ситуацию или задайте вопрос.\n"
        "Мастер проведёт для вас личный расклад таро.\n\n"
        "👇 Нажмите кнопку ниже, чтобы перейти к мастеру:"
    )

    keyboard_rows = []
    if master_url:
        keyboard_rows.append([InlineKeyboardButton(text="🔮 Написать мастеру", style="success", url=master_url)])
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
    await callback.message.answer("Введите ответ на расклад:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", style="primary", callback_data="admin_menu")]]))
    db.close()

@dp.message(States.ANSWER_READING)
async def send_reading_answer(message: types.Message, state: FSMContext):
    data = await state.get_data()
    reading_id = data.get('reading_id')
    
    if not reading_id:
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Произошла ошибка.", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
        await state.clear()
        return
    
    db = SessionLocal()
    reading = db.query(Reading).filter(Reading.id == reading_id).first()
    if not reading:
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Расклад не найден.", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
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
    
    await message.answer("<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Ответ отправлен пользователю!", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
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
        [InlineKeyboardButton(text="🎯 Заказать хиромантию", style="success", callback_data="confirm_palm_reading")],
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
    admin_demo = is_admin(callback.from_user.id) and get_demo_balance(db)
    if not admin_demo and user.balance < cost:
        await callback.message.edit_text(
            f"<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Недостаточно средств для расклада.\n"
            f"Стоимость расклада: {cost:.2f} руб.\n"
            f"Пополните баланс, используя кнопку '💰 Баланс'.",
            reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
        )
        db.close()
        return

    await state.set_state(States.PALM_PHOTO)
    db.close()
    await callback.message.edit_text("Пожалуйста, отправьте фото вашей ладони:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", icon_custom_emoji_id="5774077015388852135", style="danger", callback_data="menu")]]))

@dp.message(States.PALM_PHOTO, F.photo)
async def handle_palm_photo(message: types.Message, state: FSMContext):
    db = SessionLocal()
    cost = get_palm_reading_cost(db)
    user = get_or_create_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)

    admin = is_admin(message.from_user.id) and get_demo_balance(db)
    if not admin and user.balance < cost:
        await message.answer(
            f"<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Недостаточно средств для расклада.\n"
            f"Стоимость расклада: {cost:.2f} руб.\n"
            f"Пополните баланс, используя кнопку '💰 Баланс'.",
            reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
        )
        await state.clear()
        db.close()
        return

    if not admin:
        user.balance -= cost
        db.add(Transaction(
            user_id=user.id,
            amount=-cost,
            type='reading',
            description=f'Расклад по ладони ({cost} руб.)'
        ))

    reading = Reading(
        user_id=user.id,
        question1="", question2="", question3="", question4="", question5="",
        cost=0 if admin else cost,
        reading_type='palm'
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)
    reading_id = reading.id
    await send_log(
        f"✋ <b>Расклад по ладони</b>\n"
        f"👤 {fmt_user(user.first_name, user.username, user.telegram_id)}\n"
        f"💰 Стоимость: <b>{'бесплатно (демо-режим)' if admin else f'{cost:.0f} руб.'}</b>\n"
        f"🆔 Расклад №{reading_id}\n"
        f"⏰ {fmt_time()}"
    )
    db.close()

    await state.clear()
    progress_msg = await message.answer("✋ Анализируем ладонь...")

    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        photo_base64 = base64.b64encode(file_bytes.getvalue()).decode('utf-8')

        response = await generate_palm_reading(photo_base64)

        db = SessionLocal()
        r = db.query(Reading).filter(Reading.id == reading_id).first()
        r.response = response
        db.commit()
        db.close()

        await progress_msg.edit_text(f"✨ Ваш расклад по ладони готов!\n\n{response}", reply_markup=back_to_menu_keyboard())
    except Exception as e:
        logger.error(f"Palm reading error: {e}")
        await progress_msg.edit_text(
            "<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Произошла ошибка при генерации расклада. Попробуйте позже.",
            reply_markup=back_to_menu_keyboard(), parse_mode="HTML"
        )

@dp.callback_query(F.data == "admin_menu")
async def show_admin_menu(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text("🔐 Админ-панель:", reply_markup=admin_menu_keyboard())

@dp.callback_query(F.data == "admin_toggle_demo")
async def admin_toggle_demo_handler(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    await callback.answer()
    db = SessionLocal()
    current = get_demo_balance(db)
    set_demo_balance(db, not current)
    db.close()
    new_state = "включён" if not current else "выключен"
    await callback.message.edit_text(
        f"🔐 Админ-панель:\n\nДемо баланс {new_state}.",
        reply_markup=admin_menu_keyboard()
    )

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
           f"<tg-emoji emoji-id=\"6035084557378654059\">👤</tg-emoji> Всего пользователей: {total_users}\n" \
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
    
    await callback.message.edit_text(text, reply_markup=admin_menu_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_balance")
async def admin_balance(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    await callback.answer()
    keyboard = [
        [InlineKeyboardButton(text="Пополнить", style="success", callback_data="admin_balance_add")],
        [InlineKeyboardButton(text="Снять", style="danger", callback_data="admin_balance_sub")],
        [InlineKeyboardButton(text="« Назад к админ-панели", style="primary", callback_data="admin_menu")],
    ]
    await callback.message.edit_text(
        "💼 Изменение баланса\n\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )

@dp.callback_query(F.data.in_({"admin_balance_add", "admin_balance_sub"}))
async def admin_balance_action(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    await callback.answer()
    op = "add" if callback.data == "admin_balance_add" else "sub"
    await state.update_data(balance_op=op)
    await state.set_state(States.BALANCE_USER)
    action = "пополнения" if op == "add" else "снятия"
    await callback.message.edit_text(
        f"💼 Введите @username пользователя для {action} баланса:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", style="danger", callback_data="admin_menu")]])
    )

@dp.message(States.BALANCE_USER)
async def admin_balance_user(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    username = message.text.strip().lstrip("@")
    db = SessionLocal()
    user = db.query(User).filter(User.username == username).first()
    db.close()
    if not user:
        await message.answer(
            f'<tg-emoji emoji-id="5774077015388852135">❌</tg-emoji> Пользователь @{username} не найден.',
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", style="danger", callback_data="admin_menu")]])
        )
        return
    await state.update_data(balance_username=username, balance_telegram_id=user.telegram_id)
    await state.set_state(States.BALANCE_AMOUNT)
    data = await state.get_data()
    action = "добавить" if data["balance_op"] == "add" else "снять"
    await message.answer(
        f"💼 Пользователь: @{username}\nВведите сумму, которую нужно {action}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", style="danger", callback_data="admin_menu")]])
    )

@dp.message(States.BALANCE_AMOUNT)
async def admin_balance_amount(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        amount = float(message.text.strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer(
            '<tg-emoji emoji-id="5774077015388852135">❌</tg-emoji> Введите корректную сумму (положительное число).',
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    op = data["balance_op"]
    username = data["balance_username"]
    telegram_id = data["balance_telegram_id"]
    await state.clear()

    db = SessionLocal()
    user = db.query(User).filter(User.username == username).first()
    if not user:
        db.close()
        await message.answer('<tg-emoji emoji-id="5774077015388852135">❌</tg-emoji> Пользователь не найден.', parse_mode="HTML", reply_markup=admin_menu_keyboard())
        return

    if op == "add":
        user.balance += amount
        desc = f"Пополнение администратором ({amount:.2f} руб.)"
        tx_amount = amount
        action_text = f"пополнен на {amount:.2f} руб."
        log_action = "Пополнение"
    else:
        if user.balance < amount:
            db.close()
            await message.answer(
                f'<tg-emoji emoji-id="5774077015388852135">❌</tg-emoji> Недостаточно средств. Баланс: {user.balance:.2f} руб.',
                parse_mode="HTML"
            )
            return
        user.balance -= amount
        desc = f"Снятие администратором ({amount:.2f} руб.)"
        tx_amount = -amount
        action_text = f"уменьшен на {amount:.2f} руб."
        log_action = "Снятие"

    db.add(Transaction(user_id=user.id, amount=tx_amount, type="admin_adjust", description=desc))
    db.commit()

    await send_log(
        f"💼 <b>{log_action} баланса администратором</b>\n"
        f"👤 {fmt_user(user.first_name, user.username, user.telegram_id)}\n"
        f"💰 Сумма: <b>{amount:.2f} руб.</b>\n"
        f"💼 Баланс после: <b>{user.balance:.2f} руб.</b>\n"
        f"🛡 Админ: {message.from_user.first_name} [<code>{message.from_user.id}</code>]\n"
        f"⏰ {fmt_time()}"
    )

    db.close()

    try:
        emoji = "5774022692642492953" if op == "add" else "5774077015388852135"
        await bot.send_message(
            chat_id=telegram_id,
            text=f'<tg-emoji emoji-id="{emoji}">{"✅" if op == "add" else "❌"}</tg-emoji> Ваш баланс {action_text}.\nТекущий баланс: {user.balance:.2f} руб.',
            parse_mode="HTML"
        )
    except Exception:
        pass

    action_done = "пополнен" if op == "add" else "уменьшен"
    await message.answer(
        f'<tg-emoji emoji-id="5774022692642492953">✅</tg-emoji> Баланс @{username} {action_done} на {amount:.2f} руб.\nТекущий баланс: {user.balance:.2f} руб.',
        parse_mode="HTML",
        reply_markup=admin_menu_keyboard()
    )

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
        await callback.message.edit_text("<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Нет активных индивидуальных раскладов.", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
        db.close()
        return

    text = "🔮 <b>Активные индивидуальные расклады</b>\n\n"
    keyboard = []
    for reading in pending_readings:
        user = db.query(User).filter(User.id == reading.user_id).first()
        user_name = user.first_name or user.username or str(user.telegram_id) if user else "Unknown"
        text += (
            f"📌 Расклад <b>#{reading.id}</b>\n"
            f"<tg-emoji emoji-id=\"6035084557378654059\">👤</tg-emoji> Пользователь: <b>{user_name}</b> (ID: <code>{user.telegram_id if user else '?'}</code>)\n"
            f"💰 Стоимость: <b>{reading.cost:.0f} руб.</b>\n"
            f"🕐 Куплен: {reading.created_at.strftime('%d.%m.%Y %H:%M') if reading.created_at else '—'}\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(text=f"Завершить #{reading.id}", icon_custom_emoji_id="5774022692642492953", style="success", callback_data=f"complete_reading_{reading.id}")
        ])

    keyboard.append([InlineKeyboardButton(text="<< Назад к админ-панели", style="primary", callback_data="admin_menu")])

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
        await callback.message.edit_text("<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Нет активных индивидуальных раскладов.", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
        db2.close()
        return

    text = "🔮 <b>Активные индивидуальные расклады</b>\n\n"
    keyboard = []
    for r in pending_readings:
        user = db2.query(User).filter(User.id == r.user_id).first()
        user_name = user.first_name or user.username or str(user.telegram_id) if user else "Unknown"
        text += (
            f"📌 Расклад <b>#{r.id}</b>\n"
            f"<tg-emoji emoji-id=\"6035084557378654059\">👤</tg-emoji> Пользователь: <b>{user_name}</b> (ID: <code>{user.telegram_id if user else '?'}</code>)\n"
            f"💰 Стоимость: <b>{r.cost:.0f} руб.</b>\n"
            f"🕐 Куплен: {r.created_at.strftime('%d.%m.%Y %H:%M') if r.created_at else '—'}\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(text=f"Завершить #{r.id}", icon_custom_emoji_id="5774022692642492953", style="success", callback_data=f"complete_reading_{r.id}")
        ])

    keyboard.append([InlineKeyboardButton(text="<< Назад к админ-панели", style="primary", callback_data="admin_menu")])
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
    await callback.message.edit_text("<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Выгрузка отправлена!", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
    db.close()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    await state.set_state(States.BROADCAST)
    await callback.message.edit_text("Введите текст рассылки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", style="primary", callback_data="admin_menu")]]))

@dp.message(States.BROADCAST)
async def send_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> У вас нет прав для выполнения этого действия.", parse_mode="HTML")
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
    
    await message.answer(f"<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Рассылка отправлена!\nУспешно: {success_count}/{len(users)}", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
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
    compat_cost = get_compatibility_reading_cost(db)
    db.close()

    keyboard = [
        [InlineKeyboardButton(text=f"💎 Установить цену стандартного ({cost} руб.)", style="primary", callback_data="set_price_standard")],
        [InlineKeyboardButton(text=f"💎 Установить цену индивидуального ({individual_cost} руб.)", style="primary", callback_data="set_price_individual")],
        [InlineKeyboardButton(text=f"✋ Установить цену по ладони ({palm_cost} руб.)", style="primary", callback_data="set_price_palm")],
        [InlineKeyboardButton(text=f"❤️ Установить цену совместимости ({compat_cost} руб.)", style="primary", callback_data="set_price_compat")],
        [InlineKeyboardButton(text="« Назад к админ-панели", style="primary", callback_data="admin_menu")],
    ]

    await callback.message.edit_text("💰 Управление ценами:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data == "set_price_standard")
async def set_price_standard(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    await state.set_state(States.SET_PRICE)
    await callback.message.edit_text("Введите новую цену для стандартного расклада:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", style="primary", callback_data="admin_menu")]]))

@dp.message(States.SET_PRICE)
async def handle_set_standard_price(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> У вас нет прав для выполнения этого действия.", parse_mode="HTML")
        await state.clear()
        return
    
    try:
        new_price = int(message.text)
        if new_price < 0:
            raise ValueError
    except ValueError:
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Введите корректную цену (положительное число).", parse_mode="HTML")
        return
    
    db = SessionLocal()
    set_reading_cost(db, new_price)
    db.close()
    
    await message.answer(f"<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Цена стандартного расклада установлена на {new_price} руб.", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data == "set_price_individual")
async def set_price_individual(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    await state.set_state(States.SET_INDIVIDUAL_PRICE)
    await callback.message.edit_text("Введите новую цену для индивидуального расклада:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", style="primary", callback_data="admin_menu")]]))

@dp.message(States.SET_INDIVIDUAL_PRICE)
async def handle_set_individual_price(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> У вас нет прав для выполнения этого действия.", parse_mode="HTML")
        await state.clear()
        return
    
    try:
        new_price = int(message.text)
        if new_price < 0:
            raise ValueError
    except ValueError:
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Введите корректную цену (положительное число).", parse_mode="HTML")
        return
    
    db = SessionLocal()
    set_individual_reading_cost(db, new_price)
    db.close()
    
    await message.answer(f"<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Цена индивидуального расклада установлена на {new_price} руб.", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data == "set_price_palm")
async def set_price_palm(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return
    
    await callback.answer()
    await state.set_state(States.SET_PALM_PRICE)
    await callback.message.edit_text("Введите новую цену для расклада по ладони:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", style="primary", callback_data="admin_menu")]]))

@dp.message(States.SET_PALM_PRICE)
async def handle_set_palm_price(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> У вас нет прав для выполнения этого действия.", parse_mode="HTML")
        await state.clear()
        return
    
    try:
        new_price = int(message.text)
        if new_price < 0:
            raise ValueError
    except ValueError:
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Введите корректную цену (положительное число).", parse_mode="HTML")
        return
    
    db = SessionLocal()
    set_palm_reading_cost(db, new_price)
    db.close()
    
    await message.answer(f"<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Цена расклада по ладони установлена на {new_price} руб.", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data == "set_price_compat")
async def set_price_compat(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для доступа к админ-панели.", show_alert=True)
        return

    await callback.answer()
    await state.set_state(States.SET_COMPAT_PRICE)
    await callback.message.edit_text("Введите новую цену для расклада совместимости:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", style="primary", callback_data="admin_menu")]]))

@dp.message(States.SET_COMPAT_PRICE)
async def handle_set_compat_price(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> У вас нет прав для выполнения этого действия.", parse_mode="HTML")
        await state.clear()
        return

    try:
        new_price = int(message.text)
        if new_price < 0:
            raise ValueError
    except ValueError:
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Введите корректную цену (положительное число).", parse_mode="HTML")
        return

    db = SessionLocal()
    set_compatibility_reading_cost(db, new_price)
    db.close()

    await message.answer(f"<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Цена расклада совместимости установлена на {new_price} руб.", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
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
        await callback.message.edit_text("<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Статистика сброшена!", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
    
    elif reset_type == "all":
        db.query(Reading).delete()
        db.query(Transaction).delete()
        db.query(TopupRequest).delete()
        db.query(User).delete()
        db.query(Setting).delete()
        db.commit()
        await callback.message.edit_text("<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Все данные сброшены!", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
    
    elif reset_type == "user":
        await state.set_state(States.RESET_USER)
        await callback.message.edit_text("Введите Telegram ID пользователя:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Назад к админ-панели", style="primary", callback_data="admin_menu")]]))
    
    db.close()

@dp.message(States.RESET_USER)
async def reset_user(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> У вас нет прав для выполнения этого действия.", parse_mode="HTML")
        await state.clear()
        return
    
    try:
        telegram_id = int(message.text)
    except ValueError:
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Введите корректный ID (число).", parse_mode="HTML")
        return
    
    db = SessionLocal()
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    
    if not user:
        await message.answer("<tg-emoji emoji-id=\"5774077015388852135\">❌</tg-emoji> Пользователь не найден.", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
        await state.clear()
        db.close()
        return
    
    user.balance = 0
    db.query(Reading).filter(Reading.user_id == user.id).delete()
    db.query(Transaction).filter(Transaction.user_id == user.id).delete()
    db.query(TopupRequest).filter(TopupRequest.user_id == user.id).delete()
    db.commit()
    
    await message.answer(f"<tg-emoji emoji-id=\"5774022692642492953\">✅</tg-emoji> Данные пользователя {user.first_name or 'Unknown'} сброшены!", reply_markup=admin_menu_keyboard(), parse_mode="HTML")
    await state.clear()
    db.close()



@dp.callback_query(F.data == "admin_cards")
async def admin_cards_list(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    await callback.answer()
    db = SessionLocal()
    cards = get_active_cards(db)
    db.close()
    kb = build_cards_page_keyboard(cards, 0)
    await callback.message.edit_text(
        f"<b>Карты ({len(cards)} шт.)</b>\nНажмите на карту, чтобы удалить.",
        reply_markup=kb, parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("cpage_"))
async def cards_page(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    await callback.answer()
    page = int(callback.data.split("_")[1])
    db = SessionLocal()
    cards = get_active_cards(db)
    db.close()
    kb = build_cards_page_keyboard(cards, page)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass

@dp.callback_query(F.data.startswith("cview_"))
async def card_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    await callback.answer()
    h = callback.data[6:]
    db = SessionLocal()
    cards = get_active_cards(db)
    db.close()
    card = next((c for c in cards if _card_hash(c['name']) == h), None)
    if not card:
        await callback.answer("Карта не найдена", show_alert=True)
        return
    keyboard = [
        [InlineKeyboardButton(text="❌ Удалить карту", style="danger", callback_data=f"cdel_{h}")],
        [InlineKeyboardButton(text="« Назад к списку", style="primary", callback_data="admin_cards")],
    ]
    await callback.message.edit_text(
        f"Карта: <b>{card['name']}</b>\n\nУдалить эту карту из расклада?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("cdel_"))
async def card_delete(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    await callback.answer()
    h = callback.data[5:]
    db = SessionLocal()
    cards = get_active_cards(db)
    card = next((c for c in cards if _card_hash(c['name']) == h), None)
    if not card:
        db.close()
        await callback.answer("Карта не найдена", show_alert=True)
        return

    name = card['name']
    base_names = {c['name'] for c in ALL_CARDS}
    if name in base_names:
        disabled = get_disabled_cards(db)
        disabled.add(name)
        set_disabled_cards(db, disabled)
    else:
        extra = [c for c in get_extra_cards(db) if c['name'] != name]
        set_extra_cards(db, extra)

    remaining = get_active_cards(db)
    db.close()
    kb = build_cards_page_keyboard(remaining, 0)
    await callback.message.edit_text(
        f'<tg-emoji emoji-id="5774022692642492953">✅</tg-emoji> Карта «{name}» удалена.\n\n<b>Карты ({len(remaining)} шт.)</b>\nНажмите на карту, чтобы удалить.',
        reply_markup=kb, parse_mode="HTML"
    )

@dp.callback_query(F.data == "cadd")
async def card_add_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(States.ADMIN_ADD_CARD_PHOTO)
    await callback.message.edit_text(
        '<tg-emoji emoji-id="6030466823290360017">🖼</tg-emoji> Пришлите фото новой карты:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", style="danger", callback_data="admin_cards")]]),
        parse_mode="HTML"
    )

@dp.message(States.ADMIN_ADD_CARD_PHOTO)
async def card_add_photo(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    if not message.photo:
        await message.answer('<tg-emoji emoji-id="5774077015388852135">❌</tg-emoji> Пришлите фото карты.', parse_mode="HTML")
        return
    file_id = message.photo[-1].file_id
    await state.update_data(new_card_file_id=file_id)
    await state.set_state(States.ADMIN_ADD_CARD_NAME)
    await message.answer(
        '<tg-emoji emoji-id="6039614175917903752">✏️</tg-emoji> Введите название карты:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", style="danger", callback_data="admin_cards")]]),
        parse_mode="HTML"
    )

@dp.message(States.ADMIN_ADD_CARD_NAME)
async def card_add_name(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    name = message.text.strip() if message.text else ""
    if not name:
        await message.answer('<tg-emoji emoji-id="5774077015388852135">❌</tg-emoji> Введите название карты.', parse_mode="HTML")
        return
    data = await state.get_data()
    file_id = data.get('new_card_file_id')
    await state.clear()
    db = SessionLocal()
    extra = get_extra_cards(db)
    extra.append({"name": name, "file_id": file_id})
    set_extra_cards(db, extra)
    total = len(get_active_cards(db))
    db.close()
    await message.answer(
        f'<tg-emoji emoji-id="5774022692642492953">✅</tg-emoji> Карта «{name}» добавлена! Всего карт: {total}',
        reply_markup=admin_menu_keyboard(), parse_mode="HTML"
    )

@dp.callback_query(F.data == "noop")
async def noop(callback: types.CallbackQuery):
    await callback.answer()


async def send_daily_card():
    db = SessionLocal()
    try:
        card = get_card_of_the_day(db)
        users = db.query(User).all()
        local_path = card.get('image_path', '')

        description = await generate_daily_card_description(card['name'])
        text = f"🌞 Карта дня: {card['name']}\n\n{description}"

        for user in users:
            try:
                if local_path and os.path.isfile(local_path):
                    await bot.send_photo(
                        chat_id=user.telegram_id,
                        photo=FSInputFile(local_path),
                        caption=text[:1024]
                    )
                else:
                    await bot.send_message(chat_id=user.telegram_id, text=text)
            except Exception:
                pass
    finally:
        db.close()

scheduler = AsyncIOScheduler()


async def main():
    from sqlalchemy import func
    migrate_database()

    logger.info(f"Loaded ADMIN_USER_IDS: {ADMIN_USER_IDS}")
    scheduler.add_job(send_daily_card, 'cron', hour=12, minute=0, timezone=pytz.timezone('Europe/Moscow'), misfire_grace_time=3600)
    scheduler.start()

    logger.info("Bot started successfully!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

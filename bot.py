import asyncio
import logging
import os
import sqlite3
import uuid
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, InputMediaPhoto,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import (
    BOT_TOKEN, ADMIN_ID,
    CHANNEL_1_URL, CHANNEL_1_USERNAME,
    CRYPTO_PAY_TOKEN,
)
import mailer

# ==========================================
# НАСТРОЙКА ПУТЕЙ ДЛЯ ХОСТИНГА (Bothost)
# ==========================================
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "users.db")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
QR_DIR = os.path.join(DATA_DIR, "qr_codes")
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(QR_DIR, exist_ok=True)

CRYPTO_PAY_API = "https://pay.crypt.bot/api/"
TERMS_URL = "https://telegra.ph/Polzovatelskoe-soglashenie-11-15-41"
PRIVACY_URL = "https://telegra.ph/Politika-konfidencialnosti-11-15-78"
URL_MENU = "https://ibb.co/Ldr7DPjC"
URL_PROFILE = "https://ibb.co/bRPXrn8g"
URL_ROBOT = "https://ibb.co/GvrTY1dv"
URL_TARIFFS = "https://ibb.co/k27HHCGK"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
mailer.aiogram_bot = bot
dp = Dispatcher()

# ==========================================
# БАЗА
# ==========================================
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row  # Включаем доступ по имени столбца
cursor = conn.cursor()
cursor.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, sub1 INTEGER DEFAULT 0, sub2 INTEGER DEFAULT 0,
    tariff TEXT DEFAULT 'Нет', limit_msg INTEGER DEFAULT 0, limit_search INTEGER DEFAULT 0
)""")
cursor.execute("""CREATE TABLE IF NOT EXISTS posts (
    post_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT, text_content TEXT,
    media_msg_id INTEGER, media_type TEXT, media_file_id TEXT
)""")
cursor.execute("""CREATE TABLE IF NOT EXISTS user_groups (
    group_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT, chat_link TEXT, type TEXT
)""")

# Миграция (добавляет столбцы, если база старая)
cursor.execute("PRAGMA table_info(users)")
columns = [column[1] for column in cursor.fetchall()]
if "sub1" not in columns:
    cursor.execute("ALTER TABLE users ADD COLUMN sub1 INTEGER DEFAULT 0")
if "sub2" not in columns:
    cursor.execute("ALTER TABLE users ADD COLUMN sub2 INTEGER DEFAULT 0")
conn.commit()


def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        return {"user_id": user_id, "sub1": 0, "sub2": 0, "tariff": 'Нет', "limit_msg": 0, "limit_search": 0}
    return row


def update_user_limits(user_id, msg_deduct=0, search_deduct=0):
    cursor.execute("UPDATE users SET limit_msg = limit_msg - ?, limit_search = limit_search - ? WHERE user_id = ?",
                   (msg_deduct, search_deduct, user_id))
    conn.commit()


# ==========================================
# СОСТОЯНИЯ
# ==========================================
class BotStates(StatesGroup):
    auth_phone = State(); auth_code = State(); auth_2fa = State(); auth_qr_scan = State()
    promo_wait = State()
    post_add_title = State(); post_add_content = State()
    
    group_menu_wait = State()
    group_add_name = State()
    group_add_value = State()
    group_add_folder = State()
    
    campaign_select_group = State()
    campaign_limit = State(); campaign_delay = State(); campaign_delay_round = State(); campaign_confirm = State()
    parser_keyword = State()
    admin_broadcast = State(); admin_wait_id = State(); admin_wait_tariff = State(); admin_wait_custom = State()


# ==========================================
# ХЕЛПЕРЫ
# ==========================================
async def safe_delete(msg):
    try:
        await msg.delete()
    except Exception:
        pass


async def delete_msg_id(chat_id, msg_id):
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def send_step(chat_id, state, text, kb=None, parse_mode="HTML"):
    data = await state.get_data()
    prev = data.get("step_msg_id")
    if prev:
        await delete_msg_id(chat_id, prev)
    msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=parse_mode)
    await state.update_data(step_msg_id=msg.message_id)
    return msg


async def edit_step(chat_id, state, text, kb=None, parse_mode="HTML"):
    data = await state.get_data()
    msg_id = data.get("step_msg_id")
    if msg_id:
        try:
            return await bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                                               text=text, reply_markup=kb, parse_mode=parse_mode)
        except Exception:
            pass
    return await send_step(chat_id, state, text, kb, parse_mode)


def campaign_card(step: int, total: int, title: str, body: str) -> str:
    indicators = " ".join(["🟢" if i + 1 <= step else "⚪" for i in range(total)])
    return (
        f"🚀 <b>НОВАЯ РАССЫЛКА</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 <i>Шаг {step} из {total}</i>\n"
        f"{indicators}\n\n"
        f"<b>{title}</b>\n\n"
        f"{body}"
    )


# ==========================================
# КЛАВИАТУРЫ
# ==========================================
def get_sub1_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Подписаться на новости", url=CHANNEL_1_URL)],
        [InlineKeyboardButton(text="📜 Пользовательское соглашение", url=TERMS_URL)],
        [InlineKeyboardButton(text="🛡 Политика конфиденциальности", url=PRIVACY_URL)],
        [InlineKeyboardButton(text="✅ Я подписался и принимаю условия", callback_data="check_sub1", style="success")],
    ])


def get_main_menu(user_id):
    user = get_user(user_id)
    active = mailer.count_user_broadcasts(user_id)
    active_label = f"📊 Активные рассылки ({active})" if active > 0 else "📊 Активные рассылки"
    buttons = [
        [InlineKeyboardButton(text="📚 Мои посты", callback_data="my_posts", style="primary"),
         InlineKeyboardButton(text="📋 Мои группы", callback_data="my_groups", style="primary")],
        [InlineKeyboardButton(text="🚀 Новая рассылка", callback_data="new_campaign", style="success"),
         InlineKeyboardButton(text="🕵️‍♂️ Поиск чатов", callback_data="parser", style="success")],
        [InlineKeyboardButton(text=active_label, callback_data="active_campaigns", style="primary")],
        [InlineKeyboardButton(text="🛒 Тарифы", callback_data="tariffs", style="primary"),
         InlineKeyboardButton(text="👤 Профиль", callback_data="profile", style="primary")],
    ]
    if user["tariff"] == 'Нет':
        buttons.append([InlineKeyboardButton(text="🎁 Тест-драйв (75 сообщений)", callback_data="test_drive", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu", style="danger")]])


def get_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="back_menu", style="danger")]])


def get_tariffs_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пробный - 149₽ (3 дня) | 300 сообщ.", callback_data="buy_149", style="primary")],
        [InlineKeyboardButton(text="Старт - 299₽ (7 дней) | 500 сообщ.", callback_data="buy_299", style="primary")],
        [InlineKeyboardButton(text="Бизнес - 659₽ (30 дней) | 1000 сообщ.", callback_data="buy_659", style="primary")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_menu", style="danger")],
    ])


def get_main_menu_text(user):
    active = mailer.count_user_broadcasts(user["user_id"])
    stats = f"\n🚀 Активных рассылок: <b>{active}</b>" if active > 0 else ""
    return (
        f"<b>RD Mailing</b> — твой инструмент для автоматизации Telegram.\n\n"
        f"💎 Тариф: <b>{user['tariff']}</b>\n"
        f"📨 Осталось сообщений: <b>{user['limit_msg']}</b>"
        f"{stats}\n\n"
        f"<i>Выбери действие ниже:</i>"
    )


async def edit_menu(callback: types.CallbackQuery, photo_url: str, text: str, keyboard: InlineKeyboardMarkup):
    # Обрезаем текст, если он слишком длинный (защита от падения)
    if len(text) > 4000:
        text = text[:4000] + "..."
        
    try:
        await callback.message.edit_media(media=InputMediaPhoto(media=photo_url, caption=text, parse_mode="HTML"), reply_markup=keyboard)
        return
    except Exception:
        pass
    try:
        await callback.message.delete()
    except:
        pass
    try:
        await callback.message.answer_photo(photo_url, caption=text, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        local_path = "images/menu.jpg"
        if "profile" in photo_url.lower(): local_path = "images/profile.jpg"
        elif "robot" in photo_url.lower(): local_path = "images/robot.jpg"
        elif "tariff" in photo_url.lower(): local_path = "images/tariffs.jpg"
        try:
            await callback.message.answer_photo(FSInputFile(local_path), caption=text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")


# ==========================================
# СТАРТ И ПОДПИСКИ
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user = get_user(message.from_user.id)
    user_id = message.from_user.id
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE user_id = ?", (user_id,))
    is_new = cursor.fetchone()[0] == 0

    if is_new:
        try:
            await message.answer_dice(emoji="👋")
        except:
            pass

    if user["sub1"] == 1:
        caption = get_main_menu_text(user)
        try:
            await message.answer_photo(URL_MENU, caption=caption, reply_markup=get_main_menu(user_id), parse_mode="HTML")
        except:
            await message.answer_photo(FSInputFile("images/menu.jpg"), caption=caption, reply_markup=get_main_menu(user_id), parse_mode="HTML")
        return
    text = f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\nЭто <b>RD Mailing</b>.\n\n⚠️ Подпишитесь на канал и примите условия:"
    try:
        await message.answer_photo(URL_ROBOT, caption=text, reply_markup=get_sub1_kb(), parse_mode="HTML")
    except:
        await message.answer_photo(FSInputFile("images/robot.jpg"), caption=text, reply_markup=get_sub1_kb(), parse_mode="HTML")


@dp.callback_query(F.data == "check_sub1")
async def check_sub1(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    chat_id = CHANNEL_1_USERNAME
    if not chat_id.startswith("@") and not chat_id.startswith("-"):
        chat_id = f"@{chat_id}"

    try:
        member1 = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member1.status in ["member", "administrator", "creator", "restricted"]:
            cursor.execute("UPDATE users SET sub1 = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            user = get_user(user_id)
            await edit_menu(callback, URL_MENU, get_main_menu_text(user), get_main_menu(user_id))
        else:
            await callback.answer(f"❌ Вы не подписаны! (Статус: {member1.status})", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ Ошибка проверки: {e}", show_alert=True)


@dp.callback_query(F.data == "back_menu")
async def back_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id in mailer.qr_logins:
        del mailer.qr_logins[callback.from_user.id]
    qr_file = os.path.join(QR_DIR, f"qr_{callback.from_user.id}.png")
    if os.path.exists(qr_file):
        try: os.remove(qr_file)
        except: pass
        
    await state.clear()
    user_id = callback.from_user.id
    user = get_user(user_id)
    
    try:
        dummy = await callback.message.answer("...", reply_markup=ReplyKeyboardRemove())
        await dummy.delete()
    except:
        pass

    await edit_menu(callback, URL_MENU, get_main_menu_text(user), get_main_menu(user_id))


# ==========================================
# АВТОРИЗАЦИЯ АККАУНТА
# ==========================================
@dp.callback_query(F.data.startswith("auth_start"))
async def auth_start_cb(callback: types.CallbackQuery, state: FSMContext):
    if await mailer.is_authorized(callback.from_user.id):
        return await callback.answer("Аккаунт уже подключен!", show_alert=True)

    is_td = (callback.data == "auth_start_td")
    await state.update_data(td_after_auth=is_td)

    await safe_delete(callback.message)
    await callback.message.answer(
        "🔐 <b>Выберите способ входа в аккаунт</b>\n\n"
        "📱 <b>По номеру:</b> классический вход, но Telegram может ограничить отправку SMS.\n"
        "📷 <b>По QR-коду:</b> самый надёжный способ, всегда работает (нужно отсканировать в приложении).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Войти по номеру телефона", callback_data="auth_method_phone", style="primary")],
            [InlineKeyboardButton(text="📷 Войти по QR-коду", callback_data="auth_method_qr", style="success")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="back_menu", style="danger")],
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "auth_method_phone")
async def auth_method_phone(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    msg = await callback.message.answer(
        "📱 <b>Вход по номеру</b>\n\n"
        "Отправьте свой номер телефона с помощью кнопки ниже.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
                [KeyboardButton(text="❌ Отмена")]
            ],
            resize_keyboard=True, one_time_keyboard=False
        ),
        parse_mode="HTML"
    )
    await state.update_data(auth_msg_id=msg.message_id)
    await state.set_state(BotStates.auth_phone)


@dp.message(BotStates.auth_phone)
async def auth_phone(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await safe_delete(message)
        await message.answer("❌ Действие отменено.", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
        return

    if message.contact:
        phone = message.contact.phone_number
        if not phone.startswith('+'): phone = '+' + phone
    else:
        phone = message.text.strip().replace(" ", "").replace("-", "")
        if not phone.startswith('+'):
            phone = '+' + phone

    await safe_delete(message)
    data = await state.get_data()
    prev_msg_id = data.get("auth_msg_id")
    if prev_msg_id:
        await delete_msg_id(message.chat.id, prev_msg_id)

    msg = await message.answer("⏳ Отправляю код...", reply_markup=ReplyKeyboardRemove())
    
    try:
        res = await asyncio.wait_for(mailer.send_code(message.from_user.id, phone), timeout=20)
    except asyncio.TimeoutError:
        res = {"status": "error", "msg": "Превышено время ожидания. Попробуйте снова."}
    except Exception as e:
        res = {"status": "error", "msg": str(e)}

    if res["status"] == "ok":
        await safe_delete(msg)
        new_msg = await message.answer(
            "📩 <b>Код отправлен!</b>\n\n"
            "⚠️ <b>Важно:</b> введите код <u><b>ЧЕРЕЗ ПРОБЕЛ</b></u> (например: <code>5 4 2 4 5</code>), "
            "чтобы Telegram не заблокировал вход.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="🔄 Не получил код")],
                    [KeyboardButton(text="📱 Сменить номер")],
                    [KeyboardButton(text="❌ Отмена")]
                ],
                resize_keyboard=True
            ),
            parse_mode="HTML"
        )
        await state.update_data(auth_phone=phone, auth_msg_id=new_msg.message_id)
        await state.set_state(BotStates.auth_code)
    else:
        await safe_delete(msg)
        await message.answer(
            f"❌ Ошибка: {res['msg']}\n\nПопробуйте снова или вернитесь в меню.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="HTML"
        )
        await state.clear()


@dp.message(BotStates.auth_code)
async def auth_code(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await safe_delete(message)
        await message.answer("❌ Действие отменено.", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
        return
        
    if message.text == "📱 Сменить номер":
        await safe_delete(message)
        data = await state.get_data()
        prev_msg_id = data.get("auth_msg_id")
        if prev_msg_id:
            await delete_msg_id(message.chat.id, prev_msg_id)
        
        new_msg = await message.answer(
            "📱 <b>Введите новый номер телефона</b>\n\n"
            "Отправьте его с помощью кнопки ниже.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
                    [KeyboardButton(text="❌ Отмена")]
                ],
                resize_keyboard=True
            ),
            parse_mode="HTML"
        )
        await state.update_data(auth_msg_id=new_msg.message_id)
        await state.set_state(BotStates.auth_phone)
        return

    if message.text == "🔄 Не получил код":
        data = await state.get_data()
        phone = data.get("auth_phone")
        if not phone:
            return await message.answer("Ошибка: номер не найден.", parse_mode="HTML")
        
        await safe_delete(message)
        prev_msg_id = data.get("auth_msg_id")
        if prev_msg_id:
            await delete_msg_id(message.chat.id, prev_msg_id)

        msg = await message.answer("⏳ Отправляю код заново...", parse_mode="HTML")
        try:
            res = await asyncio.wait_for(mailer.send_code(message.from_user.id, phone), timeout=20)
        except asyncio.TimeoutError:
            res = {"status": "error", "msg": "Превышено время ожидания."}
        except Exception as e:
            res = {"status": "error", "msg": str(e)}

        if res["status"] == "ok":
            await safe_delete(msg)
            new_msg = await message.answer(
                "📩 <b>Код отправлен заново!</b>\n\n"
                "⚠️ Введите код <u><b>ЧЕРЕЗ ПРОБЕЛ</b></u> (например: <code>5 4 2 4 5</code>):",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[
                        [KeyboardButton(text="🔄 Не получил код")],
                        [KeyboardButton(text="📱 Сменить номер")],
                        [KeyboardButton(text="❌ Отмена")]
                    ],
                    resize_keyboard=True
                ),
                parse_mode="HTML"
            )
            await state.update_data(auth_msg_id=new_msg.message_id)
        else:
            await safe_delete(msg)
            await message.answer(
                f"❌ Ошибка: {res['msg']}",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode="HTML"
            )
        return

    if message.text == "🔄 Ввести код заново":
        await safe_delete(message)
        data = await state.get_data()
        prev_msg_id = data.get("auth_msg_id")
        if prev_msg_id:
            await delete_msg_id(message.chat.id, prev_msg_id)
            
        new_msg = await message.answer(
            "📩 <b>Введите код</b>\n\n"
            "⚠️ Введите код <u><b>ЧЕРЕЗ ПРОБЕЛ</b></u> (например: <code>5 4 2 4 5</code>):",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="🔄 Не получил код")],
                    [KeyboardButton(text="📱 Сменить номер")],
                    [KeyboardButton(text="❌ Отмена")]
                ],
                resize_keyboard=True
            ),
            parse_mode="HTML"
        )
        await state.update_data(auth_msg_id=new_msg.message_id)
        return

    code = "".join(filter(str.isdigit, message.text))
    if not code:
        return await message.answer("❌ Введите код цифрами (например: 5 4 2 4 5).", parse_mode="HTML")

    data = await state.get_data()
    phone = data.get("auth_phone")
    await safe_delete(message)
    prev_msg_id = data.get("auth_msg_id")
    if prev_msg_id:
        await delete_msg_id(message.chat.id, prev_msg_id)

    msg = await message.answer("⏳ Проверяю код...", parse_mode="HTML")
    try:
        res = await asyncio.wait_for(mailer.sign_in(message.from_user.id, phone, code, None), timeout=20)
    except asyncio.TimeoutError:
        res = {"status": "error", "msg": "Превышено время ожидания. Попробуйте снова."}
    except Exception as e:
        res = {"status": "error", "msg": str(e)}

    if res["status"] == "ok":
        await safe_delete(msg)
        await message.answer("✅ <b>Аккаунт успешно подключен!</b>", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
        await handle_auth_success(message.from_user.id, state)
        await message.answer("🏠 Возвращайтесь в меню", reply_markup=get_back_kb(), parse_mode="HTML")
    elif res["status"] == "2fa":
        await safe_delete(msg)
        new_msg = await message.answer(
            "🔐 <b>Включена двухфакторная аутентификация.</b>\n\nВведите ваш пароль 2FA:",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="📱 Сменить номер")],
                    [KeyboardButton(text="❌ Отмена")]
                ],
                resize_keyboard=True
            ),
            parse_mode="HTML"
        )
        await state.update_data(auth_method="phone", auth_msg_id=new_msg.message_id)
        await state.set_state(BotStates.auth_2fa)
    else:
        await safe_delete(msg)
        new_msg = await message.answer(
            "😔 <b>Упс, не получилось.</b>\n\n"
            f"{res['msg']}\n\n"
            "Возможно, код введён неверно. Попробуйте снова или запросите новый код:",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="🔄 Ввести код заново"), KeyboardButton(text="🔄 Не получил код")],
                    [KeyboardButton(text="📱 Сменить номер")],
                    [KeyboardButton(text="❌ Отмена")]
                ],
                resize_keyboard=True
            ),
            parse_mode="HTML"
        )
        await state.update_data(auth_msg_id=new_msg.message_id)


# ==========================================
# QR АВТОРИЗАЦИЯ
# ==========================================
@dp.callback_query(F.data == "auth_method_qr")
async def auth_method_qr(callback: types.CallbackQuery, state: FSMContext):
    await generate_and_send_qr(callback.message.chat.id, callback.from_user.id, state)

async def generate_and_send_qr(chat_id, user_id, state: FSMContext):
    await bot.send_chat_action(chat_id, action="upload_photo")
    res = await mailer.get_qr(user_id)
    if res["status"] == "ok":
        caption = (
            "📷 <b>Вход по QR-коду</b>\n\n"
            "1. Откройте Telegram на телефоне.\n"
            "2. Перейдите в <b>Настройки → Устройства → Подключить устройство</b>.\n"
            "3. Наведите камеру на этот QR-код.\n\n"
            "<i>QR-код действует 30 секунд. Если не успели — нажмите «Обновить».</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить QR-код", callback_data="qr_refresh", style="primary")],
            [InlineKeyboardButton(text="✅ Я отсканировал / Проверить", callback_data="qr_check", style="success")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="back_menu", style="danger")]
        ])
        new_msg = await bot.send_photo(chat_id, FSInputFile(res["path"]), caption=caption, reply_markup=kb, parse_mode="HTML")
        await state.update_data(auth_msg_id=new_msg.message_id)
        await state.set_state(BotStates.auth_qr_scan)
        
        asyncio.create_task(check_qr_status(user_id, chat_id, state))
    else:
        await bot.send_message(chat_id, f"❌ Ошибка генерации QR: {res['msg']}")

@dp.callback_query(F.data == "qr_refresh")
async def qr_refresh(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    await generate_and_send_qr(callback.message.chat.id, callback.from_user.id, state)

@dp.callback_query(F.data == "qr_check")
async def qr_check(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Проверяю...")
    res = await mailer.check_qr(callback.from_user.id)
    if res["status"] == "ok":
        await handle_qr_success(callback.message.chat.id, callback.from_user.id, state)
    elif res["status"] == "2fa":
        await handle_qr_2fa(callback.message.chat.id, callback.from_user.id, state)
    else:
        await callback.answer("Еще не отсканировано или QR устарел.", show_alert=True)

async def check_qr_status(user_id, chat_id, state: FSMContext):
    for _ in range(10):
        await asyncio.sleep(3)
        res = await mailer.check_qr(user_id)
        if res["status"] == "ok":
            await handle_qr_success(chat_id, user_id, state)
            return
        elif res["status"] == "2fa":
            await handle_qr_2fa(chat_id, user_id, state)
            return
        elif res["status"] in ["error", "expired"]:
            if "устарел" in res["msg"]:
                await bot.send_message(chat_id, "⏳ <b>Время действия QR-кода истекло.</b>\n\nНажмите кнопку «🔄 Обновить QR-код».", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить QR-код", callback_data="qr_refresh", style="primary")]]))
                return
    await bot.send_message(chat_id, "⏳ <b>Время действия QR-кода истекло.</b>\n\nНажмите «🔄 Обновить QR-код», если хотите попробовать снова.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить QR-код", callback_data="qr_refresh", style="primary")]]))

async def handle_qr_success(chat_id, user_id, state: FSMContext):
    data = await state.get_data()
    msg_id = data.get("auth_msg_id")
    if msg_id:
        await delete_msg_id(chat_id, msg_id)
    await bot.send_message(chat_id, "✅ <b>Аккаунт успешно подключен!</b>", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    await handle_auth_success(user_id, state)
    await bot.send_message(chat_id, "🏠 Возвращайтесь в меню", reply_markup=get_back_kb(), parse_mode="HTML")

async def handle_qr_2fa(chat_id, user_id, state: FSMContext):
    data = await state.get_data()
    msg_id = data.get("auth_msg_id")
    if msg_id:
        await delete_msg_id(chat_id, msg_id)
    await state.update_data(auth_method="qr")
    await bot.send_message(chat_id, "🔐 <b>Включена 2FA.</b>\n\nВведите ваш пароль 2FA:", parse_mode="HTML")
    await state.set_state(BotStates.auth_2fa)


# ==========================================
# ВВОД 2FA ПАРОЛЯ
# ==========================================
@dp.message(BotStates.auth_2fa)
async def auth_2fa(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await safe_delete(message)
        await message.answer("❌ Действие отменено.", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
        return
        
    if message.text == "📱 Сменить номер":
        await safe_delete(message)
        data = await state.get_data()
        prev_msg_id = data.get("auth_msg_id")
        if prev_msg_id:
            await delete_msg_id(message.chat.id, prev_msg_id)
        
        new_msg = await message.answer(
            "📱 <b>Введите новый номер телефона</b>\n\n"
            "Отправьте его с помощью кнопки ниже.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
                    [KeyboardButton(text="❌ Отмена")]
                ],
                resize_keyboard=True
            ),
            parse_mode="HTML"
        )
        await state.update_data(auth_msg_id=new_msg.message_id)
        await state.set_state(BotStates.auth_phone)
        return

    password = message.text.strip()
    data = await state.get_data()
    method = data.get("auth_method", "phone")

    await safe_delete(message)
    prev_msg_id = data.get("auth_msg_id")
    if prev_msg_id:
        await delete_msg_id(message.chat.id, prev_msg_id)

    msg = await message.answer("⏳ Проверяю пароль...", parse_mode="HTML")
    
    try:
        if method == "qr":
            res = await mailer.sign_in_2fa_qr(message.from_user.id, password)
        else:
            res = await mailer.sign_in_2fa_phone(message.from_user.id, password)
    except asyncio.TimeoutError:
        res = {"status": "error", "msg": "Превышено время ожидания."}
    except Exception as e:
        res = {"status": "error", "msg": str(e)}

    if res["status"] == "ok":
        await safe_delete(msg)
        await message.answer("✅ <b>Аккаунт успешно подключен!</b>", reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
        await handle_auth_success(message.from_user.id, state)
        await message.answer("🏠 Возвращайтесь в меню", reply_markup=get_back_kb(), parse_mode="HTML")
    else:
        await safe_delete(msg)
        new_msg = await message.answer(
            f"❌ <b>Ошибка:</b> {res['msg']}\n\nПопробуйте снова:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="auth_retry_2fa", style="primary")],
                [InlineKeyboardButton(text="🏠 В меню", callback_data="back_menu", style="danger")]
            ]),
            parse_mode="HTML"
        )
        await state.update_data(auth_msg_id=new_msg.message_id)

@dp.callback_query(F.data == "auth_retry_2fa")
async def auth_retry_2fa(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔐 <b>Введите ваш пароль 2FA:</b>", parse_mode="HTML")
    await state.set_state(BotStates.auth_2fa)


async def handle_auth_success(user_id, state: FSMContext):
    data = await state.get_data()
    is_td = data.get("td_after_auth", False)
    await state.clear()

    if is_td:
        user = get_user(user_id)
        if user["tariff"] == "Нет":
            cursor.execute("UPDATE users SET tariff = 'Тест', limit_msg = 75, limit_search = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            try:
                await bot.send_dice(chat_id=user_id, emoji="🎰")
                await bot.send_message(
                    user_id,
                    "🎁 <b>Вам автоматически активирован Тест-драйв (75 сообщений)!</b>\n\n"
                    "Можете приступать к созданию постов и рассылке.",
                    parse_mode="HTML"
                )
            except:
                pass


# ==========================================
# ПРОФИЛЬ, ТЕСТ, ОПЛАТА
# ==========================================
async def _render_profile(callback_or_msg, state: FSMContext = None):
    user_id = callback_or_msg.from_user.id
    user = get_user(user_id)
    username = f"@{callback_or_msg.from_user.username}" if callback_or_msg.from_user.username else "Не задан"
    active = mailer.count_user_broadcasts(user_id)
    
    is_auth = await mailer.is_authorized(user_id)
    auth_status = "🟢 Подключен" if is_auth else "🔴 Не подключен"

    text = (
        f"👤 <b>Личный профиль</b>\n\n"
        f"<blockquote>"
        f"👤 Имя: <b>{callback_or_msg.from_user.first_name}</b>\n"
        f"🔗 Юзернейм: <b>{username}</b>\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"📱 Аккаунт TG: <b>{auth_status}</b>\n"
        f"💎 Тариф: <b>{user['tariff']}</b>\n"
        f"📨 Осталось сообщений: <b>{user['limit_msg']}</b>\n"
        f"🕵️‍♂️ Осталось поисков: <b>{user['limit_search']}</b>\n"
        f"🚀 Активных рассылок: <b>{active}</b>"
        f"</blockquote>\n\n"
        f"🛠 <b>Тех. поддержка:</b> @Rendertim"
    )
    kb = []
    if not is_auth:
        kb.append([InlineKeyboardButton(text="🔗 Подключить аккаунт", callback_data="auth_start", style="primary")])
    else:
        kb.append([InlineKeyboardButton(text="🟢 Аккаунт подключен", callback_data="auth_already", style="success")])

    kb.extend([
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_profile", style="success")],
        [InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="enter_promo", style="primary")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu", style="danger")],
    ])
    
    if isinstance(callback_or_msg, types.CallbackQuery):
        await edit_menu(callback_or_msg, URL_PROFILE, text, InlineKeyboardMarkup(inline_keyboard=kb))
    else:
        await callback_or_msg.answer_photo(URL_PROFILE, caption=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data == "profile")
async def show_profile(callback: types.CallbackQuery, state: FSMContext):
    await _render_profile(callback)


@dp.callback_query(F.data == "refresh_profile")
async def refresh_profile(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("🔄 Обновлено")
    await _render_profile(callback)


@dp.callback_query(F.data == "auth_already")
async def auth_already(callback: types.CallbackQuery):
    await callback.answer("✅ Ваш аккаунт Telegram уже успешно подключен к боту!", show_alert=True)


@dp.callback_query(F.data == "enter_promo")
async def enter_promo(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    await send_step(callback.message.chat.id, state, "🎟 <b>Введите ваш промокод:</b>", kb=get_cancel_kb())
    await state.set_state(BotStates.promo_wait)


@dp.message(BotStates.promo_wait)
async def process_promo(message: types.Message, state: FSMContext):
    await safe_delete(message)
    if message.text.upper() == "START20":
        await edit_step(message.chat.id, state, "✅ <b>Промокод принят!</b>\n\nСкидка 20% будет применена при оплате.", kb=get_back_kb())
    else:
        await edit_step(message.chat.id, state, "❌ <b>Неверный промокод</b>\n\nПопробуйте другой или вернитесь в меню.", kb=get_cancel_kb())
    await state.clear()


@dp.callback_query(F.data == "test_drive")
async def test_drive(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user["tariff"] != "Нет":
        return await callback.answer("Вы уже активировали тест!", show_alert=True)
    
    if not await mailer.is_authorized(callback.from_user.id):
        return await callback.message.answer(
            "🎁 <b>Активация теста</b>\n\n"
            "Для получения 75 сообщений необходимо подключить аккаунт Telegram.\n"
            "Нажмите кнопку ниже:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Подключить аккаунт", callback_data="auth_start_td", style="primary")]]),
            parse_mode="HTML"
        )

    cursor.execute("UPDATE users SET tariff = 'Тест', limit_msg = 75, limit_search = 1 WHERE user_id = ?", (user["user_id"],))
    conn.commit()
    try:
        await bot.send_dice(chat_id=callback.from_user.id, emoji="🎰")
    except Exception:
        pass
    user = get_user(callback.from_user.id)
    await edit_menu(callback, URL_MENU, get_main_menu_text(user), get_main_menu(callback.from_user.id))


@dp.callback_query(F.data == "tariffs")
async def show_tariffs(callback: types.CallbackQuery):
    await edit_menu(callback, URL_TARIFFS, "Выберите подходящий тариф:", get_tariffs_kb())


@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(callback: types.CallbackQuery):
    tariff_code = callback.data.split("_")[1]
    prices = {"149": 149, "299": 299, "659": 659}
    amounts = {"149": 1.5, "299": 3.0, "659": 6.5}
    user_email = f"{callback.from_user.id}@telegram.org"
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{CRYPTO_PAY_API}createInvoice",
                                headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN},
                                json={
                                    "asset": "USDT", "amount": str(amounts.get(tariff_code, 3.0)),
                                    "description": f"Оплата тарифа {prices.get(tariff_code)} RUB",
                                    "payload": f"{callback.from_user.id}_{tariff_code}",
                                    "metadata": {"user_email": user_email},
                                }) as resp:
            data = await resp.json()
            if data.get("ok"):
                pay_url = data["result"]["bot_invoice_url"]
                kb = [
                    [InlineKeyboardButton(text="💳 Оплатить в CryptoBot", url=pay_url, style="success")],
                    [InlineKeyboardButton(text="🔄 Я оплатил", callback_data=f"checkpay_{tariff_code}", style="primary")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="tariffs", style="danger")],
                ]
                await safe_delete(callback.message)
                await callback.message.answer(
                    f"💳 <b>Оплата тарифа</b>\n\nК оплате: <b>{prices.get(tariff_code)} ₽</b>\n\nНажмите кнопку ниже, чтобы перейти к оплате.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
            else:
                await callback.answer("Ошибка создания счёта.", show_alert=True)


@dp.callback_query(F.data.startswith("checkpay_"))
async def check_payment(callback: types.CallbackQuery):
    tariff_code = callback.data.split("_")[1]
    await callback.answer("Проверяем оплату...", show_alert=True)
    limits = {"149": (300, 3), "299": (500, 5), "659": (1000, 8)}
    tar_names = {"149": "Пробный", "299": "Старт", "659": "Бизнес"}
    limit_msg, limit_search = limits.get(tariff_code, (0, 0))
    tar_name = tar_names.get(tariff_code, "Неизвестно")
    cursor.execute("UPDATE users SET tariff = ?, limit_msg = ?, limit_search = ? WHERE user_id = ?",
                   (tar_name, limit_msg, limit_search, callback.from_user.id))
    conn.commit()

    try:
        await bot.send_dice(chat_id=callback.from_user.id, emoji="🎰")
    except Exception:
        pass

    await safe_delete(callback.message)
    await callback.message.answer(
        f"🎉🎊✨ <b>Поздравляем с покупкой!</b> ✨🎊🎉\n\n"
        f"🎯 Тариф: <b>{tar_name}</b>\n"
        f"📨 Сообщений: <b>{limit_msg}</b>\n"
        f"🕵️‍♂️ Поисков: <b>{limit_search}</b>\n\n"
        f"<i>Спасибо за доверие! Приятной работы 🚀</i>",
        reply_markup=get_back_kb(), parse_mode="HTML")


# ==========================================
# АКТИВНЫЕ РАССЫЛКИ
# ==========================================
@dp.callback_query(F.data == "active_campaigns")
async def show_active_campaigns(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    broadcasts = mailer.active_broadcasts.get(user_id, {})
    if broadcasts:
        kb = []
        for i, (bid, st) in enumerate(broadcasts.items(), 1):
            d = st["data"]
            kb.append([InlineKeyboardButton(
                text=f"👁 #{i} · {d['percent']}% · ✅{d['success']} ❌{d['fail']}",
                callback_data=f"show_progress_{bid}",
            )])
            kb.append([InlineKeyboardButton(
                text=f"🛑 Остановить #{i}",
                callback_data=f"stop_broadcast_{bid}",
                style="danger",
            )])
        kb.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu", style="danger")])
        text = (
            f"🚀 <b>У вас активных рассылок: {len(broadcasts)}</b>\n\n"
            f"<i>Можно запускать до {mailer.MAX_BROADCASTS_PER_USER} одновременно.</i>\n\n"
            f"Нажмите <b>👁</b> — чтобы открыть прогресс, <b>🛑</b> — чтобы остановить конкретную."
        )
        await edit_menu(callback, URL_MENU, text, InlineKeyboardMarkup(inline_keyboard=kb))
    else:
        kb = [[InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu", style="danger")]]
        text = "📊 <b>Активные рассылки</b>\n\nУ вас нет активных рассылок в данный момент."
        await edit_menu(callback, URL_MENU, text, InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("show_progress_"))
async def show_progress_from_list(callback: types.CallbackQuery):
    bid = callback.data.split("_", 2)[2]
    user_id = callback.from_user.id
    st = mailer.active_broadcasts.get(user_id, {}).get(bid)
    if not st:
        return await callback.answer("Рассылка уже завершена.", show_alert=True)
    old = st.get("progress_msg")
    if old:
        try:
            await old.delete()
        except Exception:
            pass
    st["collapsed"] = False
    new_msg = await callback.message.answer("⏳ Открываю прогресс...")
    st["progress_msg"] = new_msg
    await mailer.render_progress(user_id, bid)
    await callback.answer()


@dp.callback_query(F.data.startswith("collapse_"))
async def collapse_progress(callback: types.CallbackQuery):
    bid = callback.data.split("_", 1)[1]
    user_id = callback.from_user.id
    st = mailer.active_broadcasts.get(user_id, {}).get(bid)
    if not st:
        return await callback.answer("Рассылка завершена.", show_alert=True)
    st["collapsed"] = True
    st["progress_msg"] = callback.message
    await mailer.render_progress(user_id, bid)
    await callback.answer("👁 Свёрнуто")


@dp.callback_query(F.data.startswith("expand_"))
async def expand_progress(callback: types.CallbackQuery):
    bid = callback.data.split("_", 1)[1]
    user_id = callback.from_user.id
    st = mailer.active_broadcasts.get(user_id, {}).get(bid)
    if not st:
        return await callback.answer("Рассылка завершена.", show_alert=True)
    st["collapsed"] = False
    st["progress_msg"] = callback.message
    await mailer.render_progress(user_id, bid)
    await callback.answer("👁 Развёрнуто")


# ==========================================
# ПОСТЫ
# ==========================================
@dp.callback_query(F.data == "my_posts")
async def my_posts(callback: types.CallbackQuery):
    cursor.execute("SELECT post_id, title FROM posts WHERE user_id = ?", (callback.from_user.id,))
    posts = cursor.fetchall()
    kb = []
    for p in posts:
        title = p["title"] if p["title"] else "Без названия"
        kb.append([InlineKeyboardButton(text=f"📄 {title}", callback_data=f"post_view_{p['post_id']}")])
    kb.append([InlineKeyboardButton(text="➕ Добавить пост", callback_data="post_add", style="success")])
    kb.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu", style="danger")])
    text = "📚 <b>Мои посты</b>\n\nЗдесь хранятся ваши готовые сообщения." if posts else "📚 <b>Мои посты</b>\n\nУ вас пока нет сохранённых постов."
    await edit_menu(callback, URL_MENU, text, InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data == "post_add")
async def post_add(callback: types.CallbackQuery, state: FSMContext):
    if not await mailer.is_authorized(callback.from_user.id):
        return await callback.answer("⚠️ Сначала подключите аккаунт в разделе «Профиль»!", show_alert=True)
    await safe_delete(callback.message)
    await send_step(callback.message.chat.id, state,
                    "📝 <b>Новый пост</b>\n\nВведите название поста (максимум 12 символов):", kb=get_cancel_kb())
    await state.set_state(BotStates.post_add_title)


@dp.message(BotStates.post_add_title)
async def post_process_title(message: types.Message, state: FSMContext):
    await safe_delete(message)
    title = message.text.strip()
    if len(title) > 12:
        await edit_step(message.chat.id, state, "❌ <b>Слишком длинное название!</b>\n\nМаксимум 12 символов. Введите короче:", kb=get_cancel_kb())
        return
    await state.update_data(post_title=title)
    await edit_step(message.chat.id, state,
                    f"✅ <b>Название сохранено:</b> {title}\n\n📤 Теперь отправьте сам текст поста или фото с подписью:", kb=get_cancel_kb())
    await state.set_state(BotStates.post_add_content)


@dp.message(BotStates.post_add_content)
async def post_process_content(message: types.Message, state: FSMContext):
    await safe_delete(message)
    data = await state.get_data()
    title = data.get("post_title")
    
    text = message.html_text or ""
    if not text:
        text = ""

    file_id = None
    media_type = None
    if message.photo:
        file_id = message.photo[-1].file_id; media_type = "photo"
    elif message.video:
        file_id = message.video.file_id; media_type = "video"
    elif message.document:
        file_id = message.document.file_id; media_type = "document"

    await edit_step(message.chat.id, state, "⏳ <b>Сохраняю пост...</b>", kb=None)
    
    try:
        res = await mailer.save_post_to_saved_messages(message.from_user.id, text, message if media_type else None)
        if res["status"] == "ok":
            cursor.execute("INSERT INTO posts (user_id, title, text_content, media_msg_id, media_type, media_file_id) VALUES (?, ?, ?, ?, ?, ?)",
                           (message.from_user.id, title, text, res["msg_id"], media_type, file_id))
            conn.commit()
            await edit_step(message.chat.id, state, "✅ <b>Пост сохранён!</b>", kb=get_back_kb())
        else:
            await edit_step(message.chat.id, state, f"❌ <b>Ошибка:</b> {res['msg']}", kb=get_back_kb())
    except Exception as e:
        await edit_step(message.chat.id, state, f"❌ <b>Ошибка:</b> {e}", kb=get_back_kb())
    finally:
        await state.clear()


@dp.callback_query(F.data.startswith("post_view_"))
async def post_view(callback: types.CallbackQuery):
    post_id = int(callback.data.split("_")[2])
    cursor.execute("SELECT title, text_content, media_file_id, media_type FROM posts WHERE post_id = ?", (post_id,))
    p = cursor.fetchone()
    kb = [[InlineKeyboardButton(text="🗑 Удалить пост", callback_data=f"post_del_{post_id}", style="danger")],
          [InlineKeyboardButton(text="🔙 Назад к постам", callback_data="my_posts", style="danger")]]
    title = p["title"] if p["title"] else "Без названия"
    text = p["text_content"] if p["text_content"] else ""
    file_id = p["media_file_id"]
    media_type = p["media_type"]
    caption = f"📄 <b>{title}</b>\n\n{text}" if text else f"📄 <b>{title}</b>"
    await safe_delete(callback.message)
    if file_id:
        if media_type == "photo":
            await callback.message.answer_photo(file_id, caption=caption, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
        elif media_type == "video":
            await callback.message.answer_video(file_id, caption=caption, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
        else:
            await callback.message.answer_document(file_id, caption=caption, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    else:
        await callback.message.answer(caption, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("post_del_"))
async def post_delete(callback: types.CallbackQuery):
    post_id = int(callback.data.split("_")[2])
    cursor.execute("DELETE FROM posts WHERE post_id = ?", (post_id,))
    conn.commit()
    await callback.answer("Пост удалён!")
    await my_posts(callback)


# ==========================================
# ГРУППЫ
# ==========================================
@dp.callback_query(F.data == "my_groups")
async def my_groups(callback: types.CallbackQuery):
    cursor.execute("SELECT group_id, title FROM user_groups WHERE user_id = ?", (callback.from_user.id,))
    groups = cursor.fetchall()
    kb = []
    for g in groups:
        kb.append([InlineKeyboardButton(text=f"🔗 {g['title']}", callback_data=f"group_view_{g['group_id']}")])
    kb.append([InlineKeyboardButton(text="➕ Добавить группу", callback_data="group_add_choice", style="success")])
    kb.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu", style="danger")])
    
    if groups:
        text = f"📋 <b>Мои группы</b>\n\nВсего групп: {len(groups)}/100."
    else:
        text = "📋 <b>Мои группы</b>\n\nУ вас пока нет сохранённых групп."
        
    await edit_menu(callback, URL_MENU, text, InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data == "group_add_choice")
async def group_add_choice(callback: types.CallbackQuery, state: FSMContext):
    await safe_delete(callback.message)
    await send_step(callback.message.chat.id, state,
                    "📋 <b>Как вы хотите добавить группы?</b>\n\n"
                    "📁 <b>Из папки Telegram:</b> Бот возьмет готовые чаты из вашей папки (быстро и удобно).\n"
                    "🔗 <b>Вручную:</b> Вы отправите ссылки на чаты списком.",
                    kb=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📁 Добавить из папки Telegram", callback_data="group_add_folder", style="primary")],
                        [InlineKeyboardButton(text="🔗 Добавить ссылки вручную", callback_data="group_add_manual", style="success")],
                        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_menu", style="danger")]
                    ]))
    await state.set_state(BotStates.group_menu_wait)


# ВАРИАНТ 1: ДОБАВЛЕНИЕ ИЗ ПАПКИ
@dp.callback_query(F.data == "group_add_folder")
async def group_add_folder(callback: types.CallbackQuery, state: FSMContext):
    if not await mailer.is_authorized(callback.from_user.id):
        return await callback.answer("⚠️ Сначала подключите аккаунт в разделе «Профиль»!", show_alert=True)
    
    cursor.execute("SELECT COUNT(*) FROM user_groups WHERE user_id = ?", (callback.from_user.id,))
    if cursor.fetchone()[0] >= 100:
        return await callback.answer("⚠️ Достигнут лимит! Максимум 100 групп/папок.", show_alert=True)

    await safe_delete(callback.message)
    await send_step(callback.message.chat.id, state,
                    "📁 <b>Добавление из папки</b>\n\n"
                    "Введите название папки, которую вы создали в Telegram (например: <i>Работа</i> или <i>Мои чаты</i>):\n\n"
                    "⚠️ <i>Бот возьмет оттуда до 100 чатов.</i>",
                    kb=get_cancel_kb())
    await state.set_state(BotStates.group_add_folder)


@dp.message(BotStates.group_add_folder)
async def group_process_folder(message: types.Message, state: FSMContext):
    await safe_delete(message)
    folder_name = message.text.strip()
    
    await edit_step(message.chat.id, state, f"⏳ Ищу папку '{folder_name}' в вашем аккаунте...", kb=None)
    
    res = await mailer.get_chats_from_folder(message.from_user.id, folder_name)
    if res["status"] == "ok":
        chats = res["chats"]
        combined_links = ", ".join(chats)
        
        cursor.execute("INSERT INTO user_groups (user_id, title, chat_link, type) VALUES (?, ?, ?, ?)",
                       (message.from_user.id, f"📁 {folder_name}", combined_links, "folder"))
        conn.commit()
        
        await edit_step(message.chat.id, state, 
                        f"✅ <b>Группа '{folder_name}' успешно добавлена!</b>\n\n"
                        f"🔗 Взято чатов: {len(chats)} (максимум 100)", 
                        kb=get_back_kb())
    else:
        await edit_step(message.chat.id, state, f"❌ <b>Ошибка:</b> {res['msg']}", kb=get_back_kb())
    await state.clear()


# ВАРИАНТ 2: ДОБАВЛЕНИЕ ВРУЧНУЮ
@dp.callback_query(F.data == "group_add_manual")
async def group_add_manual(callback: types.CallbackQuery, state: FSMContext):
    cursor.execute("SELECT COUNT(*) FROM user_groups WHERE user_id = ?", (callback.from_user.id,))
    count = cursor.fetchone()[0]
    if count >= 100:
        return await callback.answer("⚠️ Достигнут лимит! Максимум 100 групп.", show_alert=True)

    await safe_delete(callback.message)
    await send_step(callback.message.chat.id, state,
                    "📝 <b>Шаг 1 из 2: Название группы</b>\n\n"
                    "Введите название для этой группы (например: <i>Крипто-чаты</i> или <i>Барахолка</i>):",
                    kb=get_cancel_kb())
    await state.set_state(BotStates.group_add_name)


@dp.message(BotStates.group_add_name)
async def group_process_name(message: types.Message, state: FSMContext):
    await safe_delete(message)
    title = message.text.strip()
    
    if len(title) > 30:
        await edit_step(message.chat.id, state, "❌ <b>Слишком длинное название!</b>\n\nМаксимум 30 символов. Введите короче:", kb=get_cancel_kb())
        return
        
    await state.update_data(group_title=title)
    await edit_step(message.chat.id, state,
                    f"✅ Название сохранено: <b>{title}</b>\n\n"
                    f"📝 <b>Шаг 2 из 2: Ссылки</b>\n\n"
                    f"Отправьте до <b>7 ссылок</b> через запятую:\n"
                    f"Пример: <code>@chat1, @chat2, https://t.me/chat3</code>",
                    kb=get_cancel_kb())
    await state.set_state(BotStates.group_add_value)


@dp.message(BotStates.group_add_value)
async def group_process_value(message: types.Message, state: FSMContext):
    await safe_delete(message)
    val = message.text.strip()
    
    links = [link.strip() for link in val.split(',') if link.strip()]
    
    if not links:
        await edit_step(message.chat.id, state, "❌ Вы не отправили ни одной ссылки. Попробуйте снова:", kb=get_cancel_kb())
        return

    if len(links) > 7:
        await edit_step(message.chat.id, state, 
                        f"❌ <b>Слишком много ссылок!</b>\n\nВ одной группе может быть максимум <b>7 ссылок</b>. Вы отправили: {len(links)}.", 
                        kb=get_cancel_kb())
        return
        
    data = await state.get_data()
    title = data.get("group_title", "Без названия")
    
    cursor.execute("INSERT INTO user_groups (user_id, title, chat_link, type) VALUES (?, ?, ?, ?)",
                   (message.from_user.id, title, val, "links"))
    conn.commit()
    
    await edit_step(message.chat.id, state, 
                    f"✅ <b>Группа '{title}' успешно создана!</b>\n\n"
                    f"🔗 Ссылок внутри: {len(links)}", 
                    kb=get_back_kb())
    await state.clear()


@dp.callback_query(F.data.startswith("group_view_"))
async def group_view(callback: types.CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    cursor.execute("SELECT title, chat_link FROM user_groups WHERE group_id = ?", (group_id,))
    g = cursor.fetchone()
    
    if not g:
        return await callback.answer("Группа не найдена.", show_alert=True)
        
    links = [link.strip() for link in g["chat_link"].split(',') if link.strip()]
    count = len(links)
    
    # Показываем только первые 5 ссылок, чтобы не превысить лимит Telegram
    preview = "\n".join(links[:5])
    if count > 5:
        preview += f"\n... и еще {count - 5} чатов."

    kb = [[InlineKeyboardButton(text="🗑 Удалить группу", callback_data=f"group_del_{group_id}", style="danger")],
          [InlineKeyboardButton(text="🔙 Назад к группам", callback_data="my_groups", style="danger")]]
          
    text = f"🔗 <b>{g['title']}</b>\n\nВсего чатов: <b>{count}</b>\n\n<code>{preview}</code>"
    await edit_menu(callback, URL_MENU, text, InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("group_del_"))
async def group_delete(callback: types.CallbackQuery):
    group_id = int(callback.data.split("_")[2])
    cursor.execute("DELETE FROM user_groups WHERE group_id = ?", (group_id,))
    conn.commit()
    await callback.answer("Группы удалены!")
    await my_groups(callback))


# ==========================================
# РАССЫЛКА
# ==========================================
@dp.callback_query(F.data == "new_campaign")
async def new_campaign(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user["limit_msg"] <= 0:
        return await callback.answer("❌ У вас закончились сообщения! Купите тариф.", show_alert=True)
    if mailer.count_user_broadcasts(callback.from_user.id) >= mailer.MAX_BROADCASTS_PER_USER:
        return await callback.answer(f"⚠️ Максимум {mailer.MAX_BROADCASTS_PER_USER} одновременных рассылок!", show_alert=True)
    if not await mailer.is_authorized(callback.from_user.id):
        return await callback.answer("Сначала подключите аккаунт через Профиль!", show_alert=True)

    cursor.execute("SELECT post_id, title FROM posts WHERE user_id = ?", (callback.from_user.id,))
    posts = cursor.fetchall()
    if not posts:
        return await callback.answer("У вас нет сохранённых постов!", show_alert=True)

    if mailer.count_user_broadcasts(callback.from_user.id) == 0:
        await safe_delete(callback.message)

    kb = []
    for p in posts:
        title = p["title"] if p["title"] else "Без названия"
        kb.append([InlineKeyboardButton(text=f"📄 {title}", callback_data=f"camp_post_{p['post_id']}")])
    kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="back_menu", style="danger")])
    msg = await callback.message.answer(
        campaign_card(1, 5, "Выберите пост", "Какой пост будем рассылать?"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    await state.update_data(step_msg_id=msg.message_id)


@dp.callback_query(F.data.startswith("camp_post_"))
async def campaign_post_selected(callback: types.CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split("_")[2])
    cursor.execute("SELECT media_msg_id FROM posts WHERE post_id = ?", (post_id,))
    result = cursor.fetchone()
    telegram_msg_id = result["media_msg_id"] if result else 0
    await state.update_data(camp_post_id=telegram_msg_id, selected_groups=[])

    cursor.execute("SELECT group_id, title FROM user_groups WHERE user_id = ?", (callback.from_user.id,))
    groups = cursor.fetchall()
    if not groups:
        return await callback.answer("У вас нет сохранённых групп! Сначала создайте их в разделе «Мои группы».", show_alert=True)

    kb = []
    for g in groups:
        kb.append([InlineKeyboardButton(text=f"⬜ {g['title']}", callback_data=f"camp_toggle_{g['group_id']}")])
    kb.append([InlineKeyboardButton(text="🚀 Запустить выбранные", callback_data="camp_start_selected", style="success")])
    kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="back_menu", style="danger")])
    await callback.message.edit_text(
        campaign_card(2, 5, "Выберите группы", "Можно отметить несколько."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    await state.update_data(step_msg_id=callback.message.message_id)
    await state.set_state(BotStates.campaign_select_group)


@dp.callback_query(F.data.startswith("camp_toggle_"))
async def campaign_toggle_group(callback: types.CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[2])
    data = await state.get_data()
    selected = data.get("selected_groups", [])
    
    if group_id in selected:
        selected.remove(group_id)
        await state.update_data(selected_groups=selected)
    else:
        # Получаем тип группы (папка или ручная)
        cursor.execute("SELECT chat_link, type FROM user_groups WHERE group_id = ?", (group_id,))
        row = cursor.fetchone()
        group_type = row["type"]
        new_group_links = len([l.strip() for l in row["chat_link"].split(',') if l.strip()])

        # Если это РУЧНАЯ группа, проверяем общий лимит 7
        if group_type == "links":
            current_total_links = 0
            if selected:
                placeholders = ','.join('?' * len(selected))
                cursor.execute(f"SELECT chat_link FROM user_groups WHERE group_id IN ({placeholders})", selected)
                for r in cursor.fetchall():
                    current_total_links += len([l.strip() for l in r["chat_link"].split(',') if l.strip()])
                    
            if current_total_links + new_group_links > 7:
                return await callback.answer(
                    f"⚠️ Лимит! Максимум 7 чатов для ручных групп.\n"
                    f"Выбрано: {current_total_links}. В этой группе: {new_group_links}.",
                    show_alert=True
                )
            
        selected.append(group_id)
        await state.update_data(selected_groups=selected)

    cursor.execute("SELECT group_id, title FROM user_groups WHERE user_id = ?", (callback.from_user.id,))
    groups = cursor.fetchall()
    kb = []
    for g in groups:
        icon = "✅" if g["group_id"] in selected else "⬜"
        kb.append([InlineKeyboardButton(text=f"{icon} {g['title']}", callback_data=f"camp_toggle_{g['group_id']}")])
    kb.append([InlineKeyboardButton(text=f"🚀 Запустить выбранные", callback_data="camp_start_selected", style="success")])
    kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="back_menu", style="danger")])
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data == "camp_start_selected")
async def campaign_start_selected(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_groups", [])
    if not selected:
        return await callback.answer("⚠️ Вы не выбрали ни одной группы!", show_alert=True)

    placeholders = ','.join('?' * len(selected))
    cursor.execute(f"SELECT chat_link FROM user_groups WHERE group_id IN ({placeholders})", selected)
    links = cursor.fetchall()
    
    all_links = []
    for l in links:
        all_links.extend(l["chat_link"].split(','))
        
    all_links = [link.strip() for link in all_links if link.strip()]
    total_chats = len(all_links)

    combined_links = ",".join(all_links)
    await state.update_data(camp_group_value=combined_links, total_chats=total_chats)

    body = (f"✅ Выбрано групп: <b>{len(selected)}</b>\n"
            f"💬 Всего чатов: <b>{total_chats}</b>\n\n"
            f"Сколько кругов сделать? (введите число)")
    await callback.message.edit_text(
        campaign_card(3, 5, "Количество кругов", body),
        reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.update_data(step_msg_id=callback.message.message_id)
    await state.set_state(BotStates.campaign_limit)


@dp.message(BotStates.campaign_limit)
async def campaign_limit(message: types.Message, state: FSMContext):
    await safe_delete(message)
    if not message.text.isdigit():
        await edit_step(message.chat.id, state, campaign_card(3, 5, "Количество кругов", "❌ Введите число:"), kb=get_cancel_kb())
        return
    await state.update_data(camp_limit=int(message.text))
    data = await state.get_data()

    if data.get("total_chats", 1) <= 1:
        await state.update_data(camp_delay=0)
        body = f"🔄 Кругов: <b>{data['camp_limit']}</b>\n\n⏱ Задержка между кругами (сек, минимум 60):"
        await edit_step(message.chat.id, state, campaign_card(4, 5, "Задержка между кругами", body), kb=get_cancel_kb())
        await state.set_state(BotStates.campaign_delay_round)
    else:
        body = f"🔄 Кругов: <b>{data['camp_limit']}</b>\n\n⏱ Задержка между чатами (сек, минимум 5):"
        await edit_step(message.chat.id, state, campaign_card(4, 5, "Задержка между чатами", body), kb=get_cancel_kb())
        await state.set_state(BotStates.campaign_delay)


@dp.message(BotStates.campaign_delay)
async def campaign_delay(message: types.Message, state: FSMContext):
    await safe_delete(message)
    if not message.text.isdigit():
        await edit_step(message.chat.id, state, campaign_card(4, 5, "Задержка между чатами", "❌ Введите число:"), kb=get_cancel_kb())
        return
        
    delay = int(message.text)
    if delay < 5:
        await edit_step(message.chat.id, state, campaign_card(4, 5, "Задержка между чатами", "⚠️ Минимум 5 секунд!"), kb=get_cancel_kb())
        return
        
    await state.update_data(camp_delay=delay)
    await edit_step(message.chat.id, state, campaign_card(5, 5, "Задержка между кругами", "⏱ Задержка между кругами (сек, минимум 60):"), kb=get_cancel_kb())
    await state.set_state(BotStates.campaign_delay_round)


@dp.message(BotStates.campaign_delay_round)
async def campaign_delay_round(message: types.Message, state: FSMContext):
    await safe_delete(message)
    if not message.text.isdigit():
        await edit_step(message.chat.id, state, campaign_card(5, 5, "Задержка между кругами", "❌ Введите число:"), kb=get_cancel_kb())
        return
    await state.update_data(camp_delay_round=int(message.text))
    data = await state.get_data()

    kb = [[InlineKeyboardButton(text="🚀 Запустить", callback_data="camp_start", style="success")],
          [InlineKeyboardButton(text="❌ Отмена", callback_data="back_menu", style="danger")]]
    body = (
        f"🔄 Кругов: <b>{data['camp_limit']}</b>\n"
        f"⏱ Между чатами: <b>{data.get('camp_delay', 0)}с</b>\n"
        f"⏱ Между кругами: <b>{data['camp_delay_round']}с</b>\n\n"
        f"<i>Всё верно? Запускаем?</i>"
    )
    await edit_step(message.chat.id, state, campaign_card(5, 5, "Проверка настроек", body), kb=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(BotStates.campaign_confirm)


@dp.callback_query(F.data == "camp_start")
async def campaign_start(callback: types.CallbackQuery, state: FSMContext):
    if mailer.count_user_broadcasts(callback.from_user.id) >= mailer.MAX_BROADCASTS_PER_USER:
        await state.clear()
        return await callback.answer(f"⚠️ Максимум {mailer.MAX_BROADCASTS_PER_USER} одновременных рассылок!", show_alert=True)

    data = await state.get_data()
    broadcast_id = uuid.uuid4().hex[:8]
    start_msg = await callback.message.edit_text(
        "🚀 <b>Рассылка в процессе...</b>\n\n📊 Прогресс: <code>[░░░░░░░░░░] 0%</code>\n\n<i>Для остановки — кнопка ниже.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🛑 Остановить", callback_data=f"stop_broadcast_{broadcast_id}", style="danger")
        ]]),
        parse_mode="HTML")
    target_data = {"type": "links", "value": data['camp_group_value']}
    asyncio.create_task(run_campaign_task(
        callback.from_user.id, broadcast_id, data['camp_post_id'], target_data,
        data.get('camp_delay', 0), data['camp_delay_round'], data['camp_limit'],
        start_msg))
    await state.clear()


async def run_campaign_task(user_id, broadcast_id, post_id, target_data,
                            delay, delay_round, rounds, progress_msg):
    try:
        res = await mailer.run_campaign(user_id, broadcast_id, post_id, target_data,
                                        delay, delay_round, rounds, progress_msg)
        
        final_msg = res.get("final_msg") or progress_msg
        await safe_delete(final_msg)
        
        if res["status"] == "ok":
            total_sent = res['success']
            cursor.execute("UPDATE users SET limit_msg = limit_msg - ? WHERE user_id = ?",
                           (total_sent, user_id))
            conn.commit()
            user = get_user(user_id)
            error_text = (f"\n\n⚠️ <b>Часто встречающаяся ошибка:</b> {res['error_msg']}"
                          if res.get("fail", 0) > 0 and res.get("error_msg") != "Нет" else "")

            kb_rows = [[InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu", style="danger")]]
            if mailer.count_user_broadcasts(user_id) < mailer.MAX_BROADCASTS_PER_USER:
                kb_rows.insert(0, [InlineKeyboardButton(text="🚀 Новая рассылка", callback_data="new_campaign", style="success")])

            await bot.send_message(
                user_id,
                f"🏁 <b>Рассылка завершена</b>\n\n"
                f"✅ Отправлено: <b>{total_sent}</b>\n"
                f"❌ Ошибок: <b>{res['fail']}</b>\n"
                f"📨 Остаток: <b>{user['limit_msg']}</b>{error_text}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                parse_mode="HTML")
        else:
            await bot.send_message(user_id, f"❌ <b>Ошибка:</b> {res['msg']}", reply_markup=get_back_kb(), parse_mode="HTML")
            
    except asyncio.CancelledError:
        try:
            await progress_msg.edit_text(
                "🛑 <b>Рассылка остановлена.</b>",
                reply_markup=get_back_kb(),
                parse_mode="HTML"
            )
        except Exception:
            pass


# ==========================================
# СТОП, ПАРСЕР, АДМИНКА
# ==========================================
@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    await safe_delete(message)
    
    stopped = await mailer.stop_broadcast(message.from_user.id)
    if stopped:
        await message.answer("🛑 <b>Все активные рассылки остановлены!</b>", reply_markup=get_back_kb(), parse_mode="HTML")
    else:
        await message.answer("ℹ️ Нет активных рассылок.", reply_markup=get_back_kb())


@dp.callback_query(F.data.startswith("stop_broadcast"))
async def callback_stop(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    broadcast_id = parts[2] if len(parts) >= 3 and parts[2] else None
    user_id = callback.from_user.id

    stopped = await mailer.stop_broadcast(user_id, broadcast_id)
    if stopped:
        await callback.answer("🛑 Остановлено!", show_alert=True)
    else:
        await callback.answer("ℹ️ Рассылка не найдена.", show_alert=True)


@dp.callback_query(F.data == "parser")
async def start_parser(callback: types.CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user["limit_search"] <= 0:
        return await callback.answer("❌ У вас закончились поиски! Купите тариф.", show_alert=True)
    if not await mailer.is_authorized(callback.from_user.id):
        return await callback.answer("Сначала подключите аккаунт!", show_alert=True)
    await safe_delete(callback.message)
    await send_step(callback.message.chat.id, state,
                    f"🕵️‍♂️ <b>Поиск чатов</b>\n\nВведите ключевое слово.\n<i>Доступно поисков: <b>{user['limit_search']}</b></i>", kb=get_cancel_kb())
    await state.set_state(BotStates.parser_keyword)


@dp.message(BotStates.parser_keyword)
async def process_parser(message: types.Message, state: FSMContext):
    await safe_delete(message)
    await edit_step(message.chat.id, state, "⏳ <b>Ищу чаты...</b>\nФильтрую мёртвые группы...", kb=None)
    res = await mailer.parse_chats(message.from_user.id, message.text)
    if res["status"] == "ok":
        chats = res["chats"]
        if not chats:
            await edit_step(message.chat.id, state, "😔 <b>Ничего не найдено.</b>", kb=get_back_kb())
            await state.clear()
            return
        update_user_limits(message.from_user.id, search_deduct=1)
        user = get_user(message.from_user.id)
        text = ("✅ <b>Найденные группы:</b>\n\n" +
                "\n".join([f"• {c}" for c in chats]) +
                f"\n\n🕵️‍♂️ Осталось поисков: <b>{user['limit_search']}</b>")
        await edit_step(message.chat.id, state, text, kb=get_back_kb())
    else:
        await edit_step(message.chat.id, state, f"❌ <b>Ошибка:</b> {res['msg']}", kb=get_back_kb())
    await state.clear()


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("❌ У вас нет доступа.")
    kb = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats", style="primary")],
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast", style="primary")],
        [InlineKeyboardButton(text="👤 Выдать доступ вручную", callback_data="admin_give_access", style="success")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu", style="danger")],
    ]
    await message.answer("👑 <b>Админ-панель</b>\n\nВыберите действие:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Нет прав!", show_alert=True)
    cursor.execute("SELECT COUNT(*) FROM users"); total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE tariff != 'Нет'"); active_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM posts"); total_posts = cursor.fetchone()[0]
    await callback.message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего юзеров: <b>{total_users}</b>\n"
        f"💎 С тарифом: <b>{active_users}</b>\n"
        f"📄 Всего постов: <b>{total_posts}</b>", parse_mode="HTML")


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Нет прав!", show_alert=True)
    await callback.message.answer("Введите текст для рассылки по всем пользователям:", reply_markup=get_cancel_kb())
    await state.set_state(BotStates.admin_broadcast)


@dp.message(BotStates.admin_broadcast)
async def do_admin_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    cursor.execute("SELECT user_id FROM users"); users = cursor.fetchall()
    success = 0
    for u in users:
        try:
            await bot.send_message(u["user_id"], message.text); success += 1
        except:
            pass
    await message.answer(f"✅ Отправлено: {success}")
    await state.clear()


@dp.callback_query(F.data == "admin_give_access")
async def admin_give_access(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Нет прав!", show_alert=True)
    await callback.message.answer("Введите ID или @юзернейм пользователя:", reply_markup=get_cancel_kb())
    await state.set_state(BotStates.admin_wait_id)


@dp.message(BotStates.admin_wait_id)
async def admin_process_id(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    target = message.text.strip()
    target_id = None
    if target.startswith("@"):
        try:
            chat = await bot.get_chat(target); target_id = chat.id
        except Exception as e:
            return await message.answer(f"❌ Не найдено: {e}")
    elif target.isdigit():
        target_id = int(target)
    else:
        return await message.answer("❌ Введите ID или @юзернейм.")
    await state.update_data(target_id=target_id)
    kb = [
        [InlineKeyboardButton(text="Пробный (300 сообщ, 3 поиска)", callback_data="admin_tariff_149")],
        [InlineKeyboardButton(text="Старт (500 сообщ, 5 поисков)", callback_data="admin_tariff_299")],
        [InlineKeyboardButton(text="Бизнес (1000 сообщ, 8 поисков)", callback_data="admin_tariff_659")],
        [InlineKeyboardButton(text="✏️ Выдать вручную", callback_data="admin_tariff_custom", style="success")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel", style="danger")],
    ]
    await message.answer(f"Пользователь: <code>{target}</code>\nВыберите тариф:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    await state.set_state(BotStates.admin_wait_tariff)


@dp.callback_query(F.data.startswith("admin_tariff_"))
async def admin_process_tariff(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    if callback.data == "admin_cancel":
        await state.clear()
        return await callback.message.edit_text("❌ Отменено.")
    tariff_code = callback.data.split("_")[-1]
    data = await state.get_data(); target_id = data.get("target_id")
    if tariff_code == "custom":
        await callback.message.edit_text("Введите сообщений и поисков через пробел (например: <code>500 10</code>):", reply_markup=get_cancel_kb(), parse_mode="HTML")
        await state.set_state(BotStates.admin_wait_custom)
        return
    limits = {"149": (300, 3), "299": (500, 5), "659": (1000, 8)}
    tar_names = {"149": "Пробный", "299": "Старт", "659": "Бизнес"}
    limit_msg, limit_search = limits.get(tariff_code, (0, 0))
    tar_name = tar_names.get(tariff_code, "Неизвестно")
    cursor.execute("UPDATE users SET tariff = ?, limit_msg = ?, limit_search = ? WHERE user_id = ?", (tar_name, limit_msg, limit_search, target_id))
    conn.commit()
    await callback.message.edit_text(f"✅ Выдан тариф <b>{tar_name}</b> пользователю <code>{target_id}</code>", parse_mode="HTML")
    try:
        await bot.send_message(target_id, f"🎉 <b>Вам выдан доступ!</b>\n\nТариф: <b>{tar_name}</b>\nЛимит: <b>{limit_msg}</b>", parse_mode="HTML")
    except: pass
    await state.clear()


@dp.message(BotStates.admin_wait_custom)
async def admin_process_custom(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.split(); limit_msg = int(parts[0]); limit_search = int(parts[1]) if len(parts) > 1 else 1
    except:
        return await message.answer("❌ Введите два числа через пробел.")
    data = await state.get_data(); target_id = data.get("target_id")
    cursor.execute("UPDATE users SET tariff = 'Вручную', limit_msg = ?, limit_search = ? WHERE user_id = ?", (limit_msg, limit_search, target_id))
    conn.commit()
    await message.answer(f"✅ Выдан тариф <b>Вручную</b> ({limit_msg} сообщений) пользователю <code>{target_id}</code>", parse_mode="HTML")
    try:
        await bot.send_message(target_id, f"🎉 <b>Вам выдан доступ!</b>\n\nТариф: <b>Вручную</b>\nЛимит: <b>{limit_msg}</b>", parse_mode="HTML")
    except: pass
    await state.clear()


@dp.callback_query(F.data == "admin_cancel")
async def admin_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Действие отменено.")


async def main():
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
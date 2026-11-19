import asyncio
import os
import uuid
import qrcode
from telethon import TelegramClient, functions
from telethon.errors import (
    SessionPasswordNeededError, 
    FloodWaitError, 
    PhoneCodeExpiredError, 
    PhoneCodeInvalidError, 
    PhoneNumberInvalidError,
    ApiIdInvalidError
)
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ⚠️ Если 2040 не работает, попробуй 2496 или получи свои на my.telegram.org ⚠️
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'

MAX_BROADCASTS_PER_USER = 3

if not os.path.exists("sessions"):
    os.makedirs("sessions")
if not os.path.exists("qr_codes"):
    os.makedirs("qr_codes")

active_clients = {}
code_hashes = {}
qr_logins = {}
aiogram_bot: Bot = None
active_broadcasts = {}


def count_user_broadcasts(user_id) -> int:
    return len(active_broadcasts.get(user_id, {}))


def _humanize_error(err: str) -> str:
    if not err:
        return "Неизвестная ошибка"
    e = str(err).lower()
    if "you cannot send photos" in e or "sendmediarequest" in e:
        return "🖼 Чат не поддерживает медиа"
    if "you can't write in this chat" in e:
        return "🚫 Вам запрещено писать в этот чат"
    if "user is deactivated" in e or "user_banned" in e:
        return "💀 Аккаунт получателя удалён"
    if "flood" in e or "slowmode" in e:
        return "⏳ Telegram ограничил отправку (flood)"
    if "channel_private" in e or "channelprivate" in e:
        return "🔒 Приватный канал"
    if "chat_write_forbidden" in e:
        return "🚫 Писать в чат запрещено"
    if "username_not_occupied" in e or "cannot find entity" in e:
        return "🔍 Чат не найден"
    return err[:60]


def _progress_text(data: dict, collapsed: bool) -> str:
    if collapsed:
        return (
            f"🚀 <b>Рассылка идёт</b> · <code>{data['percent']}%</code>  ·  "
            f"✅ <b>{data['success']}</b>  ❌ <b>{data['fail']}</b>"
        )
    p = data['percent']
    bar = "▓" * (p // 10) + "░" * (10 - p // 10)
    return (
        f"🚀 <b>Рассылка в процессе...</b>\n\n"
        f"📊 Прогресс: <code>[{bar}] {p}%</code>\n"
        f"📨 Отправлено: <b>{data['current']} из {data['total']}</b>\n"
        f"✅ Успешно: <b>{data['success']}</b> | ❌ Ошибок: <b>{data['fail']}</b>\n\n"
        f"<i>Последняя ошибка: {data['last_error']}</i>"
    )


def _progress_kb(user_id, bid, collapsed):
    can_new = count_user_broadcasts(user_id) < MAX_BROADCASTS_PER_USER
    if collapsed:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👁 Развернуть", callback_data=f"expand_{bid}"),
            InlineKeyboardButton(text="🛑 Стоп", callback_data=f"stop_broadcast_{bid}", style="danger"),
        ]])
    rows = [
        [InlineKeyboardButton(text="👁 Свернуть", callback_data=f"collapse_{bid}", style="primary")],
        [InlineKeyboardButton(text="🛑 Остановить", callback_data=f"stop_broadcast_{bid}", style="danger")],
    ]
    if can_new:
        rows.append([InlineKeyboardButton(text="🚀 Новая рассылка", callback_data="new_campaign", style="success")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_progress(user_id, bid):
    state = active_broadcasts.get(user_id, {}).get(bid)
    if not state:
        return
    msg = state.get("progress_msg")
    if not msg:
        return
    try:
        await msg.edit_text(
            _progress_text(state["data"], state.get("collapsed", False)),
            reply_markup=_progress_kb(user_id, bid, state.get("collapsed", False)),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def get_client(user_id):
    session_path = f"sessions/user_{user_id}"
    if user_id in active_clients:
        client = active_clients[user_id]
        if not client.is_connected():
            await client.connect()
        return client
    client = TelegramClient(
        session_path, API_ID, API_HASH,
        device_model="iPhone 14 Pro", system_version="iOS 16.5",
        app_version="9.6.3", lang_code="ru", system_lang_code="ru-RU"
    )
    await client.connect()
    active_clients[user_id] = client
    return client


async def is_authorized(user_id):
    client = await get_client(user_id)
    return await client.is_user_authorized()


# --- ВХОД ПО НОМЕРУ ---
async def send_code(user_id, phone):
    try:
        client = await get_client(user_id)
        sent_code = await client.send_code_request(phone)
        code_hashes[user_id] = sent_code.phone_code_hash
        return {"status": "ok", "hash": sent_code.phone_code_hash}
    except FloodWaitError as e:
        return {"status": "error", "msg": f"⏳ Слишком много попыток. Пожалуйста, подождите {e.seconds} секунд."}
    except PhoneNumberInvalidError:
        return {"status": "error", "msg": "❌ Неверный формат номера. Проверьте номер."}
    except ApiIdInvalidError:
        return {"status": "error", "msg": "❌ Ошибка API Telegram. Смените API_ID и API_HASH."}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


async def sign_in(user_id, phone, code, code_hash, password=None):
    client = active_clients.get(user_id)
    if not client:
        return {"status": "error", "msg": "Клиент не найден."}
    try:
        real_hash = code_hashes.get(user_id, code_hash)
        await client.sign_in(phone, code, phone_code_hash=real_hash)
        return {"status": "ok"}
    except SessionPasswordNeededError:
        if password:
            try:
                await client.sign_in(password=password)
                return {"status": "ok"}
            except Exception:
                return {"status": "error", "msg": "Неверный пароль 2FA."}
        return {"status": "2fa"}
    except PhoneCodeInvalidError:
        return {"status": "error", "msg": "Неверный код. Попробуйте снова."}
    except PhoneCodeExpiredError:
        return {"status": "error", "msg": "Код устарел. Запросите новый."}
    except FloodWaitError as e:
        return {"status": "error", "msg": f"⏳ Слишком много попыток. Подождите {e.seconds} секунд."}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# --- ВХОД ПО QR-КОДУ ---
async def get_qr(user_id):
    try:
        client = await get_client(user_id)
        if user_id in qr_logins: 
            del qr_logins[user_id]
        qr_login = await client.qr_login()
        qr_logins[user_id] = qr_login
        img_path = f"qr_codes/qr_{user_id}.png"
        img = qrcode.make(qr_login.url)
        img.save(img_path)
        return {"status": "ok", "path": img_path, "url": qr_login.url}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


async def check_qr(user_id):
    qr_login = qr_logins.get(user_id)
    if not qr_login: 
        return {"status": "error", "msg": "Сессия истекла"}
    try:
        await asyncio.wait_for(qr_login.wait(), timeout=3.0)
        return {"status": "ok"}
    except asyncio.TimeoutError:
        return {"status": "wait", "msg": "Еще не подтверждено"}
    except SessionPasswordNeededError:
        return {"status": "2fa"}
    except Exception as e:
        if "AUTH_TOKEN_EXPIRED" in str(e): 
            return {"status": "error", "msg": "QR устарел"}
        return {"status": "error", "msg": str(e)}


async def sign_in_2fa_qr(user_id, password):
    client = active_clients.get(user_id)
    try:
        await client.sign_in(password=password)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


async def save_post_to_saved_messages(user_id, text, media_msg=None):
    client = await get_client(user_id)
    if not await client.is_user_authorized():
        return {"status": "error", "msg": "Аккаунт не подключен."}
    try:
        if media_msg and aiogram_bot:
            file_id = None
            temp_path = f"temp_post_{user_id}"
            if media_msg.photo:
                file_id = media_msg.photo[-1].file_id
                temp_path += ".jpg"
            elif media_msg.video:
                file_id = media_msg.video.file_id
                temp_path += ".mp4"
            elif media_msg.document:
                file_id = media_msg.document.file_id
                temp_path += ".dat"
            if file_id:
                file = await aiogram_bot.get_file(file_id)
                await aiogram_bot.download_file(file.file_path, destination=temp_path)
                msg = await client.send_file('me', temp_path, caption=text, parse_mode='html')
                os.remove(temp_path)
                return {"status": "ok", "msg_id": msg.id}
        msg = await client.send_message('me', text, parse_mode='html')
        return {"status": "ok", "msg_id": msg.id}
    except Exception as e:
        return {"status": "error", "msg": _humanize_error(e)}


async def run_campaign(user_id, broadcast_id, post_msg_id, target_data,
                       delay_between_chats, delay_between_rounds, rounds,
                       progress_msg_obj=None):
    client = active_clients.get(user_id)
    if not client or not await client.is_user_authorized():
        return {"status": "error", "msg": "Аккаунт не подключен"}

    task = asyncio.current_task()
    state = {
        "task": task,
        "progress_msg": progress_msg_obj,
        "collapsed": False,
        "data": {"percent": 0, "current": 0, "total": 0, "success": 0, "fail": 0, "last_error": "Нет"},
    }
    active_broadcasts.setdefault(user_id, {})[broadcast_id] = state

    try:
        entities = []
        for link in target_data["value"].split(','):
            try:
                entity = await client.get_entity(link.strip())
                entities.append(entity)
            except Exception:
                pass
        if not entities:
            return {"status": "error", "msg": "Не удалось найти ни одного чата из списка."}

        total_success = 0
        total_fail = 0
        last_error = "Нет"
        total_targets = len(entities) * rounds
        state["data"]["total"] = total_targets

        orig_msg = await client.get_messages('me', ids=post_msg_id)
        if isinstance(orig_msg, list):
            orig_msg = orig_msg[0] if orig_msg else None
        if not orig_msg:
            return {"status": "error", "msg": "Пост не найден в Избранном. Возможно, он удалён."}

        current_count = 0
        for round_num in range(rounds):
            for entity in entities:
                try:
                    await client.forward_messages(entity, orig_msg, drop_author=True)
                    total_success += 1
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 1)
                    try:
                        await client.forward_messages(entity, orig_msg, drop_author=True)
                        total_success += 1
                    except Exception as e2:
                        total_fail += 1
                        last_error = _humanize_error(e2)
                except Exception as e:
                    total_fail += 1
                    last_error = _humanize_error(e)

                current_count += 1
                state["data"] = {
                    "percent": int((current_count / total_targets) * 100),
                    "current": current_count,
                    "total": total_targets,
                    "success": total_success,
                    "fail": total_fail,
                    "last_error": last_error,
                }
                await render_progress(user_id, broadcast_id)

                if delay_between_chats > 0:
                    await asyncio.sleep(delay_between_chats)
            if round_num < rounds - 1:
                await asyncio.sleep(delay_between_rounds)

        return {
            "status": "ok",
            "success": total_success,
            "fail": total_fail,
            "error_msg": last_error,
            "final_msg": state.get("progress_msg"),
        }
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return {"status": "error", "msg": _humanize_error(e)}
    finally:
        if user_id in active_broadcasts and broadcast_id in active_broadcasts[user_id]:
            del active_broadcasts[user_id][broadcast_id]
            if not active_broadcasts[user_id]:
                del active_broadcasts[user_id]


async def stop_broadcast(user_id, broadcast_id=None):
    if user_id not in active_broadcasts:
        return False
    if broadcast_id:
        if broadcast_id in active_broadcasts[user_id]:
            active_broadcasts[user_id][broadcast_id]["task"].cancel()
            return True
        return False
    for st in list(active_broadcasts[user_id].values()):
        st["task"].cancel()
    return True


async def parse_chats(user_id, keyword):
    client = active_clients.get(user_id)
    if not client or not await client.is_user_authorized():
        return {"status": "error", "msg": "Аккаунт не подключен"}
    try:
        words = keyword.strip().split()
        found_chats = set()
        for word in words[:3]:
            if len(word) < 2:
                continue
            result = await client(functions.contacts.SearchRequest(q=word, limit=20))
            for chat in result.chats:
                if hasattr(chat, 'megagroup') and chat.megagroup:
                    if hasattr(chat, 'username') and chat.username:
                        found_chats.add(f"@{chat.username}")
        return {"status": "ok", "chats": list(found_chats)[:15]}
    except Exception as e:
        return {"status": "error", "msg": _humanize_error(e)}
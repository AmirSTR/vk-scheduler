import json
import logging
import os
import random
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import vk_api
from database import Database
from config import TG_TOKEN, VK_TOKEN, ALLOWED_USER_ID

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

(WAIT_MESSAGE, WAIT_PEER_ID, WAIT_DATETIME, WAIT_REPEAT_CHOICE,
 WAIT_REPEAT_HOURS, WAIT_ADMIN_ID, WAIT_CHAT_PEER_ID, WAIT_CHAT_NAME) = range(8)

db = Database()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()

tg_app = None
user_chat_id = ALLOWED_USER_ID if ALLOWED_USER_ID else None

VK_CHATS_FILE = os.path.join(os.path.dirname(__file__), 'data', 'vk_chats.json')

CANCEL_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("❌ Отмена")]],
    resize_keyboard=True,
)

DAYS_RU = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']


# ── VK chats (JSON storage) ────────────────────────────────────────────

def _load_chats() -> list:
    os.makedirs(os.path.dirname(VK_CHATS_FILE), exist_ok=True)
    if not os.path.exists(VK_CHATS_FILE):
        return []
    try:
        with open(VK_CHATS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def _save_chats(chats: list):
    os.makedirs(os.path.dirname(VK_CHATS_FILE), exist_ok=True)
    with open(VK_CHATS_FILE, 'w', encoding='utf-8') as f:
        json.dump(chats, f, ensure_ascii=False, indent=2)


def _add_chat(peer_id: int, title: str):
    chats = [c for c in _load_chats() if c['peer_id'] != peer_id]
    chats.append({'peer_id': peer_id, 'title': title})
    _save_chats(chats)


def _remove_chat(peer_id: int):
    _save_chats([c for c in _load_chats() if c['peer_id'] != peer_id])


def _peer_label(peer_id: int) -> str:
    chat = next((c for c in _load_chats() if c['peer_id'] == peer_id), None)
    return f"{chat['title']} ({peer_id})" if chat else str(peer_id)


# ── auth & keyboard ────────────────────────────────────────────────────

def is_super_admin(user_id: int) -> bool:
    return ALLOWED_USER_ID != 0 and user_id == ALLOWED_USER_ID


def check_auth(user_id: int) -> bool:
    return ALLOWED_USER_ID == 0 or is_super_admin(user_id) or db.is_admin(user_id)


def get_kb(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("📝 Новая задача"), KeyboardButton("📋 Мои задачи")],
        [KeyboardButton("⏸ Пауза"),          KeyboardButton("▶️ Возобновить")],
        [KeyboardButton("🗑 Удалить задачу"), KeyboardButton("💬 VK Чаты")],
    ]
    if is_super_admin(user_id):
        rows.append([KeyboardButton("👥 Администраторы")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ── VK & scheduler ────────────────────────────────────────────────

async def send_vk_message(peer_id: int, message: str) -> bool:
    try:
        vk.messages.send(
            peer_id=peer_id,
            message=message,
            random_id=random.randint(1, 2**31),
        )
        logger.info(f"Отправлено в VK peer_id={peer_id}: {message[:50]}")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки в VK: {e}")
        return False


def schedule_job(task: dict):
    job_id = f"task_{task['id']}"

    async def job_func():
        ok = await send_vk_message(task['peer_id'], task['message'])
        if user_chat_id and tg_app:
            icon = "✅" if ok else "❌"
            verb = "отправлено в ВК" if ok else "ошибка отправки в ВК"
            try:
                await tg_app.bot.send_message(
                    chat_id=user_chat_id,
                    text=(
                        f"{icon} Задача #{task['id']} — {verb}\n"
                        f"📨 {task['message'][:60]}\n"
                        f"📬 {_peer_label(task['peer_id'])}"
                    ),
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления: {e}")
        if task['repeat_type'] == 'once':
            db.delete_task(task['id'])

    if task['repeat_type'] == 'once':
        scheduler.add_job(
            job_func, 'date',
            run_date=datetime.fromisoformat(task['next_run']),
            id=job_id, replace_existing=True,
        )
    elif task['repeat_type'] == 'interval':
        scheduler.add_job(
            job_func, IntervalTrigger(minutes=int(task['repeat_value'])),
            id=job_id, replace_existing=True,
            next_run_time=datetime.fromisoformat(task['next_run']),
        )
    elif task['repeat_type'] == 'daily':
        hour, minute = task['repeat_value'].split(':')
        scheduler.add_job(
            job_func, CronTrigger(hour=int(hour), minute=int(minute), timezone="Europe/Moscow"),
            id=job_id, replace_existing=True,
        )
    elif task['repeat_type'] == 'weekly':
        # repeat_value: 'days:HH:MM'  where days = '0' or '0,2,4'
        day_str, hour, minute = task['repeat_value'].split(':')
        scheduler.add_job(
            job_func,
            CronTrigger(day_of_week=day_str, hour=int(hour), minute=int(minute), timezone="Europe/Moscow"),
            id=job_id, replace_existing=True,
        )


def repeat_label(repeat_type, repeat_value):
    if repeat_type == 'once':
        return 'Без повтора'
    if repeat_type == 'interval':
        return f'Каждые {repeat_value} мин.'
    if repeat_type == 'daily':
        return f'Каждый день в {repeat_value}'
    if repeat_type == 'weekly':
        day_str, h, m = repeat_value.split(':')
        names = ', '.join(DAYS_RU[int(d)] for d in day_str.split(','))
        return f'Каждую неделю: {names} в {h}:{m}'
    return repeat_value


def parse_datetime(text: str) -> datetime | None:
    text = text.strip().lower()
    now = datetime.now()
    if text.startswith('сегодня '):
        try:
            t = datetime.strptime(text[8:], '%H:%M')
            return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        except ValueError:
            return None
    if text.startswith('завтра '):
        try:
            t = datetime.strptime(text[7:], '%H:%M')
            return (now + timedelta(days=1)).replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        except ValueError:
            return None
    try:
        return datetime.strptime(text, '%d.%m.%Y %H:%M')
    except ValueError:
        return None


# ── helpers ───────────────────────────────────────────────────────────────

def _fmt_dt(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime('%d.%m.%Y %H:%M')


def _task_card_text(t: dict) -> str:
    status = "⏸ На паузе" if t['paused'] else "▶️ Активна"
    return (
        f"{'⏸' if t['paused'] else '▶️'} Задача #{t['id']} — {status}\n"
        f"📨 {t['message'][:80]}\n"
        f"📬 {_peer_label(t['peer_id'])}\n"
        f"🕐 Следующий запуск: {_fmt_dt(t['next_run'])}\n"
        f"🔄 {repeat_label(t['repeat_type'], t['repeat_value'])}"
    )


def _task_card_kb(t: dict) -> InlineKeyboardMarkup:
    if t['paused']:
        toggle = InlineKeyboardButton("▶️ Возобновить", callback_data=f"task_resume:{t['id']}")
    else:
        toggle = InlineKeyboardButton("⏸ Пауза", callback_data=f"task_pause:{t['id']}")
    delete = InlineKeyboardButton("🗑 Удалить", callback_data=f"task_del_ask:{t['id']}")
    return InlineKeyboardMarkup([[toggle, delete]])


def _task_confirmation(task_id, data, repeat_type, repeat_value) -> str:
    return (
        f"✅ Задача #{task_id} создана!\n\n"
        f"📨 Сообщение: {data['message'][:50]}\n"
        f"📬 {_peer_label(data['peer_id'])}\n"
        f"🕐 Первая отправка: {_fmt_dt(data['next_run'])}\n"
        f"🔄 Повтор: {repeat_label(repeat_type, repeat_value)}"
    )


def _days_keyboard(selected: list) -> InlineKeyboardMarkup:
    row1, row2 = [], []
    for i, name in enumerate(DAYS_RU):
        mark = "✅" if i in selected else "☐"
        btn = InlineKeyboardButton(f"{mark} {name}", callback_data=f"mday:{i}")
        (row1 if i < 4 else row2).append(btn)
    if selected:
        done_label = f"Готово ({len(selected)} дн.) ➡️"
    else:
        done_label = "Выбери хотя бы один день"
    row3 = [InlineKeyboardButton(done_label, callback_data="mday_done")]
    return InlineKeyboardMarkup([row1, row2, row3])


# ── /start ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_chat_id
    user = update.effective_user
    if not check_auth(user.id):
        await update.message.reply_text(
            f"🔒 У вас нет доступа к этому боту.\n\n"
            f"Ваш Telegram ID: `{user.id}`\n"
            f"Отправьте его владельцу бота для получения доступа.",
            parse_mode='Markdown',
        )
        return
    user_chat_id = update.effective_chat.id
    if db.is_admin(user.id):
        db.add_admin(user.id, user.full_name or '')
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Привет! Я бот-планировщик сообщений для ВКонтакте.\n"
        "Используй кнопки ниже для управления задачами.",
        reply_markup=get_kb(user.id),
    )


# ── /add conversation ─────────────────────────────────────────────────────

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "📝 Введи текст сообщения, которое нужно отправить в ВК:",
        reply_markup=CANCEL_KB,
    )
    return WAIT_MESSAGE


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.", reply_markup=get_kb(update.effective_user.id))
    return ConversationHandler.END


async def add_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['message'] = update.message.text
    chats = _load_chats()
    if chats:
        kb = [[InlineKeyboardButton(c['title'], callback_data=f"chat_sel:{c['peer_id']}")] for c in chats]
        kb.append([InlineKeyboardButton("✏️ Ввести peer_id вручную", callback_data="chat_sel:manual")])
        await update.message.reply_text("📬 Куда отправить?", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(
            "📬 Введи peer_id чата ВК\n\n"
            "• Беседа: /convo/4 → 2000000004\n"
            "• Личка: ID пользователя\n"
            "• Группа: −ID группы\n\n"
            "Чтобы не вводить каждый раз, сохрани чат через кнопку 💬 VK Чаты",
            reply_markup=CANCEL_KB,
        )
    return WAIT_PEER_ID


async def add_peer_id_from_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    value = query.data.split(':', 1)[1]
    if value == 'manual':
        await query.edit_message_text(
            "📬 Введи peer_id чата ВК\n\n"
            "• Беседа: /convo/4 → 2000000004\n"
            "• Личка: ID пользователя\n"
            "• Группа: −ID группы"
        )
        return WAIT_PEER_ID
    peer_id = int(value)
    context.user_data['peer_id'] = peer_id
    chat = next((c for c in _load_chats() if c['peer_id'] == peer_id), None)
    await query.edit_message_text(f"📬 Выбрано: {chat['title'] if chat else peer_id}")
    await query.message.reply_text(
        "🕐 Введи дату и время первой отправки:\n\n"
        "• сегодня 14:30\n"
        "• завтра 09:00\n"
        "• 25.04.2026 14:30\n\n"
        "Время московское (МСК).",
        reply_markup=CANCEL_KB,
    )
    return WAIT_DATETIME


async def add_peer_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['peer_id'] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ peer_id должен быть числом. Попробуй ещё раз:")
        return WAIT_PEER_ID
    await update.message.reply_text(
        "🕐 Введи дату и время первой отправки:\n\n"
        "• сегодня 14:30\n"
        "• завтра 09:00\n"
        "• 25.04.2026 14:30\n\n"
        "Время московское (МСК).",
        reply_markup=CANCEL_KB,
    )
    return WAIT_DATETIME


async def add_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dt = parse_datetime(update.message.text)
    if dt is None:
        await update.message.reply_text(
            "❌ Не понял дату. Попробуй:\n"
            "• сегодня 14:30\n"
            "• завтра 09:00\n"
            "• 25.04.2026 14:30"
        )
        return WAIT_DATETIME
    context.user_data['next_run'] = dt.isoformat()
    keyboard = [
        [InlineKeyboardButton("🔂 Раз (без повтора)",        callback_data="repeat:once")],
        [InlineKeyboardButton("🔁 Каждые N минут",           callback_data="repeat:interval")],
        [InlineKeyboardButton("📅 Каждый день в это время",  callback_data="repeat:daily")],
        [InlineKeyboardButton("📆 По дням недели",           callback_data="repeat:weekly")],
    ]
    await update.message.reply_text("🔄 Выбери режим повтора:", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAIT_REPEAT_CHOICE


async def add_repeat_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    repeat_type = query.data.split(':')[1]
    context.user_data['repeat_type'] = repeat_type

    if repeat_type == 'once':
        await _save_task_from_query(query, context, repeat_type, '')
        return ConversationHandler.END

    if repeat_type == 'interval':
        await query.edit_message_text("Каждые сколько минут отправлять?\nВведи число (например: 60):")
        return WAIT_REPEAT_HOURS

    if repeat_type == 'daily':
        dt = datetime.fromisoformat(context.user_data['next_run'])
        await _save_task_from_query(query, context, repeat_type, f"{dt.hour:02d}:{dt.minute:02d}")
        return ConversationHandler.END

    if repeat_type == 'weekly':
        context.user_data['selected_days'] = []
        await query.edit_message_text(
            "📆 Выбери дни недели (можно несколько):",
            reply_markup=_days_keyboard([]),
        )
        return WAIT_REPEAT_CHOICE


async def add_weekday_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    day = int(query.data.split(':')[1])
    selected = context.user_data.get('selected_days', [])
    if day in selected:
        selected.remove(day)
    else:
        selected.append(day)
    context.user_data['selected_days'] = selected
    await query.edit_message_reply_markup(reply_markup=_days_keyboard(selected))
    return WAIT_REPEAT_CHOICE


async def add_weekday_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    selected = context.user_data.get('selected_days', [])
    if not selected:
        await query.answer("Выбери хотя бы один день!", show_alert=True)
        return WAIT_REPEAT_CHOICE
    await query.answer()
    selected.sort()
    dt = datetime.fromisoformat(context.user_data['next_run'])
    day_str = ','.join(str(d) for d in selected)
    repeat_value = f"{day_str}:{dt.hour:02d}:{dt.minute:02d}"
    await _save_task_from_query(query, context, 'weekly', repeat_value)
    return ConversationHandler.END


async def add_repeat_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        minutes = int(update.message.text.strip())
        if minutes < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи целое число минут (например: 60):")
        return WAIT_REPEAT_HOURS
    await _save_task_from_message(update, context, 'interval', str(minutes))
    return ConversationHandler.END


async def _save_task_from_query(query, context, repeat_type, repeat_value):
    data = context.user_data
    task_id = db.add_task(
        message=data['message'],
        peer_id=data['peer_id'],
        next_run=data['next_run'],
        repeat_type=repeat_type,
        repeat_value=repeat_value,
    )
    schedule_job(db.get_task(task_id))
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        _task_confirmation(task_id, data, repeat_type, repeat_value),
        reply_markup=get_kb(query.from_user.id),
    )


async def _save_task_from_message(update, context, repeat_type, repeat_value):
    data = context.user_data
    task_id = db.add_task(
        message=data['message'],
        peer_id=data['peer_id'],
        next_run=data['next_run'],
        repeat_type=repeat_type,
        repeat_value=repeat_value,
    )
    schedule_job(db.get_task(task_id))
    await update.message.reply_text(
        _task_confirmation(task_id, data, repeat_type, repeat_value),
        reply_markup=get_kb(update.effective_user.id),
    )


# ── /list ─────────────────────────────────────────────────────────────────

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return
    tasks = db.get_all_tasks()
    if not tasks:
        await update.message.reply_text("📭 Нет запланированных задач.", reply_markup=get_kb(update.effective_user.id))
        return
    active = sum(1 for t in tasks if not t['paused'])
    paused = len(tasks) - active
    await update.message.reply_text(
        f"📋 Задач всего: {len(tasks)}  (▶️ активных: {active} / ⏸ на паузе: {paused})",
        reply_markup=get_kb(update.effective_user.id),
    )
    for t in tasks:
        await update.message.reply_text(_task_card_text(t), reply_markup=_task_card_kb(t))


# ── task card inline actions ──────────────────────────────────────────────

async def task_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split(':')[1])
    task = db.get_task(task_id)
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return
    db.set_paused(task_id, True)
    if scheduler.get_job(f"task_{task_id}"):
        scheduler.pause_job(f"task_{task_id}")
    updated = {**task, 'paused': 1}
    await query.edit_message_text(_task_card_text(updated), reply_markup=_task_card_kb(updated))


async def task_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split(':')[1])
    task = db.get_task(task_id)
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return
    db.set_paused(task_id, False)
    job_id = f"task_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.resume_job(job_id)
    else:
        schedule_job(task)
    updated = {**task, 'paused': 0}
    await query.edit_message_text(_task_card_text(updated), reply_markup=_task_card_kb(updated))


async def task_del_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split(':')[1])
    task = db.get_task(task_id)
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"task_del_yes:{task_id}"),
        InlineKeyboardButton("❌ Отмена",       callback_data=f"task_del_no:{task_id}"),
    ]])
    await query.edit_message_text(
        f"⚠️ Удалить задачу #{task_id}?\n"
        f"📨 {task['message'][:60]}\n\n"
        f"Это действие необратимо.",
        reply_markup=kb,
    )


async def task_del_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split(':')[1])
    db.delete_task(task_id)
    job_id = f"task_{task_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    await query.edit_message_text(f"🗑 Задача #{task_id} удалена.")


async def task_del_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split(':')[1])
    task = db.get_task(task_id)
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return
    await query.edit_message_text(_task_card_text(task), reply_markup=_task_card_kb(task))


# ── /delete /pause /resume (keyboard-button entry points) ────────────────

async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return
    tasks = db.get_all_tasks()
    if not tasks:
        await update.message.reply_text("📭 Нет задач для удаления.", reply_markup=get_kb(update.effective_user.id))
        return
    kb = [[InlineKeyboardButton(f"#{t['id']} {t['message'][:28]}", callback_data=f"task_del_ask:{t['id']}")] for t in tasks]
    await update.message.reply_text("Выбери задачу для удаления:", reply_markup=InlineKeyboardMarkup(kb))


async def pause_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return
    tasks = [t for t in db.get_all_tasks() if not t['paused']]
    if not tasks:
        await update.message.reply_text("Нет активных задач.", reply_markup=get_kb(update.effective_user.id))
        return
    kb = [[InlineKeyboardButton(f"#{t['id']} {t['message'][:28]}", callback_data=f"task_pause:{t['id']}")] for t in tasks]
    await update.message.reply_text("Выбери задачу для паузы:", reply_markup=InlineKeyboardMarkup(kb))


async def resume_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return
    tasks = [t for t in db.get_all_tasks() if t['paused']]
    if not tasks:
        await update.message.reply_text("Нет приостановленных задач.", reply_markup=get_kb(update.effective_user.id))
        return
    kb = [[InlineKeyboardButton(f"#{t['id']} {t['message'][:28]}", callback_data=f"task_resume:{t['id']}")] for t in tasks]
    await update.message.reply_text("Выбери задачу для возобновления:", reply_markup=InlineKeyboardMarkup(kb))


# ── /myid ─────────────────────────────────────────────────────────────────

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if db.is_admin(user.id):
        db.add_admin(user.id, user.full_name or '')
    await update.message.reply_text(
        f"👤 Ваш Telegram ID: `{user.id}`\n"
        f"Имя: {user.full_name or '—'}",
        parse_mode='Markdown',
    )


# ── Admin management ────────────────────────────────────────────────────────

def _admins_panel():
    admins = db.get_all_admins()
    if not admins:
        text = "👥 Администраторов пока нет."
    else:
        lines = [f"👥 Администраторы ({len(admins)} чел.)\n"]
        for a in admins:
            label = f"{a['name']} (ID: {a['user_id']})" if a['name'] else str(a['user_id'])
            lines.append(f"• {label}")
        text = '\n'.join(lines)
    kb = []
    for a in admins:
        label = a['name'] or str(a['user_id'])
        kb.append([InlineKeyboardButton(f"❌ Убрать: {label}", callback_data=f"admin_rm:{a['user_id']}")
])
    kb.append([InlineKeyboardButton("➕ Добавить администратора", callback_data="admin_add")])
    return text, InlineKeyboardMarkup(kb)


async def admins_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.effective_user.id):
        return
    text, kb = _admins_panel()
    await update.message.reply_text(text, reply_markup=kb)


async def admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_super_admin(query.from_user.id):
        return
    db.remove_admin(int(query.data.split(':')[1]))
    text, kb = _admins_panel()
    await query.edit_message_text(text, reply_markup=kb)


async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_super_admin(query.from_user.id):
        return ConversationHandler.END
    await query.message.reply_text(
        "Введи Telegram ID пользователя, которого хочешь добавить.\n\n"
        "Попроси его написать /myid в этом боте и прислать тебе цифры.",
        reply_markup=CANCEL_KB,
    )
    return WAIT_ADMIN_ID


async def admin_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        new_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом. Попробуй ещё раз:")
        return WAIT_ADMIN_ID
    db.add_admin(new_id, '')
    await update.message.reply_text(
        f"✅ Пользователь `{new_id}` добавлен как администратор.",
        parse_mode='Markdown',
        reply_markup=get_kb(update.effective_user.id),
    )
    return ConversationHandler.END


# ── VK Chats management ───────────────────────────────────────────────────

def _chats_panel():
    chats = _load_chats()
    if not chats:
        text = "💬 Сохранённых VK чатов нет.\n\nДобавь чат, чтобы выбирать его при создании задачи."
    else:
        lines = [f"💬 VK Чаты ({len(chats)} шт.)\n"]
        for c in chats:
            lines.append(f"• {c['title']}  (peer_id: {c['peer_id']})")
        text = '\n'.join(lines)
    kb = []
    for c in chats:
        kb.append([InlineKeyboardButton(f"❌ {c['title']}", callback_data=f"chat_rm:{c['peer_id']}")
])
    kb.append([InlineKeyboardButton("➕ Добавить чат", callback_data="chat_add")])
    return text, InlineKeyboardMarkup(kb)


async def vk_chats_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id):
        return
    text, kb = _chats_panel()
    await update.message.reply_text(text, reply_markup=kb)


async def vk_chat_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not check_auth(query.from_user.id):
        return
    _remove_chat(int(query.data.split(':')[1]))
    text, kb = _chats_panel()
    await query.edit_message_text(text, reply_markup=kb)


async def vk_chat_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not check_auth(query.from_user.id):
        return ConversationHandler.END
    await query.message.reply_text(
        "Введи peer_id чата ВК:\n\n"
        "• Беседа: /convo/4 → 2000000004\n"
        "• Личка: ID пользователя\n"
        "• Группа: −ID группы",
        reply_markup=CANCEL_KB,
    )
    return WAIT_CHAT_PEER_ID


async def vk_chat_add_peer_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['new_chat_peer_id'] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ peer_id должен быть числом. Попробуй ещё раз:")
        return WAIT_CHAT_PEER_ID
    await update.message.reply_text(
        "Введи название для этого чата (как его будет видно в меню):\n"
        "Например: Основная беседа",
        reply_markup=CANCEL_KB,
    )
    return WAIT_CHAT_NAME


async def vk_chat_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    peer_id = context.user_data.pop('new_chat_peer_id')
    _add_chat(peer_id, title)
    await update.message.reply_text(
        f"✅ Чат «{title}» сохранён!\n"
        f"Теперь его можно выбирать при создании задачи.",
        reply_markup=get_kb(update.effective_user.id),
    )
    return ConversationHandler.END


# ── MAIN ─────────────────────────────────────────────────────────────────

def main():
    global tg_app
    tg_app = Application.builder().token(TG_TOKEN).build()

    cancel_filter = filters.Regex('^❌ Отмена$')
    text_no_cmd = filters.TEXT & ~filters.COMMAND & ~cancel_filter

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler('add', add_start),
            MessageHandler(filters.Regex('^📝 Новая задача$'), add_start),
        ],
        states={
            WAIT_MESSAGE:  [MessageHandler(text_no_cmd, add_message)],
            WAIT_PEER_ID:  [
                CallbackQueryHandler(add_peer_id_from_chat, pattern='^chat_sel:'),
                MessageHandler(text_no_cmd, add_peer_id),
            ],
            WAIT_DATETIME: [MessageHandler(text_no_cmd, add_datetime)],
            WAIT_REPEAT_CHOICE: [
                CallbackQueryHandler(add_repeat_choice,   pattern='^repeat:'),
                CallbackQueryHandler(add_weekday_toggle,  pattern='^mday:'),
                CallbackQueryHandler(add_weekday_done,    pattern='^mday_done$'),
            ],
            WAIT_REPEAT_HOURS: [MessageHandler(text_no_cmd, add_repeat_minutes)],
        },
        fallbacks=[
            CommandHandler('start', start),
            MessageHandler(cancel_filter, cancel_conv),
        ],
        per_message=False,
    )

    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_start, pattern='^admin_add$')],
        states={
            WAIT_ADMIN_ID: [MessageHandler(text_no_cmd, admin_add_finish)],
        },
        fallbacks=[
            CommandHandler('start', start),
            MessageHandler(cancel_filter, cancel_conv),
        ],
        per_message=False,
    )

    vk_chat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(vk_chat_add_start, pattern='^chat_add$')],
        states={
            WAIT_CHAT_PEER_ID: [MessageHandler(text_no_cmd, vk_chat_add_peer_id)],
            WAIT_CHAT_NAME:    [MessageHandler(text_no_cmd, vk_chat_add_name)],
        },
        fallbacks=[
            CommandHandler('start', start),
            MessageHandler(cancel_filter, cancel_conv),
        ],
        per_message=False,
    )

    tg_app.add_handler(CommandHandler('start',   start))
    tg_app.add_handler(CommandHandler('myid',    my_id))
    tg_app.add_handler(add_conv)
    tg_app.add_handler(admin_conv)
    tg_app.add_handler(vk_chat_conv)
    tg_app.add_handler(CommandHandler('list',    list_tasks))
    tg_app.add_handler(CommandHandler('delete',  delete_task))
    tg_app.add_handler(CommandHandler('pause',   pause_task))
    tg_app.add_handler(CommandHandler('resume',  resume_task))
    tg_app.add_handler(CommandHandler('admins',  admins_panel))
    tg_app.add_handler(CommandHandler('chats',   vk_chats_panel))

    # Reply-keyboard buttons
    tg_app.add_handler(MessageHandler(filters.Regex('^📋 Мои задачи$'),       list_tasks))
    tg_app.add_handler(MessageHandler(filters.Regex('^⏸ Пауза$'),             pause_task))
    tg_app.add_handler(MessageHandler(filters.Regex('^▶️ Возобновить$'),       resume_task))
    tg_app.add_handler(MessageHandler(filters.Regex('^🗑 Удалить задачу$'),    delete_task))
    tg_app.add_handler(MessageHandler(filters.Regex('^💬 VK Чаты$'),           vk_chats_panel))
    tg_app.add_handler(MessageHandler(filters.Regex('^👥 Администраторы$'),    admins_panel))

    # Inline buttons
    tg_app.add_handler(CallbackQueryHandler(task_pause,      pattern='^task_pause:'))
    tg_app.add_handler(CallbackQueryHandler(task_resume,     pattern='^task_resume:'))
    tg_app.add_handler(CallbackQueryHandler(task_del_ask,    pattern='^task_del_ask:'))
    tg_app.add_handler(CallbackQueryHandler(task_del_yes,    pattern='^task_del_yes:'))
    tg_app.add_handler(CallbackQueryHandler(task_del_no,     pattern='^task_del_no:'))
    tg_app.add_handler(CallbackQueryHandler(admin_remove,    pattern='^admin_rm:'))
    tg_app.add_handler(CallbackQueryHandler(vk_chat_remove,  pattern='^chat_rm:'))

    async def on_startup(app):
        tasks = db.get_all_tasks()
        for task in tasks:
            if not task['paused']:
                try:
                    schedule_job(task)
                except Exception as e:
                    logger.warning(f"Не удалось загрузить задачу #{task['id']}: {e}")
        scheduler.start()
        logger.info("Бот запущен!")

    tg_app.post_init = on_startup
    tg_app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()

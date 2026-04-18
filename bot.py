import logging
import os
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
from datetime import datetime

logging.basicConfig(
format=’%(asctime)s - %(name)s - %(levelname)s - %(message)s’,
level=logging.INFO
)
logger = logging.getLogger(**name**)

# Conversation states

WAIT_MESSAGE, WAIT_PEER_ID, WAIT_DATETIME, WAIT_REPEAT_CHOICE, WAIT_REPEAT_HOURS = range(5)

db = Database()
scheduler = AsyncIOScheduler(timezone=“Europe/Moscow”)

vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()

def check_auth(user_id: int) -> bool:
return ALLOWED_USER_ID == 0 or user_id == ALLOWED_USER_ID

async def send_vk_message(peer_id: int, message: str):
try:
vk.messages.send(
peer_id=peer_id,
message=message,
random_id=random.randint(1, 2**31)
)
logger.info(f”Отправлено в VK peer_id={peer_id}: {message[:50]}”)
except Exception as e:
logger.error(f”Ошибка отправки в VK: {e}”)

def schedule_job(task: dict):
job_id = f”task_{task[‘id’]}”

```
async def job_func():
    await send_vk_message(task['peer_id'], task['message'])
    if task['repeat_type'] == 'once':
        db.delete_task(task['id'])

if task['repeat_type'] == 'once':
    run_date = datetime.fromisoformat(task['next_run'])
    scheduler.add_job(job_func, 'date', run_date=run_date, id=job_id, replace_existing=True)

elif task['repeat_type'] == 'interval':
    minutes = int(task["repeat_value"])
    scheduler.add_job(
        job_func, IntervalTrigger(minutes=minutes),
        id=job_id, replace_existing=True,
        next_run_time=datetime.fromisoformat(task['next_run'])
    )

elif task['repeat_type'] == 'daily':
    hour, minute = task['repeat_value'].split(':')
    scheduler.add_job(
        job_func, CronTrigger(hour=int(hour), minute=int(minute), timezone="Europe/Moscow"),
        id=job_id, replace_existing=True
    )

elif task['repeat_type'] == 'weekly':
    day, hour, minute = task['repeat_value'].split(':')
    scheduler.add_job(
        job_func, CronTrigger(day_of_week=int(day), hour=int(hour), minute=int(minute), timezone="Europe/Moscow"),
        id=job_id, replace_existing=True
    )
```

def repeat_label(repeat_type, repeat_value):
if repeat_type == ‘once’:
return ‘Без повтора’
elif repeat_type == ‘interval’:
return f’Каждые {repeat_value} мин.’
elif repeat_type == ‘daily’:
return f’Каждый день в {repeat_value}’
elif repeat_type == ‘weekly’:
days = [‘Пн’, ‘Вт’, ‘Ср’, ‘Чт’, ‘Пт’, ‘Сб’, ‘Вс’]
parts = repeat_value.split(’:’)
return f’Каждую неделю {days[int(parts[0])]} в {parts[1]}:{parts[2]}’
return repeat_value

# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not check_auth(update.effective_user.id):
return
context.user_data.clear()
await update.message.reply_text(
“👋 Привет! Я бот-планировщик сообщений для ВКонтакте.\n\n”
“Команды:\n”
“/add — добавить новое сообщение\n”
“/list — список задач\n”
“/delete — удалить задачу\n”
“/pause — приостановить задачу\n”
“/resume — возобновить задачу”
)

# ── /add ──────────────────────────────────────────────────────────────────────

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not check_auth(update.effective_user.id):
return ConversationHandler.END
context.user_data.clear()
await update.message.reply_text(“📝 Введи текст сообщения, которое нужно отправить в ВК:”)
return WAIT_MESSAGE

async def add_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
context.user_data[‘message’] = update.message.text
await update.message.reply_text(
“📬 Введи peer_id чата ВК\n\n”
“Для беседы: число из ссылки + 2000000000\n”
“Например: /convo/4 → peer_id = 2000000004\n\n”
“Введи peer_id:”
)
return WAIT_PEER_ID

async def add_peer_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
try:
peer_id = int(update.message.text.strip())
context.user_data[‘peer_id’] = peer_id
except ValueError:
await update.message.reply_text(“❌ peer_id должен быть числом. Попробуй ещё раз:”)
return WAIT_PEER_ID

```
await update.message.reply_text(
    "🕐 Введи дату и время первой отправки:\n"
    "Формат: ДД.ММ.ГГГГ ЧЧ:ММ\n\n"
    "Например: 25.04.2026 14:30\n"
    "(Время московское)"
)
return WAIT_DATETIME
```

async def add_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
try:
dt = datetime.strptime(update.message.text.strip(), “%d.%m.%Y %H:%M”)
context.user_data[‘next_run’] = dt.isoformat()
except ValueError:
await update.message.reply_text(
“❌ Неверный формат. Используй ДД.ММ.ГГГГ ЧЧ:ММ\n”
“Например: 25.04.2026 14:30”
)
return WAIT_DATETIME

```
keyboard = [
    [InlineKeyboardButton("🔂 Раз (без повтора)", callback_data="repeat:once")],
    [InlineKeyboardButton("🔁 Каждые N минут", callback_data="repeat:interval")],
    [InlineKeyboardButton("📅 Каждый день в это время", callback_data="repeat:daily")],
    [InlineKeyboardButton("📆 Раз в неделю", callback_data="repeat:weekly")],
]
await update.message.reply_text(
    "🔄 Выбери режим повтора:",
    reply_markup=InlineKeyboardMarkup(keyboard)
)
return WAIT_REPEAT_CHOICE
```

async def add_repeat_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
await query.answer()

```
repeat_type = query.data.split(':')[1]
context.user_data['repeat_type'] = repeat_type

if repeat_type == 'once':
    await save_task(query, context, repeat_type, '')
    return ConversationHandler.END

elif repeat_type == 'interval':
    await query.edit_message_text("Каждые сколько минут отправлять?\nВведи число (например: 60):")
    return WAIT_REPEAT_HOURS

elif repeat_type == 'daily':
    dt = datetime.fromisoformat(context.user_data['next_run'])
    repeat_value = f"{dt.hour:02d}:{dt.minute:02d}"
    await save_task(query, context, repeat_type, repeat_value)
    return ConversationHandler.END

elif repeat_type == 'weekly':
    days = [
        [InlineKeyboardButton("Пн", callback_data="day:0"),
         InlineKeyboardButton("Вт", callback_data="day:1"),
         InlineKeyboardButton("Ср", callback_data="day:2"),
         InlineKeyboardButton("Чт", callback_data="day:3")],
        [InlineKeyboardButton("Пт", callback_data="day:4"),
         InlineKeyboardButton("Сб", callback_data="day:5"),
         InlineKeyboardButton("Вс", callback_data="day:6")],
    ]
    await query.edit_message_text("Выбери день недели:", reply_markup=InlineKeyboardMarkup(days))
    return WAIT_REPEAT_CHOICE
```

async def add_repeat_minutes(update: Update, context: ContextTypes.DEFAULT_TYPE):
try:
hours = int(update.message.text.strip())
if hours < 1:
raise ValueError
await save_task_msg(update, context, ‘interval’, str(hours))
return ConversationHandler.END
except ValueError:
await update.message.reply_text(“❌ Введи целое число минут (например: 60):”)
return WAIT_REPEAT_HOURS

async def add_weekday(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
await query.answer()
day = query.data.split(’:’)[1]
dt = datetime.fromisoformat(context.user_data[‘next_run’])
repeat_value = f”{day}:{dt.hour:02d}:{dt.minute:02d}”
await save_task(query, context, ‘weekly’, repeat_value)
return ConversationHandler.END

async def save_task(query, context, repeat_type, repeat_value):
data = context.user_data
task_id = db.add_task(
message=data[‘message’],
peer_id=data[‘peer_id’],
next_run=data[‘next_run’],
repeat_type=repeat_type,
repeat_value=repeat_value
)
task = db.get_task(task_id)
schedule_job(task)
await query.edit_message_text(
f”✅ Задача #{task_id} создана!\n\n”
f”📨 Сообщение: {data[‘message’][:50]}\n”
f”📬 peer_id: {data[‘peer_id’]}\n”
f”🕐 Первая отправка: {data[‘next_run’].replace(‘T’, ’ ’)}\n”
f”🔄 Повтор: {repeat_label(repeat_type, repeat_value)}”
)

async def save_task_msg(update, context, repeat_type, repeat_value):
data = context.user_data
task_id = db.add_task(
message=data[‘message’],
peer_id=data[‘peer_id’],
next_run=data[‘next_run’],
repeat_type=repeat_type,
repeat_value=repeat_value
)
task = db.get_task(task_id)
schedule_job(task)
await update.message.reply_text(
f”✅ Задача #{task_id} создана!\n\n”
f”📨 Сообщение: {data[‘message’][:50]}\n”
f”📬 peer_id: {data[‘peer_id’]}\n”
f”🕐 Первая отправка: {data[‘next_run’].replace(‘T’, ’ ’)}\n”
f”🔄 Повтор: {repeat_label(repeat_type, repeat_value)}”
)

# ── /list ─────────────────────────────────────────────────────────────────────

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not check_auth(update.effective_user.id):
return
tasks = db.get_all_tasks()
if not tasks:
await update.message.reply_text(“📭 Нет запланированных задач.”)
return
text = “📋 Запланированные задачи:\n\n”
for t in tasks:
status = “⏸ “ if t[‘paused’] else “▶️ “
text += (
f”{status}#{t[‘id’]} — {t[‘message’][:30]}\n”
f”   📬 peer_id: {t[‘peer_id’]}\n”
f”   🕐 Следующий запуск: {t[‘next_run’].replace(‘T’, ’ ’)}\n”
f”   🔄 {repeat_label(t[‘repeat_type’], t[‘repeat_value’])}\n\n”
)
await update.message.reply_text(text)

# ── /delete ───────────────────────────────────────────────────────────────────

async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not check_auth(update.effective_user.id):
return
tasks = db.get_all_tasks()
if not tasks:
await update.message.reply_text(“📭 Нет задач для удаления.”)
return
keyboard = [[InlineKeyboardButton(
f”#{t[‘id’]} {t[‘message’][:25]}”, callback_data=f”del:{t[‘id’]}”
)] for t in tasks]
await update.message.reply_text(“Выбери задачу для удаления:”, reply_markup=InlineKeyboardMarkup(keyboard))

async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
await query.answer()
task_id = int(query.data.split(’:’)[1])
db.delete_task(task_id)
job_id = f”task_{task_id}”
if scheduler.get_job(job_id):
scheduler.remove_job(job_id)
await query.edit_message_text(f”🗑 Задача #{task_id} удалена.”)

# ── /pause & /resume ──────────────────────────────────────────────────────────

async def pause_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not check_auth(update.effective_user.id):
return
tasks = [t for t in db.get_all_tasks() if not t[‘paused’]]
if not tasks:
await update.message.reply_text(“Нет активных задач.”)
return
keyboard = [[InlineKeyboardButton(
f”#{t[‘id’]} {t[‘message’][:25]}”, callback_data=f”pause:{t[‘id’]}”
)] for t in tasks]
await update.message.reply_text(“Выбери задачу для паузы:”, reply_markup=InlineKeyboardMarkup(keyboard))

async def pause_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
await query.answer()
task_id = int(query.data.split(’:’)[1])
db.set_paused(task_id, True)
job_id = f”task_{task_id}”
if scheduler.get_job(job_id):
scheduler.pause_job(job_id)
await query.edit_message_text(f”⏸ Задача #{task_id} приостановлена.”)

async def resume_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not check_auth(update.effective_user.id):
return
tasks = [t for t in db.get_all_tasks() if t[‘paused’]]
if not tasks:
await update.message.reply_text(“Нет приостановленных задач.”)
return
keyboard = [[InlineKeyboardButton(
f”#{t[‘id’]} {t[‘message’][:25]}”, callback_data=f”resume:{t[‘id’]}”
)] for t in tasks]
await update.message.reply_text(“Выбери задачу для возобновления:”, reply_markup=InlineKeyboardMarkup(keyboard))

async def resume_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
await query.answer()
task_id = int(query.data.split(’:’)[1])
db.set_paused(task_id, False)
job_id = f”task_{task_id}”
if scheduler.get_job(job_id):
scheduler.resume_job(job_id)
await query.edit_message_text(f”▶️ Задача #{task_id} возобновлена.”)

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
app = Application.builder().token(TG_TOKEN).build()

```
add_conv = ConversationHandler(
    entry_points=[CommandHandler('add', add_start)],
    states={
        WAIT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_message)],
        WAIT_PEER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_peer_id)],
        WAIT_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_datetime)],
        WAIT_REPEAT_CHOICE: [
            CallbackQueryHandler(add_repeat_choice, pattern='^repeat:'),
            CallbackQueryHandler(add_weekday, pattern='^day:'),
        ],
        WAIT_REPEAT_HOURS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_repeat_minutes),
        ],
    },
    fallbacks=[CommandHandler('start', start)],
    per_message=False,
)

app.add_handler(CommandHandler('start', start))
app.add_handler(add_conv)
app.add_handler(CommandHandler('list', list_tasks))
app.add_handler(CommandHandler('delete', delete_task))
app.add_handler(CommandHandler('pause', pause_task))
app.add_handler(CommandHandler('resume', resume_task))
app.add_handler(CallbackQueryHandler(delete_confirm, pattern='^del:'))
app.add_handler(CallbackQueryHandler(pause_confirm, pattern='^pause:'))
app.add_handler(CallbackQueryHandler(resume_confirm, pattern='^resume:'))

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

app.post_init = on_startup
app.run_polling(allowed_updates=Update.ALL_TYPES)
```

if **name** == ‘**main**’:
main()

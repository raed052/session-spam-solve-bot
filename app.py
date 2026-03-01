import asyncio
import os
import time
import json
import logging
from telethon import TelegramClient, events

# ===================== إعدادات - عدّل القيم دي =====================
API_ID = 37717383
API_HASH = '32331c1b9578374921b67792e5eda886'
BOT_TOKEN = '8607538619:AAEBMaKScxDrgB4dZuBnor9e8dngrAsGH1M'
ADMIN_ID = 7780236494

SESSIONS_DIR = "sessions"
STATE_FILE = "state.json"
LOG_FILE = "bot.log"

# أوقات (بالثواني)
COOLDOWN = 3600              # 1 ساعة بين محاولات تشغيل الجلسة
COMPLAINT_COOLDOWN = 3600    # 1 ساعة بين كل شكوى لنفس الجلسة
INTER_ACCOUNT_DELAY = 30     # ثانية بين كل حساب والثاني أثناء المعالجة
CHECK_INTERVAL_EMPTY = 60    # لو الفولدر فاضي ننتظر قبل المحاولة التالية

SPAMBOT_USERNAME = "@spambot"
SUCCESS_MARKER = "Good news, no limits are currently applied"

# ===================== لوجنج =====================
logger = logging.getLogger("smart_queue_bot")
logger.setLevel(logging.INFO)
fmt = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')
fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
fh.setFormatter(fmt); logger.addHandler(fh)
ch = logging.StreamHandler(); ch.setFormatter(fmt); logger.addHandler(ch)

# ===================== تهيئة فولدر وحالة =====================
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

def load_state():
    default = {"last_run": {}, "last_complaint": {}}
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "last_run" not in data: data["last_run"] = {}
            if "last_complaint" not in data: data["last_complaint"] = {}
            return data
    except Exception as e:
        logger.exception("فشل قراءة state.json: %s", e)
        return default

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("فشل حفظ state.json: %s", e)

state = load_state()

# ===================== بوت المدير =====================
bot = TelegramClient('bot_manager', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ===================== بنية الـ Queue و Sets للحالة =====================
queue = asyncio.Queue()
in_queue = set()        # جلسات داخل الـ queue (تنتظر المعالجة)
processing = set()      # جلسات قيد المعالجة الآن
scheduled_tasks = {}    # session_file -> asyncio.Task (المهمة اللي هتعيد إدخالها للـ queue بعد cooldown)
worker_task = None
running = True          # لو عايز توقف كل الخلفيات، ضع False

# ==== وظائف مساعدة ====
def list_session_files():
    return sorted([f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')])

def remove_session_files(session_filename):
    base_name = os.path.splitext(session_filename)[0]
    removed = []
    for fname in os.listdir(SESSIONS_DIR):
        if fname.startswith(base_name):
            full = os.path.join(SESSIONS_DIR, fname)
            try:
                os.remove(full)
                removed.append(fname)
            except Exception as e:
                logger.exception("خطأ لحذف %s: %s", full, e)
    return removed

def get_remaining_cooldown_seconds(file_name):
    last = state.get('last_run', {}).get(file_name)
    if not last:
        return 0
    rem = COOLDOWN - (time.time() - last)
    return int(rem) if rem > 0 else 0

# ==== جدولة إعادة الإدخال بعد تأخير ====
async def schedule_reenqueue(file_name, delay):
    """بعد delay ثانية، يحاول يعيد إدخال الـ session إذا لسه موجود"""
    try:
        await asyncio.sleep(delay)
        scheduled_tasks.pop(file_name, None)
        # لو الملف موجود وغير موجود بالفعل في أي مكان، نضيفه
        if not os.path.exists(os.path.join(SESSIONS_DIR, file_name)):
            logger.info("ملف %s اختفى قبل إعادة الإدخال - تجاهل", file_name)
            return
        await enqueue_session(file_name)
    except asyncio.CancelledError:
        logger.info("تم إلغاء مهمة إعادة الإدخال لـ %s", file_name)
    except Exception as e:
        logger.exception("خطأ في schedule_reenqueue %s: %s", file_name, e)

async def enqueue_session(file_name):
    """يضيف ملف للجـQueue لو مش مُدرج بالفعل ولا مجدول"""
    if file_name in in_queue or file_name in processing or file_name in scheduled_tasks:
        logger.debug("لم يتم إدخال %s للـ queue لأنه موجود بالفعل", file_name)
        return False
    if not os.path.exists(os.path.join(SESSIONS_DIR, file_name)):
        logger.debug("لم يتم إدخال %s لأنه غير موجود في الملفات", file_name)
        return False
    await queue.put(file_name)
    in_queue.add(file_name)
    logger.info("تم إدخال %s في الـ queue", file_name)
    return True

# ===================== معالجة جلسة منفردة =====================
async def process_session(file_name, index):
    session_path = os.path.join(SESSIONS_DIR, file_name)
    session_base = os.path.splitext(file_name)[0]
    # نمرر Telethon اسم الجلسة كمسار داخل مجلد sessions حتى يُحفظ هناك
    session_name = os.path.join(SESSIONS_DIR, session_base)

    client = TelegramClient(session_name, API_ID, API_HASH)
    logger.info("بدء معالجة %s (رقم %d)", file_name, index)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await bot.send_message(ADMIN_ID, f"⚠️ الجلسة `{file_name}` غير مفوضة أو تالفة، تم تخطيها.")
            await client.disconnect()
            logger.warning("الجلسة غير مفوضة: %s", file_name)
            return False

        me = await client.get_me()
        phone = getattr(me, "phone", None) or file_name

        # ارسال /start وقراءة الرد
        try:
            await client.send_message(SPAMBOT_USERNAME, '/start')
        except Exception as e:
            logger.exception("فشل إرسال /start من %s: %s", file_name, e)
            await client.disconnect()
            return False

        await asyncio.sleep(3)

        # قراءة أحدث رسالة من @spambot
        async for message in client.iter_messages(SPAMBOT_USERNAME, limit=1):
            text = getattr(message, "text", "") or ""
            logger.info("رد %s على %s: %.120s", SPAMBOT_USERNAME, file_name, text)

            if SUCCESS_MARKER in text:
                # إرسال Reminder وحذف السيشن
                await bot.send_message(
                    ADMIN_ID,
                    f"🔔 REMINDER\n📌 `{file_name}`\n📞 {phone}\n✅ الحساب بدون قيود — تم حذف الجلسة."
                )
                removed = remove_session_files(file_name)
                logger.info("نجاح %s -> حذف: %s", file_name, removed)
                # تنظيف state
                state['last_run'].pop(file_name, None)
                state['last_complaint'].pop(file_name, None)
                save_state(state)
                await client.disconnect()
                return True

            else:
                # نتحقق من فاصل الشكوى
                now = time.time()
                last_complaint = state.get('last_complaint', {}).get(file_name)
                if last_complaint and (now - last_complaint) < COMPLAINT_COOLDOWN:
                    remaining = int((COMPLAINT_COOLDOWN - (now - last_complaint)) / 60)
                    await bot.send_message(ADMIN_ID,
                        f"⛔ تم تخطي إرسال شكوى لـ `{file_name}` — باقي {remaining} دقيقة قبل الإرسال مرة أخرى.")
                    logger.info("تخطي شكوى %s — باقي %d دقيقة", file_name, remaining)
                else:
                    try:
                        await client.send_message(SPAMBOT_USERNAME, 'Submit a complaint')
                        await asyncio.sleep(2)
                        await client.send_message(SPAMBOT_USERNAME, 'No, I’ll never do any of this!')
                        await asyncio.sleep(2)
                        await client.send_message(SPAMBOT_USERNAME,
                            "I did not do anything wrong, please review my account.")
                        await bot.send_message(ADMIN_ID, f"⚠️ تم إرسال شكوى لحساب `{file_name}`.")
                        logger.info("تم إرسال شكوى من %s", file_name)
                        state['last_complaint'][file_name] = now
                        save_state(state)
                    except Exception as e:
                        logger.exception("فشل إرسال الشكوى من %s: %s", file_name, e)
                        await bot.send_message(ADMIN_ID, f"❌ فشل إرسال شكوى من `{file_name}`: {e}")

        await client.disconnect()
        return False

    except Exception as e:
        logger.exception("خطأ معالجة %s: %s", file_name, e)
        try:
            if client.is_connected():
                await client.disconnect()
        except:
            pass
        return False

# ===================== الــ Worker (واحد فقط) =====================
async def worker():
    logger.info("Worker started")
    index = 0
    while running:
        try:
            file_name = await queue.get()
            # تحكم بالـ sets
            in_queue.discard(file_name)
            processing.add(file_name)
            index += 1

            # قبل المعالجة نتحقق إن الملف مازال موجود
            if not os.path.exists(os.path.join(SESSIONS_DIR, file_name)):
                logger.info("%s اختفى قبل المعالجة، تجاهل.", file_name)
                processing.discard(file_name)
                queue.task_done()
                continue

            # نتحقق من الـ cooldown قبل التشغيل الفعلي
            last = state.get('last_run', {}).get(file_name)
            now = time.time()
            if last and (now - last) < COOLDOWN:
                rem = COOLDOWN - (now - last)
                # نعيد جدولته بعد المتبقي
                logger.info("%s داخل cooldown، سيعاد جدولته بعد %d ثانية", file_name, int(rem))
                # انشاء مهمة مجدولة
                t = bot.loop.create_task(schedule_reenqueue(file_name, rem))
                scheduled_tasks[file_name] = t
                processing.discard(file_name)
                queue.task_done()
                continue

            # معالجة الجلسة
            success = await process_session(file_name, index)

            # بعد المعالجة: إذا نجحت وتم حذف الجلسة فلا حاجة لإعادة إدخالها
            if success:
                # تأكد من إلغاء أي مهمة مجدولة
                t = scheduled_tasks.pop(file_name, None)
                if t:
                    t.cancel()
                processing.discard(file_name)
                # لا نسجل last_run لأن الملف محذوف
                queue.task_done()
                continue

            # إذا لم تُحذف: سجّل last_run ومن ثم جدولة إعادة الإدخال بعد COOLDOWN
            state['last_run'][file_name] = time.time()
            save_state(state)

            # جدولة إعادة الإدخال بعد COOLDOWN
            t = bot.loop.create_task(schedule_reenqueue(file_name, COOLDOWN))
            scheduled_tasks[file_name] = t

            processing.discard(file_name)
            queue.task_done()

            # فاصل بسيط بين الحسابات
            await asyncio.sleep(INTER_ACCOUNT_DELAY)

        except asyncio.CancelledError:
            logger.info("Worker تم إلغاؤه")
            break
        except Exception as e:
            logger.exception("خطأ في ال worker: %s", e)
            await asyncio.sleep(5)
    logger.info("Worker stopped")

# ===================== إدارة الجلسات عند التشغيل =====================
async def bootstrap_existing_sessions():
    """عند بداية التشغيل: إدخال الجلسات أو جدولة إعادة الإدخال إذا كانت في cooldown"""
    files = list_session_files()
    for f in files:
        last = state.get('last_run', {}).get(f)
        if last:
            rem = COOLDOWN - (time.time() - last)
            if rem > 0:
                logger.info("جدولة %s بعد %d ثانية ( cooldown مازال شغال )", f, int(rem))
                scheduled_tasks[f] = bot.loop.create_task(schedule_reenqueue(f, rem))
                continue
        # غير ذلك — ندخله فوراً
        await enqueue_session(f)

# ===================== أوامر التليجرام =====================
@bot.on(events.NewMessage(pattern='/store'))
async def cmd_store(event):
    if event.sender_id != ADMIN_ID:
        return
    await event.reply("✅ أرسل الآن ملفات الـ `.session`. سيتم حفظها في مجلد `sessions` وإضافتها للـ queue تلقائياً.")

@bot.on(events.NewMessage(pattern='/list'))
async def cmd_list(event):
    if event.sender_id != ADMIN_ID:
        return
    files = list_session_files()
    if not files:
        await event.reply("📂 لا توجد جلسات حالياً.")
        return
    lines = []
    for f in files:
        rem = get_remaining_cooldown_seconds(f)
        rem_str = f" (يبقى {rem//60} دقيقة)" if rem>0 else ""
        lines.append(f"- `{f}`{rem_str}")
    await event.reply("📂 الجلسات الموجودة:\n" + "\n".join(lines))

@bot.on(events.NewMessage(pattern=r'/remove (.+)'))
async def cmd_remove(event):
    if event.sender_id != ADMIN_ID:
        return
    target = event.pattern_match.group(1).strip()
    if not target.endswith('.session'):
        target += '.session'
    if target not in os.listdir(SESSIONS_DIR):
        await event.reply(f"❌ الملف `{target}` غير موجود.")
        return
    # إزالة من الـ queue/processing/scheduled
    in_queue.discard(target)
    if target in processing:
        await event.reply(f"⚠️ `{target}` قيد المعالجة الآن؛ سيتم حذف الملفات فور انتهاء المعالجة.")
    t = scheduled_tasks.pop(target, None)
    if t:
        t.cancel()
    removed = remove_session_files(target)
    state['last_run'].pop(target, None)
    state['last_complaint'].pop(target, None)
    save_state(state)
    await event.reply(f"🗑️ تم حذف: {', '.join(removed)}")

@bot.on(events.NewMessage(pattern='/start_loop'))
async def cmd_start_loop(event):
    global worker_task
    if event.sender_id != ADMIN_ID:
        return
    if worker_task and not worker_task.done():
        await event.reply("🔁 اللوب شغال بالفعل.")
        return
    worker_task = bot.loop.create_task(worker())
    await event.reply("✅ شغّلت الـ worker.")

@bot.on(events.NewMessage(pattern='/stop_loop'))
async def cmd_stop_loop(event):
    global worker_task, running
    if event.sender_id != ADMIN_ID:
        return
    running = False
    if worker_task:
        worker_task.cancel()
        worker_task = None
    # إلغاء كل المهام المجدولة
    for t in list(scheduled_tasks.values()):
        t.cancel()
    scheduled_tasks.clear()
    await event.reply("⛔ تم إيقاف الـ worker والمهام المجدولة.")

# ========== استقبال ملفات .session وإضافتها للـ queue ==========
@bot.on(events.NewMessage)
async def handle_incoming_files(event):
    # نأخذ الملفات من الأدمن فقط
    if event.sender_id != ADMIN_ID:
        return

    if event.document:
        fname = getattr(event.file, "name", None)
        if not fname or not fname.endswith('.session'):
            return

        # لو الملف بنفس الإسم موجود - نضيف suffix زمني عشان ما يلغيش القديم
        target_path = os.path.join(SESSIONS_DIR, fname)
        if os.path.exists(target_path):
            base, ext = os.path.splitext(fname)
            ts = int(time.time())
            fname = f"{base}_{ts}{ext}"
            target_path = os.path.join(SESSIONS_DIR, fname)

        try:
            saved = await event.download_media(file=SESSIONS_DIR)
            saved_name = os.path.basename(saved)
            await event.reply(f"📥 تم حفظ الملف: `{saved_name}` وتمت إضافته للـ queue.")
            logger.info("تم حفظ جلسة جديدة: %s", saved_name)
            await enqueue_session(saved_name)
        except Exception as e:
            logger.exception("فشل حفظ الملف: %s", e)
            await event.reply("❌ حدث خطأ أثناء حفظ الملف.")

# ===================== بدء البوت والـ worker تلقائياً =====================
if __name__ == "__main__":
    with bot:
        # جدول البداية: إدخال الجلسات الموجودة أو جدولة إعادة ادخالها لو كانت داخل cooldown
        bot.loop.run_until_complete(bootstrap_existing_sessions())
        # تشغيل الـ worker تلقائياً
        worker_task = bot.loop.create_task(worker())
        logger.info("البوت جاهز والـ worker شغّال. المجلد: %s", os.path.abspath(SESSIONS_DIR))
        bot.run_until_disconnected()


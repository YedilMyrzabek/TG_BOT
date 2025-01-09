import os
import sys
import asyncio
import asyncpg
import datetime
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ContentType
)
from aiogram.client.bot import DefaultBotProperties  # Дұрыс импорт
from dotenv import load_dotenv
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from logging.handlers import RotatingFileHandler

# 1. Windows үшін цикл саясаттарын орнату
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 2. Логирование орнату
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Лог файлдарын ротациялау
file_handler = RotatingFileHandler("bot.log", maxBytes=10**6, backupCount=5, encoding='utf-8')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Логтарды консольға шығару
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# 3. Орта айнымалыларын жүктеу
load_dotenv()

API_TOKEN = os.getenv("TELEGRAM_TOKEN")
DB_URL = os.getenv("DB_URL")

if not API_TOKEN or not DB_URL:
    logger.error("Отсутствует TELEGRAM_TOKEN немесе DB_URL .env файлы!")
    raise ValueError("Отсутствует TELEGRAM_TOKEN немесе DB_URL .env файлы!")

# 4. Ботты инициализациялау (HTML parse_mode қолдану)
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()

# 5. Админдердің тізімі (немесе жиыны)
ADMIN_IDS = {1044841557, 1727718224}  # <-- қажетті Telegram user_id

# 6. Asyncpg арқылы дерекқорға қосылу
async def get_db_pool():
    return await asyncpg.create_pool(dsn=DB_URL, command_timeout=60)

# 7. Дерекқорды инициализациялау
async def initialize_db(pool):
    async with pool.acquire() as conn:
        # ТАБЛИЦА users
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ТАБЛИЦА user_cooldowns (обновлённая)
        # Храним кулдаун отдельно для бесплатных и слив-пробников, по каждому предмету
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_cooldowns (
                user_id BIGINT,
                subject_name TEXT,
                next_free_time TIMESTAMP,
                next_special_time TIMESTAMP,
                PRIMARY KEY (user_id, subject_name)
            );
        """)

        # ТАБЛИЦА user_access
        # Для слив-пробников: access_type='special', remaining_count (макс 10),
        # last_special_test_id хранит последний выданный слив тест
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_access (
                user_id BIGINT,
                subject_name TEXT,
                access_type TEXT,
                remaining_count INTEGER,
                last_special_test_id INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, subject_name, access_type)
            );
        """)

        # ТАБЛИЦА tests (бесплатные)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tests (
                id SERIAL PRIMARY KEY,
                subject TEXT,
                file_name TEXT,
                file_url TEXT
            );
        """)

        # ТАБЛИЦА premium_tests (слив)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS premium_tests (
                id SERIAL PRIMARY KEY,
                subject TEXT,
                access_type TEXT NOT NULL DEFAULT 'special',
                file_name TEXT,
                file_url TEXT
            );
        """)

        # Инициализация: чтобы новым пользователям автоматически давать бесплатные пробники
        # Логика берілген /start командасында жүзеге асады

# 8. Дерекқор қосылым пулын инициализациялау
pool = None

async def on_startup():
    global pool
    pool = await get_db_pool()
    await initialize_db(pool)
    logger.info("Дерекқор инициализацияланды.")

# 9. Жарияланымдар үшін күй анықтамалары
class AnnouncementStates(StatesGroup):
    waiting_for_text = State()
    waiting_for_photo = State()

# 10. Клавиатура функциялары
def get_subjects_keyboard():
    """Пәнді таңдау үшін клавиатура."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Математика 📚", callback_data="subject_math")],
        [InlineKeyboardButton(text="Информатика 💻", callback_data="subject_informatics")],
    ])
    return keyboard

def get_variant_keyboard(subject_code: str):
    """Тест түрін таңдау үшін клавиатура. "Слив нұсқа" барлық пайдаланушыларға көрсетіледі."""
    buttons = [
        [InlineKeyboardButton(text="Тегін нұсқа 🆓", callback_data=f"variant_free_{subject_code}")],
        [InlineKeyboardButton(text="Слив нұсқа 💎", callback_data=f"variant_special_{subject_code}")],
        [InlineKeyboardButton(text="Артқа 🔙", callback_data="back_subjects")],
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard

def get_help_keyboard():
    """Көмек клавиатурасы."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Меню 📋", callback_data="main_menu")],
    ])
    return keyboard

def get_skip_or_add_photo_keyboard():
    """Хабарламаға сурет қосу немесе пропуск үшін клавиатура."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Фото қосу 📷", callback_data="add_photo")],
        [InlineKeyboardButton(text="Пропустить 🛑", callback_data="skip_photo")]
    ])
    return keyboard

# 11. Көмекші функциялар
async def safe_edit_text(callback: CallbackQuery, text: str, parse_mode: str = "HTML", reply_markup: InlineKeyboardMarkup = None):
    """
    Хабарламаның мәтінін өңдеуге тырысады. Егер мүмкін болмаса, жаңа хабарлама жібереді.
    """
    try:
        await callback.message.edit_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        logger.error(f"Хабарламаны өңдеу кезінде қате: {e.message}", exc_info=True)
        # Егер хабарламаны өңдеуге болмаса, жаңа хабарлама жібереміз
        await callback.message.answer(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        # Қажет болса, бастапқы хабарламаны жою
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass  # Егер жоюға болмаса, елемейміз

async def notify_admins(message: str):
    """Барлық администраторларды маңызды қателер немесе оқиғалар туралы хабардар етеді."""
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"❗ <b>Қате:</b> {message}", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Админге хабар жіберуде қате: {admin_id} - {e}")

# Соңғы мәзір хабарламаларын сақтау үшін глобалды сөздік
user_last_menu_message = {}

# 12. /start командасын өңдеу
async def send_welcome(message: Message):
    """/start командасын өңдейді. Пайдаланушыны тіркеп, сәлемдесу хабарламасын жібереді."""
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name

    async with pool.acquire() as conn:
        try:
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO NOTHING
            """, user_id, username, first_name, last_name)
        except Exception as e:
            logger.error("Пайдаланушыны тіркеу қатесі:", exc_info=True)
            await notify_admins(f"Пайдаланушыны тіркеу кезінде қате: {user_id} - {str(e)}")
            await message.answer("❌ Тіркеу кезінде қате пайда болды. Өтінеміз, кейінірек қайта көріңіз.")
            return

        # Дополнительно: если у пользователя нет ещё записи о бесплатном доступе, выдаём ему доступ с кулдауном
        subjects = ["Математика", "Информатика"]
        for subj in subjects:
            # Проверяем, есть ли запись 'free' для данного пользователя
            record = await conn.fetchrow("""
                SELECT * FROM user_cooldowns
                WHERE user_id=$1 AND subject_name=$2
            """, user_id, subj)
            # Если нет, создаём запись с нулевым кулдауном
            if not record:
                await conn.execute("""
                    INSERT INTO user_cooldowns (user_id, subject_name, next_free_time, next_special_time)
                    VALUES ($1, $2, NOW() - INTERVAL '1 day', NOW() - INTERVAL '1 day')
                """, user_id, subj)

    # Пайдаланушының слив қолжетімділігін тексеру
    has_special_access = await check_special_access(user_id)
    logger.info(f"Пайдаланушы {user_id} слив қолжетімділікке ие: {has_special_access}")

    # Жаңартылған сәлемдесу хабарламасы
    welcome_text = (
        "👋 Сәлеметсіз бе! \n\n"
        "Біз сізге Математика және Информатика пәндер бойынша үздік нұсқаларды ұсынамыз.\n\n"
        "🔍 Тегін нұсқалар арқылы дайындалыңыз және өз деңгейіңізді арттырыңыз.\n\n"
        "💎 Слив нұсқалар арқылы өткен және алдағы уақытта кездесуі мүмкін нұсқалармен өзіңізді сынап көріңіз.\n\n"
        "P.S. келесі нұсқаны 24 сағаттан соң ала аласыз 🤓 (алу үшін әрқашан /start командасын басасыз❗️).\n\n"
        "ℹ️ Қосымша сұрақтар бойынша /help."
    )

    keyboard = get_subjects_keyboard()
    try:
        sent_message = await message.answer(welcome_text, parse_mode="HTML", reply_markup=keyboard)
        # /help шақыру кезінде жою үшін message_id сақтайды
        user_last_menu_message[user_id] = sent_message.message_id
    except TelegramBadRequest as e:
        logger.error(f"Хабарлама жіберу кезінде қате: {e.message}", exc_info=True)
        await message.answer("❌ Хабарламаны жіберу кезінде қате пайда болды.")

# Пайдаланушының слив қолжетімділігін тексеру
async def check_special_access(user_id: int) -> bool:
    """Пайдаланушының слив пробниктерге қолжетімділігін тексереді."""
    async with pool.acquire() as conn:
        access = await conn.fetchrow("""
            SELECT remaining_count FROM user_access
            WHERE user_id = $1 AND access_type = 'special' AND remaining_count > 0
            LIMIT 1
        """, user_id)
        if access:
            return True
        else:
            return False

# Пайдаланушылар санын көрсету
async def show_subscribers(message: Message):
    """Пайдаланушылар санын көрсетеді."""
    async with pool.acquire() as conn:
        try:
            count = await conn.fetchval("SELECT COUNT(*) FROM users")
            await message.answer(f"📈 <b>Количество подписчиков</b>: {count}", parse_mode="HTML")
        except Exception as e:
            logger.error("Пайдаланушылар санын есептеуде қате:", exc_info=True)
            await notify_admins(f"Пайдаланушылар санын есептеу кезінде қате: {str(e)}")
            await message.answer("❌ Пайдаланушылар санын есептеуде қате болды.")

# CallbackQuery-лерді өңдеу
async def handle_callback(callback: CallbackQuery):
    data = callback.data
    user_id = callback.from_user.id
    logger.info(f"CallbackQuery қабылданды: {data} пайдаланушыдан: {user_id}")

    # Callback-ке жауап беру
    try:
        await callback.answer()
    except TelegramBadRequest as e:
        logger.error(f"CallbackQuery жауап беру кезінде қате: {e.message}", exc_info=True)

    try:
        if data.startswith("subject_"):
            subject_code = data.split("_")[1]
            # has_special_access = await check_special_access(user_id)  # Алдыңғы шартты алып тастадық
            logger.info(f"Пайдаланушы {user_id} пәнді таңдайды: {subject_code}.")
            await safe_edit_text(
                callback,
                text="<b>🔍 Қандай нұсқа керек?</b>",
                parse_mode="HTML",
                reply_markup=get_variant_keyboard(subject_code)  # 'has_special_access' алып тасталды
            )
            return

        if data in {"main_menu", "back_subjects"}:
            # has_special_access = await check_special_access(user_id)  # Алдыңғы шартты алып тастадық
            logger.info(f"Пайдаланушы {user_id} негізгі мәзірге оралады.")
            await safe_edit_text(
                callback,
                text="<b>👋 Сәлеметсіз бе!</b> \n\nПәнді таңдаңыз:",
                parse_mode="HTML",
                reply_markup=get_subjects_keyboard()
            )
            return

        if data == "show_subscribers":
            async with pool.acquire() as conn:
                try:
                    count = await conn.fetchval("SELECT COUNT(*) FROM users")
                    await safe_edit_text(
                        callback,
                        text=f"📈 <b>Количество подписчиков</b>: {count}",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error("Пайдаланушылар санын есептеуде қате:", exc_info=True)
                    await safe_edit_text(
                        callback,
                        text="❌ Пайдаланушылар санын есептеуде қате болды.",
                        parse_mode="HTML"
                    )
            return

        if data.startswith("variant_free_"):
            subject_code = data.replace("variant_free_", "")
            await handle_free_variant(callback, subject_code)
            return

        if data.startswith("variant_special_"):
            subject_code = data.replace("variant_special_", "")
            access_type = "special"  # Тек "special" типін қолдану
            await handle_special_variant(callback, subject_code, access_type)
            return

        await callback.answer("❌ Тақырып анықталмады.", show_alert=False)
    except TelegramBadRequest as e:
        logger.error(f"TelegramBadRequest қатесі: {e.message}", exc_info=True)
        await safe_edit_text(
            callback,
            text="❌ Сұрауды өңдеу кезінде қате пайда болды.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error("Бейтаныс қате:", exc_info=True)
        await safe_edit_text(
            callback,
            text="❌ Бейтаныс қате пайда болды.",
            parse_mode="HTML"
        )

# Тегін пробникті өңдеу
async def handle_free_variant(callback: CallbackQuery, subject_code: str):
    user_id = callback.from_user.id
    subject_map = {
        "math": "Математика",
        "informatics": "Информатика",
    }
    subject_name = subject_map.get(subject_code, "Белгісіз")

    now = datetime.datetime.now()

    async with pool.acquire() as conn:
        try:
            # Егер пайдаланушы админ болса — шектеулерді елемейді
            if user_id in ADMIN_IDS:
                test = await conn.fetchrow("""
                    SELECT id, file_name, file_url
                    FROM tests
                    WHERE subject = $1
                    ORDER BY RANDOM() LIMIT 1
                """, subject_name)
                if test:
                    file_name, file_url = test["file_name"], test["file_url"]
                    await bot.send_document(
                        chat_id=user_id,
                        document=file_url,
                        caption=f"📄 <b>Тегін нұсқа (админ)</b>: {file_name}",
                        parse_mode="HTML",
                        protect_content=True
                    )
                else:
                    await callback.message.answer(
                        f"❌ Кешіріңіз, <b>{subject_name}</b> бойынша тегін пробниктер жоқ.",
                        parse_mode="HTML",
                        reply_markup=get_subjects_keyboard()
                    )
                await safe_edit_text(
                    callback,
                    text="<b>👋 Сәлеметсіз бе!</b> \n\nПәнді таңдаңыз:",
                    parse_mode="HTML",
                    reply_markup=get_subjects_keyboard()
                )
                return

            # Тегін тесттерге кулдаун тексеру
            cooldown_record = await conn.fetchrow("""
                SELECT next_free_time FROM user_cooldowns
                WHERE user_id=$1 AND subject_name=$2
            """, user_id, subject_name)

            if cooldown_record and cooldown_record["next_free_time"]:
                next_free_time = cooldown_record["next_free_time"]
                if now < next_free_time:
                    diff = next_free_time - now
                    seconds = int(diff.total_seconds())
                    hours = seconds // 3600
                    minutes = (seconds % 3600) // 60
                    await callback.message.answer(
                        f"⏳ <b>Сіз келесі тегін пробникті {hours} сағат {minutes} минуттан кейін ала аласыз.</b>",
                        parse_mode="HTML",
                        reply_markup=get_subjects_keyboard()
                    )
                    return

            # Тегін тесттерді кездейсоқ таңдау
            test = await conn.fetchrow("""
                SELECT id, file_name, file_url
                FROM tests
                WHERE subject = $1
                ORDER BY RANDOM()
                LIMIT 1
            """, subject_name)

            if not test:
                await callback.message.answer(
                    f"❌ Басқа тегін пробниктер жоқ.",
                    parse_mode="HTML",
                    reply_markup=get_subjects_keyboard()
                )
                return

            # Файлды жіберу
            test_id = test["id"]
            file_name, file_url = test["file_name"], test["file_url"]
            await bot.send_document(
                chat_id=user_id,
                document=file_url,
                caption=f"📄 <b>Тегін нұсқа</b>: {file_name}",
                parse_mode="HTML",
                protect_content=True
            )
            await safe_edit_text(
                callback,
                text="<b>👋 Сәлеметсіз бе!</b> \n\nПәнді таңдаңыз:",
                parse_mode="HTML",
                reply_markup=get_subjects_keyboard()
            )

            # Кулдаунды жаңарту: 24 сағат
            new_time = now + datetime.timedelta(hours=24)
            await conn.execute("""
                INSERT INTO user_cooldowns (user_id, subject_name, next_free_time, next_special_time)
                VALUES ($1, $2, $3, COALESCE(next_special_time, NOW() - INTERVAL '1 day'))
                ON CONFLICT (user_id, subject_name)
                DO UPDATE SET next_free_time=EXCLUDED.next_free_time
            """, user_id, subject_name, new_time)

        except TelegramBadRequest as e:
            logger.error(f"TelegramBadRequest қатесі: {e.message}", exc_info=True)
            await callback.message.answer("❌ Сұрауды өңдеу кезінде қате пайда болды.")
        except Exception as e:
            logger.error("Тегін нұсқаны орындау қатесі:", exc_info=True)
            await callback.message.answer("❌ Қате пайда болды. Админге жазыңыз.")

# Слив пробникті өңдеу
async def handle_special_variant(callback: CallbackQuery, subject_code: str, access_type: str):
    user_id = callback.from_user.id
    subject_map = {
        "math": "Математика",
        "informatics": "Информатика",
    }
    subject_name = subject_map.get(subject_code, "Белгісіз")

    now = datetime.datetime.now()

    async with pool.acquire() as conn:
        try:
            # Егер пайдаланушы админ болса, шектеулерді елемейді
            if user_id in ADMIN_IDS:
                test = await conn.fetchrow(
                    """
                    SELECT id, file_name, file_url 
                    FROM premium_tests
                    WHERE subject = $1 AND access_type = $2
                    ORDER BY RANDOM()
                    LIMIT 1
                    """,
                    subject_name, access_type
                )
                if test:
                    file_name, file_url = test["file_name"], test["file_url"]
                    await bot.send_document(
                        chat_id=user_id,
                        document=file_url,
                        caption=f"💎 <b>Слив нұсқа (админ)</b>: {file_name}",
                        parse_mode="HTML",
                        protect_content=True
                    )
                else:
                    await callback.message.answer(
                        f"❌ Бұл пән бойынша слив нұсқалар әлі жоқ.",
                        parse_mode="HTML",
                        reply_markup=get_subjects_keyboard()
                    )
                # Тест жіберілгеннен кейін мәзірді жаңарту
                await safe_edit_text(
                    callback,
                    text="<b>👋 Сәлеметсіз бе!</b> \n\nПәнді таңдаңыз:",
                    parse_mode="HTML",
                    reply_markup=get_subjects_keyboard()
                )
                return

            # Слив тесттерге кулдаун тексеру
            cooldown_record = await conn.fetchrow("""
                SELECT next_special_time FROM user_cooldowns
                WHERE user_id=$1 AND subject_name=$2
            """, user_id, subject_name)

            if cooldown_record and cooldown_record["next_special_time"]:
                next_special_time = cooldown_record["next_special_time"]
                if now < next_special_time:
                    diff = next_special_time - now
                    seconds = int(diff.total_seconds())
                    hours = seconds // 3600
                    minutes = (seconds % 3600) // 60
                    await callback.message.answer(
                        f"⏳ <b>Сіз келесі слив-пробникті {hours} сағат {minutes} минуттан кейін ала аласыз.</b>",
                        parse_mode="HTML",
                        reply_markup=get_subjects_keyboard()
                    )
                    return

            # Пайдаланушының слив қолжетімділігін тексеру
            access = await conn.fetchrow("""
                SELECT remaining_count, last_special_test_id
                FROM user_access
                WHERE user_id = $1 AND subject_name = $2 AND access_type = $3
            """, user_id, subject_name, access_type)

            if not access or access["remaining_count"] <= 0:
                await callback.message.answer(
                    "💰 <b>Бұл нұсқаға қолжетімділік жоқ. Бағасы 990 тг. Сатып алу үшін админдерге жазыңыз:</b> \n\n"
                    "📱 <a href='https://t.me/maxxsikxx'>Админ 1</a> \n"
                    "📱 <a href='https://t.me/x_ae_yedil'>Админ 2</a>",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=get_subjects_keyboard()
                )
                return

            remaining_count = access["remaining_count"]
            last_special_test_id = access["last_special_test_id"]

            # Слив тестті кездейсоқ таңдау
            test = await conn.fetchrow("""
                SELECT id, file_name, file_url 
                FROM premium_tests
                WHERE subject = $1 AND access_type = $2 AND id > $3
                ORDER BY id ASC
                LIMIT 1
            """, subject_name, access_type, last_special_test_id)

            if not test:
                await callback.message.answer(
                    f"❌ Бұл пән бойынша қолжетімді слив-нұсқалар таусылды.",
                    parse_mode="HTML",
                    reply_markup=get_subjects_keyboard()
                )
                return

            test_id, file_name, file_url = test["id"], test["file_name"], test["file_url"]
            await bot.send_document(
                chat_id=user_id,
                document=file_url,
                caption=f"💎 <b>Слив нұсқа</b>: {file_name}",
                parse_mode="HTML",
                protect_content=True
            )
            # Тест жіберілгеннен кейін мәзірді жаңарту
            await safe_edit_text(
                callback,
                text="<b>👋 Сәлеметсіз бе!</b> \n\nПәнді таңдаңыз:",
                parse_mode="HTML",
                reply_markup=get_subjects_keyboard()
            )
            # 'last_special_test_id' жаңарту және 'remaining_count' азайту
            await conn.execute("""
                UPDATE user_access
                SET remaining_count = remaining_count - 1,
                    last_special_test_id = $1
                WHERE user_id = $2 AND subject_name = $3 AND access_type = $4
            """, test_id, user_id, subject_name, access_type)

            # Кулдаунды жаңарту: 24 сағат
            new_time = now + datetime.timedelta(hours=24)
            await conn.execute("""
                INSERT INTO user_cooldowns (user_id, subject_name, next_free_time, next_special_time)
                VALUES ($1, $2, COALESCE(next_free_time, NOW() - INTERVAL '1 day'), $3)
                ON CONFLICT (user_id, subject_name)
                DO UPDATE SET next_special_time=EXCLUDED.next_special_time
            """, user_id, subject_name, new_time)

        except TelegramBadRequest as e:
            logger.error(f"TelegramBadRequest қатесі: {e.message}", exc_info=True)
            await callback.message.answer("❌ Сұрауды өңдеу кезінде қате пайда болды.")
        except Exception as e:
            logger.error("Слив нұсқаны орындау қатесі:", exc_info=True)
            await callback.message.answer("❌ Қате пайда болды (Слив нұсқа).")

# Администратор файлдарын өңдеу
async def handle_admin_files(message: Message):
    """
    Администраторларға арналған обработчик. Жүктелген файлдардың file_id-ін алады.
    """
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        return  # Егер админ болмаса, ешнәрсе жасамайды

    # Командаларды елемеу
    if message.text and message.text.startswith('/'):
        return

    if message.document:
        file_id = message.document.file_id
        await message.answer(f"📄 Құжат қабылданды!\nfile_id: {file_id}")
    elif message.photo:
        file_id = message.photo[-1].file_id
        await message.answer(f"📷 Сурет қабылданды!\nfile_id: {file_id}")
    elif message.video:
        file_id = message.video.file_id
        await message.answer(f"🎥 Видео қабылданды!\nfile_id: {file_id}")
    elif message.audio:
        file_id = message.audio.file_id
        await message.answer(f"🎵 Аудио қабылданды!\nfile_id: {file_id}")
    else:
        await message.answer("❓ Белгісіз файл түрі. Құжат, сурет, видео немесе аудио жіберіңізші.")

# 13. Администратор командалары

async def admin_grant_access(message: Message, command: Command):
    """
    Админдік команда. /grant_access <user_id> <subject>
    Пайдаланушыға слив пробниктерге қолжетімділік береді.
    """
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("❌ Сізде осы команданы пайдалану құқығы жоқ.")
        return

    args = command.args.split()
    if len(args) != 2:
        await message.answer("🔍 <b>Команданы дұрыс пайдаланыңыз:</b> /grant_access <user_id> <subject>\n\n"
                             "<b>Мысалы:</b> /grant_access 123456789 Математика",
                             parse_mode="HTML")
        return

    target_user_id, subject = args
    subject_map_reverse = {
        "Математика": "math",
        "Информатика": "informatics",
    }

    # Проверяем, ввёл ли админ правильно
    if subject not in subject_map_reverse:
        await message.answer("❌ Қате: Белгісіз пән атауы. Қол жетімді пәндер: Математика, Информатика.")
        return

    access_type = "special"
    additional_special_tests = 10  # Слив пробниктер саны

    # Записываем в user_access
    async with pool.acquire() as conn:
        try:
            # Пайдаланушыға слив пробниктерді қосу
            await conn.execute(
                """
                INSERT INTO user_access (user_id, subject_name, access_type, remaining_count, last_special_test_id)
                VALUES ($1, $2, $3, $4, 0)
                ON CONFLICT (user_id, subject_name, access_type)
                DO UPDATE SET remaining_count = user_access.remaining_count + EXCLUDED.remaining_count
                """,
                int(target_user_id), subject_map_reverse[subject], access_type, additional_special_tests
            )

            # Пайдаланушыға құттықтау хабарламасы жіберу
            await bot.send_message(
                chat_id=int(target_user_id),
                text=f"🎉 <b>Құттықтаймыз!</b> \n\nСізге <b>{subject}</b> пәні бойынша 10 слив пробниктерге қолжетімділік берілді.\n"
                     f"📈 Қосымша ақпарат алу үшін бізге хабарласыңыз.",
                parse_mode="HTML",
                protect_content=True
            )

            await message.answer(f"✅ Пайдаланушыға <b>{subject}</b> пәні бойынша 10 слив пробниктерге қолжетімділік берілді.",
                                 parse_mode="HTML")
        except Exception as e:
            logger.error("Слив қолжетімділікті беру қатесі:", exc_info=True)
            await message.answer("❌ Пайдаланушыға слив қолжетімділікті беру кезінде қате пайда болды.",
                                 parse_mode="HTML")

# 5. /add_test және /add_prem_test командаларын өңдеу

async def admin_add_test(message: Message, command: Command, state: FSMContext):
    """
    Админдік команда. /add_test <subject>
    Пайдаланушыға тегін пробниктерді қосу.
    """
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("❌ Сізде осы команданы пайдалану құқығы жоқ.")
        return

    args = command.args.split()
    if len(args) != 1:
        await message.answer("🔍 <b>Команданы дұрыс пайдаланыңыз:</b> /add_test <subject>\n\n"
                             "<b>Мысалы:</b> /add_test Математика",
                             parse_mode="HTML")
        return

    subject = args[0]
    subject_map_reverse = {
        "Математика": "math",
        "Информатика": "informatics",
    }

    if subject not in subject_map_reverse:
        await message.answer("❌ Қате: Белгісіз пән атауы. Қол жетімді пәндер: Математика, Информатика.")
        return

    # Ожидаем загрузку файла
    await message.answer("📄 <b>Тегін пробникті жүктеңіз:</b>", parse_mode="HTML")
    # Сохраняем состояние для ожидания файла
    await AnnouncementStates.waiting_for_text.set()
    # Сохраняем предметті FSMContext-ке
    await state.update_data(subject=subject_map_reverse[subject], access_type="free")

async def admin_add_prem_test(message: Message, command: Command, state: FSMContext):
    """
    Админдік команда. /add_prem_test <subject>
    Пайдаланушыға слив пробниктерді қосу.
    """
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("❌ Сізде осы команданы пайдалану құқығы жоқ.")
        return

    args = command.args.split()
    if len(args) != 1:
        await message.answer("🔍 <b>Команданы дұрыс пайдаланыңыз:</b> /add_prem_test <subject>\n\n"
                             "<b>Мысалы:</b> /add_prem_test Информатика",
                             parse_mode="HTML")
        return

    subject = args[0]
    subject_map_reverse = {
        "Математика": "math",
        "Информатика": "informatics",
    }

    if subject not in subject_map_reverse:
        await message.answer("❌ Қате: Белгісіз пән атауы. Қол жетімді пәндер: Математика, Информатика.")
        return

    # Ожидаем загрузку файла
    await message.answer("💎 <b>Слив пробникті жүктеңіз:</b>", parse_mode="HTML")
    # Сохраняем состояние для ожидания файла
    await AnnouncementStates.waiting_for_text.set()
    # Сохраняем предметті FSMContext-ке
    await state.update_data(subject=subject_map_reverse[subject], access_type="special")

async def receive_test_file(message: Message, state: FSMContext):
    """Админнан тест файлын алады және дерекқорға қосады."""
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("❌ Сізде осы команданы пайдалану құқығы жоқ.")
        return

    data = await state.get_data()
    subject = data.get("subject")
    access_type = data.get("access_type")

    file_url = None
    file_name = None

    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name
        file = await bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file.file_path}"
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_name = "photo.jpg"
        file = await bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file.file_path}"
    else:
        await message.answer("❌ Тек файл түрін ғана жүктеуіңіз қажет (құжат немесе сурет).",
                             parse_mode="HTML")
        return

    if not file_url or not file_name:
        await message.answer("❌ Файлды өңдеу кезінде қате пайда болды.", parse_mode="HTML")
        return

    async with pool.acquire() as conn:
        try:
            if access_type == "free":
                await conn.execute("""
                    INSERT INTO tests (subject, file_name, file_url)
                    VALUES ($1, $2, $3)
                """, subject, file_name, file_url)
                await message.answer(f"✅ Тегін пробникті <b>{subject}</b> пәні бойынша қосылды.",
                                     parse_mode="HTML")
            elif access_type == "special":
                await conn.execute("""
                    INSERT INTO premium_tests (subject, access_type, file_name, file_url)
                    VALUES ($1, $2, $3, $4)
                """, subject, access_type, file_name, file_url)
                await message.answer(f"✅ Слив пробникті <b>{subject}</b> пәні бойынша қосылды.",
                                     parse_mode="HTML")
            else:
                await message.answer("❌ Белгісіз access_type.", parse_mode="HTML")
                return
        except Exception as e:
            logger.error("Тест файлдарын қосу қатесі:", exc_info=True)
            await message.answer("❌ Тест файлдарын қосу кезінде қате пайда болды.", parse_mode="HTML")
            return

    # Тазарту
    await state.clear()

# 14. /help командасын өңдеу

async def show_help(message: Message):
    """
    Пайдаланушылар мен администраторларға қол жетімді командаларды көрсетеді.
    Алдыңғы мәзірді жояды, егер ол бар болса.
    """
    user_id = message.from_user.id

    # Алдыңғы мәзірді жою, егер бар болса
    if user_id in user_last_menu_message:
        try:
            await bot.delete_message(chat_id=user_id, message_id=user_last_menu_message[user_id])
            del user_last_menu_message[user_id]
            logger.info(f"Пайдаланушының {user_id} алдыңғы мәзірі жойылды.")
        except TelegramBadRequest:
            logger.warning(f"Пайдаланушының {user_id} алдыңғы мәзірін жою мүмкін болмады.")

    if user_id in ADMIN_IDS:
        help_text = (
            "<b>🛠 Административные Команды:</b>\n"
            "<b>/grant_access &lt;user_id&gt; &lt;subject&gt;</b> - Пайдаланушыға слив пробниктерге қолжетімділік беру.\n"
            "<b>/announce</b> - Барлық пайдаланушыларға хабарлама жіберу.\n"
            "<b>/add_test &lt;subject&gt;</b> - Тегін пробникті қосу.\n"
            "<b>/add_prem_test &lt;subject&gt;</b> - Слив пробникті қосу.\n\n"
            "<b>ℹ️ Негізгі ақпарат алу үшін төмендегі командаларды пайдаланыңыз.</b>"
        )
    else:
        help_text = (
            "<b>ℹ️ Қосымша сұрақтар бойынша администраторларға хабарласыңыз:</b>\n\n"
            "📱 <a href='https://t.me/maxxsikxx'>Админ 1</a> \n"
            "📱 <a href='https://t.me/x_ae_yedil'>Админ 2</a>"
        )

    if user_id in ADMIN_IDS:
        keyboard = get_help_keyboard()
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Админ 1", url="https://t.me/maxxsikxx")],
            [InlineKeyboardButton(text="Админ 2", url="https://t.me/x_ae_yedil")],
        ])

    try:
        sent_message = await message.answer(help_text, parse_mode="HTML", reply_markup=keyboard)
        user_last_menu_message[user_id] = sent_message.message_id
    except TelegramBadRequest as e:
        logger.error(f"Хабарлама жіберу кезінде қате: {e.message}", exc_info=True)
        await message.answer("❌ Хабарламаны жіберу кезінде қате пайда болды.")

# 15. Хабарлама жіберу процесін өңдеу

async def cmd_announce(message: Message, state: FSMContext):
    """Хабарлама жіберу процесін бастайды."""
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("❌ Сізде осы команданы пайдалану құқығы жоқ.")
        return

    await message.answer("📢 <b>Хабарламаны жазыңыз:</b>", parse_mode="HTML")
    await state.set_state(AnnouncementStates.waiting_for_text)

async def receive_announcement_text(message: Message, state: FSMContext):
    """Админнан хабарламаның мәтінін алады."""
    await state.update_data(announcement_text=message.text)
    await message.answer("📷 <b>Хабарламаға сурет қосқыңыз келсе, жүктеңіз немесе пропустить таңдаңыз:</b>",
                         parse_mode="HTML",
                         reply_markup=get_skip_or_add_photo_keyboard())
    await state.set_state(AnnouncementStates.waiting_for_photo)

async def receive_announcement_photo(callback: CallbackQuery, state: FSMContext):
    """Хабарламаның суретін алады немесе пропускады."""
    data = callback.data
    if data == "add_photo":
        await callback.message.answer("📷 <b>Суретті жүктеңіз:</b>", parse_mode="HTML")
        # Оставляем тот же стейт waiting_for_photo, чтобы дождаться фото
    elif data == "skip_photo":
        # Пропускаем фото
        await proceed_with_announcement(callback, state, photo=None)
    else:
        await callback.answer("❌ Түсініксіз әрекет.", show_alert=False)

async def proceed_with_announcement(callback: CallbackQuery, state: FSMContext, photo: str = None):
    """Хабарламаны барлық пайдаланушыларға жібереді."""
    data = await state.get_data()
    announcement_text = data.get("announcement_text", "")

    async with pool.acquire() as conn:
        try:
            users = await conn.fetch("SELECT user_id FROM users")
            logger.info(f"Барлық пайдаланушыларға хабарлама жіберілуде: {len(users)} адам.")
        except Exception as e:
            logger.error("Пайдаланушыларды алу қатесі:", exc_info=True)
            await notify_admins(f"Пайдаланушыларды алу кезінде қате: {str(e)}")
            await callback.message.answer("❌ Хабарламаны жіберу кезінде қате пайда болды.")
            await state.clear()
            return

    await callback.message.answer("📤 Хабарламаны жіберу басталды. Бұл біраз уақыт алуы мүмкін...", parse_mode="HTML")

    success = 0
    failed = 0
    for user in users:
        uid = user["user_id"]
        try:
            if photo:
                await bot.send_photo(
                    chat_id=uid,
                    photo=photo,
                    caption=announcement_text,
                    parse_mode="HTML",
                    protect_content=True
                )
            else:
                await bot.send_message(
                    chat_id=uid,
                    text=announcement_text,
                    parse_mode="HTML",
                    protect_content=True
                )
            success += 1
            await asyncio.sleep(0.05)  # Telegram лимиттерін сақтау үшін кідіріс
        except Exception as e:
            logger.error(f"Пайдаланушыға хабарлама жіберу кезінде қате: {uid} - {e}")
            failed += 1
            continue

    await callback.message.answer(f"✅ Хабарлама жіберілді! \n\nСәтті жіберілді: {success}\nҚателер: {failed}")
    await state.clear()

async def receive_announcement_photo_message(message: Message, state: FSMContext):
    """Админнан хабарламаның суретін алады (сообщением)."""
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("❌ Сізде осы команданы пайдалану құқығы жоқ.")
        return

    data = await state.get_data()
    announcement_text = data.get("announcement_text", "")

    if message.photo:
        photo = message.photo[-1].file_id
    else:
        photo = None

    async with pool.acquire() as conn:
        try:
            users = await conn.fetch("SELECT user_id FROM users")
            logger.info(f"Барлық пайдаланушыларға хабарлама жіберілуде: {len(users)} адам.")
        except Exception as e:
            logger.error("Пайдаланушыларды алу қатесі:", exc_info=True)
            await notify_admins(f"Пайдаланушыларды алу кезінде қате: {str(e)}")
            await message.answer("❌ Хабарламаны жіберу кезінде қате пайда болды.")
            await state.clear()
            return

    await message.answer("📤 Хабарламаны жіберу басталды. Бұл біраз уақыт алуы мүмкін...", parse_mode="HTML")

    success = 0
    failed = 0
    for user in users:
        uid = user["user_id"]
        try:
            if photo:
                await bot.send_photo(
                    chat_id=uid,
                    photo=photo,
                    caption=announcement_text,
                    parse_mode="HTML",
                    protect_content=True
                )
            else:
                await bot.send_message(
                    chat_id=uid,
                    text=announcement_text,
                    parse_mode="HTML",
                    protect_content=True
                )
            success += 1
            await asyncio.sleep(0.05)  # Telegram лимиттерін сақтау үшін кідіріс
        except Exception as e:
            logger.error(f"Пайдаланушыға хабарлама жіберу кезінде қате: {uid} - {e}")
            failed += 1
            continue

    await message.answer(f"✅ Хабарлама жіберілді! \n\nСәтті жіберілді: {success}\nҚателер: {failed}")
    await state.clear()

# 16. Администратор командаларын тіркеу
async def admin_commands_setup(dp: Dispatcher):
    dp.message.register(admin_grant_access, Command("grant_access"))
    dp.message.register(admin_add_test, Command("add_test"))
    dp.message.register(admin_add_prem_test, Command("add_prem_test"))
    dp.message.register(cmd_announce, Command("announce"))
    dp.message.register(receive_announcement_text, AnnouncementStates.waiting_for_text)
    dp.callback_query.register(receive_announcement_photo, F.data.in_({"add_photo", "skip_photo"}), AnnouncementStates.waiting_for_photo)
    dp.message.register(receive_test_file, F.content_type.in_([ContentType.DOCUMENT, ContentType.PHOTO]), AnnouncementStates.waiting_for_text)

    # Администраторларға файлдарды қабылдау обработчикін тіркеу
    dp.message.register(
        handle_admin_files,
        F.content_type.in_([ContentType.DOCUMENT, ContentType.PHOTO, ContentType.VIDEO, ContentType.AUDIO])
    )

    # /help командасын тіркеу
    dp.message.register(show_help, Command("help"))

# 17. Ботты іске қосу
async def main():
    await on_startup()
    await admin_commands_setup(dp)

    # Басқа командаларды тіркеу
    dp.message.register(send_welcome, Command("start"))
    dp.message.register(show_subscribers, Command("subscribers"))
    dp.message.register(show_subscribers, Command("count"))  # /count командасын /subscribers-ке тіркеу

    # CallbackQuery-лерді өңдеу
    dp.callback_query.register(handle_callback)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await pool.close()

if __name__ == "__main__":
    asyncio.run(main())

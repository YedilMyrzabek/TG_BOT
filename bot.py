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

# 4. Ботты инициализациялау (parse_mode жоқ)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# 5. Админдердің тізімі (немесе жиыны)
ADMIN_IDS = {1044841557, 1727718224}  # <-- қажетті Telegram user_id

# 6. Asyncpg арқылы дерекқорға қосылу
async def get_db_pool():
    return await asyncpg.create_pool(dsn=DB_URL, command_timeout=60)

# 7. Дерекқорды инициализациялау
async def initialize_db(pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_cooldown (
                user_id BIGINT,
                subject_name TEXT,
                next_free_time TIMESTAMP,
                PRIMARY KEY (user_id, subject_name)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_access (
                user_id BIGINT,
                subject_name TEXT,
                access_type TEXT,
                remaining_count INTEGER,
                last_premium_test_id INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, subject_name, access_type)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tests (
                id SERIAL PRIMARY KEY,
                subject TEXT,
                file_name TEXT,
                file_url TEXT
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS premium_tests (
                id SERIAL PRIMARY KEY,
                subject TEXT,
                access_type TEXT NOT NULL DEFAULT 'special',
                file_name TEXT,
                file_url TEXT
            );
        """)

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

def get_variant_keyboard(subject_code: str, has_premium_access: bool):
    """Тест түрін таңдау үшін клавиатура."""
    buttons = [
        [InlineKeyboardButton(text="Тегін нұсқа 🆓", callback_data=f"variant_free_{subject_code}")],
        [InlineKeyboardButton(text="Премиум нұсқа 💎", callback_data=f"variant_special_{subject_code}")],
        [InlineKeyboardButton(text="Артқа 🔙", callback_data="back_subjects")]
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
async def safe_edit_text(callback: CallbackQuery, text: str, parse_mode: str = None, reply_markup: InlineKeyboardMarkup = None):
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
            await bot.send_message(admin_id, f"❗ *Қате:* {message}", parse_mode="Markdown")
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

    # Пайдаланушының премиум қолжетімділігін тексеру
    has_premium_access = await check_premium_access(user_id)
    logger.info(f"Пайдаланушы {user_id} премиум қолжетімділікке ие: {has_premium_access}")

    # Жаңартылған сәлемдесу хабарламасы
    welcome_text = (
        "👋 Сәлеметсіз бе! \n\n"
        "Біз сізге Математика және Информатика пәндер бойынша үздік пробниктерді ұсынамыз.\n\n"
        "🔍 Бесплатные пробниктер арқылы дайындалыңыз және өз деңгейіңізді арттырыңыз.\n\n"
        "💎 Премиум пробниктер арқылы ұбтда кездескен(слив) және алдағы уақытта кездесуі мүмкін нұсқалармен өзіңізді сынап көріңіз.\n\n"
        "p.s келесі нұсқаны 24 сағаттан соң ала аласыз🤓.\n\n"
        "ℹ️ Қосымша сұрақтар бойынша /help."
    )

    keyboard = get_subjects_keyboard()
    sent_message = await message.answer(welcome_text, parse_mode="Markdown", reply_markup=keyboard)

    # /help шақыру кезінде жою үшін message_id сақтайды
    user_last_menu_message[user_id] = sent_message.message_id

# Пайдаланушының премиум қолжетімділігін тексеру
async def check_premium_access(user_id: int) -> bool:
    """Пайдаланушының премиум пробниктерге қолжетімділігін тексереді."""
    async with pool.acquire() as conn:
        access = await conn.fetchrow("""
            SELECT remaining_count, last_premium_test_id FROM user_access
            WHERE user_id = $1 AND access_type = 'special' AND remaining_count > 0
            LIMIT 1
        """, user_id)
        if access:
            logger.info(f"Пайдаланушы {user_id} {access['remaining_count']} премиум пробниктерге ие.")
            return True
        else:
            logger.info(f"Пайдаланушы {user_id} премиум пробниктерге ие емес.")
            return False

# Пайдаланушылар санын көрсету
async def show_subscribers(message: Message):
    """Пайдаланушылар санын көрсетеді."""
    async with pool.acquire() as conn:
        try:
            count = await conn.fetchval("SELECT COUNT(*) FROM users")
            await message.answer(f"📈 *Количество подписчиков*: {count}", parse_mode="Markdown")
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
            has_premium_access = await check_premium_access(user_id)
            logger.info(f"Пайдаланушы {user_id} пәнді таңдайды: {subject_code}. Премиум қолжетімділік: {has_premium_access}")
            await safe_edit_text(
                callback,
                text="🔍 *Қандай нұсқа керек?*",
                parse_mode="Markdown",
                reply_markup=get_variant_keyboard(subject_code, has_premium_access)
            )
            return

        if data in {"main_menu", "back_subjects"}:
            has_premium_access = await check_premium_access(user_id)
            logger.info(f"Пайдаланушы {user_id} негізгі мәзірге оралады. Премиум қолжетімділік: {has_premium_access}")
            await safe_edit_text(
                callback,
                text="👋 Сәлеметсіз бе! \n\nПәнді таңдаңыз:",
                parse_mode="Markdown",
                reply_markup=get_subjects_keyboard()
            )
            return

        if data == "show_subscribers":
            async with pool.acquire() as conn:
                try:
                    count = await conn.fetchval("SELECT COUNT(*) FROM users")
                    await safe_edit_text(
                        callback,
                        text=f"📈 *Количество подписчиков*: {count}",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error("Пайдаланушылар санын есептеуде қате:", exc_info=True)
                    await safe_edit_text(
                        callback,
                        text="❌ Пайдаланушылар санын есептеуде қате болды.",
                        parse_mode="Markdown"
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
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error("Бейтаныс қате:", exc_info=True)
        await safe_edit_text(
            callback,
            text="❌ Бейтаныс қате пайда болды.",
            parse_mode="Markdown"
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
            # Егер пайдаланушы админ болса, шектеулерді елемейді
            if user_id in ADMIN_IDS:
                test = await conn.fetchrow(
                    "SELECT file_name, file_url FROM tests WHERE subject = $1 ORDER BY RANDOM() LIMIT 1",
                    subject_name
                )
                if test:
                    file_name, file_url = test["file_name"], test["file_url"]
                    await bot.send_document(
                        chat_id=user_id,
                        document=file_url,
                        caption=f"📄 *Тегін нұсқа:* {file_name}",
                        parse_mode="Markdown",
                        protect_content=True  # Пересылуға тыйым салу
                    )
                else:
                    await callback.message.answer(
                        f"❌ Кешіріңіз, *{subject_name}* бойынша тегін пробниктер жоқ.",
                        parse_mode="Markdown",
                        reply_markup=get_subjects_keyboard()
                    )
                # Тест жіберілгеннен кейін мәзірді жаңарту
                await safe_edit_text(
                    callback,
                    text="👋 Сәлеметсіз бе! \n\nПәнді таңдаңыз:",
                    parse_mode="Markdown",
                    reply_markup=get_subjects_keyboard()
                )
                return

            # Премиум қолжетімділіктің бар-жоғын тексеру
            special_access = await conn.fetchrow("""
                SELECT remaining_count, last_premium_test_id 
                FROM user_access
                WHERE user_id = $1 AND subject_name = $2 AND access_type = 'special'
            """, user_id, subject_name)

            if special_access and special_access["remaining_count"] > 0:
                await callback.message.answer(
                    "💎 *Сізде премиум қолжетімділік бар, тегін тесттерді алу мүмкіндігі жоқ.*",
                    parse_mode="Markdown",
                    reply_markup=get_subjects_keyboard()
                )
                return

            # Кулдаунды тексеру
            next_free_time = await conn.fetchval("""
                SELECT next_free_time FROM user_cooldown WHERE user_id = $1 AND subject_name = $2
            """, user_id, subject_name)

            if next_free_time and now < next_free_time:
                diff = next_free_time - now
                hours = diff.seconds // 3600
                minutes = (diff.seconds % 3600) // 60
                seconds = diff.seconds % 60
                await callback.message.answer(
                    f"⏳ *Сіз осы бөлімнің тегін нұсқасын {hours} сағат {minutes} минут {seconds} секундтан кейін ғана ала аласыз.*",
                    parse_mode="Markdown",
                    reply_markup=get_subjects_keyboard()
                )
                return

            # Случайный тегін тест
            test = await conn.fetchrow(
                "SELECT file_name, file_url FROM tests WHERE subject = $1 ORDER BY RANDOM() LIMIT 1",
                subject_name
            )
            if test:
                file_name, file_url = test["file_name"], test["file_url"]
                await bot.send_document(
                    chat_id=user_id,
                    document=file_url,
                    caption=f"📄 *Тегін нұсқа:* {file_name}",
                    parse_mode="Markdown",
                    protect_content=True  # Пересылуға тыйым салу
                )
                # Тест жіберілгеннен кейін мәзірді жаңарту
                await safe_edit_text(
                    callback,
                    text="👋 Сәлеметсіз бе! \n\nПәнді таңдаңыз:",
                    parse_mode="Markdown",
                    reply_markup=get_subjects_keyboard()
                )
            else:
                await callback.message.answer(
                    f"❌ Кешіріңіз, *{subject_name}* бойынша тегін пробниктер жоқ.",
                    parse_mode="Markdown",
                    reply_markup=get_subjects_keyboard()
                )
        except TelegramBadRequest as e:
            logger.error(f"TelegramBadRequest қатесі: {e.message}", exc_info=True)
            await callback.message.answer("❌ Сұрауды өңдеу кезінде қате пайда болды.")
        except Exception as e:
            logger.error("Тегін нұсқаны орындау қатесі:", exc_info=True)
            await callback.message.answer("❌ Қате пайда болды. Админге жазыңыз.")

        # Кулдаунды жаңарту, егер пайдаланушы админ болмаса
        if user_id not in ADMIN_IDS:
            new_time = now + datetime.timedelta(hours=24)  # Кулдаунды 24 сағатқа орнату
            try:
                await conn.execute(
                    """
                    INSERT INTO user_cooldown (user_id, subject_name, next_free_time)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id, subject_name)
                    DO UPDATE SET next_free_time = EXCLUDED.next_free_time
                    """,
                    user_id, subject_name, new_time
                )
                logger.info(f"Кулдаун жаңартылды: Пайдаланушы {user_id}, пән {subject_name}, уақыт {new_time}.")
            except Exception as e:
                logger.error("Кулдаунды жаңарту қатесі:", exc_info=True)

# Премиум пробникті өңдеу
async def handle_special_variant(callback: CallbackQuery, subject_code: str, access_type: str):
    user_id = callback.from_user.id
    subject_map = {
        "math": "Математика",
        "informatics": "Информатика",
    }
    subject_name = subject_map.get(subject_code, "Белгісіз")

    async with pool.acquire() as conn:
        try:
            # Егер пайдаланушы админ болса, шектеулерді елемейді
            if user_id in ADMIN_IDS:
                logger.info(f"Пайдаланушы {user_id} админ болып табылады.")
                test = await conn.fetchrow(
                    "SELECT file_name, file_url FROM premium_tests WHERE subject = $1 AND access_type = $2 ORDER BY RANDOM() LIMIT 1",
                    subject_name, access_type
                )
                if test:
                    file_name, file_url = test["file_name"], test["file_url"]
                    await bot.send_document(
                        chat_id=user_id,
                        document=file_url,
                        caption=f"💎 *Премиум нұсқа:* {file_name}",
                        parse_mode="Markdown",
                        protect_content=True  # Пересылуға тыйым салу
                    )
                else:
                    await callback.message.answer(
                        f"❌ Бұл пән бойынша премиум нұсқалар әлі жоқ.",
                        parse_mode="Markdown",
                        reply_markup=get_subjects_keyboard()
                    )
                # Тест жіберілгеннен кейін мәзірді жаңарту
                await safe_edit_text(
                    callback,
                    text="👋 Сәлеметсіз бе! \n\nПәнді таңдаңыз:",
                    parse_mode="Markdown",
                    reply_markup=get_subjects_keyboard()
                )
                return

            # Пайдаланушының премиум қолжетімділігін тексеру
            access = await conn.fetchrow("""
                SELECT remaining_count, last_premium_test_id 
                FROM user_access
                WHERE user_id = $1 AND subject_name = $2 AND access_type = $3
                LIMIT 1
            """, user_id, subject_name, access_type)

            if not access or access["remaining_count"] <= 0:
                logger.info(f"Пайдаланушы {user_id} премиум қолжетімділігі жоқ немесе лимит таусылды.")
                await callback.message.answer(
                    "💰 *Бұл нұсқаға қолжетімділік жоқ. Бағасы 990 тг. Сатып алу үшін админдерге жазыңыз:* \n\n"
                    "📱 [Админ 1](https://t.me/maxxsikxx) \n"
                    "📱 [Админ 2](https://t.me/x_ae_yedil)",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                    reply_markup=get_subjects_keyboard()
                )
                return

            remaining_count = access["remaining_count"]
            last_premium_test_id = access["last_premium_test_id"]
            logger.info(f"Пайдаланушы {user_id} {remaining_count} премиум пробниктерге ие. Соңғы тест ID: {last_premium_test_id}")

            # Келесі премиум тестті алу
            test = await conn.fetchrow("""
                SELECT id, file_name, file_url 
                FROM premium_tests
                WHERE subject = $1 AND access_type = $2 AND id > $3
                ORDER BY id ASC
                LIMIT 1
            """, subject_name, access_type, last_premium_test_id)

            if test:
                test_id, file_name, file_url = test["id"], test["file_name"], test["file_url"]
                await bot.send_document(
                    chat_id=user_id,
                    document=file_url,
                    caption=f"💎 *Премиум нұсқа:* {file_name}",
                    parse_mode="Markdown",
                    protect_content=True  # Пересылуға тыйым салу
                )
                # Тест жіберілгеннен кейін мәзірді жаңарту
                await safe_edit_text(
                    callback,
                    text="👋 Сәлеметсіз бе! \n\nПәнді таңдаңыз:",
                    parse_mode="Markdown",
                    reply_markup=get_subjects_keyboard()
                )
                # 'last_premium_test_id' жаңарту және 'remaining_count' азайту
                await conn.execute("""
                    UPDATE user_access
                    SET remaining_count = remaining_count - 1,
                        last_premium_test_id = $1
                    WHERE user_id = $2 AND subject_name = $3 AND access_type = $4
                """, test_id, user_id, subject_name, access_type)
                logger.info(f"Пайдаланушы {user_id} премиум-пробникті алды: ID {test_id}. Қалған: {remaining_count - 1}")
            else:
                logger.warning(f"Премиум-пробниктер табылмады: Пән {subject_name}, Қолжетімділік {access_type}")
                await callback.message.answer(
                    f"❌ Бұл пән бойынша премиум нұсқалар әлі жоқ.",
                    parse_mode="Markdown",
                    reply_markup=get_subjects_keyboard()
                )
        except TelegramBadRequest as e:
            logger.error(f"TelegramBadRequest қатесі: {e.message}", exc_info=True)
            await callback.message.answer("❌ Сұрауды өңдеу кезінде қате пайда болды.")
        except Exception as e:
            logger.error("Премиум нұсқаны орындау қатесі:", exc_info=True)
            await callback.message.answer("❌ Қате пайда болды (Премиум нұсқа).")

        # Кулдаунды жаңарту, егер пайдаланушы админ болмаса
        if user_id not in ADMIN_IDS and access and access["remaining_count"] > 0:
            try:
                await conn.execute(
                    """
                    UPDATE user_access
                    SET remaining_count = remaining_count - 1
                    WHERE user_id = $1 AND subject_name = $2 AND access_type = $3
                    """,
                    user_id, subject_name, access_type
                )
            except Exception as e:
                logger.error("remaining_count жаңарту қатесі:", exc_info=True)

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
    Пайдаланушыға премиум пробниктерге қолжетімділік береді.
    """
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("❌ Сізде осы команданы пайдалану құқығы жоқ.")
        return

    args = command.args.split()
    if len(args) != 2:
        await message.answer("🔍 *Команданы дұрыс пайдаланыңыз:* /grant_access <user_id> <subject>\n\n"
                             "*Мысалы:* /grant_access 123456789 Математика",
                             parse_mode="Markdown")
        return

    target_user_id, subject = args
    subject_map_reverse = {
        "Математика": "math",
        "Информатика": "informatics",
    }

    if subject not in subject_map_reverse:
        await message.answer("❌ Қате: Белгісіз пән атауы. Қол жетімді пәндер: Математика, Информатика.")
        return

    access_type = "special"
    additional_premium_tests = 10  # Премиум пробниктер саны

    async with pool.acquire() as conn:
        try:
            # Пайдаланушыға премиум пробниктерді қосу
            await conn.execute(
                """
                INSERT INTO user_access (user_id, subject_name, access_type, remaining_count)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, subject_name, access_type)
                DO UPDATE SET remaining_count = user_access.remaining_count + EXCLUDED.remaining_count
                """,
                int(target_user_id), subject_map_reverse[subject], access_type, additional_premium_tests
            )

            # Пайдаланушыға құттықтау хабарламасы жіберу
            await bot.send_message(
                chat_id=int(target_user_id),
                text=f"🎉 *Құттықтаймыз!* \n\nСізге *{subject}* пәні бойынша 10 премиум пробниктерге қолжетімділік берілді.\n"
                     f"📈 Қосымша ақпарат алу үшін бізге хабарласыңыз.",
                parse_mode="Markdown",
                protect_content=True  # Пересылуға тыйым салу
            )

            await message.answer(f"✅ Пайдаланушыға *{subject}* пәні бойынша 10 премиум пробниктерге қолжетімділік берілді.",
                                 parse_mode="Markdown")
        except Exception as e:
            logger.error("Премиум қолжетімділікті беру қатесі:", exc_info=True)
            await message.answer("❌ Пайдаланушыға премиум қолжетімділікті беру кезінде қате пайда болды.")

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
            # Хабарламаның жазбасын жою
            del user_last_menu_message[user_id]
            logger.info(f"Пайдаланушының {user_id} алдыңғы мәзірі жойылды.")
        except TelegramBadRequest:
            logger.warning(f"Пайдаланушының {user_id} алдыңғы мәзірін жою мүмкін болмады. Мүмкін, хабарлама бұрын жойылған.")

    if user_id in ADMIN_IDS:
        help_text = (
            "🛠 *Административные Команды:* \n"
            "/grant_access <user_id> <subject> - Пайдаланушыға премиум пробниктерге қолжетімділік беру.\n"
            "/announce - Барлық пайдаланушыларға хабарлама жіберу.\n\n"
            "ℹ️ *Негізгі ақпарат алу үшін төмендегі командаларды пайдаланыңыз.*"
        )
    else:
        help_text = (
            "ℹ️ *Қосымша сұрақтар бойынша администраторларға хабарласыңыз:* \n\n"
            "📱 [Админ 1](https://t.me/maxxsikxx) \n"
            "📱 [Админ 2](https://t.me/x_ae_yedil)"
        )

    if user_id in ADMIN_IDS:
        keyboard = get_help_keyboard()
    else:
        # Пайдаланушыға админдердің контактілері көрсетіледі
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Админ 1", url="https://t.me/maxxsikxx")],
            [InlineKeyboardButton(text="Админ 2", url="https://t.me/x_ae_yedil")],
        ])

    sent_message = await message.answer(help_text, parse_mode="Markdown", reply_markup=keyboard)

    # Хабарламаның жазбасын сақтау
    user_last_menu_message[user_id] = sent_message.message_id

# 15. Хабарлама жіберу процесін өңдеу

async def cmd_announce(message: Message, state: FSMContext):
    """Хабарлама жіберу процесін бастайды."""
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("❌ Сізде осы команданы пайдалану құқығы жоқ.")
        return

    await message.answer("📢 *Хабарламаны жазыңыз:*", parse_mode="Markdown")
    await state.set_state(AnnouncementStates.waiting_for_text)

async def receive_announcement_text(message: Message, state: FSMContext):
    """Админнан хабарламаның мәтінін алады."""
    await state.update_data(announcement_text=message.text)
    await message.answer("📷 *Хабарламаға сурет қосқыңыз келсе, жүктеңіз немесе пропустить таңдаңыз:*",
                         parse_mode="Markdown",
                         reply_markup=get_skip_or_add_photo_keyboard())
    await state.set_state(AnnouncementStates.waiting_for_photo)

async def receive_announcement_photo(callback: CallbackQuery, state: FSMContext):
    """Хабарламаның суретін алады немесе пропускады."""
    data = callback.data
    if data == "add_photo":
        await callback.message.answer("📷 *Суретті жүктеңіз:*", parse_mode="Markdown")
        await state.set_state(AnnouncementStates.waiting_for_photo)
    elif data == "skip_photo":
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

    await callback.message.answer("📤 Хабарламаны жіберу басталды. Бұл біраз уақыт алуы мүмкін...", parse_mode="Markdown")

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
                    parse_mode="Markdown",
                    protect_content=True  # Пересылуға тыйым салу
                )
            else:
                await bot.send_message(
                    chat_id=uid,
                    text=announcement_text,
                    parse_mode="Markdown",
                    protect_content=True  # Пересылуға тыйым салу
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
    """Админнан хабарламаның суретін алады."""
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("❌ Сізде осы команданы пайдалану құқығы жоқ.")
        return

    if message.photo:
        photo = message.photo[-1].file_id
        await proceed_with_announcement_via_message(message, state, photo)
    else:
        await proceed_with_announcement_via_message(message, state, photo=None)

async def proceed_with_announcement_via_message(message: Message, state: FSMContext, photo: str = None):
    """Хабарламаны барлық пайдаланушыларға жібереді (қабылдау арқылы)."""
    data = await state.get_data()
    announcement_text = data.get("announcement_text", "")

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

    await message.answer("📤 Хабарламаны жіберу басталды. Бұл біраз уақыт алуы мүмкін...", parse_mode="Markdown")

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
                    parse_mode="Markdown",
                    protect_content=True  # Пересылуға тыйым салу
                )
            else:
                await bot.send_message(
                    chat_id=uid,
                    text=announcement_text,
                    parse_mode="Markdown",
                    protect_content=True  # Пересылуға тыйым салу
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

async def admin_commands_setup():
    dp.message.register(admin_grant_access, Command("grant_access"))
    dp.message.register(cmd_announce, Command("announce"))
    dp.message.register(receive_announcement_text, AnnouncementStates.waiting_for_text)
    dp.callback_query.register(receive_announcement_photo, F.data.in_({"add_photo", "skip_photo"}), AnnouncementStates.waiting_for_photo)
    dp.callback_query.register(receive_announcement_photo, AnnouncementStates.waiting_for_photo)
    dp.message.register(receive_announcement_photo_message, AnnouncementStates.waiting_for_photo)

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
    await admin_commands_setup()

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

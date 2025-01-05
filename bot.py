import os
import asyncio
import psycopg2
import datetime

from aiogram import Bot, Dispatcher
from aiogram.filters.command import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

API_TOKEN = os.getenv("TELEGRAM_TOKEN")
DB_URL = os.getenv("DB_URL")

if not API_TOKEN or not DB_URL:
    raise ValueError("Отсутствует TELEGRAM_TOKEN или DB_URL в файле .env!")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Список (или множество) админов
ADMIN_IDS = {1044841557, 1727718224}  # <-- подставьте нужные Telegram user_id

# Подключение к базе данных
try:
    conn = psycopg2.connect(DB_URL, options="-c client_encoding=UTF8")
    cursor = conn.cursor()
    print("Успешное подключение к базе данных!")
except Exception as e:
    print("Ошибка подключения к базе данных:", e)
    exit()

# ---------- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ----------

def initialize_db():
    """Создает необходимые таблицы, если они не существуют."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_cooldown (
            user_id BIGINT,
            subject_name TEXT,
            next_free_time TIMESTAMP,
            PRIMARY KEY (user_id, subject_name)
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_access (
            user_id BIGINT,
            subject_name TEXT,
            access_type TEXT,
            remaining_count INTEGER,
            PRIMARY KEY (user_id, subject_name, access_type)
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tests (
            id SERIAL PRIMARY KEY,
            subject TEXT,
            file_name TEXT,
            file_url TEXT
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS premium_tests (
            id SERIAL PRIMARY KEY,
            subject TEXT,
            access_type TEXT,
            file_name TEXT,
            file_url TEXT
        );
    """)
    conn.commit()

initialize_db()
#kjsdbc

# ---------- ФУНКЦИИ ДЛЯ СОЗДАНИЯ КЛАВИАТУР ----------

def get_subjects_keyboard():
    """Клавиатура для выбора предмета (без 'Мат. сауаттылық')."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Информатика", callback_data="subject_informatics")],
        [InlineKeyboardButton(text="Математика", callback_data="subject_math")],
    ])
    return keyboard

def get_variant_keyboard(subject_code: str):
    """Клавиатура для выбора варианта теста."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Тегін нұсқа", callback_data=f"variant_free_{subject_code}")],
        [InlineKeyboardButton(text="Ерекше нұсқа (990 тг)", callback_data=f"variant_special_{subject_code}")],
        [InlineKeyboardButton(text="Ерекше+Жауап (1490 тг)", callback_data=f"variant_special_with_answers_{subject_code}")],
        [InlineKeyboardButton(text="Артқа", callback_data="back_subjects")],
    ])
    return keyboard

def get_info_keyboard():
    """Клавиатура для отображения информации."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Меню", callback_data="main_menu")],
        [InlineKeyboardButton(text="Подписчиков: 🔢", callback_data="show_subscribers")]
    ])
    return keyboard

# ---------- ОБРАБОТЧИКИ ----------

@dp.message(Command("start"))
async def send_welcome(message: Message):
    """Обработчик команды /start. Регистрирует пользователя и отправляет приветственное сообщение."""
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name
    

    # Регистрация пользователя
    try:
        cursor.execute("""
            INSERT INTO users (user_id, username, first_name, last_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
        """, (user_id, username, first_name, last_name))
        conn.commit()
    except Exception as e:
        print("Ошибка регистрации пользователя:", e)

    keyboard = get_subjects_keyboard()
    await message.answer("Сәлем! Пәнді таңдаңыз:", reply_markup=keyboard)

@dp.message(Command("subscribers"))
async def show_subscribers(message: Message):
    """Команда для отображения количества подписчиков."""
    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]
    await message.answer(f"Количество подписчиков: {count}")

@dp.callback_query()
async def handle_callback(callback: CallbackQuery):
    data = callback.data

    if data.startswith("subject_"):
        await callback.message.edit_text(
            text="Қандай нұсқа керек?",
            reply_markup=get_variant_keyboard(data)
        )
        await callback.answer()
        return

    if data == "main_menu":
        await callback.message.edit_text(
            text="Сәлем! Пәнді таңдаңыз:",
            reply_markup=get_subjects_keyboard()
        )
        await callback.answer()
        return

    if data == "back_subjects":
        await callback.message.edit_text(
            text="Сәлем! Пәнді таңдаңыз:",
            reply_markup=get_subjects_keyboard()
        )
        await callback.answer()
        return

    if data == "show_subscribers":
        cursor.execute("SELECT COUNT(*) FROM users")
        count = cursor.fetchone()[0]
        await callback.message.answer(f"Количество подписчиков: {count}")
        await callback.answer()
        return

    if data.startswith("variant_free_"):
        subject_code = data.replace("variant_free_", "")
        await handle_free_variant(callback, subject_code)
        return

    if data.startswith("variant_special_"):
        splitted = data.split("_")
        access_type = "special_with_answers" if "with_answers" in data else "special"
        subject_code = "_".join(splitted[2:])
        await handle_special_variant(callback, subject_code, access_type)
        return

    await callback.answer("Тақырып анықталмады.", show_alert=False)

async def handle_free_variant(callback: CallbackQuery, subject_code: str):
    user_id = callback.from_user.id
    subject_map = {
        "subject_informatics": "Информатика",
        "subject_math": "Математика",
    }
    subject_name = subject_map.get(subject_code, "Белгісіз")

    cursor.execute("SELECT next_free_time FROM user_cooldown WHERE user_id = %s AND subject_name = %s", (user_id, subject_name))
    row = cursor.fetchone()
    now = datetime.datetime.now()

    if row:
        next_time = row[0]
        if now < next_time:
            diff = next_time - now
            hours = diff.seconds // 3600
            minutes = (diff.seconds % 3600) // 60
            await callback.message.answer(
                f"Сіз осы бөлімнің тегін нұсқасын {hours} сағат {minutes} минуттан кейін ғана ала аласыз.",
                reply_markup=get_subjects_keyboard()
            )
            await callback.answer()
            return
        

    try:
        cursor.execute(
            "SELECT file_name, file_url FROM tests WHERE subject = %s LIMIT 1",
            (subject_name,)
        )
        test = cursor.fetchone()
        if test:
            file_name, file_url = test
            await callback.message.answer_document(
                file_url,
                caption=f"ҰБТда келу ықтималдығы аздау нұсқа (Тегін) - {file_name}",
                reply_markup=get_subjects_keyboard(),
                protect_content=True  # Предотвращает пересылку
            )
        else:
            await callback.message.answer(
                f"Кешіріңіз, {subject_name} бойынша тегін пробниктер жоқ.",
                reply_markup=get_subjects_keyboard()
            )
    except Exception as e:
        await callback.message.answer("Қате пайда болды. Әкімшіге жазыңыз.")
        print("Ошибка при выполнении запроса (free):", e)

    new_time = now + datetime.timedelta(hours=24)
    cursor.execute(
        """
        INSERT INTO user_cooldown (user_id, subject_name, next_free_time)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, subject_name)
        DO UPDATE SET next_free_time = EXCLUDED.next_free_time
        """,
        (user_id, subject_name, new_time)
    )
    conn.commit()

    await callback.answer()

async def handle_special_variant(callback: CallbackQuery, subject_code: str, access_type: str):
    user_id = callback.from_user.id
    subject_map = {
        "subject_informatics": "Информатика",
        "subject_math": "Математика",
    }
    subject_name = subject_map.get(subject_code, "Белгісіз")

    cursor.execute(
        """
        SELECT remaining_count 
        FROM user_access
        WHERE user_id = %s
          AND subject_name = %s
          AND access_type = %s
        """,
        (user_id, subject_name, access_type)
    )
    row = cursor.fetchone()

    if not row:
        price = "990" if access_type == "special" else "1490"
        await callback.message.answer(
            f"Бұл нұсқаға қолжетімділік жоқ. Бағасы {price} тг. Сатып алу үшін әкімшіге жазыңыз.",
            reply_markup=get_subjects_keyboard()
        )
        await callback.answer()
        return

    remaining_count = row[0]
    if remaining_count <= 0:
        await callback.message.answer(f"Сіздің {subject_name} пәні бойынша пробниктердің лимиті таусылды.")
        await callback.answer()
        return

    try:
        cursor.execute(
            """
            SELECT file_name, file_url 
            FROM premium_tests
            WHERE subject = %s AND access_type = %s
            ORDER BY id
            LIMIT 1
            """,
            (subject_name, access_type)
        )
        test = cursor.fetchone()

        if test:
            file_name, file_url = test
            await callback.message.answer_document(
                file_url,
                caption=f"Ерекше нұсқа: {file_name}",
                protect_content=True  # Предотвращает пересылку
            )

            cursor.execute(
                """
                UPDATE user_access
                SET remaining_count = remaining_count - 1
                WHERE user_id = %s
                  AND subject_name = %s
                  AND access_type = %s
                """,
                (user_id, subject_name, access_type)
            )
            conn.commit()
        else:
            await callback.message.answer(f"Бұл пән бойынша Ерекше нұсқалар әлі жоқ.", reply_markup=get_subjects_keyboard())
    except Exception as e:
        await callback.message.answer("Қате пайда болды (Ерекше нұсқа).")
        print("Ошибка при выполнении запроса (special):", e)

    await callback.answer()

@dp.message()
async def handle_admin_files(message: Message):
    """
    Обработчик для администраторов. Получает file_id загруженных файлов.
    """
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        return  # Игнорируем, если пользователь не админ
    

    if message.document:
        file_id = message.document.file_id
        await message.answer(f"Документ получен!\nfile_id: {file_id}")
    elif message.photo:
        file_id = message.photo[-1].file_id
        await message.answer(f"Фото получено!\nfile_id: {file_id}")
    elif message.video:
        file_id = message.video.file_id
        await message.answer(f"Видео получено!\nfile_id: {file_id}")
    elif message.audio:
        file_id = message.audio.file_id
        await message.answer(f"Аудио получено!\nfile_id: {file_id}")
    else:
        await message.answer("Неизвестный тип файла. Пожалуйста, отправьте документ, фото, видео или аудио.")

@dp.message(Command("count"))
async def show_subscriber_count(message: Message):
    """Отправляет количество подписчиков. Доступно всем пользователям."""
    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]
    await message.answer(f"Количество подписчиков: {count}", reply_markup=get_info_keyboard())

# ---------- Запуск бота ----------

async def main():
    dp.message.register(send_welcome, Command("start"))
    dp.message.register(show_subscribers, Command("subscribers"))
    dp.message.register(show_subscriber_count, Command("count"))
    dp.callback_query.register(handle_callback)
    dp.message.register(handle_admin_files)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
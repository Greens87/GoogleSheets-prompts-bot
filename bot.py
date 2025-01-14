import os
import json
import logging
import pprint
import gspread
import telegram
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
import openai
from datetime import datetime
import random  # Для рандомного выбора --ar и --s

# ===== Временная отладка окружения =====
# Можно закомментировать после проверки
pprint.pprint(dict(os.environ))
# =======================================

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== Переменные окружения =====
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")          # Ваш Telegram-бот токен
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS")

# Проверка наличия необходимых переменных окружения
if TELEGRAM_TOKEN is None:
    raise ValueError("Переменная окружения BOT_TOKEN не установлена!")
if OPENAI_API_KEY is None:
    raise ValueError("Переменная окружения OPENAI_API_KEY не установлена!")
if GOOGLE_SHEETS_ID is None:
    raise ValueError("Переменная окружения GOOGLE_SHEETS_ID не установлена!")
if GOOGLE_CREDENTIALS_JSON is None:
    raise ValueError("Переменная окружения GOOGLE_CREDENTIALS не установлена!")

# Загрузка JSON-ключа сервисного аккаунта Google
try:
    GOOGLE_CREDENTIALS = json.loads(GOOGLE_CREDENTIALS_JSON)
except json.JSONDecodeError as e:
    raise ValueError(f"Ошибка при разборе GOOGLE_CREDENTIALS: {e}")

# Устанавливаем ключ OpenAI
openai.api_key = OPENAI_API_KEY

# Инициализация Google Sheets
try:
    gc = gspread.service_account_from_dict(GOOGLE_CREDENTIALS)
    sheet = gc.open_by_key(GOOGLE_SHEETS_ID)
except Exception as e:
    logger.error(f"Ошибка инициализации Google Sheets API: {e}")
    raise e

def get_today_sheet():
    """
    Получает или создаёт вкладку с сегодняшней датой в Google Sheets.
    В этой версии у нас будет только 1 столбец: Prompt (без даты).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        worksheet = sheet.worksheet(today)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=today, rows="1000", cols="1")
        worksheet.append_row(["Prompt"])  # Шапка одного столбца
    return worksheet

# Флаг работы бота
bot_active = True

# Устанавливаем модель
current_model = "gpt-4o-mini"  # ВАША требуемая модель

def start(update, context):
    update.message.reply_text(
        "Привет! Я бот для генерации промтов. Используй /generate <кол-во> или "
        "/generate <текст> для генерации промтов. В конце каждого промта будет --ar/--s/--no logo."
    )

def stop(update, context):
    global bot_active
    bot_active = False
    update.message.reply_text("Бот остановлен. Используй /resume для возобновления.")

def resume(update, context):
    global bot_active
    bot_active = True
    update.message.reply_text("Бот снова активен!")

def status(update, context):
    status_message = "Бот активен" if bot_active else "Бот на паузе"
    update.message.reply_text(status_message)

def set_model(update, context):
    """
    Если хотите разрешить переключение моделей командой /set_model <имя>,
    оставьте. Иначе можно убрать или закомментировать.
    """
    global current_model
    if context.args:
        new_model = context.args[0]
        current_model = new_model
        update.message.reply_text(f"Модель переключена на {current_model}")
    else:
        update.message.reply_text("Укажите модель, например: /set_model gpt-4o-mini")

def generate(update, context):
    """
    Обработчик команды /generate.
    - Если первый аргумент - число, используем его как count.
    - Иначе count=10, всё остальное — user_prompt.
    - Добавляем рандомный --ar (60%, 20%, 20%) и --s (25%, 25%, 50%) в КОНЦЕ + --no logo.
    - Пробуем заставить модель выдавать одну строку, минимум 45 слов, первое предложение <= 100 символов.
    """
    global bot_active
    if not bot_active:
        update.message.reply_text("Бот на паузе. Используй /resume для продолжения.")
        return

    args = context.args

    # Парсим аргументы
    if not args:
        count = 10
        user_prompt = ""
    else:
        if args[0].isdigit():
            count = int(args[0])
            user_prompt = " ".join(args[1:])
        else:
            count = 10
            user_prompt = " ".join(args)

    update.message.reply_text(f"Генерирую {count} промтов...")

    try:
        prompts_data = []

        for _ in range(count):
            # Генерим рандомные параметры --ar, --s:
            ar_choice = random.choices(
                ["--ar 3:2", "--ar 16:9", "--ar 2:3"], 
                weights=[0.6, 0.2, 0.2], 
                k=1
            )[0]
            s_choice = random.choices(
                ["--s 50", "--s 250", ""],
                weights=[0.25, 0.25, 0.5],
                k=1
            )[0]

            # Собираем "подсказку" для модели
            # Просим минимум 45 слов, первое предложение <= 100 символов, copy space, и т.д.
            # Уточняем, что в конце нужно обязательно вставить ar_choice, s_choice (если не пустое), и "--no logo"
            # И никаких лишних строк! Один промт = одна строка.
            system_message = (
                "Ты — помощник, который всегда отвечает одной строкой (один Midjourney-промт). "
                "Никаких дополнительных пояснений, без 'Midjourney Prompt:', без 'Here's your prompt'. "
                "Только сам текст. Соблюдай жёстко:\n"
                "1) Минимум 45 слов.\n"
                "2) Первое предложение не более 100 символов.\n"
                "3) Упомяни copy space или оставь возможность для текста.\n"
                "4) В конце всегда добавь: {ar}, {s}, --no logo.\n"
                "5) Никаких переводов на русский, всё на английском.\n"
                "6) Формат: ровно одна строка. Никаких переносов.\n"
                "7) Не перечисляй правила, просто выдай финальный prompt.\n"
            ).format(ar=ar_choice, s=s_choice if s_choice else "")

            # Собираем user-сообщение
            user_message = (
                f"{user_prompt}\n\n"
                "Remember all the rules above strictly. Output only the final single-line prompt."
            )

            response = openai.ChatCompletion.create(
                model=current_model,
                messages=[
                    {
                        "role": "system",
                        "content": system_message
                    },
                    {
                        "role": "user",
                        "content": user_message
                    }
                ],
                temperature=0.9
            )

            prompt_text = response.choices[0].message.content.strip()
            # Убираем возможные переносы строк, чтобы реально была одна строка
            prompt_text = prompt_text.replace("\n", " ").replace("\r", " ")
            # На всякий случай, если GPT вдруг добавит кавычки
            if prompt_text.startswith('"') and prompt_text.endswith('"'):
                prompt_text = prompt_text[1:-1].strip()

            # Кладём в список (один столбец)
            prompts_data.append([prompt_text])

        # Запись в таблицу
        worksheet = get_today_sheet()
        worksheet.append_rows(prompts_data)
        update.message.reply_text(f"Готово! {count} промтов записаны в Google Sheets.")

    except Exception as e:
        logger.error(f"Ошибка генерации: {e}")
        update.message.reply_text("Произошла ошибка. Попробуйте ещё раз.")

def main():
    if TELEGRAM_TOKEN is None:
        logger.error("BOT_TOKEN не установлен!")
        return

    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("stop", stop))
    dp.add_handler(CommandHandler("resume", resume))
    dp.add_handler(CommandHandler("status", status))
    dp.add_handler(CommandHandler("set_model", set_model, pass_args=True))
    dp.add_handler(CommandHandler("generate", generate, pass_args=True))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()

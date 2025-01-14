import os
import json
import logging
import pprint
import gspread
import telegram
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
import openai
from datetime import datetime

# ========== ВРЕМЕННАЯ ОТЛАДКА: вывод всех переменных окружения ==========
# Оставьте или закомментируйте после того, как всё заработает
pprint.pprint(dict(os.environ))
# ========================================================================

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== Переменные окружения ==========
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")  # Ваш Telegram-бот токен
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

# Инициализация Google Sheets API
try:
    gc = gspread.service_account_from_dict(GOOGLE_CREDENTIALS)
    sheet = gc.open_by_key(GOOGLE_SHEETS_ID)
except Exception as e:
    logger.error(f"Ошибка инициализации Google Sheets API: {e}")
    raise e

def get_today_sheet():
    """
    Получает или создаёт вкладку с сегодняшней датой в Google Sheets.
    В данной версии у нас будет только 1 столбец: Prompt.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        worksheet = sheet.worksheet(today)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=today, rows="1000", cols="1")
        # Добавим шапку в одну ячейку
        worksheet.append_row(["Prompt"])
    return worksheet

# Управление статусом
bot_active = True  # Флаг работы бота
current_model = "gpt-3.5-turbo"  # Или нужная вам модель

def start(update, context):
    update.message.reply_text(
        "Привет! Я бот для генерации промтов. Используй команду /generate <кол-во> "
        "или /generate <текст> для генерации промтов."
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
    global current_model
    if context.args:
        new_model = context.args[0]
        # При желании, ограничьте выбор доступных моделей
        current_model = new_model
        update.message.reply_text(f"Модель переключена на {current_model}")
    else:
        update.message.reply_text("Укажите модель, например: /set_model gpt-3.5-turbo")

def generate(update, context):
    """
    Обработчик команды /generate.
    Если первый аргумент - число, используем его как count.
    Если нет - берём count=10, а весь текст считаем user_prompt.
    """
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
        # Генерируем нужное количество промтов
        prompts_data = []
        for _ in range(count):
            # Здесь — вызов OpenAI, где мы в system-сообщении
            # даём "жёсткие" правила, чтобы вернуть только строку промта
            response = openai.ChatCompletion.create(
                model=current_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Ты – помощник, который отвечает СТРОГО одним строковым Midjourney-промтом. "
                            "Никаких вступлений, никаких 'Midjourney Prompt:', 'Here’s a prompt:' и т.п. "
                            "Только сам промт, без кавычек и пояснений. "
                            "Если у пользователя требования (например, 45 слов, copy space), соблюдай их, "
                            "но всё равно выводи только одну строку финального промта. "
                            "Никаких дополнений вроде 'Feel free to...' и т.д."
                        )
                    },
                    {
                        "role": "user",
                        "content": user_prompt
                    }
                ],
                temperature=0.9
            )
            prompt_text = response.choices[0].message.content.strip()
            # Добавляем в массив, чтобы потомまとめно записать
            prompts_data.append([prompt_text])

        # Пишем в Google Sheets
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

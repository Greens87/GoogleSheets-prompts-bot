import os
import json
import logging
import gspread
import telegram
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from openai import OpenAI
from datetime import datetime

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GOOGLE_CREDENTIALS = json.loads(os.getenv("GOOGLE_CREDENTIALS"))

# Инициализация OpenAI API
client = OpenAI(api_key=OPENAI_API_KEY)

# Инициализация Google Sheets API
gc = gspread.service_account_from_dict(GOOGLE_CREDENTIALS)
sheet = gc.open_by_key(GOOGLE_SHEETS_ID)

def get_today_sheet():
    """ Получает или создаёт вкладку с сегодняшней датой в Google Sheets."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        worksheet = sheet.worksheet(today)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=today, rows="1000", cols="2")
        worksheet.append_row(["Prompt", "Дата"])
    return worksheet

# Управление статусом
bot_active = True  # Флаг работы бота
current_model = "gpt-4o-mini"  # Модель по умолчанию

def start(update, context):
    update.message.reply_text("Привет! Я бот для генерации промтов. Используй команду /generate <кол-во> для генерации промтов.")

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
        if new_model in ["gpt-4o", "gpt-4o-mini"]:
            current_model = new_model
            update.message.reply_text(f"Модель переключена на {current_model}")
        else:
            update.message.reply_text("Доступные модели: gpt-4o, gpt-4o-mini")
    else:
        update.message.reply_text("Укажите модель (например, /set_model gpt-4o)")

def generate(update, context):
    if not bot_active:
        update.message.reply_text("Бот на паузе. Используй /resume для продолжения.")
        return
    
    try:
        count = int(context.args[0]) if context.args else 10  # По умолчанию 10 промтов
        update.message.reply_text(f"Генерирую {count} промтов...")

        prompts = []
        for _ in range(count):
            response = client.chat.completions.create(
                model=current_model,
                messages=[
                    {"role": "user", "content": "Generate a unique Midjourney prompt with copy space."}
                ]
            )
            prompt_text = response.choices[0].message.content.strip()
            prompts.append([prompt_text, datetime.now().strftime("%Y-%m-%d %H:%M:%S")])

        worksheet = get_today_sheet()
        worksheet.append_rows(prompts)
        update.message.reply_text(f"Готово! {count} промтов записаны в Google Sheets.")
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}")
        update.message.reply_text("Произошла ошибка. Попробуйте ещё раз.")

def main():
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

import os
import json
import logging
import pprint
import gspread
import telegram
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
import openai
from datetime import datetime
import random
import re
import time  # Для небольших пауз

# ======= Временная отладка окружения (можно закомментировать потом) =======
pprint.pprint(dict(os.environ))
# ==========================================================================

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== Читаем переменные окружения =====
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS")

# Проверка наличия необходимых переменных
if not TELEGRAM_TOKEN:
    raise ValueError("BOT_TOKEN не установлена!")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY не установлена!")
if not GOOGLE_SHEETS_ID:
    raise ValueError("GOOGLE_SHEETS_ID не установлена!")
if not GOOGLE_CREDENTIALS_JSON:
    raise ValueError("GOOGLE_CREDENTIALS не установлена!")

# Загрузка Google Credentials
try:
    GOOGLE_CREDENTIALS = json.loads(GOOGLE_CREDENTIALS_JSON)
except json.JSONDecodeError as e:
    raise ValueError(f"Ошибка при разборе GOOGLE_CREDENTIALS: {e}")

openai.api_key = OPENAI_API_KEY

# Инициализация Google Sheets
try:
    gc = gspread.service_account_from_dict(GOOGLE_CREDENTIALS)
    sheet = gc.open_by_key(GOOGLE_SHEETS_ID)
except Exception as e:
    logger.error(f"Ошибка инициализации Google Sheets API: {e}")
    raise e

def get_today_sheet():
    """Получает/создаёт вкладку с сегодняшней датой. Один столбец: Prompt."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        worksheet = sheet.worksheet(today)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=today, rows="1000", cols="1")
        worksheet.append_row(["Prompt"])
    return worksheet

bot_active = True
current_model = "gpt-4o-mini"  # Ваша требуемая модель

def start(update, context):
    update.message.reply_text(
        "Привет! Я бот для генерации промтов. Используй /generate <число> <текст>."
    )

def stop(update, context):
    global bot_active
    bot_active = False
    update.message.reply_text("Бот остановлен. /resume для возобновления.")

def resume(update, context):
    global bot_active
    bot_active = True
    update.message.reply_text("Бот снова активен!")

def status(update, context):
    st = "Бот активен" if bot_active else "Бот на паузе"
    update.message.reply_text(st)

def set_model(update, context):
    global current_model
    if context.args:
        new_model = context.args[0]
        current_model = new_model
        update.message.reply_text(f"Модель переключена на {current_model}")
    else:
        update.message.reply_text("Укажите модель: /set_model gpt-4o-mini")

def generate(update, context):
    """
    /generate <count> <prompt> 
    - Если первый арг != число, count=10, все аргументы -> user_prompt
    - Генерируем count раз (каждый раз ровно 1 промт).
    - Добавляем рандомные --ar и --s, потом --no logo, без запятых/точек рядом.
    - Минимум 45 слов, первое предложение <=100 символов. 
    - Если GPT умудряется склеить несколько промтов, делаем повторный запрос.
    """
    global bot_active
    if not bot_active:
        update.message.reply_text("Бот на паузе. /resume для продолжения.")
        return

    args = context.args
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
        count_generated = 0
        attempts = 0  # Счётчик, чтобы не зациклиться навечно

        while count_generated < count and attempts < 30:
            attempts += 1

            # Рандомим --ar
            ar_choice = random.choices(["--ar 3:2", "--ar 16:9", "--ar 2:3"], [0.6, 0.2, 0.2])[0]
            # Рандомим --s
            s_choice = random.choices(["--s 50", "--s 250", ""], [0.25, 0.25, 0.5])[0]

            # Готовим system/user подсказку
            system_message = (
                "You are an assistant that produces EXACTLY ONE single-line Midjourney prompt. "
                "No greetings, no disclaimers, no multiple prompts in one answer.\n"
                "Requirements:\n"
                "1) At least 45 words total.\n"
                "2) The first sentence must be <= 100 characters.\n"
                "3) Include the idea of copy space.\n"
                f"4) End the prompt with: {ar_choice}{(' ' + s_choice if s_choice else '')} --no logo\n"
                "5) Do NOT insert commas or periods right after --ar, --s, or --no logo.\n"
                "6) Output only one line, no matter what.\n"
                "7) Everything in English. No 'Midjourney Prompt:' text, no disclaimers.\n"
                "8) If user says '10 prompts', you still only give me ONE prompt in the answer.\n"
                "9) Absolutely do not provide multiple prompts in one message.\n"
            )

            user_message = (
                f"{user_prompt}\n\n"
                "IMPORTANT: Generate ONLY ONE prompt. Do not add multiple. Follow the system message strictly."
            )

            response = openai.ChatCompletion.create(
                model=current_model,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message}
                ],
                temperature=0.9
            )
            raw_text = response.choices[0].message.content.strip()

            # === Пост-обработка ===
            # 1) Убираем переносы строк
            raw_text = raw_text.replace("\n", " ").replace("\r", " ")
            # 2) Захватываем всё до первого "--no logo" (чтобы отсечь возможные повторные промты)
            match = re.search(r"(.*?--no logo)", raw_text, flags=re.IGNORECASE)
            if match:
                prompt_text = match.group(1).strip()
            else:
                prompt_text = raw_text  # На случай, если вдруг нет --no logo, сохраняем всё

            # 3) Удаляем лишние знаки препинания сразу ПОСЛЕ --ar/--s/--no logo
            prompt_text = re.sub(r'(\-\-ar\s*\d+:\d+)[\.,;:\!\?]+', r'\1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'(\-\-s\s*\d+)[\.,;:\!\?]+', r'\1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'(\-\-no\s+logo)[\.,;:\!\?]+', r'\1', prompt_text, flags=re.IGNORECASE)

            # 4) Удаляем знаки препинания ПЕРЕД параметрами
            prompt_text = re.sub(r'[,\.;:\!\?]+\s+(\-\-ar\s*\d+:\d+)', r' \1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'[,\.;:\!\?]+\s+(\-\-s\s*\d+)', r' \1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'[,\.;:\!\?]+\s+(\-\-no\s+logo)', r' \1', prompt_text, flags=re.IGNORECASE)

            # 5) Удаляем крайние кавычки, если вдруг
            if prompt_text.startswith('"') and prompt_text.endswith('"'):
                prompt_text = prompt_text[1:-1].strip()

            # === Проверяем, нет ли multiple prompts (несколько --ar или несколько --no logo) ===
            # Если GPT зачем-то сгенерировал 2+ раз '--ar ', считаем что это "несколько промтов"
            # и заново перезапрашиваем.
            if prompt_text.lower().count("--ar") > 1 or prompt_text.lower().count("--no logo") > 1:
                logger.warning("GPT вернул несколько промтов в одном ответе. Повтор запроса.")
                time.sleep(1.0)  # Пауза, чтобы модель "отдохнула"
                continue  # не записываем, а делаем retry

            # Если всё ок, считаем его полноценным
            prompts_data.append([prompt_text])
            count_generated += 1

        worksheet = get_today_sheet()
        worksheet.append_rows(prompts_data)

        update.message.reply_text(f"Готово! {count_generated} промтов записаны в Google Sheets.")

    except Exception as e:
        logger.error(f"Ошибка генерации: {e}")
        update.message.reply_text("Произошла ошибка. Попробуйте ещё раз.")

def main():
    if not TELEGRAM_TOKEN:
        logger.error("Не установлен BOT_TOKEN!")
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

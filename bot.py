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
import time

# ======= Временная отладка окружения (можно закомментировать) =======
pprint.pprint(dict(os.environ))
# ====================================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== Переменные окружения =====
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS")

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
    """Получает/создаёт вкладку (одна колонка: Prompt)."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        worksheet = sheet.worksheet(today)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=today, rows="1000", cols="1")
        worksheet.append_row(["Prompt"])
    return worksheet

bot_active = True
current_model = "gpt-4o-mini"  # Ваша модель

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

# ====================================================================
# ======================= ВАЖНАЯ ЛОГИКА ПРОВЕРОК ======================
# ====================================================================

def count_words_excluding_params(prompt_text: str) -> int:
    """
    Считает кол-во «обычных» слов, исключая те, что начинаются на '--'.
    """
    tokens = prompt_text.split()
    count = 0
    for t in tokens:
        candidate = t.strip(",.!?;:\r\n\"'()[]")
        if candidate.startswith("--"):
            continue
        if candidate:
            count += 1
    return count

def first_sentence_ok(prompt_text: str) -> bool:
    """
    Проверяет, что первое предложение заканчивается точкой
    и имеет не более 100 символов.
    """
    idx = prompt_text.find(".")
    if idx == -1:
        return False
    length_first_sentence = idx + 1
    return (length_first_sentence <= 100)

def check_prompt_valid(prompt_text: str) -> bool:
    """
    Возвращает True, если prompt_text удовлетворяет:
    1) Имеет не менее 28 слов (исключая --...).
    2) Первое предложение <= 100 символов и заканчивается точкой.
    """
    word_count = count_words_excluding_params(prompt_text)
    if word_count < 28:
        logger.warning(f"Слишком мало слов: {word_count}")
        return False
    
    if not first_sentence_ok(prompt_text):
        logger.warning("Первое предложение > 100 символов или нет точки.")
        return False

    return True

# ====================================================================
def generate(update, context):
    """
    /generate <count> <prompt> 
    1) Если первый арг != число, count=10, всё = user_prompt.
    2) Генерируем count раз (каждый раз ровно 1 промт).
    3) В system/user сообщениях просим 45 слов, 
       но реально принимаем от 28+ (проверка в check_prompt_valid).
    4) Первое предложение <= 100 символов.
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
        attempts = 0

        while count_generated < count and attempts < 100:
            attempts += 1

            # Рандом --ar
            ar_choice = random.choices(["--ar 3:2", "--ar 16:9", "--ar 2:3"], [0.6, 0.2, 0.2])[0]
            # Рандом --s
            s_choice = random.choices(["--s 50", "--s 250", ""], [0.25, 0.25, 0.5])[0]

            # ================== System / User ===================
            # ВНИМАНИЕ: Тут мы ПИШЕМ "минимум 45 слов",
            # но фактически в check_prompt_valid() мы проверяем >=28.
            system_message = (
                "You are an assistant that produces EXACTLY ONE single-line Midjourney prompt. "
                "No greetings, no disclaimers, no multiple prompts in one answer.\n"
                "Requirements:\n"
                "1) The prompt must be at least 45 words (excluding any words starting with --).\n"
                "2) The first sentence must be <= 100 characters and end with a period.\n"
                "3) Include the idea of copy space.\n"
                "4) If no specific style is given, make it a variety (people, no people, blurred backgrounds, objects on solid color, etc.).\n"
                f"5) End the prompt with: {ar_choice}{(' ' + s_choice if s_choice else '')} --no logo\n"
                "6) Do NOT insert commas or periods right after --ar, --s, or --no logo.\n"
                "7) Output only one line. No disclaimers, no 'Here is...' text.\n"
                "8) If user says '10 prompts', you still only give ONE prompt in the answer.\n"
                "9) Absolutely do not provide multiple prompts.\n"
                "10) The prompt must represent an image scenario, with copy space, referencing eco or other user context if provided.\n"
            )

            user_message = (
                f"{user_prompt}\n\n"
                "IMPORTANT: Generate ONLY ONE prompt. It must be unique. Follow the system rules strictly."
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

            # ========= Пост-обработка =========
            raw_text = raw_text.replace("\n", " ").replace("\r", " ")

            # Обрезаем всё после первого '--no logo'
            match = re.search(r"(.*?--no logo)", raw_text, flags=re.IGNORECASE)
            if match:
                prompt_text = match.group(1).strip()
            else:
                prompt_text = raw_text

            # Убираем пунктуацию возле --ar, --s, --no logo
            prompt_text = re.sub(r'(\-\-ar\s*\d+:\d+)[\.,;:\!\?]+', r'\1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'(\-\-s\s*\d+)[\.,;:\!\?]+', r'\1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'(\-\-no\s+logo)[\.,;:\!\?]+', r'\1', prompt_text, flags=re.IGNORECASE)

            prompt_text = re.sub(r'[,\.;:\!\?]+\s+(\-\-ar\s*\d+:\d+)', r' \1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'[,\.;:\!\?]+\s+(\-\-s\s*\d+)', r' \1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'[,\.;:\!\?]+\s+(\-\-no\s+logo)', r' \1', prompt_text, flags=re.IGNORECASE)

            if prompt_text.startswith('"') and prompt_text.endswith('"'):
                prompt_text = prompt_text[1:-1].strip()

            # Несколько --ar или --no logo => retry
            if prompt_text.lower().count("--ar") > 1 or prompt_text.lower().count("--no logo") > 1:
                logger.warning("GPT сгенерировал несколько промтов в одном ответе. Повтор запроса.")
                time.sleep(1.0)
                continue

            # Проверяем минимальные 28 слов и первое предложение <=100 символов
            if not check_prompt_valid(prompt_text):
                logger.warning("Промт не прошёл локальную валидацию. Повтор запроса.")
                time.sleep(1.0)
                continue

            # Всё ок
            prompts_data.append([prompt_text])
            count_generated += 1

        worksheet = get_today_sheet()
        worksheet.append_rows(prompts_data)

        update.message.reply_text(f"Готово! Сгенерировано {count_generated} промтов.")

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

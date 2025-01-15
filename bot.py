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

# ======= ВРЕМЕННАЯ ОТЛАДКА ОКРУЖЕНИЯ (можно закомментировать) =======
pprint.pprint(dict(os.environ))
# ====================================================================

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

if not TELEGRAM_TOKEN:
    raise ValueError("BOT_TOKEN не установлена!")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY не установлена!")
if not GOOGLE_SHEETS_ID:
    raise ValueError("GOOGLE_SHEETS_ID не установлена!")
if not GOOGLE_CREDENTIALS_JSON:
    raise ValueError("GOOGLE_CREDENTIALS не установлена!")

# Парсим GOOGLE_CREDENTIALS
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
    """Получает или создаёт вкладку (одна колонка: Prompt)."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        worksheet = sheet.worksheet(today)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=today, rows="1000", cols="1")
        worksheet.append_row(["Prompt"])
    return worksheet

bot_active = True
current_model = "gpt-4o-mini"  # Название используемой модели

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

def count_words_excluding_params(prompt_text: str) -> int:
    """
    Считает количество слов, исключая те, что начинаются на '--'.
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

def generate(update, context):
    """
    /generate <count> <prompt>
    
    - System-сообщение: Бот тщательно анализирует тему и создаёт промты для фотостоков,
      лаконичные, минималистичные, с copy space, минимум 45 слов (идеал).
    - Если итог <28 слов => делаем re-try (не принимаем).
    - Если 28..44 => принимаем c warning.
    - Если >=45 => отлично.
    - --ar: 70% (3:2), 20% (16:9), 10% (2:3)
    - 25% шанс добавить --style raw
    - Убираем любые двойные кавычки внутри, если пользователь сам не вводил их.
    """
    global bot_active
    if not bot_active:
        update.message.reply_text("Бот на паузе. /resume для продолжения.")
        return

    # Парсим аргументы
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

    # Проверяем, есть ли в user_prompt двойные кавычки
    user_has_quotes = '"' in user_prompt

    prompts_data = []
    count_generated = 0
    attempts = 0
    max_attempts = 50

    try:
        while count_generated < count and attempts < max_attempts:
            attempts += 1

            # 1. Случайный выбор --ar
            ar_choice = random.choices(
                ["--ar 3:2", "--ar 16:9", "--ar 2:3"],
                weights=[0.7, 0.2, 0.1],
                k=1
            )[0]

            # 2. 25% шанс для --style raw
            add_style_raw = (random.random() < 0.25)

            # 3. Случайный выбор --s
            s_choice = random.choices(
                ["--s 50", "--s 250", ""],
                weights=[0.25, 0.25, 0.5],
                k=1
            )[0]

            # System-message
            system_message = (
                "You are an assistant that meticulously analyzes the given theme, then composes a list "
                "of prompts specifically for photostocks. These prompts must be:\n"
                "- Laconic and minimalistic\n"
                "- Not overloaded with details\n"
                "- Must include 'copy space'\n"
                "- Must be at least 45 words (excluding words starting with --)\n"
                "- The first sentence ideally <=100 characters, ends with a period\n"
                "- End with parameters like --ar, --s, --no logo, possibly --style raw\n"
                "No greetings, disclaimers, or multiple prompts in one answer."
            )

            # User-message
            user_message = (
                f"{user_prompt}\n\n"
                "IMPORTANT: Generate ONLY ONE prompt. Follow the system rules strictly."
            )

            # Запрос к OpenAI
            response = openai.ChatCompletion.create(
                model=current_model,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message}
                ],
                temperature=0.9
            )
            raw_text = response.choices[0].message.content.strip()

            # Пост-обработка:
            raw_text = raw_text.replace("\n", " ").replace("\r", " ")

            # Если GPT не вставил --no logo
            if "--no logo" not in raw_text.lower():
                raw_text += " --no logo"

            # Если нужно --style raw
            if add_style_raw:
                match_style = re.search(r"(.*?)--no logo", raw_text, flags=re.IGNORECASE)
                if match_style:
                    before_part = match_style.group(1).strip()
                    raw_text = f"{before_part} --style raw --no logo"
                else:
                    raw_text += " --style raw"

            # Обрезаем всё после '--no logo'
            match_nl = re.search(r"(.*?--no logo)", raw_text, flags=re.IGNORECASE)
            if match_nl:
                prompt_text = match_nl.group(1).strip()
            else:
                prompt_text = raw_text

            # Убираем пунктуацию возле --ar, --s, --no logo, --style raw
            prompt_text = re.sub(r'(\-\-ar\s*\d+:\d+)[\.,;:\!\?]+', r'\1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'(\-\-s\s*\d+)[\.,;:\!\?]+', r'\1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'(\-\-no\s+logo)[\.,;:\!\?]+', r'\1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'(\-\-style\s+raw)[\.,;:\!\?]+', r'\1', prompt_text, flags=re.IGNORECASE)

            prompt_text = re.sub(r'[,\.;:\!\?]+\s+(\-\-ar\s*\d+:\d+)', r' \1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'[,\.;:\!\?]+\s+(\-\-s\s*\d+)', r' \1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'[,\.;:\!\?]+\s+(\-\-no\s+logo)', r' \1', prompt_text, flags=re.IGNORECASE)
            prompt_text = re.sub(r'[,\.;:\!\?]+\s+(\-\-style\s+raw)', r' \1', prompt_text, flags=re.IGNORECASE)

            # Удаляем крайние кавычки
            if prompt_text.startswith('"') and prompt_text.endswith('"'):
                prompt_text = prompt_text[1:-1].strip()

            # Если пользователь сам не вводил кавычки
            if not user_has_quotes:
                prompt_text = prompt_text.replace('"', '')

            # Подсчитываем кол-во слов (не включая --параметры)
            word_count = count_words_excluding_params(prompt_text)

            if word_count < 28:
                # <28 слов => re-try
                logger.warning(f"ОТКЛОНЁН промт: {word_count} слов (<28). Attempt={attempts}")
                time.sleep(1.0)
                continue
            elif word_count < 45:
                # 28..44 => принимаем, но warning
                logger.warning(f"Промт {word_count} слов, (меньше 45) => принимаем c предупреждением.")
            else:
                # >=45 => идеально
                pass

            # Промт принимаем
            prompts_data.append([prompt_text])
            count_generated += 1

        # Сохраняем результат
        worksheet = get_today_sheet()
        worksheet.append_rows(prompts_data)

        update.message.reply_text(
            f"Готово! Сгенерировано {count_generated} промтов (из {count} желаемых)."
        )

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

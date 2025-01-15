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

# ======= ВРЕМЕННАЯ ОТЛАДКА (можно закомментировать) =======
pprint.pprint(dict(os.environ))
# ==========================================================

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
current_model = "gpt-4o-mini"  # Модель

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


def count_words_excluding_params(text: str) -> int:
    """
    Считаем количество слов, исключая те, что начинаются на '--'.
    """
    tokens = text.split()
    count = 0
    for t in tokens:
        candidate = t.strip(",.!?;:\r\n\"'()[]")
        if candidate.startswith("--"):
            continue
        if candidate:
            count += 1
    return count


def generate_correct_params() -> str:
    """
    Генерирует правильную строку с параметрами:
    - --ar: 70% (3:2), 20% (16:9), 10% (2:3)
    - --s: 25% --s 50, 25% --s 250, 50% пусто
    - --style raw: 25% 
    - --no logo: всегда
    Итог: "<ar_choice> <s_choice_if_any> <style_raw_if_any> --no logo"
    """
    # 1. ar
    ar_choice = random.choices(
        ["--ar 3:2", "--ar 16:9", "--ar 2:3"],
        weights=[0.7, 0.2, 0.1],
        k=1
    )[0]

    # 2. s
    s_var = random.choices(
        ["--s 50", "--s 250", ""],
        weights=[0.25, 0.25, 0.5],
        k=1
    )[0]

    # 3. style raw (25% шанс)
    add_style_raw = (random.random() < 0.25)
    style_part = "--style raw" if add_style_raw else ""

    # Собираем финально
    parts = [ar_choice]
    if s_var != "":
        parts.append(s_var)
    if style_part:
        parts.append(style_part)
    parts.append("--no logo")

    return " ".join(parts)

def generate(update, context):
    """
    /generate <count> <prompt>
    - System-message: минимум 45 слов, "laconic, minimalistic, must include copy space..."
    - Итог: если <28 слов => re-try, 28..44 => warning, >=45 => ок
    - Убираем любые упоминания GPT о --ar, --s, --style, --no logo
    - Потом добавляем корректную строку параметров (из generate_correct_params).
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

    # Проверяем, есть ли кавычки в пользовательском запросе
    user_has_quotes = '"' in user_prompt

    prompts_data = []
    count_generated = 0
    attempts = 0
    max_attempts = 50

    try:
        while count_generated < count and attempts < max_attempts:
            attempts += 1

            # ==== System + user ====
            system_message = (
                "You are an assistant that meticulously analyzes the given theme, then composes a list "
                "of prompts specifically for photostocks. These prompts must be:\n"
                "- Laconic and minimalistic\n"
                "- Not overloaded with details\n"
                "- Must include 'copy space'\n"
                "- Must be at least 45 words (excluding words starting with --)\n"
                "- The first sentence ideally <=100 characters, ends with a period\n"
                "No greetings, disclaimers, or multiple prompts in one answer."
            )

            user_message = (
                f"{user_prompt}\n\n"
                "IMPORTANT: Generate ONLY ONE prompt. Follow the system rules strictly."
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

            # 1) Убираем переносы
            raw_text = raw_text.replace("\n", " ").replace("\r", " ")

            # 2) Полностью вычищаем любые упоминания GPT о --ar, --s, --style, --no logo
            #    Мы потом сами добавим корректные параметры в конце.
            raw_text = re.sub(r'--ar\s*\S+', '', raw_text, flags=re.IGNORECASE)
            raw_text = re.sub(r'--s\s*\S+', '', raw_text, flags=re.IGNORECASE)
            raw_text = re.sub(r'--style\s*\S+', '', raw_text, flags=re.IGNORECASE)
            raw_text = re.sub(r'--no\s+logo', '', raw_text, flags=re.IGNORECASE)

            # 3) Удаляем крайние кавычки, если GPT всё обернул
            if raw_text.startswith('"') and raw_text.endswith('"'):
                raw_text = raw_text[1:-1].strip()

            # 4) Если пользователь не вводил кавычки
            if not user_has_quotes:
                raw_text = raw_text.replace('"', '')

            # 5) Убираем двойные/тройные пробелы
            raw_text = re.sub(r'\s+', ' ', raw_text).strip()

            # 6) Теперь добавляем корректную строку параметров
            correct_params = generate_correct_params()
            prompt_text = f"{raw_text} {correct_params}".strip()

            # 7) Считаем слова
            word_count = count_words_excluding_params(prompt_text)
            if word_count < 28:
                logger.warning(f"ОТКЛОНЁН промт: {word_count} слов (<28). Attempt={attempts}")
                time.sleep(1.0)
                continue
            elif word_count < 45:
                logger.warning(f"Промт {word_count} слов (28..44) => принимаем с предупреждением.")
            else:
                # >=45 => отлично
                pass

            # 8) Добавляем в списки
            prompts_data.append([prompt_text])
            count_generated += 1

        # Записываем в Google Sheets
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

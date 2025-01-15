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
    
    Логика:
    - В system-сообщении указываем минимум 45 слов (как 'идеал').
    - Если GPT вернул <28 слов => делаем re-try (не принимаем).
    - Если 28..44 => принимаем с предупреждением.
    - Если >=45 => отлично.
    
    Дополнительно:
    - --ar: (70% 3:2, 20% 16:9, 10% 2:3)
    - 25% шанс добавить --style raw
    - Убираем лишние двойные кавычки, если пользователь сам не ввёл.
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

    # Проверяем, использовал ли пользователь кавычки
    user_has_quotes = '"' in user_prompt

    prompts_data = []
    count_generated = 0
    attempts = 0
    max_attempts = 50  # Чтоб не застрять бесконечно

    try:
        while count_generated < count and attempts < max_attempts:
            attempts += 1

            # Случайный выбор --ar
            ar_choice = random.choices(
                ["--ar 3:2", "--ar 16:9", "--ar 2:3"],
                weights=[0.7, 0.2, 0.1],
                k=1
            )[0]

            # 25% шанс для --style raw
            add_style_raw = (random.random() < 0.25)

            # Случайный выбор --s
            s_choice = random.choices(
                ["--s 50", "--s 250", ""],
                weights=[0.25, 0.25, 0.5],
                k=1
            )[0]

            system_message = (
                "You are an assistant that produces EXACTLY ONE single-line Midjourney prompt. "
                "No greetings, no disclaimers, no multiple prompts in one answer.\n"
                "Requirements:\n"
                "1) The prompt must be at least 45 words (excluding any words starting with --).\n"
                "2) The first sentence is ideally <= 100 characters and ends with a period.\n"
                "3) Include copy space.\n"
                "4) If no specific style is given, vary: people, no people, blurred backgrounds, objects on solid color, etc.\n"
                f"5) End with: {ar_choice}{(' ' + s_choice if s_choice else '')} --no logo (and maybe --style raw).\n"
                "6) Do NOT insert commas or periods right after --ar, --s, or --no logo.\n"
                "7) Output only one line.\n"
                "8) Absolutely do not provide multiple prompts.\n"
                "9) Never enclose the entire prompt (or parts) in double quotes unless user explicitly used them.\n"
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

            # Убираем переносы
            raw_text = raw_text.replace("\n", " ").replace("\r", " ")

            # Если GPT не добавил --no logo, допишем
            if "--no logo" not in raw_text.lower():
                raw_text += " --no logo"

            # Вставим --style raw (25% случай)
            if add_style_raw:
                match_style = re.search(r"(.*?)--no logo", raw_text, flags=re.IGNORECASE)
                if match_style:
                    before_part = match_style.group(1).strip()
                    raw_text = f"{before_part} --style raw --no logo"
                else:
                    raw_text += " --style raw"

            # Обрезаем всё после первого '--no logo'
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

            # Удаляем крайние кавычки, если GPT всё обернул
            if prompt_text.startswith('"') and prompt_text.endswith('"'):
                prompt_text = prompt_text[1:-1].strip()

            # Удаляем любые " внутри промта, если пользователь сам не вводил
            if not user_has_quotes:
                prompt_text = prompt_text.replace('"', '')

            # Считаем слова (исключая --параметры)
            word_count = count_words_excluding_params(prompt_text)

            if word_count < 28:
                # Меньше 28 => re-try (не добавляем к prompts_data, не count_generated++)
                logger.warning(f"ПРОМТ ОТКЛОНЁН: только {word_count} слов (<28). Попытка #{attempts}")
                # Спим и продолжаем while-цикл
                time.sleep(1.0)
                continue
            elif word_count < 45:
                # От 28 до 44 => принимаем (warning)
                logger.warning(f"Промт {word_count} слов, меньше 45, но >=28 — принимаем с предупреждением.")
            else:
                # >= 45 => всё отлично, как просили
                pass

            # Если дошли сюда — принимаем
            prompts_data.append([prompt_text])
            count_generated += 1

        # Записываем результат
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

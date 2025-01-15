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

# ---------------- Вспомогательная функция ----------------

def chunk_text(text, chunk_size=1200):
    """
    Разбивает большой текст на куски ~по 1200 символов,
    чтобы каждый из них отправить отдельным user-сообщением.
    """
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end
    return chunks

# ---------------------------------------------------------
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

def generate(update, context):
    """
    /generate <count> <prompt>
    Мы разбиваем user_prompt на несколько кусков, каждый отправляем отдельным user-сообщением,
    чтобы GPT "гарантированно" прочёл всё. В System-сообщении указываем "прочитай всё до конца".
    """
    global bot_active
    if not bot_active:
        update.message.reply_text("Бот на паузе. /resume для продолжения.")
        return

    args = context.args
    if not args:
        count = 10
        user_prompt_raw = ""
    else:
        if args[0].isdigit():
            count = int(args[0])
            user_prompt_raw = " ".join(args[1:])
        else:
            count = 10
            user_prompt_raw = " ".join(args)

    update.message.reply_text(f"Генерирую {count} промтов...")

    try:
        # 1. Разбиваем user_prompt_raw на куски ~1200 символов
        # (Можете менять chunk_size при желании)
        prompt_chunks = chunk_text(user_prompt_raw, chunk_size=1200)

        # 2. Подготовим массив messages
        # Системное сообщение: указываем, что нужно внимательно прочитать ВСЕ user-сообщения
        system_message = (
            "You are an assistant that MUST READ ALL user instructions carefully and incorporate all details. "
            "Do not ignore any part of the user's text, even if it is long. "
            "After you have read everything, you will generate EXACTLY ONE single-line Midjourney prompt per request, "
            "with the following rules:\n"
            "1) The prompt must be at least ~45 words (excluding --params).\n"
            "2) The first sentence is ideally <=100 characters and ends with a period.\n"
            "3) Include minimalistic, copy space, clean image, etc., or any other user hints.\n"
            "4) End with random --ar and --s (or empty) plus --no logo.\n"
            "5) Output only one line. No disclaimers.\n"
            "6) Absolutely do not skip the latter part of user's instructions.\n"
        )

        # Соберём messages: сначала одно system-сообщение
        messages = [
            {"role": "system", "content": system_message}
        ]

        # Затем добавляем все куски user_prompt как отдельные user-сообщения
        # Это имитирует длинный контекст, "вшитый" в диалог.
        for i, chunk in enumerate(prompt_chunks):
            messages.append({"role": "user", "content": f"(PART {i+1}/{len(prompt_chunks)})\n{chunk}"})

        # Наконец, добавляем "заключительную" user-инструкцию:
        # "Now generate a single prompt..."
        # Можете изменить wording по вкусу, но важно попросить итоговый ответ.
        messages.append({
            "role": "user",
            "content": (
                f"Now, based on ALL the instructions above (parts 1..{len(prompt_chunks)}), "
                f"generate exactly ONE single-line prompt. We need {count} total prompts, so we'll call you repeatedly. "
                "For this single call, produce exactly one prompt. Follow the rules strictly."
            )
        })

        prompts_data = []

        for _ in range(count):
            # Рандом --ar
            ar_choice = random.choices(["--ar 3:2", "--ar 16:9", "--ar 2:3"], [0.6, 0.2, 0.2])[0]
            # Рандом --s
            s_choice = random.choices(["--s 50", "--s 250", ""], [0.25, 0.25, 0.5])[0]

            # Мы можем модифицировать "system_message" на лету, если надо.
            # Но сейчас для упрощения — тот же messages, а "конкретный" ar/s подставим через "assistant"?
            # Проще всего: добавим ещё одно user-сообщение "Вставь, пожалуйста, в конце: {ar_choice} {s_choice} --no logo"

            # Делаем копию messages, чтобы не портить оригинал
            local_messages = list(messages)

            # Добавляем ещё одно user-сообщение с конкретными параметрами
            local_messages.append({
                "role": "user",
                "content": (
                    f"For THIS prompt, end with: {ar_choice}"
                    f"{(' ' + s_choice if s_choice else '')} --no logo. "
                    "Remember: exactly one single-line prompt."
                )
            })

            response = openai.ChatCompletion.create(
                model=current_model,
                messages=local_messages,
                temperature=0.9
            )

            raw_text = response.choices[0].message.content.strip()

            # Пост-обработка
            raw_text = raw_text.replace("\n", " ").replace("\r", " ")
            match = re.search(r"(.*?--no logo)", raw_text, flags=re.IGNORECASE)
            if match:
                prompt_text = match.group(1).strip()
            else:
                prompt_text = raw_text

            # Убираем пунктуацию возле --ar, --s, --no logo
            prompt_text = re.sub(
                r'(\-\-ar\s*\d+:\d+)[\.,;:\!\?]+',
                r'\1',
                prompt_text,
                flags=re.IGNORECASE
            )
            prompt_text = re.sub(
                r'(\-\-s\s*\d+)[\.,;:\!\?]+',
                r'\1',
                prompt_text,
                flags=re.IGNORECASE
            )
            prompt_text = re.sub(
                r'(\-\-no\s+logo)[\.,;:\!\?]+',
                r'\1',
                prompt_text,
                flags=re.IGNORECASE
            )

            prompt_text = re.sub(
                r'[,\.;:\!\?]+\s+(\-\-ar\s*\d+:\d+)',
                r' \1',
                prompt_text,
                flags=re.IGNORECASE
            )
            prompt_text = re.sub(
                r'[,\.;:\!\?]+\s+(\-\-s\s*\d+)',
                r' \1',
                prompt_text,
                flags=re.IGNORECASE
            )
            prompt_text = re.sub(
                r'[,\.;:\!\?]+\s+(\-\-no\s+logo)',
                r' \1',
                prompt_text,
                flags=re.IGNORECASE
            )

            if prompt_text.startswith('"') and prompt_text.endswith('"'):
                prompt_text = prompt_text[1:-1].strip()

            if prompt_text.lower().count("--ar") > 1 or prompt_text.lower().count("--no logo") > 1:
                logger.warning("GPT сгенерировал несколько промтов в одном ответе, но принимаем без наказания.")

            word_count = count_words_excluding_params(prompt_text)
            if word_count < 28:
                logger.warning(f"Промт короткий ({word_count} слов), но принимаем без наказания.")

            prompts_data.append([prompt_text])

        worksheet = get_today_sheet()
        worksheet.append_rows(prompts_data)

        update.message.reply_text(f"Готово! Сгенерировано {count} промтов.")

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

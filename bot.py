def generate(update, context):
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
                "You are an assistant that meticulously analyzes the given theme, then composes a single-line prompt "
                "aimed at photostocks. These prompts must be:\n"
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
                # При желании повысьте temperature
                temperature=1.0
            )
            raw_text = response.choices[0].message.content.strip()

            # 1) Убираем переносы
            raw_text = raw_text.replace("\n", " ").replace("\r", " ")

            # 2) Удаляем все случайные "--что_угодно" (кроме наших официальных команд),
            #    например "--Capture--" или "--Include--"
            #    Логика такая: ищем "--" + любые не-пробельные символы, 
            #    если дальше НЕ идёт (ar|s|style|no).
            #    Можно сделать более точный паттерн, но это базовый вариант:
            raw_text = re.sub(
                r'--(?!ar|s|style|no)(\S*)',  # Ищем: -- + что угодно, если дальше не ar|s|style|no
                '',  # Удаляем
                raw_text,
                flags=re.IGNORECASE
            )

            # 3) Удаляем и "декоративные" окончания "--" (если GPT писало что-то вроде "Capture--")
            #    Если вдруг осталось одиночное "--"
            #    Эту часть можно добавить, если GPT вставит "word--" в конце
            # raw_text = re.sub(r'\b(\S*?)--\b', r'\1', raw_text)  # опционально

            # 4) Удаляем любые "официальные" параметры GPT мог написать (с не теми аргументами)
            raw_text = re.sub(r'--ar\s*\S+', '', raw_text, flags=re.IGNORECASE)
            raw_text = re.sub(r'--s\s*\S+', '', raw_text, flags=re.IGNORECASE)
            raw_text = re.sub(r'--style\s*\S+', '', raw_text, flags=re.IGNORECASE)
            raw_text = re.sub(r'--no\s+logo', '', raw_text, flags=re.IGNORECASE)

            # 5) Удаляем крайние кавычки, если GPT всё обернул
            if raw_text.startswith('"') and raw_text.endswith('"'):
                raw_text = raw_text[1:-1].strip()

            # 6) Если пользователь не вводил кавычки
            if not user_has_quotes:
                raw_text = raw_text.replace('"', '')

            # 7) Убираем лишние пробелы
            raw_text = re.sub(r'\s+', ' ', raw_text).strip()

            # 8) Добавляем корректный набор параметров
            correct_params = generate_correct_params()
            prompt_text = f"{raw_text} {correct_params}".strip()

            # 9) Считаем слова
            word_count = count_words_excluding_params(prompt_text)
            if word_count < 28:
                logger.warning(f"ОТКЛОНЁН промт: {word_count} слов (<28). Attempt={attempts}")
                time.sleep(1.0)
                continue
            elif word_count < 45:
                logger.warning(f"Промт {word_count} слов (28..44) => принимаем с предупреждением.")
            else:
                pass

            prompts_data.append([prompt_text])
            count_generated += 1

        worksheet = get_today_sheet()
        worksheet.append_rows(prompts_data)

        update.message.reply_text(
            f"Готово! Сгенерировано {count_generated} промтов (из {count} желаемых)."
        )

    except Exception as e:
        logger.error(f"Ошибка генерации: {e}")
        update.message.reply_text("Произошла ошибка. Попробуйте ещё раз.")

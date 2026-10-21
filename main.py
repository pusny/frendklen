async def handle_profile_edit(update, ctx, u, text):
    msg = update.effective_message
    uid = u["user_id"]
    limit = vip_char_limit(u["vip"])
    if len(text) > limit:
        over = len(text) - limit
        ctx.user_data["edit_text_mode"] = True
        return await msg.reply_text(
            f"⚠️ Анкета превышает лимит.\nЛимит: <b>{limit}</b>.\nСейчас: <b>{len(text)}</b>.\n"
            f"Сократите на <b>{over}</b> символ(ов).",
            parse_mode=ParseMode.HTML,
        )
    data = parse_profile(text)
    if not data:
        ctx.user_data["edit_text_mode"] = True
        return await msg.reply_text(
            "❌ Анкета заполнена не по форме.\n\n"
            f"<code>{PROFILE_TEMPLATE}</code>\n\n"
            "Заполните заново.",
            parse_mode=ParseMode.HTML,
        )
    ctx.user_data.pop("edit_text_mode", None)

    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute(
            "UPDATE profiles SET name=?, age=?, city=?, diagnosis=?, about=? WHERE user_id=?",
            (data["name"], data["age"], data["city"], data["diagnosis"], data["about"], uid),
        )
        conn.commit()

    await msg.reply_text(
        "✅ Анкета обновлена.",
        reply_markup=kb_main(uid == ADMIN_ID),
    )

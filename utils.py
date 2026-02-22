from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# On déplace replace_view ici
async def replace_view(q, text, reply_markup=None, parse_mode="Markdown"):
    try:
        await q.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            await q.message.delete()
        except:
            pass
        await q.message.chat.send_message(text, reply_markup=reply_markup, parse_mode=parse_mode)

def kb_back_to_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]])

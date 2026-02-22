import sqlite3
import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Chemin vers la DB (récupéré depuis l'emplacement du fichier)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")

async def replace_view(q, text, reply_markup=None, parse_mode="Markdown"):
    """
    Met à jour le message actuel ou le renvoie si la modification est impossible.
    """
    try:
        await q.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            await q.message.delete()
        except:
            pass
        await q.message.chat.send_message(text, reply_markup=reply_markup, parse_mode=parse_mode)

def kb_back_to_menu():
    """
    Bouton de retour standard vers le menu d'accueil.
    """
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]])

def get_user_lang(user_id):
    """
    Récupère la langue de l'utilisateur depuis la base de données.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT lang FROM users WHERE user_id = ?", (str(user_id),))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] else "fr"
    except Exception:
        return "fr"

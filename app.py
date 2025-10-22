import re
import sqlite3
import shop_helpers
from telegram.ext import CallbackQueryHandler, CommandHandler
import os
import sys
import io
import csv
import atexit
import asyncio
import logging
logger = logging.getLogger("SYSTEM")
import threading
import sqlite3
import subprocess
from datetime import datetime
import pytz
from dotenv import load_dotenv
from flask import Flask, request, Response
from signalwire.rest import Client as SignalWireClient
from twilio.twiml.voice_response import VoiceResponse

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ConversationHandler, ContextTypes, CallbackQueryHandler
)
# === Helpers pour éditer la bulle + bouton Retour ===
def kb_back_to_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]])

async def replace_view(q, text, reply_markup=None, parse_mode=None):
    try:
        await q.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        await q.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

def kb_back_cancel():
    # Pour les flows (ex: Vérifier mon permis) → retourne au menu et annule
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]])

# === [CART: real implementation] ===
async def view_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import sqlite3, os
    try:
        user_id = str(update.effective_user.id)
    except Exception:
        return

    db_path = os.environ.get("DB_NAME", "/home/johnmsaaq/bot-nomen/database.db")
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("""
            SELECT p.title, p.price, COALESCE(c.qty,1) as qty
            FROM cart_items c
            JOIN products p ON p.id = c.product_id
            WHERE c.user_id = ?
        """, (user_id,))
        rows = cur.fetchall()
    finally:
        try: con.close()
        except: pass

    if not rows:
        text = "🛒 Votre panier est vide."
    else:
        total = 0.0
        lines = ["🛒 Contenu de votre panier:"]
        for title, price, qty in rows:
            price = float(price or 0.0)
            qty   = int(qty or 1)
            sub   = price * qty
            total += sub
            lines.append(f"• {title} ×{qty} — {sub:.2f} $")
        lines.append(f"\n💰 Total: {total:.2f} $")
        text = "\n".join(lines)

    if getattr(update, "message", None):
        await update.message.reply_text(text)
    elif getattr(update, "callback_query", None):
        try: await update.callback_query.answer()
        except: pass
        await update.callback_query.message.reply_text(text)
# === [/CART] ===

# ========================== ENV & LOCK ==========================

load_dotenv()

LOCK = "/tmp/bot-nomen.pid"

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

if os.path.exists(LOCK):
    try:
        old = int(open(LOCK).read().strip())
    except Exception:
        old = None
    if old and _pid_alive(old):
        print(f"[LOCK] bot-nomen déjà lancé (PID {old}).", flush=True)
        sys.exit(0)
    else:
        try:
            os.remove(LOCK)
        except FileNotFoundError:
            pass

open(LOCK, "w").write(str(os.getpid()))
atexit.register(lambda: os.path.exists(LOCK) and os.remove(LOCK))

# ========================== LOGGING ==========================
class SafeFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, 'user_id'):
            record.user_id = "SYSTEM"
        return super().format(record)

my_fmt = '%(asctime)s [%(levelname)s] [User:%(user_id)s] %(message)s'
handler = logging.FileHandler("Nomen Nescio.log")
handler.setFormatter(SafeFormatter(my_fmt))
stream = logging.StreamHandler()
stream.setFormatter(SafeFormatter(my_fmt))
logging.basicConfig(level=logging.INFO, handlers=[handler, stream])

def log(msg, user_id="SYSTEM", level="info"):
    try:
        getattr(logging, level)(msg, extra={"user_id": user_id})
    except Exception as e:
        # fallback sûr même si encodage casse
        try:
            print(f"[LOG ERROR] {e} - {msg}")
        except Exception:
            print("[LOG ERROR] (encoding issue while printing message)")

# ========================== CONFIG ==========================
SW_PROJECT_ID = os.environ.get("SW_PROJECT_ID", "e6f483f8-a47f-48d3-b7a7-34fead35200b")
SW_TOKEN = os.environ.get("SW_TOKEN", "PTf0f2493cbd50329d89c76c5d88e6b80faa3c761d2bf6d23f")
SW_SPACE = os.environ.get("SIGNALWIRE_SPACE_URL", "john-m-shop.signalwire.com")
os.environ["SIGNALWIRE_SPACE_URL"] = SW_SPACE

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
DESTINATION_NUMBER = os.environ.get("DESTINATION_NUMBER", "+18003617620")
SIGNALWIRE_NUMBER = os.environ.get("SIGNALWIRE_NUMBER", "+12029928463")
SERVER_URL = os.environ.get("SERVER_URL", "http://37.228.129.82:5001")
ADMIN_ID = os.environ.get("ADMIN_ID", "7573645008")

DB_NAME = os.environ.get("DB_NAME", "/home/johnmsaaq/bot-nomen/database.db")

client = SignalWireClient(SW_PROJECT_ID, SW_TOKEN)
app = Flask(__name__)

# ========================== CONSTANTES ==========================
# Étapes Conversation (1 permis historique)
ASK_PRENOM, ASK_NOM, ASK_DATE, CONFIRM_VERIF = range(4)

# Flow multi-permis
ASK_QTY, ASK_MODE, MANUAL_PRENOM, MANUAL_NOM, MANUAL_DATE, CSV_WAIT, BULK_CONFIRM = range(100, 107)

# Admin
ADMIN_AWAIT_AMOUNT = 200
ADMIN_WAIT_PRODUCT_TEXT = 201
ADMIN_WAIT_CSV = 202

# Paliers
FORFAITS = {
    "bronze":   {"min": 0,    "price": 10.0, "label": "🟫 Bronze"},
    "silver":   {"min": 250,  "price": 8.0,  "label": "⬜️ Silver"},
    "gold":     {"min": 500,  "price": 6.0,  "label": "🟨 Gold"},
    "platinum": {"min": 1000, "price": 4.0,  "label": "⬛️ Platinum"},
}

# ========================== GLOBALS ==========================
bot_loop = None
bot_messages = {}
user_sessions = {}
active_calls = {}
pending_payments = {}
user_validation_status = {}
batch_runs = {}
CATALOG_MSGS = {}  # messages envoyés pour le catalogue (pour les nettoyer)

# ========================== MESSAGES ==========================
MESSAGES = {
    'welcome': {
        'fr': (
            "👋 Bienvenue sur Nomen Nescio !\n\n"
            "Votre espace central, réunissant tout ce qu’il vous faut.\n\n"
            "🆔 : {telegram_id}\n"
            "💰 Solde : {balance:.2f} CAD\n"
            "{statut_label}\n"
            "\n\n🔑 Appuyez simplement sur la touche de votre choix."
        ),
        'en': (
            "👋 Welcome to Nomen Nescio!\n\n"
            "Your central hub for everything you need.\n\n"
            "🆔 : {telegram_id}\n"
            "💰 Balance: {balance:.2f} CAD\n"
            "{statut_label}\n"
            "\n\n🔑 Simply press the key of your choice."
        ),
    },
    'choose_lang': {
        'fr': "🌐 Choisissez votre langue / Select your language",
        'en': "🌐 Please select your language / Choisissez votre langue"
    },
    'balance': {
        'fr': "Votre solde actuel est : {balance:.2f} $ CAD.",
        'en': "Your current balance is: {balance:.2f} CAD."
    },
    'enter_bulk_qty': {
        'fr': "Combien de permis voulez-vous valider ? (ex: 1, 3, 6)",
        'en': "How many licenses do you want to validate? (e.g., 1, 3, 6)"
    },
    'bulk_choice': {
        'fr': "Voulez-vous saisir manuellement maintenant, ou envoyer un fichier CSV ?",
        'en': "Would you like to enter manually now, or upload a CSV file?"
    },
    'enter_firstname': {'fr': '✍️ Entre le *prénom* :', 'en': '✍️ Enter the *first name*:'},
    'enter_lastname': {'fr': '✍️ Entre le *nom de famille* :', 'en': '✍️ Enter the *last name*:'},
    'enter_birth': {'fr': '🗓 Entre la *date de naissance* (JJ-MM-AAAA) :', 'en': '🗓 Enter *birth date* (DD-MM-YYYY):'},
    'solde_insuffisant': {'fr': '⚠️ *Solde insuffisant.*\nSolde: {balance:.2f}$\nMontant requis: {prix:.2f}$\nStatut: {statut}\n\n💳 Appuie sur *Recharger* pour continuer.', 'en': '⚠️ *Insufficient balance.*\nBalance: {balance:.2f}$\nRequired: {prix:.2f}$\nTier: {statut}\n\n💳 Tap *Top up* to continue.'},
    'show_permis': {'fr': '🆔 *Permis proposé:* `{permis}`\nTarif: {prix:.2f}$ ({statut})\n\nConfirmer ? *(Oui / Non)*', 'en': '🆔 *Proposed license:* `{permis}`\nPrice: {prix:.2f}$ ({statut})\n\nConfirm? *(Yes / No)*'},
    'decrytage_en_cours': {'fr': '⏳ Décryptage en cours…', 'en': '⏳ Decryption in progress…'},
    'validation_ok': {'fr': '✅ *Valide* pour {fullname}\nPermis: `{permis}`', 'en': '✅ *Valid* for {fullname}\nLicense: `{permis}`'},
    'aucun_permis': {'fr': '❌ *Aucun permis trouvé* pour {fullname}.', 'en': '❌ *No license found* for {fullname}.'},
    'lang_updated': {'fr': '✅ Langue mise à jour.', 'en': '✅ Language updated.'}
}



def msg(user_id, key, **kwargs):
    """Récupère un message localisé sans jamais renvoyer une chaîne vide.
    Fallbacks: FR/EN -> _DEFAULT_MESSAGES -> [clé]
    """
    try:
        lang = get_user_lang(str(user_id))
    except Exception:
        lang = "fr"

    d1 = MESSAGES.get(key) or {}
    d2 = globals().get("_DEFAULT_MESSAGES", {}).get(key, {})

    template = (
        d1.get(lang) or d1.get("en") or
        d2.get(lang) or d2.get("en") or
        f"[{key}]"
    )
    try:
        return template.format(**kwargs)
    except Exception:
        # Si des placeholders manquent, renvoie tel quel
        return str(template)
# ========================== DB & USERS ==========================
def init_db():
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id TEXT PRIMARY KEY,
            balance REAL DEFAULT 0,
            lang TEXT DEFAULT 'fr',
            total_recharge REAL DEFAULT 0,
            forfait TEXT DEFAULT 'bronze'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            order_id TEXT PRIMARY KEY,
            telegram_id TEXT,
            amount REAL,
            status TEXT DEFAULT 'pending'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            title TEXT,
            price REAL,
            currency TEXT DEFAULT 'CAD',
            stock INTEGER DEFAULT 0,
            tier TEXT,
            is_active INTEGER DEFAULT 1
        )
    """)
    con.commit()
    con.close()
    log("DB initialized", "SYSTEM")

def user_exists(telegram_id: str) -> bool:
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM users WHERE telegram_id=?", (telegram_id,))
    exists = cur.fetchone() is not None
    con.close()
    return exists

def get_user_lang(telegram_id: str) -> str:
    try:
        con = sqlite3.connect(DB_NAME)
        cur = con.cursor()
        cur.execute("SELECT lang FROM users WHERE telegram_id=?", (telegram_id,))
        row = cur.fetchone()
        con.close()
        return row[0] if row else "fr"
    except Exception:
        return "fr"

def set_user_lang(telegram_id: str, lang: str):
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("UPDATE users SET lang=? WHERE telegram_id=?", (lang, telegram_id))
    if cur.rowcount == 0:
        cur.execute("INSERT INTO users (telegram_id, lang) VALUES (?,?)", (telegram_id, lang))
    con.commit()
    con.close()
    log(f"Lang set: {lang}", telegram_id)

def get_user_statut(telegram_id: str) -> str:
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT forfait FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else "bronze"

def set_user_statut(telegram_id: str, statut: str):
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("UPDATE users SET forfait=? WHERE telegram_id=?", (statut, telegram_id))
    if cur.rowcount == 0:
        cur.execute("INSERT INTO users (telegram_id, forfait) VALUES (?,?)", (telegram_id, statut))
    con.commit()
    con.close()
    log(f"Set statut {statut}", telegram_id)

def upgrade_user_statut_auto(telegram_id: str) -> str:
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT total_recharge FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    total = row[0] if row else 0.0
    if total >= FORFAITS["platinum"]["min"]:
        statut = "platinum"
    elif total >= FORFAITS["gold"]["min"]:
        statut = "gold"
    elif total >= FORFAITS["silver"]["min"]:
        statut = "silver"
    else:
        statut = "bronze"
    cur.execute("UPDATE users SET forfait=? WHERE telegram_id=?", (statut, telegram_id))
    if cur.rowcount == 0:
        cur.execute("INSERT INTO users (telegram_id, forfait, total_recharge) VALUES (?,?,?)",
                    (telegram_id, statut, total))
    con.commit()
    con.close()
    log(f"Upgrade statut auto: {statut}", telegram_id)
    return statut

def get_permit_price(telegram_id: str) -> float:
    statut = get_user_statut(telegram_id)
    return FORFAITS.get(statut, FORFAITS["bronze"])["price"]

def get_permit_label(telegram_id: str) -> str:
    statut = get_user_statut(telegram_id)
    return FORFAITS.get(statut, FORFAITS["bronze"])["label"]

def get_user_balance(telegram_id: str) -> float:
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT balance FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    con.close()
    balance = row[0] if row else 0.0
    log(f"Get balance: {balance}", telegram_id)
    return balance

def update_user_balance(telegram_id: str, amount: float) -> float:
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT balance FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    new_balance = (row[0] if row else 0.0) + amount
    if row:
        cur.execute("UPDATE users SET balance=? WHERE telegram_id=?", (new_balance, telegram_id))
    else:
        cur.execute("INSERT INTO users (telegram_id, balance) VALUES (?,?)", (telegram_id, new_balance))
    con.commit()
    con.close()
    log(f"Balance updated: {new_balance}", telegram_id)
    return new_balance

def credit_and_upgrade(telegram_id: str, montant: float):
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT balance, total_recharge FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users (telegram_id, balance, total_recharge, forfait) VALUES (?,?,?,?)",
            (telegram_id, montant, montant if montant > 0 else 0.0, "bronze")
        )
        new_balance = montant
        new_total = montant if montant > 0 else 0.0
    else:
        old_balance, old_total = row
        new_balance = old_balance + montant
        new_total = old_total + montant if montant > 0 else old_total
        cur.execute("UPDATE users SET balance=?, total_recharge=? WHERE telegram_id=?",
                    (new_balance, new_total, telegram_id))
    con.commit()
    con.close()
    statut = upgrade_user_statut_auto(telegram_id)
    return new_balance, statut

def get_users(limit: int = 50):
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("""
        SELECT
            telegram_id,
            COALESCE(balance, 0.0) AS balance,
            COALESCE(lang, 'fr')   AS lang,
            COALESCE(total_recharge, 0.0) AS total_recharge,
            COALESCE(forfait, 'bronze')   AS forfait
        FROM users
        ORDER BY rowid DESC
        LIMIT ?
    """, (limit,))
    users = cur.fetchall()
    con.close()
    return users

def create_transaction(order_id: str, telegram_id: str, amount: float):
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("INSERT INTO transactions (order_id, telegram_id, amount) VALUES (?,?,?)",
                (order_id, telegram_id, amount))
    con.commit()
    con.close()
    log(f"Transaction created: {order_id} for {telegram_id} ({amount})", telegram_id)

def update_transaction_status(order_id: str, status: str):
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("UPDATE transactions SET status=? WHERE order_id=?", (status, order_id))
    con.commit()
    con.close()
    log(f"Transaction {order_id} updated: {status}")

# ========================== UTILS PERMIS ==========================
def soundex(nom: str) -> str:
    nom = (nom or "").upper()
    if not nom:
        return "0000"
    codes = {'BFPV': '1', 'CGJKQSXZ': '2', 'DT': '3', 'L': '4', 'MN': '5', 'R': '6'}
    result = nom[0]
    for char in nom[1:]:
        for key in codes:
            if char in key:
                code = codes[key]
                if code != result[-1]:
                    result += code
                break
    return result[:4].ljust(4, '0')

def codif_prenom(prenom: str) -> str:
    mapping = {
        'A': '1','B':'1','C':'2','D':'3','E':'3','F':'3','G':'4','H':'4','I':'4',
        'J':'5','K':'5','L':'5','M':'6','N':'6','O':'6','P':'7','Q':'7','R':'7',
        'S':'8','T':'8','U':'9','V':'9','W':'9','X':'9','Y':'9','Z':'9'
    }
    letter = (prenom or "0")[0].upper()
    return mapping.get(letter, '0')

def generer_permis(nom: str, prenom: str, date_txt: str):
    date_obj = datetime.strptime(date_txt, "%d-%m-%Y")
    date_str = date_obj.strftime("%d%m%y")
    base = soundex(nom) + codif_prenom(prenom) + date_str
    formatted = f"{base[:5]}-{base[5:11]}-**"
    return formatted, base

def convertir_en_code_saaq(code: str) -> str:
    if not code:
        return ""
    clavier = {
        'A':'2','B':'2','C':'2','D':'3','E':'3','F':'3','G':'4','H':'4','I':'4',
        'J':'5','K':'5','L':'5','M':'6','N':'6','O':'6','P':'7','Q':'7','R':'7','S':'7',
        'T':'8','U':'8','V':'8','W':'9','X':'9','Y':'9','Z':'9',
    }
    first = code[0].upper()
    return (clavier.get(first, '0') + code[1:]) if len(code) > 1 else clavier.get(first, '0')

# ========================== MENUS ==========================
def build_main_menu(user_id: int) -> InlineKeyboardMarkup:
    lang = get_user_lang(str(user_id))
    menu = [
        [InlineKeyboardButton("👥 Pro's", callback_data="propro")],
        [InlineKeyboardButton("🛒 Panier", callback_data="cart:view")],
        [InlineKeyboardButton("📜 Historique", callback_data="hist:view")],
        [InlineKeyboardButton("🚗 Vérifier mon permis" if lang == "fr" else "🚗 Check my license", callback_data="start_verifier_main")],
        [InlineKeyboardButton("💳 Recharger" if lang == "fr" else "💳 Top up", callback_data="add_balance")],
        [InlineKeyboardButton("🌐 Langue/Language", callback_data="choose_lang")],
        [InlineKeyboardButton("📞 Support", callback_data="support")],
        [InlineKeyboardButton("📚 FAQ", callback_data="faq")],
    ]
    if str(user_id) == ADMIN_ID:
        menu.append([InlineKeyboardButton("⚙️ Admin", callback_data="admin_menu")])
    return InlineKeyboardMarkup(menu)

async def clear_conversation(user_id: int):
    try:
        bot = app_telegram.bot
    except NameError:
        return
    if user_id in bot_messages:
        for msg_id in bot_messages[user_id]:
            try:
                await bot.delete_message(chat_id=user_id, message_id=msg_id)
            except Exception:
                pass
        bot_messages[user_id] = []

async def show_main_menu(user_id: int, clear: bool = True):
    if clear:
        await clear_conversation(user_id)

    # Données dynamiques
    balance = get_user_balance(str(user_id))
    statut_code = get_user_statut(str(user_id)) 

    emoji_and_label = FORFAITS.get(statut_code, FORFAITS["bronze"])["label"]
    emoji = emoji_and_label.split()[0] if emoji_and_label else "⬛️"
    
    label_word = "Statut" if get_user_lang(str(user_id)) == "fr" else "Status"

    statut_label = f"{emoji} {label_word} : {statut_code.capitalize()}"

    try:
        await app_telegram.bot.send_message(
            chat_id=user_id,
            text=msg(
                user_id,
                "welcome",
                telegram_id=user_id,
                balance=balance,
                statut_label=statut_label,
            ),
            reply_markup=build_main_menu(user_id)
        )
    except NameError:
        pass

# ========================== CATALOGUE PRODUITS ==========================
def _get_products(category="propro", tier=None):
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    q = "SELECT id, title, price, currency, stock, tier, content FROM products WHERE category=? AND is_active=1 AND stock>0"
    params = [category]
    if tier:
        q += " AND tier=?"
        params.append(tier)
    q += " ORDER BY id ASC"
    rows = cur.execute(q, params).fetchall()
    con.close()
    prods = []
    for (pid, title, price, currency, stock, ptier, content) in rows:
        prods.append({
            "id": pid,
            "title": title,
            "price": float(price) if price is not None else 0.0,
            "currency": currency or "CAD",
            "stock": int(stock) if stock is not None else 0,
            "tier": ptier or "",
            "category": category,
            "active": True,
            "content": content or ""
        })
    return prods

def _fmt_price(p):
    try:
        return f"{float(p):.2f}$"
    except Exception:
        return f"{p}$"

def _build_products_keyboard(page, total_pages, tier=None):
    filt_row = [InlineKeyboardButton("🔎 Filter", callback_data="filter_open")]
    nav_row = [
        InlineKeyboardButton("«", callback_data=f"prod:page:{max(0,page-1)}"),
        InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"),
        InlineKeyboardButton("»", callback_data=f"prod:page:{page+1}"),
    ]
    back_row = [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
    return InlineKeyboardMarkup([filt_row, nav_row, back_row])

async def show_products(update, context, page=0, tier=None):
    chat_id = None
    query = getattr(update, "callback_query", None)
    if query:
        chat_id = query.message.chat_id
        try:
            await query.answer()
        except Exception:
            pass
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
        except Exception:
            pass
    else:
        chat_id = update.effective_chat.id

    # Nettoyage anciennes bulles catalogue
    try:
        prev = CATALOG_MSGS.get(chat_id, [])
        for mid in prev:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass
        CATALOG_MSGS[chat_id] = []
    except Exception:
        pass

    prods = _get_products(category="propro", tier=tier)
    # --- pagination ---
    PER_PAGE = 2
    total_pages = max(1, (len(prods) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * PER_PAGE
    chunk = prods[start:start + PER_PAGE]

    # Rien à afficher
    if not prods or not chunk:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Filter", callback_data="filter_open")],
            [
                InlineKeyboardButton("«", callback_data=f"prod:page:{max(0,page-1)}"),
                InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"),
                InlineKeyboardButton("»", callback_data=f"prod:page:{min(total_pages-1,page+1)}"),
            ],
            [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")],
        ])
        m = await context.bot.send_message(
            chat_id=chat_id,
            text="Aucun produit SSN/DOB pour le moment.",
            reply_markup=kb
        )
        CATALOG_MSGS[chat_id] = [m.message_id]
        return

    # ========== 1) formatter une fiche produit ==========
    def fmt_product(p):
        raw = (p.get("content") or "").strip()

        if not raw:
            # fallback depuis 'title'
            t = (p.get("title") or "").strip()
            parts = [x.strip() for x in t.split("•")]
            name  = parts[0] if parts else "John Doe"
            first = name.split()[0].upper() if name else "JOHN"
            year     = parts[1] if len(parts) > 1 else ""
            city     = parts[2] if len(parts) > 2 else ""
            dob_full = ""
        else:
            import re
            def grab(keys):
                for key in keys:
                    m = re.search(rf"^(?:{ '|'.join([re.escape(k) for k in keys]) })\s*:\s*(.+)$",
                                  raw, flags=re.I | re.M | re.X)
                    if m:
                        return m.group(1).strip()
                return ""

            first    = (grab(["FIRST NAME"]) or "JOHN").split()[0].upper()
            dob_full = grab(["DOB(DD/MM/YYYY)", "DOB"]) or ""

            # 1) essaie de lire l’année dans DOB
            m = re.search(r'(19|20)\d{2}', dob_full or "")
            year = m.group(0) if m else ""

            # 2) fallback sur la colonne 'year' si vide
            if not year:
                year = (p.get("year") or "").strip()

            # 3) dernier recours: tente de lire 4 chiffres dans le title
            if not year:
                t = (p.get("title") or "")
                m2 = re.search(r'(19|20)\d{2}', t)
                year = m2.group(0) if m2 else ""

            city = grab(["CITY"]) or (p.get("city") or "")

        base  = p.get("tier") or "FAKEPERSON"
        try:
            price = float(p.get("price", 10.0) or 10.0)
        except Exception:
            price = 10.0
        curr  = p.get("currency") or "CAD"

        # Année en priorité, sinon date complète, sinon N/A
        return "\n".join([
            f"FIRST NAME: {first}",
            f"DOB: {year or dob_full or 'N/A'}",
            f"CITY: {city or '—'}",
            f"BASE: {base}",
            f"PRICE: {price:.2f} {curr}",
        ])

    # ========== 2) envoi des cartes produits ==========
    sent_ids = []
    for idx, p in enumerate(chunk, start=1):
        txt = fmt_product(p)
        pid = p.get("id")

        kb_rows = [[
            InlineKeyboardButton("⚡ Buy Now", callback_data=f"buy:{pid}"),
            InlineKeyboardButton("🛒 Add to Cart", callback_data=f"cart:add:{pid}"),
        ]]

        is_last_of_page = (idx == len(chunk))
        if is_last_of_page:
            kb_rows.append([InlineKeyboardButton("🔎 Filter", callback_data="filter_open")])
            kb_rows.append([
                InlineKeyboardButton("«", callback_data=f"prod:page:{max(0, page-1)}"),
                InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"),
                InlineKeyboardButton("»", callback_data=f"prod:page:{min(total_pages-1, page+1)}"),
            ])
            kb_rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")])

        kb = InlineKeyboardMarkup(kb_rows)
        m = await context.bot.send_message(chat_id=chat_id, text=txt, reply_markup=kb)
        sent_ids.append(m.message_id)

    CATALOG_MSGS[chat_id] = sent_ids

async def filter_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = getattr(update, "callback_query", None)
    if q:
        await q.answer()
    kb = [
        [InlineKeyboardButton("Name",  callback_data="filter:name"),
         InlineKeyboardButton("City",  callback_data="filter:city")],
        [InlineKeyboardButton("Base",  callback_data="filter:base"),
         InlineKeyboardButton("Price", callback_data="filter:price")],
        [InlineKeyboardButton("Year",  callback_data="filter:year")],
        [InlineKeyboardButton("🛒 Panier",     callback_data="cart:view"),
         InlineKeyboardButton("🧾 Historique", callback_data="hist:view")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")],
    ]
    if q:
        await replace_view(q, "Choose a filter:", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text("Choose a filter:", reply_markup=InlineKeyboardMarkup(kb))

async def filter_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    _, field = q.data.split(':', 1)  # name|city|base|price|year
    context.user_data['awaiting_filter'] = field
    prompts = {
        'name':  "Type a name fragment (ex: John):",
        'city':  "Type a city fragment (ex: Toronto):",
        'base':  "Type a base fragment (ex: Montreal Pack / FAKEPERSON):",
        'price': "Max price (number, e.g. 12):",
        'year':  "Year digits (e.g. 1991):",
    }
    await q.message.reply_text(prompts[field])

def _filter_products(products, field, value):
    val = (value or "").strip().lower()
    if not val:
        return products
    def norm(s): 
        return (s or "").strip().lower()
    out = []
    for p in products:
        title = norm(p.get("title",""))
        price = float(p.get("price", 0) or 0)
        tier  = norm(p.get("tier",""))
        # heuristique de découpe (FIRST LAST • YEAR • CITY)
        parts = [x.strip() for x in (p.get("title","") or "").split("•")]
        name = parts[0] if parts else ""
        year = (parts[1] if len(parts)>1 else "")
        city = (parts[2] if len(parts)>2 else "")
        if field == "name" and val in norm(name):
            out.append(p)
        elif field == "city" and val in norm(city):
            out.append(p)
        elif field == "base" and val in tier:
            out.append(p)
        elif field == "price":
            try:
                maxp = float(val)
                if price <= maxp:
                    out.append(p)
            except:
                pass
        elif field == "year" and val in norm(year):
            out.append(p)
    return out

async def on_filter_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ✅ Ne rien faire si on n’est pas en mode filtre
    if not context.user_data.get('awaiting_filter'):
        return

    field = context.user_data.get('awaiting_filter')
    value = (update.message.text or '').strip()
    prods_all = _get_products(category="propro", tier=None)
    prods = _filter_products(prods_all, field, value)

    if not prods:
        await update.message.reply_text("Aucun résultat pour ce filtre.")
        return

    # Affiche max 3 résultats
    chunk = prods[:3]
    for p in chunk:
        t = p.get("title", "").strip()
        parts = [x.strip() for x in t.split("•")]
        name = parts[0] if parts else "John Doe"
        first = name.split()[0].upper() if name else "JOHN"
        masked = first + " " + ("*" * 3)
        dob = parts[1] if len(parts) > 1 else "N/A"
        city = parts[2] if len(parts) > 2 else ""
        base = p.get("tier", "FAKEPERSON") or "FAKEPERSON"
        price = f'{float(p.get("price", 10.0)):.2f}'
        curr = p.get("currency", "CAD") or "CAD"

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚡ Buy Now", callback_data=f"buy:{p['id']}"),
            InlineKeyboardButton("🛒 Add to Cart", callback_data=f"cart:add:{p['id']}"),
        ]])

        txt = "\n".join([
            f"FIRST NAME: {masked}",
            f"DOB: {dob}",
            f"CITY: {city or '—'}",
            f"BASE: {base}",
            f"PRICE: {price} {curr}",
        ])
        await update.message.reply_text(txt, reply_markup=kb)

# ========================== BOUTONS SIMPLES ==========================
async def callback_show_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.callback_query.message.reply_text(
        f"🆔 Ton ID Telegram est : <code>{user_id}</code>",
        parse_mode="HTML"
    )

async def add_balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    wallet_btc = "bitcoin:bc1q4dzyt56lpv2524sq3lpm69fl57523780r846xt"
    await replace_view(
        q,
        "💵 Pour recharger votre solde, envoyez le montant désiré à cette adresse Bitcoin :\n"
        f"<code>{wallet_btc}</code>\n\n"
        "Après paiement, envoyez une preuve (capture ou TXID) au support ou directement ici.\n"
        "Un admin créditera manuellement votre compte.",
        reply_markup=kb_back_to_menu(),
        parse_mode="HTML"
    )
    return ConversationHandler.END

async def choose_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    keyboard = [
        [InlineKeyboardButton("🇫🇷 Français", callback_data="set_lang_fr"),
         InlineKeyboardButton("🇬🇧 English",  callback_data="set_lang_en")],
        [InlineKeyboardButton("⬅️ Retour",    callback_data="menu_accueil")]
    ]
    await replace_view(
        q,
        MESSAGES['choose_lang']['fr'] + "\n" + MESSAGES['choose_lang']['en'],
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def set_lang_fr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    set_user_lang(str(user_id), "fr")
    await update.callback_query.message.reply_text(MESSAGES['lang_updated']['fr'])
    await show_main_menu(user_id, clear=True)

async def set_lang_en(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    set_user_lang(str(user_id), "en")
    await update.callback_query.message.reply_text(MESSAGES['lang_updated']['en'])
    await show_main_menu(user_id, clear=True)

async def callback_check_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    balance = get_user_balance(str(user_id))
    await query.message.reply_text(msg(user_id, "balance", balance=balance))

from telegram.ext import ConversationHandler

async def goto_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ferme proprement l’écran courant et affiche un menu tout neuf."""
    q = getattr(update, "callback_query", None)
    if q:
        # stoppe le spinner (animation bleue Telegram)
        try:
            await q.answer()
        except:
            pass
        # supprime la bulle actuelle (FAQ, Support, Admin, etc.)
        try:
            await q.message.delete()
        except:
            pass

    # nettoie tout l’état de la conversation
    context.user_data.clear()

    # affiche le menu principal proprement
    user_id = update.effective_user.id
    await show_main_menu(user_id, clear=True)

    # stoppe toute attente de réponse (empêche "1 ou 2")
    return ConversationHandler.END

async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await goto_menu(update, context)

async def callback_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = get_user_lang(str(update.effective_user.id))
    await replace_view(
        q,
        "Support : @johnm7777" if lang == "fr" else "Support: @johnm7777",
        reply_markup=kb_back_to_menu()
    )

async def callback_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = get_user_lang(str(update.effective_user.id))
    if lang == "fr":
        txt = (
            "📚 FAQ\n\n"
            "💵 Système de paliers :\n"
            "• Bronze : 10.00$ / permis\n"
            "• Silver : 8.00$ / permis (après 250$ rechargés)\n"
            "• Gold : 6.00$ / permis (après 500$)\n"
            "• Platinum : 4.00$ / permis (après 1000$)\n"
            "➡️ Le passage de palier est automatique selon vos recharges.\n\n"
            "🆘 Support :\n"
            "Après tout paiement, envoyez un *screenshot* de la preuve. Le crédit est ajouté très rapidement.\n"
            "Support direct : @johnm7777\n\n"
            "⚡ Délais :\n"
            "La validation des permis est quasi instantanée. Les paiements sont confirmés en quelques minutes.\n\n"
            "🤝 Partenariats :\n"
            "Nous sommes ouverts à toute collaboration ou intégration.\n\n"
            "🚀 Nouvelles fonctions :\n"
            "De nouvelles fonctionnalités arrivent bientôt (automatisations, statistiques, outils avancés)."
        )
    else:
        txt = (
            "📚 FAQ\n\n"
            "💵 Tier system:\n"
            "• Bronze: $10.00 / license\n"
            "• Silver: $8.00 / license (after $250 recharged)\n"
            "• Gold: $6.00 / license (after $500)\n"
            "• Platinum: $4.00 / license (after $1000)\n"
            "➡️ Tier upgrades are automatic based on your recharges.\n\n"
            "🆘 Support:\n"
            "After each payment, please send a *screenshot* as proof. Your balance will be credited very quickly.\n"
            "Direct support: @johnm7777\n\n"
            "⚡ Processing time:\n"
            "License validation is almost instant. Payments are confirmed within minutes.\n\n"
            "🤝 Partnerships:\n"
            "We are open to any collaboration or integration.\n\n"
            "🚀 Upcoming features:\n"
            "New functionalities coming soon (automation, statistics, advanced tools)."
        )
    await replace_view(q, txt, reply_markup=kb_back_to_menu(), parse_mode="Markdown")
    
    # ========================== HISTORIQUE (helpers communs) ==========================
def _table_exists(con, name: str) -> bool:
    try:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,))
        return cur.fetchone() is not None
    except Exception:
        return False

def _cols(con, table: str) -> set:
    cur = con.cursor()
    return {r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}

def _paginate(total: int, page: int, per_page: int = 2):
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    offset = page * per_page
    return page, total_pages, offset, per_page

def _kb_hist_pagination(prefix: str, page: int, total_pages: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("«", callback_data=f"{prefix}:page:{max(0, page-1)}"),
            InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"),
            InlineKeyboardButton("»", callback_data=f"{prefix}:page:{min(total_pages-1, page+1)}"),
        ],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")],
    ])

# ========================== HISTORIQUE PERMIS ==========================
def ensure_verifications_table():
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            fullname TEXT,
            permis TEXT,
            status TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.commit()
    con.close()

def save_permit_history(user_id: int, fullname: str, permis: str|None, status: str):
    try:
        con = sqlite3.connect(DB_NAME)
        cur = con.cursor()
        cur.execute(
            "INSERT INTO verifications (user_id, fullname, permis, status) VALUES (?,?,?,?)",
            (str(user_id), fullname, permis, status)
        )
        con.commit()
        con.close()
    except Exception as e:
        log(f"save_permit_history error: {e}", user_id, "error")

async def hist_menu(update, context):
    q = update.callback_query
    await q.answer()
    kb = [
        [InlineKeyboardButton("👥 Pro's",  callback_data="hist:pros"),
         InlineKeyboardButton("🚗 Permis", callback_data="hist:permis")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
    ]
    await replace_view(q, "📜 Choisissez une section :", reply_markup=InlineKeyboardMarkup(kb))

async def hist_pros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche l'historique des produits achetés, avec pagination propre (pas d'accumulation)."""
    from shop_helpers import full_product_text
    import json

    q = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)

    # Supprimer les anciens messages envoyés (pour ne pas accumuler)
    old_msgs = context.user_data.get("hist_msgs", [])
    for mid in old_msgs:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=mid)
        except:
            pass
    context.user_data["hist_msgs"] = []  # Réinitialiser la liste

    # Pagination
    page = 0
    if q.data.startswith("hist:pros:page:"):
        try:
            page = int(q.data.split(":")[-1])
        except:
            page = 0

    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()

    cur.execute("SELECT COUNT(*) FROM purchases WHERE user_id = ?", (uid,))
    total = cur.fetchone()[0]
    per_page = 2
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    offset = page * per_page

    cur.execute("""
        SELECT id, product_id, full_data, created_at
        FROM purchases
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """, (uid, per_page, offset))
    rows = cur.fetchall()
    con.close()

    if not rows:
        await q.edit_message_text("🧾 Aucun achat trouvé.")
        return

    # Supprimer le message précédent de pagination
    try:
        await q.message.delete()
    except Exception:
        pass

    # Envoie chaque fiche séparément
    for row in rows:
        pid = row[0]
        full_data = row[2]
        date = row[3]

        try:
            parsed = json.loads(full_data)
        except Exception:
            parsed = {}

        fiche = full_product_text(parsed)
        fiche += f"\n📅 {date}\n🆔 ID achat: `{pid}`"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Supprimer", callback_data=f"delete_history_{pid}")]
        ])

        msg_sent = await context.bot.send_message(
            chat_id=q.message.chat.id,
            text=fiche,
            parse_mode="Markdown",
            reply_markup=kb
        )
        # Enregistrer le message pour suppression future
        context.user_data["hist_msgs"].append(msg_sent.message_id)

    # Pagination + retour (message séparé)
    if total_pages == 1:
        # Si une seule page, on désactive les flèches gauche/droite
        nav_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("«", callback_data="noop"),
                InlineKeyboardButton("1/1", callback_data="noop"),
                InlineKeyboardButton("»", callback_data="noop"),
            ],
            [InlineKeyboardButton("⬅️ Retour", callback_data="hist:view")],
        ])
    else:
        nav_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("«", callback_data=f"hist:pros:page:{max(0, page-1)}"),
                InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"),
                InlineKeyboardButton("»", callback_data=f"hist:pros:page:{min(total_pages-1, page+1)}"),
            ],
            [InlineKeyboardButton("⬅️ Retour", callback_data="hist:view")],
        ])

    pagination_msg = await context.bot.send_message(
        chat_id=q.message.chat.id,
        text=f"📄 Page {page+1}/{total_pages}",
        reply_markup=nav_kb
    )

    # Ajouter aussi le message de pagination à la liste pour qu’il soit supprimé au changement de page
    context.user_data["hist_msgs"].append(pagination_msg.message_id)


async def close_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # Supprime simplement le message courant (unique) d’historique
    try:
        await q.message.delete()
    except Exception:
        # fallback: si on a stocké l’id, tente une suppression
        mid = context.user_data.get("hist_nav")
        if mid:
            try:
                await q.message.bot.delete_message(chat_id=q.message.chat.id, message_id=mid)
            except Exception:
                pass

    context.user_data.pop("hist_nav", None)
    # retour au menu
    try:
        await show_main_menu(update.effective_user.id)
    except Exception:
        pass

    # Retour propre au menu principal
    await show_main_menu(update.effective_user.id)
    



async def hist_permis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("""
        SELECT fullname, permis, status, datetime(created_at,'localtime')
        FROM verifications
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 10
    """, (str(q.from_user.id),))
    rows = cur.fetchall()
    con.close()

    if not rows:
        await replace_view(q, "📜 Aucun historique de permis pour le moment.", reply_markup=kb_back_to_menu())
        return

    lines = ["📜 Vos 10 dernières vérifications :\n"]
    for i,(fullname, permis, status, dt) in enumerate(rows, start=1):
        if status == "valide" and permis:
            lines.append(f"{i}️⃣ {fullname} — {permis} — ✅ Valide")
        else:
            lines.append(f"{i}️⃣ {fullname} — ❌ Aucun permis trouvé")
    lines.append(f"\n🕓 Dernière mise à jour : {rows[0][3]}")

    await replace_view(q, "\n".join(lines), reply_markup=kb_back_to_menu())

# ========================== FLOWS VALIDATION ==========================
def reset_session(user_id: int):
    for d in [bot_messages, user_sessions, pending_payments, user_validation_status]:
        list(d.pop(user_id, None) for _ in [0])

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log("START command received", update.effective_user.id)
    global bot_loop
    user_id = update.effective_user.id
    if not user_exists(str(user_id)):
        update_user_balance(str(user_id), 0.0)
        upgrade_user_statut_auto(str(user_id))
    await show_main_menu(user_id)

async def start_verifier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reset_session(user_id); context.user_data.clear()
    msgx = await update.message.reply_text(msg(user_id, "enter_bulk_qty"), reply_markup=kb_back_cancel())
    bot_messages.setdefault(user_id, []).append(msgx.message_id)
    return ASK_QTY

async def start_verifier_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import logging
    logger = logging.getLogger("BTN_VERIF")
    logger.info("➡️ Bouton 'Vérifier mon permis' cliqué")

    q = update.callback_query
    await q.answer()
    logger.info(f"✅ callback_query répondu pour {q.from_user.id}")

    user_id = update.effective_user.id
    logger.info(f"🎯 User ID: {user_id}")

    reset_session(user_id)
    context.user_data.clear()
    logger.info("🧹 Session et user_data reset")

    await q.message.edit_text(
        text=msg(user_id, "enter_bulk_qty"),
        reply_markup=kb_back_cancel()
    )
    logger.info("📤 Message ASK_QTY envoyé")

    return ASK_QTY

def _mode_keyboard(lang: str) -> InlineKeyboardMarkup:
    if lang == "fr":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ Saisir manuellement", callback_data="mode_manual")],
            [InlineKeyboardButton("📄 Envoyer un fichier CSV", callback_data="mode_csv")]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✍️ Enter manually", callback_data="mode_manual")],
            [InlineKeyboardButton("📄 Upload CSV file", callback_data="mode_csv")]
        ])

async def ask_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip() if update.message else ""
    try:
        qty = int(text)
        if qty <= 0:
            raise ValueError
    except Exception:
        await (update.message or update.callback_query.message).reply_text(
            "Nombre invalide. Entrez un entier positif (ex: 1, 3, 6)."
        )
        return ASK_QTY

    context.user_data["batch_total"] = qty
    context.user_data["entries"] = []
    lang = get_user_lang(str(user_id))

    if qty == 1:
        msgx = await (update.message or update.callback_query.message).reply_text(
            msg(user_id, "enter_firstname")
        )
        bot_messages.setdefault(user_id, []).append(msgx.message_id)
        return ASK_PRENOM

    await (update.message or update.callback_query.message).reply_text(
        msg(user_id, "bulk_choice"),
        reply_markup=_mode_keyboard(lang)
    )
    return ASK_MODE

async def choose_mode_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data["current_index"] = 1
    msgx = await update.callback_query.message.reply_text(
        f"✍️ Saisie #{context.user_data['current_index']} — " + msg(user_id, "enter_firstname")
    )
    bot_messages.setdefault(user_id, []).append(msgx.message_id)
    return MANUAL_PRENOM

async def choose_mode_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.callback_query.message.reply_text(
        "📄 Envoie le fichier CSV maintenant.\n"
        "Colonnes requises (en-tête, insensible à la casse) : prenom, nom, date (format: 22-11-1998).\n"
        "Exemple:\nprenom,nom,date\nJean,Martin,22-11-1998\nAlice,Dupont,01-01-1990"
    )
    return CSV_WAIT

async def manual_receive_prenom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tmp_prenom"] = update.message.text.strip()
    msgx = await update.message.reply_text(msg(update.effective_user.id, "enter_lastname"))
    bot_messages.setdefault(update.effective_user.id, []).append(msgx.message_id)
    return MANUAL_NOM

async def manual_receive_nom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tmp_nom"] = update.message.text.strip()
    msgx = await update.message.reply_text(msg(update.effective_user.id, "enter_birth"))
    bot_messages.setdefault(update.effective_user.id, []).append(msgx.message_id)
    return MANUAL_DATE

async def manual_receive_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    prenom = context.user_data.get("tmp_prenom", "")
    nom = context.user_data.get("tmp_nom", "")
    date_txt = update.message.text.strip()
    try:
        formatted, base = generer_permis(nom, prenom, date_txt)
    except Exception:
        await update.message.reply_text("⚠️ Date invalide (attendu JJ-MM-AAAA). Réessayez.")
        return MANUAL_DATE

    context.user_data["entries"].append({
        "prenom": prenom, "nom": nom, "date": date_txt,
        "formatted": formatted, "base": base
    })
    total = context.user_data["batch_total"]
    idx = context.user_data["current_index"]

    if idx < total:
        context.user_data["current_index"] = idx + 1
        msgx = await update.message.reply_text(
            f"✅ Saisie #{idx} enregistrée.\n\n"
            f"✍️ Saisie #{idx+1} — " + msg(user_id, "enter_firstname")
        )
        bot_messages.setdefault(user_id, []).append(msgx.message_id)
        return MANUAL_PRENOM

    qty = len(context.user_data["entries"])
    unit_price = get_permit_price(str(user_id))
    total_cost = unit_price * qty
    statut_label = get_permit_label(str(user_id))
    await update.message.reply_text(
        f"🧾 Vous avez saisi {qty} permis.\n"
        f"Tarif: {statut_label} — {unit_price:.2f}$ par permis\n"
        f"Coût total: {total_cost:.2f}$\n\n"
        f"Souhaitez-vous lancer la validation maintenant ? (Oui / Non)"
    )
    return BULK_CONFIRM

async def csv_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    document = update.message.document
    if not document:
        await update.message.reply_text("Aucun fichier détecté. Envoie un fichier CSV.")
        return CSV_WAIT

    file = await context.bot.get_file(document.file_id)
    bio = io.BytesIO()
    await file.download(out=bio)
    bio.seek(0)
    text = bio.read().decode('utf-8', errors='replace')
    reader = csv.DictReader(io.StringIO(text))

    rows_ok = []
    for row in reader:
        prenom = (row.get('prenom') or row.get('Prenom') or "").strip()
        nom = (row.get('nom') or row.get('Nom') or "").strip()
        date_txt = (row.get('date') or row.get('Date') or "").strip()
        if not prenom or not nom or not date_txt:
            continue
        try:
            formatted, base = generer_permis(nom, prenom, date_txt)
        except Exception:
            continue
        rows_ok.append({"prenom": prenom, "nom": nom, "date": date_txt, "formatted": formatted, "base": base})

    if not rows_ok:
        await update.message.reply_text("CSV vide ou aucune ligne valide (colonnes: prenom,nom,date).")
        return ConversationHandler.END

    context.user_data["entries"] = rows_ok
    qty = len(rows_ok)
    unit_price = get_permit_price(str(user_id))
    total_cost = unit_price * qty
    statut_label = get_permit_label(str(user_id))

    await update.message.reply_text(
        f"📄 CSV chargé: {qty} lignes valides.\n"
        f"Tarif: {statut_label} — {unit_price:.2f}$ par permis\n"
        f"Coût total: {total_cost:.2f}$\n\n"
        f"Souhaitez-vous lancer la validation maintenant ? (Oui / Non)"
    )
    return BULK_CONFIRM

async def bulk_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reply = update.message.text.strip().lower()
    if reply not in ["oui", "yes", "non", "no"]:
        await update.message.reply_text("❓ Oui ou Non / Yes or No?")
        return BULK_CONFIRM

    if reply in ["non", "no"]:
        await update.message.reply_text("🔙 Retour au menu principal…")
        await show_main_menu(user_id)
        return ConversationHandler.END

    entries = context.user_data.get("entries", [])
    if not entries:
        await update.message.reply_text("Aucune donnée à lancer.")
        return ConversationHandler.END

    unit_price = get_permit_price(str(user_id))
    total_cost = unit_price * len(entries)
    balance = get_user_balance(str(user_id))
    statut = get_user_statut(str(user_id))
    lang = get_user_lang(str(user_id))

    if balance < total_cost:
        keyboard = [[InlineKeyboardButton("💳 Recharger" if lang == "fr" else "💳 Top up", callback_data="add_balance")]]
        await update.message.reply_text(
            msg(user_id, "solde_insuffisant", balance=balance, prix=total_cost, statut=FORFAITS[statut]['label']),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END

    new_balance = update_user_balance(str(user_id), -total_cost)
    await update.message.reply_text(
        f"🏦 {msg(user_id, 'balance', balance=new_balance)}\n"
        f"{FORFAITS[statut]['label']} ({unit_price:.2f}$ x {len(entries)} permis = {total_cost:.2f}$)"
    )
    await update.message.reply_text(msg(user_id, "decrytage_en_cours"))

    batch_id = f"{user_id}:{int(datetime.now().timestamp())}"
    batch_runs[batch_id] = {"total": len(entries), "resolved": 0, "notified": False,"lock": asyncio.Lock(),}

    tasks = []

    for item in entries:

        fullname = f"{item['prenom']} {item['nom']}"

        tasks.append(asyncio.create_task(

            launch_parallel_calls(

                item["base"], user_id, num_calls=10,

                fullname=fullname, formatted=item["formatted"],

                batch_id=batch_id

            )

        ))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for (item, res) in zip(entries, results):

        if isinstance(res, Exception):

            log(f"Bulk launch error for {item['prenom']} {item['nom']}: {res}", user_id, "error")



    return ConversationHandler.END

async def receive_prenom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["prenom"] = update.message.text.strip()
    msgx = await update.message.reply_text(msg(update.effective_user.id, "enter_lastname"))
    bot_messages.setdefault(update.effective_user.id, []).append(msgx.message_id)
    return ASK_NOM

async def receive_nom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nom"] = update.message.text.strip()
    msgx = await update.message.reply_text(msg(update.effective_user.id, "enter_birth"))
    bot_messages.setdefault(update.effective_user.id, []).append(msgx.message_id)
    return ASK_DATE

async def receive_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data["date"] = update.message.text.strip()
    nom = context.user_data.get("nom", "")
    prenom = context.user_data.get("prenom", "")
    try:
        formatted, base = generer_permis(nom, prenom, context.user_data["date"])
    except Exception:
        await update.message.reply_text("⚠️ Date invalide (attendu JJ-MM-AAAA). Réessayez.")
        return ASK_DATE

    context.user_data["code_base"] = base
    context.user_data["formatted_permis"] = formatted
    statut = get_permit_label(str(user_id))
    prix = get_permit_price(str(user_id))
    msgx = await update.message.reply_text(
        msg(user_id, "show_permis", permis=formatted, prix=prix, statut=statut),
        parse_mode="Markdown"
    )
    bot_messages.setdefault(user_id, []).append(msgx.message_id)
    return CONFIRM_VERIF

async def confirm_permis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reponse = update.message.text.strip().lower()
    base = context.user_data.get("code_base")
    formatted = context.user_data.get("formatted_permis")
    fullname = context.user_data.get("prenom", "") + " " + context.user_data.get("nom", "")
    lang = get_user_lang(str(user_id))
    prix = get_permit_price(str(user_id))
    statut = get_user_statut(str(user_id))

    if reponse in ["non", "no"]:
        await update.message.reply_text(f"📄 {formatted.replace('**', '00')}")
        await update.message.reply_text("🔙 Retour au menu principal..." if lang == "fr" else "🔙 Back to main menu...")
        await show_main_menu(user_id)
        return ConversationHandler.END

    if reponse in ["oui", "yes"]:
        balance = get_user_balance(str(user_id))
        if balance < prix:
            keyboard = [[InlineKeyboardButton("💳 Recharger" if lang == "fr" else "💳 Top up", callback_data="add_balance")]]
            await update.message.reply_text(
                msg(user_id, "solde_insuffisant", balance=balance, prix=prix, statut=FORFAITS[statut]['label']),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return ConversationHandler.END
        else:
            new_balance = update_user_balance(str(user_id), -prix)
            await update.message.reply_text(
                f"🏦 {msg(user_id, 'balance', balance=new_balance)}\n{FORFAITS[statut]['label']} ({prix:.2f}$/permis)"
            )
            await update.message.reply_text(msg(user_id, "decrytage_en_cours"))

            batch_id = f"{user_id}:{context.user_data.get('code_base','one')}"
            batch_runs[batch_id] = {"total": 1, "resolved": 0, "notified": False,"lock": asyncio.Lock(),}

            await launch_parallel_calls(
                base, user_id, num_calls=10,
                fullname=fullname, formatted=formatted,
                batch_id=batch_id
            )
            return ConversationHandler.END

    await update.message.reply_text("❓ Oui ou Non / Yes or No?")
    return CONFIRM_VERIF


# ========================== ADMIN: PRODUITS PROPRO ==========================
def _get_db_from_context(context):
    # db_conn a été déposé dans bot_data au démarrage (shop_helpers.ensure_shop_tables)
    return context.application.bot_data.get('db_conn')

def _guess_columns(cur):
    # récupère la structure de la table products
    cols = [r[1] for r in cur.execute("PRAGMA table_info(products)").fetchall()]
    return set(cols)

def _parse_price(s):
    try:
        s = (s or "").replace(',', '.')
        s = re.sub(r'[^0-9\.]', '', s)
        return float(s) if s else 0.0
    except Exception:
        return 0.0

# --- Parse manuellement le bloc (dictionnaire déjà extrait) ----
def _parse_manual_block(text: str):
    import re

    # normalize lines like "FIRST NAME: John"
    pairs = {}
    for line in (text or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            pairs[k.strip().upper()] = v.strip()

    def pick(*keys, default=""):
        for k in keys:
            if k in pairs and pairs[k]:
                return pairs[k]
        return default

    # AJOUTEZ CES 3 LIGNES :
    sin      = pick('SIN')
    dl       = pick('DL')
    password = pick('PASSWORD')

    first   = pick('FIRST NAME')
    last    = pick('LAST NAME', 'LASTNAME')
    dob     = pick('DOB', 'DOB(DD/MM/YYYY)')
    address = pick('ADRESSE', 'ADDRESS')
    city    = pick('CITY')
    postal  = pick('CODE POSTAL', 'POSTAL')
    email   = pick('EMAIL')
    phone   = pick('PHONE NUMBER', 'PHONE')
    base    = pick('BASE', default='FAKEPERSON')

    def _parse_price(s):
        try:
            s = (s or "").replace(',', '.')
            s = re.sub(r'[^0-9\.]', '', s)
            return float(s) if s else 0.0
        except Exception:
            return 0.0

    price    = _parse_price(pick('PRICE'))
    currency = pick('CURRENCY', default='CAD')

    try:
        stock = int(pick('STOCK', default='1') or 1)
    except Exception:
        stock = 1

    m = re.search(r'(19|20)\d{2}', dob or '')
    year = m.group(0) if m else ''

    name  = (f"{first} {last}").strip().upper()
    title = f"{name} • {year} • {city.upper()}".strip()

    content_lines = [
        # AJOUTEZ CES 3 LIGNES ICI :
        f"SIN: {sin}",
        f"DL: {dl}",
        f"PASSWORD: {password}",
        
        # Le reste ne change pas :
        f"FIRST NAME: {first}",
        f"LAST NAME: {last}",
        f"DOB(DD/MM/YYYY): {dob}",
        f"ADRESSE: {address}",
        f"CITY: {city}",
        f"CODE POSTAL: {postal}",
        f"EMAIL: {email}",
        f"PHONE: {phone}",
        f"BASE: {base}",
        f"PRICE: {price:.2f} {currency}",
    ]
    content = "\n".join(content_lines)

    return {
        'title': title,
        'content': content,
        'price': price,
        'tier': base,
        'city': city,
        'year': year,
        'stock': stock,
        'currency': currency,
    }

async def admin_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    kb = [
        [InlineKeyboardButton("➕ Ajouter (manuel)", callback_data="admin_prod_add")],
        [InlineKeyboardButton("📥 Import CSV",       callback_data="admin_prod_csv")],
        [InlineKeyboardButton("🗑 Supprimer",        callback_data="admin_prod_del")],
        [InlineKeyboardButton("📦 Lister (10)",      callback_data="admin_prod_list")],
        [InlineKeyboardButton("🔙 Retour",           callback_data="admin_menu")],
    ]
    await update.callback_query.message.reply_text("🧱 Gestion des produits PRO'S :", reply_markup=InlineKeyboardMarkup(kb))

# ========================== ADMIN (sections ajustées) ==========================

async def admin_prod_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    db = _get_db_from_context(context)
    if not db:
        await update.callback_query.message.reply_text("DB indisponible.")
        return

    c = db.cursor()
    rows = c.execute(
        "SELECT id, title, price, stock FROM products ORDER BY id DESC LIMIT 10"
    ).fetchall()
    if not rows:
        await update.callback_query.message.reply_text("Aucun produit.")
        return

    txt = "\n".join([f"#{r[0]} — {r[1]} — {r[2]:.2f}$ — stock={r[3]}" for r in rows])
    await update.callback_query.message.reply_text(txt)


# --- keep this function as-is, just ADD the line that sets the flag (new) ---
async def admin_prod_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Vérifie que seul l'admin peut utiliser cette commande
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    # Indique qu’on attend le texte du produit
    context.user_data["awaiting_admin_product_text"] = True

    # Message d'instructions avec tous les champs importants
    await update.callback_query.message.reply_text(
        "📦 *Ajout de produit*\n"
        "Veuillez coller les infos dans ce format exact :\n\n"
        "SIN: 123456789\n"
        "DL: A123456789012\n"
        "FIRST NAME: John\n"
        "LAST NAME: Doe\n"
        "DOB(DD/MM/YYYY): 01/01/1990\n"
        "ADRESSE: 123 Rue Exemple\n"
        "CITY: Montréal\n"
        "CODE POSTAL: H1A 2B3\n"
        "EMAIL: john@example.com\n"
        "PASSWORD: swhsbhwbhww\n"
        "PHONE NUMBER: 5141234567\n"
        "BASE: Client régulier\n"
        "PRICE: 50.00\n\n"
        "_Chaque ajout crée 1 produit (stock = 1)._",
        parse_mode="Markdown"
    )

    return ADMIN_WAIT_PRODUCT_TEXT


# --- modify admin_prod_add_receive with guards ---
async def admin_prod_add_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard 1: only admin
    if str(update.effective_user.id) != ADMIN_ID:
        return
    # Guard 2: only if we actually asked for a product text
    if not context.user_data.get("awaiting_admin_product_text"):
        return

    db = _get_db_from_context(context)
    if not db:
        await update.message.reply_text("DB indisponible.")
        context.user_data.pop("awaiting_admin_product_text", None)
        return ConversationHandler.END

    c = db.cursor()
    data = _parse_manual_block(update.message.text)
    cols = _guess_columns(c)

    fields = []; values = []
    if 'title'    in cols: fields.append('title');    values.append(data['title'])
    if 'content'  in cols: fields.append('content');  values.append(data['content'])
    if 'price'    in cols: fields.append('price');    values.append(data['price'])
    if 'tier'     in cols: fields.append('tier');     values.append(data['tier'])
    if 'city'     in cols: fields.append('city');     values.append(data['city'])
    if 'year'     in cols: fields.append('year');     values.append(data['year'])
    if 'stock'    in cols: fields.append('stock');    values.append(data['stock'])
    if 'category' in cols: fields.append('category'); values.append('propro')
    if 'currency' in cols: fields.append('currency'); values.append('CAD')
    if 'is_active' in cols: fields.append('is_active'); values.append(1)

    q = f"INSERT INTO products ({', '.join(fields)}) VALUES ({', '.join(['?']*len(values))})"
    c.execute(q, values)
    db.commit()

    await update.message.reply_text(f"✅ Ajouté: {data['title']} ({data['price']:.2f}$)")
    # Clear flag so stray texts won’t trigger again
    context.user_data.pop("awaiting_admin_product_text", None)
    return ConversationHandler.END


async def admin_prod_csv_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    await update.callback_query.message.reply_text(
        "Envoie le CSV (UTF-8). En-têtes acceptées (flexibles):\n"
        "sin,dl,first,last,dob,address,city,postal,email,phone,base,price,stock\n"
        "→ 1 ligne = 1 produit (stock par défaut=1)."
    )
    return ADMIN_WAIT_CSV


async def admin_prod_csv_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        return

    doc = update.message.document
    if not doc:
        await update.message.reply_text("Aucun fichier. Réessaie.")
        return ADMIN_WAIT_CSV

    db = _get_db_from_context(context)
    if not db:
        await update.message.reply_text("DB indisponible.")
        return ConversationHandler.END

    c = db.cursor()
    from io import BytesIO, StringIO
    f = await context.bot.get_file(doc.file_id)
    bio = BytesIO(); await f.download(out=bio); bio.seek(0)
    text = bio.read().decode('utf-8', errors='replace')

    import csv, re
    rdr = csv.DictReader(StringIO(text))
    cols = _guess_columns(c)

    inserted = 0
    for row in rdr:
        password = (row.get('password') or row.get('PASSWORD') or '').strip()
        sin     = (row.get('sin') or '').strip()
        dl      = (row.get('dl') or '').strip()
        first   = (row.get('first') or row.get('FIRST') or '').strip()
        last    = (row.get('last') or row.get('LAST') or '').strip()
        dob     = (row.get('dob') or row.get('DOB') or '').strip()
        address = (row.get('address') or row.get('ADRESSE') or '').strip()
        city    = (row.get('city') or row.get('CITY') or '').strip()
        postal  = (row.get('postal') or '').strip()
        base    = (row.get('base') or row.get('BASE') or 'FAKEPERSON').strip()
        email   = (row.get('email') or '').strip()
        phone   = (row.get('phone') or '').strip()
        price   = _parse_price(row.get('price') or row.get('PRICE') or '0')

        try:
            stock = int((row.get('stock') or '1').strip() or 1)
        except Exception:
            stock = 1

        year = ''
        m = re.search(r'(\d{4})', dob)
        if m: year = m.group(1)

        title = f"{(first + ' ' + last).strip().upper()} • {year} • {city.upper()}".strip()
        content_lines = [
            f"PASSWORD: {password}",
            f"SIN: {sin}",
            f"DL: {dl}",
            f"FIRST NAME: {first}",
            f"LAST NAME: {last}",
            f"DOB(DD/MM/YYYY): {dob}",
            f"ADRESSE: {address}",
            f"CITY: {city}",
            f"CODE POSTAL: {postal}",
            f"EMAIL: {email}",
            f"PHONE NUMBER: {phone}",
            f"BASE: {base}",
            f"PRICE: {price:.2f} CAD",
        ]
        content = "\n".join(content_lines)

        fields, values = [], []
        if 'title'    in cols: fields.append('title');    values.append(title)
        if 'content'  in cols: fields.append('content');  values.append(content)
        if 'price'    in cols: fields.append('price');    values.append(price)
        if 'tier'     in cols: fields.append('tier');     values.append(base)
        if 'city'     in cols: fields.append('city');     values.append(city)
        if 'year'     in cols: fields.append('year');     values.append(year)
        if 'stock'    in cols: fields.append('stock');    values.append(stock)
        if 'category' in cols: fields.append('category'); values.append('propro')
        if 'currency' in cols: fields.append('currency'); values.append('CAD')
        if 'is_active' in cols: fields.append('is_active'); values.append(1)

        q = f"INSERT INTO products ({', '.join(fields)}) VALUES ({', '.join(['?']*len(values))})"
        c.execute(q, values)
        inserted += 1

    db.commit()
    await update.message.reply_text(f"✅ Import terminé. {inserted} produit(s) ajouté(s).")
    return ConversationHandler.END

# ========================== ADMIN (sections ajustées) ==========================

async def admin_prod_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    db = _get_db_from_context(context)
    if not db:
        await update.callback_query.message.reply_text("DB indisponible.")
        return

    c = db.cursor()
    rows = c.execute(
        "SELECT id, title FROM products ORDER BY id DESC LIMIT 10"
    ).fetchall()
    if not rows:
        await update.callback_query.message.reply_text("Aucun produit à supprimer.")
        return

    kb = [[InlineKeyboardButton(f"#{pid} — {title[:40]}", callback_data=f"admin_prod_del_{pid}")]
          for (pid, title) in rows]
    kb.append([InlineKeyboardButton("🔙 Retour", callback_data="admin_products")])

    await update.callback_query.message.reply_text(
        "Sélectionne un produit à supprimer :",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def admin_prod_del_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    db = _get_db_from_context(context)
    if not db:
        await update.callback_query.message.reply_text("DB indisponible.")
        return

    c = db.cursor()
    pid = int(update.callback_query.data.split('_')[-1])
    c.execute("DELETE FROM products WHERE id=?", (pid,))
    db.commit()

    await update.callback_query.message.reply_text(f"🗑 Supprimé #{pid}.")
    # Recharger la liste
    await admin_prod_del(update, context)


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    q = update.callback_query
    try:
        await q.answer()

        users = get_users()
        if not users:
            await q.message.reply_text("Aucun utilisateur trouvé / No user found.")
            return

        keyboard = []
        for u in users:
            tid   = str(u[0])
            bal   = float(u[1] or 0.0)
            trec  = float(u[3] or 0.0)
            tier  = (u[4] or 'bronze')
            label_tier = FORFAITS.get(tier, {}).get('label', tier)
            label = f"ID:{tid[-5:]} | {label_tier} | {bal:.2f}$ | Ajouté: {trec:.2f}$"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"admin_adjust_{tid}")])

        # Évite les messages trop gros
        if not keyboard:
            await q.message.reply_text("Aucun utilisateur trouvé / No user found.")
            return
        if len(keyboard) > 80:
            keyboard = keyboard[:80]

        await q.message.reply_text(
            "Clique un utilisateur pour ajuster son solde :",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        log(f"admin_users error: {e}", level="error")
        await q.message.reply_text("⚠️ Erreur lors du chargement des utilisateurs.")


async def admin_adjust_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    cbdata = update.callback_query.data
    telegram_id = cbdata.replace("admin_adjust_", "")
    context.user_data["target_user"] = telegram_id

    keyboard = [
        [
            InlineKeyboardButton("+10$",  callback_data=f"admin_adjval_{telegram_id}_10"),
            InlineKeyboardButton("+100$", callback_data=f"admin_adjval_{telegram_id}_100"),
            InlineKeyboardButton("+250$", callback_data=f"admin_adjval_{telegram_id}_250"),
        ],
        [
            InlineKeyboardButton("-10$",  callback_data=f"admin_adjval_{telegram_id}_-10"),
            InlineKeyboardButton("-100$", callback_data=f"admin_adjval_{telegram_id}_-100"),
            InlineKeyboardButton("-250$", callback_data=f"admin_adjval_{telegram_id}_-250"),
        ],
        [InlineKeyboardButton("Montant personnalisé", callback_data=f"admin_customamount_{telegram_id}")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="admin_users")],
    ]
    await update.callback_query.message.reply_text(
        f"Quel ajustement pour l'utilisateur {telegram_id[-5:]} ?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_adjust_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    cbdata = update.callback_query.data
    _, _, telegram_id, amount = cbdata.split("_")
    amount = float(amount)

    new_balance, new_statut = credit_and_upgrade(telegram_id, amount)
    await update.callback_query.message.reply_text(f"✅ Solde mis à jour : {new_balance:.2f} $ CAD.")

    try:
        await app_telegram.bot.send_message(
            chat_id=int(telegram_id),
            text=f"💵 Votre solde a été ajusté de {amount:+.2f} $ CAD. Nouveau solde : {new_balance:.2f} $ CAD."
        )
    except Exception as e:
        await update.callback_query.message.reply_text(
            f"Impossible de notifier l'utilisateur {telegram_id} : {e}"
        )

    return ConversationHandler.END


async def admin_customamount_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    cbdata = update.callback_query.data
    telegram_id = cbdata.replace("admin_customamount_", "")
    context.user_data["target_user"] = telegram_id

    await update.callback_query.message.reply_text(
        "Entre le montant à ajouter (+) ou retirer (-) du solde :"
    )
    return ADMIN_AWAIT_AMOUNT


# --- admin_customamount_receive (message handler, pas de callback -> pas de q.answer) ---
async def admin_customamount_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard 1: only admin
    if str(update.effective_user.id) != ADMIN_ID:
        return
    # Guard 2: only when a target user has been set
    telegram_id = context.user_data.get("target_user")
    if not telegram_id:
        return

    try:
        amount = float(update.message.text.replace(",", "."))
    except Exception:
        await update.message.reply_text("Montant invalide, recommence.")
        return ADMIN_AWAIT_AMOUNT

    new_balance, new_statut = credit_and_upgrade(telegram_id, amount)
    await update.message.reply_text(f"✅ Solde mis à jour : {new_balance:.2f} $ CAD.")

    try:
        await app_telegram.bot.send_message(
            chat_id=int(telegram_id),
            text=f"💵 Votre solde a été ajusté de {amount:+.2f} $ CAD. Nouveau solde : {new_balance:.2f} $ CAD."
        )
    except Exception as e:
        await update.message.reply_text(f"Impossible de notifier l'utilisateur {telegram_id} : {e}")
    finally:
        context.user_data.pop("target_user", None)

    return ConversationHandler.END


async def admin_setstatut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    q = update.callback_query
    try:
        await q.answer()

        users = get_users()
        keyboard = []
        for u in users:
            tid   = str(u[0])
            tier  = (u[4] or 'bronze')
            label_tier = FORFAITS.get(tier, {}).get('label', tier)
            display = f"{tid[-5:]} {label_tier}"
            keyboard.append([InlineKeyboardButton(display, callback_data=f"admin_userstatut_{tid}")])

        if not keyboard:
            await q.message.reply_text("Aucun utilisateur trouvé / No user found.")
            return
        if len(keyboard) > 80:
            keyboard = keyboard[:80]

        await q.message.reply_text(
            "Sélectionner un utilisateur :",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        log(f"admin_setstatut error: {e}", level="error")
        await q.message.reply_text("⚠️ Erreur lors du chargement de la liste.")


async def admin_userstatut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    cbdata = update.callback_query.data
    telegram_id = cbdata.replace("admin_userstatut_", "")
    keyboard = [
        [InlineKeyboardButton(f"{FORFAITS['bronze']['label']}",   callback_data=f"admin_statut_{telegram_id}_bronze")],
        [InlineKeyboardButton(f"{FORFAITS['silver']['label']}",   callback_data=f"admin_statut_{telegram_id}_silver")],
        [InlineKeyboardButton(f"{FORFAITS['gold']['label']}",     callback_data=f"admin_statut_{telegram_id}_gold")],
        [InlineKeyboardButton(f"{FORFAITS['platinum']['label']}", callback_data=f"admin_statut_{telegram_id}_platinum")],
    ]
    await update.callback_query.message.reply_text(
        "Nouveau statut :",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_setstatut_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    cbdata = update.callback_query.data
    _, _, telegram_id, statut = cbdata.split("_")
    set_user_statut(telegram_id, statut)

    await update.callback_query.message.reply_text(
        f"✅ Statut modifié pour {telegram_id[-5:]} -> {FORFAITS[statut]['label']}"
    )


async def admin_hard_reboot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    await update.callback_query.message.reply_text("⏳ Redémarrage du bot en cours…")
    try:
        subprocess.Popen(["sudo", "systemctl", "restart", "telegrambot.service"])
    except Exception as e:
        await update.callback_query.message.reply_text(f"Erreur reboot : {e}")


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) != ADMIN_ID:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return

    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass

    keyboard = [
        [InlineKeyboardButton("👥 Utilisateurs", callback_data="admin_users")],
        [InlineKeyboardButton("🏷 Forfait utilisateur", callback_data="admin_setstatut")],
        [InlineKeyboardButton("🔁 Redémarrer le bot", callback_data="admin_hard_reboot")],
        [InlineKeyboardButton("🧱 Produits Pro's", callback_data="admin_products")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")],
    ]

    try:
        await q.message.edit_text("⚙️ Menu admin :", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        # Si l’edit échoue (message trop ancien), on envoie un nouveau
        await q.message.reply_text("⚙️ Menu admin :", reply_markup=InlineKeyboardMarkup(keyboard))

# ========================== IVR & SIGNALWIRE ==========================
# ========================== IVR & SIGNALWIRE ==========================
def is_system_open():
    tz = pytz.timezone("America/Toronto")
    now = datetime.now(tz)
    weekday = now.weekday()
    if weekday in [0, 1, 3, 4]:
        return (now.hour > 8 or (now.hour == 8 and now.minute >= 30)) and (now.hour < 16 or (now.hour == 16 and now.minute < 30))
    elif weekday == 2:
        return (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and (now.hour < 16 or (now.hour == 16 and now.minute < 30))
    return False


async def launch_parallel_calls(base_code, user_id, num_calls=10, fullname="", formatted="", batch_id=None):
    if batch_id is None:
        batch_id = f"{user_id}:{base_code}"

    key = f"{batch_id}:{base_code}"

    user_validation_status[key] = {
        "notified": False,
        "total": 0,
        "fullname": fullname,
        "formatted": formatted,
        "resolved": False,
        "batch_id": batch_id
    }

    log(f"\U0001f9e9 Initialisation du batch {batch_id} pour {num_calls} variantes...", user_id)

    async def make_call(variant):
        try:
            call = client.calls.create(
                to=DESTINATION_NUMBER,
                from_=SIGNALWIRE_NUMBER,
                url=f"{SERVER_URL}/twilio_handler?code={variant}&uid={user_id}&bid={batch_id}",
                record=True,
                recording_channels="dual"
            )
            sid = call.sid
            active_calls[sid] = {"user_id": user_id, "code": variant, "batch_id": batch_id}
            log(f"\ud83d\udcde Appel lanc\u00e9 → Variante {variant} | SID: {sid}", user_id)
        except Exception as e:
            log(f"\u274c Erreur SignalWire sur la variante {variant} : {e}", user_id, "error")

    tasks = []
    for i in range(num_calls):
        variant = f"{base_code}{i:02}"
        tasks.append(asyncio.create_task(make_call(variant)))
        await asyncio.sleep(1)

    if tasks:
        await asyncio.gather(*tasks)
        log(f"\ud83d\ude80 Tous les appels pour le batch {batch_id} ont \u00e9t\u00e9 lanc\u00e9s.", user_id)


async def cancel_all_calls(batch_id: str | None = None, user_id: int | str | None = None):
    loop = asyncio.get_running_loop()
    to_cancel = []
    for sid, info in list(active_calls.items()):
        try:
            if batch_id and info.get("batch_id") == batch_id:
                to_cancel.append((sid, info))
            elif (not batch_id) and user_id and str(info.get("user_id")) == str(user_id):
                to_cancel.append((sid, info))
            elif (not batch_id) and (not user_id):
                to_cancel.append((sid, info))
        except Exception:
            to_cancel.append((sid, info))

    if not to_cancel:
        log(f"Aucun appel à annuler (batch_id={batch_id}, user_id={user_id})", "SYSTEM")
        return 0

    canceled = 0

    def _complete_call_blocking(sid):
        try:
            client.calls(sid).update(status="completed")
            return None
        except Exception as e:
            return str(e)

    for sid, info in to_cancel:
        err = await loop.run_in_executor(None, _complete_call_blocking, sid)
        if err is None:
            canceled += 1
            log(f"Appel annulé (SID={sid}) — batch={info.get('batch_id')} code={info.get('code')}", info.get("user_id", "SYSTEM"))
        else:
            log(f"Erreur annulation appel SID={sid} : {err}", info.get("user_id", "SYSTEM"), "error")
        try:
            active_calls.pop(sid, None)
        except Exception:
            pass

    if batch_id:
        br = batch_runs.get(batch_id)
        if br and not br.get("notified"):
            br["notified"] = True
            br["resolved"] = br.get("total", 0)
            user_id_for_notify = None
            for _, info in to_cancel:
                if info.get("user_id"):
                    user_id_for_notify = info.get("user_id")
                    break
            try:
                if user_id_for_notify and globals().get("app_telegram") and getattr(app_telegram, "bot", None):
                    await app_telegram.bot.send_message(chat_id=int(user_id_for_notify),
                                                       text="⛔️ Le lot d'appels a été annulé manuellement.")
            except Exception as e:
                log(f"Impossible de notifier user {user_id_for_notify} après annulation: {e}", "SYSTEM", "warning")

    log(f"cancel_all_calls: {canceled} appel(s) traités (batch_id={batch_id}, user_id={user_id})", "SYSTEM")
    return canceled

@app.route("/twilio_handler", methods=["GET", "POST"], endpoint="twilio_handler_main")
def twilio_handler():
    code = request.args.get("code", "")
    uid = request.args.get("uid")
    bid = request.args.get("bid")
    call_sid = request.form.get("CallSid", f"call_{datetime.now().timestamp()}")

    print(f"📞 Appel reçu — CallSid: {call_sid} | UID: {uid} | Code: {code} | BID: {bid}")

    active_calls[call_sid] = {
        "user_id": uid,
        "code": code,
        "batch_id": bid
    }

    r = VoiceResponse()

    if is_system_open():
        print("🕐 SAAQ ouverte — Menu 4-4-6 déclenché")
        r.pause(length=33)
        r.play(digits="4")
        r.pause(length=3)
        r.play(digits="4")
        r.pause(length=3)
        r.play(digits="6")
        r.pause(length=41)
    else:
        print("🕐 SAAQ fermée — Menu 1-1 déclenché")
        r.pause(length=40)
        r.play(digits="1")
        r.pause(length=3)
        r.play(digits="1")
        r.pause(length=45)

    redirect_url = f"{SERVER_URL}/composer_code?code={code}&uid={uid}"
    print(f"🔁 Redirection vers: {redirect_url}")
    r.redirect(redirect_url, method="POST")

    return Response(str(r), mimetype="text/xml")

@app.route("/composer_code", methods=["POST"], endpoint="composer_code_main")
def composer_code():
    code = request.args.get("code", "")
    uid = request.args.get("uid")
    code_saaq = convertir_en_code_saaq(code)
    r = VoiceResponse()
    for digit in code_saaq:
        r.play(digits=digit)
        r.pause(length=0.5)
    gather = r.gather(
        input="speech",
        language="fr-FR",
        timeout=15,
        speech_timeout="auto",
        action=f"{SERVER_URL}/analyze_response",
        method="POST"
    )
    gather.say("Merci. Veuillez répondre après le signal sonore.", language="fr-FR")
    return Response(str(r), mimetype="text/xml")

@app.route("/analyze_response", methods=["POST"], endpoint="analyze_response_main")
def analyze_response():
    from twilio.twiml.voice_response import VoiceResponse
    import re

    # === Logs d’entrée ===
    speech_raw = request.form.get("SpeechResult", "")
    speech = (speech_raw or "").lower().strip()
    call_sid = request.form.get("CallSid")
    logger.info(f"📞 [analyze_response] reçu — CallSid: {call_sid}")
    logger.info(f"🗣️ Résultat vocal brut: {speech_raw}")
    logger.info(f"🧽 Nettoyé: {speech}")

    # === Vérif session ===
    if call_sid not in active_calls:
        logger.warning(f"❌ CallSid inconnu : {call_sid}")
        return Response("<Response><Hangup/></Response>", mimetype="text/xml")

    current_call = active_calls[call_sid]
    user_id = None
    try:
        user_id = int(current_call["user_id"]) if current_call.get("user_id") else None
    except Exception as e:
        logger.warning(f"⚠️ Erreur conversion user_id: {e}")

    variant = current_call.get("code", "")
    batch_id = current_call.get("batch_id")
    if user_id is None or not variant or len(variant) < 13 or not batch_id:
        logger.error(f"❌ Données manquantes (user_id={user_id}, variant={variant}, batch_id={batch_id})")
        return Response("<Response><Hangup/></Response>", mimetype="text/xml")

    base_code = variant[:-2]
    key = f"{batch_id}:{base_code}"
    if key not in user_validation_status:
        user_validation_status[key] = {
            "notified": False, "total": 0, "fullname": "",
            "formatted": "", "resolved": False, "batch_id": batch_id
        }

    state = user_validation_status[key]
    fullname = state.get("fullname", "")
    formatted = state.get("formatted", "")
    logger.info(f"🧾 Analyse du code {variant} pour {fullname} / batch={batch_id}")

    # === Détection vocale ===
    neg_patterns = [
        r"\binvalide\b", r"\bnon\s+valide\b", r"\bpas\s+valide\b",
        r"n[\'’]est\s+pas\s+valide", r"\binvalid\b", r"\bnot\s+valid\b",
    ]
    pos_pattern = r"\b(valide|valid)\b"

    is_negative = any(re.search(p, speech) for p in neg_patterns)
    is_positive = bool(re.search(pos_pattern, speech))
    valid = is_positive and not is_negative

    logger.info(f"🔎 Interprétation — positif={is_positive}, négatif={is_negative}, valid={valid}")

    # === Sous-fonction notification ===
    def _maybe_finish_batch(add_result_text=None):
        async def _notify_serialized():
            br = batch_runs.get(batch_id)
            if not br:
                return
            async with br.setdefault("lock", asyncio.Lock()):
                if add_result_text:
                    await app_telegram.bot.send_message(chat_id=user_id, text=add_result_text)
                if not state["resolved"]:
                    state["resolved"] = True
                    br["resolved"] += 1
                    if br["resolved"] >= br["total"] and not br["notified"]:
                        br["notified"] = True
                        await app_telegram.bot.send_message(chat_id=user_id, text="🔓 Fin du décryptage.")
                        await show_main_menu(user_id)
        asyncio.run_coroutine_threadsafe(_notify_serialized(), bot_loop)

    try:
        # === Cas permis valide ===
        if valid and not state["notified"]:
            final_suffix = re.sub(r"\D", "", variant[-2:]).rjust(2, "0")
            final_permis = (formatted.replace("**", final_suffix)
                            if formatted else f"{variant[:5]}-{variant[5:11]}-{final_suffix}")
            logger.info(f"✅ Permis VALIDÉ : {final_permis}")

            state["notified"] = True
            save_permit_history(user_id, fullname, final_permis, "valide")
            _maybe_finish_batch(add_result_text=msg(user_id, "validation_ok", permis=final_permis, fullname=fullname))

        # === Cas invalide ===
        else:
            state["total"] += 1
            logger.info(f"❌ Permis rejeté (tentative {state['total']}/10)")
            if state["total"] >= 10 and not state["notified"]:
                state["notified"] = True
                save_permit_history(user_id, fullname, None, "aucun")
                _maybe_finish_batch(add_result_text=msg(user_id, "aucun_permis", fullname=fullname))

        # === Sécurité : redirection pour éviter coupure SignalWire ===
        r = VoiceResponse()
        r.pause(length=2)
        r.redirect(f"{SERVER_URL}/composer_code?uid={user_id}", method="POST")
        logger.info("➡️ Redirection vers composer_code pour continuer le cycle d’appel.")
        return Response(str(r), mimetype="text/xml")

    except Exception as e:
        logger.exception(f"❌ Exception analyze_response: {e}")
        return Response("<Response><Hangup/></Response>", mimetype="text/xml")

    finally:
        try:
            client.calls(call_sid).update(status="completed")
        except Exception as e:
            logger.warning(f"⚠️ Erreur fermeture appel: {e}")
        active_calls.pop(call_sid, None)

# ========================== MENU/ROUTEUR CALLBACKS ==========================
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    print(f"[DBG] menu_handler triggered with data={q.data}", flush=True)

    data = q.data

    if data == "menu_accueil":
        return await goto_menu(update, context)

    if data == "propro":
        context.user_data["prod_tier"] = None
        return await show_products(update, context, page=0, tier=None)

    if data.startswith("prod:page:"):
        page = int(data.split(":")[2])
        tier = context.user_data.get("prod_tier")
        return await show_products(update, context, page=page, tier=tier)

    if data == "noop":
        return

    try:
        print(f"[DBG] Unhandled callback_data: {data}", flush=True)
    except Exception:
        pass
    try:
        await q.answer("Action non gérée (voir logs)", show_alert=False)
    except Exception:
        pass

async def hist_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await hist_menu(update, context)
    except Exception as e:
        log(f"hist_view_callback error: {e}", level="error")

if __name__ == "__main__":
# --- DB ---
    db_conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    shop_helpers.ensure_shop_tables(db_conn)
    init_db()  # keep this if you already had it
    ensure_verifications_table()

    # --- Launch Flask (IVR) on PORT 5001 ---
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False),
        daemon=True
    )
    flask_thread.start()

    # --- Telegram app ---
    app_telegram = Application.builder().token(TELEGRAM_TOKEN).build()

try:
    ITEM_UNIQUE_MODE
except NameError:
    ITEM_UNIQUE_MODE = False  

from telegram import InlineKeyboardMarkup, InlineKeyboardButton

def build_categories_kb():
    buttons = [
        [InlineKeyboardButton("propro", callback_data="cat:propro")],
        [InlineKeyboardButton("permis", callback_data="cat:permis")],
    ]
    return InlineKeyboardMarkup(buttons)

def build_products_kb(db_conn, category: str):
    c = db_conn.cursor()
    if ITEM_UNIQUE_MODE:
        c.execute("SELECT id, title, price FROM products WHERE category=? ORDER BY id DESC", (category,))
        rows = [(r[0], r[1], r[2], None) for r in c.fetchall()]
    else:
        c.execute("SELECT id, title, price, stock FROM products WHERE category=? AND stock > 0 ORDER BY id DESC", (category,))
        rows = c.fetchall()

    buttons = []
    for r in rows:
        pid, title, price = r[0], r[1], float(r[2])
        label = f"{title} — {price:.2f}$"
        buttons.append([InlineKeyboardButton(label, callback_data=f"buy:{pid}")])

    buttons.append([
        InlineKeyboardButton("⬅️ Retour catégories", callback_data="back:cats"),
        InlineKeyboardButton("🛒 Voir le panier", callback_data="cart:view")
    ])
    return InlineKeyboardMarkup(buttons)

async def list_products(update, context, category: str):
    # Récupère une connexion DB (adapte si besoin)
    db = context.application.bot_data.get("db_conn") or context.bot_data.get("db_conn")
    kb = build_products_kb(db, category)
    txt = f"🗂️ *{category.upper()}* — Choisis un item :"
    q = getattr(update, "callback_query", None)
    if q:
        await q.edit_message_text(txt, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(txt, reply_markup=kb, parse_mode="Markdown")

async def on_category(update, context):
    q = update.callback_query
    await q.answer()
    _, category = q.data.split(":", 1)
    context.user_data["category"] = category
    await list_products(update, context, category)

async def on_back_cats(update, context):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🗂️ *Catégories*", parse_mode="Markdown", reply_markup=build_categories_kb())

async def buy_product(update, context):
    q = update.callback_query
    await q.answer()
    db = context.bot_data.get("db") or context.application.bot_data.get("db")
    c  = db.cursor()

    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        await q.edit_message_text("❌ Requête invalide.")
        return

    if ITEM_UNIQUE_MODE:
        c.execute("SELECT id, title, price, category FROM products WHERE id=?", (pid,))
        row = c.fetchone()
        if not row:
            await q.edit_message_text("❌ Cet item n'existe plus.")
            return
        _id, title, price, category = row
        # TODO: ajouter au panier si tu en as un (context.user_data['cart'], etc.)
        c.execute("DELETE FROM products WHERE id=?", (pid,))
        db.commit()
    else:
        c.execute("SELECT id, title, price, stock, category FROM products WHERE id=?", (pid,))
        row = c.fetchone()
        if not row:
            await q.edit_message_text("❌ Cet item n'existe plus.")
            return
        _id, title, price, stock, category = row
        if stock is None or int(stock) <= 0:
            await q.edit_message_text("⚠️ Rupture de stock.")
            return
        # TODO: ajouter au panier si tu en as un
        c.execute("UPDATE products SET stock = stock - 1 WHERE id=? AND stock > 0", (pid,))
        db.commit()

    # Reconstruit la liste pour faire disparaître l'item si nécessaire
    kb = build_products_kb(db, category)
    await q.edit_message_text(
        text=f"✅ *{title}* ajouté.\n\n🗂️ *{category}* — items disponibles :",
        parse_mode="Markdown",
        reply_markup=kb
    )
async def delete_history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()

        # Récupère l'ID de l'achat à supprimer
        purchase_id = int(query.data.split("_")[-1])
        user_id = query.from_user.id

        # Supprime l’achat de la DB
        con = sqlite3.connect(DB_NAME)
        cur = con.cursor()
        cur.execute("DELETE FROM purchases WHERE id = ? AND user_id = ?", (purchase_id, str(user_id)))
        con.commit()
        con.close()

        # Envoie confirmation
        await query.edit_message_text("🗑 Achat supprimé avec succès.")

    except Exception as e:
        print(f"Erreur dans delete_history_handler: {e}")
        await update.effective_message.reply_text("❌ Une erreur est survenue.")

# === Navigation / Historique ===
app_telegram.add_handler(CallbackQueryHandler(on_back_cats, pattern=r"^back:cats$"))
app_telegram.add_handler(CallbackQueryHandler(on_category,  pattern=r"^cat:.+$"))
app_telegram.add_handler(CallbackQueryHandler(hist_view_callback, pattern=r"^hist:view$"))
app_telegram.add_handler(CallbackQueryHandler(hist_pros,     pattern=r"^hist:pros(:page:\d+)?$"))
app_telegram.add_handler(CallbackQueryHandler(hist_permis,   pattern=r"^hist:permis(:page:\d+)?$"))
app_telegram.add_handler(CallbackQueryHandler(close_history, pattern=r"^close_history$"))
app_telegram.add_handler(CallbackQueryHandler(delete_history_handler, pattern=r"^delete_history_\d+$"))

# === Attachement des dépendances globales ===
app_telegram.bot_data['db_conn'] = db_conn
app_telegram.bot_data['db'] = db_conn
app_telegram.bot_data['get_user_balance'] = get_user_balance
app_telegram.bot_data['update_user_balance'] = update_user_balance
app_telegram.bot_data['create_transaction'] = create_transaction

# === Commandes de base ===
app_telegram.add_handler(CommandHandler("start", start_cmd))
app_telegram.add_handler(CommandHandler("historique", shop_helpers.cmd_historique))
app_telegram.add_handler(CommandHandler("panier",     shop_helpers.cmd_panier))

# === Conversation principale ===
conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("verifier", start_verifier),
        CallbackQueryHandler(start_verifier_main, pattern="^start_verifier_main$"),
    ],
    states={
        ASK_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_qty)],
        ASK_MODE: [
            CallbackQueryHandler(choose_mode_manual, pattern="mode_manual$"),
            CallbackQueryHandler(choose_mode_csv, pattern="mode_csv$"),
        ],
        MANUAL_PRENOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_receive_prenom)],
        MANUAL_NOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_receive_nom)],
        MANUAL_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_receive_date)],
        CSV_WAIT: [MessageHandler(filters.Document.ALL & ~filters.COMMAND, csv_receive_file)],
        BULK_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, bulk_confirm)],

        ASK_PRENOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_prenom)],
        ASK_NOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_nom)],
        ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_date)],
        CONFIRM_VERIF: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_permis)],
    },
    fallbacks=[CallbackQueryHandler(goto_menu, pattern="^menu_accueil$")]
)


app_telegram.add_handler(conv_handler)

# === Filtres catalogue ===
app_telegram.add_handler(CallbackQueryHandler(filter_open,   pattern="^filter_open$"))
app_telegram.add_handler(CallbackQueryHandler(filter_select, pattern="^filter:(name|city|base|price|year)$"))
app_telegram.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_filter_text, block=False),group=10,)

# === Produits ===
app_telegram.add_handler(CallbackQueryHandler(shop_helpers.handle_preview_callback, pattern=r"^prod:preview:\d+$"),group=-1)
app_telegram.add_handler(CallbackQueryHandler(shop_helpers.handle_buy_callback,     pattern=r"^buy:\d+$"),group=-1)
app_telegram.add_handler(CallbackQueryHandler(shop_helpers.handle_view_callback,    pattern=r"^prod:view:\d+$"),group=-1)

# === Panier ===
app_telegram.add_handler(CallbackQueryHandler(shop_helpers.cart_add_callback,      pattern=r"^cart:add:\d+$"), group=-1)
app_telegram.add_handler(CallbackQueryHandler(shop_helpers.cart_view_callback,     pattern=r"^cart:view$"),    group=-1)
app_telegram.add_handler(CallbackQueryHandler(shop_helpers.cart_clear_callback,    pattern=r"^cart:clear$"),   group=-1)
app_telegram.add_handler(CallbackQueryHandler(shop_helpers.cart_checkout_callback, pattern=r"^cart:checkout$"),group=-1)

# === Boutons simples ===
app_telegram.add_handler(CallbackQueryHandler(callback_show_my_id,    pattern="^show_my_id$"))
app_telegram.add_handler(CallbackQueryHandler(add_balance_start,      pattern="^add_balance$"))
app_telegram.add_handler(CallbackQueryHandler(choose_lang,            pattern="^choose_lang$"))
app_telegram.add_handler(CallbackQueryHandler(set_lang_fr,            pattern="^set_lang_fr$"))
app_telegram.add_handler(CallbackQueryHandler(set_lang_en,            pattern="^set_lang_en$"))
app_telegram.add_handler(CallbackQueryHandler(callback_check_balance, pattern="^check_balance$"))
app_telegram.add_handler(CallbackQueryHandler(callback_support,       pattern="^support$"))
app_telegram.add_handler(CallbackQueryHandler(callback_faq,           pattern="^faq$"))

# === Admin ===
app_telegram.add_handler(CallbackQueryHandler(admin_prod_del_confirm,   pattern="^admin_prod_del_\d+$"))
app_telegram.add_handler(CallbackQueryHandler(admin_prod_del,           pattern="^admin_prod_del$"))
app_telegram.add_handler(CallbackQueryHandler(admin_prod_csv_start,     pattern="^admin_prod_csv$"))
app_telegram.add_handler(CallbackQueryHandler(admin_prod_add_start,     pattern="^admin_prod_add$"))
app_telegram.add_handler(CallbackQueryHandler(admin_prod_list,          pattern="^admin_prod_list$"))
app_telegram.add_handler(CallbackQueryHandler(admin_products,           pattern="^admin_products$"))
app_telegram.add_handler(CallbackQueryHandler(admin_menu,               pattern="^admin_menu$"))
app_telegram.add_handler(CallbackQueryHandler(admin_users,              pattern="^admin_users$"))
app_telegram.add_handler(CallbackQueryHandler(admin_adjust_user,        pattern="^admin_adjust_.*$"))
app_telegram.add_handler(CallbackQueryHandler(admin_adjust_value,       pattern="^admin_adjval_.*$"))
app_telegram.add_handler(CallbackQueryHandler(admin_customamount_start, pattern="^admin_customamount_.*$"))
app_telegram.add_handler(CallbackQueryHandler(admin_setstatut,          pattern="^admin_setstatut$"))
app_telegram.add_handler(CallbackQueryHandler(admin_userstatut,         pattern="^admin_userstatut_.*$"))
app_telegram.add_handler(CallbackQueryHandler(admin_setstatut_final,    pattern="^admin_statut_.*$"))
app_telegram.add_handler(CallbackQueryHandler(admin_hard_reboot,        pattern="^admin_hard_reboot$"))

from telegram.ext import filters, MessageHandler
app_telegram.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_prod_add_receive),     group=21)
app_telegram.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_customamount_receive), group=22)

app_telegram.add_handler(CallbackQueryHandler(menu_handler))




# === Boucle principale sécurisée ===
try:
    _ptb = globals().get('app_telegram') or globals().get('application')
    if _ptb and hasattr(_ptb, 'run_polling'):
        try:
            bot_loop = asyncio.get_event_loop()
        except RuntimeError:
            bot_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(bot_loop)
        _ptb.run_polling(close_loop=False)
except Exception as _e:
    pass

import time
while True:
    time.sleep(3600)

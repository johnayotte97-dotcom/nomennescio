import re
import base58
from bip_utils import Bip32Secp256k1, P2WPKHAddr
import sqlite3
from hdwallet import HDWallet
from hdwallet.cryptocurrencies import Bitcoin as BTC
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
import time
import requests
import sqlite3
import subprocess
from datetime import datetime
import pytz
from dotenv import load_dotenv
from flask import Flask, request, Response
from signalwire.rest import Client as SignalWireClient
from twilio.twiml.voice_response import VoiceResponse
from mnemonic import Mnemonic
import tickets  # Importe notre nouveau fichier
import random
import string


from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ConversationHandler, ContextTypes, CallbackQueryHandler
)
# ================= SECURITY HEARTBEAT =================
# Ton lien Healthchecks personnel
HEARTBEAT_URL = "https://hc-ping.com/e02d463d-737c-4455-b12e-d307eb7313e4"

def start_heartbeat():
    while True:
        try:
            # Envoie le signal au site
            requests.get(HEARTBEAT_URL, timeout=10)
        except Exception:
            pass
        time.sleep(60)

# Démarrer la surveillance en arrière-plan
threading.Thread(target=start_heartbeat, daemon=True).start()
# ======================================================

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
FAKEID_API_KEY = os.environ.get("FAKEID_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
SW_PROJECT_ID = os.environ.get("SW_PROJECT_ID")
SW_TOKEN = os.environ.get("SW_TOKEN")
SW_SPACE = os.environ.get("SIGNALWIRE_SPACE_URL")
os.environ["SIGNALWIRE_SPACE_URL"] = SW_SPACE
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
DESTINATION_NUMBER = os.environ.get("DESTINATION_NUMBER")
SIGNALWIRE_NUMBER = os.environ.get("SIGNALWIRE_NUMBER")
SERVER_URL = os.environ.get("SERVER_URL")
ADMIN_IDS = ["7573645008", "8409831904"]
CHANNEL_LOGS = "-1003589564052"
NUMVERIFY_API_KEY = os.environ.get("NUMVERIFY_API_KEY")
DB_NAME = os.environ.get("DB_NAME", "/home/johnmsaaq/bot-nomen/database.db")

client = SignalWireClient(SW_PROJECT_ID, SW_TOKEN)
app = Flask(__name__)

# ========================== CONSTANTES ==========================
(ID_MENU_START, ID_CAT_VIEW, ID_PROD_VIEW, ID_ASK_QTY, ID_CONFIRM_BUY,
 ID_ASK_NAME, ID_ASK_DOB, 
 ID_ASK_STREET, ID_ASK_CITY, ID_ASK_ZIP, ID_CONFIRM_ADDR,
 ID_ASK_DOC_EMPLOYER, ID_ASK_DOC_JOB, ID_ASK_DOC_ADDR, ID_CHOOSE_INCOME_MODE,
 ID_ASK_DOC_HOURS, ID_ASK_DOC_RATE, ID_ASK_DOC_SIN,
 ID_ASK_HEIGHT, ID_ASK_EYES, ID_ASK_PHOTO,
 # --- NOUVELLES ÉTAPES AJOUTÉES ---
 ID_ASK_LASTNAME, ID_ASK_ISSUE, ID_ASK_EXPIRY, 
 ID_ASK_DL_NUM, ID_ASK_REF_NUM, ID_ASK_SEX, ID_CONFIRM_SUMMARY
 ) = range(3000, 3028) # On augmente le range à 3028
# Étapes Conversation (1 permis historique)
ASK_PRENOM, ASK_NOM, ASK_DATE, CONFIRM_VERIF = range(4)

# Flow multi-permis
ASK_QTY, ASK_MODE, MANUAL_PRENOM, MANUAL_NOM, MANUAL_DATE, CSV_WAIT, BULK_CONFIRM = range(100, 107)

# Admin
ADMIN_AWAIT_AMOUNT = 200
ADMIN_WAIT_PRODUCT_TEXT = 201
ADMIN_WAIT_CSV = 202
ADMIN_WAIT_SEARCH_ID = 203
HISTORY_FILTER_CHOICE, HISTORY_FILTER_INPUT = range(300, 302)
ADMIN_IVR_AWAIT_VALUE = 400 # Nouvelle constante
IVR_TIMINGS_FILE = "ivr_timings.json"
CATALOG_FILTER_MAIN, CATALOG_FILTER_AWAIT_VALUE = range(500, 502)
CCS_FILTER_MAIN, CCS_FILTER_AWAIT_VALUE = range(600, 602)
WAIT_AMOUNT_CRYPTO = 800
ADMIN_XPUB = os.environ.get("ADMIN_XPUB")
ID_AUTH_WAIT_PIN_CREATE = 1500  # Création du PIN
ID_AUTH_WAIT_PIN_LOGIN = 1501  # Connexion (Entrée du PIN)
ID_AUTH_WAIT_SEED = 1502       # Import d'un wallet existant
SELECT_TOOL = 900
WAIT_HLR_NUMBER = 901
ID_EDIT_MENU, ID_EDIT_INPUT = range(4000, 4002)

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
    
    # 1. Création des tables si elles n'existent pas
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT UNIQUE,
            telegram_id TEXT,
            username TEXT,
            seed_phrase TEXT,
            pin_code TEXT,
            balance REAL DEFAULT 0.0,
            banned INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            lang TEXT DEFAULT 'fr',
            total_recharge REAL DEFAULT 0,
            forfait TEXT DEFAULT 'bronze'
        )
    """)
    
    # --- PATCH AUTOMATIQUE DES COLONNES MANQUANTES ---
    # On vérifie si les colonnes critiques existent, sinon on les ajoute
    try:
        cur.execute("ALTER TABLE users ADD COLUMN user_id TEXT")
        print("✅ PATCH DB: Colonne 'user_id' ajoutée.")
        # Migration des données pour ne pas perdre les comptes
        cur.execute("UPDATE users SET user_id = telegram_id WHERE user_id IS NULL")
    except sqlite3.OperationalError:
        pass # La colonne existe déjà, on ignore

    try:
        cur.execute("ALTER TABLE users ADD COLUMN seed_phrase TEXT")
        print("✅ PATCH DB: Colonne 'seed_phrase' ajoutée.")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE users ADD COLUMN pin_code TEXT")
        print("✅ PATCH DB: Colonne 'pin_code' ajoutée.")
    except sqlite3.OperationalError:
        pass
        
    try:
        cur.execute("ALTER TABLE users ADD COLUMN username TEXT")
    except sqlite3.OperationalError:
        pass
    # ---------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            order_id TEXT PRIMARY KEY,
            telegram_id TEXT,
            amount REAL,
            status TEXT DEFAULT 'pending',
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            is_active INTEGER DEFAULT 1,
            content TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS lottery (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            tickets INTEGER DEFAULT 0
        )
    """)

    con.commit()
    con.close()
    log("DB initialized (V2.2 Auto-Patch Ready)", "SYSTEM")

def patch_db_tickets():
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    try:
        cur.execute("ALTER TABLE support_tickets ADD COLUMN message TEXT")
        cur.execute("ALTER TABLE support_tickets ADD COLUMN category TEXT")
        cur.execute("ALTER TABLE support_tickets ADD COLUMN username TEXT")
    except:
        pass 
    con.commit()
    con.close()

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
        [InlineKeyboardButton("🪪 ID/Docs", callback_data="id_menu_entry")],
        [InlineKeyboardButton("💳 Cc's", callback_data="ccs_catalog_start")],
        [InlineKeyboardButton("👥 Pro's", callback_data="propro")],
        [InlineKeyboardButton("Tools ⚒️", callback_data="section_tools")],
        [InlineKeyboardButton("🛒 Panier", callback_data="cart:view")],
        [InlineKeyboardButton("📜 Historique", callback_data="hist:view")],
        [InlineKeyboardButton("🚗 Vérifier mon permis" if lang == "fr" else "🚗 Check my license", callback_data="start_verifier_main")],
        [InlineKeyboardButton("💳 Recharger" if lang == "fr" else "💳 Top up", callback_data="add_balance")],
        [InlineKeyboardButton("🌐 Langue/Language", callback_data="choose_lang")],
        [InlineKeyboardButton("📣 Channel", callback_data="join_private_channel")],
        [InlineKeyboardButton("📞 Support", callback_data="support")],
        [InlineKeyboardButton("📚 FAQ", callback_data="faq")],
        [InlineKeyboardButton("🔒 Log Out", callback_data="auth_logout")],
    ]
    if str(user_id) in ADMIN_IDS:
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

def _get_products_optimized(category, page=0, per_page=2, filters=None, tier=None):
    """
    Moteur de recherche SQL V5 (Industriel).
    """
    con = sqlite3.connect(DB_NAME)
    con.row_factory = sqlite3.Row  # IMPORTANT pour éviter les erreurs d'accès
    cur = con.cursor()
    
    # 1. Base
    conditions = ["category=?", "is_active=1", "stock>0"]
    params = [category]
    
    if tier:
        conditions.append("tier=?")
        params.append(tier)
        
    # 2. Filtres
    if filters:
        # PRIX (Nettoyage automatique "2$" -> 2.0)
        if filters.get('price'):
            try:
                raw = str(filters['price']).replace(',', '.').replace('$', '').strip()
                max_price = float(raw)
                conditions.append("price <= ?")
                params.append(max_price)
            except: pass 

        # ANNÉE / NOM / VILLE (Recherche large)
        for key in ['year', 'name', 'city', 'base']:
            if filters.get(key):
                val = filters[key].strip()
                conditions.append("(title LIKE ? OR content LIKE ?)")
                params.append(f"%{val}%")
                params.append(f"%{val}%")

        # BINS (Spécifique Cc's)
        if filters.get('bins'):
             val = filters['bins'].strip()
             conditions.append("content LIKE ?")
             params.append(f"%{val}%") # Cherche le BIN dans le contenu

    where_sql = " AND ".join(conditions)

    try:
        # 3. Exécution
        count_query = f"SELECT COUNT(*) FROM products WHERE {where_sql}"
        cur.execute(count_query, params)
        total_count = cur.fetchone()[0]

        data_query = f"SELECT * FROM products WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?"
        full_params = params + [per_page, page * per_page]
        rows = cur.execute(data_query, full_params).fetchall()
        con.close()

        # 4. Conversion propre en dict
        prods = []
        for row in rows:
            p = dict(row)
            prods.append({
                "id": p['id'], 
                "title": p['title'], 
                "price": float(p['price'] or 0),
                "currency": p['currency'] or "CAD", 
                "stock": p['stock'], 
                "tier": p['tier'], 
                "category": category, 
                "content": p['content']
            })
            
        return prods, total_count

    except Exception as e:
        print(f"[SQL ERROR] {e}")
        try: con.close()
        except: pass
        return [], 0

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


# REMPLACE la fonction show_products (ligne 607) par CELLE-CI :

async def show_products(update, context, page=0, tier=None, from_filter=False):
    # Imports locaux pour garantir le fonctionnement
    import asyncio 
    import os
    
    query = getattr(update, "callback_query", None)
    chat_id = query.message.chat_id if query else update.effective_chat.id
    
    # 1. UX : ON AFFICHE LE CHARGEMENT TOUT DE SUITE
    loading_msg = None
    try:
        # On envoie le message AVANT de supprimer l'ancien menu
        loading_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Recherche en cours...")
        
        # --- ASTUCE UX : PETITE PAUSE ---
        # On force une pause de 0.5s pour que l'utilisateur voie le message
        # Sinon c'est trop rapide et ça fait "clignoter" l'écran
        await asyncio.sleep(0.5) 
    except: pass

    # 2. Gestion de la suppression de l'ancien message (Menu ou Filtre)
    if query:
        try: await query.answer()
        except: pass
        if not from_filter:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
            except: pass

    # 3. Nettoyage des listes de messages en arrière-plan
    try:
        msgs_to_del = []
        if from_filter:
            msgs_to_del += context.user_data.pop("filter_fiches_msg_ids", [])
            msgs_to_del += context.user_data.pop("filter_msgs", [])
        else:
            msgs_to_del += CATALOG_MSGS.pop(chat_id, [])
            msgs_to_del += context.user_data.pop("filter_msgs", [])
        
        for mid in set(msgs_to_del):
            try: await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except: pass
    except: pass

    # 4. Exécution de la Recherche SQL
    try:
        filters = context.user_data.get('active_filters', {})
        PER_PAGE = 2
        chunk, total_items = _get_products_optimized(category="propro", page=page, per_page=PER_PAGE, filters=filters, tier=tier)
    except Exception as e:
        # Si ça plante, on le dit
        if loading_msg:
            try: await loading_msg.edit_text(f"⚠️ Erreur: {e}")
            except: pass
        return

    # 5. On supprime le message "Recherche..." maintenant que c'est prêt
    if loading_msg:
        try: await loading_msg.delete()
        except: pass

    # Calculs de page
    total_pages = max(1, (total_items + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    sent_ids = []

    # 6. Si aucun résultat
    if not chunk:
        text = "❌ Aucun produit trouvé."
        kb = _build_filter_menu(context, page_info=None) if from_filter else InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]])
        m = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        if from_filter: context.user_data['filter_msgs'] = [m.message_id]
        else: CATALOG_MSGS[chat_id] = [m.message_id]
        return

    # 7. Affichage des fiches
    for p in chunk:
        title = str(p.get('title', 'Unknown')).replace("*", "").replace("`", "")
        base = str(p.get('tier', 'N/A')).replace("*", "")
        price = p.get('price', 0.0)
        
        txt = f"📦 *PROPRO*\n━━━━━━━━━━━━━━━\n👤 **NOM** : `{title}`\n📂 **BASE** : `{base}`\n💰 **PRIX** : `{price:.2f} CAD`"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Buy Now", callback_data=f"buy:{p['id']}"), InlineKeyboardButton("🛒 Add", callback_data=f"cart:add:{p['id']}") ]])
        try:
            m = await context.bot.send_message(chat_id=chat_id, text=txt, reply_markup=kb, parse_mode="Markdown")
            sent_ids.append(m.message_id)
        except: 
            try:
                m = await context.bot.send_message(chat_id=chat_id, text=txt.replace('*',''), reply_markup=kb)
                sent_ids.append(m.message_id)
            except: pass

    # 8. Menu Navigation (Bas de page)
    if from_filter:
        context.user_data['filter_fiches_msg_ids'] = sent_ids
        kb = _build_filter_menu(context, page_info={'page': page, 'total_pages': total_pages})
        m = await context.bot.send_message(chat_id=chat_id, text=f"🔎 Résultats : {total_items} (Page {page+1}/{total_pages})", reply_markup=kb)
        context.user_data['filter_msgs'] = [m.message_id]
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Filter", callback_data="filter_open")],
            [InlineKeyboardButton("«", callback_data=f"prod:page:{max(0, page-1)}"), InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"), InlineKeyboardButton("»", callback_data=f"prod:page:{min(total_pages-1, page+1)}")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
        ])
        m = await context.bot.send_message(chat_id=chat_id, text=f"📊 Catalogue ({total_items} produits)", reply_markup=kb)
        sent_ids.append(m.message_id)
        CATALOG_MSGS[chat_id] = sent_ids

def _build_filter_menu(context: ContextTypes.DEFAULT_TYPE, page_info: dict = None) -> InlineKeyboardMarkup:
    """Construit le menu de filtre dynamique, AVEC pagination optionnelle."""
    filters = context.user_data.get('pending_filters', {})
    
    def get_label(key, default):
        val = filters.get(key)
        return f"✅ {default}: {val}" if val else default

    kb = [
        [
            InlineKeyboardButton(get_label("name", "Name"),  callback_data="filter:name"),
            InlineKeyboardButton(get_label("city", "City"),  callback_data="filter:city")
        ],
        [
            InlineKeyboardButton(get_label("base", "Base"),  callback_data="filter:base"),
            InlineKeyboardButton(get_label("price", "Price"), callback_data="filter:price")
        ],
        [InlineKeyboardButton(get_label("year", "Year"),  callback_data="filter:year")],
        [
            InlineKeyboardButton("🔄 Reset", callback_data="filter_reset"),
            InlineKeyboardButton("🔎 Search ", callback_data="filter_search")
        ]
    ]
    
    # --- BLOC AJOUTÉ : Ajoute la pagination si fournie ---
    if page_info:
        page = page_info.get('page', 0)
        total_pages = page_info.get('total_pages', 1)
        
        # N'affiche la pagination que s'il y a plus d'une page
        if total_pages > 1:
            nav_row = [
                InlineKeyboardButton("«", callback_data=f"filter:page:{max(0, page-1)}"),
                InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"),
                InlineKeyboardButton("»", callback_data=f"filter:page:{min(total_pages-1, page+1)}"),
            ]
            kb.append(nav_row)
    # --- FIN DU BLOC AJOUTÉ ---

    kb.append([InlineKeyboardButton("⬅️ Annuler (logue)", callback_data="filter_cancel")])
    return InlineKeyboardMarkup(kb)

async def filter_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Démarre la conversation de filtre."""
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id

    # Initialise les filtres en attente
    context.user_data['pending_filters'] = {}
    context.user_data.pop('active_filters', None) # Vide les filtres actifs
    
    # Nettoie les anciens messages du catalogue
    old_catalog_msgs = CATALOG_MSGS.pop(chat_id, [])
    for mid in old_catalog_msgs:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass

    # Construit le menu
    kb = _build_filter_menu(context)
    m = await q.message.reply_text("Appliquez vos filtres et cliquez sur 'Search' :", reply_markup=kb) # Envoie un nouveau message
    
    # Stocke l'ID du menu de filtre pour le nettoyer
    context.user_data['filter_msgs'] = [m.message_id] 
    return CATALOG_FILTER_MAIN

async def filter_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """L'utilisateur a choisi un type de filtre (ex: Name). Demande la valeur."""
    q = update.callback_query
    await q.answer()
    
    field = q.data.split(':', 1)[1]  # name|city|base|price|year
    context.user_data['current_filter_key'] = field
    
    prompts = {
        'name':  "Type a name fragment (ex: John):",
        'city':  "Type a city fragment (ex: Toronto):",
        'base':  "Type a base fragment (ex: Montreal Pack / FAKEPERSON):",
        'price': "Max price (number, e.g. 12):",
        'year':  "Year digits (e.g. 1991):",
    }
    
    # Modifie le menu de filtre en question
    await q.message.edit_text(prompts[field], reply_markup=kb_back_cancel())
    return CATALOG_FILTER_AWAIT_VALUE

async def filter_receive_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réception de la valeur du filtre (Année, Prix, etc.) avec sécurité anti-crash."""
    try:
        # 1. Vérification de la clé
        key = context.user_data.get('current_filter_key')
        if not key:
            # Si la clé est perdue (ex: redémarrage), on renvoie au menu principal proprement
            kb = _build_filter_menu(context)
            await update.message.reply_text("⚠️ Session expirée. Refaites votre choix.", reply_markup=kb)
            return CATALOG_FILTER_MAIN
            
        # 2. Nettoyage de la valeur
        value = update.message.text.strip()
        context.user_data.setdefault('pending_filters', {})[key] = value
        
        # 3. Suppression des anciens messages (Prompt + Réponse user)
        # On le fait DANS un try pour ne pas bloquer si ça échoue
        try:
            prev_msg_id = context.user_data.get('filter_msgs', [])[0]
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prev_msg_id)
        except: pass
        
        try:
            await update.message.delete()
        except: pass
            
        # 4. Confirmation visuelle (Toast)
        # On utilise try/except au cas où
        try:
            notif = await update.message.reply_text(f"✅ Filtre '{key}' = {value}", quote=False)
            # Suppression différée (non bloquante)
            asyncio.create_task(delete_later(notif, 2)) 
        except: pass
        
        # 5. Affichage du nouveau menu
        kb = _build_filter_menu(context)
        m = await update.message.reply_text("Filtres appliqués. Cliquez sur 'Search' :", reply_markup=kb)
        context.user_data['filter_msgs'] = [m.message_id] # Mise à jour de l'ID
        
        # 6. Nettoyage de la clé temporaire
        context.user_data.pop('current_filter_key', None)
        
        return CATALOG_FILTER_MAIN

    except Exception as e:
        # AIRBAG : Si ça plante, on te le dit !
        print(f"[CRASH FILTER] {e}")
        await update.message.reply_text(f"🔥 Erreur dans le filtre : {e}")
        return CATALOG_FILTER_MAIN

# Petite fonction utilitaire pour supprimer sans bloquer
async def delete_later(msg, delay):
    await asyncio.sleep(delay)
    try: await msg.delete()
    except: pass

async def filter_page_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la navigation de page PENDANT un filtre."""
    q = update.callback_query
    
    try:
        page = int(q.data.split(":")[-1])
    except Exception:
        page = 0
        
    # Appelle show_products, en lui disant qu'on vient du filtre
    # et en passant le nouveau numéro de page.
    # Les 'active_filters' sont toujours dans context.user_data
    await show_products(update, context, page=page, tier=None, from_filter=True)
    
    # On reste dans le menu principal du filtre
    return CATALOG_FILTER_MAIN

async def filter_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Applique les filtres et lance la recherche."""
    q = update.callback_query
    
    # 1. Transfère les filtres en attente vers les filtres actifs
    context.user_data['active_filters'] = context.user_data.get('pending_filters', {})
    
    # 2. Appelle show_products, en lui disant qu'on vient du filtre
    await show_products(update, context, page=0, tier=None, from_filter=True)
    
    # On reste dans le menu principal du filtre
    return CATALOG_FILTER_MAIN

# REMPLACEZ la fonction filter_reset (lignes 1018-1035) par ceci :

async def filter_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réinitialise les filtres et nettoie les fiches produits."""
    q = update.callback_query
    await q.answer("Filtres réinitialisés")
    
    # Vide tous les filtres (en attente et actifs)
    context.user_data.pop('pending_filters', None)
    context.user_data.pop('active_filters', None)
    
    # --- CORRECTION: Nettoie les fiches produits affichées ---
    prev_filter_fiches = context.user_data.pop("filter_fiches_msg_ids", [])
    for mid in prev_filter_fiches:
         try:
             await context.bot.delete_message(chat_id=q.message.chat_id, message_id=mid)
         except: 
             pass # Ignore les erreurs si le message est déjà supprimé
    # --- FIN DE LA CORRECTION ---

    # Ré-affiche le menu de filtre (maintenant vide et SANS pagination)
    kb = _build_filter_menu(context, page_info=None) # Force la suppression de la pagination
    await q.message.edit_text("Appliquez vos filtres et cliquez sur 'Search' :", reply_markup=kb)

    return CATALOG_FILTER_MAIN # On reste dans la conversation

async def filter_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Annule le filtre et recharge le catalogue."""
    q = update.callback_query
    await q.answer()
    
    # Vide les filtres
    context.user_data.pop('pending_filters', None)
    context.user_data.pop('active_filters', None)
    
    # Appelle show_products (qui va nettoyer le menu de filtre)
    await show_products(update, context, page=0, tier=None, from_filter=False) # from_filter=False pour recharger
    return ConversationHandler.END

# ========================== CATALOGUE PRODUITS (CCS CLONE) ==========================
#
#   Ce bloc est un clone de 'show_products' et 'filter_...'
#   dédié uniquement à la catégorie 'ccs'
#
# ====================================================================================

CCS_CATALOG_MSGS = {} # Variable globale SÉPARÉE pour les messages CCS

# --- AJOUTE CECI POUR QUE LE MENU CCS FONCTIONNE ---
def _get_products(category, tier=None):
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    sql = "SELECT * FROM products WHERE category=? AND is_active=1 AND stock>0"
    args = [category]
    if tier:
        sql += " AND tier=?"
        args.append(tier)
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    con.close()
    return [dict(zip(cols, row)) for row in rows]

async def ccs_catalog_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Point d'entrée pour le catalogue CCS depuis le menu principal."""
    # Appelle la fonction d'affichage principale
    # from_filter=False nettoiera le menu principal
    return await show_products_ccs(update, context, page=0, tier=None, from_filter=False)



async def show_products_ccs(update, context, page=0, tier=None, from_filter=False):
    import asyncio
    import os
    
    query = getattr(update, "callback_query", None)
    chat_id = query.message.chat_id if query else update.effective_chat.id
    
    # 1. UX : CHARGEMENT + PAUSE
    loading_msg = None
    try:
        loading_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Recherche en cours...")
        await asyncio.sleep(0.5) # Pause UX
    except: pass
    
    if query:
        try: await query.answer()
        except: pass
        if not from_filter:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
            except: pass

    # 2. Nettoyage
    try:
        msgs_to_del = []
        if from_filter:
            msgs_to_del += context.user_data.pop("ccs_filter_fiches_msg_ids", [])
            msgs_to_del += context.user_data.pop("ccs_filter_msgs", [])
        else:
            msgs_to_del += CCS_CATALOG_MSGS.pop(chat_id, [])
            msgs_to_del += context.user_data.pop("ccs_filter_msgs", [])
        for mid in set(msgs_to_del):
            try: await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except: pass
    except: pass

    # 3. Recherche SQL
    try:
        filters = context.user_data.get('ccs_active_filters', {})
        PER_PAGE = 2
        chunk, total_items = _get_products_optimized(category="ccs", page=page, per_page=PER_PAGE, filters=filters, tier=tier)
    except Exception as e:
        if loading_msg:
            try: await loading_msg.edit_text(f"⚠️ Erreur DB: {e}")
            except: pass
        return

    # 4. Suppression Loading
    if loading_msg:
        try: await loading_msg.delete()
        except: pass

    total_pages = max(1, (total_items + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    sent_ids = []

    # Fonction locale d'affichage
    def fmt_product_ccs(p):
        try:
            lines = []
            content = p.get('content', '')
            def get_val(key):
                import re
                m = re.search(f"{key}:\\s*(.+)", content, re.IGNORECASE)
                return m.group(1).strip() if m else None

            cc = get_val("CC") or get_val("BINS")
            exp = get_val("EXP")
            if cc: lines.append(f"BINS: {cc[:6]}")
            if exp: lines.append(f"EXP: {exp}")
            raw_title = str(p.get('title', '')).split('•')[0].strip()
            lines.append(f"FIRST NAME: {raw_title}")
            lines.append(f"BASE: {p.get('tier', 'N/A')}")
            lines.append(f"PRICE: {p.get('price', 0.0):.2f} CAD")
            return "\n".join(lines)
        except:
            return f"💳 {p.get('title')}\n{p.get('price')} CAD"

    # 5. Gestion vide
    if not chunk:
        text = "❌ Aucun produit Cc's trouvé."
        kb = _build_filter_menu_ccs(context, page_info=None) if from_filter else InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]])
        m = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        if from_filter: context.user_data['ccs_filter_msgs'] = [m.message_id]
        else: CCS_CATALOG_MSGS[chat_id] = [m.message_id]
        return

    # 6. Affichage
    for p in chunk:
        txt = fmt_product_ccs(p)
        pid = p['id']
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Buy Now", callback_data=f"buy:{pid}"), InlineKeyboardButton("🛒 Add", callback_data=f"cart:add:{pid}") ]])
        try:
            m = await context.bot.send_message(chat_id=chat_id, text=txt, reply_markup=kb)
            sent_ids.append(m.message_id)
        except: pass

    # 7. Menu Bas
    if from_filter:
        context.user_data['ccs_filter_fiches_msg_ids'] = sent_ids
        kb = _build_filter_menu_ccs(context, page_info={'page': page, 'total_pages': total_pages})
        m = await context.bot.send_message(chat_id=chat_id, text=f"🔎 Résultats : {total_items} trouvés", reply_markup=kb)
        context.user_data['ccs_filter_msgs'] = [m.message_id]
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Filter", callback_data="ccs_filter_open")],
            [InlineKeyboardButton("«", callback_data=f"ccs:page:{max(0, page-1)}"), InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"), InlineKeyboardButton("»", callback_data=f"ccs:page:{min(total_pages-1, page+1)}")],
            [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
        ])
        m = await context.bot.send_message(chat_id=chat_id, text=f"💳 Catalogue Cc's ({total_items} produits)", reply_markup=kb)
        sent_ids.append(m.message_id)
        CCS_CATALOG_MSGS[chat_id] = sent_ids

    def fmt_product(p):
        # Utilise le parseur complet de shop_helpers pour tout récupérer
        f = shop_helpers._parse_product_fields(p)

        # Crée les lignes
        lines = []

  
        if f.get("cc"):
            # Affiche seulement les 6 premiers chiffres (le BIN)
            lines.append(f"BINS: {f['cc'][:6]}") # <-- Affiche "BINS: 123456"
        if f.get("exp"):
            lines.append(f"EXP: {f['exp']}")
        # --- FIN MODIFICATION ---

  
        lines.append(f"FIRST NAME: {f.get('first_up', 'N/A')}")
        lines.append(f"DOB: {f.get('year') or f.get('dob', 'N/A')}")
        lines.append(f"CITY: {f.get('city') or '—'}")
        lines.append(f"BASE: {f.get('base', 'N/A')}")
        lines.append(f"PRICE: {f.get('price', 0.0):.2f} {f.get('currency', 'CAD')}")

        return "\n".join(lines)
    
    for idx, p in enumerate(chunk, start=1):
        txt = fmt_product(p)
        pid = p.get("id")
        kb_rows = [[
            InlineKeyboardButton("⚡ Buy Now", callback_data=f"buy:{pid}"),
            InlineKeyboardButton("🛒 Add to Cart", callback_data=f"cart:add:{pid}"),
        ]]
        kb = InlineKeyboardMarkup(kb_rows)
        m = await context.bot.send_message(chat_id=chat_id, text=txt, reply_markup=kb)
        sent_ids.append(m.message_id) 

    # Affiche le bon menu en bas
    if from_filter:
        context.user_data['ccs_filter_fiches_msg_ids'] = sent_ids
        page_info = {'page': page, 'total_pages': total_pages}
        kb_with_nav = _build_filter_menu_ccs(context, page_info=page_info) # Fonction CCS
        m_nav = await context.bot.send_message(
            chat_id=chat_id, 
            text=f"Filtre actif - Page {page+1}/{total_pages}", 
            reply_markup=kb_with_nav
        )
        context.user_data['ccs_filter_msgs'] = [m_nav.message_id]
    else:
        kb_nav = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Filter", callback_data="ccs_filter_open")], # Callback CCS
            [
                InlineKeyboardButton("«", callback_data=f"ccs:page:{max(0, page-1)}"), # Callback CCS
                InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"),
                InlineKeyboardButton("»", callback_data=f"ccs:page:{min(total_pages-1, page+1)}"), # Callback CCS
            ],
            [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
        ])
        m_nav = await context.bot.send_message(chat_id=chat_id, text=f"Page {page+1}/{total_pages}", reply_markup=kb_nav)
        sent_ids.append(m_nav.message_id)
        CCS_CATALOG_MSGS[chat_id] = sent_ids
        return CCS_FILTER_MAIN      

# --- Fonctions de filtre (Clone pour CCS) ---

def _build_filter_menu_ccs(context: ContextTypes.DEFAULT_TYPE, page_info: dict = None) -> InlineKeyboardMarkup:
    """Construit le menu de filtre dynamique pour CCS."""
    filters = context.user_data.get('ccs_pending_filters', {}) # ccs_

    def get_label(key, default):
        val = filters.get(key)
        return f"✅ {default}: {val}" if val else default

    # --- MODIFICATION ICI ---
    kb = [
        [
            InlineKeyboardButton(get_label("bins", "Bins"),  callback_data="ccs_filter:bins"), # <-- LIGNE AJOUTÉE
            InlineKeyboardButton(get_label("name", "Name"),  callback_data="ccs_filter:name")
        ],
        [
            InlineKeyboardButton(get_label("city", "City"),  callback_data="ccs_filter:city") # <-- LIGNE DÉPLACÉE
        ],
    # --- FIN MODIFICATION ---
        [
            InlineKeyboardButton(get_label("base", "Base"),  callback_data="ccs_filter:base"),
            InlineKeyboardButton(get_label("price", "Price"), callback_data="ccs_filter:price")
        ],
        [InlineKeyboardButton(get_label("year", "Year"),  callback_data="ccs_filter:year")],
        [
            InlineKeyboardButton("🔄️ Reset", callback_data="ccs_filter_reset"),
            InlineKeyboardButton("🔎 Search", callback_data="ccs_filter_search")
        ]
    ]

    if page_info:
        page = page_info.get('page', 0)
        total_pages = page_info.get('total_pages', 1)
        if total_pages > 1:
            nav_row = [
                InlineKeyboardButton("«", callback_data=f"ccs_filter:page:{max(0, page-1)}"),
                InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"),
                InlineKeyboardButton("»", callback_data=f"ccs_filter:page:{min(total_pages-1, page+1)}"),
            ]
            kb.append(nav_row)

    kb.append([InlineKeyboardButton("🏠 Menu Principale", callback_data="ccs_filter_cancel")])
    return InlineKeyboardMarkup(kb)

async def filter_start_ccs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Démarre la conversation de filtre CCS."""
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id

    # Initialise les filtres en attente pour CCS
    context.user_data['ccs_pending_filters'] = {}
    context.user_data.pop('ccs_active_filters', None) 
    
    # Nettoie les anciens messages du catalogue CCS
    old_catalog_msgs = CCS_CATALOG_MSGS.pop(chat_id, [])
    for mid in old_catalog_msgs:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass

    # Construit le menu de filtre CCS
    kb = _build_filter_menu_ccs(context)
    # Supprime l'ancien message (catalogue) et envoie le menu filtre
    try:
        await q.message.delete()
    except: pass
    m = await q.message.reply_text("Appliquez vos filtres et cliquez sur 'Search' :", reply_markup=kb) 
    
    # Stocke l'ID du nouveau menu de filtre CCS
    context.user_data['ccs_filter_msgs'] = [m.message_id] 
    return CCS_FILTER_MAIN # Retourne l'état de la conversation de filtre CCS

async def filter_select_type_ccs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    field = q.data.split(':', 1)[1]
    context.user_data['ccs_current_filter_key'] = field
    
    prompts = {
        'bins':  "Type a BIN (ex: 450611):",
        'name':  "Type a name fragment (ex: John):",
        'city':  "Type a city fragment (ex: Toronto):",
        'base':  "Type a base fragment (ex: Montreal Pack / FAKEPERSON):",
        'price': "Max price (number, e.g. 12):",
        'year':  "Year digits (e.g. 1991):",
    }
    
    await q.message.edit_text(prompts[field], reply_markup=kb_back_cancel())
    return CCS_FILTER_AWAIT_VALUE

async def filter_receive_value_ccs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.pop('ccs_current_filter_key', None)
    if not key:
        return CCS_FILTER_MAIN
        
    value = update.message.text.strip()
    context.user_data.setdefault('ccs_pending_filters', {})[key] = value
    
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['ccs_filter_msgs'][0])
    except: pass
    try:
        await update.message.delete()
    except: pass
        
    notif_msg = await update.message.reply_text(f"✅ Filtre '{key}' mis à jour: {value}", quote=False)
    
    kb = _build_filter_menu_ccs(context)
    m = await update.message.reply_text("Appliquez vos filtres et cliquez sur 'Search' :", reply_markup=kb)
    context.user_data['ccs_filter_msgs'] = [m.message_id] 
    
    await asyncio.sleep(2)
    try:
        await notif_msg.delete()
    except:
        pass
    
    return CCS_FILTER_MAIN

async def filter_search_ccs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    
    context.user_data['ccs_active_filters'] = context.user_data.get('ccs_pending_filters', {})
    await show_products_ccs(update, context, page=0, tier=None, from_filter=True)
    
    return CCS_FILTER_MAIN

async def filter_page_nav_ccs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        page = int(q.data.split(":")[-1])
    except Exception:
        page = 0
    await show_products_ccs(update, context, page=page, tier=None, from_filter=True)
    return CCS_FILTER_MAIN

async def filter_reset_ccs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Filtres réinitialisés")
    
    context.user_data.pop('ccs_pending_filters', None)
    context.user_data.pop('ccs_active_filters', None)
    
    prev_filter_fiches = context.user_data.pop("ccs_filter_fiches_msg_ids", [])
    for mid in prev_filter_fiches:
         try:
             await context.bot.delete_message(chat_id=q.message.chat_id, message_id=mid)
         except: 
             pass

    kb = _build_filter_menu_ccs(context, page_info=None) 
    await q.message.edit_text("Appliquez vos filtres et cliquez sur 'Search' :", reply_markup=kb)

    return CCS_FILTER_MAIN 

async def filter_cancel_ccs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Annule le filtre et recharge le catalogue."""
    q = update.callback_query
    await q.answer()

    # Vide les filtres (par sécurité, même si goto_menu le fait)
    context.user_data.pop('ccs_pending_filters', None)
    context.user_data.pop('ccs_active_filters', None)

    # --- MODIFICATION : Appelle goto_menu ---
    return await goto_menu(update, context)
    # --- FIN MODIFICATION ---

# ========================== FIN DU BLOC CCS ==========================

# ========================== BOUTONS SIMPLES ==========================
async def callback_show_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.callback_query.message.reply_text(
        f"🆔 Ton ID Telegram est : <code>{user_id}</code>",
        parse_mode="HTML"
    )

# ================= PAIEMENT CRYPTO (ELECTRUM / NO KYC) =================

def ensure_payment_table():
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS crypto_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            address TEXT,
            amount_cad REAL,
            amount_btc_expected REAL,
            status TEXT DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.commit()
    con.close()

ensure_payment_table()

def get_btc_price_cad():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=cad", timeout=5)
        return float(r.json()['bitcoin']['cad'])
    except:
        return 135000.0 

def zpub_to_xpub(zpub: str) -> str:
    """Convertit une clé zpub (Electrum) en xpub (Standard) pour compatibilité."""
    try:
        if not zpub.startswith("zpub"):
            return zpub
        data = base58.b58decode_check(zpub)
        # Remplace le préfixe zpub (04b24746) par xpub (0488b21e)
        prefix_xpub = b'\x04\x88\xB2\x1E'
        data = prefix_xpub + data[4:]
        return base58.b58encode_check(data).decode()
    except Exception as e:
        logger.error(f"Erreur conversion zpub: {e}")
        return zpub

def generate_address(user_id: int, order_id: int) -> str:
    """Génère une adresse BTC via Bip32Secp256k1 (Force Brute)."""
    if not ADMIN_XPUB: return "ERREUR_NO_XPUB"
    try:
        # 1. Nettoyage et Conversion ZPUB -> XPUB
        raw_key = ADMIN_XPUB.strip().replace('"', '').replace("'", "")
        if raw_key.startswith("zpub"):
            try:
                data = base58.b58decode_check(raw_key)
                data = b'\x04\x88\xB2\x1E' + data[4:]
                raw_key = base58.b58encode_check(data).decode()
            except: pass 
            
        # 2. Dérivation Bas Niveau (Bip32 pur)
        # On contourne les vérifications de profondeur qui bloquaient avant
        ctx = Bip32Secp256k1.FromExtendedKey(raw_key)
        
        # Chemin : /0 (Chaîne externe) / order_id (Index commande)
        addr_ctx = ctx.ChildKey(0).ChildKey(order_id)
        
        # 3. Encodage Segwit (bc1q)
        # C'est ici qu'on applique le correctif "hrp='bc'" qui a sauvé la mise
        pub_key_bytes = addr_ctx.PublicKey().RawCompressed().ToBytes()
        return P2WPKHAddr.EncodeKey(pub_key_bytes, hrp="bc")
        
    except Exception as e:
        logger.error(f"CRASH FORCE: {e}")
        return f"ERR: {str(e)}"

def check_payment_status(address: str):
    try:
        r = requests.get(f"https://mempool.space/api/address/{address}", timeout=10)
        data = r.json()
        
        # --- SÉCURITÉ : 1 CONFIRMATION MINIMUM ---
        # chain_stats = L'argent est gravé dans un bloc (Confirmé)
        # On ignore mempool_stats (Non confirmé / Risque d'arnaque RBF)
        satoshis = data['chain_stats']['funded_txo_sum']
        
        return satoshis / 100_000_000
    except:
        return 0.0

async def add_balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    if not ADMIN_XPUB:
        await q.message.reply_text("⚠️ Erreur config: XPUB manquant dans .env")
        return ConversationHandler.END

    await replace_view(
        q,
        "💸 **Recharge Crypto (Automatique)**\n\n"
        "Combien voulez-vous ajouter ? (en CAD)\n"
        "_(Exemple: tapez 50)_",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown"
    )
    return WAIT_AMOUNT_CRYPTO

async def receive_amount_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        amount_cad = float(update.message.text.replace(',', '.').strip())
        if amount_cad < 5: 
            await update.message.reply_text("❌ Minimum 5$.")
            return WAIT_AMOUNT_CRYPTO
    except:
        await update.message.reply_text("❌ Montant invalide.")
        return WAIT_AMOUNT_CRYPTO

    msg_wait = await update.message.reply_text("⏳ Génération de l'adresse...")
    
    btc_price = get_btc_price_cad()
    amount_btc = amount_cad / btc_price
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("INSERT INTO crypto_payments (user_id, amount_cad, amount_btc_expected) VALUES (?,?,?)", 
                (str(user_id), amount_cad, amount_btc))
    order_id = cur.lastrowid
    
    address = generate_address(user_id, order_id)
    
    cur.execute("UPDATE crypto_payments SET address=? WHERE id=?", (address, order_id))
    con.commit()
    con.close()
    
    try: await msg_wait.delete()
    except: pass

    txt = (
        f"🧾 **Facture #{order_id}**\n"
        f"💰 Montant : `{amount_cad:.2f} CAD`\n"
        f"💎 BTC : `{amount_btc:.8f} BTC`\n\n"
        f"👉 **Envoyez à :**\n`{address}`\n\n"
        f"_(Cliquez l'adresse pour copier)_"
    )
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ J'ai payé (Vérifier)", callback_data=f"check_pay_{order_id}")],
        [InlineKeyboardButton("❌ Annuler", callback_data="menu_accueil")]
    ])
    
    await update.message.reply_text(txt, reply_markup=kb, parse_mode="Markdown")
    return ConversationHandler.END

async def check_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    order_id = int(q.data.split("_")[-1])
    await q.answer("🔍 Vérification...")
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT address, amount_btc_expected, status, amount_cad FROM crypto_payments WHERE id=?", (order_id,))
    row = cur.fetchone()
    
    if not row or row[2] == 'paid':
        con.close()
        await q.message.reply_text("✅ Déjà payé ou introuvable.")
        return

    address, expected, status, cad_val = row
    received = check_payment_status(address)
    
    if received >= (expected * 0.95):
        cur.execute("UPDATE crypto_payments SET status='paid' WHERE id=?", (order_id,))
        con.commit()
        con.close()
        new_bal, _ = credit_and_upgrade(str(q.from_user.id), cad_val)
        await q.message.reply_text(f"✅ **Reçu !**\nNouveau solde : {new_bal:.2f}$", parse_mode="Markdown")
        await show_main_menu(q.from_user.id)
    else:
        con.close()
        # Message rassurant
        await q.message.reply_text(
            f"⏳ **Paiement détecté, en attente de validation...**\n"
            f"Reçu: {received:.8f} BTC\n"
            f"Attendu: {expected:.8f} BTC\n\n"
            f"⚠️ **Note :** La Blockchain Bitcoin nécessite environ ~10 minutes pour confirmer une transaction. Réessayez ce bouton dans quelques minutes.",
            quote=True,
            parse_mode="Markdown"
        )
        
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
    user_id = update.effective_user.id # On récupère l'ID utilisateur ici

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
            
    # --- CORRECTION MISE À JOUR (POUR CCS) ---
    # Récupère TOUTES les listes de messages temporaires
    hist_msgs = context.user_data.pop("hist_msgs", []) # De l'historique
    verif_msgs = context.user_data.pop("verif_flow_msg_ids", []) # Du flux de vérification
    
    # Filtres Pro's
    filter_msgs = context.user_data.pop("filter_msgs", []) 
    filter_fiches = context.user_data.pop("filter_fiches_msg_ids", [])
    catalog_msgs = CATALOG_MSGS.pop(user_id, []) 
    
    # --- LIGNES AJOUTÉES POUR CCS ---
    ccs_filter_msgs = context.user_data.pop("ccs_filter_msgs", [])
    ccs_filter_fiches = context.user_data.pop("ccs_filter_fiches_msg_ids", [])
    ccs_catalog_msgs = CCS_CATALOG_MSGS.pop(user_id, [])
    
    # Combine et dédoublonne TOUTES les listes
    all_msgs_to_delete = list(set(
        hist_msgs + verif_msgs + 
        catalog_msgs + filter_msgs + filter_fiches +
        ccs_catalog_msgs + ccs_filter_msgs + ccs_filter_fiches # <-- AJOUTÉ
    )) 

    if all_msgs_to_delete:
        for mid in all_msgs_to_delete:
            try:
                # Utilise user_id car chat_id n'est pas dispo ici
                await context.bot.delete_message(chat_id=user_id, message_id=mid)
            except:
                pass # Ignore les messages déjà supprimés
    # --- FIN DE LA CORRECTION ---

    # nettoie tout l’état de la conversation (sauf les listes qu'on vient de pop)
    context.user_data.clear()

    # affiche le menu principal proprement
    # On utilise clear=True pour que show_main_menu nettoie les messages de 'bot_messages' (sécurité)
    await show_main_menu(user_id, clear=True) 

    # stoppe toute attente de réponse (empêche "1 ou 2")
    return ConversationHandler.END

async def animate_wait_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, batch_id: str, lang: str):
    """
    Anime un message "Décryptage en cours..." pendant que le batch s'exécute.
    S'arrête quand br["notified"] passe à True.
    """
    i = 0
    # Laisse le temps au batch d'être créé
    await asyncio.sleep(1) 
    br = batch_runs.get(batch_id)
    if not br:
        return

    try:
        # Boucle tant que le batch n'est pas marqué comme "notified" (terminé)
        while not br.get("notified", False):
            dots = "." * (i % 3 + 1) # Fait . .. ... . .. ...
            
            # Utilise la fonction msg() pour obtenir le texte dans la bonne langue
            base_text = msg(chat_id, 'decrytage_en_cours').replace('…','') # Récupère '🔄 Décryptage en cours'
            text = f"{base_text}{dots} ({br.get('resolved', 0)}/{br.get('total', '?')})"
            
            try:
                await context.bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=message_id
                )
            except Exception as e:
                # Si le message est supprimé ou identique ("Message is not modified"), arrête la boucle
                logger.info(f"Arrêt de l'animation (message {message_id}): {e}")
                break
            
            i += 1
            await asyncio.sleep(2) # IMPORTANT: Pause de 2s pour éviter le flood
            
            # Rafraîchit la référence au batch
            br = batch_runs.get(batch_id)
            if not br:
                break # Le batch a été supprimé

    except Exception as e:
        logger.error(f"Erreur dans animate_wait_message: {e}")
    finally:
        # Une fois la boucle finie, supprime le message d'attente
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except:
            pass

        # AJOUTE CES DEUX FONCTIONS (après animate_wait_message, ligne 915)

def get_ivr_timings():
    """Charge les temps de pause depuis le fichier JSON, ou utilise les défauts."""
    import json, os
    # Définit les temps par défaut au cas où le fichier n'existe pas
    defaults = {
        "open_1": 55,  # Pause 1 (Ouvert) avant 4-4-6
        "open_2": 41,  # Pause 2 (Ouvert) après 4-4-6
        "closed_1": 40, # Pause 1 (Fermé) avant 1-1
        "closed_2": 45  # Pause 2 (Fermé) après 1-1
    }
    try:
        with open(IVR_TIMINGS_FILE, 'r') as f:
            timings = json.load(f)
        # S'assure que toutes les clés existent, sinon ajoute le défaut
        for key, val in defaults.items():
            timings.setdefault(key, val)
        return timings
    except Exception:
        # Si le fichier n'existe pas ou est corrompu, on le crée
        try:
            with open(IVR_TIMINGS_FILE, 'w') as f:
                json.dump(defaults, f, indent=2)
        except Exception as e:
            logger.error(f"Impossible de créer le fichier ivr_timings.json: {e}")
        return defaults

def set_ivr_timing(key, value):
    """Met à jour une valeur de temps dans le fichier JSON."""
    import json, os
    timings = get_ivr_timings() # Charge les valeurs actuelles
    try:
        timings[key] = int(value) # Met à jour la valeur
        with open(IVR_TIMINGS_FILE, 'w') as f:
            json.dump(timings, f, indent=2) # Ré-écrit le fichier complet
        return True
    except Exception as e:
        logger.error(f"Erreur set_ivr_timing: {e}")
        return False

async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await goto_menu(update, context)

async def callback_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = get_user_lang(str(update.effective_user.id))
    await replace_view(
        q,
        "Support : @nomennesciosupport" if lang == "fr" else "Support: @nomennesciosupport",
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
            "Support direct : @nomennesciosupport\n\n"
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
            "Direct support: @nomennesciosupport\n\n"
            "⚡ Processing time:\n"
            "License validation is almost instant. Payments are confirmed within minutes.\n\n"
            "🤝 Partnerships:\n"
            "We are open to any collaboration or integration.\n\n"
            "🚀 Upcoming features:\n"
            "New functionalities coming soon (automation, statistics, advanced tools)."
        )
    await replace_view(q, txt, reply_markup=kb_back_to_menu(), parse_mode="Markdown")

# ================= CHANNEL PRIVÉ (FINAL) =================
async def acces_channel_prive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    
    # TON ID (Celui qui est correct)
    ID_DU_CANAL = -1003536878473

    try:
        # 1. On vérifie si tu es membre
        is_member = False
        try:
            member = await context.bot.get_chat_member(chat_id=ID_DU_CANAL, user_id=q.from_user.id)
            if member.status in ['member', 'creator', 'administrator', 'restricted']:
                is_member = True
        except Exception:
            pass # Si on arrive pas à vérifier, on assume que non

        # 2. SI TU ES DÉJÀ MEMBRE
        if is_member:
            # On construit le lien d'accès direct (t.me/c/...)
            clean_id = str(ID_DU_CANAL).replace("-100", "")
            direct_link = f"https://t.me/c/{clean_id}/1"
            
            await replace_view(
                q,
                "👋 **Tu es déjà membre !**\n\n"
                "Tu as déjà accès au canal VIP. Clique sur le bouton ci-dessous pour y entrer directement.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚀 Ouvrir le Canal", url=direct_link)],
                    [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
                ]),
                parse_mode="Markdown"
            )
            return

        # 3. SI TU N'ES PAS MEMBRE (On génère l'invitation)
        await q.answer() # Stop le chargement
        invite_link = await context.bot.create_chat_invite_link(
            chat_id=ID_DU_CANAL,
            member_limit=1, 
            name=f"Invite pour {q.from_user.first_name}"
        )

        await replace_view(
            q,
            f"🕵️ **Accès VIP Autorisé**\n\n"
            f"Voici ton lien d'accès unique.\n"
            f"⚠️ Attention : Ce lien ne fonctionne qu'une seule fois.\n\n"
            f"👉 {invite_link.invite_link}",
            reply_markup=kb_back_to_menu(),
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Erreur channel: {e}")
        await q.message.reply_text("🔴 Une erreur est survenue (vérifie que le bot est Admin du canal).")
    
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

# CODE À COLLER À LA LIGNE 1056
async def hist_pros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche l'historique des produits achetés, avec pagination ET FILTRE."""
    from shop_helpers import full_product_text
    import json

    q = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)
    chat_id = update.effective_chat.id

    # Supprimer les anciens messages envoyés (pour ne pas accumuler)
    old_msgs = context.user_data.get("hist_msgs", [])
    for mid in old_msgs:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except:
            pass
    context.user_data["hist_msgs"] = []  # Réinitialiser la liste

    # === DÉBUT DE LA LOGIQUE DE FILTRE ===
    active_filter = context.user_data.get('history_filter')
    filter_clause = ""
    params = [uid] # Paramètre SQL de base (user_id)

    filter_text = "Filtre: Aucun"
    if active_filter:
        f_type = active_filter.get('type')
        f_value = active_filter.get('value', '')

        search_key = ""
        if f_type == 'sin':
            search_key = 'sin'
            filter_text = f"Filtre (SIN): {f_value}"
        elif f_type == 'dl':
            search_key = 'dl'
            filter_text = f"Filtre (DL): {f_value}"
        elif f_type == 'name':
            # Recherche dans la colonne 'title' de la table 'purchases'
            filter_clause = " AND title LIKE ?"
            params.append(f"%{f_value}%")
            filter_text = f"Filtre (Nom): {f_value}"

        if search_key:
            # Recherche dans le blob JSON 'full_data'
            # On cherche la chaîne "key": "value" (ou une partie)
            filter_clause = f" AND full_data LIKE ?"
            params.append(f'%"{search_key}": "{f_value}%')
    # === FIN DE LA LOGIQUE DE FILTRE ===


    # Pagination
    page = 0
    if q.data.startswith("hist:pros:page:"):
        try:
            page = int(q.data.split(":")[-1])
        except:
            page = 0

    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()

    # Requête SQL modifiée pour compter avec le filtre
    count_query = f"SELECT COUNT(*) FROM purchases WHERE user_id = ? {filter_clause}"
    cur.execute(count_query, tuple(params))
    total = cur.fetchone()[0]

    per_page = 2 # Votre règle de 2 par page
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    offset = page * per_page

    # Requête SQL modifiée pour chercher avec filtre et pagination
    query = f"""
        SELECT id, product_id, full_data, created_at
        FROM purchases
        WHERE user_id = ? {filter_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
    """
    params_page = tuple(params + [per_page, offset])
    cur.execute(query, params_page)
    rows = cur.fetchall()
    con.close()

    # Supprimer le message précédent de pagination (le "Page 1/19")
    try:
        await q.message.delete()
    except Exception:
        pass

    sent_messages = [] # Pour stocker les ID des nouveaux messages

    # Envoie chaque fiche séparément
    for row in rows:
        pid = row[0]
        full_data = row[2]
        date = row[3]

        try:
            parsed = json.loads(full_data)
        except Exception:
            parsed = {}

        fiche = full_product_text(parsed) # Utilise la fonction de shop_helpers
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
        sent_messages.append(msg_sent.message_id)

    # --- Pagination + Filtre + Retour (message séparé) ---
    nav_row = [
        InlineKeyboardButton("«", callback_data=f"hist:pros:page:{max(0, page-1)}"),
        InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"),
        InlineKeyboardButton("»", callback_data=f"hist:pros:page:{min(total_pages-1, page+1)}"),
    ]

    # Le bouton filtre que vous vouliez
    filter_row = [InlineKeyboardButton("🔎 Filtrer", callback_data="history_filter_start")]
    if active_filter:
        # Ajoute le bouton Reset SEULEMENT si un filtre est actif
        filter_row.append(InlineKeyboardButton("❌ Reset Filtre", callback_data="history_filter_reset"))

    back_row = [InlineKeyboardButton("⬅️ Retour", callback_data="hist:view")] # Retour vers le menu hist

    # Désactive les flèches si une seule page
    if total_pages == 1:
        nav_row = [
            InlineKeyboardButton("«", callback_data="noop"),
            InlineKeyboardButton(f"1/1", callback_data="noop"),
            InlineKeyboardButton("»", callback_data="noop"),
        ]

    kb_layout = [nav_row, filter_row, back_row]

    # Affiche le statut du filtre
    pagination_msg_text = f"📄 Page {page+1}/{total_pages} ({filter_text})"
    if not rows and total == 0 and not active_filter:
         pagination_msg_text = "🧾 Aucun achat trouvé."
    elif not rows and (total > 0 or active_filter):
        pagination_msg_text = f"❌ Aucun achat trouvé pour ce filtre. ({filter_text})"

    pagination_msg = await context.bot.send_message(
        chat_id=q.message.chat.id,
        text=pagination_msg_text,
        reply_markup=InlineKeyboardMarkup(kb_layout)
    )

    # Ajouter tous les messages (fiches + nav) à la liste pour suppression future
    context.user_data["hist_msgs"] = sent_messages + [pagination_msg.message_id]







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

    # CODE À COLLER À LA LIGNE 1222 (SANS ESPACES DEVANT)

async def history_filter_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche les options de filtre pour l'historique."""
    q = update.callback_query
    await q.answer()

    keyboard = [
        [InlineKeyboardButton("Par Nom (Titre)", callback_data="history_filter_type:name")],
        [InlineKeyboardButton("Par SIN", callback_data="history_filter_type:sin")],
        [InlineKeyboardButton("Par DL", callback_data="history_filter_type:dl")],
        [InlineKeyboardButton("Annuler", callback_data="history_filter_cancel")]
    ]

    try:
        await q.edit_message_text(
            "🔎 Sur quel champ voulez-vous filtrer ?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception:
        # Failsafe si le message ne peut être édité
        await q.message.reply_text(
            "🔎 Sur quel champ voulez-vous filtrer ?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return HISTORY_FILTER_CHOICE

async def history_filter_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reçoit le type de filtre et demande la valeur."""
    q = update.callback_query
    await q.answer()

    filter_type = q.data.split(':')[-1] # 'name', 'sin', ou 'dl'
    context.user_data['history_filter_type'] = filter_type

    prompts = {
        'name': "✍️ Entrez le Nom (ou partie du nom) :",
        'sin': "🧾 Entrez le SIN (ou partie) :",
        'dl': "🚗 Entrez le DL (ou partie) :",
    }

    try:
        await q.message.delete() # Supprime le message de choix
    except Exception:
        pass

    await q.message.reply_text(prompts.get(filter_type, "Entrez la valeur :"))
    return HISTORY_FILTER_INPUT

async def history_filter_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reçoit la valeur du filtre, l'enregistre et relance l'historique."""
    filter_type = context.user_data.pop('history_filter_type', None)
    if not filter_type:
        return ConversationHandler.END # Sécurité

    filter_value = update.message.text.strip()

    # Enregistre le filtre dans la session
    context.user_data['history_filter'] = {'type': filter_type, 'value': filter_value}

    # Crée un faux Update avec un CallbackQuery pour appeler hist_pros
    # C'est nécessaire car hist_pros attend un callback_query
    class MockFromUser:
        id = update.effective_user.id

    class MockMessage:
        chat_id = update.effective_chat.id
        chat = update.effective_chat # Ajout pour la suppression de message
        async def delete(self):
            try: await update.message.delete() # Supprime le message de l'utilisateur
            except: pass
        async def reply_text(self, *args, **kwargs):
            return await update.message.reply_text(*args, **kwargs)

    class MockCallbackQuery:
        data = "hist:pros:page:0" # Force la page 0
        message = MockMessage()
        from_user = MockFromUser()
        async def answer(self): pass

    mock_update = Update(update.update_id, callback_query=MockCallbackQuery())

    await hist_pros(mock_update, context) # Appelle hist_pros avec le filtre activé
    return ConversationHandler.END

async def history_filter_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Annule le processus de filtre."""
    q = update.callback_query
    await q.answer()

    try:
        await q.message.delete()
    except Exception:
        pass

    context.user_data.pop('history_filter_type', None)

    # Relance l'historique normal
    q.data = "hist:pros:page:0" # simule un retour à la page 0
    await hist_pros(update, context)
    return ConversationHandler.END

async def history_filter_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réinitialise le filtre et relance l'historique."""
    q = update.callback_query
    await q.answer()

    # Supprime le filtre
    context.user_data.pop('history_filter', None)

    # Relance l'historique (qui verra que le filtre est parti)
    q.data = "hist:pros:page:0" # simule un retour à la page 0
    await hist_pros(update, context)

# FIN DU BLOC À AJOUTER

async def admin_category_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le menu de gestion pour une catégorie spécifique."""
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return

    # Récupère la catégorie cliquée (ccs ou propro)
    category = update.callback_query.data.split(":")[-1]
    # Mémorise la catégorie pour les actions suivantes (TRÈS IMPORTANT)
    context.user_data['admin_product_category'] = category 

    # Construit le menu de gestion standard (avec les callbacks SIMPLES)
    kb = [
        [InlineKeyboardButton("➕ Ajouter (manuel)", callback_data="admin_prod_add")],
        [InlineKeyboardButton("📥 Import CSV",       callback_data="admin_prod_csv")],
        [InlineKeyboardButton("🗑 Supprimer",        callback_data="admin_prod_del")],
        [InlineKeyboardButton("📦 Lister (10)",      callback_data="admin_prod_list")],
        [InlineKeyboardButton("🔙 Retour Menu Admin", callback_data="admin_menu")], 
    ]
    # Affiche le menu en mentionnant la catégorie
    await update.callback_query.message.edit_text(f"🧱 Gestion des produits [{category.upper()}] :", reply_markup=InlineKeyboardMarkup(kb))
   



# ========================== FLOWS VALIDATION ==========================
def reset_session(user_id: int):
    for d in [bot_messages, user_sessions, pending_payments, user_validation_status]:
        list(d.pop(user_id, None) for _ in [0])

# ========================== AUTHENTIFICATION (LEDGER SYSTEM) ==========================

def get_pin_keyboard():
    """Génère le clavier numérique sécurisé."""
    keys = [
        [InlineKeyboardButton("1", callback_data="pin_1"), InlineKeyboardButton("2", callback_data="pin_2"), InlineKeyboardButton("3", callback_data="pin_3")],
        [InlineKeyboardButton("4", callback_data="pin_4"), InlineKeyboardButton("5", callback_data="pin_5"), InlineKeyboardButton("6", callback_data="pin_6")],
        [InlineKeyboardButton("7", callback_data="pin_7"), InlineKeyboardButton("8", callback_data="pin_8"), InlineKeyboardButton("9", callback_data="pin_9")],
        [InlineKeyboardButton("🗑 Effacer", callback_data="pin_del"), InlineKeyboardButton("0", callback_data="pin_0"), InlineKeyboardButton("✅ Valider", callback_data="pin_enter")]
    ]
    return InlineKeyboardMarkup(keys)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    
    # Nettoyage préventif
    context.user_data['temp_pin_input'] = "" 
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    
    # 👇 CORRECTION : On cherche par telegram_id 👇
    cur.execute("SELECT pin_code, username FROM users WHERE telegram_id=?", (user_id,))
    
    row = cur.fetchone()
    con.close()

    # CAS A : Déjà sécurisé -> LOGIN AVEC CLAVIER
    if row and row[0]: 
        # On stocke le message ID pour le supprimer plus tard
        msg = await update.message.reply_text(
            f"🔒 **TERMINAL VERROUILLÉ**\n"
            f"Utilisateur : {row[1] or user.first_name}\n\n"
            f"PIN : `____`", # 4 underscores pour commencer
            reply_markup=get_pin_keyboard(),
            parse_mode="Markdown"
        )
        context.user_data['auth_msg_id'] = msg.message_id
        return ID_AUTH_WAIT_PIN_LOGIN

    # CAS B : Pas de PIN -> SETUP
    else:
        kb = [[InlineKeyboardButton("🆕 Créer un Wallet (Sécuriser)", callback_data="auth_create")]]
        await update.message.reply_text(
            f"🕵️‍♂️ **BIENVENUE SUR NOMEN NESCIO**\n━━━━━━━━━━━━━━━━━━\nVotre ligne n'est pas sécurisée.\nCréez une Identité Cryptographique.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

# --- ÉTAPE A : GÉNÉRATION DE LA SEED (24 MOTS) ---
async def auth_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    # Génération BIP39 (Standard Crypto)
    mnemo = Mnemonic("english")
    seed_phrase = mnemo.generate(strength=256) # 256 bits = 24 mots
    
    # On garde ça en mémoire vive (RAM) le temps qu'il finisse le setup
    context.user_data['temp_seed'] = seed_phrase
    
    # UX : Monospace pour copier facile
    await q.message.edit_text(
        f"🔐 **VOTRE CLÉ PRIVÉE (MASTER KEY)**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Sauvegardez ces mots. C'est le SEUL moyen de récupérer votre compte si vous perdez ce téléphone._\n\n"
        f"`{seed_phrase}`\n\n"
        f"*(Touchez le texte pour copier)*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👇 **DERNIÈRE ÉTAPE**\n"
        f"Définissez un **CODE PIN** (4 à 8 chiffres) pour ce terminal :",
        parse_mode="Markdown"
    )
    return ID_AUTH_WAIT_PIN_CREATE

# --- ÉTAPE B : SAUVEGARDE DU PIN ET DU COMPTE ---
async def auth_create_pin_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pin = update.message.text.strip()
    user_id = str(update.effective_user.id)
    username = update.effective_user.username or "Ghost"
    
    # 1. Validation PIN
    if not pin.isdigit() or not (4 <= len(pin) <= 8):
        await update.message.reply_text("❌ **Erreur :** Le PIN doit faire 4 à 8 chiffres.\nRéessayez :", parse_mode="Markdown")
        return ID_AUTH_WAIT_PIN_CREATE
        
    seed = context.user_data.get('temp_seed')
    if not seed:
        await update.message.reply_text("⚠️ Session expirée. Tapez /start.")
        return ConversationHandler.END
        
    # 2. Enregistrement DB (Update si existe, Insert si nouveau)
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    
    # On regarde si l'user existe déjà (pour ne pas écraser son solde)
    cur.execute("SELECT id FROM users WHERE user_id=?", (user_id,))
    exists = cur.fetchone()
    
    if exists:
        # Mise à jour ancien user -> User Sécurisé
        cur.execute("UPDATE users SET seed_phrase=?, pin_code=?, telegram_id=?, username=? WHERE user_id=?", 
                   (seed, pin, user_id, username, user_id))
    else:
        # Création nouveau user
        cur.execute("INSERT INTO users (user_id, telegram_id, username, seed_phrase, pin_code) VALUES (?, ?, ?, ?, ?)",
                   (user_id, user_id, username, seed, pin))
                   
    con.commit()
    con.close()
    
    # Nettoyage
    context.user_data.pop('temp_seed', None)
    
    await update.message.reply_text(f"✅ **SÉCURITÉ ACTIVÉE**\nCode PIN : `{pin}` enregistré.\n\nConnexion au Mainframe...", parse_mode="Markdown")
    
    # Lancement direct du menu
    await goto_menu(update, context)
    return ConversationHandler.END

# --- ÉTAPE C : LOGIN (VÉRIFICATION PIN) ---
async def auth_pin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    # On répond tout de suite pour éviter que le bouton charge dans le vide
    try: await q.answer() 
    except: pass
    
    data = q.data
    user_id = str(update.effective_user.id)
    
    # Récupère le PIN en cours de frappe
    current_input = context.user_data.get('temp_pin_input', "")

    # 1. Gestion des chiffres
    if data.startswith("pin_") and data[4:].isdigit():
        digit = data.split("_")[1]
        if len(current_input) < 8: # Max 8 chiffres
            current_input += digit
            context.user_data['temp_pin_input'] = current_input
            
            mask = "⚫" * len(current_input) + "_" * (4 - len(current_input)) if len(current_input) < 4 else "⚫" * len(current_input)
            try:
                await q.edit_message_text(
                    f"🔒 **TERMINAL VERROUILLÉ**\nPIN : {mask}",
                    reply_markup=get_pin_keyboard(),
                    parse_mode="Markdown"
                )
            except: pass

    # 2. Gestion Effacer
    elif data == "pin_del":
        current_input = current_input[:-1]
        context.user_data['temp_pin_input'] = current_input
        mask = "⚫" * len(current_input) + "_" * (4 - len(current_input)) if len(current_input) < 4 else "⚫" * len(current_input)
        try:
            await q.edit_message_text(
                f"🔒 **TERMINAL VERROUILLÉ**\nPIN : {mask}",
                reply_markup=get_pin_keyboard(),
                parse_mode="Markdown"
            )
        except: pass

    # 3. Gestion Valider
    elif data == "pin_enter":
        con = sqlite3.connect(DB_NAME)
        cur = con.cursor()
        
        # 👇👇 CORRECTION MAJEURE ICI 👇👇
        # On cherche par telegram_id pour être sûr de trouver le bon user
        cur.execute("SELECT pin_code FROM users WHERE telegram_id=?", (user_id,))
        
        # -------------------------------
        
        row = cur.fetchone()
        con.close()
        
        if row and row[0] == current_input:
            # --- SUCCÈS ---
            
            # A. On essaie de supprimer le clavier
            try:
                await q.message.delete()
            except:
                # B. Si on ne peut pas supprimer, on le modifie pour qu'il disparaisse visuellement
                try:
                    await q.edit_message_text("✅ **Connexion réussie.**", parse_mode="Markdown")
                except: pass
            
            # C. On lance le menu
            await show_main_menu(update.effective_user.id, clear=True)
            return ConversationHandler.END
        else:
            # --- ECHEC ---
            context.user_data['temp_pin_input'] = ""
            try:
                await q.edit_message_text(
                    "⛔ **CODE FAUX !** Réessayez.\nPIN : `____`",
                    reply_markup=get_pin_keyboard(),
                    parse_mode="Markdown"
                )
            except: pass
            return ID_AUTH_WAIT_PIN_LOGIN
            
    return ID_AUTH_WAIT_PIN_LOGIN

# --- BOUTON LOG OUT ---
async def auth_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("🔒 Options de session...")
    
    # On vide la mémoire vive
    context.user_data.clear()
    reset_session(update.effective_user.id)
    
    # Menu à deux choix
    kb = [
        [InlineKeyboardButton("🔒 Verrouiller (PIN requis)", callback_data="auth_lock_only")],
        [InlineKeyboardButton("🚪 Changer de compte (Importer)", callback_data="auth_switch_account")]
    ]
    
    await replace_view(
        q,
        "🛑 **MENU DÉCONNEXION**\n\n"
        "Voulez-vous simplement verrouiller l'écran ou changer d'utilisateur ?",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def auth_lock_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verrouille simplement le terminal."""
    q = update.callback_query
    await q.answer()
    await replace_view(q, "🔒 **Terminal Verrouillé.**\nTapez /start pour revenir.")
    return ConversationHandler.END

async def auth_switch_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prépare le terrain pour un changement de compte."""
    q = update.callback_query
    await q.answer()
    
    # Note : On ne supprime pas l'user de la DB, on le laisse juste "dormant"
    # Le prochain login par seed réattribuera l'ID Telegram.
    
    await replace_view(
        q,
        "🔄 **CHANGEMENT DE COMPTE**\n\n"
        "Pour changer de compte, vous devez posséder sa **Clé Maître (Seed Phrase)**.\n\n"
        "👉 **Cliquez ci-dessous pour commencer :**",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📥 Importer un Wallet", callback_data="auth_import_start")]])
    )
    return ConversationHandler.END

async def auth_import_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    await replace_view(
        q,
        "📝 **IMPORTATION DE COMPTE**\n\n"
        "Veuillez entrer votre **Seed Phrase** (les 12 ou 24 mots) séparés par des espaces.\n\n"
        "⚠️ _Ceci liera ce compte Telegram à ce Wallet._",
        parse_mode="Markdown"
    )
    return ID_AUTH_WAIT_SEED

async def auth_import_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    seed_input = update.message.text.strip()
    telegram_id = str(update.effective_user.id)
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    
    # On cherche si cette seed existe
    cur.execute("SELECT user_id, pin_code, username FROM users WHERE seed_phrase=?", (seed_input,))
    row = cur.fetchone()
    
    if row:
        target_user_id, pin, username = row
        
        # SÉCURITÉ CRITIQUE : 
        # 1. On détache l'ID Telegram de l'ancien compte (où qu'il soit)
        cur.execute("UPDATE users SET telegram_id = NULL WHERE telegram_id = ?", (telegram_id,))
        # 2. On attache l'ID Telegram au nouveau compte cible
        cur.execute("UPDATE users SET telegram_id = ? WHERE user_id = ?", (telegram_id, target_user_id))
        con.commit()
        con.close()
        
        await update.message.reply_text(
            f"✅ **Compte récupéré !**\n"
            f"👤 Bienvenue sur le profil de : {username or 'Utilisateur'}\n"
            f"🔑 PIN requis : `{pin}`\n\n"
            f"Tapez /start pour vous connecter.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    else:
        con.close()
        await update.message.reply_text("❌ **Erreur :** Seed phrase inconnue ou invalide. Réessayez :")
        return ID_AUTH_WAIT_SEED

# ========================== TOOLS / HLR LOGIC ==========================

def get_carrier_info(phone_number):
    """Interroge l'API NumVerify."""
    if not NUMVERIFY_API_KEY:
        return {"success": False, "error": "Clé API manquante (.env)"}

    # Nettoyage basique du numéro
    phone_number = phone_number.replace(" ", "").replace("-", "")

    url = f"http://apilayer.net/api/validate?access_key={NUMVERIFY_API_KEY}&number={phone_number}"
    try:
        response = requests.get(url, timeout=5)
        data = response.json()
        if "success" in data and data["success"] is False:
            return {"success": False, "error": data["error"]["info"]}
        
        return {
            "success": True,
            "valid": data.get("valid", False),
            "number": data.get("number", phone_number),
            "international_format": data.get("international_format", phone_number),
            "carrier": data.get("carrier", "Inconnu"),
            "line_type": data.get("line_type", "N/A"),
            "country_name": data.get("country_name", "N/A"),
            "location": data.get("location", "N/A")
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

async def show_tools_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le menu Tools."""
    q = update.callback_query
    await q.answer()
    
    kb = [
        [
            InlineKeyboardButton("📡 HLR Lookup ($0.50)", callback_data="tool_hlr"),
            InlineKeyboardButton("💳 LuxChecker ($1.00)", callback_data="tool_cc_checker") 
        ],
        [
            InlineKeyboardButton("📱 SMS Activations", callback_data="tool_5sim")
        ],
        [InlineKeyboardButton("🔙 Retour Menu", callback_data="menu_accueil")]
    ]
    
    await replace_view(
        q,
        "⚒️ **BOÎTE À OUTILS**\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Outils d'intelligence et de vérification.\n"
        "👇 Sélectionnez un service :",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SELECT_TOOL

async def tool_ask_hlr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    kb = [[InlineKeyboardButton("🔙 Annuler", callback_data="section_tools")]]
    await replace_view(
        q,
        "📡 **HLR LOOKUP**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Envoyez le numéro (ex: `+15141234567`).\n"
        "💰 Coût: **0.50$**",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return WAIT_HLR_NUMBER

async def tool_process_hlr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    phone = update.message.text.strip()
    price = 0.50

    # Vérification solde
    balance = get_user_balance(user_id)
    if balance < price:
        await update.message.reply_text("❌ Solde insuffisant (0.50$ requis).")
        return SELECT_TOOL

    msg = await update.message.reply_text("📡 **Scan réseau en cours...**", parse_mode="Markdown")

    # Appel API
    result = get_carrier_info(phone)

    if result["success"]:
        # Débit
        update_user_balance(user_id, -price)
        
        icon = "✅" if result["valid"] else "❌"
        status_txt = "VALIDE" if result["valid"] else "INVALIDE"
        
        reply = (
            f"📡 **RÉSULTAT HLR**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 Numéro : `{result['international_format']}`\n"
            f"📊 Statut : {icon} **{status_txt}**\n\n"
            f"🏢 **Opérateur** : {result['carrier']}\n"
            f"📱 **Type** : {result['line_type'].upper()}\n"
            f"🌍 **Pays** : {result['country_name']}\n"
            f"📍 **Lieu** : {result['location']}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Coût : {price}$"
        )
        kb = [[InlineKeyboardButton("🔙 Retour Tools", callback_data="section_tools")]]
        await msg.edit_text(reply, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await msg.edit_text(f"⚠️ Erreur API : {result['error']}")
        
    return SELECT_TOOL

async def tool_placeholder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("🚧 Bientôt disponible !", show_alert=True)
    return SELECT_TOOL


SIM_API_KEY = os.environ.get("SIM_API_KEY")

# --- Configuration des prix (Marge incluse) ---
SMS_CATALOG = {
    "whatsapp": {"price": 2.50, "label": "WhatsApp", "5sim_product": "whatsapp"},
    "telegram": {"price": 2.50, "label": "Telegram", "5sim_product": "telegram"},
    "google":   {"price": 2.00, "label": "Google/Gmail", "5sim_product": "google"},
    "uber":     {"price": 2.00, "label": "Uber", "5sim_product": "uber"},
    "tinder":   {"price": 2.00, "label": "Tinder", "5sim_product": "tinder"},
    "paypal":   {"price": 2.25, "label": "PayPal", "5sim_product": "paypal"}
}

def api_5sim_buy(product, country="usa", operator="virtual51"):
    """Achète un numéro USA (Virtual51 par défaut)."""
    if not SIM_API_KEY: return {"success": False, "error": "Clé API manquante"}
    
    headers = {"Authorization": "Bearer " + SIM_API_KEY, "Accept": "application/json"}
    # On force USA et l'opérateur demandé
    url = f"https://5sim.net/v1/user/buy/activation/{country}/{operator}/{product}"
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        if "id" not in data:
            return {"success": False, "error": str(data)}
        return {"success": True, "id": data['id'], "phone": data['phone'], "expires": data['expires']}
    except Exception as e:
        return {"success": False, "error": str(e)}

def api_5sim_check(order_id):
    """Vérifie le statut."""
    headers = {"Authorization": "Bearer " + SIM_API_KEY, "Accept": "application/json"}
    try:
        r = requests.get(f"https://5sim.net/v1/user/check/{order_id}", headers=headers, timeout=5)
        return r.json()
    except: return {}

def api_5sim_ban(order_id):
    """BANNIT le numéro (remboursement 5sim immédiat si pas de code)."""
    headers = {"Authorization": "Bearer " + SIM_API_KEY, "Accept": "application/json"}
    try:
        # L'action 'ban' signale que le numéro est mauvais/utilisé
        requests.get(f"https://5sim.net/v1/user/ban/{order_id}", headers=headers, timeout=5)
    except: pass

async def show_sms_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le menu SMS USA."""
    q = update.callback_query
    await q.answer()
    
    kb = []
    row = []
    for key, info in SMS_CATALOG.items():
        row.append(InlineKeyboardButton(f"{info['label']} ({info['price']}$)", callback_data=f"buy_sms:{key}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row: kb.append(row)
    
    kb.append([InlineKeyboardButton("🔙 Retour Tools", callback_data="section_tools")])
    
    await replace_view(
        q,
        "🇺🇸 **SMS ACTIVATIONS (USA - Virtual51)**\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Choisissez le service.\n"
        "⚡ _Numéros virtuels haute qualité._\n"
        "🔄 _Bouton 'Ban/Rembourser' disponible si le numéro ne marche pas._",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SELECT_TOOL

async def handle_buy_sms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lance l'achat."""
    q = update.callback_query
    # On parse soit depuis le menu (buy_sms:whatsapp) soit depuis le bouton réessayer (retry_sms:whatsapp)
    data_parts = q.data.split(":")
    service_key = data_parts[1]
    
    await q.answer("Recherche de numéro...")
    
    item = SMS_CATALOG.get(service_key)
    if not item: return
    
    user_id = str(q.from_user.id)
    price = item['price']
    
    # 1. Solde
    if get_user_balance(user_id) < price:
        await q.message.reply_text("❌ Solde insuffisant.")
        return SELECT_TOOL
        
    # 2. Achat
    msg = await q.message.reply_text(f"🇺🇸 Recherche numéro **{item['label']}** (Virtual51)...")
    
    # On demande explicitement USA + virtual51
    res = api_5sim_buy(item['5sim_product'], country="usa", operator="virtual51")
    
    if not res['success']:
        # Fallback : si virtual51 n'a pas de stock, on essaie 'any' opérateur USA
        res = api_5sim_buy(item['5sim_product'], country="usa", operator="any")
        if not res['success']:
            await msg.edit_text(f"❌ Aucun numéro dispo pour le moment.")
            return SELECT_TOOL
        
    # 3. Débit & Affichage
    update_user_balance(user_id, -price)
    order_id = res['id']
    phone = res['phone']
    
    # Lancement du monitoring
    asyncio.create_task(monitor_sms_task(context.application, q.message.chat_id, msg.message_id, order_id, user_id, price, phone, service_key))
    
    return SELECT_TOOL

async def sms_control_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère le clic sur BAN ou CANCEL."""
    q = update.callback_query
    data = q.data # ex: sms_ban_123456_2.50_whatsapp
    
    parts = data.split("_")
    action = parts[1] # ban
    order_id = parts[2]
    price = float(parts[3])
    service_key = parts[4] if len(parts) > 4 else "whatsapp"
    
    user_id = str(q.from_user.id)

    if action == "ban":
        # 1. Appel API Ban (Annule côté 5sim)
        api_5sim_ban(order_id)
        
        # 2. Remboursement Client
        update_user_balance(user_id, +price)
        
        # 3. Menu pour réessayer
        await q.answer("🚫 Numéro banni et remboursé.")
        
        kb = [
            [InlineKeyboardButton("🔄 Essayer un autre numéro", callback_data=f"buy_sms:{service_key}")],
            [InlineKeyboardButton("🔙 Retour Menu", callback_data="section_tools")]
        ]
        
        await q.message.edit_text(
            f"🚫 **Numéro annulé/banni.**\n"
            f"💰 **{price}$** ont été remboursés sur votre solde.\n\n"
            f"Voulez-vous réessayer avec un nouveau numéro ?",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

async def monitor_sms_task(app, chat_id, message_id, order_id, user_id, price, phone, service_key):
    """Boucle de vérification avec bouton d'annulation."""
    start_time = time.time()
    timeout = 900 # 15 min
    
    # Clavier "Ban / Cancel" affiché PENDANT l'attente
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Annuler / Mauvais Numéro", callback_data=f"sms_ban_{order_id}_{price}_{service_key}")]
    ])
    
    while (time.time() - start_time) < timeout:
        status_data = api_5sim_check(order_id)
        status = status_data.get('status')
        sms_list = status_data.get('sms', [])
        
        # SI LE CLIENT A CLIQUÉ SUR BAN ENTRE TEMPS
        if status == "BANNED" or status == "CANCELED":
            return # La tache s'arrête, le handler sms_control_callback a déjà géré l'affichage

        # CODE REÇU
        if sms_list and len(sms_list) > 0:
            code = sms_list[0].get('code')
            if code:
                try:
                    await app.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=f"✅ **CODE REÇU !**\n\n"
                             f"🇺🇸 Service : {service_key.upper()}\n"
                             f"☎️ Numéro : `{phone}`\n"
                             f"💬 **CODE :** `{code}`\n\n"
                             f"💰 Coût final : {price}$",
                        parse_mode="Markdown"
                    )
                    # Finir la commande 5sim
                    headers = {"Authorization": "Bearer " + SIM_API_KEY}
                    requests.get(f"https://5sim.net/v1/user/finish/{order_id}", headers=headers)
                except: pass
                return

        # Mise à jour visuelle
        if int(time.time()) % 10 == 0:
            remaining = int(timeout - (time.time() - start_time))
            mins, secs = divmod(remaining, 60)
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"🇺🇸 **En attente du SMS (USA)...**\n"
                         f"☎️ Numéro : `{phone}`\n"
                         f"⏳ Expire dans : {mins}:{secs:02d}\n\n"
                         f"_Si le numéro est déjà utilisé sur l'app, cliquez sur Annuler._",
                    reply_markup=kb, # On remet le clavier à chaque refresh
                    parse_mode="Markdown"
                )
            except: pass
        
        await asyncio.sleep(5)
        
    # TIMEOUT (Pas de code après 15 min)
    update_user_balance(str(user_id), +price)
    api_5sim_ban(order_id)
    try:
        await app.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=f"❌ **Temps écoulé.**\nLe montant de {price}$ a été remboursé."
        )
    except: pass

async def start_verifier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reset_session(user_id); context.user_data.clear()
    msgx = await update.message.reply_text(msg(user_id, "enter_bulk_qty"), reply_markup=kb_back_cancel())
    context.user_data['verif_flow_msg_ids'] = [msgx.message_id] # <-- CORRECTION ICI
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

    # --- CORRECTION ICI ---
    # On utilise replace_view pour remplacer le menu et on sauvegarde le message
    await replace_view(
        q,
        text=msg(user_id, "enter_bulk_qty"),
        reply_markup=kb_back_cancel()
    )
    # On ajoute l'ID du message à la liste pour qu'il soit supprimé plus tard
    context.user_data['verif_flow_msg_ids'] = [q.message.message_id]
    # --- FIN CORRECTION ---
    
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
        context.user_data.setdefault('verif_flow_msg_ids', []).append(msgx.message_id)
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
    context.user_data.setdefault('verif_flow_msg_ids', []).append(msgx.message_id)
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
    context.user_data.setdefault('verif_flow_msg_ids', []).append(msgx.message_id)
    return MANUAL_NOM

async def manual_receive_nom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tmp_nom"] = update.message.text.strip()
    msgx = await update.message.reply_text(msg(update.effective_user.id, "enter_birth"))
    context.user_data.setdefault('verif_flow_msg_ids', []).append(msgx.message_id)
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
        context.user_data.setdefault('verif_flow_msg_ids', []).append(msgx.message_id)
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

# REMPLACE la fonction bulk_confirm (ligne 1429) par CELLE-CI :

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
    
    # --- MODIFICATION ICI ---
    total_entries = len(entries)
    # Envoie le premier message de l'animation
    msgx = await update.message.reply_text(f"🔄 {msg(user_id, 'decrytage_en_cours').replace('…','.')} (0/{total_entries})")
    # --- FIN MODIFICATION ---

    batch_id = f"{user_id}:{int(datetime.now().timestamp())}"
    
    # --- MODIFICATION ICI ---
    batch_runs[batch_id] = {
        "total": total_entries, 
        "resolved": 0, 
        "notified": False,
        "lock": asyncio.Lock(),
        # On ne stocke plus l'ID du message ici
    }
    # --- FIN MODIFICATION ---

    # --- LANCE L'ANIMATION ---
    asyncio.create_task(animate_wait_message(context, update.effective_chat.id, msgx.message_id, batch_id, lang))
    # --- FIN ---

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
    context.user_data.setdefault('verif_flow_msg_ids', []).append(msgx.message_id) # <-- CORRECTION
    return ASK_NOM

async def receive_nom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nom"] = update.message.text.strip()
    msgx = await update.message.reply_text(msg(update.effective_user.id, "enter_birth"))
    context.user_data.setdefault('verif_flow_msg_ids', []).append(msgx.message_id) # <-- CORRECTION
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
    context.user_data.setdefault('verif_flow_msg_ids', []).append(msgx.message_id)
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

    # --- CORRECTION AJOUTÉE ---
    # On récupère la liste de TOUS les messages de la conversation
    verif_msgs = context.user_data.pop("verif_flow_msg_ids", [])
    
    # On essaie de supprimer le message de l'utilisateur (ex: "Non" ou "Oui")
    try:
        verif_msgs.append(update.message.message_id)
    except Exception:
        pass
    # --- FIN CORRECTION ---

    if reponse in ["non", "no"]:
        # --- CORRECTION PRINCIPALE ---
        # On n'envoie plus les messages "D0005..." ou "Retour...".
        
        for mid in verif_msgs: # Supprime "Combien de permis...", "Prénom...", "Nom...", "Date...", "Permis proposé..."
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=mid)
            except:
                pass
                
        await show_main_menu(user_id, clear=True) # clear=True nettoie les messages de 'bot_messages' (sécurité)
        # --- FIN CORRECTION ---
        return ConversationHandler.END

    if reponse in ["oui", "yes"]:
        balance = get_user_balance(str(user_id))
        if balance < prix:
            keyboard = [[InlineKeyboardButton("💳 Recharger" if lang == "fr" else "💳 Top up", callback_data="add_balance")]]
            
            # On nettoie la conversation avant d'afficher le message de solde
            for mid in verif_msgs:
                try: await context.bot.delete_message(chat_id=user_id, message_id=mid)
                except: pass
            
            await update.message.reply_text(
                msg(user_id, "solde_insuffisant", balance=balance, prix=prix, statut=FORFAITS[statut]['label']),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return ConversationHandler.END # On quitte la convo
        else:
            # On nettoie la conversation avant de payer
            for mid in verif_msgs:
                try: await context.bot.delete_message(chat_id=user_id, message_id=mid)
                except: pass
        
            new_balance = update_user_balance(str(user_id), -prix)
            await update.message.reply_text(
                f"🏦 {msg(user_id, 'balance', balance=new_balance)}\n{FORFAITS[statut]['label']} ({prix:.2f}$/permis)"
            )
            
            msgx = await update.message.reply_text(f"🔄 {msg(user_id, 'decrytage_en_cours').replace('…','.')} (0/1)")

            batch_id = f"{user_id}:{context.user_data.get('code_base','one')}"
            
            batch_runs[batch_id] = {
                "total": 1, 
                "resolved": 0, 
                "notified": False,
                "lock": asyncio.Lock(),
                # On ne stocke plus l'ID du message ici
            }

            asyncio.create_task(animate_wait_message(context, update.effective_chat.id, msgx.message_id, batch_id, lang))

            await launch_parallel_calls(
                base, user_id, num_calls=10,
                fullname=fullname, formatted=formatted,
                batch_id=batch_id
            )
            return ConversationHandler.END

    # Si la réponse n'est ni "oui" ni "non", on repose la question
    msgx = await update.message.reply_text("❓ Oui ou Non / Yes or No?")
    # On ajoute la nouvelle question à la liste des messages à supprimer
    context.user_data.setdefault('verif_flow_msg_ids', []).append(msgx.message_id)
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

#
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

    # --- MODIFICATION : Ajout des champs CC ---
    cc = pick('CC')
    exp = pick('EXP')
    cvc = pick('CVC')
    # --- FIN MODIFICATION ---

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
        # --- MODIFICATION : Ajout des champs CC ---
        f"CC: {cc}",
        f"EXP: {exp}",
        f"CVC: {cvc}",
        # --- FIN MODIFICATION ---
        
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
    # Filtre les lignes vides (ex: si CVC n'est pas fourni)
    content = "\n".join(l for l in content_lines if ':' in l and l.split(':', 1)[1].strip())

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


# ========================== ADMIN (sections ajustées) ==========================

async def admin_prod_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    db = _get_db_from_context(context)
    category = context.user_data.get('admin_product_category', 'propro') # Récupère la catégorie
    if not db:
        await update.callback_query.message.reply_text("DB indisponible.")
        return

    c = db.cursor()
    rows = c.execute(
        "SELECT id, title, price, stock FROM products WHERE category=? ORDER BY id DESC LIMIT 10", (category,)
    ).fetchall()
    if not rows:
        await update.callback_query.message.reply_text("Aucun produit.")
        return

    txt = "\n".join([f"#{r[0]} — {r[1]} — {r[2]:.2f}$ — stock={r[3]}" for r in rows])
    await update.callback_query.message.reply_text(txt)


# --- keep this function as-is, just ADD the line that sets the flag (new) ---
async def admin_prod_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Vérifie que seul l'admin peut utiliser cette commande
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    # --- MODIFICATION : Affiche le prompt selon la catégorie ---
    category = context.user_data.get('admin_product_category', 'propro') # Récupère la catégorie
    context.user_data["awaiting_admin_product_text"] = True # Indique qu'on attend du texte

    if category == 'ccs':
        # --- NOUVEAU PROMPT POUR CCS ---
        await update.callback_query.message.reply_text(
            "📦 *Ajout de produit (CC's)*\n"
            "Veuillez coller les infos dans ce format exact :\n\n"
            "CC: 1234567812345678\n"
            "EXP: 12/2027\n"
            "CVC: 123\n"
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
            "BASE: TEST\n"
            "PRICE: 50.00\n\n"
            "_Chaque ajout crée 1 produit (stock = 1)._",
            parse_mode="Markdown"
        )
    else: # 'propro' or default
        # --- ANCIEN PROMPT POUR PRO'S ---
        await update.callback_query.message.reply_text(
            "📦 *Ajout de produit (Pro's)*\n" 
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
    # --- FIN MODIFICATION ---

    return ADMIN_WAIT_PRODUCT_TEXT


# --- modify admin_prod_add_receive with guards ---
async def admin_prod_add_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # --- SECURITÉ AJOUTÉE ---
    if not update.effective_user:
        return # Ignore si pas d'utilisateur (ex: channel post)
    # ------------------------

    # Guard 1: only admin
    if str(update.effective_user.id) not in ADMIN_IDS:
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
    category = context.user_data.get('admin_product_category', 'propro') # Récupère la catégorie

    fields = []; values = []
    if 'title'    in cols: fields.append('title');    values.append(data['title'])
    if 'content'  in cols: fields.append('content');  values.append(data['content'])
    if 'price'    in cols: fields.append('price');    values.append(data['price'])
    if 'tier'     in cols: fields.append('tier');     values.append(data['tier'])
    if 'city'     in cols: fields.append('city');     values.append(data['city'])
    if 'year'     in cols: fields.append('year');     values.append(data['year'])
    if 'stock'    in cols: fields.append('stock');    values.append(data['stock'])
    if 'category' in cols: fields.append('category'); values.append(category)
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
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    await update.callback_query.message.reply_text(
        "Envoie le CSV (UTF-8). En-têtes acceptées (flexibles):\n"
        "sin,dl,first,last,dob,address,city,postal,email,phone,base,price,stock\n"
        "→ 1 ligne = 1 produit (stock par défaut=1)."
    )
    return ADMIN_WAIT_CSV

# ================= IMPORT LOCAL (SSH) =================
async def admin_local_import_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. Vérification Admin
    user_id_str = str(update.effective_user.id)
    if user_id_str not in ADMIN_IDS:
        return

    # 2. Vérification du fichier sur le serveur
    file_path = '/home/johnmsaaq/bot-nomen/import.csv'  # Chemin confirmé
    if not os.path.exists(file_path):
        # Fallback au cas où
        file_path = 'import.csv' 
        if not os.path.exists(file_path):
            await update.message.reply_text(f"❌ Fichier introuvable. Assurez-vous d'avoir envoyé 'import.csv' via SCP.")
            return

    await update.message.reply_text(f"📂 Fichier trouvé ! Analyse en cours...")

    # 3. Connexion DB
    db = _get_db_from_context(context)
    c = db.cursor()
    
    # 4. Lecture du fichier
    try:
        # On tente utf-8, sinon latin-1 (pour les accents Québec)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                text = f.read()
        except UnicodeDecodeError:
            with open(file_path, 'r', encoding='latin-1') as f:
                text = f.read()

        import csv, re
        from io import StringIO
        
        # Détection du délimiteur
        try:
            sample = "\n".join(text.splitlines()[:5])
            dialect = csv.Sniffer().sniff(sample)
            delimiter = dialect.delimiter
        except:
            delimiter = ','

        rdr = csv.DictReader(StringIO(text), delimiter=delimiter)
        
        # Nettoyage des en-têtes (minuscules + sans espaces)
        if rdr.fieldnames:
            rdr.fieldnames = [h.lower().strip() for h in rdr.fieldnames if h]

        # Paramètres
        inserted = 0
        cols = _guess_columns(c)
        category = context.user_data.get('admin_product_category', 'propro')

        # Colonnes qu'on ne veut pas voir en doublon dans la description
        known_keys = {
            'sin', 'sin_nas', 'dob', 'datenais', 'phone', 'telephone', 
            'address', 'adr', 'unit', 'city', 'muni', 'prov', 'prn_nom', 
            'first', 'last', 'email', 'dl', 'postal', 'password', 'price', 
            'base', 'stock', 'cc', 'exp', 'cvc', 'nom', 'prenom', 
            'langue', 'id'
        }

        # 5. Boucle ligne par ligne
        line_num = 1
        for row in rdr:
            line_num += 1
            try:
                # Nettoyage des données de la ligne
                r = {k.lower().strip(): v.strip() for k, v in row.items() if k}

                # --- LOGIQUE SPÉCIFIQUE À TON FICHIER ---
                
                # A. Nom/Prénom inversé (PRN_NOM: "LAROCHE LAURENCE")
                full_raw = r.get('prn_nom', '')
                last, first = "", ""
                if full_raw:
                    parts = full_raw.split(maxsplit=1)
                    if len(parts) >= 1: last = parts[0]   # 1er mot = NOM
                    if len(parts) == 2: first = parts[1]  # Reste = PRÉNOM
                
                # B. Adresse avec unité
                raw_adr = r.get('adr') or r.get('address') or ""
                unit = r.get('unit', '')
                address = f"{unit}-{raw_adr}" if unit else raw_adr

                # C. Ville et Province
                city = r.get('muni') or r.get('city') or ""
                prov = r.get('prov') or ""
                if prov and city: city = f"{city} ({prov})"

                # D. Prix et Base (depuis ton fichier)
                price = _parse_price(r.get('price') or '0')
                base = (r.get('base') or 'Import Local').strip()

                # E. Autres champs
                sin = r.get('sin_nas') or r.get('sin') or ""
                phone = r.get('telephone') or r.get('phone') or ""
                dob = r.get('datenais') or r.get('dob') or ""
                
                # Nettoyage date (enlève les guillemets '1982-10-05')
                dob = dob.replace("'", "").replace('"', "")
                
                # Extraction année
                year = ''
                m = re.search(r'(\d{4})', dob)
                if m: year = m.group(1)

                # F. Construction du contenu
                content_lines = [
                    f"SIN: {sin}",
                    f"FIRST NAME: {first}",
                    f"LAST NAME: {last}",
                    f"DOB: {dob}",
                    f"ADRESSE: {address}",
                    f"CITY: {city}",
                    f"PHONE NUMBER: {phone}",
                    f"BASE: {base}",
                    f"PRICE: {price:.2f} CAD"
                ]
                
                # Ajoute l'ID original à la fin
                orig_id = r.get('id', '')
                if orig_id: content_lines.append(f"ID: {orig_id}")

                # Ramasse-miettes (ajoute les colonnes inconnues)
                for k, v in r.items():
                    if k not in known_keys and v:
                        content_lines.append(f"{k.upper()}: {v}")
                
                content = "\n".join(content_lines)

                # G. Titre
                title = f"{(first + ' ' + last).strip().upper()} • {year} • {city.upper()}".strip()

                # H. Insertion SQL
                fields, values = [], []
                if 'title'    in cols: fields.append('title');    values.append(title)
                if 'content'  in cols: fields.append('content');  values.append(content)
                if 'price'    in cols: fields.append('price');    values.append(price)
                if 'tier'     in cols: fields.append('tier');     values.append(base)
                if 'city'     in cols: fields.append('city');     values.append(city)
                if 'year'     in cols: fields.append('year');     values.append(year)
                if 'stock'    in cols: fields.append('stock');    values.append(1)
                if 'category' in cols: fields.append('category'); values.append(category)
                if 'currency' in cols: fields.append('currency'); values.append('CAD')
                if 'is_active' in cols: fields.append('is_active'); values.append(1)

                q_sql = f"INSERT INTO products ({', '.join(fields)}) VALUES ({', '.join(['?']*len(values))})"
                c.execute(q_sql, values)
                inserted += 1

            except Exception as e:
                print(f"Erreur ligne {line_num}: {e}")
                continue

        db.commit()
        await update.message.reply_text(f"✅ Import terminé ! {inserted} produits ajoutés.")

    except Exception as e:
        await update.message.reply_text(f"❌ Erreur critique : {e}")

async def admin_prod_csv_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_str = str(update.effective_user.id)
    if user_id_str not in ADMIN_IDS:
        return

    doc = update.message.document
    if not doc:
        await update.message.reply_text("Aucun fichier. Réessaie.")
        return ADMIN_WAIT_CSV

    print(f"[CSV Import] Fichier reçu: {doc.file_name}")

    db = _get_db_from_context(context)
    if not db:
        await update.message.reply_text("DB indisponible.")
        return ConversationHandler.END

    c = db.cursor()
    inserted = 0
    
    try:
        from io import BytesIO, StringIO
        f = await context.bot.get_file(doc.file_id)
        bio = BytesIO()
        await f.download_to_memory(out=bio)
        bio.seek(0)
        
        try:
            text = bio.read().decode('utf-8')
        except UnicodeDecodeError:
            try:
                bio.seek(0)
                text = bio.read().decode('latin-1')
            except Exception as decode_err:
                 await update.message.reply_text(f"❌ Erreur encodage : {decode_err}")
                 return ConversationHandler.END

        import csv, re
        try:
            sample_text = "\n".join(text.splitlines()[:5])
            dialect = csv.Sniffer().sniff(sample_text)
            delimiter = dialect.delimiter
        except csv.Error:
             delimiter = ','

        rdr = csv.DictReader(StringIO(text), delimiter=delimiter)
        
        try:
            if rdr.fieldnames:
                rdr.fieldnames = [h.lower().strip() for h in rdr.fieldnames if h]
            
            headers = rdr.fieldnames or []
            if not headers: raise ValueError("En-têtes vides")

            # Validation minimale (Vérifie si on a Date et Nom)
            headers_set = set(headers)
            has_dob = 'dob' in headers_set or 'datenais' in headers_set
            has_name = 'first' in headers_set or 'prn_nom' in headers_set or 'nom' in headers_set
            
            if not (has_dob and has_name):
                 await update.message.reply_text(f"❌ Erreur colonnes: Je ne trouve pas 'DateNais' ou 'PRN_NOM'.")
                 return ConversationHandler.END

        except Exception as header_err:
             await update.message.reply_text(f"❌ Erreur lecture en-têtes : {header_err}")
             return ConversationHandler.END

        cols = _guess_columns(c)
        category = context.user_data.get('admin_product_category', 'propro')
        
        # LISTE DES COLONNES À IGNORER DANS LE "RAMASSE-MIETTES"
        # On ignore 'price' et 'base' car on les traite manuellement.
        # On ignore 'langue' car tu ne veux pas l'afficher.
        # On laisse 'id' libre pour qu'il soit ajouté automatiquement.
        known_keys = {
            'sin', 'sin_nas', 'dob', 'datenais', 'phone', 'telephone', 
            'address', 'adr', 'unit', 'city', 'muni', 'prov', 'prn_nom', 
            'first', 'last', 'email', 'dl', 'postal', 'password', 'price', 
            'base', 'stock', 'cc', 'exp', 'cvc', 'nom', 'prenom', 
            'langue' 
        }

        line_num = 1
        for row in rdr:
            line_num += 1
            try:
                r_clean = {k.lower().strip(): v.strip() for k, v in row.items() if k}

                # --- 1. SÉPARATION DU NOM (INVERSÉE) ---
                full_name_raw = r_clean.get('prn_nom') or ''
                first = r_clean.get('first') or r_clean.get('prenom') or ''
                last = r_clean.get('last') or r_clean.get('nom') or ''

                if not first and full_name_raw:
                    parts = full_name_raw.split(maxsplit=1)
                    # INVERSION : 1er mot = NOM, Reste = PRÉNOM
                    if len(parts) >= 1: last = parts[0] 
                    if len(parts) == 2: first = parts[1] 

                # --- 2. LECTURE DES CHAMPS ---
                sin = r_clean.get('sin') or r_clean.get('sin_nas') or ''
                dob = r_clean.get('dob') or r_clean.get('datenais') or ''
                phone = r_clean.get('phone') or r_clean.get('telephone') or ''
                
                # Adresse (gestion de l'unité/appartement)
                raw_addr = r_clean.get('address') or r_clean.get('adr') or ''
                unit = r_clean.get('unit') or ''
                address = f"{unit}-{raw_addr}" if unit else raw_addr
                
                # Ville
                city = r_clean.get('city') or r_clean.get('muni') or ''
                prov = r_clean.get('prov') or ''
                if prov and city: city = f"{city} ({prov})"

                email = r_clean.get('email') or ''
                dl = r_clean.get('dl') or ''
                postal = r_clean.get('postal') or ''
                password = r_clean.get('password') or ''
                
                # --- NOUVEAU : GESTION PRIX ET BASE ---
                # Lit le prix depuis ton fichier (ex: 2)
                price = _parse_price(r_clean.get('price') or '0')
                # Lit la base depuis ton fichier (ex: DEJ)
                base = (r_clean.get('base') or 'Import Excel').strip()
                
                # --- 3. CRÉATION DE LA FICHE PRODUIT ---
                content_lines = [
                    f"SIN: {sin}", f"DL: {dl}",
                    f"FIRST NAME: {first}", f"LAST NAME: {last}",
                    f"DOB(DD/MM/YYYY): {dob}", 
                    f"ADRESSE: {address}", f"CITY: {city}", f"CODE POSTAL: {postal}",
                    f"EMAIL: {email}", f"PHONE NUMBER: {phone}",
                    f"PASSWORD: {password}",
                    f"BASE: {base}",            # Affiche ta base (ex: BASE: DEJ)
                    f"PRICE: {price:.2f} CAD",  # Affiche ton prix (ex: PRICE: 2.00 CAD)
                ]

                # --- 4. LE RAMASSE-MIETTES (Ajoute ID, ignore LANGUE) ---
                for key, val in r_clean.items():
                    if key not in known_keys and val:
                        content_lines.append(f"{key.upper()}: {val}")

                content = "\n".join(l for l in content_lines if ':' in l and l.split(':', 1)[1].strip())

                if not first and not full_name_raw: 
                    continue 

                try: stock = int((r_clean.get('stock') or '1').strip() or 1)
                except: stock = 1

                year = ''
                m = re.search(r'(\d{4})', dob)
                if m: year = m.group(1)
                
                title = f"{(first + ' ' + last).strip().upper()} • {year} • {city.upper()}".strip()

                fields, values = [], []
                if 'title'    in cols: fields.append('title');    values.append(title)
                if 'content'  in cols: fields.append('content');  values.append(content)
                if 'price'    in cols: fields.append('price');    values.append(price)
                if 'tier'     in cols: fields.append('tier');     values.append(base)
                if 'city'     in cols: fields.append('city');     values.append(city)
                if 'year'     in cols: fields.append('year');     values.append(year)
                if 'stock'    in cols: fields.append('stock');    values.append(stock)
                if 'category' in cols: fields.append('category'); values.append(category)
                if 'currency' in cols: fields.append('currency'); values.append('CAD')
                if 'is_active' in cols: fields.append('is_active'); values.append(1)

                q = f"INSERT INTO products ({', '.join(fields)}) VALUES ({', '.join(['?']*len(values))})"
                c.execute(q, values)
                inserted += 1
            
            except Exception as row_err:
                 print(f"Erreur Ligne {line_num}: {row_err}")

        if inserted > 0:
            db.commit()
            await update.message.reply_text(f"✅ Import terminé. {inserted} produit(s) ajouté(s).")
        else:
             await update.message.reply_text("⚠️ Aucune ligne valide trouvée.")

    except Exception as e:
         try: db.rollback()
         except: pass
         await update.message.reply_text(f"❌ Erreur critique : {e}")
    
    return ConversationHandler.END

# ========================== ADMIN (sections ajustées) ==========================

async def admin_prod_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    db = _get_db_from_context(context)
    category = context.user_data.get('admin_product_category', 'propro') # Récupère la catégorie
    if not db:
        await update.callback_query.message.reply_text("DB indisponible.")
        return

    c = db.cursor()
    rows = c.execute(
        "SELECT id, title FROM products WHERE category=? ORDER BY id DESC LIMIT 10", (category,)
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
    if str(update.effective_user.id) not in ADMIN_IDS:
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


# REMPLACE la fonction admin_users (ligne 1618) par CELLE-CI :

def get_users_paginated(page=0, per_page=10):
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    
    # Compte total
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    
    # Récupération de la page
    offset = page * per_page
    cur.execute("""
        SELECT telegram_id, balance, forfait, total_recharge 
        FROM users 
        ORDER BY rowid DESC 
        LIMIT ? OFFSET ?
    """, (per_page, offset))
    rows = cur.fetchall()
    con.close()
    return rows, total



async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.callback_query.answer("Accès refusé.")
        return
    
    q = update.callback_query
    await q.answer()
    
    # Récupération de la page depuis le bouton (ex: "admin_users:page:2")
    page = 0
    data = q.data
    if ":page:" in data:
        try:
            page = int(data.split(":page:")[1])
        except:
            page = 0
            
    # Récupération des données (10 par page)
    users, total_count = get_users_paginated(page, 10)
    total_pages = max(1, (total_count + 9) // 10)
    
    if not users:
        await replace_view(q, "Aucun utilisateur trouvé.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="admin_menu")]]))
        return

    # Construction de la liste
    keyboard = []
    for u in users:
        tid = str(u[0])
        bal = float(u[1] or 0.0)
        tier = (u[2] or 'bronze')
        # --- ICI : ON AFFICHE L'ID COMPLET ---
        label = f"🆔 {tid} | {tier.upper()} | {bal:.2f}$"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"admin_adjust_{tid}")])

    # Barre de Navigation
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Préc.", callback_data=f"admin_users:page:{page-1}"))
    
    nav_row.append(InlineKeyboardButton(f"{page+1} / {total_pages}", callback_data="noop"))
    
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Suiv. ➡️", callback_data=f"admin_users:page:{page+1}"))
        
    if nav_row: keyboard.append(nav_row)
    
    # Boutons d'actions
    keyboard.append([InlineKeyboardButton("🔍 Chercher un ID", callback_data="admin_search_user_start")])
    keyboard.append([InlineKeyboardButton("🔙 Menu Admin", callback_data="admin_menu")])

    await replace_view(
        q, 
        f"👥 **Gestion des Utilisateurs** ({total_count} total)\nPage {page+1}", 
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# --- AJOUTE AUSSI CES DEUX FONCTIONS POUR LA RECHERCHE (JUSTE APRÈS) ---

async def admin_search_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("🔍 **Recherche**\nEntrez l'ID Telegram complet :")
    return ADMIN_WAIT_SEARCH_ID

async def admin_search_user_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search_term = update.message.text.strip()
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (search_term,))
    row = cur.fetchone()
    con.close()
    
    if row:
        # On simule l'ouverture du menu d'ajustement pour cet ID
        telegram_id = row[0]
        context.user_data["target_user"] = telegram_id
        
        # On affiche directement le menu d'ajustement (copie simplifiée de admin_adjust_user)
        keyboard = [
            [InlineKeyboardButton("+10$", callback_data=f"admin_adjval_{telegram_id}_10"), InlineKeyboardButton("+100$", callback_data=f"admin_adjval_{telegram_id}_100")],
            [InlineKeyboardButton("-10$", callback_data=f"admin_adjval_{telegram_id}_-10"), InlineKeyboardButton("-100$", callback_data=f"admin_adjval_{telegram_id}_-100")],
            [InlineKeyboardButton("Montant personnalisé", callback_data=f"admin_customamount_{telegram_id}")],
            [InlineKeyboardButton("🔙 Liste", callback_data="admin_users")]
        ]
        await update.message.reply_text(f"✅ **Utilisateur {telegram_id}** trouvé.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return ConversationHandler.END
    else:
        await update.message.reply_text("❌ ID introuvable. Réessayez ou /cancel.")
        return ADMIN_WAIT_SEARCH_ID

async def admin_adjust_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    
    q = update.callback_query # CORRIGÉ : Définit q
    await q.answer()

    cbdata = q.data # CORRIGÉ : Utilise q.data
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
        [InlineKeyboardButton("⬅️ Retour", callback_data="admin_users")], # Ce bouton ramène à la liste des utilisateurs, c'est bon
    ]
    
    # CORRIGÉ: Utilise replace_view au lieu de reply_text
    await replace_view(
        q,
        f"Quel ajustement pour l'utilisateur {telegram_id[-5:]} ?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_adjust_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) not in ADMIN_IDS:
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
    if str(update.effective_user.id) not in ADMIN_IDS:
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
    # --- SECURITÉ AJOUTÉE ---
    if not update.effective_user:
        return # Ignore si pas d'utilisateur
    # ------------------------

    # Guard 1: only admin
    if str(update.effective_user.id) not in ADMIN_IDS:
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


# REMPLACE la fonction admin_setstatut (ligne 1709) par CELLE-CI :

async def admin_setstatut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    q = update.callback_query
    try:
        await q.answer()
        
        # CORRIGÉ: Définit un clavier de retour au menu admin
        kb_back_admin = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="admin_menu")]])

        users = get_users()
        keyboard = []
        for u in users:
            tid   = str(u[0])
            tier  = (u[4] or 'bronze')
            label_tier = FORFAITS.get(tier, {}).get('label', tier)
            display = f"{tid[-5:]} {label_tier}"
            keyboard.append([InlineKeyboardButton(display, callback_data=f"admin_userstatut_{tid}")])

        if not keyboard:
            # CORRIGÉ: Utilise replace_view
            await replace_view(q, "Aucun utilisateur trouvé / No user found.", reply_markup=kb_back_admin)
            return
        if len(keyboard) > 80:
            keyboard = keyboard[:80]

        # CORRIGÉ: Ajoute le bouton Retour
        keyboard.append([InlineKeyboardButton("⬅️ Retour", callback_data="admin_menu")])

        # CORRIGÉ: Utilise replace_view
        await replace_view(
            q,
            "Sélectionner un utilisateur :",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        log(f"admin_setstatut error: {e}", str(update.effective_user.id), "error")
        try:
            # CORRIGÉ: Utilise replace_view pour le message d'erreur
            await replace_view(
                q, 
                "⚠️ Erreur lors du chargement de la liste.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="admin_menu")]])
            )
        except: pass

# REMPLACE la fonction admin_userstatut (ligne 1735) par CELLE-CI :

async def admin_userstatut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    
    q = update.callback_query # CORRIGÉ : Définit q
    await q.answer()

    cbdata = q.data # CORRIGÉ : Utilise q.data
    telegram_id = cbdata.replace("admin_userstatut_", "")
    keyboard = [
        [InlineKeyboardButton(f"{FORFAITS['bronze']['label']}",   callback_data=f"admin_statut_{telegram_id}_bronze")],
        [InlineKeyboardButton(f"{FORFAITS['silver']['label']}",   callback_data=f"admin_statut_{telegram_id}_silver")],
        [InlineKeyboardButton(f"{FORFAITS['gold']['label']}",     callback_data=f"admin_statut_{telegram_id}_gold")],
        [InlineKeyboardButton(f"{FORFAITS['platinum']['label']}", callback_data=f"admin_statut_{telegram_id}_platinum")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="admin_setstatut")] # CORRIGÉ : Bouton Retour
    ]
    
    # CORRIGÉ: Utilise replace_view
    await replace_view(
        q,
        "Nouveau statut :",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_setstatut_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) not in ADMIN_IDS:
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
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return
    await update.callback_query.answer()

    await update.callback_query.message.reply_text("⏳ Redémarrage du bot en cours…")
    try:
        subprocess.Popen(["sudo", "systemctl", "restart", "telegrambot.service"])
    except Exception as e:
        await update.callback_query.message.reply_text(f"Erreur reboot : {e}")


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Vérification Admin
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.callback_query.answer("Accès refusé / Access denied.")
        return

    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass

    # --- MODIFICATION : Ajoute les deux boutons produits ici ---
    keyboard = [
        [InlineKeyboardButton("📨 Gestion Tickets", callback_data="admin_tickets_list")],
        [InlineKeyboardButton("👥 Utilisateurs", callback_data="admin_users")],
        [InlineKeyboardButton("🏷 Forfait utilisateur", callback_data="admin_setstatut")],
        [InlineKeyboardButton("🔁 Redémarrer le bot", callback_data="admin_hard_reboot")],
        # Nouveaux boutons pour chaque catégorie
        [InlineKeyboardButton("💳 Produits Cc's", callback_data="admin_cat_menu:ccs")],
        [InlineKeyboardButton("🧱 Produits Pro's", callback_data="admin_cat_menu:propro")],
        [InlineKeyboardButton("⏱️ Réglages Temps IVR", callback_data="admin_ivr_settings")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")],
    ]
    # --- FIN MODIFICATION ---

    try:
        # S'assure d'éditer le message existant si possible
        await q.message.edit_text("⚙️ Menu admin :", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        # Si l’edit échoue (message trop ancien), on envoie un nouveau
        await q.message.reply_text("⚙️ Menu admin :", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_ivr_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le menu de réglage des temps IVR."""
    if str(update.effective_user.id) not in ADMIN_IDS:
        return
    q = update.callback_query
    await q.answer()
    
    timings = get_ivr_timings()
    
    kb = [
        [InlineKeyboardButton(f"Pause 1 (Ouvert): {timings.get('open_1', 55)}s", callback_data="admin_ivr_change:open_1")],
        [InlineKeyboardButton(f"Pause 2 (Ouvert): {timings.get('open_2', 41)}s", callback_data="admin_ivr_change:open_2")],
        [InlineKeyboardButton(f"Pause 1 (Fermé): {timings.get('closed_1', 40)}s", callback_data="admin_ivr_change:closed_1")],
        [InlineKeyboardButton(f"Pause 2 (Fermé): {timings.get('closed_2', 45)}s", callback_data="admin_ivr_change:closed_2")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="admin_menu")]
    ]
    
    await replace_view(q, "⏱️ Régler les temps d'attente IVR (en secondes) :", reply_markup=InlineKeyboardMarkup(kb))
    # On ne retourne pas de nouvel état, on reste dans le menu admin principal

async def admin_ivr_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Demande la nouvelle valeur pour un temps de pause."""
    if str(update.effective_user.id) not in ADMIN_IDS:
        return
    q = update.callback_query
    await q.answer()
    
    key_to_change = q.data.split(":")[-1]
    context.user_data['ivr_timing_key'] = key_to_change
    
    await q.message.reply_text(f"Entrez la nouvelle valeur en secondes pour '{key_to_change}' (ex: 50) :")
    return ADMIN_IVR_AWAIT_VALUE # Démarre la conversation pour attendre la réponse

async def admin_ivr_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reçoit la nouvelle valeur, la sauvegarde, et ré-affiche le menu."""
    if str(update.effective_user.id) not in ADMIN_IDS:
        return
        
    key = context.user_data.pop('ivr_timing_key', None)
    if not key:
        return ConversationHandler.END
    
    try:
        value = int(update.message.text.strip())
        if value < 0 or value > 120: # Sécurité: 0-120 secondes
            raise ValueError("Valeur hors limites (0-120s)")
    except Exception as e:
        await update.message.reply_text(f"Valeur invalide: {e}. Entrez un nombre (ex: 50).")
        context.user_data['ivr_timing_key'] = key # Redemande
        return ADMIN_IVR_AWAIT_VALUE 
    
    # Sauvegarder la valeur
    set_ivr_timing(key, value)
    
    await update.message.reply_text(f"✅ Valeur pour '{key}' mise à jour à {value}s.")
    
    # Recréer un faux "update" pour rappeler admin_ivr_settings
    class MockCallbackQuery:
        message = update.message
        async def answer(self): pass
        async def delete(self): pass # Simule la suppression
    
    # Simule un objet q.message.delete()
    mock_update = Update(update.update_id, callback_query=MockCallbackQuery())
    
    # On simule un "replace_view" manuel pour ré-afficher le menu des réglages
    try:
        await update.message.delete() # Supprime le message de la valeur (ex: "50")
    except: pass

    timings = get_ivr_timings()
    kb = [
        [InlineKeyboardButton(f"Pause 1 (Ouvert): {timings.get('open_1', 55)}s", callback_data="admin_ivr_change:open_1")],
        [InlineKeyboardButton(f"Pause 2 (Ouvert): {timings.get('open_2', 41)}s", callback_data="admin_ivr_change:open_2")],
        [InlineKeyboardButton(f"Pause 1 (Fermé): {timings.get('closed_1', 40)}s", callback_data="admin_ivr_change:closed_1")],
        [InlineKeyboardButton(f"Pause 2 (Fermé): {timings.get('closed_2', 45)}s", callback_data="admin_ivr_change:closed_2")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="admin_menu")]
    ]
    await update.message.reply_text("⏱️ Régler les temps d'attente IVR (en secondes) :", reply_markup=InlineKeyboardMarkup(kb))
    
    return ConversationHandler.END

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

# REMPLACE la fonction twilio_handler (ligne 1749) par CELLE-CI :

@app.route("/twilio_handler", methods=["GET", "POST"], endpoint="twilio_handler_main")
def twilio_handler():
    code = request.args.get("code", "")
    uid = request.args.get("uid")
    bid = request.args.get("bid")
    call_sid = request.form.get("CallSid", f"call_{datetime.now().timestamp()}")
    
    # --- MODIFICATION ---
    # Charge les temps de pause depuis le fichier
    timings = get_ivr_timings()
    # --- FIN MODIFICATION ---

    print(f"📞 Appel reçu — CallSid: {call_sid} | UID: {uid} | Code: {code} | BID: {bid}")

    active_calls[call_sid] = {
        "user_id": uid,
        "code": code,
        "batch_id": bid
    }

    r = VoiceResponse()

    if is_system_open():
        print("🕐 SAAQ ouverte — Menu 4-4-6 déclenché")
        # --- MODIFICATION ---
        r.pause(length=timings.get("open_1", 55)) # Default 55
        r.play(digits="4")
        r.pause(length=3) # Garde les petites pauses hard-codées
        r.play(digits="4")
        r.pause(length=3)
        r.play(digits="6")
        r.pause(length=timings.get("open_2", 41)) # Default 41
        # --- FIN MODIFICATION ---
    else:
        print("🕐 SAAQ fermée — Menu 1-1 déclenché")
        # --- MODIFICATION ---
        r.pause(length=timings.get("closed_1", 40)) # Default 40
        r.play(digits="1")
        r.pause(length=3)
        r.play(digits="1")
        r.pause(length=timings.get("closed_2", 45)) # Default 45
        # --- FIN MODIFICATION ---

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
                
                # --- DÉBUT DES MODIFICATIONS ---
                
                # 1. Gère l'envoi du message de résultat (valide/invalide)
                if add_result_text:
                    await app_telegram.bot.send_message(chat_id=user_id, text=add_result_text)
                
                # 2. Gère la mise à jour du compteur de permis résolus
                #    (Se déclenche 1 fois par personne, soit si valide, soit si 10e échec)
                if not state["resolved"] and (add_result_text or state.get("total", 0) >= 10): 
                    state["resolved"] = True
                    br["resolved"] += 1 # Incrémente le compteur global du batch
                
                # 3. Gère la fin du batch complet
                if br["resolved"] >= br["total"] and not br["notified"]:
                    br["notified"] = True # CELA VA ARRÊTER LA BOUCLE D'ANIMATION
                    
                    # On n'édite plus ou ne supprime plus le message d'ici.
                    # La tâche d'animation s'arrêtera d'elle-même.

                    await app_telegram.bot.send_message(chat_id=user_id, text="🔓 Fin du décryptage.")
                    await show_main_menu(user_id)
                # --- FIN DES MODIFICATIONS ---
                    
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

    # --- DEBUT DU MOUCHARD ---
    import os
    pid = os.getpid()
    query_data = update.callback_query.data
    user_id = update.effective_user.id
    print(f"\n[ESPION] 🕵️ REÇU callback: '{query_data}' | User: {user_id} | PID Processus: {pid}", flush=True)
    # --- FIN DU MOUCHARD ---

    q = update.callback_query
    await q.answer()
    print(f"[DBG] menu_handler triggered with data={q.data}", flush=True)

    data = q.data

    if data == "menu_accueil":
        return await goto_menu(update, context)

    # --- DÉBUT MODIFICATION ---


    if data.startswith("ccs:page:"):
        page = int(data.split(":")[2])
        tier = context.user_data.get("prod_tier")
        return await show_products_ccs(update, context, page=page, tier=tier)

    if data == "propro":
        context.user_data["prod_tier"] = None
        return await show_products(update, context, page=0, tier=None)

    if data.startswith("prod:page:"):
        page = int(data.split(":")[2])
        tier = context.user_data.get("prod_tier")
        return await show_products(update, context, page=page, tier=tier)
    # --- FIN MODIFICATION ---

    if data == "noop":
        return

async def hist_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        user_id = q.from_user.id
        chat_id = q.message.chat_id
        nav_message_id = q.message.message_id # L'ID du message de navigation ("Page 1/1...")

        # --- CORRECTION AJOUTÉE ---
        # Récupère la liste des messages (fiches + nav) et la vide
        old_msgs = context.user_data.pop("hist_msgs", []) 
        if old_msgs:
            for mid in old_msgs:
                if mid != nav_message_id: # Ne supprime pas le message de nav lui-même
                    try:
                        # Supprime les anciennes fiches de l'historique
                        await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                    except:
                        pass # Ignore les messages déjà supprimés
        # --- FIN DE LA CORRECTION ---
        
        # Appelle hist_menu, qui va maintenant remplacer le message de navigation (q.message)
        # par le menu "Choisissez une section :"
        await hist_menu(update, context) 
    
    except Exception as e:
        log(f"hist_view_callback error: {e}", str(update.effective_user.id), "error")

        # ==================================================================
# ================= MODULE ID/DOCS CENTER (INTÉGRÉ) ================

# 1. OUTILS GOOGLE & MAPPING & DATES
def validate_address_google(raw_address):
    """Interroge Google Maps pour nettoyer l'adresse."""
    # Sécurité : Si pas de clé configurée dans .env, on retourne vide sans planter
    if not GOOGLE_API_KEY or "AIzaSy" not in GOOGLE_API_KEY:
        return []

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    try:
        res = requests.get(url, params={"address": raw_address, "key": GOOGLE_API_KEY, "language": "fr"})
        data = res.json()
        if data['status'] == 'OK':
            return [r['formatted_address'] for r in data['results'][:3]]
    except: pass
    return []

def parse_date_smart(text):
    """Transforme '15 decembre 1990' ou '15/12/90' en '1990-12-15'."""
    text = text.lower().strip()
    mois = {
        'janvier': '01', 'fevrier': '02', 'février': '02', 'mars': '03', 'avril': '04',
        'mai': '05', 'juin': '06', 'juillet': '07', 'aout': '08', 'août': '08',
        'septembre': '09', 'octobre': '10', 'novembre': '11', 'decembre': '12', 'décembre': '12',
        'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04', 'may': '05', 'jun': '06',
        'jul': '07', 'aug': '08', 'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12'
    }
    for m_nom, m_chiffre in mois.items():
        if m_nom in text:
            text = text.replace(m_nom, m_chiffre)
            break
            
    text = text.replace('/', '-').replace(' ', '-').replace('.', '-')
    
    try:
        parts = text.split('-')
        if len(parts[0]) == 4: # YYYY-MM-DD
            dt = datetime.strptime(text, "%Y-%m-%d")
        else: # DD-MM-YYYY
            dt = datetime.strptime(text, "%d-%m-%Y")
        return dt.strftime("%Y-%m-%d")
    except:
        return None

def generate_barcode_via_api(data_dict, province_code):
    """Appelle FakeIdSolutions pour générer PDF417 et Code 128."""
    base_url = "https://barcodes.fakeidsolutions.com/api/v2"
    clean_code = province_code.replace('-D', '').replace('BAR-', '')
    jurisdiction = f"CAN-{clean_code}" 
    
    # Payload optimisé avec champs séparés
    payload = {
        "jurisdiction": jurisdiction,
        "revision": "", 
        "save": "true",
        "data[DAC]": data_dict.get('form_firstname', 'UNKNOWN'),
        "data[DCS]": data_dict.get('form_lastname', 'UNKNOWN'),       
        "data[DAQ]": data_dict.get('form_dl_number', 'N/A'),
        "data[DCF]": data_dict.get('form_ref_number', 'N/A'),
        "data[DBB]": data_dict.get('form_dob', '2000-01-01'),
        "data[DBD]": data_dict.get('form_issue', '2023-01-01'),
        "data[DBA]": data_dict.get('form_expiry', '2028-01-01'),
        "data[DAG]": data_dict.get('form_street', '123 Rue Exemple'), 
        "data[DAI]": data_dict.get('form_city', 'Montreal'),
        "data[DAK]": data_dict.get('form_zip', 'H1A1A1'),
        "data[DAJ]": "QC" if "QC" in jurisdiction else "ON",
        "data[DBC]": data_dict.get('form_sex', '1'), 
        "data[DAU]": data_dict.get('form_height', '175 cm').replace('cm', '').strip(),
        "data[DAY]": data_dict.get('form_eyes', 'BRO')
    }

    headers = {"Authorization": f"Bearer {FAKEID_API_KEY}", "Accept": "image/png"}

    try:
        r1 = requests.post(f"{base_url}/barcode", data=payload, headers=headers, timeout=15)
        if r1.status_code == 200:
            pdf417_img = r1.content
            barcode_id = r1.headers.get("X-Barcode-ID")
            linear_img = None
            if barcode_id:
                try:
                    r2 = requests.get(f"{base_url}/linear", params={"barcode_id": barcode_id}, headers=headers, timeout=15)
                    if r2.status_code == 200: linear_img = r2.content
                except: pass
            return pdf417_img, linear_img
        else:
            print(f"Erreur API ({r1.status_code}): {r1.text}")
            return None, None
    except Exception as e:
        print(f"Exception API: {e}")
        return None, None

# 2. HANDLERS DU MENU
async def id_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    
    # --- CORRECTION : SUPPRESSION FORCÉE DU MESSAGE PRÉCÉDENT ---
    # Cela permet d'effacer la photo du produit avant d'afficher le menu
    try:
        await q.message.delete()
    except:
        pass
    # ------------------------------------------------------------

    kb = [
        [InlineKeyboardButton("🪪 Physical ID", callback_data="id_cat:physical")],
        [InlineKeyboardButton("🔢 Numerical ID", callback_data="id_cat:numerical")],
        [InlineKeyboardButton("📄 Documents", callback_data="id_cat:document")],
        [InlineKeyboardButton("🛠️ Tools", callback_data="id_cat:tool")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
    ]
    
    # On utilise send_message car on a supprimé l'ancien message
    await context.bot.send_message(
        chat_id=q.message.chat_id, 
        text="🪪 **ID/Docs Center**\nChoisissez une catégorie :", 
        reply_markup=InlineKeyboardMarkup(kb), 
        parse_mode="Markdown"
    )
    return ID_CAT_VIEW

async def id_show_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    cat = q.data.split(":")[1]
    context.user_data['id_category'] = cat
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    rows = cur.execute("SELECT id, title, price, tier FROM products WHERE category=? AND is_active=1", (cat,)).fetchall()
    con.close()
    
    if not rows:
        await q.edit_message_text("❌ Aucun produit.", reply_markup=kb_back_to_menu())
        return ConversationHandler.END

    
    short_names = {
        "Quebec Driver License (Full)": "QC DRIVER LICENSE",
        "Ontario Driver License": "ON DRIVER LICENSE",
        "Quebec RESIDENCE": "CA RESIDENT PERMANENT",
        "SIN Card (Plastic)": "CA SIN",
        "Barcode Generator QC": "Gen QC",
        "Barcode Generator ON": "Gen ON"
    }

    kb = []
    for pid, title, price, code in rows:
        # On utilise le nom court si dispo, sinon le titre normal
        display_name = short_names.get(title, title)
        
        # On n'affiche PAS le prix ici, et on pointe vers 'id_view'
        kb.append([InlineKeyboardButton(f"🪪 {display_name}", callback_data=f"id_view:{pid}")])
    
    kb.append([InlineKeyboardButton("⬅️ Retour", callback_data="id_menu_entry")])
    
    titles = {
        "physical": "🪪 **Physical ID**",
        "numerical": "🔢 **Numerical ID**",
        "document": "📄 **Documents**",
        "tool": "🛠️ **Tools**"
    }
    
    await q.edit_message_text(
        f"{titles.get(cat, 'Catalogue')}\nSélectionnez un produit :", 
        reply_markup=InlineKeyboardMarkup(kb), 
        parse_mode="Markdown"
    )
    return ID_PROD_VIEW

async def id_view_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    # On supprime le menu précédent pour afficher la photo proprement
    try: await q.message.delete()
    except: pass
    
    pid = int(q.data.split(":")[1])
    
    con = sqlite3.connect(DB_NAME)
    row = con.execute("SELECT title, price, tier, content FROM products WHERE id=?", (pid,)).fetchone()
    con.close()
    
    title, price, tier, desc = row
    
    # Mapping des images (Assure-toi d'avoir ces fichiers dans le dossier assets/)
    # Si tu n'as pas les images, le bot enverra juste le texte.
    assets = {
        'QC': 'assets/qc_sample.jpg', # Pour QC DL
        'ON': 'assets/on_sample.jpg', # Pour ON DL
        'Physical': 'assets/physical_sample.jpg', # Défaut physique
        'T4': 'assets/t4_sample.jpg',
        'PAY': 'assets/paystub_sample.jpg',
        'BAR-QC': 'assets/barcode_sample.jpg'
    }
    
    # On essaie de trouver une image basée sur le titre ou le tier
    photo_path = None
    if "Quebec" in title or "QC" in title: photo_path = assets.get('QC')
    elif "Ontario" in title or "ON" in title: photo_path = assets.get('ON')
    elif "SIN" in title: photo_path = assets.get('Physical')
    elif "Generator" in title: photo_path = assets.get('BAR-QC')
    
    # Texte de description
    caption = (
        f"🪪 **{title}**\n\n"
        f"💰 Prix : **{price:.2f}$**\n\n"
        f"🌎 Province : {tier}\n\n"
        f"ℹ️ _Bankgrade, Scannable et UV. Inclut Recto/Verso._"
    )
    
    # Bouton Acheter qui redirige vers id_buy (la fonction qui demande la quantité)
    kb = [
        [InlineKeyboardButton("🛒 Commander maintenant", callback_data=f"id_buy:{pid}")],
        [InlineKeyboardButton("⬅️ Retour au menu", callback_data="id_menu_entry")]
    ]
    
    # Envoi Photo + Texte ou Texte seul
    if photo_path and os.path.exists(photo_path):
        await context.bot.send_photo(
            chat_id=q.message.chat_id,
            photo=open(photo_path, 'rb'),
            caption=caption,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    else:
        # Fallback si pas d'image
        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=caption,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        
    return ID_PROD_VIEW


# --- DÉBUT DU BLOC QUANTITÉ INTERACTIVE ---

# --- DÉBUT DU BLOC QUANTITÉ INTERACTIVE CORRIGÉ ---

async def id_start_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    # 1. On récupère l'ID du produit
    pid = int(q.data.split(":")[1])
    
    con = sqlite3.connect(DB_NAME)
    row = con.execute("SELECT title, price, tier FROM products WHERE id=?", (pid,)).fetchone()
    con.close()
    
    # 2. On initialise les données
    context.user_data['id_product'] = {'id': pid, 'name': row[0], 'price': row[1], 'code': row[2]}
    context.user_data['current_qty'] = 1 
    
    # 3. CRUCIAL : On supprime la photo (Fiche produit) pour faire place au menu texte
    # Si on ne fait pas ça, le edit_message_text suivant échouera silencieusement
    try:
        await q.message.delete()
    except Exception:
        pass

    # 4. On affiche le menu de quantité (comme un NOUVEAU message)
    await update_qty_display(update, context, new_message=True)
    
    return ID_ASK_QTY

async def update_qty_display(update: Update, context: ContextTypes.DEFAULT_TYPE, new_message=False):
    # Récupération des données
    qty = context.user_data.get('current_qty', 1)
    prod = context.user_data['id_product']
    total_price = prod['price'] * qty
    
    # Clavier Interactif [-] 1 [+]
    kb = [
        [
            InlineKeyboardButton("➖", callback_data="qty_sub"),
            InlineKeyboardButton(f"📦 {qty}", callback_data="noop"), 
            InlineKeyboardButton("➕", callback_data="qty_add")
        ],
        [
            InlineKeyboardButton("+5", callback_data="qty_add_5"),
            InlineKeyboardButton("+10", callback_data="qty_add_10")
        ],
        [InlineKeyboardButton(f"✅ Confirmer ({total_price:.2f}$)", callback_data="qty_confirm")],
        [InlineKeyboardButton("⬅️ Annuler", callback_data="id_menu_entry")]
    ]
    
    txt = (
        f"🛒 **{prod['name']}**\n"
        f"Prix unitaire : {prod['price']:.2f}$\n\n"
        f"🔢 **Quantité : {qty}**\n"
        f"💰 **Total : {total_price:.2f}$**\n\n"
        f"👇 Utilisez les boutons ou écrivez un chiffre."
    )
    
    # Si on demande explicitement un nouveau message (cas du démarrage après la photo)
    if new_message:
        chat_id = update.effective_chat.id
        await context.bot.send_message(chat_id=chat_id, text=txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return

    # Sinon, on essaie de modifier le message existant
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        except Exception:
            # Si l'edit échoue (ex: message trop vieux), on renvoie un neuf
            pass
    else:
        # Cas message texte manuel
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def id_handle_qty_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    
    current = context.user_data.get('current_qty', 1)
    
    if data == "qty_add":
        current += 1
        await q.answer("Ajouté (+1)")
    elif data == "qty_sub":
        current = max(1, current - 1)
        await q.answer("Retiré (-1)")
    elif data == "qty_add_5":
        current += 5
        await q.answer("Ajouté (+5)")
    elif data == "qty_add_10":
        current += 10
        await q.answer("Ajouté (+10)")
        
    context.user_data['current_qty'] = current
    await update_qty_display(update, context, new_message=False)
    return ID_ASK_QTY

async def id_save_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Cas A : Clic sur "✅ Confirmer"
    if update.callback_query and update.callback_query.data == "qty_confirm":
        context.user_data['id_qty'] = context.user_data['current_qty']
        return await id_start_form(update, context) 
        
    # Cas B : Texte écrit (ex: "50")
    if update.message and update.message.text:
        text = update.message.text.strip()
        if text.isdigit() and int(text) > 0:
            context.user_data['current_qty'] = int(text)
            await update_qty_display(update, context, new_message=True) # On renvoie le menu mis à jour
            return ID_ASK_QTY
        else:
            await update.message.reply_text("⚠️ Chiffre invalide.")
            return ID_ASK_QTY
            
    return ID_ASK_QTY

# --- FIN DU BLOC QUANTITÉ CORRIGÉ ---

async def id_handle_qty_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    
    # Récupère la quantité actuelle
    current = context.user_data.get('current_qty', 1)
    
    # Calcul
    if data == "qty_add":
        current += 1
        await q.answer("Ajouté (+1)") # Petite notif toast
    elif data == "qty_sub":
        current = max(1, current - 1) # On ne descend pas sous 1
        await q.answer("Retiré (-1)")
    elif data == "qty_add_5":
        current += 5
        await q.answer("Ajouté (+5)")
    elif data == "qty_add_10":
        current += 10
        await q.answer("Ajouté (+10)")
        
    # Sauvegarde et mise à jour visuelle
    context.user_data['current_qty'] = current
    await update_qty_display(update, context)
    return ID_ASK_QTY
        

async def id_save_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Cas A : Clic sur "✅ Confirmer"
    if update.callback_query and update.callback_query.data == "qty_confirm":
        # La quantité est déjà dans 'current_qty'
        context.user_data['id_qty'] = context.user_data['current_qty']
        return await id_start_form(update, context) # On passe à la suite
        
    # Cas B : Texte écrit (ex: "50")
    if update.message and update.message.text:
        text = update.message.text.strip()
        if text.isdigit() and int(text) > 0:
            context.user_data['current_qty'] = int(text)
            # On réaffiche le menu avec le nouveau chiffre pour validation
            await update_qty_display(update, context)
            return ID_ASK_QTY
        else:
            await update.message.reply_text("⚠️ Chiffre invalide.")
            return ID_ASK_QTY
            
    return ID_ASK_QTY


# 3. DÉMARRAGE FORMULAIRE (AVEC NETTOYAGE TOTAL)
async def id_start_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    user_id = str(q.from_user.id)
    
    # Initialisation de la liste de nettoyage
    context.user_data['cleanup_ids'] = []
    
    # On ajoute le message actuel (le menu quantité) à la liste
    try: context.user_data['cleanup_ids'].append(q.message.message_id)
    except: pass

    total = context.user_data['id_product']['price'] * context.user_data['id_qty']
    if get_user_balance(user_id) < total:
        await q.edit_message_text("❌ Solde insuffisant.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Recharger", callback_data="add_balance")]]))
        return ConversationHandler.END
    update_user_balance(user_id, -total)
    
    # Demande Prénom
    m = await q.edit_message_text("✍️ **Formulaire (1/10)**\n\nQuel est votre **PRÉNOM** (First Name) ?")
    # On track le message édité (c'est le même ID, mais on s'assure qu'il est dans la liste)
    if m.message_id not in context.user_data['cleanup_ids']:
        context.user_data['cleanup_ids'].append(m.message_id)
        
    return ID_ASK_NAME

async def id_save_firstname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['form_firstname'] = update.message.text.strip()
    context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id) # Track réponse user
    
    m = await update.message.reply_text("✍️ **Quel est votre NOM DE FAMILLE** (Last Name) ?")
    context.user_data['cleanup_ids'].append(m.message_id) # Track question bot
    return ID_ASK_LASTNAME

async def id_save_lastname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['form_lastname'] = update.message.text.strip()
    context.user_data['form_name'] = f"{context.user_data['form_firstname']} {context.user_data['form_lastname']}"
    context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)

    if context.user_data.get('id_category') == 'document':
        context.user_data['form_dob'] = "N/A"
        m = await update.message.reply_text("📍 **Adresse (1/3)**\nEntrez le **Numéro et la Rue** :")
        context.user_data['cleanup_ids'].append(m.message_id)
        return ID_ASK_STREET
    else:
        m = await update.message.reply_text("📅 **Date de Naissance**\n(Format: JJ/MM/AAAA ou 15 mars 1990)")
        context.user_data['cleanup_ids'].append(m.message_id)
        return ID_ASK_DOB

async def id_save_dob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)
    clean_date = parse_date_smart(update.message.text)
    if not clean_date:
        m = await update.message.reply_text("⚠️ Format invalide. Réessayez (ex: 15/05/1995).")
        context.user_data['cleanup_ids'].append(m.message_id)
        return ID_ASK_DOB
    context.user_data['form_dob'] = clean_date
    m = await update.message.reply_text("📍 **Adresse (1/3)**\nEntrez le **Numéro et la Rue** :")
    context.user_data['cleanup_ids'].append(m.message_id)
    return ID_ASK_STREET

async def id_save_street(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['addr_street'] = update.message.text
    context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)
    m = await update.message.reply_text("🏙️ **Adresse (2/3)**\nQuelle est la **Ville** ?")
    context.user_data['cleanup_ids'].append(m.message_id)
    return ID_ASK_CITY

async def id_save_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['addr_city'] = update.message.text
    context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)
    m = await update.message.reply_text("📮 **Adresse (3/3)**\nQuel est le **Code Postal** ?")
    context.user_data['cleanup_ids'].append(m.message_id)
    return ID_ASK_ZIP

async def id_save_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    zip_code = update.message.text.upper().strip()
    context.user_data['addr_zip'] = zip_code
    context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)
    
    raw = f"{context.user_data['addr_street']}, {context.user_data['addr_city']}, {zip_code}"
    m_wait = await update.message.reply_text("🔍 Validation Google Maps...")
    context.user_data['cleanup_ids'].append(m_wait.message_id) # On track le msg "validation"
    
    suggestions = validate_address_google(raw)
    context.user_data['addr_suggestions'] = suggestions or [raw]
    
    kb = [[InlineKeyboardButton(f"📍 {a[:40]}", callback_data=f"addr_pick:{i}")] for i, a in enumerate(context.user_data['addr_suggestions'])]
    kb.append([InlineKeyboardButton("✍️ Réécrire", callback_data="addr_retry")])
    
    # On édite le message d'attente, l'ID ne change pas, donc c'est déjà tracké
    await m_wait.edit_text("✅ Confirmez l'adresse :", reply_markup=InlineKeyboardMarkup(kb))
    return ID_CONFIRM_ADDR

async def id_confirm_addr_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    # Pas besoin de tracker q.message car c'est celui de l'étape d'avant (déjà tracké)
    
    if "retry" in q.data:
        await q.edit_message_text("📍 Entrez le **Numéro et la Rue** :"); return ID_ASK_STREET
    
    idx = int(q.data.split(":")[1])
    context.user_data['form_address'] = context.user_data['addr_suggestions'][idx]
    
    if context.user_data.get('id_category') == 'document':
        await q.edit_message_text("🏢 **Nom de l'Employeur** ?")
        return ID_ASK_DOC_EMPLOYER
    else:
        await q.edit_message_text("📅 **Date d'Émission (4d) ?**\n(Ex: 15/01/2023 ou Aujourd'hui)")
        return ID_ASK_ISSUE

async def id_save_issue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)
    txt = update.message.text
    if txt.lower() in ['today', "aujourd'hui", 'now']:
        clean_date = datetime.now().strftime("%Y-%m-%d")
    else:
        clean_date = parse_date_smart(txt)
    
    if not clean_date:
        m = await update.message.reply_text("⚠️ Date invalide.")
        context.user_data['cleanup_ids'].append(m.message_id)
        return ID_ASK_ISSUE
    context.user_data['form_issue'] = clean_date
    m = await update.message.reply_text("📅 **Quelle est l'Année d'Expiration ?**\n(Le jour/mois seront ceux de la naissance)\nEx: **2028**")
    context.user_data['cleanup_ids'].append(m.message_id)
    return ID_ASK_EXPIRY

async def id_save_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)
    year = update.message.text.strip()
    if not year.isdigit() or len(year) != 4:
        m = await update.message.reply_text("⚠️ Entrez juste l'année (ex: 2029).")
        context.user_data['cleanup_ids'].append(m.message_id)
        return ID_ASK_EXPIRY
    
    dob = context.user_data.get('form_dob', '2000-01-01')
    context.user_data['form_expiry'] = f"{year}-{dob[5:]}"
    m = await update.message.reply_text("🆔 **Numéro de Permis (DAQ) ?**\n(Ex: T1234-123456-12)")
    context.user_data['cleanup_ids'].append(m.message_id)
    return ID_ASK_DL_NUM

async def id_save_dl_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)
    context.user_data['form_dl_number'] = update.message.text.upper().strip()
    m = await update.message.reply_text("🔢 **Numéro de Référence (DCF/DD) ?**\n(Le petit numéro, ex: 12345678)")
    context.user_data['cleanup_ids'].append(m.message_id)
    return ID_ASK_REF_NUM

async def id_save_ref_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # On sauvegarde les IDs pour le nettoyage automatique
    context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)
    
    # Récupération et nettoyage du texte (tout en majuscules)
    user_input = update.message.text.strip().upper()

    final_ref = ""

    # --- LOGIQUE RANDOM "ANY" ---
    if user_input == "ANY":
        # 1. Choisir le préfixe au hasard
        prefix = random.choice(["R4MV", "PEVF"])
        
        # 2. Générer les 5 caractères restants (9 total - 4 préfixe = 5)
        # Mélange de chiffres et de lettres majuscules
        chars = string.ascii_uppercase + string.digits
        suffix = ''.join(random.choices(chars, k=5))
        
        final_ref = prefix + suffix
        
        # Petit message pour confirmer au client le numéro généré
        msg_gen = await update.message.reply_text(f"🎲 **Généré auto :** `{final_ref}`", parse_mode="Markdown")
        context.user_data['cleanup_ids'].append(msg_gen.message_id)
    else:
        # Si le client a écrit un vrai numéro, on le garde
        final_ref = user_input

    # Sauvegarde dans le contexte
    context.user_data['form_ref_number'] = final_ref

    # Passage à l'étape suivante (Sexe)
    kb = [
        [InlineKeyboardButton("Homme (Male)", callback_data="sex:1"), InlineKeyboardButton("Femme (Female)", callback_data="sex:2")],
        [InlineKeyboardButton("Non spécifié (X)", callback_data="sex:9")]
    ]
    m = await update.message.reply_text("👤 **Sexe / Genre ?**", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'].append(m.message_id)
    
    return ID_ASK_SEX

async def id_save_sex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sex = q.data.split(":")[1]
    context.user_data['form_sex'] = sex
    
    # On édite le message précédent pour confirmer le choix (le message est déjà tracké)
    try: await q.edit_message_text(f"👤 Sexe: {'Homme' if sex=='1' else 'Femme'}")
    except: pass
    
    m = await q.message.reply_text("📏 **Quelle est votre taille ?**\n(Ex: 175 cm)")
    context.user_data.setdefault('cleanup_ids', []).append(m.message_id)
    return ID_ASK_HEIGHT

async def id_save_height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)
    context.user_data['form_height'] = update.message.text
    kb = [
        [InlineKeyboardButton("Brun (Brown)", callback_data="eye:BRO"), InlineKeyboardButton("Bleu (Blue)", callback_data="eye:BLU")],
        [InlineKeyboardButton("Vert (Green)", callback_data="eye:GRN"), InlineKeyboardButton("Noisette (Hazel)", callback_data="eye:HZL")],
        [InlineKeyboardButton("Gris (Grey)", callback_data="eye:GRY"), InlineKeyboardButton("Noir (Black)", callback_data="eye:BLK")]
    ]
    m = await update.message.reply_text("👁️ **Couleur des yeux ?**", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'].append(m.message_id)
    return ID_ASK_EYES

async def id_save_eyes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        q = update.callback_query; await q.answer()
        context.user_data['form_eyes'] = q.data.split(":")[1]
        try: await q.edit_message_text(f"👁️ Yeux: {context.user_data['form_eyes']}")
        except: pass
    else:
        context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)
        context.user_data['form_eyes'] = update.message.text

    if context.user_data.get('id_category') == 'tool':
        return await id_show_summary(update, context)

    # Si c'est un message texte manuel, le 'm' doit être tracké
    msg_target = update.message if update.message else update.callback_query.message
    m = await msg_target.reply_text("📸 **Envoyez votre photo (Selfie)**")
    context.user_data.setdefault('cleanup_ids', []).append(m.message_id)
    return ID_ASK_PHOTO

async def id_save_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)
    context.user_data['form_photo_id'] = update.message.photo[-1].file_id
    return await id_finalize_order(update, context)

async def id_show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    sex_map = {'1': 'Homme', '2': 'Femme', '9': 'X'}
    
    # On nettoie la variable 'editing_key' pour éviter les bugs
    context.user_data.pop('editing_key', None)

    txt = (
        f"📝 **RÉSUMÉ DES DONNÉES**\n\n"
        f"👤 **Identité**\n"
        f"• Prénom (DAC) : `{d.get('form_firstname')}`\n"
        f"• Nom (DCS) : `{d.get('form_lastname')}`\n"
        f"• Sexe : `{sex_map.get(d.get('form_sex'), d.get('form_sex'))}`\n"
        f"• Taille : `{d.get('form_height')}`\n"
        f"• Yeux : `{d.get('form_eyes')}`\n\n"
        f"📅 **Dates**\n"
        f"• Naissance : `{d.get('form_dob')}`\n"
        f"• Émission : `{d.get('form_issue')}`\n"
        f"• Expiration : `{d.get('form_expiry')}`\n\n"
        f"📍 **Adresse**\n"
        f"• Rue : `{d.get('addr_street')}`\n"
        f"• Ville : `{d.get('addr_city')}`\n"
        f"• Zip : `{d.get('addr_zip')}`\n\n"
        f"🆔 **Numéros**\n"
        f"• Permis (DAQ) : `{d.get('form_dl_number')}`\n"
        f"• Référence (DCF) : `{d.get('form_ref_number')}`"
    )
    
    kb = [
        [InlineKeyboardButton("✅ CONFIRMER & GÉNÉRER", callback_data="confirm_gen")],
        # 👇 LE NOUVEAU BOUTON EST ICI 👇
        [InlineKeyboardButton("✏️ Modifier / Corriger", callback_data="edit_open_menu")],
        # -------------------------------
        [InlineKeyboardButton("❌ Annuler", callback_data="id_menu_entry")]
    ]
    
    target = update.message or update.callback_query.message
    # Si on vient d'une édition, on édite le message, sinon on envoie un nouveau
    if update.callback_query:
        try:
            m = await target.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        except:
             m = await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        m = await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        
    context.user_data.setdefault('cleanup_ids', []).append(m.message_id) 
    return ID_CONFIRM_SUMMARY

async def id_open_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche la grille de 13 boutons pour choisir quoi modifier."""
    q = update.callback_query
    await q.answer()

    # Liste des champs modifiables (Label, Clé interne)
    fields = [
        ("Prénom", "form_firstname"), ("Nom", "form_lastname"),
        ("Sexe", "form_sex"), ("Taille", "form_height"),
        ("Yeux", "form_eyes"), ("Date Naiss.", "form_dob"),
        ("Émission", "form_issue"), ("Expiration", "form_expiry"),
        ("Rue", "addr_street"), ("Ville", "addr_city"),
        ("Code Postal", "addr_zip"), ("Permis (DAQ)", "form_dl_number"),
        ("Réf (DCF)", "form_ref_number")
    ]

    # Création dynamique du clavier (2 par ligne)
    kb = []
    row = []
    for label, key in fields:
        row.append(InlineKeyboardButton(f"✏️ {label}", callback_data=f"do_edit:{key}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row: kb.append(row)
    
    kb.append([InlineKeyboardButton("🔙 Retour au Résumé", callback_data="back_to_summary")])

    await q.message.edit_text(
        "🔧 **MODE ÉDITION**\n\nAppuyez sur l'élément que vous voulez modifier :",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ID_EDIT_MENU

async def id_handle_edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """L'utilisateur a cliqué sur un champ. On lui demande la nouvelle valeur."""
    q = update.callback_query
    await q.answer()
    
    # Si clic sur "Retour"
    if q.data == "back_to_summary":
        return await id_show_summary(update, context)

    # Récupération de la clé (ex: form_firstname)
    key_to_edit = q.data.split(":")[1]
    context.user_data['editing_key'] = key_to_edit
    
    # Mapping pour afficher un beau nom
    nice_names = {
        "form_firstname": "le Prénom", "form_lastname": "le Nom",
        "form_sex": "le Sexe (1=H, 2=F)", "form_height": "la Taille",
        "form_eyes": "les Yeux (BRO, BLU, etc.)", "form_dob": "la Date de Naissance",
        "addr_zip": "le Code Postal"
    }
    field_name = nice_names.get(key_to_edit, "cette valeur")

    kb = [[InlineKeyboardButton("🔙 Annuler", callback_data="cancel_edit_input")]]

    await q.message.edit_text(
        f"✍️ **Modification :**\n\n"
        f"Veuillez entrer la nouvelle valeur pour **{field_name}** :",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ID_EDIT_INPUT

async def id_receive_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reçoit le texte corrigé, le sauvegarde et retourne au résumé."""
    # Nettoyage visuel (supprime la réponse de l'utilisateur pour garder le chat propre)
    try:
        context.user_data.setdefault('cleanup_ids', []).append(update.message.message_id)
        await update.message.delete()
    except: pass

    key = context.user_data.get('editing_key')
    if not key:
        return await id_show_summary(update, context)

    new_val = update.message.text.strip()
    
    # Petit traitement spécial pour tout mettre en majuscules (sauf si c'est déjà géré plus loin)
    if key in ['form_dl_number', 'form_ref_number', 'addr_zip']:
        new_val = new_val.upper()
        
    # Sauvegarde
    context.user_data[key] = new_val
    
    # Petit toast de confirmation (message temporaire)
    temp = await update.message.reply_text(f"✅ Modifié : {new_val}")
    time.sleep(1)
    try: await temp.delete()
    except: pass

    # Retour au résumé (il se mettra à jour avec la nouvelle valeur)
    # On doit simuler un update.callback_query pour id_show_summary
    # Astuce : on utilise le dernier message du bot qui est tracké
    
    # On rappelle le résumé. Comme on n'a plus de callback_query valide ici (on est dans MessageHandler),
    # id_show_summary va envoyer un nouveau message ou éditer le dernier connu.
    return await id_show_summary(update, context)

async def id_finalize_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio
    q = update.callback_query
    if q: await q.answer()
    
    user = update.effective_user
    d = context.user_data
    cat = d.get('id_category')
    prod = d.get('id_product')
    target = update.message or update.callback_query.message

    # 0. TRANSFORMATION EN MAJUSCULES
    fn = str(d.get('form_firstname', '')).upper()
    ln = str(d.get('form_lastname', '')).upper()
    dob = str(d.get('form_dob', '')).upper()
    issue = str(d.get('form_issue', '')).upper()
    expiry = str(d.get('form_expiry', '')).upper()
    street = str(d.get('addr_street', '')).upper()
    city = str(d.get('addr_city', '')).upper()
    zip_code = str(d.get('addr_zip', '')).upper()
    dl_num = str(d.get('form_dl_number', '')).upper()
    ref_num = str(d.get('form_ref_number', '')).upper()
    sex = str(d.get('form_sex', '')).upper()
    height = str(d.get('form_height', '')).upper()
    eyes = str(d.get('form_eyes', '')).upper()

    # 1. Préparation API
    api_data = {
        "form_firstname": fn, "form_lastname": ln,
        "form_dob": dob, "form_issue": issue,
        "form_expiry": expiry, "form_street": street,
        "form_city": city, "form_zip": zip_code,
        "form_dl_number": dl_num, "form_ref_number": ref_num,
        "form_sex": sex, "form_height": height, "form_eyes": eyes
    }

    # Cas 1 : TOOL
    if cat == 'tool':
        msg = await target.reply_text("⚙️ Génération des codes-barres...")
        pdf417, linear = generate_barcode_via_api(api_data, prod['code'])
        try:
            await msg.delete()
        except:
            pass

        if pdf417:
            await target.reply_document(document=pdf417, filename=f"pdf417_{ln}.png", caption="✅ **PDF417 (Scan)**")
            if linear:
                await target.reply_document(document=linear, filename=f"code128_{ln}.png", caption="✅ **Code 128**")
            
            success_msg = await target.reply_text("✅ **Génération terminée.** Merci !")
            await asyncio.sleep(3)
            
            # --- GRAND NETTOYAGE ---
            try:
                await success_msg.delete()
            except:
                pass
            
            # Suppression de TOUT l'historique de conversation tracké
            for mid in context.user_data.get('cleanup_ids', []):
                try:
                    await context.bot.delete_message(chat_id=target.chat_id, message_id=mid)
                except:
                    pass
            context.user_data['cleanup_ids'] = []
            # -----------------------

        else:
            await target.reply_text("⚠️ Erreur technique API.")
    
    # Cas 2 : PHYSIQUE / DOCS
    else:
        wait_msg = await target.reply_text("⏳ Traitement de la commande...")
        
        pdf417, linear = generate_barcode_via_api(api_data, prod['code'])
        
        admin_msg = (
            f"🚨 **NOUVELLE COMMANDE : {cat.upper()}** 🚨\n\n"
            f"👤 **Client** : @{user.username} (ID: {user.id})\n"
            f"🛒 **Produit** : {prod['name']} (x{d.get('id_qty')})\n\n"
            f"📋 **DONNÉES DU FORMULAIRE :**\n"
            f"--------------------------------\n"
            f"📛 Nom : {fn} {ln}\n"
            f"🎂 DDN : {dob}\n"
            f"📍 Adresse : {street}\n"
            f"🏙️ Ville : {city}\n"
            f"📮 CP : {zip_code}\n"
            f"--------------------------------\n"
            f"🆔 Permis : {dl_num}\n"
            f"🔢 Réf : {ref_num}\n"
            f"📅 Émission : {issue}\n"
            f"📅 Expiration : {expiry}\n"
            f"--------------------------------\n"
            f"📏 Taille : {height}\n"
            f"👁️ Yeux : {eyes}\n"
            f"👤 Sexe : {sex}"
        )
        
        try:
            target_id = "-1003589564052" 
            if d.get('form_photo_id'):
                await context.bot.send_photo(chat_id=target_id, photo=d['form_photo_id'], caption=admin_msg)
            else:
                await context.bot.send_message(chat_id=target_id, text=admin_msg)
            
            if pdf417:
                await context.bot.send_document(chat_id=target_id, document=pdf417, filename=f"pdf417_{ln}.png", caption="🖨️ **PDF417 (PNG HQ)**")
            if linear:
                await context.bot.send_document(chat_id=target_id, document=linear, filename=f"code128_{ln}.png", caption="🖨️ **Code 128 (PNG HQ)**")

            try:
                await wait_msg.delete()
            except:
                pass

            # --- NETTOYAGE TOTAL ---
            success_msg = await target.reply_text("✅ Commande envoyée avec succès !")
            await asyncio.sleep(2.5) # Temps de lecture
            
            try:
                await success_msg.delete()
            except:
                pass

            # Boucle qui supprime TOUS les messages trackés (Questions + Réponses)
            for mid in context.user_data.get('cleanup_ids', []):
                try:
                    await context.bot.delete_message(chat_id=target.chat_id, message_id=mid)
                except:
                    pass
            
            # On vide la liste pour la prochaine fois
            context.user_data['cleanup_ids'] = []
            # -----------------------------------
            
        except Exception as e:
            print(f"Erreur envoi canal: {e}")
            await target.reply_text(f"⚠️ Erreur lors de l'envoi : {e}")

    await show_main_menu(user.id)
    return ConversationHandler.END

# Handlers Document (Placeholder pour éviter erreurs d'import)
# Handlers Document (Placeholder pour éviter erreurs d'import)
async def id_save_employer(u,c): 
    c.user_data['form_employer'] = u.message.text
    await u.message.reply_text("Poste ?")
    return ID_ASK_DOC_JOB

async def id_save_job(u,c): 
    c.user_data['form_job'] = u.message.text
    await u.message.reply_text("Adresse Emp ?")
    return ID_ASK_DOC_ADDR

async def id_save_emp_addr(u,c): 
    c.user_data['form_emp_addr'] = u.message.text
    await u.message.reply_text("Revenu ?")
    return ID_ASK_DOC_SIN

async def id_income_mode(u,c): 
    pass

async def id_save_hours(u,c): 
    pass

async def id_save_rate(u,c): 
    pass

async def id_save_sin_or_range(u,c): 
    c.user_data['form_sin'] = (u.message.text if u.message else "Range")
    return await id_finalize_order(u,c)

# Handlers Dummy (au cas où)
async def admin_prod_list_dummy(u,c): pass
async def admin_prod_add_start_dummy(u,c): pass


# 3. AUTRES CONVERSATIONS (ADMIN, PAIEMENT, FILTRES)
admin_search_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(admin_search_user_start, pattern="^admin_search_user_start$")],
    states={ ADMIN_WAIT_SEARCH_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_search_user_receive)] },
    fallbacks=[CommandHandler("start", goto_menu), CallbackQueryHandler(admin_users, pattern="^admin_users")]
)

payment_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(add_balance_start, pattern="^add_balance$")],
    states={ WAIT_AMOUNT_CRYPTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount_crypto)] },
    fallbacks=[CallbackQueryHandler(goto_menu, pattern="^menu_accueil$")]
)

admin_csv_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(admin_prod_csv_start, pattern="^admin_prod_csv$")],
    states={ ADMIN_WAIT_CSV: [MessageHandler(filters.Document.ALL & ~filters.COMMAND, admin_prod_csv_receive)] },
    fallbacks=[CallbackQueryHandler(admin_menu, pattern="^admin_menu$")],
)

admin_ivr_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(admin_ivr_change, pattern="^admin_ivr_change:.*$")],
    states={ ADMIN_IVR_AWAIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ivr_receive)] },
    fallbacks=[CallbackQueryHandler(admin_menu, pattern="^admin_menu$")],
    map_to_parent={ ConversationHandler.END: -1 }
)

catalog_filter_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(filter_start, pattern="^filter_open$")],
    states={
        CATALOG_FILTER_MAIN: [
            CallbackQueryHandler(filter_select_type, pattern="^filter:(name|city|base|price|year)$"),
            CallbackQueryHandler(filter_search, pattern="^filter_search$"),
            CallbackQueryHandler(filter_reset, pattern="^filter_reset$"),
            CallbackQueryHandler(filter_page_nav, pattern="^filter:page:\d+$"),
        ],
        CATALOG_FILTER_AWAIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, filter_receive_value)],
    },
    fallbacks=[CallbackQueryHandler(filter_cancel, pattern="^filter_cancel$")],
    persistent=False
)

ccs_catalog_filter_conv = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(filter_start_ccs, pattern="^ccs_filter_open$"),
        CallbackQueryHandler(ccs_catalog_start, pattern="^ccs_catalog_start$"),
    ],
    states={
        CCS_FILTER_MAIN: [
            CallbackQueryHandler(filter_select_type_ccs, pattern="^ccs_filter:(bins|name|city|base|price|year)$"),
            CallbackQueryHandler(filter_search_ccs, pattern="^ccs_filter_search$"),
            CallbackQueryHandler(filter_reset_ccs, pattern="^ccs_filter_reset$"),
            CallbackQueryHandler(filter_page_nav_ccs, pattern="^ccs_filter:page:\d+$"),
        ],
        CCS_FILTER_AWAIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, filter_receive_value_ccs)],
    },
    fallbacks=[CallbackQueryHandler(filter_cancel_ccs, pattern="^ccs_filter_cancel$")],
    persistent=False
)

history_filter_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(history_filter_start, pattern="^history_filter_start$")],
    states={
        HISTORY_FILTER_CHOICE: [CallbackQueryHandler(history_filter_choice, pattern="^history_filter_type:(name|sin|dl)$")],
        HISTORY_FILTER_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_filter_input)],
    },
    fallbacks=[CallbackQueryHandler(history_filter_cancel, pattern="^history_filter_cancel$")]
)

# 4. CONVERSATION ADMIN TICKETS (RÉPONSE)
admin_ticket_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(tickets.admin_ask_reply, pattern="^adm_ticket_rep_")],
    states={
        tickets.ADMIN_TICKET_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, tickets.admin_send_reply)]
    },
    fallbacks=[CallbackQueryHandler(admin_menu, pattern="^admin_menu$"), CommandHandler("start", start)]
)

conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("start", start),
        CommandHandler("verifier", start_verifier),
        CallbackQueryHandler(start_verifier_main, pattern="^start_verifier_main$"),
        CallbackQueryHandler(auth_create_start, pattern='^auth_create$'),
        CallbackQueryHandler(auth_import_start, pattern='^auth_import_start$'),
        CallbackQueryHandler(tickets.start_support, pattern='^support$'),
        CallbackQueryHandler(show_tools_menu, pattern='^section_tools$'),
    ],
    states={
        # --- AUTHENTIFICATION ---
        ID_AUTH_WAIT_PIN_CREATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, auth_create_pin_save)],
        ID_AUTH_WAIT_PIN_LOGIN: [CallbackQueryHandler(auth_pin_handler, pattern="^pin_")],
        ID_AUTH_WAIT_SEED: [MessageHandler(filters.TEXT & ~filters.COMMAND, auth_import_verify)],

        # --- SUPPORT ---
        tickets.WAIT_CATEGORY: [
            CallbackQueryHandler(tickets.save_category, pattern="^ticket_cat:")
        ],
        tickets.WAIT_TICKET_MSG: [
            CallbackQueryHandler(tickets.start_support, pattern="^support$"),
            # Pas de start_ticket_reply ici pour le client
            CallbackQueryHandler(tickets.close_ticket, pattern="^ticket_close$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, tickets.handle_ticket_msg)
        ],

        # --- TOOLS ---
        SELECT_TOOL: [
            CallbackQueryHandler(tool_ask_hlr, pattern='^tool_hlr$'),
            CallbackQueryHandler(show_sms_menu, pattern='^tool_5sim$'),
            CallbackQueryHandler(handle_buy_sms, pattern='^buy_sms:'),
            CallbackQueryHandler(sms_control_callback, pattern='^sms_ban_'),
            CallbackQueryHandler(tool_placeholder, pattern='^tool_cc_checker$'),
            CallbackQueryHandler(show_tools_menu, pattern='^section_tools$'),
            CallbackQueryHandler(goto_menu, pattern='^menu_accueil$')
        ],
        WAIT_HLR_NUMBER: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, tool_process_hlr),
            CallbackQueryHandler(show_tools_menu, pattern='^section_tools$')
        ],

        # --- VERIF PERMIS (Legacy) ---
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
    fallbacks=[
        CallbackQueryHandler(goto_menu, pattern="^menu_accueil$"),
        CommandHandler("start", start)
    ]
)
# ID/DOCS CONVERSATION (MANQUANT)
# ==============================================================================
id_docs_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(id_menu_entry, pattern="^id_menu_entry$")],
    states={
        ID_CAT_VIEW: [CallbackQueryHandler(id_show_category, pattern="^id_cat:")],
        ID_PROD_VIEW: [
            CallbackQueryHandler(id_view_product, pattern="^id_view:"),
            CallbackQueryHandler(id_start_buy, pattern="^id_buy:")
        ],
        # Quantité
        ID_ASK_QTY: [
            CallbackQueryHandler(id_handle_qty_buttons, pattern="^qty_"),
            CallbackQueryHandler(id_save_qty, pattern="^qty_confirm$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, id_save_qty)
        ],
        ID_CONFIRM_BUY: [CallbackQueryHandler(id_start_form, pattern="^id_confirm_pay$")],
        
        # Formulaire
        ID_ASK_NAME: [MessageHandler(filters.TEXT, id_save_firstname)],
        ID_ASK_LASTNAME: [MessageHandler(filters.TEXT, id_save_lastname)],
        ID_ASK_DOB: [MessageHandler(filters.TEXT, id_save_dob)],
        ID_ASK_STREET: [MessageHandler(filters.TEXT, id_save_street)],
        ID_ASK_CITY: [MessageHandler(filters.TEXT, id_save_city)],
        ID_ASK_ZIP: [MessageHandler(filters.TEXT, id_save_zip)],
        ID_CONFIRM_ADDR: [CallbackQueryHandler(id_confirm_addr_handler, pattern="^addr_")],
        ID_ASK_ISSUE: [MessageHandler(filters.TEXT, id_save_issue)],
        ID_ASK_EXPIRY: [MessageHandler(filters.TEXT, id_save_expiry)],
        ID_ASK_DL_NUM: [MessageHandler(filters.TEXT, id_save_dl_num)],
        ID_ASK_REF_NUM: [MessageHandler(filters.TEXT, id_save_ref_num)], 
        ID_ASK_SEX: [CallbackQueryHandler(id_save_sex, pattern="^sex:")],
        ID_ASK_HEIGHT: [MessageHandler(filters.TEXT, id_save_height)],
        ID_ASK_EYES: [CallbackQueryHandler(id_save_eyes, pattern="^eye:"), MessageHandler(filters.TEXT, id_save_eyes)],
        
        # Résumé & Edition
        ID_CONFIRM_SUMMARY: [
            CallbackQueryHandler(id_finalize_order, pattern="^confirm_gen$"), 
            CallbackQueryHandler(id_open_edit_menu, pattern="^edit_open_menu$")
        ],
        ID_EDIT_MENU: [
            CallbackQueryHandler(id_handle_edit_choice, pattern="^do_edit:"), 
            CallbackQueryHandler(id_show_summary, pattern="^back_to_summary$")
        ],
        ID_EDIT_INPUT: [
            MessageHandler(filters.TEXT, id_receive_new_value), 
            CallbackQueryHandler(id_open_edit_menu, pattern="^cancel_edit_input$")
        ],
        
        ID_ASK_PHOTO: [MessageHandler(filters.PHOTO, id_save_photo)],
        
        # Docs Extras
        ID_ASK_DOC_EMPLOYER: [MessageHandler(filters.TEXT, id_save_employer)],
        ID_ASK_DOC_JOB: [MessageHandler(filters.TEXT, id_save_job)],
        ID_ASK_DOC_ADDR: [MessageHandler(filters.TEXT, id_save_emp_addr)],
        ID_CHOOSE_INCOME_MODE: [CallbackQueryHandler(id_income_mode, pattern="^inc_")],
        ID_ASK_DOC_HOURS: [MessageHandler(filters.TEXT, id_save_hours)],
        ID_ASK_DOC_RATE: [MessageHandler(filters.TEXT, id_save_rate)],
        ID_ASK_DOC_SIN: [CallbackQueryHandler(id_save_sin_or_range, pattern="^sal:"), MessageHandler(filters.TEXT, id_save_sin_or_range)],
    },
    fallbacks=[
        CallbackQueryHandler(id_menu_entry, pattern="^id_menu_entry$"),
        CallbackQueryHandler(goto_menu, pattern="^menu_accueil$"),
        CommandHandler("start", goto_menu)
    ],
    name="id_docs_conversation"
)

# ================= BOUCLE PRINCIPALE (MAIN) =================
if __name__ == "__main__":
    # --- INIT DB ---
    db_conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    shop_helpers.ensure_shop_tables(db_conn)
    init_db()
    
    # Patch DB Tickets (Important)
    try: tickets.patch_db_tickets()
    except: pass
        
    ensure_verifications_table()
    ensure_payment_table()

    # --- FLASK (IVR) ---
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False),
        daemon=True
    )
    flask_thread.start()

    # --- TELEGRAM ---
    app_telegram = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Ajout des Handlers (Ordre CRITIQUE)
    app_telegram.add_handler(admin_search_conv, group=8)
    app_telegram.add_handler(admin_csv_conv, group=6)
    app_telegram.add_handler(admin_ivr_conv, group=7)
    app_telegram.add_handler(history_filter_conv, group=5)
    app_telegram.add_handler(catalog_filter_conv)
    app_telegram.add_handler(ccs_catalog_filter_conv)
    app_telegram.add_handler(payment_conv)
    app_telegram.add_handler(id_docs_conv)
    app_telegram.add_handler(conv_handler) # Main Router
    app_telegram.add_handler(admin_ticket_conv, group=9) # Admin Tickets

    # Handlers Admin Loose
    app_telegram.add_handler(CallbackQueryHandler(admin_prod_del_confirm, pattern="^admin_prod_del_\d+$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_prod_del, pattern="^admin_prod_del$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_prod_add_start, pattern="^admin_prod_add$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_prod_list, pattern="^admin_prod_list$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_menu, pattern="^admin_menu$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_users, pattern=r"^admin_users(:page:\d+)?$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_adjust_user, pattern="^admin_adjust_.*$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_adjust_value, pattern="^admin_adjval_.*$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_customamount_start, pattern="^admin_customamount_.*$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_setstatut, pattern="^admin_setstatut$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_userstatut, pattern="^admin_userstatut_.*$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_setstatut_final, pattern="^admin_statut_.*$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_hard_reboot, pattern="^admin_hard_reboot$"))
    app_telegram.add_handler(CallbackQueryHandler(admin_ivr_settings, pattern="^admin_ivr_settings$"))
    app_telegram.add_handler(CallbackQueryHandler(history_filter_reset, pattern=r"^history_filter_reset$"), group=5)
    
    app_telegram.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_prod_add_receive), group=21)
    app_telegram.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_customamount_receive), group=22)

    # Handlers Tickets (Simples)
    app_telegram.add_handler(CallbackQueryHandler(tickets.admin_list_tickets, pattern="^admin_tickets_list$"))
    app_telegram.add_handler(CallbackQueryHandler(tickets.admin_view_ticket, pattern="^adm_ticket_view_"))
    app_telegram.add_handler(CallbackQueryHandler(tickets.admin_close_no_reply, pattern="^adm_ticket_close_"))

    # Réponse Magique Admin
    app_telegram.add_handler(MessageHandler(filters.Chat(chat_id=int(tickets.CHANNEL_LOGS)) & filters.REPLY, tickets.admin_reply_native))

    # Callbacks Généraux
    app_telegram.add_handler(CallbackQueryHandler(check_payment_callback, pattern=r"^check_pay_\d+$"))
    app_telegram.add_handler(CallbackQueryHandler(callback_show_my_id, pattern="^show_my_id$"))
    app_telegram.add_handler(CallbackQueryHandler(choose_lang, pattern="^choose_lang$"))
    app_telegram.add_handler(CallbackQueryHandler(set_lang_fr, pattern="^set_lang_fr$"))
    app_telegram.add_handler(CallbackQueryHandler(set_lang_en, pattern="^set_lang_en$"))
    app_telegram.add_handler(CallbackQueryHandler(callback_check_balance, pattern="^check_balance$"))
    app_telegram.add_handler(CallbackQueryHandler(callback_support, pattern="^support$"))
    app_telegram.add_handler(CallbackQueryHandler(callback_faq, pattern="^faq$"))
    app_telegram.add_handler(CallbackQueryHandler(acces_channel_prive, pattern="^join_private_channel$"))
    
    app_telegram.add_handler(CallbackQueryHandler(admin_category_menu, pattern="^admin_cat_menu:.*$"))
    app_telegram.add_handler(CallbackQueryHandler(on_back_cats, pattern=r"^back:cats$"))
    app_telegram.add_handler(CallbackQueryHandler(on_category, pattern=r"^cat:.+$"))
    app_telegram.add_handler(CallbackQueryHandler(hist_view_callback, pattern=r"^hist:view$"))
    app_telegram.add_handler(CallbackQueryHandler(hist_pros, pattern=r"^hist:pros(:page:\d+)?$"))
    app_telegram.add_handler(CallbackQueryHandler(hist_permis, pattern=r"^hist:permis(:page:\d+)?$"))
    app_telegram.add_handler(CallbackQueryHandler(close_history, pattern=r"^close_history$"))
    app_telegram.add_handler(CallbackQueryHandler(delete_history_handler, pattern=r"^delete_history_\d+$"))
    app_telegram.add_handler(CallbackQueryHandler(auth_logout, pattern="^auth_logout$"))
    app_telegram.add_handler(CallbackQueryHandler(auth_lock_only, pattern="^auth_lock_only$"))
    app_telegram.add_handler(CallbackQueryHandler(auth_switch_account, pattern="^auth_switch_account$"))

    # Shop Helpers
    app_telegram.add_handler(CallbackQueryHandler(shop_helpers.handle_preview_callback, pattern=r"^prod:preview:\d+$"), group=-1)
    app_telegram.add_handler(CallbackQueryHandler(shop_helpers.handle_buy_callback, pattern=r"^buy:\d+$"), group=-1)
    app_telegram.add_handler(CallbackQueryHandler(shop_helpers.handle_view_callback, pattern=r"^prod:view:\d+$"), group=-1)
    app_telegram.add_handler(CallbackQueryHandler(shop_helpers.cart_add_callback, pattern=r"^cart:add:\d+$"), group=-1)
    app_telegram.add_handler(CallbackQueryHandler(shop_helpers.cart_view_callback, pattern=r"^cart:view$"), group=-1)
    app_telegram.add_handler(CallbackQueryHandler(shop_helpers.cart_clear_callback, pattern=r"^cart:clear$"), group=-1)
    app_telegram.add_handler(CallbackQueryHandler(shop_helpers.cart_checkout_callback, pattern=r"^cart:checkout$"), group=-1)

    app_telegram.add_handler(CallbackQueryHandler(menu_handler))

    # Attachement Globals
    app_telegram.bot_data['db_conn'] = db_conn
    app_telegram.bot_data['db'] = db_conn
    app_telegram.bot_data['get_user_balance'] = get_user_balance
    app_telegram.bot_data['update_user_balance'] = update_user_balance
    app_telegram.bot_data['create_transaction'] = create_transaction

    # 4. Run
    print("✅ BOT DÉMARRÉ.")
    app_telegram.run_polling(close_loop=False)

    while True:
        time.sleep(3600)

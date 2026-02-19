import os
import sys
import io
import re
import csv
import json
import time
import atexit
import random
import string
import asyncio
import logging
import sqlite3
import threading
import subprocess
from datetime import datetime
import tickets
# --- TIERCE PARTIES ---
import pytz
import base58
import requests
import sentry_sdk
from dotenv import load_dotenv
from mnemonic import Mnemonic
from PIL import Image, ImageOps, ImageColor
from bip_utils import Bip32Secp256k1, P2WPKHAddr
from hdwallet import HDWallet
from hdwallet.cryptocurrencies import Bitcoin as BTC
from sentry_sdk.integrations.flask import FlaskIntegration


# --- WEB & TÉLÉPHONIE ---
from flask import Flask, request, Response
from signalwire.rest import Client as SignalWireClient
from twilio.twiml.voice_response import VoiceResponse

# --- TELEGRAM (CORRIGÉ AVEC ApplicationBuilder) ---
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, ApplicationBuilder,  # <--- L'AJOUT CRITIQUE EST ICI
    CommandHandler, MessageHandler, filters,
    ConversationHandler, ContextTypes, CallbackQueryHandler,
    TypeHandler, ApplicationHandlerStop
)
from typing import Dict, Any, List, Tuple
# --- MODULES LOCAUX ---



# --- CONFIG LOGGING ---
logger = logging.getLogger("SYSTEM")

load_dotenv()

app = Flask(__name__)


os.environ.pop('http_proxy', None)
os.environ.pop('https_proxy', None)
os.environ.pop('all_proxy', None)
os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
os.environ.pop('ALL_PROXY', None)

# Forcer l'exception SignalWire
os.environ["no_proxy"] = "signalwire.com,john-m-shop.signalwire.com,37.228.129.82"
os.environ["NO_PROXY"] = "signalwire.com,john-m-shop.signalwire.com,37.228.129.82"

# --- CONFIGURATION SENTRY ---
sentry_dsn = os.environ.get("SENTRY_DSN")
if sentry_dsn:
    sentry_sdk.init(
        dsn=sentry_dsn,
        integrations=[FlaskIntegration()],
        traces_sample_rate=1.0,
        _experiments={"profiles_sample_rate": 1.0},
    )
    print("✅ SENTRY : Monitoring Activé.")
else:
    print("⚠️ SENTRY : Pas de clé DSN trouvée.")

# ----------------------------


# --- Ligne de test temporaire ---
#division_by_zero = 1 / 0


# ================= SECURITY HEARTBEAT =================
# Ton lien Healthchecks personnel
HEARTBEAT_URL = "https://hc-ping.com/e02d463d-737c-4455-b12e-d307eb7313e4"

def get_final_price(user_id, base_price):
    """Renvoie 0.0 si l'utilisateur est admin, sinon le prix normal."""
    statut = get_user_statut(str(user_id))
    if statut == "admin":
        return 0.0
    return float(base_price)


def _coerce_price(raw) -> float:
    if raw is None: return 0.0
    try: return float(str(raw).replace(',', '.').replace('$', '').strip())
    except: return 0.0

def _mask_first(name: str) -> str:
    if not name: return "***"
    f = name.strip().split()[0]
    return (f[:1] + "***") if f else "***"

def _parse_product_fields(p: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "first": "", "last": "", "dob": "N/A", "address": "N/A", "city": "N/A",
        "postal": "", "email": "", "phone": "", "sin": "", "dl": "", 
        "password": "", "base": "N/A", "price": 0.0, "all_data": {}
    }
    
    content = (p.get("content") or "").strip()
    
    parsed_content = {}
    for line in content.splitlines():
        if ":" in line:
            # On sépare la clé et la valeur
            parts = line.split(":", 1)
            # 🧹 NETTOYAGE : On enlève les émojis et on met en MAJUSCULES
            raw_key = parts[0].strip()
            clean_key = re.sub(r'[^\w\s]', '', raw_key).strip().upper() 
            val = parts[1].strip()
            parsed_content[clean_key] = val

    out["all_data"] = parsed_content 

    # --- MAPPING BASÉ SUR TES RÉSULTATS DB ---
    out["first"] = parsed_content.get("NOM") or p.get("firstname") or ""
    out["sin"] = parsed_content.get("SIN") or parsed_content.get("NAS") or ""
    out["phone"] = parsed_content.get("TEL") or parsed_content.get("PHONE") or ""
    out["dob"] = parsed_content.get("DOB") or parsed_content.get("DATE") or "N/A"
    out["address"] = parsed_content.get("ADR") or parsed_content.get("ADDRESS") or "N/A"
    out["city"] = parsed_content.get("VILLE") or p.get("city") or "N/A"
    
    # Technique
    out["base"] = p.get("tier") or parsed_content.get("BASE") or "propro"
    out["price"] = _coerce_price(p.get("price")) or 0.72
    
    return out

def full_product_text(p: Dict[str, Any]) -> str:
    f = _parse_product_fields(p)
    
    lines = [
        "✅ **ACHAT RÉUSSI (LIVRAISON)**",
        "━━━━━━━━━━━━━━━━━━",
        f"👤 **NOM COMPLET**: `{f['first']}`",
        f"🎂 **DATE DE NAISSANCE**: `{f['dob']}`",
        f"🧾 **SIN (NAS)**: `{f['sin'] or 'Non disponible'}`",
        f"📞 **TÉLÉPHONE**: `{f['phone'] or 'Non disponible'}`",
        f"🏠 **ADRESSE**: `{f['address']}`",
        f"🏙️ **VILLE**: `{f['city']}`",
        "━━━━━━━━━━━━━━━━━━",
        f"💰 **PRIX**: `{f['price']:.2f} USD`",
        f"📂 **CATÉGORIE**: `PROPRO`"
    ]
    
    # On ajoute les champs bonus s'ils existent (ex: LANGUE)
    if "LANGUE" in f["all_data"]:
        lines.insert(8, f"🗣️ **LANGUE**: `{f['all_data']['LANGUE']}`")

    return "\n".join(lines)

async def handle_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        pid = int(q.data.split(":")[-1])
        user_id = str(update.effective_user.id)
        chat_id = update.effective_chat.id
        db = context.bot_data.get("db_conn")
        c = db.cursor()

        c.execute("SELECT * FROM products WHERE id=?", (pid,))
        row = c.fetchone()
        if not row: return await q.message.reply_text("❌ Produit introuvable.")

        colnames = [d[0] for d in c.description]
        prod = dict(zip(colnames, row))
        parsed = _parse_product_fields(prod)
        prod.update(parsed)

        price = prod["price"]
        balance = get_user_balance(user_id)

        if balance < price:
            return await q.message.reply_text(f"⚠️ Solde insuffisant ({balance:.2f} USD).")

        # --- 1. NETTOYAGE VISUEL IMMÉDIAT ---
        # On récupère tous les IDs de messages du catalogue et du panier pour les effacer
        msgs_to_kill = []
        msgs_to_kill += context.user_data.pop("catalog_msg_ids", [])
        msgs_to_kill += context.user_data.pop("cart_msg_ids", [])
        msgs_to_kill += CATALOG_MSGS.pop(chat_id, [])
        
        # On ajoute le message actuel (celui du bouton Payer)
        try: await q.message.delete()
        except: pass

        for mid in set(msgs_to_kill):
            try: await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except: pass

        # --- 2. TRAITEMENT DE LA TRANSACTION ---
        update_user_balance(user_id, -price)
        c.execute("INSERT INTO purchases (user_id, product_id, price, full_data, status, title, amount) VALUES (?,?,?,?,'paid',?,?)",
                  (user_id, pid, price, json.dumps(prod), prod.get("title"), price))
        c.execute("UPDATE products SET stock = stock - 1 WHERE id=? AND stock > 0", (pid,))
        
        # Nettoyage de l'article du panier s'il y était
        c.execute("DELETE FROM cart_items WHERE user_id=? AND product_id=?", (user_id, pid))
        db.commit()

        # --- 3. AFFICHAGE DU RÉSULTAT ET RETOUR ACCUEIL ---
        details = full_product_text(prod)
        sent = await context.bot.send_message(
            chat_id=chat_id, 
            text=f"✅ **ACHAT RÉUSSI**\n_Fiche visible 60s_\n\n{details}", 
            parse_mode="Markdown"
        )
        
        # Petit message flash de redirection
        redir = await context.bot.send_message(chat_id=chat_id, text="🔄 Retour au menu principal...")
        
        # Tâche de suppression de la fiche après 60s
        async def _del(m):
            await asyncio.sleep(60)
            try: await m.delete()
            except: pass
        asyncio.create_task(_del(sent))

        # Redirection réelle après un court instant
        await asyncio.sleep(1.5)
        try: await redir.delete()
        except: pass
        
        # Appel du menu principal propre
        return await show_main_menu(int(user_id), clear=True)

    except Exception as e:
        logger.error(f"Buy Error: {e}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Erreur lors de la transaction.")

async def cart_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    pid = int(q.data.split(":")[-1])
    user_id = str(update.effective_user.id)
    db = context.bot_data["db_conn"]
    c = db.cursor()
    c.execute("INSERT INTO cart_items (user_id, product_id, qty) VALUES (?,?,1)", (user_id, pid))
    db.commit()
    await q.answer("✅ Ajouté au panier (USD) !")

async def cart_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le panier (une bulle par article) en nettoyant tout l'écran avant."""
    query = update.callback_query
    try: await query.answer()
    except: pass
    
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)

    # === 🧹 GRAND NETTOYAGE ===
    msgs_to_kill = []
    
    # 1. Le bouton sur lequel on a cliqué
    try: await query.message.delete()
    except: pass

    # 2. Les listes de messages à détruire
    msgs_to_kill += context.user_data.pop('catalog_msg_ids', []) # <-- Récupère les IDs du catalogue
    msgs_to_kill += context.user_data.pop('cart_msg_ids', [])    # <-- Récupère les anciens paniers
    msgs_to_kill += context.user_data.pop('filter_msgs', [])
    msgs_to_kill += CATALOG_MSGS.pop(chat_id, []) # Par sécurité (ancien système)
    
    # Exécution du nettoyage
    for mid in set(msgs_to_kill):
        try: await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except: pass
    # ==========================
    
    # 1. Récupération DB
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT p.* FROM cart_items c JOIN products p ON c.product_id = p.id WHERE c.user_id = ?", (user_id,))
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()

    sent_ids = [] # Liste des nouveaux messages du panier

    # Panier vide
    if not items:
        m = await context.bot.send_message(chat_id=chat_id, text="🛒 **Votre panier est vide.**", reply_markup=kb_back_to_menu(), parse_mode="Markdown")
        context.user_data['cart_msg_ids'] = [m.message_id]
        return

    # 2. Affichage INDIVIDUEL (Une bulle par produit)
    for item in items:
        f = _parse_product_fields(item)
        
        def escape(t): return str(t).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
        
        nom = f"{f.get('first', '')} {f.get('last', '')}".strip().upper()
        if len(nom) < 2: nom = item['title'].split('•')[0].strip()
        
        city = f.get('city', item.get('city', 'N/A')).upper()
        year = f.get('year', item.get('year', 'N/A'))
        price = item['price']

        txt = (
            f"🔹 **{escape(nom)}**\n"
            f"🏙️ {escape(city)} | 📅 {year}\n"
            f"💰 **{price:.2f} USD**"
        )
        
        kb_item = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💳 Payer", callback_data=f"buy:{item['id']}"),
                InlineKeyboardButton("🗑 Retirer", callback_data=f"cart:del:{item['id']}")
            ]
        ])
        
        m = await context.bot.send_message(chat_id=chat_id, text=txt, reply_markup=kb_item, parse_mode="Markdown")
        sent_ids.append(m.message_id)

    # 3. Résumé final en bas
    total = sum(item['price'] for item in items)
    msg_total = f"🧾 **TOTAL GLOBAL : {total:.2f} USD**\n_Paiement groupé ou individuel ci-dessus._"
    
    kb_total = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🛍️ TOUT PAYER ({total:.2f} USD)", callback_data="cart:checkout")],
        [InlineKeyboardButton("🧹 Tout Vider", callback_data="cart:clear")],
        [InlineKeyboardButton("⬅️ Retour Menu", callback_data="menu_accueil")]
    ])
    
    m_tot = await context.bot.send_message(chat_id=chat_id, text=msg_total, reply_markup=kb_total, parse_mode="Markdown")
    sent_ids.append(m_tot.message_id)

    # On sauvegarde ces nouveaux messages pour pouvoir les effacer plus tard
    context.user_data['cart_msg_ids'] = sent_ids

async def cart_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vide le panier, efface les messages du panier et retourne au catalogue."""
    import asyncio
    import sqlite3
    
    q = update.callback_query
    try: await q.answer("🗑 Panier vidé !")
    except: pass
    
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id

    # 1. VIDER LA BASE DE DONNÉES
    con = sqlite3.connect(DB_NAME)
    con.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))
    con.commit()
    con.close()

    # 2. NETTOYAGE VISUEL (Supprime toutes les bulles du panier)
    # On récupère la liste des messages affichés par le panier
    msgs_to_kill = context.user_data.pop('cart_msg_ids', [])
    
    # On ajoute le message du bouton "Tout Vider" lui-même
    try: msgs_to_kill.append(q.message.message_id)
    except: pass
    
    # Suppression en boucle
    for mid in set(msgs_to_kill):
        try: await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except: pass

    # 3. MESSAGE DE CONFIRMATION (Temporaire)
    m = await context.bot.send_message(chat_id=chat_id, text="🗑 **Panier vidé.** Retour au catalogue...", parse_mode="Markdown")
    await asyncio.sleep(1.0) # Pause courte pour que l'utilisateur lise
    try: await m.delete()
    except: pass

    # 4. RETOUR AU CATALOGUE (Pro's)
    # On appelle show_products pour réafficher la liste des produits
    # Note: On remet page=0 pour revenir au début
    return await show_products(update, context, page=0, tier=None)

async def cart_remove_single(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retire un seul article du panier et rafraîchit l'affichage."""
    q = update.callback_query
    pid = int(q.data.split(":")[-1])
    user_id = str(update.effective_user.id)
    
    db = context.bot_data["db_conn"]
    db.execute("DELETE FROM cart_items WHERE user_id=? AND product_id=?", (user_id, pid))
    db.commit()
    
    await q.answer("🗑 Article retiré !")
    # On recharge le panier pour voir le changement
    await cart_view_callback(update, context)

async def cart_checkout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère le paiement global du panier."""
    q = update.callback_query
    await q.answer()
    
    user_id = str(update.effective_user.id)
    db = context.bot_data["db_conn"]
    c = db.cursor()
    
    # 1. Récupérer les items du panier
    c.execute("SELECT p.*, ci.qty FROM cart_items ci JOIN products p ON p.id=ci.product_id WHERE ci.user_id=?", (user_id,))
    rows = c.fetchall()
    
    if not rows:
        return await q.message.reply_text("🛒 Votre panier est vide.")

    # 2. Calculer le total
    total = 0.0
    colnames = [d[0] for d in c.description]
    items_to_buy = []
    for row in rows:
        prod = dict(zip(colnames[:-1], row[:-1]))
        qty = row[-1]
        price = _coerce_price(prod.get("price"))
        total += price * qty
        items_to_buy.append((prod, qty, price))

    # 3. Vérifier le solde
    balance = get_user_balance(user_id)
    if balance < total:
        return await q.message.reply_text(f"⚠️ Solde insuffisant ({balance:.2f} USD / Requis: {total:.2f} USD).")

    # 4. Traiter l'achat pour chaque item
    update_user_balance(user_id, -total)
    for prod, qty, price in items_to_buy:
        pid = prod['id']
        # Enregistrer l'achat
        c.execute("INSERT INTO purchases (user_id, product_id, price, full_data, status, title, amount) VALUES (?,?,?,?,'paid',?,?)",
                  (user_id, pid, price, json.dumps(prod), prod.get("title"), price))
        # Décrémenter le stock
        c.execute("UPDATE products SET stock = stock - ? WHERE id=? AND stock >= ?", (qty, pid, qty))
    
    # 5. Vider le panier et confirmer
    c.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))
    db.commit()

    await q.message.edit_text(f"✅ **PAIEMENT RÉUSSI !**\nTotal débité : `{total:.2f} USD`.\n\nConsultez votre historique pour voir vos produits.", parse_mode="Markdown")

async def handle_view_callback(update, context):
    q = update.callback_query
    pid = int(q.data.split(":")[-1])
    db = context.bot_data["db_conn"]
    row = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if row:
        colnames = [d[0] for d in db.execute("SELECT * FROM products LIMIT 1").description]
        await q.message.reply_text(full_product_text(dict(zip(colnames, row))), parse_mode="Markdown")

def generate_ref_number():
    """Génère un numéro de référence au format standard (ex: R4MV-5A2B)"""
    prefix = random.choice(["R4MV", "PEVF"]) # Vos préfixes habituels
    chars = string.ascii_uppercase + string.digits
    suffix = ''.join(random.choices(chars, k=5))
    return f"{prefix}{suffix}"

def get_signalwire_balance():
    """Vérifie la connexion SignalWire."""
    from dotenv import load_dotenv
    load_dotenv(override=True)

    pid = os.environ.get("SW_PROJECT_ID", "").strip() or os.environ.get("SIGNALWIRE_PROJECT_ID", "").strip()
    token = os.environ.get("SW_TOKEN", "").strip() or os.environ.get("SIGNALWIRE_TOKEN", "").strip()
    space_url = os.environ.get("SIGNALWIRE_SPACE_URL", "").strip()

    if not pid or not token:
        return "Clés manquantes"

    host = space_url.replace("https://", "").replace("/", "").strip()
    
    # On tente de lire l'historique juste pour tester la connexion
    url = f"https://{host}/api/laml/2010-04-01/Accounts/{pid}/Calls.json"
    params = {"PageSize": 1}

    try:
        response = requests.get(url, auth=(pid, token), params=params, timeout=5)
        
        if response.status_code == 200:
            return "✅ Connecté (Full)"
            
        elif response.status_code == 403:
            # 403 = Authentifié mais lecture interdite.
            # C'est bon signe : les clés sont valides pour envoyer des appels.
            return "✅ Connecté (Appels OK)"
            
        elif response.status_code == 401:
            return "❌ Erreur Clés (401)"
        
        elif response.status_code == 404:
            return "❌ Erreur Projet (404)"

        return f"Erreur {response.status_code}"

    except Exception as e:
        logger.error(f"[SW ERROR] {e}")
        return "Erreur Connexion"

def get_barcode_balance():
    """Récupère le solde Barcode Solution (Clé: credits)."""
    api_key = os.environ.get("FAKEID_API_KEY")
    if not api_key: 
        return "Non configuré"

    url = "https://barcodes.fakeidsolutions.com/api/v2/balance"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json"
    }

    try:
        response = requests.get(url, headers=headers, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            # 👇 C'est ici que ça change : on lit "credits"
            credits = data.get("credits")
            
            if credits is not None:
                return f"{credits} Crédits"
            else:
                return "Format inconnu"
        
        return f"Erreur {response.status_code}"
            
    except Exception as e:
        print(f"Erreur Barcode: {e}")
        return "Erreur Connexion"
    
def api_5sim_get_balance():
    """Récupère le solde 5SIM pour l'affichage admin."""
    if not SIM_API_KEY: return "Non Configuré"
    try:
        headers = {"Authorization": "Bearer " + SIM_API_KEY, "Accept": "application/json"}
        r = requests.get("https://5sim.net/v1/user/profile", headers=headers, timeout=3)
        if r.status_code == 200:
            bal = r.json().get("balance", 0.0)
            return f"{bal} RUB" # ou changez RUB par $ si votre compte est en dollars
        return "Erreur API"
    except:
        return "Erreur"

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


# ========================== CONSTANTES ==========================

(ID_MENU_START, ID_CAT_VIEW, ID_PROD_VIEW, ID_ASK_QTY, ID_CONFIRM_BUY,
 ID_ASK_NAME, ID_ASK_DOB, 
 ID_ASK_STREET, ID_ASK_CITY, ID_ASK_ZIP, ID_CONFIRM_ADDR,
 ID_ASK_DOC_EMPLOYER, ID_ASK_DOC_JOB, ID_ASK_DOC_ADDR, ID_CHOOSE_INxCOME_MODE,
 ID_ASK_DOC_HOURS, ID_ASK_DOC_RATE, ID_ASK_DOC_SIN,
 ID_ASK_HEIGHT, ID_ASK_EYES, ID_ASK_PHOTO,
 # --- NOUVELLES ÉTAPES AJOUTÉES ---
 ID_ASK_LASTNAME, ID_ASK_ISSUE, ID_ASK_EXPIRY, 
 ID_ASK_DL_NUM, ID_ASK_REF_NUM, ID_ASK_SEX, ID_CONFIRM_SUMMARY,
 TICKET_DRAFT
 ) = range(3000, 3029) # On augmente le range à 3028
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


(ACC_WAIT_NEW_PIN, ACC_WAIT_USERNAME, ACC_WAIT_JABBER, ACC_WAIT_RESET_CONFIRM) = range(3200, 3204)

# Paliers
FORFAITS = {
    "bronze":   {"min": 0,    "price": 7.00, "label": "🟫 Bronze"},   
    "silver":   {"min": 175,  "price": 5.50, "label": "⬜️ Silver"},  
    "gold":     {"min": 350,  "price": 4.25, "label": "🟨 Gold"},    
    "platinum": {"min": 700,  "price": 2.80, "label": "⬛️ Platinum"}, 
    "admin":    {"min": 0,     "price": 0.00,   "label": "👑 ADMIN"},
}

# ========================== GLOBALS ==========================
bot_loop = None
bot_messages = {}
user_sessions = {}
active_calls = {}
pending_payments = {}
user_validation_status = {}
batch_runs = {}
CATALOG_MSGS = {} 
USER_STATES = {}

# ========================== MESSAGES ==========================
MESSAGES = {
    'welcome': {
        'fr': (
            "👋 Bienvenue sur Nomen Nescio !\n\n"
            "Votre espace central, réunissant tout ce qu’il vous faut.\n\n"
            "🆔 : {telegram_id}\n"
            "💰 Solde : {balance:.2f} USD\n" # <--- ICI
            "{statut_label}\n"
            "{admin_info}\n"
            "\n\n🔑 Appuyez simplement sur la touche de votre choix."
        ),
        'en': (
            "👋 Welcome to Nomen Nescio!\n\n"
            "Your central hub for everything you need.\n\n"
            "🆔 : {telegram_id}\n"
            "💰 Balance: {balance:.2f} USD\n" # <--- ET ICI
            "{statut_label}\n"
            "{admin_info}\n"
            "\n\n🔑 Simply press the key of your choice."
        ),
    },
    'choose_lang': {
        'fr': "🌐 Choisissez votre langue / Select your language",
        'en': "🌐 Please select your language / Choisissez votre langue"
    },
    'balance': {
        'fr': "Votre solde actuel est : {balance:.2f} $ USD.",
        'en': "Your current balance is: {balance:.2f} USD."
    },
    'enter_bulk_qty': {
        'fr': "Combien de permis voulez-vous valider ? (3 Max.)",
        'en': "How many licenses do you want to validate? (3 Max.)"
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
    
    # ==========================================
    # 🚀 OPTIMISATION PERFORMANCE (Mode TURBO)
    # ==========================================
    # Permet de lire et écrire en même temps.
    # Réduit drastiquement le lag quand plusieurs utilisateurs sont actifs.
    con.execute("PRAGMA journal_mode=WAL;") 
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA cache_size=10000;") 
    # ==========================================

    cur = con.cursor()
    
    # 1. TABLE DES UTILISATEURS
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
            forfait TEXT DEFAULT 'bronze',
            custom_username TEXT,
            jabber_id TEXT,
            inactivity_timeout INTEGER DEFAULT 300,
            pagination INTEGER DEFAULT 2
        )
    """)
    
    # Patchs de sécurité (Ajoute les colonnes manquantes sans écraser les données)
    columns_to_check = [
        ("user_id", "TEXT"), 
        ("seed_phrase", "TEXT"), 
        ("pin_code", "TEXT"), 
        ("username", "TEXT"), 
        ("custom_username", "TEXT"), 
        ("jabber_id", "TEXT"),
        ("inactivity_timeout", "INTEGER DEFAULT 300"),
        ("pagination", "INTEGER DEFAULT 2")
    ]
    
    for col_name, col_type in columns_to_check:
        try:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError: 
            pass # La colonne existe déjà, on ignore

    # 2. TABLE DES TRANSACTIONS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            order_id TEXT PRIMARY KEY,
            telegram_id TEXT,
            amount REAL,
            status TEXT DEFAULT 'pending',
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 3. TABLE DES PRODUITS (Mise à jour défaut USD)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            title TEXT,
            price REAL,
            currency TEXT DEFAULT 'USD', 
            stock INTEGER DEFAULT 0,
            tier TEXT,
            is_active INTEGER DEFAULT 1,
            content TEXT
        )
    """)

    # 4. TABLE DES TICKETS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            username TEXT,
            category TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            message TEXT,
            dashboard_msg_id INTEGER
        )
    """)
    
    # Patchs pour support_tickets
    for col in [
        ("username", "TEXT"), 
        ("category", "TEXT"), 
        ("message", "TEXT"), 
        ("dashboard_msg_id", "INTEGER")
    ]:
        try:
            cur.execute(f"ALTER TABLE support_tickets ADD COLUMN {col[0]} {col[1]}")
        except sqlite3.OperationalError: pass

    # 5. HISTORIQUE DES MESSAGES (Chat Support)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ticket_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            sender_role TEXT, -- 'user' ou 'admin'
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(ticket_id) REFERENCES support_tickets(ticket_id)
        )
    """)

    # 6. TABLE LOTERIE
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lottery (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            tickets INTEGER DEFAULT 0
        )
    """)

    con.commit()
    con.close()
    log("DB initialized (V3.2 + Mode WAL TURBO 🚀)", "SYSTEM")

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

# --- AJOUTER CES DEUX FONCTIONS ---
def get_user_pagination(user_id: str) -> int:
    """Récupère le nombre d'items par page (Défaut: 2)."""
    try:
        con = sqlite3.connect(DB_NAME)
        cur = con.cursor()
        # On tente de lire la colonne. Si elle n'existe pas, le 'try' échouera et on renverra 2 (défaut).
        # Note: Assure-toi d'avoir lancé le script update_db.py avant !
        cur.execute("SELECT pagination FROM users WHERE telegram_id=?", (user_id,))
        row = cur.fetchone()
        con.close()
        return int(row[0]) if row and row[0] else 2
    except:
        return 2

def set_user_pagination(user_id: str, qty: int):
    """Sauvegarde le choix de pagination."""
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    # On ajoute la colonne si elle manque (sécurité 'à la volée')
    try: cur.execute("ALTER TABLE users ADD COLUMN pagination INTEGER DEFAULT 2")
    except: pass
    
    cur.execute("UPDATE users SET pagination=? WHERE telegram_id=?", (qty, user_id))
    con.commit()
    con.close()
# ----------------------------------

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
    """Calcul du Soundex pour la SAAQ (LNNN)."""
    nom = (nom or "").upper()
    if not nom:
        return "0000"
    
    # 1. On garde la première lettre
    first_char = nom[0]
    
    # Mapping des consonnes (Standard SAAQ)
    # A, E, H, I, O, U, W, Y ne sont pas codés (valeur 0 ou ignorés)
    mapping = {
        'B': '1', 'F': '1', 'P': '1', 'V': '1',
        'C': '2', 'G': '2', 'J': '2', 'K': '2', 'Q': '2', 'S': '2', 'X': '2', 'Z': '2',
        'D': '3', 'T': '3',
        'L': '4',
        'M': '5', 'N': '5',
        'R': '6'
    }
    
    # On commence le code avec la première lettre
    code = [first_char]
    
    # Code de la lettre précédente (pour gérer les doubles)
    # On initialise avec le code de la 1ère lettre si elle en a un
    last_code = mapping.get(first_char) 

    # On parcourt le reste du nom
    for char in nom[1:]:
        current_code = mapping.get(char)
        
        if current_code:
            # Si c'est un code différent du précédent, on ajoute
            # OU si c'est le même code mais séparé par une voyelle (last_code était devenu None)
            if current_code != last_code:
                code.append(current_code)
                last_code = current_code
        else:
            # Si c'est une voyelle ou H/W, ça brise la chaîne de fusion pour la suite
            # (Sauf H et W qui sont souvent transparents, mais pour simplifier ici on reset)
            if char not in ['H', 'W']: 
                last_code = None 

        if len(code) >= 4:
            break
            
    return "".join(code).ljust(4, '0')

def codif_prenom(prenom: str) -> str:
    mapping = {
        'A': '1','B':'1','C':'2','D':'3','E':'3','F':'3','G':'4','H':'4','I':'4',
        'J':'5','K':'5','L':'5','M':'6','N':'6','O':'6','P':'7','Q':'7','R':'7',
        'S':'8','T':'8','U':'9','V':'9','W':'9','X':'9','Y':'9','Z':'9'
    }
    letter = (prenom or "0")[0].upper()
    return mapping.get(letter, '0')

def generer_permis(nom: str, prenom: str, date_txt: str):
    try:
        # On force la lecture du format JJ-MM-AAAA
        date_obj = datetime.strptime(date_txt, "%d-%m-%Y")
        
        day_str = date_obj.strftime("%d")    # 07
        month_str = date_obj.strftime("%m")  # 05
        year_str = date_obj.strftime("%y")   # 97

        # Assemblage Date SAAQ (JJMMAA)
        date_saaq = f"{day_str}{month_str}{year_str}"
        
        # Codes d'identité
        code_nom = soundex(nom)            # P612
        code_prenom = codif_prenom(prenom) # 1
        
        
        base = code_nom + code_prenom + date_saaq
        
        # Format pour l'affichage Telegram
        formatted = f"{base[:5]}-{base[5:11]}-**"
        
        return formatted, base
    except Exception as e:
        print(f"Erreur Gen Permis: {e}")
        return "ERREUR-DATE", "00000000000"

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
    
    # --- VÉRIFICATION NOTIFICATIONS CLIENT ---
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT count(*) FROM support_tickets WHERE user_id=? AND status='replied'", (str(user_id),))
    has_reply = cur.fetchone()[0] > 0
    con.close()
    
    label_support = "📞 Support (🔴 1)" if has_reply else "📞 Support"

    menu = [
        [InlineKeyboardButton("🪪 ID's", callback_data="id_menu_entry")],
        [InlineKeyboardButton("💳 Cc's", callback_data="ccs_catalog_start")],
        [InlineKeyboardButton("👥 Pro's", callback_data="propro")],
        [InlineKeyboardButton("⚒️ Tools ", callback_data="section_tools")],
        [InlineKeyboardButton("🛒 Panier", callback_data="cart:view")],
        [InlineKeyboardButton("📜 Historique", callback_data="hist:view")],
        [InlineKeyboardButton("💳 Recharger" if lang == "fr" else "💳 Top up", callback_data="add_balance")],
        [InlineKeyboardButton("🌐 Langue/Language", callback_data="choose_lang")],
        [InlineKeyboardButton("📣 Channel", callback_data="join_private_channel")],
        [InlineKeyboardButton(label_support, callback_data="support")], 
        [InlineKeyboardButton("📚 FAQ", callback_data="faq")],
        [InlineKeyboardButton("👤 Mon Compte", callback_data="account_menu")],
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
    if clear: await clear_conversation(user_id)
    
    balance = get_user_balance(str(user_id))
    statut_code = get_user_statut(str(user_id)) 
    lang = get_user_lang(str(user_id))
    
    
    # --- LOGIQUE ADMIN INFO ---
    admin_info = ""
    if str(user_id) in ADMIN_IDS:
        sw_bal = get_signalwire_balance()
        bc_bal = get_barcode_balance()
        sim_bal = api_5sim_get_balance()

        admin_info = f"\n🏦 SignalWire: {sw_bal}\n🪪 BarcodeSolution: {bc_bal}\n📱 5SIM: {sim_bal}"

 
    details_forfait = FORFAITS.get(statut_code, FORFAITS["bronze"])
    

    statut_label = f"🏆 Statut : {details_forfait['label']}"
    # ----------------------------------------

    await app_telegram.bot.send_message(
        chat_id=user_id,
        text=MESSAGES['welcome'][lang].format(
            telegram_id=user_id,
            balance=balance,
            statut_label=statut_label,
            admin_info=admin_info 
        ),
        reply_markup=build_main_menu(user_id),
        parse_mode="Markdown"
    )

# ========================== CATALOGUE PRODUITS ==========================

def _get_products_optimized(category, page=0, per_page=2, filters=None, tier=None):
    """
    Moteur de recherche SQL V6 (Précision Stricte).
    Règle le problème de chevauchement des villes.
    """
    con = sqlite3.connect(DB_NAME)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    
    # 1. Base des conditions
    conditions = ["category=?", "is_active=1", "stock>0"]
    params = [category]
    
    if tier:
        conditions.append("tier=?")
        params.append(tier)
        
    # 2. Application des filtres intelligents
    if filters:
        # PRIX : Filtrage numérique strict
        if filters.get('price'):
            try:
                raw = str(filters['price']).replace(',', '.').replace('$', '').strip()
                conditions.append("price <= ?")
                params.append(float(raw))
            except: pass 

        # VILLE : Recherche ciblée sur la colonne city uniquement
        if filters.get('city'):
            val = filters['city'].strip()
            conditions.append("city LIKE ?")
            params.append(f"%{val}%")

        # BINS (Spécifique Cc's) : Recherche dans le contenu technique
        if filters.get('bins'):
             val = filters['bins'].strip()
             conditions.append("content LIKE ?")
             params.append(f"%{val}%")

        # AUTRES (Année, Nom, Base) : Recherche textuelle large
        # Note : 'city' a été retiré de cette boucle pour éviter les doublons
        for key in ['year', 'name', 'base']:
            if filters.get(key):
                val = filters[key].strip()
                conditions.append("(title LIKE ? OR content LIKE ?)")
                params.append(f"%{val}%")
                params.append(f"%{val}%")

    where_sql = " AND ".join(conditions)

    try:
        # 3. Comptage du total (pour la pagination)
        count_query = f"SELECT COUNT(*) FROM products WHERE {where_sql}"
        cur.execute(count_query, params)
        total_count = cur.fetchone()[0]

        # 4. Récupération des données avec tri par ID descendant (plus récents en premier)
        data_query = f"SELECT * FROM products WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?"
        full_params = params + [per_page, page * per_page]
        rows = cur.execute(data_query, full_params).fetchall()
        con.close()

        # 5. Formatage des résultats
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
                "content": p['content'],
                "city": p.get('city', 'N/A'), # <-- AJOUTER CETTE LIGNE
                "year": p.get('year', 'N/A')  # <-- AJOUTER CETTE LIGNE
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
    filt_row = [
        InlineKeyboardButton("🔎 Filter", callback_data="filter_open"),
        InlineKeyboardButton("⚙️ Vue", callback_data="open_pagination_menu") # <--- NOUVEAU BOUTON
    ]
    nav_row = [
        InlineKeyboardButton("«", callback_data=f"prod:page:{max(0,page-1)}"),
        InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"),
        InlineKeyboardButton("»", callback_data=f"prod:page:{page+1}"),
    ]
    back_row = [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
    return InlineKeyboardMarkup([filt_row, nav_row, back_row])

async def open_pagination_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le choix du nombre d'articles par page."""
    q = update.callback_query
    await q.answer()
    
    user_id = str(update.effective_user.id)
    current = get_user_pagination(user_id)
    
    def txt(val): return f"✅ {val}" if current == val else f"{val}"
    
    kb = [
        [
            InlineKeyboardButton(txt(2), callback_data="set_pg_2"),
            InlineKeyboardButton(txt(4), callback_data="set_pg_4"),
            InlineKeyboardButton(txt(8), callback_data="set_pg_8")
        ],
        [InlineKeyboardButton(f"✏️ Custom ({current})", callback_data="set_pg_custom")],
        [InlineKeyboardButton("🔙 Retour Catalogue", callback_data="propro")]
    ]
    
    await q.message.edit_text(
        f"📄 **RÉGLAGE AFFICHAGE**\n\nCombien de produits voulez-vous voir par page ?\nActuel : **{current}**",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )



async def show_products(update, context, page=0, tier=None, from_filter=False):
    import asyncio 
    import os
    import sqlite3
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton

    query = getattr(update, "callback_query", None)
    chat_id = query.message.chat_id if query else update.effective_chat.id
    user_id = str(update.effective_user.id)
    
    context.user_data['cart_return_to'] = "cat:propro"
    
    # --- NETTOYAGE PRÉALABLE ---
    # On efface tout ce qui traîne (filtres, anciens catalogues, paniers)
    msgs_to_del = []
    msgs_to_del += context.user_data.pop("filter_fiches_msg_ids", [])
    msgs_to_del += context.user_data.pop("filter_msgs", [])
    msgs_to_del += context.user_data.pop("catalog_msg_ids", []) # <-- On récupère les anciens IDs
    msgs_to_del += context.user_data.pop("cart_msg_ids", [])     # <-- On efface le panier si ouvert
    
    if query and not from_filter:
        try: await query.message.delete()
        except: pass

    for mid in set(msgs_to_del):
        try: await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except: pass
    # ---------------------------

    # 1. Message de chargement
    loading_msg = None
    try: loading_msg = await context.bot.send_message(chat_id=chat_id, text="⏳")
    except: pass

    # 2. REQUÊTE SQL TURBO
    try:
        PER_PAGE = get_user_pagination(user_id)
        
        # Filtres
        filters = context.user_data.get('active_filters', {})
        where_clause = "WHERE category='propro' AND is_active=1 AND stock > 0"
        params = []

        if filters.get('city'):
            where_clause += " AND city LIKE ?"
            params.append(f"%{filters['city']}%")
        if filters.get('year'):
            where_clause += " AND year = ?"
            params.append(filters['year'])
        if tier:
            where_clause += " AND tier = ?"
            params.append(tier)

        con = sqlite3.connect(DB_NAME)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        
        cur.execute(f"SELECT COUNT(id) FROM products {where_clause}", params)
        total_items = cur.fetchone()[0]

        query_sql = f"SELECT * FROM products {where_clause} ORDER BY id DESC LIMIT ? OFFSET ?"
        cur.execute(query_sql, params + [PER_PAGE, page * PER_PAGE])
        chunk = [dict(row) for row in cur.fetchall()]
        con.close()

    except Exception as e:
        if loading_msg: await loading_msg.edit_text("⚠️ Erreur technique.")
        return

    if loading_msg:
        try: await loading_msg.delete()
        except: pass

    total_pages = max(1, (total_items + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    
    # Liste pour mémoriser les nouveaux messages affichés
    sent_ids = []

    if not chunk:
        m = await context.bot.send_message(chat_id=chat_id, text="❌ Aucun produit trouvé.", reply_markup=kb_back_to_menu())
        context.user_data['catalog_msg_ids'] = [m.message_id]
        return

    # 3. Affichage des Produits
    for p in chunk:
        txt = (
            f"👤 **NOM** : `{p['title'].split('•')[0].strip()}`\n"
            f"🏙️ **VILLE** : `{p.get('city', 'N/A')}`\n"
            f"📅 **ANNÉE** : `{p.get('year', 'N/A')}`\n"
            f"📂 **BASE** : `{p.get('tier', 'N/A')}`\n"
            f"💰 **PRIX** : `{p['price']:.2f} USD`"
        )
        
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛒 Add", callback_data=f"cart:add:{p['id']}"), 
            InlineKeyboardButton("⚡ Buy Now", callback_data=f"buy:{p['id']}")
        ]])
        
        try:
            m = await context.bot.send_message(chat_id=chat_id, text=txt, reply_markup=kb, parse_mode="Markdown")
            sent_ids.append(m.message_id)
        except: pass

    # 4. Menu Navigation
    con = sqlite3.connect(DB_NAME)
    cart_count = con.execute("SELECT count(*) FROM cart_items WHERE user_id=?", (user_id,)).fetchone()[0]
    con.close()

    nav_row = [
        InlineKeyboardButton("«", callback_data=f"prod:page:{max(0, page-1)}"), 
        InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"), 
        InlineKeyboardButton("»", callback_data=f"prod:page:{min(total_pages-1, page+1)}")
    ]

    kb_final = []
    if cart_count > 0:
        kb_final.append([InlineKeyboardButton(f"🧺 Voir Panier ({cart_count})", callback_data="cart:view")])
    kb_final.append([InlineKeyboardButton("🔎 Filter", callback_data="filter_open"), InlineKeyboardButton("⚙️ Vue", callback_data="open_pagination_menu")])
    kb_final.append(nav_row)
    kb_final.append([InlineKeyboardButton("⬅️ Retour Menu", callback_data="menu_accueil")])

    m = await context.bot.send_message(chat_id=chat_id, text="🔻 **Navigation** 🔻", reply_markup=InlineKeyboardMarkup(kb_final))
    sent_ids.append(m.message_id)
    
    # 5. SAUVEGARDE CRITIQUE (Pour que le panier puisse les effacer)
    if from_filter:
        context.user_data['filter_fiches_msg_ids'] = sent_ids
    else:
        context.user_data['catalog_msg_ids'] = sent_ids # <-- C'est ici que ça se joue

        
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

# ==============================================================================
# 🕵️ VERSION DEBUG DU SYSTÈME DE FILTRE
# ==============================================================================

async def filter_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("\n[DEBUG] 🟢 filter_start : L'utilisateur a cliqué sur Filter", flush=True)
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id

    # Reset
    context.user_data['pending_filters'] = {}
    context.user_data.pop('active_filters', None)
    print("[DEBUG] Filtres nettoyés. Envoi du menu...", flush=True)
    
    # Nettoyage visuel
    old_catalog_msgs = CATALOG_MSGS.pop(chat_id, [])
    for mid in old_catalog_msgs:
        try: await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except: pass

    kb = _build_filter_menu(context)
    # On supprime l'ancien message et on envoie du neuf pour éviter les bugs d'ID
    try: await q.message.delete()
    except: pass
    
    m = await q.message.reply_text("🔎 **MODE FILTRE ACTIVÉ**\nChoisissez un critère ci-dessous :", reply_markup=kb)
    context.user_data['filter_msgs'] = [m.message_id]
    
    print("[DEBUG] Menu filtre affiché. Passage état CATALOG_FILTER_MAIN", flush=True)
    return CATALOG_FILTER_MAIN

async def filter_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    field = q.data.split(':', 1)[1]
    context.user_data['current_filter_key'] = field
    print(f"\n[DEBUG] 🟡 filter_select_type : Catégorie choisie = {field}", flush=True)
    
    # Suppression du menu pour afficher la question
    try: await q.message.delete()
    except: pass

    prompts = {
        'name':  "✍️ **FILTRE NOM**\nEntrez une partie du nom (ex: `Tremblay`) :",
        'city':  "✍️ **FILTRE VILLE**\nEntrez la ville (ex: `Montreal`) :",
        'base':  "✍️ **FILTRE BASE**\nEntrez la base (ex: `PROPRO`) :",
        'price': "✍️ **FILTRE PRIX**\nEntrez le prix max (ex: `10`) :",
        'year':  "✍️ **FILTRE ANNÉE**\nEntrez l'année (ex: `1995`) :",
    }
    txt = prompts.get(field, f"Entrez la valeur pour {field} :")

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=txt,
        reply_markup=kb_back_cancel(), 
        parse_mode="Markdown"
    )
    context.user_data['filter_prompt_id'] = msg.message_id
    
    print("[DEBUG] Question posée. Passage état CATALOG_FILTER_AWAIT_VALUE", flush=True)
    return CATALOG_FILTER_AWAIT_VALUE

async def filter_receive_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"\n[DEBUG] 🔵 filter_receive_value : Texte reçu = '{update.message.text}'", flush=True)
    
    user_id = update.effective_chat.id
    key = context.user_data.get('current_filter_key')

    if not key:
        print("[DEBUG] ❌ ERREUR : Pas de clé", flush=True)
        await context.bot.send_message(chat_id=user_id, text="⚠️ Erreur session. Recommencez.")
        return await show_products(update, context, page=0)

    # 1. Sauvegarde de la valeur
    value = update.message.text.strip()
    context.user_data.setdefault('pending_filters', {})[key] = value
    
    # 2. 🧹 NETTOYAGE VISUEL (La solution à ton problème)
    try:
        # Supprime ton message (ex: "1997")
        await update.message.delete() 
        
        # Supprime la question du bot (ex: "Entrez l'année")
        prompt_id = context.user_data.get('filter_prompt_id')
        if prompt_id:
            await context.bot.delete_message(chat_id=user_id, message_id=prompt_id)
            print(f"[DEBUG] Question {prompt_id} supprimée.", flush=True)
    except Exception as e:
        print(f"[DEBUG] Erreur nettoyage : {e}", flush=True)

    # 3. Reconstruction et Envoi du menu de résumé
    kb = _build_filter_menu(context)
    summary = "🔎 **FILTRES EN COURS**\n\n"
    for k, v in context.user_data.get('pending_filters', {}).items():
        summary += f"• {k.capitalize()}: `{v}`\n"
    summary += "\n👇 Ajoutez un autre critère ou cliquez sur **Search**."

    m = await context.bot.send_message(chat_id=user_id, text=summary, reply_markup=kb, parse_mode="Markdown")
    
    # On mémorise ce nouveau message pour pouvoir le supprimer au prochain tour
    context.user_data['filter_msgs'] = [m.message_id]
    
    return CATALOG_FILTER_MAIN

async def filter_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("\n[DEBUG] 🟣 filter_search : Bouton Search cliqué", flush=True)
    q = update.callback_query
    
    pending = context.user_data.get('pending_filters', {})
    print(f"[DEBUG] Filtres à appliquer : {pending}", flush=True)
    
    context.user_data['active_filters'] = pending
    
    # Appel du catalogue
    print("[DEBUG] Appel de show_products...", flush=True)
    await show_products(update, context, page=0, tier=None, from_filter=True)
    
    print("[DEBUG] Fin recherche. Retour CATALOG_FILTER_MAIN", flush=True)
    return CATALOG_FILTER_MAIN

async def filter_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("\n[DEBUG] 🟠 filter_reset cliqué", flush=True)
    q = update.callback_query
    await q.answer("Filtres réinitialisés")
    
    context.user_data.pop('pending_filters', None)
    context.user_data.pop('active_filters', None)
    
    # Nettoyage fiches
    prev = context.user_data.pop("filter_fiches_msg_ids", [])
    for mid in prev:
        try: await context.bot.delete_message(chat_id=q.message.chat_id, message_id=mid)
        except: pass

    kb = _build_filter_menu(context)
    await q.message.edit_text("🔎 **FILTRES VIDE**\nRecommencez :", reply_markup=kb, parse_mode="Markdown")
    return CATALOG_FILTER_MAIN

async def filter_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("\n[DEBUG] 🔴 filter_cancel cliqué", flush=True)
    q = update.callback_query
    await q.answer()
    
    # Nettoyage
    context.user_data.pop('pending_filters', None)
    context.user_data.pop('current_filter_key', None)
    try: await q.message.delete()
    except: pass

    print("[DEBUG] Retour catalogue normal", flush=True)
    await show_products(update, context, page=0, tier=None, from_filter=False)
    return ConversationHandler.END

CCS_CATALOG_MSGS = {} 


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
    # On utilise .get() pour ne pas perdre la clé en cas d'erreur
    key = context.user_data.get('ccs_current_filter_key') 
    
    if not key:
        return CCS_FILTER_MAIN
        
    value = update.message.text.strip()
    context.user_data.setdefault('ccs_pending_filters', {})[key] = value
    
    try: await update.message.delete()
    except: pass
        
    kb = _build_filter_menu_ccs(context)
    txt = f"✅ **Filtre Ajouté (CCS)**\n{key.capitalize()} = `{value}`\n\nAppuyez sur **Search** pour lancer."
    
    edited = False
    msg_ids = context.user_data.get('ccs_filter_msgs', [])
    
    if msg_ids:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg_ids[0],
                text=txt,
                reply_markup=kb,
                parse_mode="Markdown"
            )
            edited = True
        except: pass
    
    # LA SÉCURITÉ ICI AUSSI
    if not edited:
        m = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=txt,
            reply_markup=kb,
            parse_mode="Markdown"
        )
        context.user_data['ccs_filter_msgs'] = [m.message_id] 
    
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

def get_btc_price_usd():
    try:
        # On demande le prix en USD à CoinGecko
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", timeout=5)
        return float(r.json()['bitcoin']['usd'])
    except:
        # Fallback : Prix approximatif du BTC en USD (Mets une valeur sûre)
        return 96000.0

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
        await q.message.reply_text("⚠️ Erreur config: XPUB manquant ")
        return ConversationHandler.END

    await replace_view(
        q,
        "💸 **Recharge votre solde !**\n\n"
        "Combien voulez-vous ajouter ? \n"
        "_(Exemple: tapez 50)_",
        reply_markup=kb_back_to_menu(),
        parse_mode="Markdown"
    )
    return WAIT_AMOUNT_CRYPTO

async def receive_amount_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        # On lit le montant comme étant du USD
        amount_usd = float(update.message.text.replace(',', '.').strip())
        if amount_usd < 5: 
            await update.message.reply_text("❌ Minimum 5$ USD.")
            return WAIT_AMOUNT_CRYPTO
    except:
        await update.message.reply_text("❌ Montant invalide.")
        return WAIT_AMOUNT_CRYPTO

    msg_wait = await update.message.reply_text("⏳ Génération de l'adresse...")
    
    # Calcul en USD
    btc_price = get_btc_price_usd() # <-- Appel de la nouvelle fonction
    amount_btc = amount_usd / btc_price
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    # On sauvegarde amount_usd dans la colonne amount_cad (ou tu peux renommer ta colonne plus tard)
    cur.execute("INSERT INTO crypto_payments (user_id, amount_cad, amount_btc_expected) VALUES (?,?,?)", 
                (str(user_id), amount_usd, amount_btc))
    order_id = cur.lastrowid
    
    address = generate_address(user_id, order_id)
    
    cur.execute("UPDATE crypto_payments SET address=? WHERE id=?", (address, order_id))
    con.commit()
    con.close()
    
    try: await msg_wait.delete()
    except: pass

    txt = (
        f"🧾 **Facture #{order_id}**\n"
        f"💰 Montant : `{amount_usd:.2f} USD`\n" # <-- Affichage USD
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
    """Ferme proprement l'écran courant, nettoie TOUT et affiche le menu principal."""
    q = getattr(update, "callback_query", None)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if q:
        try: await q.answer()
        except: pass
        # Supprime le message sur lequel on a cliqué (le bouton Retour)
        try: await q.message.delete()
        except: pass

    # === 🧹 GRAND NETTOYAGE (Le "Kärcher") ===
    # On liste toutes les clés de mémoire qui peuvent contenir des IDs de messages à supprimer
    keys_to_clean = [
        "catalog_msg_ids",          # Catalogue Pro's (Nouveau)
        "cart_msg_ids",             # Panier (Nouveau)
        "filter_msgs",              # Menu Filtre Pro's
        "filter_fiches_msg_ids",    # Résultats Filtre Pro's
        "ccs_filter_msgs",          # Menu Filtre CCS
        "ccs_filter_fiches_msg_ids",# Résultats Filtre CCS
        "hist_msgs",                # Historique
        "verif_flow_msg_ids",       # Vérification Permis
        "cleanup_ids"               # Formulaires ID
    ]

    msgs_to_kill = []

    # 1. On vide la mémoire utilisateur
    for key in keys_to_clean:
        msgs_to_kill += context.user_data.pop(key, [])

    # 2. On vide les dictionnaires globaux (Ancien système, par sécurité)
    if chat_id in CATALOG_MSGS:
        msgs_to_kill += CATALOG_MSGS.pop(chat_id)
    if user_id in CATALOG_MSGS: # Parfois stocké par user_id
        msgs_to_kill += CATALOG_MSGS.pop(user_id)
    
    if chat_id in CCS_CATALOG_MSGS:
        msgs_to_kill += CCS_CATALOG_MSGS.pop(chat_id)
    if user_id in CCS_CATALOG_MSGS:
        msgs_to_kill += CCS_CATALOG_MSGS.pop(user_id)

    # 3. Exécution de la suppression
    for mid in set(msgs_to_kill):
        try: 
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except: 
            pass # Message déjà supprimé ou trop vieux
    
    # 4. Nettoyage final des données temporaires
    context.user_data.clear()

    # 5. Affichage du Menu Principal
    # clear=True demande à show_main_menu de supprimer aussi les anciens messages 'bot_messages'
    await show_main_menu(user_id, clear=True) 

    return ConversationHandler.END

async def animate_wait_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, batch_id: str, lang: str):
    """
    Anime un message "Décryptage en cours..." avec un timeout de 5 minutes et votre message personnalisé.
    """
    i = 0
    # Timeout réglé à 5 minutes (150 tours * 2 secondes = 300s) pour éviter l'apparition prématurée
    timeout_limit = 150
    
    # Laisse le temps au batch d'être créé
    await asyncio.sleep(1) 
    br = batch_runs.get(batch_id)
    if not br: return

    try:
        # Boucle tant que le batch n'est pas fini
        while not br.get("notified", False) and i < timeout_limit:
            dots = "." * (i % 3 + 1)
            base_text = msg(chat_id, 'decrytage_en_cours').replace('…','')
            text = f"{base_text}{dots} ({br.get('resolved', 0)}/{br.get('total', '?')})"
            
            try:
                await context.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id)
            except: pass # Si le message ne change pas, on ignore
            
            i += 1
            await asyncio.sleep(2)
            
            br = batch_runs.get(batch_id)
            if not br: break

        # === GESTION DU TIMEOUT (Si ça dépasse 5 min) ===
        if i >= timeout_limit and not br.get("notified", False):
            br["notified"] = True
            
            # Votre message personnalisé
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    "❌ **Échec du décryptage.**\n\n"
                    "Nous n'avons pas pu décrypter le permis.\n"
                    "La demande sera envoyée à un administrateur pour révision.\n\n"
                    "👉 Veuillez essayer un nouveau permis.\n"
                    "⚠️ Si l'erreur persiste, veuillez créer un ticket."
                ),
                parse_mode="Markdown"
            )
            return

    except Exception as e:
        logger.error(f"Erreur animate_wait_message: {e}")
    finally:
        # Si tout s'est bien passé (batch fini avant le timeout), on supprime le message d'attente
        if br and br.get("notified", True) and i < timeout_limit:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except: pass

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
    user_id = str(update.effective_user.id)
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    
    # 1. On cherche un ticket ACTIF (Ouvert ou Répondu)
    cur.execute("SELECT ticket_id, status FROM support_tickets WHERE user_id=? AND status IN ('open', 'replied') ORDER BY ticket_id DESC LIMIT 1", (user_id,))
    active_ticket = cur.fetchone()
    con.close()

    kb = []
    intro_text = "🆘 **CENTRE DE SUPPORT**\n\nComment pouvons-nous vous aider ?"

    # --- LOGIQUE UX "ENTRÉE" ---
    if not active_ticket:
        # Pas de ticket -> On propose d'en créer un
        kb.append([InlineKeyboardButton("🆕 Ouvrir un Ticket", callback_data="ticket_create_start")])
    else:
        # Ticket existant -> On propose de RENTRER DEDANS
        tid = active_ticket[0]
        status = active_ticket[1]
        
        # 👇 CHANGEMENT DU TEXTE DU BOUTON ICI 👇
        # Affiche : "🔙 Ticket #5 | 8417766973"
        btn_text = f"🔙 Ticket #{tid} | {user_id}"
        
        if status == 'replied':
            btn_text = f"🔴 RÉPONSE REÇUE (#{tid})"
            
        kb.append([InlineKeyboardButton(btn_text, callback_data=f"ticket_resume:{tid}")])
        
        intro_text += f"\n\n⏳ **Ticket en cours : #{tid}**\n_Cliquez ci-dessus pour entrer dans le chat._"
    # ---------------------------

    kb.append([InlineKeyboardButton("⬅️ Retour Menu", callback_data="menu_accueil")])
    
    await replace_view(q, intro_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def callback_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = get_user_lang(str(update.effective_user.id))
    
    if lang == "fr":
        txt = (
            "📚 FAQ (Tarifs USD)\n\n"
            "💵 Système de paliers :\n"
            "• Bronze : 7.00 USD / permis\n"
            "• Silver : 5.50 USD / permis (après 175$ rechargés)\n"
            "• Gold : 4.25 USD / permis (après 350$)\n"
            "• Platinum : 2.80 USD / permis (après 700$)\n"
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
            "📚 FAQ (USD Pricing)\n\n"
            "💵 Tier system:\n"
            "• Bronze: 7.00 USD / license\n"
            "• Silver: 5.50 USD / license (after $175 recharged)\n"
            "• Gold: 4.25 USD / license (after $350)\n"
            "• Platinum: 2.80 USD / license (after $700)\n"
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
        [InlineKeyboardButton("🆔 IDs & Physical", callback_data="hist:ids")],
        [InlineKeyboardButton("👥 Pro's",  callback_data="hist:pros"),
         InlineKeyboardButton("🚗 Permis", callback_data="hist:permis")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
    ]
    await replace_view(q, "📜 Choisissez une section :", reply_markup=InlineKeyboardMarkup(kb))

async def show_ids_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id

    try:
        # 1. On tente de récupérer les données
        con = sqlite3.connect(DB_NAME)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        
        # On vérifie d'abord si la table id_physical_submissions existe
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='id_physical_submissions'")
        exists = cur.fetchone()
        
        rows = []
        if exists:
            cur.execute("""
                SELECT type_document, first_name, last_name, status, created_at 
                FROM id_physical_submissions 
                WHERE user_id = ? 
                ORDER BY id DESC LIMIT 5
            """, (user_id,))
            rows = cur.fetchall()
        else:
            # Si cette table n'existe pas, on cherche dans support_tickets (utilisé dans tickets.py)
            cur.execute("""
                SELECT category as type_document, 'Formulaire' as first_name, '' as last_name, status, created_at 
                FROM support_tickets 
                WHERE user_id = ? AND (category LIKE '%ID%' OR category LIKE '%PHYSICAL%')
                ORDER BY ticket_id DESC LIMIT 5
            """, (user_id,))
            rows = cur.fetchall()
        con.close()

        # 2. NETTOYAGE : On supprime le menu SEULEMENT si on a réussi la requête
        try: await q.message.delete()
        except: pass

        sent_ids = []
        if not rows:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="hist:view")]])
            m = await context.bot.send_message(chat_id, "📑 **Aucun historique d'ID trouvé.**", reply_markup=kb, parse_mode="Markdown")
            sent_ids.append(m.message_id)
        else:
            for r in rows:
                status_emoji = "⏳" if r['status'] in ['pending', 'open'] else "✅" if r['status'] in ['completed', 'closed'] else "❌"
                txt = (
                    f"📑 **FORMULAIRE ID**\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📂 **TYPE** : `{r['type_document']}`\n"
                    f"👤 **NOM** : `{r['first_name']} {r['last_name']}`\n"
                    f"📊 **STATUT** : {status_emoji} `{r['status'].upper()}`\n"
                    f"📅 **DATE** : `{r['created_at']}`"
                )
                m = await context.bot.send_message(chat_id, txt, parse_mode="Markdown")
                sent_ids.append(m.message_id)

            kb_fin = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="hist:view")]])
            m_fin = await context.bot.send_message(chat_id, "🔻 **Fin de l'historique** 🔻", reply_markup=kb_fin)
            sent_ids.append(m_fin.message_id)

        context.user_data["hist_msgs"] = sent_ids

    except Exception as e:
        # SI CA CRASH, LE BOT TE LE DIT ICI
        print(f"DEBUG ID HIST: {e}")
        await q.answer(f"⚠️ Erreur technique : {str(e)[:50]}", show_alert=True)

async def hist_pros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche l'historique des produits achetés, avec pagination ET FILTRE."""
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

async def show_permis_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche les derniers permis générés avec un style UX (une bulle par item)."""
    q = update.callback_query
    await q.answer()
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id

    # 1. Nettoyage de l'écran (On efface le menu historique)
    try:
        await q.message.delete()
    except:
        pass

    # 2. Récupération des données en DB
    con = sqlite3.connect(DB_NAME)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    
    # On récupère les 5 dernières vérifications réussies
    cur.execute("""
        SELECT fullname, permis, status, created_at 
        FROM verifications 
        WHERE user_id = ? AND status = 'valide'
        ORDER BY id DESC LIMIT 5
    """, (user_id,))
    
    rows = cur.fetchall()
    con.close()

    if not rows:
        kb_vide = [[InlineKeyboardButton("⬅️ Retour", callback_data="hist:view")]]
        return await context.bot.send_message(
            chat_id=chat_id, 
            text="🚗 **Aucun permis trouvé dans votre historique.**", 
            reply_markup=InlineKeyboardMarkup(kb_vide),
            parse_mode="Markdown"
        )

    # 3. Envoi des fiches (Style UX)
    sent_ids = []
    for r in rows:
        txt = (
            f"🚗 **PERMIS GÉNÉRÉ**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 **NOM** : `{r['fullname']}`\n"
            f"🆔 **NUMÉRO** : `{r['permis']}`\n"
            f"📅 **DATE** : `{r['created_at']}`\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        # On peut ajouter un bouton pour copier le numéro facilement
        m = await context.bot.send_message(chat_id=chat_id, text=txt, parse_mode="Markdown")
        sent_ids.append(m.message_id)

    # 4. Bulle de navigation finale
    kb_fin = [[InlineKeyboardButton("⬅️ Retour à l'Historique", callback_data="hist:view")]]
    m_fin = await context.bot.send_message(
        chat_id=chat_id, 
        text="🔻 **Fin de votre historique Permis** 🔻", 
        reply_markup=InlineKeyboardMarkup(kb_fin)
    )
    sent_ids.append(m_fin.message_id)

    # On stocke les IDs pour que le bouton "Retour" puisse tout effacer d'un coup
    context.user_data["hist_msgs"] = sent_ids

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

    # On essaie de supprimer le message de choix de filtre
    try:
        await q.message.delete()
    except Exception:
        pass

    # On nettoie la mémoire
    context.user_data.pop('history_filter_type', None)

    # Relance l'historique normal
    # On modifie manuellement les data pour simuler un retour à la page 0
    q.data = "hist:pros:page:0" 
    
    # Appel de la fonction d'historique
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
        # --- 👇 CORRECTIF ANTI-CRASH 👇 ---
        # On récupère le nom (soit de la DB, soit de Telegram)
        raw_name = row[1] or user.first_name or "Utilisateur"
        # On échappe les caractères spéciaux pour le Markdown
        safe_name = str(raw_name).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
        # ----------------------------------

        # On stocke le message ID pour le supprimer plus tard
        msg = await update.message.reply_text(
            f"🔒 **TERMINAL VERROUILLÉ**\n"
            f"Utilisateur : {safe_name}\n\n"
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
    """
    Gestion du clavier PIN avec affichage fluide.
    """
    q = update.callback_query
    # On répond immédiatement pour la fluidité tactile
    try: await q.answer() 
    except: pass
    
    data = q.data
    user_id = str(update.effective_user.id)
    
    # Récupère le PIN actuel (ou vide si rien)
    current_input = context.user_data.get('temp_pin_input', "")

    # === CAS 1 : L'utilisateur tape un CHIFFRE ===
    if data.startswith("pin_") and data[4:].isdigit():
        digit = data.split("_")[1]
        
        # Limite de sécurité à 20 chiffres pour éviter les abus
        if len(current_input) < 20: 
            current_input += digit
            context.user_data['temp_pin_input'] = current_input
            
            # --- Construction du masque visuel ---
            nb = len(current_input)
            # Si moins de 4, on complète avec des ronds vides pour garder l'alignement
            if nb < 4:
                mask = "⚫" * nb + "◯" * (4 - nb)
            else:
                mask = "⚫" * nb
            
            try:
                await q.edit_message_text(
                    f"🔒 **TERMINAL VERROUILLÉ**\nPIN : {mask}",
                    reply_markup=get_pin_keyboard(),
                    parse_mode="Markdown"
                )
            except Exception:
                # Si Telegram bloque car ça va trop vite, on ignore (le prochain clic mettra à jour)
                pass

    # === CAS 2 : BOUTON EFFACER (DEL) ===
    elif data == "pin_del":
        if len(current_input) > 0:
            current_input = current_input[:-1]
            context.user_data['temp_pin_input'] = current_input
            
            nb = len(current_input)
            if nb < 4:
                mask = "⚫" * nb + "◯" * (4 - nb)
            else:
                mask = "⚫" * nb
                
            try:
                await q.edit_message_text(
                    f"🔒 **TERMINAL VERROUILLÉ**\nPIN : {mask}",
                    reply_markup=get_pin_keyboard(),
                    parse_mode="Markdown"
                )
            except: pass

    # === CAS 3 : BOUTON VALIDER (OK) ===
    elif data == "pin_enter":
        con = sqlite3.connect(DB_NAME)
        cur = con.cursor()
        cur.execute("SELECT pin_code FROM users WHERE telegram_id=?", (user_id,))
        row = cur.fetchone()
        con.close()
        
        # Vérification du code
        if row and row[0] == current_input:
            # ✅ CODE BON
            context.user_data['is_locked'] = False # Déverrouillage
            context.user_data['temp_pin_input'] = "" # Reset
            
            # Supprime le clavier PIN
            try: await q.message.delete()
            except: 
                try: await q.edit_message_text("✅")
                except: pass
            
            # Affiche le menu principal
            await show_main_menu(update.effective_user.id, clear=True)
            return ConversationHandler.END
        else:
            # ❌ CODE FAUX
            context.user_data['temp_pin_input'] = "" # On vide le champ
            try:
                await q.edit_message_text(
                    "⛔ **CODE FAUX !** Réessayez.\nPIN : `◯◯◯◯`",
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
    
    user_id = update.effective_user.id
    lang = get_user_lang(str(user_id))
    
   
    kb = [
        # Ligne 1
        [InlineKeyboardButton("🚗 Vérifier Permis" if lang == "fr" else "🚗 Check License ", callback_data="start_verifier_main")],
        
        # Ligne 2 (HLR seul pour voir tout le texte)
        [InlineKeyboardButton("📡 Carrier Lookup ($0.50)", callback_data="tool_hlr")],
        
        # Ligne 3 (LuxChecker seul)
        [InlineKeyboardButton("💳 LuxChecker ($1.00)", callback_data="tool_cc_checker")],
        
        # Ligne 4
        [InlineKeyboardButton("📱 Verification SMS", callback_data="tool_5sim")],
        
        # Ligne 5 (Retour)
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
    "google":       {"price": 2.00, "label": "Google / Gmail", "5sim_product": "google"},
    "creditkarma":  {"price": 2.50, "label": "Credit Karma",   "5sim_product": "creditkarma"},
    "paypal":       {"price": 2.25, "label": "PayPal",         "5sim_product": "paypal"},
    "uber":         {"price": 2.00, "label": "Uber",           "5sim_product": "uber"}
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
        "🇺🇸 **SMS ACTIVATIONS **\n"
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
    price = get_final_price(user_id, item['price'])
    
    # 1. Solde
    if get_user_balance(user_id) < price:
        await q.message.reply_text("❌ Solde insuffisant.")
        return SELECT_TOOL
        
    # --- CORRECTION ICI : ON SUPPRIME L'ANCIEN MESSAGE POUR ÉVITER L'ACCUMULATION ---
    try:
        await q.message.delete()
    except:
        pass
    # -------------------------------------------------------------------------------

    # 2. Achat (On utilise send_message car on a supprimé l'ancien)
    msg = await context.bot.send_message(chat_id=q.message.chat_id, text=f"🇺🇸 Recherche numéro **{item['label']}** (Virtual51)...")
    
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
            return 

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
                             f"💰 Coût final : {price}$\n\n"
                             f"⚠️ _Ce message s'autodétruira dans 2 minutes._",
                        parse_mode="Markdown"
                    )
                    # Finir la commande 5sim
                    headers = {"Authorization": "Bearer " + SIM_API_KEY}
                    requests.get(f"https://5sim.net/v1/user/finish/{order_id}", headers=headers)
                    
                    # --- CORRECTION ICI : PAUSE DE 2 MIN PUIS SUPPRESSION ---
                    await asyncio.sleep(120) # Attendre 2 minutes (120 secondes)
                    
                    # Supprimer le message du code
                    try: 
                        await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
                    except: 
                        pass
                    
                    # Retour au menu principal
                    await show_main_menu(int(user_id), clear=True)
                    # --------------------------------------------------------
                    
                except: pass
                return

        # Mise à jour visuelle (reste inchangé)
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
                    reply_markup=kb, 
                    parse_mode="Markdown"
                )
            except: pass
        
        await asyncio.sleep(5)
        
    # TIMEOUT (Reste inchangé)
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
    # Log pour le debug
    print(f"DEBUG: start_verifier_main appelé par {update.effective_user.id}")
    
    q = update.callback_query
    await q.answer()
    
    user_id = update.effective_user.id
    
    # Nettoyage complet de la session pour éviter les conflits
    reset_session(user_id)
    context.user_data.clear()
    
    # On envoie le message de départ
    # On utilise edit_message_text si possible pour fluidifier
    try:
        msgx = await q.edit_message_text(
            text=msg(user_id, "enter_bulk_qty"),
            reply_markup=kb_back_cancel()
        )
    except:
        msgx = await q.message.reply_text(
            text=msg(user_id, "enter_bulk_qty"),
            reply_markup=kb_back_cancel()
        )
    
    # On sauvegarde le message pour le nettoyage futur
    context.user_data['verif_flow_msg_ids'] = [msgx.message_id]
    
    # POINT CRITIQUE : On retourne l'état de départ de la vérification
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
    if 'currency' in cols: fields.append('currency'); values.append('USD')
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
                if 'currency' in cols: fields.append('currency'); values.append('USD')
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
                if 'currency' in cols: fields.append('currency'); values.append('USD')
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
    """
    1. Affiche le profil de l'utilisateur.
    Gère la sécurité de session et réinitialise les modes d'édition.
    """
    # 1. Guard Admin
    if str(update.effective_user.id) not in ADMIN_IDS: return

    # 2. SÉCURITÉ : On désactive le mode "Édition de Solde" si on revient ici
    context.user_data["SOLDE_EDIT_MODE"] = False 

    target_id = None
    q = None
    
    # 3. Récupération de l'ID cible
    # Cas A : Clic sur un bouton (Update standard)
    if hasattr(update, 'callback_query') and update.callback_query:
        q = update.callback_query
        try: await q.answer()
        except: pass
        if "admin_adjust_" in q.data:
            target_id = q.data.replace("admin_adjust_", "")
    
    # Cas B : Appel interne
    if not target_id:
        target_id = context.user_data.get('target_user')

    # Cas C : Fallback
    if not target_id:
        target_id = context.user_data.get('SOLDE_TARGET_ID')

    if not target_id:
        try: await update.effective_message.reply_text("❌ Erreur : ID utilisateur perdu.")
        except: pass
        return

    # 4. Sauvegarde BLINDÉE pour la suite
    context.user_data["target_user"] = target_id
    context.user_data["SOLDE_TARGET_ID"] = target_id 

    # 5. Récupération DB
    con = sqlite3.connect(DB_NAME)
    row = con.execute("SELECT username, balance FROM users WHERE telegram_id=?", (target_id,)).fetchone()
    con.close()
    
    username = row[0] if row else "Inconnu"
    balance = row[1] if row else 0.0

    # --- 👇 CORRECTIF ANTI-CRASH : Échappement des caractères Markdown 👇 ---
    # On ajoute des backslashs devant _ et * pour que Telegram ne les interprète pas comme du formatage
    safe_username = str(username).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`") if username else "Inconnu"
    # -----------------------------------------------------------------------

    # 6. Construction Interface
    txt = (
        f"👤 **GESTION UTILISATEUR**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID : `{target_id}`\n"
        f"📛 Nom : @{safe_username}\n"  # <--- On utilise safe_username ici
        f"💰 Solde actuel : **{balance:.2f} $**\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    kb = [
        [InlineKeyboardButton("✍️ Modifier le Solde (+/-)", callback_data=f"admin_customamount_{target_id}")],
        [InlineKeyboardButton("🗑 Supprimer l'utilisateur", callback_data=f"admin_deluser_ask_{target_id}")],
        [InlineKeyboardButton("🔙 Retour Liste", callback_data="admin_users")]
    ]
    
    # 7. Affichage intelligent (Edit ou Send)
    try:
        if q:
            await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except:
        # Fallback ultime : si le Markdown plante encore (ex: caractères bizarres), on envoie en texte brut
        try:
            clean_txt = txt.replace("*", "").replace("`", "").replace("_", "")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=clean_txt, reply_markup=InlineKeyboardMarkup(kb))
        except: 
            pass


async def admin_customamount_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # Extraction de l'ID depuis le bouton (admin_customamount_12345)
    target_id = q.data.split("_")[-1]
    
    # Sécurisation des variables
    context.user_data["SOLDE_TARGET_ID"] = target_id
    context.user_data["target_user"] = target_id 
    context.user_data["SOLDE_EDIT_MODE"] = True   

    # On supprime l'ancien message pour éviter les bugs d'affichage
    try: await q.message.delete()
    except: pass

    kb = [[InlineKeyboardButton("🔙 Annuler", callback_data=f"admin_adjust_{target_id}")]]

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✍️ **MODIFICATION SOLDE**\n"
             f"━━━━━━━━━━━━━━━━━━\n"
             f"👤 Cible : `{target_id}`\n\n"
             f"Veuillez entrer le montant (ex: `50` ou `-20`) :",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    
    context.user_data['prompt_msg_id'] = msg.message_id
    
    # 🔥 IMPORTANT : On renvoie "1" pour activer le filtre Regex dans admin_search_conv
    return 1


async def admin_customamount_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime
    import asyncio

    # --- 🛡️ CORRECTIF SENTRY ---
    # Si context.user_data est None (ex: message de canal), on arrête tout de suite.
    if context.user_data is None:
        return
    # ---------------------------

    # 1. Vérification : Est-ce qu'on est en train de modifier un solde ?
    if not context.user_data.get("SOLDE_EDIT_MODE"):
        return # Si non, on ignore ce message, ce n'est pas pour nous.

    user = update.effective_user
    text = update.message.text.strip()
    
    # 2. Récupération ID BLINDÉE
    target_id = context.user_data.get("SOLDE_TARGET_ID")
    
    # Sécurité ultime : Si vide, on tente le fallback
    if not target_id:
        target_id = context.user_data.get("target_user")

    if not target_id:
        await update.message.reply_text("❌ Erreur Mémoire : ID perdu. Recliquez sur le bouton Modifier.")
        context.user_data["SOLDE_EDIT_MODE"] = False # On désactive pour pas bloquer
        return

    # 3. Traitement
    try:
        amount = float(text.replace(',', '.'))
    except ValueError:
        msg_err = await update.message.reply_text("❌ Chiffre invalide.")
        await asyncio.sleep(2)
        try: 
            await update.message.delete()
            await msg_err.delete()
        except: pass
        return # On reste en mode édition tant qu'il n'y a pas de chiffre valide

    # 4. Mise à jour DB
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    
    cur.execute("SELECT balance FROM users WHERE telegram_id=?", (target_id,))
    row = cur.fetchone()
    old_bal = row[0] if row else 0.0
    new_bal = old_bal + amount
    sign = "+" if amount >= 0 else ""

    cur.execute("UPDATE users SET balance=? WHERE telegram_id=?", (new_bal, target_id))
    try:
        cur.execute("INSERT INTO transactions (user_id, amount, status, date) VALUES (?, ?, ?, ?)",
                    (target_id, amount, 'completed', datetime.now()))
    except: pass 
    con.commit()
    con.close()

    # 5. Feedback
    msg_conf = await update.message.reply_text(
        f"✅ **Succès !**\n💰 Action : {sign}{amount}€\n🏦 Nouveau : **{new_bal:.2f}€**",
        parse_mode="Markdown"
    )
    
    # 6. DÉSACTIVATION DU MODE ÉDITION (Important !)
    context.user_data["SOLDE_EDIT_MODE"] = False
    
    # 7. Nettoyage
    await asyncio.sleep(2.5)
    try:
        await update.message.delete()
        await msg_conf.delete()
        prompt_id = context.user_data.get('prompt_msg_id')
        if prompt_id:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_id)
    except: pass

    # 8. Retour Profil
    # On remet target_user pour admin_adjust_user qui l'attend
    context.user_data["target_user"] = target_id 
    await admin_adjust_user(update, context)

    return ConversationHandler.END


async def admin_setstatut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Guard admin
    if str(update.effective_user.id) not in ADMIN_IDS:
        return
    
    q = update.callback_query
    await q.answer()

    # Gestion pagination
    page = 0
    if ":page:" in q.data:
        try: page = int(q.data.split(":page:")[1])
        except: page = 0

    # Récupération des données (10 par page)
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    
    # Compte total
    cur.execute("SELECT COUNT(*) FROM users")
    total_count = cur.fetchone()[0]
    total_pages = max(1, (total_count + 9) // 10)
    page = max(0, min(page, total_pages - 1))
    offset = page * 10

    # Liste avec Username
    cur.execute("""
        SELECT telegram_id, username, forfait 
        FROM users 
        ORDER BY rowid DESC 
        LIMIT 10 OFFSET ?
    """, (offset,))
    rows = cur.fetchall()
    con.close()

    if not rows:
        await q.edit_message_text("Aucun utilisateur.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="admin_menu")]]))
        return

    # Construction du clavier
    kb = []
    for uid, uname, tier in rows:
        # Affichage propre : ID + @User + (Statut actuel)
        label_tier = FORFAITS.get(tier, {}).get('label', tier)
        # On raccourcit le username s'il est trop long
        display_name = f"@{uname}" if uname else "Inconnu"
        if len(display_name) > 10: display_name = display_name[:10] + ".."
        
        btn_txt = f"{uid} | {display_name} | {label_tier}"
        kb.append([InlineKeyboardButton(btn_txt, callback_data=f"admin_userstatut_{uid}")])

    # Navigation
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Préc.", callback_data=f"admin_setstatut:page:{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Suiv. ➡️", callback_data=f"admin_setstatut:page:{page+1}"))
    if nav_row: kb.append(nav_row)

    # Boutons d'actions
    kb.append([InlineKeyboardButton("🔍 Chercher un ID pour Statut", callback_data="admin_search_statut_start")])
    kb.append([InlineKeyboardButton("⬅️ Retour Menu Admin", callback_data="admin_menu")])

    await q.edit_message_text(
        f"🏷 **GESTION DES FORFAITS**\nPage {page+1}/{total_pages}\n\nSélectionnez un utilisateur pour changer son rang :", 
        reply_markup=InlineKeyboardMarkup(kb), 
        parse_mode="Markdown"
    )

async def admin_userstatut(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id=None):
    # Guard admin
    if str(update.effective_user.id) not in ADMIN_IDS:
        return
    
    q = update.callback_query
    await q.answer()

    # --- LOGIQUE D'EXTRACTION DE L'ID ---
    # Si target_id n'est pas fourni, on le prend dans q.data (clic normal)
    # Si target_id est fourni, on ignore q.data (appel forcé après modif)
    if target_id is None:
        target_id = q.data.replace("admin_userstatut_", "")
    
    # On récupère les infos
    con = sqlite3.connect(DB_NAME)
    # Utilisation de Row pour plus de sécurité sur les colonnes
    row = con.execute("SELECT username, forfait, balance FROM users WHERE telegram_id=?", (target_id,)).fetchone()
    con.close()
    
    if not row:
        await q.message.reply_text("❌ Utilisateur introuvable.")
        return

    uname, current_tier, bal = row
    # Gestion du label actuel (fallback sur le tier si non trouvé)
    current_label = FORFAITS.get(current_tier, {}).get('label', current_tier)

    # Menu de choix propre
    kb = [
        [InlineKeyboardButton(f"🥉 {FORFAITS['bronze']['label']}",   callback_data=f"admin_statut_{target_id}_bronze")],
        [InlineKeyboardButton(f"🥈 {FORFAITS['silver']['label']}",   callback_data=f"admin_statut_{target_id}_silver")],
        [InlineKeyboardButton(f"🥇 {FORFAITS['gold']['label']}",     callback_data=f"admin_statut_{target_id}_gold")],
        [InlineKeyboardButton(f"💎 {FORFAITS['platinum']['label']}", callback_data=f"admin_statut_{target_id}_platinum")],
        [InlineKeyboardButton("⬅️ Retour Liste", callback_data="admin_setstatut")]
    ]

    # Mise à jour du message
    await q.edit_message_text(
        f"🏷 **MODIFIER LE STATUT**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 User : `{target_id}`\n"
        f"📛 Nom : @{uname or 'Inconnu'}\n"
        f"💰 Solde : {bal:.2f} $\n"
        f"🏆 Actuel : **{current_label}**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Sélectionnez le nouveau rang :",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )


async def admin_setstatut_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    # Pas de await q.answer() ici car on va éditer le message tout de suite
    
    data = q.data.split("_")
    target_id = data[2]
    new_statut = data[3]
    
    # Mise à jour DB
    set_user_statut(target_id, new_statut)
    
    # Notification Toast (message temporaire en haut de l'écran)
    await q.answer(f"✅ Statut changé pour {new_statut.upper()} !", show_alert=False)
    
    # On modifie le q.data pour simuler un rappel de la fonction d'affichage du profil
    # Cela va rafraîchir la page et montrer le nouveau statut instantanément
    await admin_userstatut(update, context, target_id=target_id)

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
    if str(update.effective_user.id) not in ADMIN_IDS:
        return

    q = update.callback_query
    try: await q.answer()
    except: pass

    # --- VÉRIFICATION NOTIFICATIONS ADMIN ---
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT count(*) FROM support_tickets WHERE status='open'")
    open_count = cur.fetchone()[0]
    con.close()
    
    label_tickets = f"📨 Gestion Tickets (🔴 {open_count})" if open_count > 0 else "📨 Gestion Tickets"

    keyboard = [
        [InlineKeyboardButton(label_tickets, callback_data="admin_tickets_list")],
        [InlineKeyboardButton("👥 Utilisateurs", callback_data="admin_users")],
        [InlineKeyboardButton("🏷 Forfait utilisateur", callback_data="admin_setstatut")],
        [InlineKeyboardButton("🔁 Redémarrer le bot", callback_data="admin_hard_reboot")],
        [InlineKeyboardButton("🪪 ID's Orders", callback_data="admin_all_orders")],
        [InlineKeyboardButton("💳 Produits Cc's", callback_data="admin_cat_menu:ccs")],
        [InlineKeyboardButton("🧱 Produits Pro's", callback_data="admin_cat_menu:propro")],
        [InlineKeyboardButton("⏱️ Réglages Temps IVR", callback_data="admin_ivr_settings")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")],
    ]

    try: await q.message.edit_text("⚙️ Menu admin :", reply_markup=InlineKeyboardMarkup(keyboard))
    except: await q.message.reply_text("⚙️ Menu admin :", reply_markup=InlineKeyboardMarkup(keyboard))

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
    # Semaine : Lundi, Mardi, Jeudi, Vendredi
    if weekday in [0, 1, 3, 4]:
        return (now.hour > 8 or (now.hour == 8 and now.minute >= 30)) and (now.hour < 16 or (now.hour == 16 and now.minute < 30))
    # Mercredi
    elif weekday == 2:
        return (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and (now.hour < 16 or (now.hour == 16 and now.minute < 30))
    
    # Samedi (5) et Dimanche (6) -> Retourne False pour activer le menu de fin de semaine (1-1)
    return False

async def launch_parallel_calls(base_code, user_id, num_calls=10, fullname="", formatted="", batch_id=None):
    if batch_id is None:
        batch_id = f"{user_id}:{base_code}"

    # 1. Initialisation de l'état en mémoire
    key = f"{batch_id}:{base_code}"
    user_validation_status[key] = {
        "notified": False, 
        "total": 0, 
        "fullname": fullname,
        "formatted": formatted, 
        "resolved": False, 
        "batch_id": batch_id
    }

    # --- 🛡️ PROTECTION ANTI-DOUBLON ---
    # On vérifie si ce permis exact (Base + Nom) est déjà marqué 'valide'
    try:
        con = sqlite3.connect(DB_NAME)
        # On cherche un permis qui commence par la même base et pour le même nom
        existing = con.execute("""
            SELECT permis FROM verifications 
            WHERE fullname = ? AND status = 'valide' AND permis LIKE ?
            LIMIT 1
        """, (fullname, f"{base_code}%")).fetchone()
        con.close()

        if existing:
            found_permis = existing[0]
            log(f"✅ [SKIP] Permis déjà en base pour {fullname} : {found_permis}", user_id)
            
            # On marque comme notifié pour arrêter toute exécution
            user_validation_status[key]["notified"] = True
            
            # Message flash pour le client
            if globals().get("app_telegram") and getattr(app_telegram, "bot", None):
                await app_telegram.bot.send_message(
                    chat_id=int(user_id), 
                    text=f"♻️ **Permis déjà validé !**\n\nNous avons retrouvé le numéro dans vos archives :\n`{found_permis}`\n\n(Aucun crédit n'a été utilisé)."
                )
            
            # Mise à jour de l'animation du batch (pour que l'UI passe à 1/1)
            br = batch_runs.get(batch_id)
            if br:
                async with br.setdefault("lock", asyncio.Lock()):
                    br["resolved"] += 1
                    if br["resolved"] >= br["total"]:
                        br["notified"] = True
            return 
    except Exception as e:
        log(f"⚠️ Erreur check doublon: {e}", user_id, "warning")
    # --- FIN DE LA PROTECTION ---

    log(f"🚀 [SIGNALWIRE] Déclenchement séquence (Base: {base_code}) - Délai: 6s", user_id)

    # 2. Boucle séquentielle des appels
    for i in range(num_calls):
        # Vérification si un appel précédent a déjà réussi
        if user_validation_status[key].get("notified"):
            log(f"🛑 Code trouvé ! Arrêt de la séquence pour {fullname}.", user_id)
            break

        # Lancement d'un appel unique via l'ouvrier
        await _launch_single_call(base_code, i, user_id, batch_id)

        # Délai de 6 secondes entre chaque variante pour rester "propre"
        if i < num_calls - 1: # Pas besoin de dormir après le dernier appel
            log(f"⏳ Pause 6s avant la variante {i+1}...", user_id)
            await asyncio.sleep(6)

    log(f"🏁 Séquence de batch terminée pour {batch_id}", user_id)

async def _launch_single_call(base_code, i, user_id, batch_id):
    """Lance UN SEUL appel SignalWire sans faire crash le bot."""
    variant = f"{base_code}{i:02}"
    
    # Construction de l'URL proprement
    webhook_url = f"{SERVER_URL}/twilio_handler?code={variant}&uid={user_id}&bid={batch_id}"
    
    try:
        # On force l'exécution en arrière-plan pour éviter les blocages
        loop = asyncio.get_running_loop()
        
        # On utilise une fonction lambda pour passer les arguments à SignalWire
        def call_signalwire():
            return client.calls.create(
                to=DESTINATION_NUMBER,
                from_=SIGNALWIRE_NUMBER,
                url=webhook_url,
                record=False,
                machine_detection="DetectMessageEnd",
                machine_detection_timeout=5
            )

        # L'exécution réelle
        call = await loop.run_in_executor(None, call_signalwire)
        
        if call and call.sid:
            active_calls[call.sid] = {"user_id": user_id, "code": variant, "batch_id": batch_id}
            log(f"📞 Succès : Variante {variant} | SID: {call.sid}", user_id)
        
    except Exception as e:
        # On capture l'erreur mais on ne l'envoie pas plus loin (Anti-Crash)
        log(f"⚠️ SignalWire a refusé la variante {variant} : {e}", user_id, "error")

async def cancel_all_calls(batch_id: str | None = None, user_id: int | str | None = None):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return 0 # Pas de loop, pas d'appels

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

def convertir_en_code_saaq(code):
    """Convertit un code en format SAAQ."""
    # Mapping des lettres en chiffres pour la composition téléphonique
    mapping = {
        'A': '2', 'B': '2', 'C': '2',
        'D': '3', 'E': '3', 'F': '3',
        'G': '4', 'H': '4', 'I': '4',
        'J': '5', 'K': '5', 'L': '5',
        'M': '6', 'N': '6', 'O': '6',
        'P': '7', 'Q': '7', 'R': '7',
        'S': '7', 'T': '8', 'U': '8',
        'V': '8', 'W': '9', 'X': '9',
        'Y': '9', 'Z': '9'
    }
    result = ""
    for char in code.upper():
        if char.isdigit():
            result += char
        elif char in mapping:
            result += mapping[char]
    return result

# --- ÉTAPE 1 : ROUTEUR INITIAL ---
@app.route("/twilio_handler", methods=["GET", "POST"])
def twilio_handler():
    code = request.args.get("code", "")
    uid = request.args.get("uid")
    bid = request.args.get("bid")
    timings = get_ivr_timings()
    r = VoiceResponse()

    if is_system_open():
        # LOGIQUE SEMAINE (4-4-6) - Direct car éprouvé
        log(f"[IVR] Mode SEMAINE pour {code}", uid)
        r.pause(length=timings.get("open_1", 55))
        r.play(digits="4")
        r.pause(length=3)
        r.play(digits="4")
        r.pause(length=3)
        r.play(digits="6")
        r.pause(length=timings.get("open_2", 41))
        r.redirect(f"{SERVER_URL}/composer_code?code={code}&uid={uid}&bid={bid}", method="POST")
    else:
        # LOGIQUE FIN DE SEMAINE (1-1) - Structure par étapes (redirect) pour stabilité
        log(f"[IVR] Mode FIN DE SEMAINE (1-1) pour {code}", uid)
        r.pause(length=timings.get("closed_1", 40))
        r.redirect(f"{SERVER_URL}/step_2?code={code}&uid={uid}&bid={bid}", method="POST")
    
    return Response(str(r), mimetype="text/xml")

# --- ÉTAPE 2 : PREMIER '1' (DIMANCHE) ---
@app.route("/step_2", methods=["POST"])
def step_2():
    code = request.args.get("code", "")
    uid = request.args.get("uid")
    bid = request.args.get("bid")
    r = VoiceResponse()
    r.play(digits="1")
    r.pause(length=5) # Règle : attend 5 secondes
    r.redirect(f"{SERVER_URL}/step_3?code={code}&uid={uid}&bid={bid}", method="POST")
    return Response(str(r), mimetype="text/xml")

# --- ÉTAPE 3 : DEUXIÈME '1' + ATTENTE FINALE (DIMANCHE) ---
@app.route("/step_3", methods=["POST"])
def step_3():
    code = request.args.get("code", "")
    uid = request.args.get("uid")
    bid = request.args.get("bid")
    timings = get_ivr_timings()
    r = VoiceResponse()
    r.play(digits="1")
    # Pause pour atteindre les 1m25 total (env. 38s-40s)
    r.pause(length=timings.get("closed_2", 40)) 
    r.redirect(f"{SERVER_URL}/composer_code?code={code}&uid={uid}&bid={bid}", method="POST")
    return Response(str(r), mimetype="text/xml")

# --- ÉTAPE 4 : COMPOSITION SÉCURISÉE ---
@app.route("/composer_code", methods=["POST"], endpoint="composer_code_main")
def composer_code():
    code = request.args.get("code", "")
    uid = request.args.get("uid")
    bid = request.args.get("bid")
    code_saaq = convertir_en_code_saaq(code)
    r = VoiceResponse()
    
    # Composition sans micro ouvert (évite les bruits qui coupent la numérotation)
    for digit in code_saaq:
        r.play(digits=digit)
        r.pause(length=0.2)
    
    # Une fois terminé, redirection vers l'écoute
    r.redirect(f"{SERVER_URL}/listen_response?code={code}&uid={uid}&bid={bid}", method="POST")
    return Response(str(r), mimetype="text/xml")

# --- ÉTAPE 5 : OUVERTURE DU MICRO ---
@app.route("/listen_response", methods=["POST"])
def listen_response():
    r = VoiceResponse()
    gather = r.gather(
        input="speech",
        language="fr-CA", # Accent québécois pour la SAAQ
        timeout=30,
        speech_timeout="auto",
        action=f"{SERVER_URL}/analyze_response",
        method="POST"
    )
    # Failsafe en cas de silence
    r.redirect(f"{SERVER_URL}/analyze_response")
    return Response(str(r), mimetype="text/xml")

# --- ÉTAPE 6 : ANALYSE DES RÉSULTATS ---
@app.route("/analyze_response", methods=["POST"], endpoint="analyze_response_main")
def analyze_response():
    import re
    from twilio.twiml.voice_response import VoiceResponse

    speech_raw = request.form.get("SpeechResult", "")
    speech = (speech_raw or "").lower().strip()
    speech_clean = speech.replace('.', '').replace(',', '')
    call_sid = request.form.get("CallSid")
    
    log(f"📞 [analyze_response] CallSid: {call_sid} | Brut: {speech_raw}", "SYSTEM")

    if call_sid not in active_calls:
        return Response("<Response><Hangup/></Response>", mimetype="text/xml")

    current_call = active_calls[call_sid]
    # Récupération sécurisée de l'ID utilisateur
    try:
        user_id = int(current_call.get("user_id", 0))
    except:
        user_id = "SYSTEM"

    variant = current_call.get("code", "")
    batch_id = current_call.get("batch_id")
    base_code = variant[:-2]
    key = f"{batch_id}:{base_code}"
    
    # Si la clé n'existe pas dans le cache de validation, on coupe
    if key not in user_validation_status:
        return Response("<Response><Hangup/></Response>", mimetype="text/xml")

    state = user_validation_status[key]
    fullname = state.get("fullname", "")
    
    # Détection de succès
    WINNING_PHRASES = ["valide", "comprend la classe", "dossier est", "valid"]
    is_absolute_win = any(phrase in speech_clean for phrase in WINNING_PHRASES)

    # Détection d'échec
    neg_patterns = [r"invalide", r"pas valide", r"erreur", r"aucun", r"incomplet"]
    is_negative = any(re.search(p, speech_clean) for p in neg_patterns)
    
    valid = is_absolute_win or ("valide" in speech_clean and not is_negative)

    # Fonction de notification Telegram intégrée
    def _maybe_finish_batch(add_result_text=None):
        async def _notify_serialized():
            br = batch_runs.get(batch_id)
            if not br: return
            async with br.setdefault("lock", asyncio.Lock()):
                if not state["resolved"]:
                    state["resolved"] = True
                    br["resolved"] += 1 
                
                # Notification Telegram si texte fourni
                if add_result_text:
                    if "ORDER" not in str(batch_id):
                        if globals().get("app_telegram") and getattr(app_telegram, "bot", None):
                            try:
                                await app_telegram.bot.send_message(chat_id=user_id, text=add_result_text, parse_mode='Markdown')
                            except Exception as e:
                                log(f"Erreur envoi Telegram: {e}", user_id, "error")
                
                # Fermeture du batch si terminé
                if br["resolved"] >= br["total"] and not br["notified"]:
                    br["notified"] = True 
                    if "ORDER" in str(batch_id):
                        log(f"✅ [ORDER] Batch {batch_id} validé.", user_id)
                        return
                    
                    if globals().get("app_telegram") and getattr(app_telegram, "bot", None):
                        try:
                            await app_telegram.bot.send_message(chat_id=user_id, text="🔓 Fin du décryptage.")
                            # On réaffiche le menu principal
                            await show_main_menu(user_id)
                        except: pass
        
        if globals().get("bot_loop"):
            asyncio.run_coroutine_threadsafe(_notify_serialized(), bot_loop)

    r = VoiceResponse()

    # Logique de décision Finale
    if valid and not state["notified"]:
        state["notified"] = True
        real_suffix = variant[-2:]
        # Construction du permis final (AAAA-123456-XX)
        final_permis = f"{variant[:5]}-{variant[5:11]}-{real_suffix}"
        
        # Sauvegarde en DB
        save_permit_history(user_id, fullname, final_permis, "valide")
        
        # Notification Client
        _maybe_finish_batch(add_result_text=msg(user_id, "validation_ok", permis=final_permis, fullname=fullname))
        r.hangup()
        
    else:
        state["total"] += 1
        # Si on a épuisé les 10 tentatives sans succès
        if state["total"] >= 10 and not state["notified"]:
            state["notified"] = True
            save_permit_history(user_id, fullname, None, "aucun")
            
            msg_echec = (f"❌ *Recherche terminée - ÉCHEC*\n\n📂 Dossier : `{fullname}`\n"
                         f"⚠️ Résultat : *Aucun permis valide trouvé.*")
            _maybe_finish_batch(add_result_text=msg_echec)
            r.hangup()
            
        elif not state["notified"]:
            # On n'a pas encore trouvé, on redirige vers le prochain code (boucle gérée par le client SignalWire/Twilio habituellement via URL status callback, mais ici on simplifie en raccrochant pour que la boucle python lance le suivant)
            # NOTE: Dans ton architecture `launch_parallel_calls`, c'est la boucle Python qui lance les appels un par un.
            # Donc ici on raccroche simplement pour que `launch_parallel_calls` passe au `i+1`.
            r.hangup()
        else:
            r.hangup()

    # Nettoyage de l'appel actif
    active_calls.pop(call_sid, None)
    return Response(str(r), mimetype="text/xml")



# ========================== MENU/ROUTEUR CALLBACKS ==========================
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    
    # Réponse immédiate
    try: await q.answer()
    except: pass

    data = q.data
    user_id = update.effective_user.id
    print(f"[DBG] menu_handler triggered with data={data}", flush=True)

    # --- LOGIQUE DE RETOUR / ACCUEIL ---
    if data == "menu_accueil":
        context.user_data['cart_return_to'] = "menu_accueil"
        return await goto_menu(update, context)

    # --- SECTION HISTORIQUE (Navigation & Affichage) ---
    if data == "hist:view":
        return await hist_view_callback(update, context)

    # AJOUT ICI : Gestion des flèches de l'historique Pro's
    if data.startswith("hist:pros"):
        return await hist_pros(update, context)

    if data == "hist:permis":
        return await show_permis_history(update, context)

    if data == "hist:ids":
        return await show_ids_history(update, context)

    # --- SECTION BOUTIQUE (PROPRO) ---
    if data == "propro" or data == "cat:propro":
        context.user_data["prod_tier"] = None
        context.user_data['cart_return_to'] = "cat:propro"
        return await show_products(update, context, page=0, tier=None)

    if data.startswith("prod:page:"):
        page = int(data.split(":")[2])
        tier = context.user_data.get("prod_tier")
        return await show_products(update, context, page=page, tier=tier)

    # --- SECTION BOUTIQUE (CCS) ---
    if data.startswith("ccs:page:"):
        page = int(data.split(":")[2])
        tier = context.user_data.get("prod_tier")
        return await show_products_ccs(update, context, page=page, tier=tier)

    if data == "noop":
        return

async def hist_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
        if q: await q.answer()
        
        chat_id = update.effective_chat.id
        
        # --- NETTOYAGE UX ---
        # 1. On récupère et supprime toutes les fiches d'historique précédentes
        old_msgs = context.user_data.pop("hist_msgs", [])
        for mid in old_msgs:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except:
                pass # Message déjà supprimé ou introuvable
        
        # 2. Nettoyage du message de navigation/menu actuel (le triangle ou le bouton retour)
        try:
            await q.message.delete()
        except:
            pass

        # --- AFFICHAGE DU MENU ---
        # On appelle hist_menu qui va envoyer le menu "Choisissez une section :"
        # comme un nouveau message propre.
        await hist_menu(update, context) 
    
    except Exception as e:
        # Utilisation de ton système de log existant
        if hasattr(update.effective_user, 'id'):
            log(f"hist_view_callback error: {e}", str(update.effective_user.id), "error")

        # ==================================================================
# ================= MODULE ID/DOCS CENTER (INTÉGRÉ) ================

# 1. OUTILS GOOGLE & MAPPING & DATES
def validate_address_canada_post(raw_address):
    """Interroge l'API AddressComplete de Postes Canada."""
    key = os.environ.get("CANADA_POST_API_KEY")
    if not key: 
        print("⚠️ Pas de clé Postes Canada configurée.")
        return []

    # Endpoint AddressComplete Standard
    url = "https://ws1.postescanada-canadapost.ca/AddressComplete/Interactive/Find/v2.10/json3.ws"
    
    params = {
        "Key": key,
        "SearchTerm": raw_address,
        "Country": "CAN",
        "LanguagePreference": "fr",
        "MaxResults": 5
    }
    
    try:
        r = requests.get(url, params=params, timeout=5)
        data = r.json()
        
        results = []
        if "Items" in data:
            for item in data['Items']:
                # On combine Text (ex: 123 Rue A) et Description (ex: Montréal, QC, H1A 1A1)
                # Note: Parfois Description est vide si l'adresse est incomplète, on gère ça.
                text = item.get('Text', '')
                desc = item.get('Description', '')
                full = f"{text}, {desc}" if desc else text
                results.append(full)
                
        return results
    except Exception as e:
        print(f"[Canada Post Error] {e}")
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
    
    # --- CORRECTION COULEUR YEUX (MAPPING AAMVA) ---
    # On récupère la valeur brute, on met en majuscules et on enlève les espaces
    raw_eyes = str(data_dict.get('form_eyes', 'BRO')).upper().strip()

    # Dictionnaire de correspondance (Entrée possible -> Code Standard AAMVA)
    # Convertit les anciens codes (BRN), l'anglais (BROWN) et le français (BRUN)
    eye_map = {
        'BRN': 'BRO', 'BROWN': 'BRO', 'BRUN': 'BRO',
        'BLU': 'BLU', 'BLUE': 'BLU', 'BLEU': 'BLU',
        'HAZ': 'HZL', 'HAZEL': 'HZL',
        'GRN': 'GRN', 'GREEN': 'GRN', 'VERT': 'GRN',
        'GRY': 'GRY', 'GRAY': 'GRY', 'GREY': 'GRY', 'GRIS': 'GRY',
        'BLK': 'BLK', 'BLACK': 'BLK', 'NOIR': 'BLK',
        'PNK': 'PNK', 'PINK': 'PNK',
        'MAR': 'MAR', 'MAROON': 'MAR',
        'DIC': 'DIC', 'MULTI': 'DIC'
    }

    # Si le code est dans la liste, on le remplace. Sinon on garde l'original.
    final_eyes = eye_map.get(raw_eyes, raw_eyes)
    # -----------------------------------------------

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
        
        # 👇 UTILISATION DE LA VARIABLE CORRIGÉE ICI 👇
        "data[DAY]": final_eyes 
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


async def id_start_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initialise le produit et demande la quantité avant le formulaire."""
    q = update.callback_query
    await q.answer()
    
    # 1. On extrait l'ID du produit depuis le bouton (ex: id_buy:12)
    try:
        pid = int(q.data.split(":")[1])
    except (IndexError, ValueError):
        await q.message.reply_text("❌ Erreur de sélection du produit.")
        return ConversationHandler.END

    # 2. Récupération des infos en base de données
    con = sqlite3.connect(DB_NAME)
    row = con.execute("SELECT title, price, tier FROM products WHERE id=?", (pid,)).fetchone()
    con.close()
    
    if not row:
        await q.message.reply_text("❌ Produit introuvable.")
        return ConversationHandler.END
    
    # 3. Stockage dans la session utilisateur
    context.user_data['id_product'] = {
        'id': pid, 
        'name': row[0], 
        'price': row[1], 
        'code': row[2]
    }
    context.user_data['current_qty'] = 1 
    
    # 4. Nettoyage de la photo/menu précédent pour afficher proprement la sélection de quantité
    try:
        await q.message.delete()
    except:
        pass
    
    # 5. Appel de l'affichage de quantité (Assurez-vous que update_qty_display existe)
    await update_qty_display(update, context, new_message=True)
    return ID_ASK_QTY

async def update_qty_display(update: Update, context: ContextTypes.DEFAULT_TYPE, new_message=False):
    """Gère l'affichage visuel du sélecteur de quantité."""
    qty = context.user_data.get('current_qty', 1)
    prod = context.user_data.get('id_product', {'name': 'Produit', 'price': 0})
    total_price = prod['price'] * qty
    
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
    txt = f"🛒 **{prod['name']}**\n\n🔢 **Quantité : {qty}**\n💰 **Total : {total_price:.2f}$**\n\n_Utilisez les boutons ou écrivez un chiffre._"
    
    if new_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        try:
            await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        except:
            pass

async def id_handle_qty_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère les clics sur les boutons Plus/Moins/Confirmer."""
    q = update.callback_query
    data = q.data
    
    # Redirection si clic sur confirmer
    if data == "qty_confirm":
        return await id_save_qty(update, context)

    current = context.user_data.get('current_qty', 1)
    
    if data == "qty_add": current += 1
    elif data == "qty_sub": current = max(1, current - 1)
    elif data == "qty_add_5": current += 5
    elif data == "qty_add_10": current += 10
    
    context.user_data['current_qty'] = current
    await update_qty_display(update, context, new_message=False)
    return ID_ASK_QTY

async def id_save_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sauvegarde finale de la quantité et passage au formulaire avec nettoyage."""
    chat_id = update.effective_chat.id
    
    # 1. On récupère la quantité validée
    if update.callback_query:
        qty = context.user_data.get('current_qty', 1)
        # SUPPRESSION PHYSIQUE DU MENU QUANTITÉ
        try:
            await update.callback_query.message.delete()
        except:
            pass
    elif update.message and update.message.text:
        text = update.message.text.strip()
        if text.isdigit() and int(text) > 0:
            qty = int(text)
            # SUPPRESSION DE LA RÉPONSE CHIFFRÉE DE L'USER
            try: await update.message.delete()
            except: pass
        else:
            await update.message.reply_text("⚠️ Chiffre invalide.")
            return ID_ASK_QTY
    else:
        return ID_ASK_QTY

    # 2. Sauvegarde dans la session
    context.user_data['id_qty'] = qty
    
    # 3. Lancement du formulaire (Nouveau message propre)
    return await id_start_form(update, context)

# ==============================================================================
# 🧩 MODULE ID/DOCS COMPLET (LOGIQUE DE NETTOYAGE INCLUSE)
# ==============================================================================

async def clean_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nettoyeur physique : Supprime la question précédente et la réponse client."""
    chat_id = update.effective_chat.id
    cleanup_list = context.user_data.get('cleanup_ids', [])
    
    # On ajoute le message texte que l'utilisateur vient d'envoyer (ex: Benjamin)
    if update.message:
        cleanup_list.append(update.message.message_id)
        
    for msg_id in cleanup_list:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except:
            pass # Déjà supprimé ou trop vieux
            
    context.user_data['cleanup_ids'] = []

async def id_start_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    total = context.user_data['id_product']['price'] * context.user_data['id_qty']
    
    if get_user_balance(user_id) < total:
        await context.bot.send_message(chat_id=user_id, text=f"❌ **Solde insuffisant.**", reply_markup=kb_back_to_menu())
        return ConversationHandler.END
    
    context.user_data['cleanup_ids'] = []
    kb = [[InlineKeyboardButton("❌ Annuler", callback_data="id_menu_entry")]]
    m = await context.bot.send_message(
        chat_id=user_id, 
        text="✍️ **FORMULAIRE (1/10)**\n\nQuel est votre **PRÉNOM** (First Name) ?", 
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_NAME

async def id_save_firstname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['form_firstname'] = update.message.text.strip()
    await clean_chat(update, context)
    
    kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_NAME}")]]
    m = await context.bot.send_message(chat_id=update.effective_chat.id, text="✍️ **Quel est votre NOM DE FAMILLE** (Last Name) ?", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_LASTNAME

async def id_save_lastname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['form_lastname'] = update.message.text.strip()
    await clean_chat(update, context)
    
    kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_LASTNAME}")]]
    if context.user_data.get('id_category') == 'document':
        context.user_data['form_dob'] = "N/A"
        text, state = "📍 **Adresse (1/3)**\nEntrez le **Numéro et la Rue** :", ID_ASK_STREET
    else:
        text, state = "📅 **Date de Naissance**\n(Format: JJ/MM/AAAA ou 15 mars 1990)", ID_ASK_DOB

    m = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'] = [m.message_id]
    return state

async def id_save_dob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clean_date = parse_date_smart(update.message.text)
    await clean_chat(update, context)
    
    if not clean_date:
        kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_LASTNAME}")]]
        m = await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ **Format invalide.** Réessayez :", reply_markup=InlineKeyboardMarkup(kb))
        context.user_data['cleanup_ids'] = [m.message_id]
        return ID_ASK_DOB
        
    context.user_data['form_dob'] = clean_date
    kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_DOB}")]]
    m = await context.bot.send_message(chat_id=update.effective_chat.id, text="📍 **Adresse (1/3)**\nEntrez le **Numéro et la Rue** :", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_STREET

async def id_save_street(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['addr_street'] = update.message.text
    await clean_chat(update, context)
    kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_STREET}")]]
    m = await context.bot.send_message(chat_id=update.effective_chat.id, text="🏙️ **Adresse (2/3)**\nQuelle est la **Ville** ?", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_CITY

async def id_save_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['addr_city'] = update.message.text
    await clean_chat(update, context)
    kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_CITY}")]]
    m = await context.bot.send_message(chat_id=update.effective_chat.id, text="📮 **Adresse (3/3)**\nQuel est le **Code Postal** ?", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_ZIP

async def id_save_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reçoit le Code Postal et lance la recherche Postes Canada."""
    context.user_data['addr_zip'] = update.message.text.upper().strip()
    await clean_chat(update, context)
    
    m_wait = await context.bot.send_message(chat_id=update.effective_chat.id, text="📮 **Interrogation Postes Canada...**", parse_mode="Markdown")
    
    # On construit une recherche large : Rue + Ville + Code Postal
    search_query = f"{context.user_data['addr_street']}, {context.user_data['addr_city']}, {context.user_data['addr_zip']}"
    
    # Appel de la nouvelle fonction
    suggestions = validate_address_canada_post(search_query)
    
    # Fallback : Si l'API ne trouve rien, on propose ce que l'user a écrit
    if not suggestions:
        suggestions = [search_query]
        
    context.user_data['addr_suggestions'] = suggestions
    
    # Création des boutons
    kb = [[InlineKeyboardButton(f"📍 {a[:60]}", callback_data=f"addr_pick:{i}")] for i, a in enumerate(suggestions)]
    
    # Ajout des options Manuel et Retry
    kb.append([InlineKeyboardButton("✍️ Saisie Manuelle (Libre)", callback_data="addr_manual")])
    kb.append([InlineKeyboardButton("🔄 Réessayer", callback_data="addr_retry")])
    kb.append([InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_ZIP}")])
    
    await m_wait.edit_text("✅ **Adresse normalisée :**\nChoisissez la version officielle :", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'] = [m_wait.message_id]
    return ID_CONFIRM_ADDR

async def id_save_issue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    clean_date = parse_date_smart(txt) if txt.lower() not in ['today', "aujourd'hui"] else datetime.now().strftime("%Y-%m-%d")
    await clean_chat(update, context)
    
    if not clean_date:
        kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_CONFIRM_ADDR}")]]
        m = await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Date invalide.", reply_markup=InlineKeyboardMarkup(kb))
        context.user_data['cleanup_ids'] = [m.message_id]
        return ID_ASK_ISSUE
        
    context.user_data['form_issue'] = clean_date
    kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_ISSUE}")]]
    m = await context.bot.send_message(chat_id=update.effective_chat.id, text="📅 **Année d'Expiration ?** (Ex: 2028)", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_EXPIRY

async def id_save_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    year = update.message.text.strip()
    await clean_chat(update, context)
    
    if not year.isdigit() or len(year) != 4:
        kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_ISSUE}")]]
        m = await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ Année invalide.", reply_markup=InlineKeyboardMarkup(kb))
        context.user_data['cleanup_ids'] = [m.message_id]
        return ID_ASK_EXPIRY
    
    dob = context.user_data.get('form_dob', '2000-01-01')
    context.user_data['form_expiry'] = f"{year}-{dob[5:]}"
    
    try:
        d = datetime.strptime(dob, "%Y-%m-%d")
        formatted, base = generer_permis(context.user_data.get('form_lastname',''), context.user_data.get('form_firstname',''), d.strftime("%d-%m-%Y"))
        context.user_data['dl_base_code'] = base[:11]
        display_base = f"{formatted[:-2]}XX"
    except: display_base = "Calcul..."

    kb = [
        [InlineKeyboardButton("🔍 SAAQ (Auto)", callback_data="dl_mode:saaq")],
        [InlineKeyboardButton("✍️ Manuel", callback_data="dl_mode:manual")],
        [InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_EXPIRY}")]
    ]
    m = await context.bot.send_message(chat_id=update.effective_chat.id, text=f"💳 **PERMIS**\nBase : `{display_base}`", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_DL_NUM



async def id_save_ref_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip().upper()
    await clean_chat(update, context)
    context.user_data['form_ref_number'] = val
    kb = [[InlineKeyboardButton("Homme", callback_data="sex:1"), InlineKeyboardButton("Femme", callback_data="sex:2")],
          [InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_REF_NUM}")]]
    m = await context.bot.send_message(chat_id=update.effective_chat.id, text="👤 **Sexe ?**", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_SEX

async def id_save_sex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.callback_query.data.split(":")[1]
    context.user_data['form_sex'] = val
    await clean_chat(update, context)
    kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_SEX}")]]
    m = await context.bot.send_message(chat_id=update.effective_chat.id, text="📏 **Taille (cm) ?**", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_HEIGHT

async def id_save_height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    await clean_chat(update, context)
    context.user_data['form_height'] = val
    kb = [[InlineKeyboardButton("Brun", callback_data="eye:BRO"), InlineKeyboardButton("Bleu", callback_data="eye:BLU")],
          [InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_HEIGHT}")]]
    m = await context.bot.send_message(chat_id=update.effective_chat.id, text="👁️ **Yeux ?**", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_EYES

async def id_save_eyes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.callback_query.data.split(":")[1] if update.callback_query else update.message.text.strip()
    context.user_data['form_eyes'] = val
    await clean_chat(update, context)
    if context.user_data.get('id_category') == 'tool': return await id_show_summary(update, context)
    kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_EYES}")]]
    m = await context.bot.send_message(chat_id=update.effective_chat.id, text="📸 **Envoyez votre photo (Selfie)**", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_PHOTO

async def id_save_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['form_photo_id'] = update.message.photo[-1].file_id
    await clean_chat(update, context)
    return await id_show_summary(update, context)

# ==========================================
# 👇 COLLEZ CECI JUSTE APRÈS id_save_photo 👇
# ==========================================

async def id_show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le résumé avec le bouton MODIFIER."""
    d = context.user_data
    sex_map = {'1': 'Homme', '2': 'Femme', '9': 'X'}
    
    # On nettoie la variable 'editing_key' pour éviter les bugs
    context.user_data.pop('editing_key', None)

    txt = (
        f"📝 **RÉSUMÉ DE VOTRE COMMANDE**\n\n"
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
        # 👇 C'est cette ligne qui ajoute le bouton Modifier 👇
        [InlineKeyboardButton("✏️ Modifier / Corriger", callback_data="edit_open_menu")],
        # -------------------------------------------------------------
        [InlineKeyboardButton("❌ Annuler", callback_data="id_menu_entry")]
    ]
    
    target = update.message or update.callback_query.message
    
    # Gestion intelligente : Si on vient d'un bouton (callback), on édite le message.
    # Sinon (message texte), on envoie un nouveau message.
    if update.callback_query:
        try:
            m = await target.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        except:
             # Fallback si l'édition échoue (ex: message identique)
             m = await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        m = await target.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        
    context.user_data.setdefault('cleanup_ids', []).append(m.message_id) 
    return ID_CONFIRM_SUMMARY

# ==========================================
# 👆 FIN DU BLOC À COLLER 👆
# ==========================================



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

def creer_verso_complet(pdf417_bytes, linear_bytes):
    """
    Génère le verso complet avec un alignement horizontal parfait :
    1. PDF417 (Gros) à gauche (X=208).
    2. Code 128 (Petit) à droite (X=2445).
    3. Alignement horizontal strict sur la ligne du haut (Y=125).
    4. Recoloration #322c0d pour les deux.
    """
    try:
        # --- CONFIGURATION ---
        bg_path = "assets/back_template.jpg" 
        target_hex = "#322c0d"
        target_rgb = ImageColor.getrgb(target_hex)
        
        # =========================================================
        # 📍 COORDONNÉES VALIDÉES (Image 4067x2579 px)
        # =========================================================
        Y_ALIGN = 125  # Alignement horizontal parfait du haut

        # PDF417 (Le gros pavé à gauche)
        PDF_X, PDF_Y, PDF_W, PDF_H = 208, Y_ALIGN, 1938, 388
        
        # Code 128 (La petite bande à droite)
        LIN_X, LIN_Y, LIN_W, LIN_H = 2445, Y_ALIGN, 1404, 202
        # =========================================================

        # 1. Charger l'image de fond
        try:
            background = Image.open(bg_path).convert("RGBA")
        except FileNotFoundError:
            print(f"❌ ERREUR : Image de fond '{bg_path}' introuvable dans assets/ !")
            return None

        # --- Fonction interne pour traiter et recolorer les codes-barres ---
        def process_layer(img_bytes, w, h):
            if not img_bytes: return None
            # Ouvrir l'image du code-barres
            original = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
            
            # Gestion de la transparence (Masque)
            alpha = original.split()[3]
            if alpha.getextrema()[0] == 255: # Si fond blanc opaque
                mask = ImageOps.invert(original.convert('L'))
            else: # Si fond déjà transparent
                mask = alpha

            # Appliquer la couleur cible (#322c0d)
            colored_img = Image.new("RGBA", original.size, target_rgb)
            colored_img.putalpha(mask)

            # Redimensionnement haute qualité
            return colored_img.resize((w, h), Image.Resampling.LANCZOS)

        # 2. Traitement et Collage du PDF417 (Gauche)
        if pdf417_bytes:
            pdf_img = process_layer(pdf417_bytes, PDF_W, PDF_H)
            if pdf_img:
                background.paste(pdf_img, (PDF_X, PDF_Y), pdf_img)

        # 3. Traitement et Collage du Code 128 (Droite)
        if linear_bytes:
            lin_img = process_layer(linear_bytes, LIN_W, LIN_H)
            if lin_img:
                background.paste(lin_img, (LIN_X, LIN_Y), lin_img)

        # 4. Export Final en PNG (Qualité maximale)
        output = io.BytesIO()
        background.save(output, format="PNG", optimize=True)
        output.seek(0)
        return output

    except Exception as e:
        print(f"❌ Erreur PIL Verso Complet: {e}")
        import traceback
        traceback.print_exc()
        return None

async def id_finalize_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio
    import sqlite3
    q = update.callback_query
    
    if q:
        try: await q.answer()
        except: pass
    
    user = update.effective_user
    d = context.user_data
    
    # 1. Récupération des données
    cat = d.get('id_category', 'ID')
    prod = d.get('id_product', {})
    prod_name = prod.get('name', 'Produit Inconnu')
    prod_code = prod.get('code', 'QC')

    def clean(key): return str(d.get(key, '')).upper().strip()

    api_data = {
        "form_firstname": clean('form_firstname'),
        "form_lastname": clean('form_lastname'),
        "form_dob": clean('form_dob'),
        "form_sex": clean('form_sex'),
        "form_height": clean('form_height'),
        "form_eyes": clean('form_eyes'),
        "form_street": clean('addr_street'),
        "form_city": clean('addr_city'),
        "form_zip": clean('addr_zip'),
        "form_dl_number": clean('form_dl_number'),
        "form_ref_number": clean('form_ref_number'),
        "form_issue": clean('form_issue'),
        "form_expiry": clean('form_expiry')
    }

    # 2. Calcul du coût
    total_cost = d.get('id_qty', 1) * prod.get('price', 0)

    # 3. Construction du texte Admin
    admin_txt = (
        f"🚨 NOUVELLE COMMANDE : {cat.upper()} 🚨\n\n"
        f"👤 CLIENT : {user.first_name} (@{user.username if user.username else 'N/A'})\n"
        f"🆔 ID : {user.id}\n"
        f"🛒 PRODUIT : {prod_name} (x{d.get('id_qty', 1)})\n"
        f"💰 TOTAL : {total_cost:.2f} $\n"
        f"--------------\n\n"
        f"🆔 DOCUMENTS :\n\n"
        f"💳 Permis : {api_data['form_dl_number']}\n"
        f"📛 Nom : {api_data['form_lastname']}\n"
        f"📛 Prénom : {api_data['form_firstname']}\n"
        f"🎂 DDN : {api_data['form_dob']}\n"
        f"📍 Adresse : {api_data['form_street']}\n"
        f"🏙️ Ville: {api_data['form_city']}\n"
        f"📨 CP: {api_data['form_zip']}\n"
        f"📏 Taille: {api_data['form_height']} cm\n"
        f"👁 Yeux : {api_data['form_eyes']}\n"
        f"🧬 Sexe: {api_data['form_sex']}\n"
        f"🔢 Référence : {api_data['form_ref_number']}\n"
        f"📅 Valide : {api_data['form_issue']}\n"
        f"📅 Expiration : {api_data['form_expiry']}\n"
    )

    # 4. DOUBLE SAUVEGARDE EN BASE DE DONNÉES
    try:
        con = sqlite3.connect(DB_NAME)
        cur = con.cursor()
        
        # A. Sauvegarde dans support_tickets (pour l'admin)
        cur.execute(
            "INSERT INTO support_tickets (user_id, username, category, status, message) VALUES (?, ?, ?, ?, ?)",
            (str(user.id), user.username or "Inconnu", f"ORDER: {prod_name}", "closed", admin_txt)
        )
        order_ticket_id = cur.lastrowid

        # B. Sauvegarde détaillée dans id_physical_submissions (pour modifications/historique)
        cur.execute("""
            INSERT INTO id_physical_submissions (
                user_id, type_document, status, last_name, first_name, dob, 
                street, city, zip, height, eyes, sex, 
                dl_number, ref_number, issue_date, expiry_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(user.id), cat.upper(), 'pending',
            api_data['form_lastname'], api_data['form_firstname'], api_data['form_dob'],
            api_data['form_street'], api_data['form_city'], api_data['form_zip'],
            api_data['form_height'], api_data['form_eyes'], api_data['form_sex'],
            api_data['form_dl_number'], api_data['form_ref_number'],
            api_data['form_issue'], api_data['form_expiry']
        ))
        
        con.commit()
        con.close()
        admin_txt = f"🧾 **ORDER #{order_ticket_id}**\n" + admin_txt
        print(f"[DB] Commande {order_ticket_id} archivée totalement.")
        
    except Exception as db_err:
        print(f"❌ DB SAVE ERROR: {db_err}")

    # 5. Message d'attente client
    status_msg = await context.bot.send_message(
        chat_id=user.id, 
        text="⏳ **Validation et génération des fichiers...**", 
        parse_mode="Markdown"
    )

    try:
        # 6. Débit
        update_user_balance(str(user.id), -total_cost)

        # 7. Génération Barcode (API)
        loop = asyncio.get_running_loop()
        pdf417_raw, linear_raw = await loop.run_in_executor(None, lambda: generate_barcode_via_api(api_data, prod_code))

        # 8. CRÉATION DU VERSO COMPLET
        final_verso = None
        if pdf417_raw:
            final_verso = await loop.run_in_executor(None, lambda: creer_verso_complet(pdf417_raw, linear_raw))

        # 9. Envoi au Canal Logs (Admin)
        target_id = CHANNEL_LOGS 
        
        if d.get('form_photo_id'):
            await context.bot.send_photo(chat_id=target_id, photo=d['form_photo_id'], caption=admin_txt)
        else:
            await context.bot.send_message(chat_id=target_id, text=admin_txt)
        
        if final_verso: 
            await context.bot.send_document(
                chat_id=target_id, 
                document=final_verso, 
                filename=f"Back_{api_data['form_lastname']}.png",
                caption="🖨️ **Verso Généré (Prêt à imprimer)**"
            )
        else:
            if pdf417_raw:
                 await context.bot.send_document(chat_id=target_id, document=pdf417_raw, filename="raw_pdf417.png")

        # 10. Succès Client
        await status_msg.edit_text("✅ **Commande reçue !**\nLes fichiers ont été envoyés à l'équipe.\nVotre solde a été débité.", parse_mode="Markdown")

    except Exception as e:
        print(f"CRITICAL ERROR in Order: {e}")
        await status_msg.edit_text(f"⚠️ **Commande enregistrée mais erreur technique.**\nL'admin a reçu les détails.")
        try:
            await context.bot.send_message(chat_id=CHANNEL_LOGS, text=f"⚠️ ERREUR TECHNIQUE {user.id}: {e}")
        except: pass

    # 11. Nettoyage et Retour Menu
    await asyncio.sleep(2)
    for mid in d.get('cleanup_ids', []):
        try: await context.bot.delete_message(chat_id=user.id, message_id=mid)
        except: pass
    
    context.user_data['cleanup_ids'] = []
    # On marque le retour pour le panier/navigation
    context.user_data['cart_return_to'] = "menu_accueil"
    await show_main_menu(user.id)
    return ConversationHandler.END

async def id_form_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère le clic sur le bouton Retour dans le formulaire et NETTOIE l'écran."""
    q = update.callback_query
    await q.answer()
    
    # --- NETTOYAGE PHYSIQUE (ÉVITE L'ACCUMULATION) ---
    chat_id = update.effective_chat.id
    cleanup_list = context.user_data.get('cleanup_ids', [])
    
    # On supprime tout ce qui est dans la liste (vieilles questions + vos réponses Benjamin etc.)
    # Sauf le tout dernier message qui est celui que nous allons éditer pour afficher le "Retour"
    if len(cleanup_list) > 1:
        for msg_id in cleanup_list[:-1]:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except:
                pass # Déjà supprimé ou trop vieux
        
        # On ne garde que le message actuel (le menu) dans la liste
        context.user_data['cleanup_ids'] = [cleanup_list[-1]]
    # --------------------------------------------------

    try:
        target_state = int(q.data.split(":")[1])
    except:
        return ConversationHandler.END
        
    async def show(text, kb_back_state=None):
        kb = []
        if kb_back_state is not None:
            kb.append([InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{kb_back_state}")])
        else:
            kb.append([InlineKeyboardButton("❌ Annuler", callback_data="id_menu_entry")])
            
        # On édite le message actuel qui est maintenant le seul message visible
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    # --- ROUTAGE DES ÉTAPES DE RETOUR ---
    
    if target_state == ID_ASK_NAME:
        await show("✍️ **FORMULAIRE (1/10)**\n\nQuel est votre **PRÉNOM** (First Name) ?", kb_back_state=None)
        return ID_ASK_NAME
        
    elif target_state == ID_ASK_LASTNAME:
        await show("✍️ **Quel est votre NOM DE FAMILLE** (Last Name) ?", kb_back_state=ID_ASK_NAME)
        return ID_ASK_LASTNAME
        
    elif target_state == ID_ASK_DOB:
        await show("📅 **Date de Naissance**\n(Format: JJ/MM/AAAA ou 15 mars 1990)", kb_back_state=ID_ASK_LASTNAME)
        return ID_ASK_DOB
        
    elif target_state == ID_ASK_STREET:
        # On vérifie si c'est un Document ou une ID pour savoir si on retourne à DOB ou Nom
        prev = ID_ASK_DOB if context.user_data.get('id_category') != 'document' else ID_ASK_LASTNAME
        await show("📍 **Adresse (1/3)**\nEntrez le **Numéro et la Rue** :", kb_back_state=prev)
        return ID_ASK_STREET
        
    elif target_state == ID_ASK_CITY:
        await show("🏙️ **Adresse (2/3)**\nQuelle est la **Ville** ?", kb_back_state=ID_ASK_STREET)
        return ID_ASK_CITY
        
    elif target_state == ID_ASK_ZIP:
        await show("📮 **Adresse (3/3)**\nQuel est le **Code Postal** ?", kb_back_state=ID_ASK_CITY)
        return ID_ASK_ZIP
        
    elif target_state == ID_CONFIRM_ADDR:
        suggs = context.user_data.get('addr_suggestions', [])
        raw = f"{context.user_data.get('addr_street')}, {context.user_data.get('addr_city')}"
        if not suggs: suggs = [raw]
        
        kb = [[InlineKeyboardButton(f"📍 {a[:40]}", callback_data=f"addr_pick:{i}")] for i, a in enumerate(suggs)]
        kb.append([InlineKeyboardButton("✍️ Réécrire", callback_data="addr_retry")])
        kb.append([InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_ZIP}")])
        
        await q.edit_message_text("✅ Confirmez l'adresse :", reply_markup=InlineKeyboardMarkup(kb))
        return ID_CONFIRM_ADDR
        
    elif target_state == ID_ASK_ISSUE:
        await show("📅 **Date d'Émission (4d) ?**\n(Ex: 15/01/2023 ou Aujourd'hui)", kb_back_state=ID_CONFIRM_ADDR)
        return ID_ASK_ISSUE
        
    elif target_state == ID_ASK_EXPIRY:
        await show("📅 **Quelle est l'Année d'Expiration ?**\n(Le jour/mois seront ceux de la naissance)\nEx: **2028**", kb_back_state=ID_ASK_ISSUE)
        return ID_ASK_EXPIRY
        
    elif target_state == ID_ASK_DL_NUM:
        base = context.user_data.get('dl_base_code', 'Permis')
        text = (
            "💳 **NUMÉRO DE PERMIS**\n\n"
            f"Base calculée : `{base}`\n\n"
            "Que voulez-vous faire ?\n"
            "1️⃣ **Vérifier SAAQ** (Auto)\n"
            "2️⃣ **Manuel** (Saisie des 2 derniers chiffres)"
        )
        kb = [
            [InlineKeyboardButton("🔍 Vérifier via SAAQ (Auto)", callback_data="dl_mode:saaq")],
            [InlineKeyboardButton("✍️ Entrer les 2 chiffres", callback_data="dl_mode:manual")],
            [InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_EXPIRY}")]
        ]
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return ID_ASK_DL_NUM
        
    elif target_state == ID_ASK_REF_NUM:
        # Appel de la fonction interactive qui gère sa propre édition de message
        return await ask_ref_number_interactive(update, context)
        
    elif target_state == ID_ASK_SEX:
        kb_sex = [
            [InlineKeyboardButton("Homme (Male)", callback_data="sex:1"), 
             InlineKeyboardButton("Femme (Female)", callback_data="sex:2")],
            [InlineKeyboardButton("Non spécifié (X)", callback_data="sex:9")],
            [InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_REF_NUM}")]
        ]
        await q.edit_message_text("👤 **Sexe / Genre ?**", reply_markup=InlineKeyboardMarkup(kb_sex))
        return ID_ASK_SEX
        
    elif target_state == ID_ASK_HEIGHT:
        await show("📏 **Quelle est votre taille ?**\n_(Entrez uniquement les chiffres en cm, ex: 175)_", kb_back_state=ID_ASK_SEX)
        return ID_ASK_HEIGHT
        
    elif target_state == ID_ASK_EYES:
        kb = [
            [InlineKeyboardButton("Brun (Brown)", callback_data="eye:BRO"), InlineKeyboardButton("Bleu (Blue)", callback_data="eye:BLU")],
            [InlineKeyboardButton("Vert (Green)", callback_data="eye:GRN"), InlineKeyboardButton("Noisette (Hazel)", callback_data="eye:HZL")],
            [InlineKeyboardButton("Gris (Grey)", callback_data="eye:GRY"), InlineKeyboardButton("Noir (Black)", callback_data="eye:BLK")],
            [InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_HEIGHT}")]
        ]
        await q.edit_message_text("👁️ **Couleur des yeux ?**", reply_markup=InlineKeyboardMarkup(kb))
        return ID_ASK_EYES
        
    elif target_state == ID_ASK_PHOTO:
        await show("📸 **Envoyez votre photo (Selfie)**", kb_back_state=ID_ASK_EYES)
        return ID_ASK_PHOTO

    return ConversationHandler.END

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
# --- À REMPLACER COMPLÈTEMENT DANS APP.PY ---
admin_search_conv = ConversationHandler(
    entry_points=[
        # 1. Entrée par recherche d'ID
        CallbackQueryHandler(admin_search_user_start, pattern="^admin_search_user_start$"),
        
        # 2. 🔥 ENTREE CRUCIALE AJOUTÉE ICI 🔥
        # C'est ça qui manquait : le bouton "Modifier solde" déclenche l'écoute
        CallbackQueryHandler(admin_customamount_start, pattern="^admin_customamount_")
    ],
    states={
        # État A : Recherche ID
        ADMIN_WAIT_SEARCH_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_search_user_receive)],
        
        # État B : Attente du montant (L'état "1")
        1: [MessageHandler(filters.Regex(r"^-?\d+([.,]\d+)?$"), admin_customamount_receive)]
    },
    fallbacks=[
        CallbackQueryHandler(admin_users, pattern="^admin_users"),
        CallbackQueryHandler(goto_menu, pattern="^menu_accueil$")
    ],
    allow_reentry=True,
    name="admin_solde_conversation"
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
            CallbackQueryHandler(filter_cancel, pattern="^filter_cancel$"),
            # J'ai supprimé la ligne 'filter_page_nav' qui faisait planter
        ],
        CATALOG_FILTER_AWAIT_VALUE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, filter_receive_value)
        ],
    },
    fallbacks=[CallbackQueryHandler(filter_cancel, pattern="^filter_cancel$")],
    allow_reentry=True
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



async def delete_history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        # Récupère l'ID de l'achat depuis le bouton
        item_id = int(q.data.split("_")[-1])
        user_id = str(q.from_user.id)
        
        con = sqlite3.connect(DB_NAME)
        cur = con.cursor()
        # Sécurité : on supprime seulement si ça appartient à l'utilisateur
        cur.execute("DELETE FROM purchases WHERE id=? AND user_id=?", (item_id, user_id))
        con.commit()
        con.close()
        
        await q.answer("🗑 Supprimé !")
        # On supprime le message du bot pour nettoyer l'écran
        try:
            await q.message.delete()
        except:
            await q.message.edit_text("🗑 Cet élément a été supprimé.")
            
    except Exception as e:
        print(f"Erreur suppression historique: {e}")
        try: await q.answer("Erreur technique.")
        except: pass

def build_categories_kb():
    """Génère le clavier des catégories."""
    kb = [
        [InlineKeyboardButton("💳 Cc's", callback_data="ccs_catalog_start")],
        [InlineKeyboardButton("👥 Pro's", callback_data="propro")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
    ]
    return InlineKeyboardMarkup(kb)

async def on_back_cats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retourne au menu des catégories."""
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_text("📂 **Catégories**", reply_markup=build_categories_kb(), parse_mode="Markdown")
    except:
        await q.message.reply_text("📂 **Catégories**", reply_markup=build_categories_kb(), parse_mode="Markdown")

async def on_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère le clic sur une catégorie générique."""
    q = update.callback_query
    await q.answer()
    data = q.data.split(":")[-1]
    
    if data == "ccs":
       
        return await show_products_ccs(update, context, page=0)
    elif data == "propro":

        return await show_products(update, context, page=0)
    else:
        await q.message.reply_text(f"Catégorie : {data}")




# ==============================================================================
# 👤 GESTION DU COMPTE (NETTOYAGE AUTOMATIQUE)
# ==============================================================================

async def account_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le menu Mon Compte."""
    q = update.callback_query
    if q: await q.answer()
    
    user_id = str(update.effective_user.id)
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    try:
        cur.execute("SELECT custom_username, jabber_id FROM users WHERE telegram_id=?", (user_id,))
        row = cur.fetchone()
        c_user = row[0] if row and row[0] else "Non défini"
        c_jabber = row[1] if row and row[1] else "Non défini"
    except:
        c_user = "Non défini"
        c_jabber = "Non défini"
    con.close()
    
    text = (
        f"👤 **MON COMPTE**\n\n"
        f"🆔 **ID Telegram:** `{user_id}`\n"
        f"👤 **Username:** `{c_user}`\n"
        f"💬 **Jabber:** `{c_jabber}`\n"
        f"🔐 **Sécurité:** PIN Actif\n\n"
        "Que voulez-vous modifier ?"
    )
    
    kb = [
        [InlineKeyboardButton("🔐 Changer mon PIN", callback_data="acc_change_pin")],
        [InlineKeyboardButton("⏳ Délai Inactivité", callback_data="acc_timeout_menu")],
        [InlineKeyboardButton("👤 Changer Username", callback_data="acc_set_user")],
        [InlineKeyboardButton("💬 Changer Jabber", callback_data="acc_set_jabber")],
        [InlineKeyboardButton("⚠️ Reset Wallet (Seed)", callback_data="acc_reset_seed")],
        [InlineKeyboardButton("⬅️ Retour Menu", callback_data="menu_accueil")]
    ]
    
    if q:
        try: await q.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        except: await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        
    return SELECT_TOOL 

# --- 1. CHANGER PIN (CLEAN) ---
async def acc_ask_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    msg = await q.message.edit_text(
        "🔐 **Nouveau PIN**\n\nVeuillez entrer votre nouveau code PIN (4 à 8 chiffres) :",
        reply_markup=kb_back_cancel()
    )
    context.user_data['acc_prompt_id'] = msg.message_id # On retient l'ID de la question
    return ACC_WAIT_NEW_PIN

async def acc_save_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio
    new_pin = update.message.text.strip()
    user_id = str(update.effective_user.id)
    
    # 🧹 NETTOYAGE 1 : Supprime la réponse de l'utilisateur
    try: await update.message.delete()
    except: pass

    # 🧹 NETTOYAGE 2 : Supprime la question du bot
    try: await context.bot.delete_message(chat_id=user_id, message_id=context.user_data.get('acc_prompt_id'))
    except: pass
    
    if not new_pin.isdigit() or len(new_pin) < 4 or len(new_pin) > 8:
        # Erreur : on renvoie un message (et on le track pour le supprimer au prochain essai)
        msg = await update.message.reply_text("❌ Le PIN doit contenir 4 à 8 chiffres.\nRéessayez :")
        context.user_data['acc_prompt_id'] = msg.message_id
        return ACC_WAIT_NEW_PIN
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("UPDATE users SET pin_code=? WHERE telegram_id=?", (new_pin, user_id))
    con.commit()
    con.close()
    
    # 🧹 NETTOYAGE 3 : Confirmation Flash (2 secondes puis disparait)
    conf = await update.message.reply_text("✅ **PIN modifié avec succès !**")
    await asyncio.sleep(2)
    try: await conf.delete()
    except: pass
    
    await show_main_menu(int(user_id), clear=True)
    return ConversationHandler.END

# --- 2. USERNAME (CLEAN) ---
async def acc_ask_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    msg = await q.message.edit_text(
        "👤 **Username**\n\nEntrez le nom d'affichage souhaité (ex: LeBossDu93) :",
        reply_markup=kb_back_cancel()
    )
    context.user_data['acc_prompt_id'] = msg.message_id
    return ACC_WAIT_USERNAME

async def acc_save_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio
    txt = update.message.text.strip()
    user_id = str(update.effective_user.id)
    
    # 🧹 NETTOYAGE : User msg + Bot Question
    try: await update.message.delete()
    except: pass
    try: await context.bot.delete_message(chat_id=user_id, message_id=context.user_data.get('acc_prompt_id'))
    except: pass
    
    con = sqlite3.connect(DB_NAME)
    con.execute("UPDATE users SET custom_username=? WHERE telegram_id=?", (txt, user_id))
    con.commit()
    con.close()
    
    # 🧹 NETTOYAGE : Confirmation Flash
    conf = await update.message.reply_text(f"✅ Username défini sur : **{txt}**")
    await asyncio.sleep(2)
    try: await conf.delete()
    except: pass
    
    await show_main_menu(int(user_id), clear=True)
    return ConversationHandler.END

# --- 3. JABBER (CLEAN) ---
async def acc_ask_jabber(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    msg = await q.message.edit_text(
        "💬 **Jabber ID**\n\nEntrez votre adresse Jabber/XMPP (ex: user@jabb.im) :",
        reply_markup=kb_back_cancel()
    )
    context.user_data['acc_prompt_id'] = msg.message_id
    return ACC_WAIT_JABBER

async def acc_save_jabber(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio
    txt = update.message.text.strip()
    user_id = str(update.effective_user.id)
    
    # 🧹 NETTOYAGE
    try: await update.message.delete()
    except: pass
    try: await context.bot.delete_message(chat_id=user_id, message_id=context.user_data.get('acc_prompt_id'))
    except: pass
    
    con = sqlite3.connect(DB_NAME)
    con.execute("UPDATE users SET jabber_id=? WHERE telegram_id=?", (txt, user_id))
    con.commit()
    con.close()
    
    # 🧹 NETTOYAGE
    conf = await update.message.reply_text(f"✅ Jabber défini sur : **{txt}**")
    await asyncio.sleep(2)
    try: await conf.delete()
    except: pass
    
    await show_main_menu(int(user_id), clear=True)
    return ConversationHandler.END

async def acc_timeout_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    user_id = str(update.effective_user.id)
    
    # Récupérer la valeur actuelle
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT inactivity_timeout FROM users WHERE telegram_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    
    current = row[0] if row and row[0] else 300 # 300s (5 min) par défaut
    
    # Petit helper pour mettre le check ✅
    def fmt(val, label):
        return f"✅ {label}" if current == val else label

    kb = [
        [InlineKeyboardButton(fmt(300, "5 min"), callback_data="set_timeout_300"),
         InlineKeyboardButton(fmt(1800, "30 min"), callback_data="set_timeout_1800")],
        [InlineKeyboardButton(fmt(3600, "1h"), callback_data="set_timeout_3600"),
         InlineKeyboardButton(fmt(21600, "6h (Max)"), callback_data="set_timeout_21600")],
        [InlineKeyboardButton("⬅️ Retour Compte", callback_data="account_menu")]
    ]
    
    await replace_view(
        q, 
        f"⏳ **AUTO-LOCK TIMER**\n"
        f"Actuellement : **{current//60} minutes**\n\n"
        f"Au bout de combien de temps d'inactivité le bot doit-il se verrouiller (PIN) ?", 
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return SELECT_TOOL

async def acc_set_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    # data format: set_timeout_300
    val = int(q.data.split("_")[2])
    user_id = str(update.effective_user.id)
    
    # 1. Mise à jour DB
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("UPDATE users SET inactivity_timeout=? WHERE telegram_id=?", (val, user_id))
    con.commit()
    con.close()
    
    # 2. Mise à jour Mémoire immédiate (pour que le job le sache tout de suite)
    context.user_data['inactivity_limit'] = val
    
    await q.answer(f"✅ Délai réglé sur {val//60} min", show_alert=False)
    
    # 3. Rafraîchir le menu pour voir le check
    await acc_timeout_menu(update, context)
    return SELECT_TOOL

# --- 4. RESET SEED (Reste inchangé) ---
async def acc_reset_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kb = [
        [InlineKeyboardButton("🔥 OUI, RESET TOUT 🔥", callback_data="confirm_reset_seed")],
        [InlineKeyboardButton("❌ Annuler", callback_data="account_menu")]
    ]
    await q.message.edit_text(
        "⚠️ **DANGER : RESET WALLET** ⚠️\n\n"
        "Vous êtes sur le point de déconnecter ce wallet de votre compte.\n"
        "Si vous n'avez pas sauvegardé votre Seed (24 mots), **VOS FONDS SERONT PERDUS À JAMAIS**.\n\n"
        "Voulez-vous vraiment générer un nouveau wallet ?",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ACC_WAIT_RESET_CONFIRM

async def acc_reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = str(update.effective_user.id)
    
    con = sqlite3.connect(DB_NAME)
    con.execute("DELETE FROM users WHERE telegram_id=?", (user_id,))
    con.commit()
    con.close()
    
    await q.message.edit_text(
        "♻️ **Compte réinitialisé.**\n\n"
        "Votre wallet a été détaché.\n"
        "Veuillez taper /start pour configurer un nouveau portefeuille."
    )
    return ConversationHandler.END

# ==============================================================================
# ⌨️ SYSTÈME TICKET "TERMINAL" (CLAVIER VIRTUEL + DASHBOARD)
# ==============================================================================

def get_virtual_keyboard(page='letters', prefix="vkey"):
    kb = []
    
    if page == 'letters':
        # ✨ DESIGN GRILLE 5x5 (Symétrique & Propre)
        # On coupe QWERTYUIOP (10) en deux lignes de 5 parfaits
        rows = [
            list("QWERT"),    # 5 touches
            list("YUIOP"),    # 5 touches (Alignement parfait)
            list("ASDFG"),    # 5 touches
            list("HJKL"),     # 4 touches (Un peu plus larges, confortables)
            list("ZXCVBNM"),  # 7 touches (Rentre sur une ligne)
        ]
        
        for r in rows: 
            kb.append([InlineKeyboardButton(char, callback_data=f"{prefix}:{char}") for char in r])
            
        # Barre d'outils (Ponctuation rapide)
        kb.append([
            InlineKeyboardButton(".", callback_data=f"{prefix}:."),
            InlineKeyboardButton(",", callback_data=f"{prefix}:,"),
            InlineKeyboardButton("?", callback_data=f"{prefix}:?"),
            InlineKeyboardButton("!", callback_data=f"{prefix}:!"),
            InlineKeyboardButton("@", callback_data=f"{prefix}:@")
        ])

        # Commandes du bas
        kb.append([
            InlineKeyboardButton("🔢 123", callback_data=f"{prefix}:switch_num"), 
            InlineKeyboardButton("␣ ESPACE ␣", callback_data=f"{prefix}:SPACE"), 
            InlineKeyboardButton("⌫", callback_data=f"{prefix}:DEL")
        ])

    elif page == 'numbers':
        # Pavé numérique "Téléphone" (3 par ligne = très gros boutons)
        rows = [
            ['1','2','3'],
            ['4','5','6'],
            ['7','8','9'],
            ['.', '0', ',']
        ]
        for r in rows: 
            kb.append([InlineKeyboardButton(char, callback_data=f"{prefix}:{char}") for char in r])
            
        # Ligne de symboles math/web
        kb.append([
            InlineKeyboardButton("@", callback_data=f"{prefix}:@"),
            InlineKeyboardButton("-", callback_data=f"{prefix}:-"),
            InlineKeyboardButton("_", callback_data=f"{prefix}:_"),
            InlineKeyboardButton("/", callback_data=f"{prefix}:/")
        ])

        kb.append([
            InlineKeyboardButton("🔤 ABC", callback_data=f"{prefix}:switch_let"), 
            InlineKeyboardButton("␣ ESPACE ␣", callback_data=f"{prefix}:SPACE"), 
            InlineKeyboardButton("⌫", callback_data=f"{prefix}:DEL")
        ])
    
    # Boutons d'Action (Séparés pour éviter les erreurs de clic)
    cancel_cb = "adm_main_menu" if "adm" in prefix else "menu_accueil" 
    
    kb.append([
        InlineKeyboardButton("🔙 Retour", callback_data=f"{prefix}:CANCEL"), 
        InlineKeyboardButton("✅ Envoyer", callback_data=f"{prefix}:SEND")
    ])
    
    return InlineKeyboardMarkup(kb)
def _generate_dashboard_text(cat, user_text, status_label, admin_reply=None):
    content = user_text if user_text else "_(Écrivez votre message ici...)_"
    txt = f"📝 **SUPPORT LIVE // {cat}**\nStatut : {status_label}\n━━━━━━━━━━━━━━━━━━\n\n👤 **VOUS :**\n{content}\n"
    if admin_reply: txt += f"\n━━━━━━━━━━━━━━━━━━\n👮‍♂️ **ADMIN :**\n{admin_reply}\n"
    return txt

async def ticket_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permet de retourner DANS le terminal d'un ticket existant."""
    q = update.callback_query
    await q.answer()
    
    # Récupération de l'ID depuis le bouton (ticket_resume:123)
    try:
        tid = q.data.split(":")[1]
    except:
        await q.message.reply_text("❌ Erreur ID Ticket.")
        return ConversationHandler.END
    
    # Restauration de la session en mémoire
    context.user_data['current_ticket_id'] = tid
    context.user_data['ticket_buffer'] = "" # On vide la saisie en cours
    
    # On marque le ticket comme "Lu" si c'était une réponse admin
    con = sqlite3.connect(DB_NAME)
    con.execute("UPDATE support_tickets SET status='open' WHERE ticket_id=? AND status='replied'", (tid,))
    con.commit()
    con.close()
    
    # On relance l'affichage du Terminal (Clavier Virtuel + Historique)
    # Assurez-vous d'avoir la fonction refresh_ticket_dashboard que je vous ai donnée avant
    await refresh_ticket_dashboard(context, q.message.chat_id, q.message.message_id, tid, "")
    
    # IMPORTANT : On retourne l'état TICKET_DRAFT pour que les boutons du clavier virtuel fonctionnent !
    return TICKET_DRAFT

async def ticket_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = str(update.effective_user.id)

    # --- NOUVELLE VÉRIFICATION : TICKET EN COURS ---
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    # On cherche si l'utilisateur a un ticket qui n'est pas 'closed' ou 'read'
    cur.execute(
        "SELECT ticket_id FROM support_tickets WHERE user_id=? AND status IN ('open', 'replied') LIMIT 1", 
        (user_id,)
    )
    existing_ticket = cur.fetchone()
    con.close()

    if existing_ticket:
        # Si un ticket est trouvé, on refuse l'ouverture d'un nouveau
        await q.edit_message_text(
            "⚠️ **ACTION IMPOSSIBLE**\n\n"
            f"Vous avez déjà un ticket en cours (ID: #{existing_ticket[0]}).\n"
            "Veuillez attendre la fermeture de celui-ci avant d'en ouvrir un nouveau.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="support")]]),
            parse_mode="Markdown"
        )
        return ConversationHandler.END # On arrête la conversation ici
    # -----------------------------------------------

    # Si aucun ticket en cours, on continue normalement
    context.user_data['ticket_draft'] = ""
    kb = [
        [InlineKeyboardButton("💳 PAIEMENT", callback_data="tick_cat:PAIEMENT")],
        [InlineKeyboardButton("🚚 COMMANDE", callback_data="tick_cat:COMMANDE")],
        [InlineKeyboardButton("🆘 AUTRE", callback_data="tick_cat:AUTRE")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
    ]
    await replace_view(q, "📨 **NOUVEAU TICKET**\n\nSélectionnez le sujet :", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return tickets.WAIT_CATEGORY

async def ticket_init_virtual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cat = q.data.split(":")[1]
    context.user_data['ticket_cat'] = cat
    context.user_data['ticket_buffer'] = ""
    try: await q.message.delete()
    except: pass
    
    msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=_generate_dashboard_text(cat, "", "✏️ En rédaction..."), reply_markup=get_virtual_keyboard('letters'), parse_mode="Markdown")
    context.user_data['dashboard_id'] = msg.message_id
    return TICKET_DRAFT

async def ticket_handle_virtual_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer()
    except: pass
    
    data = q.data.split(":")[1]
    current_text = context.user_data.get('ticket_buffer', "")
    tid = context.user_data.get('current_ticket_id', "?")

    # --- GESTION DU RETOUR ---
    if data == "CANCEL":
        # 1. On nettoie le buffer
        context.user_data['ticket_buffer'] = ""
        
        # 2. On supprime le clavier virtuel
        try: await q.message.delete()
        except: pass
        
        # 3. On affiche le menu précédent (Ici le Menu Principal)
        # Si vous avez un menu "Mes Tickets", appelez-le ici à la place.
        await show_main_menu(update.effective_user.id, clear=True)
        
        # 4. IMPORTANT : On quitte le mode conversation
        return ConversationHandler.END

    # ... (Le reste de la logique DEL, SPACE, SEND reste pareil) ...
    elif data == "DEL":
        current_text = current_text[:-1]
    elif data == "SPACE":
        current_text += " "
    elif data == "switch_num": 
        await q.edit_message_reply_markup(reply_markup=get_virtual_keyboard('numbers'))
        return TICKET_DRAFT
    elif data == "switch_let": 
        await q.edit_message_reply_markup(reply_markup=get_virtual_keyboard('letters'))
        return TICKET_DRAFT
    elif data == "SEND": 
        if len(current_text) < 1: return TICKET_DRAFT
        return await ticket_finalize_send(update, context, current_text)
    else:
        current_text += data
    
    context.user_data['ticket_buffer'] = current_text
    await refresh_ticket_dashboard(context, q.message.chat_id, q.message.message_id, tid, current_text)
        
    return TICKET_DRAFT

async def ticket_reject_physical(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: await update.message.delete()
    except: pass
    m = await update.message.reply_text("⛔ **ERREUR PROTOCOLE**\nUtilisez le clavier virtuel uniquement.")
    await asyncio.sleep(2); 
    try: await m.delete()
    except: pass
    return TICKET_DRAFT

async def refresh_ticket_dashboard(context, chat_id, message_id, ticket_id, current_buffer=""):
    """Met à jour l'écran USER : Version Design Épuré (Minimaliste)."""
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT sender_role, message, created_at FROM ticket_messages WHERE ticket_id=? ORDER BY created_at ASC", (ticket_id,))
    rows = cur.fetchall()
    con.close()
    
    # 🎨 HEADER : Simple
    header = f"🎫 **Ticket #{ticket_id}**\n\n"
    
    # 🎨 FOOTER : Juste le curseur (très clean)
    footer = f"\n\n✎ `{current_buffer}█`"
    
    # Calcul dynamique de l'espace
    reserved_space = len(header) + len(footer) + 100
    available_history = 4096 - reserved_space
    if available_history < 500: available_history = 500

    history_txt = ""
    for role, msg, date in rows:
        # On utilise le gras pour la structure
        if role == 'user':
            line = f"👤 **Vous :** {msg}\n"
        else:
            line = f"👨‍💻 **Support :** {msg}\n"
        history_txt += line
    
    if not history_txt: history_txt = "_(Aucun message)_"

    if len(history_txt) > available_history:
        history_txt = "..." + history_txt[-available_history:]

    full_text = header + history_txt + footer
    
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, 
            message_id=message_id, 
            text=full_text, 
            reply_markup=get_virtual_keyboard('letters'), 
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Refresh User Error: {e}")

async def ticket_finalize_send(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text):
    q = update.callback_query
    user = update.effective_user
    user_id = str(user.id)
    cat = context.user_data.get('ticket_cat', 'General')
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()

    # --- 1. Gestion Ticket (Anti-Doublon) ---
    cur.execute("SELECT ticket_id FROM support_tickets WHERE user_id=? AND status IN ('open', 'replied') ORDER BY ticket_id DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    
    if row:
        ticket_id = row[0]
        context.user_data['current_ticket_id'] = ticket_id
    else:
        cur.execute("INSERT INTO support_tickets (user_id, category, message, status, username) VALUES (?,?,?,?,?)", 
                    (user_id, cat, message_text, 'open', user.username or user.first_name))
        ticket_id = cur.lastrowid
        context.user_data['current_ticket_id'] = ticket_id

    # 2. Sauvegarde du message
    cur.execute("INSERT INTO ticket_messages (ticket_id, sender_role, message) VALUES (?, 'user', ?)", (ticket_id, message_text))
    
    # 3. Mise à jour Dashboard
    cur.execute("UPDATE support_tickets SET dashboard_msg_id=?, status='open' WHERE ticket_id=?", (q.message.message_id, ticket_id))
    
    con.commit()
    con.close()
    
    # --- 4. Notification Admin (MODIFIÉE) ---
    # Pas de bouton, juste le texte demandé
    admin_txt = f"⚠️ **Vous avez reçu une notification (Ticket #{ticket_id}).**\nConsultez la section ticket."
    
    try: 
        await context.bot.send_message(
            chat_id=CHANNEL_LOGS, 
            text=admin_txt, 
            parse_mode="Markdown"
            # reply_markup retiré comme demandé
        )
    except: pass

    # 5. Rafraîchissement Terminal Client
    context.user_data['ticket_buffer'] = "" 
    await refresh_ticket_dashboard(context, user.id, q.message.message_id, ticket_id, "")
    
    return TICKET_DRAFT

async def refresh_admin_dashboard(context, chat_id, message_id, ticket_id, current_buffer=""):
    """Met à jour l'écran ADMIN : Version Design Épuré (Minimaliste)."""
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT sender_role, message, created_at FROM ticket_messages WHERE ticket_id=? ORDER BY created_at ASC", (ticket_id,))
    rows = cur.fetchall()
    con.close()
    
    header = f"🔐 **Admin • #{ticket_id}**\n\n"
    footer = f"\n\n✎ `{current_buffer}█`"
    
    reserved_space = len(header) + len(footer) + 100
    available_history = 4096 - reserved_space
    if available_history < 500: available_history = 500

    history_txt = ""
    for role, msg, date in rows:
        if role == 'user':
            line = f"👤 **Client :** {msg}\n"
        else:
            line = f"👉 **Moi :** {msg}\n"
        history_txt += line
    
    if not history_txt: history_txt = "_(Historique vide)_"

    if len(history_txt) > available_history:
        history_txt = "..." + history_txt[-available_history:]

    full_text = header + history_txt + footer

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=full_text, 
            reply_markup=get_virtual_keyboard('letters', prefix="adm_vkey"), 
            parse_mode="Markdown"
        )
    except: pass

# --- LOGIQUE ADMIN RÉPONSE ---
async def admin_reply_start_virtual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    try:
        # Format: adm_ticket_rep_{tid}_{uid}
        parts = q.data.split("_")
        tid = parts[3]
    except:
        await q.message.reply_text("❌ Erreur ID Ticket.")
        return

    context.user_data['current_ticket_id'] = tid
    context.user_data['admin_buffer'] = ""
    
    # Affiche le dashboard complet dès le début
    await refresh_admin_dashboard(context, q.message.chat_id, q.message.message_id, tid, "")
    
    return tickets.ADMIN_TICKET_REPLY

async def admin_handle_virtual_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer()
    except: pass
    
    try: action = q.data.split(":")[1]
    except: return tickets.ADMIN_TICKET_REPLY

    curr = context.user_data.get('admin_buffer', "")
    tid = context.user_data.get('current_ticket_id')
    
    if not tid:
        await q.edit_message_text("❌ Erreur : ID Ticket perdu.")
        return ConversationHandler.END

    # --- RETOUR : ON RECRÉE EXACTEMENT "admin_view_ticket" ---
    if action == "CANCEL":
        context.user_data['admin_buffer'] = ""
        
        con = sqlite3.connect(DB_NAME)
        # On utilise la même requête que dans votre fonction admin_view_ticket
        row = con.execute("SELECT user_id, username, category, message, created_at FROM support_tickets WHERE ticket_id=?", (tid,)).fetchone()
        con.close()
        
        if row:
            uid, uname, cat, msg_content, date = row

            clean_date = str(date).strip()
            
            # 1. LE TEXTE EXACT (Copie conforme de admin_view_ticket)
            txt = (
                f"🎫 **TICKET #{tid}**\n"
                f"👤 {uname or 'Inconnu'} (`{uid}`)\n"
                f"📂 {cat}\n"
                f"📅 {clean_date}"
            )
            # 2. LES BOUTONS EXACTS (Copie conforme de admin_view_ticket)
            kb = [
                [InlineKeyboardButton("✍️ Répondre", callback_data=f"adm_ticket_rep_{tid}_{uid}")],
                [InlineKeyboardButton("🗑 Fermer", callback_data=f"adm_ticket_close_{tid}")],
                [InlineKeyboardButton("🔙 Liste", callback_data="admin_tickets_list")]
            ]
            
            try:
                await q.edit_message_text(text=txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
            except Exception as e:
                print(f"Erreur Restore Admin: {e}")
        else:
            await q.edit_message_text("❌ Ticket introuvable.")

        # On quitte la conversation pour que les boutons "Répondre" fonctionnent à nouveau
        return ConversationHandler.END

    # --- Logic de Saisie (Pas de changement ici) ---
    elif action == "DEL": curr = curr[:-1]
    elif action == "SPACE": curr += " "
    elif action == "switch_num": 
        await q.edit_message_reply_markup(reply_markup=get_virtual_keyboard('numbers', prefix="adm_vkey"))
        return tickets.ADMIN_TICKET_REPLY
    elif action == "switch_let": 
        await q.edit_message_reply_markup(reply_markup=get_virtual_keyboard('letters', prefix="adm_vkey"))
        return tickets.ADMIN_TICKET_REPLY
    elif action == "SEND": 
        return await admin_finalize_reply(update, context, curr)
    else: 
        curr += action
    
    context.user_data['admin_buffer'] = curr
    await refresh_admin_dashboard(context, q.message.chat_id, q.message.message_id, tid, curr)
    
    return tickets.ADMIN_TICKET_REPLY

async def admin_finalize_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_text):
    q = update.callback_query
    tid = context.user_data.get('current_ticket_id')
    
    if not reply_text.strip(): return tickets.ADMIN_TICKET_REPLY

    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    
    cur.execute("SELECT user_id, dashboard_msg_id FROM support_tickets WHERE ticket_id=?", (tid,))
    row = cur.fetchone()
    
    if row:
        user_id, dashboard_mid = row
        
        # 1. Sauvegarde BD
        cur.execute("INSERT INTO ticket_messages (ticket_id, sender_role, message) VALUES (?, 'admin', ?)", (tid, reply_text))
        cur.execute("UPDATE support_tickets SET status='replied' WHERE ticket_id=?", (tid,))
        con.commit()
        
        # 2. Mise à jour LIVE de l'écran CLIENT (si son écran est ouvert)
        if dashboard_mid:
            try:
                await refresh_ticket_dashboard(context, int(user_id), int(dashboard_mid), tid, "")
            except: pass 

    con.close()
    
    # 3. Mise à jour LIVE de l'écran ADMIN (On reste sur le terminal)
    context.user_data['admin_buffer'] = "" # On vide la saisie
    
    # On rafraîchit l'écran admin : Le message envoyé apparaîtra maintenant dans l'historique (en haut)
    await refresh_admin_dashboard(context, q.message.chat_id, q.message.message_id, tid, "")

    # On reste en mode conversation pour pouvoir envoyer un autre message
    return tickets.ADMIN_TICKET_REPLY

async def admin_close_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tid = query.data.split("_")[-1]
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    # Le statut doit être exactement 'closed'
    cur.execute("UPDATE support_tickets SET status='closed' WHERE ticket_id=?", (tid,))
    con.commit()
    con.close()
    
    await query.edit_message_text(f"✅ Ticket #{tid} fermé avec succès.")

async def admin_repost_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tid = q.data.split("_")[-1]
    
    # Petit effet de chargement pour confirmer le clic
    await q.answer("🚀 Envoi en cours vers le canal...")

    con = sqlite3.connect(DB_NAME)
    row = con.execute("SELECT message, user_id, username FROM support_tickets WHERE ticket_id=?", (tid,)).fetchone()
    con.close()

    if row:
        admin_txt, user_id, username = row
        
        # On construit le message de mise à jour
        header = f"🔄 **MISE À JOUR COMMANDE #{tid}**\nClient: @{username} (`{user_id}`)\n\n"
        full_msg = header + admin_txt
        
        try:
            # Envoi au canal défini dans tickets.py
            await context.bot.send_message(
                chat_id=tickets.CHANNEL_LOGS, 
                text=full_msg,
                parse_mode="Markdown"
            )
            await q.message.reply_text(f"✅ **Succès !**\nLa commande #{tid} a été renvoyée au canal de production.")
        except Exception as e:
            await q.message.reply_text(f"❌ Erreur lors de l'envoi : {e}")
    else:
        await q.message.reply_text("❌ Commande introuvable en base de données.")

async def admin_all_orders_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    con = sqlite3.connect(DB_NAME)
    # On récupère les 10 dernières commandes de type ID ou PHYSICAL
    rows = con.execute("""
        SELECT ticket_id, username, category, status 
        FROM support_tickets 
        WHERE category LIKE '%ID%' OR category LIKE '%PHYSICAL%'
        ORDER BY ticket_id DESC LIMIT 10
    """).fetchall()
    con.close()

    if not rows:
        await query.edit_message_text(
            "📦 **Logistique**\nAucune commande récente trouvée.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Retour", callback_data="admin_menu")]])
        )
        return

    kb = []
    for tid, uname, cat, status in rows:
        # Icone selon le statut
        icon = "✅" if status == 'closed' else "🟡"
        # Bouton : [ 🟡 #1024 - Jean (QC ID) ]
        label = f"{icon} #{tid} - {uname} ({cat})"
        kb.append([InlineKeyboardButton(label, callback_data=f"adm_ord_view_{tid}")])
    
    kb.append([InlineKeyboardButton("🔙 Menu Admin", callback_data="admin_menu")])
    
    await query.edit_message_text(
        "📦 **LOGISTIQUE : COMMANDES ID's**\nSélectionnez une commande pour voir les détails et actions.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

async def admin_view_order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # On extrait l'ID (ex: adm_ord_view_123 -> 123)
    tid = query.data.split("_")[-1]

    con = sqlite3.connect(DB_NAME)
    row = con.execute("SELECT message, username, user_id, category FROM support_tickets WHERE ticket_id=?", (tid,)).fetchone()
    con.close()

    if not row:
        await query.message.reply_text("❌ Erreur : Commande introuvable.")
        return

    admin_txt, username, user_id, category = row

    kb = [
        # --- LE BOUTON STRATÉGIQUE ---
        [InlineKeyboardButton("🚀 Renvoyer au Channel", callback_data=f"adm_repost_{tid}")],
        # -----------------------------
        [
            InlineKeyboardButton("📝 Modifier Info", callback_data=f"adm_edit_menu_{tid}"),
            InlineKeyboardButton("🗑 Supprimer", callback_data=f"adm_del_order_{tid}")
        ],
        [InlineKeyboardButton("🔙 Liste des Commandes", callback_data="admin_all_orders")]
    ]

    await query.edit_message_text(
        text=f"📦 **DÉTAIL COMMANDE #{tid}**\nClient: @{username} (`{user_id}`)\n\n{admin_txt}",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

async def ticket_view_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tid = q.data.split(":")[1]
    
    con = sqlite3.connect(DB_NAME); cur = con.cursor()
    
    # ON RÉCUPÈRE TOUT LE FIL (User + Admin)
    cur.execute("SELECT sender_role, message FROM ticket_messages WHERE ticket_id=? ORDER BY created_at ASC", (tid,))
    rows = cur.fetchall()
    
    # On récupère les infos du ticket pour le titre
    cur.execute("SELECT category FROM support_tickets WHERE ticket_id=?", (tid,))
    res = cur.fetchone()
    cat = res[0] if res else "Support"
    
    # Marquer comme lu
    cur.execute("UPDATE support_tickets SET status='read' WHERE ticket_id=? AND status='replied'", (tid,))
    con.commit(); con.close()

    # Construction du texte du chat
    history_text = f"📨 **FIL DU TICKET #{tid}**\n📂 Sujet : `{cat}`\n━━━━━━━━━━━━━━━━━━\n\n"
    
    if not rows:
        history_text += "_Aucun message dans l'historique._"
    else:
        for role, msg in rows:
            author = "👤 **VOUS :**" if role == 'user' else "👮‍♂️ **SUPPORT :**"
            history_text += f"{author}\n{msg}\n\n"
    
    history_text += "━━━━━━━━━━━━━━━━━━"
    
    kb = [[InlineKeyboardButton("⬅️ Retour", callback_data="support")]]
    await q.edit_message_text(text=history_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def id_confirm_addr_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la confirmation de l'adresse avec parsing intelligent et nettoyage."""
    q = update.callback_query
    
    # --- GESTION DES BOUTONS ---
    if q:
        await q.answer()
        data = q.data

        if data == "addr_retry":
            await clean_chat(update, context)
            prev = ID_ASK_DOB if context.user_data.get('id_category') != 'document' else ID_ASK_LASTNAME
            kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{prev}")]]
            m = await context.bot.send_message(chat_id=update.effective_chat.id, text="📍 **Adresse (1/3)**\nEntrez le **Numéro et la Rue** :", reply_markup=InlineKeyboardMarkup(kb))
            context.user_data['cleanup_ids'] = [m.message_id]
            return ID_ASK_STREET

        if data == "addr_manual":
            # On supprime le menu de choix pour afficher la demande de saisie
            try: await q.message.delete()
            except: pass
            
            m = await context.bot.send_message(chat_id=update.effective_chat.id, text="✍️ **SAISIE MANUELLE**\n\nVeuillez écrire l'adresse complète :\n_(Ex: 123 Rue de la Paix, Apt 4, Montréal, QC, H1A 1A1)_")
            context.user_data['cleanup_ids'] = [m.message_id] # On track ce nouveau message
            return ID_CONFIRM_ADDR 

        # Choix d'une suggestion Postes Canada
        try:
            if data.startswith("addr_pick:"):
                idx = int(data.split(":")[1])
                suggestions = context.user_data.get('addr_suggestions', [])
                if suggestions and 0 <= idx < len(suggestions):
                    full_addr = suggestions[idx]
                    context.user_data['form_address'] = full_addr
                    
                    # --- PARSING INTELLIGENT ---
                    parts = [p.strip() for p in full_addr.split(',')]
                    
                    if len(parts) >= 3:
                        context.user_data['addr_zip'] = parts[-1] 
                        candidate_city = parts[-3]
                        
                        # Fix si la ville commence par un chiffre (c'est encore la rue)
                        if candidate_city and candidate_city[0].isdigit():
                             candidate_city = parts[-2]
                             if len(candidate_city) == 2: candidate_city = parts[-3]

                        context.user_data['addr_city'] = candidate_city
                        context.user_data['addr_street'] = parts[0]
                    
                    # 🔥 C'EST ICI LA CORRECTION 🔥
                    # On supprime le message "Choisissez la version officielle"
                    try: await q.message.delete()
                    except: pass
                    # On vide la liste car on vient de supprimer le message manuellement
                    context.user_data['cleanup_ids'] = []

                else:
                    # Fallback
                    raw = f"{context.user_data.get('addr_street')}, {context.user_data.get('addr_city')}"
                    context.user_data['form_address'] = raw
                    try: await q.message.delete()
                    except: pass
        except Exception as e:
            print(f"Erreur Parsing Adresse: {e}")

    # --- GESTION DU TEXTE MANUEL ---
    elif update.message:
        manual_addr = update.message.text.strip()
        context.user_data['form_address'] = manual_addr
        parts = manual_addr.split(',')
        if len(parts) > 0: context.user_data['addr_street'] = parts[0].strip()
        if len(parts) > 1: context.user_data['addr_city'] = parts[1].strip()
        if len(parts) > 2: context.user_data['addr_zip'] = parts[-1].strip()
        
        await clean_chat(update, context)

    # --- SUITE DU FORMULAIRE ---
    if context.user_data.get('id_category') == 'document':
        kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_CONFIRM_ADDR}")]]
        m = await context.bot.send_message(chat_id=update.effective_chat.id, text="🏢 **Nom de l'Employeur** ?", reply_markup=InlineKeyboardMarkup(kb))
        context.user_data['cleanup_ids'] = [m.message_id]
        return ID_ASK_DOC_EMPLOYER
    else:
        kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_CONFIRM_ADDR}")]]
        m = await context.bot.send_message(chat_id=update.effective_chat.id, text="📅 **Date d'Émission (4d) ?**\n(Ex: 15/01/2023 ou Aujourd'hui)", reply_markup=InlineKeyboardMarkup(kb))
        context.user_data['cleanup_ids'] = [m.message_id]
        return ID_ASK_ISSUE

async def id_handle_dl_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère le choix entre SAAQ Auto et Saisie Manuelle."""
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    mode = q.data.split(":")[1]
    
    if mode == "manual":
        await clean_chat(update, context)
        kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_EXPIRY}")]]
        m = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✍️ **SAISIE MANUELLE**\n\nVeuillez entrer les **2 derniers chiffres** de votre permis (ex: 03) :",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        context.user_data['cleanup_ids'] = [m.message_id]
        return ID_ASK_DL_NUM

    if mode == "saaq":
        base_code = str(context.user_data.get('dl_base_code', '')).upper().strip()
        status_msg = await q.edit_message_text(f"⏳ **Recherche SAAQ en cours...**\nBase : `{base_code}`\n\n_Validation en cours (3m 30s max)..._", reply_markup=None, parse_mode="Markdown")
        
        batch_id = f"{user_id}:ORDER:{int(time.time())}"
        batch_runs[batch_id] = { "total": 1, "resolved": 0, "notified": False, "lock": asyncio.Lock() }
        asyncio.create_task(launch_parallel_calls(base_code, user_id, num_calls=5, fullname="Commande ID", formatted=base_code, batch_id=batch_id))
        
        final_dl_found = None
        for i in range(300):
            await asyncio.sleep(2)
            with sqlite3.connect(DB_NAME) as con:
                row = con.execute("SELECT permis FROM verifications WHERE user_id=? AND status='valide' AND length(replace(permis, '-', '')) = 13 AND created_at > datetime('now', '-5 minutes') ORDER BY id DESC LIMIT 1", (str(user_id),)).fetchone()
                if row:
                    final_dl_found = row[0]
                    break
        
        if final_dl_found:
            context.user_data['form_dl_number'] = final_dl_found
            await status_msg.edit_text(f"✅ **Permis SAAQ détecté !**\nNuméro : `{final_dl_found}`", parse_mode="Markdown")
            await asyncio.sleep(1.5)
            return await ask_ref_number_interactive(update, context)
        else:
            await status_msg.edit_text("❌ **Délai dépassé.**\nVeuillez entrer les chiffres manuellement :")
            return ID_ASK_DL_NUM

async def id_save_dl_manual_digits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la saisie manuelle des 2 chiffres (ou complet) et PASSE À LA RÉFÉRENCE."""
    user_input = update.message.text.strip()
    
    # Nettoyage de l'écran (Benjamin / Chiffres tapez)
    await clean_chat(update, context)
    
    # Récupération de la base calculée précédemment
    base = context.user_data.get('dl_base_code', '')
    
    # Si l'utilisateur entre 2 chiffres, on complète la base
    if len(user_input) == 2 and user_input.isdigit():
        final_dl = f"{base[:5]}-{base[5:]}-{user_input}"
    # Si l'utilisateur réécrit tout le permis
    elif len(user_input) > 10:
        final_dl = user_input.upper()
    else:
        # Erreur de saisie
        kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_EXPIRY}")]]
        m = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ **Format invalide.** Entrez les 2 derniers chiffres (ex: 03) ou le numéro complet.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        context.user_data['cleanup_ids'] = [m.message_id]
        return ID_ASK_DL_NUM

    context.user_data['form_dl_number'] = final_dl
    
    # 🔥 LA CORRECTION EST ICI : On appelle la fonction suivante
    return await ask_ref_number_interactive(update, context)

async def ask_ref_number_interactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le choix du numéro de référence avec le bouton Retour en bas de liste."""
    proposed_ref = generate_ref_number()
    context.user_data['temp_proposed_ref'] = proposed_ref

    text = (
        "🔢 **NUMÉRO DE RÉFÉRENCE**\n\n"
        f"Référence proposée : `{proposed_ref}`\n\n"
        "Souhaitez-vous utiliser ce numéro ou en générer un autre ?"
    )

    # Le bouton Retour est maintenant placé à la FIN de la liste kb
    kb = [
        [InlineKeyboardButton("✅ Utiliser celui-ci", callback_data="ref_action:accept")],
        [InlineKeyboardButton("🔄 Un autre", callback_data="ref_action:next")],
        [InlineKeyboardButton("✍️ Saisie manuelle", callback_data="ref_action:manual")],
        [InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:ID_ASK_DL_NUM")] # Tout en bas
    ]
    
    reply_markup = InlineKeyboardMarkup(kb)

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except:
            m = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
            context.user_data.setdefault('cleanup_ids', []).append(m.message_id)
    else:
        m = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
        context.user_data.setdefault('cleanup_ids', []).append(m.message_id)
    
    return ID_ASK_REF_NUM

async def handle_ref_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère les boutons du sélecteur de numéro de référence interactif."""
    q = update.callback_query
    await q.answer()
    
    action = q.data.split(":")[1]

    if action == "next":
        # Génère et affiche une nouvelle proposition
        return await ask_ref_number_interactive(update, context)

    elif action == "accept":
        # Enregistre le numéro actuellement proposé
        final_ref = context.user_data.get('temp_proposed_ref')
        context.user_data['form_ref_number'] = final_ref
        
        # Nettoyage et passage à l'étape suivante (Sexe)
        await clean_chat(update, context)
        
        kb_sex = [
            [InlineKeyboardButton("Homme", callback_data="sex:1"), 
             InlineKeyboardButton("Femme", callback_data="sex:2")],
            [InlineKeyboardButton("Non spécifié (X)", callback_data="sex:9")],
            [InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_REF_NUM}")]
        ]
        
        m = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="👤 **Sexe / Genre ?**",
            reply_markup=InlineKeyboardMarkup(kb_sex),
            parse_mode="Markdown"
        )
        context.user_data['cleanup_ids'] = [m.message_id]
        return ID_ASK_SEX

    elif action == "manual":
        # Switch en mode saisie manuelle au clavier
        await q.edit_message_text("✍️ Veuillez entrer votre numéro de référence manuellement :")
        return ID_ASK_REF_NUM

async def id_save_ref_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sauvegarde le numéro de référence écrit manuellement et passe au Sexe."""
    val = update.message.text.strip().upper()
    
    # Nettoyage de l'écran (Benjamin / Numéro écrit)
    await clean_chat(update, context)
    
    # Sauvegarde dans la session
    context.user_data['form_ref_number'] = val
    
    # Préparation de l'étape suivante : Sexe
    kb_sex = [
        [InlineKeyboardButton("Homme", callback_data="sex:1"), 
         InlineKeyboardButton("Femme", callback_data="sex:2")],
        [InlineKeyboardButton("Non spécifié (X)", callback_data="sex:9")],
        [InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_REF_NUM}")]
    ]
    
    m = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="👤 **Sexe / Genre ?**",
        reply_markup=InlineKeyboardMarkup(kb_sex),
        parse_mode="Markdown"
    )
    
    # On mémorise la question pour le prochain nettoyage
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_SEX

async def id_save_sex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enregistre le choix du sexe et demande la taille."""
    q = update.callback_query
    await q.answer()
    
    # Enregistrement (ex: sex:1 -> 1)
    val = q.data.split(":")[1]
    context.user_data['form_sex'] = val
    
    # Nettoyage
    await clean_chat(update, context)
    
    # Étape suivante : Taille
    kb = [[InlineKeyboardButton("🔙 Retour", callback_data=f"form_back:{ID_ASK_SEX}")]]
    m = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="📏 **Quelle est votre taille ?**\n(En cm, ex: 175)",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_HEIGHT

async def id_save_height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enregistre la taille et déclenche la sélection de la couleur des yeux."""
    val = update.message.text.strip()
    
    # Validation de la taille
    if not val.isdigit():
        await update.message.reply_text("⚠️ Please enter a valid number in cm (e.g., 180).")
        return ID_ASK_HEIGHT
        
    context.user_data['form_height'] = val
    
    # Nettoyage du message de l'utilisateur
    await clean_chat(update, context)
    
    # On appelle directement ask_eye_color pour afficher le menu des yeux
    return await ask_eye_color(update, context)

async def ask_eye_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Demande la couleur des yeux avec codes 3 lettres anglais."""
    chat_id = update.effective_chat.id
    text = "👁 **EYE COLOR**\n\nSelect your eye color (Official 3-letter codes):"
    kb = [
        [InlineKeyboardButton("Brown (BRO)", callback_data="eyes:BRO"), InlineKeyboardButton("Blue (BLU)", callback_data="eyes:BLU")],
        [InlineKeyboardButton("Grey (GRY)", callback_data="eyes:GRY"), InlineKeyboardButton("Green (GRN)", callback_data="eyes:GRN")],
        [InlineKeyboardButton("Hazel (HZL)", callback_data="eyes:HZL"), InlineKeyboardButton("Black (BLK)", callback_data="eyes:BLK")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"form_back:{ID_ASK_HEIGHT}")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        m = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        context.user_data['cleanup_ids'] = [m.message_id]
    return ID_ASK_EYES

id_docs_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(id_menu_entry, pattern="^id_menu_entry$")],
    states={
        ID_CAT_VIEW: [CallbackQueryHandler(id_show_category, pattern="^id_cat:")],
        ID_PROD_VIEW: [
            CallbackQueryHandler(id_view_product, pattern="^id_view:"),
            CallbackQueryHandler(id_start_buy, pattern="^id_buy:"),
            CallbackQueryHandler(id_menu_entry, pattern="^id_menu_entry$")
        ],
        ID_ASK_QTY: [
            CallbackQueryHandler(id_handle_qty_buttons, pattern="^qty_"),
            CallbackQueryHandler(id_save_qty, pattern="^qty_confirm$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, id_save_qty)
        ],
        ID_ASK_NAME: [CallbackQueryHandler(id_form_back, pattern="^form_back:"), MessageHandler(filters.TEXT, id_save_firstname)],
        ID_ASK_LASTNAME: [CallbackQueryHandler(id_form_back, pattern="^form_back:"), MessageHandler(filters.TEXT, id_save_lastname)],
        ID_ASK_DOB: [CallbackQueryHandler(id_form_back, pattern="^form_back:"), MessageHandler(filters.TEXT, id_save_dob)],
        ID_ASK_STREET: [CallbackQueryHandler(id_form_back, pattern="^form_back:"), MessageHandler(filters.TEXT, id_save_street)],
        ID_ASK_CITY: [CallbackQueryHandler(id_form_back, pattern="^form_back:"), MessageHandler(filters.TEXT, id_save_city)],
        ID_ASK_ZIP: [CallbackQueryHandler(id_form_back, pattern="^form_back:"), MessageHandler(filters.TEXT, id_save_zip)],
        ID_CONFIRM_ADDR: [CallbackQueryHandler(id_form_back, pattern="^form_back:"), CallbackQueryHandler(id_confirm_addr_handler, pattern="^addr_")],
        ID_ASK_ISSUE: [CallbackQueryHandler(id_form_back, pattern="^form_back:"), MessageHandler(filters.TEXT, id_save_issue)],
        ID_ASK_EXPIRY: [CallbackQueryHandler(id_form_back, pattern="^form_back:"), MessageHandler(filters.TEXT, id_save_expiry)],
        
        # --- ETAT DL NUM CORRIGÉ POUR LE MANUEL ---
        ID_ASK_DL_NUM: [
            CallbackQueryHandler(id_form_back, pattern="^form_back:"),
            CallbackQueryHandler(id_handle_dl_method, pattern="^dl_mode:"), 
            MessageHandler(filters.TEXT & ~filters.COMMAND, id_save_dl_manual_digits)
        ],
        
        ID_ASK_REF_NUM: [
            CallbackQueryHandler(id_form_back, pattern="^form_back:"),
            CallbackQueryHandler(handle_ref_callback, pattern="^ref_action:"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, id_save_ref_num)
        ],
        ID_ASK_SEX: [CallbackQueryHandler(id_form_back, pattern="^form_back:"), CallbackQueryHandler(id_save_sex, pattern="^sex:")],
        ID_ASK_HEIGHT: [CallbackQueryHandler(id_form_back, pattern="^form_back:"), MessageHandler(filters.TEXT, id_save_height)],
        
        # --- ETAT EYES CORRIGÉ ---
        ID_ASK_EYES: [
            CallbackQueryHandler(id_form_back, pattern="^form_back:"), 
            CallbackQueryHandler(id_save_eyes, pattern="^eyes:") # Pattern matching avec ask_eye_color
        ],
        
        ID_ASK_PHOTO: [CallbackQueryHandler(id_form_back, pattern="^form_back:"), MessageHandler(filters.PHOTO, id_save_photo)],
        
        # --- ETAT RÉSUMÉ AVEC ÉDITION ---
        ID_CONFIRM_SUMMARY: [
            CallbackQueryHandler(id_finalize_order, pattern="^confirm_gen$"), 
            CallbackQueryHandler(id_open_edit_menu, pattern="^edit_open_menu$"), # <-- Le bouton Modifier
            CallbackQueryHandler(id_menu_entry, pattern="^id_menu_entry$")       # <-- Le bouton Annuler
        ],
        
        # --- NOUVEAUX ÉTATS D'ÉDITION ---
        ID_EDIT_MENU: [
            CallbackQueryHandler(id_handle_edit_choice, pattern="^do_edit:"), 
            CallbackQueryHandler(id_show_summary, pattern="^back_to_summary$")
        ],
        ID_EDIT_INPUT: [
            MessageHandler(filters.TEXT, id_receive_new_value), 
            CallbackQueryHandler(id_open_edit_menu, pattern="^cancel_edit_input$")
        ],
    },
    fallbacks=[CommandHandler("start", goto_menu), CallbackQueryHandler(goto_menu, pattern="^menu_accueil$")],
    name="id_docs_conversation"
)


# ==============================================================================
# 🧩 SYSTÈME DE VERROUILLAGE AUTO (INACTIVITÉ)
# ==============================================================================

async def enforcement_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Middleware de sécurité. Gère le verrouillage et empêche les doubles clics.
    """
    if not update.effective_user:
        return

    # 1. Mise à jour de l'activité
    context.user_data['last_active'] = time.time()

    # 2. Vérification du verrouillage
    if context.user_data.get('is_locked', False):
        
        # Est-ce une tentative de PIN ?
        is_pin_attempt = False
        if update.callback_query and update.callback_query.data.startswith("pin_"):
            is_pin_attempt = True

        if is_pin_attempt:
            # --- PROTECTION ANTI-DOUBLON ---
            # Si cette update a déjà été traitée par un autre handler, on arrête.
            if getattr(context, "pin_handled_flag", False):
                raise ApplicationHandlerStop
            
            # Sinon, on marque comme traité
            context.pin_handled_flag = True
            # -------------------------------

            # On appelle le handler du PIN manuellement
            res = await auth_pin_handler(update, context)
            
            # Si le code est bon (END), on déverrouille
            if res == ConversationHandler.END:
                context.user_data['is_locked'] = False
                # On arrête tout pour ne pas déclencher d'autres menus
                raise ApplicationHandlerStop
            else:
                # Code faux ou incomplet, on arrête la propagation
                raise ApplicationHandlerStop

        else:
            # Si l'utilisateur clique ailleurs (Menu, etc.) alors qu'il est bloqué
            if update.callback_query:
                try: await update.callback_query.answer("🔒 Terminal Verrouillé. Entrez le PIN.", show_alert=True)
                except: pass
            
            try: await update.message.delete()
            except: pass
            
            # On bloque tout le reste
            raise ApplicationHandlerStop
        

async def check_inactivity_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Tâche de fond qui tourne chaque minute.
    Vérifie l'inactivité selon le réglage perso de chaque user.
    """
    now = time.time()
    
    if not context.application.user_data:
        return

    for user_id, data in context.application.user_data.items():
        # Si déjà verrouillé, on passe
        if data.get('is_locked'):
            continue
            
        last = data.get('last_active', 0)
        if last == 0: continue

        # --- RECUPERATION DU TIMEOUT PERSO ---
        # On regarde en mémoire, sinon on charge depuis la DB (une seule fois)
        timeout = data.get('inactivity_limit')
        if not timeout:
            try:
                con = sqlite3.connect(DB_NAME)
                cur = con.cursor()
                cur.execute("SELECT inactivity_timeout FROM users WHERE telegram_id=?", (str(user_id),))
                row = cur.fetchone()
                con.close()
                timeout = int(row[0]) if row and row[0] else 300 # Défaut 5 min
            except:
                timeout = 300
            data['inactivity_limit'] = timeout # Mise en cache mémoire
        # -------------------------------------
        
        # Si inactif depuis plus que LEUR temps défini
        if (now - last) > timeout:
            data['is_locked'] = True
            
            # ... (LE RESTE DU CODE DE NETTOYAGE NE CHANGE PAS) ...
            # (Copie le bloc de nettoyage msgs_to_delete existant ici)
            
            # Action : On affiche le PIN
            try:
                # Récup pseudo pour l'affichage
                display_name = data.get('username', 'Utilisateur') 
                
                await context.bot.send_message(
                    chat_id=int(user_id),
                    text=f"🔒 **VERROUILLAGE AUTO**\nInactivité > {timeout//60} min.\n\nUtilisateur : {display_name}\nPIN : `____`",
                    reply_markup=get_pin_keyboard(),
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"[AUTO-LOCK ERROR] {e}")


async def admin_clean_ghost_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Sécurité Admin
    if str(update.effective_user.id) not in ADMIN_IDS:
        return

    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    
    # On compte d'abord combien on va en supprimer
    # Critères : username NULL ou vide ou 'None' ET solde = 0 (pour ne pas supprimer qqun qui a de l'argent)
    cur.execute("""
        SELECT COUNT(*) FROM users 
        WHERE (username IS NULL OR username = '' OR username = 'None') 
        AND balance = 0
    """)
    count = cur.fetchone()[0]
    
    if count == 0:
        await update.message.reply_text("✅ Aucun utilisateur fantôme à supprimer.")
        con.close()
        return

    # Suppression effective
    cur.execute("""
        DELETE FROM users 
        WHERE (username IS NULL OR username = '' OR username = 'None') 
        AND balance = 0
    """)
    con.commit()
    con.close()
    
    await update.message.reply_text(f"🗑️ **Nettoyage terminé !**\n{count} utilisateurs fantômes (solde 0$) ont été supprimés.")

# 1. GESTIONNAIRE DES TICKETS (Correction: "tickets." retiré devant la fonction)
admin_ticket_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(admin_reply_start_virtual, pattern="^adm_ticket_rep_")], 
    states={
        tickets.ADMIN_TICKET_REPLY: [
            CallbackQueryHandler(admin_handle_virtual_click, pattern="^adm_vkey:"),
            CallbackQueryHandler(admin_menu, pattern="^admin_menu$")
        ]
    },
    fallbacks=[CallbackQueryHandler(admin_menu, pattern="^admin_menu$"), CommandHandler("start", start)]
)

# 2. ROUTEUR PRINCIPAL (Start, Vérif, Outils, Compte)
conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("start", start),
        CallbackQueryHandler(start_verifier_main, pattern="^start_verifier_main$"),
        CallbackQueryHandler(show_tools_menu, pattern="^section_tools$"),
        CallbackQueryHandler(auth_import_start, pattern="^auth_import_start$"),
        CallbackQueryHandler(auth_create_start, pattern="^auth_create$"),
        CallbackQueryHandler(ticket_create_start, pattern="^ticket_create_start$"),
        CallbackQueryHandler(tickets.start_support, pattern="^support$"),
        CallbackQueryHandler(account_menu, pattern="^account_menu$"),
        CallbackQueryHandler(admin_menu, pattern="^admin_menu$"),
        CallbackQueryHandler(auth_logout, pattern="^auth_logout$"),
        CallbackQueryHandler(auth_lock_only, pattern="^auth_lock_only$"),
        CallbackQueryHandler(auth_switch_account, pattern="^auth_switch_account$"),
    ],
    states={
        # --- AUTHENTIFICATION ---
        ID_AUTH_WAIT_PIN_CREATE: [MessageHandler(filters.TEXT, auth_create_pin_save)],
        ID_AUTH_WAIT_PIN_LOGIN: [CallbackQueryHandler(auth_pin_handler, pattern="^pin_")],
        ID_AUTH_WAIT_SEED: [MessageHandler(filters.TEXT, auth_import_verify)],
        
        # --- BOÎTE À OUTILS ---
        SELECT_TOOL: [
            CallbackQueryHandler(show_tools_menu, pattern="^section_tools$"),
            CallbackQueryHandler(start_verifier_main, pattern="^start_verifier_main$"),
            CallbackQueryHandler(tool_ask_hlr, pattern="^tool_hlr$"),
            CallbackQueryHandler(show_sms_menu, pattern="^tool_5sim$"),
            CallbackQueryHandler(tool_placeholder, pattern="^tool_cc_checker$"),
            CallbackQueryHandler(acc_ask_pin, pattern="^acc_change_pin$"),
            CallbackQueryHandler(acc_timeout_menu, pattern="^acc_timeout_menu$"),
            CallbackQueryHandler(acc_ask_user, pattern="^acc_set_user$"),
            CallbackQueryHandler(acc_ask_jabber, pattern="^acc_set_jabber$"),
            CallbackQueryHandler(acc_reset_ask, pattern="^acc_reset_seed$"),
            CallbackQueryHandler(handle_buy_sms, pattern="^buy_sms:"), 
            CallbackQueryHandler(sms_control_callback, pattern="^sms_ban_"),
            CallbackQueryHandler(acc_set_timeout, pattern="^set_timeout_"),
            CallbackQueryHandler(account_menu, pattern="^account_menu$"),
            CallbackQueryHandler(goto_menu, pattern="^menu_accueil$")
        ],
        WAIT_HLR_NUMBER: [MessageHandler(filters.TEXT, tool_process_hlr)],
        
        # --- GESTION COMPTE ---
        ACC_WAIT_NEW_PIN: [MessageHandler(filters.TEXT, acc_save_pin)],
        ACC_WAIT_USERNAME: [MessageHandler(filters.TEXT, acc_save_user)],
        ACC_WAIT_JABBER: [MessageHandler(filters.TEXT, acc_save_jabber)],
        ACC_WAIT_RESET_CONFIRM: [CallbackQueryHandler(acc_reset_confirm, pattern="^confirm_reset_seed$")],

        # --- VÉRIFICATION PERMIS (MULTI & UNIQUE) ---
        ASK_QTY: [MessageHandler(filters.TEXT, ask_qty)],
        ASK_MODE: [
            CallbackQueryHandler(choose_mode_manual, pattern="^mode_manual$"),
            CallbackQueryHandler(choose_mode_csv, pattern="^mode_csv$")
        ],
        MANUAL_PRENOM: [MessageHandler(filters.TEXT, manual_receive_prenom)],
        MANUAL_NOM: [MessageHandler(filters.TEXT, manual_receive_nom)],
        MANUAL_DATE: [MessageHandler(filters.TEXT, manual_receive_date)],
        CSV_WAIT: [MessageHandler(filters.Document.ALL, csv_receive_file)],
        BULK_CONFIRM: [MessageHandler(filters.TEXT, bulk_confirm)],
        
        ASK_PRENOM: [MessageHandler(filters.TEXT, receive_prenom)],
        ASK_NOM: [MessageHandler(filters.TEXT, receive_nom)],
        ASK_DATE: [MessageHandler(filters.TEXT, receive_date)],
        CONFIRM_VERIF: [MessageHandler(filters.TEXT, confirm_permis)],
        
        # --- SUPPORT CLIENT ---
        # 👇 CORRECTION ICI : Ajout de "tickets." devant save_category
        tickets.WAIT_CATEGORY: [CallbackQueryHandler(tickets.save_category, pattern="^ticket_cat:")],
        # 👇 CORRECTION ICI : Ajout de "tickets." devant handle_ticket_msg
        tickets.WAIT_TICKET_MSG: [MessageHandler(filters.TEXT, tickets.handle_ticket_msg)],
        
        TICKET_DRAFT: [
            CallbackQueryHandler(ticket_handle_virtual_click, pattern="^vkey:"),
            MessageHandler(filters.TEXT, ticket_reject_physical)
        ]
    },
    fallbacks=[
        CallbackQueryHandler(goto_menu, pattern="^menu_accueil$"),
        CommandHandler("start", goto_menu),
        CallbackQueryHandler(ticket_resume, pattern="^ticket_resume:")
    ],
    name="main_conversation"
)


def setup_all_handlers(application):
    # ====================================================
    # GROUPE -1 : SYSTÈME & DÉBUG (Priorité maximale)
    # ====================================================
    
    # 1. L'ESPION (Affiche les messages dans la console sans les arrêter)
    async def espion(update, context):
        if update.message and update.message.text:
            print(f"🕵️ ESPION: Message reçu -> '{update.message.text}'", flush=True)
            
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, espion), group=-1)
    application.add_handler(catalog_filter_conv)      # Filtre Pro's
    application.add_handler(ccs_catalog_filter_conv)  # Filtre Cc's
    
    # 5. BOUTIQUE (Actions sur produits)
    application.add_handler(CallbackQueryHandler(handle_buy_callback, pattern=r"^buy:\d+$"))
    application.add_handler(CallbackQueryHandler(cart_add_callback, pattern=r"^cart:add:\d+$"))
    application.add_handler(CallbackQueryHandler(handle_view_callback, pattern=r"^prod:view:\d+$"))
    application.add_handler(CallbackQueryHandler(cart_view_callback, pattern=r"^cart:view$"))
    application.add_handler(CallbackQueryHandler(cart_clear_callback, pattern=r"^cart:clear$"))
    application.add_handler(CallbackQueryHandler(cart_checkout_callback, pattern=r"^cart:checkout$"))
    application.add_handler(CallbackQueryHandler(cart_remove_single, pattern=r"^cart:del:\d+$"))

    # 6. AUTRES FLUX (ID Docs, Paiement, Menu Principal)
    application.add_handler(payment_conv)
    application.add_handler(id_docs_conv)
    application.add_handler(conv_handler) # Ceci gère le /start et menu_accueil

    # 7. ADMIN (Callbacks simples)
    application.add_handler(CallbackQueryHandler(tickets.admin_list_tickets, pattern="^admin_tickets_list$"))
    application.add_handler(CallbackQueryHandler(tickets.admin_view_ticket, pattern="^adm_ticket_view_"))
    application.add_handler(CallbackQueryHandler(tickets.admin_close_no_reply, pattern="^adm_ticket_close_"))
    application.add_handler(CallbackQueryHandler(admin_users, pattern="^admin_users"))
    application.add_handler(CallbackQueryHandler(admin_adjust_user, pattern="^admin_adjust_"))
    application.add_handler(CallbackQueryHandler(admin_setstatut, pattern="^admin_setstatut"))
    application.add_handler(CallbackQueryHandler(admin_userstatut, pattern="^admin_userstatut_"))
    application.add_handler(CallbackQueryHandler(admin_setstatut_final, pattern="^admin_statut_"))
    application.add_handler(CallbackQueryHandler(admin_category_menu, pattern="^admin_cat_menu:"))
    application.add_handler(CallbackQueryHandler(admin_prod_list, pattern="^admin_prod_list$"))
    application.add_handler(CallbackQueryHandler(admin_prod_del, pattern="^admin_prod_del$"))
    application.add_handler(CallbackQueryHandler(admin_prod_del_confirm, pattern="^admin_prod_del_"))
    application.add_handler(CallbackQueryHandler(admin_prod_add_start, pattern="^admin_prod_add$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_prod_add_receive))
    application.add_handler(CallbackQueryHandler(admin_all_orders_list, pattern="^admin_all_orders$"))
    application.add_handler(CallbackQueryHandler(admin_view_order_detail, pattern="^adm_ord_view_"))
    application.add_handler(CallbackQueryHandler(admin_repost_to_channel, pattern="^adm_repost_"))
    application.add_handler(CallbackQueryHandler(admin_hard_reboot, pattern="^admin_hard_reboot$"))
    application.add_handler(CallbackQueryHandler(admin_ivr_settings, pattern="^admin_ivr_settings$"))
    application.add_handler(CallbackQueryHandler(admin_ivr_change, pattern="^admin_ivr_change:"))
    

    # 8. VUE & NAVIGATION
    application.add_handler(CallbackQueryHandler(open_pagination_menu, pattern="^open_pagination_menu$"))
    application.add_handler(CallbackQueryHandler(set_pg_callback, pattern="^set_pg_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, custom_pg_receive), group=50)

    # ====================================================
    # GROUPES SECONDAIRES (Conversations Admin & Hist)
    # ====================================================
    application.add_handler(history_filter_conv, group=5)
    application.add_handler(admin_csv_conv, group=6)
    application.add_handler(admin_ivr_conv, group=7)
    application.add_handler(admin_search_conv, group=8)
    application.add_handler(admin_ticket_conv, group=9)

    # 9. HANDLER GÉNÉRIQUE (Tout clic non géré revient ici)
    application.add_handler(CallbackQueryHandler(menu_handler))
# ====================================================
#      2. FONCTIONS DE PAGINATION
# ====================================================

async def set_pg_callback(update, context):
    q = update.callback_query
    user_id = str(update.effective_user.id)
    if "custom" in q.data:
        USER_STATES[int(user_id)] = "waiting_pagination_custom"
        await q.message.edit_text("🔢 **Entrez le nombre souhaité (1-50) :**", 
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Annuler", callback_data="propro")]]), 
                                  parse_mode="Markdown")
        return
    qty = int(q.data.split("_")[2])
    set_user_pagination(user_id, qty)
    await q.answer(f"✅ Vue réglée sur {qty} !")
    await show_products(update, context, page=0)

async def custom_pg_receive(update, context):
    # Sécurité : Si pas d'utilisateur, on ignore
    if not update.effective_user:
        return
        
    user_id = update.effective_user.id
    if USER_STATES.get(user_id) == "waiting_pagination_custom":
        try:
            qty = int(update.message.text.strip())
            if 1 <= qty <= 50:
                set_user_pagination(str(user_id), qty)
                await update.message.reply_text(f"✅ Noté : {qty} par page.")
                USER_STATES.pop(user_id, None)
                await asyncio.sleep(1)
                await show_products(update, context, page=0)
            else:
                await update.message.reply_text("⚠️ Chiffre entre 1 et 50.")
        except: 
            await update.message.reply_text("⚠️ Invalide.")


def run_bot_polling():
    global bot_loop, app_telegram

    print("🤖 Bot Telegram : Initialisation...")
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot_loop = loop 
        
        # INSTANCE UNIQUE
        app_telegram = Application.builder().token(TELEGRAM_TOKEN).build()

        # Injection bot_data
        print("🔌 Connexion DB et Helpers...")
        db_conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        app_telegram.bot_data["db_conn"] = db_conn
        app_telegram.bot_data["get_user_balance"] = get_user_balance
        app_telegram.bot_data["update_user_balance"] = update_user_balance

        # Configuration des Handlers
        setup_all_handlers(app_telegram)

        # JobQueue
        if app_telegram.job_queue:
            app_telegram.job_queue.run_repeating(check_inactivity_job, interval=60, first=60)
        
        print("✅ BOT EN LIGNE !")
        app_telegram.run_polling(close_loop=False, stop_signals=False)
    except Exception as e:
        print(f"❌ Erreur critique Bot: {e}")

# ====================================================
#      4. INITIALISATION ET DÉMARRAGE
# ====================================================

def check_and_restore_default_ids():
    """Force l'apparition des produits QC/ON dans Physical ID."""
    try:
        con = sqlite3.connect(DB_NAME)
        cur = con.cursor()
        cur.execute("SELECT count(*) FROM products WHERE category='physical'")
        if cur.fetchone()[0] == 0:
            print("⚠️ Restauration des produits Physical ID...")
            prods = [
                ('physical', 'Quebec Driver License (Full)', 150.0, 'USD', 999, 'QC', 1, 'BASE: QC'),
                ('physical', 'Ontario Driver License', 150.0, 'USD', 999, 'ON', 1, 'BASE: ON')
            ]
            cur.executemany("""
                INSERT INTO products (category, title, price, currency, stock, tier, is_active, content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", prods)
            con.commit()
            print("✅ Produits Physical ID restaurés.")
        con.close()
    except Exception as e:
        print(f"⚠️ Erreur restauration : {e}")

def start_everything():
    print("📦 Préparation DB...")
    try:
        init_db() 
        tickets.patch_db_tickets()
        ensure_verifications_table()
        ensure_payment_table()
        
        # 🔥 AJOUT CRITIQUE ICI
        check_and_restore_default_ids()
        
    except Exception as e:
        print(f"⚠️ Erreur DB au démarrage: {e}")

    bot_thread = threading.Thread(target=run_bot_polling, daemon=True)
    bot_thread.start()
    print("🚀 SYSTÈME PRÊT")
 
    
if __name__ == "__main__":
    start_everything()
    # Lancement du serveur Web pour l'IVR
    app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)

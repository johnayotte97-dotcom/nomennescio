import sqlite3
import os
import re
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

# Utilisation du même chemin DB que app.py
DB_NAME = os.environ.get("DB_NAME", "/home/johnmsaaq/bot-nomen/database.db")
CHANNEL_LOGS = "-1003589564052" 

# États
WAIT_CATEGORY = 2000
WAIT_TICKET_MSG = 2001
ADMIN_TICKET_REPLY = 2005

# --- FONCTION DEBUG ---
async def send_error(update, text):
    try:
        if update.callback_query:
            await update.callback_query.message.reply_text(f"⚠️ {text}")
        else:
            await update.message.reply_text(f"⚠️ {text}")
    except: pass

def patch_db_tickets():
    """Crée la table et RÉPARE les colonnes mal nommées."""
    print("🛠️ [TICKETS] Vérification et réparation DB...")
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    try:
        # 1. On récupère les colonnes actuelles pour voir ce qui cloche
        try:
            cur.execute("PRAGMA table_info(support_tickets)")
            columns = [info[1] for info in cur.fetchall()]
            
            # CAS CRITIQUE : Si on a 'id' mais pas 'ticket_id', on renomme !
            if 'id' in columns and 'ticket_id' not in columns:
                print("🔧 Réparation : Renommage de colonne 'id' -> 'ticket_id'...")
                try:
                    cur.execute("ALTER TABLE support_tickets RENAME COLUMN id TO ticket_id")
                    print("✅ Colonne renommée avec succès.")
                except Exception as e:
                    print(f"⚠️ Echec renommage (Peut-être pas supporté) : {e}")
                    # Si le renommage échoue, on recrée la table proprement (Drastique mais nécessaire)
                    cur.execute("ALTER TABLE support_tickets RENAME TO support_tickets_old")
        except:
            pass

        # 2. Création de la table propre (si elle n'existe pas ou a été renommée)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                username TEXT,
                category TEXT,
                message TEXT,
                status TEXT DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 3. Ajout des colonnes manquantes (Patch standard)
        try:
            cur.execute("ALTER TABLE support_tickets ADD COLUMN message TEXT")
        except: pass

        try:
            cur.execute("ALTER TABLE support_tickets ADD COLUMN category TEXT")
        except: pass

        try:
            cur.execute("ALTER TABLE support_tickets ADD COLUMN username TEXT")
        except: pass
        
        con.commit()
        print("✅ [TICKETS] DB prête et corrigée.")
    except Exception as e:
        print(f"❌ [TICKETS] Erreur DB Critique: {e}")
    finally:
        con.close()

# --- PARTIE ADMIN ---

async def admin_list_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche la liste des tickets."""
    q = update.callback_query
    try: await q.answer()
    except: pass
    
    print(f"🔎 [ADMIN] Liste tickets demandée par {q.from_user.id}")

    try:
        con = sqlite3.connect(DB_NAME)
        # On sélectionne ticket_id. Grâce au patch, cette colonne existe forcément maintenant.
        rows = con.execute("SELECT ticket_id, username, category FROM support_tickets WHERE status='open' ORDER BY ticket_id DESC LIMIT 10").fetchall()
        con.close()
    except Exception as e:
        # Si ça plante encore, c'est grave, on affiche l'erreur
        print(f"❌ Erreur SQL: {e}")
        await q.message.reply_text(f"❌ Erreur SQL : {e}\n\nEssayez de redémarrer le bot pour appliquer le patch.")
        return ConversationHandler.END

    if not rows:
        kb = [[InlineKeyboardButton("⬅️ Retour Admin", callback_data="admin_menu")]]
        msg_text = "✅ **Aucun ticket ouvert.** Tout est propre."
        try: await q.message.edit_text(msg_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        except: await q.message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return ConversationHandler.END

    kb = []
    for tid, uname, cat in rows:
        label = f"#{tid} {uname} ({cat})" if uname else f"#{tid} Inconnu"
        kb.append([InlineKeyboardButton(label, callback_data=f"adm_ticket_view_{tid}")])
    
    kb.append([InlineKeyboardButton("⬅️ Retour Admin", callback_data="admin_menu")])
    
    try: await q.message.edit_text("📨 **TICKETS EN ATTENTE**", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except: await q.message.reply_text("📨 **TICKETS EN ATTENTE**", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def admin_view_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tid = int(q.data.split("_")[-1])
    
    con = sqlite3.connect(DB_NAME)
    row = con.execute("SELECT user_id, username, category, message, created_at FROM support_tickets WHERE ticket_id=?", (tid,)).fetchone()
    con.close()
    
    if not row:
        await q.message.reply_text("❌ Ticket introuvable.")
        return await admin_list_tickets(update, context)
    
    uid, uname, cat, msg_content, date = row
    # Sécurité si les champs sont vides
    uname = uname or "Inconnu"
    cat = cat or "Général"
    msg_content = msg_content or "(Pas de message)"

    txt = f"🎫 **TICKET #{tid}**\n👤 {uname} (`{uid}`)\n📂 {cat}\n📅 {date}\n\n📝 `{msg_content}`"
    
    kb = [
        [InlineKeyboardButton("✍️ Répondre", callback_data=f"adm_ticket_rep_{tid}_{uid}")],
        [InlineKeyboardButton("🗑 Fermer", callback_data=f"adm_ticket_close_{tid}")],
        [InlineKeyboardButton("🔙 Liste", callback_data="admin_tickets_list")]
    ]
    await q.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def admin_ask_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data.split("_")
    context.user_data['reply_tid'] = data[3]
    context.user_data['reply_uid'] = data[4]
    
    await q.message.reply_text(
        f"✍️ **Réponse pour le ticket #{data[3]} :**\n"
        "Écrivez votre message ci-dessous.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Annuler", callback_data="admin_tickets_list")]])
    )
    return ADMIN_TICKET_REPLY

async def admin_send_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = context.user_data.get('reply_tid')
    uid = context.user_data.get('reply_uid')
    msg_resp = update.message.text
    
    try:
        await context.bot.send_message(
            chat_id=uid,
            text=f"👨‍💻 **SUPPORT (Ticket #{tid}) :**\n━━━━━━━━━━━━━━━━━━\n{msg_resp}",
            parse_mode="Markdown"
        )
        await update.message.reply_text("✅ Réponse envoyée.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Échec envoi : {e}")
        
    con = sqlite3.connect(DB_NAME)
    con.execute("UPDATE support_tickets SET status='closed' WHERE ticket_id=?", (tid,))
    con.commit(); con.close()
    
    kb = [[InlineKeyboardButton("🔙 Liste", callback_data="admin_tickets_list")]]
    await update.message.reply_text("Ticket fermé.", reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

async def admin_close_no_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tid = q.data.split("_")[-1]
    con = sqlite3.connect(DB_NAME)
    con.execute("UPDATE support_tickets SET status='closed' WHERE ticket_id=?", (tid,))
    con.commit(); con.close()
    await admin_list_tickets(update, context)

async def admin_reply_native(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message: return
    orig = update.message.reply_to_message.text or ""
    match = re.search(r"\(`?(\d+)`?\)", orig)
    if match:
        uid = match.group(1)
        try:
            await context.bot.send_message(chat_id=uid, text=f"👨‍💻 **RÉPONSE :**\n{update.message.text}", parse_mode="Markdown")
            await update.message.set_reaction("👍")
        except Exception as e:
            await update.message.reply_text(f"❌ Erreur: {e}")

# --- PARTIE CLIENT ---
async def start_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    kb = [
        [InlineKeyboardButton("💸 Paiement", callback_data="ticket_cat:paiement"),
         InlineKeyboardButton("📦 Produit", callback_data="ticket_cat:produit")],
        [InlineKeyboardButton("ℹ️ Autre", callback_data="ticket_cat:general"),
         InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
    ]
    txt = "📞 **SUPPORT**\nChoisissez un sujet :"
    if q: await q.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else: await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return WAIT_CATEGORY

async def save_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data['ticket_cat'] = q.data.split(":")[1].upper()
    await q.message.edit_text(f"📝 **Sujet: {context.user_data['ticket_cat']}**\nDécrivez votre problème :")
    return WAIT_TICKET_MSG

async def handle_ticket_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message.text
    cat = context.user_data.get('ticket_cat', 'GENERAL')
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    # On utilise bien ticket_id ici (auto-incrémenté)
    cur.execute("INSERT INTO support_tickets (user_id, username, category, message, status) VALUES (?,?,?,?,?)",
                (str(user.id), user.username or user.first_name, cat, msg, 'open'))
    tid = cur.lastrowid
    con.commit(); con.close()
    
    await update.message.reply_text(f"✅ **Ticket #{tid} créé.**\nOn vous répond bientôt.")
    try:
        await context.bot.send_message(chat_id=CHANNEL_LOGS, text=f"🔔 **Ticket #{tid}**\n👤 {user.id}\n📝 {msg}")
    except: pass
    return ConversationHandler.END

# Fonctions dummy pour éviter ImportError si app.py les cherche
async def start_ticket_reply(u,c): pass
async def close_ticket(u,c): pass

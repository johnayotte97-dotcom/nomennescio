import sqlite3
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

# ==============================================================================
# CONFIGURATION
# ==============================================================================
# Mets ici le vrai chemin vers ta DB si nécessaire, ou laisse par défaut
import os
DB_NAME = os.environ.get("DB_NAME", "database.db")
CHANNEL_LOGS = "-1003589564052" # Ton ID de groupe pour les logs

# États de conversation
WAIT_CATEGORY = 2000
WAIT_TICKET_MSG = 2001
ADMIN_TICKET_REPLY = 2005

# ==============================================================================
# PARTIE CLIENT (SUPPORT)
# ==============================================================================

async def start_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu principal du support."""
    q = update.callback_query
    if q: await q.answer()
    
    kb = [
        [InlineKeyboardButton("💸 Problème Paiement", callback_data="ticket_cat:paiement")],
        [InlineKeyboardButton("📦 Problème Produit", callback_data="ticket_cat:produit")],
        [InlineKeyboardButton("ℹ️ Question Générale", callback_data="ticket_cat:general")],
        [InlineKeyboardButton("⬅️ Retour Accueil", callback_data="menu_accueil")]
    ]
    
    text = "📞 **SERVICE CLIENT**\n\nDe quoi s'agit-il ?"
    
    if q:
        try: await q.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        except: await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        
    return WAIT_CATEGORY

async def save_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enregistre la catégorie."""
    q = update.callback_query; await q.answer()
    context.user_data['ticket_cat'] = q.data.split(":")[1].upper()
    
    kb = [[InlineKeyboardButton("⬅️ Retour", callback_data="support")]]
    await q.message.edit_text(
        f"📝 **Sujet : {context.user_data['ticket_cat']}**\n\n"
        "Écrivez votre message maintenant :",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return WAIT_TICKET_MSG

async def handle_ticket_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enregistre le ticket en DB."""
    user = update.effective_user
    msg = update.message.text
    cat = context.user_data.get('ticket_cat', 'GENERAL')
    username = f"@{user.username}" if user.username else user.first_name

    # Enregistrement DB
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("INSERT INTO support_tickets (user_id, username, category, message, status) VALUES (?,?,?,?,?)",
                (str(user.id), username, cat, msg, 'open'))
    ticket_id = cur.lastrowid
    con.commit()
    con.close()

    # Confirmation au client
    kb = [[InlineKeyboardButton("🔒 Fermer le ticket", callback_data="ticket_close")]]
    await update.message.reply_text(
        f"✅ **Ticket #{ticket_id} créé.**\nUn administrateur vous répondra bientôt.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

    # Notif Admin dans le channel
    try:
        await context.bot.send_message(
            chat_id=CHANNEL_LOGS,
            text=f"🔔 **Nouveau Ticket #{ticket_id}**\n👤 {username} (`{user.id}`)\n📂 {cat}\n📝 {msg}\n\n👉 *Répondez à ce message pour répondre au client.*",
            parse_mode="Markdown"
        )
    except: pass
    
    # Retour au menu principal (import local pour éviter boucle)
    from app import show_main_menu
    await show_main_menu(user.id, clear=False)
    return ConversationHandler.END

# Ces fonctions existent pour la compatibilité avec app.py
async def start_ticket_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Écrivez votre réponse :")
    return WAIT_TICKET_MSG

async def close_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Fermé")
    try: await q.message.delete()
    except: pass
    return ConversationHandler.END

# ==============================================================================
# PARTIE ADMIN (DASHBOARD & RÉPONSES)
# ==============================================================================

def patch_db_tickets():
    """Fonction utilitaire appelée par app.py au démarrage"""
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    try:
        cur.execute("ALTER TABLE support_tickets ADD COLUMN message TEXT")
        cur.execute("ALTER TABLE support_tickets ADD COLUMN category TEXT")
        cur.execute("ALTER TABLE support_tickets ADD COLUMN username TEXT")
    except: pass
    con.commit(); con.close()

async def admin_list_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    
    con = sqlite3.connect(DB_NAME)
    rows = con.execute("SELECT ticket_id, username, category FROM support_tickets WHERE status='open' ORDER BY ticket_id DESC LIMIT 10").fetchall()
    con.close()

    if not rows:
        kb = [[InlineKeyboardButton("⬅️ Retour Admin", callback_data="admin_menu")]]
        try: await q.message.edit_text("✅ **Aucun ticket ouvert.**", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        except: await q.message.reply_text("✅ **Aucun ticket ouvert.**", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return ConversationHandler.END

    kb = []
    for tid, uname, cat in rows:
        kb.append([InlineKeyboardButton(f"#{tid} {uname} ({cat})", callback_data=f"adm_ticket_view_{tid}")])
    
    kb.append([InlineKeyboardButton("⬅️ Retour Admin", callback_data="admin_menu")])
    
    try: await q.message.edit_text("📨 **TICKETS EN ATTENTE**", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except: await q.message.reply_text("📨 **TICKETS EN ATTENTE**", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def admin_view_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tid = int(q.data.split("_")[-1])
    
    con = sqlite3.connect(DB_NAME)
    row = con.execute("SELECT user_id, username, category, message, created_at FROM support_tickets WHERE ticket_id=?", (tid,)).fetchone()
    con.close()
    
    if not row: return await admin_list_tickets(update, context)
    
    uid, uname, cat, msg_content, date = row
    txt = f"🎫 **TICKET #{tid}**\n👤 {uname} (`{uid}`)\n📂 {cat}\n📅 {date}\n\n📝 `{msg_content}`"
    
    kb = [
        [InlineKeyboardButton("✍️ Répondre", callback_data=f"adm_ticket_rep_{tid}_{uid}")],
        [InlineKeyboardButton("🗑 Fermer", callback_data=f"adm_ticket_close_{tid}")],
        [InlineKeyboardButton("🔙 Retour", callback_data="admin_tickets_list")]
    ]
    await q.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def admin_ask_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Demande à l'admin d'entrer sa réponse."""
    q = update.callback_query; await q.answer()
    data = q.data.split("_")
    context.user_data['reply_tid'] = data[3]
    context.user_data['reply_uid'] = data[4]
    
    await q.message.reply_text(f"✍️ **Réponse pour le ticket #{data[3]} :**\nÉcrivez le message maintenant.")
    return ADMIN_TICKET_REPLY

async def admin_send_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envoie la réponse au client et ferme le ticket."""
    tid = context.user_data.get('reply_tid')
    uid = context.user_data.get('reply_uid')
    msg = update.message.text
    
    # Envoi au client
    try:
        await context.bot.send_message(
            chat_id=uid,
            text=f"👨‍💻 **RÉPONSE DU SUPPORT :**\n━━━━━━━━━━━━━━━━━━\n{msg}\n━━━━━━━━━━━━━━━━━━\n_Le ticket a été fermé._",
            parse_mode="Markdown"
        )
        await update.message.reply_text("✅ Réponse envoyée.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Échec envoi : {e}")
        
    # Fermeture DB
    con = sqlite3.connect(DB_NAME)
    con.execute("UPDATE support_tickets SET status='closed' WHERE ticket_id=?", (tid,))
    con.commit(); con.close()
    
    return ConversationHandler.END

async def admin_close_no_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Fermé")
    tid = q.data.split("_")[-1]
    con = sqlite3.connect(DB_NAME)
    con.execute("UPDATE support_tickets SET status='closed' WHERE ticket_id=?", (tid,))
    con.commit(); con.close()
    await admin_list_tickets(update, context)

async def admin_reply_native(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réponse magique depuis le groupe (Reply to message)."""
    if not update.message.reply_to_message: return
    
    # On vérifie si le message original contient l'ID client
    orig = update.message.reply_to_message.text or ""
    match = re.search(r"\(`?(\d+)`?\)", orig)
    
    # Fallback : Cherche "ID Client : `12345`"
    if not match: match = re.search(r"ID Client : `?(\d+)`?", orig)
    
    if match:
        uid = match.group(1)
        resp = update.message.text
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"👨‍💻 **RÉPONSE RAPIDE :**\n{resp}",
                parse_mode="Markdown"
            )
            await update.message.set_reaction("👍")
        except:
            await update.message.reply_text("❌ Échec envoi (Bot bloqué ?)")

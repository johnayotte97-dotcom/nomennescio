import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

# Config
ADMIN_IDS = ["7573645008", "8409831904"]
CHANNEL_LOGS = "-1003589564052"
DB_NAME = "/home/johnmsaaq/bot-nomen/database.db" # Assure-toi du chemin

# États Conversation Client
WAIT_CATEGORY = 2000
WAIT_TICKET_MSG = 2001

# États Conversation Admin (Nouveau)
ADMIN_TICKET_REPLY = 2005

# ==============================================================================
# 1. CÔTÉ CLIENT (Création)
# ==============================================================================

async def start_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kb = [
        [InlineKeyboardButton("💸 Problème Paiement", callback_data="ticket_cat:paiement")],
        [InlineKeyboardButton("📦 Problème Produit", callback_data="ticket_cat:produit")],
        [InlineKeyboardButton("ℹ️ Question Générale", callback_data="ticket_cat:general")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
    ]
    await q.message.edit_text("📞 **SUPPORT**\nChoisissez le sujet :", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return WAIT_CATEGORY

async def save_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data['ticket_cat'] = q.data.split(":")[1].upper()
    kb = [[InlineKeyboardButton("⬅️ Retour", callback_data="support")]]
    await q.message.edit_text(f"📝 **Écrivez votre message :**\nSujet: {context.user_data['ticket_cat']}", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return WAIT_TICKET_MSG

async def handle_ticket_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message.text
    cat = context.user_data.get('ticket_cat', 'GENERAL')
    username = f"@{user.username}" if user.username else user.first_name

    # 1. Enregistrement en DB
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("INSERT INTO support_tickets (user_id, username, category, message, status) VALUES (?,?,?,?,?)",
                (str(user.id), username, cat, msg, 'open'))
    ticket_id = cur.lastrowid
    con.commit()
    con.close()

    # 2. Confirmation Client
    await update.message.reply_text(f"✅ **Ticket #{ticket_id} créé.**\nUn admin vous répondra bientôt.")

    # 3. Notification légère dans le channel (juste pour dire "Allez voir le bot")
    try:
        await context.bot.send_message(
            chat_id=CHANNEL_LOGS,
            text=f"🔔 **Nouveau Ticket #{ticket_id}**\n👤 {username}\n📂 {cat}\n👉 Voir dans le bot : /start > Admin > Gestion Tickets"
        )
    except: pass

    # Retour menu
    from app import show_main_menu
    await show_main_menu(user.id, clear=False)
    return ConversationHandler.END

# ==============================================================================
# 2. CÔTÉ ADMIN (Dashboard)
# ==============================================================================

async def admin_list_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche la liste des tickets OUVERTS."""
    q = update.callback_query; await q.answer()
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    # On récupère les 10 derniers tickets ouverts
    rows = cur.execute("SELECT ticket_id, username, category FROM support_tickets WHERE status='open' ORDER BY ticket_id DESC LIMIT 10").fetchall()
    con.close()

    if not rows:
        kb = [[InlineKeyboardButton("⬅️ Retour Admin", callback_data="admin_menu")]]
        await q.message.edit_text("✅ **Aucun ticket ouvert.** Bon travail !", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return ConversationHandler.END

    kb = []
    for tid, uname, cat in rows:
        # Bouton : "#12 @User (Paiement)"
        kb.append([InlineKeyboardButton(f"#{tid} {uname} ({cat})", callback_data=f"adm_ticket_view_{tid}")])
    
    kb.append([InlineKeyboardButton("⬅️ Retour Admin", callback_data="admin_menu")])

    await q.message.edit_text("📨 **TICKETS EN ATTENTE**\nCliquez pour traiter :", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ConversationHandler.END

async def admin_view_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le détail d'un ticket spécifique."""
    q = update.callback_query; await q.answer()
    ticket_id = int(q.data.split("_")[-1])
    
    con = sqlite3.connect(DB_NAME)
    row = con.execute("SELECT user_id, username, category, message, created_at FROM support_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    con.close()

    if not row:
        await q.message.reply_text("❌ Ticket introuvable.")
        return await admin_list_tickets(update, context)

    uid, uname, cat, msg_content, date = row
    
    txt = (
        f"🎫 **TICKET #{ticket_id}**\n"
        f"👤 {uname} (`{uid}`)\n"
        f"📂 {cat} | 📅 {date}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{msg_content}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    kb = [
        [InlineKeyboardButton("✍️ Répondre & Fermer", callback_data=f"adm_ticket_rep_{ticket_id}_{uid}")],
        [InlineKeyboardButton("🗑 Fermer sans répondre", callback_data=f"adm_ticket_close_{ticket_id}")],
        [InlineKeyboardButton("🔙 Retour Liste", callback_data="admin_tickets_list")]
    ]
    
    await q.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def admin_ask_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Demande à l'admin de taper sa réponse."""
    q = update.callback_query; await q.answer()
    data = q.data.split("_") # adm, ticket, rep, ID, UID
    ticket_id = data[3]
    user_id = data[4]
    
    context.user_data['reply_ticket_id'] = ticket_id
    context.user_data['reply_user_id'] = user_id
    
    await q.message.reply_text(
        f"✍️ **Réponse pour le ticket #{ticket_id} :**\n"
        "Écrivez votre message maintenant.\n"
        "(Tapez /cancel pour annuler)"
    )
    return ADMIN_TICKET_REPLY

async def admin_send_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envoie la réponse et ferme le ticket."""
    tid = context.user_data.get('reply_ticket_id')
    uid = context.user_data.get('reply_user_id')
    reply_msg = update.message.text
    
    # 1. Envoi au client
    try:
        await context.bot.send_message(
            chat_id=uid,
            text=f"👨‍💻 **RÉPONSE ADMIN (Ticket #{tid}) :**\n━━━━━━━━━━━━━━━━━━\n{reply_msg}"
        )
        await update.message.reply_text("✅ Réponse envoyée.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur envoi client: {e}")

    # 2. Fermeture en DB
    con = sqlite3.connect(DB_NAME)
    con.execute("UPDATE support_tickets SET status='closed' WHERE ticket_id=?", (tid,))
    con.commit(); con.close()
    
    # Retour liste
    # On simule un bouton retour pour réafficher la liste
    kb = [[InlineKeyboardButton("🔙 Liste des tickets", callback_data="admin_tickets_list")]]
    await update.message.reply_text("Ticket fermé.", reply_markup=InlineKeyboardMarkup(kb))
    
    return ConversationHandler.END

async def admin_close_no_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Fermé.")
    tid = q.data.split("_")[-1]
    
    con = sqlite3.connect(DB_NAME)
    con.execute("UPDATE support_tickets SET status='closed' WHERE ticket_id=?", (tid,))
    con.commit(); con.close()
    
    await admin_list_tickets(update, context)

import sqlite3
import os
import re
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

# --- LOGIQUE DE CHEMIN DYNAMIQUE ---
# On détecte le dossier où se trouve tickets.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# On définit le chemin vers la DB sans écrire le nom d'utilisateur en dur
DB_PATH = os.path.join(BASE_DIR, "database.db")

# Utilisation du même chemin DB que app.py
DB_NAME = os.environ.get("DB_NAME", DB_PATH)
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
                    cur.execute("ALTER TABLE support_tickets RENAME TO support_tickets_old")
        except:
            pass

        # 2. Création de la table propre
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
        
        # 3. Table Historique (Crucial pour la vue client)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER,
                sender_role TEXT,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Patch colonnes
        for col in ["message", "category", "username"]:
            try: cur.execute(f"ALTER TABLE support_tickets ADD COLUMN {col} TEXT")
            except: pass
        
        con.commit()
        print("✅ [TICKETS] DB prête et corrigée.")
    except Exception as e:
        print(f"❌ [TICKETS] Erreur DB Critique: {e}")
    finally:
        con.close()

# --- PARTIE ADMIN ---

async def admin_list_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche la liste des tickets ouverts."""
    q = update.callback_query
    try: await q.answer()
    except: pass
    
    try:
        con = sqlite3.connect(DB_NAME)
        rows = con.execute("SELECT ticket_id, username, category FROM support_tickets WHERE status IN ('open', 'replied') ORDER BY ticket_id DESC LIMIT 10").fetchall()
        con.close()
    except Exception as e:
        await q.message.reply_text(f"❌ Erreur SQL : {e}")
        return ConversationHandler.END

    if not rows:
        kb = [[InlineKeyboardButton("⬅️ Retour Admin", callback_data="admin_menu")]]
        await q.message.edit_text("✅ **Aucun ticket ouvert.**", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return ConversationHandler.END

    kb = []
    for tid, uname, cat in rows:
        label = f"#{tid} {uname or 'Inconnu'} ({cat})"
        kb.append([InlineKeyboardButton(label, callback_data=f"adm_ticket_view_{tid}")])
    
    kb.append([InlineKeyboardButton("⬅️ Retour Admin", callback_data="admin_menu")])
    await q.message.edit_text("📨 **TICKETS EN ATTENTE**", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

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

    # --- SÉCURITÉ MARKDOWN : Échappement des caractères spéciaux ---
    # On remplace les caractères qui font planter Telegram (_, *, `) par leur version sécurisée
    safe_uname = str(uname or 'Inconnu').replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
    safe_uid = str(uid).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
    safe_cat = str(cat).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
    # -------------------------------------------------------------

    clean_date = str(date).strip()

    txt = (
        f"🎫 **TICKET #{tid}**\n"
        f"👤 {safe_uname} (`{safe_uid}`)\n"
        f"📂 {safe_cat}\n"
        f"📅 {clean_date}"
    )
    
    kb = [
        [InlineKeyboardButton("✍️ Répondre", callback_data=f"adm_ticket_rep_{tid}_{uid}")],
        [InlineKeyboardButton("🗑 Fermer", callback_data=f"adm_ticket_close_{tid}")],
        [InlineKeyboardButton("🔙 Liste", callback_data="admin_tickets_list")]
    ]
    
    try:
        await q.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except Exception as e:
        # Fallback au cas où le Markdown pose encore problème malgré le nettoyage
        print(f"Erreur Markdown dans admin_view_ticket: {e}")
        await q.message.edit_text(txt.replace("**", "").replace("`", ""), reply_markup=InlineKeyboardMarkup(kb))

async def admin_ask_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    # On récupère les infos depuis le bouton (format: adm_ticket_rep_TICKETID_USERID)
    data = q.data.split("_")
    tid = data[3]
    uid = data[4]
    
    # Sauvegarde dans le contexte pour la suite
    context.user_data['reply_tid'] = tid
    context.user_data['reply_uid'] = uid
    
    # --- AJOUT : Récupération du message original pour l'afficher ---
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute("SELECT message, username FROM support_tickets WHERE ticket_id=?", (tid,))
    row = cur.fetchone()
    con.close()
    
    user_msg = row[0] if row else "(Message introuvable)"
    username = row[1] if row and row[1] else "Client"
    # ---------------------------------------------------------------
    
    # Affichage propre
    text = (
        f"✍️ **RÉPONSE AU TICKET #{tid}**\n"
        f"👤 Client : {username}\n"
        f"💬 Message : `{user_msg}`\n\n"
        "👉 **Écrivez votre réponse maintenant :**"
    )
    
    await q.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Annuler", callback_data="admin_tickets_list")]]),
        parse_mode="Markdown"
    )
    return ADMIN_TICKET_REPLY

async def admin_send_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = context.user_data.get('reply_tid')
    uid = context.user_data.get('reply_uid')
    msg_resp = update.message.text
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    try:
        # 1. On insère la réponse dans l'historique (Table ticket_messages)
        cur.execute("INSERT INTO ticket_messages (ticket_id, sender_role, message) VALUES (?, 'admin', ?)", (tid, msg_resp))
        
        # 2. ✅ CRUCIAL : On met le statut à 'replied' (et PAS 'closed')
        # C'est ce statut spécifique qui fait apparaître le bouton rouge chez le client.
        cur.execute("UPDATE support_tickets SET status='replied' WHERE ticket_id=?", (tid,))
        con.commit()

        # 3. Envoi de la notif
        await context.bot.send_message(
            chat_id=uid,
            text=f"👨‍💻 **SUPPORT (Ticket #{tid}) :**\n━━━━━━━━━━━━━━━━━━\n{msg_resp}",
            parse_mode="Markdown"
        )
        await update.message.reply_text("✅ Réponse envoyée et ticket mis à jour (replied).")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Erreur SQL : {e}")
    finally:
        con.close()
        
    return ConversationHandler.END

async def admin_close_no_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tid = q.data.split("_")[-1]
    con = sqlite3.connect(DB_NAME)
    con.execute("UPDATE support_tickets SET status='closed' WHERE ticket_id=?", (tid,))
    con.commit(); con.close()
    await q.answer("Ticket fermé.")
    await admin_list_tickets(update, context)

async def admin_reply_native(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Réponse rapide depuis le canal de logs.
    Sauvegarde le message mais envoie seulement une notification générique au client.
    """
    # On vérifie que c'est bien une réponse à un message
    if not update.message.reply_to_message:
        return

    # On récupère le texte du message auquel on répond pour trouver l'ID du ticket
    orig = update.message.reply_to_message.text or ""
    match = re.search(r"TICKET #(\d+)", orig)
    
    if match:
        tid = match.group(1) # L'ID dynamique (ex: 124)
        
        con = sqlite3.connect(DB_NAME)
        cur = con.cursor()
        
        # On récupère l'ID du client propriétaire du ticket
        cur.execute("SELECT user_id FROM support_tickets WHERE ticket_id=?", (tid,))
        row = cur.fetchone()
        
        if row:
            uid = row[0]
            msg_text = update.message.text # Votre vrai message (ex: "C'est fait merci")
            
            try:
                # 1. SAUVEGARDE EN BASE (Crucial pour que le client puisse lire le message plus tard)
                cur.execute("INSERT INTO ticket_messages (ticket_id, sender_role, message) VALUES (?, 'admin', ?)", (tid, msg_text))
                cur.execute("UPDATE support_tickets SET status='replied' WHERE ticket_id=?", (tid,))
                con.commit()

                # 2. ENVOI DE LA NOTIFICATION GÉNÉRIQUE (Ce que vous avez demandé)
                custom_message = f"⚠️ *Vous avez reçu un message, veuillez consulter le ticket #{tid}*"
                
                await context.bot.send_message(
                    chat_id=uid,
                    text=custom_message,
                    parse_mode="Markdown"
                )
                
                # Petit feedback pour vous dire que c'est envoyé
                await update.message.set_reaction("👍")
                
            except Exception as e:
                await update.message.reply_text(f"❌ Erreur d'envoi : {e}")
        
        con.close()
        
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
    """Crée le ticket et envoie la notif Admin avec le bouton 'Ouvrir'."""
    user = update.effective_user
    msg = update.message.text
    cat = context.user_data.get('ticket_cat', 'GENERAL')
    
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    
    # 1. Création Ticket
    cur.execute("INSERT INTO support_tickets (user_id, username, category, message, status) VALUES (?,?,?,?,?)",
                (str(user.id), user.username or user.first_name, cat, msg, 'open'))
    tid = cur.lastrowid
    
    # 2. Historique
    cur.execute("INSERT INTO ticket_messages (ticket_id, sender_role, message) VALUES (?, 'user', ?)", (tid, msg))
    
    con.commit()
    con.close()
    
    # Confirmation au client
    await update.message.reply_text(f"✅ **Ticket #{tid} créé.**\nOn vous répond bientôt.")
    
    try:
        # Notif admin
        # 👇 CHANGEMENT ICI : "📂 Ouvrir" au lieu de "✍️ Répondre"
        kb_admin = InlineKeyboardMarkup([[InlineKeyboardButton("📂 Ouvrir", callback_data=f"adm_ticket_rep_{tid}_{user.id}")]])
        
        await context.bot.send_message(
            chat_id=CHANNEL_LOGS, 
            text=f"🚨 **NOUVEAU TICKET # {tid}**\n👤 {user.id} (@{user.username})\n📂 {cat}\n📝 {msg}", 
            reply_markup=kb_admin
        )
    except: pass
    
    return ConversationHandler.END

# Fonctions dummy pour compatibilité app.py
async def start_ticket_reply(u,c): pass
async def close_ticket(u,c): pass

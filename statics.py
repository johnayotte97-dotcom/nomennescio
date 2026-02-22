from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import logging

# On importe UNIQUEMENT depuis utils pour casser la boucle d'importation
from utils import replace_view, kb_back_to_menu

logger = logging.getLogger(__name__)

async def callback_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try: await q.answer()
    except: pass
    
    user_id = str(update.effective_user.id)
    
    # IMPORT LOCAL pour éviter la boucle circulaire avec app.py
    try:
        from app import get_user_lang
        lang = get_user_lang(user_id)
    except ImportError:
        lang = "fr" # Valeur par défaut si l'import échoue
    
    text = (
        "❓ **F.A.Q - Questions Fréquentes**\n\n"
        "1️⃣ **Comment acheter ?**\nUtilisez la Boutique et payez en Crypto.\n\n"
        "2️⃣ **Support ?**\nOuvrez un ticket via le menu Support.\n\n"
        "3️⃣ **Délais ?**\nLes produits digitaux sont instantanés."
    )
    
    if lang == "en":
        text = (
            "❓ **F.A.Q - Frequently Asked Questions**\n\n"
            "1️⃣ **How to buy?**\nUse the Shop and pay with Crypto.\n\n"
            "2️⃣ **Support?**\nOpen a ticket via the Support menu.\n\n"
            "3️⃣ **Delivery time?**\nDigital products are instant."
        )

    await replace_view(q, text, reply_markup=kb_back_to_menu())

async def acces_channel_prive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    
    # Sécurité anti-crash
    try: await q.answer()
    except: pass
    
    ID_DU_CANAL = -1003536878473

    try:
        # 1. On vérifie si tu es membre
        is_member = False
        try:
            member = await context.bot.get_chat_member(chat_id=ID_DU_CANAL, user_id=q.from_user.id)
            if member.status in ['member', 'creator', 'administrator', 'restricted']:
                is_member = True
        except Exception:
            pass 

        # 2. SI TU ES DÉJÀ MEMBRE
        if is_member:
            clean_id = str(ID_DU_CANAL).replace("-100", "")
            direct_link = f"https://t.me/c/{clean_id}/1"
            
            await replace_view(
                q,
                "👋 **Tu es déjà membre !**\n\n"
                "Tu as déjà accès au canal VIP. Cliquez sur le bouton ci-dessous pour y entrer directement.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚀 Ouvrir le Canal", url=direct_link)],
                    [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
                ]),
                parse_mode="Markdown"
            )
            return

        # 3. SI TU N'ES PAS MEMBRE (Génération lien unique)
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

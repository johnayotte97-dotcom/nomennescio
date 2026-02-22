from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import logging

# On importe TOUT depuis utils : c'est notre base commune propre
from utils import replace_view, kb_back_to_menu, get_user_lang

logger = logging.getLogger(__name__)

async def callback_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    
    # On retire le q.answer() car il est déjà géré par le menu_handler dans app.py
    # Cela évite le bug du bouton qui reste "enfoncé" ou gris.
    
    user_id = str(update.effective_user.id)
    
    # Utilisation de la fonction depuis utils.py (plus d'import de app.py !)
    lang = get_user_lang(user_id)
    
    if lang == "en":
        text = (
            "❓ **F.A.Q - Frequently Asked Questions**\n\n"
            "1️⃣ **How to buy?**\nUse the Shop and pay with Crypto.\n\n"
            "2️⃣ **Support?**\nOpen a ticket via the Support menu.\n\n"
            "3️⃣ **Delivery time?**\nDigital products are instant."
        )
    else:
        text = (
            "❓ **F.A.Q - Questions Fréquentes**\n\n"
            "1️⃣ **Comment acheter ?**\nUtilisez la Boutique et payez en Crypto.\n\n"
            "2️⃣ **Support ?**\nOuvrez un ticket via le menu Support.\n\n"
            "3️⃣ **Délais ?**\nLes produits digitaux sont instantanés."
        )

    await replace_view(q, text, reply_markup=kb_back_to_menu())

async def acces_channel_prive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    
    # L'ID de ton canal
    ID_DU_CANAL = -1003536878473

    try:
        # 1. Vérification du statut de membre
        is_member = False
        try:
            member = await context.bot.get_chat_member(chat_id=ID_DU_CANAL, user_id=q.from_user.id)
            if member.status in ['member', 'creator', 'administrator', 'restricted']:
                is_member = True
        except Exception:
            pass 

        # 2. Cas : Déjà membre
        if is_member:
            clean_id = str(ID_DU_CANAL).replace("-100", "")
            direct_link = f"https://t.me/c/{clean_id}/1"
            
            await replace_view(
                q,
                "👋 **Accès VIP**\n\nTu es déjà membre du canal. Utilise le lien ci-dessous pour y accéder.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚀 Rejoindre le Canal", url=direct_link)],
                    [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
                ])
            )
            return

        # 3. Cas : Nouveau membre (Génération lien unique)
        invite_link = await context.bot.create_chat_invite_link(
            chat_id=ID_DU_CANAL,
            member_limit=1, 
            name=f"Invite_{q.from_user.id}"
        )

        await replace_view(
            q,
            f"🕵️ **Lien d'accès unique généré**\n\n"
            f"Ce lien ne fonctionne qu'une seule fois :\n\n"
            f"👉 {invite_link.invite_link}",
            reply_markup=kb_back_to_menu()
        )

    except Exception as e:
        logger.error(f"Erreur channel: {e}")
        await q.message.reply_text("🔴 Impossible de générer le lien. Vérifiez que le bot est admin du canal.")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import logging

# On utilise uniquement utils.py pour casser la boucle d'importation avec app.py
from utils import replace_view, kb_back_to_menu, get_user_lang

logger = logging.getLogger(__name__)

async def callback_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche la FAQ selon la langue de l'utilisateur."""
    q = update.callback_query
    
    # NOTE: Pas de q.answer() ici, il est déjà géré par le menu_handler dans app.py
    # pour éviter que le bouton ne reste bloqué en gris foncé.
    
    user_id = q.from_user.id
    lang = get_user_lang(user_id)
    
    if lang == "en":
        text = (
            "❓ **F.A.Q - Frequently Asked Questions**\n\n"
            "1️⃣ **How to buy?**\nGo to the Shop section, select your product, and follow the crypto payment instructions.\n\n"
            "2️⃣ **Support?**\nIf you have any issues, open a ticket in the Support menu. Our team will help you shortly.\n\n"
            "3️⃣ **Delivery?**\nDigital products are delivered instantly after payment confirmation (2 confirmations required)."
        )
    else:
        text = (
            "❓ **F.A.Q - Questions Fréquentes**\n\n"
            "1️⃣ **Comment acheter ?**\nAllez dans la boutique, choisissez votre produit et suivez les instructions de paiement crypto.\n\n"
            "2️⃣ **Support ?**\nEn cas de problème, ouvrez un ticket via le menu Support. Notre équipe vous répondra rapidement.\n\n"
            "3️⃣ **Délais ?**\nLes produits digitaux sont livrés instantanément après la validation du paiement (2 confirmations)."
        )

    await replace_view(q, text, reply_markup=kb_back_to_menu())

async def acces_channel_prive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère l'accès au canal privé avec vérification d'adhésion."""
    q = update.callback_query
    
    # ID du canal privé (Assure-toi que le bot est ADMIN du canal)
    ID_DU_CANAL = -1003536878473

    try:
        # 1. Vérification si l'utilisateur est déjà dans le canal
        is_member = False
        try:
            member = await context.bot.get_chat_member(chat_id=ID_DU_CANAL, user_id=q.from_user.id)
            if member.status in ['member', 'creator', 'administrator', 'restricted']:
                is_member = True
        except Exception:
            # Si erreur, on considère qu'il n'est pas membre
            is_member = False 

        # 2. Cas : L'utilisateur est déjà membre
        if is_member:
            # On génère un lien direct vers le premier message du canal
            clean_id = str(ID_DU_CANAL).replace("-100", "")
            direct_link = f"https://t.me/c/{clean_id}/1"
            
            await replace_view(
                q,
                "👋 **Accès VIP déjà actif**\n\nTu fais déjà partie du canal privé. Utilise le bouton ci-dessous pour le rejoindre directement.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚀 Rejoindre le Canal", url=direct_link)],
                    [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
                ])
            )
            return

        # 3. Cas : Nouveau membre (Lien d'invitation unique)
        invite_link = await context.bot.create_chat_invite_link(
            chat_id=ID_DU_CANAL,
            member_limit=1, # Le lien expire après 1 utilisation
            name=f"Access_{q.from_user.id}"
        )

        await replace_view(
            q,
            f"🕵️ **Lien d'invitation généré**\n\n"
            f"Voici ton accès unique pour rejoindre notre canal VIP.\n"
            f"⚠️ *Attention : Ce lien ne fonctionne qu'une seule fois.*\n\n"
            f"👉 {invite_link.invite_link}",
            reply_markup=kb_back_to_menu()
        )

    except Exception as e:
        logger.error(f"Erreur accès channel: {e}")
        # En cas d'erreur (bot non admin, etc.), on informe l'utilisateur sans crasher
        await q.message.reply_text("🔴 Une erreur technique est survenue. Contactez le support si le problème persiste.")

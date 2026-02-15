# -*- coding: utf-8 -*-
import re
import json
import sqlite3
from typing import Dict, Any, List, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import os
import asyncio

DB_NAME = os.environ.get("DB_NAME", "/home/johnmsaaq/bot-nomen/database.db")


# =========================
# Schéma & migrations
# =========================

def ensure_shop_tables(db: sqlite3.Connection):
    c = db.cursor()

    # purchases
    c.execute("""
    CREATE TABLE IF NOT EXISTS purchases(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id    TEXT NOT NULL,
      product_id INTEGER NOT NULL,
      price      REAL NOT NULL,
      full_data  TEXT,
      status     TEXT NOT NULL,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # cart_items
    c.execute("""
    CREATE TABLE IF NOT EXISTS cart_items(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id    TEXT NOT NULL,
      product_id INTEGER NOT NULL,
      qty        INTEGER DEFAULT 1,
      added_at   DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # transactions (au cas où tu l’utilises via helpers)
    c.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
      order_id    TEXT PRIMARY KEY,
      telegram_id TEXT,
      amount      REAL,
      status      TEXT DEFAULT 'completed',
      created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # migrations douces
    try:
        cols_tx = [r[1] for r in c.execute("PRAGMA table_info(transactions)").fetchall()]
        if 'created_at' not in cols_tx:
            c.execute("ALTER TABLE transactions ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    except Exception:
        pass

    # 👉 ajoute title/amount sur purchases si absents (pour hist_pros)
    try:
        cols_p = [r[1] for r in c.execute("PRAGMA table_info(purchases)").fetchall()]
        if 'title' not in cols_p:
            c.execute("ALTER TABLE purchases ADD COLUMN title TEXT")
        if 'amount' not in cols_p:
            c.execute("ALTER TABLE purchases ADD COLUMN amount REAL")
    except Exception:
        pass

    # Index
    c.execute("CREATE INDEX IF NOT EXISTS idx_cart_user ON cart_items(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cart_user_prod ON cart_items(user_id, product_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_purch_user ON purchases(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_purch_created ON purchases(created_at)")
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(transactions)").fetchall()]
        if 'created_at' in cols:
            c.execute("CREATE INDEX IF NOT EXISTS idx_tx_user_created ON transactions(telegram_id, created_at)")
        else:
            c.execute("CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(telegram_id)")
    except Exception:
        pass

    db.commit()

# =========================
# Utils
# =========================

def _coerce_price(raw) -> float:
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except Exception:
        try:
            return float(str(raw).replace(',', '.'))
        except Exception:
            return 0.0

def _mask_first(name: str) -> str:
    if not name:
        return "***"
    f = name.strip().split()[0]
    return (f[:1] + "***") if f else "***"

def _parse_content_block(content: str) -> Dict[str, str]:
    """
    Parser tolérant pour extraire paires clef:valeur d'un bloc content.
    Normalise 'DOB', 'PHONE', 'SIN', 'DL', etc.
    """
    out = {
    "sin": "", "dl": "",
    "firstname": "", "lastname": "", "dob": "", "address": "", "city": "",
    "postal": "", "email": "", "phone": "", "password": "", "base": "", "price": ""
    }

    if not content:
        return out

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        # SIN
        m = re.match(r'^\s*SIN\s*:?\s*(.+)$', line, re.I)
        if m:
            out["sin"] = m.group(1).strip()
            continue

        # DL (driver’s license)
        m = re.match(r'^\s*DL\s*:?\s*(.+)$', line, re.I)
        if m:
            out["dl"] = m.group(1).strip()
            continue

        # FIRST NAME
        m = re.match(r'^\s*FIRST\s*NAME\s*:?\s*(.+)$', line, re.I)
        if m:
            out["firstname"] = m.group(1).strip()
            continue

        # LAST NAME
        m = re.match(r'^\s*LAST(?:\s*NAME|NAMEL)?\s*:?\s*(.+)$', line, re.I)
        if m:
            out["lastname"] = m.group(1).strip()
            continue

        # DOB
        m = re.match(r'^\s*DOB(?:\s*\(DD\s*\/\s*MM\s*\/\s*YYYY\))?\s*:?\s*(.+)$', line, re.I)
        if m:
            out["dob"] = m.group(1).strip()
            continue

        # ADDRESS / ADRESSE
        m = re.match(r'^\s*(ADRESSE|ADDRESS)\s*:?\s*(.+)$', line, re.I)
        if m:
            out["address"] = m.group(2 if m.lastindex == 2 else 1).strip()
            continue

        # CITY
        m = re.match(r'^\s*CITY\s*:?\s*(.+)$', line, re.I)
        if m:
            out["city"] = m.group(1).strip()
            continue

        # CODE POSTAL / POSTAL CODE
        m = re.match(r'^\s*(CODE\s*POSTAL|POSTAL|POSTAL_CODE)\s*:?\s*(.+)$', line, re.I)
        if m:
            out["postal"] = m.group(2).strip()
            continue

        # EMAIL
        m = re.match(r'^\s*EMAIL\s*:?\s*(.+)$', line, re.I)
        if m:
            out["email"] = m.group(1).strip()
            continue

        m = re.match(r'^\s*PASSWORD\s*:?\s*(.+)$', line, re.I)
        if m:
            out["password"] = m.group(1).strip()
            continue

        # PHONE NUMBER
        m = re.match(r'^\s*PHONE(?:\s*NUMBER)?\s*:?\s*(.+)$', line, re.I)
        if m:
            out["phone"] = m.group(1).strip()
            continue

        # BASE
        m = re.match(r'^\s*BASE\s*:?\s*(.+)$', line, re.I)
        if m:
            out["base"] = m.group(1).strip()
            continue

        # PRICE
        m = re.match(r'^\s*PRICE\s*:?\s*([0-9\.,]+)', line, re.I)
        if m:
            out["price"] = m.group(1).replace(',', '.').strip()
            continue

    return out

def _grab_line(block: str, keys):
    if not block:
        return ""
    for key in keys:
        m = re.search(rf"(?mi)^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", block)
        if m:
            return m.group(1).strip()
    return ""

def _parse_product_fields(p: Dict[str, Any]) -> Dict[str, Any]:
    """
    Construit des champs cohérents à partir des colonnes + content + title.
    Analyse le bloc texte complet (content) pour extraire tous les champs,
    y compris SIN et DL, peu importe l'ordre ou la casse.
    """
    title   = (p.get("title") or "").strip()
    content = (p.get("content") or "").strip()
    price   = _coerce_price(p.get("price"))
    curr    = p.get("currency") or "CAD"
    base    = (p.get("base") or p.get("tier") or "").strip() or "FAKEPERSON"

    # Découper le titre pour extraire année et ville si présentes
    parts = [x.strip() for x in title.split("•")]
    year_from_title = parts[1] if len(parts) > 1 else ""
    city_from_title = parts[2] if len(parts) > 2 else ""

    # 🧠 Extraire tous les champs du bloc content
    parsed = {}
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        # --- MODIFICATION : Met la clé en minuscule ICI ---
        key = key.strip().lower() 
        val = val.strip()
        parsed[key] = val
    # --- FIN MODIFICATION ---

    # Récupération intelligente des champs
    # Le parseur cherche maintenant 'first name' (minuscule)
    first  = p.get("firstname") or p.get("first_name") or parsed.get("first name") or parsed.get("firstname") or ""
    last   = p.get("lastname")  or p.get("last_name")  or parsed.get("last name") or parsed.get("lastname") or ""
    dob    = parsed.get("dob(dd/mm/yyyy)") or parsed.get("dob") or ""
    addr   = parsed.get("adresse") or parsed.get("address") or ""
    city   = parsed.get("city") or city_from_title or ""
    postal = parsed.get("code postal") or parsed.get("postal code") or parsed.get("postal") or ""
    email  = parsed.get("email") or ""
    phone  = parsed.get("phone number") or parsed.get("phone") or ""

    # --- MODIFICATION : Le parseur cherche 'sin', 'dl', 'cc', etc. (minuscules) ---
    sin    = parsed.get("sin") or p.get("sin") or ""
    dl     = parsed.get("dl") or p.get("dl") or ""
    password = parsed.get("password") or p.get("password") or ""
    cc     = parsed.get("cc") or p.get("cc") or ""
    exp    = parsed.get("exp") or p.get("exp") or ""
    cvc    = parsed.get("cvc") or p.get("cvc") or ""
    # --- FIN MODIFICATION ---


    # Validation du prix
    if not price:
        try:
            # 'price' est aussi lu en minuscule
            price = float(parsed.get("price", "0").replace(",", ".")) 
        except Exception:
            price = 0.0

    # DOB final (année ou date complète)
    dob_final = dob or year_from_title or "N/A"
    first_up  = (first or "").split()[0].upper() or "JOHN"



    return {
        "first": first.strip(),
        "last": last.strip(),
        "first_up": first_up,
        "dob": dob_final,
        "year": year_from_title,
        "city": city.strip(),
        "email": email.strip(),
        "phone": phone.strip(),
        "address": addr.strip(),
        "postal": postal.strip(),
        "base": base,
        "price": price,
        "currency": curr,
        "content": content,
        "title": title,
        "sin": sin.strip(),
        "dl": dl.strip(),
        "password": password.strip(),
        "cc": cc.strip(),
        "exp": exp.strip(),
        "cvc": cvc.strip(),
    }
# =========================
# Rendu produits
# =========================

def mask_product_display(p: Dict[str, Any]) -> str:
    f = _parse_product_fields(p)
    dob_for_preview = f["year"] or f["dob"] or "N/A"
    return (
        f"FIRST NAME: {_mask_first(f['first'])}\n"
        f"DOB: {dob_for_preview}\n"
        f"CITY: {f['city'] or '—'}\n"
        f"BASE: {f['base']}\n"
        f"PRICE: {f['price']:.2f} {f['currency']}"
    )

def full_product_text(p: Dict[str, Any]) -> str:
    f = _parse_product_fields(p)

    # 🔐 Champs sensibles en fallback si absents
    f["sin"] = f.get("sin") or p.get("sin", "")
    f["dl"] = f.get("dl") or p.get("dl", "")
    f["password"] = f.get("password") or p.get("password", "")
    

    lines = [
        f"FIRST NAME: {f['first'] or f['first_up']}",
        f"LAST NAME: {f['last'] or ''}",
    ]

    # Date de naissance
    dob = f.get("dob", "")
    if re.search(r"\b\d{2}[-/]\d{2}[-/]\d{4}\b", dob):
        lines.append(f"DOB(DD/MM/YYYY): {dob}")
    elif dob:
        lines.append(f"DOB: {dob}")
    
    if f.get("cc"):
        lines.append(f"CC: {f['cc']}")
    if f.get("exp"):
        lines.append(f"EXP: {f['exp']}")
    if f.get("cvc"):
        lines.append(f"CVC: {f['cvc']}")

    # Champs sensibles
    if f.get("sin"):
        lines.append(f"SIN: {f['sin']}")
    if f.get("dl"):
        lines.append(f"DL: {f['dl']}")
    if f.get("password"):
        lines.append(f"PASSWORD: {f['password']}")

    # Coordonnées
    lines.extend([
        f"ADRESSE: {f.get('address', '')}",
        f"CITY: {f.get('city', '')}",
        f"CODE POSTAL: {f.get('postal', '')}",
        f"EMAIL: {f.get('email', '')}",
        f"PHONE: {f.get('phone', '')}",
        f"BASE: {f.get('base', '')}",
        f"PRICE: {f['price']:.2f} {f['currency']}",
    ])

    # Nettoyage final
    return "\n".join([x for x in lines if x.strip()])
# =========================
# DB helpers
# =========================

def _row_to_dict(cursor, row) -> Dict[str, Any]:
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))

def _purchases_cols(db: sqlite3.Connection) -> set:
    c = db.cursor()
    return {r[1] for r in c.execute("PRAGMA table_info(purchases)").fetchall()}

def user_has_bought(db: sqlite3.Connection, user_id: str, pid: int) -> bool:
    c = db.cursor()
    c.execute(
        "SELECT 1 FROM purchases WHERE user_id=? AND product_id=? AND status='paid' LIMIT 1",
        (str(user_id), int(pid))
    )
    return c.fetchone() is not None

def save_purchase(db: sqlite3.Connection, user_id: str, pid: int, price: float, product: Dict[str, Any]):
    parsed = _parse_product_fields(product)
    safe_prod = dict(product)

    # 💾 Champs sensibles à injecter dans le JSON (full_data)
    password = parsed.get("password", "").strip()
    sin = parsed.get("sin", "").strip()
    dl = parsed.get("dl", "").strip()

    safe_prod["password"] = password
    safe_prod["sin"] = sin
    safe_prod["dl"] = dl

    # 🧹 Nettoyer le bloc content existant
    content_lines = (safe_prod.get("content") or "").splitlines()
    content_lines = [line.strip() for line in content_lines if line.strip()]

    # 🔍 Identifier les clés déjà présentes, peu importe la casse
    existing_keys = {line.split(":")[0].strip().upper() for line in content_lines if ":" in line}
    force_add_keys = {"SIN", "DL", "PASSWORD"}

    # 🧽 Supprimer les anciennes versions mal formatées ou incomplètes
    content_lines = [line for line in content_lines if line.split(":")[0].strip().upper() not in force_add_keys]

    # ✅ Ajouter à la fin les valeurs actuelles, même si elles existaient déjà
    if sin:
        content_lines.append(f"SIN: {sin}")
    if dl:
        content_lines.append(f"DL: {dl}")
    if password:
        content_lines.append(f"PASSWORD: {password}")

    # ❌ Supprimer toute ligne de prix pour éviter les doublons
    content_lines = [line for line in content_lines if not line.upper().startswith("PRICE:")]
    safe_prod["content"] = "\n".join(content_lines)

    # 🔖 Titre du produit (fallback si vide)
    title = (
        product.get("title")
        or f"{(parsed.get('first') or 'John').upper()} {(parsed.get('last') or '').upper()} • "
           f"{parsed.get('year') or ''} • {(parsed.get('city') or '').upper()}"
    ).strip()

    # ✅ Champs à insérer dans la base
    cols = _purchases_cols(db)
    fields = ["user_id", "product_id", "price", "full_data", "status"]
    values = [
        str(user_id),
        int(pid),
        float(price),
        json.dumps(safe_prod, ensure_ascii=False),
        "paid"
    ]
    if "title" in cols:
        fields.append("title")
        values.append(title)
    if "amount" in cols:
        fields.append("amount")
        values.append(float(price))

    # 💾 Enregistrement final en base
    q = f"INSERT INTO purchases ({', '.join(fields)}) VALUES ({', '.join(['?'] * len(values))})"
    db.execute(q, values)
    db.commit()

def cart_clear(db: sqlite3.Connection, user_id: str):
    db.execute("DELETE FROM cart_items WHERE user_id=?", (str(user_id),))
    db.commit()

def cart_get(db: sqlite3.Connection, user_id: str) -> List[Tuple[Dict[str,Any], int]]:
    c = db.cursor()
    c.execute("""
        SELECT ci.product_id, ci.qty, p.*
        FROM cart_items ci
        JOIN products p ON p.id=ci.product_id
        WHERE ci.user_id=?
        ORDER BY ci.added_at DESC
    """, (str(user_id),))
    rows = c.fetchall()
    out = []
    if rows:
        colnames = [d[0] for d in c.description]  # ['product_id','qty', <products cols...>]
        for row in rows:
            pid = row[0]; qty = row[1]
            prod_cols = colnames[2:]
            prod_vals = row[2:]
            prod = dict(zip(prod_cols, prod_vals))
            out.append((prod, qty))
    return out

def cart_total(items: List[Tuple[Dict[str,Any], int]]) -> float:
    total = 0.0
    for prod, qty in items:
        total += _coerce_price(prod.get("price")) * int(qty)
    return total

# =========================
# Claviers
# =========================

def build_product_keyboard(db: sqlite3.Connection, user_id: str, pid: int, prod: Dict[str,Any]):
    # Si déjà acheté -> Voir
    if user_has_bought(db, user_id, pid):
        return InlineKeyboardMarkup([[InlineKeyboardButton("⚡ View full", callback_data=f"prod:view:{pid}")]])
    
    # Sinon -> Seulement "Add" et "Buy Now". Le bouton "Panier" sera dans le menu global.
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛒 Add", callback_data=f"cart:add:{pid}"),
            InlineKeyboardButton("⚡ Buy Now", callback_data=f"buynow:{pid}")
        ]
    ])

# =========================
# Handlers produits
# =========================

async def handle_preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: 
        try: await q.answer()
        except: pass
    if not q or not q.data.startswith("prod:preview:"):
        return
    pid = int(q.data.split(":",1)[1])
    db: sqlite3.Connection = context.bot_data["db_conn"]
    c = db.cursor()
    c.execute("SELECT * FROM products WHERE id=? LIMIT 1", (pid,))
    row = c.fetchone()
    if not row:
        return await q.message.reply_text("Produit introuvable.")
    prod = _row_to_dict(c, row)
    txt = mask_product_display(prod)
    kb = build_product_keyboard(db, str(update.effective_user.id), pid, prod)
    await q.message.reply_text(txt, reply_markup=kb)

async def handle_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: 
        try: await q.answer()
        except: pass
    if not q or not q.data.startswith("prod:view:"):
        return
    pid = int(q.data.split(":",1)[1])
    user_id = str(update.effective_user.id)
    db: sqlite3.Connection = context.bot_data["db_conn"]
    if not user_has_bought(db, user_id, pid):
        return await q.message.reply_text("Vous n'avez pas encore débloqué ce produit.")
    c = db.cursor()
    c.execute("SELECT * FROM products WHERE id=? LIMIT 1", (pid,))
    row = c.fetchone()
    if not row:
        return await q.message.reply_text("Produit introuvable.")
    prod = _row_to_dict(c, row)
    await q.message.reply_text(full_product_text(prod))

async def handle_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        try: await q.answer()
        except: pass

    data = q.data if q else ""
    if not (data.startswith("buy:") or data.startswith("buynow:")):
        return

    user_id = str(update.effective_user.id)
    pid = int(data.split(":", 1)[1])

    db: sqlite3.Connection = context.bot_data["db_conn"]
    c = db.cursor()

    try:
        # 1) Charger le produit
        c.execute("SELECT * FROM products WHERE id=? LIMIT 1", (pid,))
        row = c.fetchone()
        if not row:
            return await q.message.reply_text("Produit introuvable.")

        prod = _row_to_dict(c, row)
        parsed = _parse_product_fields(prod)

        # 2) Ajouter manuellement les champs sensibles si absents
        prod["sin"] = parsed.get("sin", "") or prod.get("sin", "")
        prod["dl"] = parsed.get("dl", "") or prod.get("dl", "")
        prod["password"] = parsed.get("password", "") or prod.get("password", "")

        # 3) Prix
        price = _coerce_price(prod.get("price"))
        if not price:
            content = (prod.get("content") or "") + "\n" + (prod.get("title") or "")
            m = re.search(r"(?mi)\bPRICE\s*:\s*([0-9]+(?:[.,][0-9]+)?)", content)
            if m:
                price = _coerce_price(m.group(1))
        if not price or price < 0:
            return await q.message.reply_text("Prix invalide pour cet item.")

        # 4) Solde utilisateur
        get_bal = context.bot_data.get("get_user_balance")
        upd_bal = context.bot_data.get("update_user_balance")
        if callable(get_bal) and callable(upd_bal):
            bal = get_bal(user_id)
            if bal < price:
                return await q.message.reply_text("Solde insuffisant. Rechargez, svp.")
            upd_bal(user_id, -price)

        # 5) Enregistrer l'achat
        save_purchase(db, user_id, pid, price, prod)

        # 6) Décrémenter stock
        try:
            if "stock" in prod and prod["stock"] is not None:
                c.execute("UPDATE products SET stock = stock - 1 WHERE id=? AND stock > 0", (pid,))
                db.commit()
        except Exception as e_upd:
            print(f"[BUY] WARN stock update failed: {e_upd}", flush=True)

        # 7) Afficher la fiche complète avec les champs sensibles
        try:
            details = full_product_text(prod)
        except Exception as e_fmt:
            print(f"[BUY] WARN full_product_text failed: {e_fmt}", flush=True)
            details = f"{prod.get('title','(sans titre)')}\nPRICE: {price:.2f} CAD"

        # --- MODIFICATION DÉBUT : Nettoyage & Autodestruction ---

        # A. On supprime le message du produit (celui avec le bouton Buy Now)
        try:
            await q.message.delete()
        except Exception:
            pass

        # B. On envoie la fiche complète
        sent_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ Achat confirmé — fiche complète (Disparaît dans 60s) :\n\n" + details
        )

        # C. On lance le compte à rebours de 60 secondes en arrière-plan
        async def _self_destruct(msg, delay):
            await asyncio.sleep(delay)
            try:
                await msg.delete()
            except Exception:
                pass

        # Cela lance la suppression SANS bloquer le reste du bot
        asyncio.create_task(_self_destruct(sent_msg, 60))
        
        return
        # --- MODIFICATION FIN ---

    except Exception as e:
        try:
            await q.message.reply_text(f"❌ Erreur achat: {e}")
        except Exception:
            pass
        print(f"[BUY] ERROR: {e}", flush=True)

# =========================
# Handlers panier
# =========================

def _kb_cart_base():
    # rangée actions + rangée Retour menu
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Payer", callback_data="cart:checkout"),
         InlineKeyboardButton("🧹 Vider", callback_data="cart:clear")],
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
    ])

def _kb_cart_back_only():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Retour", callback_data="menu_accueil")]
    ])

async def cart_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    
    # On ne répond pas au début pour garder le contrôle du Toast à la fin
    if not q or not q.data.startswith("cart:add:"): 
        return

    try:
        pid = int(q.data.split(":", 2)[2])
        user_id = str(update.effective_user.id)

        if "db_conn" not in context.bot_data:
            await q.answer("❌ Erreur interne", show_alert=False)
            return
        
        db: sqlite3.Connection = context.bot_data["db_conn"]
        c = db.cursor()

        # A. VÉRIFICATION DOUBLON
        c.execute("SELECT 1 FROM cart_items WHERE user_id=? AND product_id=?", (user_id, pid))
        exists = c.fetchone()

        if exists:
            # Si déjà là, on bloque avec un message rouge (Toast)
            await q.answer("❌ Déjà dans le panier !", show_alert=False)
            return

        # B. AJOUT
        c.execute("INSERT INTO cart_items (user_id, product_id, qty) VALUES (?,?,1)", (user_id, pid))
        db.commit()

        # C. FEEDBACK POSITIF (Toast rouge)
        # On calcule le total pour l'afficher dans la notif
        c.execute("SELECT count(*) FROM cart_items WHERE user_id=?", (user_id,))
        count = c.fetchone()[0]
        
        await q.answer(f"🔴 Ajouté ! (Total: {count})", show_alert=False)

        # Astuce : On modifie le bouton pour montrer qu'il est ajouté (visuel)
        try:
            new_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Added", callback_data="noop"), # Bouton inactif
                    InlineKeyboardButton("⚡ Buy Now", callback_data=f"buynow:{pid}")
                ]
            ])
            await q.edit_message_reply_markup(reply_markup=new_kb)
        except: pass

    except Exception as e:
        print(f"[CART ERROR] {e}", flush=True)
        try: await q.answer("❌ Erreur ajout", show_alert=False)
        except: pass

async def _send_or_edit(update: Update, text: str, reply_markup: InlineKeyboardMarkup | None = None):
    q = getattr(update, "callback_query", None)
    if q and getattr(q, "message", None):
        try:
            await q.message.edit_text(text, reply_markup=reply_markup, parse_mode=None)
            return
        except Exception:
            # si edit_text échoue (ex: message trop ancien), on répond en dessous
            pass
    target = getattr(update, "message", None) or (q.message if q else None)
    if target:
        await target.reply_text(text, reply_markup=reply_markup)

async def cart_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    
    # 1. NETTOYAGE RADICAL (On efface tout avant d'afficher le panier)
    from app import CATALOG_MSGS 
    msgs_to_del = CATALOG_MSGS.pop(chat_id, [])
    for mid in msgs_to_del:
        try: await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except: pass
    
    # On supprime aussi le message sur lequel on a cliqué (le triangle 🔻)
    try: await q.message.delete()
    except: pass

    # 2. RÉCUPÉRATION DES ITEMS
    db: sqlite3.Connection = context.bot_data["db_conn"]
    items = cart_get(db, user_id)
    
    return_target = context.user_data.get('cart_return_to', 'menu_accueil')
    return_label = "⬅️ Retour aux Pro's" if return_target == "cat:propro" else "⬅️ Retour"

    if not items:
        kb_empty = InlineKeyboardMarkup([[InlineKeyboardButton(return_label, callback_data=return_target)]])
        return await context.bot.send_message(chat_id=chat_id, text="🛒 **VOTRE PANIER EST VIDE**", reply_markup=kb_empty, parse_mode="Markdown")

    # 3. ENVOI D'UNE BULLE PAR PRODUIT
    total_global = 0.0
    sent_cart_ids = []

    for prod, qty in items:
        raw_title = str(prod.get('title', 'Unknown'))
        name = raw_title.split('•')[0].strip().upper()
        city = str(prod.get('city', 'N/A')).upper()
        
        year = str(prod.get('year', ''))
        if not year or year == 'N/A':
            parts = raw_title.split('•')
            year = parts[1].strip() if len(parts) > 1 else "19XX"
        
        price = _coerce_price(prod.get("price"))
        subtotal = price * int(qty)
        total_global += subtotal
        pid = prod.get('id')

        txt = (
            f"👤 **NOM** : `{name}`\n"
            f"🏙️ **VILLE** : `{city}`\n"
            f"📅 **ANNÉE** : `{year}`\n"
            f"💰 **PRIX** : `{subtotal:.2f} CAD`"
        )
        
        kb_item = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Payer", callback_data=f"buynow:{pid}")]])
        
        m = await context.bot.send_message(chat_id=chat_id, text=txt, reply_markup=kb_item, parse_mode="Markdown")
        sent_cart_ids.append(m.message_id)

    # 4. BULLE DE RÉSUMÉ FINAL (TOUT PAYER)
    summary_txt = f"🧾 **TOTAL GÉNÉRAL : `{total_global:.2f} CAD`**"
    
    kb_final_rows = []
    if len(items) > 1:
        kb_final_rows.append([InlineKeyboardButton(f"🛍️ TOUT PAYER ({total_global:.2f} CAD)", callback_data="cart:checkout")])
    
    kb_final_rows.append([InlineKeyboardButton("🧹 Vider le panier", callback_data="cart:clear")])
    kb_final_rows.append([InlineKeyboardButton(return_label, callback_data=return_target)])

    m_final = await context.bot.send_message(chat_id=chat_id, text=summary_txt, reply_markup=InlineKeyboardMarkup(kb_final_rows), parse_mode="Markdown")
    sent_cart_ids.append(m_final.message_id)

    # On stocke les IDs pour pouvoir les effacer si on clique sur "Retour"
    CATALOG_MSGS[chat_id] = sent_cart_ids
    
   
async def cart_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: await q.answer()
    
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    db: sqlite3.Connection = context.bot_data["db_conn"]
    
    # --- 1. NETTOYAGE DES BULLES ---
    # On récupère les IDs des fiches du panier qu'on a stockés
    from app import CATALOG_MSGS
    
    msgs_to_del = CATALOG_MSGS.pop(chat_id, [])
    for mid in msgs_to_del:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except:
            pass

    # --- 2. VIDAGE DE LA BASE DE DONNÉES ---
    cart_clear(db, user_id)
    
    # --- 3. CONFIRMATION ---
    return_target = context.user_data.get('cart_return_to', 'menu_accueil')
    return_label = "⬅️ Retour aux Pro's" if return_target == "cat:propro" else "⬅️ Retour"
    
    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton(return_label, callback_data=return_target)]])
    
    # On édite le dernier message (celui du total) pour dire que c'est vidé
    await q.edit_message_text("🧹 **Le panier a été vidé.**", reply_markup=kb_back, parse_mode="Markdown")

async def cart_checkout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: 
        try: await q.answer()
        except: pass
    user_id = str(update.effective_user.id)
    db: sqlite3.Connection = context.bot_data["db_conn"]
    items = cart_get(db, user_id)
    if not items:
        return await _send_or_edit(update, "🛒 Panier vide.", reply_markup=_kb_cart_back_only())

    total = cart_total(items)
    get_bal = context.bot_data.get("get_user_balance")
    upd_bal = context.bot_data.get("update_user_balance")
    if callable(get_bal) and callable(upd_bal):
        bal = get_bal(user_id)
        if bal < total:
            return await _send_or_edit(update, f"⚠️ Solde insuffisant. Requis: {total:.2f} CAD", reply_markup=_kb_cart_back_only())
        upd_bal(user_id, -total)

    # envoyer chaque produit payé
    for prod, qty in items:
        pid = int(prod.get("id"))
        price = _coerce_price(prod.get("price"))
        for _ in range(int(qty)):
            save_purchase(db, user_id, pid, price, prod)
        try:
            if "stock" in prod and prod["stock"] is not None:
                db.execute(
                    "UPDATE products SET stock = stock - ? WHERE id=? AND stock>=?",
                    (int(qty), pid, int(qty))
                )
                db.commit()
        except Exception:
            pass
        # fiche complète de l’item
        await _send_or_edit(update, "🧾 " + full_product_text(prod), reply_markup=_kb_cart_back_only())

    cart_clear(db, user_id)
    await _send_or_edit(update, f"✅ Paiement réussi. Total débité: {total:.2f} CAD", reply_markup=_kb_cart_back_only())

# =========================
# Commandes de confort
# =========================

async def cmd_historique(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Supprime tous les anciens messages affichés (fiche + boutons navigation)
    if "history_msg_ids" in context.user_data:
        for msg_id in context.user_data["history_msg_ids"]:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
            except:
                pass
    context.user_data["history_msg_ids"] = []

    # ✅ Connexion à la bonne base
    DB_NAME = "/home/johnmsaaq/bot-nomen/database.db"
    con = sqlite3.connect(DB_NAME)
    cur = con.cursor()
    cur.execute(
        "SELECT id, full_data, created_at FROM purchases WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,)
    )
    results = cur.fetchall()
    con.close()

    if not results:
        await update.message.reply_text("❌ Aucun historique pour le moment.")
        return

    # Pagination
    max_per_page = 2
    page = int(context.args[0]) if context.args else 1
    total_pages = (len(results) + max_per_page - 1) // max_per_page
    results_page = results[(page - 1) * max_per_page: page * max_per_page]

    # ✅ Affichage propre avec SIN et DL bien mis en avant
    for purchase_id, full_data, created_at in results_page:
        try:
            data = json.loads(full_data)
        except Exception:
            data = {}

        title = data.get("title", "Produit inconnu")
        first = data.get("first") or data.get("firstname") or ""
        last = data.get("last") or data.get("lastname") or ""
        city = data.get("city") or ""
        dob = data.get("dob") or data.get("year") or "N/A"
        sin = data.get("sin", "")
        dl = data.get("dl", "")
        password = data.get("password", "")

        # Contenu structuré clair
        lines = [
            f"<b>🏷️ {title}</b>",
            f"👤 <b>Nom :</b> {first} {last}",
            f"🎂 <b>Date de naissance :</b> {dob}",
            f"🏙️ <b>Ville :</b> {city}",
        ]

        if sin:
            lines.append(f"🧾 <b>SIN :</b> {sin}")
        if dl:
            lines.append(f"🚗 <b>DL :</b> {dl}")
        if password:
             lines.append(f"🔐 <b>Password :</b> {password}")

        lines.append(f"<i>Date d’achat :</i> <code>{created_at}</code>")
        message = "\n".join(lines)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Supprimer", callback_data=f"delete_history_{purchase_id}")]
        ])
        msg = await update.message.reply_text(message, parse_mode="HTML", reply_markup=keyboard)
        context.user_data["history_msg_ids"].append(msg.message_id)

    # Navigation (page précédente / suivante)
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Page précédente", callback_data=f"history_page_{page-1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("➡️ Page suivante", callback_data=f"history_page_{page+1}"))

    # Boutons de navigation
    if nav_buttons:
        msg = await update.message.reply_text(
            "⬇️ Navigation :",
            reply_markup=InlineKeyboardMarkup(nav_buttons)
        )
        context.user_data["history_msg_ids"].append(msg.message_id)

async def cmd_panier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await cart_view_callback(update, context)




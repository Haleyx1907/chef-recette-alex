import os
import re
import json
import sqlite3
import logging
import httpx
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# httpx logue chaque requête HTTP (y compris le polling Telegram toutes les
# quelques secondes) — on baisse son niveau pour ne garder que les erreurs
# et ne pas noyer les vrais logs utiles.
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- Config ---
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AUTHORIZED_CHAT_ID = os.environ["AUTHORIZED_CHAT_ID"]

DB_PATH = "/app/data/chef_recette.db"
SYSTEM_PROMPT_PATH = "/app/system_prompt.md"
MODEL = "claude-sonnet-4-6"

client = Anthropic(api_key=ANTHROPIC_API_KEY)


# --- Météo (Veurey-Voroize, 38113) ---
WEATHER_LAT = 45.272
WEATHER_LON = 5.613
WEATHER_LOCATION_NAME = "Veurey-Voroize (38113)"

WMO_CODE_DESCRIPTIONS = {
    0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
    45: "brouillard", 48: "brouillard givrant",
    51: "bruine légère", 53: "bruine modérée", 55: "bruine forte",
    61: "pluie légère", 63: "pluie modérée", 65: "pluie forte",
    71: "neige légère", 73: "neige modérée", 75: "neige forte",
    80: "averses légères", 81: "averses modérées", 82: "averses violentes",
    95: "orage", 96: "orage avec grêle", 99: "orage violent avec grêle",
}


def get_weather_forecast():
    """Récupère les prévisions météo à 7 jours via Open-Meteo (API gratuite,
    sans clé). Renvoie un texte prêt à insérer dans le contexte du prompt.
    En cas d'échec (réseau, API indisponible), renvoie une chaîne vide plutôt
    que de faire planter la génération du menu."""
    try:
        response = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": WEATHER_LAT,
                "longitude": WEATHER_LON,
                "daily": "temperature_2m_max,temperature_2m_min,weathercode,precipitation_sum",
                "timezone": "Europe/Paris",
                "forecast_days": 7,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()

        dates = data["daily"]["time"]
        tmax = data["daily"]["temperature_2m_max"]
        tmin = data["daily"]["temperature_2m_min"]
        codes = data["daily"]["weathercode"]
        precip = data["daily"]["precipitation_sum"]

        lines = [f"Météo prévue à {WEATHER_LOCATION_NAME} pour les 7 prochains jours :"]
        for i in range(len(dates)):
            desc = WMO_CODE_DESCRIPTIONS.get(codes[i], "conditions variables")
            lines.append(
                f"- {dates[i]} : {tmin[i]:.0f}°C à {tmax[i]:.0f}°C, {desc}, "
                f"précipitations {precip[i]:.0f}mm"
            )
        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"Impossible de récupérer la météo : {e}")
        return ""


# --- DB setup ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dish_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dish_name TEXT NOT NULL,
            date_proposed TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS current_menu (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            week_start TEXT,
            menu_text TEXT,
            dishes_json TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_recent_dishes(days=30):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    cur.execute(
        "SELECT dish_name, date_proposed FROM dish_history WHERE date_proposed >= ? ORDER BY date_proposed DESC",
        (cutoff,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def save_dishes(dish_names):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.now().isoformat()
    for name in dish_names:
        cur.execute(
            "INSERT INTO dish_history (dish_name, date_proposed) VALUES (?, ?)",
            (name, now),
        )
    conn.commit()
    conn.close()


def save_current_menu(menu_text, dishes=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    dishes_json = json.dumps(dishes if dishes is not None else [])
    cur.execute(
        """
        INSERT INTO current_menu (id, week_start, menu_text, dishes_json)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            week_start=excluded.week_start,
            menu_text=excluded.menu_text,
            dishes_json=excluded.dishes_json
        """,
        (datetime.now().isoformat(), menu_text, dishes_json),
    )
    conn.commit()
    conn.close()


def clear_dish_history():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM dish_history")
    conn.commit()
    conn.close()


def get_current_menu():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT menu_text FROM current_menu WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_current_menu_dishes():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT dishes_json FROM current_menu WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return []


# --- Claude call ---
def load_system_prompt():
    with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


SYSTEM_PROMPT = load_system_prompt()


def ask_claude(user_message, extra_context=""):
    full_system = SYSTEM_PROMPT
    if extra_context:
        full_system += "\n\n## Contexte additionnel pour cette requête\n" + extra_context

    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=full_system,
        messages=[{"role": "user", "content": user_message}],
    )

    if response.stop_reason == "max_tokens":
        logger.warning(
            "Réponse Claude tronquée (max_tokens atteint) — le bloc DISH_LIST "
            "risque d'être manquant ou le contenu incomplet."
        )

    return "".join(block.text for block in response.content if block.type == "text")


# --- Auth guard ---
def authorized(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_chat.id) != str(AUTHORIZED_CHAT_ID):
            logger.warning(f"Unauthorized access attempt from chat_id={update.effective_chat.id}")
            return
        return await func(update, context)
    return wrapper


# --- Handlers ---
@authorized
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salut, c'est ChefRecetteAlex ! 🍳\n\n"
        "Commandes disponibles :\n"
        "/menu — génère le menu complet de la semaine (midi + soir)\n"
        "/menu soir — génère uniquement le menu du soir\n"
        "/menu midi — génère uniquement le menu du midi\n"
        "/repas — suggère un seul plat, à l'improviste\n"
        "/repas [ingrédients] — suggère un plat avec ce que tu as sous la main\n"
        "/courses — liste de courses du menu actuel\n"
        "/remplace [plat] — remplace un plat du menu\n"
        "/recette [plat] — recette détaillée d'un plat du menu\n"
        "/reset_historique — vide l'historique anti-répétition (confirmation requise)"
    )


@authorized
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    meal_filter = None
    if context.args:
        arg = context.args[0].lower()
        if arg in ("midi", "dejeuner", "déjeuner"):
            meal_filter = "midi"
        elif arg in ("soir", "diner", "dîner"):
            meal_filter = "soir"
        else:
            await update.message.reply_text(
                "Je n'ai pas reconnu cet argument. Utilise /menu, /menu midi, ou /menu soir."
            )
            return

    await update.message.reply_text("Je te prépare ça, une seconde... 👨‍🍳")

    recent = get_recent_dishes(days=30)
    recent_list = "\n".join(f"- {name} (proposé le {date[:10]})" for name, date in recent)
    recent_context = (
        f"Plats déjà proposés dans les 30 derniers jours (à ne pas répéter plus de 2x/mois) :\n{recent_list}"
        if recent_list else "Aucun plat proposé récemment."
    )

    weather_context = get_weather_forecast()

    extra_context = recent_context
    if weather_context:
        extra_context += "\n\n" + weather_context
        extra_context += (
            "\n\nAdapte le menu à ces prévisions : privilégie des plats plus légers/frais "
            "si les températures sont élevées, et des plats plus réconfortants/mijotés "
            "si le temps est froid ou pluvieux."
        )

    if meal_filter:
        request_text = (
            f"Génère uniquement le menu du {meal_filter} pour la semaine (lundi à dimanche), "
            "avec pour chaque plat le temps de préparation et la recette détaillée. "
            f"Ne propose rien pour l'autre repas ({'soir' if meal_filter == 'midi' else 'midi'}) — "
            "uniquement le repas demandé."
        )
    else:
        request_text = (
            "Génère le menu complet de la semaine (midi et soir, du lundi au dimanche), "
            "avec pour chaque plat le temps de préparation et la recette détaillée."
        )

    raw_response = ask_claude(request_text, extra_context=extra_context)

    menu_text, dishes = extract_dish_list(raw_response)

    save_current_menu(menu_text, dishes=dishes)
    if dishes:
        save_dishes(dishes)
    else:
        await update.message.reply_text(
            "⚠️ Je n'ai pas pu extraire la liste des plats pour l'historique anti-répétition "
            "cette fois-ci — le menu reste valide, mais ce menu ne sera pas comptabilisé pour la règle des 2x/mois."
        )

    await _send_long_message(update, menu_text)


@authorized
async def courses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = get_current_menu()
    if not current:
        await update.message.reply_text("Pas encore de menu généré cette semaine — lance /menu d'abord !")
        return

    await update.message.reply_text("Je prépare ta liste de courses...")

    courses_text = ask_claude(
        f"Voici le menu actuel :\n\n{current}\n\n"
        "Génère la liste de courses correspondante, groupée par rayon "
        "(fruits & légumes, boucherie/poissonnerie, crémerie, épicerie, surgelés, etc.), "
        "avec quantités adaptées à 4 personnes."
    )

    await _send_long_message(update, courses_text)


@authorized
async def remplace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Précise le plat à remplacer : /remplace [nom du plat]")
        return

    plat = " ".join(context.args)
    current = get_current_menu()
    if not current:
        await update.message.reply_text("Pas encore de menu généré cette semaine — lance /menu d'abord !")
        return

    previous_dishes = get_current_menu_dishes()

    await update.message.reply_text(f"Je remplace « {plat} »...")

    raw_response = ask_claude(
        f"Voici le menu actuel :\n\n{current}\n\n"
        f"Remplace uniquement le plat « {plat} » par une alternative pertinente "
        "(même repas, mêmes contraintes du foyer), garde le reste du menu identique. "
        "Renvoie le menu complet mis à jour, avec recette détaillée pour le nouveau plat."
    )

    updated_menu, new_dishes = extract_dish_list(raw_response)

    save_current_menu(updated_menu, dishes=new_dishes)

    if new_dishes:
        # On ne veut enregistrer dans l'historique que le(s) plat(s) réellement
        # nouveau(x) par rapport à l'ancien menu, pour ne pas fausser le comptage
        # "2x/mois" des plats qui n'ont pas bougé.
        newly_added = [d for d in new_dishes if d not in previous_dishes]
        if newly_added:
            save_dishes(newly_added)
        else:
            logger.warning("Aucun nouveau plat détecté après /remplace — historique inchangé.")
    else:
        await update.message.reply_text(
            "⚠️ Je n'ai pas pu extraire la liste des plats mise à jour — "
            "le menu reste valide mais l'historique anti-répétition n'est pas mis à jour pour ce remplacement."
        )

    await _send_long_message(update, updated_menu)
    await update.message.reply_text(
        "✅ Menu mis à jour. Pense à retaper /courses si tu veux la liste de courses à jour."
    )


@authorized
async def repas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ingredients = " ".join(context.args) if context.args else None

    await update.message.reply_text("Je réfléchis à ça... 🍳")

    if ingredients:
        request_text = (
            f"Propose UN SEUL plat (un repas, pas un menu de semaine), avec recette complète "
            f"(ingrédients avec quantités, étapes de préparation), en utilisant en priorité ces "
            f"ingrédients disponibles : {ingredients}. Respecte les contraintes du foyer. "
            "Si besoin, tu peux compléter avec d'autres ingrédients courants, mais privilégie "
            "au maximum ce qui a été fourni."
        )
    else:
        request_text = (
            "Propose UN SEUL plat (un repas, pas un menu de semaine), au choix, avec recette "
            "complète (ingrédients avec quantités, étapes de préparation), en respectant les "
            "contraintes du foyer."
        )

    recette_text = ask_claude(request_text)
    await _send_long_message(update, recette_text)


@authorized
async def reset_historique(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() != "confirme":
        await update.message.reply_text(
            "⚠️ Cette action vide définitivement l'historique anti-répétition "
            "(les 2x/mois ne seront plus comptés pour les plats déjà proposés).\n\n"
            "Pour confirmer, tape : /reset_historique confirme"
        )
        return

    clear_dish_history()
    await update.message.reply_text("✅ Historique des plats vidé. Le compteur repart de zéro.")


@authorized
async def recette(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Précise le plat : /recette [nom du plat]")
        return

    plat = " ".join(context.args)
    current = get_current_menu() or ""

    recette_text = ask_claude(
        f"Voici le menu actuel (pour contexte) :\n\n{current}\n\n"
        f"Donne-moi la recette détaillée et complète du plat « {plat} » "
        "(ingrédients avec quantités, étapes de préparation)."
    )

    await _send_long_message(update, recette_text)


DISH_LIST_PATTERN = re.compile(
    r"===DISH_LIST===\s*(\[.*?\])\s*===END_DISH_LIST===",
    re.DOTALL,
)


def extract_dish_list(text):
    """Extrait la liste JSON des plats depuis la réponse de Claude, et renvoie
    (texte_nettoyé, liste_des_plats). Si le bloc est absent ou malformé,
    renvoie le texte inchangé et une liste vide (l'historique ne sera juste
    pas mis à jour pour cet appel — pas d'erreur bloquante pour Alex)."""
    match = DISH_LIST_PATTERN.search(text)
    if not match:
        logger.warning("Bloc DISH_LIST introuvable dans la réponse de Claude.")
        return text, []

    raw_list = match.group(1)
    cleaned_text = DISH_LIST_PATTERN.sub("", text).strip()

    try:
        dishes = json.loads(raw_list)
        if not isinstance(dishes, list):
            raise ValueError("DISH_LIST n'est pas une liste")
        return cleaned_text, dishes
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"DISH_LIST malformé, ignoré : {e}")
        return cleaned_text, []


async def _send_long_message(update: Update, text: str):
    # Telegram limite les messages à 4096 caractères
    max_len = 4000
    for i in range(0, len(text), max_len):
        await update.message.reply_text(text[i:i + max_len])


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("repas", repas))
    app.add_handler(CommandHandler("courses", courses))
    app.add_handler(CommandHandler("remplace", remplace))
    app.add_handler(CommandHandler("recette", recette))
    app.add_handler(CommandHandler("reset_historique", reset_historique))

    logger.info("ChefRecetteAlex démarré.")
    app.run_polling()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
#!/usr/bin/env python3
"""
Bot d'alerte Vinted.

Lit une liste de recherches Vinted (URLs copiées depuis l'app/le site),
interroge l'API interne de Vinted pour chaque recherche, et envoie une
alerte Discord (via webhook) pour chaque nouvelle annonce jamais vue.

Configuration : config.json
Mémoire des annonces déjà vues : seen_items.json (persisté entre les runs)
"""

import json
import os
import sys
import time
from urllib.parse import urlparse, parse_qs

import requests

CONFIG_PATH = "config.json"
SEEN_PATH = "seen_items.json"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# IDs Vinted correspondant à chaque état d'article (identiques sur tous les
# domaines Vinted, seul l'affichage est traduit selon la langue du site).
STATUS_IDS = {
    "neuf avec étiquette": 1,
    "neuf sans étiquette": 2,
    "très bon état": 3,
    "bon état": 4,
    "satisfaisant": 5,
}

ALLOWED_SIZES = {"XS", "S", "M", "L", "XL", "XXL", "XXXL"}


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_session(base_url):
    """Récupère une session avec les cookies nécessaires pour interroger l'API Vinted."""
    session = requests.Session()
    session.headers.update(HEADERS)
    # Un premier GET sur la page d'accueil permet d'obtenir les cookies
    # anti-bot nécessaires pour que l'API accepte les requêtes suivantes.
    resp = session.get(base_url, timeout=15)
    resp.raise_for_status()
    return session


def search_url_to_params(search_url):
    """Convertit une URL de recherche Vinted (copiée depuis le site/app) en
    paramètres de requête utilisables directement sur l'API catalog/items."""
    parsed = urlparse(search_url)
    query = parse_qs(parsed.query)
    params = {k: v[0] if len(v) == 1 else v for k, v in query.items()}
    # On force le tri par nouveauté pour détecter les nouvelles annonces en premier
    params["order"] = "newest_first"
    params.setdefault("per_page", "20")
    return params


def search_config_to_params(search):
    """Construit les paramètres de recherche directement à partir de champs
    simples dans config.json (keyword, price_max, price_min, conditions...),
    sans avoir besoin de coller une URL Vinted."""
    params = {"order": "newest_first", "per_page": "20"}
    if search.get("keyword"):
        params["search_text"] = search["keyword"]
    if search.get("price_max") is not None:
        params["price_to"] = search["price_max"]
    if search.get("price_min") is not None:
        params["price_from"] = search["price_min"]

    conditions = search.get("conditions")
    if conditions:
        status_ids = [
            STATUS_IDS[c.lower()] for c in conditions if c.lower() in STATUS_IDS
        ]
        if status_ids:
            params["status_ids[]"] = status_ids

    return params


def get_search_params(search):
    """Renvoie les paramètres de requête pour une recherche, qu'elle soit
    définie via une URL Vinted complète ('url') ou via des champs simples
    ('keyword', 'price_max', 'price_min')."""
    if search.get("url"):
        return search_url_to_params(search["url"])
    return search_config_to_params(search)


def fetch_items(session, search, base_url, api_endpoint):
    params = get_search_params(search)
    resp = session.get(api_endpoint, params=params, timeout=15)
    if resp.status_code == 401 or resp.status_code == 403:
        # La session a expiré / a été rejetée : on en recrée une et on réessaie une fois
        session = get_session(base_url)
        resp = session.get(api_endpoint, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", [])


RARE_KEYWORDS = ["ispa", "gyakusou", "sample", "prototype", "archive", "vintage", "rare", "collab"]


def compute_score(item, price_max):
    """Calcule un score de pertinence sur 5, basé sur le prix (plus c'est
    en dessous du budget max, mieux c'est), l'état de l'article, et la
    présence de mots signalant une pièce rare ou recherchée."""
    score = 2  # score de base

    price_obj = item.get("price", {})
    try:
        price = float(price_obj.get("amount", 0))
    except (TypeError, ValueError):
        price = 0

    if price_max and price:
        ratio = price / float(price_max)
        if ratio <= 0.4:
            score += 2
        elif ratio <= 0.65:
            score += 1

    status = (item.get("status") or "").lower()
    if "neuf avec" in status:
        score += 1
    elif "neuf sans" in status:
        score += 0.5

    title = (item.get("title") or "").lower()
    if any(kw in title for kw in RARE_KEYWORDS):
        score += 1

    return round(min(score, 5), 1)


def send_discord_alert(item, search_name, base_url, score=None, is_deal=False):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️  DISCORD_WEBHOOK_URL manquant, alerte non envoyée.")
        return

    title = item.get("title", "Sans titre")
    price_obj = item.get("price", {})
    price = f"{price_obj.get('amount', '?')} {price_obj.get('currency_code', '')}".strip()
    brand = item.get("brand_title", "")
    size = item.get("size_title", "")
    url = item.get("url", base_url)
    photo = (item.get("photo") or {}).get("url")

    score_line = f"\n⭐ Score : {score}/5" if score is not None else ""
    deal_line = "\n🔥 **Bonne affaire potentielle**" if is_deal else ""

    embed = {
        "title": title[:256],
        "url": url,
        "description": f"💶 **{price}**" + (f"\n🏷️ {brand}" if brand else "") + (f"\n📏 {size}" if size else "") + score_line + deal_line,
        "footer": {"text": f"Recherche : {search_name}"},
    }
    if photo:
        embed["thumbnail"] = {"url": photo}

    header = "🔥 Bonne affaire trouvée" if is_deal else "🆕 Nouvelle annonce trouvée"
    payload = {
        "content": f"{header} pour **{search_name}** !",
        "embeds": [embed],
    }

    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    if resp.status_code >= 300:
        print(f"⚠️  Erreur envoi Discord ({resp.status_code}): {resp.text}")


def main():
    config = load_json(CONFIG_PATH, {"searches": []})
    seen = load_json(SEEN_PATH, {})  # {search_name: [item_ids...]}

    if not config.get("searches"):
        print("Aucune recherche configurée dans config.json.")
        sys.exit(0)

    # Domaine Vinted à utiliser : "fr" (par défaut), "co.uk", "de", "es", "it"...
    domain = config.get("domain", "fr")
    base_url = f"https://www.vinted.{domain}"
    api_endpoint = f"https://www.vinted.{domain}/api/v2/catalog/items"

    session = get_session(base_url)

    for search in config["searches"]:
        name = search.get("name", "Recherche sans nom")
        if not search.get("url") and not search.get("keyword"):
            print(f"⚠️  Recherche '{name}' ignorée : ni 'url' ni 'keyword' renseigné.")
            continue

        print(f"🔍 Scan de la recherche : {name}")
        try:
            items = fetch_items(session, search, base_url, api_endpoint)
        except Exception as e:
            print(f"❌ Erreur lors du scan de '{name}': {e}")
            continue

        exclude_words = [w.lower() for w in search.get("exclude", [])]
        if exclude_words:
            before = len(items)
            items = [
                item for item in items
                if not any(w in item.get("title", "").lower() for w in exclude_words)
            ]
            print(f"   → {before - len(items)} annonce(s) filtrée(s) par exclusion.")

        allowed_sizes = search.get("sizes")
        if allowed_sizes:
            allowed_sizes = {s.strip().upper() for s in allowed_sizes}
            before = len(items)

            def size_matches(item):
                size_title = (item.get("size_title") or "").strip().upper()
                # Le champ ressemble à "L / 40 / 12" ou juste "M" : on ne garde
                # que le premier segment (la taille lettre).
                first_part = size_title.split("/")[0].strip()
                return first_part in allowed_sizes

            items = [item for item in items if size_matches(item)]
            print(f"   → {before - len(items)} annonce(s) filtrée(s) par taille.")

        seen_ids = set(seen.get(name, []))
        new_items = [item for item in items if str(item.get("id")) not in seen_ids]

        # Score chaque nouvelle annonce et trie les meilleures en premier,
        # pour que tu voies les pépites avant le reste.
        price_max = search.get("price_max")
        scored = [(compute_score(item, price_max), item) for item in new_items]
        scored.sort(key=lambda pair: pair[0], reverse=True)

        new_ids = []
        for score, item in scored:
            item_id = str(item.get("id"))
            new_ids.append(item_id)
            is_deal = score >= 4
            send_discord_alert(item, name, base_url, score=score, is_deal=is_deal)
            time.sleep(1)  # éviter de spammer Discord trop vite

        # On garde uniquement les IDs vus dans ce scan + les nouveaux,
        # pour ne pas laisser grossir le fichier indéfiniment.
        current_ids = [str(item.get("id")) for item in items]
        seen[name] = list(set(current_ids) | seen_ids)[:500]

        print(f"   → {len(new_ids)} nouvelle(s) annonce(s) trouvée(s).")
        time.sleep(2)  # petite pause entre chaque recherche pour ne pas se faire bloquer

    save_json(SEEN_PATH, seen)


if __name__ == "__main__":
    main()

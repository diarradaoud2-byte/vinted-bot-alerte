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

# Score minimum pour qu'une annonce soit envoyée sur Discord — en dessous,
# elle est ignorée silencieusement (comptée dans le résumé mais pas alertée).
MIN_SCORE_TO_ALERT = 3


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


def search_config_to_params(search, global_catalog_ids=None):
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

    # Filtre par vraie catégorie Vinted (vestes/pulls/pantalons), pour
    # éliminer les faux positifs sans dépendre uniquement des mots du titre.
    catalog_ids = search.get("catalog_ids", global_catalog_ids)
    if catalog_ids:
        params["catalog[]"] = catalog_ids

    return params


def get_search_params(search, global_catalog_ids=None):
    """Renvoie les paramètres de requête pour une recherche, qu'elle soit
    définie via une URL Vinted complète ('url') ou via des champs simples
    ('keyword', 'price_max', 'price_min')."""
    if search.get("url"):
        return search_url_to_params(search["url"])
    return search_config_to_params(search, global_catalog_ids)


def fetch_items(session, search, base_url, api_endpoint, global_catalog_ids=None):
    params = get_search_params(search, global_catalog_ids)
    resp = session.get(api_endpoint, params=params, timeout=15)
    if resp.status_code == 401 or resp.status_code == 403:
        # La session a expiré / a été rejetée : on en recrée une et on réessaie une fois
        session = get_session(base_url)
        resp = session.get(api_endpoint, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", [])


RARE_KEYWORDS = ["ispa", "gyakusou", "sample", "prototype", "archive", "vintage", "rare", "collab"]

# Lignes considérées comme plus recherchées / meilleure revente : bonus de
# score plus élevé que les lignes basiques (Tech Fleece, Tech Pack) qui sont
# très courantes et se revendent moins bien.
HIGH_VALUE_LINES = [
    "acg", "ispa", "gyakusou", "aeroswift", "phenom elite",
    "running division", "run division", "trail", "storm-fit",
    "windrunner", "tokyo", "berlin", "japan",
]
COMMON_LINES = ["tech fleece", "tech pack", "coldgear"]


def compute_score(item, price_max, search_name=""):
    """Calcule un score de pertinence sur 5, basé sur le prix (plus c'est
    en dessous du budget max, mieux c'est), l'état de l'article, la
    présence de mots signalant une pièce rare, et si la ligne elle-même
    est considérée comme plus recherchée (bonne revente) ou plus basique."""
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
        elif ratio <= 0.85:
            score += 0.5

    status = (item.get("status") or "").lower()
    if "neuf avec" in status or "new with tags" in status:
        score += 1
    elif "neuf sans" in status or "new without tags" in status:
        score += 0.75
    elif "très bon état" in status or "tres bon état" in status or "very good" in status:
        score += 0.5

    title = (item.get("title") or "").lower()
    if any(kw in title for kw in RARE_KEYWORDS):
        score += 1

    search_name_lower = search_name.lower()
    if any(line in search_name_lower for line in HIGH_VALUE_LINES):
        score += 1.5
    elif any(line in search_name_lower for line in COMMON_LINES):
        score -= 2

    return round(max(0, min(score, 5)), 1)


def fetch_item_details(session, item_id, base_url, domain):
    """Récupère les détails complets d'une annonce (dont l'état exact),
    car cette info n'est pas toujours incluse dans les résultats de recherche."""
    try:
        url = f"https://www.vinted.{domain}/api/v2/items/{item_id}"
        resp = session.get(url, timeout=10)
        if resp.status_code >= 300:
            return None
        data = resp.json()
        return data.get("item", {})
    except Exception:
        return None


def send_discord_alert(item, search_name, base_url, score=None, is_deal=False):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️  DISCORD_WEBHOOK_URL manquant, alerte non envoyée.")
        return

    title = item.get("title", "Sans titre")
    price_obj = item.get("price", {})
    price = f"{price_obj.get('amount', '?')} {price_obj.get('currency_code', '')}".strip()
    brand = item.get("brand_title", "")
    size = item.get("size_title", "")
    condition = item.get("status", "")
    url = item.get("url", base_url)
    photo = (item.get("photo") or {}).get("url")

    score_line = f"\n⭐ Score : {score}/5" if score is not None else ""
    deal_line = "\n🔥 **Bonne affaire potentielle**" if is_deal else ""

    embed = {
        "title": title[:256],
        "url": url,
        "description": f"💶 **{price}**" + (f"\n🏷️ {brand}" if brand else "") + (f"\n📏 {size}" if size else "") + (f"\n✨ État : {condition}" if condition else "") + score_line + deal_line,
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

    # Domaines Vinted à scanner : liste de pays ("domains"), ou "domain" pour
    # un seul pays (compatibilité avec l'ancien format).
    domains = config.get("domains") or [config.get("domain", "fr")]
    # Catégories Vinted par défaut : Outerwear (1206), Jumpers & Sweaters (79),
    # Trousers (34) — pour ne récupérer que vestes/pulls/pantalons.
    global_catalog_ids = config.get("catalog_ids", [1206, 79, 34])

    scan_summary = []
    alerted_ids_this_run = set()  # évite d'alerter 2x la même annonce si elle
                                    # matche plusieurs mots-clés dans ce run

    for domain in domains:
        base_url = f"https://www.vinted.{domain}"
        api_endpoint = f"https://www.vinted.{domain}/api/v2/catalog/items"
        session = get_session(base_url)
        print(f"\n🌍 === Domaine : vinted.{domain} ===")

        for search in config["searches"]:
            base_name = search.get("name", "Recherche sans nom")
            name = f"{base_name} [{domain}]"  # clé unique par pays pour la mémoire
            if not search.get("url") and not search.get("keyword"):
                print(f"⚠️  Recherche '{name}' ignorée : ni 'url' ni 'keyword' renseigné.")
                scan_summary.append((name, "ignorée"))
                continue

            print(f"🔍 Scan de la recherche : {name}")
            try:
                items = fetch_items(session, search, base_url, api_endpoint, global_catalog_ids)
            except Exception as e:
                print(f"❌ Erreur lors du scan de '{name}': {e}")
                scan_summary.append((name, f"échec ({e})"))
                continue

        # Vinted matche parfois de façon large (marque + catégorie) sans que
            # tous les mots du mot-clé apparaissent réellement dans le titre. On
            # vérifie ici que chaque mot du mot-clé est bien présent, pour éviter
            # de recevoir un simple jogging à la place d'un modèle précis.
            keyword = search.get("keyword")
            if keyword and not search.get("url"):
                keyword_words = [w.lower() for w in keyword.split() if len(w) > 1]
                min_words_required = len(keyword_words)  # tous les mots requis
                before = len(items)

                def title_matches(item):
                    title = item.get("title", "").lower()
                    matched = sum(1 for w in keyword_words if w in title)
                    return matched >= min_words_required

                items = [item for item in items if title_matches(item)]
                print(f"   → {before - len(items)} annonce(s) filtrée(s) car titre ne contenait pas assez de mots du mot-clé.")

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

            price_max = search.get("price_max")
            new_ids = []
            alerted_count = 0

            for item in new_items:
                item_id = str(item.get("id"))
                new_ids.append(item_id)

                # On va chercher l'état détaillé avant de calculer le score
                # définitif, car l'état influence la note (neuf/très bon état).
                if not item.get("status"):
                    details = fetch_item_details(session, item_id, base_url, domain)
                    if details and details.get("status"):
                        item["status"] = details["status"]

                score = compute_score(item, price_max, name)

                if score < MIN_SCORE_TO_ALERT:
                    continue  # annonce ignorée : score trop bas

                if item_id in alerted_ids_this_run:
                    continue  # déjà alertée via un autre mot-clé dans ce run
                alerted_ids_this_run.add(item_id)

                is_deal = score >= 4.5
                send_discord_alert(item, name, base_url, score=score, is_deal=is_deal)
                alerted_count += 1
                time.sleep(1)  # éviter de spammer Discord trop vite

            # On garde uniquement les IDs vus dans ce scan + les nouveaux,
            # pour ne pas laisser grossir le fichier indéfiniment.
            current_ids = [str(item.get("id")) for item in items]
            seen[name] = list(set(current_ids) | seen_ids)[:500]

            print(f"   → {len(new_items)} nouvelle(s) annonce(s), {alerted_count} alertée(s) (score ≥ {MIN_SCORE_TO_ALERT}).")
            scan_summary.append((name, f"ok ({alerted_count}/{len(new_items)} alertée(s))"))
            time.sleep(2)  # petite pause entre chaque recherche pour ne pas se faire bloquer

    total_runs = len(config["searches"]) * len(domains)
    print("\n📋 Résumé du scan :")
    for name, status in scan_summary:
        print(f"   - {name}: {status}")
    print(f"\n✅ {len(scan_summary)}/{total_runs} recherches traitées ({len(domains)} pays × {len(config['searches'])} mots-clés).")

    save_json(SEEN_PATH, seen)


if __name__ == "__main__":
    main()

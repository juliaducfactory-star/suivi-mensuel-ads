"""
Suivi Mensuel des Performances
Récupère les données Google Ads et Meta Ads du mois précédent
et les écrit dans le Google Sheet de suivi.
À lancer le 1er de chaque mois.
"""

import os
import json
import smtplib
from email.mime.text import MIMEText
from datetime import date, timedelta
import requests
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from dotenv import load_dotenv

load_dotenv()

GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
NOTIFICATION_EMAIL = os.getenv("NOTIFICATION_EMAIL", "juliaducfactory@gmail.com")

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
DEVELOPER_TOKEN = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN")
MCC_ID = os.getenv("GOOGLE_ADS_MCC_ID")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "google_token.json")

GOOGLE_ADS_ACCOUNTS = {
    "ThésDirect": os.getenv("GOOGLE_ADS_THESDIRECT_ID"),
    "MonExpresso": os.getenv("GOOGLE_ADS_MONEXPRESSO_ID"),
    "HerboDirect": os.getenv("GOOGLE_ADS_HERBODIRECT_ID"),
    "EpicesDirect": os.getenv("GOOGLE_ADS_EPICESDIRECT_ID"),
}

META_ACCOUNTS = {
    "ThésDirect": os.getenv("META_THESDIRECT_ACCOUNT"),
    "MonExpresso": os.getenv("META_MONEXPRESSO_ACCOUNT"),
}

MOIS_FR = [
    "", "Janv", "Fév", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Sept", "Oct", "Nov", "Déc"
]


# ── Dates ─────────────────────────────────────────────────────────────────────

def get_last_month():
    today = date.today()
    premier_ce_mois = today.replace(day=1)
    dernier_mois_fin = premier_ce_mois - timedelta(days=1)
    dernier_mois_debut = dernier_mois_fin.replace(day=1)
    return dernier_mois_debut, dernier_mois_fin


# ── Google Auth ───────────────────────────────────────────────────────────────

def get_google_credentials():
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data["scopes"],
    )

    if creds.expired:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(TOKEN_FILE, "w") as f:
            json.dump(token_data, f, indent=2)

    return creds


# ── Google Ads ─────────────────────────────────────────────────────────────────

GOOGLE_ADS_API_VERSION = "v20"
GOOGLE_ADS_BASE_URL = f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}"


def fetch_google_ads_data(creds, customer_id, date_debut, date_fin):
    url = f"{GOOGLE_ADS_BASE_URL}/customers/{customer_id}/googleAds:search"
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "developer-token": DEVELOPER_TOKEN,
        "login-customer-id": MCC_ID,
        "Content-Type": "application/json",
    }
    query = f"""
        SELECT
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value
        FROM campaign
        WHERE segments.date BETWEEN '{date_debut}' AND '{date_fin}'
    """
    payload = {"query": query}
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()

    totaux = {"spend": 0.0, "conversions": 0.0, "conversion_value": 0.0}
    for row in response.json().get("results", []):
        metrics = row.get("metrics", {})
        totaux["spend"] += int(metrics.get("costMicros", 0)) / 1_000_000
        totaux["conversions"] += float(metrics.get("conversions", 0))
        totaux["conversion_value"] += float(metrics.get("conversionsValue", 0))

    totaux["cac"] = (
        round(totaux["spend"] / totaux["conversions"], 2)
        if totaux["conversions"] > 0 else 0
    )
    totaux["roas"] = (
        round(totaux["conversion_value"] / totaux["spend"], 2)
        if totaux["spend"] > 0 else 0
    )
    totaux["spend"] = round(totaux["spend"], 2)
    totaux["conversions"] = round(totaux["conversions"], 0)
    totaux["conversion_value"] = round(totaux["conversion_value"], 2)

    return totaux


# ── Meta Ads ───────────────────────────────────────────────────────────────────

def fetch_meta_ads_data(account_id, date_debut, date_fin):
    url = f"https://graph.facebook.com/v19.0/{account_id}/insights"
    params = {
        "access_token": META_ACCESS_TOKEN,
        "time_range": json.dumps({
            "since": str(date_debut),
            "until": str(date_fin)
        }),
        "fields": "spend,actions,action_values",
        "level": "account",
    }

    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json().get("data", [])

    if not data:
        return {"spend": 0, "conversions": 0, "conversion_value": 0, "cac": 0, "roas": 0}

    row = data[0]
    spend = float(row.get("spend", 0))

    conversions = 0.0
    for action in row.get("actions", []):
        if action["action_type"] == "purchase":
            conversions = float(action["value"])

    conversion_value = 0.0
    for action in row.get("action_values", []):
        if action["action_type"] == "purchase":
            conversion_value = float(action["value"])

    return {
        "spend": round(spend, 2),
        "conversions": round(conversions, 0),
        "conversion_value": round(conversion_value, 2),
        "cac": round(spend / conversions, 2) if conversions > 0 else 0,
        "roas": round(conversion_value / spend, 2) if spend > 0 else 0,
    }


# ── Google Sheets ──────────────────────────────────────────────────────────────

def get_sheets_client(creds):
    return gspread.authorize(creds)


def trouver_colonne_mois(premiere_ligne, mois_nom):
    """Cherche la colonne correspondant au mois dans la 1ère ligne."""
    for i, valeur in enumerate(premiere_ligne):
        if mois_nom.lower() in valeur.lower():
            return i + 1  # gspread utilise l'index 1-based
    return None


def trouver_ligne_budget(toutes_valeurs, marque, canal):
    """Cherche la ligne Budget pour une marque + canal.
    La marque n'apparait que dans la premiere ligne de son bloc."""
    marque_courante = ""
    for i, row in enumerate(toutes_valeurs):
        if row[0].strip():
            marque_courante = row[0].strip()
        if (marque.lower() in marque_courante.lower()
                and row[1].strip().lower() == canal.lower()
                and row[2].strip().lower() == "budget"):
            return i + 1  # 1-based
    return None


def ecrire_donnees(worksheet, ligne_budget, col_mois, donnees):
    """Ecrit les 5 metriques verticalement a partir de la ligne Budget."""
    valeurs = [
        donnees["spend"],
        donnees["conversions"],
        donnees["cac"],
        donnees["conversion_value"],
        donnees["roas"],
    ]
    for i, val in enumerate(valeurs):
        worksheet.update_cell(ligne_budget + i, col_mois, val)


# ── Email ─────────────────────────────────────────────────────────────────────

def envoyer_notification(sujet, corps):
    if not GMAIL_APP_PASSWORD:
        print("Email non configure, notification ignoree.")
        return
    msg = MIMEText(corps, "plain", "utf-8")
    msg["Subject"] = sujet
    msg["From"] = NOTIFICATION_EMAIL
    msg["To"] = NOTIFICATION_EMAIL
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls()
        smtp.login(NOTIFICATION_EMAIL, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print(f"Notification envoyee a {NOTIFICATION_EMAIL}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    date_debut, date_fin = get_last_month()
    mois_nom = MOIS_FR[date_debut.month]
    print(f"Récupération des données pour : {mois_nom} {date_debut.year}")
    print(f"Periode : {date_debut} au {date_fin}\n")

    # Auth Google
    creds = get_google_credentials()
    gc = get_sheets_client(creds)
    sheet = gc.open_by_key(SPREADSHEET_ID)
    worksheet = next(
        ws for ws in sheet.worksheets() if ws.id == 1783220445
    )

    # Lecture unique du sheet
    toutes_valeurs = worksheet.get_all_values()
    premiere_ligne = toutes_valeurs[0]

    # Colonne du mois dans le sheet
    col_mois = trouver_colonne_mois(premiere_ligne, mois_nom)
    if not col_mois:
        msg = f"ERREUR : colonne '{mois_nom}' introuvable dans le sheet."
        print(msg)
        envoyer_notification(f"[ERREUR] Suivi {mois_nom} {date_debut.year}", msg)
        return

    lignes_resume = [f"Suivi mensuel {mois_nom} {date_debut.year} - Resultats\n"]

    # ── Google Ads ──
    print("=== Google Ads ===")
    lignes_resume.append("=== Google Ads ===")

    for marque, customer_id in GOOGLE_ADS_ACCOUNTS.items():
        if not customer_id:
            print(f"  {marque} : customer ID manquant, ignore.")
            continue

        donnees = fetch_google_ads_data(creds, customer_id, date_debut, date_fin)
        print(f"  {marque} : {donnees}")

        ligne = trouver_ligne_budget(toutes_valeurs, marque, "GOOGLE")
        if ligne:
            ecrire_donnees(worksheet, ligne, col_mois, donnees)
            print(f"    Ecrit lignes {ligne}-{ligne+4}, colonne {col_mois}")
            lignes_resume.append(
                f"  {marque} | Budget: {donnees['spend']}€ | Conv: {donnees['conversions']} | CAC: {donnees['cac']}€ | ROAS: {donnees['roas']}"
            )
        else:
            print(f"    Ligne introuvable pour {marque} / Google")
            lignes_resume.append(f"  {marque} : ERREUR ligne introuvable")

    # ── Meta Ads ──
    print("\n=== Meta Ads ===")
    lignes_resume.append("\n=== Meta Ads ===")

    for marque, account_id in META_ACCOUNTS.items():
        if not account_id:
            continue

        donnees = fetch_meta_ads_data(account_id, date_debut, date_fin)
        print(f"  {marque} : {donnees}")

        ligne = trouver_ligne_budget(toutes_valeurs, marque, "META")
        if ligne:
            ecrire_donnees(worksheet, ligne, col_mois, donnees)
            print(f"    Ecrit lignes {ligne}-{ligne+4}, colonne {col_mois}")
            lignes_resume.append(
                f"  {marque} | Budget: {donnees['spend']}€ | Conv: {donnees['conversions']} | CAC: {donnees['cac']}€ | ROAS: {donnees['roas']}"
            )
        else:
            print(f"    Ligne introuvable pour {marque} / Meta")
            lignes_resume.append(f"  {marque} : ERREUR ligne introuvable")

    print("\nTermine !")
    envoyer_notification(
        f"[OK] Suivi {mois_nom} {date_debut.year} mis a jour",
        "\n".join(lignes_resume)
    )


if __name__ == "__main__":
    main()

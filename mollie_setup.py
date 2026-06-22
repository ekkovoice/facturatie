"""
Eenmalige Mollie-setup per klant.

Stap 1 - klant aanmaken + betaallink genereren:
  python mollie_setup.py drfinn

Stuur de link naar de klant. Zodra de klant via iDEAL betaald heeft:

Stap 2 - mandate opslaan:
  python mollie_setup.py drfinn
"""
import os
import sys
import json
import requests
from pathlib import Path

BASE_DIR = Path(__file__).parent
MOLLIE_API_KEY = os.environ["MOLLIE_API_KEY"]
HEADERS = {"Authorization": f"Bearer {MOLLIE_API_KEY}"}


def load_klanten():
    with open(BASE_DIR / "klanten.json", encoding="utf-8") as f:
        return json.load(f)


def save_klanten(data):
    with open(BASE_DIR / "klanten.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def create_customer(klant):
    resp = requests.post(
        "https://api.mollie.com/v2/customers",
        headers=HEADERS,
        json={"name": klant["naam"], "email": klant["email"]},
    )
    resp.raise_for_status()
    cid = resp.json()["id"]
    print(f"Mollie customer aangemaakt: {cid}")
    return cid


def create_first_payment(customer_id, klant):
    bedrag = sum(p["bedrag_excl"] for p in klant["posten"])
    btw = round(bedrag * 0.21, 2)
    totaal = round(bedrag + btw, 2)

    resp = requests.post(
        "https://api.mollie.com/v2/payments",
        headers=HEADERS,
        json={
            "amount": {"currency": "EUR", "value": f"{totaal:.2f}"},
            "customerId": customer_id,
            "sequenceType": "first",
            "description": "Eerste betaling + SEPA-machtiging ekkovoice maandabonnement",
            "redirectUrl": "https://ekkovoice.nl",
            "method": "ideal",
        },
    )
    resp.raise_for_status()
    return resp.json()


def get_active_mandate(customer_id):
    resp = requests.get(
        f"https://api.mollie.com/v2/customers/{customer_id}/mandates",
        headers=HEADERS,
    )
    resp.raise_for_status()
    mandates = resp.json().get("_embedded", {}).get("mandates", [])
    valid = [m for m in mandates if m["status"] == "valid"]
    return valid[0]["id"] if valid else None


def run():
    if len(sys.argv) < 2:
        print("Gebruik: python mollie_setup.py <klant_id>")
        print("Voorbeeld: python mollie_setup.py drfinn")
        sys.exit(1)

    klant_id = sys.argv[1]
    data = load_klanten()
    klant = next((k for k in data["klanten"] if k["id"] == klant_id), None)

    if not klant:
        print(f"Klant '{klant_id}' niet gevonden in klanten.json.")
        sys.exit(1)

    customer_id = klant.get("mollie_customer_id")

    if not customer_id:
        customer_id = create_customer(klant)
        klant["mollie_customer_id"] = customer_id
        save_klanten(data)

    mandate_id = get_active_mandate(customer_id)

    if mandate_id:
        klant["mollie_mandate_id"] = mandate_id
        save_klanten(data)
        print(f"Mandate actief: {mandate_id}")
        print(f"Klant {klant['naam']} is klaar voor automatische incasso.")
        return

    payment = create_first_payment(customer_id, klant)
    checkout_url = payment["_links"]["checkout"]["href"]

    print()
    print(f"Stuur deze betaallink naar {klant['naam']}:")
    print(f"  {checkout_url}")
    print()
    print("Nadat de klant betaald heeft, run opnieuw:")
    print(f"  python mollie_setup.py {klant_id}")


if __name__ == "__main__":
    run()

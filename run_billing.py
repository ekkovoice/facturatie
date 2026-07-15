import os
import sys
import json
import ssl
import smtplib
import requests
import base64
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path
from jinja2 import Template
from weasyprint import HTML

BASE_DIR = Path(__file__).parent

MOLLIE_API_KEY = os.environ["MOLLIE_API_KEY"]
SMTP_HOST = os.environ.get("SMTP_HOST", "ekkovoice.nl")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
MAIL_FROM = os.environ.get("MAIL_FROM", "info@ekkovoice.nl")

# Testmodus-schakelaars (leeg = productie)
DRY_RUN = os.environ.get("BILLING_DRY_RUN") == "1"        # slaat Mollie/SEPA volledig over
TEST_EMAIL = os.environ.get("BILLING_TEST_EMAIL", "").strip()  # stuurt mail hierheen i.p.v. klant
ENV_FORCE = os.environ.get("BILLING_FORCE") == "1"        # draait ongeacht de dag

DUTCH_MONTHS = {
    1: "januari", 2: "februari", 3: "maart", 4: "april",
    5: "mei", 6: "juni", 7: "juli", 8: "augustus",
    9: "september", 10: "oktober", 11: "november", 12: "december",
}


def fmt_eur(amount):
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def dutch_date(d):
    return f"{d.day} {DUTCH_MONTHS[d.month]} {d.year}"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def next_factuurnummer(persist=True):
    teller_path = BASE_DIR / "factuur_teller.json"
    teller = load_json(teller_path)
    teller["laatste_nummer"] += 1
    if persist:
        save_json(teller_path, teller)
    today = date.today()
    return f"{today.year}{today.month:02d}{teller['laatste_nummer']:04d}"


def bereken_periode(facturatie_dag, today=None):
    today = today or date.today()
    start = today.replace(day=facturatie_dag)
    if today.month == 12:
        eind = start.replace(year=today.year + 1, month=1) - timedelta(days=1)
    else:
        eind = start.replace(month=today.month + 1) - timedelta(days=1)
    return f"{dutch_date(start)} t/m {dutch_date(eind)}"


def generate_pdf(klant, factuurnummer, today=None):
    today = today or date.today()
    logo_path = BASE_DIR / "assets" / "logo.png"
    template_path = BASE_DIR / "templates" / "factuur.html"

    with open(logo_path, "rb") as f:
        logo_b64 = base64.b64encode(f.read()).decode()

    with open(template_path, encoding="utf-8") as f:
        template = Template(f.read())

    vervaldatum = today + timedelta(days=14)
    periode = bereken_periode(klant["facturatie_dag"], today)

    posten = []
    subtotaal = 0.0
    for post in klant["posten"]:
        btw = round(post["bedrag_excl"] * post["btw_pct"] / 100, 2)
        incl = round(post["bedrag_excl"] + btw, 2)
        posten.append({
            **post,
            "btw": btw,
            "bedrag_incl": incl,
            "bedrag_excl_fmt": fmt_eur(post["bedrag_excl"]),
            "bedrag_incl_fmt": fmt_eur(incl),
        })
        subtotaal += post["bedrag_excl"]

    btw_totaal = round(sum(p["btw"] for p in posten), 2)
    totaal = round(subtotaal + btw_totaal, 2)

    html = template.render(
        logo_b64=logo_b64,
        factuurnummer=factuurnummer,
        factuurdatum=f"{today.day:02d}/{today.month:02d}/{today.year}",
        vervaldatum=f"{vervaldatum.day:02d}/{vervaldatum.month:02d}/{vervaldatum.year}",
        klant=klant,
        posten=posten,
        periode=periode,
        subtotaal_fmt=fmt_eur(subtotaal),
        btw_totaal_fmt=fmt_eur(btw_totaal),
        totaal_fmt=fmt_eur(totaal),
        totaal=totaal,
        betaallink=klant.get("eerste_betaallink", ""),
    )

    pdf_dir = BASE_DIR / "facturen"
    pdf_dir.mkdir(exist_ok=True)
    pdf_path = pdf_dir / f"factuur-{factuurnummer}.pdf"

    HTML(string=html).write_pdf(str(pdf_path))
    return pdf_path, totaal


def send_email(klant, factuurnummer, pdf_path, totaal):
    recipient = TEST_EMAIL or klant["email"]
    subject = f"Factuur {factuurnummer} - ekkovoice"
    if DRY_RUN:
        subject = f"[TEST] {subject}"

    msg = MIMEMultipart()
    msg["From"] = f"ekkovoice <{MAIL_FROM}>"
    msg["To"] = recipient
    msg["Subject"] = subject

    aanhef = klant.get("aanhef", klant["naam"])
    betaallink = klant.get("eerste_betaallink", "")

    if betaallink:
        body = (
            f"Beste {aanhef},\n\n"
            f"Bijgaand ontvang je factuur {factuurnummer} voor het maandabonnement bij ekkovoice.\n\n"
            f"Betaal via onderstaande iDEAL-link. Door te betalen geef je direct de machtiging voor automatische maandelijkse incasso, zodat je hier verder niets meer voor hoeft te doen.\n\n"
            f"Betaallink: {betaallink}\n\n"
            f"Met vriendelijke groet,\n"
            f"Enes Dere\n"
            f"ekkovoice\n"
            f"enes@ekkovoice.nl | +31 6 365 97990"
        )
    else:
        body = (
            f"Beste {aanhef},\n\n"
            f"Bijgaand ontvang je factuur {factuurnummer} voor het maandabonnement bij ekkovoice.\n\n"
            f"Het bedrag van {fmt_eur(totaal)} euro wordt automatisch via SEPA-incasso afgeschreven.\n\n"
            f"Met vriendelijke groet,\n"
            f"Enes Dere\n"
            f"ekkovoice\n"
            f"enes@ekkovoice.nl | +31 6 365 97990"
        )

    msg.attach(MIMEText(body, "plain", "utf-8"))

    with open(pdf_path, "rb") as f:
        attachment = MIMEApplication(f.read(), _subtype="pdf")
        attachment.add_header(
            "Content-Disposition", "attachment",
            filename=f"Factuur {factuurnummer}.pdf"
        )
        msg.attach(attachment)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

    print(f"Email verstuurd naar {recipient}")


def mollie_eerste_betaallink(klant, totaal):
    resp = requests.post(
        "https://api.mollie.com/v2/payments",
        headers={"Authorization": f"Bearer {MOLLIE_API_KEY}"},
        json={
            "amount": {"currency": "EUR", "value": f"{totaal:.2f}"},
            "customerId": klant["mollie_customer_id"],
            "sequenceType": "first",
            "description": "Eerste betaling + SEPA-machtiging ekkovoice maandabonnement",
            "redirectUrl": "https://ekkovoice.nl",
        },
    )
    resp.raise_for_status()
    return resp.json()["_links"]["checkout"]["href"]


def mollie_charge(klant, totaal, factuurnummer):
    if DRY_RUN:
        print(f"DRY RUN: Mollie/SEPA-incasso overgeslagen voor {klant['naam']} (EUR {totaal:.2f})")
        return

    customer_id = klant.get("mollie_customer_id")
    mandate_id = klant.get("mollie_mandate_id")

    if not customer_id or not mandate_id:
        print(f"SKIP Mollie voor {klant['naam']}: geen mandate. Voer eerst mollie_setup.py uit.")
        return

    resp = requests.post(
        "https://api.mollie.com/v2/payments",
        headers={"Authorization": f"Bearer {MOLLIE_API_KEY}"},
        json={
            "amount": {"currency": "EUR", "value": f"{totaal:.2f}"},
            "customerId": customer_id,
            "mandateId": mandate_id,
            "sequenceType": "recurring",
            "description": f"Factuur {factuurnummer} - ekkovoice maandabonnement",
            "redirectUrl": "https://ekkovoice.nl",
        },
    )

    if resp.status_code == 201:
        payment = resp.json()
        print(f"Mollie incasso aangemaakt: {payment['id']} | EUR {totaal:.2f}")
    else:
        print(f"Mollie FOUT {resp.status_code}: {resp.text}")
        raise RuntimeError(f"Mollie charge mislukt voor {klant['naam']}")


def run(force=False):
    force = force or ENV_FORCE
    if DRY_RUN:
        print("=== TESTMODUS (DRY RUN): geen echte incasso, mail naar test-adres ===")

    klanten_data = load_json(BASE_DIR / "klanten.json")
    today = date.today()

    verwerkt = 0
    for klant in klanten_data["klanten"]:
        if not klant.get("actief"):
            continue
        if not force and klant.get("facturatie_dag") != today.day:
            print(f"Geen facturatie vandaag voor {klant['naam']} (dag {klant['facturatie_dag']}).")
            continue

        print(f"Verwerken: {klant['naam']}")

        factuurnummer = next_factuurnummer(persist=not DRY_RUN)

        # Genereer verse betaallink als er nog geen mandate is
        heeft_mandate = bool(klant.get("mollie_customer_id") and klant.get("mollie_mandate_id"))
        if not heeft_mandate and klant.get("mollie_customer_id") and not DRY_RUN:
            klant["eerste_betaallink"] = mollie_eerste_betaallink(klant, 240.79)
        else:
            klant.pop("eerste_betaallink", None)

        pdf_path, totaal = generate_pdf(klant, factuurnummer, today)
        print(f"PDF gegenereerd: {pdf_path.name}")

        send_email(klant, factuurnummer, pdf_path, totaal)
        mollie_charge(klant, totaal, factuurnummer)

        verwerkt += 1
        print(f"Klaar: {klant['naam']} | {factuurnummer} | EUR {totaal:.2f}")

    if verwerkt == 0:
        print(f"Niets te doen op dag {today.day}.")


if __name__ == "__main__":
    force = "--force" in sys.argv
    run(force=force)

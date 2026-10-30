#!/usr/bin/env python3
"""
Passvakt - overvaker ledige passtimer hos politiet og varsler deg.

Kjor:
  python passvakt.py --oppsett   Viser tjenester og passkontor som matcher innstillingene
  python passvakt.py --test      Sender et testvarsel til telefonen
  python passvakt.py             Starter overvakingen (la vinduet sta apent)
  python passvakt.py --en-gang   Sjekker en gang og husker hva som er varslet (brukes av GitHub)
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime

# ================= INNSTILLINGER =================

# Passkontor du kan mote pa (del av navnet holder, store/sma bokstaver spiller ingen rolle)
KONTORER = ["Sandnes", "Stavanger", "Bryne", "Sola"]

# Hvilken tjeneste (del av navnet). Kjor --oppsett for a se hva som finnes.
TJENESTE = "pass"

# Varsle bare om timer FOR denne datoen (AAAA-MM-DD). Tom streng = alle timer.
SENEST_DATO = "2027-02-01"

# Hvor ofte det sjekkes (minutter). 3-5 er passe, ikke ga lavere.
INTERVALL_MIN = 3

# Push-varsel til mobilen via ntfy-appen (gratis, ingen konto).
# Velg et unikt, vanskelig a gjette navn og abonner pa samme navn i appen.
# Pa GitHub hentes navnet fra hemmeligheten NTFY_EMNE, sa det ikke blir synlig.
NTFY_EMNE = os.environ.get("NTFY_EMNE", "")

# Apne bookingsiden i nettleseren automatisk nar en time dukker opp (ikke pa GitHub)
APNE_NETTLESER = not os.environ.get("CI")

# Fil som husker hvilke timer det allerede er varslet om (brukes av --en-gang)
TILSTANDSFIL = "passvakt_state.json"

# Reserveplan: lim inn "dates"-URL(er) fra nettleserens utviklerverktoy
# hvis --oppsett ikke finner kontorene. Da brukes disse i stedet.
MANUELLE_URLER = []

# =================================================

BASE = "https://pass-og-id.politiet.no/qmaticwebbooking/rest/schedule"
BOOKING_URL = "https://pass-og-id.politiet.no/timebestilling/index.html#/"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (passvakt; privat bruk)",
}


def hent_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8") or "[]")


def navn(obj):
    return (obj.get("name") or obj.get("publicName") or "").strip()


def finn_tjenester():
    tjenester = hent_json(f"{BASE}/services")
    return [t for t in tjenester if TJENESTE.lower() in navn(t).lower()]


def finn_kontorer(service_id):
    try:
        alle = hent_json(f"{BASE}/branches;servicePublicId={service_id}")
    except urllib.error.HTTPError:
        alle = hent_json(f"{BASE}/branches")
    return [b for b in alle if any(k.lower() in navn(b).lower() for k in KONTORER)]


def bygg_mal():
    """Returnerer liste med (etikett, dates_url, times_url_mal)."""
    if MANUELLE_URLER:
        mal = []
        for i, u in enumerate(MANUELLE_URLER, 1):
            u = u.split(";customSlotLength")[0]
            times = u.replace("/dates;", "/dates/{dato}/times;")
            mal.append((f"Kontor {i}", u, times))
        return mal

    mal = []
    for t in finn_tjenester():
        sid = t["publicId"]
        for b in finn_kontorer(sid):
            bid = b["publicId"]
            etikett = f"{navn(b)} ({navn(t)})"
            dates = f"{BASE}/branches/{bid}/dates;servicePublicId={sid}"
            times = f"{BASE}/branches/{bid}/dates/{{dato}}/times;servicePublicId={sid}"
            mal.append((etikett, dates, times))
    return mal


def hent_tider(times_mal, dato):
    try:
        data = hent_json(times_mal.format(dato=dato))
        return [str(x.get("time", ""))[:5] for x in data if x.get("time")]
    except Exception:
        return []


def varsle(tittel, tekst):
    print("\a")
    print("=" * 60)
    print(tittel)
    print(tekst)
    print(f"Book her: {BOOKING_URL}")
    print("=" * 60, flush=True)
    if NTFY_EMNE:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(NTFY_EMNE)}",
                data=tekst.encode("utf-8"),
                headers={
                    "Title": tittel.encode("ascii", "ignore").decode(),
                    "Priority": "high",
                    "Tags": "passport_control",
                    "Click": BOOKING_URL,
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"(Klarte ikke sende push-varsel: {e})")
    if APNE_NETTLESER:
        try:
            webbrowser.open(BOOKING_URL)
        except Exception:
            pass


def oppsett():
    print("Tjenester som matcher", repr(TJENESTE))
    try:
        tjenester = finn_tjenester()
    except Exception as e:
        print(f"  Klarte ikke hente tjenester: {e}")
        print("  Bruk reserveplanen med MANUELLE_URLER (se README).")
        return
    for t in tjenester:
        print(f"  - {navn(t)}")
        try:
            for b in finn_kontorer(t["publicId"]):
                print(f"      * {navn(b)}")
        except Exception as e:
            print(f"      Klarte ikke hente kontorer: {e}")
    if not tjenester:
        print("  Ingen treff. Prov et annet ord i TJENESTE.")


def hent_mal_eller_avslutt():
    try:
        mal = bygg_mal()
    except Exception as e:
        print(f"Klarte ikke hente kontorlisten fra politiet: {e}")
        sys.exit(1)
    if not mal:
        print("Fant ingen kontor a overvake. Kjor --oppsett eller bruk MANUELLE_URLER.")
        sys.exit(1)
    return mal


def sjekk(mal, sett):
    """En runde. Returnerer (nye funn, aktive nokler, antall feil)."""
    nye, aktive, feil = [], set(), 0
    for etikett, dates_url, times_mal in mal:
        try:
            datoer = [str(d.get("date", d))[:10] for d in hent_json(dates_url)]
        except Exception as e:
            feil += 1
            print(f"{datetime.now():%H:%M} Feil for {etikett}: {e}", flush=True)
            continue
        for dato in datoer:
            if SENEST_DATO and dato >= SENEST_DATO:
                continue
            nokkel = (etikett, dato)
            aktive.add(nokkel)
            if nokkel not in sett:
                nye.append((etikett, dato, hent_tider(times_mal, dato)))
        time.sleep(1)
    return nye, aktive, feil


def rapporter(nye):
    if nye:
        linjer = []
        for etikett, dato, tider in sorted(nye, key=lambda x: x[1]):
            t = ", ".join(tider[:6]) if tider else "se bookingsiden"
            linjer.append(f"{dato} - {etikett}: {t}")
        varsle("Ledig passtime!", "\n".join(linjer))
    else:
        print(f"{datetime.now():%H:%M} Ingen nye timer.", flush=True)


def en_gang():
    mal = hent_mal_eller_avslutt()
    print("Sjekker:", ", ".join(e for e, _, _ in mal))
    try:
        with open(TILSTANDSFIL, encoding="utf-8") as f:
            sett = {tuple(x) for x in json.load(f)}
    except (FileNotFoundError, ValueError):
        sett = set()
    nye, aktive, feil = sjekk(mal, sett)
    if feil == len(mal):
        print("Alle oppslag feilet, beholder tidligere tilstand.")
        sys.exit(1)
    rapporter(nye)
    # Glem timer som er borte igjen, slik at de varsles pa nytt hvis de kommer tilbake
    ny_tilstand = (sett & aktive) | {(e, d) for e, d, _ in nye}
    if ny_tilstand != sett:
        with open(TILSTANDSFIL, "w", encoding="utf-8") as f:
            json.dump(sorted(ny_tilstand), f, ensure_ascii=False, indent=1)


def overvak():
    mal = hent_mal_eller_avslutt()
    print(f"Overvaker {len(mal)} kontor/tjeneste-kombinasjoner hvert {INTERVALL_MIN}. minutt:")
    for etikett, _, _ in mal:
        print(f"  - {etikett}")
    if SENEST_DATO:
        print(f"Varsler om timer for {SENEST_DATO}.")
    print("Trykk Ctrl+C for a stoppe.\n", flush=True)

    sett = set()
    while True:
        nye, aktive, feil = sjekk(mal, sett)
        sett = (sett & aktive) | {(e, d) for e, d, _ in nye}
        rapporter(nye)
        vent = INTERVALL_MIN * 60 + random.randint(0, 45)
        if feil == len(mal):
            vent = max(vent, 15 * 60)  # serveren sliter, ta det rolig
        time.sleep(vent)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--oppsett":
        oppsett()
    elif arg == "--test":
        varsle("Passvakt test", "Testvarsel: varslingen fungerer.")
    elif arg == "--en-gang":
        en_gang()
    else:
        try:
            overvak()
        except KeyboardInterrupt:
            print("\nStoppet.")

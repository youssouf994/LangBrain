"""
Scenari della simulazione.

`scenario_italia()` descrive l'Italia con dati PLAUSIBILI, NON REALI: zone di mercato, limiti di transito, parchi e
potenze sono ispirati all'ordine di grandezza del sistema italiano (~310 TWh/anno di domanda elettrica, ~62 miliardi
di m³ di gas) ma semplificati e arrotondati. La centrale nucleare di Trino è ipotetica.

`scenario_generato()` costruisce una rete sintetica di dimensione arbitraria per le prove di carico.
"""

import math
import random

from app.simulazione import ambiente
from app.simulazione.modello import (
    Accumulo, Centrale, Citta, Condotta, IngressoGas, Linea, Scenario, StoccaggioGas, Zona,
)

DOMANDA_ELETTRICA_MEDIA_MW = 35_400.0  # ≈ 310 TWh/anno
GAS_RESIDENZIALE_MEDIO = 3_400.0        # kSm³/h ≈ 30 miliardi di m³/anno (civile e terziario)
GAS_INDUSTRIALE_MEDIO = 1_400.0         # kSm³/h ≈ 12 miliardi di m³/anno
QUOTA_GAS_BASE = 0.22                   # parte del consumo civile che non dipende dalla temperatura


def gradi_riscaldamento_medi(zona: Zona) -> float:
    """Media annua dei gradi di riscaldamento orari della zona, con la temperatura senza anomalie."""
    totale, campioni = 0.0, 0
    for giorno in range(1, 366, 2):
        for ora in range(0, 24, 3):
            t = ambiente.temperatura_base(zona.temp_media, zona.temp_ampiezza, zona.temp_giornaliera, giorno, ora)
            totale += ambiente.gradi_riscaldamento(t)
            campioni += 1
    return totale / campioni


def distribuisci_domanda(scenario: Scenario, domanda_totale: float, gas_residenziale: float, gas_industriale: float,
                         industria: dict[str, float], quote_zona: dict[str, float] | None = None) -> None:
    """
    Assegna a ogni città la domanda elettrica media (per popolazione pesata sull'industria) e i parametri del gas,
    calibrati in modo che la media nazionale corrisponda ai totali indicati. Con `quote_zona` la domanda elettrica
    di ogni zona è fissata e le città della zona se la spartiscono.
    """
    zone = {z.id: z for z in scenario.zone}
    pesi = {c.id: c.popolazione * (1 + 0.35 * industria.get(c.id, 0.3)) for c in scenario.citta}
    if quote_zona:
        somma_quote = sum(quote_zona.values())
        for zid, quota in quote_zona.items():
            nella_zona = [c for c in scenario.citta if c.zona == zid]
            peso_zona = sum(pesi[c.id] for c in nella_zona) or 1.0
            for c in nella_zona:
                c.domanda_media_mw = domanda_totale * quota / somma_quote * pesi[c.id] / peso_zona
    else:
        somma_pesi = sum(pesi.values()) or 1.0
        for c in scenario.citta:
            c.domanda_media_mw = domanda_totale * pesi[c.id] / somma_pesi

    con_gas = [c for c in scenario.citta if zone[c.zona].rete_gas]
    popolazione_gas = sum(c.popolazione for c in con_gas) or 1.0
    gradi = {z.id: gradi_riscaldamento_medi(z) for z in scenario.zone}
    somma_gradi = sum(c.popolazione * gradi[c.zona] for c in con_gas) or 1.0
    peso_industria = sum(industria.get(c.id, 0.3) for c in con_gas) or 1.0
    for c in con_gas:
        c.gas_base = gas_residenziale * QUOTA_GAS_BASE * c.popolazione / popolazione_gas
        c.gas_riscaldamento = gas_residenziale * (1 - QUOTA_GAS_BASE) * c.popolazione / somma_gradi
        c.gas_industria = gas_industriale * industria.get(c.id, 0.3) / peso_industria


def _slug(testo: str) -> str:
    pulito = "".join(ch.lower() if ch.isalnum() else "_" for ch in testo)
    return "_".join(p for p in pulito.split("_") if p)


def scenario_italia() -> Scenario:
    s = Scenario(
        nome="Italia",
        descrizione="Sistema elettrico e gas italiano con dati plausibili ma non reali, su 7 zone di mercato.",
    )
    s.zone = [
        Zona("NORD", "Nord", 9.9, 45.4, 13.0, 10.5, 5.0, 4.9, 0.60, 0.30, linepack_max=22000),
        Zona("CNOR", "Centro-Nord", 11.4, 43.6, 15.0, 9.0, 5.0, 5.6, 0.52, 0.22, linepack_max=7000),
        Zona("CSUD", "Centro-Sud", 13.6, 41.8, 16.0, 8.5, 5.0, 6.4, 0.48, 0.18, linepack_max=9000),
        Zona("SUD", "Sud", 16.3, 40.9, 17.0, 8.0, 5.0, 7.4, 0.42, 0.15, linepack_max=6000),
        Zona("CALA", "Calabria", 16.4, 38.9, 18.0, 7.0, 4.5, 7.1, 0.42, 0.12, linepack_max=4000),
        Zona("SICI", "Sicilia", 14.2, 37.5, 18.5, 7.0, 4.5, 7.3, 0.40, 0.10, linepack_max=5000),
        Zona("SARD", "Sardegna", 9.0, 40.1, 17.0, 7.5, 5.0, 7.9, 0.42, 0.12, rete_gas=False, linepack_max=0),
    ]

    # (nome, zona, lon, lat, popolazione in milioni, peso industriale)
    citta = [
        ("Milano", "NORD", 9.19, 45.46, 4.2, 1.0), ("Torino", "NORD", 7.68, 45.07, 2.3, 0.8),
        ("Genova", "NORD", 8.95, 44.41, 0.9, 0.4), ("Venezia-Padova", "NORD", 12.1, 45.45, 2.6, 0.9),
        ("Bologna", "NORD", 11.34, 44.49, 1.9, 0.8), ("Brescia-Bergamo", "NORD", 10.0, 45.62, 2.4, 1.3),
        ("Verona", "NORD", 10.99, 45.44, 1.3, 0.7), ("Firenze", "CNOR", 11.25, 43.77, 1.9, 0.5),
        ("Pisa-Livorno", "CNOR", 10.4, 43.62, 0.9, 0.4), ("Ancona-Perugia", "CNOR", 13.0, 43.4, 1.6, 0.4),
        ("Roma", "CSUD", 12.5, 41.9, 4.5, 0.4), ("Napoli", "CSUD", 14.27, 40.85, 3.6, 0.6),
        ("Pescara", "CSUD", 14.2, 42.46, 1.1, 0.4), ("Bari", "SUD", 16.87, 41.12, 1.6, 0.4),
        ("Taranto", "SUD", 17.24, 40.47, 0.9, 1.4), ("Foggia-Potenza", "SUD", 15.7, 41.1, 1.1, 0.2),
        ("Reggio Calabria", "CALA", 15.65, 38.11, 0.9, 0.1), ("Cosenza-Catanzaro", "CALA", 16.4, 39.1, 1.0, 0.2),
        ("Palermo", "SICI", 13.36, 38.12, 2.0, 0.2), ("Catania-Siracusa", "SICI", 15.09, 37.4, 2.0, 0.6),
        ("Cagliari", "SARD", 9.11, 39.22, 0.9, 0.4), ("Sassari", "SARD", 8.56, 40.73, 0.7, 0.2),
    ]
    industria = {}
    for nome, zona, x, y, pop, ind in citta:
        cid = f"citta:{_slug(nome)}"
        s.citta.append(Citta(cid, nome, zona, x, y, pop))
        industria[cid] = ind

    # (nome, tipo, zona, lon, lat, MW, flotta, bacino MWh)
    centrali = [
        # Nord
        ("Fotovoltaico Pianura Padana", "solare", "NORD", 10.6, 45.1, 13000, True, 0),
        ("Eolico Appennino settentrionale", "eolico", "NORD", 9.6, 44.6, 400, True, 0),
        ("Idroelettrico fluente alpino", "idro_fluente", "NORD", 9.4, 46.1, 5500, True, 0),
        ("Bacini della Valtellina", "idro_bacino", "NORD", 10.2, 46.35, 4000, True, 1_800_000),
        ("Bacini del Piemonte", "idro_bacino", "NORD", 7.5, 45.85, 2500, True, 900_000),
        ("Turbigo", "gas", "NORD", 8.73, 45.53, 1700, False, 0),
        ("Tavazzano", "gas", "NORD", 9.40, 45.33, 1400, False, 0),
        ("Sermide", "gas", "NORD", 11.28, 45.02, 1150, False, 0),
        ("Ostiglia", "gas", "NORD", 11.14, 45.07, 1100, False, 0),
        ("La Casella", "gas", "NORD", 9.45, 45.1, 1500, False, 0),
        ("Fusina", "gas", "NORD", 12.25, 45.43, 1000, False, 0),
        ("Flotta gas Nord", "gas", "NORD", 10.3, 45.25, 9500, True, 0),
        ("Biomasse Nord", "biomasse", "NORD", 11.2, 45.65, 1800, True, 0),
        ("Trino (nucleare, ipotetica)", "nucleare", "NORD", 8.30, 45.19, 1000, False, 0),
        ("Interconnessione Francia-Svizzera", "import", "NORD", 7.0, 45.95, 6500, False, 0),
        ("Interconnessione Austria-Slovenia", "import", "NORD", 13.6, 46.4, 1500, False, 0),
        # Centro-Nord
        ("Fotovoltaico Centro-Nord", "solare", "CNOR", 12.2, 43.3, 3800, True, 0),
        ("Eolico Toscana-Marche", "eolico", "CNOR", 11.8, 43.0, 400, True, 0),
        ("Larderello", "geotermico", "CNOR", 10.87, 43.24, 800, False, 0),
        ("Idroelettrico Appennino centrale", "idro_fluente", "CNOR", 12.7, 43.1, 700, True, 0),
        ("Livorno", "gas", "CNOR", 10.33, 43.53, 800, False, 0),
        ("Flotta gas Centro-Nord", "gas", "CNOR", 11.4, 43.85, 2500, True, 0),
        ("Biomasse Centro-Nord", "biomasse", "CNOR", 11.9, 43.9, 500, True, 0),
        # Centro-Sud
        ("Fotovoltaico Lazio-Campania", "solare", "CSUD", 13.1, 41.5, 6000, True, 0),
        ("Eolico Campania-Molise", "eolico", "CSUD", 14.8, 41.35, 3000, True, 0),
        ("Bacini dell'Abruzzo", "idro_bacino", "CSUD", 13.8, 42.2, 1200, True, 400_000),
        ("Idroelettrico fluente Centro-Sud", "idro_fluente", "CSUD", 13.4, 42.0, 600, True, 0),
        ("Montalto di Castro", "gas", "CSUD", 11.62, 42.35, 3600, False, 0),
        ("Sparanise", "gas", "CSUD", 14.1, 41.19, 800, False, 0),
        ("Flotta gas Centro-Sud", "gas", "CSUD", 13.3, 41.65, 3000, True, 0),
        ("Torrevaldaliga Nord", "carbone", "CSUD", 11.77, 42.13, 1980, False, 0),
        ("Interconnessione Montenegro", "import", "CSUD", 14.7, 42.4, 600, False, 0),
        # Sud
        ("Fotovoltaico Puglia-Basilicata", "solare", "SUD", 16.5, 40.75, 7000, True, 0),
        ("Eolico Puglia-Basilicata", "eolico", "SUD", 15.7, 41.0, 4600, True, 0),
        ("Candela", "gas", "SUD", 15.51, 41.14, 400, False, 0),
        ("Modugno", "gas", "SUD", 16.78, 41.08, 800, False, 0),
        ("Flotta gas Sud", "gas", "SUD", 16.4, 40.55, 3500, True, 0),
        ("Brindisi Sud", "carbone", "SUD", 18.03, 40.56, 1320, False, 0),
        ("Biomasse Sud", "biomasse", "SUD", 16.2, 40.35, 300, True, 0),
        ("Interconnessione Grecia", "import", "SUD", 18.35, 40.15, 500, False, 0),
        # Calabria
        ("Eolico Calabria", "eolico", "CALA", 16.65, 39.05, 1300, True, 0),
        ("Fotovoltaico Calabria", "solare", "CALA", 16.25, 38.6, 1000, True, 0),
        ("Rossano", "gas", "CALA", 16.63, 39.6, 1600, False, 0),
        ("Flotta gas Calabria", "gas", "CALA", 16.95, 39.1, 800, True, 0),
        ("Bacini della Sila", "idro_bacino", "CALA", 16.55, 39.35, 700, True, 200_000),
        # Sicilia
        ("Fotovoltaico Sicilia", "solare", "SICI", 14.3, 37.3, 3000, True, 0),
        ("Eolico Sicilia", "eolico", "SICI", 13.3, 37.8, 2200, True, 0),
        ("Priolo Gargallo", "gas", "SICI", 15.2, 37.15, 1400, False, 0),
        ("Flotta gas Sicilia", "gas", "SICI", 13.9, 37.65, 2500, True, 0),
        # Sardegna
        ("Fotovoltaico Sardegna", "solare", "SARD", 9.0, 39.6, 1400, True, 0),
        ("Eolico Sardegna", "eolico", "SARD", 8.9, 40.55, 1250, True, 0),
        ("Fiume Santo", "carbone", "SARD", 8.47, 40.84, 600, False, 0),
        ("Sulcis Portovesme", "carbone", "SARD", 8.4, 39.2, 480, False, 0),
        ("Biomasse Sardegna", "biomasse", "SARD", 9.2, 40.3, 250, True, 0),
    ]
    rng = random.Random(1861)  # manutenzioni programmate fisse per lo scenario
    for nome, tipo, zona, x, y, mw, flotta, bacino in centrali:
        # I cicli combinati singoli sono moderni; le flotte mescolano impianti vecchi e turbogas di punta.
        rendimento = (0.46 if flotta else round(rng.uniform(0.53, 0.58), 3)) if tipo == "gas" else 0.0
        s.centrali.append(Centrale(
            f"centrale:{_slug(nome)}", nome, tipo, zona, x, y, mw, flotta=flotta, bacino_mwh=bacino,
            giorno_manutenzione=rng.choice([rng.randint(60, 140), rng.randint(250, 320)]), rendimento=rendimento,
        ))

    s.accumuli = [
        Accumulo("accumulo:edolo", "Edolo (pompaggio)", "pompaggio", "NORD", 10.33, 46.18, 1000, 8000, 0.76),
        Accumulo("accumulo:entracque", "Entracque (pompaggio)", "pompaggio", "NORD", 7.40, 44.24, 1300, 10000, 0.76),
        Accumulo("accumulo:bess_nord", "Batterie Nord", "batteria", "NORD", 9.9, 45.05, 600, 2400, 0.88),
        Accumulo("accumulo:presenzano", "Presenzano (pompaggio)", "pompaggio", "CSUD", 14.07, 41.37, 1000, 6000, 0.76),
        Accumulo("accumulo:bess_csud", "Batterie Centro-Sud", "batteria", "CSUD", 12.9, 41.6, 400, 1600, 0.88),
        Accumulo("accumulo:bess_sud", "Batterie Sud", "batteria", "SUD", 16.8, 40.5, 900, 3600, 0.88),
        Accumulo("accumulo:bess_sicilia", "Batterie Sicilia", "batteria", "SICI", 14.6, 37.2, 700, 2800, 0.88),
        Accumulo("accumulo:bess_sardegna", "Batterie Sardegna", "batteria", "SARD", 9.2, 39.9, 500, 2000, 0.88),
    ]
    s.linee = [
        Linea("linea:NORD-CNOR", "NORD", "CNOR", 4200),
        Linea("linea:CNOR-CSUD", "CNOR", "CSUD", 2900),
        Linea("linea:CSUD-SUD", "CSUD", "SUD", 5000),
        Linea("linea:SUD-CALA", "SUD", "CALA", 2700),
        Linea("linea:CALA-SICI", "CALA", "SICI", 1100),
        Linea("linea:CSUD-SARD", "CSUD", "SARD", 900),
        Linea("linea:CNOR-SARD", "CNOR", "SARD", 300),
    ]
    s.ingressi = [
        IngressoGas("ingresso:mazara", "Mazara del Vallo (Algeria)", "gasdotto", "SICI", 12.59, 37.65, 3000),
        IngressoGas("ingresso:gela", "Gela (Libia)", "gasdotto", "SICI", 14.25, 37.07, 450),
        IngressoGas("ingresso:melendugno", "Melendugno (TAP)", "gasdotto", "SUD", 18.33, 40.27, 1250),
        IngressoGas("ingresso:tarvisio", "Tarvisio (Austria)", "gasdotto", "NORD", 13.58, 46.5, 1200),
        IngressoGas("ingresso:passo_gries", "Passo Gries (Nord Europa)", "gasdotto", "NORD", 8.4, 46.45, 1500),
        IngressoGas("ingresso:porto_viro", "Rigassificatore Porto Viro", "gnl", "NORD", 12.55, 45.08, 1000, 0.2),
        IngressoGas("ingresso:panigaglia", "Rigassificatore Panigaglia", "gnl", "NORD", 9.85, 44.07, 400, 0.2),
        IngressoGas("ingresso:ravenna", "Rigassificatore Ravenna", "gnl", "NORD", 12.4, 44.5, 570, 0.2),
        IngressoGas("ingresso:livorno", "Rigassificatore Livorno", "gnl", "CNOR", 10.1, 43.6, 570, 0.2),
        IngressoGas("ingresso:piombino", "Rigassificatore Piombino", "gnl", "CNOR", 10.52, 42.93, 570, 0.2),
        IngressoGas("ingresso:adriatico", "Produzione nazionale Adriatico", "produzione", "CSUD", 14.4, 42.75, 350, 0.9),
    ]
    s.stoccaggi = [
        StoccaggioGas("stoccaggio:pianura_padana", "Stoccaggi Pianura Padana", "NORD", 9.75, 45.15, 14000, 6500, 3600),
        StoccaggioGas("stoccaggio:fiume_treste", "Stoccaggio Fiume Treste", "CSUD", 14.6, 41.95, 3000, 1500, 700),
    ]
    s.condotte = [
        Condotta("condotta:SICI-CALA", "SICI", "CALA", 5000),
        Condotta("condotta:CALA-SUD", "CALA", "SUD", 5000),
        Condotta("condotta:SUD-CSUD", "SUD", "CSUD", 4500),
        Condotta("condotta:CSUD-CNOR", "CSUD", "CNOR", 4500),
        Condotta("condotta:CNOR-NORD", "CNOR", "NORD", 5500),
        Condotta("condotta:SUD-NORD", "SUD", "NORD", 3000),
    ]
    quote_zona = {"NORD": 0.55, "CNOR": 0.105, "CSUD": 0.17, "SUD": 0.08, "CALA": 0.02, "SICI": 0.06, "SARD": 0.03}
    distribuisci_domanda(s, DOMANDA_ELETTRICA_MEDIA_MW, GAS_RESIDENZIALE_MEDIO, GAS_INDUSTRIALE_MEDIO, industria, quote_zona)
    return s


# ---------------------------------------------------------------------------------------------- reti generate

_MIX_GENERATO = (  # (tipo, peso nel numero di impianti, quota della potenza installata rispetto alla domanda media, flotta)
    ("solare", 0.24, 0.62, True), ("eolico", 0.16, 0.30, True), ("idro_fluente", 0.06, 0.12, True),
    ("idro_bacino", 0.05, 0.14, True), ("geotermico", 0.02, 0.02, False), ("biomasse", 0.05, 0.06, True),
    ("nucleare", 0.02, 0.06, False), ("gas", 0.30, 0.75, False), ("carbone", 0.05, 0.08, False), ("import", 0.05, 0.12, False),
)


def scenario_generato(zone: int = 20, citta: int = 200, centrali: int = 120, seed: int = 7) -> Scenario:
    """
    Rete sintetica per le prove di carico: zone sparse nel riquadro dell'Italia, collegate alle più vicine;
    città e centrali distribuite a caso; parco termico dimensionato perché la rete regga la punta.
    """
    zone, citta, centrali = max(2, int(zone)), max(1, int(citta)), max(1, int(centrali))
    rng = random.Random(seed)
    s = Scenario(
        nome="Rete generata",
        descrizione=f"Rete sintetica per prove di carico: {zone} zone, {citta} città, {centrali} centrali (seed {seed}).",
    )
    for i in range(zone):
        x, y = rng.uniform(6.8, 18.4), rng.uniform(36.8, 46.8)
        media = 13 + (45 - y) * 0.55
        s.zone.append(Zona(
            f"Z{i:03d}", f"Zona {i + 1}", x, y, media, rng.uniform(7, 10.5), rng.uniform(4, 5.5),
            rng.uniform(4.5, 8), rng.uniform(0.38, 0.6), rng.uniform(0.1, 0.3),
            rete_gas=rng.random() > 0.08, linepack_max=rng.uniform(3000, 12000),
        ))
    if not any(z.rete_gas for z in s.zone):
        s.zone[0].rete_gas = True

    # Collegamenti: albero dei vicini più prossimi (rete connessa) più qualche anello.
    def distanza(a: Zona, b: Zona) -> float:
        return math.hypot(a.x - b.x, a.y - b.y)

    collegate, coppie = {0}, set()
    while len(collegate) < zone:
        a, b = min(
            ((i, j) for i in collegate for j in range(zone) if j not in collegate),
            key=lambda p: distanza(s.zone[p[0]], s.zone[p[1]]),
        )
        collegate.add(b)
        coppie.add((min(a, b), max(a, b)))
    for i in range(zone):
        vicini = sorted((j for j in range(zone) if j != i), key=lambda j: distanza(s.zone[i], s.zone[j]))[:2]
        for j in vicini:
            if rng.random() < 0.5:
                coppie.add((min(i, j), max(i, j)))

    for i in range(citta):
        z = rng.choice(s.zone)
        s.citta.append(Citta(
            f"citta:c{i:05d}", f"Città {i + 1}", z.id, z.x + rng.gauss(0, 0.35), z.y + rng.gauss(0, 0.3),
            round(rng.lognormvariate(-0.9, 0.8), 3),
        ))
    # Le popolazioni casuali vengono riscalate a una media di 25 000 abitanti: 3000 città fanno un paese come l'Italia.
    fattore = 0.025 * citta / sum(c.popolazione for c in s.citta)
    for c in s.citta:
        c.popolazione = round(c.popolazione * fattore, 5)
    industria = {c.id: rng.uniform(0.1, 1.4) for c in s.citta}
    popolazione = sum(c.popolazione for c in s.citta)
    domanda_totale = 1_000.0 * popolazione * 0.62  # ≈ 0,6 kW medi per abitante, come in Italia
    distribuisci_domanda(
        s, domanda_totale, GAS_RESIDENZIALE_MEDIO * popolazione / 59, GAS_INDUSTRIALE_MEDIO * popolazione / 59, industria,
    )

    tipi, pesi = [m[0] for m in _MIX_GENERATO], [m[1] for m in _MIX_GENERATO]
    dati = {m[0]: m for m in _MIX_GENERATO}
    zone_gas = [z for z in s.zone if z.rete_gas]
    scelti = [rng.choices(tipi, pesi)[0] for _ in range(centrali)]
    quanti = {t: scelti.count(t) for t in tipi}
    for i, tipo in enumerate(scelti):
        z = rng.choice(zone_gas if tipo == "gas" else s.zone)
        _, _, quota, flotta = dati[tipo]
        mw = max(1, round(domanda_totale * quota / quanti[tipo] * rng.uniform(0.5, 1.5)))
        s.centrali.append(Centrale(
            f"centrale:g{i:05d}", f"Centrale {i + 1} ({tipo})", tipo, z.id, z.x + rng.gauss(0, 0.4), z.y + rng.gauss(0, 0.35),
            mw, flotta=flotta, bacino_mwh=mw * 450 if tipo == "idro_bacino" else 0,
            giorno_manutenzione=rng.choice([rng.randint(60, 140), rng.randint(250, 320)]),
            rendimento=round(rng.uniform(0.42, 0.58), 3) if tipo == "gas" else 0.0,
        ))

    # Ogni zona deve poter reggere da sola la propria punta con impianti programmabili (più un margine del 15%):
    # le reti generate servono a misurare il motore, non a produrre blackout per mancanza di impianti.
    programmabili = {"gas", "carbone", "nucleare", "idro_bacino", "import", "biomasse", "geotermico"}
    for z in s.zone:
        punta = sum(c.domanda_media_mw for c in s.citta if c.zona == z.id) * 1.45
        firme = sum(c.potenza_mw for c in s.centrali if c.zona == z.id and c.tipo in programmabili)
        if firme < punta:
            tipo = "gas" if z.rete_gas else "carbone"
            mw = round((punta - firme) * 1.15) + 1
            s.centrali.append(Centrale(
                f"centrale:riserva_{z.id.lower()}", f"Riserva {tipo} {z.nome}", tipo, z.id, z.x + 0.15, z.y - 0.15, mw,
                flotta=True, rendimento=0.42 if tipo == "gas" else 0.0,
            ))

    for i in range(max(1, zone // 3)):
        z = rng.choice(s.zone)
        potenza = round(rng.uniform(200, 900))
        tipo = rng.choice(["batteria", "pompaggio"])
        s.accumuli.append(Accumulo(
            f"accumulo:a{i:04d}", f"Accumulo {i + 1}", tipo, z.id, z.x - 0.2, z.y + 0.2, potenza,
            potenza * (4 if tipo == "batteria" else 8), 0.88 if tipo == "batteria" else 0.76,
        ))

    domanda_media_zona = domanda_totale / zone
    for a, b in sorted(coppie):
        za, zb = s.zone[a], s.zone[b]
        s.linee.append(Linea(f"linea:{za.id}-{zb.id}", za.id, zb.id, round(domanda_media_zona * rng.uniform(0.3, 0.9))))
        if za.rete_gas and zb.rete_gas:
            s.condotte.append(Condotta(f"condotta:{za.id}-{zb.id}", za.id, zb.id, round(3000 * popolazione / 59 / zone * rng.uniform(1.5, 3))))

    civile_medio = (GAS_RESIDENZIALE_MEDIO + GAS_INDUSTRIALE_MEDIO) * popolazione / 59
    gas_medio = civile_medio + 0.17 * 0.45 * domanda_totale  # più le centrali a gas (circa metà della domanda elettrica)
    # Ogni zona con il gas ha un proprio ingresso (le condotte generate non garantiscono il transito necessario);
    # l'erogazione di punta degli stoccaggi copre l'inverno.
    for i, z in enumerate(zone_gas):
        tipo = rng.choice(["gasdotto", "gasdotto", "gnl"])
        s.ingressi.append(IngressoGas(
            f"ingresso:i{i:04d}", f"Ingresso {i + 1} ({tipo})", tipo, z.id, z.x + 0.3, z.y + 0.3,
            round(gas_medio * 2.6 / len(zone_gas) * rng.uniform(0.8, 1.2)), 0.3 if tipo == "gasdotto" else 0.2,
        ))
    for i, z in enumerate(zone_gas[::3] or zone_gas[:1]):
        capacita = gas_medio * 24 * 365 / 1000 * 0.28 / max(1, len(zone_gas[::3]))
        s.stoccaggi.append(StoccaggioGas(
            f"stoccaggio:s{i:04d}", f"Stoccaggio {i + 1}", z.id, z.x - 0.3, z.y - 0.3,
            round(capacita), round(gas_medio * 1.1 / max(1, len(zone_gas[::3]))), round(gas_medio * 0.6 / max(1, len(zone_gas[::3]))),
        ))
    return s

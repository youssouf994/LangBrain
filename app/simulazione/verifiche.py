"""
Controlli di coerenza dopo un passo di simulazione: bilanci di energia e di gas, limiti fisici di impianti, linee,
condotte e stoccaggi. Li usano i test e lo script di prova di carico (scripts/stress_energia.py).
"""

from app.simulazione.motore import Simulatore


def verifica_invarianti(sim: Simulatore, tolleranza: float = 1e-3, linepack_prima: float | None = None) -> list[str]:
    """
    Restituisce l'elenco delle violazioni (vuoto se tutto torna). `linepack_prima` (somma del linepack prima del passo)
    abilita anche il bilancio nazionale del gas.
    """
    s, n = sim.scenario, sim.nazionale
    errori: list[str] = []

    def controlla(condizione: bool, messaggio: str) -> None:
        if not condizione:
            errori.append(messaggio)

    generata = sum(v for k, v in n["produzione_mw"].items() if k != "accumuli")
    servita = n["domanda_mw"] - n["distacco_mw"] - n.get("interrotto_mw", 0.0)
    controlla(abs(generata + n["produzione_mw"]["accumuli"] - servita) <= tolleranza * max(1.0, n["domanda_mw"]),
              f"bilancio elettrico nazionale: generazione {generata:.1f} + accumuli {n['produzione_mw']['accumuli']:.1f} ≠ servita {servita:.1f}")

    netto = {z.id: 0.0 for z in s.zone}
    for c in s.centrali:
        netto[c.zona] += c.produzione_mw
        controlla(c.produzione_mw >= -tolleranza, f"{c.id}: produzione negativa")
        controlla(c.produzione_mw <= c.disponibile_mw + tolleranza, f"{c.id}: produzione oltre la disponibilità")
        controlla(c.disponibile_mw <= c.potenza_mw + tolleranza, f"{c.id}: disponibilità oltre la potenza")
        if c.tipo == "idro_bacino":
            controlla(-tolleranza <= c.riserva_bacino_mwh <= c.bacino_mwh + tolleranza, f"{c.id}: bacino fuori dai limiti")
    for a in s.accumuli:
        netto[a.zona] += a.potenza_attuale_mw
        controlla(-tolleranza <= a.carica_mwh <= a.capacita_mwh + tolleranza, f"{a.id}: carica fuori dai limiti")
        controlla(abs(a.potenza_attuale_mw) <= a.potenza_mw + tolleranza, f"{a.id}: potenza oltre il limite")
    for l in s.linee:
        netto[l.da] -= l.flusso_mw
        netto[l.a] += l.flusso_mw
        capacita = l.capacita_mw * ((1 - l.perdita_guasto) if l.guasto_fino > sim.passo - 1 else 1)
        controlla(abs(l.flusso_mw) <= capacita + tolleranza, f"{l.id}: flusso oltre la capacità")
    for z in s.zone:
        servita_zona = z.domanda_mw - z.distacco_mw - z.interrotto_mw
        controlla(abs(netto[z.id] - servita_zona) <= tolleranza * max(1.0, z.domanda_mw),
                  f"{z.id}: bilancio di zona non torna ({netto[z.id]:.1f} ≠ {servita_zona:.1f})")
        controlla(-tolleranza <= z.interrotto_mw <= 0.08 * z.domanda_mw + tolleranza, f"{z.id}: carico interrotto fuori dai limiti")
        controlla(z.distacco_mw >= -tolleranza, f"{z.id}: distacco negativo")
        controlla(-tolleranza <= z.linepack <= z.linepack_max + tolleranza, f"{z.id}: linepack fuori dai limiti")
    for c in s.condotte:
        controlla(abs(c.flusso) <= c.capacita + tolleranza, f"{c.id}: flusso oltre la capacità")
    for st in s.stoccaggi:
        controlla(-tolleranza <= st.giacenza <= st.capacita + tolleranza, f"{st.id}: giacenza fuori dai limiti")
    for i in s.ingressi:
        controlla(-tolleranza <= i.portata <= sim.portata_massima(i) + tolleranza, f"{i.id}: portata fuori dai limiti")

    if linepack_prima is not None:
        dopo = sum(z.linepack for z in s.zone)
        offerta = sum(n["gas_ingressi"].values()) + n["gas_erogazione_stoccaggi"] - n["gas_iniezione_stoccaggi"]
        consumo = n["gas_domanda"] - n["gas_non_servito"]
        controlla(abs(offerta + (linepack_prima - dopo) / sim.dt - consumo) <= 0.01 * max(1.0, n["gas_domanda"]),
                  f"bilancio gas nazionale: offerta {offerta:.1f} + linepack {(linepack_prima - dopo) / sim.dt:.1f} ≠ consumo {consumo:.1f}")
    return errori

"""
Prova di carico della simulazione energetica, senza server e senza LLM.

Esegue il motore alla massima velocità su uno scenario (Italia o rete generata di dimensione scelta) e misura:
velocità (passi al secondo, ore simulate al secondo), memoria di picco, costo dell'istantanea che l'API manda al
frontend (tempo di costruzione e dimensione del JSON), violazioni dei bilanci e dei limiti fisici, statistiche finali.

Esempi (dalla radice del progetto):
    python scripts/stress_energia.py --anni 1
    python scripts/stress_energia.py --scenario generata --zone 80 --citta 3000 --centrali 1500 --anni 1
    python scripts/stress_energia.py --anni 10 --controlli 0 --json risultati.json

Uscita con codice 1 se i controlli trovano violazioni.
"""

import argparse
import json
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.simulazione.motore import PASSI_AMMESSI_MINUTI, Simulatore  # noqa: E402
from app.simulazione.scenari import scenario_generato, scenario_italia  # noqa: E402
from app.simulazione.verifiche import verifica_invarianti  # noqa: E402


def memoria_picco_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux: kB


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", choices=("italia", "generata"), default="italia")
    p.add_argument("--zone", type=int, default=40)
    p.add_argument("--citta", type=int, default=1000)
    p.add_argument("--centrali", type=int, default=500)
    p.add_argument("--anni", type=float, default=None, help="durata in anni (alternativa a --ore)")
    p.add_argument("--ore", type=float, default=24 * 30)
    p.add_argument("--passo", type=int, choices=PASSI_AMMESSI_MINUTI, default=60, help="passo in minuti")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--controlli", type=int, default=1,
                   help="verifica bilanci e limiti ogni N passi (0: mai; 1: a ogni passo, più lento)")
    p.add_argument("--json", type=Path, default=None, help="salva i risultati in questo file")
    a = p.parse_args()

    ore = a.anni * 8760 if a.anni is not None else a.ore
    inizio = time.perf_counter()
    if a.scenario == "italia":
        scenario = scenario_italia()
    else:
        scenario = scenario_generato(a.zone, a.citta, a.centrali, a.seed)
    sim = Simulatore(scenario, seed=a.seed, passo_minuti=a.passo, durata_ore=ore)
    preparazione = time.perf_counter() - inizio
    print(f"Scenario: {scenario.descrizione}")
    print(f"Entità: {scenario.numero_entita()} · passi da eseguire: {sim.passi_totali} · preparazione {preparazione:.2f} s")

    violazioni: list[str] = []
    passi_con_violazioni = 0
    ultimo_rapporto = time.perf_counter()
    calcolo = 0.0
    while not sim.finita:
        controlla = a.controlli > 0 and sim.passo % a.controlli == 0
        prima = sum(z.linepack for z in scenario.zone) if controlla else None
        t0 = time.perf_counter()
        sim.avanza(1)
        calcolo += time.perf_counter() - t0
        if controlla:
            trovate = verifica_invarianti(sim, linepack_prima=prima)
            if trovate:
                passi_con_violazioni += 1
                violazioni.extend(f"{sim.tempo.isoformat(timespec='minutes')} {v}" for v in trovate[:3])
        if time.perf_counter() - ultimo_rapporto > 5:
            ultimo_rapporto = time.perf_counter()
            print(f"  {sim.tempo:%Y-%m-%d} · {sim.passo / sim.passi_totali:6.1%} · {sim.passo / calcolo:,.0f} passi/s")

    # Costo dell'istantanea che lo stream manda al frontend
    t0 = time.perf_counter()
    for _ in range(20):
        corpo = json.dumps({"istantanea": sim.istantanea(), "riepilogo": sim.riepilogo()}, separators=(",", ":"))
    istantanea_ms = (time.perf_counter() - t0) / 20 * 1000

    r = sim.riepilogo()
    risultati = {
        "scenario": scenario.nome, "entita": scenario.numero_entita(), "passo_minuti": a.passo, "ore_simulate": sim.passo * sim.dt,
        "passi": sim.passo, "secondi_di_calcolo": round(calcolo, 2), "passi_al_secondo": round(sim.passo / calcolo, 1),
        "ms_per_passo": round(calcolo / sim.passo * 1000, 3), "ore_simulate_al_secondo": round(sim.passo * sim.dt / calcolo, 1),
        "memoria_picco_mb": round(memoria_picco_mb(), 1), "istantanea_ms": round(istantanea_ms, 2),
        "istantanea_kb": round(len(corpo) / 1024, 1), "passi_controllati_ogni": a.controlli,
        "passi_con_violazioni": passi_con_violazioni, "prime_violazioni": violazioni[:20], "riepilogo": r,
    }

    print()
    print(f"Velocità:   {risultati['passi_al_secondo']:,} passi/s · {risultati['ms_per_passo']} ms/passo · "
          f"{risultati['ore_simulate_al_secondo']:,} ore simulate/s ({calcolo:.1f} s di calcolo)")
    print(f"Memoria:    picco {risultati['memoria_picco_mb']} MB")
    print(f"Frontend:   istantanea {risultati['istantanea_kb']} kB, costruita e serializzata in {risultati['istantanea_ms']} ms")
    print(f"Domanda:    {r['domanda_twh']} TWh · non servita {r['distacco_mwh']:,.0f} MWh in {r['ore_con_distacco']} h")
    print(f"Produzione: " + ", ".join(f"{k} {v} TWh" for k, v in r["produzione_twh"].items()))
    print(f"Rinnovabili {r['quota_rinnovabili_fer']:.1%} · CO₂ {r['co2_mt']} Mt · prezzo medio {r['prezzo_medio']} €/MWh")
    print(f"Gas:        {r['gas_domanda_mld_m3']} mld m³ · non servito {r['gas_non_servito_msm3']} mln m³")
    print(f"Eventi:     {r['eventi_per_tipo']}")
    if a.controlli:
        esito = "nessuna violazione" if not passi_con_violazioni else f"{passi_con_violazioni} passi con violazioni"
        print(f"Controlli:  bilanci e limiti verificati ogni {a.controlli} passi: {esito}")
        for v in violazioni[:10]:
            print(f"   - {v}")
    if a.json:
        a.json.write_text(json.dumps(risultati, ensure_ascii=False, indent=2))
        print(f"Risultati salvati in {a.json}")
    return 1 if passi_con_violazioni else 0


if __name__ == "__main__":
    sys.exit(main())

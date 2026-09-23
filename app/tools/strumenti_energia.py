"""
Tool del dominio energia: gli apparati della rete simulata (app.simulazione), che hanno già elaborato i dati.

- Sensori (sola lettura): riassumono la situazione della rete elettrica, degli accumuli e della rete gas in un
  dizionario JSON e aggiungono le `verifiche`, cioè i confronti con le soglie delle regole già fatti in Python
  (funzioni deterministiche): il modello non deve confrontare numeri da solo.
- Leve (ON/OFF): i comandi operativi della rete (vedi `app.simulazione.motore.LEVE`). Ogni leva ha il proprio
  automatismo (`LevaRete.valuta`): applica le regole di `REGOLE_LEVE` ai fatti e dà un'indicazione (ATTIVA, DISATTIVA,
  ESCALATE, NESSUNA) con la regola che l'ha prodotta. Ogni comando passa comunque dalla validazione di
  configurazione.toml e dall'audit log di `applica_stato`.

Le regole sono scritte una volta sola, qui: i prompt degli agenti le riportano con gli stessi testi.

I tool leggono sempre la simulazione corrente dell'esecutore: se lo scenario viene sostituito, restano validi.
"""

import json
from dataclasses import dataclass
from typing import Any, Callable

from app.core.constants import is_control_flag
from app.core.risultati import ErroreTool
from app.simulazione.esecutore import esecutore
from app.simulazione.motore import LEVE
from app.tools.baseTool import BaseTool
from app.tools.sensor_tools import registra_tool

TIPI_ELETTRICI = {"distacco_carico", "riserva_bassa", "congestione"}
TIPI_GAS = {"gas_non_servito", "pressione_gas_bassa", "stoccaggio_gas_basso", "crisi_gas"}


def _anomalie(sim, tipi: set[str]) -> list[str]:
    return [f"{a['descrizione']} (da {a['da_ore']} h)" for a in sim.anomalie if a["tipo"] in tipi][:12]


# ---------------------------------------------------------------------------------------------- verifiche

SOGLIE_MARGINE_PCT = (3, 10, 15, 25)
SOGLIE_PRESSIONE_SOTTO_BAR = (46, 50, 52)
SOGLIE_PRESSIONE_SOPRA_BAR = (55, 58, 60)
MESI_INVERNALI = (11, 12, 1, 2, 3)


def fatti_elettrici(sim) -> dict[str, Any]:
    """Fatti della rete elettrica già valutati: numeri e confronti con ogni soglia delle regole."""
    n = sim.nazionale
    margine = round(100 * n.get("margine_riserva", 0), 1)
    fatti: dict[str, Any] = {
        "carico_non_servito": n.get("distacco_mw", 0) > 1,
        "margine_riserva_pct": margine,
        "ondata_in_corso": any(e["tipo"] in ("ondata_calore", "ondata_freddo") for e in sim.eventi_attivi),
        "anomalie_elettriche": any(a["tipo"] in ("distacco_carico", "riserva_bassa") for a in sim.anomalie),
    }
    for soglia in SOGLIE_MARGINE_PCT:
        fatti[f"margine_sotto_{soglia}"] = margine < soglia
        fatti[f"margine_sopra_{soglia}"] = margine > soglia
    return fatti


def fatti_gas(sim) -> dict[str, Any]:
    """Fatti della rete gas già valutati: numeri, confronti con le soglie e scarto dall'obiettivo degli stoccaggi."""
    n = sim.nazionale
    zone_gas = [z for z in sim.scenario.zone if z.rete_gas and z.linepack_max > 0]
    pressione = round(min(z.pressione for z in zone_gas), 1) if zone_gas else 70.0
    stoccaggi = round(100 * n.get("stoccaggio_gas_pct", 0), 1)
    obiettivo = round(100 * n.get("obiettivo_stoccaggio_pct", 0), 1)
    fatti: dict[str, Any] = {
        "gas_non_servito": n.get("gas_non_servito", 0) > 1,
        "pressione_minima_bar": pressione,
        "inverno": sim.tempo.month in MESI_INVERNALI,
        "ingressi_in_crisi": sum(1 for i in sim.scenario.ingressi if i.riduzione < 1),
        "stoccaggi_pct": stoccaggi,
        "obiettivo_stoccaggi_pct": obiettivo,
        "stoccaggi_scarto_dall_obiettivo_punti": round(stoccaggi - obiettivo, 1),
        "stoccaggi_sotto_obiettivo": stoccaggi < obiettivo,
    }
    for soglia in SOGLIE_PRESSIONE_SOTTO_BAR:
        fatti[f"pressione_sotto_{soglia}"] = pressione < soglia
    for soglia in SOGLIE_PRESSIONE_SOPRA_BAR:
        fatti[f"pressione_sopra_{soglia}"] = pressione > soglia
    return fatti


def fatti_rete(sim) -> dict[str, Any]:
    return {**fatti_elettrici(sim), **fatti_gas(sim)}


# ---------------------------------------------------------------------------------------------- automatismi delle leve

@dataclass(frozen=True)
class Regola:
    decisione: str                                   # ATTIVA | DISATTIVA | ESCALATE
    testo: str                                       # come la legge l'agente nel prompt
    vale: Callable[[dict[str, Any], bool], bool]     # (fatti, leva accesa) -> la regola si applica


# In ordine: vale la prima regola che si applica; se nessuna si applica l'indicazione è NESSUNA.
REGOLE_LEVE: dict[str, tuple[Regola, ...]] = {
    "rete_el_import_emergenza": (
        Regola("ESCALATE", "leva accesa e ancora carico non servito", lambda f, on: on and f["carico_non_servito"]),
        Regola("ATTIVA", "leva spenta e carico non servito o margine di riserva sotto il 10%",
               lambda f, on: not on and (f["carico_non_servito"] or f["margine_sotto_10"])),
        Regola("DISATTIVA", "leva accesa, margine sopra il 25% e nessun carico non servito",
               lambda f, on: on and f["margine_sopra_25"] and not f["carico_non_servito"]),
    ),
    "rete_el_accumuli_riserva": (
        Regola("ESCALATE", "leva accesa e ancora carico non servito", lambda f, on: on and f["carico_non_servito"]),
        Regola("ATTIVA", "leva spenta e margine sotto il 10%, oppure ondata di calore o di freddo con margine sotto il 25%",
               lambda f, on: not on and (f["margine_sotto_10"] or (f["ondata_in_corso"] and f["margine_sotto_25"]))),
        Regola("DISATTIVA", "leva accesa, margine sopra il 25% e nessuna anomalia elettrica",
               lambda f, on: on and f["margine_sopra_25"] and not f["anomalie_elettriche"]),
    ),
    "rete_el_interrompibili": (
        Regola("ATTIVA", "leva spenta e carico non servito o margine sotto il 3% (diventa una richiesta al padre)",
               lambda f, on: not on and (f["carico_non_servito"] or f["margine_sotto_3"])),
        Regola("DISATTIVA", "leva accesa, nessun carico non servito e margine sopra il 15%",
               lambda f, on: on and not f["carico_non_servito"] and f["margine_sopra_15"]),
    ),
    "rete_gas_gnl_spot": (
        Regola("ESCALATE", "leva accesa e ancora gas non servito o pressione minima sotto 50 bar",
               lambda f, on: on and (f["gas_non_servito"] or f["pressione_sotto_50"])),
        Regola("ATTIVA", "leva spenta e gas non servito, o pressione minima sotto 52 bar, o inverno con ingressi in crisi "
                         "e stoccaggi sotto l'obiettivo stagionale",
               lambda f, on: not on and (f["gas_non_servito"] or f["pressione_sotto_52"]
                                         or (f["inverno"] and f["ingressi_in_crisi"] and f["stoccaggi_sotto_obiettivo"]))),
        Regola("DISATTIVA", "leva accesa, nessun ingresso in crisi, pressione sopra 60 bar e nessun gas non servito",
               lambda f, on: on and not f["ingressi_in_crisi"] and f["pressione_sopra_60"] and not f["gas_non_servito"]),
    ),
    "rete_gas_stoccaggio_strategico": (
        Regola("ESCALATE", "leva accesa e ancora gas non servito", lambda f, on: on and f["gas_non_servito"]),
        Regola("ATTIVA", "leva spenta e gas non servito o pressione minima sotto 50 bar",
               lambda f, on: not on and (f["gas_non_servito"] or f["pressione_sotto_50"])),
        Regola("DISATTIVA", "leva accesa, pressione sopra 58 bar e nessun gas non servito",
               lambda f, on: on and f["pressione_sopra_58"] and not f["gas_non_servito"]),
    ),
    "rete_gas_interrompibili": (
        Regola("ATTIVA", "leva spenta e gas non servito o pressione minima sotto 46 bar (diventa una richiesta al padre)",
               lambda f, on: not on and (f["gas_non_servito"] or f["pressione_sotto_46"])),
        Regola("DISATTIVA", "leva accesa, nessun gas non servito e pressione sopra 55 bar",
               lambda f, on: on and not f["gas_non_servito"] and f["pressione_sopra_55"]),
    ),
}


def indicazione(leva: str, fatti: dict[str, Any], accesa: bool) -> tuple[str, str]:
    """Applica le regole della leva ai fatti: (decisione, testo della regola). Vale la prima regola che si applica."""
    for regola in REGOLE_LEVE[leva]:
        if regola.vale(fatti, accesa):
            return regola.decisione, regola.testo
    return "NESSUNA", "nessuna regola si applica: la leva è già nello stato giusto"


def valuta_leva(leva: str, sim) -> dict[str, Any]:
    """Automatismo della leva: applica le regole ai fatti della rete e restituisce l'indicazione con la sua ragione."""
    decisione, regola = indicazione(leva, fatti_rete(sim), sim.leve[leva] == "ON")
    return {"leva": leva, "stato": sim.leve[leva], "indicazione": decisione, "regola": regola}


def testo_regole(leva: str) -> str:
    """Le regole della leva come le legge l'agente nel prompt (stessi testi dell'automatismo)."""
    righe = [f"- {r.decisione} se: {r.testo}." for r in REGOLE_LEVE[leva]]
    righe.append("- NESSUNA in tutti gli altri casi, anche con anomalie in corso.")
    return "\n".join(righe)


# ---------------------------------------------------------------------------------------------- sensori

def situazione_elettrica(sim) -> dict[str, Any]:
    n = sim.nazionale
    s = sim.scenario
    guasti = [e["descrizione"] for e in sim.eventi_attivi if e["tipo"] == "guasto" and not str(e["entita"]).startswith(("condotta:",))]
    return {
        "ora_simulata": sim.tempo.isoformat(timespec="minutes"),
        "domanda_mw": round(n.get("domanda_mw", 0)),
        "carico_non_servito_mw": round(n.get("distacco_mw", 0)),
        "carico_interrotto_mw": round(n.get("interrotto_mw", 0)),
        "margine_riserva_pct": round(100 * n.get("margine_riserva", 0), 1),
        "zona_margine_minimo": n.get("zona_margine_minimo"),
        "margine_riserva_nazionale_pct": round(100 * n.get("margine_riserva_nazionale", 0), 1),
        "prezzo_eur_mwh": round(n.get("prezzo_energia", 0), 1),
        "import_mw": round((n.get("produzione_mw") or {}).get("import", 0)),
        "zone_con_carico_non_servito": [z.nome for z in s.zone if z.distacco_mw > 1],
        "temperatura_media_c": round(sum(z.temperatura for z in s.zone) / len(s.zone), 1),
        "anomalie": _anomalie(sim, TIPI_ELETTRICI),
        "guasti_attivi": guasti[:8],
        "verifiche": fatti_elettrici(sim),
    }


def situazione_accumuli(sim) -> dict[str, Any]:
    accumuli = sim.scenario.accumuli
    capacita = sum(a.capacita_mwh for a in accumuli) or 1.0
    return {
        "ora_simulata": sim.tempo.isoformat(timespec="minutes"),
        "carica_media_pct": round(100 * sum(a.carica_mwh for a in accumuli) / capacita, 1),
        "energia_disponibile_mwh": round(sum(a.carica_mwh for a in accumuli)),
        "potenza_netta_mw": round(sum(a.potenza_attuale_mw for a in accumuli)),
        "potenza_massima_mw": round(sum(a.potenza_mw for a in accumuli)),
        "margine_riserva_pct": round(100 * sim.nazionale.get("margine_riserva", 0), 1),
        "zona_margine_minimo": sim.nazionale.get("zona_margine_minimo"),
        "carico_non_servito_mw": round(sim.nazionale.get("distacco_mw", 0)),
        "verifiche": fatti_elettrici(sim),
    }


def situazione_gas(sim) -> dict[str, Any]:
    n = sim.nazionale
    zone_gas = [z for z in sim.scenario.zone if z.rete_gas and z.linepack_max > 0]
    piu_bassa = min(zone_gas, key=lambda z: z.pressione) if zone_gas else None
    return {
        "ora_simulata": sim.tempo.isoformat(timespec="minutes"),
        "domanda_ksm3_h": round(n.get("gas_domanda", 0)),
        "gas_non_servito_ksm3_h": round(n.get("gas_non_servito", 0)),
        "gas_interrotto_ksm3_h": round(n.get("gas_interrotto", 0)),
        "pressione_minima_bar": round(piu_bassa.pressione, 1) if piu_bassa else None,
        "zona_pressione_minima": piu_bassa.nome if piu_bassa else None,
        "stoccaggi_pct": round(100 * n.get("stoccaggio_gas_pct", 0), 1),
        "obiettivo_stoccaggi_pct": round(100 * n.get("obiettivo_stoccaggio_pct", 0), 1),
        "erogazione_stoccaggi_ksm3_h": round(n.get("gas_erogazione_stoccaggi", 0)),
        "ingressi_in_crisi": [f"{i.nome} al {round(100 * i.riduzione)}%" for i in sim.scenario.ingressi if i.riduzione < 1],
        "anomalie": _anomalie(sim, TIPI_GAS),
        "verifiche": fatti_gas(sim),
    }


SENSORI = {
    "sensore_rete_elettrica": situazione_elettrica,
    "sensore_accumuli": situazione_accumuli,
    "sensore_rete_gas": situazione_gas,
}


class SensoreRete(BaseTool):
    """Sensore di sola lettura: restituisce la situazione come JSON compatto."""

    def __init__(self, nome: str):
        super().__init__(target_device=nome)
        self.unit = "json"

    async def get_tool_value(self) -> str:
        return json.dumps(SENSORI[self.target_device](esecutore.simulatore()), ensure_ascii=False)

    async def set_tool_value(self, value: Any) -> bool:
        raise ErroreTool(f"'{self.target_device}' è un sensore di sola lettura.", codice="SOLA_LETTURA")


class LevaRete(BaseTool):
    """Leva operativa ON/OFF della simulazione."""

    def __init__(self, nome: str):
        super().__init__(target_device=nome)
        self.unit = ""

    async def get_tool_value(self) -> str:
        return esecutore.simulatore().leve[self.target_device]

    def valuta(self) -> dict[str, Any]:
        """Indicazione dell'automatismo della leva sui dati attuali (deterministica)."""
        return valuta_leva(self.target_device, esecutore.simulatore())

    async def set_tool_value(self, value: Any) -> bool:
        if is_control_flag(value):
            # Un flag interno non cambia la leva. REJECTED è il rifiuto di una richiesta da parte del Brain (le richieste
            # sono sempre di accensione): diventa un veto sull'accensione, in tempo simulato.
            if "REJECTED" in str(value).upper():
                esecutore.simulatore().imposta_veto(self.target_device, "ON", autore="Brain", motivo="Richiesta di accensione respinta dal Brain")
                esecutore.versione += 1
            return True
        sim = esecutore.simulatore()
        try:
            sim.imposta_leva(self.target_device, str(value))
        except ValueError as e:
            raise ErroreTool(str(e), codice="VALORE_NON_VALIDO") from e
        # Chi arriva fin qui ha l'autorità per farlo (gli agenti rispettano i veti prima di chiedere il comando, il Brain
        # approva scavalcando il proprio veto): un veto proprio su questo valore non ha più senso.
        if sim.veto(self.target_device, str(value)):
            sim.veti.pop(self.target_device, None)
        esecutore.versione += 1
        return True


def strumenti_energia() -> dict[str, BaseTool]:
    strumenti: dict[str, BaseTool] = {nome: SensoreRete(nome) for nome in SENSORI}
    strumenti.update({nome: LevaRete(nome) for nome in LEVE})
    return strumenti


def registra_strumenti_energia() -> dict[str, BaseTool]:
    """Crea i tool e li registra nel registry del processo (così anche GET/POST /tools/{nome} li trovano)."""
    strumenti = strumenti_energia()
    for nome, strumento in strumenti.items():
        registra_tool(nome, strumento)
    return strumenti

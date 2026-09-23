"""
Ruoli di chi usa l'API, con nomi dalla nomenclatura medica.

  tirocinante         sola lettura: osserva stato, tool, eventi, agenti e configurazione, ma non interviene.
  medico_di_guardia   l'operatore di turno: in più esegue le procedure ordinarie (cicli del grafo, comandi ai dispositivi,
                      decisioni APPROVA/RESPINGI sulle richieste HITL, sblocchi).
  primario            l'amministratore: in più le decisioni critiche e la configurazione (OVERRIDE, reset, agenti,
                      politica HITL, simulazioni, proxy LLM).

Ogni ruolo ha la propria chiave, nel `.env`. Sono ruoli di chi CHIAMA l'API (persone o servizi): non c'entrano con i
poteri degli agenti, regolati da `priority_weight` e dall'elenco dei dispositivi in `configurazione.toml`.
"""

import os
import secrets
from enum import IntEnum


class Ruolo(IntEnum):
    TIROCINANTE = 1
    MEDICO_DI_GUARDIA = 2
    PRIMARIO = 3

    @property
    def nome(self) -> str:
        return self.name.lower()


# Variabili d'ambiente che contengono la chiave di ciascun ruolo. `API_KEY` è la chiave unica storica: vale come primario.
VARIABILI_CHIAVE: dict[Ruolo, tuple[str, ...]] = {
    Ruolo.PRIMARIO: ("API_KEY_PRIMARIO", "API_KEY"),
    Ruolo.MEDICO_DI_GUARDIA: ("API_KEY_MEDICO_DI_GUARDIA",),
    Ruolo.TIROCINANTE: ("API_KEY_TIROCINANTE",),
}

ROTTE_PUBBLICHE = {("GET", "/"), ("GET", "/demo"), ("GET", "/energia")}

# Ruolo minimo per ogni endpoint (metodo, percorso). Un endpoint non elencato richiede il ruolo massimo.
PERMESSI: dict[tuple[str, str], Ruolo] = {
    # Osservazione
    ("GET", "/graph/state"): Ruolo.TIROCINANTE,
    ("GET", "/hitl/config"): Ruolo.TIROCINANTE,
    ("GET", "/hitl/scadenze"): Ruolo.TIROCINANTE,
    ("GET", "/agents"): Ruolo.TIROCINANTE,
    ("GET", "/agents/hierarchy"): Ruolo.TIROCINANTE,
    ("GET", "/agents/{agent_name}/model"): Ruolo.TIROCINANTE,
    ("GET", "/tools"): Ruolo.TIROCINANTE,
    ("GET", "/tools/{device_id}"): Ruolo.TIROCINANTE,
    ("GET", "/events"): Ruolo.TIROCINANTE,
    ("GET", "/energia/rete"): Ruolo.TIROCINANTE,
    ("GET", "/energia/stato"): Ruolo.TIROCINANTE,
    ("GET", "/energia/stream"): Ruolo.TIROCINANTE,
    ("GET", "/energia/storico"): Ruolo.TIROCINANTE,
    ("GET", "/energia/entita/{entita_id}"): Ruolo.TIROCINANTE,
    # Procedure ordinarie (/graph/resume con OVERRIDE richiede comunque il primario)
    ("POST", "/graph/run"): Ruolo.MEDICO_DI_GUARDIA,
    ("POST", "/graph/resume"): Ruolo.MEDICO_DI_GUARDIA,
    ("POST", "/graph/run/stream"): Ruolo.MEDICO_DI_GUARDIA,
    ("POST", "/graph/resume/stream"): Ruolo.MEDICO_DI_GUARDIA,
    ("POST", "/graph/health-check"): Ruolo.MEDICO_DI_GUARDIA,
    ("POST", "/tools"): Ruolo.MEDICO_DI_GUARDIA,
    ("POST", "/events/unblock"): Ruolo.MEDICO_DI_GUARDIA,
    ("DELETE", "/events/reset-conflicts/{target}"): Ruolo.MEDICO_DI_GUARDIA,
    ("POST", "/energia/controllo"): Ruolo.MEDICO_DI_GUARDIA,
    ("POST", "/energia/leva"): Ruolo.MEDICO_DI_GUARDIA,
    ("POST", "/energia/sistema-nervoso/ciclo"): Ruolo.MEDICO_DI_GUARDIA,
    ("POST", "/energia/sistema-nervoso/decisione"): Ruolo.MEDICO_DI_GUARDIA,
    # Decisioni critiche e configurazione
    ("POST", "/hitl/config"): Ruolo.PRIMARIO,
    ("POST", "/agents/create"): Ruolo.PRIMARIO,
    ("DELETE", "/agents/{agent_name}"): Ruolo.PRIMARIO,
    ("PUT", "/agents/{agent_name}/model"): Ruolo.PRIMARIO,
    ("DELETE", "/agents/{agent_name}/model"): Ruolo.PRIMARIO,
    ("POST", "/tools/{device_id}/fault"): Ruolo.PRIMARIO,
    ("POST", "/events/seed-conflict"): Ruolo.PRIMARIO,
    ("POST", "/llm/invoke"): Ruolo.PRIMARIO,
    ("DELETE", "/system/reset"): Ruolo.PRIMARIO,
    ("POST", "/energia/scenario"): Ruolo.PRIMARIO,
    ("POST", "/energia/evento"): Ruolo.PRIMARIO,
    ("POST", "/energia/sistema-nervoso/configura"): Ruolo.PRIMARIO,
}


def chiavi_configurate() -> dict[Ruolo, list[str]]:
    """Chiavi valorizzate per ciascun ruolo, lette dall'ambiente a ogni richiesta."""
    risultato: dict[Ruolo, list[str]] = {}
    for ruolo, variabili in VARIABILI_CHIAVE.items():
        chiavi = [v.strip() for nome in variabili if (v := os.getenv(nome, "")).strip()]
        if chiavi:
            risultato[ruolo] = chiavi
    return risultato


def autenticazione_attiva() -> bool:
    """False se nessuna chiave è configurata: l'API resta aperta e chi la usa vale come primario."""
    return bool(chiavi_configurate())


def ruolo_della_chiave(chiave: str | None) -> Ruolo | None:
    """
    Ruolo associato alla chiave, oppure None se non corrisponde a nessuna. Se la stessa chiave è assegnata a più
    ruoli vale il più alto. Il confronto è a tempo costante e scorre sempre tutte le chiavi.
    """
    if not chiave:
        return None
    candidata = chiave.encode()
    trovato: Ruolo | None = None
    for ruolo, chiavi in chiavi_configurate().items():
        for attesa in chiavi:
            if secrets.compare_digest(candidata, attesa.encode()) and (trovato is None or ruolo > trovato):
                trovato = ruolo
    return trovato


def ruolo_minimo(metodo: str, percorso: str) -> Ruolo:
    """Ruolo minimo per l'endpoint; se non è classificato serve il primario (rifiuto predefinito)."""
    return PERMESSI.get((metodo.upper(), percorso), Ruolo.PRIMARIO)


def avvisi_configurazione() -> list[str]:
    """Messaggi da mostrare all'avvio sulla protezione dell'API."""
    configurate = chiavi_configurate()
    if not configurate:
        return ["Nessuna chiave API configurata: tutti gli endpoint sono aperti a chiunque raggiunga il server."]
    avvisi = []
    tutte = [c for chiavi in configurate.values() for c in chiavi]
    if len(tutte) != len(set(tutte)):
        avvisi.append("La stessa chiave API è assegnata a più ruoli: vale il ruolo più alto.")
    mancanti = [r.nome for r in Ruolo if r not in configurate]
    if mancanti:
        avvisi.append(f"Nessuna chiave configurata per i ruoli: {', '.join(mancanti)} (non potranno accedere).")
    return avvisi

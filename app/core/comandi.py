"""
"Tenta il comando": un comando a un dispositivo chiesto dall'esterno del grafo (un'API, un adattatore, uno script), con
un esito strutturato invece di eccezioni.

Applica le stesse verifiche del flusso degli agenti (`BaseAgent.applica_stato`), nello stesso ordine:
  1. il dispositivo e il valore devono essere ammessi da `configurazione.toml`;
  2. nessun blocco attivo di un attore con priorità maggiore (`app/core/priorita.py`);
  3. nessuna regola deterministica registrata con `registra_regola` deve opporsi;
  4. si aziona il tool del dispositivo.
Ogni esito scrive UNA decisione nel registro (`events`, con `outcome` e `correlation_id`) e ne restituisce l'`event_id`
come `decision_id`. Un comando bloccato da una regola o da una priorità è un esito `rejected`, non un'eccezione:
`tenta_comando` non solleva mai.

I motivi di `EsitoComando` si mappano uno a uno sui motivi di fallimento del contratto 1.0.4 (`motivo_contratto`). `no_confirmation`
non esiste qui: la conferma dell'effetto sul dispositivo (potenza misurata) spetta a chi ha il misuratore.
"""

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.core import contesto
from app.core.configurazione import get_configurazione
from app.core.priorita import NOME_BRAIN, PRIORITA_BRAIN, priorita_attore, trova_blocco_prevalente
from app.core.risultati import comanda_tool, leggi_tool
from app.tools.effectors import trova_tool
from app.tools.nervous_system import NervousSystem

logger = logging.getLogger(__name__)

# Stati di `EsitoComando`
APPLICATO = "applicato"
BLOCCATO_DA_PRIORITA = "bloccato_da_priorita"
BLOCCATO_DA_REGOLA = "bloccato_da_regola"
FUORI_LIMITI = "fuori_limiti"
DISPOSITIVO_OFFLINE = "dispositivo_offline"
RIFIUTATO_DAL_DISPOSITIVO = "rifiutato_dal_dispositivo"
ERRORE_INTERNO = "errore_interno"

STATI = (APPLICATO, BLOCCATO_DA_PRIORITA, BLOCCATO_DA_REGOLA, FUORI_LIMITI, DISPOSITIVO_OFFLINE,
         RIFIUTATO_DAL_DISPOSITIVO, ERRORE_INTERNO)

# Stato -> motivo di fallimento del contratto 1.0.4 (None: nessun fallimento)
MOTIVO_CONTRATTO = {
    APPLICATO: None,
    BLOCCATO_DA_PRIORITA: "blocked_by_priority",
    BLOCCATO_DA_REGOLA: "blocked_by_rule",
    FUORI_LIMITI: "out_of_limits",
    DISPOSITIVO_OFFLINE: "device_offline",
    RIFIUTATO_DAL_DISPOSITIVO: "device_rejected",
    ERRORE_INTERNO: "internal_error",
}

# Stato -> `outcome` della decisione registrata
_ESITO_DECISIONE = {
    APPLICATO: "applied",
    BLOCCATO_DA_PRIORITA: "rejected",
    BLOCCATO_DA_REGOLA: "rejected",
    FUORI_LIMITI: "rejected",
    DISPOSITIVO_OFFLINE: "failed",
    RIFIUTATO_DAL_DISPOSITIVO: "failed",
    ERRORE_INTERNO: "failed",
}

# Codici di `ErroreTool` che significano "il dispositivo non risponde"
_CODICI_OFFLINE = {"DEVICE_OFFLINE", "OFFLINE", "TIMEOUT", "UNREACHABLE", "DEVICE_UNAVAILABLE"}
# Codici che significano "il dispositivo ha risposto e ha detto di no" (o è guasto per i comandi)
_CODICI_RIFIUTO = {"COMMAND_REJECTED", "DEVICE_REJECTED", "SIMULATED_FAULT", "TOOL_ERROR"}

FINESTRA_CONFLITTO_MINUTI = 30


@dataclass(frozen=True)
class EsitoComando:
    stato: str
    motivo: str
    decision_id: int | None = None
    correlation_id: str | None = None
    attore: str | None = None
    target: str | None = None
    valore_precedente: str | None = None
    valore_richiesto: str | None = None
    regola: str | None = None

    @property
    def riuscito(self) -> bool:
        return self.stato == APPLICATO

    @property
    def motivo_contratto(self) -> str | None:
        """Il motivo di fallimento del contratto (`blocked_by_rule`, `device_offline`...), None se applicato."""
        return MOTIVO_CONTRATTO[self.stato]


@dataclass(frozen=True)
class RichiestaComando:
    """Ciò che una regola vede: il comando richiesto e il valore attuale del dispositivo (None se non letto)."""
    attore: str
    target: str
    valore: Any
    valore_attuale: Any = None


# Una regola riceve la richiesta e restituisce il motivo (in italiano) per cui blocca il comando, oppure None.
Regola = Callable[[RichiestaComando], "str | None | Awaitable[str | None]"]
_regole: dict[str, Regola] = {}


def registra_regola(id_regola: str, funzione: Regola) -> None:
    """Registra una regola deterministica (un riflesso) che può bloccare i comandi. Stesso id: sostituisce."""
    _regole[id_regola] = funzione


def rimuovi_regola(id_regola: str) -> bool:
    return _regole.pop(id_regola, None) is not None


def regole_registrate() -> list[str]:
    return list(_regole)


def _peso_richiedente(attore: str) -> float:
    if attore.casefold() == NOME_BRAIN.casefold():
        return PRIORITA_BRAIN
    peso = priorita_attore(attore)
    return 0.0 if peso is None else float(peso)


async def _prima_regola_che_blocca(richiesta: RichiestaComando) -> tuple[str, str] | None:
    """(id della regola, motivo) della prima regola che si oppone. Una regola che solleva fa fallire il comando (chiusura sicura)."""
    for id_regola, funzione in list(_regole.items()):
        risultato = funzione(richiesta)
        if inspect.isawaitable(risultato):
            risultato = await risultato
        if risultato:
            return id_regola, str(risultato)
    return None


def _stato_da_errore_tool(codice: str | None) -> str:
    if codice in _CODICI_OFFLINE:
        return DISPOSITIVO_OFFLINE
    if codice in _CODICI_RIFIUTO:
        return RIFIUTATO_DAL_DISPOSITIVO
    return ERRORE_INTERNO


async def tenta_comando(
    attore: str,
    target: str,
    valore: Any,
    correlation_id: str | None = None,
    *,
    azione: str = "SET_VALUE",
    motivazione: str | None = None,
    command_id: str | None = None,
    tools_map: dict[str, Any] | None = None,
    db_path: str | None = None,
    finestra_conflitto_minuti: int = FINESTRA_CONFLITTO_MINUTI,
) -> EsitoComando:
    """
    Tenta di portare `target` a `valore` per conto di `attore` (`user:<email>`, `rule:<id>`, `organ:<nome>`, `brain`,
    `assistant`, `system`, o il nome di un agente). Non solleva mai: restituisce un `EsitoComando` e scrive la decisione.
    """
    cid = correlation_id or contesto.correlation_id_corrente() or contesto.nuovo_correlation_id()
    attore = contesto.mappa_attore(attore) or (attore or "").strip() or contesto.ATTORE_SISTEMA
    richiesto = str(valore)
    log = NervousSystem(db_path=db_path) if db_path else NervousSystem()
    stato_corrente: dict[str, Any] = {"precedente": "UNKNOWN"}

    async def concludi(stato: str, motivo: str, *, regola: str | None = None, attore_decisione: str | None = None,
                       azione_decisione: str | None = None) -> EsitoComando:
        decision_id = None
        try:
            decision_id = await log.log_event(
                attore_decisione or attore, azione_decisione or azione, target, stato_corrente["precedente"], richiesto,
                f"{motivazione} — {motivo}" if motivazione else motivo,
                outcome=_ESITO_DECISIONE[stato], correlation_id=cid, command_id=command_id)
        except Exception as errore:  # il comando resta com'è; manca solo la riga di audit
            logger.error("[Comandi] Decisione non registrata per '%s' (%s): %s", target, stato, errore)
        return EsitoComando(stato, motivo, decision_id, cid, attore, target, stato_corrente["precedente"], richiesto, regola)

    try:
        # 1. Dispositivo e valore ammessi
        validazione = get_configurazione().valida_comando(target, valore)
        if not validazione.ammesso:
            return await concludi(FUORI_LIMITI, validazione.motivo, azione_decisione=f"INVALID_COMMAND_{azione}")
        valore = validazione.valore
        richiesto = str(valore)

        tool = trova_tool(target, tools_map)
        if tool is None:
            return await concludi(DISPOSITIVO_OFFLINE, f"Il dispositivo '{target}' non è registrato nel sistema.",
                                  azione_decisione=f"TOOL_ERROR_{azione}")

        # 2. Valore attuale (serve alla traccia e alle regole); una lettura fallita non ferma il comando
        lettura = await leggi_tool(tool, target)
        if lettura["success"]:
            stato_corrente["precedente"] = str(lettura["value"])

        # 3. Blocchi con priorità maggiore (il Brain non è mai bloccato)
        if attore.casefold() != NOME_BRAIN.casefold():
            eventi = await log.get_recent_events()
            motivo = trova_blocco_prevalente(eventi, target, attore, _peso_richiedente(attore), finestra_conflitto_minuti)
            if motivo:
                return await concludi(BLOCCATO_DA_PRIORITA, motivo, azione_decisione=f"REJECTED_{azione}")

        # 4. Regole deterministiche
        richiesta = RichiestaComando(attore, target, valore, lettura.get("value") if lettura["success"] else None)
        bloccante = await _prima_regola_che_blocca(richiesta)
        if bloccante:
            id_regola, motivo = bloccante
            return await concludi(BLOCCATO_DA_REGOLA, motivo, regola=id_regola, attore_decisione=f"rule:{id_regola}",
                                  azione_decisione=f"REJECTED_{azione}")

        # 5. Azionamento
        comando = await comanda_tool(tool, target, valore)
        if not comando["success"]:
            stato = _stato_da_errore_tool(comando.get("error_type"))
            return await concludi(stato, comando["response"], azione_decisione=f"TOOL_ERROR_{azione}")
        return await concludi(APPLICATO, f"'{target}': {stato_corrente['precedente']} -> {richiesto}.")
    except Exception as errore:
        logger.exception("[Comandi] Errore interno su '%s'", target)
        return await concludi(ERRORE_INTERNO, f"Errore interno: {errore}", azione_decisione=f"TOOL_ERROR_{azione}")

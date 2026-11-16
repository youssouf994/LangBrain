"""
Configurazione delle scelte dell'utente (file `configurazione.toml`).

Contiene ciò che è una scelta strutturata dell'utente e non un segreto: l'elenco dei dispositivi con i valori
ammessi per i comandi e la politica di ripiego dei provider LLM. Il `.env` resta per chiavi API e parametri d'ambiente.
Priorità: variabile d'ambiente > configurazione.toml > default nel codice.

Il file viene riletto a caldo quando cambia. Se la nuova versione è errata resta attiva la precedente.
"""

import fnmatch
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.constants import FINESTRA_SEGNALI_ORE_PREDEFINITA, is_control_flag

logger = logging.getLogger(__name__)

PERCORSO_PREDEFINITO = Path(__file__).resolve().parents[2] / "configurazione.toml"
# Nome alternativo per chi preferisce i nomi inglesi: usato se configurazione.toml non esiste.
PERCORSO_INGLESE = PERCORSO_PREDEFINITO.with_name("config.toml")


def percorso_configurazione() -> Path:
    """LANGBRAIN_CONFIG, altrimenti configurazione.toml, altrimenti config.toml."""
    if os.getenv("LANGBRAIN_CONFIG"):
        return Path(os.environ["LANGBRAIN_CONFIG"])
    return PERCORSO_PREDEFINITO if PERCORSO_PREDEFINITO.exists() or not PERCORSO_INGLESE.exists() else PERCORSO_INGLESE

POLITICA_RIFIUTA = "rifiuta"
POLITICA_CONSENTI = "consenti"

# Cosa succede quando scade il timer HITL senza una risposta dell'operatore.
SCADENZA_SISTEMA = "sistema"   # decide il sistema: il Brain valuta con il suo modello, come senza HITL
SCADENZA_RESPINGI = "respingi"  # respinge in automatico, nessuna azione fisica
SCADENZA_UMANO = "umano"        # il grafo resta in pausa e aspetta l'operatore
_ALIAS_SCADENZA = {"nessuna": SCADENZA_UMANO}
TIMER_HITL_PREDEFINITO_SECONDI = 300

# Dove si applica l'HITL: sui nodi del grafo (wrapper), sulle escalation valutate dal Brain, oppure su entrambi.
HITL_LIVELLO_NODI = "nodi"
HITL_LIVELLO_BRAIN = "brain"
HITL_LIVELLO_ENTRAMBI = "entrambi"
TARGET_CRITICI_BRAIN_PREDEFINITI = ["alarm_system", "front_door_lock"]

# Ordine e modelli di ripiego usati se il file manca o non li indica: solo modelli gratuiti o molto economici.
ORDINE_PROVIDER_PREDEFINITO = ["google_studio", "mistral", "openrouter", "local"]
MODELLI_DI_RIPIEGO_PREDEFINITI = {
    "openrouter": ["google/gemma-4-31b-it:free", "nvidia/nemotron-3-super-120b-a12b:free"],
    "mistral": ["ministral-3b-latest"],
    "google_studio": ["gemini-flash-latest"],
}


class ErroreConfigurazione(ValueError):
    """Il file di configurazione non è valido."""


@dataclass(frozen=True)
class RegolaDispositivo:
    nome: str
    valori: tuple[str, ...] = ()
    intervallo: tuple[float, float] | None = None
    unita: str = ""
    valore_attivo: str | None = None
    adattatore: str | None = None
    """Come si parla col dispositivo vero (`"http"`); None: effettore simulato in memoria."""
    opzioni_adattatore: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def senza_vincoli(self) -> bool:
        return not self.valori and self.intervallo is None

    def descrizione(self) -> str:
        parti = []
        if self.valori:
            parti.append(str(list(self.valori)))
        if self.intervallo is not None:
            minimo, massimo = self.intervallo
            parti.append(f"numero tra {minimo:g} e {massimo:g}{(' ' + self.unita) if self.unita else ''}")
        return " oppure ".join(parti) if parti else "qualsiasi valore"


@dataclass(frozen=True)
class EsitoValidazione:
    ammesso: bool
    valore: Any = None
    """Valore da inviare al dispositivo (forma canonica del valore accettato)."""
    motivo: str = ""


@dataclass(frozen=True)
class AttoreEsterno:
    """
    Un organo che scrive sul sistema nervoso senza essere un nodo del grafo (un sistema esterno, una centrale di
    sicurezza, un agente di un altro processo). Il livello lo colloca nella gerarchia per le statistiche; la priorità
    pesa i suoi blocchi sui dispositivi come quella di un agente registrato.
    """
    nome: str
    livello: int = 1
    priorita: float = 0.0
    descrizione: str = ""


@dataclass
class Configurazione:
    llm_fallback: bool = False
    ordine_provider: list[str] = field(default_factory=lambda: list(ORDINE_PROVIDER_PREDEFINITO))
    modelli_di_ripiego: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in MODELLI_DI_RIPIEGO_PREDEFINITI.items()}
    )
    dispositivi_non_elencati: str = POLITICA_RIFIUTA
    regole: list[RegolaDispositivo] = field(default_factory=list)
    hitl_timer_attivo: bool = False
    hitl_timer_secondi: int = TIMER_HITL_PREDEFINITO_SECONDI
    hitl_azione_alla_scadenza: str = SCADENZA_UMANO
    hitl_livello: str = HITL_LIVELLO_ENTRAMBI
    hitl_target_critici_brain: list[str] = field(default_factory=lambda: list(TARGET_CRITICI_BRAIN_PREDEFINITI))
    demo_conflitto_all_avvio: bool = False
    demo_pagina_web: bool = True
    finestra_segnali_ore: float = FINESTRA_SEGNALI_ORE_PREDEFINITA   # `[segnali] finestra_ore`: quanto indietro arrivano i segnali nello stato
    prompt: dict[str, str] = field(default_factory=dict)
    llm_soglia_errori: int = 5            # `[llm.protezione]`: errori di fila che aprono l'interruttore di un modello
    llm_pausa_secondi: float = 60.0       # ...e per quanto resta aperto
    llm_timeout: dict[str, float] = field(default_factory=dict)   # `[llm.timeout_secondi]`: per provider o "provider/modello"
    llm_budget: dict[str, dict[str, int]] = field(default_factory=dict)   # `[llm.budget]`: "*" e per provider; 0 = nessun limite
    stato_fisico_persistenza: bool = True          # `[stato_fisico] persistenza`
    stato_fisico_intervallo_secondi: float = 300.0   # `[stato_fisico] intervallo_secondi`: 0 = solo all'avvio
    lingua: str = "it"     # lingua dei prompt predefiniti, dei messaggi dell'API e delle pagine (it, en)
    attori_esterni: dict[str, AttoreEsterno] = field(default_factory=dict)

    # ------------------------------------------------------------------ dispositivi

    def regola_per(self, dispositivo: str) -> RegolaDispositivo | None:
        """Regola del dispositivo: prima il nome esatto, poi il primo pattern che corrisponde."""
        for regola in self.regole:
            if regola.nome == dispositivo:
                return regola
        for regola in self.regole:
            if any(c in regola.nome for c in "*?[") and fnmatch.fnmatchcase(dispositivo, regola.nome):
                return regola
        return None

    def dispositivo_ammesso(self, dispositivo: str) -> bool:
        """True se il dispositivo è elencato oppure la politica consente quelli non elencati."""
        return self.regola_per(dispositivo) is not None or self.dispositivi_non_elencati == POLITICA_CONSENTI

    def valida_comando(self, dispositivo: str, valore: Any) -> EsitoValidazione:
        """Verifica che `valore` sia un comando ammesso per `dispositivo` e ne restituisce la forma canonica."""
        if valore is None or str(valore).strip() == "":
            return EsitoValidazione(False, motivo=f"Valore mancante per il dispositivo '{dispositivo}'.")
        testo = str(valore).strip()
        if is_control_flag(testo):
            return EsitoValidazione(True, valore)

        regola = self.regola_per(dispositivo)
        if regola is None:
            if self.dispositivi_non_elencati == POLITICA_CONSENTI:
                return EsitoValidazione(True, valore)
            return EsitoValidazione(
                False, motivo=f"Il dispositivo '{dispositivo}' non è elencato in configurazione.toml: comando non ammesso."
            )
        if regola.senza_vincoli:
            return EsitoValidazione(True, valore)

        for ammesso in regola.valori:
            if ammesso.casefold() == testo.casefold():
                return EsitoValidazione(True, ammesso)
        if regola.intervallo is not None:
            unita = re.escape(regola.unita) if regola.unita else ""
            trovato = re.fullmatch(rf"\s*(-?\d+(?:[.,]\d+)?)\s*(?:{unita})?\s*" if unita else r"\s*(-?\d+(?:[.,]\d+)?)\s*", testo, re.IGNORECASE)
            if trovato:
                numero = float(trovato.group(1).replace(",", "."))
                if regola.intervallo[0] <= numero <= regola.intervallo[1]:
                    return EsitoValidazione(True, valore)
        return EsitoValidazione(
            False, motivo=f"Valore '{testo}' non ammesso per '{dispositivo}': ammessi {regola.descrizione()}."
        )

    def valore_attivo(self, dispositivo: str, default: str = "ON") -> str:
        """Valore da inviare quando un agente decide genericamente di attivare il dispositivo."""
        regola = self.regola_per(dispositivo)
        return regola.valore_attivo if regola and regola.valore_attivo else default

    def descrizione_valori(self, dispositivo: str) -> str | None:
        """Testo dei valori ammessi per guidare l'LLM, oppure None se il dispositivo non è elencato o non ha vincoli."""
        regola = self.regola_per(dispositivo)
        return None if regola is None or regola.senza_vincoli else regola.descrizione()


# --------------------------------------------------------------------------- lettura del file


def _regola_da_dati(nome: str, dati: Any) -> RegolaDispositivo:
    if not isinstance(dati, dict):
        raise ErroreConfigurazione(f"[dispositivi.{nome}] deve essere una sezione.")
    valori = dati.get("valori", [])
    if not isinstance(valori, list) or not all(isinstance(v, str) and v.strip() for v in valori):
        raise ErroreConfigurazione(f"[dispositivi.{nome}] 'valori' deve essere una lista di stringhe non vuote.")
    intervallo = dati.get("intervallo")
    if intervallo is not None:
        if (
            not isinstance(intervallo, list) or len(intervallo) != 2
            or not all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in intervallo)
            or intervallo[0] > intervallo[1]
        ):
            raise ErroreConfigurazione(f"[dispositivi.{nome}] 'intervallo' deve essere [minimo, massimo] numerico.")
        intervallo = (float(intervallo[0]), float(intervallo[1]))
    adattatore, opzioni = _adattatore_da_dati(nome, dati)
    regola = RegolaDispositivo(
        nome=nome,
        valori=tuple(valori),
        intervallo=intervallo,
        unita=str(dati.get("unita", "")),
        valore_attivo=dati.get("valore_attivo"),
        adattatore=adattatore,
        opzioni_adattatore=opzioni,
    )
    return regola


ADATTATORI_NOTI = ("http",)


def _adattatore_da_dati(nome: str, dati: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """`adattatore` e le sue opzioni da `[dispositivi.nome]`: None se il dispositivo è simulato."""
    adattatore = dati.get("adattatore")
    if adattatore is None:
        return None, {}
    dove = f"[dispositivi.{nome}]"
    if adattatore not in ADATTATORI_NOTI:
        raise ErroreConfigurazione(f"{dove} 'adattatore' deve essere uno tra {', '.join(ADATTATORI_NOTI)}.")
    if any(c in nome for c in "*?["):
        raise ErroreConfigurazione(f"{dove} un adattatore vale per un dispositivo solo: il nome non può essere un pattern.")
    opzioni: dict[str, Any] = {}
    for chiave in ("url", "url_comando"):
        valore = dati.get(chiave)
        if valore is None:
            continue
        if not isinstance(valore, str) or not valore.startswith(("http://", "https://")):
            raise ErroreConfigurazione(f"{dove} '{chiave}' deve essere un indirizzo http:// o https://.")
        opzioni[chiave] = valore
    if "url" not in opzioni:
        raise ErroreConfigurazione(f"{dove} con l'adattatore \"{adattatore}\" serve 'url'.")
    for chiave in ("token_env", "campo_valore"):
        valore = dati.get(chiave)
        if valore is not None:
            if not isinstance(valore, str) or not valore.strip():
                raise ErroreConfigurazione(f"{dove} '{chiave}' deve essere un testo non vuoto.")
            opzioni[chiave] = valore.strip()
    timeout = dati.get("timeout_secondi")
    if timeout is not None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 120:
            raise ErroreConfigurazione(f"{dove} 'timeout_secondi' deve essere un numero maggiore di zero e al massimo 120.")
        opzioni["timeout_secondi"] = float(timeout)
    soglia = dati.get("soglia_errori")
    if soglia is not None:
        if not isinstance(soglia, int) or isinstance(soglia, bool) or soglia < 1:
            raise ErroreConfigurazione(f"{dove} 'soglia_errori' deve essere un intero maggiore o uguale a 1.")
        opzioni["soglia_errori"] = soglia
    pausa = dati.get("pausa_secondi")
    if pausa is not None:
        if not isinstance(pausa, (int, float)) or isinstance(pausa, bool) or pausa <= 0:
            raise ErroreConfigurazione(f"{dove} 'pausa_secondi' deve essere un numero maggiore di zero.")
        opzioni["pausa_secondi"] = float(pausa)
    return adattatore, opzioni


def _numero(dove: str, nome: str, valore: Any, intero: bool = False, consenti_zero: bool = False):
    minimo_ok = (lambda v: v >= 0) if consenti_zero else (lambda v: v > 0)
    tipo_ok = isinstance(valore, int) if intero else isinstance(valore, (int, float))
    if not tipo_ok or isinstance(valore, bool) or not minimo_ok(valore):
        qual = "un intero" if intero else "un numero"
        limite = "maggiore o uguale a zero" if consenti_zero else "maggiore di zero"
        raise ErroreConfigurazione(f"{dove} '{nome}' deve essere {qual} {limite}.")
    return valore


def _protezione_llm_da_dati(llm: dict[str, Any]) -> tuple[int, float, dict[str, float], dict[str, dict[str, int]]]:
    """Soglia e pausa dell'interruttore, timeout per provider/modello e budget da `[llm.*]`."""
    protezione = llm.get("protezione", {})
    if not isinstance(protezione, dict):
        raise ErroreConfigurazione("[llm.protezione] deve essere una sezione.")
    soglia = int(_numero("[llm.protezione]", "soglia_errori", protezione.get("soglia_errori", 5), intero=True))
    pausa = float(_numero("[llm.protezione]", "pausa_secondi", protezione.get("pausa_secondi", 60)))
    timeout_grezzo = llm.get("timeout_secondi", {})
    if not isinstance(timeout_grezzo, dict):
        raise ErroreConfigurazione("[llm.timeout_secondi] deve contenere una voce per provider (o \"provider/modello\").")
    timeout = {str(k): float(_numero("[llm.timeout_secondi]", str(k), v)) for k, v in timeout_grezzo.items()}
    budget_grezzo = llm.get("budget", {})
    if not isinstance(budget_grezzo, dict):
        raise ErroreConfigurazione("[llm.budget] deve essere una sezione.")
    budget: dict[str, dict[str, int]] = {}
    voci = {"*": {k: v for k, v in budget_grezzo.items() if not isinstance(v, dict)},
            **{k: v for k, v in budget_grezzo.items() if isinstance(v, dict)}}
    for provider, limiti in voci.items():
        dove = "[llm.budget]" if provider == "*" else f"[llm.budget.{provider}]"
        ignote = set(limiti) - {"richieste_ora", "token_giorno"}
        if ignote:
            raise ErroreConfigurazione(f"{dove} voci sconosciute: {', '.join(sorted(ignote))} (ammesse: richieste_ora, token_giorno).")
        budget[provider] = {k: int(_numero(dove, k, v, intero=True, consenti_zero=True)) for k, v in limiti.items()}
    return soglia, pausa, timeout, budget


def _interruttore(sezione: str, nome: str, valore: Any) -> bool:
    """Legge una voce 0/1 (accetta anche true/false)."""
    if isinstance(valore, bool):
        return valore
    if valore in (0, 1) and isinstance(valore, int):
        return bool(valore)
    raise ErroreConfigurazione(f"[{sezione}] '{nome}' deve essere 0 o 1.")


# ---------------------------------------------------------------------------------------------- nomi inglesi
# Il file accetta i nomi italiani e quelli inglesi di sezioni, chiavi e valori (stesso significato). Internamente si
# usano gli italiani; `[effectors.*]` è il nome del corpo umano di `[devices.*]` / `[dispositivi.*]`. Un file che usa entrambi i nomi per la stessa voce è rifiutato.
LINGUE = ("it", "en")
_SEZIONI_EN = {"general": "generale", "policy": "politica", "devices": "dispositivi", "effectors": "dispositivi", "prompts": "prompt",
               "organism": "organismo", "signals": "segnali", "physical_state": "stato_fisico"}
_CHIAVI_EN = {
    "generale": {"language": "lingua"},
    "llm": {"provider_order": "ordine_provider", "fallback_models": "modelli_di_ripiego", "protection": "protezione",
            "timeout_seconds": "timeout_secondi"},
    "stato_fisico": {"persistence": "persistenza", "interval_seconds": "intervallo_secondi"},
    "hitl": {"timer_enabled": "timer_attivo", "default_timer_seconds": "timer_predefinito_secondi",
             "on_timeout": "azione_alla_scadenza", "level": "livello", "brain_critical_targets": "target_critici_brain"},
    "demo": {"seed_conflict_at_start": "conflitto_all_avvio", "web_page": "pagina_web"},
    "politica": {"unlisted_devices": "dispositivi_non_elencati", "unlisted_effectors": "dispositivi_non_elencati"},
    "segnali": {"window_hours": "finestra_ore"},
    "organismo": {"external_actors": "attori_esterni"},
    "prompt": {"brain_escalation_user": "brain_escalation_utente", "routine_check": "controllo_macro"},  # + alias delle estensioni
}
_CHIAVI_DISPOSITIVO_EN = {"values": "valori", "range": "intervallo", "unit": "unita", "active_value": "valore_attivo",
                          "adapter": "adattatore", "command_url": "url_comando",
                          "timeout_seconds": "timeout_secondi", "value_field": "campo_valore",
                          "error_threshold": "soglia_errori", "pause_seconds": "pausa_secondi"}
_CHIAVI_PROTEZIONE_EN = {"error_threshold": "soglia_errori", "pause_seconds": "pausa_secondi"}
_CHIAVI_BUDGET_EN = {"requests_per_hour": "richieste_ora", "tokens_per_day": "token_giorno"}
_CHIAVI_ATTORE_EN = {"level": "livello", "priority": "priorita", "description": "descrizione"}
_VALORI_EN = {
    ("hitl", "azione_alla_scadenza"): {"human": "umano", "system": "sistema", "reject": "respingi"},
    ("hitl", "livello"): {"nodes": "nodi", "both": "entrambi"},
    ("politica", "dispositivi_non_elencati"): {"reject": "rifiuta", "refuse": "rifiuta", "allow": "consenti"},
}


def _rinomina(dati: dict[str, Any], nomi: dict[str, str], dove: str) -> dict[str, Any]:
    risultato = dict(dati)
    for inglese, italiano in nomi.items():
        if inglese in risultato:
            if italiano in risultato:
                raise ErroreConfigurazione(f"{dove}: '{inglese}' e '{italiano}' sono la stessa voce, usane una sola.")
            risultato[italiano] = risultato.pop(inglese)
    return risultato


def normalizza_nomi(dati: dict[str, Any]) -> dict[str, Any]:
    """Il contenuto del file con i nomi inglesi tradotti in quelli italiani usati dal codice."""
    dati = _rinomina(dati, _SEZIONI_EN, "configurazione")
    for sezione, nomi in _CHIAVI_EN.items():
        if sezione == "prompt" and isinstance(dati.get(sezione), dict):
            from app.core.estensioni import estensioni

            nomi = {**nomi, **{k: v for e in estensioni() for k, v in e.alias_prompt.items()}}
        if isinstance(dati.get(sezione), dict):
            dati[sezione] = _rinomina(dati[sezione], nomi, f"[{sezione}]")
    llm_sez = dati.get("llm")
    if isinstance(llm_sez, dict):
        if isinstance(llm_sez.get("protezione"), dict):
            llm_sez["protezione"] = _rinomina(llm_sez["protezione"], _CHIAVI_PROTEZIONE_EN, "[llm.protezione]")
        budget = llm_sez.get("budget")
        if isinstance(budget, dict):
            budget = _rinomina(budget, _CHIAVI_BUDGET_EN, "[llm.budget]")
            llm_sez["budget"] = {k: (_rinomina(v, _CHIAVI_BUDGET_EN, f"[llm.budget.{k}]") if isinstance(v, dict) else v)
                                 for k, v in budget.items()}
    for (sezione, chiave), valori in _VALORI_EN.items():
        valore = dati.get(sezione, {}).get(chiave) if isinstance(dati.get(sezione), dict) else None
        if isinstance(valore, str) and valore in valori:
            dati[sezione][chiave] = valori[valore]
    if isinstance(dati.get("dispositivi"), dict):
        dati["dispositivi"] = {nome: _rinomina(regola, _CHIAVI_DISPOSITIVO_EN, f"[dispositivi.{nome}]") if isinstance(regola, dict) else regola
                               for nome, regola in dati["dispositivi"].items()}
    attori = dati.get("organismo", {}).get("attori_esterni") if isinstance(dati.get("organismo"), dict) else None
    if isinstance(attori, dict):
        dati["organismo"]["attori_esterni"] = {nome: _rinomina(a, _CHIAVI_ATTORE_EN, f"[organismo.attori_esterni.{nome}]") if isinstance(a, dict) else a
                                               for nome, a in attori.items()}
    return dati


def _sezione(dati: dict[str, Any], nome: str) -> dict[str, Any]:
    sezione = dati.get(nome, {})
    if not isinstance(sezione, dict):
        raise ErroreConfigurazione(f"[{nome}] deve essere una sezione.")
    return sezione


def _prompt_da_dati(sezione: Any) -> dict[str, str]:
    from app.core.prompt import ErrorePrompt, valida_prompt

    if not isinstance(sezione, dict):
        raise ErroreConfigurazione("[prompt] deve essere una sezione di testi.")
    risultato = {}
    for nome, testo in sezione.items():
        try:
            risultato[nome] = valida_prompt(nome, testo)
        except ErrorePrompt as e:
            raise ErroreConfigurazione(f"[prompt] {e}.") from e
    return risultato


def _attori_esterni_da_dati(sezione: Any) -> dict[str, AttoreEsterno]:
    if not isinstance(sezione, dict):
        raise ErroreConfigurazione("[organismo.attori_esterni] deve contenere una sezione per attore.")
    risultato = {}
    for nome, dati in sezione.items():
        etichetta = f"[organismo.attori_esterni.{nome}]"
        if not isinstance(dati, dict):
            raise ErroreConfigurazione(f"{etichetta} deve essere una sezione.")
        if nome.casefold().startswith("brain"):
            raise ErroreConfigurazione(f"{etichetta} il Brain non è un attore esterno.")
        livello = dati.get("livello", 1)
        if not isinstance(livello, int) or isinstance(livello, bool) or livello < 1:
            raise ErroreConfigurazione(f"{etichetta} 'livello' deve essere un intero maggiore o uguale a 1.")
        priorita = dati.get("priorita", 0.0)
        if not isinstance(priorita, (int, float)) or isinstance(priorita, bool) or not 0 <= priorita < float("inf"):
            raise ErroreConfigurazione(f"{etichetta} 'priorita' deve essere un numero finito maggiore o uguale a 0.")
        descrizione = dati.get("descrizione", "")
        if not isinstance(descrizione, str):
            raise ErroreConfigurazione(f"{etichetta} 'descrizione' deve essere un testo.")
        risultato[nome] = AttoreEsterno(nome=nome, livello=livello, priorita=float(priorita), descrizione=descrizione)
    return risultato


def costruisci_configurazione(dati: dict[str, Any]) -> Configurazione:
    """Costruisce e valida la configurazione a partire dal contenuto del file TOML (nomi italiani o inglesi)."""
    dati = normalizza_nomi(dati)
    lingua = _sezione(dati, "generale").get("lingua", "it")
    if lingua not in LINGUE:
        raise ErroreConfigurazione(f"[generale] 'lingua' deve essere uno tra {', '.join(LINGUE)}.")
    llm = dati.get("llm", {})
    if not isinstance(llm.get("fallback", False), bool):
        raise ErroreConfigurazione("[llm] 'fallback' deve essere true o false.")
    ordine = llm.get("ordine_provider", ORDINE_PROVIDER_PREDEFINITO)
    if not isinstance(ordine, list) or not all(isinstance(p, str) for p in ordine):
        raise ErroreConfigurazione("[llm] 'ordine_provider' deve essere una lista di nomi di provider.")
    ripiego = {k: list(v) for k, v in MODELLI_DI_RIPIEGO_PREDEFINITI.items()}
    for provider, modelli in llm.get("modelli_di_ripiego", {}).items():
        if not isinstance(modelli, list) or not all(isinstance(m, str) for m in modelli):
            raise ErroreConfigurazione(f"[llm.modelli_di_ripiego] '{provider}' deve essere una lista di nomi di modelli.")
        ripiego[provider] = modelli

    politica = dati.get("politica", {}).get("dispositivi_non_elencati", POLITICA_RIFIUTA)
    if politica not in (POLITICA_RIFIUTA, POLITICA_CONSENTI):
        raise ErroreConfigurazione(
            f"[politica] 'dispositivi_non_elencati' deve essere \"{POLITICA_RIFIUTA}\" o \"{POLITICA_CONSENTI}\"."
        )

    hitl = dati.get("hitl", {})
    durata = hitl.get("timer_predefinito_secondi", TIMER_HITL_PREDEFINITO_SECONDI)
    if not isinstance(durata, int) or isinstance(durata, bool) or durata <= 0:
        raise ErroreConfigurazione("[hitl] 'timer_predefinito_secondi' deve essere un intero maggiore di zero.")
    azione = _ALIAS_SCADENZA.get(hitl.get("azione_alla_scadenza", SCADENZA_UMANO), hitl.get("azione_alla_scadenza", SCADENZA_UMANO))
    if azione not in (SCADENZA_SISTEMA, SCADENZA_RESPINGI, SCADENZA_UMANO):
        raise ErroreConfigurazione(
            f"[hitl] 'azione_alla_scadenza' deve essere \"{SCADENZA_SISTEMA}\", \"{SCADENZA_RESPINGI}\" o \"{SCADENZA_UMANO}\"."
        )
    livello = hitl.get("livello", HITL_LIVELLO_ENTRAMBI)
    if livello not in (HITL_LIVELLO_NODI, HITL_LIVELLO_BRAIN, HITL_LIVELLO_ENTRAMBI):
        raise ErroreConfigurazione(
            f"[hitl] 'livello' deve essere \"{HITL_LIVELLO_NODI}\", \"{HITL_LIVELLO_BRAIN}\" o \"{HITL_LIVELLO_ENTRAMBI}\"."
        )
    critici = hitl.get("target_critici_brain", TARGET_CRITICI_BRAIN_PREDEFINITI)
    if not isinstance(critici, list) or not all(isinstance(t, str) and t for t in critici):
        raise ErroreConfigurazione("[hitl] 'target_critici_brain' deve essere una lista di nomi di dispositivi.")

    finestra = _sezione(dati, "segnali").get("finestra_ore", FINESTRA_SEGNALI_ORE_PREDEFINITA)
    if not isinstance(finestra, (int, float)) or isinstance(finestra, bool) or not 0 < finestra <= 24 * 90:
        raise ErroreConfigurazione("[segnali] 'finestra_ore' deve essere un numero maggiore di zero e al massimo 2160 (90 giorni).")

    soglia_llm, pausa_llm, timeout_llm, budget_llm = _protezione_llm_da_dati(llm)
    stato_fisico = _sezione(dati, "stato_fisico")
    intervallo_stato = stato_fisico.get("intervallo_secondi", 300)
    if not isinstance(intervallo_stato, (int, float)) or isinstance(intervallo_stato, bool) or intervallo_stato < 0:
        raise ErroreConfigurazione("[stato_fisico] 'intervallo_secondi' deve essere un numero maggiore o uguale a zero (0: solo all'avvio).")

    configurazione = Configurazione(
        llm_soglia_errori=soglia_llm,
        llm_pausa_secondi=pausa_llm,
        llm_timeout=timeout_llm,
        llm_budget=budget_llm,
        stato_fisico_persistenza=_interruttore("stato_fisico", "persistenza", stato_fisico.get("persistenza", 1)),
        stato_fisico_intervallo_secondi=float(intervallo_stato),
        finestra_segnali_ore=float(finestra),
        hitl_timer_attivo=_interruttore("hitl", "timer_attivo", hitl.get("timer_attivo", 0)),
        hitl_timer_secondi=durata,
        hitl_azione_alla_scadenza=azione,
        hitl_livello=livello,
        hitl_target_critici_brain=list(critici),
        demo_conflitto_all_avvio=_interruttore("demo", "conflitto_all_avvio", dati.get("demo", {}).get("conflitto_all_avvio", 0)),
        demo_pagina_web=_interruttore("demo", "pagina_web", dati.get("demo", {}).get("pagina_web", 1)),
        llm_fallback=llm.get("fallback", False),
        ordine_provider=ordine,
        modelli_di_ripiego=ripiego,
        dispositivi_non_elencati=politica,
        regole=[_regola_da_dati(nome, sezione) for nome, sezione in dati.get("dispositivi", {}).items()],
        prompt=_prompt_da_dati(dati.get("prompt", {})),
        lingua=lingua,
        attori_esterni=_attori_esterni_da_dati(_sezione(dati, "organismo").get("attori_esterni", {})),
    )
    # Il valore di attivazione deve essere a sua volta un comando ammesso dal dispositivo.
    for regola in configurazione.regole:
        if regola.valore_attivo is not None and not configurazione.valida_comando(regola.nome, regola.valore_attivo).ammesso:
            raise ErroreConfigurazione(
                f"[dispositivi.{regola.nome}] 'valore_attivo' = '{regola.valore_attivo}' non rientra nei valori ammessi "
                f"({regola.descrizione()})."
            )
    return configurazione


def carica_configurazione(percorso: str | os.PathLike | None = None) -> Configurazione:
    """
    Legge il file di configurazione. Se il file non esiste usa i default con la politica "consenti" (comportamento
    precedente) e un avviso: senza elenco esplicito non c'è nulla da rifiutare.
    Solleva ErroreConfigurazione se il file esiste ma non è valido.
    """
    percorso = Path(percorso or percorso_configurazione())
    if not percorso.exists():
        logger.warning("[Configurazione] File %s non trovato: uso i default, dispositivi non elencati consentiti.", percorso)
        return Configurazione(dispositivi_non_elencati=POLITICA_CONSENTI)
    try:
        with open(percorso, "rb") as f:
            return costruisci_configurazione(tomllib.load(f))
    except tomllib.TOMLDecodeError as e:
        raise ErroreConfigurazione(f"{percorso.name}: TOML non valido ({e}).") from e


# --------------------------------------------------------------------------- istanza condivisa con ricarica a caldo

_configurazione: Configurazione | None = None
_percorso_letto: Path | None = None
_mtime_letto: int | None = None


def _mtime(percorso: Path) -> int | None:
    try:
        return percorso.stat().st_mtime_ns
    except OSError:
        return None


def get_configurazione() -> Configurazione:
    """Configurazione corrente; se il file è cambiato dall'ultima lettura viene riletto (errori: resta la precedente)."""
    global _configurazione, _percorso_letto, _mtime_letto
    percorso = percorso_configurazione()
    mtime = _mtime(percorso)
    if _configurazione is not None and percorso == _percorso_letto and mtime == _mtime_letto:
        return _configurazione
    try:
        nuova = carica_configurazione(percorso)
    except ErroreConfigurazione as e:
        if _configurazione is None:
            raise
        logger.error("[Configurazione] File modificato ma non valido, resta attiva la versione precedente: %s", e)
        _mtime_letto = mtime
        return _configurazione
    if _configurazione is not None:
        logger.info("[Configurazione] File %s ricaricato.", percorso.name)
    _configurazione, _percorso_letto, _mtime_letto = nuova, percorso, mtime
    return nuova


def imposta_configurazione(configurazione: Configurazione | None) -> None:
    """Sostituisce la configurazione condivisa (test). None forza una nuova lettura dal file."""
    global _configurazione, _percorso_letto, _mtime_letto
    _configurazione = configurazione
    _percorso_letto = percorso_configurazione() if configurazione else None
    _mtime_letto = _mtime(_percorso_letto) if configurazione else None

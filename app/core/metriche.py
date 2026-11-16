"""
Metriche in formato Prometheus (`GET /metrics`), senza dipendenze esterne.

Tre tipi: contatore (`incrementa`), indicatore (`imposta`, può salire e scendere) e istogramma (`osserva`, per le durate).
Le etichette sono parole chiave: `incrementa("langbrain_llm_calls_total", provider="mistral", outcome="ok")`.

  - Le metriche sono in memoria del processo. Con più processi (`python -m app.serve`) ognuno pubblica ogni pochi secondi la
    propria istantanea nella tabella `runtime_state` e `GET /metrics` somma quelle di tutti (i contatori e gli istogrammi si
    sommano, gli indicatori prendono il massimo): chi legge le metriche vede un servizio solo, qualunque processo risponda.
  - I `collettori` sono funzioni che a ogni lettura calcolano gli indicatori che non si incrementano da soli (stato dei
    dispositivi e degli interruttori, database raggiungibile, durata di vita).
  - Le etichette devono avere pochi valori possibili: nessun id di richiesta, nessun testo libero. Le rotte HTTP si
    etichettano col modello del percorso (`/effectors/{device_id}`), non col percorso richiesto.

Un errore nella raccolta o nella pubblicazione non deve mai rompere ciò che si sta misurando: tutto qui dentro è protetto.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Awaitable, Callable

from sqlalchemy import select

from app.core import archivio, multiprocesso
from app.db import schema

logger = logging.getLogger(__name__)

PREFISSO_CHIAVE = "metriche:"
INTERVALLO_PUBBLICAZIONE_SECONDI = 5.0
VALIDITA_ISTANTANEA_SECONDI = 60.0
DURATE_SECONDI = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

SECCHIO_INFINITO = 'le="+Inf"'
CONTATORE, INDICATORE, ISTOGRAMMA = "counter", "gauge", "histogram"

# Elenco delle metriche note: tipo e descrizione (la riga `# HELP` / `# TYPE`). Una metrica non elencata si pubblica lo stesso, come `untyped`.
DESCRIZIONI: dict[str, tuple[str, str]] = {
    "langbrain_http_requests_total": (CONTATORE, "Richieste HTTP servite, per metodo, rotta e stato."),
    "langbrain_http_request_duration_seconds": (ISTOGRAMMA, "Durata delle richieste HTTP, per metodo e rotta."),
    "langbrain_llm_calls_total": (CONTATORE, "Chiamate al modello linguistico, per provider, modello ed esito (ok o codice dell'errore)."),
    "langbrain_llm_call_duration_seconds": (ISTOGRAMMA, "Durata delle chiamate al modello linguistico, per provider."),
    "langbrain_llm_tokens_estimated_total": (CONTATORE, "Token stimati (4 caratteri per token), per provider e direzione (in/out)."),
    "langbrain_llm_circuit_state": (INDICATORE, "Stato dell'interruttore del modello: 0 chiuso, 1 in prova, 2 aperto."),
    "langbrain_llm_circuit_rejections_total": (CONTATORE, "Chiamate respinte senza raggiungere il provider (interruttore aperto o budget esaurito)."),
    "langbrain_llm_budget_used_ratio": (INDICATORE, "Quota di budget usata (0..1), per provider e tipo (richieste_ora, token_giorno)."),
    "langbrain_device_operations_total": (CONTATORE, "Letture e comandi ai dispositivi, per dispositivo, operazione ed esito."),
    "langbrain_device_operation_duration_seconds": (ISTOGRAMMA, "Durata di letture e comandi ai dispositivi, per operazione."),
    "langbrain_device_online": (INDICATORE, "1 se il dispositivo risponde, 0 se no."),
    "langbrain_device_circuit_state": (INDICATORE, "Stato dell'interruttore del dispositivo: 0 chiuso, 1 in prova, 2 aperto."),
    "langbrain_device_reconciliations_total": (CONTATORE, "Riconciliazioni dello stato fisico, per esito (allineato, deriva, irraggiungibile, ripristinato)."),
    "langbrain_graph_node_runs_total": (CONTATORE, "Esecuzioni dei nodi del grafo, per nodo ed esito."),
    "langbrain_graph_node_duration_seconds": (ISTOGRAMMA, "Durata dei nodi del grafo, per nodo."),
    "langbrain_database_up": (INDICATORE, "1 se il database risponde."),
    "langbrain_uptime_seconds": (INDICATORE, "Secondi dall'avvio del processo."),
    "langbrain_build_info": (INDICATORE, "Versione del nucleo (sempre 1)."),
    "langbrain_workers": (INDICATORE, "Processi che hanno pubblicato le metriche di recente."),
}

_Chiave = tuple[str, tuple[tuple[str, str], ...]]
_contatori: dict[_Chiave, float] = {}
_indicatori: dict[_Chiave, float] = {}
_istogrammi: dict[_Chiave, list[float]] = {}   # [conteggio, somma, c(b1), c(b2), ...] con i secchi cumulativi in fondo
_collettori: list[Callable[[], Awaitable[None]]] = []


def _chiave(nome: str, etichette: dict) -> _Chiave:
    return nome, tuple(sorted((k, str(v)) for k, v in etichette.items()))


def incrementa(nome: str, valore: float = 1.0, **etichette) -> None:
    chiave = _chiave(nome, etichette)
    _contatori[chiave] = _contatori.get(chiave, 0.0) + valore


def imposta(nome: str, valore: float, **etichette) -> None:
    _indicatori[_chiave(nome, etichette)] = float(valore)


def osserva(nome: str, valore: float, **etichette) -> None:
    chiave = _chiave(nome, etichette)
    riga = _istogrammi.get(chiave)
    if riga is None:
        riga = _istogrammi[chiave] = [0.0, 0.0] + [0.0] * len(DURATE_SECONDI)
    riga[0] += 1
    riga[1] += valore
    for i, limite in enumerate(DURATE_SECONDI):
        if valore <= limite:
            riga[2 + i] += 1


def registra_collettore(funzione: Callable[[], Awaitable[None]]) -> None:
    """Una funzione asincrona chiamata a ogni lettura per aggiornare gli indicatori; se è già registrata non si duplica."""
    if funzione not in _collettori:
        _collettori.append(funzione)


@contextlib.contextmanager
def misura(nome_durata: str, **etichette):
    """Misura la durata del blocco e la registra nell'istogramma `nome_durata`."""
    inizio = time.perf_counter()
    try:
        yield
    finally:
        osserva(nome_durata, time.perf_counter() - inizio, **etichette)


def azzera() -> None:
    """Svuota tutte le metriche (test)."""
    _contatori.clear()
    _indicatori.clear()
    _istogrammi.clear()


# ------------------------------------------------------------------------------------------ istantanee e somma

def istantanea() -> dict:
    """Tutte le metriche del processo come dizionario serializzabile (JSON)."""
    def come_lista(tabella):
        return [[nome, list(etichette), valore] for (nome, etichette), valore in tabella.items()]
    return {"contatori": come_lista(_contatori), "indicatori": come_lista(_indicatori), "istogrammi": come_lista(_istogrammi)}


def _in_chiave(nome: str, etichette) -> _Chiave:
    return nome, tuple((k, v) for k, v in etichette)


def somma(istantanee: list[dict]) -> dict[str, dict[_Chiave, object]]:
    """Unisce le istantanee dei processi: contatori e istogrammi si sommano, gli indicatori prendono il valore massimo."""
    contatori: dict[_Chiave, float] = {}
    indicatori: dict[_Chiave, float] = {}
    istogrammi: dict[_Chiave, list[float]] = {}
    for foto in istantanee:
        for nome, etichette, valore in foto.get("contatori", []):
            k = _in_chiave(nome, etichette)
            contatori[k] = contatori.get(k, 0.0) + valore
        for nome, etichette, valore in foto.get("indicatori", []):
            k = _in_chiave(nome, etichette)
            indicatori[k] = max(indicatori.get(k, valore), valore)
        for nome, etichette, riga in foto.get("istogrammi", []):
            k = _in_chiave(nome, etichette)
            attuale = istogrammi.get(k)
            istogrammi[k] = list(riga) if attuale is None else [a + b for a, b in zip(attuale, riga)]
    return {"contatori": contatori, "indicatori": indicatori, "istogrammi": istogrammi}


# ------------------------------------------------------------------------------------------ pubblicazione tra processi

def _chiave_processo() -> str:
    return PREFISSO_CHIAVE + multiprocesso._PROCESSO


async def pubblica_istantanea() -> None:
    """Scrive l'istantanea del processo in `runtime_state` (solo con più processi). Non solleva."""
    if not multiprocesso.condiviso():
        return
    try:
        await multiprocesso.scrivi(_chiave_processo(), istantanea())
    except Exception as errore:
        logger.warning("[Metriche] Pubblicazione dell'istantanea non riuscita: %s", errore)


async def sorveglia() -> None:
    """Compito in background di ogni processo: pubblica l'istantanea a intervalli regolari (con un processo solo non fa nulla)."""
    if not multiprocesso.condiviso():
        return
    while True:
        await pubblica_istantanea()
        await asyncio.sleep(INTERVALLO_PUBBLICAZIONE_SECONDI)


async def _istantanee_degli_altri() -> list[dict]:
    """Le istantanee recenti degli altri processi."""
    if not multiprocesso.condiviso():
        return []
    try:
        motore = await multiprocesso._motore()
        tabella = schema.runtime_state
        limite = time.time() - VALIDITA_ISTANTANEA_SECONDI
        async with motore.connect() as db:
            righe = (await db.execute(select(tabella.c.key, tabella.c.value).where(
                tabella.c.key.like(PREFISSO_CHIAVE + "%"), tabella.c.updated_at >= limite))).all()
        return [json.loads(valore) for chiave, valore in righe if chiave != _chiave_processo()]
    except Exception as errore:
        logger.warning("[Metriche] Lettura delle istantanee degli altri processi non riuscita: %s", errore)
        return []


# ------------------------------------------------------------------------------------------ esposizione

def _etichette(coppie, extra: str = "") -> str:
    voci = [f'{k}="{_proteggi(v)}"' for k, v in coppie]
    if extra:
        voci.append(extra)
    return "{" + ",".join(voci) + "}" if voci else ""


def _proteggi(valore: str) -> str:
    return str(valore).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _numero(valore: float) -> str:
    return repr(int(valore)) if float(valore).is_integer() else repr(float(valore))


def formatta(dati: dict[str, dict]) -> str:
    """Il formato testuale di Prometheus (0.0.4) per i dati già sommati."""
    righe: list[str] = []
    nomi = sorted({k[0] for t in dati.values() for k in t})
    for nome in nomi:
        tipo, aiuto = DESCRIZIONI.get(nome, ("untyped", nome))
        righe.append(f"# HELP {nome} {aiuto}")
        righe.append(f"# TYPE {nome} {tipo}")
        for (n, coppie), valore in sorted(dati["contatori"].items()):
            if n == nome:
                righe.append(f"{nome}{_etichette(coppie)} {_numero(valore)}")
        for (n, coppie), valore in sorted(dati["indicatori"].items()):
            if n == nome:
                righe.append(f"{nome}{_etichette(coppie)} {_numero(valore)}")
        for (n, coppie), riga in sorted(dati["istogrammi"].items()):
            if n != nome:
                continue
            for limite, conteggio in zip(DURATE_SECONDI, riga[2:]):
                secchio = 'le="%g"' % limite
                righe.append(f"{nome}_bucket{_etichette(coppie, secchio)} {_numero(conteggio)}")
            righe.append(f"{nome}_bucket{_etichette(coppie, SECCHIO_INFINITO)} {_numero(riga[0])}")
            righe.append(f"{nome}_sum{_etichette(coppie)} {_numero(riga[1])}")
            righe.append(f"{nome}_count{_etichette(coppie)} {_numero(riga[0])}")
    return "\n".join(righe) + "\n"


async def esporta() -> str:
    """Il testo di `GET /metrics`: raccoglie gli indicatori calcolati, somma i processi e formatta."""
    for collettore in list(_collettori):
        try:
            await collettore()
        except Exception as errore:
            logger.warning("[Metriche] Il collettore %s non è riuscito: %s", getattr(collettore, "__name__", collettore), errore)
    altri = await _istantanee_degli_altri()
    dati = somma([istantanea(), *altri])
    dati["indicatori"][_in_chiave("langbrain_workers", [])] = float(1 + len(altri))
    return formatta(dati)


def contesto_http_attivo() -> bool:
    """Le metriche HTTP si possono spegnere con `LANGBRAIN_METRICS=0` (le altre restano: costano quasi nulla)."""
    return (os.getenv("LANGBRAIN_METRICS") or "1").strip().lower() not in ("0", "false", "no", "off")


# ------------------------------------------------------------------------------------------ middleware HTTP

class MiddlewareMetriche:
    """
    Middleware ASGI puro (come `MiddlewareContesto`: non rompe i flussi SSE). Conta le richieste e ne misura la durata,
    etichettando la rotta col suo modello (`/effectors/{device_id}`) e le rotte sconosciute come `non_trovata`.
    La durata di un flusso SSE è quella dell'intero flusso.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not contesto_http_attivo():
            await self.app(scope, receive, send)
            return
        inizio = time.perf_counter()
        stato = {"codice": 500}

        async def invia(messaggio):
            if messaggio["type"] == "http.response.start":
                stato["codice"] = messaggio["status"]
            await send(messaggio)

        try:
            await self.app(scope, receive, invia)
        finally:
            percorso = getattr(scope.get("route"), "path", None) or "non_trovata"
            if percorso != "/metrics":  # chi legge le metriche non deve gonfiarle
                metodo = scope.get("method", "?")
                incrementa("langbrain_http_requests_total", method=metodo, route=percorso, status=stato["codice"])
                osserva("langbrain_http_request_duration_seconds", time.perf_counter() - inizio, method=metodo, route=percorso)

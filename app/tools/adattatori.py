"""
Adattatori per dispositivi veri: ciò che sta tra il nucleo e l'hardware.

Un adattatore è un `Reflex` (stessa interfaccia di `SimulatedEffector`: `get_tool_value`, `set_tool_value`), quindi gli agenti, i
permessi, l'elenco dei valori ammessi e i guasti strutturati funzionano senza accorgersi della differenza. Si sceglie in
`configurazione.toml`, dispositivo per dispositivo:

    [dispositivi.front_door_lock]
    valori = ["LOCKED", "UNLOCKED"]
    adattatore = "http"
    url = "http://gateway.local:8080/devices/front_door_lock"   # GET: legge; POST: comanda (se manca url_comando)
    token_env = "GATEWAY_TOKEN"        # variabile di .env con il token Bearer (mai nel TOML)
    timeout_secondi = 5
    soglia_errori = 3                  # errori di fila che aprono l'interruttore di circuito del dispositivo
    pausa_secondi = 30

`EffettoreHttp` parla con un gateway (Home Assistant, un bridge MQTT, un microservizio del produttore) con un contratto minimo:

    GET  <url>          ->  200  {"value": "LOCKED"}     (anche un JSON scalare o un testo semplice)
    POST <url_comando>  <-  {"value": "UNLOCKED", "command_id": "..."}   ->  2xx (il corpo può riportare {"value": ...} confermato)
                            4xx = il dispositivo rifiuta il comando; 5xx, timeout, rete = guasto

Ogni richiesta porta `Idempotency-Key` (= `command_id`, lo stesso su un eventuale secondo tentativo), `X-Correlation-Id` e `traceparent`
(vedi `app/core/contesto.py`): un gateway che li registra collega il comando fisico alla decisione che l'ha causato. I comandi non si
ripetono alla cieca: solo se la connessione non è mai stata stabilita (`ConnectError`, la richiesta non è partita) si riprova una volta.

Per un protocollo diverso (MQTT, Modbus, Zigbee diretto) basta una sottoclasse di `Reflex` registrata con `registra_tool`: stesso
contratto, nessuna modifica al nucleo.
"""

import json
import logging
import os
import uuid
from typing import Any

import httpx

from app.core import contesto
from app.core.circuito import APERTO, VALORE_NUMERICO, Interruttore
from app.core.risultati import ErroreTool
from app.tools.reflex import Reflex

logger = logging.getLogger(__name__)

TIMEOUT_PREDEFINITO_SECONDI = 5.0
SOGLIA_PREDEFINITA = 3
PAUSA_PREDEFINITA_SECONDI = 30.0


class EffettoreHttp(Reflex):
    """Effettore che legge e comanda un dispositivo tramite un gateway HTTP (vedi la descrizione del modulo)."""

    simulato = False

    def __init__(self, target_device: str, opzioni: dict[str, Any], unit: str = "", transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(target_device=target_device)
        self.unit = unit
        self.url = opzioni["url"]
        self.url_comando = opzioni.get("url_comando", opzioni["url"])
        self.token_env = opzioni.get("token_env")
        self.campo_valore = opzioni.get("campo_valore", "value")
        self.timeout = opzioni.get("timeout_secondi", TIMEOUT_PREDEFINITO_SECONDI)
        self.interruttore = Interruttore(opzioni.get("soglia_errori", SOGLIA_PREDEFINITA),
                                         opzioni.get("pausa_secondi", PAUSA_PREDEFINITA_SECONDI))
        self.ultimo_contatto = None   # datetime UTC dell'ultima operazione riuscita
        self._ultimo_esito_ok: bool | None = None
        self._transport = transport   # per i test: un `httpx.MockTransport`

    @property
    def online(self) -> bool:
        """False se l'ultima operazione è fallita per un guasto di rete/dispositivo o se l'interruttore è aperto."""
        return self._ultimo_esito_ok is not False and self.interruttore.stato != APERTO

    # ------------------------------------------------------------------ rete

    def _intestazioni(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        intestazioni = {"Accept": "application/json"}
        token = os.getenv(self.token_env, "").strip() if self.token_env else ""
        if token:
            intestazioni["Authorization"] = f"Bearer {token}"
        correlazione = contesto.correlation_id_corrente()
        if correlazione:
            intestazioni["X-Correlation-Id"] = correlazione
        traccia = contesto.traceparent_uscente()
        if traccia:
            intestazioni["traceparent"] = traccia
        intestazioni.update(extra or {})
        return intestazioni

    def _segna(self, riuscita: bool) -> None:
        self._ultimo_esito_ok = riuscita
        if riuscita:
            from app.db.schema import adesso_utc
            self.ultimo_contatto = adesso_utc()

    def _verifica_interruttore(self) -> None:
        if not self.interruttore.consenti():
            self._segna(False)
            attesa = int(self.interruttore.attesa_residua) + 1
            raise ErroreTool(
                f"Dispositivo '{self.target_device}' sospeso dopo {self.interruttore.errori_consecutivi} errori di fila: "
                f"nuovo tentativo tra {attesa} s.", codice="DEVICE_CIRCUIT_OPEN", dettagli={"riprova_tra_secondi": attesa})

    def _dopo_errore(self, errore: BaseException) -> None:
        """Un'operazione interrotta (annullamento) non è un guasto: libera la prova dell'interruttore senza contarla."""
        if isinstance(errore, Exception):
            self.interruttore.fallimento()
            self._segna(False)
        else:
            self.interruttore.annulla_prova()

    async def _richiesta(self, metodo: str, url: str, *, json_corpo: Any = None, intestazioni: dict[str, str] | None = None,
                         ripeti_se_non_partita: bool = False, ripeti_sempre: bool = False) -> httpx.Response:
        """Una richiesta HTTP con gli errori di rete tradotti in `ErroreTool`. Gli stati 4xx/5xx li giudica il chiamante."""
        tentativi = 2 if (ripeti_se_non_partita or ripeti_sempre) else 1
        for tentativo in range(tentativi):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                    return await client.request(metodo, url, json=json_corpo, headers=self._intestazioni(intestazioni))
            except httpx.ConnectError as errore:
                if tentativo + 1 < tentativi:
                    continue   # la richiesta non è mai partita: ripeterla è sicuro anche per un comando
                raise ErroreTool(f"Dispositivo non raggiungibile: {errore}", codice="DEVICE_UNREACHABLE") from errore
            except httpx.TimeoutException as errore:
                if ripeti_sempre and tentativo + 1 < tentativi:
                    continue
                raise ErroreTool(f"Nessuna risposta dal dispositivo entro {self.timeout:g} s.", codice="TIMEOUT") from errore
            except httpx.TransportError as errore:
                if ripeti_sempre and tentativo + 1 < tentativi:
                    continue
                raise ErroreTool(f"Errore di rete verso il dispositivo: {errore}", codice="DEVICE_UNREACHABLE") from errore
        raise AssertionError("irraggiungibile")  # pragma: no cover

    def _estrai_valore(self, risposta: httpx.Response) -> Any:
        testo = risposta.text.strip()
        try:
            dati = json.loads(testo)
        except ValueError:
            return testo
        if isinstance(dati, dict):
            if self.campo_valore not in dati:
                raise ErroreTool(f"La risposta del dispositivo non contiene il campo '{self.campo_valore}'.", codice="BAD_RESPONSE")
            return dati[self.campo_valore]
        return dati

    # ------------------------------------------------------------------ interfaccia Reflex

    async def get_tool_value(self) -> Any:
        self._verifica_interruttore()
        try:
            risposta = await self._richiesta("GET", self.url, ripeti_sempre=True)
        except BaseException as errore:
            self._dopo_errore(errore)
            raise
        if risposta.status_code >= 500 or risposta.status_code in (408, 429):
            self.interruttore.fallimento()
            self._segna(False)
            raise ErroreTool(f"Il dispositivo ha risposto HTTP {risposta.status_code}.", codice="DEVICE_ERROR")
        if risposta.status_code >= 400:
            self.interruttore.successo()   # il gateway risponde: l'errore è della richiesta (dispositivo ignoto, permessi)
            self._segna(True)
            raise ErroreTool(f"Lettura rifiutata dal gateway (HTTP {risposta.status_code}).", codice="READ_REJECTED")
        self.interruttore.successo()
        self._segna(True)
        return self._estrai_valore(risposta)

    async def set_tool_value(self, value: Any) -> bool:
        self._verifica_interruttore()
        comando = str(uuid.uuid4())
        try:
            risposta = await self._richiesta(
                "POST", self.url_comando, json_corpo={"value": value, "command_id": comando},
                intestazioni={"Idempotency-Key": comando}, ripeti_se_non_partita=True)
        except BaseException as errore:
            self._dopo_errore(errore)
            raise
        if risposta.status_code >= 500 or risposta.status_code in (408, 429):
            self.interruttore.fallimento()
            self._segna(False)
            raise ErroreTool(f"Il dispositivo ha risposto HTTP {risposta.status_code} al comando.", codice="DEVICE_ERROR")
        self.interruttore.successo()
        self._segna(True)
        if risposta.status_code >= 400:
            raise ErroreTool(f"Il dispositivo ha rifiutato il comando (HTTP {risposta.status_code}).", codice="COMMAND_REJECTED")
        logger.info("[Tool: %s] Comando '%s' accettato dal dispositivo (command_id %s).", self.target_device, value, comando)
        return True

    async def aggiorna_metriche(self) -> None:
        from app.core import metriche
        metriche.imposta("langbrain_device_circuit_state", VALORE_NUMERICO[self.interruttore.stato], device=self.target_device)


def crea_adattatore(nome_adattatore: str, dispositivo: str, opzioni: dict[str, Any], unita: str = "") -> Reflex:
    """L'effettore dell'adattatore indicato in configurazione (`ADATTATORI_NOTI` in `app/core/configurazione.py`)."""
    if nome_adattatore == "http":
        return EffettoreHttp(dispositivo, opzioni, unita)
    raise ValueError(f"Adattatore sconosciuto: '{nome_adattatore}'.")

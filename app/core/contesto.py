"""
Contesto di una richiesta: correlation id e attore.

Un `ContextVar` porta, lungo tutta l'esecuzione di una richiesta (anche attraverso `await` e task creati da essa), chi l'ha
chiesta e con quale identificatore di correlazione. Chi scrive nel registro delle decisioni (`NervousSystem.log_event`) e i
log JSON li leggono da qui, senza che ogni funzione debba passarseli.

  - `X-Correlation-Id`: se arriva, si usa così com'è (una stringa stampabile, al massimo 128 caratteri); altrimenti si
    genera un UUID4. Si restituisce nell'intestazione di risposta.
  - `X-Actor`: email -> `user:<email>`, `system:<nome>` -> `system`; gli attori interni restano `rule:<id>`, `organ:<nome>`,
    `brain`, `assistant`. Un valore che non corrisponde a nulla di questo viene ignorato (nessun attore nel contesto).
  - `traceparent` (W3C Trace Context): se arriva valido ne si mantiene il `trace-id` e se ne apre un nuovo span; altrimenti
    la traccia nasce qui. Senza `X-Correlation-Id` il correlation id è il `trace-id` ricevuto, così il registro delle decisioni e
    i log si collegano alla traccia di chi ha chiamato. La risposta riporta `traceparent` e le chiamate in uscita
    (`traceparent_uscente`, usata dagli adattatori dei dispositivi) lo propagano: nessun esportatore OpenTelemetry, ma ogni
    sistema a monte e a valle che lo capisce ricostruisce la catena.
  - I fatti interni (riflessi, timer, azioni programmate) non nascono da una richiesta: `fatto_interno()` apre un contesto
    con un correlation id proprio.

`MiddlewareContesto` è un middleware ASGI puro (non `BaseHTTPMiddleware`): non rompe i flussi SSE e il contesto arriva
intatto agli endpoint.
"""

import contextlib
import re
import secrets
import uuid
from contextvars import ContextVar
from typing import Iterator

_correlation_id: ContextVar[str | None] = ContextVar("langbrain_correlation_id", default=None)
_attore: ContextVar[str | None] = ContextVar("langbrain_attore", default=None)
_traccia: ContextVar[tuple[str, str] | None] = ContextVar("langbrain_traccia", default=None)  # (trace-id, span-id)

INTESTAZIONE_CORRELATION = "X-Correlation-Id"
INTESTAZIONE_ATTORE = "X-Actor"
INTESTAZIONE_TRACEPARENT = "traceparent"
ATTORE_SISTEMA = "system"

_LUNGHEZZA_MASSIMA_ID = 128
_EMAIL = re.compile(r"^[^@\s:]+@[^@\s]+\.[^@\s]+$")
_ATTORI_FISSI = {"brain", "assistant", "system"}
_PREFISSI_ATTORE = ("rule:", "organ:", "user:")
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def nuovo_correlation_id() -> str:
    return str(uuid.uuid4())


def correlation_id_corrente() -> str | None:
    return _correlation_id.get()


def attore_corrente() -> str | None:
    return _attore.get()


def trace_id_corrente() -> str | None:
    traccia = _traccia.get()
    return traccia[0] if traccia else None


def nuovo_trace_id() -> str:
    return secrets.token_hex(16)


def nuovo_span_id() -> str:
    return secrets.token_hex(8)


def analizza_traceparent(valore: str | None) -> str | None:
    """Il `trace-id` di un header `traceparent` valido (versione 00, nessun id nullo), altrimenti None."""
    trovato = _TRACEPARENT.match((valore or "").strip().lower())
    if not trovato or set(trovato.group(1)) == {"0"} or set(trovato.group(2)) == {"0"}:
        return None
    return trovato.group(1)


def traceparent_uscente() -> str | None:
    """L'header `traceparent` per una chiamata in uscita (nuovo span nella traccia corrente); None fuori da una traccia."""
    trace_id = trace_id_corrente()
    return f"00-{trace_id}-{nuovo_span_id()}-01" if trace_id else None


def pulisci_correlation_id(valore: str | None) -> str | None:
    """Il correlation id ricevuto se è una stringa stampabile e non troppo lunga, altrimenti None."""
    if valore is None:
        return None
    valore = valore.strip()
    if not valore or len(valore) > _LUNGHEZZA_MASSIMA_ID or not valore.isprintable():
        return None
    return valore


def mappa_attore(valore: str | None) -> str | None:
    """L'attore nella forma del registro delle decisioni, oppure None se il valore non è riconoscibile."""
    if valore is None:
        return None
    valore = valore.strip()
    if not valore:
        return None
    minuscolo = valore.lower()
    if minuscolo.startswith("system:") or minuscolo == "system":
        return ATTORE_SISTEMA
    if minuscolo in _ATTORI_FISSI:
        return minuscolo
    if minuscolo.startswith(_PREFISSI_ATTORE):
        prefisso, _, resto = valore.partition(":")
        return f"{prefisso.lower()}:{resto}" if resto else None
    if _EMAIL.match(valore):
        return f"user:{valore}"
    return None


@contextlib.contextmanager
def contesto(correlation_id: str | None = None, attore: str | None = None, traccia: tuple[str, str] | None = None) -> Iterator[None]:
    """Imposta correlation id, attore (già mappato) e traccia per il blocco; al termine ripristina i valori precedenti."""
    t_cid = _correlation_id.set(correlation_id if correlation_id is not None else _correlation_id.get())
    t_att = _attore.set(attore if attore is not None else _attore.get())
    t_tr = _traccia.set(traccia if traccia is not None else _traccia.get())
    try:
        yield
    finally:
        _correlation_id.reset(t_cid)
        _attore.reset(t_att)
        _traccia.reset(t_tr)


@contextlib.contextmanager
def fatto_interno(attore: str = ATTORE_SISTEMA) -> Iterator[str]:
    """Contesto di un fatto che non nasce da una richiesta: ha un correlation id proprio. Restituisce l'id."""
    cid = nuovo_correlation_id()
    t_cid = _correlation_id.set(cid)
    t_att = _attore.set(attore)
    t_tr = _traccia.set((nuovo_trace_id(), nuovo_span_id()))
    try:
        yield cid
    finally:
        _correlation_id.reset(t_cid)
        _attore.reset(t_att)
        _traccia.reset(t_tr)


class MiddlewareContesto:
    """Middleware ASGI: legge `X-Correlation-Id` e `X-Actor`, imposta il contesto e riporta l'id nella risposta."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        intestazioni = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        trace_ricevuto = analizza_traceparent(intestazioni.get(INTESTAZIONE_TRACEPARENT))
        cid = pulisci_correlation_id(intestazioni.get(INTESTAZIONE_CORRELATION.lower())) or trace_ricevuto or nuovo_correlation_id()
        attore = mappa_attore(intestazioni.get(INTESTAZIONE_ATTORE.lower()))
        traccia = (trace_ricevuto or nuovo_trace_id(), nuovo_span_id())

        async def invia(messaggio):
            if messaggio["type"] == "http.response.start":
                presenti = [(k, v) for k, v in messaggio.get("headers", []) if k.lower() not in (b"x-correlation-id", b"traceparent")]
                presenti.append((b"x-correlation-id", cid.encode("latin-1")))
                presenti.append((b"traceparent", f"00-{traccia[0]}-{traccia[1]}-01".encode("latin-1")))
                messaggio = {**messaggio, "headers": presenti}
            await send(messaggio)

        with contesto(cid, attore, traccia):
            await self.app(scope, receive, invia)

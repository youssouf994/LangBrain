"""
Contesto di una richiesta: correlation id e attore.

Un `ContextVar` porta, lungo tutta l'esecuzione di una richiesta (anche attraverso `await` e task creati da essa), chi l'ha
chiesta e con quale identificatore di correlazione. Chi scrive nel registro delle decisioni (`NervousSystem.log_event`) e i
log JSON li leggono da qui, senza che ogni funzione debba passarseli.

  - `X-Correlation-Id`: se arriva, si usa così com'è (una stringa stampabile, al massimo 128 caratteri); altrimenti si
    genera un UUID4. Si restituisce nell'intestazione di risposta.
  - `X-Actor`: email -> `user:<email>`, `system:<nome>` -> `system`; gli attori interni restano `rule:<id>`, `organ:<nome>`,
    `brain`, `assistant`. Un valore che non corrisponde a nulla di questo viene ignorato (nessun attore nel contesto).
  - I fatti interni (riflessi, timer, azioni programmate) non nascono da una richiesta: `fatto_interno()` apre un contesto
    con un correlation id proprio.

`MiddlewareContesto` è un middleware ASGI puro (non `BaseHTTPMiddleware`): non rompe i flussi SSE e il contesto arriva
intatto agli endpoint.
"""

import contextlib
import re
import uuid
from contextvars import ContextVar
from typing import Iterator

_correlation_id: ContextVar[str | None] = ContextVar("langbrain_correlation_id", default=None)
_attore: ContextVar[str | None] = ContextVar("langbrain_attore", default=None)

INTESTAZIONE_CORRELATION = "X-Correlation-Id"
INTESTAZIONE_ATTORE = "X-Actor"
ATTORE_SISTEMA = "system"

_LUNGHEZZA_MASSIMA_ID = 128
_EMAIL = re.compile(r"^[^@\s:]+@[^@\s]+\.[^@\s]+$")
_ATTORI_FISSI = {"brain", "assistant", "system"}
_PREFISSI_ATTORE = ("rule:", "organ:", "user:")


def nuovo_correlation_id() -> str:
    return str(uuid.uuid4())


def correlation_id_corrente() -> str | None:
    return _correlation_id.get()


def attore_corrente() -> str | None:
    return _attore.get()


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
def contesto(correlation_id: str | None = None, attore: str | None = None) -> Iterator[None]:
    """Imposta correlation id e attore (già mappato) per il blocco; al termine ripristina i valori precedenti."""
    t_cid = _correlation_id.set(correlation_id if correlation_id is not None else _correlation_id.get())
    t_att = _attore.set(attore if attore is not None else _attore.get())
    try:
        yield
    finally:
        _correlation_id.reset(t_cid)
        _attore.reset(t_att)


@contextlib.contextmanager
def fatto_interno(attore: str = ATTORE_SISTEMA) -> Iterator[str]:
    """Contesto di un fatto che non nasce da una richiesta: ha un correlation id proprio. Restituisce l'id."""
    cid = nuovo_correlation_id()
    t_cid = _correlation_id.set(cid)
    t_att = _attore.set(attore)
    try:
        yield cid
    finally:
        _correlation_id.reset(t_cid)
        _attore.reset(t_att)


class MiddlewareContesto:
    """Middleware ASGI: legge `X-Correlation-Id` e `X-Actor`, imposta il contesto e riporta l'id nella risposta."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        intestazioni = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        cid = pulisci_correlation_id(intestazioni.get(INTESTAZIONE_CORRELATION.lower())) or nuovo_correlation_id()
        attore = mappa_attore(intestazioni.get(INTESTAZIONE_ATTORE.lower()))

        async def invia(messaggio):
            if messaggio["type"] == "http.response.start":
                presenti = [(k, v) for k, v in messaggio.get("headers", []) if k.lower() != b"x-correlation-id"]
                presenti.append((b"x-correlation-id", cid.encode("latin-1")))
                messaggio = {**messaggio, "headers": presenti}
            await send(messaggio)

        with contesto(cid, attore):
            await self.app(scope, receive, invia)

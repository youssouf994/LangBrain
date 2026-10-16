"""
Registro delle decisioni nel formato `Decision` del contratto 1.0.4 (sorgente: il sistema nervoso, tabella `events`).

  - `come_decisione(riga)`: una riga di `events` come `Decision` (`decision_id` stringa, `ts` ISO 8601 UTC al millisecondo,
    `actor` nella forma del contratto, `target {type, device_id}`, `old_value`/`new_value` nulli se mancano).
  - `cerca_decisioni(...)`: `GET /decisions` — dalla più recente, filtri `from`, `to`, `device_id`, `actor`,
    `correlation_id`, `limit` (1-200) e cursore opaco.
  - `attore_per_contratto(...)`: gli attori scritti dagli agenti (`Brain`, `organ_security`...) nella forma
    `brain` / `organ:<nome>`; `user:`, `rule:`, `assistant`, `system` restano; ciò che non si riconosce è `system`.

Le righe scritte prima dell'introduzione del `correlation_id` lo hanno vuoto: nel contratto è obbligatorio, quindi escono
con l'UUID nullo.
"""

import base64
import binascii
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.core import archivio, contesto
from app.core.priorita import priorita_attore
from app.db import schema
from app.tools.nervous_system import esito_da_azione

UUID_NULLO = "00000000-0000-0000-0000-000000000000"
TIPI_TARGET_NON_DISPOSITIVO = ("apartment", "rules", "schedule")
LIMITE_MASSIMO = 200
LIMITE_PREDEFINITO = 50


def attore_per_contratto(attore: str | None) -> str:
    """L'attore come lo vuole il contratto: `rule:<id>`, `organ:<nome>`, `brain`, `user:<email>`, `assistant`, `system`."""
    if not attore or not attore.strip():
        return contesto.ATTORE_SISTEMA
    noto = contesto.mappa_attore(attore)
    if noto:
        return noto
    pulito = attore.strip()
    if pulito.casefold() == "brain":
        return "brain"
    if priorita_attore(pulito) is not None or pulito.startswith(("organ_", "component_", "agent_")):
        return f"organ:{pulito}"
    return contesto.ATTORE_SISTEMA


def target_per_contratto(target: str | None) -> dict[str, str]:
    """`{type: device, device_id}`, oppure `{type: apartment|rules|schedule}` per i target che non sono dispositivi."""
    if target in TIPI_TARGET_NON_DISPOSITIVO:
        return {"type": target}
    return {"type": "device", "device_id": target or ""}


def _data(valore: Any) -> datetime | None:
    """Una data ISO 8601 (con o senza fuso) come datetime UTC senza fuso, oppure None se non è valida."""
    if valore is None:
        return None
    if isinstance(valore, datetime):
        parsed = valore
    else:
        try:
            parsed = datetime.fromisoformat(str(valore).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def come_decisione(riga: dict[str, Any]) -> dict[str, Any]:
    """Una riga di `events` (come la restituisce `NervousSystem`) nel formato `Decision`."""
    vecchio, nuovo = riga.get("old_value"), riga.get("new_value")
    return {
        "decision_id": str(riga["event_id"]),
        "ts": riga.get("ts") or schema.iso_ms(riga.get("timestamp")),
        "actor": attore_per_contratto(riga.get("actor")),
        "target": target_per_contratto(riga.get("target")),
        "action": riga.get("action", ""),
        "old_value": None if vecchio is None else str(vecchio),
        "new_value": None if nuovo is None else str(nuovo),
        "reasoning": riga.get("reasoning") or "",
        "outcome": riga.get("outcome") or esito_da_azione(str(riga.get("action", ""))),
        "command_id": riga.get("command_id"),
        "approval_id": riga.get("approval_id"),
        "correlation_id": riga.get("correlation_id") or UUID_NULLO,
    }


def codifica_cursore(ultimo_event_id: int) -> str:
    return base64.urlsafe_b64encode(str(ultimo_event_id).encode()).decode().rstrip("=")


def decodifica_cursore(cursore: str | None) -> int | None:
    """L'`event_id` da cui continuare, oppure None se non c'è un cursore. Un cursore malformato solleva `ValueError`."""
    if not cursore:
        return None
    try:
        return int(base64.urlsafe_b64decode(cursore + "=" * (-len(cursore) % 4)).decode())
    except (binascii.Error, UnicodeDecodeError, ValueError) as errore:
        raise ValueError("Cursore non valido.") from errore


async def cerca_decisioni(
    da: str | datetime | None = None,
    a: str | datetime | None = None,
    device_id: str | None = None,
    actor: str | None = None,
    correlation_id: str | None = None,
    limite: int = LIMITE_PREDEFINITO,
    cursore: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """
    Le decisioni dalla più recente, `{"items": [...], "next_cursor": str | None}`. `limite` è portato tra 1 e 200.
    Date non valide o cursore malformato sollevano `ValueError` (l'adattatore le traduce in 400).
    """
    limite = max(1, min(int(limite), LIMITE_MASSIMO))
    inizio, fine = _data(da), _data(a)
    if da is not None and inizio is None:
        raise ValueError("Il filtro 'from' non è una data valida.")
    if a is not None and fine is None:
        raise ValueError("Il filtro 'to' non è una data valida.")
    ultimo = decodifica_cursore(cursore)

    ev = schema.events
    base = select(ev).order_by(ev.c.event_id.desc())
    if inizio is not None:
        base = base.where(ev.c.timestamp >= inizio)
    if fine is not None:
        base = base.where(ev.c.timestamp <= fine)
    if device_id:
        base = base.where(ev.c.target == device_id)
    if correlation_id:
        base = base.where(ev.c.correlation_id == correlation_id)

    # Il filtro sull'attore vale sulla forma del contratto, che non si può scrivere in SQL (`Brain` -> `brain`, nome agente ->
    # `organ:<nome>`): si leggono pagine di righe finché ne servono `limite + 1`.
    trovate: list[dict[str, Any]] = []
    pagina = max(limite * 3, 100)
    posizione = ultimo
    while len(trovate) <= limite:
        interrogazione = base.limit(pagina)
        if posizione is not None:
            interrogazione = interrogazione.where(ev.c.event_id < posizione)
        async with archivio.motore(db_path).connect() as db:
            righe = [dict(r._mapping) for r in (await db.execute(interrogazione)).all()]
        if not righe:
            break
        for riga in righe:
            riga["ts"] = schema.iso_ms(riga["timestamp"])
            if actor and attore_per_contratto(riga["actor"]) != actor:
                continue
            trovate.append(riga)
        posizione = righe[-1]["event_id"]
        if len(righe) < pagina:
            break

    pagina_finale = trovate[:limite]
    prossimo = codifica_cursore(pagina_finale[-1]["event_id"]) if len(trovate) > limite else None
    return {"items": [come_decisione(r) for r in pagina_finale], "next_cursor": prossimo}

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, insert, literal, select, update

from app.core import archivio, contesto
from app.db import schema
from app.db.database import DB_PATH

logger = logging.getLogger(__name__)

events = schema.events
tab_segnali = schema.signals
node_runs = schema.node_runs


def _taglia_utc(**delta) -> datetime:
    """Istante di `adesso - delta` in UTC senza fuso, confrontabile con la colonna `timestamp`."""
    return (datetime.now(timezone.utc) - timedelta(**delta)).replace(tzinfo=None, microsecond=0)


def _riga(riga) -> dict:
    """Una riga come dizionario, con il timestamp nel formato storico `YYYY-MM-DD HH:MM:SS`."""
    dati = dict(riga._mapping)
    if "timestamp" in dati:
        dati["ts"] = schema.iso_ms(dati["timestamp"])  # ISO 8601 UTC al millisecondo
        dati["timestamp"] = schema.come_testo(dati["timestamp"])
    return dati


def _escalation_non_chiusa():
    """Condizione: l'azione non è già stata risolta né riconciliata (il `\\_` è un carattere `_` letterale)."""
    return events.c.action.not_like("RESOLVED\\_%", escape="\\") & events.c.action.not_like("RECONCILED\\_%", escape="\\")


def esito_da_azione(azione: str) -> str:
    """L'`outcome` più probabile di un evento in base al nome dell'azione, quando chi scrive non lo indica."""
    if azione.startswith("REJECTED"):
        return "rejected"
    if azione.startswith(("TOOL_ERROR", "TOOL_FAILURE", "FAILED")):
        return "failed"
    if azione.startswith(("RESOLVED_", "RECONCILED_", "EXPIRED_", "UNBLOCKED", "ESCALATION")):
        return "informational"
    return "applied"


class NervousSystem:
    """
    Gestisce la lettura e la registrazione degli eventi sul database con storico transizioni old_value -> new_value.
    """
    def __init__(self, target: list[str] | None = None, frequency: int = 240, db_path: str = DB_PATH) -> None:
        self.target = target if target is not None else ["all"]
        self.frequency = frequency
        self.db_path = db_path

    def _motore(self):
        return archivio.motore(self.db_path)

    async def get_recent_events(self) -> list[dict]:
        """Recupera gli eventi registrati nella finestra temporale impostata."""
        cutoff_time = _taglia_utc(minutes=self.frequency)

        try:
            interrogazione = select(events).where(events.c.timestamp >= cutoff_time)
            if "all" not in self.target:
                interrogazione = interrogazione.where(events.c.target.in_(self.target))
            async with self._motore().connect() as db:
                righe = (await db.execute(interrogazione.order_by(events.c.timestamp.desc(), events.c.event_id.desc()))).all()
            return [_riga(r) for r in righe]

        except Exception as e:
            logger.error(f"Errore durante il recupero degli eventi recenti: {e}")
            raise

    async def log_event(
        self,
        actor: str | None,
        action: str,
        target: str,
        old_value: str,
        new_value: str,
        reasoning: str,
        escalated: bool = False,
        *,
        correlation_id: str | None = None,
        outcome: str | None = None,
        command_id: str | None = None,
        approval_id: str | None = None,
    ) -> int:
        """
        Registra una decisione/evento con old_value e new_value nel DB e restituisce il suo `event_id` (il `decision_id`).
        `actor` mancante: l'attore della richiesta (`X-Actor`) oppure `system`. `correlation_id` mancante: quello della
        richiesta, oppure uno nuovo (un fatto interno). `outcome` mancante: dedotto dall'azione (`esito_da_azione`).
        """
        esito = outcome or esito_da_azione(action)
        if esito not in schema.ESITI_DECISIONE:
            raise ValueError(f"Esito '{esito}' non valido: usa uno tra {list(schema.ESITI_DECISIONE)}.")
        attore = actor or contesto.attore_corrente() or contesto.ATTORE_SISTEMA
        correlazione = correlation_id or contesto.correlation_id_corrente() or contesto.nuovo_correlation_id()
        try:
            async with self._motore().begin() as db:
                risultato = await db.execute(insert(events).values(
                    actor=attore, action=action, target=target, old_value=str(old_value), new_value=str(new_value),
                    reasoning=reasoning, escalated=int(bool(escalated)), correlation_id=correlazione, outcome=esito,
                    command_id=command_id, approval_id=approval_id))
                evento_id = risultato.inserted_primary_key[0]
            logger.info(f"[NervousSystem] DB Audit -> {attore} su {target}: {old_value} -> {new_value} ({action})")
            return evento_id
        except Exception as e:
            logger.error(f"Errore durante la registrazione dell'evento su {target}: {e}")
            raise

    async def record_signals(self, signals: list[dict]) -> int:
        """
        Salva nella tabella `signals` i segnali numerici (`receptor_id`/`sensor_id`, `agent_owner`, `value`, `unit`);
        quelli non numerici (es. "ON") restano solo nello stato del grafo. Restituisce quanti ne ha scritti.
        Un errore del database viene registrato e rilanciato: chi chiama decide se fermarsi.
        """
        righe = []
        for s in signals:
            try:
                valore = float(s["value"])
            except (KeyError, TypeError, ValueError):
                continue
            righe.append({"receptor_id": str(s.get("receptor_id") or s.get("sensor_id")), "agent_owner": str(s.get("agent_owner", "")),
                          "value": valore, "unit": str(s.get("unit", ""))})
        if not righe:
            return 0
        try:
            async with self._motore().begin() as db:
                await db.execute(insert(tab_segnali), righe)
            return len(righe)
        except Exception as e:
            logger.error(f"Errore durante la registrazione dei segnali: {e}")
            raise

    async def get_recent_signals(self, hours: float = 4.0, receptor_id: str | None = None) -> list[dict]:
        """Segnali registrati nelle ultime `hours` ore (più recenti per primi), opzionalmente di un solo recettore."""
        interrogazione = select(tab_segnali).where(tab_segnali.c.timestamp >= _taglia_utc(hours=hours))
        if receptor_id:
            interrogazione = interrogazione.where(tab_segnali.c.receptor_id == receptor_id)
        try:
            async with self._motore().connect() as db:
                righe = (await db.execute(interrogazione.order_by(tab_segnali.c.timestamp.desc(), tab_segnali.c.signal_id.desc()))).all()
            return [_riga(r) for r in righe]
        except Exception as e:
            logger.error(f"Errore durante il recupero dei segnali: {e}")
            raise

    async def record_node_run(self, run_id: str, node: str, started_at: str, duration_ms: float, outcome: str,
                              detail: str = "", thread_id: str = "") -> None:
        """
        Traccia l'esecuzione di un nodo del grafo (`outcome`: ok, waiting, error, ...). Serve alle statistiche
        (`GET /graph/stats`): un errore qui viene registrato e NON rilanciato, la traccia non deve fermare il grafo.
        """
        try:
            async with self._motore().begin() as db:
                await db.execute(insert(node_runs).values(
                    run_id=run_id, thread_id=thread_id, node=node, started_at=started_at,
                    duration_ms=round(duration_ms, 2), outcome=outcome[:100], detail=detail[:500]))
        except Exception as e:
            logger.warning(f"Traccia dell'esecuzione del nodo '{node}' non registrata: {e}")

    async def get_node_runs(self, hours: float = 24.0, run_id: str | None = None) -> list[dict]:
        """Le esecuzioni dei nodi delle run avviate nelle ultime `hours` ore (o di una sola run), in ordine di esecuzione."""
        try:
            if run_id:
                interrogazione = select(node_runs).where(node_runs.c.run_id == run_id)
            else:
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
                recenti = (select(node_runs.c.run_id).group_by(node_runs.c.run_id)
                           .having(func.min(node_runs.c.started_at) >= cutoff))
                interrogazione = select(node_runs).where(node_runs.c.run_id.in_(recenti))
            async with self._motore().connect() as db:
                righe = (await db.execute(interrogazione.order_by(node_runs.c.run_no))).all()
            return [dict(r._mapping) for r in righe]
        except Exception as e:
            logger.error(f"Errore durante il recupero delle esecuzioni dei nodi: {e}")
            raise

    async def mark_resolved(self, target: str) -> int:
        """
        Marca come risolte tutte le escalation ancora aperte del target (qualunque nome abbia l'azione: un agente
        personalizzato può registrarla come vuole, es. CRITICAL_ARHYTHMIA_ESCALATION). Senza questo il Brain
        le rivedrebbe come "non riconciliate" a ogni ciclo. L'UPDATE imposta escalated = 0 e antepone RESOLVED_
        all'azione, così i controlli di conflitto le ignorano. Restituisce quante escalation ha chiuso.
        Un errore del database viene registrato e rilanciato (come in `log_event`): chi chiama decide se fermarsi.
        """
        try:
            async with self._motore().begin() as db:
                result = await db.execute(
                    update(events)
                    .where(events.c.target == target, events.c.escalated == 1, _escalation_non_chiusa())
                    .values(action=literal("RESOLVED_") + events.c.action, escalated=0))
                chiuse = max(result.rowcount, 0)
            if chiuse > 0:
                logger.info(f"[NervousSystem] Marcate {chiuse} escalation come RESOLVED per '{target}'.")
            return chiuse
        except Exception as e:
            logger.error(f"Errore durante il reset degli eventi di escalation per {target}: {e}")
            raise

    async def unblock_target(self, target: str, reasoning: str, actor: str = "event_producer") -> None:
        """
        Event-Driven Unblock — Sblocca un dispositivo precedentemente in stato di blocco (es. REJECTED).
        Registra l'evento UNBLOCKED nel DB per azzerare lo stato di blocco. Un errore del database viene registrato e
        rilanciato: chi chiama non deve sbloccare il dispositivo se lo sblocco non è stato registrato.
        """
        try:
            async with self._motore().begin() as db:
                await db.execute(insert(events).values(
                    actor=actor, action="UNBLOCKED", target=target, old_value="REJECTED", new_value="OFF",
                    reasoning=reasoning, escalated=0, outcome="informational",
                    correlation_id=contesto.correlation_id_corrente() or contesto.nuovo_correlation_id()))
                # Invalida anche vecchi eventi di blocco pendenti
                await db.execute(
                    update(events)
                    .where(events.c.target == target, events.c.action.like("REJECTED%") | events.c.action.like("FORCE_SHUTDOWN%"))
                    .values(action=literal("RESOLVED_") + events.c.action, escalated=0))
            logger.info(f"[NervousSystem] Target '{target}' sbloccato con successo: {reasoning}")
        except Exception as e:
            logger.error(f"Errore durante lo sblocco del target {target}: {e}")
            raise

    async def expire_old_control_flags(self, ttl_minutes: int = 60) -> int:
        """
        TTL (Time-To-Live) — Marca come 'EXPIRED' tutti gli eventi di blocco/conflitto antecedenti a ttl_minutes.
        Ritorna il numero di eventi scaduti. Un errore del database viene registrato e rilanciato.
        """
        cutoff_time = _taglia_utc(minutes=ttl_minutes)
        try:
            async with self._motore().begin() as db:
                result = await db.execute(
                    update(events)
                    .where(events.c.timestamp < cutoff_time,
                           (events.c.escalated == 1) | events.c.action.like("REJECTED%") | events.c.action.like("FORCE_SHUTDOWN%"),
                           events.c.action.not_like("EXPIRED\\_%", escape="\\"),
                           events.c.action.not_like("RESOLVED\\_%", escape="\\"))
                    .values(action=literal("EXPIRED_") + events.c.action, escalated=0))
                expired_count = result.rowcount
            if expired_count > 0:
                logger.info(f"[NervousSystem] TTL: Scaduti {expired_count} vecchi flag di controllo (> {ttl_minutes} min).")
            return expired_count
        except Exception as e:
            logger.error(f"Errore durante l'applicazione del TTL sui flag: {e}")
            raise




# Alias del vecchio nome (retrocompatibilità)
EventLog = NervousSystem

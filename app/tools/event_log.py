import logging
import aiosqlite
from datetime import datetime, timedelta, timezone
from app.db.database import DB_PATH

logger = logging.getLogger(__name__)

class EventLog:
    """
    Gestisce la lettura e la registrazione degli eventi sul database con storico transizioni old_value -> new_value.
    """
    def __init__(self, target: list[str] | None = None, frequency: int = 240, db_path: str = DB_PATH) -> None:
        self.target = target if target is not None else ["all"]
        self.frequency = frequency
        self.db_path = db_path

    async def get_recent_events(self) -> list[dict]:
        """Recupera gli eventi registrati nella finestra temporale impostata."""
        cutoff_time = (datetime.now(timezone.utc) - timedelta(minutes=self.frequency)).strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                if "all" in self.target:
                    query = "SELECT * FROM events WHERE timestamp >= ? ORDER BY timestamp DESC"
                    params = (cutoff_time,)
                else:
                    placeholders = ",".join("?" for _ in self.target)
                    query = f"SELECT * FROM events WHERE target IN ({placeholders}) AND timestamp >= ? ORDER BY timestamp DESC"
                    params = (*self.target, cutoff_time)
                
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
                    
        except Exception as e:
            logger.error(f"Errore durante il recupero degli eventi recenti: {e}")
            raise

    async def log_event(
        self, 
        actor: str, 
        action: str, 
        target: str, 
        old_value: str,
        new_value: str, 
        reasoning: str, 
        escalated: bool = False
    ) -> None:
        """Registra un nuovo evento con old_value e new_value nel DB."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO events (actor, action, target, old_value, new_value, reasoning, escalated) 
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (actor, action, target, str(old_value), str(new_value), reasoning, escalated)
                )
                await db.commit()
                logger.info(f"[EventLog] DB Audit -> {actor} su {target}: {old_value} -> {new_value} ({action})")
        except Exception as e:
            logger.error(f"Errore durante la registrazione dell'evento su {target}: {e}")
            raise

    async def mark_resolved(self, target: str) -> None:
        """
        Fix 1 — Marca come risolti tutti gli eventi ESCALATION_PROPOSED del target nella finestra corrente.
        Questo impedisce che i cicli successivi vedano ancora il conflitto come 'attivo' nel DB.
        L'UPDATE setta escalated = 0 e aggiunge il prefisso RESOLVED_ all'action per escluderli dai
        controlli di conflitto (check_for_recent_conflict usa actor != self.name, ma la logica in
        agent_climate ora esclude anche azioni RECONCILED_* e RESOLVED_*).
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                result = await db.execute(
                    """UPDATE events
                       SET action = 'RESOLVED_' || action, escalated = 0
                       WHERE target = ? AND action = 'ESCALATION_PROPOSED' AND escalated = 1""",
                    (target,)
                )
                await db.commit()
                if result.rowcount > 0:
                    logger.info(f"[EventLog] Marcati {result.rowcount} eventi ESCALATION_PROPOSED come RESOLVED per '{target}'.")
        except Exception as e:
            logger.error(f"Errore durante il reset degli eventi di escalation per {target}: {e}")

    async def unblock_target(self, target: str, reasoning: str, actor: str = "event_producer") -> None:
        """
        Event-Driven Unblock — Sblocca un dispositivo precedentemente in stato di blocco (es. REJECTED).
        Registra l'evento UNBLOCKED nel DB per azzerare lo stato di blocco.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO events (actor, action, target, old_value, new_value, reasoning, escalated)
                       VALUES (?, 'UNBLOCKED', ?, 'REJECTED', 'OFF', ?, 0)""",
                    (actor, target, reasoning)
                )
                # Invalida anche vecchi eventi di blocco pendenti
                await db.execute(
                    """UPDATE events
                       SET action = 'RESOLVED_' || action, escalated = 0
                       WHERE target = ? AND (action LIKE 'REJECTED%' OR action LIKE 'FORCE_SHUTDOWN%')""",
                    (target,)
                )
                await db.commit()
                logger.info(f"[EventLog] Target '{target}' sbloccato con successo: {reasoning}")
        except Exception as e:
            logger.error(f"Errore durante lo sblocco del target {target}: {e}")

    async def expire_old_control_flags(self, ttl_minutes: int = 60) -> int:
        """
        TTL (Time-To-Live) — Marca come 'EXPIRED' tutti gli eventi di blocco/conflitto antecedenti a ttl_minutes.
        Ritorna il numero di eventi scaduti.
        """
        cutoff_time = (datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            async with aiosqlite.connect(self.db_path) as db:
                result = await db.execute(
                    """UPDATE events
                       SET action = 'EXPIRED_' || action, escalated = 0
                       WHERE timestamp < ? AND (escalated = 1 OR action LIKE 'REJECTED%' OR action LIKE 'FORCE_SHUTDOWN%')
                       AND action NOT LIKE 'EXPIRED_%' AND action NOT LIKE 'RESOLVED_%'""",
                    (cutoff_time,)
                )
                await db.commit()
                expired_count = result.rowcount
                if expired_count > 0:
                    logger.info(f"[EventLog] TTL: Scaduti {expired_count} vecchi flag di controllo (> {ttl_minutes} min).")
                return expired_count
        except Exception as e:
            logger.error(f"Errore durante l'applicazione del TTL sui flag: {e}")
            return 0



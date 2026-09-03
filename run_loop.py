import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.db.database import Database
from app.graph.builder import build_graph
from app.tools.sensor_tools import get_default_iot_tools
from app.core.constants import is_control_flag

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Modello di Evento Sensore
# ---------------------------------------------------------------------------

@dataclass
class SensorEvent:
    sensor_id: str
    old_value: Any
    new_value: Any
    source: str = "mock"


# ---------------------------------------------------------------------------
# Produttore di eventi (sostituisce il polling fisso)
# Simula variazioni reali dei sensori. In produzione questo modulo
# verrebbe sostituito da un consumer MQTT, WebSocket o webhook.
# ---------------------------------------------------------------------------

async def sensor_event_producer(
    event_queue: asyncio.Queue[SensorEvent],
    shared_tools: dict,
    poll_interval: float = 5.0,
    max_events: int | None = None,
) -> None:
    """
    Rileva variazioni nello stato dei tool rispetto all'ultimo valore noto
    e pubblica un SensorEvent nella coda solo quando c'è un cambiamento reale.
    In assenza di variazioni non genera traffico verso il grafo.
    """
    last_known: dict[str, Any] = {}
    emitted = 0

    # Lettura iniziale per stabilire il baseline
    for device_id, tool in shared_tools.items():
        last_known[device_id] = await tool.get_tool_value()

    logger.info("[EventProducer] Avviato. Monitoring attivo su: %s", list(shared_tools.keys()))

    while True:
        await asyncio.sleep(poll_interval)

        for device_id, tool in shared_tools.items():
            current = await tool.get_tool_value()
            previous = last_known.get(device_id)

            if current != previous:
                if is_control_flag(current):
                    # Aggiorna il baseline senza scatenare un ciclo:
                    # il flag REJECTED/RECONCILED è scritto dal Brain come stato interno,
                    # non è una variazione fisica del sensore che richiede rivalutazione.
                    last_known[device_id] = current
                    logger.info(
                        "[EventProducer] Cambio su '%s' ignorato (flag di controllo interno): %s -> %s",
                        device_id, previous, current,
                    )
                    continue

                event = SensorEvent(
                    sensor_id=device_id,
                    old_value=previous,
                    new_value=current,
                )
                await event_queue.put(event)
                last_known[device_id] = current
                logger.info(
                    "[EventProducer] Cambio rilevato su '%s': %s -> %s",
                    device_id, previous, current
                )
                emitted += 1
                if max_events and emitted >= max_events:
                    logger.info("[EventProducer] Raggiunto il limite di eventi. Arresto produttore.")
                    return


# ---------------------------------------------------------------------------
# Loop Principale Event-Driven
# ---------------------------------------------------------------------------

async def run_agent_loop(
    health_check_interval: float = 30.0,
    max_iterations: int | None = 3,
) -> None:
    """
    Loop principale event-driven.
    - Il grafo viene invocato SOLO quando arriva un SensorEvent nella coda.
    - Se la coda è vuota per più di `health_check_interval` secondi, viene invocato
      un ciclo di health-check (fallback timer) per garantire la reattività del sistema.
    """
    # 1. Inizializzazione DB
    db = Database()
    await db.init_db()
    logger.info("Database SQLite inizializzato.")

    # 2. Tool condivisi (singleton, già pronti grazie a build_graph)
    graph, shared_tools = build_graph()
    thread_config = {"configurable": {"thread_id": "smart_home_demo_session"}}

    # Stato iniziale del grafo
    initial_state = {
        "messages": [],
        "readings": [],
        "recent_events": [],
        "pending_escalations": [],
        "next_agent": "brain",
        "hitl_required": False,
        "config": {"readings_window_hours": 4},
    }

    # 3. Coda eventi
    event_queue: asyncio.Queue[SensorEvent] = asyncio.Queue()

    # 4. Avvia il produttore di eventi come task asincrono parallelo
    #    In produzione: sostituire con consumer MQTT/WebSocket
    producer_task = asyncio.create_task(
        sensor_event_producer(
            event_queue=event_queue,
            shared_tools=shared_tools,
            poll_interval=5.0,
        ),
        name="SensorEventProducer",
    )

    iteration = 0
    logger.info("=== AVVIO CICLO EVENT-DRIVEN (health-check ogni %.0fs) ===", health_check_interval)

    try:
        while True:
            # Attende un evento dalla coda; scatta il health-check se il timer scade
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=health_check_interval)
                trigger = f"Evento sensore: {event.sensor_id} ({event.old_value} -> {event.new_value})"
                # Aggiorna lo stato iniziale con il reading reale dell'evento
                initial_state["readings"] = [{
                    "sensor_id": event.sensor_id,
                    "agent_owner": "sensor_producer",
                    "value": str(event.new_value),
                    "unit": "",
                }]
                event_queue.task_done()

            except asyncio.TimeoutError:
                trigger = f"Health-check periodico (timeout {health_check_interval:.0f}s senza eventi)"

            iteration += 1
            logger.info("\n--- [CICLO #%d] %s ---", iteration, trigger)

            # TTL check ad ogni ciclo: sposta in EXPIRED_ i vecchi flag antecedenti al TTL
            try:
                from app.tools.event_log import EventLog
                from app.core.constants import DEFAULT_FLAG_TTL_MINUTES
                log_mgr = EventLog()
                expired = await log_mgr.expire_old_control_flags(ttl_minutes=DEFAULT_FLAG_TTL_MINUTES)
                if expired > 0:
                    logger.info("[TTL] %d flag di controllo scaduti e rimossi dai blocchi attivi.", expired)
            except Exception as ttl_err:
                logger.error("Errore controllo TTL: %s", ttl_err)

            try:
                result = await graph.ainvoke(initial_state, config=thread_config)

                messages = result.get("messages", [])
                if messages:
                    last_msg = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])
                    logger.info("Esito Grafo: %s", last_msg)

                ac_val = await shared_tools["ac_living_room"].get_tool_value()
                logger.info("Stato Reale del Tool 'ac_living_room': %s", ac_val)

            except Exception as e:
                logger.error("Errore durante l'esecuzione del ciclo #%d: %s", iteration, e, exc_info=True)

            if max_iterations and iteration >= max_iterations:
                logger.info("Raggiunto il numero massimo di cicli di test. Arresto.")
                break

    finally:
        producer_task.cancel()
        try:
            await producer_task
        except asyncio.CancelledError:
            pass
        logger.info("[EventProducer] Task terminato.")


if __name__ == "__main__":
    asyncio.run(run_agent_loop(health_check_interval=30.0, max_iterations=3))

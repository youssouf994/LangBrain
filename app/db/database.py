import os
import logging

from sqlalchemy import delete, insert

from app.core import archivio
from app.core.configurazione import get_configurazione
from app.db import schema

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "langbrain.db")

class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    async def init_db(self):
        # Solo per un file SQLite: con un URL (Postgres) non c'è una cartella da creare.
        dirname = "" if "://" in self.db_path else os.path.dirname(self.db_path)
        if dirname:
            try:
                os.makedirs(dirname, exist_ok=True)
            except OSError as e:
                logger.error(f"Errore durante la creazione della cartella: {e}")
                raise

        try:
            async with archivio.motore(self.db_path).begin() as db:
                # 1-5. Tabelle (eventi / audit log, segnali, esecuzioni dei nodi, stato condiviso) e indici
                await db.run_sync(archivio.crea_tabelle, schema.metadata)

                # Auto-migrazione: aggiunta colonne mancanti se lo schema è obsoleto
                await db.run_sync(archivio.aggiungi_colonne_mancanti, schema.metadata)

                # 6. Conflitto dimostrativo: solo se richiesto da configurazione.toml ([demo] conflitto_all_avvio = 1).
                # Di default il database non riceve dati di prova in automatico.
                if get_configurazione().demo_conflitto_all_avvio:
                    # Elimina vecchi eventi di test simili per evitare accumuli a ogni riavvio
                    await db.execute(delete(schema.events).where(
                        schema.events.c.actor == "agent_security", schema.events.c.target == "ac_living_room"))
                    await db.execute(insert(schema.events).values(
                        actor="agent_security", action="FORCE_SHUTDOWN", target="ac_living_room",
                        old_value="22.5°C", new_value="OFF", reasoning="Simulazione: Finestra Aperta!", escalated=0))
                    logger.info("Conflitto dimostrativo inserito su 'events' ([demo] conflitto_all_avvio = 1).")

                logger.info("Database inizializzato: %s", archivio.url_mascherato(self.db_path))

        except Exception as e:
            logger.error(f"Errore durante l'inizializzazione/migrazione del database: {e}")
            raise

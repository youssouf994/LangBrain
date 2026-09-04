import os
import pickle
import logging
from typing import Optional

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger(__name__)

# File persisted next to the project root
_PERSIST_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".checkpointer.pickle"))


class PersistentInMemorySaver(InMemorySaver):
    """In-memory saver esteso con persistenza su disco via pickle.

    Questo mantiene l'API attesa da LangGraph (`BaseCheckpointSaver`) ma
    scrive lo stato in un file per sopravvivere a ricompilazioni.
    """

    def persist(self):
        try:
            with open(_PERSIST_PATH, "wb") as fh:
                pickle.dump(self, fh)
            logger.debug("Persisted InMemorySaver to %s", _PERSIST_PATH)
        except Exception as ex:
            logger.warning("Failed to persist InMemorySaver: %s", ex)


def _load_persistent() -> BaseCheckpointSaver | None:
    if os.path.exists(_PERSIST_PATH):
        try:
            with open(_PERSIST_PATH, "rb") as fh:
                obj = pickle.load(fh)
            if isinstance(obj, BaseCheckpointSaver):
                logger.info("Loaded persistent checkpointer from %s", _PERSIST_PATH)
                return obj
        except Exception as ex:
            logger.warning("Unable to load persisted checkpointer: %s", ex)
    return None


# Singleton instance
_GLOBAL_PERSISTENT: Optional[BaseCheckpointSaver] = None


def get_persistent_checkpointer() -> BaseCheckpointSaver:
    global _GLOBAL_PERSISTENT
    if _GLOBAL_PERSISTENT is None:
        loaded = _load_persistent()
        if loaded is not None:
            _GLOBAL_PERSISTENT = loaded
        else:
            _GLOBAL_PERSISTENT = PersistentInMemorySaver()
            try:
                # attempt to persist initial state
                getattr(_GLOBAL_PERSISTENT, "persist", lambda: None)()
            except Exception:
                pass
    return _GLOBAL_PERSISTENT

"""
Avvio del server con più processi (worker), perché l'app sia usabile da più persone contemporaneamente.

    python -m app.serve                       # processi: LANGBRAIN_WORKERS, altrimenti "auto" (fino a 4, uno per CPU)
    python -m app.serve --workers 4 --host 0.0.0.0 --port 8000

Ogni processo serve l'API sulla stessa porta; lo stato condiviso, i compiti in background (un solo processo alla volta) e
i blocchi tra processi sono in `app/core/multiprocesso.py`. Il riavvio dalla pagina /config funziona anche qui: il
processo che riceve la richiesta lascia un file, il supervisore lo vede, ferma tutti i processi e riparte (in Docker esce
soltanto e ci pensa la policy di riavvio del contenitore). `python -m uvicorn app.api.main:app` continua a funzionare, con
un processo solo.
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn
from uvicorn.supervisors import Multiprocess

from app.core import riavvio
from app.core.avvio import ErroreAvvio, verifica_avvio
from app.core.log_json import configura_log, configurazione_uvicorn

APP = "app.api.main:app"
MAX_PROCESSI_AUTOMATICI = 4


def processi_richiesti(valore: str | int | None) -> int:
    """Numero di processi da `--workers` / `LANGBRAIN_WORKERS`: un intero, oppure "auto" (CPU, al massimo 4)."""
    testo = str(valore if valore not in (None, "") else (os.getenv("LANGBRAIN_WORKERS") or "auto")).strip().lower()
    if testo == "auto":
        return max(1, min(MAX_PROCESSI_AUTOMATICI, os.cpu_count() or 1))
    try:
        return max(1, int(testo))
    except ValueError as errore:
        raise SystemExit(f"--workers deve essere un intero o 'auto', non '{testo}'.") from errore


def _sorveglia_richiesta_di_riavvio(file: Path, fermati, esito: dict) -> None:
    """Thread del supervisore: quando un processo lascia il file di riavvio, ferma il server e ricorda di ripartire."""
    while not esito.get("fine"):
        if file.exists():
            try:
                file.unlink()
            except OSError:
                pass
            esito["riavvia"] = True
            fermati()
            return
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Avvia LangBrain con più processi.")
    parser.add_argument("--host", default=os.getenv("LANGBRAIN_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("LANGBRAIN_PORT", "8000")))
    parser.add_argument("--workers", default=None, help="numero di processi o 'auto' (predefinito: LANGBRAIN_WORKERS o auto)")
    parser.add_argument("--log-level", default="info")
    argomenti = parser.parse_args(argv)

    try:
        verifica_avvio()  # un messaggio chiaro e nessun processo avviato, invece di N worker che falliscono
    except ErroreAvvio as errore:
        raise SystemExit(f"Avvio rifiutato: {errore}") from errore
    processi = processi_richiesti(argomenti.workers)
    os.environ["LANGBRAIN_WORKERS"] = str(processi)  # lo vedono i processi figli: attiva lo stato condiviso
    file = riavvio.file_richiesta()
    os.environ[riavvio.VARIABILE_FILE] = str(file)
    file.unlink(missing_ok=True)

    extra = {}
    if configura_log():  # LANGBRAIN_LOG_FORMAT=json: anche i log di uvicorn escono in JSON
        extra["log_config"] = configurazione_uvicorn()
    config = uvicorn.Config(APP, host=argomenti.host, port=argomenti.port, workers=processi, log_level=argomenti.log_level,
                            **extra)
    esito: dict = {}
    sock = config.bind_socket()
    if processi > 1:
        supervisore = Multiprocess(config, sockets=[sock])
        fermati = supervisore.should_exit.set
    else:
        server = uvicorn.Server(config)
        supervisore = None

        def fermati() -> None:
            server.should_exit = True
    threading.Thread(target=_sorveglia_richiesta_di_riavvio, args=(file, fermati, esito), daemon=True).start()
    try:
        if supervisore is not None:
            supervisore.run()
        else:
            server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        esito["fine"] = True
        sock.close()
    if esito.get("riavvia") and not riavvio.in_docker():
        subprocess.Popen(riavvio.comando_di_avvio(), cwd=os.getcwd())
    return 0


if __name__ == "__main__":
    sys.exit(main())

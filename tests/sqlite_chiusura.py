"""
Connessione SQLite che si chiude all'uscita del `with`, per i test che ispezionano o modificano il file del database.

Il `with sqlite3.connect(...) as db:` della libreria standard conferma (o annulla) la transazione ma NON chiude la
connessione: su Linux il file si può comunque cancellare, su Windows no (`PermissionError [WinError 32]` alla pulizia della
cartella temporanea). L'applicazione non è toccata: usa SQLAlchemy/aiosqlite e chiude da sé. Nei test si usa `connetti`:

    with connetti(percorso) as db:
        db.execute(...)

Il comportamento di `with` è quello di sempre (commit all'uscita, rollback se c'è un'eccezione), in più la connessione si chiude.
"""

import sqlite3


class ConnessioneChiusaAllUscita(sqlite3.Connection):
    def __exit__(self, *argomenti):
        try:
            return super().__exit__(*argomenti)
        finally:
            self.close()


def connetti(percorso, **opzioni) -> ConnessioneChiusaAllUscita:
    """Apre il file SQLite con una connessione che si chiude all'uscita del `with`."""
    opzioni.setdefault("factory", ConnessioneChiusaAllUscita)
    return sqlite3.connect(percorso, **opzioni)

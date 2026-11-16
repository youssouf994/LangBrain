"""
Interruttore di circuito (circuit breaker): dopo troppi errori di fila verso una risorsa (un modello, un dispositivo) smette di
chiamarla per un po', invece di far aspettare ogni richiesta fino al timeout.

  CHIUSO   le chiamate passano; `soglia` errori consecutivi lo aprono (un successo azzera il conteggio).
  APERTO   le chiamate sono respinte subito, per `pausa` secondi.
  IN_PROVA scaduta la pausa passa UNA chiamata di prova alla volta: se riesce l'interruttore si chiude, se fallisce si riapre.

Non è thread-safe e non serve esserlo: vive nel ciclo di eventi di un processo. Con più processi ogni processo ha i suoi.
"""

import time
from typing import Callable

CHIUSO, APERTO, IN_PROVA = "chiuso", "aperto", "in_prova"
VALORE_NUMERICO = {CHIUSO: 0, IN_PROVA: 1, APERTO: 2}


class Interruttore:
    def __init__(self, soglia: int = 5, pausa: float = 60.0, orologio: Callable[[], float] = time.monotonic):
        self.soglia = soglia
        self.pausa = pausa
        self._orologio = orologio
        self._errori_consecutivi = 0
        self._aperto_il: float | None = None
        self._prova_in_corso = False

    @property
    def stato(self) -> str:
        if self._aperto_il is None:
            return CHIUSO
        if self._orologio() - self._aperto_il >= self.pausa:
            return IN_PROVA
        return APERTO

    @property
    def attesa_residua(self) -> float:
        """Secondi prima della prossima chiamata di prova (0 se l'interruttore non è aperto)."""
        if self._aperto_il is None:
            return 0.0
        return max(0.0, self.pausa - (self._orologio() - self._aperto_il))

    def consenti(self) -> bool:
        """True se la chiamata può partire. In prova ne passa una sola: finché non finisce le altre sono respinte."""
        stato = self.stato
        if stato == CHIUSO:
            return True
        if stato == IN_PROVA and not self._prova_in_corso:
            self._prova_in_corso = True
            return True
        return False

    def successo(self) -> None:
        self._errori_consecutivi = 0
        self._aperto_il = None
        self._prova_in_corso = False

    def fallimento(self) -> None:
        self._errori_consecutivi += 1
        if self._prova_in_corso or self._errori_consecutivi >= self.soglia:
            self._aperto_il = self._orologio()
        self._prova_in_corso = False

    def annulla_prova(self) -> None:
        """La chiamata di prova non è arrivata al risultato (annullata): si potrà riprovare."""
        self._prova_in_corso = False

    def configura(self, soglia: int, pausa: float) -> None:
        """Aggiorna i parametri (la configurazione si ricarica a caldo) senza cambiare lo stato."""
        self.soglia, self.pausa = soglia, pausa

    @property
    def errori_consecutivi(self) -> int:
        return self._errori_consecutivi

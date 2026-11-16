"""
Protezione delle chiamate al modello linguistico: interruttore di circuito per modello, budget per provider, timeout per modello.

  - **Interruttore** (`[llm.protezione]`): dopo `soglia_errori` errori di fila di un modello (provider non raggiungibile, errore del
    provider, limite di richieste, crediti, chiave) le chiamate a quel modello sono respinte subito con `CIRCUITO_APERTO` per
    `pausa_secondi`; poi una chiamata di prova decide se riaprire. Gli errori della singola richiesta (risposta troncata, contesto
    superato, modello inesistente) NON lo aprono: il provider ha risposto.
  - **Budget** (`[llm.budget]`, `[llm.budget.<provider>]`): `richieste_ora` (chiamate negli ultimi 60 minuti) e `token_giorno` (token
    del giorno UTC, stimati a 4 caratteri per token: il conteggio reale lo ha il provider, qui serve un freno, non una fattura).
    Esaurito il budget la chiamata è respinta con `BUDGET_ESAURITO` e, come ogni errore del modello, il grafo si ferma per
    l'operatore (nessun ripiego silenzioso verso un altro provider, a meno che il ripiego sia attivo).
  - **Timeout** (`[llm.timeout_secondi]`, per provider o `"provider/modello"`): massimo per il singolo tentativo, anche con
    le rigenerazioni di una risposta troncata. Senza voce vale `MAO_TIMEOUT_SECONDS` del client.

Lo stato vive nella memoria del processo. Con più processi ognuno ha il suo interruttore e il suo conteggio: i limiti del budget
si dividono per il numero di processi, così il totale non li supera.
"""

import math
import time
from collections import deque

from app.core import metriche, multiprocesso
from app.core.circuito import APERTO, CHIUSO, IN_PROVA, VALORE_NUMERICO, Interruttore
from app.core.configurazione import get_configurazione
from app.core.errori_llm import ErroreLLM

# Errori che dicono "il provider (o il modello) non è utilizzabile adesso": contano per l'interruttore.
CODICI_CHE_APRONO = {
    "PROVIDER_NON_RAGGIUNGIBILE", "ERRORE_PROVIDER", "LIMITE_RICHIESTE", "CREDITI_ESAURITI", "CHIAVE_NON_VALIDA",
    "ERRORE_IMPREVISTO",
}
CARATTERI_PER_TOKEN = 4
UN_ORA_SECONDI = 3600.0


def stima_token(testo: str) -> int:
    return math.ceil(len(testo or "") / CARATTERI_PER_TOKEN)


class ProtezioneLlm:
    def __init__(self, orologio=time.monotonic, ora_reale=time.time):
        self._orologio = orologio
        self._ora_reale = ora_reale
        self._interruttori: dict[tuple[str, str], Interruttore] = {}
        self._richieste: dict[str, deque[float]] = {}
        self._token: dict[str, tuple[str, int]] = {}   # provider -> (giorno UTC, token)

    # ------------------------------------------------------------------ configurazione

    @staticmethod
    def timeout_per(provider: str, modello: str | None) -> float | None:
        tabella = get_configurazione().llm_timeout
        return tabella.get(f"{provider}/{modello}") or tabella.get(provider)

    @staticmethod
    def limiti(provider: str) -> dict[str, int]:
        """`richieste_ora` e `token_giorno` in vigore per il provider (0 o assente: nessun limite), divisi per i processi."""
        budget = get_configurazione().llm_budget
        grezzi = {**budget.get("*", {}), **budget.get(provider, {})}
        divisore = multiprocesso.lavoratori() if multiprocesso.condiviso() else 1
        return {k: (math.ceil(v / divisore) if v else 0) for k, v in grezzi.items()}

    def _interruttore(self, provider: str, modello: str) -> Interruttore:
        configurazione = get_configurazione()
        chiave = (provider, modello)
        interruttore = self._interruttori.get(chiave)
        if interruttore is None:
            interruttore = self._interruttori[chiave] = Interruttore(orologio=self._orologio)
        interruttore.configura(configurazione.llm_soglia_errori, configurazione.llm_pausa_secondi)
        return interruttore

    # ------------------------------------------------------------------ prima e dopo la chiamata

    def controlla(self, provider: str, modello: str) -> None:
        """Solleva `ErroreLLM` (BUDGET_ESAURITO o CIRCUITO_APERTO) se la chiamata non deve partire; altrimenti registra la richiesta."""
        self._controlla_budget(provider, modello)
        interruttore = self._interruttore(provider, modello)
        if not interruttore.consenti():
            metriche.incrementa("langbrain_llm_circuit_rejections_total", provider=provider, model=modello, motivo="circuito")
            attesa = math.ceil(interruttore.attesa_residua)
            raise ErroreLLM(
                "CIRCUITO_APERTO",
                f"Il modello '{modello}' di '{provider}' è sospeso dopo {interruttore.errori_consecutivi} errori consecutivi: "
                f"nuovo tentativo tra {attesa} s.",
                provider=provider, modello=modello, dettagli={"riprova_tra_secondi": attesa},
            )
        self._richieste.setdefault(provider, deque()).append(self._ora_reale())

    def _controlla_budget(self, provider: str, modello: str) -> None:
        limiti = self.limiti(provider)
        massimo_richieste = limiti.get("richieste_ora", 0)
        if massimo_richieste:
            usate = self._richieste_ultima_ora(provider)
            if usate >= massimo_richieste:
                self._rifiuta_per_budget(provider, modello, f"{usate} richieste nell'ultima ora (limite {massimo_richieste})",
                                         {"richieste_ora": usate, "limite": massimo_richieste})
        massimo_token = limiti.get("token_giorno", 0)
        if massimo_token:
            usati = self._token_oggi(provider)
            if usati >= massimo_token:
                self._rifiuta_per_budget(provider, modello, f"{usati} token stimati oggi (limite {massimo_token})",
                                         {"token_giorno": usati, "limite": massimo_token})

    @staticmethod
    def _rifiuta_per_budget(provider: str, modello: str, dettaglio: str, dettagli: dict) -> None:
        metriche.incrementa("langbrain_llm_circuit_rejections_total", provider=provider, model=modello, motivo="budget")
        raise ErroreLLM("BUDGET_ESAURITO", f"Budget del provider '{provider}' esaurito: {dettaglio}.",
                        provider=provider, modello=modello, dettagli=dettagli)

    def _richieste_ultima_ora(self, provider: str) -> int:
        coda = self._richieste.get(provider)
        if not coda:
            return 0
        limite = self._ora_reale() - UN_ORA_SECONDI
        while coda and coda[0] < limite:
            coda.popleft()
        return len(coda)

    def _giorno(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self._ora_reale()))

    def _token_oggi(self, provider: str) -> int:
        giorno, token = self._token.get(provider, (self._giorno(), 0))
        return token if giorno == self._giorno() else 0

    def _aggiungi_token(self, provider: str, quanti: int) -> None:
        self._token[provider] = (self._giorno(), self._token_oggi(provider) + quanti)

    def successo(self, provider: str, modello: str, token_in: int = 0, token_out: int = 0) -> None:
        self._interruttore(provider, modello).successo()
        self._conta_token(provider, token_in, token_out)

    def errore(self, provider: str, modello: str, errore: ErroreLLM, token_in: int = 0) -> None:
        """Registra un errore. Quelli che aprono l'interruttore lo contano; gli altri dicono che il provider ha risposto."""
        interruttore = self._interruttore(provider, modello)
        if errore.codice in ("CIRCUITO_APERTO", "BUDGET_ESAURITO"):
            return   # respinta prima di partire: nessun tentativo da contare (e nessuna prova da chiudere)
        if errore.codice in CODICI_CHE_APRONO:
            interruttore.fallimento()
        else:
            interruttore.successo()
        self._conta_token(provider, token_in, 0)

    def _conta_token(self, provider: str, token_in: int, token_out: int) -> None:
        if token_in or token_out:
            self._aggiungi_token(provider, token_in + token_out)
            metriche.incrementa("langbrain_llm_tokens_estimated_total", token_in, provider=provider, direction="in")
            metriche.incrementa("langbrain_llm_tokens_estimated_total", token_out, provider=provider, direction="out")

    def annulla(self, provider: str, modello: str) -> None:
        """La chiamata è stata interrotta senza esito (annullamento): se era la prova dell'interruttore, se ne potrà fare un'altra."""
        self._interruttore(provider, modello).annulla_prova()

    def azzera(self) -> None:
        """Dimentica interruttori e conteggi (test)."""
        self._interruttori.clear()
        self._richieste.clear()
        self._token.clear()

    # ------------------------------------------------------------------ osservazione

    def stato(self) -> list[dict]:
        """Interruttori e budget, per le metriche e la diagnosi."""
        righe = []
        for (provider, modello), interruttore in sorted(self._interruttori.items()):
            righe.append({"provider": provider, "model": modello, "state": interruttore.stato,
                          "consecutive_errors": interruttore.errori_consecutivi,
                          "retry_in_s": round(interruttore.attesa_residua, 1)})
        return righe

    async def aggiorna_metriche(self) -> None:
        """Collettore delle metriche: stato degli interruttori e quota di budget usata."""
        for riga in self.stato():
            metriche.imposta("langbrain_llm_circuit_state", VALORE_NUMERICO[riga["state"]],
                             provider=riga["provider"], model=riga["model"])
        for provider in {p for p, _ in self._interruttori} | set(self._richieste) | set(self._token):
            limiti = self.limiti(provider)
            if limiti.get("richieste_ora"):
                metriche.imposta("langbrain_llm_budget_used_ratio", self._richieste_ultima_ora(provider) / limiti["richieste_ora"],
                                 provider=provider, tipo="richieste_ora")
            if limiti.get("token_giorno"):
                metriche.imposta("langbrain_llm_budget_used_ratio", self._token_oggi(provider) / limiti["token_giorno"],
                                 provider=provider, tipo="token_giorno")


protezione = ProtezioneLlm()
metriche.registra_collettore(protezione.aggiorna_metriche)

__all__ = ["protezione", "ProtezioneLlm", "stima_token", "APERTO", "CHIUSO", "IN_PROVA"]

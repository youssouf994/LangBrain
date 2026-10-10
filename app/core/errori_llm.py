"""
Errori dell'LLM e risposta standard.

Quando il modello non è utilizzabile (provider in errore, crediti esauriti, chiave non valida, limite di richieste,
risposta troncata dai token o vuota) non si deve procedere con una decisione inventata: l'errore risale come
`ErroreLLM`, con codice e suggerimento per l'operatore, e il grafo si ferma in attesa di un intervento umano (HITL).

Anche il modello può dichiararsi impossibilitato a rispondere: `ISTRUZIONE_ERRORE_STANDARD` viene aggiunta a ogni
system prompt e gli chiede di rispondere con una sola riga `LLM_ERRORE: <motivo>` invece di inventare una decisione.
"""

import asyncio
import re
from typing import Any

PREFISSO_RISPOSTA_STANDARD = "LLM_ERRORE"

ISTRUZIONE_ERRORE_STANDARD = (
    "Rispondi sempre nel formato richiesto, anche quando i dati sono scarsi o parziali: in quel caso scegli l'opzione "
    "più prudente tra quelle consentite. Usa la riga "
    f"{PREFISSO_RISPOSTA_STANDARD}: <motivo breve> SOLO se non riesci a produrre alcuna risposta nel formato "
    "richiesto (ad esempio la richiesta è troncata o incomprensibile). Non inventare decisioni o valori non previsti."
)
STANDARD_ERROR_INSTRUCTION = (
    "Always answer in the requested format, even when the data are scarce or partial: in that case choose the most "
    "cautious of the allowed options. Use the line "
    f"{PREFISSO_RISPOSTA_STANDARD}: <short reason> ONLY if you cannot produce any answer in the requested format "
    "(for example the request is truncated or incomprehensible). Do not invent decisions or values that are not allowed."
)


def istruzione_errore_standard() -> str:
    """L'istruzione nella lingua in vigore (il prefisso LLM_ERRORE è lo stesso in entrambe)."""
    from app.core.lingua import lingua
    return STANDARD_ERROR_INSTRUCTION if lingua() == "en" else ISTRUZIONE_ERRORE_STANDARD

# Codice -> cosa può fare l'operatore
SUGGERIMENTI = {
    "CREDITI_ESAURITI": "Il provider segnala crediti insufficienti: ricarica il piano oppure passa a un modello gratuito o a un altro provider.",
    "CHIAVE_NON_VALIDA": "Il provider ha rifiutato la chiave API: verifica o sostituisci la chiave nel file .env.",
    "CHIAVE_NON_CONFIGURATA": "Nessuna chiave API utilizzabile per il provider scelto: impostala nel file .env.",
    "LIMITE_RICHIESTE": "Il provider ha raggiunto il limite di richieste: attendi qualche minuto, usa una tua chiave o cambia modello.",
    "MODELLO_NON_TROVATO": "Il modello indicato non esiste o non è disponibile: correggi il nome del modello (variabile *_MODEL nel .env o pagina /config, oppure il modello dell'agente).",
    "CONTESTO_SUPERATO": "La richiesta supera il contesto del modello: scegli un modello con contesto più ampio.",
    "RISPOSTA_TRONCATA": "La risposta è stata interrotta dal limite di token: aumenta MAO_MAX_TOKENS_LIMITE o scegli un modello più capiente.",
    "RISPOSTA_VUOTA": "Il modello non ha prodotto testo (spesso i token sono finiti nel ragionamento): aumenta MAO_MAX_TOKENS_MINIMO.",
    "RISPOSTA_NON_UTILIZZABILE": "Il modello ha dichiarato di non poter rispondere: verifica il contesto fornito o cambia modello.",
    "PROVIDER_NON_RAGGIUNGIBILE": "Il provider non risponde: verifica la rete, l'URL configurato e lo stato del servizio.",
    "ERRORE_PROVIDER": "Errore interno del provider: riprova più tardi o cambia provider.",
    "PROVIDER_NON_SUPPORTATO": "Provider non riconosciuto: correggi DEFAULT_PROVIDER nel file .env.",
    "ERRORE_IMPREVISTO": "Errore imprevisto nella chiamata al modello: controlla i log del server.",
}

SUGGERIMENTI_EN = {
    "CREDITI_ESAURITI": "The provider reports insufficient credits: top up the plan or switch to a free model or another provider.",
    "CHIAVE_NON_VALIDA": "The provider refused the API key: check or replace the key in the .env file.",
    "CHIAVE_NON_CONFIGURATA": "No usable API key for the chosen provider: set it in the .env file.",
    "LIMITE_RICHIESTE": "The provider reached its request limit: wait a few minutes, use your own key or change model.",
    "MODELLO_NON_TROVATO": "The model does not exist or is not available: fix the model name (the *_MODEL variable in .env or the /config page, or the agent's own model).",
    "CONTESTO_SUPERATO": "The request exceeds the model's context: choose a model with a larger context.",
    "RISPOSTA_TRONCATA": "The answer was cut by the token limit: raise MAO_MAX_TOKENS_LIMITE or choose a larger model.",
    "RISPOSTA_VUOTA": "The model produced no text (tokens often went into reasoning): raise MAO_MAX_TOKENS_MINIMO.",
    "RISPOSTA_NON_UTILIZZABILE": "The model declared it cannot answer: check the context given or change model.",
    "PROVIDER_NON_RAGGIUNGIBILE": "The provider does not answer: check the network, the configured URL and the service status.",
    "ERRORE_PROVIDER": "Internal error of the provider: try again later or change provider.",
    "PROVIDER_NON_SUPPORTATO": "Provider not recognised: fix DEFAULT_PROVIDER in the .env file.",
    "ERRORE_IMPREVISTO": "Unexpected error calling the model: check the server logs.",
}

# Chiavi API e token non devono finire in payload di interrupt, checkpoint o risposte HTTP.
_SEGRETI = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|AIza[0-9A-Za-z_\-]{10,}|Bearer\s+[A-Za-z0-9._\-]{12,})")
_LUNGHEZZA_MASSIMA_MESSAGGIO = 400


def oscura_segreti(testo: str) -> str:
    return _SEGRETI.sub("***", str(testo))


class ErroreLLM(RuntimeError):
    """Il modello non è utilizzabile per la richiesta. Estende RuntimeError per compatibilità con i chiamanti esistenti."""

    def __init__(
        self,
        codice: str,
        messaggio: str,
        *,
        provider: str | None = None,
        modello: str | None = None,
        dettagli: dict[str, Any] | None = None,
    ):
        messaggio = oscura_segreti(messaggio)[:_LUNGHEZZA_MASSIMA_MESSAGGIO]
        super().__init__(messaggio)
        self.codice = codice
        self.messaggio = messaggio
        self.provider = provider
        self.modello = modello
        self.dettagli = dettagli or {}

    @property
    def suggerimento(self) -> str:
        from app.core.lingua import lingua
        tabella = SUGGERIMENTI_EN if lingua() == "en" else SUGGERIMENTI
        return tabella.get(self.codice, tabella["ERRORE_IMPREVISTO"])

    def come_dizionario(self) -> dict[str, Any]:
        """Forma serializzabile per payload di interrupt e risposte HTTP."""
        risultato: dict[str, Any] = {
            "codice": self.codice, "messaggio": self.messaggio, "suggerimento": self.suggerimento,
        }
        if self.provider:
            risultato["provider"] = self.provider
        if self.modello:
            risultato["modello"] = self.modello
        if self.dettagli:
            risultato["dettagli"] = self.dettagli
        return risultato


def classifica_errore(errore: BaseException) -> tuple[str, str]:
    """Traduce un'eccezione del client (openai/httpx) in (codice, messaggio) comprensibili all'operatore."""
    if isinstance(errore, ErroreLLM):
        return errore.codice, errore.messaggio
    stato = getattr(errore, "status_code", None)
    testo = str(errore)
    minuscolo = testo.casefold()

    if stato in (401, 403) or (stato == 400 and "api key" in minuscolo and any(k in minuscolo for k in ("not valid", "invalid", "expired"))):
        # Google segnala una chiave errata con un 400 invece che con un 401.
        codice = "CHIAVE_NON_VALIDA"
    elif stato == 402 or "insufficient credits" in minuscolo or "insufficient_quota" in minuscolo:
        codice = "CREDITI_ESAURITI"
    elif stato == 429:
        codice = "LIMITE_RICHIESTE"
    elif stato == 404 or (stato == 400 and any(k in minuscolo for k in (
            "invalid model", "invalid_model", "model not found", "model_not_found", "unknown model", "does not exist"))):
        # Alcuni provider (Mistral, OpenAI-compatibili) segnalano un nome di modello sbagliato con un 400 invece di un 404.
        codice = "MODELLO_NON_TROVATO"
    elif stato == 400 and any(k in minuscolo for k in ("context length", "context_length", "too many tokens", "maximum context")):
        codice = "CONTESTO_SUPERATO"
    elif isinstance(stato, int) and stato >= 500:
        codice = "ERRORE_PROVIDER"
    elif isinstance(errore, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)) or type(errore).__name__ in (
        "APITimeoutError", "APIConnectionError", "ConnectTimeout", "ReadTimeout", "ConnectError",
    ):
        codice = "PROVIDER_NON_RAGGIUNGIBILE"
    else:
        codice = "ERRORE_IMPREVISTO"
    return codice, f"{type(errore).__name__}: {testo}" if not stato else testo


def risposta_standard(errore: ErroreLLM) -> str:
    """Rappresentazione testuale uniforme dell'errore, nello stesso formato che il modello usa per dichiararsi incapace."""
    return f"{PREFISSO_RISPOSTA_STANDARD}: {errore.codice} - {errore.messaggio}"


def interpreta_risposta_standard(testo: str | None) -> str | None:
    """Se la risposta è quella standard di errore restituisce il motivo, altrimenti None."""
    pulito = (testo or "").strip()
    if pulito.upper().startswith(PREFISSO_RISPOSTA_STANDARD):
        return pulito[len(PREFISSO_RISPOSTA_STANDARD):].lstrip(" :-") or "motivo non indicato"
    return None

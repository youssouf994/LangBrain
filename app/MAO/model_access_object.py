import logging
import os
import httpx
import httpx2
from anthropic import AsyncAnthropic
from dotenv import dotenv_values, find_dotenv, load_dotenv
from openai import AsyncOpenAI

from app.core.configurazione import get_configurazione
from app.core.errori_llm import ErroreLLM, classifica_errore

# `LANGBRAIN_ENV_FILE` permette di indicare un file .env diverso da quello del progetto.
_PERCORSO_ENV = os.getenv("LANGBRAIN_ENV_FILE") or find_dotenv()
if _PERCORSO_ENV:
    load_dotenv(_PERCORSO_ENV)
logger = logging.getLogger(__name__)

# Ricarica a caldo del .env: un operatore che cambia chiave o modello dopo un errore del provider
# può riprendere il grafo senza riavviare il server. Si applicano solo le variabili cambiate nel file,
# così le variabili impostate a mano nell'ambiente (es. da Docker) non vengono sovrascritte.
_snapshot_env: dict[str, str | None] = dotenv_values(_PERCORSO_ENV) if _PERCORSO_ENV else {}
_generazione_env = 0


def _mtime_env() -> int | None:
    try:
        return os.stat(_PERCORSO_ENV).st_mtime_ns if _PERCORSO_ENV else None
    except OSError:
        return None


_ultimo_mtime_env = _mtime_env()


def ricarica_env_se_modificato() -> bool:
    """Applica a os.environ le variabili cambiate nel file .env dall'ultima lettura. True se ce ne sono."""
    global _PERCORSO_ENV, _snapshot_env, _generazione_env, _ultimo_mtime_env
    if not _PERCORSO_ENV:
        # Nessun .env all'avvio: può essere stato creato dopo (es. dalla pagina delle impostazioni, anche da un altro processo).
        trovato = os.getenv("LANGBRAIN_ENV_FILE") or find_dotenv()
        if trovato:
            _PERCORSO_ENV = trovato
    mtime = _mtime_env()
    if mtime is None or mtime == _ultimo_mtime_env:
        return False
    _ultimo_mtime_env = mtime
    nuovo = dotenv_values(_PERCORSO_ENV)
    modificate = {k: v for k, v in nuovo.items() if v is not None and _snapshot_env.get(k) != v}
    _snapshot_env = nuovo
    if not modificate:
        return False
    os.environ.update(modificate)
    _generazione_env += 1
    logger.info("[MAO] File .env modificato: ricaricate le variabili %s.", sorted(modificate))
    return True


# Provider gestiti dal MAO e alias accettati (DEFAULT_PROVIDER, impostazioni per agente, parametro `provider`).
PROVIDER_NOTI = ("google_studio", "openrouter", "mistral", "anthropic", "local")
_ALIAS_PROVIDER = {"google": "google_studio", "gemini": "google_studio", "or": "openrouter", "mistralai": "mistral", "mistral_ai": "mistral", "claude": "anthropic"}


def normalizza_provider(nome: str | None) -> str:
    """Nome canonico del provider (minuscolo, alias risolti). 'auto' resta 'auto'."""
    pulito = str(nome or "").strip().lower()
    return _ALIAS_PROVIDER.get(pulito, pulito)


# Errori che riguardano l'intero provider e non il singolo modello: provare altri modelli dello stesso provider
# sarebbe inutile (e potrebbe costare), si passa direttamente al provider successivo.
_ERRORI_DELL_INTERO_PROVIDER = {
    "CHIAVE_NON_VALIDA", "CHIAVE_NON_CONFIGURATA", "CREDITI_ESAURITI", "PROVIDER_NON_RAGGIUNGIBILE",
}


def _intero_env(nome: str, default: int) -> int:
    try:
        valore = int(os.getenv(nome, str(default)))
        if valore <= 0:
            raise ValueError
        return valore
    except ValueError:
        logger.warning("[MAO] %s non valido; uso il default di %d.", nome, default)
        return default


def _has_usable_api_key(value: str | None) -> bool:
    """Esclude valori vuoti e placeholder dalle catene di fallback remote."""
    normalized = str(value or "").strip().strip('"\'').casefold()
    return bool(normalized) and normalized not in {
        "nessuna",
        "none",
        "not-required",
        "replace-with-your-gemini-api-key",
        "replace-with-your-google-api-key",
        "replace-with-your-openrouter-api-key",
        "replace-with-your-mistral-api-key",
        "replace-with-your-anthropic-api-key",
    }


class Mao:
    def __init__(self):
        self._configura()

    def _configura(self) -> None:
        """Legge la configurazione dall'ambiente e costruisce i client dei provider."""
        self._generazione_config = _generazione_env

        # Provider di default ('auto', 'google_studio', 'local', 'openrouter', 'mistral', 'anthropic')
        self.default_provider = os.getenv("DEFAULT_PROVIDER", "google_studio").strip().strip('"\'')
        logger.info(f"[MAO] Provider di default risolto: '{self.default_provider}' (da DEFAULT_PROVIDER env)")

        try:
            self.timeout_seconds = float(os.getenv("MAO_TIMEOUT_SECONDS", "40"))
            if self.timeout_seconds <= 0:
                raise ValueError
        except ValueError:
            self.timeout_seconds = 40.0
            logger.warning("[MAO] MAO_TIMEOUT_SECONDS non valido; uso il default di 40 secondi.")

        # Token di output: i modelli con ragionamento ne consumano molti prima della risposta, quindi le chiamate
        # degli agenti partono da un minimo adeguato e, se la risposta viene troncata, raddoppiano fino al limite.
        self.max_tokens_minimo = _intero_env("MAO_MAX_TOKENS_MINIMO", 8192)
        self.max_tokens_limite = max(_intero_env("MAO_MAX_TOKENS_LIMITE", 32768), self.max_tokens_minimo)

        # Per default un errore del provider o del modello scelto NON scivola in silenzio su altri provider o modelli
        # (potrebbero essere a pagamento): risale all'operatore. Il ripiego si attiva con MAO_FALLBACK=1 oppure con
        # `fallback = true` in configurazione.toml (la variabile d'ambiente ha la precedenza).
        scelte = get_configurazione()
        fallback_env = os.getenv("MAO_FALLBACK", "").strip()
        self.fallback = (fallback_env == "1") if fallback_env else scelte.llm_fallback
        self.ordine_provider = list(scelte.ordine_provider)

        # Transport HTTP generico per evitare blocchi IPv6
        self.http_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
            timeout=self.timeout_seconds,
        )
        # L'SDK di Anthropic usa httpx2 e rifiuta i client httpx: stesso trasporto IPv4, libreria diversa.
        self.http_client_anthropic = httpx2.AsyncClient(
            transport=httpx2.AsyncHTTPTransport(local_address="0.0.0.0"),
            timeout=self.timeout_seconds,
        )

        # Mappa dei client OpenAI-compatibili. Per aggiungere un provider basta una riga con `_crea_provider`.
        self.providers = {
            "google_studio": self._crea_provider(
                "google_studio", chiavi_env=("GEMINI_API_KEY", "GOOGLE_API_KEY"), url_env="GOOGLE_BASE_URL",
                url_default="https://generativelanguage.googleapis.com/v1beta/openai/",
                modello_env="GEMINI_MODEL", modello_default="gemini-3.5-flash", max_retries=2,
            ),
            "local": self._crea_provider(
                "local", chiavi_env=("LOCAL_API_KEY",), url_env="LOCAL_MODEL_BASE_URL",
                url_default="http://127.0.0.1:8080/v1", modello_env="LOCAL_MODEL", modello_default="local-model",
                max_retries=1, sempre_attivo=True,
            ),
            "openrouter": self._crea_provider(
                "openrouter", chiavi_env=("OPENROUTER_API_KEY",), url_env="OPENROUTER_BASE_URL",
                url_default="https://openrouter.ai/api/v1", modello_env="OPENROUTER_MODEL",
                modello_default="qwen/qwen3.8-27b:free", max_retries=2,
                intestazioni={
                    "HTTP-Referer": os.getenv("OPENROUTER_REFERER", "https://github.com/youssouf994/LangBrain"),
                    "X-Title": os.getenv("OPENROUTER_APP_TITLE", "LangBrain"),
                },
            ),
            "mistral": self._crea_provider(
                "mistral", chiavi_env=("MISTRAL_API_KEY",), url_env="MISTRAL_BASE_URL",
                url_default="https://api.mistral.ai/v1", modello_env="MISTRAL_MODEL",
                modello_default="ministral-8b-latest", max_retries=2,
            ),
            # Claude usa il client nativo di Anthropic (Messages API), non lo strato OpenAI-compatibile.
            "anthropic": self._crea_provider(
                "anthropic", chiavi_env=("ANTHROPIC_API_KEY",), url_env="ANTHROPIC_BASE_URL",
                url_default="https://api.anthropic.com", modello_env="ANTHROPIC_MODEL",
                modello_default="claude-haiku-5-5", max_retries=2, client_nativo=AsyncAnthropic,
            ),
        }

    def _crea_provider(
        self,
        nome: str,
        *,
        chiavi_env: tuple[str, ...],
        url_env: str,
        url_default: str,
        modello_env: str,
        modello_default: str,
        max_retries: int,
        intestazioni: dict[str, str] | None = None,
        sempre_attivo: bool = False,
        client_nativo: type | None = None,
    ) -> dict:
        """
        Costruisce la voce di un provider OpenAI-compatibile leggendo dall'ambiente chiave (la prima variabile
        valorizzata tra `chiavi_env`), URL e modello. I modelli di ripiego vengono da configurazione.toml.
        Un provider senza chiave utilizzabile resta escluso dalle catene di ripiego (`enabled` False).
        `client_nativo` sostituisce AsyncOpenAI con l'SDK del provider (stessi parametri base_url, api_key, http_client).
        """
        chiave = next((os.getenv(k) for k in chiavi_env if os.getenv(k)), "nessuna").strip().strip('"\'')
        return {
            "client": (client_nativo or AsyncOpenAI)(
                base_url=os.getenv(url_env, url_default).strip().strip('"\''),
                api_key=chiave,
                http_client=self.http_client_anthropic if client_nativo else self.http_client,
                max_retries=max_retries,
                **({"default_headers": intestazioni} if intestazioni else {}),
            ),
            "model": os.getenv(modello_env, modello_default).strip().strip('"\''),
            "fallback_models": list(get_configurazione().modelli_di_ripiego.get(nome, [])),
            "enabled": True if sempre_attivo else _has_usable_api_key(chiave),
        }

    async def _aggiorna_configurazione_se_cambiata(self) -> None:
        ricarica_env_se_modificato()
        if self._generazione_config != _generazione_env:
            vecchi_client = (self.http_client, self.http_client_anthropic)
            self._configura()
            for vecchio in vecchi_client:
                await vecchio.aclose()

    @staticmethod
    def _come_errore_llm(errore: BaseException, provider: str, modello: str | None) -> ErroreLLM:
        if isinstance(errore, ErroreLLM):
            errore.provider = errore.provider or provider
            errore.modello = errore.modello or modello
            return errore
        codice, messaggio = classifica_errore(errore)
        return ErroreLLM(codice, messaggio, provider=provider, modello=modello)

    async def _chiedi_modello(
        self,
        client: AsyncOpenAI | AsyncAnthropic,
        provider_key: str,
        modello: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        enable_reasoning: bool,
        rigenera_se_troncata: bool,
    ) -> str:
        tokens = max_tokens
        if rigenera_se_troncata:
            tokens = min(max(max_tokens, self.max_tokens_minimo), self.max_tokens_limite)

        while True:
            if isinstance(client, AsyncAnthropic):
                # Messages API: il system prompt è un parametro a parte e max_tokens è obbligatorio.
                risposta = await client.messages.create(
                    model=modello, system=system_prompt, messages=[{"role": "user", "content": user_prompt}],
                    temperature=temperature, max_tokens=tokens,
                )
                contenuto = "".join(b.text for b in risposta.content if getattr(b, "type", None) == "text").strip()
                troncata = risposta.stop_reason == "max_tokens"
            else:
                contenuto, troncata = await self._chiedi_openai(client, modello, system_prompt, user_prompt, temperature, tokens, enable_reasoning)

            if troncata and rigenera_se_troncata:
                if tokens < self.max_tokens_limite:
                    nuovo_limite = min(tokens * 2, self.max_tokens_limite)
                    logger.warning(
                        "[MAO] [%s] Risposta di '%s' troncata a %d token: riprovo con %d.",
                        provider_key, modello, tokens, nuovo_limite,
                    )
                    tokens = nuovo_limite
                    continue
                raise ErroreLLM(
                    "RISPOSTA_TRONCATA", f"Risposta interrotta dal limite di {tokens} token.",
                    provider=provider_key, modello=modello,
                )
            if not contenuto:
                raise ErroreLLM(
                    "RISPOSTA_VUOTA", f"Il modello '{modello}' non ha prodotto testo.",
                    provider=provider_key, modello=modello,
                )
            return contenuto

    @staticmethod
    async def _chiedi_openai(client, modello, system_prompt, user_prompt, temperature, tokens, enable_reasoning) -> tuple[str, bool]:
        """Una chiamata chat-completions OpenAI-compatibile. Restituisce (testo, risposta_troncata)."""
        kwargs = {
            "model": modello,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": tokens,
        }
        if enable_reasoning:
            kwargs["extra_body"] = {"reasoning": {"enabled": True}}

        response = await client.chat.completions.create(**kwargs)
        scelta = response.choices[0] if response.choices else None
        contenuto = ((scelta.message.content if scelta else None) or "").strip()
        return contenuto, scelta is not None and scelta.finish_reason == "length"

    async def _execute_chat(
        self,
        provider_key: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        model: str | None = None,
        enable_reasoning: bool = False,
        rigenera_se_troncata: bool = False,
    ) -> str:
        """Esecutore generico per qualsiasi endpoint OpenAI-compatibile."""
        p_cfg = self.providers.get(provider_key)
        if not p_cfg:
            raise ErroreLLM("PROVIDER_NON_SUPPORTATO", f"Provider '{provider_key}' non supportato o non configurato.", provider=provider_key)

        client = p_cfg["client"]
        target_model = model or p_cfg["model"]
        candidate_models = [target_model]
        if self.fallback:
            candidate_models += [m for m in p_cfg["fallback_models"] if m != target_model]

        ultimo_errore: ErroreLLM | None = None
        for m in candidate_models:
            try:
                return await self._chiedi_modello(
                    client, provider_key, m, system_prompt, user_prompt, temperature,
                    max_tokens, enable_reasoning, rigenera_se_troncata,
                )
            except Exception as e:
                ultimo_errore = self._come_errore_llm(e, provider_key, m)
                logger.warning(f"[MAO] [{provider_key}] Errore con il modello '{m}': {ultimo_errore.messaggio}")
                if ultimo_errore.codice in _ERRORI_DELL_INTERO_PROVIDER:
                    break

        raise ultimo_errore

    async def call_model(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        provider: str | None = None,
        model: str | None = None,
        fallback_on_error: bool | None = None,
        enable_reasoning: bool = False,
        rigenera_se_troncata: bool = False,
    ) -> str:
        """
        Interfaccia principale invocata dagli agenti.
        Solleva ErroreLLM (con codice e suggerimento per l'operatore) se nessun provider completa la richiesta.
        `fallback_on_error` None usa MAO_FALLBACK. `rigenera_se_troncata` porta i token almeno a MAO_MAX_TOKENS_MINIMO
        e li raddoppia se la risposta viene interrotta (fino a MAO_MAX_TOKENS_LIMITE); se resta troncata solleva ErroreLLM.
        """
        await self._aggiorna_configurazione_se_cambiata()
        target_provider = (provider or self.default_provider).lower()
        logger.debug(f"[MAO] call_model -> provider richiesto: '{target_provider}'")

        # Supporto di debug/test: MOCK provider via env var per flussi OVERRIDE senza chiavi
        if os.getenv("MAO_ENABLE_MOCK", "0").strip() == "1":
            logger.info("[MAO] MOCK mode abilitato via MAO_ENABLE_MOCK=1 — restituisco risposta canned.")
            # Un JSON di esempio che il Brain può parsare per eseguire un UNBLOCK_AND_SET
            return '[{"target": "device_l3", "action": "UNBLOCK_AND_SET", "value": "ON"}]'

        target_provider = normalizza_provider(target_provider)

        usa_fallback = self.fallback if fallback_on_error is None else fallback_on_error

        # Definizione sequenza tentativi (fallback)
        if target_provider == "auto":
            chain = ["local"] + [p for p in self.ordine_provider if p != "local"]
        elif usa_fallback:
            chain = [target_provider] + [p for p in self.ordine_provider if p != target_provider]
        else:
            chain = [target_provider]

        errori: list[ErroreLLM] = []
        for p_key in chain:
            if p_key not in self.providers:
                errori.append(ErroreLLM("PROVIDER_NON_SUPPORTATO", f"Provider '{p_key}' non supportato.", provider=p_key))
                continue
            if not self.providers[p_key].get("enabled", True):
                logger.info("[MAO] Provider '%s' ignorato: credenziali non configurate.", p_key)
                errori.append(ErroreLLM(
                    "CHIAVE_NON_CONFIGURATA", f"Chiave API non configurata per il provider '{p_key}'.", provider=p_key,
                ))
                continue
            try:
                risposta = await self._execute_chat(
                    provider_key=p_key,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    model=model if p_key == target_provider else None,
                    enable_reasoning=enable_reasoning,
                    rigenera_se_troncata=rigenera_se_troncata,
                )
                if errori:
                    causa = errori[0]
                    logger.warning(
                        "[MAO] RIPIEGO ATTIVO: ha risposto '%s' al posto di '%s' (%s: %s).",
                        p_key, target_provider, causa.codice, causa.messaggio,
                    )
                return risposta
            except Exception as err:
                errore = self._come_errore_llm(err, p_key, None)
                errori.append(errore)
                logger.warning(f"[MAO] Fallimento provider '{p_key}' ({errore.messaggio}).")

        # La causa che interessa all'operatore è quella del provider scelto, cioè del primo tentativo.
        principale = errori[0] if errori else ErroreLLM("PROVIDER_NON_SUPPORTATO", f"Provider '{target_provider}' non disponibile.")
        raise ErroreLLM(
            principale.codice,
            f"Nessun provider LLM disponibile ha completato la richiesta. Causa: {principale.messaggio}",
            provider=principale.provider,
            modello=principale.modello,
            dettagli={"tentativi": [e.come_dizionario() for e in errori]} if len(errori) > 1 else None,
        )

    def descrivi_provider(self, provider: str | None, modello: str | None) -> dict:
        """Provider e modello che verrebbero usati davvero (i default del provider se non indicati)."""
        nome = normalizza_provider(provider) or normalizza_provider(self.default_provider)
        cfg = self.providers.get(nome)
        return {"provider": nome, "model": modello or (cfg["model"] if cfg else None), "abilitato": bool(cfg and cfg["enabled"])}

    async def aclose(self) -> None:
        """Chiude i client HTTP asincroni condivisi dai provider."""
        await self.http_client.aclose()
        await self.http_client_anthropic.aclose()

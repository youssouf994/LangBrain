import logging
import os
import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()
logger = logging.getLogger(__name__)


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
    }


class Mao:
    def __init__(self):
        # Provider di default ('auto', 'google_studio', 'local', 'openrouter')
        self.default_provider = os.getenv("DEFAULT_PROVIDER", "google_studio").strip().strip('"\'')
        logger.info(f"[MAO] Provider di default risolto: '{self.default_provider}' (da DEFAULT_PROVIDER env)")

        try:
            self.timeout_seconds = float(os.getenv("MAO_TIMEOUT_SECONDS", "40"))
            if self.timeout_seconds <= 0:
                raise ValueError
        except ValueError:
            self.timeout_seconds = 40.0
            logger.warning("[MAO] MAO_TIMEOUT_SECONDS non valido; uso il default di 40 secondi.")

        # Transport HTTP generico per evitare blocchi IPv6
        self.http_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
            timeout=self.timeout_seconds,
        )

        google_api_key = os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", "nessuna")).strip().strip('"\'')
        local_api_key = os.getenv("LOCAL_API_KEY", "nessuna").strip().strip('"\'')
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "nessuna").strip().strip('"\'')

        # Mappa dei client OpenAI-compatibili caricati interamente da .env
        self.providers = {
            "google_studio": {
                "client": AsyncOpenAI(
                    base_url=os.getenv("GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/").strip().strip('"\''),
                    api_key=google_api_key,
                    http_client=self.http_client,
                    max_retries=2,
                ),
                "model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip().strip('"\''),
                "fallback_models": ["gemini-3.5-flash", "gemini-flash-latest"],
                "enabled": _has_usable_api_key(google_api_key),
            },
            "local": {
                "client": AsyncOpenAI(
                    base_url=os.getenv("LOCAL_MODEL_BASE_URL", "http://127.0.0.1:8080/v1").strip().strip('"\''),
                    api_key=local_api_key,
                    http_client=self.http_client,
                    max_retries=1,
                ),
                "model": os.getenv("LOCAL_MODEL", "local-model").strip().strip('"\''),
                "fallback_models": [],
                "enabled": True,
            },
            "openrouter": {
                "client": AsyncOpenAI(
                    base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip().strip('"\''),
                    api_key=openrouter_api_key,
                    http_client=self.http_client,
                    max_retries=2,
                    default_headers={
                        "HTTP-Referer": os.getenv("OPENROUTER_REFERER", "https://github.com/youssouf994/LangBrain"),
                        "X-Title": os.getenv("OPENROUTER_APP_TITLE", "LangBrain"),
                    },
                ),
                "model": os.getenv("OPENROUTER_MODEL", "openrouter/auto").strip().strip('"\''),
                "fallback_models": [
                    "openrouter/auto",
                    "deepseek/deepseek-r1:free",
                    "meta-llama/llama-3.2-11b-vision-instruct:free",
                    "google/gemini-2.5-flash",
                ],
                "enabled": _has_usable_api_key(openrouter_api_key),
            },
        }

    async def _execute_chat(
        self,
        provider_key: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        model: str | None = None,
        enable_reasoning: bool = False,
    ) -> str:
        """Esecutore generico per qualsiasi endpoint OpenAI-compatibile."""
        p_cfg = self.providers.get(provider_key)
        if not p_cfg:
            raise ValueError(f"Provider '{provider_key}' non supportato o non configurato.")

        client: AsyncOpenAI = p_cfg["client"]
        target_model = model or p_cfg["model"]
        candidate_models = [target_model] + [m for m in p_cfg["fallback_models"] if m != target_model]

        last_error = None
        for m in candidate_models:
            try:
                kwargs = {
                    "model": m,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if enable_reasoning:
                    kwargs["extra_body"] = {"reasoning": {"enabled": True}}

                response = await client.chat.completions.create(**kwargs)
                if response.choices:
                    content = response.choices[0].message.content
                    if content is not None:
                        return content.strip()
            except Exception as e:
                last_error = e
                logger.warning(f"[MAO] [{provider_key}] Errore con il modello '{m}': {e}. Tento il prossimo...")

        raise last_error or ValueError(f"Nessuna risposta generata da {provider_key}")

    async def call_model(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        provider: str | None = None,
        model: str | None = None,
        fallback_on_error: bool = True,
        enable_reasoning: bool = False,
    ) -> str:
        """Interfaccia principale invocata dagli agenti."""
        target_provider = (provider or self.default_provider).lower()
        logger.debug(f"[MAO] call_model -> provider richiesto: '{target_provider}'")

        # Supporto di debug/test: MOCK provider via env var per flussi OVERRIDE senza chiavi
        if os.getenv("MAO_ENABLE_MOCK", "0").strip() == "1":
            logger.info("[MAO] MOCK mode abilitato via MAO_ENABLE_MOCK=1 — restituisco risposta canned.")
            # Un JSON di esempio che il Brain può parsare per eseguire un UNBLOCK_AND_SET
            return '[{"target": "device_l3", "action": "UNBLOCK_AND_SET", "value": "ON"}]'

        # Mappatura alias rapida
        if target_provider in ("google", "gemini"):
            target_provider = "google_studio"
        elif target_provider in ("or",):
            target_provider = "openrouter"

        # Definizione sequenza tentativi (fallback)
        if target_provider == "auto":
            chain = ["local", "google_studio", "openrouter"]
        elif fallback_on_error:
            chain = [target_provider] + [p for p in ["google_studio", "openrouter", "local"] if p != target_provider]
        else:
            chain = [target_provider]

        for p_key in chain:
            if p_key not in self.providers:
                continue
            if not self.providers[p_key].get("enabled", True):
                logger.info("[MAO] Provider '%s' ignorato: credenziali non configurate.", p_key)
                continue
            try:
                return await self._execute_chat(
                    provider_key=p_key,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    model=model if p_key == target_provider else None,
                    enable_reasoning=enable_reasoning,
                )
            except Exception as err:
                logger.warning(f"[MAO] Fallimento provider '{p_key}' ({err}).")

        raise RuntimeError("Nessun provider LLM disponibile ha completato la richiesta.")

    async def aclose(self) -> None:
        """Chiude il client HTTP asincrono condiviso dai provider."""
        await self.http_client.aclose()

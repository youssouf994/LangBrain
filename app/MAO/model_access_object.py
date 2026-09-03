import logging
import os
import httpx
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
logger = logging.getLogger(__name__)


class Mao:
    def __init__(self):
        # Provider di default ('auto', 'google_studio', 'local', 'openrouter')
        self.default_provider = os.getenv("DEFAULT_PROVIDER", "google_studio").strip().strip('"\'')
        logger.info(f"[MAO] Provider di default risolto: '{self.default_provider}' (da DEFAULT_PROVIDER env)")

        # Transport HTTP generico per evitare blocchi IPv6
        http_client = httpx.Client(
            transport=httpx.HTTPTransport(local_address="0.0.0.0"),
            timeout=20.0,
        )

        # Mappa dei client OpenAI-compatibili caricati interamente da .env
        self.providers = {
            "google_studio": {
                "client": OpenAI(
                    base_url=os.getenv("GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/").strip().strip('"\''),
                    api_key=os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", "nessuna")).strip().strip('"\''),
                    http_client=http_client,
                    max_retries=2,
                ),
                "model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip().strip('"\''),
                "fallback_models": ["gemini-3.5-flash", "gemini-flash-latest"],
            },
            "local": {
                "client": OpenAI(
                    base_url=os.getenv("LOCAL_MODEL_BASE_URL", "http://127.0.0.1:8080/v1").strip().strip('"\''),
                    api_key=os.getenv("LOCAL_API_KEY", "nessuna").strip().strip('"\''),
                    http_client=http_client,
                    max_retries=1,
                ),
                "model": os.getenv("LOCAL_MODEL", "local-model").strip().strip('"\''),
                "fallback_models": [],
            },
            "openrouter": {
                "client": OpenAI(
                    base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip().strip('"\''),
                    api_key=os.getenv("OPENROUTER_API_KEY", "nessuna").strip().strip('"\''),
                    http_client=http_client,
                    max_retries=2,
                    default_headers={
                        "HTTP-Referer": os.getenv("OPENROUTER_REFERER", "https://github.com/IoTBoilerplate"),
                        "X-Title": os.getenv("OPENROUTER_APP_TITLE", "IoT Boilerplate Agent"),
                    },
                ),
                "model": os.getenv("OPENROUTER_MODEL", "openrouter/auto").strip().strip('"\''),
                "fallback_models": [
                    "openrouter/auto",
                    "deepseek/deepseek-r1:free",
                    "meta-llama/llama-3.2-11b-vision-instruct:free",
                    "google/gemini-2.5-flash",
                ],
            },
        }

    def _execute_chat(
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

        client: OpenAI = p_cfg["client"]
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

                response = client.chat.completions.create(**kwargs)
                if response.choices:
                    content = response.choices[0].message.content
                    if content is not None:
                        return content.strip()
            except Exception as e:
                last_error = e
                logger.warning(f"[MAO] [{provider_key}] Errore con il modello '{m}': {e}. Tento il prossimo...")

        raise last_error or ValueError(f"Nessuna risposta generata da {provider_key}")

    def call_model(
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
            try:
                return self._execute_chat(
                    provider_key=p_key,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    model=model if p_key == target_provider else None,
                    enable_reasoning=enable_reasoning,
                )
            except Exception as err:
                logger.warning(f"[MAO] Fallimento provider '{p_key}' ({err}). Passaggio al fallback...")

        raise RuntimeError("Tutti i provider LLM configurati hanno fallito.")
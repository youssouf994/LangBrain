from abc import ABC, abstractmethod
import copy
import json
import logging
import re
from typing import Any

from app.core.configurazione import get_configurazione
from app.core.modelli_agenti import risolvi_modello
from app.core.errori_llm import (
    ErroreLLM, classifica_errore, interpreta_risposta_standard, istruzione_errore_standard,
)
from app.core.lingua import scegli
from app.core.prompt import prompt_di_sistema
from app.core.priorita import NOME_BRAIN, priorita_attore, registra_priorita, trova_blocco_prevalente
from app.core.risultati import (
    APPLICATO, COMANDO_NON_AMMESSO, RESPINTO_PRIORITA, SOLO_LOG, TOOL_ASSENTE, TOOL_ERRORE, GIA_IMPOSTATO,
    comanda_tool, e_comando_non_ammesso, e_guasto_tool, leggi_tool, nuovo_risultato,
)
from app.graph.state import GraphState, EscalationItem, filtra_segnali, finestra_segnali_ore
from app.tools.nervous_system import NervousSystem as EventLog  # nome storico: i vecchi test lo sostituiscono con patch
from app.tools.effectors import trova_tool
from app.MAO.model_access_object import Mao

logger = logging.getLogger(__name__)

class BaseAgent(ABC):
    """
    Classe base astratta per tutti gli agenti di dominio.
    Gestisce readout dei tool, tracciamento old_value -> new_value su DB e prevenzione comandi ridondanti.
    """

    def __init__(self, name: str, managed_targets: list[str], conflict_window_minutes: int = 30, priority_weight: float = 0.000):
        self.name = name
        self.managed_targets = managed_targets
        self.conflict_window_minutes = conflict_window_minutes
        self.priority_weight = priority_weight
        registra_priorita(name, priority_weight)
        self.mao = Mao()
        self.event_log = EventLog(target=managed_targets, frequency=conflict_window_minutes)

    async def __call__(self, state: GraphState) -> dict[str, Any]:
        """
        Metodo invocato da LangGraph quando il nodo dell'agente viene eseguito.
        """
        logger.info(f"[{self.name}] Invocazione agente (Priorità: {self.priority_weight})")

        # 1. Recupera le escalation aperte relative ai target gestiti
        pending_escalations = state.get("pending_escalations", [])
        accepts_all_targets = "all" in self.managed_targets
        child_agent_names = set(getattr(self, "sub_agent_names", []))
        agent_escalations = [
            esc for esc in pending_escalations 
            if accepts_all_targets
            or esc.get("target_device") in self.managed_targets
            or esc.get("source_agent") in child_agent_names
        ]

        # 2. Recupera gli eventi recenti dal DB per i target gestiti (ultimi N minuti)
        try:
            recent_events = await self.event_log.get_recent_events()
        except Exception as e:
            logger.error(f"[{self.name}] Errore nel recupero degli eventi recenti: {e}")
            recent_events = []

        # 3. Recupera le ultime letture dei sensori per i target gestiti
        all_readings = filtra_segnali(state.get("readings", []), finestra_segnali_ore(state.get("config")))
        relevant_readings = [
            r for r in all_readings 
            if r.get("sensor_id") in self.managed_targets or r.get("agent_owner") == self.name
        ]

        # 4. Esegue la logica specifica del sotto-agente (process)
        updates = await self.process(
            state=state, 
            recent_events=recent_events, 
            relevant_readings=relevant_readings,
            agent_escalations=agent_escalations
        )
        return updates

    @abstractmethod
    async def process(
        self, 
        state: GraphState, 
        recent_events: list[dict], 
        relevant_readings: list[dict], 
        agent_escalations: list[dict]
    ) -> dict[str, Any]:
        """
        Metodo astratto da implementare in ciascun agente concreto.
        """
        pass

    async def check_priority_lock(self, target: str) -> tuple[bool, str]:
        """
        Verifica se sul target c'è un blocco attivo imposto da un attore con priorità strettamente maggiore
        (es. Sicurezza=500.0 vs Clima=1.0). Restituisce (consentito, motivo del rifiuto).
        """
        try:
            events = await self.event_log.get_recent_events()
            motivo = trova_blocco_prevalente(events, target, self.name, self.priority_weight, self.conflict_window_minutes)
            if motivo:
                return False, motivo
        except Exception as e:
            logger.error(f"[{self.name}] Errore durante la verifica dei blocchi di priorità: {e}")

        return True, ""

    async def applica_stato(
        self,
        target: str,
        action: str,
        new_value: str,
        reasoning: str,
        escalated: bool = False,
        tools_map: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Aziona il tool reale e registra la transizione old_value -> new_value su DB, con prevenzione delle
        esecuzioni ridondanti e controllo dei blocchi a priorità superiore.
        Non solleva mai: restituisce un dizionario (vedi app.core.risultati) con `status`, `response` (il motivo in
        chiaro) e, se il dispositivo è guasto, `error_type`, così il padre può fare troubleshooting o escalation.
        L'esito dell'attuazione (`success`) è separato da quello della registrazione su audit log (`audit_logged`).
        """
        old_value = "UNKNOWN"
        tool_obj = trova_tool(target, tools_map)
        dettagli: dict[str, Any] = {}

        def esito(status: str, response: str, **extra: Any) -> dict[str, Any]:
            return nuovo_risultato(
                target, status, response, actor=self.name, action=action,
                old_value=old_value, requested_value=new_value, **extra,
            )

        async def registra(azione_evento: str, valore: Any, motivo: str, escalata: bool = False) -> bool:
            try:
                await self.event_log.log_event(
                    actor=self.name, action=azione_evento, target=target, old_value=old_value,
                    new_value=str(valore), reasoning=motivo, escalated=escalata,
                )
                return True
            except Exception as e:
                logger.error(f"[{self.name}] Errore durante il salvataggio su DB: {e}")
                return False

        # 0. Il comando deve essere ammesso dall'elenco dei dispositivi di configurazione.toml
        validazione = get_configurazione().valida_comando(target, new_value)
        if not validazione.ammesso:
            logger.warning(f"[{self.name}] Comando RIFIUTATO su '{target}': {validazione.motivo}")
            audit = await registra(f"INVALID_COMMAND_{action}", "INVALID", validazione.motivo)
            return esito(COMANDO_NON_AMMESSO, validazione.motivo, audit_logged=audit)
        new_value = validazione.valore

        # 1. Legge il valore attuale reale dal tool
        if tool_obj:
            lettura = await leggi_tool(tool_obj, target)
            if lettura["success"]:
                old_value = str(lettura["value"])
            else:
                dettagli["lettura"] = lettura["response"]
                logger.warning(f"[{self.name}] Lettura stato dal tool {target} fallita: {lettura['response']}")

        # 2. Controllo blocchi a priorità superiore nel DB
        if self.name != NOME_BRAIN and not action.startswith("RECONCILED_") and not action.startswith("UNBLOCKED"):
            allowed, block_msg = await self.check_priority_lock(target)
            if not allowed:
                logger.warning(f"[{self.name}] Azione RESPINTA su '{target}': {block_msg}")
                audit = await registra(
                    f"REJECTED_{action}", "REJECTED", f"Azione respinta da vincolo di priorità superiore: {block_msg}"
                )
                return esito(RESPINTO_PRIORITA, block_msg, audit_logged=audit)

        # 3. Controllo Ridondanza: se il tool è GIÀ al valore desiderato, non rieseguire!
        if old_value.upper() == str(new_value).upper() and not escalated:
            logger.info(f"[{self.name}] Saltata azione su '{target}': valore attuale già a '{old_value}'. In fase di stabilizzazione.")
            return esito(GIA_IMPOSTATO, f"'{target}' è già al valore '{old_value}'.")

        # 4. Azionamento del Tool
        if tool_obj:
            comando = await comanda_tool(tool_obj, target, new_value)
            if not comando["success"]:
                logger.error(f"[{self.name}] Errore durante l'azionamento del tool '{target}': {comando['response']}")
                audit = await registra(
                    f"TOOL_ERROR_{action}", "FAILED", f"Guasto del dispositivo '{target}': {comando['response']}"
                )
                return esito(
                    TOOL_ERRORE, comando["response"], error_type=comando.get("error_type"),
                    details={**dettagli, **(comando.get("details") or {})} or None, audit_logged=audit,
                )
            logger.info(f"[{self.name}] Tool '{target}' azionato: {old_value} -> {new_value}")
        elif not escalated:
            # Senza tool e fuori da un'escalation l'azione non può essere applicata.
            messaggio = f"Tool '{target}' non trovato nella mappa dei tool."
            logger.warning(f"[{self.name}] {messaggio} Azione non applicata.")
            audit = await registra(f"TOOL_ERROR_{action}", "FAILED", messaggio)
            return esito(TOOL_ASSENTE, messaggio, error_type="TOOL_MISSING", audit_logged=audit)

        # 5. Registrazione dell'evento con old_value e new_value nel DB SQLite
        audit = await registra(action, new_value, reasoning, escalated)
        risposta = f"'{target}': {old_value} -> {new_value}."
        if not audit:
            risposta += " Attenzione: registrazione su audit log non riuscita."
        return esito(APPLICATO if tool_obj else SOLO_LOG, risposta, details=dettagli or None, audit_logged=audit)

    async def apply_status(
        self,
        target: str,
        action: str,
        new_value: str,
        reasoning: str,
        escalated: bool = False,
        tools_map: dict[str, Any] | None = None,
    ) -> bool:
        """
        Versione booleana di `applica_stato`: True se l'azione è stata applicata, False se saltata, respinta o fallita.
        Chi deve conoscere il motivo di un fallimento usa `applica_stato`.
        """
        risultato = await self.applica_stato(target, action, new_value, reasoning, escalated, tools_map)
        return risultato["status"] in (APPLICATO, SOLO_LOG)

    def check_for_recent_conflict(self, target: str, recent_events: list[dict], ignore_actor: str | None = None) -> tuple[bool, dict | None]:
        """
        Verifica se ci sono stati eventi recenti su 'target' da parte del Cervello o di agenti a priorità superiore.
        Helper per gli agenti di dominio: il core non lo chiama (i blocchi li decide `trova_blocco_prevalente`).
        Esclude eventi la cui azione è già stata risolta/scaduta o che abbiano superato il TTL.
        """
        from app.core.constants import is_flag_expired

        for event in recent_events:
            if event.get("target") == target:
                action = str(event.get("action", ""))
                # Se l'azione è già marcata come EXPIRED_ o RESOLVED_, ignorala
                if action.startswith("EXPIRED_") or action.startswith("RESOLVED_"):
                    continue

                # Controlla il TTL del timestamp dell'evento
                ts = str(event.get("timestamp", ""))
                if is_flag_expired(ts, ttl_minutes=self.conflict_window_minutes):
                    continue

                actor = event.get("actor")
                if actor and actor != (ignore_actor or self.name):
                    return True, event
        return False, None

    def create_escalation(
        self,
        target_device: str,
        proposed_action: str,
        reason: str,
        conflict_detected: bool = False,
        context_events: list[dict] | None = None,
        tool_result: dict[str, Any] | None = None,
    ):
        """
        Helper per costruire un oggetto EscalationItem pronto da inserire nello stato.
        `tool_result` porta con sé l'esito strutturato di un'attuazione fallita.
        """
        item = EscalationItem(
            source_agent=self.name,
            target_device=target_device,
            proposed_action=proposed_action,
            reason=reason,
            conflict_detected=conflict_detected,
            context_events=context_events or [],
            tool_result=tool_result,
        )
        return item.model_dump()

    async def escala_da_risultato(self, risultato: dict[str, Any], proposed_action: str) -> dict[str, Any]:
        """
        Crea l'escalation per un'attuazione non riuscita. Per un guasto del dispositivo l'escalation porta il
        risultato strutturato (con il motivo) e viene registrata sull'audit log; per una priorità insufficiente
        riporta il vincolo che ha respinto l'azione.
        """
        target = risultato["device_name"]
        guasto = e_guasto_tool(risultato)
        if guasto:
            reason = f"Guasto del dispositivo '{target}' rilevato da {self.name}: {risultato['response']}"
        elif e_comando_non_ammesso(risultato):
            reason = f"Comando di {self.name} su '{target}' non ammesso: {risultato['response']}"
        else:
            reason = f"Azione su {target} bloccata da vincolo di priorità per {self.name}: {risultato['response']}"
        escalation = self.create_escalation(
            target_device=target,
            proposed_action=proposed_action,
            reason=reason,
            conflict_detected=True,
            context_events=[],
            tool_result=risultato if guasto else None,
        )
        if guasto:
            try:
                await self.event_log.log_event(
                    actor=self.name, action="ESCALATION_PROPOSED", target=target,
                    old_value=str(risultato.get("old_value", "UNKNOWN")), new_value=str(proposed_action),
                    reasoning=reason, escalated=True,
                )
            except Exception as e:
                logger.error(f"[{self.name}] Errore salvataggio ESCALATION_PROPOSED su DB: {e}")
        return escalation

    # --- Troubleshooting dei guasti segnalati dai sotto-agenti -------------------------------------------------

    @staticmethod
    def _agenti_che_hanno_tentato(escalation: dict[str, Any]) -> set[str]:
        tentativi = (escalation.get("tool_result") or {}).get("attempts", [])
        return {str(t.get("agent")) for t in tentativi}

    def puo_fare_troubleshooting(self, escalation: dict[str, Any]) -> bool:
        """
        L'agente interviene su un guasto solo se ha priorità almeno pari a quella di chi lo ha segnalato
        (il Brain sempre) e non ha già provato a risolverlo: ogni agente tenta al massimo una volta.
        """
        if not e_guasto_tool(escalation.get("tool_result")):
            return False
        segnalante = str(escalation.get("source_agent"))
        if segnalante == self.name or self.name in self._agenti_che_hanno_tentato(escalation):
            return False
        if self.name == NOME_BRAIN:
            return True
        return self.priority_weight >= (priorita_attore(segnalante) or 0.0)

    async def risolvi_guasto_tool(self, escalation: dict[str, Any], tools_map: dict[str, Any] | None) -> dict[str, Any]:
        """
        Diagnostica un guasto con il modello, a partire dall'errore e dallo storico del dispositivo, ed
        eventualmente ritenta una volta il comando (sottoposto al controllo di priorità dell'agente).
        Restituisce {resolved, diagnosis, decision, tool_result}: `tool_result` è aggiornato con diagnosi e tentativo.
        """
        tool_result = copy.deepcopy(escalation["tool_result"])
        tool_result.setdefault("attempts", [])
        device = tool_result["device_name"]
        valore = tool_result.get("requested_value", escalation.get("proposed_action"))

        try:
            storico = await EventLog(target=[device], frequency=60).get_recent_events()
        except Exception as e:
            logger.warning(f"[{self.name}] Storico del dispositivo '{device}' non disponibile: {e}")
            storico = []
        storico_sintetico = [
            {k: ev.get(k) for k in ("timestamp", "actor", "action", "old_value", "new_value")} for ev in storico[:10]
        ]
        ritenti_falliti = sum(
            1 for t in tool_result["attempts"]
            if t.get("phase") == "troubleshooting" and t.get("status") in ("TOOL_ERROR", "TOOL_MISSING")
        )
        esito = json.dumps({k: v for k, v in tool_result.items() if k != 'attempts'}, ensure_ascii=False, default=str)
        tentativi = json.dumps(tool_result['attempts'], ensure_ascii=False, default=str)
        storico_json = json.dumps(storico_sintetico, ensure_ascii=False, default=str)
        prompt_utente = scegli(
            f"Dispositivo: {device}\n"
            f"Esito del comando fallito: {esito}\n"
            f"Ritenti di troubleshooting già falliti: {ritenti_falliti}\n"
            f"Tentativi già fatti: {tentativi}\n"
            f"Storico recente del dispositivo: {storico_json}\n"
            "Qual è la diagnosi e la decisione?",
            f"Device: {device}\n"
            f"Outcome of the failed command: {esito}\n"
            f"Troubleshooting retries already failed: {ritenti_falliti}\n"
            f"Attempts already made: {tentativi}\n"
            f"Recent history of the device: {storico_json}\n"
            "What is the diagnosis and the decision?"
        )
        risposta = await self.ask_brain(prompt_di_sistema("troubleshooting"), prompt_utente, temperature=0.0, max_tokens=2048)
        diagnosi, decisione = self._interpreta_diagnosi(risposta)
        logger.info(f"[{self.name}] Troubleshooting su '{device}': decisione={decisione}. {diagnosi}")

        tentativo: dict[str, Any] = {"agent": self.name, "phase": "troubleshooting", "decision": decisione}
        risolto = False
        if decisione == "RETRY":
            esito = await self.applica_stato(
                target=device,
                action=f"TROUBLESHOOT_RETRY_{tool_result.get('action', 'ACTION')}",
                new_value=valore,
                reasoning=f"Nuovo tentativo dopo diagnosi: {diagnosi}",
                escalated=False,
                tools_map=tools_map,
            )
            tentativo.update(status=esito["status"], response=esito["response"])
            risolto = bool(esito["success"])
        else:
            tentativo.update(status="NOT_ATTEMPTED", response="Nessun nuovo tentativo: la diagnosi consiglia l'escalation.")
        tool_result["attempts"].append(tentativo)
        tool_result["diagnosis"] = diagnosi

        if risolto:
            try:
                await self.event_log.mark_resolved(device)
            except Exception as e:
                logger.warning(f"[{self.name}] Impossibile chiudere le escalation di '{device}': {e}")
        return {"resolved": risolto, "diagnosis": diagnosi, "decision": decisione, "tool_result": tool_result}

    # Il modello può rispondere in italiano o in inglese: i valori inglesi valgono come quelli italiani.
    _SINONIMI_DECISIONE = {"APPROVE": "APPROVA", "REJECT": "RESPINGI"}

    @staticmethod
    def estrai_decisione(risposta: str, ammesse: tuple[str, ...]) -> str | None:
        """
        Legge `DECISIONE: <valore>` (o `DECISION: <value>`) dalla risposta del modello tollerando il markdown.
        Restituisce il valore canonico in maiuscolo (APPROVE -> APPROVA), oppure None se non è tra `ammesse`.
        """
        pulita = re.sub(r"[*_`]", "", risposta or "")
        sinonimi = {en: it for en, it in BaseAgent._SINONIMI_DECISIONE.items() if it in ammesse}
        valori = "|".join(map(re.escape, [*ammesse, *sinonimi]))
        trovata = re.search(r"DECISIONE?:\s*(" + valori + r")\b", pulita, re.IGNORECASE)
        if not trovata:
            return None
        valore = trovata.group(1).upper()
        return sinonimi.get(valore, valore)

    @staticmethod
    def dividi_in_blocchi(risposta: str, etichetta: str, chiavi: list[str]) -> dict[str, str]:
        """
        Risposta del modello a una sola chiamata per più casi, divisa in un blocco per caso: ogni blocco inizia con la
        riga `<etichetta>: <chiave>` (es. `TARGET: lampada`, `ESCALATION: 2`), tollerando markdown e maiuscole. Con una
        sola chiave la risposta senza blocchi vale per quella (il formato a caso singolo). Un blocco di una chiave
        sconosciuta o ripetuta viene ignorato; una chiave senza blocco resta senza decisione.
        """
        parti = re.split(rf"(?im)^[\s*_`#>-]*{re.escape(etichetta)}\s*:", risposta or "")
        if len(parti) == 1:
            return {chiavi[0]: risposta} if len(chiavi) == 1 else {}
        note = {str(c).casefold(): c for c in chiavi}
        blocchi: dict[str, str] = {}
        for parte in parti[1:]:
            riga, _, resto = parte.partition("\n")
            nome = re.sub(r"[*_`'\"\[\]#]", "", riga).strip().casefold()
            if nome in note and note[nome] not in blocchi:
                blocchi[note[nome]] = resto
        return blocchi

    @staticmethod
    def decisione_non_riconosciuta(risposta: str) -> ErroreLLM:
        """Errore per una risposta senza una decisione valida: il grafo si ferma per l'operatore invece di assumere 'nessuna azione'."""
        return ErroreLLM(
            "RISPOSTA_NON_UTILIZZABILE",
            f"Il modello non ha indicato una decisione riconoscibile: {(risposta or '').strip()[:160]!r}",
        )

    @staticmethod
    def _interpreta_diagnosi(risposta: str) -> tuple[str, str]:
        testo = (risposta or "").strip()
        decisione = "RETRY" if re.search(r"DECISIONE?:\s*RETRY", testo, re.IGNORECASE) else "ESCALATE"
        trovata = re.search(r"DIAGNOS[IS]S?:\s*(.*?)(?:\n\s*DECISIONE?:|$)", testo, re.IGNORECASE | re.DOTALL)
        diagnosi = (trovata.group(1) if trovata else testo).strip() or scegli("Diagnosi non fornita dal modello.",
                                                                              "No diagnosis given by the model.")
        return diagnosi, decisione

    async def ask_brain(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        """
        Invocazione centralizzata del modello tramite MAO.
        Solleva ErroreLLM se il modello non è utilizzabile (errore del provider, crediti, chiave, token, risposta
        vuota) o se dichiara di non poter rispondere: non restituisce mai un errore mascherato da risposta, così
        nessun agente prende decisioni sulla base di un testo d'errore. Il grafo si ferma per l'operatore (HITL).
        """
        sistema_completo = f"{system_prompt}\n\n{istruzione_errore_standard()}"
        if provider is None and model is None:
            # Modello specifico dell'agente, ereditato dal padre se non impostato (vedi app.core.modelli_agenti).
            modello_agente = await risolvi_modello(self.name)
            provider, model = modello_agente.provider, modello_agente.model
        try:
            risposta = await self.mao.call_model(
                sistema_completo, user_prompt, temperature, max_tokens,
                provider=provider, model=model, rigenera_se_troncata=True,
            )
        except ErroreLLM as e:
            logger.error(f"[{self.name}] Modello non utilizzabile ({e.codice}): {e.messaggio}")
            raise
        except Exception as e:
            codice, messaggio = classifica_errore(e)
            logger.error(f"[{self.name}] Errore nell'invocazione del modello ({codice}): {messaggio}")
            raise ErroreLLM(codice, messaggio) from e

        motivo = interpreta_risposta_standard(risposta)
        if motivo is not None:
            logger.error(f"[{self.name}] Il modello ha dichiarato di non poter rispondere: {motivo}")
            raise ErroreLLM("RISPOSTA_NON_UTILIZZABILE", motivo)
        return risposta

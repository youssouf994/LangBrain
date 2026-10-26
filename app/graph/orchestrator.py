import json
import logging
from typing import Any
from langchain_core.messages import AIMessage

from app.agents.base_agent import BaseAgent
from app.core.constants import is_control_flag
from app.core.configurazione import get_configurazione
from app.graph.hitl_config import e_decisione_sistema, flusso_brain_attivo, hitl_manager
from app.core.errori_llm import ErroreLLM
from app.core.lingua import scegli
from app.core.prompt import compila_template, prompt_di_sistema
from app.core.risultati import APPLICATO, e_comando_non_ammesso, e_guasto_tool
from datetime import datetime, timezone
from app.graph.state import GraphState, filtra_segnali, finestra_segnali_ore
from app.tools.reflex import Reflex
from app.tools.effectors import default_effectors, get_tool, tool_registrati
from app.tools.motor_control import force_execute_tool

logger = logging.getLogger(__name__)


def _tool_value_catalog(tools: dict[str, Any] | None = None) -> str:
    """Genera una checklist dei valori validi per gli attuatori in modo da guidare l'LLM."""
    tools = tools or {}
    catalog: list[str] = []
    for name, tool_obj in tools.items():
        elencati = get_configurazione().descrizione_valori(str(name))
        if elencati:
            catalog.append(f"- {name}: {elencati}")
            continue
        device = str(name).lower()
        if "valve" in device or "air" in device:
            valid = ["OPEN", "CLOSED", "100%"]
        elif "breaker" in device:
            valid = ["ON", "OFF"]
        elif "light" in device or "lamp" in device:
            valid = ["ON", "OFF"]
        elif "lock" in device or "door" in device:
            valid = ["LOCKED", "UNLOCKED"]
        elif "alarm" in device:
            valid = ["ARMED", "DISARMED"]
        else:
            current = getattr(tool_obj, "_current_value", None)
            valid = [str(v) for v in [current, "OFF", "ON", "OPEN", "CLOSED", "100%", "22.5°C"] if v not in (None, "")]
        deduped: list[str] = []
        for value in valid:
            if value not in deduped:
                deduped.append(value)
        catalog.append(f"- {name}: {deduped}")
    return "\n".join(catalog) if catalog else "- Nessun tool registrato."


def _normalize_action_value(target: str, action: str, explicit_value: Any = None, fallback_value: Any = None) -> Any:
    """Converte un comando in un valore di dispositivo reale; ignora flag/nomi di azione che non sono stati fisici."""
    action_name = str(action or "").upper()
    if action_name in {"TURN_ON", "TURN_OFF"}:
        return "ON" if action_name == "TURN_ON" else "OFF"

    candidate = explicit_value if explicit_value not in (None, "", "NULL", "null", "NONE", "none") else fallback_value
    candidate_str = str(candidate).strip() if candidate is not None else ""
    if is_control_flag(candidate_str):
        # Flag e nomi di evento interni (es. TOOL_ERROR_*, RECONCILED_*) non sono mai stati fisici del dispositivo.
        candidate_str = ""
    invalid_tokens = {
        "TURN_ON", "TURN_OFF", "FORCE_SHUTDOWN", "SECURITY_LOCK", "UNBLOCK_AND_SET",
        "REJECTED", "RECONCILED", "RESOLVED", "BLOCKED", "ESCALATION_PROPOSED", "NULL", "NONE"
    }
    if candidate_str and candidate_str.upper() not in invalid_tokens:
        return candidate

    device_name = str(target).upper()
    if "VALVE" in device_name or "AIR" in device_name:
        return get_configurazione().valore_attivo(target, default="100%")
    return get_configurazione().valore_attivo(target, default="ON")


class BrainAgent(BaseAgent):
    """
    Orchestratore Supremo (Cervello / Padre).
    Possiede i privilegi massimi, esegue il readout iniziale dei tool e gestisce le escalation.
    """

    def __init__(
        self,
        tools: list[Reflex] | None = None,
        sub_agent_names: list[str] | None = None,
        registered_agent_names: list[str] | None = None,
    ):
        super().__init__(
            name="Brain", 
            managed_targets=["all"], 
            conflict_window_minutes=240, 
            priority_weight=1000.0
        )
        if tools:
            self.tools: dict[str, Reflex] = {tool.target_device: tool for tool in tools}
        else:
            self.tools = default_effectors()
        # Se il Brain viene usato direttamente, conserva il comportamento storico.
        # Il builder passa invece l'elenco effettivo (anche vuoto) dei nodi radice.
        self.sub_agent_names = list(sub_agent_names) if sub_agent_names is not None else ["agent_climate"]
        self.registered_agent_names = (
            list(registered_agent_names)
            if registered_agent_names is not None
            else list(self.sub_agent_names)
        )

        self.tool_value_catalog = _tool_value_catalog(self.tools)

    @property
    def system_prompt(self) -> str:
        """Prompt dell'escalation in vigore (.env, configurazione.toml o default), con il catalogo dei valori."""
        return prompt_di_sistema("brain_escalation", scegli("Catalogo valori validi per i device:", "Catalogue of valid values per device:")
                                 + f"\n{self.tool_value_catalog}")

    async def process(self, 
        state: GraphState, recent_events: list[dict], relevant_readings: list[dict], agent_escalations: list[dict] ) -> dict[str, Any]:
        """
        Ciclo principale del Padre:
        1. Esegue il readout di tutti i tool per aggiornare le letture attuali nel GraphState.
        2. Riconcilia eventuali escalation aperte dei sotto-agenti.
        3. Smista al sotto-agente o termina se il sistema è stabile.
        """
        logger.info(f"[{self.name}] Avvio ciclo Orchestratore Padre (Escalation aperte: {len(agent_escalations)})")
        updates: dict[str, Any] = {}
        
        # --- Readout reale di tutti i tool registrati ---
        current_readings_map = {}
        for device_name, tool_obj in self.tools.items():
            try:
                val = await tool_obj.get_tool_value()
                current_readings_map[device_name] = {
                    "sensor_id": device_name,
                    "agent_owner": self.name,
                    "value": str(val),
                    "unit": getattr(tool_obj, 'unit', ''),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as e:
                logger.error(f"[{self.name}] Errore durante il readout del tool '{device_name}': {e}")

        # Inserisce i segnali freschi nello stato e, se numerici, nella tabella `signals` (storico oltre il run)
        if current_readings_map:
            updates["readings"] = list(current_readings_map.values())
            try:
                await self.event_log.record_signals(updates["readings"])
            except Exception as e:
                logger.error(f"[{self.name}] Segnali non salvati su DB (restano nello stato): {e}")

        pending_escalations = list(state.get("pending_escalations", []))

        # Se non ci sono escalation pendenti nello stato del grafo, verifica conflitti non ancora risolti nel DB
        if not pending_escalations:
            unresolved_events = [
                e for e in recent_events
                if not str(e.get("action", "")).startswith("RECONCILED_")
                and not str(e.get("action", "")).startswith("RESOLVED_")
                and not str(e.get("action", "")).startswith("UNBLOCKED")
                and (str(e.get("action", "")).startswith("FORCE_") or e.get("escalated", False))
            ]
            for ev in unresolved_events:
                pending_escalations.append({
                    "source_agent": ev.get("actor", "agent_security"),
                    "target_device": ev.get("target"),
                    "proposed_action": ev.get("action"),
                    "reason": ev.get("reasoning", "Conflitto non riconciliato nel DB audit log"),
                })

        # --- CASO 1: Gestione Escalation Pendenti / Conflitti DB (Reconciliation) ---
        if pending_escalations:
            logger.info(f"[{self.name}] Inizio Reconciliation per {len(pending_escalations)} escalation/conflitti...")
            resolved_messages = []
            guasti_da_escalare: list[dict[str, Any]] = []

            # 1. Guasti di dispositivo non ancora diagnosticati dal Brain: diagnosi ed eventuale nuovo tentativo.
            da_diagnosticare = [e for e in pending_escalations if self.puo_fare_troubleshooting(e)]
            if da_diagnosticare:
                aggiornamenti: list[dict[str, Any]] = []
                irrisolti: list[str] = []
                for esc in da_diagnosticare:
                    esito = await self.risolvi_guasto_tool(esc, self.tools)
                    if esito["resolved"]:
                        aggiornamenti.append({**esc, "resolved": True})
                        resolved_messages.append(
                            f"[{self.name}] Guasto di '{esc.get('target_device')}' risolto con troubleshooting: {esito['diagnosis']}"
                        )
                    else:
                        aggiornamenti.append({**esc, "tool_result": esito["tool_result"]})
                        irrisolti.append(str(esc.get("target_device")))
                if irrisolti:
                    # Si ripassa dal Brain: la richiesta di intervento umano parte da uno stato già salvato,
                    # così alla ripresa dopo l'interrupt non si ripetono né la diagnosi né il nuovo tentativo.
                    resolved_messages.append(
                        f"[{self.name}] Troubleshooting non risolutivo per {irrisolti}: richiesto l'intervento umano."
                    )
                    return {
                        "pending_escalations": aggiornamenti,
                        "next_agent": "brain",
                        "messages": [AIMessage(content="\n".join(resolved_messages))],
                    }
                risolti = {e.get("id") for e in da_diagnosticare if e.get("id")}
                pending_escalations = [e for e in pending_escalations if e.get("id") not in risolti]

            # Fase 1 — decisioni umane. Tutte le richieste all'operatore (interrupt) vengono raccolte prima di qualsiasi
            # effetto: alla ripresa LangGraph riesegue il nodo dall'inizio, e così non ripete né chiamate al modello né
            # comandi ai dispositivi già fatti per le escalation precedenti.
            casi: list[dict[str, Any]] = []
            for esc in pending_escalations:
                if e_guasto_tool(esc.get("tool_result")):
                    casi.append({"esc": esc, "tipo": "guasto", "intervento": self._chiedi_intervento_umano(esc)})
                    continue
                caso: dict[str, Any] = {"esc": esc, "tipo": "decisione", "risposta": None}
                target = esc.get("target_device")
                # Flusso HITL "brain": [hitl] livello in configurazione.toml
                if flusso_brain_attivo() and self._richiede_approvazione_umana(state, target):
                    decision_val, decision_reason = self._chiedi_approvazione(esc)
                    if "OVERRIDE" in decision_val:
                        logger.warning(f"[{self.name}] OVERRIDE ricevuto. Avvio Arbitrato Semantico per: '{decision_reason}'")
                        caso.update(tipo="override", direttiva=decision_reason)
                    elif e_decisione_sistema(decision_val):
                        # Timer scaduto con azione "sistema": decide il Brain con il suo modello, come senza HITL.
                        logger.warning(f"[{self.name}] Nessuna risposta dell'operatore per '{target}': decide il sistema.")
                    elif "APPROVA" in decision_val or "APPROVE" in decision_val or "YES" in decision_val:
                        caso["risposta"] = f"DECISIONE: APPROVA\nMOTIVAZIONE: {decision_reason}"
                    else:
                        caso["risposta"] = f"DECISIONE: RESPINGI\nMOTIVAZIONE: {decision_reason}"
                casi.append(caso)

            # Fase 2 — una sola chiamata al modello per tutte le escalation che il Brain decide da sé. Ogni decisione
            # viene letta prima di agire: una risposta incompleta non comanda nessun dispositivo e ferma il grafo.
            da_decidere = [c for c in casi if c["tipo"] == "decisione" and c["risposta"] is None]
            if da_decidere:
                await self._decidi_escalation(da_decidere, current_readings_map, recent_events)
            for c in casi:
                if c["tipo"] == "decisione":
                    c["decisione"] = self.estrai_decisione(c["risposta"], ("APPROVA", "RESPINGI"))
                    if c["decisione"] is None:
                        # Una risposta senza decisione non equivale a un rifiuto: si ferma il grafo per l'operatore.
                        raise self.decisione_non_riconosciuta(c["risposta"])

            # Fase 3 — esecuzione, nell'ordine delle escalation.
            for c in casi:
                esc = c["esc"]
                if c["tipo"] == "guasto":
                    resolved_messages.extend(await self._esegui_intervento_umano(esc, *c["intervento"]))
                elif c["tipo"] == "override":
                    resolved_messages.extend(await self._execute_semantic_override(
                        human_directive=c["direttiva"],
                        fallback_target=esc.get("target_device"),
                        fallback_action=esc.get("proposed_action"),
                    ))
                else:
                    await self._applica_decisione(esc, c["decisione"], c["risposta"], resolved_messages, guasti_da_escalare)

            updates["messages"] = [AIMessage(content="\n".join(resolved_messages))]
            if guasti_da_escalare:
                # Le escalation gestite escono dalla coda; i nuovi guasti restano e vengono ripresi al passaggio successivo.
                gestite = [{**e, "resolved": True} for e in pending_escalations if e.get("id")]
                updates["pending_escalations"] = gestite + guasti_da_escalare
                updates["next_agent"] = "brain"
                return updates

            # Svuota le escalation e chiude il ciclo
            updates["pending_escalations"] = []
            updates["next_agent"] = "END"
            return updates

        # --- CASO 2: Avvio da START (Padre che smista verso il sotto-agente) ---
        target_agent = state.get("next_agent", "END")
        target_agent_key = str(target_agent).casefold()
        registered_by_key = {name.casefold(): name for name in self.registered_agent_names}
        root_agents_by_key = {name.casefold(): name for name in self.sub_agent_names}
        if target_agent_key != "end" and target_agent_key != "brain":
            registered_target = registered_by_key.get(target_agent_key)
            if registered_target:
                logger.info(f"[{self.name}] Status check ok. Smistamento verso sotto-agente '{registered_target}'...")
                updates["next_agent"] = registered_target
            else:
                logger.warning(f"[{self.name}] Agente richiesto '{target_agent}' non registrato. Chiusura ciclo.")
                updates["next_agent"] = "END"
        elif target_agent_key == "brain":
            runtime_config = state.get("config", {})
            if runtime_config.get("_hierarchy_visited"):
                # Un agente radice ha terminato ed è risalito al Brain: il ciclo è completo.
                updates["next_agent"] = "END"
            else:
                configured_default = runtime_config.get("default_sub_agent")
                selected = root_agents_by_key.get(str(configured_default).casefold()) if configured_default else None
                updates["next_agent"] = selected or (self.sub_agent_names[0] if self.sub_agent_names else "END")
        else:
            updates["next_agent"] = "END"

        return updates

    def _chiedi_approvazione(self, esc: dict[str, Any]) -> tuple[str, str]:
        """Interrupt di approvazione di un'escalation: restituisce (decisione in maiuscolo, motivazione dell'operatore)."""
        from langgraph.types import interrupt

        target, action = esc.get("target_device"), esc.get("proposed_action")
        source, reason = esc.get("source_agent"), esc.get("reason")
        logger.warning(f"[{self.name}] HITL ATTIVO — Invocazione interrupt(). Grafo in PAUSA per target '{target}' ({action})...")
        human_payload = interrupt({
            "type": "escalation_approval_request",
            "target_device": target,
            "proposed_action": action,
            "source_agent": source,
            "reason": reason,
            "prompt": scegli(f"Approvazione Umana Richiesta per '{target}': l'agente '{source}' propone '{action}'. Motivo: {reason}.",
                             f"Human approval required for '{target}': agent '{source}' proposes '{action}'. Reason: {reason}.")
        })
        if isinstance(human_payload, dict):
            return (str(human_payload.get("decision", "RESPINGI")).upper(),
                    human_payload.get("reasoning", "Decisione fornita dall'utente via HITL API"))
        return str(human_payload).upper(), "Decisione fornita dall'utente via HITL API"

    async def _decidi_escalation(self, casi: list[dict[str, Any]], letture: dict[str, Any], recent_events: list[dict]) -> None:
        """
        Una sola chiamata al modello per tutte le escalation da decidere; scrive in ogni caso la sua parte di risposta
        (`risposta`). Con un'escalation il formato è quello di sempre; con più escalation ognuna è numerata e il
        modello risponde con un blocco per escalation (`ESCALATION: <numero>` seguito da DECISIONE e MOTIVAZIONE).
        """
        def blocco(esc: dict[str, Any], eventi: Any) -> str:
            return compila_template(
                "brain_escalation_utente",
                source=esc.get("source_agent"),
                target=esc.get("target_device"),
                action=esc.get("proposed_action"),
                reason=esc.get("reason"),
                readings=letture.get(esc.get("target_device"), {}),
                recent_events=eventi,
            )

        if len(casi) == 1:
            risposta = await self.ask_brain(self.system_prompt, blocco(casi[0]["esc"], recent_events),
                                            temperature=0.0, max_tokens=2048)
            logger.info(f"[{self.name}] Risoluzione (AI) per {casi[0]['esc'].get('target_device')}: {risposta}")
            casi[0]["risposta"] = risposta
            return

        # Lo storico degli eventi è lo stesso per tutte: compare una volta sola, in fondo.
        vedi_sotto = scegli("(vedi lo storico in fondo)", "(see the history at the end)")
        parti = [f"ESCALATION: {i}\n{blocco(c['esc'], vedi_sotto)}" for i, c in enumerate(casi, start=1)]
        parti.append(scegli("Storico eventi recenti: ", "Recent events: ") + str(recent_events))
        formato = scegli(
            f"Ricevi {len(casi)} escalation numerate. Decidi OGNUNA separatamente: rispondi con un blocco per escalation, "
            "che inizia con la riga `ESCALATION: <numero>` e prosegue con il formato di risposta indicato sopra.",
            f"You receive {len(casi)} numbered escalations. Decide EACH one separately: answer with one block per "
            "escalation, starting with the line `ESCALATION: <number>` and going on with the answer format given above.")
        risposta = await self.ask_brain(f"{self.system_prompt}\n\n{formato}", "\n\n".join(parti),
                                        temperature=0.0, max_tokens=2048)
        logger.info(f"[{self.name}] Risoluzione (AI) di {len(casi)} escalation in una chiamata: {risposta}")
        blocchi = self.dividi_in_blocchi(risposta, "ESCALATION", [str(i) for i in range(1, len(casi) + 1)])
        for i, c in enumerate(casi, start=1):
            c["risposta"] = blocchi.get(str(i), "")

    async def _applica_decisione(self, esc: dict[str, Any], decisione: str, decision_response: str,
                                 resolved_messages: list[str], guasti_da_escalare: list[dict[str, Any]]) -> None:
        """Esegue la decisione (APPROVA o RESPINGI) su un'escalation e ne scrive l'esito nei messaggi."""
        source, target, action = esc.get("source_agent"), esc.get("target_device"), esc.get("proposed_action")
        if decisione == "APPROVA":
            risultato = await self.applica_stato(
                target=target,
                action=f"RECONCILED_{action}",
                new_value=action,   # action è già il valore canonico (es. "22.5°C")
                reasoning=f"Approvato da Orchestratore Padre. Detail: {decision_response}",
                escalated=False,
                tools_map=self.tools
            )
            if e_comando_non_ammesso(risultato):
                # L'approvazione riguarda la riconciliazione: il valore proposto non è un comando ammesso, il dispositivo non cambia.
                await self._chiudi_escalation(target, resolved_messages)
                resolved_messages.append(
                    f"[{self.name}] Escalation APPROVATA per {target} ma NON APPLICATA: {risultato['response']}"
                )
                return
            if e_guasto_tool(risultato):
                # L'approvazione non basta: il dispositivo è guasto. Si passa alla richiesta di intervento umano.
                guasti_da_escalare.append(self.create_escalation(
                    target_device=target,
                    proposed_action=action,
                    reason=f"Guasto del dispositivo '{target}' durante l'attuazione approvata: {risultato['response']}",
                    conflict_detected=True,
                    tool_result=risultato,
                ))
                resolved_messages.append(
                    f"[{self.name}] Escalation APPROVATA per {target} ma il dispositivo è guasto: {risultato['response']}"
                )
                return
            await self._chiudi_escalation(target, resolved_messages)
            status_text = "APPLICATA" if risultato["status"] == APPLICATO else "SALTATA (già a regime)"
            resolved_messages.append(f"[{self.name}] Escalation APPROVATA [{status_text}] per {target} ({action}).")
        else:
            await self.apply_status(
                target=target,
                action=f"REJECTED_{action}",
                new_value="REJECTED",
                reasoning=f"Respinto da Orchestratore Padre. Detail: {decision_response}",
                escalated=False,
                tools_map=self.tools
            )
            await self._chiudi_escalation(target, resolved_messages)
            resolved_messages.append(f"[{self.name}] Escalation RESPINTA per {target} su richiesta di {source}.")

    async def _chiudi_escalation(self, target: str, messaggi: list[str]) -> None:
        """
        Chiude le escalation del target dopo la decisione. Se il database non risponde la decisione resta valida (il
        dispositivo è già stato comandato): il ciclo prosegue e il messaggio avvisa l'operatore che l'escalation è
        rimasta aperta e verrà riproposta al prossimo ciclo.
        """
        try:
            await self.event_log.mark_resolved(target)
        except Exception as e:
            logger.error(f"[{self.name}] Escalation di '{target}' non chiusa: database degli eventi non disponibile ({e}).")
            messaggi.append(scegli(
                f"[{self.name}] ATTENZIONE: escalation di '{target}' decisa ma non chiusa nel database ({e}): verrà riproposta al prossimo ciclo.",
                f"[{self.name}] WARNING: escalation of '{target}' decided but not closed in the database ({e}): it will come back at the next cycle."))

    def _richiede_approvazione_umana(self, state: GraphState, target: str | None) -> bool:
        """
        Flusso HITL "brain": il Brain chiede all'operatore prima di valutare un'escalation se la richiesta è marcata come
        da approvare, se `hitl_all` è attivo, oppure se il dispositivo è protetto: in `hitl_targets` (POST /hitl/config o
        contesto del grafo) o tra i `target_critici_brain` di configurazione.toml.
        """
        impostazioni = hitl_manager.get_config()
        contesto = state.get("config", {})
        protetti = (
            set(impostazioni.hitl_targets) | set(contesto.get("hitl_targets", []))
            | set(get_configurazione().hitl_target_critici_brain)
        )
        return bool(
            state.get("hitl_required", False) or impostazioni.hitl_all or contesto.get("hitl_all", False) or target in protetti
        )

    async def _richiedi_intervento_umano(self, escalation: dict[str, Any]) -> list[str]:
        """Richiesta all'operatore per un guasto non risolto ed esecuzione della sua decisione (vedi le due parti)."""
        return await self._esegui_intervento_umano(escalation, *self._chiedi_intervento_umano(escalation))

    def _chiedi_intervento_umano(self, escalation: dict[str, Any]) -> tuple[str, str, str]:
        """
        Ultima istanza per un guasto che il troubleshooting non ha risolto: mette il grafo in pausa (interrupt) con
        l'esito strutturato e la diagnosi. Con OVERRIDE l'operatore può impartire una direttiva in linguaggio naturale;
        qualsiasi altra decisione prende atto del guasto e chiude l'escalation. Restituisce (dispositivo, decisione,
        motivo) senza effetti: l'esecuzione è in `_esegui_intervento_umano`.
        """
        from langgraph.types import interrupt

        tool_result = escalation.get("tool_result") or {}
        device = str(tool_result.get("device_name") or escalation.get("target_device"))
        diagnosi = tool_result.get("diagnosis", "nessuna diagnosi disponibile")
        logger.warning(f"[{self.name}] Guasto di '{device}' non risolto: richiesto intervento umano.")

        risposta = interrupt({
            "type": "tool_failure_human_intervention",
            "device_name": device,
            "source_agent": escalation.get("source_agent"),
            "tool_result": tool_result,
            "diagnosis": diagnosi,
            "prompt": scegli(
                f"Intervento umano richiesto: il dispositivo '{device}' non risponde ({tool_result.get('response')}). "
                f"Diagnosi: {diagnosi}. Decisioni: OVERRIDE (direttiva nel campo reasoning) oppure altro per prendere atto.",
                f"Human intervention required: device '{device}' does not respond ({tool_result.get('response')}). "
                f"Diagnosis: {diagnosi}. Decisions: OVERRIDE (directive in the reasoning field) or anything else to acknowledge.",
            ),
        })
        decisione = "APPROVA"
        motivo = "Guasto preso in carico dall'operatore via HITL API"
        if isinstance(risposta, dict):
            decisione = str(risposta.get("decision", decisione)).upper()
            motivo = risposta.get("reasoning", motivo)
        elif risposta:
            decisione = str(risposta).upper()

        return device, decisione, motivo

    async def _esegui_intervento_umano(self, escalation: dict[str, Any], device: str, decisione: str,
                                       motivo: str) -> list[str]:
        """Esegue la decisione dell'operatore su un guasto: override, presa d'atto o scadenza del timer."""
        messaggi: list[str] = []
        if e_decisione_sistema(decisione):
            motivo = f"nessun operatore ha risposto entro il timer: guasto non risolto, registrato senza altre azioni ({motivo})"
            messaggi.append(f"[{self.name}] Guasto di '{device}' non risolto: {motivo}")
        elif "OVERRIDE" in decisione:
            messaggi.extend(await self._execute_semantic_override(
                human_directive=motivo, fallback_target=device, fallback_action="UNBLOCK_AND_SET",
            ))
        else:
            messaggi.append(f"[{self.name}] Guasto di '{device}' preso in carico dall'operatore ({decisione}): {motivo}")
        try:
            await self.event_log.log_event(
                actor=self.name, action="TOOL_FAILURE_HANDLED", target=device, old_value="FAILED",
                new_value=decisione, reasoning=f"Intervento umano: {motivo}", escalated=False,
            )
            await self.event_log.mark_resolved(device)
        except Exception as e:
            logger.error(f"[{self.name}] Errore nella chiusura del guasto di '{device}': {e}")
        return messaggi

    async def _execute_semantic_override(
        self,
        human_directive: str,
        fallback_target: str,
        fallback_action: str,
    ) -> list[str]:
        """
        Arbitrato Semantico: traduce la direttiva umana in linguaggio naturale
        in un array JSON di comandi eseguibili via MAO, poi li esegue con force_execute_tool.
        """
        # Il Brain vede anche i tool creati on-demand dopo la sua costruzione (es. via API), non solo la sua copia.
        tool_visibili = {**tool_registrati(), **self.tools}
        catalogo = _tool_value_catalog(tool_visibili)
        override_prompt = scegli(
            f"Direttiva umana da tradurre:\n\"{human_directive}\"\n\n"
            f"Contesto: i dispositivi disponibili nel sistema includono {list(tool_visibili.keys())}.\n"
            f"Catalogo valori validi:\n{catalogo}\n\n"
            "Checklist: target corretto, azione corretta, valore valido per il device, e mai usare il nome dell'azione come stato.\n"
            "Se la direttiva non specifica un dispositivo riconoscibile, usa il dispositivo di fallback: "
            f"\"{fallback_target}\" con azione \"{fallback_action}\".",
            f"Human directive to translate:\n\"{human_directive}\"\n\n"
            f"Context: the devices available in the system include {list(tool_visibili.keys())}.\n"
            f"Catalogue of valid values:\n{catalogo}\n\n"
            "Checklist: right target, right action, valid value for the device, and never an action name as a state.\n"
            "If the directive names no recognisable device, use the fallback device: "
            f"\"{fallback_target}\" with action \"{fallback_action}\".",
        )

        raw_response = ""
        try:
            raw_response = await self.ask_brain(
                prompt_di_sistema("override", scegli("Catalogo valori validi per i tool:", "Catalogue of valid values per tool:")
                                  + f"\n{catalogo}"),
                override_prompt,
                temperature=0.0,
                max_tokens=1024,
            )
            logger.info(f"[{self.name}] Risposta MAO Semantic Override: {raw_response}")
        except ErroreLLM:
            # Senza una risposta valida del modello non si esegue nessun comando: decide l'operatore.
            raise
        except Exception as e:
            logger.error(f"[{self.name}] MAO fallito nell'Arbitrato Semantico: {e}")

        # Pulizia difensiva: rimuove backticks e markdown
        cleaned = raw_response.strip()
        for strip_token in ["```json", "```JSON", "```"]:
            cleaned = cleaned.replace(strip_token, "")
        cleaned = cleaned.strip()

        # Parsing JSON
        # Una risposta che non è un JSON di comandi non autorizza nessuna azione fisica: non si esegue un comando
        # di ripiego, il grafo si ferma e decide l'operatore.
        try:
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError) as je:
            logger.warning(f"[{self.name}] Impossibile parsare JSON dall'Override MAO: {je}. Risposta raw: {cleaned!r}")
            raise ErroreLLM(
                "RISPOSTA_NON_UTILIZZABILE",
                f"La risposta dell'Arbitrato Semantico non è un JSON di comandi valido ({je}). Nessun comando eseguito.",
            ) from je
        commands = parsed if isinstance(parsed, list) else [parsed]
        if not all(isinstance(cmd, dict) for cmd in commands):
            raise ErroreLLM(
                "RISPOSTA_NON_UTILIZZABILE",
                "La risposta dell'Arbitrato Semantico contiene elementi che non sono comandi. Nessun comando eseguito.",
            )

        messages_out = []
        for cmd in commands:
            cmd_target = str(cmd.get("target", fallback_target))
            cmd_action = str(cmd.get("action", "UNBLOCK_AND_SET")).upper()
            cmd_value = cmd.get("value")

            # Un OVERRIDE non può comandare un dispositivo che non è nell'elenco di configurazione.toml
            if not get_configurazione().dispositivo_ammesso(cmd_target):
                rifiuto = f"[Brain_Override] ✗ RIFIUTATO — '{cmd_target}' non è un dispositivo elencato in configurazione.toml"
                logger.warning(rifiuto)
                messages_out.append(rifiuto)
                continue

            # Risolve il tool: usa il registro condiviso oppure ne crea uno on-demand
            tool_obj = tool_visibili.get(cmd_target)
            if tool_obj is None:
                tool_obj = get_tool(cmd_target, initial_value="OFF", unit="")
                self.tools[cmd_target] = tool_obj
                logger.info(f"[Brain_Override] Tool '{cmd_target}' creato on-demand.")

            final_value = _normalize_action_value(cmd_target, cmd_action, cmd_value, fallback_action)

            ok, msg = await force_execute_tool(
                target=cmd_target,
                tool_obj=tool_obj,
                action=cmd_action,
                new_value=final_value,
                reasoning=f"Brain Override: {human_directive}",
                event_log=self.event_log,
            )

            status = "✓ ESEGUITO" if ok else "✗ FALLITO"
            log_msg = f"[Brain_Override] {status} — {cmd_action} su '{cmd_target}' → '{final_value}'"
            logger.info(log_msg)
            messages_out.append(log_msg)

        return messages_out

    async def check_body_status(
        self, 
        state: GraphState, 
        relevant_readings: list[dict], 
        recent_events: list[dict]
    ) -> dict[str, Any]:
        """
        Analisi Periodica Macro (Health Check / Trend della casa).
        Valuta i soli segnali dentro la finestra del run (`config["signals_window_hours"]` dello stato, default 4 ore):
        quelli senza `timestamp` non si possono datare e restano.
        """
        relevant_readings = filtra_segnali(relevant_readings, finestra_segnali_ore(state.get("config")))
        logger.info(f"[{self.name}] Esecuzione check_body_status (Analisi Macro Trend)...")

        flagged_readings = [
            reading
            for reading in relevant_readings
            if is_control_flag(reading.get("value"))
        ]
        if flagged_readings:
            flagged_targets = ", ".join(
                f"{reading.get('sensor_id', 'unknown')}={reading.get('value')}"
                for reading in flagged_readings
            )
            macro_response = (
                "STATUS: MACRO_ADJUSTMENT_REQUIRED\n"
                f"DETTAGLI: flag di controllo presenti ({flagged_targets}); "
                "la risoluzione fisica deve essere implementata dal dominio applicativo."
            )
            return {
                "messages": [AIMessage(content=f"[{self.name}] Macro Check Completato: {macro_response}")]
            }

        system_prompt = prompt_di_sistema("controllo_macro")

        user_prompt = scegli(
            f"Letture reali: {relevant_readings}\nEventi recenti: {recent_events}\nValuta la situazione macro del sistema.",
            f"Real readings: {relevant_readings}\nRecent events: {recent_events}\nAssess the overall state of the system.",
        )

        macro_response = await self.ask_brain(system_prompt, user_prompt, temperature=0.1, max_tokens=2048)
        logger.info(f"[{self.name}] Risultato check_body_status: {macro_response}")

        if "MACRO_ADJUSTMENT_REQUIRED" in macro_response.upper():
            await self.apply_status(
                target="ac_living_room",
                action="MACRO_ECO_MODE",
                new_value="24°C",
                reasoning=f"Regolazione macro da check_body_status: {macro_response}",
                escalated=False,
                tools_map=self.tools
            )

        updates: dict[str, Any] = {
            "messages": [AIMessage(content=f"[{self.name}] Macro Check Completato: {macro_response}")]
        }
        return updates

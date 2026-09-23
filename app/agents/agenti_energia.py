"""
Agenti del dominio energia: la gerarchia che governa la rete elettrica e gas simulata (app.simulazione).

    Brain  (Centro nazionale di controllo, priorità 1000)
    ├── organo_rete_elettrica   leva: import d'emergenza                (600)
    │   ├── componente_accumuli     leva: accumuli in riserva           (300)
    │   └── componente_carichi      leva: carichi interrompibili        (250, chiede sempre al padre)
    └── organo_rete_gas         leva: GNL spot                          (600)
        ├── componente_stoccaggi    leva: stoccaggio strategico         (300)
        └── componente_consumi_gas  leva: interrompibilità del gas      (250, chiede sempre al padre)

Ogni agente legge i propri sensori e l'automatismo della propria leva (tool deterministici: gli apparati della rete
hanno già confrontato i dati con le soglie delle regole e danno un'indicazione) e decide con il modello: ATTIVA,
DISATTIVA, ESCALATE (chiede al padre) o NESSUNA. Il modello segue l'indicazione e se ne discosta solo spiegando quale
rischio le regole non coprono; lo scostamento resta nell'audit log. Arco riflesso: se l'indicazione è NESSUNA e nel
dominio dell'agente non c'è nessuna anomalia, il modello non viene interpellato (nessuna chiamata, nessun costo). Le leve che fermano consumi
industriali non vengono mai azionate dal componente: l'attivazione sale al padre e da lì al Brain, che per queste
leve chiede l'approvazione dell'operatore (flusso HITL "brain").
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage

from app.agents.base_agent import BaseAgent
from app.core.risultati import APPLICATO, GIA_IMPOSTATO
from app.graph.state import GraphState
from app.simulazione.motore import LEVE
from app.tools.sensor_tools import trova_tool
from app.tools.strumenti_energia import testo_regole

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------------- costanti del dominio

# Leve che fermano consumi industriali: servono l'approvazione del Brain e, lì, dell'operatore
LEVE_CRITICHE = ("rete_el_interrompibili", "rete_gas_interrompibili")

SPIEGAZIONE_MARGINE = (
    "Il margine di riserva (margine_riserva_pct) è quello della zona più debole (zona_margine_minimo): capacità ancora "
    "disponibile per quella zona, anche dalle zone vicine attraverso le linee, divisa per la sua domanda. Sotto il 10% "
    "la zona è a rischio di distacco alla prossima punta."
)

FORMATO_RISPOSTA = (
    "Rispondi ESATTAMENTE nel formato:\n"
    "DECISIONE: [ATTIVA|DISATTIVA|ESCALATE|NESSUNA]\n"
    "MOTIVAZIONE: [due o tre frasi con i numeri che hai usato]"
)

REGOLE_COMUNI = (
    "Come decidere:\n"
    "- L'automatismo della tua leva ha già applicato queste regole ai dati e ti dà un'indicazione. I sensori riportano "
    "nelle 'verifiche' i confronti con le soglie già fatti: fidati di quelli, non rifare i confronti a occhio.\n"
    "- Segui l'indicazione. Discostatene solo se i dati mostrano un rischio concreto che le regole non coprono, e in quel "
    "caso scrivi quale nella motivazione: lo scostamento viene registrato e riletto.\n"
    "- Usa solo i numeri dei sensori: non inventare valori.\n"
    "- ESCALATE significa che la tua leva è già accesa e non basta: il livello superiore riceve la richiesta della "
    "misura successiva. Non usarlo per dubbi."
)


@dataclass(frozen=True)
class Ruolo:
    nome: str
    padre: str
    livello: int
    priorita: float
    leva: str
    sensori: tuple[str, ...]
    anomalie: frozenset[str]
    descrizione: str
    regole: str
    chiede_al_padre: bool = False
    figli: tuple[str, ...] = field(default_factory=tuple)
    # Misure che un ESCALATE chiede al padre quando la propria leva è già accesa, dalla meno alla più invasiva:
    # si propone la prima ancora spenta. Se sono tutte accese l'escalation diventa una segnalazione senza comando.
    misure_successive: tuple[str, ...] = field(default_factory=tuple)


GERARCHIA_ENERGIA: tuple[Ruolo, ...] = (
    Ruolo(
        nome="organo_rete_elettrica", padre="Brain", livello=1, priorita=600.0,
        leva="rete_el_import_emergenza", sensori=("sensore_rete_elettrica", "sensore_accumuli"),
        anomalie=frozenset({"distacco_carico", "riserva_bassa"}),
        figli=("componente_accumuli", "componente_carichi"),
        misure_successive=("rete_el_accumuli_riserva", "rete_el_interrompibili"),
        descrizione="l'organo che governa la rete elettrica nazionale: bilancio tra produzione e domanda, riserva, import",
        regole=(
            "- Linee sature o guasti di singole centrali, senza carico non servito, non richiedono l'import d'emergenza."
        ),
    ),
    Ruolo(
        nome="componente_accumuli", padre="organo_rete_elettrica", livello=2, priorita=300.0,
        leva="rete_el_accumuli_riserva", sensori=("sensore_accumuli", "sensore_rete_elettrica"),
        anomalie=frozenset({"distacco_carico", "riserva_bassa"}),
        misure_successive=("rete_el_import_emergenza", "rete_el_interrompibili"),
        descrizione="il componente che gestisce batterie e pompaggi",
        regole=(
            "- In modalità normale (leva OFF) gli accumuli comprano energia quando costa poco e la rivendono quando costa "
            "di più. In riserva (leva ON) si ricaricano e si scaricano solo in emergenza, pronti per la punta difficile."
        ),
    ),
    Ruolo(
        nome="componente_carichi", padre="organo_rete_elettrica", livello=2, priorita=250.0,
        leva="rete_el_interrompibili", sensori=("sensore_rete_elettrica",), chiede_al_padre=True,
        anomalie=frozenset({"distacco_carico", "riserva_bassa"}),
        descrizione="il componente che gestisce i contratti di interrompibilità dei grandi clienti industriali",
        regole=(
            "- Gli interrompibili staccano in modo programmato fino all'8% della domanda di ogni zona, fermando fabbriche: "
            "è molto meglio di un blackout ma ha un costo economico e sociale alto. La tua attivazione diventa sempre una "
            "richiesta al livello superiore, che decide.\n"
            "- Non usare ESCALATE: la tua è l'ultima misura della rete elettrica."
        ),
    ),
    Ruolo(
        nome="organo_rete_gas", padre="Brain", livello=1, priorita=600.0,
        leva="rete_gas_gnl_spot", sensori=("sensore_rete_gas",),
        anomalie=frozenset({"gas_non_servito", "pressione_gas_bassa", "stoccaggio_gas_basso", "crisi_gas"}),
        figli=("componente_stoccaggi", "componente_consumi_gas"),
        misure_successive=("rete_gas_stoccaggio_strategico", "rete_gas_interrompibili"),
        descrizione="l'organo che governa la rete gas: ingressi (gasdotti e rigassificatori), pressione, stoccaggi",
        regole=(
            "- Ingressi in crisi con gas non servito a zero, pressione normale e stoccaggi sopra l'obiettivo sono una "
            "situazione sotto controllo: il GNL spot costa e non serve."
        ),
    ),
    Ruolo(
        nome="componente_stoccaggi", padre="organo_rete_gas", livello=2, priorita=300.0,
        leva="rete_gas_stoccaggio_strategico", sensori=("sensore_rete_gas",),
        anomalie=frozenset({"gas_non_servito", "pressione_gas_bassa", "crisi_gas"}),
        misure_successive=("rete_gas_gnl_spot", "rete_gas_interrompibili"),
        descrizione="il componente che gestisce gli stoccaggi di gas",
        regole=(
            "- Lo stoccaggio strategico fa erogare gli stoccaggi alla portata massima: sostiene la pressione ma consuma la "
            "riserva per il resto dell'inverno."
        ),
    ),
    Ruolo(
        nome="componente_consumi_gas", padre="organo_rete_gas", livello=2, priorita=250.0,
        leva="rete_gas_interrompibili", sensori=("sensore_rete_gas",), chiede_al_padre=True,
        anomalie=frozenset({"gas_non_servito", "pressione_gas_bassa"}),
        descrizione="il componente che gestisce l'interrompibilità dei consumi industriali di gas",
        regole=(
            "- L'interrompibilità riduce del 30% i consumi industriali di gas: protegge le case e le centrali ma ferma "
            "produzioni industriali. La tua attivazione diventa sempre una richiesta al livello superiore, che decide.\n"
            "- Non usare ESCALATE: la tua è l'ultima misura della rete gas."
        ),
    ),
)

TARGET_ENERGIA = tuple(LEVE) + ("sensore_rete_elettrica", "sensore_accumuli", "sensore_rete_gas")

BRAIN_SYSTEM_PROMPT_ENERGIA = (
    "Sei il Brain: il Centro nazionale di controllo della rete elettrica e del gas. Ricevi le richieste degli organi "
    "(rete elettrica, rete gas) e dei loro componenti quando una situazione supera ciò che possono decidere da soli.\n"
    "Leve della rete (tutte ON/OFF):\n"
    + "\n".join(f"- {nome}: {descrizione}" for nome, descrizione in LEVE.items())
    + "\nCriteri:\n"
    "- Proteggi prima le persone (case, ospedali), poi le centrali, per ultima l'industria.\n"
    "- Le leve che fermano consumi industriali (rete_el_interrompibili, rete_gas_interrompibili) si approvano solo se "
    "c'è carico o gas non servito, oppure il rischio è imminente e le leve meno invasive sono già attive o non bastano.\n"
    "- Le richieste sono sempre di accensione. Possono riguardare una leva diversa da quella dell'agente: è la misura "
    "successiva che l'agente chiede quando la sua leva è già accesa e non basta. Approvala solo se i numeri mostrano "
    "energia o gas non servito, o una soglia critica superata, nonostante le misure già attive.\n"
    "- Se la richiesta è ripetuta dopo un tuo rifiuto, approvala solo se i numeri sono peggiorati rispetto ad allora "
    "(energia o gas non servito, margine o pressione più bassi).\n"
    "- Usa solo i numeri che ricevi.\n"
    "Valuta la richiesta e decidi se APPROVARLA o RESPINGERLA.\n"
    "Formato Risposta:\n"
    "DECISIONE: [APPROVA|RESPINGI]\n"
    "MOTIVAZIONE: [spiegazione con i numeri]"
)

BRAIN_USER_PROMPT_ENERGIA = (
    "Agente richiedente: {source}\n"
    "Leva: {target}\n"
    "Valore proposto: {action}\n"
    "Motivo e situazione della rete: {reason}\n"
    "Stato attuale della leva: {readings}\n"
    "Eventi recenti su queste leve: {recent_events}\n"
    "Qual è la risoluzione corretta?"
)


# ---------------------------------------------------------------------------------------------- agente

class AgenteEnergia(BaseAgent):
    """Agente del dominio energia: organo o componente, secondo il `Ruolo`."""

    def __init__(self, ruolo: Ruolo, tools: dict[str, Any]):
        super().__init__(name=ruolo.nome, managed_targets=[ruolo.leva], conflict_window_minutes=60, priority_weight=ruolo.priorita)
        self.ruolo = ruolo
        self.parent_agent_name = ruolo.padre
        self.sub_agent_names = list(ruolo.figli)
        self.level = ruolo.livello
        self.tools = tools
        self.system_prompt = (
            f"Sei '{ruolo.nome}' (livello {ruolo.livello}), {ruolo.descrizione}, in una gerarchia di agenti che governa la "
            "rete energetica italiana simulata. Sopra di te c'è "
            f"{'il Brain (Centro nazionale di controllo)' if ruolo.padre == 'Brain' else repr(ruolo.padre)}.\n"
            f"La tua leva è '{ruolo.leva}': {LEVE[ruolo.leva]}\n"
            + (f"{SPIEGAZIONE_MARGINE}\n" if ruolo.leva.startswith("rete_el") else "")
            + f"Contesto:\n{ruolo.regole}\n"
            + f"Regole della tua leva, in ordine (vale la prima che si applica):\n{testo_regole(ruolo.leva)}\n"
            + f"{REGOLE_COMUNI}\n"
            + (f"Con la tua leva già accesa, ESCALATE chiede al livello superiore, nell'ordine: {', '.join(ruolo.misure_successive)}.\n"
               if ruolo.misure_successive else "")
            + FORMATO_RISPOSTA
        )

    async def check_priority_lock(self, target: str) -> tuple[bool, str]:
        """
        Nel dominio energia OFF è uno stato normale di una leva, non un blocco: la regola generale del framework (un
        evento OFF o REJECTED di un attore più importante blocca il dispositivo per una finestra di tempo reale) qui
        creerebbe veti su valori mai vietati e scaduti in ore reali. I veti sono espliciti, per valore e in tempo
        simulato (`Simulatore.veti`), e li gestisce `process`.
        """
        return True, ""

    def _padre(self) -> str:
        return "brain" if self.ruolo.padre.casefold() == "brain" else self.ruolo.padre

    async def _leggi(self, nome: str) -> Any:
        strumento = trova_tool(nome, self.tools)
        valore = await strumento.get_tool_value()
        try:
            return json.loads(valore)
        except (TypeError, ValueError):
            return valore

    @staticmethod
    def _serve_il_modello(valutazione: dict[str, Any], anomalie: list[dict[str, Any]]) -> bool:
        """Arco riflesso: se l'automatismo indica NESSUNA e nel dominio non ci sono anomalie, non si chiede nulla."""
        return valutazione["indicazione"] != "NESSUNA" or bool(anomalie)

    async def process(self, state: GraphState, recent_events: list[dict], relevant_readings: list[dict],
                      agent_escalations: list[dict]) -> dict[str, Any]:
        from app.simulazione.esecutore import esecutore  # import locale: evita il ciclo con strumenti_energia

        config = dict(state.get("config", {}))
        visitati = list(config.get("_hierarchy_visited", []))
        if self.name not in visitati:
            visitati.append(self.name)
        config["_hierarchy_visited"] = visitati

        # Prima si visitano i figli, una volta per ciclo; al ritorno l'organo decide sulla propria leva.
        figlio = next((f for f in self.sub_agent_names if f not in visitati), None)
        if figlio:
            return {"next_agent": figlio, "config": config,
                    "messages": [AIMessage(content=f"[{self.name}] Delega l'analisi a {figlio}.")]}

        leva = self.ruolo.leva
        stato_leva = str(await self._leggi(leva)).upper()
        sensori = {nome: await self._leggi(nome) for nome in self.ruolo.sensori}
        sim = esecutore.simulatore()
        anomalie = [a for a in sim.anomalie if a["tipo"] in self.ruolo.anomalie]
        veto_attuale = sim.veto(leva)
        valutazione = trova_tool(leva, self.tools).valuta()
        inoltrate = [e for e in agent_escalations if e.get("source_agent") != self.name]
        prefisso = f"[{self.name}] " + (f"Inoltra al padre {len(inoltrate)} richieste dei figli. " if inoltrate else "")

        if not self._serve_il_modello(valutazione, anomalie):
            return {"next_agent": self._padre(), "config": config,
                    "messages": [AIMessage(content=(
                        f"{prefisso}Leva {leva} {stato_leva}, automatismo: NESSUNA, nessuna anomalia: nessuna decisione "
                        "(arco riflesso, nessuna chiamata al modello)."))]}

        prompt = (
            f"Ora simulata: {sim.tempo.isoformat(timespec='minutes')}\n"
            f"La tua leva '{leva}' è: {stato_leva}\n"
            f"Stato di tutte le leve della rete: {json.dumps(sim.leve)}\n"
            + "".join(f"Sensore {nome}: {json.dumps(valore, ensure_ascii=False)}\n" for nome, valore in sensori.items())
            + f"Anomalie del tuo dominio: {json.dumps([f'{a['descrizione']} (da {a['da_ore']} h)' for a in anomalie], ensure_ascii=False)}\n"
            + (f"Richieste già inviate al padre dai tuoi sotto-agenti: {json.dumps([e.get('reason', '')[:200] for e in inoltrate], ensure_ascii=False)}\n" if inoltrate else "")
            + (f"Veto sulla tua leva: non portarla a {veto_attuale['valore_vietato']} fino a {veto_attuale['fino']} "
               f"(deciso da {veto_attuale['autore']}).\n" if veto_attuale else "")
            + f"Costo delle misure finora: {round(sim.cumulativi['costo_misure_eur'])} €\n"
            + f"Indicazione dell'automatismo della tua leva: {valutazione['indicazione']} (regola: {valutazione['regola']}).\n"
            "Qual è la decisione corretta?"
        )
        risposta = await self.ask_brain(self.system_prompt, prompt, temperature=0.0, max_tokens=2048)
        decisione = self.estrai_decisione(risposta, ("ATTIVA", "DISATTIVA", "ESCALATE", "NESSUNA"))
        if decisione is None:
            raise self.decisione_non_riconosciuta(risposta)
        motivazione = risposta.split("MOTIVAZIONE:", 1)[-1].strip(" *\n") if "MOTIVAZIONE:" in risposta.upper() else risposta.strip()
        aggiornamento: dict[str, Any] = {"next_agent": self._padre(), "config": config}
        if decisione != valutazione["indicazione"]:
            # Il modello si discosta dall'automatismo: si registra, così lo scostamento si può rileggere e valutare.
            await self.event_log.log_event(
                actor=self.name, action="SCOSTAMENTO_AUTOMATISMO", target=leva, old_value=valutazione["indicazione"],
                new_value=decisione, reasoning=f"Regola dell'automatismo: {valutazione['regola']}. Motivazione: {motivazione[:800]}",
            )
            prefisso += f"(Si discosta dall'automatismo, che indicava {valutazione['indicazione']}.) "

        valore = {"ATTIVA": "ON", "DISATTIVA": "OFF"}.get(decisione)
        richiesta = None
        if decisione == "ESCALATE":
            # Leva spenta: si chiede di accenderla. Leva già accesa: si chiede la prima misura successiva ancora spenta.
            # Le leve fissate dall'operatore non si propongono: il Brain approverebbe scavalcandolo.
            def proponibile(m: str) -> bool:
                veto = sim.veto(m, "ON")
                return sim.leve.get(m) != "ON" and not (veto and veto["autore"] == "operatore")
            candidate = ((leva,) if stato_leva != "ON" else ()) + self.ruolo.misure_successive
            richiesta = next((m for m in candidate if proponibile(m)), None)
        elif decisione == "ATTIVA" and stato_leva != "ON" and (self.ruolo.chiede_al_padre or sim.veto(leva, "ON")):
            veto = sim.veto(leva, "ON")
            if veto and veto["autore"] != "Brain":
                aggiornamento["messages"] = [AIMessage(content=(
                    f"{prefisso}Vorrebbe accendere {leva}, ma l'operatore l'ha fissata {stato_leva} fino a {veto['fino']}: "
                    f"nessuna azione. {motivazione[:300]}"))]
                return aggiornamento
            richiesta = leva  # con un veto del Brain si torna a chiedere, con i numeri di adesso
        situazione = "; ".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in sensori.items())
        if richiesta:
            gia_respinta = sim.veto(richiesta, "ON")
            ripetuta = (f" Richiesta ripetuta: il Brain aveva respinto l'accensione (veto fino a {gia_respinta['fino']});"
                        " valuta i numeri di adesso." if gia_respinta else "")
            automatismo = trova_tool(richiesta, self.tools).valuta()
            motivo = (f"{self.name} chiede {richiesta} = ON.{ripetuta} {motivazione[:600]} Automatismo di {richiesta}: "
                      f"{automatismo['indicazione']} ({automatismo['regola']}). Situazione: {situazione[:900]}")
            aggiornamento["pending_escalations"] = [self.create_escalation(richiesta, "ON", motivo, conflict_detected=True)]
            await self.event_log.log_event(
                actor=self.name, action="ESCALATION_PROPOSED", target=richiesta, old_value=sim.leve.get(richiesta, "OFF"),
                new_value="ON", reasoning=motivo[:1000], escalated=True,
            )
            aggiornamento["messages"] = [AIMessage(content=f"{prefisso}Chiede al padre {richiesta} = ON: {motivazione[:400]}")]
        elif decisione == "ESCALATE":
            # Tutte le misure a disposizione sono già accese: non c'è un comando da chiedere, resta una segnalazione.
            await self.event_log.log_event(
                actor=self.name, action="SEGNALAZIONE", target=leva, old_value=stato_leva, new_value=stato_leva,
                reasoning=f"Misure esaurite. {motivazione[:600]} Situazione: {situazione[:600]}",
            )
            aggiornamento["messages"] = [AIMessage(content=f"{prefisso}Segnalazione al padre: tutte le misure sono già attive. {motivazione[:400]}")]
        elif valore and valore != stato_leva and (veto := sim.veto(leva, valore)):
            aggiornamento["messages"] = [AIMessage(content=(
                f"{prefisso}Vorrebbe portare {leva} a {valore}, ma {veto['autore']} l'ha vietato fino a {veto['fino']}: "
                f"nessuna azione. {motivazione[:300]}"))]
        elif valore and valore != stato_leva:
            risultato = await self.applica_stato(leva, f"LEVA_{valore}", valore, f"{self.name}: {motivazione[:800]}", tools_map=self.tools)
            if risultato["status"] == APPLICATO:
                testo = f"{prefisso}Leva {leva}: {stato_leva} -> {valore}. {motivazione[:400]}"
            elif risultato["status"] == GIA_IMPOSTATO:
                testo = f"{prefisso}Leva {leva} già {valore}."
            else:
                aggiornamento["pending_escalations"] = [await self.escala_da_risultato(risultato, proposed_action=valore)]
                testo = f"{prefisso}Leva {leva} non azionata ({risultato['response']}): richiesta al padre."
            aggiornamento["messages"] = [AIMessage(content=testo)]
        else:
            aggiornamento["messages"] = [AIMessage(content=f"{prefisso}{decisione}: leva {leva} resta {stato_leva}. {motivazione[:400]}")]
        return aggiornamento


def crea_agenti_energia(tools: dict[str, Any]) -> dict[str, AgenteEnergia]:
    return {ruolo.nome: AgenteEnergia(ruolo, tools) for ruolo in GERARCHIA_ENERGIA}

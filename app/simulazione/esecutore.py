"""
Esecuzione della simulazione in background, dentro il processo dell'API.

La velocità è in secondi simulati per secondo reale (3600 = un'ora simulata al secondo); 0 vuol dire "massima":
il motore corre quanto può. I passi vengono eseguiti a blocchi di al massimo `BLOCCO_SECONDI` di calcolo, poi il
ciclo cede il controllo al loop asincrono: l'API resta reattiva anche alla velocità massima.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from app.simulazione.motore import PASSI_AMMESSI_MINUTI, Simulatore
from app.simulazione.scenari import scenario_generato, scenario_italia

logger = logging.getLogger(__name__)

BLOCCO_SECONDI = 0.04
VELOCITA_PREDEFINITA = 3600.0
VELOCITA_MASSIMA_IMPOSTABILE = 365 * 24 * 3600.0  # un anno simulato al secondo
LIMITI_RETE_GENERATA = {"zone": (2, 300), "citta": (1, 20000), "centrali": (1, 10000)}


class EsecutoreSimulazione:
    def __init__(self) -> None:
        self.sim: Simulatore | None = None
        self.parametri: dict[str, Any] = {}
        self.velocita = VELOCITA_PREDEFINITA
        self.stato = "pronta"          # pronta | in_corso | in_pausa | finita
        self.versione = 0              # cresce a ogni blocco di passi, evento o cambio di stato
        self.versione_rete = 0         # cresce quando cambia lo scenario (la topologia va riletta)
        self._compito: asyncio.Task | None = None
        self._ancora: tuple[float, int] | None = None  # (istante reale, passo) da cui si misura la velocità
        self._prestazioni: dict[str, Any] = {}
        self._finestra: list[tuple[float, int, float]] = []  # (istante, passi, secondi di calcolo)

    # ------------------------------------------------------------------------------------------ configurazione

    def simulatore(self) -> Simulatore:
        if self.sim is None:
            self.configura()
        return self.sim

    def configura(
        self,
        tipo: str = "italia",
        seed: int = 42,
        durata_ore: float = 8760,
        passo_minuti: int = 60,
        inizio: str | None = None,
        zone: int = 20,
        citta: int = 200,
        centrali: int = 120,
    ) -> Simulatore:
        """Crea una nuova simulazione (ferma quella in corso). Solleva ValueError per parametri non validi."""
        if tipo not in ("italia", "generata"):
            raise ValueError("Tipo di scenario non valido: 'italia' oppure 'generata'.")
        if passo_minuti not in PASSI_AMMESSI_MINUTI:
            raise ValueError(f"Passo non ammesso: {passo_minuti} minuti (ammessi: {list(PASSI_AMMESSI_MINUTI)}).")
        try:
            data_inizio = datetime.fromisoformat(inizio) if inizio else datetime(2026, 1, 1)
        except ValueError as e:
            raise ValueError(f"Data di inizio non valida: {inizio!r} (formato AAAA-MM-GG o AAAA-MM-GGTHH:MM).") from e
        if tipo == "generata":
            for nome, valore in (("zone", zone), ("citta", citta), ("centrali", centrali)):
                minimo, massimo = LIMITI_RETE_GENERATA[nome]
                if not minimo <= int(valore) <= massimo:
                    raise ValueError(f"'{nome}' deve essere tra {minimo} e {massimo}.")
            scenario = scenario_generato(zone, citta, centrali, seed)
        else:
            scenario = scenario_italia()
        self._ferma_compito()
        self.sim = Simulatore(scenario, seed=seed, inizio=data_inizio, passo_minuti=passo_minuti, durata_ore=durata_ore)
        self.parametri = {
            "tipo": tipo, "seed": seed, "durata_ore": durata_ore, "passo_minuti": passo_minuti,
            "inizio": data_inizio.isoformat(timespec="minutes"),
            **({"zone": zone, "citta": citta, "centrali": centrali} if tipo == "generata" else {}),
        }
        self.stato = "pronta"
        self._prestazioni = {}
        self._finestra = []
        self.versione += 1
        self.versione_rete += 1
        logger.info("[Simulazione] Nuovo scenario '%s': %d entità, %d passi.", scenario.nome, scenario.numero_entita(), self.sim.passi_totali)
        return self.sim

    def imposta_velocita(self, velocita: float) -> None:
        if not 0 <= velocita <= VELOCITA_MASSIMA_IMPOSTABILE:
            raise ValueError(f"Velocità non valida: da 0 (massima) a {VELOCITA_MASSIMA_IMPOSTABILE:.0f} secondi simulati al secondo.")
        self.velocita = float(velocita)
        self._ancora = None  # il ciclo riparte dal punto attuale con la nuova velocità
        self.versione += 1

    # ------------------------------------------------------------------------------------------ comandi

    def avvia(self) -> None:
        sim = self.simulatore()
        if sim.finita:
            self.stato = "finita"
            return
        if self._compito is None or self._compito.done():
            self.stato = "in_corso"
            self._compito = asyncio.get_running_loop().create_task(self._ciclo())
        self.versione += 1

    def pausa(self) -> None:
        if self.stato == "in_corso":
            self._ferma_compito()
            self.stato = "in_pausa"
            self.versione += 1

    def passo_singolo(self, passi: int = 1) -> int:
        """Avanza a mano di `passi` passi (solo a simulazione ferma)."""
        if self.stato == "in_corso":
            raise ValueError("La simulazione è in corso: mettila in pausa prima di avanzare a mano.")
        sim = self.simulatore()
        eseguiti = sim.avanza(max(1, min(int(passi), 10000)))
        self.stato = "finita" if sim.finita else ("in_pausa" if sim.passo else "pronta")
        self.versione += 1
        return eseguiti

    def inietta_evento(self, tipo: str, entita: str | None, durata_ore: float | None) -> dict[str, Any]:
        evento = self.simulatore().inietta_evento(tipo, entita, durata_ore)
        self.versione += 1
        return evento

    def _ferma_compito(self) -> None:
        if self._compito is not None and not self._compito.done():
            self._compito.cancel()
        self._compito = None

    async def chiudi(self) -> None:
        compito = self._compito
        self._ferma_compito()
        if compito is not None:
            try:
                await compito
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------------------------------ ciclo

    async def _ciclo(self) -> None:
        sim = self.sim
        self._ancora = None
        try:
            while not sim.finita:
                if self._ancora is None:
                    self._ancora = (time.monotonic(), sim.passo)
                inizio_blocco = time.perf_counter()
                if self.velocita == 0:
                    dovuti = 10 ** 9
                else:
                    istante, passo_ancora = self._ancora
                    trascorsi = (time.monotonic() - istante) * self.velocita / (sim.passo_minuti * 60)
                    dovuti = passo_ancora + int(trascorsi) - sim.passo
                    if dovuti <= 0:
                        prossimo = (1 - (trascorsi % 1)) * sim.passo_minuti * 60 / self.velocita
                        await asyncio.sleep(min(0.05, max(0.001, prossimo)))
                        continue
                eseguiti = 0
                while eseguiti < dovuti and not sim.finita and time.perf_counter() - inizio_blocco < BLOCCO_SECONDI:
                    sim.avanza(1)
                    eseguiti += 1
                calcolo = time.perf_counter() - inizio_blocco
                self._misura(eseguiti, calcolo, in_ritardo=self.velocita > 0 and eseguiti < dovuti)
                self.versione += 1
                await asyncio.sleep(0.004 if self.velocita == 0 else 0.001)
            self.stato = "finita"
            self.versione += 1
            logger.info("[Simulazione] Terminata dopo %d passi.", sim.passo)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[Simulazione] Errore nel ciclo di simulazione: messa in pausa.")
            self.stato = "in_pausa"
            self._prestazioni["errore"] = "Errore interno del motore: vedi il log del server."
            self.versione += 1

    def _misura(self, passi: int, calcolo: float, in_ritardo: bool) -> None:
        adesso = time.monotonic()
        self._finestra.append((adesso, passi, calcolo))
        self._finestra = [f for f in self._finestra if adesso - f[0] <= 2.0]
        durata = max(1e-6, adesso - self._finestra[0][0]) if len(self._finestra) > 1 else max(calcolo, 1e-6)
        totale_passi = sum(f[1] for f in self._finestra)
        totale_calcolo = sum(f[2] for f in self._finestra)
        passi_al_secondo = totale_passi / durata
        self._prestazioni = {
            "passi_al_secondo": round(passi_al_secondo, 1),
            "ms_per_passo": round(1000 * totale_calcolo / totale_passi, 3) if totale_passi else None,
            "ore_simulate_al_secondo": round(passi_al_secondo * self.sim.dt, 2),
            "in_ritardo": in_ritardo,
        }

    # ------------------------------------------------------------------------------------------ viste

    def stato_esecuzione(self) -> dict[str, Any]:
        sim = self.simulatore()
        return {
            "stato": self.stato, "velocita": self.velocita, "versione": self.versione, "versione_rete": self.versione_rete,
            "parametri": self.parametri, "passo": sim.passo, "passi_totali": sim.passi_totali,
            "avanzamento": round(sim.passo / sim.passi_totali, 5), "tempo": sim.tempo.isoformat(timespec="minutes"),
            "entita": sim.scenario.numero_entita(), "prestazioni": dict(self._prestazioni),
        }


esecutore = EsecutoreSimulazione()

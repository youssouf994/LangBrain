"""
Motore della simulazione energetica.

A ogni passo (5, 15, 30 o 60 minuti simulati):
  1. meteo e prezzi avanzano con processi casuali correlati nel tempo (seed fisso: la storia è ripetibile);
  2. eventi casuali: guasti di centrali, linee, condotte e accumuli, crisi di fornitura gas, ondate di calore e di
     freddo, calme di vento, picchi del prezzo del gas;
  3. domanda elettrica delle città (ora, giorno, stagione, temperatura) e disponibilità degli impianti;
  4. dispacciamento elettrico in ordine di merito sulla rete delle zone, con i limiti di transito delle linee; gli
     accumuli si scaricano quando conviene e si ricaricano con l'energia a basso costo; la domanda non coperta è
     distacco di carico;
  5. dispacciamento del gas (città, industria e centrali a gas) con ingressi, stoccaggi e linepack; il gas mancante
     riduce le centrali a gas al passo successivo (accoppiamento tra le due reti);
  6. anomalie, statistiche cumulative, serie storiche.

La stessa combinazione di scenario, seed e passo produce sempre la stessa storia (salvo eventi iniettati a mano).
"""

import math
import random
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from app.simulazione import ambiente
from app.simulazione.modello import KWH_PER_SM3, TECNOLOGIE, Scenario

EPS = 1e-6
CO2_GAS_T_MWH_TERMICO = 0.202  # t di CO2 per MWh termico di gas bruciato
PASSI_AMMESSI_MINUTI = (5, 15, 30, 60)
COSTO_DISTACCO = 3000.0       # €/MWh, tetto del prezzo quando manca energia
DURATA_MASSIMA_ORE = 100 * 8760

CATEGORIE_PRODUZIONE = ("solare", "eolico", "idro", "gas", "carbone", "altre", "import", "accumuli")
_CATEGORIA = {
    "solare": "solare", "eolico": "eolico", "idro_fluente": "idro", "idro_bacino": "idro", "gas": "gas",
    "carbone": "carbone", "nucleare": "altre", "geotermico": "altre", "biomasse": "altre", "import": "import",
}
_A_COSTO_ZERO = {"solare", "eolico", "idro_fluente"}
_TIPO_TECNOLOGIA = {"import_emergenza": "import"}
_PROGRAMMABILI = {"gas", "carbone", "nucleare", "idro_bacino", "import", "biomasse", "geotermico"}

# Leve operative: comandi che l'operatore (o un agente) può dare alla rete. Tutte ON/OFF, spente all'inizio.
LEVE = {
    "rete_el_interrompibili": "Carichi industriali interrompibili: in ogni zona fino all'8% della domanda si stacca in modo "
                              "programmato a 400 €/MWh, prima di arrivare al distacco incontrollato.",
    "rete_el_import_emergenza": "Import d'emergenza: le interconnessioni con l'estero salgono dal 78% al 100% della capacità; "
                                "la parte in più costa 40 €/MWh sopra l'import normale.",
    "rete_el_accumuli_riserva": "Accumuli in riserva: si ricaricano appena possono e si scaricano solo in emergenza "
                                "(prezzo oltre 350 €/MWh), invece di fare arbitraggio sul prezzo.",
    "rete_gas_interrompibili": "Interrompibilità del gas: i consumi industriali di gas si riducono del 30% in modo programmato.",
    "rete_gas_stoccaggio_strategico": "Stoccaggio strategico: gli stoccaggi erogano alla portata massima qualunque sia il "
                                      "riempimento, anche fuori stagione, intaccando la riserva.",
    "rete_gas_gnl_spot": "GNL spot: i rigassificatori salgono al 135% della portata abituale comprando carichi sul mercato "
                         "spot, a costo più alto.",
}
VETO_ORE = 6.0                 # durata simulata di un veto su una leva (rifiuto del Brain o comando dell'operatore)
SOGLIA_RISERVA_BASSA = 0.10   # margine di zona sotto cui scatta l'anomalia di riserva bassa
QUOTA_INTERROMPIBILI = 0.08
COSTO_INTERROMPIBILI = 400.0
SOVRAPPREZZO_IMPORT_EMERGENZA = 40.0
DISPONIBILITA_IMPORT = 0.78
RIDUZIONE_GAS_INTERROMPIBILI = 0.30
FATTORE_GNL_SPOT = 1.35

TIPI_EVENTO = {
    "guasto": "Guasto di una centrale, linea, condotta o accumulo (entità obbligatoria)",
    "crisi_gas": "Riduzione della fornitura di un ingresso gas (entità: un ingresso)",
    "ondata_calore": "Temperature più alte di 5-8 °C su tutte le zone",
    "ondata_freddo": "Temperature più basse di 5-8 °C su tutte le zone",
    "calma_vento": "Vento ridotto al 30% su tutte le zone",
    "picco_prezzo_gas": "Prezzo del gas moltiplicato per 1,8-2,6, in rientro graduale",
    "ripristino": "Fine anticipata del guasto o della crisi di un'entità",
}


class _Offerta:
    """Quantità offerta a un costo in una zona; `unita` sono le entità che la compongono con la loro disponibilità."""
    __slots__ = ("costo", "zona", "quantita", "usato", "unita", "tipo")

    def __init__(self, costo: float, zona: int, quantita: float, unita: list, tipo: str):
        self.costo, self.zona, self.quantita, self.usato, self.unita, self.tipo = costo, zona, quantita, 0.0, unita, tipo


class _Trasporto:
    """Grafo delle zone con capacità sugli archi; instrada le quantità con percorsi aumentanti (ricerca in ampiezza)."""

    def __init__(self, zone: int, archi: list[tuple[int, int]]):
        self.zone = zone
        self.adiacenza: list[list[tuple[int, int, int]]] = [[] for _ in range(zone)]
        for k, (u, v) in enumerate(archi):
            self.adiacenza[u].append((k, v, 1))
            self.adiacenza[v].append((k, u, -1))
        self.capacita = [0.0] * len(archi)
        self.flusso = [0.0] * len(archi)

    def prepara(self, capacita: list[float]) -> None:
        self.capacita = capacita
        self.flusso = [0.0] * len(capacita)

    def instrada(self, origine: int, fabbisogno: list[float], quantita: float) -> tuple[float, int]:
        """Porta fino a `quantita` dalla zona di origine alla zona più vicina (in salti) con fabbisogno residuo."""
        padre: list[tuple[int, int, int] | None] = [None] * self.zone
        visitata = [False] * self.zone
        visitata[origine] = True
        coda = deque([origine])
        destinazione = -1
        while coda and destinazione < 0:
            u = coda.popleft()
            for k, v, verso in self.adiacenza[u]:
                if visitata[v] or self.capacita[k] - verso * self.flusso[k] <= EPS:
                    continue
                visitata[v] = True
                padre[v] = (u, k, verso)
                if fabbisogno[v] > EPS:
                    destinazione = v
                    break
                coda.append(v)
        if destinazione < 0:
            return 0.0, -1
        collo, v = min(quantita, fabbisogno[destinazione]), destinazione
        while v != origine:
            u, k, verso = padre[v]
            collo = min(collo, self.capacita[k] - verso * self.flusso[k])
            v = u
        v = destinazione
        while v != origine:
            u, k, verso = padre[v]
            self.flusso[k] += verso * collo
            v = u
        return collo, destinazione


def _dispaccia(trasporto: _Trasporto, offerte: list[_Offerta], fabbisogno: list[float], limite_costo: float = math.inf) -> float:
    """
    Copre il fabbisogno delle zone con le offerte in ordine di costo (già ordinate), prima nella zona dell'offerta e
    poi, se avanza, nelle altre zone raggiungibili entro i limiti di transito. Restituisce il costo marginale.
    """
    residuo = sum(fabbisogno)
    marginale = 0.0
    for offerta in offerte:
        if residuo <= EPS or offerta.costo > limite_costo:
            break
        disponibile = offerta.quantita - offerta.usato
        if disponibile <= EPS:
            continue
        z = offerta.zona
        consegnato = 0.0
        if fabbisogno[z] > EPS:
            q = min(disponibile, fabbisogno[z])
            fabbisogno[z] -= q
            disponibile -= q
            consegnato += q
        while disponibile > EPS and residuo - consegnato > EPS:
            q, destinazione = trasporto.instrada(z, fabbisogno, disponibile)
            if q <= EPS:
                break
            fabbisogno[destinazione] -= q
            disponibile -= q
            consegnato += q
        if consegnato > EPS:
            offerta.usato += consegnato
            residuo -= consegnato
            marginale = offerta.costo
    return marginale


def _ar1(valore: float, rho: float, sigma: float, rumore: float) -> float:
    return rho * valore + math.sqrt(max(0.0, 1 - rho * rho)) * sigma * rumore


class Simulatore:
    def __init__(
        self,
        scenario: Scenario,
        seed: int = 42,
        inizio: datetime | None = None,
        passo_minuti: int = 60,
        durata_ore: float = 24 * 7,
    ):
        if passo_minuti not in PASSI_AMMESSI_MINUTI:
            raise ValueError(f"Passo non ammesso: {passo_minuti} minuti (ammessi: {PASSI_AMMESSI_MINUTI}).")
        if not 0 < durata_ore <= DURATA_MASSIMA_ORE:
            raise ValueError(f"Durata non valida: {durata_ore} ore (da più di 0 a {DURATA_MASSIMA_ORE}).")
        self.scenario = scenario
        self.seed = int(seed)
        self.rng = random.Random(self.seed)
        self.inizio = inizio or datetime(2026, 1, 1)
        self.passo_minuti = passo_minuti
        self.dt = passo_minuti / 60
        self.durata_ore = float(durata_ore)
        self.passi_totali = max(1, math.ceil(durata_ore * 60 / passo_minuti))
        self.passo = 0
        self.tempo = self.inizio

        s = scenario
        self.indice_zona = {z.id: i for i, z in enumerate(s.zone)}
        self.entita: dict[str, tuple[str, Any]] = {}
        for categoria, elenco in (
            ("zona", s.zone), ("centrale", s.centrali), ("citta", s.citta), ("accumulo", s.accumuli), ("linea", s.linee),
            ("ingresso", s.ingressi), ("stoccaggio", s.stoccaggi), ("condotta", s.condotte),
        ):
            for e in elenco:
                self.entita[e.id] = (categoria, e)

        self._zona_centrale = [self.indice_zona[c.zona] for c in s.centrali]
        self._zona_citta = [self.indice_zona[c.zona] for c in s.citta]
        self._zona_accumulo = [self.indice_zona[a.zona] for a in s.accumuli]
        self._trasporto_el = _Trasporto(len(s.zone), [(self.indice_zona[l.da], self.indice_zona[l.a]) for l in s.linee])
        self._trasporto_gas = _Trasporto(len(s.zone), [(self.indice_zona[c.da], self.indice_zona[c.a]) for c in s.condotte])
        self._norma_carico = self._calibra_carico()

        # Stato casuale: anomalie meteo per zona, rumore del carico, prezzi.
        n = len(s.zone)
        self._anomalia_temp = [0.0] * n
        self._anomalia_nuvole = [0.0] * n
        self._anomalia_vento = [0.0] * n
        self._rumore_carico = [0.0] * n
        self._log_prezzo_gas = 0.0
        self._rumore_import = 0.0
        self.prezzo_co2 = 70.0
        self.prezzo_gas = 35.0
        self._moltiplicatore_gas = 1.0
        self._scostamento_temp = 0.0
        self._fattore_vento = 1.0
        self._prezzo_medio = 100.0  # media mobile a 24 ore del prezzo elettrico: riferimento per gli accumuli
        self.leve: dict[str, str] = {nome: "OFF" for nome in LEVE}
        # Veti sulle leve: {leva: {"valore_vietato", "fino_passo", "fino", "autore", "motivo"}}, in tempo simulato.
        self.veti: dict[str, dict[str, Any]] = {}

        doy = ambiente.giorno_anno(self.inizio)
        for z in s.zone:
            z.linepack = 0.7 * z.linepack_max
            z.pressione = self._pressione(z)
        for c in s.centrali:
            c.riserva_bacino_mwh = 0.6 * c.bacino_mwh
        for a in s.accumuli:
            a.carica_mwh = 0.5 * a.capacita_mwh
        for st in s.stoccaggi:
            st.giacenza = st.capacita * ambiente.obiettivo_stoccaggio_gas(doy)

        self.eventi_attivi: list[dict[str, Any]] = []
        self.storico_eventi: deque[dict[str, Any]] = deque(maxlen=2000)
        self._contatore_eventi = 0
        self.anomalie: list[dict[str, Any]] = []
        self._inizio_anomalie: dict[str, int] = {}

        self.nazionale: dict[str, Any] = {}
        self.cumulativi: dict[str, Any] = {
            "domanda_mwh": 0.0, "servita_mwh": 0.0, "distacco_mwh": 0.0, "ore_con_distacco": 0.0,
            "produzione_mwh": {k: 0.0 for k in CATEGORIE_PRODUZIONE}, "taglio_rinnovabili_mwh": 0.0, "co2_t": 0.0,
            "costo_energia_eur": 0.0, "gas_domanda_msm3": 0.0, "gas_non_servito_msm3": 0.0,
            "gas_ingressi_msm3": {"gasdotto": 0.0, "gnl": 0.0, "produzione": 0.0}, "gas_stoccaggio_netto_msm3": 0.0,
            "eventi_per_tipo": {}, "carico_massimo_linee": 0.0, "prezzo_massimo": 0.0,
            "interrotto_mwh": 0.0, "gas_interrotto_msm3": 0.0, "costo_misure_eur": 0.0, "azionamenti_leve": 0,
        }
        passi_7_giorni = int(7 * 24 / self.dt)
        self.serie_passi: deque[dict[str, Any]] = deque(maxlen=passi_7_giorni)
        self.serie_giorni: list[dict[str, Any]] = []
        self._giorno_corrente: dict[str, Any] | None = None
        self.storico_entita: dict[str, deque] = {eid: deque(maxlen=24 * 14) for eid in self.entita}

    # ------------------------------------------------------------------------------------------ calibrazione

    def _calibra_carico(self) -> list[float]:
        """
        Fattore per zona che rende uguale a 1 la media annua di forma giornaliera × calendario × effetto della
        temperatura: così la domanda media di ogni città resta quella dello scenario.
        """
        zone = self.scenario.zone
        somme = [0.0] * len(zone)
        campioni = 0
        base = datetime(2026, 1, 1)
        for giorno in range(0, 365, 2):
            for ora in range(24):
                t = base + timedelta(days=giorno, hours=ora)
                comune = ambiente.forma_giornaliera(t) * ambiente.fattore_calendario(t)
                doy = giorno + 1 + ora / 24
                for i, z in enumerate(zone):
                    temp = ambiente.temperatura_base(z.temp_media, z.temp_ampiezza, z.temp_giornaliera, doy, ora)
                    somme[i] += comune * ambiente.effetto_temperatura_elettrico(temp)
                campioni += 1
        return [campioni / s if s > 0 else 1.0 for s in somme]

    # ------------------------------------------------------------------------------------------ passo

    @property
    def finita(self) -> bool:
        return self.passo >= self.passi_totali

    def avanza(self, passi: int = 1) -> int:
        """Esegue fino a `passi` passi (si ferma alla fine della durata). Restituisce quanti ne ha eseguiti."""
        eseguiti = 0
        while eseguiti < passi and not self.finita:
            self._passo()
            eseguiti += 1
        return eseguiti

    def _passo(self) -> None:
        t = self.tempo
        doy = ambiente.giorno_anno(t)
        ora = t.hour + t.minute / 60
        self._aggiorna_meteo_e_prezzi(t, doy, ora)
        self._eventi_casuali(t)
        consumo_termico = self._passo_elettrico(t, doy, ora)
        self._passo_gas(t, doy, consumo_termico)
        self._aggiorna_anomalie()
        self._registra(t)
        self.passo += 1
        self.tempo = self.inizio + timedelta(minutes=self.passo * self.passo_minuti)

    def _aggiorna_meteo_e_prezzi(self, t: datetime, doy: float, ora: float) -> None:
        rng, dt = self.rng, self.dt
        rho_temp, rho_nuvole, rho_vento = math.exp(-dt / 60), math.exp(-dt / 14), math.exp(-dt / 20)
        comune_temp, comune_nuvole, comune_vento = rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1)
        for i, z in enumerate(self.scenario.zone):
            self._anomalia_temp[i] = _ar1(self._anomalia_temp[i], rho_temp, 2.4, 0.8 * comune_temp + 0.6 * rng.gauss(0, 1))
            self._anomalia_nuvole[i] = _ar1(self._anomalia_nuvole[i], rho_nuvole, 0.28, 0.6 * comune_nuvole + 0.8 * rng.gauss(0, 1))
            self._anomalia_vento[i] = _ar1(self._anomalia_vento[i], rho_vento, 0.5, 0.7 * comune_vento + 0.71 * rng.gauss(0, 1))
            self._rumore_carico[i] = _ar1(self._rumore_carico[i], math.exp(-dt / 6), 0.02, rng.gauss(0, 1))
            z.temperatura = ambiente.temperatura_base(z.temp_media, z.temp_ampiezza, z.temp_giornaliera, doy, ora) \
                + self._anomalia_temp[i] + self._scostamento_temp
            z.nuvole = min(1.0, max(0.0, ambiente.nuvole_stagionali(z.nuvole_inverno, z.nuvole_estate, doy) + self._anomalia_nuvole[i]))
            z.vento = ambiente.vento_stagionale(z.vento_medio, doy) * math.exp(self._anomalia_vento[i] - 0.125) * self._fattore_vento

        self._log_prezzo_gas = _ar1(self._log_prezzo_gas, math.exp(-dt / (24 * 20)), 0.18, rng.gauss(0, 1))
        stagione = 1 + 0.12 * math.cos(2 * math.pi * (doy - 20) / 365.25)
        self._moltiplicatore_gas = 1 + (self._moltiplicatore_gas - 1) * math.exp(-dt / (24 * 12))
        self.prezzo_gas = 35.0 * stagione * math.exp(self._log_prezzo_gas) * self._moltiplicatore_gas
        self.prezzo_co2 = min(200.0, max(20.0, self.prezzo_co2 + rng.gauss(0, 0.15) * math.sqrt(dt) + (70 - self.prezzo_co2) * 0.0005 * dt))
        self._rumore_import = _ar1(self._rumore_import, math.exp(-dt / 12), 8.0, rng.gauss(0, 1))

    # ------------------------------------------------------------------------------------------ eventi

    def _nuovo_evento(self, tipo: str, entita: str | None, durata_ore: float, descrizione: str, **extra: Any) -> dict[str, Any]:
        self._contatore_eventi += 1
        durata_passi = max(1, round(durata_ore / self.dt))
        evento = {
            "id": self._contatore_eventi, "tipo": tipo, "entita": entita, "descrizione": descrizione,
            "inizio": self.tempo.isoformat(timespec="minutes"),
            "fine": (self.tempo + timedelta(hours=durata_passi * self.dt)).isoformat(timespec="minutes"),
            "fine_passo": self.passo + durata_passi, "attivo": True, **extra,
        }
        self.eventi_attivi.append(evento)
        self.storico_eventi.append(evento)
        conteggio = self.cumulativi["eventi_per_tipo"]
        conteggio[tipo] = conteggio.get(tipo, 0) + 1
        return evento

    def _eventi_casuali(self, t: datetime) -> None:
        rng, dt = self.rng, self.dt
        # Chiusura degli eventi scaduti
        ancora = []
        for e in self.eventi_attivi:
            if self.passo >= e["fine_passo"]:
                self._chiudi_evento(e)
            else:
                ancora.append(e)
        self.eventi_attivi = ancora

        for c in self.scenario.centrali:
            if c.guasto_fino > self.passo:
                continue
            tecnologia = TECNOLOGIE[c.tipo]
            if rng.random() < tecnologia.guasti_per_ora * dt:
                perdita = rng.uniform(0.1, 0.3) if c.flotta else 1.0
                durata = max(2.0, rng.expovariate(1 / tecnologia.riparazione_ore))
                self._guasto(c, perdita, durata)
        for linea in self.scenario.linee:
            if linea.guasto_fino <= self.passo and rng.random() < dt / 4000:
                self._guasto(linea, rng.uniform(0.4, 1.0), rng.uniform(4, 36))
        for condotta in self.scenario.condotte:
            if condotta.guasto_fino <= self.passo and rng.random() < dt / 15000:
                self._guasto(condotta, rng.uniform(0.3, 0.8), rng.uniform(6, 72))
        for a in self.scenario.accumuli:
            if a.guasto_fino <= self.passo and rng.random() < dt / 15000:
                self._guasto(a, 1.0, rng.uniform(6, 72))
        for ingresso in self.scenario.ingressi:
            if ingresso.riduzione_fino <= self.passo and ingresso.tipo != "produzione" and rng.random() < dt / 9000:
                self._crisi_gas(ingresso, rng.uniform(0.2, 0.6), rng.uniform(48, 336))

        attivi = {e["tipo"] for e in self.eventi_attivi}
        if t.month in (6, 7, 8) and "ondata_calore" not in attivi and rng.random() < dt / 700:
            self._ondata("ondata_calore", rng.uniform(5, 8), rng.uniform(96, 240))
        if t.month in (12, 1, 2) and "ondata_freddo" not in attivi and rng.random() < dt / 900:
            self._ondata("ondata_freddo", -rng.uniform(5, 8), rng.uniform(72, 168))
        if "calma_vento" not in attivi and rng.random() < dt / 1200:
            self._calma_vento(rng.uniform(48, 120))
        if "picco_prezzo_gas" not in attivi and rng.random() < dt / 6000:
            self._picco_gas(rng.uniform(1.8, 2.6), 24 * 20)

    def _guasto(self, oggetto: Any, perdita: float, durata_ore: float) -> dict[str, Any]:
        oggetto.guasto_fino = self.passo + max(1, round(durata_ore / self.dt))
        if hasattr(oggetto, "perdita_guasto"):
            oggetto.perdita_guasto = perdita
        nome = getattr(oggetto, "nome", oggetto.id)
        quota = f"{round(perdita * 100)}% della capacità" if perdita < 1 else "fuori servizio"
        return self._nuovo_evento("guasto", oggetto.id, durata_ore, f"Guasto: {nome} ({quota})", perdita=round(perdita, 3))

    def _crisi_gas(self, ingresso: Any, residua: float, durata_ore: float) -> dict[str, Any]:
        ingresso.riduzione = residua
        ingresso.riduzione_fino = self.passo + max(1, round(durata_ore / self.dt))
        return self._nuovo_evento(
            "crisi_gas", ingresso.id, durata_ore, f"Crisi di fornitura: {ingresso.nome} al {round(residua * 100)}%",
        )

    def _ondata(self, tipo: str, scostamento: float, durata_ore: float) -> dict[str, Any]:
        self._scostamento_temp = scostamento
        nome = "Ondata di calore" if scostamento > 0 else "Ondata di freddo"
        return self._nuovo_evento(tipo, None, durata_ore, f"{nome}: {scostamento:+.1f} °C su tutte le zone", scostamento=scostamento)

    def _calma_vento(self, durata_ore: float) -> dict[str, Any]:
        self._fattore_vento = 0.3
        return self._nuovo_evento("calma_vento", None, durata_ore, "Anticiclone: vento al 30% su tutte le zone")

    def _picco_gas(self, moltiplicatore: float, durata_ore: float) -> dict[str, Any]:
        self._moltiplicatore_gas = moltiplicatore
        return self._nuovo_evento(
            "picco_prezzo_gas", None, durata_ore, f"Picco del prezzo del gas: ×{moltiplicatore:.1f}, in rientro graduale",
        )

    def _chiudi_evento(self, evento: dict[str, Any]) -> None:
        evento["attivo"] = False
        tipo = evento["tipo"]
        if tipo in ("ondata_calore", "ondata_freddo"):
            self._scostamento_temp = 0.0
        elif tipo == "calma_vento":
            self._fattore_vento = 1.0
        elif tipo == "crisi_gas":
            _, ingresso = self.entita[evento["entita"]]
            ingresso.riduzione, ingresso.riduzione_fino = 1.0, -1
        elif tipo == "guasto":
            _, oggetto = self.entita[evento["entita"]]
            oggetto.guasto_fino = -1
            if hasattr(oggetto, "perdita_guasto"):
                oggetto.perdita_guasto = 0.0

    def inietta_evento(self, tipo: str, entita: str | None = None, durata_ore: float | None = None) -> dict[str, Any]:
        """Evento imposto dall'operatore (o, in futuro, da un agente). Solleva ValueError se non applicabile."""
        if tipo not in TIPI_EVENTO:
            raise ValueError(f"Tipo di evento sconosciuto: '{tipo}'. Ammessi: {', '.join(TIPI_EVENTO)}.")
        if durata_ore is not None and not 0 < durata_ore <= 24 * 365:
            raise ValueError("La durata dell'evento deve essere tra 0 e 8760 ore.")
        if tipo in ("guasto", "crisi_gas", "ripristino"):
            if entita not in self.entita:
                raise ValueError(f"Entità sconosciuta: '{entita}'.")
            categoria, oggetto = self.entita[entita]
            if tipo == "ripristino":
                chiusi = [e for e in self.eventi_attivi if e["entita"] == entita]
                if not chiusi:
                    raise ValueError(f"Nessun guasto o crisi attiva su '{entita}'.")
                for e in chiusi:
                    self._chiudi_evento(e)
                self.eventi_attivi = [e for e in self.eventi_attivi if e["entita"] != entita]
                return self._nuovo_evento("ripristino", entita, self.dt, f"Ripristino: {getattr(oggetto, 'nome', entita)}")
            if tipo == "crisi_gas":
                if categoria != "ingresso":
                    raise ValueError("La crisi di fornitura si applica solo a un ingresso gas.")
                self._chiudi_attivi(entita)
                return self._crisi_gas(oggetto, 0.3, durata_ore or 24 * 7)
            if categoria not in ("centrale", "linea", "condotta", "accumulo"):
                raise ValueError("Il guasto si applica a una centrale, linea, condotta o accumulo.")
            self._chiudi_attivi(entita)
            return self._guasto(oggetto, 1.0, durata_ore or 24)
        self._chiudi_attivi(tipo=tipo)
        if tipo == "ondata_calore":
            return self._ondata(tipo, 7.0, durata_ore or 24 * 6)
        if tipo == "ondata_freddo":
            return self._ondata(tipo, -7.0, durata_ore or 24 * 5)
        if tipo == "calma_vento":
            return self._calma_vento(durata_ore or 24 * 3)
        return self._picco_gas(2.2, durata_ore or 24 * 20)

    def imposta_leva(self, nome: str, valore: str) -> dict[str, Any]:
        """Accende o spegne una leva operativa. Solleva ValueError per nomi o valori non validi."""
        if nome not in LEVE:
            raise ValueError(f"Leva sconosciuta: '{nome}'. Ammesse: {', '.join(LEVE)}.")
        valore = str(valore).strip().upper()
        if valore not in ("ON", "OFF"):
            raise ValueError(f"Valore non valido per '{nome}': '{valore}' (ammessi ON e OFF).")
        precedente = self.leve[nome]
        self.leve[nome] = valore
        registro = {
            "id": None, "tipo": "leva", "entita": nome, "attivo": False,
            "descrizione": f"Leva {nome}: {precedente} -> {valore}",
            "inizio": self.tempo.isoformat(timespec="minutes"), "fine": self.tempo.isoformat(timespec="minutes"),
        }
        if precedente != valore:
            self.cumulativi["azionamenti_leve"] += 1
            self.storico_eventi.append(registro)
        return registro

    def imposta_veto(self, leva: str, valore_vietato: str, autore: str, motivo: str = "", ore: float = VETO_ORE) -> dict[str, Any]:
        """Vieta di portare `leva` a `valore_vietato` per `ore` simulate (rifiuto del Brain, comando dell'operatore)."""
        if leva not in LEVE:
            raise ValueError(f"Leva sconosciuta: '{leva}'.")
        passi = max(1, round(ore / self.dt))
        veto = {
            "valore_vietato": valore_vietato.upper(), "fino_passo": self.passo + passi, "autore": autore, "motivo": motivo[:300],
            "fino": (self.tempo + timedelta(hours=passi * self.dt)).isoformat(timespec="minutes"),
        }
        self.veti[leva] = veto
        return veto

    def veto(self, leva: str, valore: str | None = None) -> dict[str, Any] | None:
        """Il veto attivo sulla leva (se `valore` è indicato, solo se vieta proprio quel valore)."""
        veto = self.veti.get(leva)
        if veto is None or veto["fino_passo"] <= self.passo:
            self.veti.pop(leva, None)
            return None
        if valore is not None and veto["valore_vietato"] != str(valore).upper():
            return None
        return veto

    def portata_massima(self, ingresso: Any) -> float:
        """Portata massima attuale di un ingresso gas (crisi di fornitura e GNL spot comprese)."""
        spot = FATTORE_GNL_SPOT if ingresso.tipo == "gnl" and self.leve["rete_gas_gnl_spot"] == "ON" else 1.0
        return ingresso.portata_max * ingresso.riduzione * spot

    def _chiudi_attivi(self, entita: str | None = None, tipo: str | None = None) -> None:
        restano = []
        for e in self.eventi_attivi:
            if (entita and e["entita"] == entita) or (tipo and e["tipo"] == tipo):
                self._chiudi_evento(e)
            else:
                restano.append(e)
        self.eventi_attivi = restano

    # ------------------------------------------------------------------------------------------ elettricità

    def _costo(self, tipo: str, rendimento: float = 0.0, ora: float = 12.0) -> float:
        if tipo in _A_COSTO_ZERO:
            return 0.0
        if tipo == "geotermico":
            return 5.0
        if tipo == "nucleare":
            return 12.0
        if tipo == "biomasse":
            return 25.0
        if tipo == "gas":
            rendimento = rendimento or TECNOLOGIE["gas"].rendimento
            return (self.prezzo_gas + self.prezzo_co2 * CO2_GAS_T_MWH_TERMICO) / rendimento + 4.0
        if tipo == "carbone":
            return 12.0 / 0.38 + self.prezzo_co2 * 0.90 + 3.0
        if tipo == "import":
            # All'estero l'energia costa meno di notte (nucleare francese) e a metà giornata (solare).
            forma = -14.0 if ora < 6 or ora >= 23 else (-6.0 if 11 <= ora < 15 else 4.0)
            return max(30.0, 0.45 * (self.prezzo_gas / 0.55 + self.prezzo_co2 * 0.37) + 45.0 + forma + self._rumore_import)
        raise ValueError(tipo)

    def _disponibilita(self, c: Any, zona: Any, doy: float, ora: float, t: datetime) -> float:
        if c.guasto_fino > self.passo and c.perdita_guasto >= 1.0:
            return 0.0
        tecnologia = TECNOLOGIE[c.tipo]
        c.in_manutenzione = tecnologia.manutenzione and not c.flotta and 0 <= doy - c.giorno_manutenzione < 14
        if c.in_manutenzione:
            return 0.0
        tipo = c.tipo
        if tipo == "solare":
            fattore = ambiente.fattore_solare(zona.y, doy, ora, zona.nuvole)
        elif tipo == "eolico":
            v = zona.vento
            fattore = (ambiente.fattore_eolico(0.75 * v) + ambiente.fattore_eolico(v) + ambiente.fattore_eolico(1.25 * v)) / 3
        elif tipo == "idro_fluente":
            fattore = ambiente.fattore_idro_fluente(doy)
        elif tipo == "idro_bacino":
            fattore = min(1.0, c.riserva_bacino_mwh / (c.potenza_mw * self.dt)) if c.potenza_mw else 0.0
        elif tipo == "geotermico":
            fattore = 0.9
        elif tipo == "biomasse":
            fattore = 0.55
        elif tipo == "nucleare":
            fattore = 0.95
        elif tipo == "gas":
            fattore = zona.fattore_gas
        elif tipo == "import":
            fattore = 1.0 if self.leve["rete_el_import_emergenza"] == "ON" else DISPONIBILITA_IMPORT
        else:
            fattore = 1.0
        if c.guasto_fino > self.passo:
            fattore *= 1 - c.perdita_guasto
        return c.potenza_mw * fattore

    def _passo_elettrico(self, t: datetime, doy: float, ora: float) -> list[float]:
        s, dt, n = self.scenario, self.dt, len(self.scenario.zone)
        forma = ambiente.forma_giornaliera(t) * ambiente.fattore_calendario(t)

        fattore_zona = [
            self._norma_carico[i] * forma * ambiente.effetto_temperatura_elettrico(z.temperatura) * (1 + self._rumore_carico[i])
            for i, z in enumerate(s.zone)
        ]
        fabbisogno = [0.0] * n
        for i, c in enumerate(s.citta):
            zi = self._zona_citta[i]
            c.domanda_mw = c.domanda_media_mw * fattore_zona[zi]
            fabbisogno[zi] += c.domanda_mw
        for i, z in enumerate(s.zone):
            z.domanda_mw = fabbisogno[i]
        domanda = sum(fabbisogno)

        # Offerte aggregate per (zona, tecnologia, rendimento): le singole unità si spartiscono poi la produzione.
        gruppi: dict[tuple[int, str, float], list] = {}
        for i, c in enumerate(s.centrali):
            zi = self._zona_centrale[i]
            disponibile = self._disponibilita(c, s.zone[zi], doy, ora, t)
            c.disponibile_mw = disponibile
            c.produzione_mw = 0.0
            gruppi.setdefault((zi, c.tipo, c.rendimento), []).append((c, disponibile))
        offerte: list[_Offerta] = []
        costi = {tipo: self._costo(tipo, ora=ora) for tipo in TECNOLOGIE if tipo not in ("idro_bacino", "gas")}
        rendimenti_gas: dict[int, float] = {}
        for (zi, tipo, rendimento), unita in gruppi.items():
            quantita = sum(d for _, d in unita)
            if quantita <= EPS:
                continue
            if tipo == "idro_bacino":
                capienza = sum(c.bacino_mwh for c, _ in unita) or 1.0
                livello = sum(c.riserva_bacino_mwh for c, _ in unita) / capienza
                costo = 45.0 + 110.0 * (1 - livello) ** 1.5
            elif tipo == "gas":
                costo = self._costo(tipo, rendimento)
            else:
                costo = costi[tipo]
            if tipo == "import" and self.leve["rete_el_import_emergenza"] == "ON":
                # La capacità oltre quella abituale è un'offerta separata, più cara.
                base = [(c, d * DISPONIBILITA_IMPORT) for c, d in unita]
                extra = [(c, d - b) for (c, d), (_, b) in zip(unita, base)]
                offerte.append(_Offerta(costo, zi, sum(b for _, b in base), base, tipo))
                offerte.append(_Offerta(costo + SOVRAPPREZZO_IMPORT_EMERGENZA, zi, sum(e for _, e in extra), extra, "import_emergenza"))
                continue
            offerta = _Offerta(costo, zi, quantita, unita, tipo)
            if tipo == "gas":
                rendimenti_gas[id(offerta)] = rendimento or TECNOLOGIE["gas"].rendimento
            offerte.append(offerta)

        gruppi_accumulo: dict[tuple[int, str], list] = {}
        for i, a in enumerate(s.accumuli):
            a.potenza_attuale_mw = 0.0
            if a.guasto_fino > self.passo:
                continue
            gruppi_accumulo.setdefault((self._zona_accumulo[i], a.tipo), []).append(a)
        offerte_accumulo: dict[tuple[int, str], _Offerta] = {}
        riserva = self.leve["rete_el_accumuli_riserva"] == "ON"
        for (zi, tipo), unita in gruppi_accumulo.items():
            disponibili = [(a, min(a.potenza_mw, a.carica_mwh * math.sqrt(a.rendimento) / dt)) for a in unita]
            quantita = sum(d for _, d in disponibili)
            if quantita <= EPS:
                continue
            livello = sum(a.carica_mwh for a in unita) / (sum(a.capacita_mwh for a in unita) or 1.0)
            # Si scaricano quando il prezzo supera la media recente, tanto più facilmente quanto più sono carichi;
            # in riserva solo in emergenza.
            costo = 350.0 if riserva else self._prezzo_medio * (1.0 + 0.2 * (1 - livello) ** 2)
            offerta = _Offerta(costo, zi, quantita, disponibili, tipo)
            offerte.append(offerta)
            offerte_accumulo[(zi, tipo)] = offerta
        if self.leve["rete_el_interrompibili"] == "ON":
            for i, z in enumerate(s.zone):
                if z.domanda_mw > EPS:
                    offerte.append(_Offerta(COSTO_INTERROMPIBILI, i, QUOTA_INTERROMPIBILI * z.domanda_mw, [(z, 1.0)], "interrompibili"))
        offerte.sort(key=lambda o: o.costo)

        self._trasporto_el.prepara([
            l.capacita_mw * ((1 - l.perdita_guasto) if l.guasto_fino > self.passo else 1.0) for l in s.linee
        ])
        prezzo = _dispaccia(self._trasporto_el, offerte, fabbisogno)
        distacco_zona = fabbisogno
        distacco = sum(distacco_zona)
        if distacco > EPS:
            prezzo = COSTO_DISTACCO
        peso = 1 - math.exp(-dt / 24)
        self._prezzo_medio += peso * (min(prezzo, 300.0) - self._prezzo_medio)

        # Ricarica degli accumuli con l'energia che costa meno della loro offerta d'acquisto.
        ricariche: list[tuple[float, int, str, list]] = []
        for (zi, tipo), unita in gruppi_accumulo.items():
            offerta = offerte_accumulo.get((zi, tipo))
            if offerta is not None and offerta.usato > EPS:
                continue  # un gruppo che si sta scaricando non si ricarica nello stesso passo
            spazio = [(a, min(a.potenza_mw, (a.capacita_mwh - a.carica_mwh) / (math.sqrt(a.rendimento) * dt))) for a in unita]
            spazio = [(a, q) for a, q in spazio if q > EPS]
            if not spazio:
                continue
            livello = sum(a.carica_mwh for a in unita) / (sum(a.capacita_mwh for a in unita) or 1.0)
            # Comprano sotto la media recente, tanto meno volentieri quanto più sono carichi; in riserva si ricaricano sempre.
            offerta_acquisto = self._prezzo_medio * 1.05 if riserva else self._prezzo_medio * (0.94 - 0.12 * livello)
            ricariche.append((offerta_acquisto, zi, tipo, spazio))
        ricariche.sort(key=lambda r: -r[0])
        ricarica_totale = 0.0
        for offerta_acquisto, zi, tipo, spazio in ricariche:
            richiesta = [0.0] * n
            richiesta[zi] = sum(q for _, q in spazio)
            totale = richiesta[zi]
            _dispaccia(self._trasporto_el, offerte, richiesta, limite_costo=offerta_acquisto)
            caricato = totale - richiesta[zi]
            if caricato <= EPS:
                continue
            ricarica_totale += caricato
            for a, q in spazio:
                quota = caricato * q / totale
                a.potenza_attuale_mw -= quota
                a.carica_mwh = min(a.capacita_mwh, a.carica_mwh + quota * dt * math.sqrt(a.rendimento))

        # Ripartizione della produzione sulle unità
        produzione = {k: 0.0 for k in CATEGORIE_PRODUZIONE}
        taglio = 0.0
        co2 = 0.0
        consumo_termico = [0.0] * n
        libera_zona = [0.0] * n  # capacità programmabile e di accumulo non usata, per zona
        interrotto_zona = [0.0] * n
        costo_misure = 0.0
        for c in s.centrali:
            c.produzione_mw = 0.0
        for offerta in offerte:
            if offerta.tipo == "interrompibili":
                interrotto_zona[offerta.zona] += offerta.usato
                costo_misure += offerta.usato * offerta.costo * dt
                continue
            if offerta.tipo == "import_emergenza":
                costo_misure += offerta.usato * SOVRAPPREZZO_IMPORT_EMERGENZA * dt
            if offerta.tipo in ("batteria", "pompaggio"):
                if offerta.usato > EPS:
                    for a, disponibile in offerta.unita:
                        quota = offerta.usato * disponibile / offerta.quantita
                        a.potenza_attuale_mw += quota
                        a.carica_mwh = max(0.0, a.carica_mwh - quota * dt / math.sqrt(a.rendimento))
                libera_zona[offerta.zona] += offerta.quantita - offerta.usato
                continue
            usato = offerta.usato
            if offerta.tipo in _A_COSTO_ZERO:
                taglio += max(0.0, offerta.quantita - usato)
            elif offerta.tipo in _PROGRAMMABILI or offerta.tipo == "import_emergenza":
                libera_zona[offerta.zona] += offerta.quantita - usato
            for c, disponibile in offerta.unita:
                c.produzione_mw += usato * disponibile / offerta.quantita if offerta.quantita > EPS else 0.0
            produzione[_CATEGORIA.get(offerta.tipo, "import")] += usato
            if offerta.tipo == "gas":
                rendimento = rendimenti_gas[id(offerta)]
                co2 += usato * CO2_GAS_T_MWH_TERMICO / rendimento * dt
                consumo_termico[offerta.zona] += usato / (rendimento * KWH_PER_SM3)
            else:
                co2 += usato * TECNOLOGIE[_TIPO_TECNOLOGIA.get(offerta.tipo, offerta.tipo)].co2_t_mwh * dt

        # Bacini idroelettrici: afflussi, prelievi, sfiori
        afflusso = ambiente.afflusso_bacino(doy)
        for c in s.centrali:
            if c.tipo == "idro_bacino":
                c.riserva_bacino_mwh = min(c.bacino_mwh, max(0.0, c.riserva_bacino_mwh + (c.potenza_mw * afflusso - c.produzione_mw) * dt))

        scarica_accumuli = sum(a.potenza_attuale_mw for a in s.accumuli)
        produzione["accumuli"] = scarica_accumuli
        for i, l in enumerate(s.linee):
            l.flusso_mw = self._trasporto_el.flusso[i]
        # Margine di riserva di zona: capacità libera nella zona più quella che le linee possono ancora portare dalle
        # zone vicine (limitata a ciò che lì è libero). Conta la zona più debole: una Sicilia isolata va in riserva
        # bassa anche se il resto d'Italia ha capacità in abbondanza.
        margine_zona = list(libera_zona)
        for k, l in enumerate(s.linee):
            capacita = self._trasporto_el.capacita[k]
            da, a = self.indice_zona[l.da], self.indice_zona[l.a]
            margine_zona[a] += min(max(0.0, capacita - l.flusso_mw), libera_zona[da])
            margine_zona[da] += min(max(0.0, capacita + l.flusso_mw), libera_zona[a])
        for i, z in enumerate(s.zone):
            z.distacco_mw = distacco_zona[i]
            z.interrotto_mw = interrotto_zona[i]
            z.margine_riserva = margine_zona[i] / z.domanda_mw if z.domanda_mw > EPS else 1.0
        zona_debole = min(s.zone, key=lambda z: z.margine_riserva)
        for i, c in enumerate(s.citta):
            zona = s.zone[self._zona_citta[i]]
            quota = (zona.distacco_mw + zona.interrotto_mw) / zona.domanda_mw if zona.domanda_mw > EPS else 0.0
            c.servita_mw = c.domanda_mw * (1 - quota)
        interrotto = sum(interrotto_zona)

        rinnovabile = sum(produzione[k] for k in ("solare", "eolico", "idro")) + sum(
            c.produzione_mw for c in s.centrali if c.tipo in ("geotermico", "biomasse")
        )
        generata = sum(v for k, v in produzione.items() if k != "accumuli")
        self.nazionale.update({
            "domanda_mw": domanda, "servita_mw": domanda - distacco - interrotto, "distacco_mw": distacco,
            "interrotto_mw": interrotto,
            "produzione_mw": produzione, "generazione_mw": generata, "ricarica_accumuli_mw": ricarica_totale,
            "taglio_rinnovabili_mw": taglio, "quota_rinnovabili": rinnovabile / generata if generata > EPS else 0.0,
            "prezzo_energia": prezzo, "co2_t_h": co2 / dt, "margine_riserva": zona_debole.margine_riserva, "zona_margine_minimo": zona_debole.nome,
            "margine_riserva_nazionale": sum(libera_zona) / domanda if domanda > EPS else 0.0,
            "prezzo_co2": self.prezzo_co2,
        })
        c = self.cumulativi
        c["domanda_mwh"] += domanda * dt
        c["servita_mwh"] += (domanda - distacco - interrotto) * dt
        c["distacco_mwh"] += distacco * dt
        c["interrotto_mwh"] += interrotto * dt
        c["costo_misure_eur"] += costo_misure
        if distacco > EPS:
            c["ore_con_distacco"] += dt
        for k, v in produzione.items():
            c["produzione_mwh"][k] += v * dt
        c["taglio_rinnovabili_mwh"] += taglio * dt
        c["co2_t"] += co2
        c["costo_energia_eur"] += min(prezzo, 500.0) * (domanda - distacco - interrotto) * dt
        c["prezzo_massimo"] = max(c["prezzo_massimo"], prezzo)
        return consumo_termico

    # ------------------------------------------------------------------------------------------ gas

    @staticmethod
    def _pressione(z: Any) -> float:
        return 40.0 + 35.0 * (z.linepack / z.linepack_max) if z.linepack_max > 0 else 0.0

    def _passo_gas(self, t: datetime, doy: float, consumo_termico: list[float]) -> None:
        s, dt, n = self.scenario, self.dt, len(self.scenario.zone)
        feriale = 1.1 if t.weekday() < 5 else 0.75
        calendario = 0.7 if (t.month == 8 and 8 <= t.day <= 22) else 1.0
        civile, industriale = [0.0] * n, [0.0] * n
        riduzione_industria = RIDUZIONE_GAS_INTERROMPIBILI if self.leve["rete_gas_interrompibili"] == "ON" else 0.0
        gas_interrotto = 0.0
        for i, c in enumerate(s.citta):
            zi = self._zona_citta[i]
            zona = s.zone[zi]
            if not zona.rete_gas:
                c.gas_domanda = c.gas_servito = 0.0
                continue
            r = c.gas_base + c.gas_riscaldamento * ambiente.gradi_riscaldamento(zona.temperatura)
            ind = c.gas_industria * feriale * calendario
            gas_interrotto += ind * riduzione_industria
            ind *= 1 - riduzione_industria
            civile[zi] += r
            industriale[zi] += ind
            c.gas_domanda = r + ind
        termico = [consumo_termico[i] if s.zone[i].rete_gas else 0.0 for i in range(n)]
        fabbisogno = [civile[i] + industriale[i] + termico[i] for i in range(n)]
        domanda = sum(fabbisogno)

        offerte: list[_Offerta] = []
        for ingresso in s.ingressi:
            zi = self.indice_zona[ingresso.zona]
            ingresso.portata = 0.0
            massima = ingresso.portata_max * ingresso.riduzione
            costo_flessibile = {"gasdotto": 1.0, "gnl": 1.5, "produzione": 0.5}[ingresso.tipo]
            offerte.append(_Offerta(0.1, zi, massima * ingresso.quota_minima, [(ingresso, 1.0)], ingresso.tipo))
            offerte.append(_Offerta(costo_flessibile, zi, massima * (1 - ingresso.quota_minima), [(ingresso, 1.0)], ingresso.tipo))
            if ingresso.tipo == "gnl" and self.leve["rete_gas_gnl_spot"] == "ON":
                offerte.append(_Offerta(3.0, zi, massima * (FATTORE_GNL_SPOT - 1), [(ingresso, 1.0)], ingresso.tipo))
        obiettivo = ambiente.obiettivo_stoccaggio_gas(doy)
        iniezione = ambiente.stagione_iniezione(doy)
        for st in s.stoccaggi:
            st.portata = 0.0
            livello = st.giacenza / st.capacita if st.capacita else 0.0
            if self.leve["rete_gas_stoccaggio_strategico"] == "ON":
                erogabile = min(st.erogazione_max, st.giacenza * 1000 / dt)
                costo = 2.0
            else:
                erogabile = min(st.erogazione_max * (0.35 + 0.65 * livello), st.giacenza * 1000 / dt)
                costo = 9.0 if iniezione else 2.0 + 30.0 * max(0.0, obiettivo - livello)
            offerte.append(_Offerta(costo, self.indice_zona[st.zona], erogabile, [(st, 1.0)], "stoccaggio"))
        for i, z in enumerate(s.zone):
            if z.rete_gas and z.linepack_max > 0:
                offerte.append(_Offerta(20.0, i, max(0.0, z.linepack - 0.15 * z.linepack_max) / dt, [(z, 1.0)], "linepack"))
        offerte.sort(key=lambda o: o.costo)

        self._trasporto_gas.prepara([
            c.capacita * ((1 - c.perdita_guasto) if c.guasto_fino > self.passo else 1.0) for c in s.condotte
        ])
        _dispaccia(self._trasporto_gas, offerte, fabbisogno)
        mancante = fabbisogno

        # Iniezione negli stoccaggi (stagione estiva) con il gas economico avanzato; poi ricostituzione del linepack,
        # anche con gli stoccaggi: dopo una crisi l'operatore riporta in pressione la rete.
        iniettato = 0.0
        for st in s.stoccaggi:
            livello = st.giacenza / st.capacita if st.capacita else 1.0
            if not iniezione or livello >= obiettivo:
                continue
            zi = self.indice_zona[st.zona]
            richiesta = [0.0] * n
            richiesta[zi] = min(st.iniezione_max * min(1.0, (obiettivo - livello) * 8), (st.capacita - st.giacenza) * 1000 / dt)
            totale = richiesta[zi]
            _dispaccia(self._trasporto_gas, offerte, richiesta, limite_costo=1.8)
            q = totale - richiesta[zi]
            st.portata -= q
            iniettato += q
        ricostituzione = [0.0] * n
        for i, z in enumerate(s.zone):
            if not z.rete_gas or z.linepack_max <= 0:
                continue
            richiesta = [0.0] * n
            richiesta[i] = max(0.0, 0.7 * z.linepack_max - z.linepack) / dt
            totale = richiesta[i]
            if totale > EPS:
                _dispaccia(self._trasporto_gas, offerte, richiesta, limite_costo=12.0)
                ricostituzione[i] = totale - richiesta[i]

        ingressi = {"gasdotto": 0.0, "gnl": 0.0, "produzione": 0.0}
        erogato = 0.0
        for offerta in offerte:
            if offerta.usato <= EPS:
                continue
            entita = offerta.unita[0][0]
            if offerta.tipo in ingressi:
                entita.portata += offerta.usato
                ingressi[offerta.tipo] += offerta.usato
            elif offerta.tipo == "stoccaggio":
                entita.portata += offerta.usato
                erogato += offerta.usato
            elif offerta.tipo == "linepack":
                entita.linepack -= offerta.usato * dt
        for st in s.stoccaggi:
            st.giacenza = min(st.capacita, max(0.0, st.giacenza - st.portata * dt / 1000))
        for i, z in enumerate(s.zone):
            z.gas_domanda = civile[i] + industriale[i] + termico[i]
            z.gas_non_servito = mancante[i]
            if z.linepack_max > 0:
                z.linepack = min(z.linepack_max, z.linepack + ricostituzione[i] * dt)
                z.pressione = self._pressione(z)
            # Il gas mancante si toglie prima all'industria, poi alle centrali, per ultimo alle case.
            restante = mancante[i]
            tolto_industria = min(restante, industriale[i])
            restante -= tolto_industria
            tolto_centrali = min(restante, termico[i])
            restante -= tolto_centrali
            quota_centrali = 1 - tolto_centrali / termico[i] if termico[i] > EPS else 1.0
            if z.rete_gas and z.linepack_max > 0 and z.pressione < 48:
                quota_centrali = min(quota_centrali, 0.85)
            z.fattore_gas = max(0.0, quota_centrali) if tolto_centrali > EPS or z.pressione < 48 else 1.0
        for i, c in enumerate(s.citta):
            zona = s.zone[self._zona_citta[i]]
            quota = zona.gas_non_servito / zona.gas_domanda if zona.gas_domanda > EPS else 0.0
            c.gas_servito = c.gas_domanda * (1 - quota)
        for i, condotta in enumerate(s.condotte):
            condotta.flusso = self._trasporto_gas.flusso[i]

        capacita = sum(st.capacita for st in s.stoccaggi)
        non_servito = sum(mancante)
        self.nazionale.update({
            "gas_domanda": domanda, "gas_termoelettrico": sum(termico), "gas_civile": sum(civile),
            "gas_industriale": sum(industriale), "gas_non_servito": non_servito, "gas_ingressi": ingressi,
            "gas_erogazione_stoccaggi": erogato, "gas_iniezione_stoccaggi": iniettato,
            "stoccaggio_gas_pct": sum(st.giacenza for st in s.stoccaggi) / capacita if capacita else 0.0,
            "obiettivo_stoccaggio_pct": obiettivo, "prezzo_gas": self.prezzo_gas, "gas_interrotto": gas_interrotto,
        })
        c = self.cumulativi
        c["gas_domanda_msm3"] += domanda * dt / 1000
        c["gas_non_servito_msm3"] += non_servito * dt / 1000
        c["gas_interrotto_msm3"] += gas_interrotto * dt / 1000
        for k, v in ingressi.items():
            c["gas_ingressi_msm3"][k] += v * dt / 1000
        c["gas_stoccaggio_netto_msm3"] += (erogato - iniettato) * dt / 1000

    # ------------------------------------------------------------------------------------------ anomalie

    def _aggiorna_anomalie(self) -> None:
        s = self.scenario
        trovate: list[dict[str, Any]] = []

        def aggiungi(tipo: str, gravita: str, entita: str, descrizione: str, valore: float) -> None:
            trovate.append({"id": f"{tipo}:{entita}", "tipo": tipo, "gravita": gravita, "entita": entita,
                            "descrizione": descrizione, "valore": round(valore, 3)})

        for z in s.zone:
            if z.distacco_mw > 1:
                aggiungi("distacco_carico", "critica", z.id, f"{z.nome}: {z.distacco_mw:.0f} MW di carico non servito", z.distacco_mw)
            if z.gas_non_servito > 1:
                aggiungi("gas_non_servito", "critica", z.id, f"{z.nome}: {z.gas_non_servito:.0f} kSm³/h di gas non servito", z.gas_non_servito)
            if z.rete_gas and z.linepack_max > 0 and z.pressione < 50:
                gravita = "critica" if z.pressione < 46 else "avviso"
                aggiungi("pressione_gas_bassa", gravita, z.id, f"{z.nome}: pressione gas {z.pressione:.1f} bar", z.pressione)
        for l in s.linee:
            capacita = l.capacita_mw * ((1 - l.perdita_guasto) if l.guasto_fino > self.passo else 1.0)
            carico = abs(l.flusso_mw) / capacita if capacita > EPS else 0.0
            self.cumulativi["carico_massimo_linee"] = max(self.cumulativi["carico_massimo_linee"], carico)
            if carico >= 0.98 and capacita > EPS:
                aggiungi("congestione", "avviso", l.id, f"Linea {l.da}-{l.a} satura ({abs(l.flusso_mw):.0f} MW)", carico)
        for z in s.zone:
            if z.domanda_mw > 1 and z.margine_riserva < SOGLIA_RISERVA_BASSA and z.distacco_mw <= 1:
                aggiungi("riserva_bassa", "avviso", z.id, f"{z.nome}: margine di riserva al {z.margine_riserva:.1%}", z.margine_riserva)
        pct = self.nazionale.get("stoccaggio_gas_pct", 1.0)
        if pct < 0.1:
            aggiungi("stoccaggio_gas_basso", "critica" if pct < 0.03 else "avviso", "stoccaggi", f"Stoccaggi gas al {pct:.0%}", pct)
        for e in self.eventi_attivi:
            if e["tipo"] in ("guasto", "crisi_gas") and e["entita"]:
                aggiungi(e["tipo"], "avviso", e["entita"], e["descrizione"], e.get("perdita", 1.0))

        attuali = {a["id"] for a in trovate}
        self._inizio_anomalie = {k: v for k, v in self._inizio_anomalie.items() if k in attuali}
        for a in trovate:
            inizio = self._inizio_anomalie.setdefault(a["id"], self.passo)
            a["da_ore"] = round((self.passo - inizio) * self.dt, 2)
        self.anomalie = trovate

    # ------------------------------------------------------------------------------------------ statistiche

    def _registra(self, t: datetime) -> None:
        naz = self.nazionale
        riga = {
            "t": t.isoformat(timespec="minutes"), "domanda": round(naz["domanda_mw"]), "distacco": round(naz["distacco_mw"]),
            "produzione": {k: round(v) for k, v in naz["produzione_mw"].items()},
            "prezzo": round(naz["prezzo_energia"], 1), "prezzo_gas": round(naz["prezzo_gas"], 1),
            "rinnovabili": round(naz["quota_rinnovabili"], 3), "gas_domanda": round(naz["gas_domanda"]),
            "gas_non_servito": round(naz["gas_non_servito"]), "stoccaggio_gas": round(naz["stoccaggio_gas_pct"], 4),
        }
        self.serie_passi.append(riga)

        giorno = t.date().isoformat()
        g = self._giorno_corrente
        if g is None or g["giorno"] != giorno:
            if g is not None:
                self._chiudi_giorno(g)
            g = self._giorno_corrente = {
                "giorno": giorno, "n": 0, "domanda": 0.0, "distacco_mwh": 0.0, "prezzo": 0.0, "prezzo_gas": 0.0,
                "rinnovabili": 0.0, "gas_domanda": 0.0, "gas_non_servito": 0.0, "temperatura": 0.0,
                "produzione": {k: 0.0 for k in CATEGORIE_PRODUZIONE},
            }
        g["n"] += 1
        g["domanda"] += naz["domanda_mw"]
        g["distacco_mwh"] += naz["distacco_mw"] * self.dt
        g["prezzo"] += naz["prezzo_energia"]
        g["prezzo_gas"] += naz["prezzo_gas"]
        g["rinnovabili"] += naz["quota_rinnovabili"]
        g["gas_domanda"] += naz["gas_domanda"]
        g["gas_non_servito"] += naz["gas_non_servito"]
        g["temperatura"] += sum(z.temperatura for z in self.scenario.zone) / len(self.scenario.zone)
        for k, v in naz["produzione_mw"].items():
            g["produzione"][k] += v
        g["stoccaggio_gas"] = naz["stoccaggio_gas_pct"]
        if self.passo + 1 >= self.passi_totali:
            self._chiudi_giorno(g)
            self._giorno_corrente = None

        if t.minute == 0:
            for eid, valore in self._valori_principali():
                self.storico_entita[eid].append((riga["t"], valore))

    def _chiudi_giorno(self, g: dict[str, Any]) -> None:
        n = g["n"] or 1
        self.serie_giorni.append({
            "giorno": g["giorno"], "domanda": round(g["domanda"] / n), "distacco_mwh": round(g["distacco_mwh"]),
            "prezzo": round(g["prezzo"] / n, 1), "prezzo_gas": round(g["prezzo_gas"] / n, 1),
            "rinnovabili": round(g["rinnovabili"] / n, 3), "gas_domanda": round(g["gas_domanda"] / n),
            "gas_non_servito": round(g["gas_non_servito"] / n), "temperatura": round(g["temperatura"] / n, 1),
            "stoccaggio_gas": round(g["stoccaggio_gas"], 4),
            "produzione": {k: round(v / n) for k, v in g["produzione"].items()},
        })

    def _valori_principali(self):
        s = self.scenario
        for z in s.zone:
            yield z.id, round(z.temperatura, 1)
        for c in s.centrali:
            yield c.id, round(c.produzione_mw, 1)
        for c in s.citta:
            yield c.id, round(c.domanda_mw, 1)
        for a in s.accumuli:
            yield a.id, round(a.carica_mwh / a.capacita_mwh, 3) if a.capacita_mwh else 0.0
        for l in s.linee:
            yield l.id, round(l.flusso_mw, 1)
        for i in s.ingressi:
            yield i.id, round(i.portata, 1)
        for st in s.stoccaggi:
            yield st.id, round(st.giacenza / st.capacita, 4) if st.capacita else 0.0
        for c in s.condotte:
            yield c.id, round(c.flusso, 1)

    # ------------------------------------------------------------------------------------------ viste

    def topologia(self) -> dict[str, Any]:
        """Parte statica della rete, nell'ordine usato da `istantanea()`."""
        s = self.scenario
        return {
            "scenario": {"nome": s.nome, "descrizione": s.descrizione, "entita": s.numero_entita()},
            "zone": [{"id": z.id, "nome": z.nome, "x": z.x, "y": z.y, "rete_gas": z.rete_gas} for z in s.zone],
            "centrali": [{"id": c.id, "nome": c.nome, "tipo": c.tipo, "zona": c.zona, "x": c.x, "y": c.y,
                          "potenza_mw": c.potenza_mw, "flotta": c.flotta} for c in s.centrali],
            "citta": [{"id": c.id, "nome": c.nome, "zona": c.zona, "x": c.x, "y": c.y, "popolazione": c.popolazione,
                       "domanda_media_mw": round(c.domanda_media_mw, 1)} for c in s.citta],
            "accumuli": [{"id": a.id, "nome": a.nome, "tipo": a.tipo, "zona": a.zona, "x": a.x, "y": a.y,
                          "potenza_mw": a.potenza_mw, "capacita_mwh": a.capacita_mwh} for a in s.accumuli],
            "linee": [{"id": l.id, "da": l.da, "a": l.a, "capacita_mw": l.capacita_mw} for l in s.linee],
            "ingressi": [{"id": i.id, "nome": i.nome, "tipo": i.tipo, "zona": i.zona, "x": i.x, "y": i.y,
                          "portata_max": i.portata_max} for i in s.ingressi],
            "stoccaggi": [{"id": st.id, "nome": st.nome, "zona": st.zona, "x": st.x, "y": st.y, "capacita": st.capacita}
                          for st in s.stoccaggi],
            "condotte": [{"id": c.id, "da": c.da, "a": c.a, "capacita": c.capacita} for c in s.condotte],
            "tipi_evento": TIPI_EVENTO,
            "leve": LEVE,
        }

    def istantanea(self) -> dict[str, Any]:
        """Valori dinamici compatti (liste nello stesso ordine della topologia) più i dati nazionali."""
        s, p = self.scenario, self.passo
        naz = self.nazionale
        return {
            "t": self.tempo.isoformat(timespec="minutes"), "passo": p, "passi_totali": self.passi_totali,
            "finita": self.finita,
            "nazionale": {
                k: (round(v, 4) if isinstance(v, float) else ({kk: round(vv, 1) for kk, vv in v.items()} if isinstance(v, dict) else v))
                for k, v in naz.items()
            },
            "zone": [[round(z.temperatura, 1), round(z.vento, 1), round(z.nuvole, 2), round(z.pressione, 1),
                      round(z.domanda_mw), round(z.distacco_mw), round(z.gas_non_servito), round(z.interrotto_mw)] for z in s.zone],
            "leve": dict(self.leve),
            "veti": {leva: {k: v for k, v in veto.items() if k != "fino_passo"}
                     for leva in list(self.veti) if (veto := self.veto(leva))},
            "centrali": [[round(c.produzione_mw), round(c.disponibile_mw),
                          2 if c.guasto_fino > p else (1 if c.in_manutenzione else 0)] for c in s.centrali],
            "citta": [[round(c.domanda_mw), round(c.servita_mw), round(c.gas_domanda), round(c.gas_servito)] for c in s.citta],
            "accumuli": [[round(a.carica_mwh / a.capacita_mwh, 3) if a.capacita_mwh else 0, round(a.potenza_attuale_mw),
                          1 if a.guasto_fino > p else 0] for a in s.accumuli],
            "linee": [[round(l.flusso_mw), 1 if l.guasto_fino > p else 0,
                       round(l.capacita_mw * ((1 - l.perdita_guasto) if l.guasto_fino > p else 1))] for l in s.linee],
            "ingressi": [[round(i.portata), round(i.riduzione, 2)] for i in s.ingressi],
            "stoccaggi": [[round(st.giacenza / st.capacita, 4) if st.capacita else 0, round(st.portata)] for st in s.stoccaggi],
            "condotte": [[round(c.flusso), 1 if c.guasto_fino > p else 0,
                          round(c.capacita * ((1 - c.perdita_guasto) if c.guasto_fino > p else 1))] for c in s.condotte],
            "anomalie": self.anomalie[:100],
            "numero_anomalie": len(self.anomalie),
            "eventi_attivi": [{k: e[k] for k in ("id", "tipo", "entita", "descrizione", "inizio", "fine")} for e in self.eventi_attivi[-50:]],
        }

    def riepilogo(self) -> dict[str, Any]:
        """Statistiche cumulative dall'inizio della simulazione."""
        c = self.cumulativi
        ore = self.passo * self.dt
        generata = sum(v for k, v in c["produzione_mwh"].items() if k != "accumuli")
        rinnovabile = c["produzione_mwh"]["solare"] + c["produzione_mwh"]["eolico"] + c["produzione_mwh"]["idro"]
        return {
            "ore_simulate": ore,
            "domanda_twh": round(c["domanda_mwh"] / 1e6, 3),
            "distacco_mwh": round(c["distacco_mwh"], 1),
            "ore_con_distacco": round(c["ore_con_distacco"], 2),
            "produzione_twh": {k: round(v / 1e6, 3) for k, v in c["produzione_mwh"].items()},
            "quota_rinnovabili_fer": round(rinnovabile / generata, 4) if generata else 0.0,
            "taglio_rinnovabili_twh": round(c["taglio_rinnovabili_mwh"] / 1e6, 3),
            "co2_mt": round(c["co2_t"] / 1e6, 3),
            "prezzo_medio": round(c["costo_energia_eur"] / c["servita_mwh"], 2) if c["servita_mwh"] else 0.0,
            "prezzo_massimo": round(c["prezzo_massimo"], 1),
            "gas_domanda_mld_m3": round(c["gas_domanda_msm3"] / 1000, 3),
            "gas_non_servito_msm3": round(c["gas_non_servito_msm3"], 2),
            "gas_ingressi_mld_m3": {k: round(v / 1000, 3) for k, v in c["gas_ingressi_msm3"].items()},
            "eventi_per_tipo": dict(c["eventi_per_tipo"]),
            "carico_massimo_linee": round(c["carico_massimo_linee"], 3),
            "carico_interrotto_mwh": round(c["interrotto_mwh"], 1),
            "gas_interrotto_msm3": round(c["gas_interrotto_msm3"], 2),
            "costo_misure_eur": round(c["costo_misure_eur"]),
            "azionamenti_leve": c["azionamenti_leve"],
        }

    def descrivi_entita(self, entita_id: str) -> dict[str, Any]:
        if entita_id not in self.entita:
            raise KeyError(entita_id)
        categoria, oggetto = self.entita[entita_id]
        dati = asdict(oggetto)
        return {
            "categoria": categoria,
            "valori": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in dati.items()},
            "eventi_attivi": [e for e in self.eventi_attivi if e["entita"] == entita_id],
            "anomalie": [a for a in self.anomalie if a["entita"] == entita_id],
            "storico_orario": list(self.storico_entita[entita_id]),
        }

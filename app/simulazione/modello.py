"""
Entità della rete energetica simulata.

Unità: potenza in MW, energia in MWh, portate di gas in kSm³/h (migliaia di metri cubi standard all'ora),
giacenze di gas in MSm³ (milioni di metri cubi standard), pressione in bar, temperatura in °C, vento in m/s.
Ogni entità ha una parte statica (dallo scenario) e una dinamica (aggiornata a ogni passo).
"""

from dataclasses import dataclass, field

# Potere calorifico del gas naturale: 1 Sm³ ≈ 10,57 kWh.
KWH_PER_SM3 = 10.57


@dataclass(frozen=True, slots=True)
class Tecnologia:
    nome: str
    rinnovabile: bool
    co2_t_mwh: float           # emissioni per MWh elettrico prodotto
    guasti_per_ora: float      # probabilità di guasto per ora di esercizio
    riparazione_ore: float     # durata media di un guasto
    rendimento: float = 0.0    # solo per le centrali a combustibile (gas: consumo dalla rete gas)
    manutenzione: bool = False  # le centrali singole (non flotte) si fermano 14 giorni all'anno


TECNOLOGIE: dict[str, Tecnologia] = {
    "solare": Tecnologia("Fotovoltaico", True, 0.0, 1 / 30000, 24),
    "eolico": Tecnologia("Eolico", True, 0.0, 1 / 20000, 36),
    "idro_fluente": Tecnologia("Idroelettrico fluente", True, 0.0, 1 / 20000, 48),
    "idro_bacino": Tecnologia("Idroelettrico a bacino", True, 0.0, 1 / 15000, 48),
    "geotermico": Tecnologia("Geotermico", True, 0.0, 1 / 8000, 72, manutenzione=True),
    "biomasse": Tecnologia("Biomasse", True, 0.0, 1 / 4000, 48),
    "nucleare": Tecnologia("Nucleare", False, 0.0, 1 / 6000, 120, manutenzione=True),
    "gas": Tecnologia("Termoelettrico a gas", False, 0.37, 1 / 2500, 36, rendimento=0.55, manutenzione=True),
    "carbone": Tecnologia("Termoelettrico a carbone", False, 0.90, 1 / 1800, 48, manutenzione=True),
    "import": Tecnologia("Importazione", False, 0.0, 1 / 10000, 12),
}

TECNOLOGIE_ACCUMULO = {"batteria": "Batteria", "pompaggio": "Pompaggio idroelettrico"}


@dataclass(slots=True)
class Zona:
    id: str
    nome: str
    x: float                   # longitudine (o coordinata equivalente nelle reti generate)
    y: float                   # latitudine
    temp_media: float          # media annua
    temp_ampiezza: float       # escursione stagionale (metà della differenza estate-inverno)
    temp_giornaliera: float    # escursione giornaliera
    vento_medio: float
    nuvole_inverno: float      # copertura media 0..1
    nuvole_estate: float
    rete_gas: bool = True
    linepack_max: float = 8000.0   # kSm³ di gas contenuti nei tubi della zona
    # dinamici
    temperatura: float = 0.0
    vento: float = 0.0
    nuvole: float = 0.0
    linepack: float = 0.0
    pressione: float = 0.0
    domanda_mw: float = 0.0
    distacco_mw: float = 0.0
    interrotto_mw: float = 0.0     # carico staccato in modo programmato (leva degli interrompibili)
    margine_riserva: float = 1.0   # capacità ancora disponibile per la zona (anche dalle vicine) / domanda
    gas_domanda: float = 0.0
    gas_non_servito: float = 0.0
    fattore_gas: float = 1.0   # quota del gas richiesto dalle centrali effettivamente disponibile


@dataclass(slots=True)
class Centrale:
    id: str
    nome: str
    tipo: str
    zona: str
    x: float
    y: float
    potenza_mw: float
    flotta: bool = False           # insieme di impianti aggregati: un guasto ne ferma solo una parte
    bacino_mwh: float = 0.0        # solo idro_bacino: energia immagazzinabile
    giorno_manutenzione: int = 0   # giorno dell'anno di inizio della manutenzione programmata
    rendimento: float = 0.0        # 0: quello tipico della tecnologia
    # dinamici
    disponibile_mw: float = 0.0
    produzione_mw: float = 0.0
    riserva_bacino_mwh: float = 0.0
    guasto_fino: int = -1          # indice del passo in cui termina il guasto (-1: nessun guasto)
    perdita_guasto: float = 0.0    # quota di potenza persa per il guasto
    in_manutenzione: bool = False


@dataclass(slots=True)
class Citta:
    id: str
    nome: str
    zona: str
    x: float
    y: float
    popolazione: float             # milioni di abitanti (area servita)
    domanda_media_mw: float = 0.0  # calcolata dallo scenario
    gas_base: float = 0.0          # kSm³/h per usi non termici (acqua calda, cucina)
    gas_riscaldamento: float = 0.0  # kSm³/h per grado-giorno
    gas_industria: float = 0.0     # kSm³/h medi
    # dinamici
    domanda_mw: float = 0.0
    servita_mw: float = 0.0
    gas_domanda: float = 0.0
    gas_servito: float = 0.0


@dataclass(slots=True)
class Accumulo:
    id: str
    nome: str
    tipo: str                      # batteria | pompaggio
    zona: str
    x: float
    y: float
    potenza_mw: float
    capacita_mwh: float
    rendimento: float              # andata e ritorno
    # dinamici
    carica_mwh: float = 0.0
    potenza_attuale_mw: float = 0.0  # > 0 scarica verso la rete, < 0 carica
    guasto_fino: int = -1


@dataclass(slots=True)
class Linea:
    """Collegamento elettrico tra due zone (limite di transito)."""
    id: str
    da: str
    a: str
    capacita_mw: float
    # dinamici
    flusso_mw: float = 0.0          # > 0 da `da` verso `a`
    guasto_fino: int = -1
    perdita_guasto: float = 0.0


@dataclass(slots=True)
class IngressoGas:
    id: str
    nome: str
    tipo: str                      # gasdotto | gnl | produzione
    zona: str
    x: float
    y: float
    portata_max: float             # kSm³/h
    quota_minima: float = 0.3      # quota a costo minimo (contratti take-or-pay)
    # dinamici
    portata: float = 0.0
    riduzione: float = 1.0         # 1: piena disponibilità; < 1 durante una crisi di fornitura
    riduzione_fino: int = -1


@dataclass(slots=True)
class StoccaggioGas:
    id: str
    nome: str
    zona: str
    x: float
    y: float
    capacita: float                # MSm³ di gas di lavoro
    erogazione_max: float          # kSm³/h
    iniezione_max: float           # kSm³/h
    # dinamici
    giacenza: float = 0.0
    portata: float = 0.0           # > 0 erogazione verso la rete, < 0 iniezione


@dataclass(slots=True)
class Condotta:
    """Collegamento della rete gas tra due zone."""
    id: str
    da: str
    a: str
    capacita: float                # kSm³/h
    # dinamici
    flusso: float = 0.0
    guasto_fino: int = -1
    perdita_guasto: float = 0.0


@dataclass(slots=True)
class Scenario:
    nome: str
    descrizione: str
    zone: list[Zona] = field(default_factory=list)
    centrali: list[Centrale] = field(default_factory=list)
    citta: list[Citta] = field(default_factory=list)
    accumuli: list[Accumulo] = field(default_factory=list)
    linee: list[Linea] = field(default_factory=list)
    ingressi: list[IngressoGas] = field(default_factory=list)
    stoccaggi: list[StoccaggioGas] = field(default_factory=list)
    condotte: list[Condotta] = field(default_factory=list)

    def numero_entita(self) -> int:
        return sum(len(x) for x in (
            self.zone, self.centrali, self.citta, self.accumuli, self.linee, self.ingressi, self.stoccaggi, self.condotte,
        ))

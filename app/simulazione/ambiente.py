"""
Meteo, profili di carico e calendario: funzioni pure, senza stato.
La parte casuale (anomalie di temperatura, nuvole, vento, prezzi) sta nel motore, che la tiene con il proprio seed.
"""

import math
from datetime import datetime

# Forma del carico elettrico in un giorno feriale (ora 0..23), media ≈ 1.
_FORMA_FERIALE = (
    0.74, 0.70, 0.68, 0.67, 0.68, 0.72, 0.82, 0.94, 1.05, 1.10, 1.12, 1.12,
    1.08, 1.06, 1.08, 1.09, 1.10, 1.12, 1.15, 1.16, 1.12, 1.04, 0.94, 0.84,
)
_MEDIA_FERIALE = sum(_FORMA_FERIALE) / 24
# Nel fine settimana il carico diurno cala di più di quello notturno.
_FORMA_FESTIVA = tuple(0.78 * v + 0.12 * _MEDIA_FERIALE for v in _FORMA_FERIALE)

SOGLIA_RISCALDAMENTO = 17.0   # °C sotto cui si accende il riscaldamento (gradi-giorno)
SOGLIA_RAFFRESCAMENTO = 23.0  # °C sopra cui si accendono i condizionatori


def giorno_anno(t: datetime) -> float:
    """Giorno dell'anno con la frazione oraria (1.0 = mezzanotte del 1° gennaio)."""
    return t.timetuple().tm_yday + (t.hour + t.minute / 60) / 24


def festivo(t: datetime) -> bool:
    if t.weekday() >= 5:
        return True
    return (t.month, t.day) in {(1, 1), (1, 6), (4, 25), (5, 1), (6, 2), (8, 15), (11, 1), (12, 8), (12, 25), (12, 26)}


def fattore_calendario(t: datetime) -> float:
    """Ferie di agosto e periodo natalizio riducono il carico industriale e quindi quello totale."""
    if t.month == 8 and 8 <= t.day <= 22:
        return 0.80
    if (t.month == 12 and t.day >= 24) or (t.month == 1 and t.day <= 6):
        return 0.88
    return 1.0


def forma_giornaliera(t: datetime) -> float:
    """Carico relativo all'ora del giorno, interpolato tra un'ora e la successiva."""
    forma = _FORMA_FESTIVA if festivo(t) else _FORMA_FERIALE
    ora = t.hour + t.minute / 60
    i = int(ora) % 24
    frazione = ora - int(ora)
    return (forma[i] * (1 - frazione) + forma[(i + 1) % 24] * frazione) / _MEDIA_FERIALE


def temperatura_base(media: float, ampiezza: float, escursione: float, doy: float, ora: float) -> float:
    """Temperatura senza anomalie: minimo a metà gennaio, massimo a fine luglio; minimo giornaliero all'alba."""
    stagionale = media - ampiezza * math.cos(2 * math.pi * (doy - 18) / 365.25)
    giornaliera = escursione * math.cos(2 * math.pi * (ora - 15) / 24)
    return stagionale + giornaliera


def effetto_temperatura_elettrico(temperatura: float) -> float:
    """Moltiplicatore del carico elettrico: condizionatori d'estate, pompe di calore d'inverno."""
    return 1 + 0.028 * max(0.0, temperatura - SOGLIA_RAFFRESCAMENTO) + 0.006 * max(0.0, 10.0 - temperatura)


def gradi_riscaldamento(temperatura: float) -> float:
    return max(0.0, SOGLIA_RISCALDAMENTO - temperatura)


def seno_elevazione_solare(latitudine: float, doy: float, ora: float) -> float:
    """Seno dell'elevazione del sole; `ora` è l'ora locale (fuso di Roma, senza ora legale)."""
    declinazione = math.radians(23.44) * math.sin(2 * math.pi * (284 + doy) / 365)
    angolo_orario = math.radians(15 * (ora - 12.5))
    lat = math.radians(latitudine)
    return math.sin(lat) * math.sin(declinazione) + math.cos(lat) * math.cos(declinazione) * math.cos(angolo_orario)


def fattore_solare(latitudine: float, doy: float, ora: float, nuvole: float) -> float:
    """Frazione della potenza fotovoltaica installata effettivamente prodotta."""
    s = seno_elevazione_solare(latitudine, doy, ora)
    if s <= 0:
        return 0.0
    cielo_sereno = 0.84 * s ** 1.15
    return cielo_sereno * (1 - 0.72 * nuvole)


def fattore_eolico(vento: float) -> float:
    """Curva di potenza di un aerogeneratore tipico (attacco 3 m/s, nominale 12, arresto 25)."""
    if vento < 3 or vento >= 25:
        return 0.0
    if vento >= 12:
        return 1.0
    return ((vento - 3) / 9) ** 3


def fattore_idro_fluente(doy: float) -> float:
    """Portata dei fiumi: massima con lo scioglimento delle nevi (fine maggio), minima a fine estate."""
    return 0.38 + 0.22 * math.cos(2 * math.pi * (doy - 150) / 365.25)


def afflusso_bacino(doy: float) -> float:
    """Afflusso ai bacini in frazione della potenza installata."""
    return 0.24 + 0.16 * math.cos(2 * math.pi * (doy - 140) / 365.25)


def nuvole_stagionali(inverno: float, estate: float, doy: float) -> float:
    peso_estate = 0.5 - 0.5 * math.cos(2 * math.pi * (doy - 18) / 365.25)
    return inverno + (estate - inverno) * peso_estate


def vento_stagionale(medio: float, doy: float) -> float:
    return medio * (1 + 0.18 * math.cos(2 * math.pi * (doy - 30) / 365.25))


def obiettivo_stoccaggio_gas(doy: float) -> float:
    """
    Riempimento desiderato degli stoccaggi: dal 30% a inizio aprile al 95% a inizio novembre, poi erogazione invernale.
    """
    inizio_aprile, inizio_novembre = 91, 305
    if inizio_aprile <= doy < inizio_novembre:
        return 0.30 + 0.65 * (doy - inizio_aprile) / (inizio_novembre - inizio_aprile)
    giorni_da_novembre = (doy - inizio_novembre) % 365
    return 0.95 - 0.65 * giorni_da_novembre / (365 - (inizio_novembre - inizio_aprile))


def stagione_iniezione(doy: float) -> bool:
    return 91 <= doy < 305

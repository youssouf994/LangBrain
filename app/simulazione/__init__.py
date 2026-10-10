"""
Simulazione di un sistema energetico (elettricità, gas e accumuli) con valori dinamici.

Il motore è deterministico a parità di seed: stessi parametri, stessa storia. La rete è modellata per zone di mercato
(come le zone di Terna): dentro una zona la rete è un nodo unico, tra le zone ci sono limiti di transito.

  modello.py     entità (zone, centrali, città, accumuli, linee, ingressi e stoccaggi gas, condotte) e tecnologie
  ambiente.py    meteo, profili di carico e calendario
  scenari.py     scenario Italia (dati plausibili, non reali) e reti generate per le prove di carico
  motore.py      il simulatore: dispacciamento elettrico e gas, eventi, anomalie, statistiche
  esecutore.py   esecuzione in background con durata e velocità regolabili
"""

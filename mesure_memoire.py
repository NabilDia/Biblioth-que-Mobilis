"""Mesure le pic mémoire du pipeline : python mesure_memoire.py <trajets.csv.gz>"""

import sys
import time
import tracemalloc
from pathlib import Path

from mobilis.trajets import compter_flux, exploitables, lire_trajets, sans_doublons

tracemalloc.start()
depart = time.perf_counter()
flux = compter_flux(exploitables(sans_doublons(lire_trajets(Path(sys.argv[1])))))
duree = time.perf_counter() - depart
pic = tracemalloc.get_traced_memory()[1] / 1e6
print(f"{flux.departs.total()} trajets exploitables, pic {pic:.1f} Mo, {duree:.0f} s")

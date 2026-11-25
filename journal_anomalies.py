"""Compte les anomalies des exports : python journal_anomalies.py <dossier des données>"""

import csv
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from mobilis.facturation import Formule, lire_abonnements
from mobilis.stations import Statut, lire_stations
from mobilis.trajets import convertir, lire_lignes, sans_doublons

donnees = Path(sys.argv[1])
trajets_csv = sorted(donnees.glob("trajets_*.csv.gz"))

print("stations.csv")
stations, anomalies = lire_stations(donnees / "stations.csv")
compte: Counter[str] = Counter(anomalie.traitement.value for anomalie in anomalies)
with (donnees / "stations.csv").open(encoding="cp1252", newline="") as fichier:
    for ligne in csv.DictReader(fichier, delimiter=";"):
        compte["(lignes)"] += 1
        compte["statut écrit autrement"] += ligne["Statut"] not in {s.value for s in Statut}
        compte["espaces en trop"] += any(valeur != valeur.strip() for valeur in ligne.values())
        compte["date au format ISO"] += "-" in ligne["Mise en service"]
for nom, nombre in sorted(compte.items()):
    print(f"  {nom} : {nombre}")

print("trajets")
compte = Counter()
abonnes_vus: set[str] = set()
for chemin in trajets_csv:
    for _, ligne in lire_lignes(chemin):
        compte["(lignes)"] += 1
        compte["début sans fuseau"] += "/" in ligne["debut"]
        compte["début en UTC (Z)"] += ligne["debut"].endswith("Z")
        compte["distance vide"] += not ligne["distance_m"]
        compte["sans station ou heure d'arrivée"] += not (ligne["station_arrivee"] and ligne["fin"])
        compte["sans abonné"] += not ligne["abonne_id"]
    precedent: datetime | None = None
    for trajet in convertir(lire_lignes(chemin)):
        compte["convertis"] += 1
        compte["plus ancien que la ligne précédente"] += (
            precedent is not None and trajet.debut < precedent
        )
        precedent = trajet.debut
        if trajet.abonne_id is not None:
            abonnes_vus.add(trajet.abonne_id)
compte["distincts"] = sum(1 for c in trajets_csv for _ in sans_doublons(convertir(lire_lignes(c))))
compte["doublons"] = compte["convertis"] - compte["distincts"]
compte["illisibles"] = compte["(lignes)"] - compte["convertis"]
for nom, nombre in sorted(compte.items()):
    print(f"  {nom} : {nombre}")

print("abonnes.jsonl")
abonnements = lire_abonnements(donnees / "abonnes.jsonl")  # signale chaque ligne abîmée
with (donnees / "abonnes.jsonl").open(encoding="utf-8", errors="replace") as fichier:
    textes = list(fichier)
pleines = [bool(texte.strip()) for texte in textes]
ecrites = [m[1] for texte in textes if (m := re.search(r'"formule": "([^"]*)"', texte))]
periodes = sum(len(p) for p in abonnements.values())
print(f"  (lignes) : {len(pleines)}")
print(f"  lignes vides : {pleines.count(False)}")
print(f"  lignes abîmées : {pleines.count(True) - periodes}")
print(f"  formule écrite autrement : {sum(f not in {g.value for g in Formule} for f in ecrites)}")
print(f"  abonnés à plusieurs périodes : {sum(len(p) > 1 for p in abonnements.values())}")
print(f"  identifiants de trajets introuvables : {len(abonnes_vus - abonnements.keys())}")

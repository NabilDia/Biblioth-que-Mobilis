"""Référentiel des stations : lecture et nettoyage de stations.csv."""

import csv
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

ABSENT = {"", "NC"}
OBLIGATOIRES = {"Code station", "Statut", "Mise en service"}


class Statut(Enum):
    """États possibles d'une station dans le référentiel."""

    EN_SERVICE = "EN SERVICE"
    HORS_SERVICE = "HORS SERVICE"
    FERMEE = "FERMEE"


@dataclass(frozen=True)
class Station:
    """Une station nettoyée ; None remplace une valeur absente de l'export."""

    code: str
    nom: str
    commune: str
    capacite: int | None
    latitude: float | None
    longitude: float | None
    statut: Statut
    mise_en_service: date


class Traitement(Enum):
    """Ce que la lecture a fait d'une anomalie."""

    REJETEE = "Ligne rejetée"
    DOUBLON = "Station en double"
    CORRIGEE = "Valeur corrigée"
    ABSENTE = "Valeur absente, station gardée"


@dataclass(frozen=True)
class Anomalie:
    """Un défaut relevé dans stations.csv, avec sa ligne et son traitement."""

    ligne: int
    code: str
    champ: str
    valeur: str
    traitement: Traitement
    detail: str


def lire_stations(chemin: Path) -> tuple[dict[str, Station], list[Anomalie]]:
    """Renvoie les stations par code et toutes les anomalies rencontrées."""
    stations: dict[str, Station] = {}
    anomalies: list[Anomalie] = []
    with chemin.open(encoding="cp1252", newline="") as fichier:
        lecteur = csv.DictReader(fichier, delimiter=";")
        for ligne in lecteur:
            station, trouvees = _convertir(ligne, lecteur.line_num)
            anomalies += trouvees
            if station is None:
                continue
            connue = stations.get(station.code)
            if connue is not None:
                detail = (
                    f"déjà lue avec {connue.mise_en_service}, ici {station.mise_en_service} : "
                    "la plus récente est gardée"
                )
                anomalies.append(
                    Anomalie(
                        lecteur.line_num,
                        station.code,
                        "Code station",
                        station.code,
                        Traitement.DOUBLON,
                        detail,
                    )
                )
                if connue.mise_en_service > station.mise_en_service:
                    continue
            stations[station.code] = station
    return stations, anomalies


def rapport(anomalies: list[Anomalie]) -> str:
    """Présente les anomalies regroupées par traitement."""
    lignes = [f"{len(anomalies)} anomalie(s)"]
    for traitement in Traitement:
        groupe = [a for a in anomalies if a.traitement is traitement]
        if groupe:
            lignes.append(f"\n{traitement.value} ({len(groupe)})")
            lignes += [
                f"  ligne {a.ligne}, {a.code or '?'}, {a.champ} = {a.valeur!r} : {a.detail}"
                for a in groupe
            ]
    return "\n".join(lignes)


def _convertir(ligne: dict[str, str], numero: int) -> tuple[Station | None, list[Anomalie]]:
    code_lu = (ligne["Code station"] or "").strip()
    anomalies: list[Anomalie] = []

    def signaler(champ: str, traitement: Traitement, detail: str) -> None:
        valeur = ligne.get(champ) or ""
        anomalies.append(Anomalie(numero, code_lu, champ, valeur, traitement, detail))

    def lire(champ: str, convertir: Callable[[str], T]) -> T | None:
        texte = ligne[champ].strip()
        if texte in ABSENT:
            traitement = Traitement.REJETEE if champ in OBLIGATOIRES else Traitement.ABSENTE
            signaler(champ, traitement, "valeur absente")
            return None
        try:
            return convertir(texte)
        except ValueError:
            signaler(champ, Traitement.REJETEE, "valeur invalide")
            return None

    if None in ligne or None in ligne.values():
        signaler("(ligne)", Traitement.REJETEE, "nombre de colonnes inattendu")
        return None, anomalies

    code = lire("Code station", str)
    nom = lire("Nom", str)
    commune = lire("Commune", str)
    capacite = lire("Capacité", _capacite)
    latitude = lire("Latitude", _degres)
    longitude = lire("Longitude", _degres)
    statut = lire("Statut", _statut)
    mise_en_service = lire("Mise en service", _date)

    if latitude is None or longitude is None:
        latitude = longitude = None
    # shortcut: vrai pour cette agglomération (lat ≈ 45, lon ≈ 4), à revoir si le réseau change.
    elif latitude < longitude:
        latitude, longitude = longitude, latitude
        signaler("Latitude", Traitement.CORRIGEE, "inversée avec la longitude")

    rejetee = any(a.traitement is Traitement.REJETEE for a in anomalies)
    if rejetee or code is None or statut is None or mise_en_service is None:
        return None, anomalies
    station = Station(
        code=code,
        nom=nom or "",
        commune=commune or "",
        capacite=capacite,
        latitude=latitude,
        longitude=longitude,
        statut=statut,
        mise_en_service=mise_en_service,
    )
    return station, anomalies


def _capacite(texte: str) -> int:
    capacite = int(texte)
    if capacite <= 0:
        raise ValueError("capacité négative ou nulle")
    return capacite


def _degres(texte: str) -> float:
    degres = float(texte.replace(",", "."))
    if not -180 <= degres <= 180:  # écarte aussi nan
        raise ValueError("coordonnée hors limites")
    return degres


def _statut(texte: str) -> Statut:
    texte = texte.upper().replace("_", " ")
    return Statut.HORS_SERVICE if texte == "HS" else Statut(texte)


def _date(texte: str) -> date:
    if "/" in texte:
        return datetime.strptime(texte, "%d/%m/%Y").date()
    return date.fromisoformat(texte)

"""Pipeline des trajets : lecture, conversion, dédoublonnage, filtres et agrégations."""

import csv
import gzip
import logging
from collections import Counter, deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PARIS = ZoneInfo("Europe/Paris")
DUREE_FANTOME = timedelta(minutes=2)  # R8
DUREE_MAX = timedelta(hours=24)  # R9 : seuil_non_restitution_heures de tarifs.json
MATIN = range(7, 10)
SOIR = range(17, 20)


class TypeVelo(Enum):
    """Types de vélo de l'export."""

    MECANIQUE = "mecanique"
    ELECTRIQUE = "electrique"


@dataclass(frozen=True, slots=True)
class Trajet:
    """Un trajet ; debut et fin sont en UTC, pour que leur écart soit une vraie durée."""

    trajet_id: str
    abonne_id: str | None
    velo_id: str
    type_velo: TypeVelo
    station_depart: str
    debut: datetime
    station_arrivee: str | None
    fin: datetime | None
    distance_m: int | None


def lire_lignes(chemin: Path) -> Iterator[tuple[int, dict[str, str]]]:
    """Étape 1 : décompresse et découpe le fichier, une ligne numérotée à la fois."""
    with gzip.open(chemin, "rt", encoding="utf-8", newline="") as fichier:
        lecteur = csv.DictReader(fichier)
        for ligne in lecteur:
            yield lecteur.line_num, ligne


def convertir(lignes: Iterable[tuple[int, dict[str, str]]]) -> Iterator[Trajet]:
    """Étape 2 : lignes brutes -> trajets ; une ligne illisible est signalée puis ignorée."""
    for numero, ligne in lignes:
        try:
            # Sans ce contrôle, une ligne tronquée passerait pour un vélo non restitué.
            if None in ligne or None in ligne.values():
                raise ValueError("nombre de colonnes inattendu")
            trajet = _convertir(ligne)
        except ValueError as erreur:
            logger.warning("trajets, ligne %d ignorée : %s", numero, erreur)
            continue
        yield trajet


def lire_trajets(chemin: Path) -> Iterator[Trajet]:
    """Étapes 1 et 2 : produit les trajets un par un, doublons compris."""
    return convertir(lire_lignes(chemin))


def sans_doublons(trajets: Iterable[Trajet], fenetre: int = 100_000) -> Iterator[Trajet]:
    """Étape 3 : ne laisse passer qu'une fois chaque trajet_id."""
    # shortcut: un doublon plus éloigné que `fenetre` trajets passe, à agrandir si cela arrive
    # (ils se suivent tous dans l'export d'août à octobre 2026).
    recents: deque[str] = deque()
    vus: set[str] = set()
    for trajet in trajets:
        if trajet.trajet_id in vus:
            continue
        if len(recents) == fenetre:
            vus.remove(recents.popleft())
        recents.append(trajet.trajet_id)
        vus.add(trajet.trajet_id)
        yield trajet


def exploitables(trajets: Iterable[Trajet]) -> Iterator[Trajet]:
    """Étape 4 : écarte les trajets qui ne décrivent pas un déplacement (R8, R9, R10)."""
    for trajet in trajets:
        if trajet.fin is None or trajet.station_arrivee is None:
            continue
        duree = trajet.fin - trajet.debut
        if not timedelta(0) <= duree <= DUREE_MAX:
            continue
        if duree < DUREE_FANTOME and trajet.station_arrivee == trajet.station_depart:
            continue
        yield trajet


@dataclass
class Flux:
    """Comptages par (station, week-end ?, heure de Paris) : leur taille ne dépend pas du fichier."""

    departs: Counter[tuple[str, bool, int]] = field(default_factory=Counter)
    arrivees: Counter[tuple[str, bool, int]] = field(default_factory=Counter)
    jours: set[date] = field(default_factory=set)

    def nombre_de_jours(self, weekend: bool) -> int:
        """Nombre de jours observés, en semaine ou le week-end."""
        return sum(_est_weekend(jour) == weekend for jour in self.jours)


def compter_flux(trajets: Iterable[Trajet]) -> Flux:
    """Étape 5 : consomme le pipeline ; les deux analyses se déduisent du résultat."""
    flux = Flux()
    for trajet in trajets:
        debut = trajet.debut.astimezone(PARIS)
        flux.jours.add(debut.date())
        flux.departs[trajet.station_depart, _est_weekend(debut), debut.hour] += 1
        if trajet.fin is not None and trajet.station_arrivee is not None:
            fin = trajet.fin.astimezone(PARIS)
            flux.arrivees[trajet.station_arrivee, _est_weekend(fin), fin.hour] += 1
    return flux


def profil_horaire(flux: Flux, weekend: bool) -> list[float]:
    """Départs moyens par jour, pour chaque heure de 0 à 23."""
    total: Counter[int] = Counter()
    for (_, est_weekend, heure), nombre in flux.departs.items():
        if est_weekend == weekend:
            total[heure] += nombre
    jours = flux.nombre_de_jours(weekend)
    return [total[heure] / jours if jours else 0.0 for heure in range(24)]


def solde(flux: Flux, station: str, heures: range) -> float:
    """Arrivées moins départs, par jour de semaine : négatif si la station se vide."""
    net = sum(
        flux.arrivees[station, False, heure] - flux.departs[station, False, heure]
        for heure in heures
    )
    return net / (flux.nombre_de_jours(weekend=False) or 1)


def vides_le_matin(flux: Flux) -> dict[str, tuple[float, float]]:
    """Stations qui se vident le matin et se remplissent le soir : (solde matin, solde soir)."""
    stations = {cle[0] for cle in flux.departs} | {cle[0] for cle in flux.arrivees}
    soldes = {s: (solde(flux, s, MATIN), solde(flux, s, SOIR)) for s in sorted(stations)}
    return {s: (matin, soir) for s, (matin, soir) in soldes.items() if matin < 0 < soir}


def _convertir(ligne: dict[str, str]) -> Trajet:
    if not ligne["trajet_id"]:
        raise ValueError("trajet_id vide")
    return Trajet(
        trajet_id=ligne["trajet_id"],
        abonne_id=ligne["abonne_id"] or None,
        velo_id=ligne["velo_id"],
        type_velo=TypeVelo(ligne["type_velo"]),
        station_depart=ligne["station_depart"],
        debut=_instant(ligne["debut"]),
        station_arrivee=ligne["station_arrivee"] or None,
        fin=_instant(ligne["fin"]) if ligne["fin"] else None,
        distance_m=int(ligne["distance_m"]) if ligne["distance_m"] else None,
    )


def _instant(texte: str) -> datetime:
    if "/" in texte:
        instant = datetime.strptime(texte, "%d/%m/%Y %H:%M:%S")
    else:
        instant = datetime.fromisoformat(texte)
    if instant.tzinfo is None:
        # shortcut: l'heure répétée d'octobre est lue comme la première (heure d'été),
        # à revoir si une borne sans fuseau y enregistre un trajet (aucun d'août à octobre 2026).
        instant = instant.replace(tzinfo=PARIS)
    return instant.astimezone(UTC)


def _est_weekend(jour: date) -> bool:
    return jour.weekday() >= 5

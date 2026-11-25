"""Photographie des stations par l'API des bornes et comparaison au référentiel."""

import functools
import json
import random
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from http.client import HTTPException
from threading import BoundedSemaphore
from typing import Any, ParamSpec, TypeVar
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from mobilis.stations import Station, Statut

P = ParamSpec("P")
R = TypeVar("R")

SIMULTANES = 8
DELAI_REQUETE = 5.0  # secondes
ATTENTE_MAX = 5.0
STATUTS_A_REESSAYER = {429, 503, 504}


class ErreurBornes(Exception):
    """L'API a répondu une erreur qu'il est inutile de réessayer."""


class ErreurTemporaire(ErreurBornes):
    """Erreur à réessayer, après le délai que l'API demande s'il y en a un."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def attente(erreur: ErreurTemporaire, tentative: int) -> float:
    """Retry-After s'il est fourni ; sinon une pause qui double, avec une part de hasard."""
    if erreur.retry_after is not None:
        return min(erreur.retry_after, ATTENTE_MAX)
    return float(0.2 * 2 ** (tentative - 1) * random.uniform(0.5, 1.5))


def reessayer(tentatives: int = 4) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Décorateur : rappelle la fonction tant qu'elle lève ErreurTemporaire."""

    def decorer(fonction: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fonction)
        def enveloppe(*args: P.args, **kwargs: P.kwargs) -> R:
            for tentative in range(1, tentatives):
                try:
                    return fonction(*args, **kwargs)
                except ErreurTemporaire as erreur:
                    time.sleep(attente(erreur, tentative))
            return fonction(*args, **kwargs)

        return enveloppe

    return decorer


@dataclass(frozen=True)
class EtatStation:
    """Ce que la borne d'une station déclare à un instant donné."""

    code: str
    velos_mecaniques: int
    velos_electriques: int
    places_libres: int
    horodatage: datetime

    @property
    def velos(self) -> int:
        """Vélos présents, mécaniques et électriques."""
        return self.velos_mecaniques + self.velos_electriques

    @property
    def capacite(self) -> int:
        """Nombre total d'emplacements vu par la borne."""
        return self.velos + self.places_libres

    @property
    def vide(self) -> bool:
        """Vrai s'il n'y a plus de vélo à prendre."""
        return self.velos == 0

    @property
    def pleine(self) -> bool:
        """Vrai s'il n'y a plus de place pour rendre un vélo."""
        return self.places_libres == 0


class ClientBornes:
    """Accès à l'API des bornes, limité à 8 appels simultanés par client."""

    def __init__(self, adresse: str, cle: str) -> None:
        self._adresse = adresse.rstrip("/")
        self._cle = cle
        self._places = BoundedSemaphore(SIMULTANES)

    def codes(self) -> Iterator[str]:
        """Codes de toutes les stations, page après page."""
        page: int | None = 1
        while page is not None:
            reponse = self._get(f"/stations?page={page}")
            yield from reponse["stations"]
            page = reponse["page_suivante"]

    def etat(self, code: str) -> EtatStation:
        """Lit l'état d'une station ; lève ErreurBornes si l'API ne le donne pas."""
        reponse = self._get(f"/stations/{quote(code, safe='')}")
        try:
            return EtatStation(
                code=reponse["code"],
                velos_mecaniques=int(reponse["velos_mecaniques"]),
                velos_electriques=int(reponse["velos_electriques"]),
                places_libres=int(reponse["places_libres"]),
                horodatage=datetime.fromisoformat(reponse["horodatage"]),
            )
        except (KeyError, TypeError, ValueError) as erreur:
            raise ErreurBornes(f"réponse invalide : {erreur!r}") from erreur

    @reessayer()
    def _get(self, chemin: str) -> Any:
        requete = Request(self._adresse + chemin, headers={"X-Api-Key": self._cle})
        try:
            # La place est rendue avant l'attente du réessai, qui a lieu dans le décorateur.
            with self._places, urlopen(requete, timeout=DELAI_REQUETE) as reponse:
                return json.load(reponse)
        except HTTPError as erreur:
            erreur.close()
            if erreur.code in STATUTS_A_REESSAYER:
                delai = erreur.headers.get("Retry-After", "")
                raise ErreurTemporaire(
                    f"HTTP {erreur.code}", float(delai) if delai.isdigit() else None
                ) from erreur
            raise ErreurBornes(f"HTTP {erreur.code}") from erreur
        except (OSError, HTTPException) as erreur:  # coupure, délai dépassé
            raise ErreurTemporaire(f"réseau : {erreur}") from erreur
        except ValueError as erreur:
            raise ErreurBornes("réponse JSON invalide") from erreur


@dataclass
class Photo:
    """États reçus pendant une photographie, et raison de chaque absence."""

    etats: dict[str, EtatStation] = field(default_factory=dict)
    injoignables: dict[str, str] = field(default_factory=dict)  # code -> raison

    @property
    def codes(self) -> set[str]:
        """Toutes les stations annoncées par l'API, jointes ou non."""
        return self.etats.keys() | self.injoignables.keys()

    @property
    def vides(self) -> list[str]:
        """Codes des stations sans vélo, triés."""
        return sorted(code for code, etat in self.etats.items() if etat.vide)

    @property
    def pleines(self) -> list[str]:
        """Codes des stations sans place libre, triés."""
        return sorted(code for code, etat in self.etats.items() if etat.pleine)


def photographier(client: ClientBornes, limite: float = 14.0) -> Photo:
    """Interroge toutes les stations ; au-delà de `limite` secondes, rend ce qui est arrivé."""
    fin = time.monotonic() + limite
    photo = Photo()
    futurs: dict[Future[EtatStation], str] = {}
    executeur = ThreadPoolExecutor(SIMULTANES)

    def lister() -> None:
        for code in client.codes():
            futurs[executeur.submit(client.etat, code)] = code

    try:
        # La liste occupe un des 8 threads et les états partent dès la première page : elle ne
        # dispute donc pas sa place aux états, et son thread les rejoint une fois terminée.
        liste = executeur.submit(lister)
        if not wait([liste], timeout=limite).done:
            raise ErreurBornes(f"liste des stations non reçue en {limite:g} s")
        liste.result()
        wait(futurs, timeout=max(0.0, fin - time.monotonic()))
    finally:
        executeur.shutdown(wait=False, cancel_futures=True)
    for futur, code in futurs.items():
        if futur.cancelled() or not futur.done():
            photo.injoignables[code] = "délai dépassé"
            continue
        try:
            photo.etats[code] = futur.result()
        except ErreurBornes as erreur:
            photo.injoignables[code] = str(erreur)
    return photo


@dataclass(frozen=True)
class Ecarts:
    """Désaccords entre l'API des bornes et le référentiel."""

    inconnues: list[str]  # dans l'API, pas dans le référentiel
    absentes: list[str]  # non fermées dans le référentiel, pas dans l'API
    fermees_presentes: list[str]
    hors_service_qui_repondent: list[str]
    capacites: dict[str, tuple[int, int]]  # code -> (référentiel, borne)


def comparer(photo: Photo, stations: Mapping[str, Station]) -> Ecarts:
    """Confronte une photographie au référentiel des stations."""

    def au_statut(statut: Statut) -> set[str]:
        return {code for code, station in stations.items() if station.statut is statut}

    capacites: dict[str, tuple[int, int]] = {}
    for code, etat in sorted(photo.etats.items()):
        station = stations.get(code)
        if station is None or station.capacite is None:
            continue
        if station.capacite != etat.capacite:
            capacites[code] = (station.capacite, etat.capacite)
    fermees = au_statut(Statut.FERMEE)
    return Ecarts(
        inconnues=sorted(photo.codes - stations.keys()),
        absentes=sorted(stations.keys() - fermees - photo.codes),
        fermees_presentes=sorted(fermees & photo.codes),
        hors_service_qui_repondent=sorted(au_statut(Statut.HORS_SERVICE) & photo.etats.keys()),
        capacites=capacites,
    )

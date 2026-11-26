"""Facturation des trajets : règles R1 à R12 de specification_facturation.md."""

import json
import logging
from collections.abc import Container, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from mobilis.trajets import DUREE_FANTOME, PARIS, Trajet, TypeVelo, sans_doublons

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
CENTIME = Decimal("0.01")
JOURS_OUVERTS = 7  # les bornes hors réseau ont au plus 78 h de retard (août à octobre 2026)


class Formule(Enum):
    """Formules tarifaires de tarifs.json."""

    ANNUEL = "annuel"
    MENSUEL = "mensuel"
    SOLIDAIRE = "solidaire"
    OCCASIONNEL = "occasionnel"


class Motif(Enum):
    """Raison du montant d'une facture."""

    USAGE = "usage"
    PENALITE = "non-restitution"
    FANTOME = "trajet fantôme"
    INCOHERENT = "horodatage incohérent"


class Signalement(Enum):
    """Point à transmettre à une équipe, sans effet sur le prix."""

    ABONNE_INTROUVABLE = "abonné introuvable"
    STATION_INCONNUE = "station inconnue"
    PLAFOND_NON_VERIFIE = "trajet trop en retard pour vérifier le plafond"


class Tarification(Protocol):
    """Tout ce qui sait donner le prix d'un trajet."""

    def prix(self, trajet: Trajet) -> Decimal:
        """Prix du trajet, avant arrondi et plafond."""


@dataclass(frozen=True)
class Forfait:
    """Montant fixe, quelle que soit la durée : pénalité (R9) ou gratuité (R8, R10)."""

    montant: Decimal

    def prix(self, trajet: Trajet) -> Decimal:
        """Le montant fixe, sans regarder le trajet."""
        return self.montant


@dataclass(frozen=True)
class TarifTranches:
    """Prix à la durée d'une formule (R4 et R5)."""

    minutes_incluses: timedelta
    tranche: timedelta
    prix_tranche: Decimal
    supplement_electrique_tranche: Decimal
    frais_deblocage: Decimal
    plafond_jour: Decimal | None

    def prix(self, trajet: Trajet) -> Decimal:
        """Déblocage, tranches d'usage et supplément électrique, avant plafond."""
        if trajet.fin is None:
            raise ValueError(f"trajet {trajet.trajet_id} sans fin")
        duree = trajet.fin - trajet.debut
        usage = max(timedelta(0), duree - self.minutes_incluses)
        montant = self.frais_deblocage + self._entamees(usage) * self.prix_tranche
        if trajet.type_velo is TypeVelo.ELECTRIQUE:
            montant += self._entamees(duree) * self.supplement_electrique_tranche
        return montant

    def _entamees(self, duree: timedelta) -> int:
        return -(-duree // self.tranche)  # division entière arrondie vers le haut, sans float


@dataclass(frozen=True)
class Tarifs:
    """Contenu de tarifs.json : pénalité, seuil et grilles datées."""

    penalite: Decimal
    seuil_non_restitution: timedelta
    grilles: tuple[tuple[date, dict[Formule, TarifTranches]], ...]

    def tarif(self, formule: Formule, jour: date) -> TarifTranches:
        """R1 : la dernière grille entrée en vigueur à cette date."""
        _, formules = max((g for g in self.grilles if g[0] <= jour), key=lambda grille: grille[0])
        return formules[formule]


@dataclass(frozen=True)
class Abonnement:
    """Une période d'abonnement, bornes comprises ; fin vide = en cours."""

    formule: Formule
    debut: date
    fin: date | None


@dataclass(frozen=True)
class Facture:
    """Montant dû pour un trajet, avec sa justification."""

    trajet_id: str
    jour: date
    formule: Formule
    motif: Motif
    montant: Decimal
    signalements: tuple[Signalement, ...]


@dataclass
class Cumul:
    """Nombre et somme de factures."""

    nombre: int = 0
    montant: Decimal = ZERO


def lire_tarifs(chemin: Path) -> Tarifs:
    """Lit tarifs.json sans jamais passer par un float."""
    # parse_float : même un prix écrit sans guillemets ne passe jamais par un float.
    donnees = json.loads(chemin.read_text(encoding="utf-8"), parse_float=Decimal)
    return Tarifs(
        penalite=Decimal(donnees["penalite_non_restitution"]),
        seuil_non_restitution=timedelta(hours=donnees["seuil_non_restitution_heures"]),
        grilles=tuple(
            (
                date.fromisoformat(grille["en_vigueur_du"]),
                {Formule(nom): _tarif(valeurs) for nom, valeurs in grille["formules"].items()},
            )
            for grille in donnees["grilles"]
        ),
    )


def _tarif(valeurs: dict[str, Any]) -> TarifTranches:
    plafond = valeurs["plafond_jour"]
    return TarifTranches(
        minutes_incluses=timedelta(minutes=valeurs["minutes_incluses"]),
        tranche=timedelta(minutes=valeurs["tranche_minutes"]),
        prix_tranche=Decimal(valeurs["prix_tranche"]),
        supplement_electrique_tranche=Decimal(valeurs["supplement_electrique_tranche"]),
        frais_deblocage=Decimal(valeurs["frais_deblocage"]),
        plafond_jour=None if plafond is None else Decimal(plafond),
    )


def lire_abonnements(chemin: Path) -> dict[str, list[Abonnement]]:
    """Périodes d'abonnement par abonné ; une ligne abîmée est signalée puis ignorée."""
    abonnements: dict[str, list[Abonnement]] = {}
    with chemin.open(encoding="utf-8", errors="replace") as fichier:
        for numero, texte in enumerate(fichier, start=1):
            if not texte.strip():
                continue
            try:
                ligne = json.loads(texte)
                abonnement = Abonnement(
                    formule=Formule(ligne["formule"].strip().lower()),
                    debut=date.fromisoformat(ligne["debut"]),
                    fin=date.fromisoformat(ligne["fin"]) if ligne["fin"] else None,
                )
                abonnements.setdefault(ligne["id"], []).append(abonnement)
            except (ValueError, KeyError, TypeError, AttributeError) as erreur:
                logger.warning("abonnés, ligne %d ignorée : %s", numero, erreur)
    return abonnements


def facturer(
    trajets: Iterable[Trajet],
    tarifs: Tarifs,
    abonnements: dict[str, list[Abonnement]],
    stations: Container[str],
) -> Iterator[Facture]:
    """Produit une facture par trajet distinct (R11)."""
    plafonds = _Plafonds()
    for trajet in sans_doublons(trajets):
        jour = trajet.debut.astimezone(PARIS).date()
        signalements: list[Signalement] = []
        if trajet.abonne_id is not None and trajet.abonne_id not in abonnements:
            signalements.append(Signalement.ABONNE_INTROUVABLE)
        if any(
            s is not None and s not in stations
            for s in (trajet.station_depart, trajet.station_arrivee)
        ):
            signalements.append(Signalement.STATION_INCONNUE)  # R12

        formule = _formule(abonnements.get(trajet.abonne_id or "", []), jour)
        tarif = tarifs.tarif(formule, jour)
        motif, tarification = _tarification(trajet, tarif, tarifs)
        montant = tarification.prix(trajet).quantize(CENTIME, ROUND_HALF_UP)  # R7

        if motif is Motif.USAGE and trajet.abonne_id and tarif.plafond_jour is not None:
            if not plafonds.ouvrir(jour):
                signalements.append(Signalement.PLAFOND_NON_VERIFIE)
            montant = plafonds.appliquer(trajet.abonne_id, jour, montant, tarif.plafond_jour)
        yield Facture(trajet.trajet_id, jour, formule, motif, montant, tuple(signalements))


def _formule(periodes: list[Abonnement], jour: date) -> Formule:
    """R2 et R3 : la formule active ce jour-là, sinon occasionnel."""
    for periode in periodes:
        if periode.debut <= jour and (periode.fin is None or jour <= periode.fin):
            return periode.formule
    return Formule.OCCASIONNEL


def _tarification(
    trajet: Trajet, tarif: TarifTranches, tarifs: Tarifs
) -> tuple[Motif, Tarification]:
    """Choisit la tarification ; R10 passe avant R8, car une durée négative est aussi < 2 min."""
    if trajet.fin is not None and trajet.fin < trajet.debut:
        return Motif.INCOHERENT, Forfait(ZERO)  # R10
    if trajet.fin is None or trajet.station_arrivee is None:
        return Motif.PENALITE, Forfait(tarifs.penalite)  # R9
    duree = trajet.fin - trajet.debut
    if duree > tarifs.seuil_non_restitution:
        return Motif.PENALITE, Forfait(tarifs.penalite)  # R9
    if duree < DUREE_FANTOME and trajet.station_arrivee == trajet.station_depart:
        return Motif.FANTOME, Forfait(ZERO)  # R8
    return Motif.USAGE, tarif


class _Plafonds:
    """R6 : cumul par abonné et par jour, sur les derniers jours seulement (mémoire bornée)."""

    def __init__(self) -> None:
        self._cumuls: dict[date, dict[str, Decimal]] = {}

    def ouvrir(self, jour: date) -> bool:
        """Faux si ce jour, trop ancien, a déjà été oublié."""
        if jour in self._cumuls:
            return True
        if len(self._cumuls) == JOURS_OUVERTS:
            plus_ancien = min(self._cumuls)
            if jour < plus_ancien:
                return False
            del self._cumuls[plus_ancien]
        self._cumuls[jour] = {}
        return True

    def appliquer(self, abonne: str, jour: date, montant: Decimal, plafond: Decimal) -> Decimal:
        cumuls = self._cumuls.get(jour, {})
        deja = cumuls.get(abonne, ZERO)
        montant = min(montant, plafond - deja)
        cumuls[abonne] = deja + montant
        return montant


def recettes(factures: Iterable[Facture]) -> dict[tuple[str, Formule, Motif], Cumul]:
    """Nombre et montant par (mois « 2026-08 », formule, motif)."""
    totaux: dict[tuple[str, Formule, Motif], Cumul] = {}
    for facture in factures:
        cle = (f"{facture.jour:%Y-%m}", facture.formule, facture.motif)
        cumul = totaux.setdefault(cle, Cumul())
        cumul.nombre += 1
        cumul.montant += facture.montant
    return totaux

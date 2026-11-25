"""Commande `mobilis` : refait tous les chiffres à partir du dossier de données."""

import argparse
import os
import sys
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import replace
from decimal import Decimal
from itertools import chain
from pathlib import Path

from mobilis.bornes import ClientBornes, ErreurBornes, comparer, photographier
from mobilis.facturation import (
    ZERO,
    Cumul,
    Facture,
    Formule,
    Motif,
    Signalement,
    facturer,
    lire_abonnements,
    lire_tarifs,
    recettes,
)
from mobilis.stations import Station, Statut, lire_stations, rapport
from mobilis.trajets import (
    MATIN,
    SOIR,
    Trajet,
    compter_flux,
    exploitables,
    lire_trajets,
    profil_horaire,
    sans_doublons,
    solde,
    vides_le_matin,
)

Recettes = dict[tuple[str, Formule, Motif], Cumul]


def main(argv: Sequence[str] | None = None) -> int:
    parseur = _parseur()
    options = parseur.parse_args(argv)
    if options.donnees is None:
        parseur.error("indiquez le dossier des données : --donnees ou MOBILIS_DONNEES")
    try:
        options.commande(options)
    except (OSError, EOFError, ErreurBornes) as erreur:
        print(f"mobilis : {erreur}", file=sys.stderr)
        return 1
    return 0


def _parseur() -> argparse.ArgumentParser:
    commun = argparse.ArgumentParser(add_help=False)
    commun.add_argument(
        "--donnees",
        type=Path,
        default=os.environ.get("MOBILIS_DONNEES"),
        help="dossier des données (par défaut : variable MOBILIS_DONNEES)",
    )
    parseur = argparse.ArgumentParser(prog="mobilis", description=__doc__)
    commandes = parseur.add_subparsers(required=True, metavar="commande")
    for nom, fonction, aide in (
        ("stations", _stations, "référentiel et rapport d'anomalies"),
        ("frequentation", _frequentation, "profil horaire et stations vides le matin"),
        ("facturation", _facturation, "recettes par mois et effet de la dernière grille"),
        ("etat", _etat, "stations vides ou pleines en ce moment, écarts au référentiel"),
    ):
        commande = commandes.add_parser(nom, help=aide, parents=[commun])
        commande.set_defaults(commande=fonction)
        if nom == "etat":
            commande.add_argument(
                "--adresse",
                default=os.environ.get("MOBILIS_API", "http://127.0.0.1:8765"),
                help="adresse de l'API ; la clé se met dans MOBILIS_CLE",
            )
    return parseur


def _referentiel(options: argparse.Namespace) -> dict[str, Station]:
    return lire_stations(Path(options.donnees) / "stations.csv")[0]


def _trajets(options: argparse.Namespace) -> Iterator[Trajet]:
    fichiers = sorted(Path(options.donnees).glob("trajets_*.csv.gz"))
    if not fichiers:
        raise FileNotFoundError(f"aucun fichier trajets_*.csv.gz dans {options.donnees}")
    return chain.from_iterable(lire_trajets(fichier) for fichier in fichiers)


def _stations(options: argparse.Namespace) -> None:
    stations, anomalies = lire_stations(Path(options.donnees) / "stations.csv")
    en_service = [s for s in stations.values() if s.statut is Statut.EN_SERVICE]
    completes = [s for s in en_service if s.capacite is not None and s.latitude is not None]
    print(f"{len(stations)} stations distinctes")
    for statut in Statut:
        print(f"  {statut.value.lower()} : {sum(s.statut is statut for s in stations.values())}")
    print(f"  en service avec capacité et coordonnées : {len(completes)}\n")
    print(rapport(anomalies))


def _frequentation(options: argparse.Namespace) -> None:
    stations = _referentiel(options)
    flux = compter_flux(exploitables(sans_doublons(_trajets(options))))
    print(f"{flux.departs.total()} trajets exploitables\n")

    semaine, weekend = profil_horaire(flux, weekend=False), profil_horaire(flux, weekend=True)
    print(
        f"Départs moyens par jour ({flux.nombre_de_jours(False)} jours de semaine, "
        f"{flux.nombre_de_jours(True)} de week-end)"
    )
    print("heure  semaine  week-end")
    for heure in range(24):
        print(f"{heure:4} h {semaine[heure]:8.1f} {weekend[heure]:9.1f}")
    print(f"total  {sum(semaine):8.1f} {sum(weekend):9.1f}\n")

    vues = {cle[0] for cle in flux.departs} | {cle[0] for cle in flux.arrivees}
    connues = vues & stations.keys()
    vides = {code: soldes for code, soldes in vides_le_matin(flux).items() if code in connues}
    inverses = [s for s in connues if solde(flux, s, MATIN) > 0 > solde(flux, s, SOIR)]
    print(
        f"Sur {len(connues)} stations actives, {len(vides)} se vident de {MATIN.start} h à {MATIN.stop} h et se remplissent "
        f"de {SOIR.start} h à {SOIR.stop} h (semaine) ; {len(inverses)} font l'inverse"
    )
    print("solde matin et soir, en vélos par jour :")
    for code, (matin, soir) in sorted(vides.items(), key=lambda element: element[1]):
        print(f"  {code} {stations[code].nom:28} {matin:+6.1f} {soir:+6.1f}")

    en_service = {c for c, s in stations.items() if s.statut is Statut.EN_SERVICE}
    print(f"\nEn service sans aucun trajet : {_liste(en_service - vues)}")
    print(f"Absentes du référentiel : {_liste(vues - stations.keys())}")


def _facturation(options: argparse.Namespace) -> None:
    donnees = Path(options.donnees)
    stations = _referentiel(options)
    tarifs = lire_tarifs(donnees / "tarifs.json")
    abonnements = lire_abonnements(donnees / "abonnes.jsonl")
    signalements: Counter[Signalement] = Counter()
    totaux = recettes(
        _avec_signalements(facturer(_trajets(options), tarifs, abonnements, stations), signalements)
    )
    mois = sorted({cle[0] for cle in totaux})

    print(f"{'Recettes':12}" + "".join(f"{m:>14}" for m in mois))
    for formule in Formule:
        _ligne(formule.value, [_montant(totaux, m, Motif.USAGE, formule) for m in mois])
    _ligne("usage", [_montant(totaux, m, Motif.USAGE) for m in mois])
    _ligne("pénalités", [_montant(totaux, m, Motif.PENALITE) for m in mois])
    par_mois = [sum((c.montant for k, c in totaux.items() if k[0] == m), ZERO) for m in mois]
    _ligne("total", par_mois)
    print(f"Total général : {_euros(sum(par_mois, ZERO))}\n")

    for motif in Motif:
        print(f"{motif.value} : {sum(c.nombre for k, c in totaux.items() if k[2] is motif)}")
    for signalement in Signalement:
        print(f"{signalement.value} : {signalements[signalement]}")

    grilles = sorted(tarifs.grilles, key=lambda grille: grille[0])
    if len(grilles) < 2:
        return
    entree = grilles[-1][0]
    anciens = replace(tarifs, grilles=tuple(grilles[:-1]))
    print(f"\nEffet de la grille du {entree} sur l'usage, à trajets identiques")
    avant = _usage_depuis(
        facturer(_trajets(options), anciens, abonnements, stations), entree.isoformat()
    )
    apres = _usage_depuis(
        facturer(_trajets(options), tarifs, abonnements, stations), entree.isoformat()
    )
    print(f"{'':12}{'ancienne':>14}{'nouvelle':>14}{'effet':>10}")
    for nom, ancien, nouveau in [
        *((f.value, avant[f], apres[f]) for f in Formule),
        ("total", sum(avant.values(), ZERO), sum(apres.values(), ZERO)),
    ]:
        effet = f"{(nouveau - ancien) / ancien * 100:+.1f} %" if ancien else "-"
        print(f"{nom:12}{_euros(ancien):>14}{_euros(nouveau):>14}{effet:>10}")


def _avec_signalements(
    factures: Iterable[Facture], compteur: Counter[Signalement]
) -> Iterator[Facture]:
    for facture in factures:
        compteur.update(facture.signalements)
        yield facture


def _usage_depuis(factures: Iterable[Facture], jour: str) -> dict[Formule, Decimal]:
    usage = dict.fromkeys(Formule, ZERO)
    for facture in factures:
        if facture.motif is Motif.USAGE and facture.jour.isoformat() >= jour:
            usage[facture.formule] += facture.montant
    return usage


def _montant(totaux: Recettes, mois: str, motif: Motif, formule: Formule | None = None) -> Decimal:
    return sum(
        (
            cumul.montant
            for (m, f, mo), cumul in totaux.items()
            if m == mois and mo is motif and formule in (None, f)
        ),
        ZERO,
    )


def _ligne(nom: str, montants: list[Decimal]) -> None:
    print(f"{nom:12}" + "".join(f"{_euros(montant):>14}" for montant in montants))


def _euros(montant: Decimal) -> str:
    return f"{montant:,.2f} €".replace(",", " ").replace(".", ",")


def _liste(codes: Iterable[str]) -> str:
    return ", ".join(sorted(codes)) or "aucune"


def _etat(options: argparse.Namespace) -> None:
    cle = os.environ.get("MOBILIS_CLE")
    if not cle:
        raise ErreurBornes("indiquez la clé de l'API dans la variable MOBILIS_CLE")
    stations = _referentiel(options)
    depart = time.perf_counter()
    photo = photographier(ClientBornes(options.adresse, cle))
    duree = time.perf_counter() - depart
    ecarts = comparer(photo, stations)
    print(f"{len(photo.etats)} stations sur {len(photo.codes)} photographiées en {duree:.1f} s")
    print(f"Vides : {_liste(photo.vides)}")
    print(f"Pleines : {_liste(photo.pleines)}")
    raisons = (f"{code} ({raison})" for code, raison in photo.injoignables.items())
    print(f"Injoignables : {_liste(raisons)}\n")
    print("Écarts avec le référentiel")
    print(f"  inconnues du référentiel : {_liste(ecarts.inconnues)}")
    print(f"  non fermées, absentes de l'API : {_liste(ecarts.absentes)}")
    print(f"  fermées, présentes dans l'API : {_liste(ecarts.fermees_presentes)}")
    print(f"  hors service qui répondent : {_liste(ecarts.hors_service_qui_repondent)}")
    print(f"  capacités différentes : {len(ecarts.capacites)}")

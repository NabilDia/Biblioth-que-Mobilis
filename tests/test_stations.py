from datetime import date
from pathlib import Path

import pytest

from mobilis.stations import Anomalie, Station, Statut, Traitement, lire_stations, rapport

ENTETE = "Code station;Nom;Commune;Capacité;Latitude;Longitude;Statut;Mise en service"


def lire_tout(dossier: Path, *lignes: str) -> tuple[dict[str, Station], list[Anomalie]]:
    chemin = dossier / "stations.csv"
    chemin.write_bytes("\r\n".join([ENTETE, *lignes]).encode("cp1252"))
    return lire_stations(chemin)


def lire(dossier: Path, *lignes: str) -> dict[str, Station]:
    return lire_tout(dossier, *lignes)[0]


def test_nettoie_une_ligne(tmp_path: Path) -> None:
    ligne = "MB-0001;Hôpital Lamartine  ;Valmont; 37;45,78525;4,87212;En service ;16/12/2017"
    assert lire(tmp_path, ligne)["MB-0001"] == Station(
        code="MB-0001",
        nom="Hôpital Lamartine",
        commune="Valmont",
        capacite=37,
        latitude=45.78525,
        longitude=4.87212,
        statut=Statut.EN_SERVICE,
        mise_en_service=date(2017, 12, 16),
    )


def test_valeurs_manquantes_et_date_iso(tmp_path: Path) -> None:
    station = lire(tmp_path, "MB-1;Nom;Valmont;NC;;;En service;2024-06-05")["MB-1"]
    assert (station.capacite, station.latitude, station.longitude) == (None, None, None)
    assert station.mise_en_service == date(2024, 6, 5)


def test_coordonnees_inversees(tmp_path: Path) -> None:
    station = lire(tmp_path, "MB-1;Nom;Valmont;39;4,79377;45,73036;En service;18/09/2020")["MB-1"]
    assert (station.latitude, station.longitude) == (45.73036, 4.79377)


@pytest.mark.parametrize(
    ("texte", "attendu"),
    [
        ("En service ", Statut.EN_SERVICE),
        ("EN SERVICE", Statut.EN_SERVICE),
        ("en_service", Statut.EN_SERVICE),
        ("Hors service", Statut.HORS_SERVICE),
        ("HS", Statut.HORS_SERVICE),
        ("FERMEE", Statut.FERMEE),
    ],
)
def test_statuts(tmp_path: Path, texte: str, attendu: Statut) -> None:
    stations = lire(tmp_path, f"MB-1;Nom;Valmont;39;45,7;4,8;{texte};18/09/2020")
    assert stations["MB-1"].statut is attendu


@pytest.mark.parametrize("ordre", [1, -1])
def test_doublon_garde_la_plus_recente(tmp_path: Path, ordre: int) -> None:
    lignes = [
        "MB-1;Nom;Valmont;36;45,7;4,8;Hors service;15/05/2017",
        "MB-1;Nom;Valmont;40;45,7;4,8;En service;26/04/2026",
    ]
    stations = lire(tmp_path, *lignes[::ordre])
    assert stations["MB-1"].capacite == 40


BONNE = "MB-1;Nom;Valmont;39;45,7;4,8;En service;18/09/2020"


def test_fichier_propre_sans_anomalie(tmp_path: Path) -> None:
    stations, anomalies = lire_tout(tmp_path, BONNE)
    assert list(stations) == ["MB-1"]
    assert anomalies == []


@pytest.mark.parametrize(
    ("mauvaise", "champ"),
    [
        ("MB-2;Nom;Valmont;39;45,7;4,8;Inconnu;18/09/2020", "Statut"),
        ("MB-2;Nom;Valmont;abc;45,7;4,8;En service;18/09/2020", "Capacité"),
        ("MB-2;Nom;Valmont;-5;45,7;4,8;En service;18/09/2020", "Capacité"),
        ("MB-2;Nom;Valmont;39;nan;4,8;En service;18/09/2020", "Latitude"),
        ("MB-2;Nom;Valmont;39;45,7;4,8;En service;31/02/2020", "Mise en service"),
        ("MB-2;Nom;Valmont;39;45,7;4,8;En service;", "Mise en service"),
        (";Nom;Valmont;39;45,7;4,8;En service;18/09/2020", "Code station"),
        ("MB-2;tronquée", "(ligne)"),
        ("MB-2;Nom;Valmont;39;45,7;4,8;En service;18/09/2020;en trop", "(ligne)"),
    ],
)
def test_ligne_rejetee_sans_bloquer_la_suite(tmp_path: Path, mauvaise: str, champ: str) -> None:
    stations, anomalies = lire_tout(tmp_path, mauvaise, BONNE)
    assert list(stations) == ["MB-1"]
    [anomalie] = anomalies
    assert (anomalie.ligne, anomalie.champ) == (2, champ)
    assert anomalie.traitement is Traitement.REJETEE


def test_toutes_les_erreurs_d_une_ligne_sont_signalees(tmp_path: Path) -> None:
    _, anomalies = lire_tout(tmp_path, "MB-2;Nom;Valmont;abc;45,7;4,8;Inconnu;hier", BONNE)
    assert [a.champ for a in anomalies] == ["Capacité", "Statut", "Mise en service"]


def test_anomalies_non_bloquantes(tmp_path: Path) -> None:
    stations, anomalies = lire_tout(
        tmp_path,
        "MB-1;Nom;Valmont;NC;4,8;45,7;En service;18/09/2020",
        "MB-2;Nom;Valmont;39;45,7;;En service;18/09/2020",
        "MB-2;Nom;Valmont;40;45,7;4,8;En service;18/09/2021",
    )
    assert list(stations) == ["MB-1", "MB-2"]
    assert [(a.ligne, a.champ, a.traitement) for a in anomalies] == [
        (2, "Capacité", Traitement.ABSENTE),
        (2, "Latitude", Traitement.CORRIGEE),
        (3, "Longitude", Traitement.ABSENTE),
        (4, "Code station", Traitement.DOUBLON),
    ]


def test_rapport(tmp_path: Path) -> None:
    _, anomalies = lire_tout(tmp_path, "MB-1;Nom;Valmont;NC;45,7;4,8;En service;18/09/2020")
    assert rapport(anomalies) == (
        "1 anomalie(s)\n"
        "\n"
        "Valeur absente, station gardée (1)\n"
        "  ligne 2, MB-1, Capacité = 'NC' : valeur absente"
    )

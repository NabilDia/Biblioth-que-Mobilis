import gzip
import json
from pathlib import Path

import pytest
from conftest import ADRESSE, FausseApi

from mobilis.cli import main

STATIONS = """Code station;Nom;Commune;Capacité;Latitude;Longitude;Statut;Mise en service
MB-0001;Hôpital;Valmont;15;45,7;4,8;En service;16/12/2017
MB-0002;Gare;Valmont;NC;45,7;4,8;En service;16/12/2017
MB-0003;Parc;Valmont;20;45,7;4,8;En service;16/12/2017
"""
TRAJETS = """trajet_id,abonne_id,velo_id,type_velo,station_depart,debut,station_arrivee,fin,distance_m
T1,A1,V1,mecanique,MB-0001,2026-09-10T08:00:00+02:00,MB-0002,2026-09-10T09:01:00+02:00,10
T1,A1,V1,mecanique,MB-0001,2026-09-10T08:00:00+02:00,MB-0002,2026-09-10T09:01:00+02:00,10
T2,,V2,mecanique,MB-0002,2026-10-05T18:00:00+02:00,MB-0001,2026-10-05T18:10:00+02:00,10
T3,,V2,mecanique,MB-0099,2026-10-05T12:00:00+02:00,,,
T4,,V2,mecanique,MB-0099,2026-10-05T08:00:00+02:00,MB-0099,2026-10-05T18:10:00+02:00,10
"""


def _formule(prix: str, deblocage: str, tranche: int, incluses: int) -> dict[str, object]:
    return {
        "minutes_incluses": incluses,
        "tranche_minutes": tranche,
        "prix_tranche": prix,
        "supplement_electrique_tranche": "0.50",
        "frais_deblocage": deblocage,
        "plafond_jour": None,
    }


@pytest.fixture
def donnees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("MOBILIS_DONNEES", raising=False)
    (tmp_path / "stations.csv").write_bytes(STATIONS.encode("cp1252"))
    (tmp_path / "trajets_test.csv.gz").write_bytes(gzip.compress(TRAJETS.encode()))
    (tmp_path / "abonnes.jsonl").write_text(
        '{"id": "A1", "formule": "annuel", "debut": "2026-01-01", "fin": null}\n'
    )
    grilles = [
        {
            "en_vigueur_du": jour,
            "formules": {
                "annuel": _formule(prix, "0.00", 30, 30),
                "mensuel": _formule(prix, "0.00", 30, 30),
                "solidaire": _formule(prix, "0.00", 30, 45),
                "occasionnel": _formule(prix, "1.00", 15, 0),
            },
        }
        for jour, prix in (("2025-01-01", "1.00"), ("2026-10-01", "2.00"))
    ]
    (tmp_path / "tarifs.json").write_text(
        json.dumps(
            {
                "penalite_non_restitution": "150.00",
                "seuil_non_restitution_heures": 24,
                "grilles": grilles,
            }
        )
    )
    return tmp_path


def test_stations(donnees: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["stations", "--donnees", str(donnees)]) == 0
    sortie = capsys.readouterr().out
    assert "3 stations distinctes" in sortie
    assert "en service avec capacité et coordonnées : 2" in sortie
    assert "ligne 3, MB-0002, Capacité = 'NC'" in sortie


def test_frequentation(donnees: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["frequentation", "--donnees", str(donnees)]) == 0
    sortie = capsys.readouterr().out
    assert "3 trajets exploitables" in sortie
    assert "Sur 2 stations actives, 1 se vident" in sortie
    assert "En service sans aucun trajet : MB-0003" in sortie
    assert "Absentes du référentiel : MB-0099" in sortie


def test_facturation(donnees: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["facturation", "--donnees", str(donnees)]) == 0
    lignes = [" ".join(ligne.split()) for ligne in capsys.readouterr().out.splitlines()]
    assert "Recettes 2026-09 2026-10" in lignes
    assert "annuel 2,00 € 0,00 €" in lignes
    assert "occasionnel 0,00 € 86,00 €" in lignes
    assert "pénalités 0,00 € 150,00 €" in lignes
    assert "Total général : 238,00 €" in lignes
    assert "station inconnue : 2" in lignes
    assert "occasionnel 44,00 € 86,00 € +95.5 %" in lignes


def test_etat(
    donnees: Path,
    api: FausseApi,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MOBILIS_CLE", "cle")
    assert main(["etat", "--donnees", str(donnees), "--adresse", ADRESSE]) == 0
    sortie = capsys.readouterr().out
    assert "30 stations sur 30 photographiées" in sortie
    assert "Vides : aucune" in sortie
    assert "capacités différentes : 1" in sortie


def test_etat_sans_cle(
    donnees: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MOBILIS_CLE", raising=False)
    assert main(["etat", "--donnees", str(donnees)]) == 1
    assert "MOBILIS_CLE" in capsys.readouterr().err


def test_dossier_par_variable_d_environnement(
    donnees: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOBILIS_DONNEES", str(donnees))
    assert main(["stations"]) == 0


def test_dossier_non_indique(donnees: Path) -> None:
    with pytest.raises(SystemExit) as arret:
        main(["stations"])
    assert arret.value.code == 2


@pytest.mark.parametrize("commande", ["stations", "frequentation", "facturation"])
def test_fichier_absent(tmp_path: Path, capsys: pytest.CaptureFixture[str], commande: str) -> None:
    assert main([commande, "--donnees", str(tmp_path)]) == 1
    assert capsys.readouterr().err.startswith("mobilis : ")

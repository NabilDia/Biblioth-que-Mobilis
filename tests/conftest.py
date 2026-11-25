import io
import json
import threading
import time
from email.message import Message
from urllib.error import HTTPError
from urllib.request import Request

import pytest

ADRESSE = "http://bornes.invalid"
CODES = [f"MB-{i:04d}" for i in range(1, 31)]


class FausseApi:
    """Remplace urlopen : deux pages de stations, sans serveur ni réseau."""

    def __init__(self) -> None:
        self.erreurs: dict[str, list[int]] = {}  # statuts à renvoyer avant de répondre
        self.lenteur = 0.0
        self.maximum = 0  # plus grand nombre d'appels simultanés observé
        self._en_cours = 0
        self._verrou = threading.Lock()

    def __call__(self, requete: Request, timeout: float) -> io.BytesIO:
        chemin = requete.selector
        with self._verrou:
            self._en_cours += 1
            self.maximum = max(self.maximum, self._en_cours)
            erreurs = self.erreurs.get(chemin, [])
            statut = erreurs.pop(0) if erreurs else 200
        time.sleep(self.lenteur)
        with self._verrou:
            self._en_cours -= 1
        if requete.get_header("X-api-key") != "cle":
            statut = 401
        if statut != 200:
            entetes = Message()
            if statut == 429:
                entetes["Retry-After"] = "1"
            raise HTTPError(requete.full_url, statut, "erreur", entetes, io.BytesIO())
        return io.BytesIO(json.dumps(self._corps(chemin)).encode())

    def _corps(self, chemin: str) -> dict[str, object]:
        if chemin.startswith("/stations?page="):
            premiere = chemin.endswith("=1")
            return {
                "stations": CODES[:25] if premiere else CODES[25:],
                "page_suivante": 2 if premiere else None,
            }
        return {
            "code": chemin.rsplit("/", 1)[1],
            "velos_mecaniques": 3,
            "velos_electriques": 2,
            "places_libres": 10,
            "horodatage": "2026-11-24T08:00:00+00:00",
        }


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FausseApi:
    fausse = FausseApi()
    monkeypatch.setattr("mobilis.bornes.urlopen", fausse)
    return fausse

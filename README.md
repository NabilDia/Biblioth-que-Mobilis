# mobilis

Bibliothèque et commande qui traitent les données Mobilis : référentiel des stations, fréquentation, facturation et état des bornes.

## Installer

Python 3.11 ou plus récent. Les données ne sont pas dans ce dépôt : il faut le kit `mobilis-kit`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export MOBILIS_DONNEES=/chemin/vers/mobilis-kit/donnees
```

## Refaire les chiffres

| Commande | Réponse |
|---|---|
| `mobilis stations` | Stations exploitables et rapport d'anomalies |
| `mobilis frequentation` | Profil horaire, stations qui se vident le matin |
| `mobilis facturation` | Recettes par mois et par formule, effet de la dernière grille |
| `mobilis etat` | Stations vides ou pleines, écarts avec le référentiel |

`mobilis etat` interroge l'API des bornes. Dans un autre terminal, depuis `mobilis-kit` :

```bash
python3 outils/serveur_bornes.py
```

Puis, avec la clé donnée dans le README du kit :

```bash
export MOBILIS_CLE=...
mobilis etat
```

Les trois premières commandes donnent toujours les mêmes chiffres. `etat` change à chaque appel.

## Exemples

```console
$ mobilis stations
150 stations distinctes
  en service : 144
  hors service : 3
  fermee : 3
  en service avec capacité et coordonnées : 141

9 anomalie(s)
...
$ mobilis facturation --donnees /chemin/vers/mobilis-kit/donnees
Recettes           2026-08       2026-09       2026-10
annuel         12 510,50 €   18 311,00 €   18 332,20 €
...
Total général : 249 166,70 €
```

Depuis Python, par exemple pour les trois heures de pointe en semaine :

```python
import os
from pathlib import Path

from mobilis.trajets import compter_flux, exploitables, lire_trajets, profil_horaire, sans_doublons

chemin = Path(os.environ["MOBILIS_DONNEES"]) / "trajets_2026-08_2026-10.csv.gz"
flux = compter_flux(exploitables(sans_doublons(lire_trajets(chemin))))
semaine = profil_horaire(flux, weekend=False)
print(sorted(range(24), key=lambda heure: -semaine[heure])[:3])  # [8, 17, 18]
```

## Mesures et journal des anomalies

```bash
python journal_anomalies.py "$MOBILIS_DONNEES"
python mesure_memoire.py "$MOBILIS_DONNEES"/trajets_2026-08_2026-10.csv.gz
python mesure_bornes.py
```

## Choix de conception

Mesures faites sur macOS, Python 3.14, avec le serveur lent du kit (sans `--calme`).

| Construction | Où | Pourquoi |
|---|---|---|
| Dataclasses | `Station`, `Trajet`, `Facture`, `EtatStation`... | Une ligne nettoyée devient un objet typé. `frozen=True` empêche de modifier une donnée après sa lecture. |
| Générateurs | `trajets.py`, de `lire_lignes` à `exploitables` | Un trajet à la fois : pic de 14,1 Mo, contre 80,6 Mo en chargeant d'abord une liste. |
| Interface (`Protocol`) | `Tarification`, avec `TarifTranches` et `Forfait` | `facturer` appelle `prix()` sans savoir s'il s'agit d'un prix à la durée (R4, R5), d'une pénalité (R9) ou d'une gratuité (R8, R10). |
| Décorateur | `reessayer` dans `bornes.py` | Le réessai est séparé de l'appel HTTP et s'applique à toute fonction qui lève `ErreurTemporaire`. Un seul passage donne 137 à 138 états sur 149 sans lui, 146 avec. |
| Threads | `photographier` dans `bornes.py` | Le programme attend le réseau : les appels peuvent se recouvrir. |
| Tests | `tests/` | Chaque règle de facturation a son test. Seul `urlopen` est remplacé : le réessai et la limite de 8 appels sont réellement exécutés. |

### Pourquoi des threads

| Stratégie | Médiane de 3 | Maximum |
|---|---|---|
| Séquentiel | 65,2 s | 73,2 s |
| Threads | 10,2 s | 10,8 s |
| asyncio | 9,0 s | 9,7 s |

Seuls les threads et asyncio passent sous les 15 s. Leur écart change d'une série à l'autre, car la limite de 8 appels simultanés fixe presque toute la durée. Les threads suffisent avec la bibliothèque standard, alors qu'asyncio demanderait une bibliothèque HTTP asynchrone. Des processus n'apporteraient rien : le temps passe à attendre, pas à calculer.

## Vérifier le code

```bash
ruff check . && ruff format --check . && mypy && pytest
```

Les tests n'ont besoin ni des données ni du serveur.

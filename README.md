# EcoShare School

Plateforme Flask de partage de ressources scolaires (fiches, cours, PDF/images) avec moderation, roles, signalements et administration.

## Fonctionnalites

- Authentification: inscription, connexion, deconnexion, suppression de compte.
- Roles: eleve, prof, admin avec restrictions d'acces.
- Upload de ressources: JPG, PNG, PDF, avec verification de validite des images.
- OCR optionnel (EasyOCR): filtrage des images non pertinentes.
- Signalement des ressources par les utilisateurs.
- Back-office staff: suivi des signalements, suppression de ressources, gestion utilisateurs.
- Admin avance: creation de profs, synchronisation demo de comptes, historique et logs d'audit.
- Parametres utilisateur: theme, densite, profil et photo de profil.

## Stack technique

- Python 3.10+
- Flask
- Flask-Login
- Flask-SQLAlchemy
- SQLite
- Pillow
- EasyOCR (optionnel)

## Installation locale

1. Cloner le projet

```bash
git clone https://github.com/Godzi992/project-school.git
cd project-school
```

2. Creer un environnement virtuel

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

3. Installer les dependances

```bash
pip install -r requirements.txt
```

4. Lancer l'application

```bash
python app.py
```

Application accessible sur:

http://127.0.0.1:5000

## Comptes de demonstration

Un compte admin est cree automatiquement au premier lancement (bootstrap):

- Email: admin@eco-share.local
- Mot de passe: admin1234

Des comptes demo supplementaires peuvent etre importes depuis l'admin.

## Structure du projet

```text
app.py
requirements.txt
templates/
static/
   uploads/
   profiles/
```

## Moderation des uploads

- Les fichiers autorises sont: png, jpg, jpeg, pdf.
- Pour les images:
   - Avec EasyOCR: analyse de presence de texte.
   - Sans EasyOCR: fallback par densite de contours.
- Les ressources invalides sont rejetees avec message utilisateur.

## Securite et bonnes pratiques

- Mettre SECRET_KEY dans les variables d'environnement en production.
- Ne pas versionner les fichiers locaux sensibles (.env, .db, uploads, profiles).
- Changer le mot de passe admin par defaut avant mise en ligne.

## Roadmap suggeree

- Ajouter Alembic pour des migrations SQL robustes.
- Ajouter tests unitaires et integration.
- Ajouter CI (lint + tests) via GitHub Actions.
- Ajouter stockage cloud pour les fichiers.

## Licence

Projet scolaire / usage educatif.

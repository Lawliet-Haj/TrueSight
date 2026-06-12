"""Extensions Flask partagées.

Centralise l'instance SQLAlchemy pour éviter les imports circulaires.
On utilise Flask-SQLAlchemy (basé sur SQLAlchemy 2) qui fournit ``db.session``,
``db.Model`` et ``db.create_all()`` intégrés au cycle de vie de l'application.
"""
from flask_sqlalchemy import SQLAlchemy

# Instance unique partagée par toute l'application.
db = SQLAlchemy()

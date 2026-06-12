"""Point d'entrée WSGI pour gunicorn.

Expose ``app`` afin que gunicorn puisse être lancé avec :
    gunicorn -b 0.0.0.0:8000 wsgi:app
"""
from app import create_app

# Instance d'application exposée à gunicorn.
app = create_app()


if __name__ == "__main__":
    # Lancement direct utile en développement uniquement.
    app.run(host="0.0.0.0", port=8000, debug=False)

FROM python:3.12-slim

# On utilise uv pour installer rapidement les dépendances
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/

WORKDIR /app

# Variables d'environnement pour l'installation avec uv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# On copie les fichiers de configuration des dépendances
COPY pyproject.toml uv.lock ./

# On installe les dépendances sans le projet (met en cache cette étape)
RUN uv sync --frozen --no-install-project

# On copie le reste de l'application
COPY . .

# On installe le projet
RUN uv sync --frozen

# Port exposé
EXPOSE 8000

# Commande de démarrage
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

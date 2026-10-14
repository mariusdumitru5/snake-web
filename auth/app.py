"""
python-auth: registrazione, login, profilo utente e verifica dei token.

Configurazione letta dalle variabili d'ambiente:
  REDIS_HOST, REDIS_PORT, TOKEN_TTL  -> da ConfigMap-auth
  REDIS_PASSWORD                     -> da Secret-auth

Struttura dei dati in Redis (condivisa con snake):
  hash   user:<username>   nome, cognome, email, username, password (hash),
                           created_at, games_played, best_score, last_score,
                           rank, rank_points, ranked_games (aggiornati da snake)
  chiave email:<email>     username a cui appartiene l'email (email univoche)
  chiave token:<token>     username; scade dopo TOKEN_TTL secondi
  zset   leaderboard       username -> miglior punteggio (scritto da snake)
  zset   ranked_leaderboard username -> punti rank (scritto da snake)
"""
import os
import re
import secrets
from datetime import datetime, timezone

import redis
from flask import Flask, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
application = app  # nome che gunicorn cerca nell'immagine Python di OpenShift

TOKEN_TTL = int(os.environ.get("TOKEN_TTL", "3600"))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")

db = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    password=os.environ.get("REDIS_PASSWORD"),
    decode_responses=True,
    socket_timeout=3,
)


def body():
    return request.get_json(silent=True) or {}


def bearer_token():
    header = request.headers.get("Authorization", "")
    return header[len("Bearer "):] if header.startswith("Bearer ") else ""


def user_from_token():
    token = bearer_token()
    return db.get(f"token:{token}") if token else None


def public_profile(data):
    """Tutti i dati dell'utente tranne la password."""
    return {
        "username": data.get("username"),
        "nome": data.get("nome"),
        "cognome": data.get("cognome"),
        "email": data.get("email"),
        "created_at": data.get("created_at"),
        "games_played": int(data.get("games_played", 0)),
        "best_score": int(data.get("best_score", 0)),
        "last_score": int(data.get("last_score", 0)),
        "rank": data.get("rank", "bronzo"),
        "rank_points": int(data.get("rank_points", 0)),
        "ranked_games": int(data.get("ranked_games", 0)),
    }


@app.get("/health")
def health():
    try:
        db.ping()
        return jsonify(status="ok", redis="ok")
    except redis.RedisError as err:
        return jsonify(status="error", redis=str(err)), 503


@app.post("/register")
def register():
    data = body()
    nome = str(data.get("nome", "")).strip()
    cognome = str(data.get("cognome", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if not nome or not cognome:
        return jsonify(error="Inserisci nome e cognome."), 400
    if not EMAIL_RE.match(email):
        return jsonify(error="L'indirizzo email non è valido."), 400
    if not USERNAME_RE.match(username):
        return jsonify(error="Il nome utente deve avere 3-30 caratteri tra lettere, numeri, punto, trattino e underscore."), 400
    if len(password) < 6:
        return jsonify(error="La password deve avere almeno 6 caratteri."), 400

    key = f"user:{username}"
    # 1. "Prenota" lo username: hsetnx scrive solo se il campo non esiste ancora
    if not db.hsetnx(key, "username", username):
        return jsonify(error="Questo nome utente è già registrato."), 409
    # 2. "Prenota" l'email: set con nx=True scrive solo se la chiave non esiste
    if not db.set(f"email:{email}", username, nx=True):
        db.delete(key)  # annulla la prenotazione dello username
        return jsonify(error="Questa email è già usata da un altro account."), 409
    # 3. Salva il resto del profilo e azzera le statistiche di gioco
    db.hset(key, mapping={
        "nome": nome,
        "cognome": cognome,
        "email": email,
        "password": generate_password_hash(password),  # mai in chiaro
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "games_played": 0,
        "best_score": 0,
        "last_score": 0,
        "rank": "bronzo",           # ogni account parte da Bronzo
        "rank_points": 0,
        "ranked_games": 0,
    })
    return jsonify(message="Registrazione completata."), 201


@app.post("/login")
def login():
    data = body()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    stored_hash = db.hget(f"user:{username}", "password")
    if not stored_hash or not check_password_hash(stored_hash, password):
        return jsonify(error="Nome utente o password errati."), 401
    token = secrets.token_hex(32)
    db.setex(f"token:{token}", TOKEN_TTL, username)
    return jsonify(token=token, username=username, expires_in=TOKEN_TTL)


@app.get("/verify")
def verify():
    """Usato da snake per sapere chi c'è dietro un token."""
    username = user_from_token()
    if not username:
        return jsonify(error="Token mancante o scaduto."), 401
    return jsonify(username=username)


@app.get("/me")
def me():
    """Profilo completo dell'utente collegato, statistiche di gioco incluse."""
    username = user_from_token()
    if not username:
        return jsonify(error="Sessione scaduta: accedi di nuovo."), 401
    data = db.hgetall(f"user:{username}")
    if not data:
        return jsonify(error="Utente non trovato."), 404
    return jsonify(public_profile(data))


@app.post("/logout")
def logout():
    token = bearer_token()
    if token:
        db.delete(f"token:{token}")
    return jsonify(message="Disconnesso.")


if __name__ == "__main__":
    # Avvio senza gunicorn: deve ascoltare su 0.0.0.0:8080 per OpenShift
    app.run(host="0.0.0.0", port=8080)

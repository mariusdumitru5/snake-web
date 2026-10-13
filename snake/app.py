"""
python-app-snake: impostazioni del gioco, salvataggio partite e classifica.

Configurazione letta dalle variabili d'ambiente:
  AUTH_URL, REDIS_HOST, REDIS_PORT, GRID_SIZE, SPEED_MS  -> da ConfigMap-snake
  REDIS_PASSWORD                                         -> da Secret-auth

Snake chiede ad auth CHI è l'utente (/verify) e poi scrive in Redis
le statistiche di gioco nello stesso hash user:<username> del profilo:
  games_played  quante partite ha giocato
  best_score    punteggio migliore
  last_score    punteggio dell'ultima partita
e aggiorna lo zset "leaderboard" (username -> miglior punteggio).
Nessun dato è in memoria: si possono avere più repliche.
"""
import os

import redis
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
application = app  # nome che gunicorn cerca nell'immagine Python di OpenShift

AUTH_URL = os.environ.get("AUTH_URL", "http://auth:8080").rstrip("/")
GRID_SIZE = int(os.environ.get("GRID_SIZE", "20"))
SPEED_MS = int(os.environ.get("SPEED_MS", "120"))

db = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    password=os.environ.get("REDIS_PASSWORD"),
    decode_responses=True,
    socket_timeout=3,
)


@app.get("/health")
def health():
    try:
        db.ping()
        return jsonify(status="ok", redis="ok")
    except redis.RedisError as err:
        return jsonify(status="error", redis=str(err)), 503


@app.get("/config")
def config():
    """Il frontend legge da qui le regole del gioco (arrivano dalla ConfigMap)."""
    return jsonify(grid_size=GRID_SIZE, speed_ms=SPEED_MS)


@app.post("/score")
def submit_score():
    # 1. Chiede ad auth di chi è il token ricevuto dal browser
    try:
        reply = requests.get(
            f"{AUTH_URL}/verify",
            headers={"Authorization": request.headers.get("Authorization", "")},
            timeout=3,
        )
    except requests.RequestException:
        return jsonify(error="Il servizio di accesso non risponde. Riprova tra poco."), 503
    if reply.status_code != 200:
        return jsonify(error="Sessione scaduta: accedi di nuovo per salvare il punteggio."), 401
    username = reply.json()["username"]
    key = f"user:{username}"
    if not db.exists(key):
        return jsonify(error="Utente non trovato."), 404

    # 2. Valida il punteggio
    data = request.get_json(silent=True) or {}
    try:
        score = int(data.get("score", 0))
    except (TypeError, ValueError):
        return jsonify(error="Il punteggio deve essere un numero intero."), 400
    if score < 0 or score > GRID_SIZE * GRID_SIZE:
        return jsonify(error="Punteggio fuori dai limiti del campo."), 400

    # 3. Aggiorna tutto in un'unica transazione Redis (MULTI/EXEC)
    pipe = db.pipeline(transaction=True)
    pipe.hincrby(key, "games_played", 1)              # +1 partita
    pipe.hset(key, "last_score", score)               # ultimo punteggio
    pipe.zadd("leaderboard", {username: score}, gt=True)  # aggiorna solo se migliora
    pipe.zscore("leaderboard", username)              # legge il record attuale
    games_played, _, _, best = pipe.execute()
    best = int(best)
    db.hset(key, "best_score", best)                  # copia il record nel profilo

    return jsonify(username=username, score=score, best=best, games_played=games_played)


@app.get("/leaderboard")
def leaderboard():
    top = db.zrevrange("leaderboard", 0, 9, withscores=True)
    return jsonify([{"username": name, "score": int(score)} for name, score in top])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

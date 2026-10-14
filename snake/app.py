"""
python-app-snake: impostazioni del gioco, partite, classifica e rank.

Configurazione letta dalle variabili d'ambiente:
  AUTH_URL, REDIS_HOST, REDIS_PORT     -> da ConfigMap-snake
  GRID_SIZE, SPEED_MS                  -> mappa e velocità proposte di default al browser
  MAX_GRID_SIZE                        -> mappa più grande ammessa (per validare i punteggi)
  REDIS_PASSWORD                       -> da Secret-auth

Snake chiede ad auth CHI è l'utente (/verify) e poi scrive in Redis.

Nello hash del profilo user:<username>:
  games_played, best_score, last_score   tutte le partite (normali e classificate)
  rank, rank_points, ranked_games        solo le partite classificate

Altre chiavi:
  zset   leaderboard           username -> miglior punteggio (classifica "Record")
  zset   ranked_leaderboard    username -> punti rank (classifica "Classificata")
  hash   match:<id>            partita classificata in corso: impostazioni scelte,
                               moltiplicatore, ora di inizio (scade dopo 2 ore)
  chiave ranked:active:<user>  id della partita classificata in corso dell'utente

Partita classificata:
  1. POST /ranked/start   il giocatore sceglie mappa, velocità e cibo in campo;
                          la modalità è SEMPRE Caos. Il server controlla i valori
                          e calcola il moltiplicatore di difficoltà
  2. il browser gioca con quelle impostazioni
  3. POST /ranked/finish  prestazione = punteggio x moltiplicatore;
                          punti rank += (prestazione - obiettivo del rank) x 2.5
                          (da -30 a +40). Se i punti scendono sotto la soglia del
                          rank, si scende di rank.
  Se si inizia una nuova classificata senza aver finito la precedente, la
  precedente conta come 0 punti: chiudere la pagina quando va male non conviene.
"""
import os
import secrets
import time

import redis
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
application = app  # nome che gunicorn cerca nell'immagine Python di OpenShift

AUTH_URL = os.environ.get("AUTH_URL", "http://auth:8080").rstrip("/")
GRID_SIZE = int(os.environ.get("GRID_SIZE", "20"))
SPEED_MS = int(os.environ.get("SPEED_MS", "120"))
MAX_GRID_SIZE = int(os.environ.get("MAX_GRID_SIZE", "30"))

db = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    password=os.environ.get("REDIS_PASSWORD"),
    decode_responses=True,
    socket_timeout=3,
)

# ---------------------------------------------------------------------------
# Regole della modalità classificata
# ---------------------------------------------------------------------------
# min_points: punti rank necessari per avere quel rank
# target:     prestazione "attesa" a quel rank; fare di più fa salire, di meno fa scendere
RANKS = [
    {"id": "bronzo", "name": "Bronzo", "min_points": 0, "target": 6},
    {"id": "argento", "name": "Argento", "min_points": 100, "target": 10},
    {"id": "oro", "name": "Oro", "min_points": 250, "target": 15},
    {"id": "platino", "name": "Platino", "min_points": 450, "target": 20},
    {"id": "diamante", "name": "Diamante", "min_points": 700, "target": 26},
]
GAIN_FACTOR = 2.5
MAX_GAIN = 40
MAX_LOSS = 30
MATCH_TTL = 2 * 3600

# Impostazioni e quanto rendono difficile la partita (moltiplicatore)
COLORS = ["classico", "blu", "rosso", "viola", "arancio", "petrolio"]
FOODS = ["classico", "mela", "ciliegie", "banana", "uva", "fragola"]
COUNTS = {"1": 1.0, "3": 0.85, "5": 0.75}                    # più cibo = più facile
SIZES = {"piccola": (12, 1.2), "media": (18, 1.0), "grande": (24, 0.9)}
SPEEDS = {"lenta": (170, 0.8), "normale": (120, 1.0), "veloce": (80, 1.3)}
MODES = {"classica": 1.0, "muri": 1.2, "senza-bordi": 0.85, "caos": 1.05}
RANKED_MODE = "caos"                                         # la classificata è sempre Caos


def ranked_multipliers():
    """Moltiplicatore di ogni combinazione, così il browser lo mostra prima della partita."""
    table = {}
    for size in SIZES:
        for speed in SPEEDS:
            for count in COUNTS:
                key = f"{size}|{speed}|{count}"
                table[key] = multiplier_for({"size": size, "speed": speed, "count": count, "mode": RANKED_MODE})
    return table


def rank_for(points):
    current = RANKS[0]
    for rank in RANKS:
        if points >= rank["min_points"]:
            current = rank
    return current


def rank_index(rank_id):
    return next(i for i, r in enumerate(RANKS) if r["id"] == rank_id)


def multiplier_for(s):
    value = COUNTS[s["count"]] * SIZES[s["size"]][1] * SPEEDS[s["speed"]][1] * MODES[s["mode"]]
    return round(value, 2)


# ---------------------------------------------------------------------------
# Funzioni di supporto
# ---------------------------------------------------------------------------
def authenticated_user():
    """Chiede ad auth di chi è il token. Restituisce (username, None) o (None, risposta di errore)."""
    try:
        reply = requests.get(
            f"{AUTH_URL}/verify",
            headers={"Authorization": request.headers.get("Authorization", "")},
            timeout=3,
        )
    except requests.RequestException:
        return None, (jsonify(error="Il servizio di accesso non risponde. Riprova tra poco."), 503)
    if reply.status_code != 200:
        return None, (jsonify(error="Sessione scaduta: accedi di nuovo per salvare il punteggio."), 401)
    username = reply.json()["username"]
    if not db.exists(f"user:{username}"):
        return None, (jsonify(error="Utente non trovato."), 404)
    return username, None


def read_score():
    data = request.get_json(silent=True) or {}
    try:
        return int(data.get("score", 0)), data
    except (TypeError, ValueError):
        return None, data


def record_game(username, score):
    """Statistiche comuni a tutte le partite. Restituisce (partite giocate, record)."""
    key = f"user:{username}"
    pipe = db.pipeline(transaction=True)
    pipe.hincrby(key, "games_played", 1)
    pipe.hset(key, "last_score", score)
    pipe.zadd("leaderboard", {username: score}, gt=True)   # aggiorna solo se migliora
    pipe.zscore("leaderboard", username)
    games_played, _, _, best = pipe.execute()
    best = int(best)
    db.hset(key, "best_score", best)
    return games_played, best


def settle_match(username, match, score):
    """Calcola e salva l'effetto di una partita classificata sul rank."""
    key = f"user:{username}"
    multiplier = float(match["multiplier"])
    performance = round(score * multiplier, 1)

    old_points = int(db.hget(key, "rank_points") or 0)
    old_rank = rank_for(old_points)
    change = round((performance - old_rank["target"]) * GAIN_FACTOR)
    change = max(-MAX_LOSS, min(MAX_GAIN, change))
    new_points = max(0, old_points + change)       # i punti rank non vanno sotto zero
    new_rank = rank_for(new_points)

    pipe = db.pipeline(transaction=True)
    pipe.hset(key, mapping={"rank": new_rank["id"], "rank_points": new_points})
    pipe.hincrby(key, "ranked_games", 1)
    pipe.zadd("ranked_leaderboard", {username: new_points})
    _, ranked_games, _ = pipe.execute()
    games_played, best = record_game(username, score)

    return {
        "score": score,
        "multiplier": multiplier,
        "performance": performance,
        "target": old_rank["target"],
        "delta": new_points - old_points,
        "rank_points": new_points,
        "rank": new_rank["id"],
        "previous_rank": old_rank["id"],
        "promoted": rank_index(new_rank["id"]) > rank_index(old_rank["id"]),
        "demoted": rank_index(new_rank["id"]) < rank_index(old_rank["id"]),
        "ranked_games": ranked_games,
        "games_played": games_played,
        "best": best,
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        db.ping()
        return jsonify(status="ok", redis="ok")
    except redis.RedisError as err:
        return jsonify(status="error", redis=str(err)), 503


@app.get("/config")
def config():
    """Impostazioni di partenza e regole dei rank per il browser."""
    return jsonify(
        grid_size=GRID_SIZE,
        speed_ms=SPEED_MS,
        max_grid_size=MAX_GRID_SIZE,
        ranks=RANKS,
        ranked={"mode": RANKED_MODE, "multipliers": ranked_multipliers()},
    )


@app.post("/score")
def submit_score():
    """Fine di una partita normale."""
    username, error = authenticated_user()
    if error:
        return error
    score, _ = read_score()
    if score is None:
        return jsonify(error="Il punteggio deve essere un numero intero."), 400
    # Il giocatore sceglie la mappa nel browser: il punteggio non può superare
    # il numero di caselle della mappa più grande ammessa
    if score < 0 or score > MAX_GRID_SIZE * MAX_GRID_SIZE:
        return jsonify(error="Punteggio fuori dai limiti del campo."), 400
    games_played, best = record_game(username, score)
    return jsonify(username=username, score=score, best=best, games_played=games_played)


@app.post("/ranked/start")
def ranked_start():
    username, error = authenticated_user()
    if error:
        return error

    # Una classificata lasciata a metà conta come partita da 0 punti
    abandoned = None
    previous_id = db.get(f"ranked:active:{username}")
    if previous_id:
        previous = db.hgetall(f"match:{previous_id}")
        if previous and db.delete(f"match:{previous_id}"):
            abandoned = settle_match(username, previous, 0)

    data = request.get_json(silent=True) or {}
    size = str(data.get("size", ""))
    speed = str(data.get("speed", ""))
    count = str(data.get("count", ""))
    if size not in SIZES or speed not in SPEEDS or count not in COUNTS:
        return jsonify(error="Impostazioni non valide per la classificata."), 400
    settings = {
        "color": data.get("color") if data.get("color") in COLORS else "classico",   # solo estetica
        "food": data.get("food") if data.get("food") in FOODS else "mela",          # solo estetica
        "count": count,
        "size": size,
        "speed": speed,
        "mode": RANKED_MODE,
    }
    multiplier = multiplier_for(settings)
    match_id = secrets.token_hex(16)

    pipe = db.pipeline(transaction=True)
    pipe.hset(f"match:{match_id}", mapping={
        **settings,
        "username": username,
        "multiplier": multiplier,
        "started_at": time.time(),
    })
    pipe.expire(f"match:{match_id}", MATCH_TTL)
    pipe.setex(f"ranked:active:{username}", MATCH_TTL, match_id)
    pipe.execute()

    return jsonify(match_id=match_id, settings=settings, multiplier=multiplier, abandoned=abandoned)


@app.post("/ranked/finish")
def ranked_finish():
    username, error = authenticated_user()
    if error:
        return error
    score, data = read_score()
    if score is None:
        return jsonify(error="Il punteggio deve essere un numero intero."), 400

    match_id = str(data.get("match_id", ""))
    match = db.hgetall(f"match:{match_id}") if match_id else {}
    if not match or match.get("username") != username:
        return jsonify(error="Partita classificata non trovata o scaduta."), 404

    # Controllo di plausibilità: ogni cibo richiede almeno un passo del serpente,
    # e i passi possibili dipendono dal tempo trascorso e dalla velocità
    grid = SIZES[match["size"]][0]
    step_ms = SPEEDS[match["speed"]][0]
    elapsed_ms = (time.time() - float(match["started_at"])) * 1000
    max_score = min(grid * grid - 3, int(elapsed_ms / step_ms) + 1)
    if score < 0 or score > max_score:
        return jsonify(error="Punteggio non valido per questa partita."), 400

    # delete restituisce 1 solo alla prima chiamata: una partita non si chiude due volte
    if not db.delete(f"match:{match_id}"):
        return jsonify(error="Questa partita è già stata conclusa."), 409
    if db.get(f"ranked:active:{username}") == match_id:
        db.delete(f"ranked:active:{username}")

    result = settle_match(username, match, score)
    return jsonify(username=username, **result)


@app.get("/leaderboard")
def leaderboard():
    """?type=record (miglior punteggio) oppure ?type=ranked (punti rank)."""
    ranked = request.args.get("type") == "ranked"
    top = db.zrevrange("ranked_leaderboard" if ranked else "leaderboard", 0, 9, withscores=True)
    pipe = db.pipeline()
    for name, _ in top:
        pipe.hmget(f"user:{name}", "rank", "rank_points")
    profiles = pipe.execute()
    rows = []
    for (name, value), (rank, points) in zip(top, profiles):
        rows.append({
            "username": name,
            "score": int(value),
            "rank": rank or rank_for(int(points or 0))["id"],
        })
    return jsonify(rows)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

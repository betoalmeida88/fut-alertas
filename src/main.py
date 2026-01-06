import os
import math
import datetime as dt
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Sao_Paulo")

# ✅ Aceita TODAS as ligas/campeonatos.
# Se quiser bloquear só amistosos/treinos, mantenha essas palavras.
BLOCK_KEYWORDS = [
    "Friendly", "Friendlies", "Amistoso", "Amistosos", "Test", "Treino"
]

TARGET_LEAGUES = None  # None = aceita TODAS as ligas

# Forma recente (mínimo desejado)
LAST_N = 10

# Margem da casa (odd “abaixo” da justa)
BOOK_MARGIN = 0.07  # 7% (ajuste depois se quiser)

# Odds por perna (como você pediu)
MIN_ODD = 1.18
MAX_ODD = 1.50

FINISHED_STATUSES = {"FT", "AET", "PEN"}  # finalizado, prorrogação, pênaltis


def api_get(path: str, api_key: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    headers = {"x-apisports-key": api_key}
    r = requests.get(url, headers=headers, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=30)
    r.raise_for_status()


def safe_float(x, default=0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def is_target_game(league_country: str, league_name: str) -> bool:
    if any(k.lower() in league_name.lower() for k in BLOCK_KEYWORDS):
        return False
    if TARGET_LEAGUES is None:
        return True
    return (league_country, league_name) in TARGET_LEAGUES


def poisson_probs(lam: float, max_k: int = 10) -> list[float]:
    lam = max(0.2, float(lam))
    p0 = math.exp(-lam)
    probs = [p0]
    for k in range(1, max_k + 1):
        probs.append(probs[-1] * lam / k)
    s = sum(probs)
    return [p / s for p in probs]


def match_probs(lh: float, la: float, max_g: int = 10) -> dict:
    ph = poisson_probs(lh, max_g)
    pa = poisson_probs(la, max_g)

    p_home_win = 0.0
    p_draw = 0.0
    p_away_win = 0.0

    p_total_leq_1 = 0.0
    p_total_leq_3 = 0.0
    p_total_leq_4 = 0.0

    for i in range(max_g + 1):
        for j in range(max_g + 1):
            p = ph[i] * pa[j]
            if i > j:
                p_home_win += p
            elif i == j:
                p_draw += p
            else:
                p_away_win += p

            if i + j <= 1:
                p_total_leq_1 += p
            if i + j <= 3:
                p_total_leq_3 += p
            if i + j <= 4:
                p_total_leq_4 += p

    return {
        "home_win": p_home_win,
        "draw": p_draw,
        "away_win": p_away_win,
        "over_1_5": 1.0 - p_total_leq_1,   # total >= 2
        "under_3_5": p_total_leq_3,        # total <= 3
        "under_4_5": p_total_leq_4,        # total <= 4
        "home_score_1+": 1.0 - ph[0],
        "away_score_1+": 1.0 - pa[0],
    }


def odds_with_margin(p: float, margin: float = BOOK_MARGIN) -> float:
    # odd justa = 1/p
    # odd "casa" = 1 / (p * (1 + margem)) -> menor que a justa
    p = max(0.0001, min(0.9999, float(p)))
    p_adj = min(0.9999, p * (1.0 + float(margin)))
    return 1.0 / p_adj


def get_recent_avgs(api_key: str, team_id: int, venue: str, n: int = LAST_N) -> tuple[float, float, int]:
    """
    Retorna (avg_for, avg_against, sample_count) usando os últimos n jogos do time no venue (home/away).
    """
    data = api_get(
        "/fixtures",
        api_key,
        params={
            "team": team_id,
            "last": max(25, n),   # pega mais pra garantir que tenha FT
            "venue": venue,       # "home" ou "away"
            "timezone": "America/Sao_Paulo",
        },
    )
    fx = data.get("response", []) or []

    goals_for = []
    goals_against = []

    for f in fx:
        st = (f.get("fixture", {}).get("status", {}) or {}).get("short", "")
        if st not in FINISHED_STATUSES:
            continue

        home_id = int(f["teams"]["home"]["id"])
        away_id = int(f["teams"]["away"]["id"])
        gh = f.get("goals", {}).get("home", None)
        ga = f.get("goals", {}).get("away", None)
        if gh is None or ga is None:
            continue

        if team_id == home_id:
            goals_for.append(int(gh))
            goals_against.append(int(ga))
        elif team_id == away_id:
            goals_for.append(int(ga))
            goals_against.append(int(gh))

        if len(goals_for) >= n:
            break

    if not goals_for:
        return 0.0, 0.0, 0

    avg_for = sum(goals_for) / len(goals_for)
    avg_against = sum(goals_against) / len(goals_against)
    return avg_for, avg_against, len(goals_for)


def build_combo(
    target: float,
    candidates: list[dict],
    used_fixture_ids: set[int],
    used_leg_ids: set[str],
    base_n: int,
):
    legs = []
    product = 1.0

    def pick_best(desired_odd: float):
        best = None
        best_key = None
        for c in candidates:
            if c["fixture_id"] in used_fixture_ids:
                continue
            if c["leg_id"] in used_leg_ids:
                continue
            odd = c["odd_book"]
            if odd > 1.5:
                continue
            key = (abs(odd - desired_odd), -c["p"])
            if best is None or key < best_key:
                best = c
                best_key = key
        return best

    desired_per_leg = target ** (1.0 / base_n)

    for _ in range(base_n):
        c = pick_best(desired_per_leg)
        if not c:
            break
        legs.append(c)
        used_fixture_ids.add(c["fixture_id"])
        used_leg_ids.add(c["leg_id"])
        product *= c["odd_book"]

    while product < target * 0.95 and len(legs) < 6:
        remaining = min(1.5, target / max(1e-9, product))
        c = pick_best(remaining)
        if not c:
            break
        legs.append(c)
        used_fixture_ids.add(c["fixture_id"])
        used_leg_ids.add(c["leg_id"])
        product *= c["odd_book"]

    return legs, product


def main() -> None:
    api_key = os.environ["APISPORTS_KEY"]
    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_chat_id = os.environ["TELEGRAM_CHAT_ID"]

    # ✅ Queremos SEMPRE olhar jogos do DIA SEGUINTE em Brasília.
    now_sp = dt.datetime.now(TZ)
    target_date = (now_sp.date() + dt.timedelta(days=1)).isoformat()

    # Buscamos 3 datas UTC para garantir cobertura por causa do fuso.
    utc0 = dt.datetime.utcnow().date()
    utc1 = utc0 + dt.timedelta(days=1)
    utc2 = utc0 + dt.timedelta(days=2)

    games_map = {}
    for d in [utc0.isoformat(), utc1.isoformat(), utc2.isoformat()]:
        fx = api_get("/fixtures", api_key, params={"date": d, "timezone": "America/Sao_Paulo"})
        for g in fx.get("response", []) or []:
            fid = int(g["fixture"]["id"])
            games_map[fid] = g

    games_all = list(games_map.values())

    games = []
    for g in games_all:
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"])
        if kickoff.date().isoformat() != target_date:
            continue

        league_country = g["league"].get("country", "") or ""
        league_name = g["league"].get("name", "") or ""
        if not is_target_game(league_country, league_name):
            continue
        games.append(g)

    # Limite para não estourar requests no plano free.
    games = games[:25]

    candidates = []

    # Cache: (team_id, venue) -> (avg_for, avg_against, sample)
    recent_cache: dict[tuple[int, str], tuple[float, float, int]] = {}

    def recent(team_id: int, venue: str) -> tuple[float, float, int]:
        key = (team_id, venue)
        if key in recent_cache:
            return recent_cache[key]
        val = get_recent_avgs(api_key, team_id, venue, LAST_N)
        recent_cache[key] = val
        return val

    for g in games:
        fixture_id = int(g["fixture"]["id"])
        league = g["league"]["name"]
        home_id = int(g["teams"]["home"]["id"])
        away_id = int(g["teams"]["away"]["id"])
        home = g["teams"]["home"]["name"]
        away = g["teams"]["away"]["name"]
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"]).strftime("%H:%M")

        home_for, home_against, home_n = recent(home_id, "home")
        away_for, away_against, away_n = recent(away_id, "away")

        # Precisa ter pelo menos 10 jogos (como você pediu)
        if home_n < LAST_N or away_n < LAST_N:
            continue

        lam_home = max(0.2, (home_for + away_against) / 2.0)
        lam_away = max(0.2, (away_for + home_against) / 2.0)

        probs = match_probs(lam_home, lam_away)

        markets = [
            ("OVER_1_5", "Over 1.5 gols", probs["over_1_5"]),
            ("UNDER_3_5", "Under 3.5 gols", probs["under_3_5"]),
            ("UNDER_4_5", "Under 4.5 gols", probs["under_4_5"]),
            ("1X", "Dupla chance 1X", probs["home_win"] + probs["draw"]),
            ("X2", "Dupla chance X2", probs["away_win"] + probs["draw"]),
            ("HOME_1+", "Casa marca 1+ gol", probs["home_score_1+"]),
            ("AWAY_1+", "Fora marca 1+ gol", probs["away_score_1+"]),
        ]

        for code, label, p in markets:
            p = max(0.0001, float(p))
            odd_fair = 1.0 / p
            odd_book = odds_with_margin(p, BOOK_MARGIN)

            if MIN_ODD <= odd_book <= MAX_ODD:
                candidates.append({
                    "fixture_id": fixture_id,
                    "leg_id": f"{fixture_id}:{code}",
                    "home": home,
                    "away": away,
                    "league": league,
                    "kickoff": kickoff,
                    "label": label,
                    "p": p,
                    "odd_fair": odd_fair,
                    "odd_book": odd_book,
                    "sample_home": home_n,
                    "sample_away": away_n,
                })


    candidates.sort(key=lambda x: (-x["p"], abs(x["odd_book"] - 1.40)))

    used_fixture_ids: set[int] = set()
    used_leg_ids: set[str] = set()

    combos_plan = [
        (2.0, 2),
        (3.0, 3),
        (4.0, 4),
        (5.0, 4),
    ]

    out = []
    out.append(f"📌 Fut Alertas — jogos de {target_date} (SP)")
    out.append(f"Amostra: últimos {LAST_N} jogos (casa=home, fora=away).")
    out.append(f"Odd estimada já com margem (~{int(BOOK_MARGIN*100)}%).")
    out.append("Regras: pernas não se repetem + cada perna ≤ 1.50.")
    out.append("")

    if not candidates:
        out.append("⚠️ Não encontrei pernas suficientes (com 10 jogos mínimos por time e odd ≤ 1.50).")
        out.append("Dica: aumente 'games = games[:25]' para 35 OU reduza LAST_N para 8.")
        send_telegram(tg_token, tg_chat_id, "\n".join(out))
        return

    for target, base_n in combos_plan:
        legs, prod = build_combo(target, candidates, used_fixture_ids, used_leg_ids, base_n)
        if len(legs) < 2:
            out.append(f"❌ Combo alvo ~{target:.0f}: não encontrei pernas suficientes.")
            out.append("")
            continue

        out.append(f"✅ Combo alvo ~{target:.0f} | odd estimada ≈ {prod:.2f} | pernas: {len(legs)}")
        for i, leg in enumerate(legs, 1):
            out.append(
                f"  {i}) {leg['home']} x {leg['away']} ({leg['league']} {leg['kickoff']})"
                f" — {leg['label']} | p={leg['p']:.0%} | odd≈{leg['odd_book']:.2f}"
            )
        out.append("")

    send_telegram(tg_token, tg_chat_id, "\n".join(out))


if __name__ == "__main__":
    main()
    

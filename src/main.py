import os
import math
import datetime as dt
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Sao_Paulo")

# Para evitar ligas “estranhas” (U20, Women, etc.)
BLOCK_KEYWORDS = [
    "Women", "Feminil", "Feminino", "U20", "U19", "U17", "Youth", "Reserve", "Reserves"
]

# Principais competições (você pode ajustar depois)
TARGET_LEAGUES = {
    ("Brazil", "Serie A"),
    ("Brazil", "Serie B"),
    ("England", "Premier League"),
    ("Spain", "La Liga"),
    ("Germany", "Bundesliga"),
    ("Italy", "Serie A"),
    ("France", "Ligue 1"),
    ("Portugal", "Primeira Liga"),
    ("Netherlands", "Eredivisie"),
    ("World", "UEFA Champions League"),
    ("World", "UEFA Europa League"),
    ("World", "UEFA Europa Conference League"),
    ("World", "Copa Libertadores"),
    ("World", "Copa Sudamericana"),
    ("Brazil", "Copa do Brasil"),
}


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

            if i + j <= 3:
                p_total_leq_3 += p
            if i + j <= 4:
                p_total_leq_4 += p

    p_over_1_5 = 1.0 - p_total_leq_3  # total >= 4? (errado) -> vamos ajustar abaixo

    # Correção: Over 1.5 = total >= 2
    # P(total <=1) = somar i+j <= 1
    p_total_leq_1 = 0.0
    for i in range(max_g + 1):
        for j in range(max_g + 1):
            if i + j <= 1:
                p_total_leq_1 += ph[i] * pa[j]
    p_over_1_5 = 1.0 - p_total_leq_1

    return {
        "home_win": p_home_win,
        "draw": p_draw,
        "away_win": p_away_win,
        "under_3_5": p_total_leq_3,   # total <= 3
        "under_4_5": p_total_leq_4,   # total <= 4
        "over_1_5": p_over_1_5,       # total >= 2
        "home_score_1+": 1.0 - ph[0],
        "away_score_1+": 1.0 - pa[0],
    }


def is_target_game(league_country: str, league_name: str) -> bool:
    if any(k.lower() in league_name.lower() for k in BLOCK_KEYWORDS):
        return False
    return (league_country, league_name) in TARGET_LEAGUES


def build_combo(target: float, candidates: list[dict], used_fixture_ids: set[int], used_leg_ids: set[str], base_n: int):
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
            odd = c["odd"]
            if odd > 1.5:
                continue
            # escolhe o que mais se aproxima da odd desejada, e em empate pega maior prob
            key = (abs(odd - desired_odd), -c["p"])
            if best is None or key < best_key:
                best = c
                best_key = key
        return best

    # Primeiro, pega base_n pernas
    desired_per_leg = target ** (1.0 / base_n)
    for _ in range(base_n):
        c = pick_best(desired_per_leg)
        if not c:
            break
        legs.append(c)
        used_fixture_ids.add(c["fixture_id"])
        used_leg_ids.add(c["leg_id"])
        product *= c["odd"]

    # Se ficou abaixo do alvo, tenta completar com mais pernas (até 6)
    while product < target * 0.95 and len(legs) < 6:
        remaining = min(1.5, target / product)
        c = pick_best(remaining)
        if not c:
            break
        legs.append(c)
        used_fixture_ids.add(c["fixture_id"])
        used_leg_ids.add(c["leg_id"])
        product *= c["odd"]

    return legs, product


def main() -> None:
    api_key = os.environ["APISPORTS_KEY"]
    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_chat_id = os.environ["TELEGRAM_CHAT_ID"]

    now_sp = dt.datetime.now(TZ)
    sp_date = now_sp.date().isoformat()

    # Para garantir “jogos do dia SP”, buscamos 2 datas UTC e filtramos pelo dia SP
    utc0 = dt.datetime.utcnow().date()
    utc1 = utc0 + dt.timedelta(days=1)

    games_all = []
    for d in [utc0.isoformat(), utc1.isoformat()]:
        fx = api_get("/fixtures", api_key, params={"date": d, "timezone": "America/Sao_Paulo"})
        games_all.extend(fx.get("response", []))

    # Filtra jogos do dia (SP) e ligas alvo
    games = []
    for g in games_all:
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"])
        if kickoff.date().isoformat() != sp_date:
            continue

        league_country = g["league"]["country"]
        league_name = g["league"]["name"]

        if is_target_game(league_country, league_name):
            games.append(g)

    # Se ficou muito curto, relaxa: pega qualquer liga (menos U20/Women etc.)
    if len(games) < 13:
        games = []
        for g in games_all:
            kickoff = dt.datetime.fromisoformat(g["fixture"]["date"])
            if kickoff.date().isoformat() != sp_date:
                continue
            league_name = g["league"]["name"]
            if any(k.lower() in league_name.lower() for k in BLOCK_KEYWORDS):
                continue
            games.append(g)

    # Limite para não estourar o plano free
    games = games[:18]

    # Cache de stats por time/league/season
    stats_cache: dict[tuple[int, int, int], dict] = {}

    def team_stats(team_id: int, league_id: int, season: int) -> dict:
        key = (team_id, league_id, season)
        if key in stats_cache:
            return stats_cache[key]
        data = api_get("/teams/statistics", api_key, params={"team": team_id, "league": league_id, "season": season})
        resp = data.get("response") or {}
        stats_cache[key] = resp
        return resp

    candidates = []
    # Preferimos odds teóricas entre 1.22 e 1.50 para conseguir montar combos 2/3/4/5
    MIN_ODD = 1.22
    MAX_ODD = 1.50

    for g in games:
        fixture_id = int(g["fixture"]["id"])
        league = g["league"]["name"]
        season = int(g["league"]["season"])
        league_id = int(g["league"]["id"])

        home_id = int(g["teams"]["home"]["id"])
        away_id = int(g["teams"]["away"]["id"])
        home = g["teams"]["home"]["name"]
        away = g["teams"]["away"]["name"]
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"]).strftime("%H:%M")

        hs = team_stats(home_id, league_id, season)
        aws = team_stats(away_id, league_id, season)

        # médias casa/fora
        home_for_home = safe_float(hs.get("goals", {}).get("for", {}).get("average", {}).get("home"))
        home_against_home = safe_float(hs.get("goals", {}).get("against", {}).get("average", {}).get("home"))
        away_for_away = safe_float(aws.get("goals", {}).get("for", {}).get("average", {}).get("away"))
        away_against_away = safe_float(aws.get("goals", {}).get("against", {}).get("average", {}).get("away"))

        # estimativa simples de gols esperados
        lam_home = max(0.2, (home_for_home + away_against_away) / 2.0)
        lam_away = max(0.2, (away_for_away + home_against_home) / 2.0)

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
            odd = 1.0 / p
            if MIN_ODD <= odd <= MAX_ODD:
                candidates.append({
                    "fixture_id": fixture_id,
                    "leg_id": f"{fixture_id}:{code}",
                    "home": home,
                    "away": away,
                    "league": league,
                    "kickoff": kickoff,
                    "label": label,
                    "p": p,
                    "odd": odd,
                    "lam_home": lam_home,
                    "lam_away": lam_away,
                })

    # Ordena candidatos por maior prob (mais “seguro”)
    candidates.sort(key=lambda x: (-x["p"], abs(x["odd"] - 1.40)))

    used_fixture_ids: set[int] = set()
    used_leg_ids: set[str] = set()

    combos_plan = [
        (2.0, 2),
        (3.0, 3),
        (4.0, 4),
        (5.0, 4),  # 4 pernas já pode chegar perto de 5 com odds <= 1.5
    ]

    out = []
    out.append(f"📌 Fut Alertas — {sp_date} (horário SP)")
    out.append("Odds abaixo são *teóricas* (modelo). Confira odds reais na casa.")
    out.append("Regras: pernas não se repetem + cada perna ≤ 1.50.")
    out.append("")

    if not candidates:
        out.append("⚠️ Hoje não encontrei pernas suficientes (odd teórica entre 1.22 e 1.50).")
        send_telegram(tg_token, tg_chat_id, "\n".join(out))
        return

    for target, base_n in combos_plan:
        legs, prod = build_combo(target, candidates, used_fixture_ids, used_leg_ids, base_n)
        if len(legs) < 2:
            out.append(f"❌ Combo alvo ~{target:.0f}: não encontrei pernas suficientes.")
            out.append("")
            continue

        out.append(f"✅ Combo alvo ~{target:.0f} | odd teórica ≈ {prod:.2f} | pernas: {len(legs)}")
        for i, leg in enumerate(legs, 1):
            out.append(
                f"  {i}) {leg['home']} x {leg['away']} ({leg['league']} {leg['kickoff']})"
                f" — {leg['label']} | p={leg['p']:.0%} | odd≈{leg['odd']:.2f}"
            )
        out.append("")

    send_telegram(tg_token, tg_chat_id, "\n".join(out))


if __name__ == "__main__":
    main()
    

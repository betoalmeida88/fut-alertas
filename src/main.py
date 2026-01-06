import os
import math
import re
import time
import unicodedata
import datetime as dt
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Tuple

import requests

BASE_URL = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Sao_Paulo")

# ✅ Histórico real exigido (não inventa/fallback)
LAST_N = 10
HISTORY_LAST_FETCH = 80  # últimos 80 jogos para garantir que ache 10 finalizados

# ✅ Odds por perna (já com margem)
MIN_ODD = 1.15
MAX_ODD = 1.50
BOOK_MARGIN = 0.07  # 7%

# ✅ Controle para não estourar plano free
API_CALL_BUDGET = 95
API_CALLS = 0

FINISHED_STATUSES = {"FT", "AET", "PEN"}

# ✅ Bloqueio agressivo (somente masculino/profissional)
BLOCK_LEAGUE_WORDS = [
    "women", "woman", "femin", "feminino", "femenino", "femenil",
    "u23", "u22", "u21", "u20", "u19", "u18", "u17", "u16", "u15",
    "youth", "junior", "reserve", "reserves",
    "friendly", "friendlies", "amistoso", "amistosos", "treino", "test",
    "development", "academy",
]

# times B/II, etc (evita Barcelona B / Valencia II)
BLOCK_TEAM_PATTERNS = [
    r"\b(u(1[5-9]|2[0-3]))\b",           # U15..U23
    r"\b(women|woman|femin)\b",
    r"\b(reserve|reserves)\b",
    r"\b(ii|iii)\b$",                    # termina com II/III
    r"\b(b)\b$",                         # termina com "B"
]

COUNTRY_ALIASES = {
    "united states": "usa",
    "usa": "usa",
    "england": "england",
    "spain": "spain",
    "germany": "germany",
    "italy": "italy",
    "france": "france",
    "portugal": "portugal",
    "netherlands": "netherlands",
    "belgium": "belgium",
    "turkey": "turkey",
    "scotland": "scotland",
    "argentina": "argentina",
    "mexico": "mexico",
    "brazil": "brazil",
    "world": "world",
    "international": "world",
}

# ✅ Competições principais (masculino/profissional)
ALLOW = {
    "england": {
        "premier league",
        "championship",
        "fa cup",
        "efl cup",
        "league cup",
    },
    "spain": {
        "la liga",
        "segunda division",
        "copa del rey",
        "supercopa de espana",
    },
    "germany": {
        "bundesliga",
        "2 bundesliga",
        "dfb pokal",
        "dfl supercup",
    },
    "italy": {
        "serie a",
        "serie b",
        "coppa italia",
        "supercoppa italiana",
    },
    "france": {
        "ligue 1",
        "ligue 2",
        "coupe de france",
        "trophee des champions",
    },
    "portugal": {
        "primeira liga",
        "taca de portugal",
        "supertaca candido de oliveira",
    },
    "netherlands": {
        "eredivisie",
        "knvb beker",
        "johan cruijff schaal",
    },
    "belgium": {
        "jupiler pro league",
        "pro league",
        "belgian cup",
        "croky cup",
    },
    "turkey": {
        "super lig",
        "turkish cup",
    },
    "scotland": {
        "premiership",
        "scottish cup",
    },
    "argentina": {
        "liga profesional argentina",
        "primera division",
        "copa argentina",
    },
    "mexico": {
        "liga mx",
    },
    "usa": {
        "major league soccer",
        "mls",
        "us open cup",
    },
    "brazil": {
        "serie a",
        "serie b",
        "serie c",
        "copa do brasil",
        "copa do nordeste",
        "supercopa do brasil",
    },
    "world": {
        "uefa champions league",
        "uefa europa league",
        "uefa europa conference league",
        "uefa super cup",
        "copa libertadores",
        "copa sudamericana",
        "recopa sudamericana",
        "fifa club world cup",
        "club world cup",
    },
}

# ✅ Estaduais principais (1ª divisão) — por contém
BRAZIL_STATE_KEYWORDS = [
    "paulista", "carioca", "mineiro", "gaucho", "gaúcho",
    "paranaense", "catarinense", "baiano", "pernambucano",
    "cearense", "goiano", "paraense",
]


def norm(s: str) -> str:
    s = s or ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def looks_blocked_text(text: str) -> bool:
    t = norm(text)
    return any(w in t for w in BLOCK_LEAGUE_WORDS)


def looks_blocked_team(name: str) -> bool:
    t = norm(name)
    for pat in BLOCK_TEAM_PATTERNS:
        if re.search(pat, t):
            return True
    return False


def is_allowed_competition(country: str, league_name: str) -> bool:
    nc = norm(country)
    nl = norm(league_name)

    if looks_blocked_text(nl):
        return False

    key = COUNTRY_ALIASES.get(nc, nc)

    if key == "world":
        return nl in ALLOW["world"]

    if key == "brazil":
        if nl in ALLOW["brazil"]:
            return True

        # estaduais principais (evita A2/A3 e divisões inferiores por nome)
        if any(norm(k) in nl for k in BRAZIL_STATE_KEYWORDS):
            if "a2" in nl or "a3" in nl:
                return False
            if "u2" in nl or "u1" in nl:
                return False
            if looks_blocked_text(nl):
                return False
            return True

        return False

    if key in ALLOW:
        return nl in ALLOW[key]

    return False


def api_request(method: str, path: str, api_key: str, params: dict | None = None, data: dict | None = None) -> dict:
    """
    Sincrono (1 por vez) + retry em 429/5xx.
    """
    global API_CALLS
    if API_CALLS >= API_CALL_BUDGET:
        raise RuntimeError(f"API budget excedido ({API_CALLS}/{API_CALL_BUDGET}).")

    url = f"{BASE_URL}{path}"
    headers = {"x-apisports-key": api_key}

    backoff = 1.0
    for _ in range(5):
        API_CALLS += 1
        try:
            if method.upper() == "GET":
                r = requests.get(url, headers=headers, params=params or {}, timeout=30)
            else:
                r = requests.post(url, headers=headers, params=params or {}, data=data or {}, timeout=30)

            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                time.sleep(min(15.0, wait))
                backoff = min(15.0, backoff * 1.8)
                continue

            if 500 <= r.status_code < 600:
                time.sleep(min(10.0, backoff))
                backoff = min(10.0, backoff * 1.6)
                continue

            r.raise_for_status()
            return r.json()

        except requests.RequestException:
            time.sleep(min(10.0, backoff))
            backoff = min(10.0, backoff * 1.6)

    raise RuntimeError(f"Falha ao chamar {path} após retries.")


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=30)
    r.raise_for_status()


def send_telegram_document(token: str, chat_id: str, file_path: str, caption: str | None = None) -> None:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with open(file_path, "rb") as f:
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        r = requests.post(url, data=data, files={"document": f}, timeout=60)
        r.raise_for_status()


def poisson_probs(lam: float, max_k: int = 10) -> List[float]:
    lam = max(0.2, float(lam))
    p0 = math.exp(-lam)
    probs = [p0]
    for k in range(1, max_k + 1):
        probs.append(probs[-1] * lam / k)
    s = sum(probs)
    return [p / s for p in probs]


def match_probs(lh: float, la: float, max_g: int = 10) -> Dict[str, float]:
    ph = poisson_probs(lh, max_g)
    pa = poisson_probs(la, max_g)

    p_home_win = p_draw = p_away_win = 0.0
    p_total_leq_1 = p_total_leq_3 = p_total_leq_4 = 0.0

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
        "over_1_5": 1.0 - p_total_leq_1,
        "under_3_5": p_total_leq_3,
        "under_4_5": p_total_leq_4,
        "home_score_1+": 1.0 - ph[0],
        "away_score_1+": 1.0 - pa[0],
    }


def odds_with_margin(p: float) -> float:
    p = max(0.0001, min(0.9999, float(p)))
    p_adj = min(0.9999, p * (1.0 + BOOK_MARGIN))
    return 1.0 / p_adj



def get_last10_team_avgs(api_key: str, team_id: int) -> Tuple[float, float, int]:
    """
    Últimos 10 jogos FINALIZADOS (qualquer ano). Sem fallback.
    Retorna: (avg_gf, avg_ga, n_finalizados)
    """
    data = api_request(
        "GET",
        "/fixtures",
        api_key,
        params={
            "team": team_id,
            "last": HISTORY_LAST_FETCH,
            "timezone": "America/Sao_Paulo",
        },
    )
    fx = data.get("response", []) or []

    gf: List[int] = []
    ga: List[int] = []

    for f in fx:
        st = ((f.get("fixture", {}) or {}).get("status", {}) or {}).get("short", "")
        if st not in FINISHED_STATUSES:
            continue

        home_id = int(f["teams"]["home"]["id"])
        away_id = int(f["teams"]["away"]["id"])
        gh = f.get("goals", {}).get("home", None)
        ga_ = f.get("goals", {}).get("away", None)
        if gh is None or ga_ is None:
            continue

        gh = int(gh)
        ga_ = int(ga_)

        if team_id == home_id:
            gf.append(gh)
            ga.append(ga_)
        elif team_id == away_id:
            gf.append(ga_)
            ga.append(gh)

        if len(gf) >= LAST_N:
            break

    if len(gf) < LAST_N:
        return 0.0, 0.0, len(gf)

    return sum(gf) / len(gf), sum(ga) / len(ga), len(gf)


def build_combo_high_odds(
    target: float,
    candidates: List[dict],
    used_leg_ids_global: set,
    min_legs: int,
) -> Tuple[List[dict], float]:
    """
    ✅ Estratégia: usar as MAIORES odds possíveis para reduzir a chance de precisar adicionar pernas.
    - fixa um mínimo de pernas (2,3,4,4 para 2/3/4/5)
    - tenta escolher odds perto de 1.50 (respeitando o alvo)
    - evita repetir o mesmo jogo dentro do combo
    - não repete pernas entre combos (used_leg_ids_global)
    """
    legs: List[dict] = []
    product = 1.0
    used_fixture_ids_local = set()

    def feasible_if_pick(prod: float, odd: float, remaining_after_pick: int) -> bool:
        max_possible = (prod * odd) * (MAX_ODD ** remaining_after_pick)
        return max_possible >= target * 0.98

    # fase 1: pegar exatamente min_legs, priorizando odds altas
    for i in range(min_legs):
        remaining_legs = min_legs - i
        desired = (target / max(1e-9, product)) ** (1.0 / remaining_legs)
        desired = max(MIN_ODD, min(MAX_ODD, desired))

        best = None
        best_key = None

        for c in candidates:
            if c["leg_id"] in used_leg_ids_global:
                continue
            if c["fixture_id"] in used_fixture_ids_local:
                continue

            odd = c["odd_book"]
            if not (MIN_ODD <= odd <= MAX_ODD):
                continue

            remaining_after = remaining_legs - 1
            if not feasible_if_pick(product, odd, remaining_after):
                continue

            # chave: manter próximo do "desired", mas puxando para odds maiores
            key = (abs(odd - desired), -odd, -c["p"])
            if best is None or key < best_key:
                best = c
                best_key = key

        if not best:
            break

        legs.append(best)
        used_leg_ids_global.add(best["leg_id"])
        used_fixture_ids_local.add(best["fixture_id"])
        product *= best["odd_book"]

    # fase 2: se ainda ficou baixo, tenta completar com mais pernas,
    # mas ainda priorizando odds altas para não virar "combo infinito"
    while product < target * 0.98 and len(legs) < 10:
        remaining = target / max(1e-9, product)
        desired = max(MIN_ODD, min(MAX_ODD, remaining))

        best = None
        best_key = None
        for c in candidates:
            if c["leg_id"] in used_leg_ids_global:
                continue
            if c["fixture_id"] in used_fixture_ids_local:
                continue

            odd = c["odd_book"]
            if not (MIN_ODD <= odd <= MAX_ODD):
                continue

            key = (abs(odd - desired), -odd, -c["p"])
            if best is None or key < best_key:
                best = c
                best_key = key

        if not best:
            break

        legs.append(best)
        used_leg_ids_global.add(best["leg_id"])
        used_fixture_ids_local.add(best["fixture_id"])
        product *= best["odd_book"]

    return legs, product


def main() -> None:
    api_key = os.environ["APISPORTS_KEY"]
    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_chat_id = os.environ["TELEGRAM_CHAT_ID"]

    now_sp = dt.datetime.now(TZ)
    target_date = (now_sp.date() + dt.timedelta(days=1)).isoformat()

    # pega 3 datas UTC para cobrir fuso
    utc0 = dt.datetime.utcnow().date()
    utc1 = utc0 + dt.timedelta(days=1)
    utc2 = utc0 + dt.timedelta(days=2)

    games_map: Dict[int, dict] = {}
    for d in [utc0.isoformat(), utc1.isoformat(), utc2.isoformat()]:
        fx = api_request("GET", "/fixtures", api_key, params={"date": d, "timezone": "America/Sao_Paulo"})
        for g in fx.get("response", []) or []:
            fid = int(g["fixture"]["id"])
            games_map[fid] = g

    games_all = list(games_map.values())

    tomorrow_all = []
    for g in games_all:
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"])
        if kickoff.date().isoformat() == target_date:
            tomorrow_all.append(g)

    tomorrow_all.sort(key=lambda x: x["fixture"]["date"])

    # filtros de competição + masculino + profissional
    tomorrow_filtered = []
    reasons = {
        "blocked_league_text": 0,
        "blocked_team_name": 0,
        "not_allowed_competition": 0,
    }

    for g in tomorrow_all:
        league = g.get("league", {}) or {}
        country = league.get("country", "") or ""
        league_name = league.get("name", "") or ""

        home = (g.get("teams", {}) or {}).get("home", {}) or {}
        away = (g.get("teams", {}) or {}).get("away", {}) or {}
        home_name = home.get("name", "") or ""
        away_name = away.get("name", "") or ""

        if looks_blocked_text(league_name):
            reasons["blocked_league_text"] += 1
            continue

        if looks_blocked_team(home_name) or looks_blocked_team(away_name):
            reasons["blocked_team_name"] += 1
            continue

        if not is_allowed_competition(country, league_name):
            reasons["not_allowed_competition"] += 1
            continue

        tomorrow_filtered.append(g)

    # cache de times (1 chamada por time)
    team_cache: Dict[int, Tuple[float, float, int]] = {}
    team_fail: set[int] = set()

    def team_avgs(team_id: int) -> Tuple[float, float, int] | None:
        if team_id in team_cache:
            return team_cache[team_id]
        if team_id in team_fail:
            return None
        if API_CALLS >= API_CALL_BUDGET:
            team_fail.add(team_id)
            return None
        try:
            avg_gf, avg_ga, n = get_last10_team_avgs(api_key, team_id)
            team_cache[team_id] = (avg_gf, avg_ga, n)
            return team_cache[team_id]
        except Exception:
            team_fail.add(team_id)
            return None

    candidates: List[dict] = []
    processed_games = 0
    skipped_history = 0
    skipped_budget = 0

    # gera pernas apenas quando AMBOS têm 10 jogos finalizados
    for g in tomorrow_filtered:
        fixture_id = int(g["fixture"]["id"])
        league = g["league"]["name"]
        country = g["league"].get("country", "") or ""
        league_id = int(g["league"]["id"])

        home_id = int(g["teams"]["home"]["id"])
        away_id = int(g["teams"]["away"]["id"])
        home = g["teams"]["home"]["name"]
        away = g["teams"]["away"]["name"]
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"]).strftime("%H:%M")

        ha = team_avgs(home_id)
        aa = team_avgs(away_id)

        if ha is None or aa is None:
            skipped_budget += 1
            continue

        home_gf, home_ga, home_n = ha
        away_gf, away_ga, away_n = aa

        if home_n < LAST_N or away_n < LAST_N:
            skipped_history += 1
            continue

        lam_home = max(0.2, (home_gf + away_ga) / 2.0)
        lam_away = max(0.2, (away_gf + home_ga) / 2.0)

        probs = match_probs(lam_home, lam_away)

        # mercados principais (inclui Dupla Chance)
        markets = [
            ("OVER_1_5", "Over 1.5 gols", probs["over_1_5"]),
            ("UNDER_3_5", "Under 3.5 gols", probs["under_3_5"]),
            ("UNDER_4_5", "Under 4.5 gols", probs["under_4_5"]),
            ("1X", "Dupla chance 1X", probs["home_win"] + probs["draw"]),
            ("X2", "Dupla chance X2", probs["away_win"] + probs["draw"]),
            ("12", "Sem empate (12)", probs["home_win"] + probs["away_win"]),
            ("HOME_1+", "Casa marca 1+ gol", probs["home_score_1+"]),
            ("AWAY_1+", "Fora marca 1+ gol", probs["away_score_1+"]),
        ]

        for code, label, p in markets:
            p = max(0.0001, float(p))
            odd_book = odds_with_margin(p)
            if MIN_ODD <= odd_book <= MAX_ODD:
                candidates.append({
                    "fixture_id": fixture_id,
                    "leg_id": f"{fixture_id}:{code}",
                    "country": country,
                    "league": league,
                    "league_id": league_id,
                    "kickoff": kickoff,
                    "home": home,
                    "away": away,
                    "label": label,
                    "p": p,
                    "odd_book": odd_book,
                    "lam_home": lam_home,
                    "lam_away": lam_away,
                    "home_avg_gf": home_gf,
                    "home_avg_ga": home_ga,
                    "away_avg_gf": away_gf,
                    "away_avg_ga": away_ga,
                })

        processed_games += 1

    # ordena para favorecer odds mais altas (menos pernas) sem ignorar probabilidade
    candidates.sort(key=lambda x: (-x["odd_book"], -x["p"]))

    # montar 4 apostas alvo ~2/~3/~4/~5 com mínimo de pernas
    combos_plan = [
        (2.0, 2),
        (3.0, 3),
        (4.0, 4),
        (5.0, 4),
    ]

    used_leg_ids_global: set[str] = set()
    combos = []
    for target, min_legs in combos_plan:
        legs, prod = build_combo_high_odds(target, candidates, used_leg_ids_global, min_legs)
        combos.append((target, legs, prod))

    # Telegram (texto): APENAS apostas
    msg_lines = []
    msg_lines.append(f"🎯 Apostas — jogos de {target_date} (Brasília)")
    msg_lines.append("")

    for target, legs, prod in combos:
        if len(legs) < 2:
            msg_lines.append(f"❌ Odd ~{int(target)}: sem pernas suficientes.")
            msg_lines.append("")
            continue

        msg_lines.append(f"✅ Odd ~{int(target)} (estimada ≈ {prod:.2f}) — {len(legs)} pernas")
        for i, leg in enumerate(legs, 1):
            msg_lines.append(
                f"  {i}) {leg['home']} x {leg['away']} ({leg['league']} {leg['kickoff']})"
                f" — {leg['label']} | odd≈{leg['odd_book']:.2f}"
            )
        msg_lines.append("")

    send_telegram_message(tg_token, tg_chat_id, "\n".join(msg_lines).strip())

    # Relatório completo (arquivo)
    report = []
    report.append("Fut Alertas — RELATÓRIO COMPLETO")
    report.append(f"Data alvo (Brasília): {target_date}")
    report.append(f"Execução: {now_sp.strftime('%Y-%m-%d %H:%M:%S')} (America/Sao_Paulo)")
    report.append("")
    report.append("CONFIG:")
    report.append(f"- LAST_N: {LAST_N} (últimos 10 jogos finalizados por time)")
    report.append(f"- HISTORY_LAST_FETCH: {HISTORY_LAST_FETCH}")
    report.append(f"- Odds (com margem): {MIN_ODD}..{MAX_ODD}")
    report.append(f"- Margem casa: {BOOK_MARGIN:.2%}")
    report.append(f"- API calls: {API_CALLS}/{API_CALL_BUDGET}")
    report.append("")
    report.append("CONTAGEM:")
    report.append(f"- Jogos amanhã (total): {len(tomorrow_all)}")
    report.append(f"- Jogos amanhã (filtrados relevantes): {len(tomorrow_filtered)}")
    report.append(f"- Processados (com histórico válido): {processed_games}")
    report.append(f"- Descartados (histórico <10 em algum time): {skipped_history}")
    report.append(f"- Pulados (budget/erro ao buscar histórico): {skipped_budget}")
    report.append(f"- Pernas no range ({MIN_ODD}..{MAX_ODD}): {len(candidates)}")
    report.append("")
    report.append("FILTROS (descartes por motivo):")
    for k, v in reasons.items():
        report.append(f"- {k}: {v}")
    report.append("")

    report.append("=== JOGOS AMANHÃ (FILTRADOS) ===")
    for g in tomorrow_filtered:
        fid = int(g["fixture"]["id"])
        league = g["league"]["name"]
        country = g["league"].get("country", "") or ""
        home = g["teams"]["home"]["name"]
        away = g["teams"]["away"]["name"]
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"]).strftime("%H:%M")
        report.append(f"- [{fid}] {kickoff} | {country} - {league} | {home} x {away}")
    report.append("")

    report.append("=== PERNAS (odd no range) ===")
    for c in candidates:
        report.append(
            f"- [{c['fixture_id']}] {c['home']} x {c['away']} | {c['country']} - {c['league']} (league_id={c['league_id']}) {c['kickoff']} | "
            f"{c['label']} | p={c['p']:.4f} | odd≈{c['odd_book']:.2f} | "
            f"avgGF/GA casa={c['home_avg_gf']:.2f}/{c['home_avg_ga']:.2f} fora={c['away_avg_gf']:.2f}/{c['away_avg_ga']:.2f} | "
            f"lambda={c['lam_home']:.2f}-{c['lam_away']:.2f}"
        )
    report.append("")

    report.append("=== APOSTAS (COMBINADAS) ===")
    for target, legs, prod in combos:
        if len(legs) < 2:
            report.append(f"- Odd ~{int(target)}: sem pernas suficientes.")
            continue
        report.append(f"- Odd ~{int(target)} (estimada ≈ {prod:.2f})")
        for i, leg in enumerate(legs, 1):
            report.append(
                f"  {i}) [{leg['fixture_id']}] {leg['home']} x {leg['away']} ({leg['league']} {leg['kickoff']})"
                f" — {leg['label']} | odd≈{leg['odd_book']:.2f}"
            )
        report.append("")

    report_path = "/tmp/fut_alertas_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    send_telegram_document(
        tg_token,
        tg_chat_id,
        report_path,
        caption="📎 Relatório completo (filtros + histórico + pernas + combos + contagens)"
    )


if __name__ == "__main__":
    main()
    


import os
import math
import re
import unicodedata
import datetime as dt
from zoneinfo import ZoneInfo
from typing import Any

import requests

BASE_URL = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Sao_Paulo")

# Histórico REAL exigido (não inventa/fallback)
LAST_N = 10
HISTORY_LAST_FETCH = 90  # puxa bastante para conseguir 10 mandante/10 visitante

# Odds por perna (já com "margem da casa")
MIN_ODD = 1.15
MAX_ODD = 1.50

# Margem "casa" (reduz odd vs justa)
BOOK_MARGIN = 0.07  # 7% (ajuste depois se quiser)

# Plano free tem 100 req/dia -> usamos um budget por execução
API_CALL_BUDGET = 95
API_CALLS = 0

FINISHED_STATUSES = {"FT", "AET", "PEN"}

# Bloqueios básicos (evita amistosos/treino)
BLOCK_KEYWORDS = {"friendly", "friendlies", "amistoso", "amistosos", "treino", "test"}


def norm(s: str) -> str:
    s = s or ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Allowlist de competições (país + nome normalizado)
# Principais ligas + 2ª divisão, competições internacionais, Brasil e estaduais principais.
ALLOW = {
    "england": {"premier league", "championship"},
    "spain": {"la liga", "segunda division", "segunda division 1", "segunda division 2", "segunda", "laliga"},
    "germany": {"bundesliga", "2 bundesliga", "2 bundesliga 1", "2 bundesliga 2"},
    "italy": {"serie a", "serie b"},
    "france": {"ligue 1", "ligue 2"},
    "portugal": {"primeira liga"},
    "netherlands": {"eredivisie"},
    "belgium": {"pro league", "jupiler pro league"},
    "turkey": {"super lig"},
    "scotland": {"premiership"},
    "argentina": {"liga profesional argentina", "primera division"},
    "mexico": {"liga mx"},
    "usa": {"major league soccer", "mls"},
    "canada": {"major league soccer", "mls"},
    # Brasil nacionais (clubes)
    "brazil": {
        "serie a",
        "serie b",
        "copa do brasil",
        "copa do nordeste",
        "supercopa do brasil",
        # estaduais principais (nomes variam; tratamos por regras abaixo também)
        "carioca",
        "mineiro",
        "gaucho",
        "gaúcho",
        "paranaense",
        "catarinense",
        "baiano",
        "pernambucano",
        "cearense",
        "goiano",
        "paulista",
        "campeonato paulista",
    },
    # Internacionais (API costuma vir como World)
    "world": {
        "uefa champions league",
        "uefa europa league",
        "uefa europa conference league",
        "copa libertadores",
        "copa sudamericana",
        "libertadores",
        "sudamericana",
    },
}


def is_allowed_competition(country: str, league_name: str) -> bool:
    nc = norm(country)
    nl = norm(league_name)

    if any(k in nl for k in BLOCK_KEYWORDS):
        return False

    # Internacionais (muitas vezes vêm como World)
    if nc in ("world", "international", ""):
        if (
            "uefa champions league" in nl
            or "uefa europa league" in nl
            or "uefa europa conference league" in nl
            or "libertadores" in nl
            or "sudamericana" in nl
        ):
            return True

    # Brasil estaduais: aceitar somente "A1" / "primeira divisão" quando aplicável (para evitar A2/A3)
    if nc == "brazil":
        # nacionais diretos (match exato)
        if nl in ALLOW["brazil"]:
            return True

        # Estadual Paulista: aceitar "paulista" mas evitar A2/A3
        if "paulista" in nl:
            if "a2" in nl or "a3" in nl:
                return False
            return True

        # Outros estaduais principais (nome costuma ser "Carioca", "Mineiro", etc.)
        for key in ["carioca", "mineiro", "gaucho", "paranaense", "catarinense", "baiano", "pernambucano", "cearense", "goiano"]:
            if key in nl:
                # evita "2" / "segunda" quando aparecer
                if "2" in nl or "segunda" in nl:
                    return False
                return True

        return False

    # Outros países: match direto por conjunto
    if nc in ALLOW:
        if nl in ALLOW[nc]:
            return True

        # alguns nomes vêm com variações (ex.: "la liga" vs "laliga")
        if nc == "spain" and ("la liga" in nl or "laliga" in nl or "segunda" in nl):
            # evita categorias de base/reservas por nome (se existir)
            if "u" in nl and any(x in nl for x in ["u20", "u19", "u17"]):
                return False
            return True

    return False


def api_get(path: str, api_key: str, params: dict | None = None) -> dict:
    global API_CALLS
    if API_CALLS >= API_CALL_BUDGET:
        raise RuntimeError(f"API budget excedido ({API_CALLS}/{API_CALL_BUDGET}).")
    API_CALLS += 1

    url = f"{BASE_URL}{path}"
    headers = {"x-apisports-key": api_key}
    r = requests.get(url, headers=headers, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


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
        "over_1_5": 1.0 - p_total_leq_1,  # total >= 2
        "under_3_5": p_total_leq_3,       # total <= 3
        "under_4_5": p_total_leq_4,       # total <= 4
        "home_score_1+": 1.0 - ph[0],
        "away_score_1+": 1.0 - pa[0],
    }


def odds_with_margin(p: float, margin: float = BOOK_MARGIN) -> float:
    # odd "casa" = 1 / (p * (1+margem)) -> menor que a justa
    p = max(0.0001, min(0.9999, float(p)))
    p_adj = min(0.9999, p * (1.0 + float(margin)))
    return 1.0 / p_adj


def parse_finished(fixtures: list[dict], team_id: int) -> list[tuple[int, int, str]]:
    """
    Retorna lista (mais recente -> mais antigo) de (gf, ga, venue_as_team),
    venue_as_team = "home" se o team_id era mandante, "away" se era visitante.
    """
    out = []
    for f in fixtures:
        st = ((f.get("fixture", {}) or {}).get("status", {}) or {}).get("short", "")
        if st not in FINISHED_STATUSES:
            continue

        home_id = int(f["teams"]["home"]["id"])
        away_id = int(f["teams"]["away"]["id"])
        gh = f.get("goals", {}).get("home", None)
        ga = f.get("goals", {}).get("away", None)
        if gh is None or ga is None:
            continue

        gh = int(gh)
        ga = int(ga)

        if team_id == home_id:
            out.append((gh, ga, "home"))
        elif team_id == away_id:
            out.append((ga, gh, "away"))
    return out


def avg_last_n(items: list[tuple[int, int, str]], n: int, venue: str) -> tuple[float, float, int]:
    gf = []
    ga = []
    for xgf, xga, v in items:
        if v == venue:
            gf.append(xgf)
            ga.append(xga)
        if len(gf) >= n:
            break
    if not gf:
        return 0.0, 0.0, 0
    return sum(gf) / len(gf), sum(ga) / len(ga), len(gf)


def get_team_home_away_last10(api_key: str, team_id: int) -> dict[str, Any]:
    """
    Uma chamada por time. Exige depois que exista LAST_N como home e LAST_N como away (dependendo do uso).
    """
    data = api_get(
        "/fixtures",
        api_key,
        params={
            "team": team_id,
            "last": HISTORY_LAST_FETCH,
            "timezone": "America/Sao_Paulo",
        },
    )
    fx = (data.get("response", []) or [])
    parsed = parse_finished(fx, team_id)

    home_for, home_against, home_n = avg_last_n(parsed, LAST_N, "home")
    away_for, away_against, away_n = avg_last_n(parsed, LAST_N, "away")

    return {
        "home_for": home_for,
        "home_against": home_against,
        "home_n": home_n,
        "away_for": away_for,
        "away_against": away_against,
        "away_n": away_n,
    }


def build_combo(
    target: float,
    candidates: list[dict],
    used_leg_ids_global: set[str],
    base_n: int,
) -> tuple[list[dict], float]:
    """
    Monta combo aproximando target, sem repetir pernas globalmente.
    Evita repetir o mesmo jogo dentro do combo.
    """
    legs: list[dict] = []
    product = 1.0
    local_fixture_ids: set[int] = set()

    def pick_best(desired_odd: float):
        best = None
        best_key = None
        for c in candidates:
            if c["leg_id"] in used_leg_ids_global:
                continue
            if c["fixture_id"] in local_fixture_ids:
                continue
            odd = c["odd_book"]
            if odd > MAX_ODD or odd < MIN_ODD:
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
        used_leg_ids_global.add(c["leg_id"])
        local_fixture_ids.add(c["fixture_id"])
        product *= c["odd_book"]

    while product < target * 0.98 and len(legs) < 10:
        remaining = target / max(1e-9, product)
        desired = min(MAX_ODD, remaining)
        c = pick_best(desired)
        if not c:
            break
        legs.append(c)
        used_leg_ids_global.add(c["leg_id"])
        local_fixture_ids.add(c["fixture_id"])
        product *= c["odd_book"]

    return legs, product


def main() -> None:
    api_key = os.environ["APISPORTS_KEY"]
    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_chat_id = os.environ["TELEGRAM_CHAT_ID"]

    now_sp = dt.datetime.now(TZ)
    target_date = (now_sp.date() + dt.timedelta(days=1)).isoformat()

    # Buscar fixtures por 3 datas UTC para cobrir o fuso de Brasília
    utc0 = dt.datetime.utcnow().date()
    utc1 = utc0 + dt.timedelta(days=1)
    utc2 = utc0 + dt.timedelta(days=2)

    games_map: dict[int, dict] = {}
    for d in [utc0.isoformat(), utc1.isoformat(), utc2.isoformat()]:
        fx = api_get("/fixtures", api_key, params={"date": d, "timezone": "America/Sao_Paulo"})
        for g in fx.get("response", []) or []:
            fid = int(g["fixture"]["id"])
            games_map[fid] = g

    games_all = list(games_map.values())

    # Jogos de amanhã (SP) - todos
    tomorrow_all = []
    for g in games_all:
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"])
        if kickoff.date().isoformat() == target_date:
            tomorrow_all.append(g)

    # Jogos de amanhã (SP) filtrados por competições relevantes
    tomorrow_filtered = []
    for g in tomorrow_all:
        country = (g.get("league", {}) or {}).get("country", "") or ""
        league_name = (g.get("league", {}) or {}).get("name", "") or ""
        if is_allowed_competition(country, league_name):
            tomorrow_filtered.append(g)

    tomorrow_all.sort(key=lambda x: x["fixture"]["date"])
    tomorrow_filtered.sort(key=lambda x: x["fixture"]["date"])

    # Cache: 1 chamada por time
    team_cache: dict[int, dict[str, Any]] = {}
    team_fail: set[int] = set()

    def team_stats(team_id: int) -> dict[str, Any] | None:
        if team_id in team_cache:
            return team_cache[team_id]
        if team_id in team_fail:
            return None
        if API_CALLS >= API_CALL_BUDGET:
            team_fail.add(team_id)
            return None
        try:
            stats = get_team_home_away_last10(api_key, team_id)
            team_cache[team_id] = stats
            return stats
        except Exception:
            team_fail.add(team_id)
            return None

    candidates: list[dict] = []
    processed_games = 0
    skipped_no_history = 0

    # Processa apenas jogos filtrados (relevantes)
    for g in tomorrow_filtered:
        fixture_id = int(g["fixture"]["id"])
        league = g["league"]["name"]
        home_id = int(g["teams"]["home"]["id"])
        away_id = int(g["teams"]["away"]["id"])
        home = g["teams"]["home"]["name"]
        away = g["teams"]["away"]["name"]
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"]).strftime("%H:%M")

        hs = team_stats(home_id)
        aws = team_stats(away_id)
        if hs is None or aws is None:
            skipped_no_history += 1
            continue

        # EXIGÊNCIA: histórico real de 10 jogos do mandante como mandante e visitante como visitante
        if hs["home_n"] < LAST_N or aws["away_n"] < LAST_N:
            skipped_no_history += 1
            continue

        lam_home = max(0.2, (hs["home_for"] + aws["away_against"]) / 2.0)
        lam_away = max(0.2, (aws["away_for"] + hs["home_against"]) / 2.0)

        probs = match_probs(lam_home, lam_away)

        # Inclui Dupla Chance
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
                    "odd_book": odd_book,
                    "lam_home": lam_home,
                    "lam_away": lam_away,
                    "home_n": hs["home_n"],
                    "away_n": aws["away_n"],
                })

        processed_games += 1

    # Ordena pernas mais seguras primeiro
    candidates.sort(key=lambda x: (-x["p"], abs(x["odd_book"] - 1.40)))

    # Montar 4 apostas (odd ~2, ~3, ~4, ~5) sem repetir pernas entre elas
    used_leg_ids_global: set[str] = set()
    combos_plan = [
        (2.0, 2),
        (3.0, 3),
        (4.0, 4),
        (5.0, 4),
    ]

    combos = []
    for target, base_n in combos_plan:
        legs, prod = build_combo(target, candidates, used_leg_ids_global, base_n)
        combos.append((target, legs, prod))

    # Telegram (texto): APENAS as apostas
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
    report_lines = []
    report_lines.append(f"Fut Alertas — RELATÓRIO COMPLETO")
    report_lines.append(f"Data alvo (Brasília): {target_date}")
    report_lines.append(f"Execução: {now_sp.strftime('%Y-%m-%d %H:%M:%S')} (America/Sao_Paulo)")
    report_lines.append("")
    report_lines.append("CONFIG:")
    report_lines.append(f"- LAST_N (histórico exigido): {LAST_N} (mandante como mandante / visitante como visitante)")
    report_lines.append(f"- HISTORY_LAST_FETCH: {HISTORY_LAST_FETCH}")
    report_lines.append(f"- Odds (com margem): MIN={MIN_ODD} MAX={MAX_ODD}")
    report_lines.append(f"- Margem casa: {BOOK_MARGIN:.2%}")
    report_lines.append(f"- API calls: {API_CALLS} / budget {API_CALL_BUDGET}")
    report_lines.append("")
    report_lines.append("CONTAGEM:")
    report_lines.append(f"- Jogos amanhã (todos na API): {len(tomorrow_all)}")
    report_lines.append(f"- Jogos amanhã (FILTRADOS competições relevantes): {len(tomorrow_filtered)}")
    report_lines.append(f"- Jogos processados (com histórico válido): {processed_games}")
    report_lines.append(f"- Jogos descartados (sem histórico 10/10 ou sem dados/budget): {skipped_no_history}")
    report_lines.append(f"- Pernas encontradas (odd {MIN_ODD}..{MAX_ODD}): {len(candidates)}")
    report_lines.append("")

    report_lines.append("=== JOGOS AMANHÃ (TODOS) ===")
    for g in tomorrow_all:
        fid = int(g["fixture"]["id"])
        league = g["league"]["name"]
        country = g["league"].get("country", "") or ""
        home = g["teams"]["home"]["name"]
        away = g["teams"]["away"]["name"]
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"]).strftime("%H:%M")
        report_lines.append(f"- [{fid}] {kickoff} | {country} - {league} | {home} x {away}")
    report_lines.append("")

    report_lines.append("=== JOGOS AMANHÃ (FILTRADOS: COMPETIÇÕES RELEVANTES) ===")
    for g in tomorrow_filtered:
        fid = int(g["fixture"]["id"])
        league = g["league"]["name"]
        country = g["league"].get("country", "") or ""
        home = g["teams"]["home"]["name"]
        away = g["teams"]["away"]["name"]
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"]).strftime("%H:%M")
        report_lines.append(f"- [{fid}] {kickoff} | {country} - {league} | {home} x {away}")
    report_lines.append("")

    report_lines.append("=== PERNAS (odd dentro do range) ===")
    for c in candidates:
        report_lines.append(
            f"- [{c['fixture_id']}] {c['home']} x {c['away']} | {c['league']} {c['kickoff']} | "
            f"{c['label']} | p={c['p']:.4f} | odd≈{c['odd_book']:.2f} | "
            f"hist: home_n={c['home_n']} away_n={c['away_n']} | "
            f"lambda: {c['lam_home']:.2f}-{c['lam_away']:.2f}"
        )
    report_lines.append("")

    report_lines.append("=== APOSTAS (COMBINADAS) ===")
    for target, legs, prod in combos:
        if len(legs) < 2:
            report_lines.append(f"- Odd ~{int(target)}: sem pernas suficientes.")
            continue
        report_lines.append(f"- Odd ~{int(target)} (estimada ≈ {prod:.2f})")
        for i, leg in enumerate(legs, 1):
            report_lines.append(
                f"  {i}) [{leg['fixture_id']}] {leg['home']} x {leg['away']} ({leg['league']} {leg['kickoff']})"
                f" — {leg['label']} | odd≈{leg['odd_book']:.2f}"
            )
        report_lines.append("")

    report_path = "/tmp/fut_alertas_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))

    send_telegram_document(
        tg_token,
        tg_chat_id,
        report_path,
        caption="📎 Relatório completo (jogos + filtros + pernas + contagens + apostas)"
    )


if __name__ == "__main__":
    main()
    

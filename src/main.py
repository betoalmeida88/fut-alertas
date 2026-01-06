import os
import math
import datetime as dt
from zoneinfo import ZoneInfo
from typing import Any

import requests

BASE_URL = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Sao_Paulo")

# Aceita TODAS as ligas. Se quiser bloquear amistosos/treinos, mantenha.
BLOCK_KEYWORDS = ["Friendly", "Friendlies", "Amistoso", "Amistosos", "Test", "Treino"]

# Histórico recente: tentamos extrair splits home/away a partir do "last"
LAST_N = 10
HISTORY_LAST_FETCH = 60  # puxa até 60 últimos jogos do time para conseguir N em casa e N fora

# Margem da casa (reduz a odd vs odd justa)
BOOK_MARGIN = 0.07  # 7%

# Regra: perna deve ter odd estimada <= 1.50 (sem limite inferior)
MAX_ODD = 1.50

# Segurança de plano free (100 req/dia): limite por execução
API_CALL_BUDGET = 90

FINISHED_STATUSES = {"FT", "AET", "PEN"}

API_CALLS = 0


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


def is_allowed_competition(league_name: str) -> bool:
    if any(k.lower() in (league_name or "").lower() for k in BLOCK_KEYWORDS):
        return False
    return True


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
    # odd "casa" (menor) = 1 / (p * (1+margem))
    p = max(0.0001, min(0.9999, float(p)))
    p_adj = min(0.9999, p * (1.0 + float(margin)))
    return 1.0 / p_adj


def parse_finished(fixtures: list[dict], team_id: int) -> list[tuple[int, int, str]]:
    """
    Retorna lista de (gf, ga, venue_as_team) em ordem do mais recente para o mais antigo (como a API retorna).
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


def avg_last_n(items: list[tuple[int, int, str]], n: int, venue: str | None) -> tuple[float, float, int]:
    """
    items: (gf, ga, venue_as_team) mais recente -> mais antigo
    venue: "home", "away" ou None (qualquer)
    """
    gf = []
    ga = []
    for xgf, xga, v in items:
        if venue is None or v == venue:
            gf.append(xgf)
            ga.append(xga)
        if len(gf) >= n:
            break
    if not gf:
        return 0.0, 0.0, 0
    return sum(gf) / len(gf), sum(ga) / len(ga), len(gf)

def get_team_recent(api_key: str, team_id: int) -> dict[str, Any]:
    """
    Uma chamada por time: /fixtures?team=ID&last=...
    A partir dela, calcula:
      - últimos N jogos do time como mandante (home)
      - últimos N jogos do time como visitante (away)
      - últimos N jogos no geral (any)
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
    any_for, any_against, any_n = avg_last_n(parsed, LAST_N, None)

    return {
        "home_for": home_for, "home_against": home_against, "home_n": home_n,
        "away_for": away_for, "away_against": away_against, "away_n": away_n,
        "any_for": any_for, "any_against": any_against, "any_n": any_n,
    }


def choose_stat(team_recent: dict, venue: str, key_for: str, key_against: str) -> tuple[float, float, int, str]:
    """
    Tenta usar split (home/away). Se não tiver amostra suficiente, cai para "any".
    Retorna (avg_for, avg_against, n, source)
    """
    if venue == "home" and team_recent["home_n"] > 0:
        return team_recent["home_for"], team_recent["home_against"], team_recent["home_n"], "home"
    if venue == "away" and team_recent["away_n"] > 0:
        return team_recent["away_for"], team_recent["away_against"], team_recent["away_n"], "away"
    if team_recent["any_n"] > 0:
        return team_recent["any_for"], team_recent["any_against"], team_recent["any_n"], "any"
    return 0.0, 0.0, 0, "none"


def build_combo(target: float, candidates: list[dict], used_leg_ids_global: set[str], base_n: int) -> tuple[list[dict], float]:
    """
    Monta uma combinada aproximando 'target', sem repetir pernas globalmente.
    Dentro da mesma combinada, evita pegar 2 pernas do mesmo jogo (reduz correlação).
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
            if odd > MAX_ODD:
                continue
            # prioriza: proximidade da odd desejada, maior prob, e odd um pouco maior (para não explodir nº de pernas)
            key = (abs(odd - desired_odd), -c["p"], -odd)
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

    # completa até chegar perto do alvo
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

    # Amanhã no fuso de Brasília
    now_sp = dt.datetime.now(TZ)
    target_date = (now_sp.date() + dt.timedelta(days=1)).isoformat()

    # Buscar fixtures em 3 dias UTC para cobrir fuso
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

    # Filtra somente jogos do target_date
    games = []
    for g in games_all:
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"])
        if kickoff.date().isoformat() != target_date:
            continue
        league_name = g["league"].get("name", "") or ""
        if not is_allowed_competition(league_name):
            continue
        games.append(g)

    # Ordena por horário
    games.sort(key=lambda x: x["fixture"]["date"])

    # Cache por time (1 chamada por time no máximo)
    team_cache: dict[int, dict[str, Any]] = {}

    def team_recent(team_id: int) -> dict[str, Any] | None:
        if team_id in team_cache:
            return team_cache[team_id]
        # Não estourar budget
        if API_CALLS >= API_CALL_BUDGET:
            return None
        try:
            tr = get_team_recent(api_key, team_id)
        except Exception:
            return None
        team_cache[team_id] = tr
        return tr

    candidates: list[dict] = []
    processed_games = 0
    skipped_games_no_history = 0

    for g in games:
        fixture_id = int(g["fixture"]["id"])
        league = g["league"]["name"]
        home_id = int(g["teams"]["home"]["id"])
        away_id = int(g["teams"]["away"]["id"])
        home = g["teams"]["home"]["name"]
        away = g["teams"]["away"]["name"]
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"]).strftime("%H:%M")

        hr = team_recent(home_id)
        ar = team_recent(away_id)
        if hr is None or ar is None:
            skipped_games_no_history += 1
            continue

        # escolhe split home/away se existir, senão usa "any"
        home_for, home_against, home_n, home_src = choose_stat(hr, "home", "for", "against")
        away_for, away_against, away_n, away_src = choose_stat(ar, "away", "for", "against")

        # lambdas
        lam_home = max(0.2, (home_for + away_against) / 2.0)
        lam_away = max(0.2, (away_for + home_against) / 2.0)

        probs = match_probs(lam_home, lam_away)

        # ✅ Inclui Dupla Chance: 1X e X2
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
            if odd_book <= MAX_ODD:
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
                    "home_n": home_n,
                    "away_n": away_n,
                    "home_src": home_src,
                    "away_src": away_src,
                    "lam_home": lam_home,
                    "lam_away": lam_away,
                })

        processed_games += 1


    # Ordena pernas: maior prob primeiro
    candidates.sort(key=lambda x: (-x["p"], abs(x["odd_book"] - 1.40)))

    # Montar combinadas 2/3/4/5 (sem repetir perna entre elas)
    used_leg_ids_global: set[str] = set()
    combos_plan = [
        (2.0, 2),
        (3.0, 3),
        (4.0, 4),
        (5.0, 4),
    ]

    combos_out_lines: list[str] = []
    for target, base_n in combos_plan:
        legs, prod = build_combo(target, candidates, used_leg_ids_global, base_n)
        if len(legs) < 2:
            combos_out_lines.append(f"❌ Combo alvo ~{target:.0f}: não consegui montar com pernas disponíveis.")
            combos_out_lines.append("")
            continue

        combos_out_lines.append(f"✅ Combo alvo ~{target:.0f} | odd est. ≈ {prod:.2f} | pernas: {len(legs)}")
        for i, leg in enumerate(legs, 1):
            combos_out_lines.append(
                f"  {i}) {leg['home']} x {leg['away']} ({leg['league']} {leg['kickoff']})"
                f" — {leg['label']} | p={leg['p']:.0%} | odd≈{leg['odd_book']:.2f}"
            )
        combos_out_lines.append("")

    # Relatório completo em TXT (lista TODOS os jogos e TODAS as pernas encontradas)
    report_lines: list[str] = []
    report_lines.append(f"Fut Alertas — alvo: jogos de {target_date} (America/Sao_Paulo)")
    report_lines.append(f"Execução: {now_sp.strftime('%Y-%m-%d %H:%M:%S')} (SP)")
    report_lines.append(f"API calls nesta execução: {API_CALLS} (budget {API_CALL_BUDGET})")
    report_lines.append(f"Histórico: last={HISTORY_LAST_FETCH} por time, agregando últimos {LAST_N} home/away/any")
    report_lines.append(f"Odd estimada com margem: ~{int(BOOK_MARGIN*100)}% | filtro: odd <= {MAX_ODD}")
    report_lines.append("")

    report_lines.append("=== TODOS OS JOGOS DE AMANHÃ (SP) ===")
    report_lines.append(f"Total de jogos amanhã encontrados: {len(games)}")
    report_lines.append("")
    for g in games:
        fixture_id = int(g["fixture"]["id"])
        league = g["league"]["name"]
        home = g["teams"]["home"]["name"]
        away = g["teams"]["away"]["name"]
        kickoff = dt.datetime.fromisoformat(g["fixture"]["date"]).strftime("%H:%M")
        report_lines.append(f"- [{fixture_id}] {kickoff} | {league} | {home} x {away}")
    report_lines.append("")

    report_lines.append("=== PERNAS (odd <= 1.50) DOS JOGOS PROCESSADOS ===")
    report_lines.append(f"Jogos processados com histórico: {processed_games}")
    report_lines.append(f"Jogos pulados (sem histórico por budget/erro): {skipped_games_no_history}")
    report_lines.append(f"Total de pernas encontradas: {len(candidates)}")
    report_lines.append("")
    for c in candidates:
        report_lines.append(
            f"- [{c['fixture_id']}] {c['home']} x {c['away']} | {c['league']} {c['kickoff']} | "
            f"{c['label']} | p={c['p']:.3f} | odd≈{c['odd_book']:.2f} | "
            f"amostra casa={c['home_n']}({c['home_src']}) fora={c['away_n']}({c['away_src']})"
        )
    report_lines.append("")

    report_lines.append("=== COMBINADAS ===")
    report_lines.extend(combos_out_lines)

    report_path = "/tmp/fut_alertas_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))

    # Mensagem curta (Telegram tem limite de tamanho)
    msg_lines = []
    msg_lines.append(f"📌 Fut Alertas — jogos de {target_date} (SP)")
    msg_lines.append(f"API calls nesta execução: {API_CALLS}/{API_CALL_BUDGET}")
    msg_lines.append(f"Jogos amanhã: {len(games)} | processados: {processed_games} | pulados: {skipped_games_no_history}")
    msg_lines.append(f"Pernas (odd<=1.50): {len(candidates)} | margem ~{int(BOOK_MARGIN*100)}%")
    msg_lines.append("")
    msg_lines.append("COMBINADAS:")
    msg_lines.extend(combos_out_lines[:60])  # corta para não estourar tamanho

    send_telegram_message(tg_token, tg_chat_id, "\n".join(msg_lines))
    send_telegram_document(tg_token, tg_chat_id, report_path, caption="📎 Relatório completo (jogos + pernas + combinadas)")


if __name__ == "__main__":
    main()
    

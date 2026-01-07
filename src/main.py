# =========================
# BLOCO 1/5 — CONFIG + FILTROS
# =========================
import os
import math
import re
import time
import unicodedata
import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE_URL = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Sao_Paulo")
FINISHED_STATUSES = {"FT", "AET", "PEN"}

# Odds estimadas (com margem)
MIN_ODD = float(os.getenv("MIN_ODD", "1.15"))
MAX_ODD = float(os.getenv("MAX_ODD", "1.50"))
BOOK_MARGIN = float(os.getenv("BOOK_MARGIN", "0.07"))  # 7%

# Histórico
HIST_MAX_GAMES = int(os.getenv("HIST_MAX_GAMES", "20"))     # queremos até 20
HIST_MIN_GAMES = int(os.getenv("HIST_MIN_GAMES", "10"))     # mínimo 10
HISTORY_LAST_FETCH = int(os.getenv("HISTORY_LAST_FETCH", "140"))  # busca ampla p/ achar 20 válidos

# Budget (Pro aguenta mais; ainda assim deixa controlado)
API_CALL_BUDGET = int(os.getenv("API_CALL_BUDGET", "450"))
API_CALLS = 0

# Bloqueios (masc/profissional + sem amistosos)
BLOCK_LEAGUE_WORDS = [
    "women", "woman", "femin", "feminino", "femenino", "femenil",
    "u23", "u22", "u21", "u20", "u19", "u18", "u17", "u16", "u15",
    "youth", "junior", "reserve", "reserves",
    "friendly", "friendlies", "amistoso", "amistosos", "treino", "test",
    "development", "academy",
]

BLOCK_TEAM_PATTERNS = [
    r"\b(u(1[5-9]|2[0-3]))\b",
    r"\b(women|woman|femin)\b",
    r"\b(reserve|reserves)\b",
    r"\b(ii|iii)\b$",
    r"\b(b)\b$",
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

# Apenas estaduais BR permitidos
BRAZIL_STATE_KEYWORDS = ["paulista", "carioca", "mineiro"]

# Competições principais (jogos do "dia")
ALLOW: Dict[str, set[str]] = {
    "england": {"premier league", "championship", "fa cup", "efl cup", "league cup"},
    "spain": {"la liga", "segunda division", "copa del rey", "supercopa de espana"},
    "germany": {"bundesliga", "2 bundesliga", "dfb pokal", "dfl supercup"},
    "italy": {"serie a", "serie b", "coppa italia", "supercoppa italiana"},
    "france": {"ligue 1", "ligue 2", "coupe de france", "trophee des champions"},
    "portugal": {"primeira liga", "taca de portugal", "supertaca candido de oliveira"},
    "netherlands": {"eredivisie", "knvb beker", "johan cruijff schaal"},
    "belgium": {"jupiler pro league", "pro league", "belgian cup", "croky cup"},
    "turkey": {"super lig", "turkish cup"},
    "scotland": {"premiership", "scottish cup"},
    "argentina": {"liga profesional argentina", "primera division", "copa argentina"},
    "mexico": {"liga mx"},
    "usa": {"major league soccer", "mls", "us open cup"},
    "brazil": {"serie a", "serie b", "serie c", "copa do brasil", "copa do nordeste", "supercopa do brasil"},
    "world": {
        # clubes
        "uefa champions league", "uefa europa league", "uefa europa conference league",
        "uefa super cup", "copa libertadores", "copa sudamericana", "recopa sudamericana",
        "fifa club world cup", "club world cup",
        # seleções (oficiais)
        "world cup", "euro championship", "copa america", "uefa nations league",
        "africa cup of nations", "afcon", "asian cup", "concacaf gold cup",
    },
}


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

    # World: usa "contains" para cobrir variações
    if key == "world":
        for allowed in ALLOW["world"]:
            if allowed in nl:
                return True
        return False

    # Brasil: principais + estaduais (somente paulista/carioca/mineiro) — evita A2/A3 etc
    if key == "brazil":
        if nl in ALLOW["brazil"]:
            return True
        if any(k in nl for k in BRAZIL_STATE_KEYWORDS):
            if "a2" in nl or "a3" in nl:
                return False
            if looks_blocked_text(nl):
                return False
            return True
        return False

    # Demais países: match exato (estável) + fallback por contains p/ pequenas variações
    if key in ALLOW:
        if nl in ALLOW[key]:
            return True
        for allowed in ALLOW[key]:
            if allowed in nl:
                return True
        return False

    return False


# =========================
# BLOCO 2/5 — API + HISTÓRICO CASA/FORA + STATS
# =========================
def api_request(method: str, path: str, api_key: str, params: dict | None = None) -> dict:
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
                r = requests.post(url, headers=headers, params=params or {}, timeout=30)

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


def to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


# Cache de stats por fixture (muito importante p/ não explodir chamadas)
_fixture_stats_cache: Dict[int, Dict[int, Dict[str, Optional[int]]]] = {}
_team_fixtures_cache: Dict[int, List[dict]] = {}


def get_fixture_stats(api_key: str, fixture_id: int) -> Dict[int, Dict[str, Optional[int]]]:
    """
    Retorna: {team_id: {"corners":int|None, "sog":int|None, "yellow":int|None, "red":int|None}}
    """
    if fixture_id in _fixture_stats_cache:
        return _fixture_stats_cache[fixture_id]

    data = api_request("GET", "/fixtures/statistics", api_key, params={"fixture": fixture_id})
    resp = data.get("response", []) or []

    # normaliza chaves possíveis
    def pick(stat_list: List[dict], keys: List[str]) -> Optional[int]:
        for s in stat_list:
            t = (s.get("type") or "").strip().lower()
            if any(k.lower() == t for k in keys):
                return to_int(s.get("value"))
        return None

    out: Dict[int, Dict[str, Optional[int]]] = {}
    for row in resp:
        team = row.get("team", {}) or {}
        tid = to_int(team.get("id"))
        if tid is None:
            continue
        stats = row.get("statistics", []) or []
        corners = pick(stats, ["Corner Kicks"])
        sog = pick(stats, ["Shots on Goal", "Shots on Target"])
        yellow = pick(stats, ["Yellow Cards"])
        red = pick(stats, ["Red Cards"])
        out[int(tid)] = {"corners": corners, "sog": sog, "yellow": yellow, "red": red}

    _fixture_stats_cache[fixture_id] = out
    return out


def get_team_fixtures(api_key: str, team_id: int) -> List[dict]:
    """
    Busca últimos N jogos do time (mistura competições), depois filtramos.
    """
    if team_id in _team_fixtures_cache:
        return _team_fixtures_cache[team_id]

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
    _team_fixtures_cache[team_id] = fx
    return fx


@dataclass
class TeamHistory:
    team_id: int
    context: str  # "home" ou "away"
    n_games: int = 0

    gf: List[int] = field(default_factory=list)
    ga: List[int] = field(default_factory=list)

    corners_for: List[int] = field(default_factory=list)
    corners_against: List[int] = field(default_factory=list)

    cards_for: List[int] = field(default_factory=list)
    cards_against: List[int] = field(default_factory=list)

    sog_for: List[int] = field(default_factory=list)
    sog_against: List[int] = field(default_factory=list)

    def has_min_games(self) -> bool:
        return self.n_games >= HIST_MIN_GAMES

    def valid_count(self, arr: List[int]) -> int:
        return len(arr)

    def mean(self, arr: List[int]) -> Optional[float]:
        return (sum(arr) / len(arr)) if arr else None


_team_history_cache: Dict[Tuple[int, str], TeamHistory] = {}


def build_team_history(api_key: str, team_id: int, context: str) -> TeamHistory:
    """
    Contexto:
      - "home": apenas jogos onde o time foi mandante
      - "away": apenas jogos onde o time foi visitante

    Regras:
      - somente jogos finalizados
      - excluir amistosos (por texto) e times não-oficiais
      - NÃO limita por campeonato (mistura tudo oficial)
      - coleta até 20 jogos; exige mínimo 10 para "usar"
      - para corners/cards/sog: só conta jogo se houver dado (>=0)
    """
    key = (team_id, context)
    if key in _team_history_cache:
        return _team_history_cache[key]

    hist = TeamHistory(team_id=team_id, context=context)
    fx = get_team_fixtures(api_key, team_id)

    for f in fx:
        if hist.n_games >= HIST_MAX_GAMES:
            break

        fixture = f.get("fixture", {}) or {}
        status = ((fixture.get("status", {}) or {}).get("short") or "").strip()
        if status not in FINISHED_STATUSES:
            continue

        league = f.get("league", {}) or {}
        league_name = league.get("name", "") or ""
        if looks_blocked_text(league_name):
            continue  # elimina friendlies/amistosos e afins

        teams = f.get("teams", {}) or {}
        home = teams.get("home", {}) or {}
        away = teams.get("away", {}) or {}
        home_id = to_int(home.get("id"))
        away_id = to_int(away.get("id"))
        if home_id is None or away_id is None:
            continue

        home_name = home.get("name", "") or ""
        away_name = away.get("name", "") or ""
        if looks_blocked_team(home_name) or looks_blocked_team(away_name):
            continue

        is_home = int(home_id) == int(team_id)
        is_away = int(away_id) == int(team_id)
        if context == "home" and not is_home:
            continue
        if context == "away" and not is_away:
            continue

        goals = f.get("goals", {}) or {}
        gh = to_int(goals.get("home"))
        ga = to_int(goals.get("away"))
        if gh is None or ga is None:
            continue

        # gols for/against no contexto certo
        if is_home:
            gf, ga_ = int(gh), int(ga)
            opp_id = int(away_id)
        else:
            gf, ga_ = int(ga), int(gh)
            opp_id = int(home_id)

        hist.gf.append(gf)
        hist.ga.append(ga_)
        hist.n_games += 1

        fixture_id = to_int(fixture.get("id"))
        if fixture_id is None:
            continue

        # stats (se vierem)
        st = get_fixture_stats(api_key, int(fixture_id))
        me = st.get(int(team_id), {})
        opp = st.get(int(opp_id), {})

        corners_me = to_int(me.get("corners"))
        corners_opp = to_int(opp.get("corners"))
        if corners_me is not None and corners_opp is not None:
            hist.corners_for.append(int(corners_me))
            hist.corners_against.append(int(corners_opp))

        sog_me = to_int(me.get("sog"))
        sog_opp = to_int(opp.get("sog"))
        if sog_me is not None and sog_opp is not None:
            hist.sog_for.append(int(sog_me))
            hist.sog_against.append(int(sog_opp))

        y_me, r_me = to_int(me.get("yellow")), to_int(me.get("red"))
        y_opp, r_opp = to_int(opp.get("yellow")), to_int(opp.get("red"))
        if y_me is not None and r_me is not None and y_opp is not None and r_opp is not None:
            hist.cards_for.append(int(y_me + r_me))
            hist.cards_against.append(int(y_opp + r_opp))

    _team_history_cache[key] = hist
    return hist


# =========================
# BLOCO 3/5 — PROBABILIDADES + GERAÇÃO DE PERNAS (7 MERCADOS)
# =========================
def odds_with_margin(p: float) -> float:
    p = max(0.0001, min(0.9999, float(p)))
    p_adj = min(0.9999, p * (1.0 + BOOK_MARGIN))
    return 1.0 / p_adj


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
    p_total_leq = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}

    for i in range(max_g + 1):
        for j in range(max_g + 1):
            p = ph[i] * pa[j]
            if i > j:
                p_home_win += p
            elif i == j:
                p_draw += p
            else:
                p_away_win += p
            s = i + j
            for k in p_total_leq:
                if s <= k:
                    p_total_leq[k] += p

    return {
        "home_win": p_home_win,
        "draw": p_draw,
        "away_win": p_away_win,
        "over_1_5": 1.0 - p_total_leq[1],
        "over_2_5": 1.0 - p_total_leq[2],
        "under_3_5": p_total_leq[3],
        "under_4_5": p_total_leq[4],
        "home_score_1+": 1.0 - ph[0],
        "away_score_1+": 1.0 - pa[0],
    }


def freq_over(arr: List[int], line: float) -> Tuple[Optional[float], int]:
    if len(arr) < HIST_MIN_GAMES:
        return None, len(arr)
    win = sum(1 for x in arr if x > line)
    return win / len(arr), len(arr)


def freq_under(arr: List[int], line: float) -> Tuple[Optional[float], int]:
    if len(arr) < HIST_MIN_GAMES:
        return None, len(arr)
    win = sum(1 for x in arr if x < line)
    return win / len(arr), len(arr)


def safe_add_candidate(
    candidates: List[dict],
    fixture_id: int,
    leg_code: str,
    label: str,
    p: Optional[float],
    sample_n: int,
    meta: dict,
) -> None:
    if p is None:
        return
    if sample_n < HIST_MIN_GAMES:
        return
    odd = odds_with_margin(p)
    if MIN_ODD <= odd <= MAX_ODD:
        candidates.append(
            {
                "fixture_id": fixture_id,
                "leg_id": f"{fixture_id}:{leg_code}",
                "label": label,
                "p": float(p),
                "odd_book": float(odd),
                "sample_n": sample_n,
                **meta,
            }
        )


def expected_goals_from_hist(home_h: TeamHistory, away_h: TeamHistory) -> Tuple[Optional[float], Optional[float]]:
    """
    Modelo mais preciso: manda (casa) x visitante (fora)
    E[GH] ~ média( ataque_casa_mandante , defesa_fora_visitante )
    E[GA] ~ média( ataque_fora_visitante , defesa_casa_mandante )
    """
    if not home_h.has_min_games() and not away_h.has_min_games():
        return None, None

    home_attack = home_h.mean(home_h.gf) if home_h.has_min_games() else None
    home_def = home_h.mean(home_h.ga) if home_h.has_min_games() else None
    away_attack = away_h.mean(away_h.gf) if away_h.has_min_games() else None
    away_def = away_h.mean(away_h.ga) if away_h.has_min_games() else None

    # E[home goals]
    parts_h = [x for x in [home_attack, away_def] if x is not None]
    eh = (sum(parts_h) / len(parts_h)) if parts_h else None

    # E[away goals]
    parts_a = [x for x in [away_attack, home_def] if x is not None]
    ea = (sum(parts_a) / len(parts_a)) if parts_a else None

    if eh is not None:
        eh = max(0.2, float(eh))
    if ea is not None:
        ea = max(0.2, float(ea))
    return eh, ea


def build_candidates_for_match(
    fixture: dict,
    home_hist: TeamHistory,
    away_hist: TeamHistory,
) -> List[dict]:
    """
    Gera pernas SOMENTE nos 7 mercados definidos.
    Regras:
      - Se ambos têm >=10 (no contexto), habilita mercados de "jogo" (resultado, total, BTTS etc).
      - Se só um tem >=10, gera apenas mercados "solo" daquele time (gols/time, corners/time, cards/time, sog/time).
      - Para corners/cards/sog: só gera se houver >=10 amostras válidas daquele dado.
    """
    fixture_id = int((fixture.get("fixture", {}) or {}).get("id"))
    league = (fixture.get("league", {}) or {}).get("name", "") or ""
    country = (fixture.get("league", {}) or {}).get("country", "") or ""
    kickoff = dt.datetime.fromisoformat((fixture.get("fixture", {}) or {}).get("date")).strftime("%H:%M")

    home = ((fixture.get("teams", {}) or {}).get("home", {}) or {}).get("name", "") or ""
    away = ((fixture.get("teams", {}) or {}).get("away", {}) or {}).get("name", "") or ""

    meta_common = {"league": league, "country": country, "kickoff": kickoff, "home": home, "away": away}

    candidates: List[dict] = []

    home_ok = home_hist.has_min_games()
    away_ok = away_hist.has_min_games()

    # ========= GOALS / RESULTADO / DUPLA CHANCE / BTTS =========
    eh, ea = expected_goals_from_hist(home_hist, away_hist)

    if home_ok and away_ok and eh is not None and ea is not None:
        probs = match_probs(eh, ea)

        # Vitória seca
        safe_add_candidate(candidates, fixture_id, "1", "Vitória do mandante (1)", probs["home_win"], HIST_MAX_GAMES, meta_common)
        safe_add_candidate(candidates, fixture_id, "2", "Vitória do visitante (2)", probs["away_win"], HIST_MAX_GAMES, meta_common)

        # Dupla chance
        safe_add_candidate(candidates, fixture_id, "1X", "Dupla chance (1X)", probs["home_win"] + probs["draw"], HIST_MAX_GAMES, meta_common)
        safe_add_candidate(candidates, fixture_id, "X2", "Dupla chance (X2)", probs["away_win"] + probs["draw"], HIST_MAX_GAMES, meta_common)
        safe_add_candidate(candidates, fixture_id, "12", "Dupla chance (12 — sem empate)", probs["home_win"] + probs["away_win"], HIST_MAX_GAMES, meta_common)

        # Mais/Menos gols (jogo)
        safe_add_candidate(candidates, fixture_id, "G_O15", "Mais de 1.5 gols (jogo)", probs["over_1_5"], HIST_MAX_GAMES, meta_common)
        safe_add_candidate(candidates, fixture_id, "G_O25", "Mais de 2.5 gols (jogo)", probs["over_2_5"], HIST_MAX_GAMES, meta_common)
        safe_add_candidate(candidates, fixture_id, "G_U35", "Menos de 3.5 gols (jogo)", probs["under_3_5"], HIST_MAX_GAMES, meta_common)
        safe_add_candidate(candidates, fixture_id, "G_U45", "Menos de 4.5 gols (jogo)", probs["under_4_5"], HIST_MAX_GAMES, meta_common)

        # Ambos marcam / apenas um time marca
        p_h_scores = probs["home_score_1+"]
        p_a_scores = probs["away_score_1+"]

        # Aproximação: independência (boa o suficiente p/ filtro de odds)
        p_btts_yes = p_h_scores * p_a_scores
        p_btts_no = 1.0 - p_btts_yes

        safe_add_candidate(candidates, fixture_id, "BTTS_Y", "Ambos marcam — SIM", p_btts_yes, HIST_MAX_GAMES, meta_common)
        safe_add_candidate(candidates, fixture_id, "BTTS_N", "Ambos marcam — NÃO", p_btts_no, HIST_MAX_GAMES, meta_common)

        # “apenas um time marca” (variantes)
        safe_add_candidate(candidates, fixture_id, "H_1+", "Mandante marca (>=1)", p_h_scores, HIST_MAX_GAMES, meta_common)
        safe_add_candidate(candidates, fixture_id, "A_1+", "Visitante marca (>=1)", p_a_scores, HIST_MAX_GAMES, meta_common)

    else:
        # Modo SOLO: só o time que tiver dados gera mercados de gols do próprio time
        if home_ok and (eh is not None):
            # prob do mandante marcar (>=1) pela própria média (Poisson)
            p = 1.0 - poisson_probs(eh, 10)[0]
            safe_add_candidate(candidates, fixture_id, "H_1+_SOLO", "Mandante marca (>=1)", p, home_hist.n_games, meta_common)

        if away_ok and (ea is not None):
            p = 1.0 - poisson_probs(ea, 10)[0]
            safe_add_candidate(candidates, fixture_id, "A_1+_SOLO", "Visitante marca (>=1)", p, away_hist.n_games, meta_common)

    # ========= CORNERS / CARDS / SOG =========
    # Regras por mercado: precisa ter >=10 amostras válidas do dado
    # Linhas conservadoras (tendem a cair no range 1.15–1.50 quando há consistência)
    CORNERS_TOTAL_LINES = [7.5, 8.5, 9.5, 10.5]
    CORNERS_TEAM_LINES = [3.5, 4.5, 5.5]
    CARDS_TOTAL_LINES = [2.5, 3.5, 4.5, 5.5]
    CARDS_TEAM_LINES = [1.5, 2.5, 3.5]
    SOG_TOTAL_LINES = [6.5, 7.5, 8.5, 9.5]
    SOG_TEAM_LINES = [2.5, 3.5, 4.5]

    # Helpers de séries
    def total_series(h: TeamHistory, kind: str) -> List[int]:
        if kind == "corners":
            return [a + b for a, b in zip(h.corners_for, h.corners_against)]
        if kind == "cards":
            return [a + b for a, b in zip(h.cards_for, h.cards_against)]
        if kind == "sog":
            return [a + b for a, b in zip(h.sog_for, h.sog_against)]
        return []

    # Mercados "TOTAL DO JOGO" (precisa dos dois times com dado suficiente)
    # Faz média das frequências do contexto mandante (casa) e visitante (fora).
    def add_total_market(kind: str, line: float, over: bool) -> None:
        h_ser = total_series(home_hist, kind)
        a_ser = total_series(away_hist, kind)
        if len(h_ser) < HIST_MIN_GAMES or len(a_ser) < HIST_MIN_GAMES:
            return
        ph = (sum(1 for x in h_ser if x > line) / len(h_ser)) if over else (sum(1 for x in h_ser if x < line) / len(h_ser))
        pa = (sum(1 for x in a_ser if x > line) / len(a_ser)) if over else (sum(1 for x in a_ser if x < line) / len(a_ser))
        p = (ph + pa) / 2.0
        code = f"{kind.upper()}_{'O' if over else 'U'}_{str(line).replace('.','')}"
        label = f"{'Mais' if over else 'Menos'} de {line} {('escanteios' if kind=='corners' else 'cartões' if kind=='cards' else 'chutes a gol')} (jogo)"
        safe_add_candidate(candidates, fixture_id, code, label, p, min(len(h_ser), len(a_ser)), meta_common)

    for line in CORNERS_TOTAL_LINES:
        add_total_market("corners", line, over=True)
        add_total_market("corners", line, over=False)

    for line in CARDS_TOTAL_LINES:
        add_total_market("cards", line, over=True)
        add_total_market("cards", line, over=False)

    for line in SOG_TOTAL_LINES:
        add_total_market("sog", line, over=True)
        add_total_market("sog", line, over=False)

    # Mercados "POR TIME" (permite SOLO)
    def add_team_market(team_side: str, kind: str, line: float, over: bool) -> None:
        h = home_hist if team_side == "home" else away_hist
        arr = (
            h.corners_for if kind == "corners" else
            h.cards_for if kind == "cards" else
            h.sog_for
        )
        if len(arr) < HIST_MIN_GAMES:
            return
        p = (sum(1 for x in arr if x > line) / len(arr)) if over else (sum(1 for x in arr if x < line) / len(arr))
        code = f"{team_side[0].upper()}{kind.upper()}_{'O' if over else 'U'}_{str(line).replace('.','')}"
        side_label = "Mandante" if team_side == "home" else "Visitante"
        label_kind = "escanteios" if kind == "corners" else "cartões" if kind == "cards" else "chutes a gol"
        label = f"{side_label}: {'Mais' if over else 'Menos'} de {line} {label_kind}"
        safe_add_candidate(candidates, fixture_id, code, label, p, len(arr), meta_common)

    for line in CORNERS_TEAM_LINES:
        add_team_market("home", "corners", line, over=True)
        add_team_market("home", "corners", line, over=False)
        add_team_market("away", "corners", line, over=True)
        add_team_market("away", "corners", line, over=False)

    for line in CARDS_TEAM_LINES:
        add_team_market("home", "cards", line, over=True)
        add_team_market("home", "cards", line, over=False)
        add_team_market("away", "cards", line, over=True)
        add_team_market("away", "cards", line, over=False)

    for line in SOG_TEAM_LINES:
        add_team_market("home", "sog", line, over=True)
        add_team_market("home", "sog", line, over=False)
        add_team_market("away", "sog", line, over=True)
        add_team_market("away", "sog", line, over=False)

    return candidates


# =========================
# BLOCO 4/5 — MONTAGEM DAS COMBINADAS + MENSAGEM AMIGÁVEL
# =========================
def build_combo_high_odds(
    target: float,
    candidates: List[dict],
    used_leg_ids_global: set,
    min_legs: int,
) -> Tuple[List[dict], float]:
    """
    Monta combo priorizando MAIOR odd possível.
    Regras:
      - não repetir fixture dentro do combo
      - não repetir perna entre combos (used_leg_ids_global)
    """
    legs: List[dict] = []
    product = 1.0
    used_fixture_ids_local: set[int] = set()

    def feasible_if_pick(prod: float, odd: float, remaining_after_pick: int) -> bool:
        max_possible = (prod * odd) * (MAX_ODD ** remaining_after_pick)
        return max_possible >= target * 0.98

    # fase 1: garantir min_legs
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

            # chave: alta odd primeiro; desempata por maior prob (p)
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

    # fase 2: completar se necessário
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


def market_legend(label: str) -> str:
    """
    Legenda curta por tipo (baseada no texto do label).
    """
    l = label.lower()
    if "vitória" in l:
        return "Vitória seca: precisa vencer no tempo normal."
    if "dupla chance" in l:
        return "Dupla chance: cobre 2 resultados (ex.: 1X = casa ou empate)."
    if "ambos marcam" in l:
        return "Ambos marcam: SIM (os dois fazem gol) / NÃO (apenas um ou nenhum)."
    if "marca" in l and "ambos" not in l:
        return "Time marca: time faz pelo menos 1 gol."
    if "gols" in l:
        return "Mais/Menos gols: total de gols no jogo (ou do time, quando indicado)."
    if "escante" in l:
        return "Mais/Menos escanteios: total do jogo (ou do time, quando indicado)."
    if "cart" in l:
        return "Mais/Menos cartões: amarelo+vermelho (total ou do time)."
    if "chutes a gol" in l:
        return "Mais/Menos chutes a gol: finalizações no alvo (total ou do time)."
    return "Mercado conforme descrito."


def format_telegram_message(target_date: str, combos: List[Tuple[float, List[dict], float]]) -> str:
    lines: List[str] = []
    lines.append(f"📅 *Palpites para {target_date}* (Brasília)")
    lines.append("Seleção automática com histórico *casa x fora* (até 20 jogos oficiais).")
    lines.append("")

    any_legs = False
    used_legends: Dict[str, str] = {}

    for target, legs, prod in combos:
        title = f"🧩 Combo ~{int(target)}  |  odd estimada ≈ {prod:.2f}"
        lines.append(title)

        if len(legs) < 2:
            lines.append("• Sem pernas suficientes dentro dos critérios hoje.")
            lines.append("")
            continue

        any_legs = True
        for leg in legs:
            # linha bem curta e legível
            info = f"{leg['home']} x {leg['away']}"
            when = f"{leg['kickoff']} • {leg['league']}"
            pick = f"{leg['label']}  (odd≈{leg['odd_book']:.2f})"
            lines.append(f"• {info}")
            lines.append(f"  {when}")
            lines.append(f"  ✅ {pick}")

            # legenda do mercado (sem repetir)
            key = market_legend(leg["label"])
            used_legends[key] = key

        lines.append("")

    if not any_legs:
        lines.append("😕 Hoje não encontrei combinações que respeitem os filtros mínimos.")
        lines.append("")

    # Legenda final (somente o que foi usado)
    if used_legends:
        lines.append("📌 *Legenda dos mercados usados*")
        for txt in used_legends.values():
            lines.append(f"• {txt}")

    # Telegram markdown: manter simples (sem HTML)
    return "\n".join(lines).strip()


# =========================
# BLOCO 5/5 — MAIN (BUSCA JOGOS DE AMANHÃ + GERA CANDIDATOS + ENVIA)
# =========================
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
            fid = int((g.get("fixture", {}) or {}).get("id"))
            games_map[fid] = g

    games_all = list(games_map.values())

    # só amanhã (no fuso BR)
    tomorrow_all = []
    for g in games_all:
        kickoff_iso = (g.get("fixture", {}) or {}).get("date")
        if not kickoff_iso:
            continue
        kickoff = dt.datetime.fromisoformat(kickoff_iso)
        if kickoff.date().isoformat() == target_date:
            tomorrow_all.append(g)

    tomorrow_all.sort(key=lambda x: (x.get("fixture", {}) or {}).get("date", ""))

    # filtros de competição + masculino/profissional
    tomorrow_filtered: List[dict] = []
    for g in tomorrow_all:
        league = g.get("league", {}) or {}
        country = league.get("country", "") or ""
        league_name = league.get("name", "") or ""

        teams = g.get("teams", {}) or {}
        home = teams.get("home", {}) or {}
        away = teams.get("away", {}) or {}
        home_name = home.get("name", "") or ""
        away_name = away.get("name", "") or ""

        if looks_blocked_text(league_name):
            continue
        if looks_blocked_team(home_name) or looks_blocked_team(away_name):
            continue
        if not is_allowed_competition(country, league_name):
            continue

        tomorrow_filtered.append(g)

    # cache de históricos por time/contexto
    def hist(team_id: int, context: str) -> TeamHistory:
        return build_team_history(api_key, team_id, context)

    # gera candidatos por jogo
    candidates: List[dict] = []
    for g in tomorrow_filtered:
        teams = g.get("teams", {}) or {}
        home_id = int((teams.get("home", {}) or {}).get("id"))
        away_id = int((teams.get("away", {}) or {}).get("id"))

        home_hist = hist(home_id, "home")
        away_hist = hist(away_id, "away")

        # regra: só descarta jogo se nenhum tiver >=10 (no contexto)
        if not home_hist.has_min_games() and not away_hist.has_min_games():
            continue

        candidates.extend(build_candidates_for_match(g, home_hist, away_hist))

    # priorizar odds maiores (e um pouco de prob)
    candidates.sort(key=lambda x: (-x["odd_book"], -x["p"], -x.get("sample_n", 0)))

    combos_plan = [
        (2.0, 2),
        (3.0, 3),
        (4.0, 4),
        (5.0, 4),
    ]

    used_leg_ids_global: set[str] = set()
    combos: List[Tuple[float, List[dict], float]] = []
    for target, min_legs in combos_plan:
        legs, prod = build_combo_high_odds(target, candidates, used_leg_ids_global, min_legs)
        combos.append((target, legs, prod))

    msg = format_telegram_message(target_date, combos)

    # envia (sem arquivo extra)
    send_telegram_message(tg_token, tg_chat_id, msg)


if __name__ == "__main__":
    main()
    

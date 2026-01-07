# =========================
# BLOCO 1/6 — CONFIG + FILTROS
# =========================
import os
import io
import math
import re
import time
import unicodedata
import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple, Set

import requests

# -------------------------
# CONFIG (ENV)
# -------------------------
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TZ_SP = ZoneInfo("America/Sao_Paulo")

# odds consideradas são SEMPRE as odds já descontadas (modelo)
MIN_ODD = float(os.getenv("MIN_ODD", "1.15"))
MAX_ODD = float(os.getenv("MAX_ODD", "1.30"))

# desconto conservador aplicado sobre odd teórica (1/p)
ODD_DISCOUNT = float(os.getenv("ODD_DISCOUNT", "0.075"))  # 7.5%

# orçamentos (evita estourar limites / travar)
API_CALLS_BUDGET = int(os.getenv("API_CALLS_BUDGET", "130"))
STATS_CALLS_BUDGET = int(os.getenv("STATS_CALLS_BUDGET", "240"))

# alvo de combos
N_TARGET_COMBOS = int(os.getenv("N_TARGET_COMBOS", "10"))
TARGET_COMBO_ODD = float(os.getenv("TARGET_COMBO_ODD", "3.0"))
MIN_LEGS_TARGET = int(os.getenv("MIN_LEGS_TARGET", "2"))
MAX_LEGS_TARGET = int(os.getenv("MAX_LEGS_TARGET", "10"))

# seed pool (para fallback mix)
SEED_POOL = int(os.getenv("SEED_POOL", "70"))

# preferir combos de um jogo só (quando possível)
PREFER_SINGLE_FIXTURE_PER_COMBO = (os.getenv("PREFER_SINGLE_FIXTURE_PER_COMBO", "1").strip().lower() in {"1","true","yes","y"})

# mínimos de histórico para considerar (por time)
HIST_MIN_GAMES = int(os.getenv("HIST_MIN_GAMES", "10"))

# parâmetros EWMA/Bayes
EWMA_HALFLIFE_GAMES = float(os.getenv("EWMA_HALFLIFE_GAMES", "7.0"))
BETA_PRIOR_A = float(os.getenv("BETA_PRIOR_A", "1.0"))
BETA_PRIOR_B = float(os.getenv("BETA_PRIOR_B", "1.0"))

# limiares de probabilidade
P_MIN = float(os.getenv("P_MIN", "0.62"))

# debug
DEBUG = (os.getenv("DEBUG", "0").strip().lower() in {"1","true","yes","y"})
DEBUG_MAX_MATCH_DETAIL = int(os.getenv("DEBUG_MAX_MATCH_DETAIL", "18"))
DEBUG_MAX_API_ERROR_SAMPLES = int(os.getenv("DEBUG_MAX_API_ERROR_SAMPLES", "8"))

# -------------------------
# FILTROS DE LIGA / TIMES (ajuste conforme necessidade)
# -------------------------
ALLOWED_LEAGUES_BY_COUNTRY = {
    "england": {"premier league", "championship", "fa cup", "efl cup", "league cup"},
    "spain": {"la liga", "segunda division", "copa del rey", "supercopa de espana"},
    "germany": {"bundesliga", "2 bundesliga", "dfb pokal", "dfl supercup"},
    "italy": {"serie a", "serie b", "coppa italia", "supercoppa italiana"},
    "france": {"ligue 1", "ligue 2", "coupe de france", "trophee des champions"},
    "portugal": {"primeira liga", "taca de portugal", "supertaca candido de oliveira"},
    "netherlands": {"eredivisie", "knvb beker", "johan cruijff schaal"},
    "belgium": {"jupiler pro league", "belgian cup", "super cup"},
    "turkey": {"super lig", "turkiye kupasi", "super kupa"},
    "scotland": {"premiership", "scottish cup", "league cup"},
    "argentina": {"liga profesional argentina", "copa argentina"},
    "mexico": {"liga mx", "copa mx"},
    "brazil": {
        "serie a", "serie b", "copa do brasil", "supercopa do brasil",
        "paulista", "carioca", "mineiro"
    },
}

# bloquear competições/torneios específicos por nome (normalizado)
BLOCKED_LEAGUE_TOKENS = {
    "women",
    "u19",
    "u20",
    "u21",
    "u23",
    "reserve",
    "reserves",
    "friendly",
    "friendlies",
    "youth",
}

# bloquear times por tokens (normalizados) — se quiser
BLOCKED_TEAM_TOKENS = {
    # exemplos:
    # "b team",
    # "reserves",
}

# -------------------------
# HELPERS DE NORMALIZAÇÃO
# -------------------------
def normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    return s

def contains_any_token(name: str, tokens: Set[str]) -> bool:
    n = normalize(name)
    for t in tokens:
        if t in n:
            return True
    return False

def allowed_league(country: str, league: str) -> bool:
    c = normalize(country)
    l = normalize(league)
    if contains_any_token(l, BLOCKED_LEAGUE_TOKENS):
        return False
    allowed = ALLOWED_LEAGUES_BY_COUNTRY.get(c)
    if not allowed:
        return False
    return l in allowed

def allowed_fixture(fx: dict) -> bool:
    try:
        league_country = fx["league"]["country"]
        league_name = fx["league"]["name"]
        home = fx["teams"]["home"]["name"]
        away = fx["teams"]["away"]["name"]
    except Exception:
        return False
    if not allowed_league(league_country, league_name):
        return False
    if contains_any_token(home, BLOCKED_TEAM_TOKENS) or contains_any_token(away, BLOCKED_TEAM_TOKENS):
        return False
    return True

def utc_iso_to_sp_datetime(iso_utc: str) -> Optional[dt.datetime]:
    if not iso_utc:
        return None
    try:
        # iso_utc vem como "2025-01-01T18:00:00+00:00"
        t = dt.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        return t.astimezone(TZ_SP)
    except Exception:
        return None

def now_sp() -> dt.datetime:
    return dt.datetime.now(tz=TZ_SP)

def is_future_fixture(fx: dict) -> bool:
    try:
        iso = fx["fixture"]["date"]
    except Exception:
        return False
    t = utc_iso_to_sp_datetime(iso)
    if t is None:
        return False
    return t > now_sp()

# -------------------------
# API-FOOTBALL (base)
# -------------------------
API_BASE = "https://v3.football.api-sports.io"

def api_request(method: str, path: str, api_key: str, params: Optional[dict] = None, dbg: Optional["DebugCollector"] = None) -> dict:
    if not api_key:
        raise RuntimeError("API_FOOTBALL_KEY ausente")
    url = API_BASE + path
    headers = {"x-apisports-key": api_key}
    if dbg:
        dbg.api_calls_used += 1
        if dbg.api_calls_used > dbg.api_calls_budget:
            raise RuntimeError("API calls budget estourado")
    r = requests.request(method, url, headers=headers, params=params, timeout=25)
    if r.status_code != 200:
        if dbg:
            dbg.add_api_error(f"{r.status_code} {path} {params}")
        raise RuntimeError(f"API error {r.status_code}: {r.text[:160]}")
    data = r.json()
    return data

# -------------------------
# TELEGRAM
# -------------------------
def telegram_send_message(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    requests.post(url, data=payload, timeout=20)


# =========================
# BLOCO 2/6 — ESTATÍSTICA (EWMA + BAYES) + UTIL
# =========================
def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))

def safe_div(a: float, b: float, default: float = 0.0) -> float:
    if b == 0:
        return default
    return a / b

def _ewma_weights(n: int, halflife: float) -> List[float]:
    if n <= 0:
        return []
    # peso mais alto pro jogo mais recente
    # half-life: peso cai pela metade a cada "halflife" jogos
    w = []
    for i in range(n):
        # i=0 mais antigo, i=n-1 mais recente
        age = (n - 1 - i)
        w.append(0.5 ** (age / max(0.0001, halflife)))
    s = sum(w)
    if s <= 0:
        return [1.0 / n] * n
    return [x / s for x in w]

def weighted_beta_rate(
    arr: List[int],
    predicate,
    halflife: float,
    prior_a: float = BETA_PRIOR_A,
    prior_b: float = BETA_PRIOR_B,
) -> Optional[float]:
    """
    Beta-Binomial com EWMA:
      - cada jogo vira sucesso/fracasso segundo predicate
      - pesos EWMA dão mais peso aos jogos recentes
      - prior_a/prior_b suavizam
    """
    if not arr:
        return None
    n = len(arr)
    w = _ewma_weights(n, halflife)
    s = 0.0
    f = 0.0
    for i in range(n):
        ok = bool(predicate(arr[i]))
        if ok:
            s += w[i]
        else:
            f += w[i]
    # posterior mean
    a = prior_a + s
    b = prior_b + f
    return float(a / (a + b))

def to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None

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

def apply_odd_discount(odd_theoretical: Optional[float]) -> Optional[float]:
    """Aplica desconto conservador na odd teórica (ex.: 2.00 -> 1.85 com 7.5%)."""
    if odd_theoretical is None:
        return None
    try:
        o = float(odd_theoretical)
    except (TypeError, ValueError):
        return None
    if o <= 0:
        return None
    return o * (1.0 - ODD_DISCOUNT)

@dataclass
class DebugCollector:
    run_started_sp: str = ""
    target_date: str = ""
    api_calls_budget: int = 0
    api_calls_used: int = 0
    stats_calls_budget: int = 0
    stats_calls_used: int = 0

    counts: Dict[str, int] = field(default_factory=dict)
    filter_reasons: Dict[str, int] = field(default_factory=dict)
    hist_summary: Dict[str, int] = field(default_factory=dict)
    stats_missing: Dict[str, int] = field(default_factory=dict)

    candidates_total: int = 0
    candidates_by_type: Dict[str, int] = field(default_factory=dict)
    candidate_reject_reasons: Dict[str, int] = field(default_factory=dict)
    candidate_reject_samples: List[str] = field(default_factory=list)

    combos_generated: int = 0
    combos_samples: List[str] = field(default_factory=list)

    match_details: List[str] = field(default_factory=list)
    api_error_samples: List[str] = field(default_factory=list)
    empty_team_fixtures_samples: List[str] = field(default_factory=list)

    def inc(self, d: Dict[str, int], k: str, v: int = 1) -> None:
        d[k] = d.get(k, 0) + v

    def add_match_detail(self, s: str) -> None:
        if len(self.match_details) < DEBUG_MAX_MATCH_DETAIL:
            self.match_details.append(s)

    def add_api_error(self, s: str) -> None:
        if len(self.api_error_samples) < DEBUG_MAX_API_ERROR_SAMPLES:
            self.api_error_samples.append(s)

# =========================
# BLOCO 3/6 — HISTÓRICO POR TIME + EXTRAÇÃO DE FEATURES
# =========================
def _get_last_n(arr: List[Any], n: int) -> List[Any]:
    if not arr:
        return []
    return arr[-n:] if len(arr) > n else list(arr)

def _need_min_samples(*lists: List[int], n: int = HIST_MIN_GAMES) -> bool:
    return all(isinstance(lst, list) and len(lst) >= n for lst in lists)

def _extract_team_fixtures(api_key: str, team_id: int, season: int, venue: str, dbg: Optional[DebugCollector] = None) -> List[dict]:
    """
    venue: 'home' ou 'away'
    Retorna fixtures finalizados do time, filtrados por venue.
    """
    params = {"team": team_id, "season": season, "status": "FT"}
    try:
        data = api_request("GET", "/fixtures", api_key, params=params, dbg=dbg)
    except Exception as e:
        if dbg:
            dbg.inc(dbg.stats_missing, f"team_fixtures_error_{venue}")
        return []

    res = data.get("response") or []
    out = []
    for fx in res:
        try:
            if fx["teams"]["home"]["id"] == team_id and venue == "home":
                out.append(fx)
            elif fx["teams"]["away"]["id"] == team_id and venue == "away":
                out.append(fx)
        except Exception:
            continue
    return out

def _fixture_goals_for_team(fx: dict, team_id: int) -> Optional[Tuple[int, int]]:
    """
    Retorna (goals_for, goals_against) para o time no fixture (FT).
    """
    try:
        h_id = fx["teams"]["home"]["id"]
        a_id = fx["teams"]["away"]["id"]
        g_home = fx["goals"]["home"]
        g_away = fx["goals"]["away"]
        if g_home is None or g_away is None:
            return None
    except Exception:
        return None
    if team_id == h_id:
        return int(g_home), int(g_away)
    if team_id == a_id:
        return int(g_away), int(g_home)
    return None

def _collect_gf_ga(fixtures: List[dict], team_id: int) -> Tuple[List[int], List[int]]:
    gf = []
    ga = []
    for fx in fixtures:
        gg = _fixture_goals_for_team(fx, team_id)
        if gg is None:
            continue
        gf.append(int(gg[0]))
        ga.append(int(gg[1]))
    return gf, ga




def _collect_win_draw_loss(fixtures: List[dict], team_id: int) -> Tuple[List[int], List[int], List[int]]:
    """
    Constrói 3 arrays binários (win, draw, loss) pro time no fixture.
    """
    w = []
    d = []
    l = []
    for fx in fixtures:
        gg = _fixture_goals_for_team(fx, team_id)
        if gg is None:
            continue
        gf, ga = gg
        if gf > ga:
            w.append(1); d.append(0); l.append(0)
        elif gf < ga:
            w.append(0); d.append(0); l.append(1)
        else:
            w.append(0); d.append(1); l.append(0)
    return w, d, l

def _team_hist_summary(gf: List[int], ga: List[int]) -> Dict[str, int]:
    return {
        "games": len(gf),
        "gf_sum": int(sum(gf)),
        "ga_sum": int(sum(ga)),
        "gf_avg_x10": int(round(10.0 * safe_div(sum(gf), max(1, len(gf))))),
        "ga_avg_x10": int(round(10.0 * safe_div(sum(ga), max(1, len(ga))))),
    }

def _weighted_win_draw_loss(gf: List[int], ga: List[int], halflife: float) -> Optional[Tuple[float, float, float]]:
    """
    Estima P(win), P(draw), P(loss) a partir de gf/ga com EWMA.
    """
    if not gf or not ga or len(gf) != len(ga):
        return None
    n = len(gf)
    if n <= 0:
        return None

    w = _ewma_weights(n, halflife)
    cH = cD = cA = 0.0
    for i in range(n):
        if gf[i] > ga[i]:
            cH += w[i]
        elif gf[i] < ga[i]:
            cA += w[i]
        else:
            cD += w[i]
    tot = cH + cD + cA
    if tot <= 0:
        return None

    # posterior mean (com prior simétrico)
    # Para simplificar, aplica uma suavização leve:
    alpha = 0.6
    pH = (cH + alpha) / (tot + 3*alpha)
    pD = (cD + alpha) / (tot + 3*alpha)
    pA = (cA + alpha) / (tot + 3*alpha)
    # normaliza
    s = pH + pD + pA
    if s <= 0:
        return None
    return float(pH/s), float(pD/s), float(pA/s)

def _weighted_under(arr: List[int], K: int) -> Optional[float]:
    return weighted_beta_rate(arr, lambda x: x < K, EWMA_HALFLIFE_GAMES)

def _weighted_over(arr: List[int], K: int) -> Optional[float]:
    return weighted_beta_rate(arr, lambda x: x > K, EWMA_HALFLIFE_GAMES)

def _weighted_ge(arr: List[int], K: int) -> Optional[float]:
    return weighted_beta_rate(arr, lambda x: x >= K, EWMA_HALFLIFE_GAMES)

def _weighted_le(arr: List[int], K: int) -> Optional[float]:
    return weighted_beta_rate(arr, lambda x: x <= K, EWMA_HALFLIFE_GAMES)

def _combine_independent(p1: float, p2: float) -> float:
    # combinação simples assumindo independência (aproximação)
    return clamp01(p1 * p2)

def _p_btts(gf_home: List[int], ga_home: List[int], gf_away: List[int], ga_away: List[int]) -> Optional[float]:
    """
    Estima BTTS (ambos marcam) com heurística:
      - P(home>=1) * P(away>=1)
    """
    pH = _weighted_ge(gf_home, 1)
    pA = _weighted_ge(gf_away, 1)
    if pH is None or pA is None:
        return None
    return _combine_independent(pH, pA)

def _p_under_over_goals(
    gf_home: List[int], ga_home: List[int],
    gf_away: List[int], ga_away: List[int],
    K: int,
    mode: str,
) -> Optional[float]:
    """
    Estima prob de total goals <K (under) ou >K (over)
    usando soma aproximada: total = goals_home + goals_away.
    Usa distribuição empírica: total = (gf_home + ga_home?) não.
    Simplificação: total ~ (goals_for_home + goals_for_away) com EWMA.
    """
    if not gf_home or not gf_away:
        return None
    # aproximação: total = gf_home + gf_away
    totals = [int(gf_home[i]) + int(gf_away[i]) for i in range(min(len(gf_home), len(gf_away)))]
    if not totals:
        return None
    if mode == "under":
        return weighted_beta_rate(totals, lambda x: x < K, EWMA_HALFLIFE_GAMES)
    if mode == "over":
        return weighted_beta_rate(totals, lambda x: x > K, EWMA_HALFLIFE_GAMES)
    return None

def _p_home_draw_away(
    gf_home: List[int], ga_home: List[int],
    gf_away: List[int], ga_away: List[int]
) -> Optional[Tuple[float, float, float]]:
    """
    Estima P(H/D/A) usando WDL EWMA do mandante (em casa) e visitante (fora).
    Combina de forma heurística.
    """
    wH, dH, lH = _collect_win_draw_loss_from_gfga(gf_home, ga_home)
    wA, dA, lA = _collect_win_draw_loss_from_gfga(gf_away, ga_away)
    if not wH or not wA:
        return None
    pH = _weighted_ratio(wH, EWMA_HALFLIFE_GAMES)
    pD1 = _weighted_ratio(dH, EWMA_HALFLIFE_GAMES)
    pA1 = _weighted_ratio(lH, EWMA_HALFLIFE_GAMES)

    pA = _weighted_ratio(wA, EWMA_HALFLIFE_GAMES)  # away wins away
    pD2 = _weighted_ratio(dA, EWMA_HALFLIFE_GAMES)
    pH2 = _weighted_ratio(lA, EWMA_HALFLIFE_GAMES)

    if None in (pH, pD1, pA1, pA, pD2, pH2):
        return None

    # heurística: home win combina pH (home) com pH2 (away loses)
    p_home = clamp01(0.55 * pH + 0.45 * pH2)
    p_away = clamp01(0.55 * pA + 0.45 * pA1)
    p_draw = clamp01(0.5 * pD1 + 0.5 * pD2)

    s = p_home + p_draw + p_away
    if s <= 0:
        return None
    return float(p_home / s), float(p_draw / s), float(p_away / s)

def _collect_win_draw_loss_from_gfga(gf: List[int], ga: List[int]) -> Tuple[List[int], List[int], List[int]]:
    w = []
    d = []
    l = []
    for i in range(min(len(gf), len(ga))):
        if gf[i] > ga[i]:
            w.append(1); d.append(0); l.append(0)
        elif gf[i] < ga[i]:
            w.append(0); d.append(0); l.append(1)
        else:
            w.append(0); d.append(1); l.append(0)
    return w, d, l

def _weighted_ratio(arr: List[int], halflife: float) -> Optional[float]:
    if not arr:
        return None
    n = len(arr)
    w = _ewma_weights(n, halflife)
    s = 0.0
    for i in range(n):
        s += w[i] * (1.0 if arr[i] else 0.0)
    return float(s)

# =========================
# BLOCO 4/6 — CONSTRAINTS (COERÊNCIA INTRA-JOGO) + CANDIDATOS
# =========================
def _fresh_constraint_state() -> dict:
    """
    Estado de constraints por fixture para evitar contradições:
      - allowed_results: conjunto {H,D,A} possível com legs já escolhidas
      - bounds: faixas em gols (ex.: total>0.5 implica total>=1)
      - btts: None/True/False
    """
    return {
        "allowed_results": {"H", "D", "A"},
        "bounds": {
            "home_goals_min": 0, "home_goals_max": 99,
            "away_goals_min": 0, "away_goals_max": 99,
            "total_goals_min": 0, "total_goals_max": 99,
        },
        "btts": None,
    }

def _constraints_apply(state: dict, meta: dict) -> bool:
    """
    Aplica uma perna no estado de constraints.
    Retorna False se ficar inconsistente.
    """
    try:
        t = meta.get("ctype")
    except Exception:
        return True

    # resultados (1X2 / double chance / draw no bet etc.)
    if t == "result_allow":
        allow = set(meta.get("allow") or [])
        state["allowed_results"] &= allow
        if not state["allowed_results"]:
            return False
        return True

    # gols (min/max)
    if t == "bounds":
        b = state["bounds"]
        for k, v in (meta.get("set") or {}).items():
            if k not in b:
                continue
            if k.endswith("_min"):
                b[k] = max(b[k], int(v))
            elif k.endswith("_max"):
                b[k] = min(b[k], int(v))
        # consistência
        if b["home_goals_min"] > b["home_goals_max"]:
            return False
        if b["away_goals_min"] > b["away_goals_max"]:
            return False
        if b["total_goals_min"] > b["total_goals_max"]:
            return False
        # total deve respeitar soma dos mínimos
        if b["home_goals_min"] + b["away_goals_min"] > b["total_goals_max"]:
            return False
        if b["home_goals_max"] + b["away_goals_max"] < b["total_goals_min"]:
            return False
        return True

    # BTTS
    if t == "btts":
        v = meta.get("value")
        if v is None:
            return True
        if state["btts"] is None:
            state["btts"] = bool(v)
            return True
        return state["btts"] == bool(v)

    return True

def pick_label_type(label: str) -> str:
    lbl = normalize(label)
    if "double chance" in lbl:
        return "dc"
    if "draw no bet" in lbl:
        return "dnb"
    if "btts" in lbl:
        return "btts"
    if "over" in lbl or "under" in lbl:
        return "totals"
    if "win" in lbl or "draw" in lbl:
        return "result"
    return "other"



def odds_from_prob(p: float) -> float:
    """
    Converte probabilidade teórica em odd teórica (sem margem),
    depois aplica desconto conservador (7,5%) na odd.
    """
    p = max(0.0001, min(0.9999, float(p)))
    odd_theo = 1.0 / p
    odd_model = apply_odd_discount(odd_theo)
    return float(odd_model) if odd_model is not None else float("inf")

def _in_range_after_discount(odd_model: float) -> bool:
    return (odd_model >= MIN_ODD) and (odd_model <= MAX_ODD)

def _mk_candidate(
    key: str,
    label: str,
    p: float,
    samples: int,
    fixture_id: int,
    meta: dict,
    dbg: Optional[DebugCollector] = None,
) -> Optional[dict]:
    if samples < HIST_MIN_GAMES:
        if dbg:
            dbg.inc(dbg.candidate_reject_reasons, "min_samples")
        return None
    odd_model = odds_from_prob(p)
    if not math.isfinite(odd_model):
        return None
    # aplica range depois do desconto (como definido)
    if not _in_range_after_discount(odd_model):
        if dbg:
            dbg.inc(dbg.candidate_reject_reasons, "odd_outside_range")
            if len(dbg.candidate_reject_samples) < 10:
                dbg.candidate_reject_samples.append(f"{label} odd={odd_model:.3f} p={p:.3f} samples={samples}")
        return None
    if p < P_MIN:
        if dbg:
            dbg.inc(dbg.candidate_reject_reasons, "p_below_min")
        return None

    c = {
        "key": key,
        "label": label,
        "p": float(p),
        "odd_book": float(odd_model),  # mantém o nome legado usado no resto do projeto
        "odd_theoretical": float(1.0 / max(0.0001, min(0.9999, p))),
        "samples": int(samples),
        "type": pick_label_type(label),
        "meta": meta,
        "fixture_id": int(fixture_id),
        "leg_id": f"{int(fixture_id)}::{key}",
    }
    return c

def _fixture_season_from_date(target_date: str) -> int:
    # heurística: temporada europeia geralmente muda no meio do ano
    try:
        y, m, d = [int(x) for x in target_date.split("-")]
    except Exception:
        y = now_sp().year
        m = now_sp().month
    if m >= 7:
        return y
    return y - 1

def _get_fixture_basic_ids(fx: dict) -> Optional[Tuple[int, int, int, str, str]]:
    try:
        fixture_id = int(fx["fixture"]["id"])
        home_id = int(fx["teams"]["home"]["id"])
        away_id = int(fx["teams"]["away"]["id"])
        home_name = fx["teams"]["home"]["name"]
        away_name = fx["teams"]["away"]["name"]
    except Exception:
        return None
    return fixture_id, home_id, away_id, str(home_name), str(away_name)

def build_match_candidates(api_key: str, fx: dict, dbg: Optional[DebugCollector] = None) -> List[dict]:
    """
    Para um fixture, monta candidatos de apostas "seguras" com base em histórico.
    """
    ids = _get_fixture_basic_ids(fx)
    if ids is None:
        return []
    fixture_id, home_id, away_id, home_name, away_name = ids

    # temporada baseada na data alvo
    try:
        target_date = fx["fixture"]["date"]
        t = utc_iso_to_sp_datetime(target_date)
        target_date_str = t.strftime("%Y-%m-%d") if t else now_sp().strftime("%Y-%m-%d")
    except Exception:
        target_date_str = now_sp().strftime("%Y-%m-%d")
    season = _fixture_season_from_date(target_date_str)

    # coleta histórico
    home_fixtures = _extract_team_fixtures(api_key, home_id, season, venue="home", dbg=dbg)
    away_fixtures = _extract_team_fixtures(api_key, away_id, season, venue="away", dbg=dbg)

    if dbg:
        if not home_fixtures or not away_fixtures:
            dbg.empty_team_fixtures_samples.append(f"{fixture_id} {home_name} vs {away_name} season={season}")

    # pega últimos N para consistência
    home_fixtures = _get_last_n(home_fixtures, 22)
    away_fixtures = _get_last_n(away_fixtures, 22)

    gf_home, ga_home = _collect_gf_ga(home_fixtures, home_id)
    gf_away, ga_away = _collect_gf_ga(away_fixtures, away_id)

    samples = min(len(gf_home), len(gf_away), len(ga_home), len(ga_away))
    if not _need_min_samples(gf_home, ga_home, gf_away, ga_away, n=HIST_MIN_GAMES):
        if dbg:
            dbg.inc(dbg.filter_reasons, "insufficient_history")
        return []

    if dbg:
        dbg.hist_summary[f"{fixture_id}_home_games"] = len(gf_home)
        dbg.hist_summary[f"{fixture_id}_away_games"] = len(gf_away)

    cands: List[dict] = []

    # -------------------------
    # Mercado: 1X2 simplificado (mais seguro via double chance)
    # -------------------------
    phda = _p_home_draw_away(gf_home, ga_home, gf_away, ga_away)
    if phda is not None:
        pH, pD, pA = phda

        # 1X (home or draw)
        p1x = clamp01(pH + pD)
        cand = _mk_candidate(
            key="1X",
            label="Double chance: Home or Draw (1X)",
            p=p1x,
            samples=samples,
            fixture_id=fixture_id,
            meta={"ctype": "result_allow", "allow": ["H", "D"]},
            dbg=dbg
        )
        if cand:
            cands.append(cand)

        # X2 (draw or away)
        px2 = clamp01(pD + pA)
        cand = _mk_candidate(
            key="X2",
            label="Double chance: Draw or Away (X2)",
            p=px2,
            samples=samples,
            fixture_id=fixture_id,
            meta={"ctype": "result_allow", "allow": ["D", "A"]},
            dbg=dbg
        )
        if cand:
            cands.append(cand)

        # 12 (home or away) — sem empate
        p12 = clamp01(pH + pA)
        cand = _mk_candidate(
            key="12",
            label="Double chance: Home or Away (12)",
            p=p12,
            samples=samples,
            fixture_id=fixture_id,
            meta={"ctype": "result_allow", "allow": ["H", "A"]},
            dbg=dbg
        )
        if cand:
            cands.append(cand)

    # -------------------------
    # Mercado: under/over (seguro: over 0.5, under 4.5 etc.)
    # -------------------------
    # Over 0.5 total goals (>=1)
    p_over05 = weighted_beta_rate(
        [int(gf_home[i]) + int(gf_away[i]) for i in range(min(len(gf_home), len(gf_away)))],
        lambda x: x >= 1,
        EWMA_HALFLIFE_GAMES
    )
    if p_over05 is not None:
        cand = _mk_candidate(
            key="O0.5",
            label="Over 0.5 total goals",
            p=float(p_over05),
            samples=samples,
            fixture_id=fixture_id,
            meta={"ctype": "bounds", "set": {"total_goals_min": 1}},
            dbg=dbg
        )
        if cand:
            cands.append(cand)

    # Under 4.5 total goals (<=4)
    p_under45 = weighted_beta_rate(
        [int(gf_home[i]) + int(gf_away[i]) for i in range(min(len(gf_home), len(gf_away)))],
        lambda x: x <= 4,
        EWMA_HALFLIFE_GAMES
    )
    if p_under45 is not None:
        cand = _mk_candidate(
            key="U4.5",
            label="Under 4.5 total goals",
            p=float(p_under45),
            samples=samples,
            fixture_id=fixture_id,
            meta={"ctype": "bounds", "set": {"total_goals_max": 4}},
            dbg=dbg
        )
        if cand:
            cands.append(cand)

    # -------------------------
    # Mercado: BTTS No (mais conservador em alguns cenários)
    # -------------------------
    p_btts = _p_btts(gf_home, ga_home, gf_away, ga_away)
    if p_btts is not None:
        p_btts_no = clamp01(1.0 - float(p_btts))
        cand = _mk_candidate(
            key="BTTS_NO",
            label="BTTS: No (Both teams to score: NO)",
            p=p_btts_no,
            samples=samples,
            fixture_id=fixture_id,
            meta={"ctype": "btts", "value": False},
            dbg=dbg
        )
        if cand:
            cands.append(cand)

    # detalhe debug do jogo
    if dbg and DEBUG:
        dbg.add_match_detail(
            f"{fixture_id} {home_name} vs {away_name} | samples={samples} | cands={len(cands)}"
        )

    return cands



# =========================
# BLOCO 5/6 — COLETA DO DIA + MONTAGEM DE LISTA GLOBAL DE CANDIDATOS
# =========================
def collect_candidates_for_date(api_key: str, target_date: str, dbg: Optional[DebugCollector] = None) -> List[dict]:
    """
    Busca fixtures do dia e gera todos os candidatos possíveis.
    """
    params = {"date": target_date, "timezone": "America/Sao_Paulo"}
    try:
        data = api_request("GET", "/fixtures", api_key, params=params, dbg=dbg)
    except Exception as e:
        if dbg:
            dbg.add_api_error(f"/fixtures date={target_date} err={str(e)[:120]}")
        return []

    res = data.get("response") or []
    all_cands: List[dict] = []
    for fx in res:
        if not fx:
            continue
        # só jogos futuros
        if not is_future_fixture(fx):
            if dbg:
                dbg.inc(dbg.filter_reasons, "fixture_not_future")
            continue
        # filtro liga/time
        if not allowed_fixture(fx):
            if dbg:
                dbg.inc(dbg.filter_reasons, "fixture_filtered")
            continue

        c = build_match_candidates(api_key, fx, dbg=dbg)
        if c:
            all_cands.extend(c)

    if dbg:
        dbg.candidates_total = len(all_cands)
        by_type: Dict[str, int] = {}
        for c in all_cands:
            by_type[c["type"]] = by_type.get(c["type"], 0) + 1
        dbg.candidates_by_type = by_type
    return all_cands

# =========================
# BLOCO 5.5/6 — FORMATAÇÃO / TEXTO TELEGRAM
# =========================
def format_combo(legs: List[dict], prod: float) -> str:
    parts = []
    for c in legs:
        parts.append(f"- {c['label']} | odd={c['odd_book']:.2f} | p={c['p']:.2f} | samples={c['samples']}")
    return "\n".join(parts) + f"\nODD total aprox (produto): {prod:.2f}"

def build_telegram_message(
    target_date: str,
    combos: List[Tuple[List[dict], float]],
    dbg: Optional[DebugCollector] = None
) -> str:
    header = (
        f"🎯 Combos gerados ({len(combos)}) — alvo ~{TARGET_COMBO_ODD:.2f}\n"
        f"📅 Data: {target_date}\n"
        f"🎲 Odds por perna (já descontadas): [{MIN_ODD:.2f} .. {MAX_ODD:.2f}]\n"
        f"🧩 Pernas por aposta: [{MIN_LEGS_TARGET} .. {MAX_LEGS_TARGET}]\n"
        f"📉 Desconto odd: {ODD_DISCOUNT*100:.1f}%\n"
    )
    body = []
    for i, (legs, prod) in enumerate(combos, start=1):
        body.append(f"\n=================\n✅ Aposta {i} | ODD total ~ {prod:.2f} | legs={len(legs)}\n{format_combo(legs, prod)}")
    tail = ""
    if dbg and DEBUG:
        tail = "\n\n--- DEBUG ---\n"
        tail += f"API calls: {dbg.api_calls_used}/{dbg.api_calls_budget}\n"
        tail += f"Stats calls: {dbg.stats_calls_used}/{dbg.stats_calls_budget}\n"
        if dbg.filter_reasons:
            tail += f"Filtros: {dbg.filter_reasons}\n"
        if dbg.candidates_total:
            tail += f"Candidates: total={dbg.candidates_total} by_type={dbg.candidates_by_type}\n"
        if dbg.candidate_reject_reasons:
            tail += f"Candidate rejects: {dbg.candidate_reject_reasons}\n"
        if dbg.candidate_reject_samples:
            tail += "Reject samples:\n" + "\n".join(dbg.candidate_reject_samples[:8]) + "\n"
        if dbg.match_details:
            tail += "Match details:\n" + "\n".join(dbg.match_details[:DEBUG_MAX_MATCH_DETAIL]) + "\n"
        if dbg.api_error_samples:
            tail += "API error samples:\n" + "\n".join(dbg.api_error_samples[:DEBUG_MAX_API_ERROR_SAMPLES]) + "\n"
        if dbg.empty_team_fixtures_samples:
            tail += "Empty team fixtures samples:\n" + "\n".join(dbg.empty_team_fixtures_samples[:8]) + "\n"
    return header + "\n".join(body) + tail

# =========================
# BLOCO 5.8/6 — UTIL DE IDs
# =========================
def _combo_leg_ids(legs: List[dict]) -> List[str]:
    return [str(c["leg_id"]) for c in legs]

def _combo_fixture_ids(legs: List[dict]) -> List[int]:
    return [int(c["fixture_id"]) for c in legs]

def _count_distinct_fixtures(legs: List[dict]) -> int:
    return len(set(_combo_fixture_ids(legs)))

# =========================
# BLOCO 5.9/6 — MONTADOR DE COMBOS (~TARGET) COM COERÊNCIA
# =========================
def _candidate_sort_key(c: dict) -> Tuple[int, float, float]:
    # prioridade: mais amostras -> menor odd -> maior p
    return (int(c.get("samples", 0)), -float(c.get("odd_book", 0.0)), float(c.get("p", 0.0)))

def _best_combo_from_single_fixture(
    fixture_cands: List[dict],
    target_odd: float,
    min_legs: int,
    max_legs: int,
    banned_leg_ids: Set[str],
    fixture_state: dict,
) -> Optional[Tuple[List[dict], float]]:
    """
    Tenta montar um combo apenas com legs do MESMO jogo (preferência),
    garantindo coerência interna via constraints.
    """
    cand = [c for c in fixture_cands if c["leg_id"] not in banned_leg_ids]
    if not cand:
        return None

    # ordena por (amostras, odd baixa, p)
    cand.sort(key=_candidate_sort_key, reverse=True)

    best: Optional[Tuple[List[dict], float, float]] = None  # (legs, prod, score)
    log_target = math.log(max(0.0001, target_odd))

    # busca limitada (beam/dfs pequeno) para não explodir
    # tenta primeiro menos pernas (para cumprir "menos pernas possível")
    for L in range(max(3, min_legs), max_legs + 1):
        # beam
        beam: List[Tuple[List[dict], float, dict]] = [([], 1.0, {"allowed_results": set(fixture_state["allowed_results"]),
                                                                "bounds": {k:[v[0],v[1]] for k,v in fixture_state["bounds"].items()},
                                                                "btts": fixture_state["btts"]})]
        for _ in range(L):
            new_beam: List[Tuple[List[dict], float, dict]] = []
            for legs, prod, st in beam:
                for c in cand:
                    if c in legs:
                        continue
                    # copy state
                    st2 = {"allowed_results": set(st["allowed_results"]),
                           "bounds": {k:[v[0],v[1]] for k,v in st["bounds"].items()},
                           "btts": st["btts"]}
                    if not _constraints_apply(st2, c.get("meta") or {}):
                        continue
                    prod2 = prod * float(c["odd_book"])
                    new_beam.append((legs + [c], prod2, st2))

            if not new_beam:
                beam = []
                break

            # mantém top-N por proximidade do target e preferindo odds mais baixas
            def beam_score(item):
                legs, prod, _st = item
                return (abs(math.log(max(0.0001, prod)) - log_target), -len(legs), prod)
            new_beam.sort(key=beam_score)
            beam = new_beam[:40]

        for legs, prod, st in beam:
            if len(legs) != L:
                continue
            # score final: proximidade do target + penaliza pernas
            score = abs(math.log(max(0.0001, prod)) - log_target) + (0.03 * len(legs))
            if best is None or score < best[2]:
                best = (legs, prod, score)

        if best is not None:
            return (best[0], best[1])

    return None



def greedy_select_legs_for_target(
    all_cands: List[dict],
    target_odd: float,
    seed_leg: Optional[dict] = None,
    max_legs: int = 10,
    banned_leg_ids: Optional[Set[str]] = None,
) -> Tuple[List[dict], float]:
    """
    Fallback (mix de jogos): tenta aproximar target_odd respeitando:
      - perna única
      - coerência por fixture quando repetir fixture dentro do combo
    """
    banned_leg_ids = banned_leg_ids or set()

    cands = [c for c in all_cands if c["leg_id"] not in banned_leg_ids]
    cands.sort(key=_candidate_sort_key, reverse=True)

    legs: List[dict] = []
    prod = 1.0
    per_fixture_state: Dict[int, dict] = {}

    def can_add(c: dict) -> bool:
        fid = int(c["fixture_id"])
        st = per_fixture_state.get(fid)
        if st is None:
            st = _fresh_constraint_state()
        st2 = {"allowed_results": set(st["allowed_results"]),
               "bounds": {k:[v[0],v[1]] for k,v in st["bounds"].items()},
               "btts": st["btts"]}
        if not _constraints_apply(st2, c.get("meta") or {}):
            return False
        # ok
        per_fixture_state[fid] = st2
        return True

    if seed_leg is not None:
        if seed_leg["leg_id"] in banned_leg_ids:
            return [], 1.0
        if not can_add(seed_leg):
            return [], 1.0
        legs.append(seed_leg)
        prod *= float(seed_leg["odd_book"])

    log_target = math.log(max(0.0001, target_odd))

    while len(legs) < max_legs:
        best = None
        best_score = None
        for c in cands:
            if c in legs:
                continue
            # teste coerência sem mutar estado
            fid = int(c["fixture_id"])
            st = per_fixture_state.get(fid) or _fresh_constraint_state()
            st_test = {"allowed_results": set(st["allowed_results"]),
                       "bounds": {k:[v[0],v[1]] for k,v in st["bounds"].items()},
                       "btts": st["btts"]}
            if not _constraints_apply(st_test, c.get("meta") or {}):
                continue

            prod2 = prod * float(c["odd_book"])
            # leve viés para concentrar mais legs no(s) mesmo(s) jogo(s) dentro do combo
            fixture_bias = 0.0
            if legs:
                fixture_bias = (-0.002 if fid in per_fixture_state else 0.002)
            score = abs(math.log(max(0.0001, prod2)) - log_target) + (0.04 * (len(legs) + 1)) + fixture_bias
            # preferir mais amostras já vem do sort; aqui só escolhe melhor aproximação
            if best_score is None or score < best_score:
                best, best_score = c, score

        if best is None:
            break

        # agora aplica de verdade
        if not can_add(best):
            # deveria ser raro, mas por segurança
            cands.remove(best)
            continue

        legs.append(best)
        prod *= float(best["odd_book"])

    return legs, prod

def build_target_combos(
    all_cands: List[dict],
    n_combos: int,
    target_odd: float,
    min_legs: int,
    max_legs: int,
    seed_pool: int,
    dbg: Optional[DebugCollector] = None
) -> List[Tuple[List[dict], float]]:
    """
    Gera até N combos (~target_odd) garantindo:
      ✅ cada perna usada 1 vez (global)
      ✅ coerência por jogo quando o mesmo fixture aparece mais de uma vez
      ✅ preferência: combo com um único jogo (quando possível)
    """
    if not all_cands or n_combos <= 0:
        return []

    # ordenação global (mais amostras primeiro)
    cands = sorted(all_cands, key=_candidate_sort_key, reverse=True)

    # agrupa por fixture para tentar "1 aposta = 1 jogo"
    by_fixture: Dict[int, List[dict]] = {}
    for c in cands:
        by_fixture.setdefault(int(c["fixture_id"]), []).append(c)

    # fixtures ordenados pelo melhor candidato (mais amostras, menor odd)
    fixture_order = sorted(
        by_fixture.keys(),
        key=lambda fid: _candidate_sort_key(by_fixture[fid][0]),
        reverse=True
    )

    out: List[Tuple[List[dict], float]] = []
    used_global: Set[str] = set()
    fixture_used_as_primary: Set[int] = set()
    fixture_states_global: Dict[int, dict] = {}  # coerência entre apostas se repetir fixture

    # Passo 1: tenta montar combos por fixture (preferência)
    for fid in fixture_order:
        if len(out) >= n_combos:
            break
        if fid in fixture_used_as_primary and PREFER_SINGLE_FIXTURE_PER_COMBO:
            continue

        base_state = fixture_states_global.get(fid) or _fresh_constraint_state()
        best = _best_combo_from_single_fixture(
            fixture_cands=by_fixture[fid],
            target_odd=target_odd,
            min_legs=min_legs,
            max_legs=max_legs,
            banned_leg_ids=used_global,
            fixture_state=base_state,
        )
        if best is None:
            continue

        legs, prod = best
        # valida e atualiza estados globais
        st = {"allowed_results": set(base_state["allowed_results"]),
              "bounds": {k:[v[0],v[1]] for k,v in base_state["bounds"].items()},
              "btts": base_state["btts"]}
        ok = True
        for l in legs:
            if l["leg_id"] in used_global:
                ok = False
                break
            if not _constraints_apply(st, l.get("meta") or {}):
                ok = False
                break
        if not ok:
            continue

        out.append((legs, prod))
        used_global.update(l["leg_id"] for l in legs)
        fixture_states_global[fid] = st
        fixture_used_as_primary.add(fid)

    # Passo 2: se faltou, mistura jogos (fallback) mantendo perna única e coerência por fixture
    # (não inventa nada — só usa o que existir)
    while len(out) < n_combos:
        # seed: pega o melhor que ainda não foi usado
        seed = None
        for c in cands[:max(10, seed_pool)]:
            if c["leg_id"] not in used_global:
                seed = c
                break
        legs, prod = greedy_select_legs_for_target(
            all_cands=cands,
            target_odd=target_odd,
            seed_leg=seed,
            max_legs=max_legs,
            banned_leg_ids=used_global,
        )
        if not legs:
            break



        # valida coerência global para fixtures repetidos
        ok = True
        local_by_fixture: Dict[int, dict] = {}
        for l in legs:
            fid = int(l["fixture_id"])
            st_base = fixture_states_global.get(fid) or _fresh_constraint_state()
            st = {"allowed_results": set(st_base["allowed_results"]),
                  "bounds": {k:[v[0],v[1]] for k,v in st_base["bounds"].items()},
                  "btts": st_base["btts"]}
            # aplica legs desse combo pro mesmo fixture
            for x in [z for z in legs if int(z["fixture_id"]) == fid]:
                if not _constraints_apply(st, x.get("meta") or {}):
                    ok = False
                    break
            if not ok:
                break
            local_by_fixture[fid] = st

        if not ok:
            # remove legs usadas? não, pois não aplicamos ainda
            # tenta de novo com outro seed
            # (para evitar loop infinito, remove seed do pool)
            if seed is not None and seed in cands:
                cands.remove(seed)
            continue

        # perna única global
        if any(l["leg_id"] in used_global for l in legs):
            if seed is not None and seed in cands:
                cands.remove(seed)
            continue

        out.append((legs, prod))
        used_global.update(l["leg_id"] for l in legs)
        # atualiza estados globais
        for fid, st in local_by_fixture.items():
            fixture_states_global[fid] = st

    if dbg:
        dbg.inc(dbg.counts, f"target_combos_generated_{len(out)}")
    return out

# =========================
# BLOCO 6/6 — MAIN + DEBUG OUTPUT
# =========================
def select_day_fixtures(api_key: str, target_date: str, dbg: Optional[DebugCollector] = None) -> List[dict]:
    params = {"date": target_date, "timezone": "America/Sao_Paulo"}
    try:
        data = api_request("GET", "/fixtures", api_key, params=params, dbg=dbg)
    except Exception as e:
        if dbg:
            dbg.add_api_error(f"/fixtures select_day_fixtures date={target_date} err={str(e)[:120]}")
        return []

    res = data.get("response") or []
    out = []
    for fx in res:
        if not fx:
            continue
        if not is_future_fixture(fx):
            continue
        if not allowed_fixture(fx):
            continue
        out.append(fx)
    return out

def main():
    dbg = DebugCollector()
    dbg.run_started_sp = now_sp().strftime("%Y-%m-%d %H:%M:%S")
    dbg.api_calls_budget = API_CALLS_BUDGET
    dbg.stats_calls_budget = STATS_CALLS_BUDGET

    if not API_FOOTBALL_KEY:
        raise RuntimeError("API_FOOTBALL_KEY não definido")

    # data alvo: hoje (SP) ou via ENV TARGET_DATE
    target_date = os.getenv("TARGET_DATE", "").strip()
    if not target_date:
        target_date = now_sp().strftime("%Y-%m-%d")
    dbg.target_date = target_date

    # coleta candidatos do dia
    all_cands = collect_candidates_for_date(API_FOOTBALL_KEY, target_date, dbg=dbg)

    # monta combos
    combos = build_target_combos(
        all_cands=all_cands,
        n_combos=N_TARGET_COMBOS,
        target_odd=TARGET_COMBO_ODD,
        min_legs=MIN_LEGS_TARGET,
        max_legs=MAX_LEGS_TARGET,
        seed_pool=SEED_POOL,
        dbg=dbg
    )

    # mensagem
    msg = build_telegram_message(target_date, combos, dbg=dbg)

    # envia
    telegram_send_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)

    # também imprime
    print(msg)

if __name__ == "__main__":
    main()




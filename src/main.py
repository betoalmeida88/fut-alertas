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
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE_URL = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Sao_Paulo")
FINISHED_STATUSES = {"FT", "AET", "PEN"}

MIN_ODD = float(os.getenv("MIN_ODD", "1.15"))
MAX_ODD = float(os.getenv("MAX_ODD", "1.50"))
BOOK_MARGIN = float(os.getenv("BOOK_MARGIN", "0.07"))

HIST_MAX_GAMES = int(os.getenv("HIST_MAX_GAMES", "20"))
HIST_MIN_GAMES = int(os.getenv("HIST_MIN_GAMES", "10"))

HISTORY_LAST_FETCH_RAW = int(os.getenv("HISTORY_LAST_FETCH", "99"))
HISTORY_LAST_FETCH = max(1, min(99, HISTORY_LAST_FETCH_RAW))  # evita erro silencioso

API_CALL_BUDGET = int(os.getenv("API_CALL_BUDGET", "450"))
API_CALLS = 0

# Budget específico para /fixtures/statistics (evita estourar o budget total)
STATS_CALL_BUDGET = int(os.getenv("STATS_CALL_BUDGET", "260"))
STATS_CALLS = 0
STATS_DISABLED_GLOBAL = False

DEBUG_MAX_MATCH_DETAIL = int(os.getenv("DEBUG_MAX_MATCH_DETAIL", "140"))
DEBUG_MAX_REJECT_SAMPLES = int(os.getenv("DEBUG_MAX_REJECT_SAMPLES", "80"))
DEBUG_MAX_API_ERROR_SAMPLES = int(os.getenv("DEBUG_MAX_API_ERROR_SAMPLES", "60"))
DEBUG_MAX_EMPTY_FIXTURES_SAMPLES = int(os.getenv("DEBUG_MAX_EMPTY_FIXTURES_SAMPLES", "60"))

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
    "united states": "usa", "usa": "usa",
    "england": "england", "spain": "spain", "germany": "germany",
    "italy": "italy", "france": "france", "portugal": "portugal",
    "netherlands": "netherlands", "belgium": "belgium",
    "turkey": "turkey", "scotland": "scotland",
    "argentina": "argentina", "mexico": "mexico",
    "brazil": "brazil",
    "world": "world", "international": "world",
}
BRAZIL_STATE_KEYWORDS = ["paulista", "carioca", "mineiro"]

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
        "uefa champions league", "uefa europa league", "uefa europa conference league",
        "uefa super cup", "copa libertadores", "copa sudamericana", "recopa sudamericana",
        "fifa club world cup", "club world cup",
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
    return any(re.search(pat, t) for pat in BLOCK_TEAM_PATTERNS)

def is_allowed_competition(country: str, league_name: str) -> bool:
    nc = norm(country)
    nl = norm(league_name)
    if looks_blocked_text(nl):
        return False
    key = COUNTRY_ALIASES.get(nc, nc)
    if key == "world":
        return any(a in nl for a in ALLOW["world"])
    if key == "brazil":
        if nl in ALLOW["brazil"]:
            return True
        if any(k in nl for k in BRAZIL_STATE_KEYWORDS):
            if "a2" in nl or "a3" in nl:
                return False
            return True
        return False
    if key in ALLOW:
        if nl in ALLOW[key]:
            return True
        return any(a in nl for a in ALLOW[key])
    return False




# =========================
# BLOCO 2/6 — API + TELEGRAM + DEBUG + BUDGET (sem crash)
# =========================
class ApiBudgetExceeded(Exception):
    pass

def api_request(method: str, path: str, api_key: str, params: dict | None = None) -> dict:
    global API_CALLS
    if API_CALLS >= API_CALL_BUDGET:
        raise ApiBudgetExceeded(f"API budget excedido ({API_CALLS}/{API_CALL_BUDGET}).")

    url = f"{BASE_URL}{path}"
    headers = {"x-apisports-key": api_key}

    backoff = 1.0
    for _ in range(5):
        if API_CALLS >= API_CALL_BUDGET:
            raise ApiBudgetExceeded(f"API budget excedido ({API_CALLS}/{API_CALL_BUDGET}).")
        API_CALLS += 1
        try:
            r = requests.get(url, headers=headers, params=params or {}, timeout=30) if method.upper() == "GET" \
                else requests.post(url, headers=headers, params=params or {}, timeout=30)

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

def send_telegram_document(token: str, chat_id: str, filename: str, content: str, caption: str = "") -> None:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    bio = io.BytesIO(content.encode("utf-8", errors="replace"))
    bio.name = filename
    files = {"document": (filename, bio, "text/plain")}
    data = {"chat_id": chat_id, "caption": caption}
    r = requests.post(url, data=data, files=files, timeout=60)
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

    match_details: List[str] = field(default_factory=list)
    api_error_samples: List[str] = field(default_factory=list)
    empty_team_fixtures_samples: List[str] = field(default_factory=list)

    def inc(self, d: Dict[str, int], k: str, v: int = 1) -> None:
        d[k] = d.get(k, 0) + v

    def add_match_detail(self, s: str) -> None:
        if len(self.match_details) < DEBUG_MAX_MATCH_DETAIL:
            self.match_details.append(s)

    def add_reject_sample(self, s: str) -> None:
        if len(self.candidate_reject_samples) < DEBUG_MAX_REJECT_SAMPLES:
            self.candidate_reject_samples.append(s)

    def add_api_error(self, s: str) -> None:
        if len(self.api_error_samples) < DEBUG_MAX_API_ERROR_SAMPLES:
            self.api_error_samples.append(s)

    def add_empty_team_fixtures(self, s: str) -> None:
        if len(self.empty_team_fixtures_samples) < DEBUG_MAX_EMPTY_FIXTURES_SAMPLES:
            self.empty_team_fixtures_samples.append(s)

def pick_label_type(label: str) -> str:
    l = label.lower()
    if "vitória" in l:
        return "vitoria_seca"
    if "dupla chance" in l:
        return "dupla_chance"
    if "gols" in l:
        return "gols"
    if "ambos marcam" in l or "marca" in l:
        return "btts_time_marca"
    if "escante" in l:
        return "escanteios"
    if "cart" in l:
        return "cartoes"
    if "chutes a gol" in l:
        return "sog"
    return "outros"

def debug_check_api_payload(d: dict, path: str, params: dict, dbg: Optional[DebugCollector]) -> None:
    if not dbg:
        return
    errors = d.get("errors")
    if errors:
        dbg.inc(dbg.counts, "api_payload_has_errors")
        dbg.add_api_error(f"[API ERR] {path} params={params} errors={errors}")
    res = d.get("results")
    if isinstance(res, int) and res == 0:
        dbg.inc(dbg.counts, "api_payload_results_0")




# =========================
# BLOCO 3/6 — FIXTURES + STATS (com budget de stats e cache)
# =========================
_fixture_stats_cache: Dict[int, Dict[int, Dict[str, Optional[int]]]] = {}
_team_fixtures_cache: Dict[int, List[dict]] = {}
_team_history_cache: Dict[Tuple[int, str], "TeamHistory"] = {}

def get_team_fixtures(api_key: str, team_id: int, dbg: Optional[DebugCollector] = None) -> List[dict]:
    if team_id in _team_fixtures_cache:
        return _team_fixtures_cache[team_id]

    params = {"team": team_id, "last": HISTORY_LAST_FETCH, "timezone": "America/Sao_Paulo"}
    try:
        data = api_request("GET", "/fixtures", api_key, params=params)
    except ApiBudgetExceeded as e:
        if dbg:
            dbg.inc(dbg.counts, "budget_exceeded_on_team_fixtures")
            dbg.add_api_error(f"[BUDGET] /fixtures(team) team={team_id} err={e}")
        _team_fixtures_cache[team_id] = []
        return []

    debug_check_api_payload(data, "/fixtures(team)", params, dbg)
    fx = data.get("response", []) or []
    _team_fixtures_cache[team_id] = fx

    if dbg and len(fx) == 0:
        dbg.inc(dbg.counts, "team_fixtures_empty")
        dbg.add_empty_team_fixtures(
            f"[EMPTY FIX] team={team_id} last={HISTORY_LAST_FETCH} results={data.get('results')} "
            f"errors={data.get('errors')} parameters={data.get('parameters')}"
        )
    return fx

def _pick_stat(stat_list: List[dict], keys: List[str]) -> Optional[int]:
    for s in stat_list:
        t = (s.get("type") or "").strip().lower()
        if any(k.lower() == t for k in keys):
            return to_int(s.get("value"))
    return None

def get_fixture_stats(api_key: str, fixture_id: int, dbg: Optional[DebugCollector] = None) -> Dict[int, Dict[str, Optional[int]]]:
    global STATS_CALLS, STATS_DISABLED_GLOBAL
    if fixture_id in _fixture_stats_cache:
        return _fixture_stats_cache[fixture_id]

    if STATS_DISABLED_GLOBAL or STATS_CALLS >= STATS_CALL_BUDGET:
        STATS_DISABLED_GLOBAL = True
        if dbg:
            dbg.inc(dbg.counts, "stats_disabled_or_budget_reached")
        _fixture_stats_cache[fixture_id] = {}
        return {}

    params = {"fixture": fixture_id}
    try:
        data = api_request("GET", "/fixtures/statistics", api_key, params=params)
        STATS_CALLS += 1
    except ApiBudgetExceeded as e:
        STATS_DISABLED_GLOBAL = True
        if dbg:
            dbg.inc(dbg.counts, "budget_exceeded_on_fixture_stats")
            dbg.add_api_error(f"[BUDGET] /fixtures/statistics fixture={fixture_id} err={e}")
        _fixture_stats_cache[fixture_id] = {}
        return {}

    debug_check_api_payload(data, "/fixtures/statistics", params, dbg)
    resp = data.get("response", []) or []

    out: Dict[int, Dict[str, Optional[int]]] = {}
    for row in resp:
        team = row.get("team", {}) or {}
        tid = to_int(team.get("id"))
        if tid is None:
            continue
        stats = row.get("statistics", []) or []
        out[int(tid)] = {
            "corners": _pick_stat(stats, ["Corner Kicks"]),
            "sog": _pick_stat(stats, ["Shots on Goal", "Shots on Target"]),
            "yellow": _pick_stat(stats, ["Yellow Cards"]),
            "red": _pick_stat(stats, ["Red Cards"]),
        }

    _fixture_stats_cache[fixture_id] = out
    return out

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

    fixture_ids: List[int] = field(default_factory=list)

    def has_min_games(self) -> bool:
        return self.n_games >= HIST_MIN_GAMES

    def mean(self, arr: List[int]) -> Optional[float]:
        return (sum(arr) / len(arr)) if arr else None




# =========================
# BLOCO 4/6 — BUILD HISTORY (stats só até atingir amostras mínimas)
# =========================
def build_team_history(api_key: str, team_id: int, context: str, dbg: Optional[DebugCollector] = None) -> TeamHistory:
    key = (team_id, context)
    if key in _team_history_cache:
        return _team_history_cache[key]

    hist = TeamHistory(team_id=team_id, context=context)
    fx = get_team_fixtures(api_key, team_id, dbg)

    scanned = 0
    skipped_status = skipped_league = skipped_team = skipped_context = skipped_goals = 0
    stats_calls_skipped_enough = 0
    stats_calls_skipped_budget = 0

    for f in fx:
        scanned += 1
        if hist.n_games >= HIST_MAX_GAMES:
            break

        fixture = f.get("fixture", {}) or {}
        status = ((fixture.get("status", {}) or {}).get("short") or "").strip()
        if status not in FINISHED_STATUSES:
            skipped_status += 1
            continue

        league = f.get("league", {}) or {}
        league_name = league.get("name", "") or ""
        if looks_blocked_text(league_name):
            skipped_league += 1
            continue

        teams = f.get("teams", {}) or {}
        home = teams.get("home", {}) or {}
        away = teams.get("away", {}) or {}
        home_id = to_int(home.get("id"))
        away_id = to_int(away.get("id"))
        if home_id is None or away_id is None:
            skipped_team += 1
            continue
        if looks_blocked_team(home.get("name", "") or "") or looks_blocked_team(away.get("name", "") or ""):
            skipped_team += 1
            continue

        is_home = int(home_id) == int(team_id)
        is_away = int(away_id) == int(team_id)
        if context == "home" and not is_home:
            skipped_context += 1
            continue
        if context == "away" and not is_away:
            skipped_context += 1
            continue

        goals = f.get("goals", {}) or {}
        gh = to_int(goals.get("home"))
        ga = to_int(goals.get("away"))
        if gh is None or ga is None:
            skipped_goals += 1
            continue

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
        hist.fixture_ids.append(int(fixture_id))

        # ===== stats (corners/cards/sog) =====
        # só chama stats enquanto ainda precisa atingir >=10 amostras por categoria
        needs_corners = len(hist.corners_for) < HIST_MIN_GAMES
        needs_cards = len(hist.cards_for) < HIST_MIN_GAMES
        needs_sog = len(hist.sog_for) < HIST_MIN_GAMES
        if not (needs_corners or needs_cards or needs_sog):
            stats_calls_skipped_enough += 1
            continue

        if STATS_DISABLED_GLOBAL:
            stats_calls_skipped_budget += 1
            continue

        st = get_fixture_stats(api_key, int(fixture_id), dbg)
        if not st:
            stats_calls_skipped_budget += 1
            continue

        me = st.get(int(team_id), {})
        opp = st.get(int(opp_id), {})

        c_me, c_opp = to_int(me.get("corners")), to_int(opp.get("corners"))
        if c_me is not None and c_opp is not None:
            hist.corners_for.append(int(c_me))
            hist.corners_against.append(int(c_opp))
        elif dbg:
            dbg.inc(dbg.stats_missing, "corners_missing")

        s_me, s_opp = to_int(me.get("sog")), to_int(opp.get("sog"))
        if s_me is not None and s_opp is not None:
            hist.sog_for.append(int(s_me))
            hist.sog_against.append(int(s_opp))
        elif dbg:
            dbg.inc(dbg.stats_missing, "sog_missing")

        y_me, r_me = to_int(me.get("yellow")), to_int(me.get("red"))
        y_opp, r_opp = to_int(opp.get("yellow")), to_int(opp.get("red"))
        if y_me is not None and r_me is not None and y_opp is not None and r_opp is not None:
            hist.cards_for.append(int(y_me + r_me))
            hist.cards_against.append(int(y_opp + r_opp))
        elif dbg:
            dbg.inc(dbg.stats_missing, "cards_missing")

    if dbg:
        dbg.add_match_detail(
            f"[HIST] team={team_id} ctx={context} usados={hist.n_games}/{HIST_MAX_GAMES} (min={HIST_MIN_GAMES}) "
            f"corners_ok={len(hist.corners_for)} cards_ok={len(hist.cards_for)} sog_ok={len(hist.sog_for)} "
            f"scanned={scanned} skip_status={skipped_status} skip_league={skipped_league} "
            f"skip_team={skipped_team} skip_ctx={skipped_context} skip_goals={skipped_goals} "
            f"skip_stats_enough={stats_calls_skipped_enough} skip_stats_budget={stats_calls_skipped_budget}"
        )
        dbg.inc(dbg.hist_summary, "hist_built")
        if hist.has_min_games():
            dbg.inc(dbg.hist_summary, f"hist_ok_{context}")

    _team_history_cache[key] = hist
    return hist




# =========================
# BLOCO 5/6 — PROBABILIDADES + CANDIDATOS + COMBOS
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

def expected_goals_from_hist(home_h: TeamHistory, away_h: TeamHistory) -> Tuple[Optional[float], Optional[float]]:
    if not home_h.has_min_games() and not away_h.has_min_games():
        return None, None
    home_attack = home_h.mean(home_h.gf) if home_h.has_min_games() else None
    home_def = home_h.mean(home_h.ga) if home_h.has_min_games() else None
    away_attack = away_h.mean(away_h.gf) if away_h.has_min_games() else None
    away_def = away_h.mean(away_h.ga) if away_h.has_min_games() else None
    parts_h = [x for x in [home_attack, away_def] if x is not None]
    parts_a = [x for x in [away_attack, home_def] if x is not None]
    eh = (sum(parts_h) / len(parts_h)) if parts_h else None
    ea = (sum(parts_a) / len(parts_a)) if parts_a else None
    if eh is not None: eh = max(0.2, float(eh))
    if ea is not None: ea = max(0.2, float(ea))
    return eh, ea

def safe_add_candidate(
    candidates: List[dict],
    fixture_id: int,
    leg_code: str,
    label: str,
    p: Optional[float],
    sample_n: int,
    meta: dict,
    dbg: Optional[DebugCollector] = None,
) -> None:
    if p is None:
        if dbg:
            dbg.inc(dbg.candidate_reject_reasons, "p_none")
            dbg.add_reject_sample(f"[REJ] {fixture_id} {leg_code} {label} | p=None")
        return
    if sample_n < HIST_MIN_GAMES:
        if dbg:
            dbg.inc(dbg.candidate_reject_reasons, "sample_lt_min")
            dbg.add_reject_sample(f"[REJ] {fixture_id} {leg_code} {label} | sample={sample_n} < {HIST_MIN_GAMES}")
        return
    odd = odds_with_margin(p)
    if odd < MIN_ODD:
        if dbg:
            dbg.inc(dbg.candidate_reject_reasons, "odd_lt_min")
            dbg.add_reject_sample(f"[REJ] {fixture_id} {leg_code} {label} | p={p:.3f} odd={odd:.2f} < {MIN_ODD}")
        return
    if odd > MAX_ODD:
        if dbg:
            dbg.inc(dbg.candidate_reject_reasons, "odd_gt_max")
            dbg.add_reject_sample(f"[REJ] {fixture_id} {leg_code} {label} | p={p:.3f} odd={odd:.2f} > {MAX_ODD}")
        return
    row = {"fixture_id": fixture_id, "leg_id": f"{fixture_id}:{leg_code}", "label": label,
           "p": float(p), "odd_book": float(odd), "sample_n": int(sample_n), **meta}
    candidates.append(row)
    if dbg:
        dbg.candidates_total += 1
        dbg.inc(dbg.candidates_by_type, pick_label_type(label))

def build_candidates_for_match(
    fixture: dict,
    home_hist: TeamHistory,
    away_hist: TeamHistory,
    dbg: Optional[DebugCollector] = None,
) -> List[dict]:
    fixture_id = int((fixture.get("fixture", {}) or {}).get("id"))
    league = (fixture.get("league", {}) or {}).get("name", "") or ""
    kickoff_iso = (fixture.get("fixture", {}) or {}).get("date") or ""
    kickoff = dt.datetime.fromisoformat(kickoff_iso).strftime("%H:%M") if kickoff_iso else "??:??"
    home = ((fixture.get("teams", {}) or {}).get("home", {}) or {}).get("name", "") or ""
    away = ((fixture.get("teams", {}) or {}).get("away", {}) or {}).get("name", "") or ""
    meta = {"league": league, "kickoff": kickoff, "home": home, "away": away}

    cands: List[dict] = []
    home_ok, away_ok = home_hist.has_min_games(), away_hist.has_min_games()
    eh, ea = expected_goals_from_hist(home_hist, away_hist)

    if dbg:
        dbg.add_match_detail(
            f"[MATCH] {home} x {away} ({league}) {kickoff} home_ok={home_ok} away_ok={away_ok} "
            f"n_home={home_hist.n_games} n_away={away_hist.n_games} eh={eh} ea={ea} "
            f"corners_samples=({len(home_hist.corners_for)},{len(away_hist.corners_for)}) "
            f"cards_samples=({len(home_hist.cards_for)},{len(away_hist.cards_for)}) "
            f"sog_samples=({len(home_hist.sog_for)},{len(away_hist.sog_for)})"
        )

    # 1) Vitória / Dupla chance / Gols / BTTS / Time marca
    if home_ok and away_ok and eh is not None and ea is not None:
        probs = match_probs(eh, ea)
        n_pair = min(home_hist.n_games, away_hist.n_games)

        safe_add_candidate(cands, fixture_id, "1", "Vitória do mandante (1)", probs["home_win"], n_pair, meta, dbg)
        safe_add_candidate(cands, fixture_id, "2", "Vitória do visitante (2)", probs["away_win"], n_pair, meta, dbg)
        safe_add_candidate(cands, fixture_id, "1X", "Dupla chance (1X)", probs["home_win"] + probs["draw"], n_pair, meta, dbg)
        safe_add_candidate(cands, fixture_id, "X2", "Dupla chance (X2)", probs["away_win"] + probs["draw"], n_pair, meta, dbg)

        safe_add_candidate(cands, fixture_id, "G_O15", "Mais de 1.5 gols (jogo)", probs["over_1_5"], n_pair, meta, dbg)
        safe_add_candidate(cands, fixture_id, "G_U35", "Menos de 3.5 gols (jogo)", probs["under_3_5"], n_pair, meta, dbg)

        p_h, p_a = probs["home_score_1+"], probs["away_score_1+"]
        safe_add_candidate(cands, fixture_id, "H_1+", "Mandante marca (>=1)", p_h, n_pair, meta, dbg)
        safe_add_candidate(cands, fixture_id, "A_1+", "Visitante marca (>=1)", p_a, n_pair, meta, dbg)
        safe_add_candidate(cands, fixture_id, "BTTS_Y", "Ambos marcam — SIM", p_h * p_a, n_pair, meta, dbg)
        safe_add_candidate(cands, fixture_id, "BTTS_N", "Ambos marcam — NÃO", 1.0 - (p_h * p_a), n_pair, meta, dbg)
    else:
        # modo SOLO (quando só um lado tem base >=10)
        if home_ok and eh is not None:
            p = 1.0 - poisson_probs(eh, 10)[0]
            safe_add_candidate(cands, fixture_id, "H_1+_SOLO", "Mandante marca (>=1)", p, home_hist.n_games, meta, dbg)
        if away_ok and ea is not None:
            p = 1.0 - poisson_probs(ea, 10)[0]
            safe_add_candidate(cands, fixture_id, "A_1+_SOLO", "Visitante marca (>=1)", p, away_hist.n_games, meta, dbg)

    # 2) Corners / Cards / SOG — total e por time (se um lado tiver 10+, usa só ele)
    CORNERS_TOTAL_LINES = [7.5, 8.5, 9.5, 10.5]
    CARDS_TOTAL_LINES = [2.5, 3.5, 4.5, 5.5]
    SOG_TOTAL_LINES = [6.5, 7.5, 8.5, 9.5]
    TEAM_LINES = {"corners": [3.5, 4.5, 5.5], "cards": [1.5, 2.5, 3.5], "sog": [2.5, 3.5, 4.5]}

    def total_series(h: TeamHistory, kind: str) -> List[int]:
        if kind == "corners": return [a + b for a, b in zip(h.corners_for, h.corners_against)]
        if kind == "cards": return [a + b for a, b in zip(h.cards_for, h.cards_against)]
        if kind == "sog": return [a + b for a, b in zip(h.sog_for, h.sog_against)]
        return []

    def add_total_market(kind: str, line: float, over: bool) -> None:
        hs, as_ = total_series(home_hist, kind), total_series(away_hist, kind)
        use = None
        if len(hs) >= HIST_MIN_GAMES and len(as_) >= HIST_MIN_GAMES:
            ph = (sum(1 for x in hs if x > line) / len(hs)) if over else (sum(1 for x in hs if x < line) / len(hs))
            pa = (sum(1 for x in as_ if x > line) / len(as_)) if over else (sum(1 for x in as_ if x < line) / len(as_))
            use = ((ph + pa) / 2.0, min(len(hs), len(as_)))
        elif len(hs) >= HIST_MIN_GAMES:
            p = (sum(1 for x in hs if x > line) / len(hs)) if over else (sum(1 for x in hs if x < line) / len(hs))
            use = (p, len(hs))
        elif len(as_) >= HIST_MIN_GAMES:
            p = (sum(1 for x in as_ if x > line) / len(as_)) if over else (sum(1 for x in as_ if x < line) / len(as_))
            use = (p, len(as_))
        else:
            if dbg: dbg.inc(dbg.candidate_reject_reasons, f"{kind}_total_samples_lt_min")
            return

        p, n = use
        unit = "escanteios" if kind == "corners" else "cartões" if kind == "cards" else "chutes a gol"
        code = f"{kind.upper()}_{'O' if over else 'U'}_{str(line).replace('.','')}"
        label = f"{'Mais' if over else 'Menos'} de {line} {unit} (jogo)"
        safe_add_candidate(cands, fixture_id, code, label, p, n, meta, dbg)

    def add_team_market(side: str, kind: str, line: float, over: bool) -> None:
        h = home_hist if side == "home" else away_hist
        arr = h.corners_for if kind == "corners" else h.cards_for if kind == "cards" else h.sog_for
        if len(arr) < HIST_MIN_GAMES:
            if dbg: dbg.inc(dbg.candidate_reject_reasons, f"{kind}_team_samples_lt_min")
            return
        p = (sum(1 for x in arr if x > line) / len(arr)) if over else (sum(1 for x in arr if x < line) / len(arr))
        unit = "escanteios" if kind == "corners" else "cartões" if kind == "cards" else "chutes a gol"
        who = "Mandante" if side == "home" else "Visitante"
        code = f"{side[0].upper()}{kind.upper()}_{'O' if over else 'U'}_{str(line).replace('.','')}"
        label = f"{who}: {'Mais' if over else 'Menos'} de {line} {unit}"
        safe_add_candidate(cands, fixture_id, code, label, p, len(arr), meta, dbg)

    for ln in CORNERS_TOTAL_LINES:
        add_total_market("corners", ln, True); add_total_market("corners", ln, False)
    for ln in CARDS_TOTAL_LINES:
        add_total_market("cards", ln, True); add_total_market("cards", ln, False)
    for ln in SOG_TOTAL_LINES:
        add_total_market("sog", ln, True); add_total_market("sog", ln, False)

    for kind, lines in TEAM_LINES.items():
        for ln in lines:
            add_team_market("home", kind, ln, True); add_team_market("home", kind, ln, False)
            add_team_market("away", kind, ln, True); add_team_market("away", kind, ln, False)

    return cands

def build_combo_high_odds(target: float, candidates: List[dict], used_leg_ids: set, min_legs: int) -> Tuple[List[dict], float]:
    legs: List[dict] = []
    prod = 1.0
    used_fixture_ids: set[int] = set()

    def feasible(prod_now: float, odd: float, remaining: int) -> bool:
        return (prod_now * odd) * (MAX_ODD ** remaining) >= target * 0.98

    for i in range(min_legs):
        remaining = min_legs - i
        desired = (target / max(1e-9, prod)) ** (1.0 / remaining)
        desired = max(MIN_ODD, min(MAX_ODD, desired))
        best, best_key = None, None
        for c in candidates:
            if c["leg_id"] in used_leg_ids: continue
            if c["fixture_id"] in used_fixture_ids: continue
            odd = c["odd_book"]
            if not (MIN_ODD <= odd <= MAX_ODD): continue
            if not feasible(prod, odd, remaining - 1): continue
            key = (abs(odd - desired), -odd, -c["p"])
            if best is None or key < best_key:
                best, best_key = c, key
        if not best: break
        legs.append(best); used_leg_ids.add(best["leg_id"]); used_fixture_ids.add(best["fixture_id"]); prod *= best["odd_book"]

    while prod < target * 0.98 and len(legs) < 10:
        desired = max(MIN_ODD, min(MAX_ODD, target / max(1e-9, prod)))
        best, best_key = None, None
        for c in candidates:
            if c["leg_id"] in used_leg_ids: continue
            if c["fixture_id"] in used_fixture_ids: continue
            odd = c["odd_book"]
            if not (MIN_ODD <= odd <= MAX_ODD): continue
            key = (abs(odd - desired), -odd, -c["p"])
            if best is None or key < best_key:
                best, best_key = c, key
        if not best: break
        legs.append(best); used_leg_ids.add(best["leg_id"]); used_fixture_ids.add(best["fixture_id"]); prod *= best["odd_book"]

    return legs, prod




# =========================
# BLOCO 6/6 — MENSAGEM + DEBUG TXT + MAIN (sempre envia TXT)
# =========================
def market_legend(label: str) -> str:
    l = label.lower()
    if "vitória" in l: return "Vitória seca: precisa vencer no tempo normal."
    if "dupla chance" in l: return "Dupla chance: cobre 2 resultados (ex.: 1X = casa ou empate)."
    if "ambos marcam" in l: return "Ambos marcam: SIM (os dois fazem gol) / NÃO (apenas um ou nenhum)."
    if "marca" in l and "ambos" not in l: return "Time marca: time faz pelo menos 1 gol."
    if "gols" in l: return "Mais/Menos gols: total de gols no jogo (ou do time, quando indicado)."
    if "escante" in l: return "Mais/Menos escanteios: total do jogo (ou do time, quando indicado)."
    if "cart" in l: return "Mais/Menos cartões: amarelo+vermelho (total ou do time)."
    if "chutes a gol" in l: return "Mais/Menos chutes a gol: finalizações no alvo (total ou do time)."
    return "Mercado conforme descrito."

def format_telegram_message(target_date: str, combos: List[Tuple[float, List[dict], float]]) -> str:
    lines: List[str] = []
    lines.append(f"📅 Palpites para {target_date} (Brasília)")
    lines.append("Critérios: casa x fora (até 20 oficiais), min 10 amostras por mercado.")
    lines.append("")
    used_legends: Dict[str, str] = {}
    any_legs = False

    for target, legs, prod in combos:
        lines.append(f"🧩 Combo ~{int(target)}  |  odd estimada ≈ {prod:.2f}")
        if len(legs) < 2:
            lines.append("• Sem pernas suficientes dentro dos critérios hoje.\n")
            continue
        any_legs = True
        for leg in legs:
            lines.append(f"• {leg['home']} x {leg['away']}")
            lines.append(f"  {leg['kickoff']} • {leg['league']}")
            lines.append(f"  ✅ {leg['label']}  (odd≈{leg['odd_book']:.2f})")
            used_legends[market_legend(leg["label"])] = market_legend(leg["label"])
        lines.append("")

    if not any_legs:
        lines.append("😕 Hoje não encontrei combinações que respeitem os filtros mínimos.\n")

    if used_legends:
        lines.append("📌 Legenda dos mercados usados")
        for txt in used_legends.values():
            lines.append(f"• {txt}")
    return "\n".join(lines).strip()

def build_debug_report(dbg: DebugCollector, combos: List[Tuple[float, List[dict], float]], candidates: List[dict]) -> str:
    lines: List[str] = []
    lines.append("===== DEBUG REPORT (APOSTAS) =====")
    lines.append(f"Run (SP): {dbg.run_started_sp}")
    lines.append(f"Target date (SP): {dbg.target_date}\n")
    lines.append("---- Config ----")
    lines.append(f"MIN_ODD={MIN_ODD} MAX_ODD={MAX_ODD} BOOK_MARGIN={BOOK_MARGIN}")
    lines.append(f"HIST_MAX_GAMES={HIST_MAX_GAMES} HIST_MIN_GAMES={HIST_MIN_GAMES}")
    lines.append(f"HISTORY_LAST_FETCH_RAW={HISTORY_LAST_FETCH_RAW} HISTORY_LAST_FETCH_CAPPED={HISTORY_LAST_FETCH}")
    lines.append(f"API_CALL_BUDGET={dbg.api_calls_budget} API_CALLS_USED={dbg.api_calls_used}")
    lines.append(f"STATS_CALL_BUDGET={dbg.stats_calls_budget} STATS_CALLS_USED={dbg.stats_calls_used}")
    lines.append(f"STATS_DISABLED_GLOBAL={STATS_DISABLED_GLOBAL}\n")

    lines.append("---- Contagens do pipeline ----")
    for k in sorted(dbg.counts.keys()):
        lines.append(f"{k}: {dbg.counts[k]}")
    lines.append("")

    if dbg.filter_reasons:
        lines.append("---- Motivos de filtro (jogos) ----")
        for k in sorted(dbg.filter_reasons.keys()):
            lines.append(f"{k}: {dbg.filter_reasons[k]}")
        lines.append("")

    lines.append("---- Histórico (resumo) ----")
    for k in sorted(dbg.hist_summary.keys()):
        lines.append(f"{k}: {dbg.hist_summary[k]}")
    lines.append("")

    if dbg.stats_missing:
        lines.append("---- Stats ausentes em fixtures (contagem) ----")
        for k in sorted(dbg.stats_missing.keys()):
            lines.append(f"{k}: {dbg.stats_missing[k]}")
        lines.append("")

    lines.append("---- Candidatos ----")
    lines.append(f"candidates_total_aceitos: {dbg.candidates_total}")
    for k in sorted(dbg.candidates_by_type.keys()):
        lines.append(f"  by_type.{k}: {dbg.candidates_by_type[k]}")
    lines.append("")

    if dbg.candidate_reject_reasons:
        lines.append("---- Rejeições (motivos) ----")
        for k in sorted(dbg.candidate_reject_reasons.keys()):
            lines.append(f"{k}: {dbg.candidate_reject_reasons[k]}")
        lines.append("")

    if dbg.candidate_reject_samples:
        lines.append("---- Exemplos de rejeição (amostra) ----")
        lines.extend(dbg.candidate_reject_samples)
        lines.append("")

    if dbg.api_error_samples:
        lines.append("---- API payload errors / budget (amostra) ----")
        lines.extend(dbg.api_error_samples)
        lines.append("")

    if dbg.empty_team_fixtures_samples:
        lines.append("---- Times sem fixtures retornados (amostra) ----")
        lines.extend(dbg.empty_team_fixtures_samples)
        lines.append("")

    lines.append("---- Combos montados ----")
    for target, legs, prod in combos:
        lines.append(f"Combo ~{int(target)} | prod={prod:.2f} | legs={len(legs)}")
        for leg in legs:
            lines.append(f"  - {leg['home']} x {leg['away']} | {leg['kickoff']} {leg['league']} | "
                         f"{leg['label']} | odd={leg['odd_book']:.2f} p={leg['p']:.3f} n={leg['sample_n']}")
    lines.append("")

    if dbg.match_details:
        lines.append("---- Detalhes por partida/histórico (limitado) ----")
        lines.extend(dbg.match_details)
        lines.append("")

    lines.append("---- Snapshot: Top 25 candidatos por odd ----")
    top = sorted(candidates, key=lambda x: (-x["odd_book"], -x["p"]))[:25]
    for c in top:
        lines.append(f"{c['odd_book']:.2f} | p={c['p']:.3f} n={c['sample_n']} | "
                     f"{c['home']} x {c['away']} | {c['label']} | {c['league']}")
    return "\n".join(lines).strip()

def main() -> None:
    global STATS_CALLS
    api_key = os.environ["APISPORTS_KEY"]
    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_chat_id = os.environ["TELEGRAM_CHAT_ID"]

    dbg = DebugCollector()
    dbg.run_started_sp = dt.datetime.now(TZ).isoformat(timespec="seconds")
    dbg.api_calls_budget = API_CALL_BUDGET
    dbg.stats_calls_budget = STATS_CALL_BUDGET

    now_sp = dt.datetime.now(TZ)
    target_date = (now_sp.date() + dt.timedelta(days=1)).isoformat()
    dbg.target_date = target_date

    # carrega jogos (3 datas UTC para cobrir fuso)
    utc0 = dt.datetime.utcnow().date()
    utc_dates = [utc0, utc0 + dt.timedelta(days=1), utc0 + dt.timedelta(days=2)]
    games_map: Dict[int, dict] = {}

    for d in utc_dates:
        params = {"date": d.isoformat(), "timezone": "America/Sao_Paulo"}
        try:
            fx = api_request("GET", "/fixtures", api_key, params=params)
        except ApiBudgetExceeded as e:
            dbg.inc(dbg.counts, "budget_exceeded_on_daily_fixtures")
            dbg.add_api_error(f"[BUDGET] /fixtures(date) date={d.isoformat()} err={e}")
            break
        debug_check_api_payload(fx, "/fixtures(date)", params, dbg)
        for g in fx.get("response", []) or []:
            fid = int((g.get("fixture", {}) or {}).get("id"))
            games_map[fid] = g

    games_all = list(games_map.values())
    dbg.counts["games_all_3days_utc"] = len(games_all)

    tomorrow_all: List[dict] = []
    for g in games_all:
        kickoff_iso = (g.get("fixture", {}) or {}).get("date")
        if not kickoff_iso:
            dbg.inc(dbg.filter_reasons, "missing_kickoff_iso")
            continue
        kickoff = dt.datetime.fromisoformat(kickoff_iso)
        if kickoff.date().isoformat() == target_date:
            tomorrow_all.append(g)
    dbg.counts["tomorrow_all"] = len(tomorrow_all)

    tomorrow_filtered: List[dict] = []
    for g in tomorrow_all:
        league = g.get("league", {}) or {}
        country = league.get("country", "") or ""
        league_name = league.get("name", "") or ""
        teams = g.get("teams", {}) or {}
        home_name = (teams.get("home", {}) or {}).get("name", "") or ""
        away_name = (teams.get("away", {}) or {}).get("name", "") or ""

        if looks_blocked_text(league_name):
            dbg.inc(dbg.filter_reasons, "blocked_league_text"); continue
        if looks_blocked_team(home_name) or looks_blocked_team(away_name):
            dbg.inc(dbg.filter_reasons, "blocked_team_name"); continue
        if not is_allowed_competition(country, league_name):
            dbg.inc(dbg.filter_reasons, "not_allowed_competition"); continue
        tomorrow_filtered.append(g)
    dbg.counts["tomorrow_filtered"] = len(tomorrow_filtered)

    candidates: List[dict] = []
    kept_matches = 0
    for g in tomorrow_filtered:
        teams = g.get("teams", {}) or {}
        home_id = to_int((teams.get("home", {}) or {}).get("id"))
        away_id = to_int((teams.get("away", {}) or {}).get("id"))
        if home_id is None or away_id is None:
            dbg.inc(dbg.filter_reasons, "missing_team_id"); continue

        home_hist = build_team_history(api_key, int(home_id), "home", dbg)
        away_hist = build_team_history(api_key, int(away_id), "away", dbg)

        if not home_hist.has_min_games() and not away_hist.has_min_games():
            dbg.inc(dbg.filter_reasons, "both_hist_lt_min10"); continue

        kept_matches += 1
        candidates.extend(build_candidates_for_match(g, home_hist, away_hist, dbg))

    dbg.counts["matches_kept_after_hist_rule"] = kept_matches
    candidates.sort(key=lambda x: (-x["odd_book"], -x["p"], -x.get("sample_n", 0)))
    dbg.counts["candidates_final"] = len(candidates)

    combos_plan = [(2.0, 2), (3.0, 3), (4.0, 4), (5.0, 4)]
    used_leg_ids: set[str] = set()
    combos: List[Tuple[float, List[dict], float]] = []
    for target, min_legs in combos_plan:
        legs, prod = build_combo_high_odds(target, candidates, used_leg_ids, min_legs)
        combos.append((target, legs, prod))

    dbg.api_calls_used = API_CALLS
    dbg.stats_calls_used = STATS_CALLS

    msg = format_telegram_message(target_date, combos)
    debug_txt = build_debug_report(dbg, combos, candidates)

    # sempre tenta enviar os dois
    try:
        send_telegram_message(tg_token, tg_chat_id, msg)
    except Exception as e:
        debug_txt += "\n\n[ERRO] Falha ao enviar mensagem: " + repr(e)

    try:
        send_telegram_document(tg_token, tg_chat_id, f"debug_{target_date}.txt", debug_txt, caption="📎 Debug do processamento")
    except Exception as e:
        send_telegram_message(tg_token, tg_chat_id, f"Falha ao enviar debug TXT: {repr(e)}")

if __name__ == "__main__":
    main()


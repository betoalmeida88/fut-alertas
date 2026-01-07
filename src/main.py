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

# Range de odds por perna (interpretei "1,5 e 1,15" como 1.15–1.50)
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

# Novo: modo "5 combos alvo ~3"
N_TARGET_COMBOS = int(os.getenv("N_TARGET_COMBOS", "5"))            # <<< AJUSTE (era 10)
TARGET_COMBO_ODD = float(os.getenv("TARGET_COMBO_ODD", "3.0"))
MIN_LEGS_TARGET = int(os.getenv("MIN_LEGS_TARGET", "2"))
MAX_LEGS_TARGET = int(os.getenv("MAX_LEGS_TARGET", "5"))
SEED_POOL_TARGET = int(os.getenv("SEED_POOL_TARGET", "80"))

# Telegram: split para não estourar limite
TELEGRAM_MAX_LEN = int(os.getenv("TELEGRAM_MAX_LEN", "3800"))

BLOCK_LEAGUE_WORDS = [
    "women", "woman", "femin", "feminino", "femenino", "femenil",
    "u23", "u22", "u21", "u20", "u19", "u18", "u17", "u16", "u15",
    "youth", "junior", "reserve", "reserves",
    "friendly", "friendlies", "amistoso", "amistosos", "treino", "test",
    "development", "academy",
    "serie c",
    "serie d",
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
    "brazil": {"serie a", "serie b", "copa do brasil", "copa do nordeste", "supercopa do brasil"},
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
        return nl in ALLOW[key]
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

def _split_text_for_telegram(text: str, max_len: int) -> List[str]:
    text = text or ""
    if len(text) <= max_len:
        return [text]

    parts: List[str] = []
    buf: List[str] = []
    cur = 0
    for line in text.splitlines(True):  # mantém \n
        if cur + len(line) > max_len and buf:
            parts.append("".join(buf).rstrip())
            buf = []
            cur = 0
        buf.append(line)
        cur += len(line)

    if buf:
        parts.append("".join(buf).rstrip())

    return [p for p in parts if p.strip()]

def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = _split_text_for_telegram(text, TELEGRAM_MAX_LEN)
    for i, chunk in enumerate(chunks):
        r = requests.post(url, data={"chat_id": chat_id, "text": chunk}, timeout=30)
        r.raise_for_status()
        if i < len(chunks) - 1:
            time.sleep(0.25)

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
# BLOCO 4/6 — BUILD HISTORY (stats opcionais, usa cache e respeita filtros)
# =========================
def _fixture_final_score(fx: dict) -> Tuple[Optional[int], Optional[int]]:
    g = fx.get("goals") or {}
    gh = to_int(g.get("home"))
    ga = to_int(g.get("away"))
    return gh, ga

def _fixture_status_short(fx: dict) -> str:
    st = (((fx.get("fixture") or {}).get("status") or {}).get("short")) or ""
    return str(st)

def _fixture_league_ok(fx: dict) -> bool:
    league = fx.get("league") or {}
    name = str(league.get("name") or "")
    country = str(league.get("country") or "")
    return is_allowed_competition(country, name)

def _fixture_teams_ok(fx: dict, team_id: int, context: str) -> bool:
    teams = fx.get("teams") or {}
    h = teams.get("home") or {}
    a = teams.get("away") or {}
    hid = to_int(h.get("id"))
    aid = to_int(a.get("id"))

    if context == "home":
        if hid != team_id:
            return False
        if looks_blocked_team(str(a.get("name") or "")):
            return False
    else:
        if aid != team_id:
            return False
        if looks_blocked_team(str(h.get("name") or "")):
            return False

    if looks_blocked_team(str(h.get("name") or "")):
        return False
    if looks_blocked_team(str(a.get("name") or "")):
        return False
    return True

def build_team_history(api_key: str, team_id: int, context: str, dbg: Optional[DebugCollector] = None) -> TeamHistory:
    key = (team_id, context)
    if key in _team_history_cache:
        if dbg:
            dbg.inc(dbg.hist_summary, "hist_cache_hit")
        return _team_history_cache[key]

    fx = get_team_fixtures(api_key, team_id, dbg=dbg)
    hist = TeamHistory(team_id=team_id, context=context)

    for m in fx:
        st = _fixture_status_short(m)
        if st not in FINISHED_STATUSES:
            if dbg:
                dbg.inc(dbg.filter_reasons, f"hist_skip_status_{st or 'NA'}")
            continue

        if not _fixture_league_ok(m):
            if dbg:
                dbg.inc(dbg.filter_reasons, "hist_skip_league_not_allowed")
            continue

        if not _fixture_teams_ok(m, team_id, context):
            if dbg:
                dbg.inc(dbg.filter_reasons, "hist_skip_team_blocked_or_context_mismatch")
            continue

        if hist.n_games >= HIST_MAX_GAMES:
            break

        fixture = m.get("fixture") or {}
        fid = to_int(fixture.get("id"))
        if fid is None:
            if dbg:
                dbg.inc(dbg.filter_reasons, "hist_skip_no_fixture_id")
            continue

        teams = m.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        hid = to_int(home.get("id"))
        aid = to_int(away.get("id"))
        if hid is None or aid is None:
            if dbg:
                dbg.inc(dbg.filter_reasons, "hist_skip_no_team_id")
            continue

        gh, ga = _fixture_final_score(m)
        if gh is None or ga is None:
            if dbg:
                dbg.inc(dbg.filter_reasons, "hist_skip_missing_goals")
            continue

        if context == "home":
            gf = gh
            ga_ = ga
            opp_id = aid
        else:
            gf = ga
            ga_ = gh
            opp_id = hid

        hist.gf.append(int(gf))
        hist.ga.append(int(ga_))
        hist.fixture_ids.append(int(fid))
        hist.n_games += 1

        # stats opcionais (corners/sog/cards) se o budget permitir
        stats = get_fixture_stats(api_key, int(fid), dbg=dbg)
        if stats:
            me = stats.get(int(team_id)) or {}
            opp = stats.get(int(opp_id)) or {}

            c_for = me.get("corners")
            c_against = opp.get("corners")
            if c_for is not None and c_against is not None:
                hist.corners_for.append(int(c_for))
                hist.corners_against.append(int(c_against))
            else:
                if dbg:
                    dbg.inc(dbg.stats_missing, "corners_missing")

            sog_for = me.get("sog")
            sog_against = opp.get("sog")
            if sog_for is not None and sog_against is not None:
                hist.sog_for.append(int(sog_for))
                hist.sog_against.append(int(sog_against))
            else:
                if dbg:
                    dbg.inc(dbg.stats_missing, "sog_missing")

            y_for = me.get("yellow")
            y_against = opp.get("yellow")
            r_for = me.get("red")
            r_against = opp.get("red")
            if y_for is not None and y_against is not None:
                hist.cards_for.append(int(y_for) + int(r_for or 0))
                hist.cards_against.append(int(y_against) + int(r_against or 0))
            else:
                if dbg:
                    dbg.inc(dbg.stats_missing, "cards_missing")
        else:
            if dbg:
                dbg.inc(dbg.stats_missing, "stats_unavailable_or_budget")

    if dbg:
        dbg.inc(dbg.hist_summary, "hist_built")
        if not hist.has_min_games():
            dbg.inc(dbg.hist_summary, "hist_below_min_games")

    _team_history_cache[key] = hist
    return hist


# =========================
# BLOCO 5/6 — PROBABILIDADES + CANDIDATOS + COMBOS
# =========================
def odds_with_margin(p: float) -> float:
    p = max(0.0001, min(0.9999, float(p)))
    p_adj = p * (1.0 - BOOK_MARGIN)
    p_adj = max(0.0001, min(0.9999, p_adj))
    return 1.0 / p_adj

def clamp_prob(p: Optional[float]) -> Optional[float]:
    if p is None:
        return None
    return max(0.0, min(1.0, float(p)))

def mean(arr: List[int]) -> Optional[float]:
    return (sum(arr) / len(arr)) if arr else None

def rate_at_least_1(arr: List[int]) -> Optional[float]:
    if not arr:
        return None
    return sum(1 for x in arr if x >= 1) / len(arr)

def rate_under(arr: List[int], thr: float) -> Optional[float]:
    if not arr:
        return None
    return sum(1 for x in arr if x < thr) / len(arr)

def rate_over(arr: List[int], thr: float) -> Optional[float]:
    if not arr:
        return None
    return sum(1 for x in arr if x > thr) / len(arr)

def btts_rate(gf_a: List[int], gf_b: List[int]) -> Optional[float]:
    if not gf_a or not gf_b:
        return None
    n = min(len(gf_a), len(gf_b))
    if n == 0:
        return None
    ok = 0
    for i in range(n):
        if gf_a[i] >= 1 and gf_b[i] >= 1:
            ok += 1
    return ok / n

def compute_market_probs(home_hist: TeamHistory, away_hist: TeamHistory) -> Dict[str, Optional[float]]:
    probs: Dict[str, Optional[float]] = {}

    probs["home_scores"] = clamp_prob(rate_at_least_1(home_hist.gf))
    probs["away_scores"] = clamp_prob(rate_at_least_1(away_hist.gf))

    probs["btts_yes"] = clamp_prob(btts_rate(home_hist.gf, away_hist.gf))
    probs["btts_no"] = (1.0 - probs["btts_yes"]) if probs["btts_yes"] is not None else None

    mu_goals = None
    m_h = mean(home_hist.gf)
    m_a = mean(away_hist.gf)
    if m_h is not None and m_a is not None:
        mu_goals = m_h + m_a

    if mu_goals is not None:
        lam = max(0.01, mu_goals)
        p_le_1 = math.exp(-lam) * (1.0 + lam)
        probs["over_1_5_goals"] = clamp_prob(1.0 - p_le_1)

        p_le_3 = math.exp(-lam) * (1.0 + lam + lam**2 / 2.0 + lam**3 / 6.0)
        probs["under_3_5_goals"] = clamp_prob(p_le_3)

    tot_corners = []
    n = min(len(home_hist.corners_for), len(away_hist.corners_for))
    if n > 0:
        for i in range(n):
            tot_corners.append(home_hist.corners_for[i] + away_hist.corners_for[i])

    probs["under_10_5_corners"] = clamp_prob(rate_under(tot_corners, 10.5))
    probs["over_7_5_corners"] = clamp_prob(rate_over(tot_corners, 7.5))

    tot_sog = []
    n2 = min(len(home_hist.sog_for), len(away_hist.sog_for))
    if n2 > 0:
        for i in range(n2):
            tot_sog.append(home_hist.sog_for[i] + away_hist.sog_for[i])

    probs["under_7_5_sog"] = clamp_prob(rate_under(tot_sog, 7.5))
    probs["away_under_3_5_sog"] = clamp_prob(rate_under(away_hist.sog_for, 3.5))
    probs["home_over_3_5_sog"] = clamp_prob(rate_over(home_hist.sog_for, 3.5))

    return probs

def candidate_markets_from_probs(probs: Dict[str, Optional[float]]) -> List[dict]:
    candidates: List[dict] = []

    def add(label: str, key: str):
        p = probs.get(key)
        if p is None:
            return
        odd_book = odds_with_margin(p)
        candidates.append({
            "label": label,
            "key": key,
            "p": float(p),
            "odd_book": float(odd_book),
            "type": pick_label_type(label),
        })

    add("Mandante marca (>=1)", "home_scores")
    add("Visitante marca (>=1)", "away_scores")

    add("Ambos marcam — SIM", "btts_yes")
    add("Ambos marcam — NÃO", "btts_no")

    add("Mais de 1.5 gols (jogo)", "over_1_5_goals")
    add("Menos de 3.5 gols (jogo)", "under_3_5_goals")

    add("Menos de 10.5 escanteios (jogo)", "under_10_5_corners")
    add("Mais de 7.5 escanteios (jogo)", "over_7_5_corners")

    add("Menos de 7.5 chutes a gol (jogo)", "under_7_5_sog")
    add("Visitante: Menos de 3.5 chutes a gol", "away_under_3_5_sog")
    add("Mandante: Mais de 3.5 chutes a gol", "home_over_3_5_sog")

    return candidates

def filter_candidates(cands: List[dict], dbg: Optional[DebugCollector] = None) -> List[dict]:
    out = []
    for c in cands:
        odd = c["odd_book"]
        if odd < MIN_ODD or odd > MAX_ODD:
            if dbg:
                dbg.inc(dbg.candidate_reject_reasons, "odd_out_of_range")
                dbg.add_reject_sample(f"[ODD] {c['label']} odd={odd:.2f}")
            continue
        if c["p"] < 0.55:
            if dbg:
                dbg.inc(dbg.candidate_reject_reasons, "p_too_low")
                dbg.add_reject_sample(f"[P] {c['label']} p={c['p']:.3f}")
            continue
        out.append(c)
    return out

def build_match_candidates(api_key: str, fx: dict, dbg: Optional[DebugCollector] = None) -> List[dict]:
    fixture = fx.get("fixture") or {}
    teams = fx.get("teams") or {}
    league = fx.get("league") or {}

    fid = to_int(fixture.get("id"))
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    hid = to_int(home.get("id"))
    aid = to_int(away.get("id"))

    if fid is None or hid is None or aid is None:
        if dbg:
            dbg.inc(dbg.filter_reasons, "day_skip_missing_ids")
        return []

    home_hist = build_team_history(api_key, int(hid), "home", dbg=dbg)
    away_hist = build_team_history(api_key, int(aid), "away", dbg=dbg)

    if not home_hist.has_min_games() or not away_hist.has_min_games():
        if dbg:
            dbg.inc(dbg.filter_reasons, "day_skip_hist_below_min")
        return []

    probs = compute_market_probs(home_hist, away_hist)
    cands = candidate_markets_from_probs(probs)
    cands = filter_candidates(cands, dbg=dbg)

    kickoff = to_int(fixture.get("timestamp")) or 0
    ctry = str(league.get("country") or "")
    lname = str(league.get("name") or "")
    hname = str(home.get("name") or "")
    aname = str(away.get("name") or "")

    for c in cands:
        c["fixture_id"] = int(fid)
        c["kickoff_ts"] = int(kickoff)
        c["country"] = ctry
        c["league"] = lname
        c["home"] = hname
        c["away"] = aname
        c["leg_id"] = f"{fid}:{c['key']}"

    return cands

def _fmt_hhmm_from_ts(ts: int) -> str:
    try:
        return dt.datetime.fromtimestamp(ts, tz=TZ).strftime("%H:%M")
    except Exception:
        return "--:--"

def select_best_legs_for_combo(
    all_cands: List[dict],
    size: int,
    dbg: Optional[DebugCollector] = None
) -> Tuple[List[dict], float]:
    if not all_cands:
        return [], 1.0

    cands = sorted(all_cands, key=lambda x: (x["odd_book"], x["p"]), reverse=True)

    legs: List[dict] = []
    used_fixture_ids = set()
    used_leg_ids = set()
    prod = 1.0

    for _ in range(size):
        best = None
        best_key = None
        for c in cands:
            if c["fixture_id"] in used_fixture_ids:
                continue
            if c["leg_id"] in used_leg_ids:
                continue

            key = (c["odd_book"], c["p"])
            if best is None or key > best_key:
                best, best_key = c, key

        if not best:
            break

        legs.append(best)
        used_fixture_ids.add(best["fixture_id"])
        used_leg_ids.add(best["leg_id"])
        prod *= best["odd_book"]

    if dbg:
        if len(legs) < size:
            dbg.inc(dbg.counts, f"combo_size_{size}_incomplete")
        else:
            dbg.inc(dbg.counts, f"combo_size_{size}_ok")

    return legs, prod

def format_combo(legs: List[dict], odd_prod: float) -> str:
    out = []
    out.append(f"🧩 Combo ~{len(legs)}  |  odd estimada ≈ {odd_prod:.2f}")
    for c in legs:
        out.append(f"• {c['home']} x {c['away']}")
        out.append(f"  {_fmt_hhmm_from_ts(c['kickoff_ts'])} • {c['league']}")
        out.append(f"  ✅ {c['label']}  (odd≈{c['odd_book']:.2f})")
    return "\n".join(out)

def build_combos_for_day(
    all_cands: List[dict],
    dbg: Optional[DebugCollector] = None
) -> List[Tuple[List[dict], float]]:
    combos: List[Tuple[List[dict], float]] = []
    for size in (2, 3, 4, 5):
        legs, prod = select_best_legs_for_combo(all_cands, size=size, dbg=dbg)
        # só aceita combo COMPLETO (evita duplicar ~4 vindo de size=5 incompleto)
        if len(legs) == size and len(legs) >= 2:
            combos.append((legs, prod))
    return combos

def build_all_candidates_for_day(api_key: str, day_fixtures: List[dict], dbg: Optional[DebugCollector] = None) -> List[dict]:
    all_cands: List[dict] = []
    for fx in day_fixtures:
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

def greedy_select_legs_for_target(
    all_cands: List[dict],
    target_odd: float,
    seed_leg: Optional[dict] = None,
    max_legs: int = 5,
    dbg: Optional[DebugCollector] = None
) -> Tuple[List[dict], float]:
    if not all_cands:
        return [], 1.0

    cands = sorted(all_cands, key=lambda x: (x["odd_book"], x["p"]), reverse=True)

    legs: List[dict] = []
    used_leg_ids = set()
    used_fixture_ids = set()
    prod = 1.0

    if seed_leg is not None:
        legs.append(seed_leg)
        used_leg_ids.add(seed_leg["leg_id"])
        used_fixture_ids.add(seed_leg["fixture_id"])
        prod *= seed_leg["odd_book"]

    cur_delta = abs(target_odd - prod)

    while len(legs) < max_legs:
        best = None
        best_key = None

        for c in cands:
            if c["leg_id"] in used_leg_ids:
                continue
            if c["fixture_id"] in used_fixture_ids:
                continue

            new_prod = prod * c["odd_book"]
            delta = abs(target_odd - new_prod)

            key = (-delta, c["odd_book"], c["p"])
            if best is None or key > best_key:
                best, best_key = c, key

        if not best:
            break

        new_prod = prod * best["odd_book"]
        new_delta = abs(target_odd - new_prod)

        if new_delta + 1e-12 >= cur_delta:
            break

        legs.append(best)
        used_leg_ids.add(best["leg_id"])
        used_fixture_ids.add(best["fixture_id"])
        prod = new_prod
        cur_delta = new_delta

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
    if not all_cands or n_combos <= 0:
        return []

    cands = sorted(all_cands, key=lambda x: (x["odd_book"], x["p"]), reverse=True)
    seeds = cands[:max(1, min(len(cands), seed_pool))]

    scored: List[Tuple[float, float, float, Tuple[str, ...], List[dict], float]] = []
    seen: set[Tuple[str, ...]] = set()

    for seed in seeds:
        legs, prod = greedy_select_legs_for_target(
            all_cands=cands,
            target_odd=target_odd,
            seed_leg=seed,
            max_legs=max_legs,
            dbg=dbg
        )
        if len(legs) < min_legs:
            continue

        key = tuple(sorted(l["leg_id"] for l in legs))
        if key in seen:
            continue
        seen.add(key)

        delta = abs(target_odd - prod)
        avg_odd = sum(l["odd_book"] for l in legs) / len(legs)
        avg_p = sum(l["p"] for l in legs) / len(legs)

        scored.append((delta, -avg_odd, -avg_p, key, legs, prod))

    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    out: List[Tuple[List[dict], float]] = [(x[4], x[5]) for x in scored[:n_combos]]

    if len(out) < n_combos:
        legacy = build_combos_for_day(all_cands, dbg=dbg)
        for legs, prod in legacy:
            if len(out) >= n_combos:
                break
            key = tuple(sorted(l["leg_id"] for l in legs))
            if key in seen:
                continue
            seen.add(key)
            out.append((legs, prod))

    if dbg:
        dbg.inc(dbg.counts, f"target_combos_generated_{len(out)}")

    return out


# =========================
# BLOCO 6/6 — MAIN + DEBUG OUTPUT
# =========================
def select_day_fixtures(api_key: str, target_date: str, dbg: Optional[DebugCollector] = None) -> List[dict]:
    params = {"date": target_date, "timezone": "America/Sao_Paulo"}
    try:
        data = api_request("GET", "/fixtures", api_key, params=params)
    except ApiBudgetExceeded as e:
        if dbg:
            dbg.inc(dbg.counts, "budget_exceeded_on_day_fixtures")
            dbg.add_api_error(f"[BUDGET] /fixtures(date) date={target_date} err={e}")
        return []

    debug_check_api_payload(data, "/fixtures(date)", params, dbg)
    fx = data.get("response", []) or []

    now_sp = dt.datetime.now(TZ)
    out = []
    for m in fx:
        fixture = m.get("fixture") or {}
        ts = to_int(fixture.get("timestamp")) or 0
        dtt = dt.datetime.fromtimestamp(ts, tz=TZ) if ts else None
        if not dtt or dtt <= now_sp:
            if dbg:
                dbg.inc(dbg.filter_reasons, "day_skip_not_future")
            continue

        league = m.get("league") or {}
        if not is_allowed_competition(str(league.get("country") or ""), str(league.get("name") or "")):
            if dbg:
                dbg.inc(dbg.filter_reasons, "day_skip_league_not_allowed")
            continue

        teams = m.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        if looks_blocked_team(str(home.get("name") or "")) or looks_blocked_team(str(away.get("name") or "")):
            if dbg:
                dbg.inc(dbg.filter_reasons, "day_skip_blocked_team")
            continue

        out.append(m)

    return out

def build_picks_message(target_date: str, combos: List[Tuple[List[dict], float]]) -> str:
    out = []
    out.append(f"📅 Palpites para {target_date} (Brasília)")
    out.append(
        "Critérios: casa x fora (até 20 oficiais), min 10 amostras por mercado.\n"
        f"Odds por perna: {MIN_ODD:.2f}–{MAX_ODD:.2f} | "
        f"Objetivo: ~{TARGET_COMBO_ODD:.2f} | "
        f"Qtd combos: {len(combos)}\n"
    )
    for legs, prod in combos:
        out.append(format_combo(legs, prod))
        out.append("")
    out.append("📌 Legenda dos mercados usados")
    out.append("• Time marca: time faz pelo menos 1 gol.")
    out.append("• Mais/Menos escanteios: total do jogo (ou do time, quando indicado).")
    out.append("• Mais/Menos chutes a gol: finalizações no alvo (total ou do time).")
    out.append("• Mais/Menos gols: total de gols no jogo (ou do time, quando indicado).")
    out.append("• Ambos marcam: SIM (os dois fazem gol) / NÃO (apenas um ou nenhum).")
    return "\n".join(out).strip()

def build_debug_report(dbg: DebugCollector) -> str:
    out = []
    out.append("==== DEBUG REPORT ====")
    out.append(f"run_started_sp: {dbg.run_started_sp}")
    out.append(f"target_date: {dbg.target_date}")
    out.append(f"api_calls_budget: {dbg.api_calls_budget}")
    out.append(f"api_calls_used: {dbg.api_calls_used}")
    out.append(f"stats_calls_budget: {dbg.stats_calls_budget}")
    out.append(f"stats_calls_used: {dbg.stats_calls_used}")
    out.append("")

    def dump_counter(title: str, d: Dict[str, int]):
        if not d:
            return
        out.append(f"-- {title} --")
        for k, v in sorted(d.items(), key=lambda x: (-x[1], x[0])):
            out.append(f"{k}: {v}")
        out.append("")

    dump_counter("counts", dbg.counts)
    dump_counter("filter_reasons", dbg.filter_reasons)
    dump_counter("hist_summary", dbg.hist_summary)
    dump_counter("stats_missing", dbg.stats_missing)
    dump_counter("candidates_by_type", dbg.candidates_by_type)
    dump_counter("candidate_reject_reasons", dbg.candidate_reject_reasons)

    if dbg.candidate_reject_samples:
        out.append("-- candidate_reject_samples --")
        out.extend(dbg.candidate_reject_samples)
        out.append("")

    if dbg.match_details:
        out.append("-- match_details --")
        out.extend(dbg.match_details)
        out.append("")

    if dbg.api_error_samples:
        out.append("-- api_error_samples --")
        out.extend(dbg.api_error_samples)
        out.append("")

    if dbg.empty_team_fixtures_samples:
        out.append("-- empty_team_fixtures_samples --")
        out.extend(dbg.empty_team_fixtures_samples)
        out.append("")

    return "\n".join(out).strip()

def main() -> None:
    api_key = os.environ.get("APISPORTS_KEY", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not api_key or not tg_token or not tg_chat_id:
        raise SystemExit("Faltam envs: APISPORTS_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")

    now_sp = dt.datetime.now(TZ)
    t_date = (now_sp + dt.timedelta(days=1)).date().isoformat()
    target_date = os.getenv("TARGET_DATE") or t_date

    dbg = DebugCollector()
    dbg.run_started_sp = now_sp.isoformat()
    dbg.target_date = target_date
    dbg.api_calls_budget = API_CALL_BUDGET
    dbg.stats_calls_budget = STATS_CALL_BUDGET

    day_fx = select_day_fixtures(api_key, target_date, dbg=dbg)
    day_fx = day_fx[:20]

    all_cands = build_all_candidates_for_day(api_key, day_fx, dbg=dbg)

    if N_TARGET_COMBOS > 0:
        combos = build_target_combos(
            all_cands=all_cands,
            n_combos=N_TARGET_COMBOS,
            target_odd=TARGET_COMBO_ODD,
            min_legs=MIN_LEGS_TARGET,
            max_legs=MAX_LEGS_TARGET,
            seed_pool=SEED_POOL_TARGET,
            dbg=dbg
        )
    else:
        combos = build_combos_for_day(all_cands, dbg=dbg)

    msg = build_picks_message(target_date, combos)

    try:
        send_telegram_message(tg_token, tg_chat_id, msg)
    except Exception as e:
        try:
            send_telegram_message(tg_token, tg_chat_id, f"Falha ao enviar mensagem principal: {repr(e)}")
        except Exception:
            pass

    dbg.api_calls_used = API_CALLS
    dbg.stats_calls_used = STATS_CALLS

    send_debug = (os.getenv("SEND_DEBUG") or "").strip().lower() in {"1", "true", "yes", "y"}
    if not send_debug:
        return

    debug_txt = build_debug_report(dbg)

    try:
        send_telegram_document(tg_token, tg_chat_id, f"debug_{target_date}.txt", debug_txt, caption="📎 Debug do processamento")
    except Exception as e:
        send_telegram_message(tg_token, tg_chat_id, f"Falha ao enviar debug TXT: {repr(e)}")

if __name__ == "__main__":
    main()
    

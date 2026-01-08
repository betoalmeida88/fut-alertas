#versão atual
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
 
BASE_URL = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Sao_Paulo")
FINISHED_STATUSES = {"FT", "AET", "PEN"}
 
# Range de odds por perna (aplicado APÓS o desconto de odd teórica)
MIN_ODD = float(os.getenv("MIN_ODD", "1.15"))
MAX_ODD = float(os.getenv("MAX_ODD", "1.30"))
 
# Desconto conservador sobre a odd teórica (ex.: 2.00 -> 1.85 com 7.5%)
ODD_DISCOUNT = float(os.getenv("ODD_DISCOUNT", "0.075"))  # 7.5%
 
# (LEGACY) método antigo (ajuste na probabilidade) — mantido por compatibilidade
# será substituído pelo ODD_DISCOUNT quando atualizarmos o BLOCO 5
BOOK_MARGIN = float(os.getenv("BOOK_MARGIN", "0.07"))
 
# Ponderação por recência no histórico (EWMA): meia-vida em jogos
EWMA_HALFLIFE_GAMES = float(os.getenv("EWMA_HALFLIFE_GAMES", "6.0"))
 
# Suavização Bayesiana (prior Beta) para eventos binários (over/under, BTTS, etc.)
BETA_PRIOR_ALPHA = float(os.getenv("BETA_PRIOR_ALPHA", "1.0"))
BETA_PRIOR_BETA = float(os.getenv("BETA_PRIOR_BETA", "1.0"))
 
# #
# Data padrão: jogos do DIA (fuso SP). 0 = hoje, 1 = amanhã, etc.
# DEFAULT_TARGET_DATE_OFFSET_DAYS = int(os.getenv("DEFAULT_TARGET_DATE_OFFSET_DAYS", "0"))
# Data padrão: jogos do DIA (fuso SP). 0 = hoje, 1 = amanhã, etc.
DEFAULT_TARGET_DATE_OFFSET_DAYS = int(os.getenv("DEFAULT_TARGET_DATE_OFFSET_DAYS", "0"))
 
# Preferências/garantias do portfólio (validadas no BLOCO 5)
PREFER_SINGLE_FIXTURE_PER_COMBO = (
    os.getenv("PREFER_SINGLE_FIXTURE_PER_COMBO", "1").strip().lower() in {"1", "true", "yes", "y"}
)
ENFORCE_GLOBAL_UNIQUE_LEGS = (
    os.getenv("ENFORCE_GLOBAL_UNIQUE_LEGS", "1").strip().lower() in {"1", "true", "yes", "y"}
)
ENFORCE_GLOBAL_FIXTURE_COHERENCE = (
    os.getenv("ENFORCE_GLOBAL_FIXTURE_COHERENCE", "1").strip().lower() in {"1", "true", "yes", "y"}
)
 
HIST_MAX_GAMES = int(os.getenv("HIST_MAX_GAMES", "20"))
HIST_MIN_GAMES = int(os.getenv("HIST_MIN_GAMES", "10"))
 
HISTORY_LAST_FETCH_RAW = int(os.getenv("HISTORY_LAST_FETCH", "99"))
HISTORY_LAST_FETCH = max(1, min(99, HISTORY_LAST_FETCH_RAW))
 
API_CALL_BUDGET = int(os.getenv("API_CALL_BUDGET", "800"))
API_CALLS = 0
 
# Budget específico para /fixtures/statistics
STATS_CALL_BUDGET = int(os.getenv("STATS_CALL_BUDGET", "600"))
STATS_CALLS = 0
STATS_DISABLED_GLOBAL = False
 
DEBUG_MAX_MATCH_DETAIL = int(os.getenv("DEBUG_MAX_MATCH_DETAIL", "140"))
DEBUG_MAX_REJECT_SAMPLES = int(os.getenv("DEBUG_MAX_REJECT_SAMPLES", "80"))
DEBUG_MAX_API_ERROR_SAMPLES = int(os.getenv("DEBUG_MAX_API_ERROR_SAMPLES", "60"))
DEBUG_MAX_EMPTY_FIXTURES_SAMPLES = int(os.getenv("DEBUG_MAX_EMPTY_FIXTURES_SAMPLES", "60"))
 
# Objetivo: 10 combos ~3.0
N_TARGET_COMBOS = int(os.getenv("N_TARGET_COMBOS", "10"))
TARGET_COMBO_ODD = float(os.getenv("TARGET_COMBO_ODD", "3.0"))
MIN_LEGS_TARGET = int(os.getenv("MIN_LEGS_TARGET", "5"))
MAX_LEGS_TARGET = int(os.getenv("MAX_LEGS_TARGET", "5"))
SEED_POOL_TARGET = int(os.getenv("SEED_POOL_TARGET", "120"))
 
# Telegram split
TELEGRAM_MAX_LEN = int(os.getenv("TELEGRAM_MAX_LEN", "3800"))
 
BLOCK_LEAGUE_WORDS = [
    "women",
    "woman",
    "femin",
    "feminino",
    "femenino",
    "femenil",
    "u23",
    "u22",
    "u21",
    "u20",
    "u19",
    "u18",
    "u17",
    "u16",
    "u15",
    "youth",
    "juvenil",
    "junior",
    "reserves",
    "reserve",
    "b team",
    "ii",
    "ii ",
    "friendly",
    "friendlies",
    "amistoso",
    "cup women",
    "feminine",
    "academy",
    "acad",
    "development",
]
 
BLOCK_TEAM_PATTERNS = [
    r"\bu(?:\s)?\d{2}\b",  # U20, U-20, u 20
    r"\b(reserve|reserves)\b",
    r"\bii\b",  # time B / II
    r"\b(women|femin|femen)\b",
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
 
BRAZIL_STATE_KEYWORDS = ["paulista", "carioca", "mineiro"]
 
ALLOW: Dict[str, set[str]] = {
    "england": {"premier league", "championship", "fa cup", "efl cup", "league cup"},
    "spain": {"la liga", "segunda division", "copa del rey", "supercopa de espana", "Super Cup", "Super Copa da Espanha"},
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
    "brazil": {"serie a", "serie b", "copa do brasil", "supercopa do brasil", "paulista", "carioca", "mineiro"},
    "world": {
        "uefa champions league",
        "champions league",
        "uefa europa league",
        "uefa europa conference league",
        "fifa club world cup",
        "club world cup",
        "world cup",
        "euro championship",
        "copa america",
        "uefa nations league",
        "africa cup of nations",
        "afcon",
        "asian cup",
        "concacaf gold cup",
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
            r = (
                requests.get(url, headers=headers, params=params or {}, timeout=30)
                if method.upper() == "GET"
                else requests.post(url, headers=headers, params=params or {}, timeout=30)
            )
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
    for line in text.splitlines(True):
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

    def add_empty_team_fixtures(self, s: str) -> None:
        if len(self.empty_team_fixtures_samples) < DEBUG_MAX_EMPTY_FIXTURES_SAMPLES:
            self.empty_team_fixtures_samples.append(s)

    def add_reject_sample(self, s: str) -> None:
        if len(self.candidate_reject_samples) < DEBUG_MAX_REJECT_SAMPLES:
            self.candidate_reject_samples.append(s)

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
# BLOCO 3/6 — FIXTURES + STATS (com budget + cache)
# =========================
_fixture_stats_cache: Dict[int, Dict[int, Dict[str, Optional[int]]]] = {}
_team_fixtures_cache: Dict[int, List[dict]] = {}
_team_history_cache: Dict[Tuple[int, str], "TeamHistory"] = {}

def get_team_fixtures(api_key: str, team_id: int, dbg: Optional[DebugCollector] = None, last: Optional[int] = None) -> List[dict]:
    """
    Retorna fixtures recentes do time (mix home/away) — o corte final de 20 home/away
    é feito no BLOCO 4.
    """
    if team_id in _team_fixtures_cache:
        if dbg:
            dbg.inc(dbg.counts, "team_fixtures_cache_hit")
        return _team_fixtures_cache[team_id]

    params = {
        "team": team_id,
        "last": int(last) if last is not None else HISTORY_LAST_FETCH,
        "timezone": "America/Sao_Paulo",
    }
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
            f"[EMPTY FIX] team={team_id} last={params['last']} results={data.get('results')} "
            f"errors={data.get('errors')} parameters={data.get('parameters')}"
        )
    return fx

def _pick_stat(stat_list: List[dict], keys: List[str]) -> Optional[int]:
    """
    Procura dentro de 'statistics' (API-Football) por um tipo específico.
    Retorna int quando possível, senão None.
    """
    for s in stat_list:
        t = (s.get("type") or "").strip().lower()
        if any(k.lower() == t for k in keys):
            return to_int(s.get("value"))
    return None

def get_fixture_stats(api_key: str, fixture_id: int, dbg: Optional[DebugCollector] = None) -> Dict[int, Dict[str, Optional[int]]]:
    """
    Retorna stats por team_id para um fixture:
      - corners
      - sog
      - yellow
      - red
    Respeita budget específico de stats e faz cache por fixture_id.
    """
    global STATS_CALLS, STATS_DISABLED_GLOBAL

    if fixture_id in _fixture_stats_cache:
        if dbg:
            dbg.inc(dbg.counts, "fixture_stats_cache_hit")
        return _fixture_stats_cache[fixture_id]

    # Desabilita globalmente se atingiu budget (evita travar execução)
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
    if not resp and dbg:
        dbg.inc(dbg.counts, "fixture_stats_empty_response")

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
 
    # Gols (inteiros)
    gf: List[int] = field(default_factory=list)
    ga: List[int] = field(default_factory=list)
 
    # Stats (inteiros) — podem faltar (amostras mínimas serão validadas no BLOCO 5)
    corners_for: List[int] = field(default_factory=list)
    corners_against: List[int] = field(default_factory=list)
 
    cards_for: List[int] = field(default_factory=list)
    cards_against: List[int] = field(default_factory=list)
 
    sog_for: List[int] = field(default_factory=list)
    sog_against: List[int] = field(default_factory=list)
 
    # Para recência/ponderação: ids e (opcional) timestamps do kickoff
    fixture_ids: List[int] = field(default_factory=list)
    fixture_ts: List[int] = field(default_factory=list)  # epoch seconds (opcional)
 
    def has_min_games(self) -> bool:
        return self.n_games >= HIST_MIN_GAMES
 
    def mean(self, arr: List[int]) -> Optional[float]:
        return (sum(arr) / len(arr)) if arr else None
 
    def market_sample_counts(self) -> Dict[str, int]:
        """
        Contagem de amostras válidas (por mercado) no recorte de histórico já filtrado (home/away).
        Observação:
        - Para gols/resultado (chance dupla e totais de gols), usamos n_games/gf/ga (placar sempre existe).
        - Para corners/cards/sog, as listas só recebem valores quando houver dados completos
          para ambos os times (for e against) naquele fixture do histórico (regra dura).
        """
        return {
            "dupla_chance": int(self.n_games),
            "gols": int(len(self.gf)),  # equivalente a n_games, mas mantido explícito
            "escanteios": int(min(len(self.corners_for), len(self.corners_against))),
            "cartoes": int(min(len(self.cards_for), len(self.cards_against))),
            "sog": int(min(len(self.sog_for), len(self.sog_against))),
        }
 
    def has_min_samples_all_5_markets(self, min_samples: int) -> bool:
        """
        Regra dura (ajuste alinhado):
        Para o time ser considerado apto, exige pelo menos `min_samples` amostras válidas
        em cada um dos 5 mercados:
          - dupla chance (baseado em resultados / n_games)
          - gols (baseado em gf/ga)
          - escanteios (for/against completos)
          - cartões (for/against completos)
          - chutes a gol (for/against completos)
 
        Obs.: "dupla_chance" e "gols" compartilham a mesma base (n_games/gf/ga).
        """
        c = self.market_sample_counts()
        return (
            c["dupla_chance"] >= min_samples
            and c["gols"] >= min_samples
            and c["escanteios"] >= min_samples
            and c["cartoes"] >= min_samples
            and c["sog"] >= min_samples
        )
 
 
# =========================
# BLOCO 4/6 — BUILD HISTORY (20 casa/fora, min 10 geral, stats opcionais)
# =========================
def _fixture_final_score(fx: dict) -> Tuple[Optional[int], Optional[int]]:
    g = fx.get("goals") or {}
    gh = to_int(g.get("home"))
    ga = to_int(g.get("away"))
    return gh, ga
 
 
def _fixture_status_short(fx: dict) -> str:
    st = (((fx.get("fixture") or {}).get("status") or {}).get("short")) or ""
    return str(st)
 
 
def _fixture_timestamp(fx: dict) -> int:
    fixture = fx.get("fixture") or {}
    return to_int(fixture.get("timestamp")) or 0
 
 
def _fixture_league_ok(fx: dict) -> bool:
    league = fx.get("league") or {}
    name = str(league.get("name") or "")
    country = str(league.get("country") or "")
    return is_allowed_competition(country, name)
 
 
def _fixture_teams_ok(fx: dict, team_id: int, context: str) -> bool:
    """
    Garante:
      - fixture é do contexto correto (team_id é home se context=home, away se context=away)
      - nomes não bloqueados (youth/women/reserves etc.)
    """
    teams = fx.get("teams") or {}
    h = teams.get("home") or {}
    a = teams.get("away") or {}
    hid = to_int(h.get("id"))
    aid = to_int(a.get("id"))
 
    if context == "home":
        if hid != team_id:
            return False
    else:
        if aid != team_id:
            return False
 
    if looks_blocked_team(str(h.get("name") or "")):
        return False
    if looks_blocked_team(str(a.get("name") or "")):
        return False
    return True
 
 
def build_team_history(api_key: str, team_id: int, context: str, dbg: Optional[DebugCollector] = None) -> TeamHistory:
    """
    Constrói histórico do time, respeitando:
      - Apenas jogos finalizados (FT/AET/PEN)
      - Apenas ligas permitidas
      - Apenas contexto home/away correto
      - Até HIST_MAX_GAMES (20)
 
    Stats (corners/sog/cards) são opcionais e podem gerar menos amostras.
    Regra de amostra (ajuste alinhado):
      - corners/sog/cards só contam quando houver dados completos para ambos os times
        (for e against) no fixture histórico.
    """
    key = (team_id, context)
    if key in _team_history_cache:
        if dbg:
            dbg.inc(dbg.hist_summary, "hist_cache_hit")
        return _team_history_cache[key]
 
    fx = get_team_fixtures(api_key, team_id, dbg=dbg)
    hist = TeamHistory(team_id=team_id, context=context)
 
    # Garante ordem do mais recente -> mais antigo (para ponderação EWMA depois)
    fx_sorted = sorted(fx, key=_fixture_timestamp, reverse=True)
    seen_fixtures: Set[int] = set()
 
    for m in fx_sorted:
        if hist.n_games >= HIST_MAX_GAMES:
            break
 
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
 
        fixture = m.get("fixture") or {}
        fid = to_int(fixture.get("id"))
        if fid is None:
            if dbg:
                dbg.inc(dbg.filter_reasons, "hist_skip_no_fixture_id")
            continue
        if fid in seen_fixtures:
            if dbg:
                dbg.inc(dbg.filter_reasons, "hist_skip_duplicate_fixture")
            continue
        seen_fixtures.add(int(fid))
 
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
 
        ts = _fixture_timestamp(m)
 
        # Define GF/GA do time no recorte home/away
        if context == "home":  # team_id é o mandante
            gf = gh
            ga_ = ga
            opp_id = aid
        else:  # team_id é o visitante
            gf = ga
            ga_ = gh
            opp_id = hid
 
        hist.gf.append(int(gf))
        hist.ga.append(int(ga_))
        hist.fixture_ids.append(int(fid))
        hist.fixture_ts.append(int(ts))
        hist.n_games += 1
 
        # Stats opcionais (corners/sog/cards)
        stats = get_fixture_stats(api_key, int(fid), dbg=dbg)
        if stats:
            me = stats.get(int(team_id)) or {}
            opp = stats.get(int(opp_id)) or {}
 
            # Corners (for/against) — regra dura: exige ambos presentes
            c_for = me.get("corners")
            c_against = opp.get("corners")
            if c_for is not None and c_against is not None:
                hist.corners_for.append(int(c_for))
                hist.corners_against.append(int(c_against))
            else:
                if dbg:
                    dbg.inc(dbg.stats_missing, "corners_missing")
 
            # Shots on goal (for/against) — regra dura: exige ambos presentes
            sog_for = me.get("sog")
            sog_against = opp.get("sog")
            if sog_for is not None and sog_against is not None:
                hist.sog_for.append(int(sog_for))
                hist.sog_against.append(int(sog_against))
            else:
                if dbg:
                    dbg.inc(dbg.stats_missing, "sog_missing")
 
            # Cards (yellow + red) (for/against)
            # Regra dura: exige ambos amarelos presentes (vermelhos podem vir None e serão tratados como 0)
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
 
 
P_MIN = float(os.getenv("P_MIN", "0.55"))  # mantido por compatibilidade/segurança
 
 
def clamp_prob(p: Optional[float]) -> Optional[float]:
    if p is None:
        return None
    try:
        p = float(p)
    except Exception:
        return None
    return max(0.0, min(1.0, p))
 
 
def _ewma_weights(n: int, halflife: float) -> List[float]:
    if n <= 0:
        return []
    hl = max(0.5, float(halflife))
    lam = math.log(2.0) / hl  # i=0 mais recente
    return [math.exp(-lam * i) for i in range(n)]
 
 
def _ewma_weights_by_time(ts: List[int], halflife_days: float, fallback_halflife_games: float) -> List[float]:
    """
    Ponderação por recência usando DIFERENÇA REAL DE TEMPO (dias), para lidar com intervalos irregulares entre jogos.
    - ts deve estar no mesmo alinhamento do array alvo (mesma ordem), com ts[0] sendo o mais recente.
    - Se timestamps forem ausentes/zerados, faz fallback para pesos por índice (EWMA por jogos).
    """
    if not ts:
        return []
    if halflife_days <= 0:
        # desabilitado -> fallback
        return _ewma_weights(len(ts), fallback_halflife_games)
 
    t0 = ts[0] or 0
    if t0 <= 0:
        return _ewma_weights(len(ts), fallback_halflife_games)
 
    # Verifica se há muitos zeros; se sim, fallback
    if sum(1 for t in ts if not t) > (len(ts) // 2):
        return _ewma_weights(len(ts), fallback_halflife_games)
 
    hl = max(0.5, float(halflife_days))
    lam = math.log(2.0) / hl
 
    out: List[float] = []
    for t in ts:
        ti = int(t or 0)
        if ti <= 0:
            # se um item específico está sem timestamp, usa aproximação por índice dele
            out.append(0.0)
            continue
        delta_days = max(0.0, (t0 - ti) / 86400.0)
        out.append(math.exp(-lam * delta_days))
 
    # Se muitos pesos zerados (por timestamps faltantes), fallback
    if sum(1 for w in out if w > 0) < max(1, len(out) // 2):
        return _ewma_weights(len(ts), fallback_halflife_games)
 
    return out
 
 
def _n_eff(weights: List[float]) -> float:
    if not weights:
        return 0.0
    s1 = sum(weights)
    s2 = sum(w * w for w in weights)
    if s2 <= 0:
        return 0.0
    return (s1 * s1) / s2
 
 
def weighted_mean(arr: List[int], halflife: float) -> Optional[float]:
    if not arr:
        return None
    w = _ewma_weights(len(arr), halflife)
    sw = sum(w)
    if sw <= 0:
        return None
    return sum(w[i] * float(arr[i]) for i in range(len(arr))) / sw

def weighted_beta_rate(
    arr: List[int],
    pred_fn,
    halflife: float,
    ts: Optional[List[int]] = None,
    halflife_days: Optional[float] = None,
) -> Optional[float]:
    """Retorna E[p | Beta(prior) + evidência ponderada], onde evidência usa pesos EWMA.
 
    Ajuste alinhado:
      - Se `ts` (timestamps) for fornecido e `halflife_days` > 0, pondera por DIFERENÇA REAL DE TEMPO (dias),
        para lidar com intervalos irregulares entre jogos.
      - Caso contrário, mantém o comportamento atual (ponderação por índice / número de jogos).
    """
    if not arr:
        return None
 
    if ts is not None and isinstance(ts, list) and len(ts) >= len(arr):
        use_ts = ts[: len(arr)]
    else:
        use_ts = None
 
    hld = float(halflife_days) if halflife_days is not None else 0.0
    if use_ts and hld > 0.0:
        w = _ewma_weights_by_time(use_ts, hld, halflife)
    else:
        w = _ewma_weights(len(arr), halflife)
 
    tot_w = sum(w)
    if tot_w <= 0:
        return None
 
    succ = 0.0
    for i, x in enumerate(arr):
        succ += w[i] * (1.0 if pred_fn(x) else 0.0)
 
    # suavização bayesiana
    a = BETA_PRIOR_ALPHA
    b = BETA_PRIOR_BETA
    return (a + succ) / (a + b + tot_w)
 
 
def _dirichlet_outcome_probs(
    gf: List[int],
    ga: List[int],
    halflife: float,
    alpha0: float = 1.0,
    ts: Optional[List[int]] = None,
    halflife_days: Optional[float] = None,
) -> Optional[Dict[str, float]]:
    """Probabilidades de H/D/A a partir de resultados (gf vs ga) com pesos EWMA, suavização Dirichlet(alpha0).
 
    Ajuste alinhado:
      - Se `ts` (timestamps) for fornecido e `halflife_days` > 0, pondera por DIFERENÇA REAL DE TEMPO (dias),
        para lidar com intervalos irregulares entre jogos.
      - Caso contrário, mantém o comportamento atual (ponderação por índice / número de jogos).
    """
    if not gf or not ga:
        return None
 
    n = min(len(gf), len(ga))
    if n <= 0:
        return None
 
    if ts is not None and isinstance(ts, list) and len(ts) >= n:
        use_ts = ts[:n]
    else:
        use_ts = None
 
    hld = float(halflife_days) if halflife_days is not None else 0.0
    if use_ts and hld > 0.0:
        w = _ewma_weights_by_time(use_ts, hld, halflife)
    else:
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
 
    # posterior mean
    aH = alpha0 + cH
    aD = alpha0 + cD
    aA = alpha0 + cA
    s = aH + aD + aA
    return {"H": aH / s, "D": aD / s, "A": aA / s}
 
 
def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) para X~Poisson(lam). k inteiro >= -1."""
    lam = max(0.0001, float(lam))
    if k < 0:
        return 0.0
    # soma iterativa estável
    term = math.exp(-lam)
    s = term
    for i in range(1, k + 1):
        term *= lam / float(i)
        s += term
    return max(0.0, min(1.0, s))
 
 
def odds_from_prob(p: float) -> float:
    """Converte probabilidade teórica em odd teórica (sem margem), depois aplica desconto conservador (7,5%) na odd."""
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
    meta: Dict[str, Any],
) -> Optional[dict]:
    p = clamp_prob(p)
    if p is None or p <= 0.0 or p >= 1.0:
        return None
 
    odd_model = odds_from_prob(p)
    if not math.isfinite(odd_model):
        return None
 
    # aplica range depois do desconto (como definido)
    if not _in_range_after_discount(odd_model):
        return None
 
    if p < P_MIN:
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
    }
    return c
 
 
def _need_min_samples(*lists: List[int], n: int = HIST_MIN_GAMES) -> bool:
    return all(isinstance(lst, list) and len(lst) >= n for lst in lists)
 
 
def _weighted_under(arr: List[int], K: int, ts: Optional[List[int]] = None, halflife_days: Optional[float] = None) -> Optional[float]:
    return weighted_beta_rate(arr, lambda x: x < K, EWMA_HALFLIFE_GAMES, ts=ts, halflife_days=halflife_days)
 
 
def _weighted_over(arr: List[int], K: int, ts: Optional[List[int]] = None, halflife_days: Optional[float] = None) -> Optional[float]:
    return weighted_beta_rate(arr, lambda x: x > K, EWMA_HALFLIFE_GAMES, ts=ts, halflife_days=halflife_days)
 
 
def _combo_prob(p1: Optional[float], p2: Optional[float]) -> Optional[float]:
    if p1 is None or p2 is None:
        return None
    return clamp_prob((p1 + p2) / 2.0)
 
 
def _constraints_apply(state: dict, leg_meta: dict) -> bool:
    """
    Verifica se adicionar leg_meta mantém coerência para um fixture.
    state é um dict com:
      - allowed_results: set {"H","D","A"}
      - bounds: {metric: [min,max]} (min/max podem ser None)
      - btts: None/"yes"/"no"
    """
    # copia leve
    allowed = set(state.get("allowed_results", {"H", "D", "A"}))
    bounds = {k: [v[0], v[1]] for k, v in (state.get("bounds") or {}).items()}
    btts = state.get("btts")
 
    def upd_bound(metric: str, mn: Optional[int], mx: Optional[int]) -> bool:
        cur = bounds.get(metric, [None, None])
        cmin, cmax = cur[0], cur[1]
        if mn is not None:
            cmin = mn if cmin is None else max(cmin, mn)
        if mx is not None:
            cmax = mx if cmax is None else min(cmax, mx)
        if cmin is not None and cmax is not None and cmin > cmax:
            return False
        bounds[metric] = [cmin, cmax]
        return True
 
    # resultado (H/D/A)
    res_allowed = leg_meta.get("result_allowed")
    if isinstance(res_allowed, set) and res_allowed:
        allowed &= set(res_allowed)
    if not allowed:
        return False
 
    # BTTS
    b = leg_meta.get("btts")
    if b in ("yes", "no"):
        if btts is not None and btts != b:
            return False
        # se já temos bounds que implicam ambos >=1 e o cara pede BTTS no -> incoerente
        if b == "no":
            gh = bounds.get("goals_home", [None, None])[0]
            ga = bounds.get("goals_away", [None, None])[0]
            if gh is not None and gh >= 1 and ga is not None and ga >= 1:
                return False
        if b == "yes":
            # impõe gols >=1 para ambos
            if not upd_bound("goals_home", 1, None):
                return False
            if not upd_bound("goals_away", 1, None):
                return False
        btts = b
 
    # bounds simples (x < K) ou (x > K)
    metric = leg_meta.get("metric")
    op = leg_meta.get("op")
    K = leg_meta.get("K")
    if metric and op in ("lt", "gt") and isinstance(K, int):
        if op == "lt":
            # x < K => x <= K-1
            if not upd_bound(metric, None, K - 1):
                return False
        else:
            # x > K => x >= K+1
            if not upd_bound(metric, K + 1, None):
                return False
 
    # checagens cruzadas básicas (total vs time)
    def cross_check(prefix: str) -> bool:
        tot = bounds.get(f"{prefix}_total", [None, None])
        h = bounds.get(f"{prefix}_home", [None, None])
        a = bounds.get(f"{prefix}_away", [None, None])
        tot_min, tot_max = tot[0], tot[1]
        h_min, h_max = h[0], h[1]
        a_min, a_max = a[0], a[1]
 
        if tot_max is not None:
            if h_min is not None and tot_max < h_min:
                return False
            if a_min is not None and tot_max < a_min:
                return False
            if h_min is not None and a_min is not None and tot_max < (h_min + a_min):
                return False
 
        if tot_min is not None and h_max is not None and a_max is not None:
            if tot_min > (h_max + a_max):
                return False
        return True
 
    for pref in ("goals", "corners", "sog", "cards"):
        if not cross_check(pref):
            return False
 
    # se BTTS=no, impedir que bounds já obriguem ambos >=1
    if btts == "no":
        gh = bounds.get("goals_home", [None, None])[0]
        ga = bounds.get("goals_away", [None, None])[0]
        if gh is not None and gh >= 1 and ga is not None and ga >= 1:
            return False
 
    # se chegou aqui, é coerente; atualiza state
    state["allowed_results"] = allowed
    state["bounds"] = bounds
    state["btts"] = btts
    return True
 
 
def _fresh_constraint_state() -> dict:
    return {"allowed_results": {"H", "D", "A"}, "bounds": {}, "btts": None}

def filter_candidates(cands: List[dict], dbg: Optional[DebugCollector] = None) -> List[dict]:
    # aqui as regras principais já foram aplicadas no _mk_candidate (range/p_min/odd_discount).
    # mantemos por compatibilidade e para logar rejects caso algo passe.
    out: List[dict] = []
    for c in cands:
        odd = float(c.get("odd_book", 0.0))
        p = float(c.get("p", 0.0))
        if odd < MIN_ODD or odd > MAX_ODD:
            if dbg:
                dbg.inc(dbg.candidate_reject_reasons, "odd_out_of_range")
                dbg.add_reject_sample(f"[ODD] {c.get('label')} odd={odd:.2f}")
            continue
        if p < P_MIN:
            if dbg:
                dbg.inc(dbg.candidate_reject_reasons, "p_too_low")
                dbg.add_reject_sample(f"[P] {c.get('label')} p={p:.3f}")
            continue
        if int(c.get("samples", 0)) < HIST_MIN_GAMES:
            if dbg:
                dbg.inc(dbg.candidate_reject_reasons, "samples_too_low")
                dbg.add_reject_sample(f"[SAMPLES] {c.get('label')} n={c.get('samples')}")
            continue
        out.append(c)
    return out
 
 
def weighted_mean_by_time(arr: List[int], ts: List[int], halflife_days: float, fallback_halflife_games: float) -> Optional[float]:
    """
    Média ponderada por recência usando DIFERENÇA REAL DE TEMPO (dias), para lidar com intervalos irregulares entre jogos.
    - Espera arrays em ordem do mais recente -> mais antigo (como construído no histórico).
    - Se timestamps estiverem ausentes/zerados ou halflife_days <= 0, usa fallback por índice (EWMA por jogos).
    """
    if not arr:
        return None
    n = len(arr)
    use_ts = ts[:n] if isinstance(ts, list) and len(ts) >= n else []
    if not use_ts or float(halflife_days) <= 0.0:
        return weighted_mean(arr, fallback_halflife_games)
 
    w = _ewma_weights_by_time(use_ts, float(halflife_days), float(fallback_halflife_games))
    sw = sum(w)
    if sw <= 0:
        return weighted_mean(arr, fallback_halflife_games)
 
    return sum(w[i] * float(arr[i]) for i in range(n)) / sw
 
 
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
 
    # histórico: mandante em casa / visitante fora
    home_hist = build_team_history(api_key, int(hid), "home", dbg=dbg)
    away_hist = build_team_history(api_key, int(aid), "away", dbg=dbg)
 
    # Regra dura (ajuste alinhado):
    # ambos os times precisam ter pelo menos HIST_MIN_GAMES amostras em cada um dos 5 mercados
    # no recorte home/away dos últimos HIST_MAX_GAMES (20) jogos.
    if (not home_hist.has_min_samples_all_5_markets(HIST_MIN_GAMES)) or (not away_hist.has_min_samples_all_5_markets(HIST_MIN_GAMES)):
        if dbg:
            dbg.inc(dbg.filter_reasons, "day_skip_hist_below_min_all_5_markets")
        return []
 
    # Config local (ajuste alinhado): ponderação por tempo real (dias) opcional
    EWMA_HALFLIFE_DAYS = float(os.getenv("EWMA_HALFLIFE_DAYS", "0.0"))
 
    # ====== outcome (vitória simples / dupla chance) ======
    probs_home = _dirichlet_outcome_probs(
        home_hist.gf,
        home_hist.ga,
        EWMA_HALFLIFE_GAMES,
        alpha0=1.0,
        ts=home_hist.fixture_ts,
        halflife_days=EWMA_HALFLIFE_DAYS,
    )
    probs_away = _dirichlet_outcome_probs(
        away_hist.gf,
        away_hist.ga,
        EWMA_HALFLIFE_GAMES,
        alpha0=1.0,
        ts=away_hist.fixture_ts,
        halflife_days=EWMA_HALFLIFE_DAYS,
    )
 
    # para away_hist: "H" significa vitória do visitante (pois gf/ga estão do ponto de vista do visitante)
    # e "A" significa derrota do visitante.
    # mapeamentos úteis:
    # home side:
    #   home_win_rate = probs_home["H"]
    #   home_draw_rate = probs_home["D"]
    #   home_loss_rate = probs_home["A"]
    # away side (na ótica do visitante):
    #   away_win_rate = probs_away["H"]
    #   away_draw_rate = probs_away["D"]
    #   away_loss_rate = probs_away["A"]
 
    cands: List[dict] = []
 
    if probs_home and probs_away:
        n_out = min(len(home_hist.gf), len(away_hist.gf))
 
        p_home_win_raw = (probs_home["H"] + probs_away["A"]) / 2.0
        p_draw_raw = (probs_home["D"] + probs_away["D"]) / 2.0
        p_away_win_raw = (probs_away["H"] + probs_home["A"]) / 2.0
        s = p_home_win_raw + p_draw_raw + p_away_win_raw
        if s > 0:
            p_home_win = p_home_win_raw / s
            p_draw = p_draw_raw / s
            p_away_win = p_away_win_raw / s
 
            # (DESATIVADO por padrão - ajuste alinhado: apenas 5 mercados)
            ENABLE_MARKET_VITORIA_SECA = (
                os.getenv("ENABLE_MARKET_VITORIA_SECA", "0").strip().lower() in {"1", "true", "yes", "y"}
            )
            if ENABLE_MARKET_VITORIA_SECA:
                # Vitória simples (somente mandante/visitante, sem "empate")
                c = _mk_candidate(
                    key="res_home_win",
                    label="Vitória simples: Mandante vence",
                    p=p_home_win,
                    samples=n_out,
                    meta={"result_allowed": {"H"}},
                )
                if c:
                    cands.append(c)
                c = _mk_candidate(
                    key="res_away_win",
                    label="Vitória simples: Visitante vence",
                    p=p_away_win,
                    samples=n_out,
                    meta={"result_allowed": {"A"}},
                )
                if c:
                    cands.append(c)
 
            # Dupla chance (ajuste alinhado: mercado permitido)
            c = _mk_candidate(
                key="dc_1x",
                label="Chance dupla: Mandante ou empate (1X)",
                p=(p_home_win + p_draw),
                samples=n_out,
                meta={"result_allowed": {"H", "D"}},
            )
            if c:
                cands.append(c)
            c = _mk_candidate(
                key="dc_x2",
                label="Chance dupla: Visitante ou empate (X2)",
                p=(p_away_win + p_draw),
                samples=n_out,
                meta={"result_allowed": {"A", "D"}},
            )
            if c:
                cands.append(c)
            c = _mk_candidate(
                key="dc_12",
                label="Chance dupla: Mandante ou visitante (12)",
                p=(p_home_win + p_away_win),
                samples=n_out,
                meta={"result_allowed": {"H", "A"}},
            )
            if c:
                cands.append(c)
 
    # ====== gols (Poisson via matchup) ======
    if _need_min_samples(home_hist.gf, away_hist.ga, away_hist.gf, home_hist.ga):
        mu_h_gf = weighted_mean_by_time(home_hist.gf, home_hist.fixture_ts, EWMA_HALFLIFE_DAYS, EWMA_HALFLIFE_GAMES)
        mu_a_ga = weighted_mean_by_time(away_hist.ga, away_hist.fixture_ts, EWMA_HALFLIFE_DAYS, EWMA_HALFLIFE_GAMES)
        mu_a_gf = weighted_mean_by_time(away_hist.gf, away_hist.fixture_ts, EWMA_HALFLIFE_DAYS, EWMA_HALFLIFE_GAMES)
        mu_h_ga = weighted_mean_by_time(home_hist.ga, home_hist.fixture_ts, EWMA_HALFLIFE_DAYS, EWMA_HALFLIFE_GAMES)
 
        if None not in (mu_h_gf, mu_a_ga, mu_a_gf, mu_h_ga):
            lam_home = max(0.01, (mu_h_gf + mu_a_ga) / 2.0)
            lam_away = max(0.01, (mu_a_gf + mu_h_ga) / 2.0)
            lam_total = lam_home + lam_away
            n_g = min(len(home_hist.gf), len(away_hist.ga), len(away_hist.gf), len(home_hist.ga))
 
            # (DESATIVADO por padrão - ajuste alinhado: apenas 5 mercados)
            ENABLE_MARKET_BTTS = (
                os.getenv("ENABLE_MARKET_BTTS", "0").strip().lower() in {"1", "true", "yes", "y"}
            )
            if ENABLE_MARKET_BTTS:
                # BTTS (sim/não) — consistente e rápido
                p_h_score = 1.0 - math.exp(-lam_home)
                p_a_score = 1.0 - math.exp(-lam_away)
                p_btts_yes = clamp_prob(p_h_score * p_a_score)
                if p_btts_yes is not None:
                    c = _mk_candidate(
                        key="btts_yes",
                        label="Ambas marcam: SIM",
                        p=p_btts_yes,
                        samples=n_g,
                        meta={"btts": "yes"},
                    )
                    if c:
                        cands.append(c)
                    c = _mk_candidate(
                        key="btts_no",
                        label="Ambas marcam: NÃO",
                        p=(1.0 - p_btts_yes),
                        samples=n_g,
                        meta={"btts": "no"},
                    )
                    if c:
                        cands.append(c)
 
            # Totais do jogo (inteiros): "Menos de K" => total < K ; "Mais de K" => total > K
            for K in (2, 3, 4, 5, 6):
                p_under = clamp_prob(poisson_cdf(K - 1, lam_total))
                if p_under is not None:
                    c = _mk_candidate(
                        key=f"g_total_u_{K}",
                        label=f"Menos de {K} gols (jogo)",
                        p=p_under,
                        samples=n_g,
                        meta={"metric": "goals_total", "op": "lt", "K": int(K)},
                    )
                    if c:
                        cands.append(c)
 
            for K in (1, 2, 3, 4):  # total > K => >= K+1 => 1 - P(total <= K)
                p_over = clamp_prob(1.0 - poisson_cdf(K, lam_total))
                if p_over is not None:
                    c = _mk_candidate(
                        key=f"g_total_o_{K}",
                        label=f"Mais de {K} gols (jogo)",
                        p=p_over,
                        samples=n_g,
                        meta={"metric": "goals_total", "op": "gt", "K": int(K)},
                    )
                    if c:
                        cands.append(c)
 
            # Gols por time (inteiros)
            for K in (1, 2, 3, 4):
                p_under_h = clamp_prob(poisson_cdf(K - 1, lam_home))
                if p_under_h is not None:
                    c = _mk_candidate(
                        key=f"g_home_u_{K}",
                        label=f"Menos de {K} gols (mandante)",
                        p=p_under_h,
                        samples=n_g,
                        meta={"metric": "goals_home", "op": "lt", "K": int(K)},
                    )
                    if c:
                        cands.append(c)
 
                p_under_a = clamp_prob(poisson_cdf(K - 1, lam_away))
                if p_under_a is not None:
                    c = _mk_candidate(
                        key=f"g_away_u_{K}",
                        label=f"Menos de {K} gols (visitante)",
                        p=p_under_a,
                        samples=n_g,
                        meta={"metric": "goals_away", "op": "lt", "K": int(K)},
                    )
                    if c:
                        cands.append(c)
 
            for K in (0, 1, 2, 3):
                p_over_h = clamp_prob(1.0 - poisson_cdf(K, lam_home))
                if p_over_h is not None:
                    c = _mk_candidate(
                        key=f"g_home_o_{K}",
                        label=f"Mais de {K} gols (mandante)",
                        p=p_over_h,
                        samples=n_g,
                        meta={"metric": "goals_home", "op": "gt", "K": int(K)},
                    )
                    if c:
                        cands.append(c)
 
                p_over_a = clamp_prob(1.0 - poisson_cdf(K, lam_away))
                if p_over_a is not None:
                    c = _mk_candidate(
                        key=f"g_away_o_{K}",
                        label=f"Mais de {K} gols (visitante)",
                        p=p_over_a,
                        samples=n_g,
                        meta={"metric": "goals_away", "op": "gt", "K": int(K)},
                    )
                    if c:
                        cands.append(c)
 
    # ====== escanteios / SOG / cartões (empírico ponderado por recência + matchup) ======
    def add_stat_markets(
        stat_name: str,
        home_for: List[int],
        home_against: List[int],
        away_for: List[int],
        away_against: List[int],
        K_total: Tuple[int, ...],
        K_team: Tuple[int, ...],
    ) -> None:
        # total do jogo
        if _need_min_samples(home_for, home_against, away_for, away_against):
            home_tot = [home_for[i] + home_against[i] for i in range(min(len(home_for), len(home_against)))]
            away_tot = [away_for[i] + away_against[i] for i in range(min(len(away_for), len(away_against)))]
            n_tot = min(len(home_tot), len(away_tot))
 
            for K in K_total:
                p_u = _combo_prob(_weighted_under(home_tot, K), _weighted_under(away_tot, K))
                if p_u is not None:
                    c = _mk_candidate(
                        key=f"{stat_name}_total_u_{K}",
                        label=f"Menos de {K} {stat_name} (jogo)",
                        p=p_u,
                        samples=n_tot,
                        meta={"metric": f"{stat_name}_total", "op": "lt", "K": int(K)},
                    )
                    if c:
                        cands.append(c)
 
                p_o = _combo_prob(_weighted_over(home_tot, K), _weighted_over(away_tot, K))
                if p_o is not None:
                    c = _mk_candidate(
                        key=f"{stat_name}_total_o_{K}",
                        label=f"Mais de {K} {stat_name} (jogo)",
                        p=p_o,
                        samples=n_tot,
                        meta={"metric": f"{stat_name}_total", "op": "gt", "K": int(K)},
                    )
                    if c:
                        cands.append(c)
 
        # por time (mandante)
        if _need_min_samples(home_for, away_against):
            n_h = min(len(home_for), len(away_against))
            for K in K_team:
                p_u = _combo_prob(_weighted_under(home_for, K), _weighted_under(away_against, K))
                if p_u is not None:
                    c = _mk_candidate(
                        key=f"{stat_name}_home_u_{K}",
                        label=f"Menos de {K} {stat_name} (mandante)",
                        p=p_u,
                        samples=n_h,
                        meta={"metric": f"{stat_name}_home", "op": "lt", "K": int(K)},
                    )
                    if c:
                        cands.append(c)
 
                p_o = _combo_prob(_weighted_over(home_for, K), _weighted_over(away_against, K))
                if p_o is not None:
                    c = _mk_candidate(
                        key=f"{stat_name}_home_o_{K}",
                        label=f"Mais de {K} {stat_name} (mandante)",
                        p=p_o,
                        samples=n_h,
                        meta={"metric": f"{stat_name}_home", "op": "gt", "K": int(K)},
                    )
                    if c:
                        cands.append(c)
 
        # por time (visitante)
        if _need_min_samples(away_for, home_against):
            n_a = min(len(away_for), len(home_against))
            for K in K_team:
                p_u = _combo_prob(_weighted_under(away_for, K), _weighted_under(home_against, K))
                if p_u is not None:
                    c = _mk_candidate(
                        key=f"{stat_name}_away_u_{K}",
                        label=f"Menos de {K} {stat_name} (visitante)",
                        p=p_u,
                        samples=n_a,
                        meta={"metric": f"{stat_name}_away", "op": "lt", "K": int(K)},
                    )
                    if c:
                        cands.append(c)
 
                p_o = _combo_prob(_weighted_over(away_for, K), _weighted_over(home_against, K))
                if p_o is not None:
                    c = _mk_candidate(
                        key=f"{stat_name}_away_o_{K}",
                        label=f"Mais de {K} {stat_name} (visitante)",
                        p=p_o,
                        samples=n_a,
                        meta={"metric": f"{stat_name}_away", "op": "gt", "K": int(K)},
                    )
                    if c:
                        cands.append(c)
 
    # Escanteios
    add_stat_markets(
        stat_name="escanteios",
        home_for=home_hist.corners_for,
        home_against=home_hist.corners_against,
        away_for=away_hist.corners_for,
        away_against=away_hist.corners_against,
        K_total=(8, 9, 10, 11, 12, 13, 14, 15),
        K_team=(3, 4, 5, 6, 7, 8, 9, 10),
    )
 
    # Chutes a gol (SOG)
    add_stat_markets(
        stat_name="chutes a gol",
        home_for=home_hist.sog_for,
        home_against=home_hist.sog_against,
        away_for=away_hist.sog_for,
        away_against=away_hist.sog_against,
        K_total=(5, 6, 7, 8, 9, 10, 11, 12),
        K_team=(1, 2, 3, 4, 5, 6, 7, 8),
    )
 
    # Cartões
    add_stat_markets(
        stat_name="cartões",
        home_for=home_hist.cards_for,
        home_against=home_hist.cards_against,
        away_for=away_hist.cards_for,
        away_against=away_hist.cards_against,
        K_total=(3, 4, 5, 6, 7, 8, 9, 10),
        K_team=(1, 2, 3, 4, 5, 6, 7),
    )
 
    # metadata do fixture
    kickoff = to_int(fixture.get("timestamp")) or 0
    lname = str(league.get("name") or "")
    hname = str(home.get("name") or "")
    aname = str(away.get("name") or "")
 
    # aplica filtros finais e injeta ids
    cands = filter_candidates(cands, dbg=dbg)
    for c in cands:
        c["fixture_id"] = int(fid)
        c["kickoff_ts"] = int(kickoff)
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
 
 
def format_combo(legs: List[dict], odd_prod: float) -> str:
    """
    Ajuste alinhado:
      - Agora cada "combo" é, na prática, um BILHETE de 1 jogo com 5 mercados fixos.
      - Este formatter retorna um bloco amigável para ser usado na mensagem final do Telegram.
      - A numeração "Bilhete 1/2/..." será feita no BLOCO de montagem da mensagem.
    """
    if not legs:
        return ""
 
    # Como o bilhete é 1 jogo, os metadados podem ser lidos do primeiro item
    first = legs[0]
    h = str(first.get("home") or "")
    a = str(first.get("away") or "")
    league = str(first.get("league") or "")
    kickoff = int(first.get("kickoff_ts") or 0)
 
    # Ordena para sempre imprimir na ordem dos 5 mercados
    order = {"dupla_chance": 0, "gols": 1, "escanteios": 2, "cartoes": 3, "sog": 4}
    legs_sorted = sorted(legs, key=lambda x: order.get(str(x.get("type") or ""), 99))
 
    out: List[str] = []
    out.append(f"{h} x {a} — {_fmt_hhmm_from_ts(kickoff)} (Brasília)")
    out.append(f"{league}")
 
    for c in legs_sorted:
        ctype = str(c.get("type") or "")
        label = str(c.get("label") or "").strip()
        odd = float(c.get("odd_book", 0.0))
 
        if ctype == "dupla_chance":
            out.append(f"• Chance dupla: {label} (odd≈{odd:.2f})")
        elif ctype == "gols":
            out.append(f"• Total de gols: {label} (odd≈{odd:.2f})")
        elif ctype == "escanteios":
            out.append(f"• Escanteios: {label} (odd≈{odd:.2f})")
        elif ctype == "cartoes":
            out.append(f"• Cartões: {label} (odd≈{odd:.2f})")
        elif ctype == "sog":
            out.append(f"• Chutes ao gol: {label} (odd≈{odd:.2f})")
        else:
            out.append(f"• {label} (odd≈{odd:.2f})")
 
    out.append(f"Odd estimada do bilhete ≈ {odd_prod:.2f}")
    return "\n".join(out)
 
 
def build_all_candidates_for_day(
    api_key: str,
    day_fixtures: List[dict],
    dbg: Optional[DebugCollector] = None
) -> List[dict]:
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
 
 
def _candidate_sort_key(c: dict) -> Tuple[int, float, float]:
    # prioridade: mais amostras -> maior p -> menor odd
    return (
        int(c.get("samples", 0)),
        float(c.get("p", 0.0)),
        -float(c.get("odd_book", 0.0)),
    )
 
 
def _market_prefers_total(c: dict) -> int:
    """
    Tie-break alinhado:
      - Em empate de proximidade/odd dentro do mesmo mercado, preferir a seleção referente ao JOGO (total).
      - Detecta via label "(jogo)" ou meta.metric com sufixo "_total" ou prefixo "goals_total"/"corners_total"/etc.
    """
    label = str(c.get("label") or "").lower()
    meta = c.get("meta") or {}
    metric = str(meta.get("metric") or "").lower()
 
    if "(jogo)" in label:
        return 1
    if metric.endswith("_total"):
        return 1
    if metric in {"goals_total", "corners_total", "cards_total", "sog_total"}:
        return 1
    return 0
 
 
def _market_score(c: dict, target_market_odd: float) -> Tuple[float, int, int, float]:
    """
    Score para ranquear legs dentro de um mesmo mercado:
      1) proximidade da odd ao alvo (default 1.25)
      2) preferir "jogo" (total) em empate
      3) mais amostras
      4) maior probabilidade
    """
    odd = float(c.get("odd_book", 0.0))
    diff = abs(math.log(max(0.0001, odd)) - math.log(max(0.0001, target_market_odd)))
    prefer_total = _market_prefers_total(c)
    samples = int(c.get("samples", 0))
    p = float(c.get("p", 0.0))
    return (diff, -prefer_total, -samples, -p)
 
 
def _best_combo_from_single_fixture(
    fixture_cands: List[dict],
    target_odd: float,
    min_legs: int,
    max_legs: int,
    banned_leg_ids: Set[str],
    fixture_state: dict,
) -> Optional[Tuple[List[dict], float]]:
    """
    Ajuste alinhado:
      - Agora montamos um "bilhete" por fixture, com EXATAMENTE 5 mercados:
          dupla chance, gols, escanteios, cartões, chutes ao gol.
      - Para cada mercado escolhemos a leg com odd mais próxima de 1.25 (já descontada),
        com tie-break preferindo "(jogo)" quando aplicável.
      - Mantém coerência interna via constraints.
      - Retorna (legs, odd_prod) com 5 legs (ou None se não for possível).
    """
    REQUIRED_TYPES = ("dupla_chance", "gols", "escanteios", "cartoes", "sog")
    target_market_odd = float(os.getenv("TARGET_MARKET_ODD", "1.25"))
 
    cand = [
        c for c in fixture_cands
        if c.get("leg_id") not in banned_leg_ids
        and str(c.get("type") or "") in REQUIRED_TYPES
    ]
    if not cand:
        return None
 
    by_type: Dict[str, List[dict]] = {t: [] for t in REQUIRED_TYPES}
    for c in cand:
        by_type[str(c.get("type") or "")].append(c)
 
    # precisa ter pelo menos 1 candidato em cada mercado
    for t in REQUIRED_TYPES:
        if not by_type.get(t):
            return None
 
    # ordena candidatos por mercado por proximidade de 1.25 (com tie-breaks)
    for t in REQUIRED_TYPES:
        by_type[t].sort(key=lambda x: _market_score(x, target_market_odd))
 
    # limita busca para não explodir (top-N por mercado)
    TOPK = int(os.getenv("TOPK_PER_MARKET", "12"))
    pools: Dict[str, List[dict]] = {t: by_type[t][:TOPK] for t in REQUIRED_TYPES}
 
    best: Optional[Tuple[List[dict], float, float]] = None  # (legs, prod, score)
    log_target_ticket = math.log(max(0.0001, float(target_odd)))
    log_target_market = math.log(max(0.0001, float(target_market_odd)))
 
    # brute-force pequeno: 12^5 ~ 248k no pior caso, ainda ok; pode ajustar TOPK se necessário
    for dc in pools["dupla_chance"]:
        st0 = {
            "allowed_results": set(fixture_state["allowed_results"]),
            "bounds": {k: [v[0], v[1]] for k, v in fixture_state["bounds"].items()},
            "btts": fixture_state["btts"],
        }
        if not _constraints_apply(st0, dc.get("meta") or {}):
            continue
 
        for g in pools["gols"]:
            st1 = {
                "allowed_results": set(st0["allowed_results"]),
                "bounds": {k: [v[0], v[1]] for k, v in st0["bounds"].items()},
                "btts": st0["btts"],
            }
            if not _constraints_apply(st1, g.get("meta") or {}):
                continue
 
            for cor in pools["escanteios"]:
                st2 = {
                    "allowed_results": set(st1["allowed_results"]),
                    "bounds": {k: [v[0], v[1]] for k, v in st1["bounds"].items()},
                    "btts": st1["btts"],
                }
                if not _constraints_apply(st2, cor.get("meta") or {}):
                    continue
 
                for car in pools["cartoes"]:
                    st3 = {
                        "allowed_results": set(st2["allowed_results"]),
                        "bounds": {k: [v[0], v[1]] for k, v in st2["bounds"].items()},
                        "btts": st2["btts"],
                    }
                    if not _constraints_apply(st3, car.get("meta") or {}):
                        continue
 
                    for sog in pools["sog"]:
                        st4 = {
                            "allowed_results": set(st3["allowed_results"]),
                            "bounds": {k: [v[0], v[1]] for k, v in st3["bounds"].items()},
                            "btts": st3["btts"],
                        }
                        if not _constraints_apply(st4, sog.get("meta") or {}):
                            continue
 
                        legs = [dc, g, cor, car, sog]
                        prod = 1.0
                        for c in legs:
                            prod *= float(c.get("odd_book", 1.0))
 
                        # score principal: cada mercado próximo de 1.25
                        score_market = 0.0
                        for c in legs:
                            o = float(c.get("odd_book", 0.0))
                            score_market += abs(math.log(max(0.0001, o)) - log_target_market)
 
                        # tie-break: bilhete próximo do target (≈3.0)
                        diff_ticket = math.log(max(0.0001, prod)) - log_target_ticket
                        score_ticket = abs(diff_ticket)
 
                        # tie-break adicional: preferir passar levemente do target (como no projeto atual)
                        score = score_market + (0.35 * score_ticket) + (1e-6 if diff_ticket < 0 else 0.0)
 
                        if best is None or score < best[2]:
                            best = (legs, prod, score)
 
    if best is not None:
        return (best[0], best[1])
 
    return None

def greedy_select_legs_for_target(
    all_cands: List[dict],
    target_odd: float,
    seed_leg: Optional[dict] = None,
    max_legs: int = 5,
    banned_leg_ids: Optional[Set[str]] = None,
) -> Tuple[List[dict], float]:
    """
    Fallback (ajuste alinhado): monta um BILHETE de 1 jogo com 5 mercados fixos, tentando:
      - 1 jogo apenas (fixture único)
      - coerência por fixture (constraints)
      - exatamente 1 perna por mercado (type)
      - tipos permitidos: dupla_chance, gols, escanteios, cartoes, sog
      - escolha por mercado já é feita dentro de _best_combo_from_single_fixture (odd ≈ 1.25 com tie-break pró "(jogo)")
    """
    banned_leg_ids = banned_leg_ids or set()
 
    REQUIRED_TYPES = ("dupla_chance", "gols", "escanteios", "cartoes", "sog")
 
    cands = [
        c
        for c in all_cands
        if c.get("leg_id") not in banned_leg_ids
        and str(c.get("type") or "") in REQUIRED_TYPES
    ]
 
    if not cands:
        return [], 1.0
 
    # Se existir seed_leg, força o fixture dele e tenta montar bilhete completo daquele jogo
    if seed_leg is not None:
        if seed_leg.get("leg_id") in banned_leg_ids:
            return [], 1.0
        if str(seed_leg.get("type") or "") not in REQUIRED_TYPES:
            return [], 1.0
 
        fixed_fid = int(seed_leg.get("fixture_id") or 0)
        if fixed_fid <= 0:
            return [], 1.0
 
        fixture_cands = [c for c in cands if int(c.get("fixture_id") or 0) == fixed_fid]
        if not fixture_cands:
            return [], 1.0
 
        st0 = _fresh_constraint_state()
        res = _best_combo_from_single_fixture(
            fixture_cands=fixture_cands,
            target_odd=target_odd,
            min_legs=5,
            max_legs=5,
            banned_leg_ids=banned_leg_ids,
            fixture_state=st0,
        )
        if res is None:
            return [], 1.0
        legs, prod = res
        return legs, prod
 
    # Caso sem seed: procura um fixture que permita montar bilhete completo (5 mercados), e retorna o mais próximo do target_odd
    by_fid: Dict[int, List[dict]] = {}
    for c in cands:
        fid = int(c.get("fixture_id") or 0)
        if fid <= 0:
            continue
        by_fid.setdefault(fid, []).append(c)
 
    best: Optional[Tuple[List[dict], float, float]] = None  # legs, prod, score
    log_target = math.log(max(0.0001, float(target_odd)))
 
    for fid, fixture_cands in by_fid.items():
        st0 = _fresh_constraint_state()
        res = _best_combo_from_single_fixture(
            fixture_cands=fixture_cands,
            target_odd=target_odd,
            min_legs=5,
            max_legs=5,
            banned_leg_ids=banned_leg_ids,
            fixture_state=st0,
        )
        if res is None:
            continue
        legs, prod = res
        diff = math.log(max(0.0001, float(prod))) - log_target
        score = abs(diff) + (1e-6 if diff < 0 else 0.0)  # empate: prefere passar do alvo
        if best is None or score < best[2]:
            best = (legs, float(prod), float(score))
 
    if best is None:
        return [], 1.0
 
    return best[0], best[1]

def build_target_combos(
    all_cands: List[dict],
    n_combos: int,
    target_odd: float,
    min_legs: int,
    max_legs: int,
    seed_pool: int,
    dbg: Optional[DebugCollector] = None,
) -> List[Tuple[List[dict], float]]:
    """
    Ajuste alinhado (novo fluxo):
      ✅ 1 combo = 1 jogo (fixture)
      ✅ cada combo/bilhete deve ter EXATAMENTE 5 mercados (types):
           - dupla_chance
           - gols
           - escanteios
           - cartoes
           - sog
      ✅ para cada mercado, escolhe a perna com odd mais próxima de 1.25 (tie-break pró "(jogo)"),
         e monta o bilhete (odd total tende a ~3)
      ✅ ranking de jogos: TOTALMENTE ALEATÓRIO (shuffle)
      ✅ não força completar 10 se não houver jogos aptos
 
    Observação importante:
      - A regra dura de amostras mínimas (10 em cada um dos 5 mercados para ambos os times no recorte home/away)
        já é aplicada no build_match_candidates (via has_min_samples_all_5_markets), então fixtures sem histórico
        mínimo não geram candidatos e naturalmente não entram aqui.
      - O controle fino de chamadas de API por ranking aleatório (buscar histórico apenas conforme necessário)
        é tratado no fluxo de seleção do dia (fora desta função), mas aqui mantemos a montagem final dos bilhetes
        a partir dos candidatos disponíveis.
    """
    if not all_cands or n_combos <= 0:
        return []
 
    # agrupa por fixture (1 aposta = 1 jogo)
    by_fixture: Dict[int, List[dict]] = {}
    for c in all_cands:
        fid = int(c.get("fixture_id") or 0)
        if fid <= 0:
            continue
        by_fixture.setdefault(fid, []).append(c)
 
    if not by_fixture:
        return []
 
    # ranking de jogos: totalmente aleatório
    import random
    fixture_ids = list(by_fixture.keys())
    random.shuffle(fixture_ids)
 
    out: List[Tuple[List[dict], float]] = []
    used_global: Set[str] = set()
    fixture_used_as_primary: Set[int] = set()
    fixture_states_global: Dict[int, dict] = {}  # mantido por compatibilidade
 
    for fid in fixture_ids:
        if len(out) >= n_combos:
            break
 
        # um jogo só pode aparecer em um combo
        if fid in fixture_used_as_primary:
            continue
 
        base_state = fixture_states_global.get(fid) or _fresh_constraint_state()
 
        best = _best_combo_from_single_fixture(
            fixture_cands=by_fixture[fid],
            target_odd=target_odd,
            min_legs=min_legs,
            max_legs=max_legs,
            banned_leg_ids=used_global if ENFORCE_GLOBAL_UNIQUE_LEGS else set(),
            fixture_state=base_state,
        )
        if best is None:
            continue
 
        legs, prod = best
 
        # valida e atualiza estados globais
        st = {
            "allowed_results": set(base_state["allowed_results"]),
            "bounds": {k: [v[0], v[1]] for k, v in base_state["bounds"].items()},
            "btts": base_state["btts"],
        }
 
        ok = True
        used_types_local: Set[str] = set()
        for l in legs:
            if ENFORCE_GLOBAL_UNIQUE_LEGS and l.get("leg_id") in used_global:
                ok = False
                break
 
            # 1 fixture por bilhete
            if int(l.get("fixture_id") or 0) != int(fid):
                ok = False
                break
 
            t = str(l.get("type") or "")
            if t in used_types_local:
                ok = False
                break
            used_types_local.add(t)
 
            if not _constraints_apply(st, l.get("meta") or {}):
                ok = False
                break
 
        # precisa ser exatamente 5 mercados
        REQUIRED_TYPES = {"dupla_chance", "gols", "escanteios", "cartoes", "sog"}
        if ok:
            if len(legs) != 5:
                ok = False
            else:
                tset = set(str(x.get("type") or "") for x in legs)
                if tset != REQUIRED_TYPES:
                    ok = False
 
        if not ok:
            continue
 
        out.append((legs, prod))
 
        if ENFORCE_GLOBAL_UNIQUE_LEGS:
            used_global.update(str(l["leg_id"]) for l in legs)
 
        fixture_states_global[fid] = st
        fixture_used_as_primary.add(fid)
 
        if dbg:
            dbg.inc(dbg.counts, f"target_combos_generated_{len(out)}")
 
    return out
#################################################################################################################################################################
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
    out: List[dict] = []
    for m in fx:
        fixture = m.get("fixture") or {}
        ts = to_int(fixture.get("timestamp")) or 0
        dtt = dt.datetime.fromtimestamp(ts, tz=TZ) if ts else None
 
        # só jogos que ainda não começaram (evita apostas em andamento)
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
    out: List[str] = []
    out.append(f"🌞 Bom dia! Separei os bilhetes de hoje ({target_date}) no horário de Brasília.")
    out.append("Boa sorte e jogue com responsabilidade. 🍀")
    out.append("")
 
    if not combos:
        out.append("⚠️ Hoje não encontrei jogos aptos dentro dos critérios (sem inventar dados).")
        out.append("Volto amanhã com novas oportunidades! ✅")
        out.append("")
        out.append("📌 Legenda (linhas inteiras)")
        out.append("• Chance dupla: 1X = mandante ou empate | X2 = visitante ou empate | 12 = mandante ou visitante.")
        out.append("• Mais de K: significa ≥ K+1 (ex.: Mais de 3 gols = 4+).")
        out.append("• Menos de K: significa ≤ K-1 (ex.: Menos de 8 escanteios = até 7).")
        out.append("• Gols / Escanteios / Cartões / Chutes ao gol: podem ser do jogo (total) ou por time (quando indicado).")
        return "\n".join(out).strip()
 
    def _fmt_market_line(prefix: str, c: dict) -> str:
        label = str(c.get("label") or "").strip()
 
        # evita duplicar prefixos no caso de chance dupla
        if prefix.lower().startswith("chance dupla"):
            if label.lower().startswith("chance dupla:"):
                label = label[len("chance dupla:") :].strip()
 
        odd = float(c.get("odd_book", 0.0))
        return f"{prefix}: {label} (odd≈{odd:.2f})"
 
    for i, (legs, prod) in enumerate(combos, start=1):
        if not legs:
            continue
 
        first = legs[0]
        h = str(first.get("home") or "")
        a = str(first.get("away") or "")
        league = str(first.get("league") or "")
        kickoff = int(first.get("kickoff_ts") or 0)
 
        # indexa legs por type para garantir saída nos 5 mercados
        by_type: Dict[str, dict] = {}
        for c in legs:
            by_type[str(c.get("type") or "")] = c
 
        out.append(f"🎫 Bilhete {i} — {h} x {a} — {_fmt_hhmm_from_ts(kickoff)} (Brasília)")
        if league:
            out.append(f"{league}")
 
        if "dupla_chance" in by_type:
            out.append(_fmt_market_line("Mercado chance dupla", by_type["dupla_chance"]))
        if "gols" in by_type:
            out.append(_fmt_market_line("Mercado total de gols", by_type["gols"]))
        if "escanteios" in by_type:
            out.append(_fmt_market_line("Mercado total de escanteios", by_type["escanteios"]))
        if "cartoes" in by_type:
            out.append(_fmt_market_line("Mercado total de cartões", by_type["cartoes"]))
        if "sog" in by_type:
            out.append(_fmt_market_line("Mercado chutes ao gol", by_type["sog"]))
 
        out.append(f"Odd estimada do bilhete ≈ {float(prod):.2f}")
        out.append("")
 
    out.append("📌 Legenda (linhas inteiras)")
    out.append("• Chance dupla: 1X = mandante ou empate | X2 = visitante ou empate | 12 = mandante ou visitante.")
    out.append("• Mais de K: significa ≥ K+1 (ex.: Mais de 3 gols = 4+).")
    out.append("• Menos de K: significa ≤ K-1 (ex.: Menos de 8 escanteios = até 7).")
    out.append("• Gols / Escanteios / Cartões / Chutes ao gol: podem ser do jogo (total) ou por time (quando indicado).")
    return "\n".join(out).strip()
 
 
def build_debug_report(dbg: DebugCollector) -> str:
    out: List[str] = []
    out.append("==== DEBUG REPORT ====")
    out.append(f"run_started_sp: {dbg.run_started_sp}")
    out.append(f"target_date: {dbg.target_date}")
    out.append(f"api_calls_budget: {dbg.api_calls_budget}")
    out.append(f"api_calls_used: {dbg.api_calls_used}")
    out.append(f"stats_calls_budget: {dbg.stats_calls_budget}")
    out.append(f"stats_calls_used: {dbg.stats_calls_used}")
    out.append("")
    out.append(f"MIN_ODD: {MIN_ODD} | MAX_ODD: {MAX_ODD}")
    out.append(f"ODD_DISCOUNT: {ODD_DISCOUNT}")
    out.append(f"EWMA_HALFLIFE_GAMES: {EWMA_HALFLIFE_GAMES}")
    out.append(f"HIST_MAX_GAMES: {HIST_MAX_GAMES} | HIST_MIN_GAMES: {HIST_MIN_GAMES}")
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
        out.append("-- match_details (ordem do ranking, decisões e legs escolhidas) --")
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
 
    # Agora o padrão é HOJE (offset configurável). TARGET_DATE ainda sobrescreve tudo.
    default_date = (now_sp + dt.timedelta(days=DEFAULT_TARGET_DATE_OFFSET_DAYS)).date().isoformat()
    target_date = (os.getenv("TARGET_DATE") or default_date).strip()
 
    dbg = DebugCollector()
    dbg.run_started_sp = now_sp.isoformat()
    dbg.target_date = target_date
    dbg.api_calls_budget = API_CALL_BUDGET
    dbg.stats_calls_budget = STATS_CALL_BUDGET
 
    # Busca jogos do dia (somente futuros), com filtros de liga/time
    day_fx = select_day_fixtures(api_key, target_date, dbg=dbg)
 
    # limite opcional (para evitar excesso em dias gigantes)
    max_day = int(os.getenv("MAX_DAY_FIXTURES", "60"))
    if max_day > 0:
        day_fx = day_fx[:max_day]
 
    # Ranking totalmente aleatório (ajuste alinhado) + log para debug
    import random
    seed_raw = (os.getenv("RANDOM_SEED") or "").strip()
    if seed_raw:
        try:
            seed_val = int(seed_raw)
        except Exception:
            seed_val = None
    else:
        seed_val = None
 
    rng = random.Random(seed_val)
    rng.shuffle(day_fx)
 
    if dbg:
        dbg.add_match_detail(f"[RANK] total_day_fixtures_after_filters={len(day_fx)} max_day={max_day}")
        dbg.add_match_detail(f"[RANK] random_seed={'none' if seed_val is None else seed_val}")
 
    combos: List[Tuple[List[dict], float]] = []
    used_fixture_ids: Set[int] = set()
    used_leg_ids: Set[str] = set()
 
    # Seleção incremental: percorre o ranking e vai montando até fechar N bilhetes (ou acabar)
    for idx, fx in enumerate(day_fx, start=1):
        if len(combos) >= N_TARGET_COMBOS:
            if dbg:
                dbg.add_match_detail(f"[STOP] reached_target_combos={N_TARGET_COMBOS}")
            break
 
        fixture = fx.get("fixture") or {}
        teams = fx.get("teams") or {}
        league = fx.get("league") or {}
 
        fid = to_int(fixture.get("id"))
        kickoff_ts = to_int(fixture.get("timestamp")) or 0
 
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        hname = str(home.get("name") or "")
        aname = str(away.get("name") or "")
        lname = str(league.get("name") or "")
        lcountry = str(league.get("country") or "")
 
        if fid is None:
            if dbg:
                dbg.add_match_detail(f"[SKIP] idx={idx} fixture_id=None {hname} x {aname} {lname}")
            continue
 
        if int(fid) in used_fixture_ids:
            if dbg:
                dbg.add_match_detail(f"[SKIP] idx={idx} fid={int(fid)} already_used_fixture")
            continue
 
        if dbg:
            dbg.add_match_detail(
                f"[TRY] idx={idx} fid={int(fid)} {hname} x {aname} "
                f"kickoff={_fmt_hhmm_from_ts(kickoff_ts)} league={lname} country={lcountry}"
            )
 
        # Constrói candidatos do jogo (aplica regra dura de amostras dentro de build_match_candidates)
        fixture_cands = build_match_candidates(api_key, fx, dbg=dbg)
        if not fixture_cands:
            if dbg:
                dbg.add_match_detail(f"[SKIP] idx={idx} fid={int(fid)} no_candidates_after_rules")
            continue
 
        # Log: distribuição de candidatos por type
        if dbg:
            by_type: Dict[str, int] = {}
            for c in fixture_cands:
                t = str(c.get("type") or "")
                by_type[t] = by_type.get(t, 0) + 1
            dbg.add_match_detail(f"[CANDS] idx={idx} fid={int(fid)} by_type={by_type} total={len(fixture_cands)}")
 
        base_state = _fresh_constraint_state()
        best = _best_combo_from_single_fixture(
            fixture_cands=fixture_cands,
            target_odd=TARGET_COMBO_ODD,
            min_legs=MIN_LEGS_TARGET,
            max_legs=MAX_LEGS_TARGET,
            banned_leg_ids=used_leg_ids if ENFORCE_GLOBAL_UNIQUE_LEGS else set(),
            fixture_state=base_state,
        )
        if best is None:
            if dbg:
                dbg.add_match_detail(f"[SKIP] idx={idx} fid={int(fid)} best_ticket=None")
            continue
 
        legs, prod = best
        if not legs:
            if dbg:
                dbg.add_match_detail(f"[SKIP] idx={idx} fid={int(fid)} best_ticket_empty")
            continue
 
        # Log: legs escolhidas (com odds)
        if dbg:
            legs_str = []
            for l in legs:
                legs_str.append(
                    f"{str(l.get('type') or '')}:{str(l.get('label') or '')} odd≈{float(l.get('odd_book',0.0)):.2f}"
                )
            dbg.add_match_detail(f"[PICK] idx={idx} fid={int(fid)} legs={legs_str} ticket_odd≈{float(prod):.3f}")
 
        combos.append((legs, prod))
        used_fixture_ids.add(int(fid))
        if ENFORCE_GLOBAL_UNIQUE_LEGS:
            used_leg_ids.update(str(l.get("leg_id") or "") for l in legs if l.get("leg_id") is not None)
 
        if dbg:
            dbg.add_match_detail(f"[OK] idx={idx} fid={int(fid)} combos_now={len(combos)}")
 
    msg = build_picks_message(target_date, combos)
 
    send_ok = True
    try:
        send_telegram_message(tg_token, tg_chat_id, msg)
    except Exception as e:
        send_ok = False
        try:
            send_telegram_message(tg_token, tg_chat_id, f"Falha ao enviar mensagem principal: {repr(e)}")
        except Exception:
            pass
 
    dbg.api_calls_used = API_CALLS
    dbg.stats_calls_used = STATS_CALLS
 
    # Debug TXT: agora com mais "match_details" (ranking, motivos e legs).
    # Pode enviar sempre, ou apenas quando habilitado por envs.
    send_debug = (os.getenv("SEND_DEBUG") or "").strip().lower() in {"1", "true", "yes", "y"}
    send_debug_on_empty = (os.getenv("SEND_DEBUG_ON_EMPTY") or "1").strip().lower() in {"1", "true", "yes", "y"}
    send_debug_on_fail = (os.getenv("SEND_DEBUG_ON_FAIL") or "1").strip().lower() in {"1", "true", "yes", "y"}
 
    should_send_debug = (
        send_debug
        or (send_debug_on_empty and len(combos) == 0)
        or (send_debug_on_fail and (not send_ok))
    )
 
    if not should_send_debug:
        return
 
    debug_txt = build_debug_report(dbg)
 
    # Anexa também a mensagem final enviada (útil para auditoria do output)
    debug_with_msg = debug_txt + "\n\n==== MESSAGE_SENT ====\n" + msg + "\n"
 
    try:
        send_telegram_document(
            tg_token,
            tg_chat_id,
            f"debug_{target_date}.txt",
            debug_with_msg,
            caption="📎 Debug do processamento (ranking, decisões e legs)",
        )
    except Exception as e:
        send_telegram_message(tg_token, tg_chat_id, f"Falha ao enviar debug TXT: {repr(e)}")
 
 
if __name__ == "__main__":
    main()

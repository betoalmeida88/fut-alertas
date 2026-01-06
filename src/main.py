import os
import datetime as dt
import requests


BASE_URL = "https://v3.football.api-sports.io"


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


def today_utc_date() -> str:
    # por enquanto: usa data UTC do runner
    return dt.datetime.utcnow().strftime("%Y-%m-%d")


def main() -> None:
    api_key = os.environ["APISPORTS_KEY"]
    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_chat_id = os.environ["TELEGRAM_CHAT_ID"]

    date = today_utc_date()

    # 1) Buscar jogos do dia
    fixtures = api_get("/fixtures", api_key, params={"date": date})
    games = fixtures.get("response", [])

    # pega só os 10 primeiros para não estourar limite (vamos otimizar depois)
    games = games[:10]

    # 2) Montar mensagem simples
    lines = [f"📅 Jogos do dia (UTC): {date}", f"Encontrados: {len(games)}", ""]
    for g in games:
        league = g["league"]["name"]
        home = g["teams"]["home"]["name"]
        away = g["teams"]["away"]["name"]
        kickoff = g["fixture"]["date"]  # ISO
        lines.append(f"- {home} x {away} | {league} | {kickoff}")

    msg = "\n".join(lines) if games else f"📅 {date}\nSem jogos retornados pela API."

    send_telegram(tg_token, tg_chat_id, msg)


if __name__ == "__main__":
    main()
    

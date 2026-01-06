import os
import datetime as dt
import requests


def apisports_status(api_key: str) -> dict:
    url = "https://v3.football.api-sports.io/status"
    headers = {"x-apisports-key": api_key}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def send_telegram(token: str, chat_id: str, text: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=30)
    r.raise_for_status()
    return r.json()


def main() -> None:
    api_key = os.environ["APISPORTS_KEY"]
    tg_token = os.environ["TELEGRAM_BOT_TOKEN"]
    tg_chat_id = os.environ["TELEGRAM_CHAT_ID"]

    now_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    status = apisports_status(api_key)
    plan = status.get("response", {}).get("subscription", {}).get("plan", "?")
    used = status.get("response", {}).get("requests", {}).get("current", "?")
    limit_day = status.get("response", {}).get("requests", {}).get("limit_day", "?")

    msg = (
        f"✅ Fut Alertas rodando ({now_utc})\n"
        f"Plano: {plan}\n"
        f"Uso hoje: {used}/{limit_day}\n"
        f"Próximo passo: gerar picks 2/3/4/5."
    )

    send_telegram(tg_token, tg_chat_id, msg)


if __name__ == "__main__":
    main()
  

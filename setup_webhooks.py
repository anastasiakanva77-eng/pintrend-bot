import os
import asyncio
import httpx

BASE_URL = os.environ["BASE_URL"].rstrip("/")
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
MAX_TOKEN = os.environ["MAX_BOT_TOKEN"]
MAX_SECRET = os.environ["MAX_WEBHOOK_SECRET"]

async def main():
    async with httpx.AsyncClient(timeout=30) as client:
        tg_url = f"https://api.telegram.org/bot{TG_TOKEN}/setWebhook"
        tg_payload = {
            "url": f"{BASE_URL}/webhook/telegram",
            "secret_token": TG_SECRET,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": True,
        }
        r = await client.post(tg_url, json=tg_payload)
        print("Telegram:", r.status_code, r.text)

        max_api = "https://platform-api2.max.ru"
        headers = {"Authorization": MAX_TOKEN}
        max_webhook = f"{BASE_URL}/webhook/max"

        # Удаляем только подписку на наш же URL, если она уже существует.
        try:
            existing = await client.get(f"{max_api}/subscriptions", headers=headers)
            if existing.is_success:
                data = existing.json()
                subs = data.get("subscriptions", data if isinstance(data, list) else [])
                if isinstance(subs, list):
                    for sub in subs:
                        if isinstance(sub, dict) and sub.get("url") == max_webhook:
                            await client.delete(
                                f"{max_api}/subscriptions",
                                headers=headers,
                                params={"url": max_webhook},
                            )
        except Exception as e:
            print("MAX subscriptions check warning:", e)

        max_payload = {
            "url": max_webhook,
            "update_types": ["message_created", "message_callback", "bot_started"],
            "secret": MAX_SECRET,
        }
        r = await client.post(
            f"{max_api}/subscriptions",
            headers={**headers, "Content-Type": "application/json"},
            json=max_payload,
        )
        print("MAX:", r.status_code, r.text)

if __name__ == "__main__":
    asyncio.run(main())

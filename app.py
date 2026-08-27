import os
import re
import json
import sqlite3
import secrets
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent

# На Railway автоматически используем подключённый Volume.
# Локально всё по-прежнему хранится рядом с приложением.
STORAGE_ROOT = Path(
    os.environ.get("STORAGE_ROOT")
    or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or BASE_DIR
)
DATA_DIR = STORAGE_ROOT / "data"
MEDIA_DIR = STORAGE_ROOT / "media"
DB_PATH = DATA_DIR / "products.sqlite3"

DATA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

_railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")
if not BASE_URL:
    BASE_URL = f"https://{_railway_domain}" if _railway_domain else "http://localhost:8000"
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
MAX_TOKEN = os.environ.get("MAX_BOT_TOKEN", "")
MAX_SECRET = os.environ.get("MAX_WEBHOOK_SECRET", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}" if TG_TOKEN else ""
MAX_API = "https://platform-api2.max.ru"

app = FastAPI(title="StyleBot Telegram + MAX")
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")
security = HTTPBasic()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                article TEXT PRIMARY KEY,
                article_norm TEXT NOT NULL UNIQUE,
                aliases TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                wb_url TEXT NOT NULL DEFAULT '',
                images_json TEXT NOT NULL DEFAULT '[]'
            )
        """)
        conn.commit()


init_db()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username, ADMIN_USER)
    ok_pass = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def norm_article(value: str) -> str:
    # Пользователь может написать "j130-51", "J130 51", "j130_51".
    # Для поиска считаем это одним и тем же артикулом.
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "", (value or "")).upper()


def get_product(user_input: str):
    n = norm_article(user_input)
    if not n:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE article_norm = ?", (n,)
        ).fetchone()
        if row:
            return dict(row)

        rows = conn.execute("SELECT * FROM products").fetchall()
        for r in rows:
            aliases = [norm_article(x) for x in (r["aliases"] or "").split(",") if x.strip()]
            if n in aliases:
                return dict(r)
    return None


def list_products():
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM products ORDER BY article"
        ).fetchall()]


def public_image_urls(row):
    names = json.loads(row["images_json"] or "[]")
    return [f"{BASE_URL}/media/{name}" for name in names]


async def tg_call(method: str, payload: dict):
    if not TG_API:
        return None
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(f"{TG_API}/{method}", json=payload)
        r.raise_for_status()
        return r.json()


async def connect_telegram_webhook():
    if not TG_TOKEN:
        return False, "Не задан TELEGRAM_BOT_TOKEN"
    if not BASE_URL.startswith("https://"):
        return False, f"Нужен публичный HTTPS-домен. Сейчас BASE_URL={BASE_URL}"
    payload = {
        "url": f"{BASE_URL}/webhook/telegram",
        "secret_token": TG_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    }
    try:
        result = await tg_call("setWebhook", payload)
        ok = bool(result and result.get("ok"))
        return ok, (result or {}).get("description", "Webhook настроен" if ok else "Ошибка")
    except Exception as exc:
        return False, str(exc)


async def tg_prompt(chat_id: int):
    await tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": "Введите артикул товара, и я покажу готовые образы с ним 🤍",
    })


async def tg_send_product(chat_id: int, row: dict):
    images = public_image_urls(row)[:10]
    if len(images) == 1:
        await tg_call("sendPhoto", {"chat_id": chat_id, "photo": images[0]})
    elif len(images) >= 2:
        media = [{"type": "photo", "media": url} for url in images]
        await tg_call("sendMediaGroup", {"chat_id": chat_id, "media": media})

    keyboard = {
        "inline_keyboard": [
            [{"text": "Посмотреть товар на WB", "url": row["wb_url"]}],
            [{"text": "Ввести другой артикул", "callback_data": "another_article"}],
        ]
    }
    await tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": f"Нашла ваш товар 🤍\n{row['title']}\n\nВот готовые образы с ним.",
        "reply_markup": keyboard,
    })


async def tg_not_found(chat_id: int):
    await tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": "Не нашла такой артикул. Проверьте цифры/буквы и попробуйте ещё раз 🤍",
    })


async def max_call(path: str, *, params=None, payload=None):
    headers = {"Authorization": MAX_TOKEN, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(
            f"{MAX_API}{path}",
            headers=headers,
            params=params,
            json=payload,
        )
        r.raise_for_status()
        return r.json()


async def max_prompt(user_id: int):
    await max_call("/messages", params={"user_id": user_id}, payload={
        "text": "Введите артикул товара, и я покажу готовые образы с ним 🤍"
    })


async def max_send_product(user_id: int, row: dict):
    attachments = [
        {"type": "image", "payload": {"url": url}}
        for url in public_image_urls(row)[:10]
    ]
    buttons = [
        [{"type": "link", "text": "Посмотреть товар на WB", "url": row["wb_url"]}],
        [{"type": "callback", "text": "Ввести другой артикул", "payload": "another_article"}],
    ]
    attachments.append({
        "type": "inline_keyboard",
        "payload": {"buttons": buttons},
    })
    await max_call("/messages", params={"user_id": user_id}, payload={
        "text": f"Нашла ваш товар 🤍\n{row['title']}\n\nВот готовые образы с ним.",
        "attachments": attachments,
    })


async def max_not_found(user_id: int):
    await max_call("/messages", params={"user_id": user_id}, payload={
        "text": "Не нашла такой артикул. Проверьте цифры/буквы и попробуйте ещё раз 🤍"
    })


def _deep_find(d, key):
    if isinstance(d, dict):
        if key in d:
            return d[key]
        for v in d.values():
            found = _deep_find(v, key)
            if found is not None:
                return found
    elif isinstance(d, list):
        for v in d:
            found = _deep_find(v, key)
            if found is not None:
                return found
    return None


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    if TG_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secrets.compare_digest(got, TG_SECRET):
            raise HTTPException(status_code=403, detail="bad secret")

    update = await request.json()

    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq.get("message", {}).get("chat", {}).get("id")
        data = cq.get("data")
        if cq.get("id"):
            await tg_call("answerCallbackQuery", {"callback_query_id": cq["id"]})
        if chat_id and data == "another_article":
            await tg_prompt(chat_id)
        return {"ok": True}

    msg = update.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()

    if not chat_id:
        return {"ok": True}

    if text in ("/start", "start", "Старт"):
        await tg_prompt(chat_id)
        return {"ok": True}

    if not text:
        await tg_prompt(chat_id)
        return {"ok": True}

    row = get_product(text)
    if row:
        await tg_send_product(chat_id, row)
    else:
        await tg_not_found(chat_id)
    return {"ok": True}


@app.post("/webhook/max")
async def max_webhook(request: Request):
    if MAX_SECRET:
        got = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not secrets.compare_digest(got, MAX_SECRET):
            raise HTTPException(status_code=403, detail="bad secret")

    update = await request.json()
    update_type = update.get("update_type", "")

    # Защита от эха: не обрабатываем сообщения самого бота, если поле присутствует.
    sender = update.get("message", {}).get("sender") or update.get("user") or {}
    if isinstance(sender, dict) and sender.get("is_bot"):
        return {"ok": True}

    user_id = None
    if isinstance(sender, dict):
        user_id = sender.get("user_id")
    if not user_id:
        user_id = _deep_find(update, "user_id")

    if update_type == "bot_started":
        if user_id:
            await max_prompt(int(user_id))
        return {"ok": True}

    if update_type == "message_callback":
        callback = update.get("callback") or {}
        payload = callback.get("payload") or _deep_find(update, "payload")
        callback_id = callback.get("callback_id") or _deep_find(update, "callback_id")
        if callback_id:
            # Закрываем "часики" callback. Тело можно оставить пустым.
            try:
                await max_call("/answers", params={"callback_id": callback_id}, payload={})
            except Exception:
                pass
        if user_id and payload == "another_article":
            await max_prompt(int(user_id))
        return {"ok": True}

    if update_type == "message_created":
        message = update.get("message") or {}
        body = message.get("body") or {}
        text = (body.get("text") or message.get("text") or "").strip()
        if not user_id:
            return {"ok": True}
        if text in ("/start", "start", "Старт") or not text:
            await max_prompt(int(user_id))
            return {"ok": True}
        row = get_product(text)
        if row:
            await max_send_product(int(user_id), row)
        else:
            await max_not_found(int(user_id))

    return {"ok": True}


def html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(_: str = Depends(require_admin)):
    rows = list_products()
    items = []
    for r in rows:
        count = len(json.loads(r["images_json"] or "[]"))
        items.append(
            f"<tr><td>{html_escape(r['article'])}</td>"
            f"<td>{html_escape(r['title'])}</td>"
            f"<td>{count}</td>"
            f"<td><a href='{html_escape(r['wb_url'])}' target='_blank'>WB</a></td>"
            f"<td><form method='post' action='/admin/delete' onsubmit=\"return confirm('Удалить товар?')\">"
            f"<input type='hidden' name='article' value='{html_escape(r['article'])}'>"
            f"<button type='submit'>Удалить</button></form></td></tr>"
        )

    page = f"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StyleBot — товары</title>
<style>
body{{font-family:Arial,sans-serif;max-width:1000px;margin:30px auto;padding:0 16px;background:#fafafa;color:#222}}
.card{{background:white;border:1px solid #ddd;border-radius:16px;padding:20px;margin-bottom:24px}}
label{{display:block;margin-top:12px;font-weight:600}}
input{{box-sizing:border-box;width:100%;padding:10px;margin-top:5px;border:1px solid #ccc;border-radius:8px}}
button{{padding:10px 16px;border:0;border-radius:8px;cursor:pointer}}
.primary{{background:#111;color:white;margin-top:16px}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:10px;border-bottom:1px solid #eee;text-align:left}}
small{{color:#666}}
</style>
</head>
<body>
<h1>StyleBot</h1>
<div class="card">
<h2>Подключение Telegram</h2>
<p>Бот: <b>@pintrend_bot</b></p>
<form method="post" action="/admin/connect/telegram">
<button class="primary" type="submit">Подключить Telegram webhook</button>
</form>
<small>Нажимать после того, как Railway выдал публичный HTTPS-домен и TELEGRAM_BOT_TOKEN добавлен в Variables.</small>
</div>
<div class="card">
<h2>Добавить / обновить товар</h2>
<form method="post" action="/admin/product" enctype="multipart/form-data">
<label>Артикул покупателя</label>
<input name="article" required placeholder="Например J130 или 123456789">
<label>Дополнительные варианты</label>
<input name="aliases" placeholder="Через запятую: J130-51, Ж130">
<small>Можно оставить пустым.</small>
<label>Название товара</label>
<input name="title" required placeholder="Брюки алладины хаки">
<label>Ссылка на Wildberries</label>
<input name="wb_url" required placeholder="https://www.wildberries.ru/catalog/...">
<label>Фото образов (1–10)</label>
<input type="file" name="images" accept="image/*" multiple>
<small>При обновлении: если новые фото не выбраны, старые сохранятся. Если выбраны — заменятся.</small>
<br>
<button class="primary" type="submit">Сохранить товар</button>
</form>
</div>
<div class="card">
<h2>Товары</h2>
<table>
<thead><tr><th>Артикул</th><th>Название</th><th>Фото</th><th>WB</th><th></th></tr></thead>
<tbody>{''.join(items) if items else '<tr><td colspan="5">Пока нет товаров</td></tr>'}</tbody>
</table>
</div>
</body>
</html>
"""
    return HTMLResponse(page)


@app.post("/admin/connect/telegram", response_class=HTMLResponse)
async def admin_connect_telegram(_: str = Depends(require_admin)):
    ok, message = await connect_telegram_webhook()
    status = "✅ Telegram подключён" if ok else "❌ Не удалось подключить Telegram"
    return HTMLResponse(f"""
<!doctype html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram — StyleBot</title>
<style>
body{{font-family:Arial,sans-serif;max-width:700px;margin:40px auto;padding:0 16px}}
.card{{border:1px solid #ddd;border-radius:16px;padding:24px}}
a{{display:inline-block;margin-top:18px}}
</style></head>
<body><div class="card">
<h2>{status}</h2>
<p>{html_escape(message)}</p>
<p>Webhook: <code>{html_escape(BASE_URL)}/webhook/telegram</code></p>
<a href="/admin">← Вернуться в админку</a>
</div></body></html>
""")


@app.post("/admin/product")
async def admin_product(
    article: str = Form(...),
    aliases: str = Form(""),
    title: str = Form(...),
    wb_url: str = Form(...),
    images: list[UploadFile] = File(default=[]),
    _: str = Depends(require_admin),
):
    n = norm_article(article)
    if not n:
        raise HTTPException(status_code=400, detail="Некорректный артикул")
    if not wb_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="WB URL должен начинаться с http:// или https://")

    images = [x for x in images if x and x.filename]
    if len(images) > 10:
        raise HTTPException(status_code=400, detail="Максимум 10 фотографий")

    with db() as conn:
        old = conn.execute(
            "SELECT images_json FROM products WHERE article_norm = ?", (n,)
        ).fetchone()
        old_names = json.loads(old["images_json"]) if old else []

        new_names = old_names
        if images:
            # Удаляем старые фото только когда пользователь действительно загрузил новый комплект.
            for rel in old_names:
                try:
                    (MEDIA_DIR / rel).unlink(missing_ok=True)
                except Exception:
                    pass

            folder = MEDIA_DIR / n
            folder.mkdir(parents=True, exist_ok=True)
            new_names = []
            for idx, upload in enumerate(images, start=1):
                ext = Path(upload.filename).suffix.lower() or ".jpg"
                if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                    ext = ".jpg"
                rel = f"{n}/{idx:02d}{ext}"
                target = MEDIA_DIR / rel
                content = await upload.read()
                target.write_bytes(content)
                new_names.append(rel)

        conn.execute("""
            INSERT INTO products(article, article_norm, aliases, title, wb_url, images_json)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(article_norm) DO UPDATE SET
                article=excluded.article,
                aliases=excluded.aliases,
                title=excluded.title,
                wb_url=excluded.wb_url,
                images_json=excluded.images_json
        """, (article.strip(), n, aliases.strip(), title.strip(), wb_url.strip(), json.dumps(new_names, ensure_ascii=False)))
        conn.commit()

    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/delete")
async def admin_delete(
    article: str = Form(...),
    _: str = Depends(require_admin),
):
    n = norm_article(article)
    with db() as conn:
        row = conn.execute(
            "SELECT images_json FROM products WHERE article_norm = ?", (n,)
        ).fetchone()
        if row:
            for rel in json.loads(row["images_json"] or "[]"):
                try:
                    (MEDIA_DIR / rel).unlink(missing_ok=True)
                except Exception:
                    pass
            conn.execute("DELETE FROM products WHERE article_norm = ?", (n,))
            conn.commit()
    return RedirectResponse("/admin", status_code=303)

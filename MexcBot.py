import os
import time
import hmac
import hashlib
import logging
import aiohttp
import asyncio
from dotenv import load_dotenv
from aiohttp import ClientTimeout
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from fastapi import FastAPI
import uvicorn
import threading

# ====================== НАСТРОЙКИ ======================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Глобальные переменные
ALL_SYMBOLS = set()
user_settings = {}
user_state = {}
user_temp = {}

SHOW_INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"]
NOTIFY_EMOJI = "Активно"
DISABLED_EMOJI = "Отключено"

# ====================== КЛАВИАТУРЫ ======================
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить алерт", callback_data="add")],
        [InlineKeyboardButton("📋 Мои алерты", callback_data="list")],
        [InlineKeyboardButton("❌ Удалить алерт", callback_data="delete")],
    ])

def intervals_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱ 1m", callback_data="int_1m"),
            InlineKeyboardButton("⏱ 5m", callback_data="int_5m"),
            InlineKeyboardButton("⏱ 15m", callback_data="int_15m"),
        ],
        [
            InlineKeyboardButton("⏱ 30m", callback_data="int_30m"),
            InlineKeyboardButton("🕐 1h", callback_data="int_1h"),
            InlineKeyboardButton("🕓 4h", callback_data="int_4h"),
        ],
        [
            InlineKeyboardButton("🕗 8h", callback_data="int_8h"),
            InlineKeyboardButton("📅 1d", callback_data="int_1d"),
        ],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
    ])

def volume_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("2000", callback_data="volbtn_2000"),
            InlineKeyboardButton("3000", callback_data="volbtn_3000"),
        ],
        [
            InlineKeyboardButton("4000", callback_data="volbtn_4000"),
            InlineKeyboardButton("5000", callback_data="volbtn_5000"),
        ],
        [InlineKeyboardButton("✏️ Ввести вручную", callback_data="vol_custom")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
    ])

def list_kb(chat_id):
    sets = user_settings.get(chat_id, [])
    kb = []
    for i, s in enumerate(sets):
        status = NOTIFY_EMOJI if s.get("notifications_enabled", True) else DISABLED_EMOJI
        kb.append([InlineKeyboardButton(
            f"{s['symbol']} {s['interval']} ≥{s['threshold']:,} USDT {status}",
            callback_data=f"alert_options_{i}"
        )])
    if sets:
        kb.append([InlineKeyboardButton("🔄 Обновить все", callback_data="refresh_all")])
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    return InlineKeyboardMarkup(kb)

# ====================== MEXC API ======================
async def load_symbols():
    global ALL_SYMBOLS
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://contract.mexc.com/api/v1/contract/detail", timeout=ClientTimeout(total=10)) as r:
                if r.status == 200:
                    j = await r.json()
                    if j.get("success") and j.get("data"):
                        ALL_SYMBOLS = {x["symbol"].replace("_USDT", "USDT") for x in j["data"] if "_USDT" in x["symbol"]}
                        logger.info(f"Загружено {len(ALL_SYMBOLS)} пар")
                    else:
                        ALL_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    except Exception as e:
        logger.error(f"Ошибка загрузки символов: {e}")
        ALL_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

async def fetch_volume(symbol: str, interval: str) -> int:
    interval_map = {
        "1m": "Min1", "5m": "Min5", "15m": "Min15", "30m": "Min30",
        "1h": "Min60", "4h": "Hour4", "8h": "Hour8", "1d": "Day1",
    }
    sym = symbol.replace("USDT", "_USDT")
    ts = str(int(time.time() * 1000))
    query = f"symbol={sym}&interval={interval_map.get(interval, 'Min1')}&limit=1"
    sign = hmac.new(MEXC_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    headers = {"ApiKey": MEXC_API_KEY, "Request-Time": ts, "Signature": sign}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://contract.mexc.com/api/v1/contract/kline/{sym}",
                params={"symbol": sym, "interval": interval_map.get(interval, "Min1"), "limit": 1},
                headers=headers,
                timeout=ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    j = await r.json()
                    if j.get("success") and j.get("data", {}).get("amount"):
                        return int(float(j["data"]["amount"][0]))
    except Exception as e:
        logger.error(f"Ошибка получения объёма {symbol}: {e}")
    return 0

# ====================== МОНИТОРИНГ (БЕЗОПАСНЫЙ) ======================
async def monitor_volumes(application: Application):
    await asyncio.sleep(10)
    await load_symbols()
    logger.info("Мониторинг объёмов запущен — работает 24/7")

    while True:
        try:
            for chat_id, alerts in list(user_settings.items()):
                for alert in alerts[:]:
                    try:
                        vol = await fetch_volume(alert["symbol"], alert["interval"])
                        if (vol >= alert["threshold"]
                            and vol > alert.get("last_notified", 0) + 1000
                            and alert.get("notifications_enabled", True)):
                            url = f"https://www.mexc.com/ru-RU/futures/{alert['symbol'][:-4]}_USDT"
                            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Перейти на MEXC", url=url)]])
                            await application.bot.send_message(
                                chat_id,
                                f"<b>ВСПЛЕСК ОБЪЁМА!</b>\n\n"
                                f"<b>Пара:</b> {alert['symbol']}\n"
                                f"<b>Таймфрейм:</b> {alert['interval']}\n"
                                f"<b>Порог:</b> {alert['threshold']:,} USDT\n"
                                f"<b>Текущий объем:</b> {vol:,} USDT",
                                parse_mode="HTML",
                                reply_markup=kb
                            )
                            alert["last_notified"] = vol
                    except Exception as e:
                        logger.error(f"Ошибка проверки алерта: {e}")
            await asyncio.sleep(30)
        except (asyncio.CancelledError, GeneratorExit):
            logger.info("Мониторинг временно остановлен — перезапуск через 10 сек...")
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Критическая ошибка мониторинга: {e}")
            await asyncio.sleep(60)

# ====================== POST_INIT ======================
async def post_init(application: Application):
    await load_symbols()
    application.create_task(monitor_volumes(application))

# ====================== ДЕТАЛИ АЛЕРТА С ОБЪЁМАМИ ======================
async def show_alert_details_with_volumes(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    s = user_settings[chat_id][idx]
    symbol = s["symbol"]

    await q.edit_message_text("<b>Загружаем текущие объёмы...</b>", parse_mode="HTML")
    tasks = [fetch_volume(symbol, tf) for tf in SHOW_INTERVALS]
    results = await asyncio.gather(*tasks)
    vols = dict(zip(SHOW_INTERVALS, results))

    status = NOTIFY_EMOJI if s.get("notifications_enabled", True) else DISABLED_EMOJI
    text = (
        f"<b>Настройки алерта:</b>\n\n"
        f"<b>Пара:</b> {symbol}\n"
        f"<b>Таймфрейм:</b> {s['interval']}\n"
        f"<b>Порог:</b> {s['threshold']:,} USDT\n"
        f"<b>Уведомления:</b> {status}\n\n"
        f"<b>Текущие объёмы:</b>\n"
    )
    for tf in SHOW_INTERVALS:
        v = vols[tf]
        emoji = "🟢" if v > 10_000_000 else "🟡" if v > 1_000_000 else "🔴"
        text += f"{emoji} <code>{tf.rjust(3)}</code> → <b>{v:,} USDT</b>\n"

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Перейти на MEXC", url=f"https://www.mexc.com/ru-RU/futures/{symbol[:-4]}_USDT"),
            InlineKeyboardButton(f"Уведомления: {status}", callback_data=f"toggle_notify_{idx}")
        ],
        [InlineKeyboardButton("Назад", callback_data="list")],
    ])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

# ====================== ОБРАБОТЧИКИ ======================
async def any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("🚫 Доступ запрещён")
        return

    chat_id = update.effective_chat.id
    user_settings.setdefault(chat_id, [])
    text = (update.message.text or "").strip().lower()

    if not text or any(w in text for w in ["меню", "start", "привет", "/start"]):
        await update.message.reply_text(
            "🔥 <b>MEXC Volume Tracker</b> 🔥\n\n"
            "📈 Отслеживание объемов в реальном времени\n"
            "🔔 Мгновенные уведомления о всплесках\n"
            "⚡ Работает 24/7 без перерывов\n\n"
            "Выберите действие:",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    state = user_state.get(chat_id)
    if state == "wait_symbol":
        sym = text.upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        if sym not in ALL_SYMBOLS:
            await update.message.reply_text(f"⚠️ Пара <b>{sym}</b> не найдена", parse_mode="HTML")
            return
        user_temp[chat_id] = {"symbol": sym}
        user_state[chat_id] = "wait_interval"
        await update.message.reply_text(f"✅ Пара: <b>{sym}</b>\nВыберите таймфрейм:", parse_mode="HTML", reply_markup=intervals_kb())
        return

    if state in ["wait_threshold", "edit_threshold", "wait_threshold_custom", "edit_threshold_custom"]:
        try:
            threshold_value = int("".join(filter(str.isdigit, update.message.text.strip())))
            if threshold_value < 1000:
                await update.message.reply_text("⚠️ Минимальный порог 1000 USDT")
                return
        except:
            await update.message.reply_text("⚠️ Введите число ≥ 1000")
            return

        is_edit = state in ["edit_threshold", "edit_threshold_custom"]
        if is_edit:
            idx = user_temp[chat_id]["edit_idx"]
            user_settings[chat_id][idx]["threshold"] = threshold_value
            msg = f"✅ Алерт обновлён!\n<b>{user_settings[chat_id][idx]['symbol']} {user_settings[chat_id][idx]['interval']}</b>\nПорог: {threshold_value:,} USDT"
        else:
            alert = {
                "symbol": user_temp[chat_id]["symbol"],
                "interval": user_temp[chat_id]["interval"],
                "threshold": threshold_value,
                "last_notified": 0,
                "notifications_enabled": True,
            }
            user_settings[chat_id].append(alert)
            msg = f"✅ Алерт добавлен!\n<b>{alert['symbol']} {alert['interval']}</b>\nПорог: {threshold_value:,} USDT"

        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=main_menu())
        user_state.pop(chat_id, None)
        user_temp.pop(chat_id, None)
        return

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    data = q.data
    chat_id = q.message.chat_id

    if data == "back":
        user_state.pop(chat_id, None)
        user_temp.pop(chat_id, None)
        await q.edit_message_text("Главное меню", reply_markup=main_menu())
        return

    if data == "add":
        user_state[chat_id] = "wait_symbol"
        await q.edit_message_text("Введите тикер монеты (например: BTC, ETH, SOL):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="back")]]))
        return

    if data == "list":
        text = "📋 Ваши активные алерты:" if user_settings.get(chat_id) else "ℹ️ У вас нет активных алертов"
        await q.edit_message_text(text, reply_markup=list_kb(chat_id))
        return

    if data == "delete":
        if not user_settings.get(chat_id):
            await q.edit_message_text("ℹ️ Нет алертов для удаления", reply_markup=main_menu())
            return
        kb = [[InlineKeyboardButton(f"{s['symbol']} {s['interval']} ≥{s['threshold']:,} USDT", callback_data=f"del_{i}")] 
              for i, s in enumerate(user_settings[chat_id])]
        kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
        await q.edit_message_text("❌ Выберите алерт для удаления:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("del_"):
        idx = int(data.split("_")[1])
        deleted = user_settings[chat_id].pop(idx)["symbol"]
        await q.edit_message_text(f"✅ Алерт для {deleted} удалён", reply_markup=main_menu())
        return

    if data.startswith("alert_options_"):
        idx = int(data.split("_")[2])
        await show_alert_details_with_volumes(update, context, idx)
        return

    if data.startswith("toggle_notify_"):
        idx = int(data.split("_")[2])
        s = user_settings[chat_id][idx]
        s["notifications_enabled"] = not s.get("notifications_enabled", True)
        await show_alert_details_with_volumes(update, context, idx)
        return

    if data.startswith("int_"):
        interval = data.split("_")[1]
        if user_state.get(chat_id) == "edit_interval":
            user_temp[chat_id]["interval"] = interval
            user_state[chat_id] = "edit_threshold"
            await q.edit_message_text(f"🆕 Новый таймфрейм: <b>{interval}</b>\nВыберите порог объема:", parse_mode="HTML", reply_markup=volume_kb())
        else:
            user_temp[chat_id]["interval"] = interval
            user_state[chat_id] = "wait_threshold"
            await q.edit_message_text(f"✅ Таймфрейм: <b>{interval}</b>\nВыберите порог объема:", parse_mode="HTML", reply_markup=volume_kb())
        return

    if data.startswith("volbtn_"):
        volume = int(data.split("_")[1])
        is_edit = user_state.get(chat_id) == "edit_threshold"
        if is_edit:
            idx = user_temp[chat_id]["edit_idx"]
            user_settings[chat_id][idx]["threshold"] = volume
            msg = f"✅ Алерт обновлён!\n<b>{user_settings[chat_id][idx]['symbol']} {user_settings[chat_id][idx]['interval']}</b>\nПорог: {volume:,} USDT"
        else:
            alert = {
                "symbol": user_temp[chat_id]["symbol"],
                "interval": user_temp[chat_id]["interval"],
                "threshold": volume,
                "last_notified": 0,
                "notifications_enabled": True,
            }
            user_settings[chat_id].append(alert)
            msg = f"✅ Алерт добавлен!\n<b>{alert['symbol']} {alert['interval']}</b>\nПорог: {volume:,} USDT"

        await q.edit_message_text(msg, parse_mode="HTML", reply_markup=main_menu())
        user_state.pop(chat_id, None)
        user_temp.pop(chat_id, None)
        return

    if data == "vol_custom":
        is_edit = user_state.get(chat_id) == "edit_threshold"
        user_state[chat_id] = "edit_threshold_custom" if is_edit else "wait_threshold_custom"
        await q.edit_message_text("✏️ Введите порог объема в USDT (например: 10000):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]]))
        return

# ====================== ВЕБ-СЕРВЕР ДЛЯ RENDER ======================
web_app = FastAPI()

@web_app.get("/")
async def root():
    print("UptimeRobot пинганул — ответил 200 OK")  # будет видно в логах Render
    return {"status": "OK", "bot": "MEXC Volume Bot живой", "time": time.strftime("%H:%M:%S")}

def run_web_server():
    port = int(os.environ.get("PORT", 8000))
    print(f"Веб-сервер успешно запущен на порту {port} — Render доволен!")
    uvicorn.run(web_app, host="0.0.0.0", port=port, log_level="info")

# ====================== ЗАПУСК ======================
def run_bot():
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_message))
    application.add_handler(CallbackQueryHandler(button_handler))

    logger.info("MEXC Volume Bot запущен и работает стабильно 24/7")
    application.run_polling(drop_pending_updates=True, timeout=30)

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()  
    time.sleep(2)
    run_bot()











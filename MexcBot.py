import hmac
import hashlib
import logging
import aiohttp
import asyncio
import time
import json
import os
from dotenv import load_dotenv
from aiohttp import ClientTimeout
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CommandHandler
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log")
    ]
)
logger = logging.getLogger()

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")

# Глобальные переменные (создаются при запуске)
ALL_SYMBOLS = set()
user_settings = {}
user_state = {}
user_temp = {}

# Параметры отображения
SHOW_INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"]
ADD_INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "8h"]

# Символы для статуса уведомлений
NOTIFY_EMOJI = "✅"
DISABLED_EMOJI = "❌"

# ====================== ВИЗУАЛЬНЫЕ ЭЛЕМЕНТЫ ======================
def main_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить алерт", callback_data="add")],
            [InlineKeyboardButton("📋 Мои алерты", callback_data="list")],
            [InlineKeyboardButton("❌ Удалить алерт", callback_data="delete")],
        ]
    )


def intervals_kb():
    return InlineKeyboardMarkup(
        [
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
        ]
    )


def volume_kb():
    return InlineKeyboardMarkup(
        [
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
        ]
    )


def list_kb(chat_id):
    sets = user_settings.get(chat_id, [])
    kb = []
    for i, s in enumerate(sets):
        status = (
            NOTIFY_EMOJI if s.get("notifications_enabled", True) else DISABLED_EMOJI
        )
        kb.append(
            [
                InlineKeyboardButton(
                    f"{s['symbol']} {s['interval']} ≥{s['threshold']:,} USDT {status}",
                    callback_data=f"alert_options_{i}",
                )
            ]
        )
    if sets:
        kb.append(
            [InlineKeyboardButton("🔄 Обновить все", callback_data="refresh_all")]
        )
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
    return InlineKeyboardMarkup(kb)


# ====================== КОНЕЦ ВИЗУАЛЬНЫХ ЭЛЕМЕНТОВ ======================


async def load_symbols():
    global ALL_SYMBOLS
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://contract.mexc.com/api/v1/contract/detail",
                timeout=ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    j = await r.json()
                    if j.get("success") and j.get("data"):
                        ALL_SYMBOLS = {
                            x["symbol"].replace("_USDT", "USDT")
                            for x in j["data"]
                            if "_USDT" in x["symbol"]
                        }
                        logger.info(f"Loaded {len(ALL_SYMBOLS)} symbols")
                    else:
                        logger.warning("API returned non-success status")
                        ALL_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    except Exception as e:
        logger.error(f"Error loading symbols: {e}")
        ALL_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}


async def fetch_volume(symbol: str, interval: str) -> int:
    interval_map = {
        "1m": "Min1",
        "5m": "Min5",
        "15m": "Min15",
        "30m": "Min30",
        "1h": "Min60",
        "4h": "Hour4",
        "8h": "Hour8",
        "1d": "Day1",
    }
    sym = symbol.replace("USDT", "_USDT")
    ts = str(int(time.time() * 1000))
    query = f"symbol={sym}&interval={interval_map.get(interval, 'Min1')}&limit=1"
    sign = hmac.new(
        MEXC_SECRET_KEY.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    headers = {"ApiKey": MEXC_API_KEY, "Request-Time": ts, "Signature": sign}

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://contract.mexc.com/api/v1/contract/kline/{sym}",
                params={
                    "symbol": sym,
                    "interval": interval_map.get(interval, "Min1"),
                    "limit": 1,
                },
                headers=headers,
                timeout=ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    j = await r.json()
                    if j.get("success") and j.get("data"):
                        amounts = j["data"].get("amount")
                        if isinstance(amounts, list) and amounts:
                            return int(float(amounts[0]))
                    logger.warning(f"Invalid response format for {symbol}")
    except Exception as e:
        logger.error(f"Error fetching volume for {symbol}: {e}")
    return 0


async def monitor_volumes(app):
    logger.info("Volume monitoring started")
    await load_symbols()

    while True:
        try:
            # Основной цикл мониторинга
            for chat_id, sets in list(user_settings.items()):
                for s in sets[:]:
                    try:
                        vol = await fetch_volume(s["symbol"], s["interval"])
                        if (
                            vol >= s["threshold"]
                            and vol > s.get("last_notified", 0) + 1000
                            and s.get("notifications_enabled", True)
                        ):
                            asset = s['symbol'][:-4]  # Удалить USDT
                            url = f"https://www.mexc.com/ru-RU/futures/{asset}_USDT"
                            kb = InlineKeyboardMarkup(
                                [[InlineKeyboardButton("Перейти на MEXC", url=url)]]
                            )
                            await app.bot.send_message(
                                chat_id,
                                f"<b>ВСПЛЕСК ОБЪЁМА!</b>\n\n"
                                f"<b>Пара:</b> {s['symbol']}\n"
                                f"<b>Таймфрейм:</b> {s['interval']}\n"
                                f"<b>Порог:</b> {s['threshold']:,} USDT\n"
                                f"<b>Текущий объем:</b> {vol:,} USDT",
                                parse_mode="HTML",
                                reply_markup=kb,
                            )
                            s["last_notified"] = vol
                    except Exception as e:
                        logger.error(f"Error in alert check: {e}")

            await asyncio.sleep(30)

        except asyncio.CancelledError:
            logger.info("Monitoring task cancelled")
            break
        except Exception as e:
            logger.error(f"Critical monitor error: {e}")
            await asyncio.sleep(60)


async def show_volumes(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
    q = update.callback_query
    await q.answer()

    try:
        # Показываем анимацию загрузки
        await q.edit_message_text(
            f"<b>🔍 Загружаем данные для {symbol}...</b>", parse_mode="HTML"
        )

        # Получаем объемы
        tasks = [fetch_volume(symbol, tf) for tf in SHOW_INTERVALS]
        results = await asyncio.gather(*tasks)
        vols = dict(zip(SHOW_INTERVALS, results))

        # Форматируем сообщение
        text = f"<b>📊 Объемы {symbol}</b>\n<i>🕒 {time.strftime('%H:%M:%S')}</i>\n\n"
        for tf in SHOW_INTERVALS:
            v = vols[tf]
            emoji = "🟢" if v > 10000000 else "🟡" if v > 1000000 else "🔴"
            text += f"{emoji} <code>{tf.rjust(3)}</code> → <b>{v:,} USDT</b>\n"

        # Создаем клавиатуру
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Обновить", callback_data=f"ref_{symbol}")],
                [
                    InlineKeyboardButton(
                        "🔹 MEXC",
                        url=f"https://www.mexc.com/ru-RU/futures/{symbol[:-4]}_USDT",
                    )
                ],
                [InlineKeyboardButton("🔙 Назад", callback_data="list")],
            ]
        )

        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

    except Exception as e:
        logger.error(f"Error showing volumes: {e}")
        await q.edit_message_text(
            "⚠️ Ошибка при загрузке данных. Попробуйте позже.", reply_markup=main_menu()
        )


async def show_alert_details_with_volumes(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    q = update.callback_query
    await q.answer()
    
    chat_id = q.message.chat_id
    s = user_settings[chat_id][idx]
    symbol = s["symbol"]
    
    # Загружаем объёмы
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
        emoji = "🟢" if v > 10000000 else "🟡" if v > 1000000 else "🔴"
        text += f"{emoji} <code>{tf.rjust(3)}</code> → <b>{v:,} USDT</b>\n"
    
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Перейти на MEXC", 
                    url=f"https://www.mexc.com/ru-RU/futures/{symbol[:-4]}_USDT"
                ),
                InlineKeyboardButton(
                    f"Уведомления: {status}",
                    callback_data=f"toggle_notify_{idx}"
                ),
            ],
            [InlineKeyboardButton("Назад", callback_data="list")],
        ]
    )
    
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("🚫 Доступ запрещён")
        return

    chat_id = update.effective_chat.id
    user_settings.setdefault(chat_id, [])
    text = (update.message.text or "").strip().lower()

    if not text or "меню" in text or "start" in text or "привет" in text:
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
            await update.message.reply_text(
                f"⚠️ Пара <b>{sym}</b> не найдена", parse_mode="HTML"
            )
            return

        user_temp[chat_id] = {"symbol": sym}
        user_state[chat_id] = "wait_interval"
        await update.message.reply_text(
            f"✅ Пара: <b>{sym}</b>\n" "Выберите таймфрейм:",
            parse_mode="HTML",
            reply_markup=intervals_kb(),
        )
        return

    if state in [
        "wait_threshold",
        "edit_threshold",
        "wait_threshold_custom",
        "edit_threshold_custom",
    ]:
        try:
            # Обработка ручного ввода порога
            raw_text = update.message.text.strip()
            threshold_value = int("".join(filter(str.isdigit, raw_text)))
            if threshold_value < 1000:
                await update.message.reply_text("⚠️ Минимальный порог 1000 USDT")
                return
        except:
            await update.message.reply_text(
                "⚠️ Введите число ≥ 1000 (например: 2000, 5000, 10000)"
            )
            return

        # Определяем режим (редактирование или добавление)
        is_edit_mode = state in ["edit_threshold", "edit_threshold_custom"]

        # Обновление или добавление алерта
        if is_edit_mode:
            idx = user_temp[chat_id].get("edit_idx", 0)
            s = user_settings[chat_id][idx]
            s["threshold"] = threshold_value
            response_text = f"✅ Алерт обновлен!\n<b>{s['symbol']} {s['interval']}</b>\nПорог: {threshold_value:,} USDT"
            logger.info(f"Alert updated: {s['symbol']} {s['interval']} ≥{threshold_value}")
        else:
            alert_data = {
                "symbol": user_temp[chat_id]["symbol"],
                "interval": user_temp[chat_id]["interval"],
                "threshold": threshold_value,
                "last_notified": 0,
                "notifications_enabled": True,
            }
            user_settings[chat_id].append(alert_data)
            response_text = f"✅ Алерт добавлен!\n<b>{user_temp[chat_id]['symbol']} {user_temp[chat_id]['interval']}</b>\nПорог: {threshold_value:,} USDT"
            logger.info(f"Alert added: {alert_data}")

        await update.message.reply_text(
            response_text, parse_mode="HTML", reply_markup=main_menu()
        )

        # Сброс состояний
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

    # Обработка основных команд
    if data == "back":
        user_state.pop(chat_id, None)
        user_temp.pop(chat_id, None)
        await q.edit_message_text("Главное меню", reply_markup=main_menu())
        return

    if data == "add":
        user_state[chat_id] = "wait_symbol"
        await q.edit_message_text(
            "Введите тикер монеты (например: BTC, ETH, SOL):",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Отмена", callback_data="back")]]
            ),
        )
        return

    if data == "list":
        kb = list_kb(chat_id)
        await q.edit_message_text(
            (
                "📋 Ваши активные алерты:"
                if user_settings.get(chat_id)
                else "ℹ️ У вас нет активных алертов"
            ),
            reply_markup=kb if kb else main_menu(),
        )
        return

    if data == "delete":
        if not user_settings.get(chat_id):
            await q.edit_message_text(
                "ℹ️ Нет алертов для удаления", reply_markup=main_menu()
            )
            return

        kb = []
        for i, s in enumerate(user_settings[chat_id]):
            kb.append(
                [
                    InlineKeyboardButton(
                        f"{s['symbol']} {s['interval']} ≥{s['threshold']:,} USDT",
                        callback_data=f"del_{i}",
                    )
                ]
            )
        kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])

        await q.edit_message_text(
            "❌ Выберите алерт для удаления:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # Обработка конкретных действий
    if data.startswith("del_"):
        idx = int(data.split("_")[1])
        symbol = user_settings[chat_id][idx]["symbol"]
        del user_settings[chat_id][idx]
        await q.edit_message_text(
            f"✅ Алерт для {symbol} удален", reply_markup=main_menu()
        )
        logger.info(f"Alert deleted: {symbol}")
        return

    if data.startswith("showvol_"):
        idx = int(data.split("_")[1])
        await show_volumes(update, context, user_settings[chat_id][idx]["symbol"])
        return

    if data.startswith("ref_"):
        symbol = data.split("_", 1)[1]
        await show_volumes(update, context, symbol)
        return

    if data.startswith("edit_"):
        idx = int(data.split("_")[1])
        user_temp[chat_id] = {"edit_idx": idx}
        user_state[chat_id] = "edit_interval"
        s = user_settings[chat_id][idx]
        await q.edit_message_text(
            f"✏️ Редактирование алерта:\n"
            f"<b>{s['symbol']} {s['interval']}</b>\n"
            f"Текущий порог: {s['threshold']:,} USDT\n\n"
            "Выберите новый таймфрейм:",
            parse_mode="HTML",
            reply_markup=intervals_kb(),
        )
        return

    if data.startswith("alert_options_"):
        idx = int(data.split("_")[2])
        await show_alert_details_with_volumes(update, context, idx)
        return

    if data.startswith("toggle_notify_"):
        idx = int(data.split("_")[2])
        s = user_settings[chat_id][idx]
        current_state = s.get("notifications_enabled", True)
        s["notifications_enabled"] = not current_state
        new_status = NOTIFY_EMOJI if s["notifications_enabled"] else DISABLED_EMOJI
        status_text = "включены" if s["notifications_enabled"] else "выключены"
        await q.answer(
            f"Уведомления {status_text}",
            show_alert=True,
        )
        logger.info(f"Notifications toggled for {s['symbol']}: {status_text}")
        
        # Обновляем сообщение
        if idx < len(user_settings[chat_id]):
            await show_alert_details_with_volumes(update, context, idx)
        else:
            await q.edit_message_text(
                "⚠️ Ошибка при обновлении", reply_markup=main_menu()
            )
        return

    if data.startswith("int_"):
        interval = data.split("_")[1]
        if user_state.get(chat_id) == "edit_interval":
            user_temp[chat_id]["interval"] = interval
            user_state[chat_id] = "edit_threshold"
            await q.edit_message_text(
                f"🆕 Новый таймфрейм: <b>{interval}</b>\n" "Выберите порог объема:",
                parse_mode="HTML",
                reply_markup=volume_kb(),
            )
        else:
            user_temp[chat_id]["interval"] = interval
            user_state[chat_id] = "wait_threshold"
            await q.edit_message_text(
                f"✅ Таймфрейм: <b>{interval}</b>\n" "Выберите порог объема:",
                parse_mode="HTML",
                reply_markup=volume_kb(),
            )
        return

    if data.startswith("volbtn_"):
        try:
            volume = int(data.split("_")[1])
            if user_state.get(chat_id) == "edit_threshold":
                idx = user_temp[chat_id].get("edit_idx", 0)
                s = user_settings[chat_id][idx]
                s["threshold"] = volume
                await q.edit_message_text(
                    f"✅ Алерт обновлен!\n"
                    f"<b>{s['symbol']} {s['interval']}</b>\n"
                    f"Порог: {volume:,} USDT",
                    parse_mode="HTML",
                    reply_markup=main_menu(),
                )
                logger.info(f"Alert updated: {s['symbol']} {s['interval']} ≥{volume}")
            else:
                alert_data = {
                    "symbol": user_temp[chat_id]["symbol"],
                    "interval": user_temp[chat_id]["interval"],
                    "threshold": volume,
                    "last_notified": 0,
                    "notifications_enabled": True,
                }
                user_settings[chat_id].append(alert_data)
                await q.edit_message_text(
                    f"✅ Алерт добавлен!\n"
                    f"<b>{user_temp[chat_id]['symbol']} {user_temp[chat_id]['interval']}</b>\n"
                    f"Порог: {volume:,} USDT",
                    parse_mode="HTML",
                    reply_markup=main_menu(),
                )
                logger.info(f"Alert added: {alert_data}")

            # Сброс состояния
            user_state.pop(chat_id, None)
            user_temp.pop(chat_id, None)
        except Exception as e:
            logger.error(f"Error processing volume button: {e}")
            await q.answer("Произошла ошибка при обработке", show_alert=True)
        return

    if data == "vol_custom":
        # Определяем состояние (редактирование или добавление)
        is_edit = user_state.get(chat_id) == "edit_threshold"
        state_name = "edit_threshold_custom" if is_edit else "wait_threshold_custom"
        user_state[chat_id] = state_name

        await q.edit_message_text(
            "✏️ Введите порог объема в USDT (например: 2000, 5000, 10000):",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
            ),
        )
        return


async def post_init(application: Application) -> None:
    try:
        await load_symbols()
        logger.info("Symbols loaded successfully")
    except Exception as e:
        logger.error(f"Error loading symbols on startup: {e}")


async def start_bot(application: Application):
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    # Создаем задачу мониторинга
    monitoring_task = asyncio.create_task(monitor_volumes(application))
    
    # Ждем завершения или отмены
    try:
        while True:
            await asyncio.sleep(3600)  # Проверяем каждые час
    except asyncio.CancelledError:
        logger.info("Shutting down monitoring")
        monitoring_task.cancel()
        await monitoring_task
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


def main():
    # Создаем приложение Telegram
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # Регистрируем обработчики
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_message))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("start", any_message))

    # Запускаем бота
    logger.info("MEXC Volume Bot запущен")
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(start_bot(application))
    except KeyboardInterrupt:
        logger.info("Bot stopped by keyboard interrupt")
    except Exception as e:
        logger.error(f"Critical error: {e}")
    finally:
        logger.info("MEXC Volume Bot остановлен")


# Веб-сервер для Render.com
from fastapi import FastAPI
import uvicorn

app = FastAPI()

@app.get("/")
def health_check():
    return {"status": "running", "service": "MEXC Volume Bot"}

if __name__ == "__main__":
    # Запустить веб-сервер в отдельном процессе
    import threading
    web_thread = threading.Thread(
        target=lambda: uvicorn.run(
            app, 
            host="0.0.0.0", 
            port=int(os.environ.get("PORT", 8080))
        ),
        daemon=True
    )
    web_thread.start()
    
    # Запустить основное приложение
    main()









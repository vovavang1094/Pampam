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
import json

# ====================== НАСТРОЙКИ ======================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")
DATA_FILE = "alerts.json"  # Файл для сохранения настроек

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
_monitor_task = None  # Задача мониторинга
_is_monitoring_running = True  # Флаг работы мониторинга

SHOW_INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"]
NOTIFY_EMOJI = "🔔 Активно"
DISABLED_EMOJI = "🔕 Отключено"

# ====================== СОХРАНЕНИЕ И ЗАГРУЗКА ДАННЫХ ======================
def save_settings():
    """Сохранить настройки в файл"""
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump({str(k): v for k, v in user_settings.items()}, f)
        logger.info(f"Настройки сохранены в {DATA_FILE}")
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {e}")

def load_settings():
    """Загрузить настройки из файла"""
    global user_settings
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                # Преобразуем строковые ключи обратно в int
                user_settings = {int(k): v for k, v in data.items()}
            logger.info(f"Настройки загружены из {DATA_FILE}")
    except Exception as e:
        logger.error(f"Ошибка загрузки настроек: {e}")
        user_settings = {}

# ====================== КЛАВИАТУРЫ ======================
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить алерт", callback_data="add")],
        [InlineKeyboardButton("➕ Добавить несколько монет", callback_data="add_multiple")],
        [InlineKeyboardButton("📋 Мои алерты", callback_data="list")],
        [InlineKeyboardButton("❌ Удалить алерт", callback_data="delete")],
        [InlineKeyboardButton("🔄 Обновить пары", callback_data="refresh_symbols")],
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
            InlineKeyboardButton("1000", callback_data="volbtn_1000"),
            InlineKeyboardButton("2000", callback_data="volbtn_2000"),
        ],
        [
            InlineKeyboardButton("3000", callback_data="volbtn_3000"),
            InlineKeyboardButton("5000", callback_data="volbtn_5000"),
        ],
        [
            InlineKeyboardButton("10000", callback_data="volbtn_10000"),
            InlineKeyboardButton("20000", callback_data="volbtn_20000"),
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
            f"{i+1}. {s['symbol']} {s['interval']} ≥{s['threshold']:,} USDT {status}",
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
                        return True
                    else:
                        logger.warning("Не удалось получить список пар, используем дефолтный")
                        ALL_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "XRPUSDT", "DOGEUSDT", "DOTUSDT", "AVAXUSDT"}
                else:
                    logger.warning(f"API вернул статус {r.status}")
    except Exception as e:
        logger.error(f"Ошибка загрузки символов: {e}")
        ALL_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "XRPUSDT", "DOGEUSDT", "DOTUSDT", "AVAXUSDT"}
    return False

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
    
    for attempt in range(3):  # 3 попытки
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"https://contract.mexc.com/api/v1/contract/kline/{sym}",
                    params={
                        "symbol": sym, 
                        "interval": interval_map.get(interval, "Min1"), 
                        "limit": 1
                    },
                    headers=headers,
                    timeout=ClientTimeout(total=10)
                ) as r:
                    if r.status == 200:
                        j = await r.json()
                        if j.get("success") and j.get("data", {}).get("amount"):
                            return int(float(j["data"]["amount"][0]))
                    elif r.status == 429:
                        logger.warning(f"Rate limit для {symbol}, попытка {attempt + 1}")
                        await asyncio.sleep(2 * (attempt + 1))
                    else:
                        logger.error(f"Ошибка API для {symbol}: {r.status}")
        except asyncio.TimeoutError:
            logger.warning(f"Таймаут для {symbol}, попытка {attempt + 1}")
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Ошибка получения объёма {symbol}: {e}")
            break
    return 0

# ====================== МОНИТОРИНГ (БЕЗОПАСНЫЙ) ======================
async def monitor_volumes(application: Application):
    """Фоновая задача мониторинга объёмов"""
    global _is_monitoring_running
    
    await asyncio.sleep(5)  # Небольшая задержка для инициализации
    await load_symbols()
    logger.info("Мониторинг объёмов запущен — работает 24/7")
    
    error_count = 0
    max_errors = 10
    
    while _is_monitoring_running:
        try:
            # Создаем копию для безопасной итерации
            current_settings = user_settings.copy()
            
            for chat_id, alerts in current_settings.items():
                if not alerts:
                    continue
                    
                for alert in alerts[:]:  # Копия списка
                    try:
                        if not alert.get("notifications_enabled", True):
                            continue
                            
                        vol = await fetch_volume(alert["symbol"], alert["interval"])
                        threshold = alert["threshold"]
                        last_notified = alert.get("last_notified", 0)
                        
                        # Проверяем, превышен ли порог и не отправляли ли уже уведомление
                        if vol >= threshold and vol != last_notified:
                            # Обновляем последнее уведомленное значение
                            alert["last_notified"] = vol
                            
                            # Находим соответствующий алерт в основном словаре
                            for main_alert in user_settings.get(chat_id, []):
                                if (main_alert["symbol"] == alert["symbol"] and 
                                    main_alert["interval"] == alert["interval"] and
                                    main_alert["threshold"] == alert["threshold"]):
                                    main_alert["last_notified"] = vol
                                    break
                            
                            # Сохраняем изменения
                            save_settings()
                            
                            # Отправляем уведомление
                            url = f"https://www.mexc.com/ru-RU/futures/{alert['symbol'][:-4]}_USDT"
                            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📈 Перейти на MEXC", url=url)]])
                            
                            try:
                                await application.bot.send_message(
                                    chat_id,
                                    f"<b>🚨 ВСПЛЕСК ОБЪЁМА!</b>\n\n"
                                    f"<b>Пара:</b> {alert['symbol']}\n"
                                    f"<b>Таймфрейм:</b> {alert['interval']}\n"
                                    f"<b>Порог:</b> {threshold:,} USDT\n"
                                    f"<b>Текущий объем:</b> {vol:,} USDT\n"
                                    f"<b>Превышение:</b> {(vol - threshold):,} USDT",
                                    parse_mode="HTML",
                                    reply_markup=kb
                                )
                                logger.info(f"Уведомление отправлено: {alert['symbol']} {alert['interval']} - {vol:,} USDT")
                            except Exception as e:
                                logger.error(f"Ошибка отправки сообщения: {e}")
                                
                    except Exception as e:
                        logger.error(f"Ошибка проверки алерта {alert.get('symbol', 'Unknown')}: {e}")
                        continue
            
            # Сбрасываем счетчик ошибок при успешной итерации
            error_count = 0
            await asyncio.sleep(30)  # Проверка каждые 30 секунд
            
        except asyncio.CancelledError:
            logger.info("Мониторинг остановлен (CancelledError)")
            break
        except Exception as e:
            error_count += 1
            logger.error(f"Критическая ошибка мониторинга ({error_count}/{max_errors}): {e}")
            
            if error_count >= max_errors:
                logger.error("Достигнуто максимальное количество ошибок, перезапуск мониторинга...")
                error_count = 0
                await asyncio.sleep(60)
            else:
                await asyncio.sleep(10)
    
    logger.info("Мониторинг завершен")

async def stop_monitoring():
    """Безопасная остановка мониторинга"""
    global _is_monitoring_running, _monitor_task
    _is_monitoring_running = False
    
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
        logger.info("Мониторинг корректно остановлен")

# ====================== POST_INIT И POST_STOP ======================
async def post_init(application: Application):
    """Инициализация после запуска бота"""
    global _monitor_task
    load_settings()
    await load_symbols()
    
    # Запускаем мониторинг в фоне
    _monitor_task = asyncio.create_task(monitor_volumes(application))
    logger.info("Бот инициализирован и готов к работе")

async def post_stop(application: Application):
    """Действия перед остановкой бота"""
    logger.info("Останавливаем бота...")
    await stop_monitoring()
    save_settings()
    logger.info("Бот остановлен")

# ====================== ДЕТАЛИ АЛЕРТА С ОБЪЁМАМИ ======================
async def show_alert_details_with_volumes(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    
    if chat_id not in user_settings or idx >= len(user_settings[chat_id]):
        await q.edit_message_text("⚠️ Алерт не найден", reply_markup=main_menu())
        return
    
    s = user_settings[chat_id][idx]
    symbol = s["symbol"]

    await q.edit_message_text("<b>⏳ Загружаем текущие объёмы...</b>", parse_mode="HTML")
    
    # Показываем прогресс
    progress_text = "📊 Загрузка данных:"
    progress_msg = await q.message.reply_text(progress_text)
    
    vols = {}
    for i, tf in enumerate(SHOW_INTERVALS, 1):
        try:
            vol = await fetch_volume(symbol, tf)
            vols[tf] = vol
            
            # Обновляем прогресс
            progress_emoji = "🟢" if vol > 0 else "🟡"
            progress = f"📊 Загрузка данных ({i}/{len(SHOW_INTERVALS)}):\n"
            for j, loaded_tf in enumerate(SHOW_INTERVALS[:i], 1):
                loaded_vol = vols.get(loaded_tf, 0)
                progress += f"{progress_emoji} {loaded_tf}: {loaded_vol:,}\n"
            
            await progress_msg.edit_text(progress)
            await asyncio.sleep(0.1)  # Небольшая задержка для визуализации
            
        except Exception as e:
            vols[tf] = 0
            logger.error(f"Ошибка загрузки объема {symbol} {tf}: {e}")
    
    await progress_msg.delete()
    
    status = NOTIFY_EMOJI if s.get("notifications_enabled", True) else DISABLED_EMOJI
    text = (
        f"<b>📊 Детали алерта:</b>\n\n"
        f"<b>Пара:</b> {symbol}\n"
        f"<b>Таймфрейм:</b> {s['interval']}\n"
        f"<b>Порог:</b> {s['threshold']:,} USDT\n"
        f"<b>Уведомления:</b> {status}\n\n"
        f"<b>Текущие объёмы на разных ТФ:</b>\n"
    )
    
    for tf in SHOW_INTERVALS:
        v = vols[tf]
        threshold = s["threshold"]
        
        # Эмодзи в зависимости от объема
        if v == 0:
            emoji = "🔴"  # Нет данных
        elif v >= threshold:
            emoji = "🟢"  # Превышен порог
        elif v >= threshold * 0.5:
            emoji = "🟡"  > # Близко к порогу
        else:
            emoji = "🔴"  # Далеко от порога
            
        text += f"{emoji} <code>{tf.rjust(3)}</code> → <b>{v:,} USDT</b>\n"
    
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 MEXC", url=f"https://www.mexc.com/ru-RU/futures/{symbol[:-4]}_USDT"),
            InlineKeyboardButton(f"Уведомления: {'Вкл' if s.get('notifications_enabled', True) else 'Выкл'}", 
                               callback_data=f"toggle_notify_{idx}")
        ],
        [
            InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_{idx}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"del_{idx}")
        ],
        [InlineKeyboardButton("🔙 Назад", callback_data="list")],
    ])
    
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

# ====================== ОБРАБОТЧИКИ ======================
async def any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("🚫 Доступ запрещён")
        return

    chat_id = update.effective_chat.id
    user_settings.setdefault(chat_id, [])
    text = (update.message.text or "").strip()

    if not text or any(w in text.lower() for w in ["меню", "start", "привет", "/start"]):
        await update.message.reply_text(
            "🔥 <b>MEXC Volume Tracker Pro</b> 🔥\n\n"
            "📈 Отслеживание объемов в реальном времени\n"
            "🔔 Мгновенные уведомления о всплесках\n"
            "📊 Поддержка множества монет\n"
            "⚡ Работает 24/7 без перерывов\n\n"
            f"<b>Доступно пар:</b> {len(ALL_SYMBOLS)}\n"
            f"<b>Ваших алертов:</b> {len(user_settings.get(chat_id, []))}\n\n"
            "Выберите действие:",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    state = user_state.get(chat_id)
    
    if state == "wait_symbol":
        sym = text.upper().strip()
        if not sym.endswith("USDT"):
            sym += "USDT"
        
        if sym not in ALL_SYMBOLS:
            # Предлагаем похожие символы
            suggestions = [s for s in ALL_SYMBOLS if sym[:-4] in s][:5]
            suggestions_text = "\n".join(suggestions) if suggestions else "Нет похожих пар"
            
            await update.message.reply_text(
                f"⚠️ Пара <b>{sym}</b> не найдена\n\n"
                f"<b>Похожие пары:</b>\n{suggestions_text}\n\n"
                f"Попробуйте еще раз или нажмите /start",
                parse_mode="HTML"
            )
            return
        
        user_temp[chat_id] = {"symbol": sym}
        user_state[chat_id] = "wait_interval"
        await update.message.reply_text(
            f"✅ Пара: <b>{sym}</b>\n"
            f"Выберите таймфрейм для отслеживания:",
            parse_mode="HTML", 
            reply_markup=intervals_kb()
        )
        return
    
    elif state == "wait_multiple_symbols":
        symbols_input = text.upper().strip()
        symbols_list = []
        
        # Обрабатываем разные форматы ввода
        for sym in symbols_input.replace(',', ' ').replace('\n', ' ').split():
            sym = sym.strip()
            if not sym:
                continue
                
            if not sym.endswith("USDT"):
                sym += "USDT"
                
            if sym in ALL_SYMBOLS:
                symbols_list.append(sym)
            else:
                await update.message.reply_text(f"⚠️ Пара {sym} не найдена и будет пропущена")
        
        if not symbols_list:
            await update.message.reply_text("❌ Не найдено ни одной валидной пары")
            user_state.pop(chat_id, None)
            return
        
        user_temp[chat_id]["symbols"] = symbols_list
        user_state[chat_id] = "wait_multiple_interval"
        
        await update.message.reply_text(
            f"✅ Найдено пар: <b>{len(symbols_list)}</b>\n"
            f"<code>{', '.join(symbols_list)}</code>\n\n"
            f"Выберите таймфрейм для всех пар:",
            parse_mode="HTML",
            reply_markup=intervals_kb()
        )
        return
    
    elif state in ["wait_threshold", "edit_threshold", "wait_threshold_custom", "edit_threshold_custom"]:
        try:
            # Извлекаем числа из текста
            import re
            numbers = re.findall(r'\d+', text)
            if not numbers:
                raise ValueError
            threshold_value = int(''.join(numbers[:10]))  # Берем первое число
            if threshold_value < 1000:
                await update.message.reply_text("⚠️ Минимальный порог 1000 USDT")
                return
        except:
            await update.message.reply_text("⚠️ Введите число ≥ 1000 (например: 10000 или 10,000)")
            return

        is_edit = state in ["edit_threshold", "edit_threshold_custom"]
        
        if "symbols" in user_temp.get(chat_id, {}):
            # Добавление нескольких монет
            symbols = user_temp[chat_id]["symbols"]
            interval = user_temp[chat_id]["interval"]
            added_count = 0
            
            for sym in symbols:
                # Проверяем, нет ли уже такого алерта
                existing = False
                for alert in user_settings.get(chat_id, []):
                    if alert["symbol"] == sym and alert["interval"] == interval:
                        existing = True
                        break
                
                if not existing:
                    alert = {
                        "symbol": sym,
                        "interval": interval,
                        "threshold": threshold_value,
                        "last_notified": 0,
                        "notifications_enabled": True,
                    }
                    user_settings[chat_id].append(alert)
                    added_count += 1
            
            msg = (f"✅ Добавлено <b>{added_count}</b> алертов!\n"
                   f"<b>Таймфрейм:</b> {interval}\n"
                   f"<b>Порог:</b> {threshold_value:,} USDT\n"
                   f"<b>Всего алертов:</b> {len(user_settings[chat_id])}")
            
        elif is_edit:
            # Редактирование существующего алерта
            idx = user_temp[chat_id]["edit_idx"]
            user_settings[chat_id][idx]["threshold"] = threshold_value
            msg = (f"✅ Алерт обновлён!\n"
                   f"<b>{user_settings[chat_id][idx]['symbol']} {user_settings[chat_id][idx]['interval']}</b>\n"
                   f"Порог: {threshold_value:,} USDT")
        else:
            # Добавление одного алерта
            alert = {
                "symbol": user_temp[chat_id]["symbol"],
                "interval": user_temp[chat_id]["interval"],
                "threshold": threshold_value,
                "last_notified": 0,
                "notifications_enabled": True,
            }
            user_settings[chat_id].append(alert)
            msg = (f"✅ Алерт добавлен!\n"
                   f"<b>{alert['symbol']} {alert['interval']}</b>\n"
                   f"Порог: {threshold_value:,} USDT\n"
                   f"<b>Всего алертов:</b> {len(user_settings[chat_id])}")
        
        # Сохраняем настройки
        save_settings()
        
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
    user_settings.setdefault(chat_id, [])

    if data == "back":
        user_state.pop(chat_id, None)
        user_temp.pop(chat_id, None)
        await q.edit_message_text("Главное меню", reply_markup=main_menu())
        return

    if data == "add":
        user_state[chat_id] = "wait_symbol"
        await q.edit_message_text(
            "Введите тикер монеты (например: BTC, ETH, SOL):\n\n"
            "<i>Примечание: монета должна торговаться на MEXC с USDT</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="back")]])
        )
        return
    
    if data == "add_multiple":
        user_state[chat_id] = "wait_multiple_symbols"
        user_temp[chat_id] = {}
        await q.edit_message_text(
            "Введите несколько тикеров через пробел или запятую:\n\n"
            "<i>Пример: BTC ETH SOL ADA DOT</i>\n"
            "<i>Или: BTC, ETH, SOL, ADA, DOT</i>\n\n"
            "Бот автоматически добавит USDT и проверит наличие пар.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="back")]])
        )
        return

    if data == "refresh_symbols":
        await q.answer("Обновляем список пар...", show_alert=False)
        success = await load_symbols()
        if success:
            await q.edit_message_text(
                f"✅ Список пар обновлен!\n"
                f"<b>Доступно:</b> {len(ALL_SYMBOLS)} пар",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
        else:
            await q.edit_message_text(
                "⚠️ Не удалось обновить список пар\n"
                "Используется локальный список",
                reply_markup=main_menu()
            )
        return

    if data == "list":
        alerts_count = len(user_settings.get(chat_id, []))
        text = (f"📋 Ваши активные алерты: <b>{alerts_count}</b>\n\n"
                "<i>Нажмите на алерт для деталей и управления</i>" 
                if alerts_count > 0 else "ℹ️ У вас нет активных алертов")
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=list_kb(chat_id))
        return

    if data == "delete":
        if not user_settings.get(chat_id):
            await q.edit_message_text("ℹ️ Нет алертов для удаления", reply_markup=main_menu())
            return
        kb = []
        for i, s in enumerate(user_settings[chat_id]):
            status = "🔔" if s.get("notifications_enabled", True) else "🔕"
            kb.append([InlineKeyboardButton(
                f"{i+1}. {s['symbol']} {s['interval']} ≥{s['threshold']:,} {status}", 
                callback_data=f"del_{i}"
            )])
        kb.append([InlineKeyboardButton("🔙 Назад", callback_data="list")])
        await q.edit_message_text("❌ Выберите алерт для удаления:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("del_"):
        idx = int(data.split("_")[1])
        if idx < len(user_settings[chat_id]):
            deleted = user_settings[chat_id].pop(idx)
            save_settings()
            await q.edit_message_text(
                f"✅ Алерт удалён:\n"
                f"<b>{deleted['symbol']} {deleted['interval']}</b>\n"
                f"Порог: {deleted['threshold']:,} USDT",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
        else:
            await q.edit_message_text("⚠️ Алерт не найден", reply_markup=main_menu())
        return

    if data.startswith("alert_options_"):
        idx = int(data.split("_")[2])
        await show_alert_details_with_volumes(update, context, idx)
        return

    if data.startswith("toggle_notify_"):
        idx = int(data.split("_")[2])
        if idx < len(user_settings[chat_id]):
            s = user_settings[chat_id][idx]
            s["notifications_enabled"] = not s.get("notifications_enabled", True)
            save_settings()
            await show_alert_details_with_volumes(update, context, idx)
        else:
            await q.edit_message_text("⚠️ Алерт не найден", reply_markup=main_menu())
        return
    
    if data.startswith("edit_"):
        idx = int(data.split("_")[1])
        if idx < len(user_settings[chat_id]):
            user_state[chat_id] = "edit_interval"
            user_temp[chat_id] = {"edit_idx": idx}
            await q.edit_message_text(
                f"✏️ Редактирование алерта:\n"
                f"<b>{user_settings[chat_id][idx]['symbol']}</b>\n\n"
                f"Выберите новый таймфрейм:",
                parse_mode="HTML",
                reply_markup=intervals_kb()
            )
        else:
            await q.edit_message_text("⚠️ Алерт не найден", reply_markup=main_menu())
        return

    if data.startswith("int_"):
        interval = data.split("_")[1]
        
        if "symbols" in user_temp.get(chat_id, {}):
            # Для нескольких монет
            user_temp[chat_id]["interval"] = interval
            user_state[chat_id] = "wait_threshold"
            await q.edit_message_text(
                f"✅ Таймфрейм для всех пар: <b>{interval}</b>\n"
                f"<b>Количество пар:</b> {len(user_temp[chat_id]['symbols'])}\n\n"
                f"Выберите порог объема:",
                parse_mode="HTML",
                reply_markup=volume_kb()
            )
        elif user_state.get(chat_id) == "edit_interval":
            # Редактирование таймфрейма
            idx = user_temp[chat_id]["edit_idx"]
            user_settings[chat_id][idx]["interval"] = interval
            user_state[chat_id] = "edit_threshold"
            await q.edit_message_text(
                f"🆕 Новый таймфрейм: <b>{interval}</b>\n"
                f"<b>Пара:</b> {user_settings[chat_id][idx]['symbol']}\n\n"
                f"Выберите порог объема:",
                parse_mode="HTML",
                reply_markup=volume_kb()
            )
        else:
            # Обычное добавление
            user_temp[chat_id]["interval"] = interval
            user_state[chat_id] = "wait_threshold"
            await q.edit_message_text(
                f"✅ Таймфрейм: <b>{interval}</b>\n"
                f"<b>Пара:</b> {user_temp[chat_id]['symbol']}\n\n"
                f"Выберите порог объема:",
                parse_mode="HTML",
                reply_markup=volume_kb()
            )
        return

    if data.startswith("volbtn_"):
        volume = int(data.split("_")[1])
        
        if "symbols" in user_temp.get(chat_id, {}):
            # Добавление нескольких алертов
            symbols = user_temp[chat_id]["symbols"]
            interval = user_temp[chat_id]["interval"]
            added_count = 0
            
            for sym in symbols:
                # Проверяем, нет ли уже такого алерта
                existing = False
                for alert in user_settings.get(chat_id, []):
                    if alert["symbol"] == sym and alert["interval"] == interval:
                        existing = True
                        break
                
                if not existing:
                    alert = {
                        "symbol": sym,
                        "interval": interval,
                        "threshold": volume,
                        "last_notified": 0,
                        "notifications_enabled": True,
                    }
                    user_settings[chat_id].append(alert)
                    added_count += 1
            
            save_settings()
            await q.edit_message_text(
                f"✅ Добавлено <b>{added_count}</b> алертов!\n\n"
                f"<b>Таймфрейм:</b> {interval}\n"
                f"<b>Порог:</b> {volume:,} USDT\n"
                f"<b>Всего алертов:</b> {len(user_settings[chat_id])}",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            user_state.pop(chat_id, None)
            user_temp.pop(chat_id, None)
            
        elif user_state.get(chat_id) == "edit_threshold":
            # Редактирование порога
            idx = user_temp[chat_id]["edit_idx"]
            user_settings[chat_id][idx]["threshold"] = volume
            save_settings()
            await q.edit_message_text(
                f"✅ Алерт обновлён!\n\n"
                f"<b>{user_settings[chat_id][idx]['symbol']} {user_settings[chat_id][idx]['interval']}</b>\n"
                f"Порог: {volume:,} USDT",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            user_state.pop(chat_id, None)
            user_temp.pop(chat_id, None)
        else:
            # Обычное добавление
            alert = {
                "symbol": user_temp[chat_id]["symbol"],
                "interval": user_temp[chat_id]["interval"],
                "threshold": volume,
                "last_notified": 0,
                "notifications_enabled": True,
            }
            user_settings[chat_id].append(alert)
            save_settings()
            await q.edit_message_text(
                f"✅ Алерт добавлен!\n\n"
                f"<b>{alert['symbol']} {alert['interval']}</b>\n"
                f"Порог: {volume:,} USDT\n"
                f"<b>Всего алертов:</b> {len(user_settings[chat_id])}",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            user_state.pop(chat_id, None)
            user_temp.pop(chat_id, None)
        return

    if data == "vol_custom":
        if "symbols" in user_temp.get(chat_id, {}):
            state_prefix = "wait_threshold"
        elif user_state.get(chat_id) == "edit_threshold":
            state_prefix = "edit_threshold"
        else:
            state_prefix = "wait_threshold"
            
        user_state[chat_id] = f"{state_prefix}_custom"
        await q.edit_message_text(
            "✏️ Введите порог объема в USDT:\n\n"
            "<i>Пример: 10000 или 25,000</i>\n"
            "<i>Минимальный порог: 1,000 USDT</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]])
        )
        return
    
    if data == "refresh_all":
        await q.answer("Обновляем все алерты...", show_alert=False)
        await q.edit_message_text("🔄 Обновление данных...", reply_markup=list_kb(chat_id))
        return

# ====================== ВЕБ-СЕРВЕР ДЛЯ RENDER ======================
web_app = FastAPI()

@web_app.get("/")
async def root():
    total_alerts = sum(len(alerts) for alerts in user_settings.values())
    return {
        "status": "MEXC Volume Bot работает 24/7",
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "symbols_available": len(ALL_SYMBOLS),
        "total_alerts": total_alerts,
        "users": len(user_settings)
    }

@web_app.get("/health")
async def health():
    return {"status": "healthy", "monitoring": _is_monitoring_running}

def run_web_server():
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        web_app, 
        host="0.0.0.0", 
        port=port, 
        log_level="error",
        access_log=False
    )

# ====================== ЗАПУСК ======================
def run_bot():
    try:
        application = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .post_init(post_init)
            .post_stop(post_stop)
            .concurrent_updates(True)
            .build()
        )

        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_message))
        application.add_handler(CallbackQueryHandler(button_handler))

        logger.info("=" * 50)
        logger.info("MEXC Volume Bot запускается...")
        logger.info(f"Доступ для пользователя: {ALLOWED_USER_ID}")
        logger.info(f"Токен API: {'Установлен' if MEXC_API_KEY else 'НЕ УСТАНОВЛЕН!'}")
        logger.info("=" * 50)

        # Запускаем веб-сервер в отдельном потоке
        web_thread = threading.Thread(target=run_web_server, daemon=True)
        web_thread.start()
        logger.info(f"Веб-сервер запущен на порту {os.environ.get('PORT', 8000)}")

        application.run_polling(
            drop_pending_updates=True,
            timeout=30,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False  # Не закрываем event loop самостоятельно
        )
        
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске бота: {e}")
        raise

if __name__ == "__main__":
    # Устанавливаем политику event loop для Windows
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    run_bot()

















import os
import time
import hmac
import hashlib
import logging
import aiohttp
import asyncio
import json
import sys
import psutil
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
import re

# ====================== ПРОВЕРКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ======================
REQUIRED_ENV_VARS = ['TELEGRAM_TOKEN', 'ALLOWED_USER_ID', 'MEXC_API_KEY', 'MEXC_SECRET_KEY']
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]

if missing_vars:
    print(f"❌ ОШИБКА: Отсутствуют переменные окружения: {', '.join(missing_vars)}")
    print("Добавьте их в настройках Render Dashboard → Environment")
    sys.exit(1)

# ====================== НАСТРОЙКИ ПУТЕЙ ДЛЯ RENDER ======================
load_dotenv()

# Определяем путь для сохранения данных
if os.environ.get('RENDER'):
    DATA_DIR = '/opt/render/project/src/data'
    os.makedirs(DATA_DIR, exist_ok=True)
    DATA_FILE = os.path.join(DATA_DIR, 'alerts.json')
else:
    DATA_DIR = 'data'
    os.makedirs(DATA_DIR, exist_ok=True)
    DATA_FILE = os.path.join(DATA_DIR, 'alerts.json')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")
IS_RENDER = os.environ.get('RENDER', False)

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

SHOW_INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"]
NOTIFY_EMOJI = "🔔 Активно"
DISABLED_EMOJI = "🔕 Отключено"

# Глобальные задачи
_monitor_task = None
_heartbeat_task = None
_is_monitoring_running = True
_start_time = time.time()

# ====================== СОХРАНЕНИЕ И ЗАГРУЗКА ДАННЫХ ======================
def save_settings():
    """Сохранить настройки в файл"""
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump({str(k): v for k, v in user_settings.items()}, f, ensure_ascii=False, indent=2)
        logger.debug(f"Настройки сохранены в {DATA_FILE}")
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {e}")

def load_settings():
    """Загрузить настройки из файла"""
    global user_settings
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Преобразуем строковые ключи обратно в int
                user_settings = {int(k): v for k, v in data.items()}
            logger.info(f"Загружено {sum(len(v) for v in user_settings.values())} алертов из {DATA_FILE}")
        else:
            user_settings = {}
            logger.info("Файл с настройками не найден, создаем новый")
    except Exception as e:
        logger.error(f"Ошибка загрузки настроек: {e}")
        user_settings = {}

# ====================== HEARTBEAT ДЛЯ RENDER ======================
async def heartbeat():
    """Периодический пинг для поддержания активности на Render"""
    logger.info("❤️ Heartbeat система запущена")
    
    while True:
        try:
            # 1. Пинг собственного эндпоинта
            port = os.environ.get('PORT', 8000)
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(f"http://localhost:{port}/health", timeout=5) as resp:
                        if resp.status == 200:
                            logger.debug(f"Heartbeat: сервис активен (HTTP {resp.status})")
                        else:
                            logger.warning(f"Heartbeat: сервис отвечает с кодом {resp.status}")
                except Exception as e:
                    logger.warning(f"Heartbeat: ошибка подключения к localhost: {e}")
            
            # 2. Периодическая проверка MEXC API
            if len(ALL_SYMBOLS) < 10:
                logger.info("Heartbeat: обновляем список пар...")
                await load_symbols()
                
            # 3. Логирование статистики
            try:
                process = psutil.Process()
                memory_mb = process.memory_info().rss / 1024 / 1024
                cpu_percent = process.cpu_percent()
                
                total_alerts = sum(len(alerts) for alerts in user_settings.values())
                logger.info(
                    f"📊 Статистика: {memory_mb:.1f}MB RAM, "
                    f"{cpu_percent:.1f}% CPU, "
                    f"{len(ALL_SYMBOLS)} пар, "
                    f"{total_alerts} алертов"
                )
            except:
                pass
            
            # 4. Сохранение настроек каждые 30 минут
            if int(time.time() - _start_time) % 1800 < 60:  # Каждые ~30 минут
                save_settings()
                logger.info("Heartbeat: автосохранение настроек")
            
            await asyncio.sleep(300)  # Каждые 5 минут
            
        except asyncio.CancelledError:
            logger.info("Heartbeat: получен сигнал остановки")
            break
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(60)

# ====================== КЛАВИАТУРЫ ======================
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить алерт", callback_data="add")],
        [InlineKeyboardButton("➕ Добавить несколько монет", callback_data="add_multiple")],
        [InlineKeyboardButton("📋 Мои алерты", callback_data="list")],
        [InlineKeyboardButton("❌ Удалить алерт", callback_data="delete")],
        [InlineKeyboardButton("🔄 Обновить пары", callback_data="refresh_symbols")],
        [InlineKeyboardButton("📊 Статус", callback_data="status")],
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
            async with s.get("https://contract.mexc.com/api/v1/contract/detail", 
                           timeout=ClientTimeout(total=10)) as r:
                if r.status == 200:
                    j = await r.json()
                    if j.get("success") and j.get("data"):
                        symbols = {x["symbol"].replace("_USDT", "USDT") for x in j["data"] if "_USDT" in x["symbol"]}
                        ALL_SYMBOLS = symbols
                        logger.info(f"Загружено {len(ALL_SYMBOLS)} пар")
                        return True
                    else:
                        logger.warning("Не удалось получить список пар из API")
                else:
                    logger.warning(f"API вернул статус {r.status}")
    except Exception as e:
        logger.error(f"Ошибка загрузки символов: {e}")
    
    # Если не удалось загрузить, используем дефолтный список
    if len(ALL_SYMBOLS) < 50:
        ALL_SYMBOLS = {
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", 
            "XRPUSDT", "DOGEUSDT", "DOTUSDT", "AVAXUSDT", "LINKUSDT",
            "MATICUSDT", "SHIBUSDT", "TRXUSDT", "UNIUSDT", "ATOMUSDT",
            "LTCUSDT", "XLMUSDT", "ALGOUSDT", "VETUSDT", "FILUSDT"
        }
        logger.info(f"Используется дефолтный список из {len(ALL_SYMBOLS)} пар")
    
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
    
    for attempt in range(2):  # 2 попытки
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
                            amount = j["data"]["amount"][0]
                            if amount:  # Проверяем, что значение не None или пустое
                                return int(float(amount))
                    elif r.status == 429:
                        logger.warning(f"Rate limit для {symbol}, попытка {attempt + 1}")
                        await asyncio.sleep(1)
                    else:
                        logger.debug(f"Ошибка API для {symbol}: {r.status}")
        except asyncio.TimeoutError:
            logger.debug(f"Таймаут для {symbol}, попытка {attempt + 1}")
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.debug(f"Ошибка получения объёма {symbol}: {e}")
            break
    
    return 0

# ====================== МОНИТОРИНГ (БЕЗОПАСНЫЙ) ======================
async def monitor_volumes(application: Application):
    """Фоновая задача мониторинга объёмов"""
    global _is_monitoring_running
    
    await asyncio.sleep(3)  # Небольшая задержка для инициализации
    logger.info("📈 Мониторинг объёмов запущен — работает 24/7")
    
    error_count = 0
    max_errors = 5
    check_interval = 30  # секунд
    
    while _is_monitoring_running:
        try:
            current_time = time.time()
            
            # Проверяем, есть ли алерты для мониторинга
            total_alerts = sum(len(alerts) for alerts in user_settings.values())
            if total_alerts == 0:
                logger.debug("Нет алертов для мониторинга, ждем...")
                await asyncio.sleep(check_interval)
                continue
            
            # Мониторим каждый алерт
            notifications_sent = 0
            for chat_id, alerts in list(user_settings.items()):
                if not alerts:
                    continue
                    
                for alert in alerts:
                    try:
                        # Пропускаем отключенные алерты
                        if not alert.get("notifications_enabled", True):
                            continue
                        
                        # Получаем текущий объем
                        vol = await fetch_volume(alert["symbol"], alert["interval"])
                        if vol == 0:
                            continue  # Пропускаем если нет данных
                        
                        threshold = alert["threshold"]
                        last_notified = alert.get("last_notified", 0)
                        
                        # Проверяем условие срабатывания
                        if vol >= threshold and vol != last_notified:
                            # Обновляем последнее уведомленное значение
                            alert["last_notified"] = vol
                            notifications_sent += 1
                            
                            # Отправляем уведомление
                            url = f"https://www.mexc.com/ru-RU/futures/{alert['symbol'][:-4]}_USDT"
                            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📈 Перейти на MEXC", url=url)]])
                            
                            message = (
                                f"<b>🚨 ВСПЛЕСК ОБЪЁМА!</b>\n\n"
                                f"<b>Пара:</b> {alert['symbol']}\n"
                                f"<b>Таймфрейм:</b> {alert['interval']}\n"
                                f"<b>Порог:</b> {threshold:,} USDT\n"
                                f"<b>Текущий объем:</b> {vol:,} USDT\n"
                                f"<b>Превышение:</b> {(vol - threshold):,} USDT"
                            )
                            
                            try:
                                await application.bot.send_message(
                                    chat_id,
                                    message,
                                    parse_mode="HTML",
                                    reply_markup=kb
                                )
                                logger.info(f"Уведомление: {alert['symbol']} {alert['interval']} - {vol:,} USDT")
                            except Exception as e:
                                logger.error(f"Ошибка отправки сообщения: {e}")
                                
                    except Exception as e:
                        logger.debug(f"Ошибка в алерте {alert.get('symbol', 'Unknown')}: {e}")
                        continue
            
            if notifications_sent > 0:
                logger.info(f"📨 Отправлено уведомлений: {notifications_sent}")
            
            # Сохраняем настройки если были изменения
            if notifications_sent > 0:
                save_settings()
            
            # Сбрасываем счетчик ошибок при успешной итерации
            error_count = 0
            await asyncio.sleep(check_interval)
            
        except asyncio.CancelledError:
            logger.info("Мониторинг остановлен (CancelledError)")
            break
        except Exception as e:
            error_count += 1
            logger.error(f"Ошибка мониторинга ({error_count}/{max_errors}): {e}")
            
            if error_count >= max_errors:
                logger.error(f"Много ошибок, увеличиваем интервал проверки")
                await asyncio.sleep(300)  # 5 минут при множественных ошибках
                error_count = 0
            else:
                await asyncio.sleep(60)  # 1 минута при ошибке
    
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
    global _monitor_task, _heartbeat_task, _start_time
    
    logger.info("=" * 60)
    logger.info(f"🚀 Запуск MEXC Volume Bot")
    logger.info(f"📍 Хост: {'Render.com' if IS_RENDER else 'Локальный'}")
    logger.info(f"👤 Разрешенный ID: {ALLOWED_USER_ID}")
    logger.info(f"🌐 Порт: {os.environ.get('PORT', 8000)}")
    logger.info("=" * 60)
    
    # Загружаем настройки
    load_settings()
    
    # Загружаем символы
    await load_symbols()
    
    # Запускаем мониторинг
    _monitor_task = asyncio.create_task(monitor_volumes(application))
    logger.info("✅ Мониторинг объемов запущен")
    
    # Запускаем heartbeat на Render
    if IS_RENDER:
        _heartbeat_task = asyncio.create_task(heartbeat())
        logger.info("✅ Heartbeat система активирована")
    
    # Отправляем сообщение о запуске
    try:
        total_alerts = sum(len(alerts) for alerts in user_settings.values())
        await application.bot.send_message(
            ALLOWED_USER_ID,
            f"🤖 <b>MEXC Bot запущен!</b>\n\n"
            f"📍 <b>Хост:</b> {'Render.com' if IS_RENDER else 'Локальный'}\n"
            f"⏰ <b>Время:</b> {time.strftime('%H:%M:%S')}\n"
            f"📊 <b>Пар доступно:</b> {len(ALL_SYMBOLS)}\n"
            f"🔔 <b>Алертов:</b> {total_alerts}\n"
            f"🔄 <b>Состояние:</b> Активно 24/7\n\n"
            f"Используйте /start для управления",
            parse_mode="HTML"
        )
        logger.info("Стартовое сообщение отправлено пользователю")
    except Exception as e:
        logger.error(f"Не удалось отправить стартовое сообщение: {e}")

async def post_stop(application: Application):
    """Действия перед остановкой бота"""
    global _heartbeat_task
    
    logger.info("🛑 Останавливаем бота...")
    
    # Останавливаем heartbeat
    if _heartbeat_task and not _heartbeat_task.done():
        _heartbeat_task.cancel()
        try:
            await _heartbeat_task
        except asyncio.CancelledError:
            pass
        logger.info("Heartbeat остановлен")
    
    # Останавливаем мониторинг
    await stop_monitoring()
    
    # Сохраняем настройки
    save_settings()
    
    # Отправляем сообщение об остановке
    try:
        total_alerts = sum(len(alerts) for alerts in user_settings.values())
        await application.bot.send_message(
            ALLOWED_USER_ID,
            f"🛑 <b>MEXC Bot остановлен</b>\n\n"
            f"⏰ <b>Время:</b> {time.strftime('%H:%M:%S')}\n"
            f"📊 <b>Алертов сохранено:</b> {total_alerts}\n"
            f"⏱ <b>Аптайм:</b> {int(time.time() - _start_time) // 3600}ч {int((time.time() - _start_time) % 3600) // 60}м\n\n"
            f"Бот будет перезапущен автоматически 🔄",
            parse_mode="HTML"
        )
        logger.info("Сообщение об остановке отправлено")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение об остановке: {e}")
    
    logger.info("Бот корректно остановлен")

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
            progress = f"📊 Загрузка данных ({i}/{len(SHOW_INTERVALS)}):\n"
            for j, loaded_tf in enumerate(SHOW_INTERVALS[:i], 1):
                loaded_vol = vols.get(loaded_tf, 0)
                emoji = "🟢" if loaded_vol > 0 else "🟡"
                progress += f"{emoji} {loaded_tf}: {loaded_vol:,}\n"
            
            await progress_msg.edit_text(progress)
            await asyncio.sleep(0.1)
            
        except Exception as e:
            vols[tf] = 0
            logger.debug(f"Ошибка загрузки объема {symbol} {tf}: {e}")
    
    await progress_msg.delete()
    
    status = NOTIFY_EMOJI if s.get("notifications_enabled", True) else DISABLED_EMOJI
    text = (
        f"<b>📊 Детали алерта:</b>\n\n"
        f"<b>Пара:</b> {symbol}\n"
        f"<b>Таймфрейм:</b> {s['interval']}\n"
        f"<b>Порог:</b> {s['threshold']:,} USDT\n"
        f"<b>Уведомления:</b> {status}\n"
        f"<b>Последний объем:</b> {vols.get(s['interval'], 0):,} USDT\n\n"
        f"<b>Объёмы на разных ТФ:</b>\n"
    )
    
    for tf in SHOW_INTERVALS:
        v = vols[tf]
        threshold = s["threshold"]
        
        # Эмодзи в зависимости от объема
        if v == 0:
            emoji = "⚪"  # Нет данных
        elif v >= threshold:
            emoji = "🟢"  # Превышен порог
        elif v >= threshold * 0.7:
            emoji = "🟡"  # Близко к порогу
        elif v >= threshold * 0.3:
            emoji = "🟠"  # Средний уровень
        else:
            emoji = "🔴"  # Низкий уровень
            
        text += f"{emoji} <code>{tf.rjust(3)}</code> → <b>{v:,} USDT</b>\n"
    
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 MEXC", url=f"https://www.mexc.com/ru-RU/futures/{symbol[:-4]}_USDT"),
            InlineKeyboardButton(f"{'🔔' if s.get('notifications_enabled', True) else '🔕'} Увед.", 
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
        total_alerts = sum(len(alerts) for alerts in user_settings.values())
        user_alerts = len(user_settings.get(chat_id, []))
        
        await update.message.reply_text(
            "🔥 <b>MEXC Volume Tracker Pro</b> 🔥\n\n"
            "📈 Отслеживание объемов в реальном времени\n"
            "🔔 Мгновенные уведомления о всплесках\n"
            "📊 Поддержка множества монет\n"
            "⚡ Работает 24/7 без перерывов\n\n"
            f"<b>📍 Хост:</b> {'Render.com' if IS_RENDER else 'Локальный'}\n"
            f"<b>📊 Пар доступно:</b> {len(ALL_SYMBOLS)}\n"
            f"<b>🔔 Всего алертов:</b> {total_alerts}\n"
            f"<b>👤 Ваших алертов:</b> {user_alerts}\n\n"
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
            suggestions = [s for s in ALL_SYMBOLS if sym[:-4].lower() in s.lower()][:5]
            suggestions_text = "\n".join(suggestions) if suggestions else "Нет похожих пар"
            
            await update.message.reply_text(
                f"⚠️ Пара <b>{sym}</b> не найдена\n\n"
                f"<b>Похожие пары:</b>\n{suggestions_text}\n\n"
                f"Введите тикер снова или нажмите /start",
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
            f"<code>{', '.join(symbols_list[:10])}</code>"
            f"{'...' if len(symbols_list) > 10 else ''}\n\n"
            f"Выберите таймфрейм для всех пар:",
            parse_mode="HTML",
            reply_markup=intervals_kb()
        )
        return
    
    elif state in ["wait_threshold", "edit_threshold", "wait_threshold_custom", "edit_threshold_custom"]:
        try:
            # Извлекаем числа из текста
            numbers = re.findall(r'\d+', text.replace(',', '').replace(' ', ''))
            if not numbers:
                raise ValueError
            threshold_value = int(numbers[0])
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
            
            save_settings()
            msg = (f"✅ Добавлено <b>{added_count}</b> алертов!\n"
                   f"<b>Таймфрейм:</b> {interval}\n"
                   f"<b>Порог:</b> {threshold_value:,} USDT\n"
                   f"<b>Всего алертов:</b> {len(user_settings[chat_id])}")
            
        elif is_edit:
            # Редактирование существующего алерта
            idx = user_temp[chat_id]["edit_idx"]
            user_settings[chat_id][idx]["threshold"] = threshold_value
            save_settings()
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
            save_settings()
            msg = (f"✅ Алерт добавлен!\n"
                   f"<b>{alert['symbol']} {alert['interval']}</b>\n"
                   f"Порог: {threshold_value:,} USDT\n"
                   f"<b>Всего алертов:</b> {len(user_settings[chat_id])}")
        
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

    if data == "status":
        total_alerts = sum(len(alerts) for alerts in user_settings.values())
        uptime_seconds = int(time.time() - _start_time)
        hours = uptime_seconds // 3600
        minutes = (uptime_seconds % 3600) // 60
        
        try:
            memory_mb = psutil.Process().memory_info().rss / 1024 / 1024
        except:
            memory_mb = 0
        
        status_text = (
            f"<b>📊 Статус системы</b>\n\n"
            f"📍 <b>Хост:</b> {'Render.com' if IS_RENDER else 'Локальный'}\n"
            f"⏱ <b>Аптайм:</b> {hours}ч {minutes}м\n"
            f"💾 <b>Память:</b> {memory_mb:.1f} MB\n"
            f"📈 <b>Пар доступно:</b> {len(ALL_SYMBOLS)}\n"
            f"🔔 <b>Всего алертов:</b> {total_alerts}\n"
            f"👤 <b>Активных пользователей:</b> {len(user_settings)}\n"
            f"🔄 <b>Мониторинг:</b> {'Активен ✅' if _is_monitoring_running else 'Остановлен ❌'}\n"
            f"❤️ <b>Heartbeat:</b> {'Активен ✅' if IS_RENDER and _heartbeat_task and not _heartbeat_task.done() else 'Неактивен'}\n\n"
            f"<i>Последнее обновление: {time.strftime('%H:%M:%S')}</i>"
        )
        
        await q.edit_message_text(status_text, parse_mode="HTML", reply_markup=main_menu())
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
    uptime_seconds = int(time.time() - _start_time)
    hours = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60
    
    return {
        "status": "MEXC Volume Bot работает 24/7",
        "host": "Render.com" if IS_RENDER else "Local",
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime": f"{hours}h {minutes}m",
        "symbols_available": len(ALL_SYMBOLS),
        "total_alerts": total_alerts,
        "users": len(user_settings),
        "monitoring_active": _is_monitoring_running,
        "version": "2.0"
    }

@web_app.get("/health")
async def health():
    """Эндпоинт для проверки здоровья (используется Render и heartbeat)"""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "monitoring": _is_monitoring_running,
        "memory_usage_mb": psutil.Process().memory_info().rss / 1024 / 1024 if hasattr(psutil, 'Process') else 0
    }

@web_app.get("/stats")
async def stats():
    """Детальная статистика"""
    total_alerts = sum(len(alerts) for alerts in user_settings.values())
    alerts_by_symbol = {}
    for alerts in user_settings.values():
        for alert in alerts:
            symbol = alert['symbol']
            alerts_by_symbol[symbol] = alerts_by_symbol.get(symbol, 0) + 1
    
    return {
        "total_alerts": total_alerts,
        "unique_symbols": len(alerts_by_symbol),
        "top_symbols": dict(sorted(alerts_by_symbol.items(), key=lambda x: x[1], reverse=True)[:10]),
        "users_count": len(user_settings),
        "all_symbols_count": len(ALL_SYMBOLS),
        "start_time": _start_time,
        "uptime_seconds": int(time.time() - _start_time)
    }

def run_web_server():
    """Запуск веб-сервера в отдельном потоке"""
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"🌐 Запуск веб-сервера на порту {port}")
    
    # Настройка uvicorn для работы в Render
    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
        timeout_keep_alive=30
    )
    server = uvicorn.Server(config)
    
    # Запускаем сервер
    asyncio.run(server.serve())

# ====================== ЗАПУСК БОТА ======================
def run_bot():
    """Основная функция запуска бота"""
    try:
        logger.info("=" * 60)
        logger.info("🤖 Инициализация MEXC Volume Bot")
        logger.info(f"📍 Режим: {'PRODUCTION (Render)' if IS_RENDER else 'DEVELOPMENT'}")
        logger.info("=" * 60)
        
        # Создаем приложение
        application = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .post_init(post_init)
            .post_stop(post_stop)
            .concurrent_updates(True)
            .build()
        )

        # Добавляем обработчики
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_message))
        application.add_handler(CallbackQueryHandler(button_handler))

        logger.info("✅ Бот инициализирован")
        logger.info("🔄 Запуск polling...")

        # Запускаем polling
        application.run_polling(
            drop_pending_updates=True,
            timeout=30,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False,  # Важно для Render!
            poll_interval=1.0,
            bootstrap_retries=-1,  # Бесконечные попытки переподключения
        )
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка при запуске бота: {e}")
        logger.error(f"Тип ошибки: {type(e).__name__}")
        import traceback
        logger.error(f"Трассировка: {traceback.format_exc()}")
        
        # Пытаемся отправить сообщение об ошибке
        try:
            # Создаем временное приложение для отправки сообщения
            from telegram import Bot
            bot = Bot(token=TELEGRAM_TOKEN)
            asyncio.run(bot.send_message(
                ALLOWED_USER_ID,
                f"⚠️ <b>Бот упал с ошибкой!</b>\n\n"
                f"<b>Ошибка:</b> {type(e).__name__}\n"
                f"<b>Время:</b> {time.strftime('%H:%M:%S')}\n\n"
                f"Перезапустится автоматически через 60 секунд 🔄",
                parse_mode="HTML"
            ))
        except:
            pass
        
        # Ждем и перезапускаем
        time.sleep(60)
        logger.info("🔄 Перезапуск бота...")
        run_bot()  # Рекурсивный перезапуск

if __name__ == "__main__":
    # Устанавливаем политику event loop для Windows
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # Запускаем веб-сервер в отдельном потоке
    if IS_RENDER:
        web_thread = threading.Thread(target=run_web_server, daemon=True)
        web_thread.start()
        logger.info("✅ Веб-сервер запущен в отдельном потоке")
    
    # Запускаем бота
    run_bot()


















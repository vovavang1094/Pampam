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
    CommandHandler
)
from fastapi import FastAPI
import uvicorn
import threading
import re
from datetime import datetime

# ====================== ПРОВЕРКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ======================
REQUIRED_ENV_VARS = ['TELEGRAM_TOKEN', 'ALLOWED_USER_ID', 'MEXC_API_KEY', 'MEXC_SECRET_KEY']
missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]

if missing_vars:
    print(f"❌ ОШИБКА: Отсутствуют переменные окружения: {', '.join(missing_vars)}")
    print("Добавьте их в настройках Render Dashboard → Environment")
    sys.exit(1)

# ====================== НАСТРОЙКИ ======================
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

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Отключаем логирование от библиотек
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

# Глобальные переменные
ALL_SYMBOLS = set()
user_settings = {}
user_state = {}
user_temp = {}

SHOW_INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"]
NOTIFY_EMOJI = "🔔"
DISABLED_EMOJI = "🔕"

# Глобальные задачи
_monitor_task = None
_heartbeat_task = None
_status_task = None
_is_monitoring_running = True
_start_time = time.time()
_last_status_notification = 0

# ====================== РЕЙТ ЛИМИТЕР ДЛЯ TELEGRAM ======================
class TelegramRateLimiter:
    """Лимитер запросов к Telegram API"""
    def __init__(self, max_per_second=0.5):
        self.max_per_second = max_per_second
        self.last_call = 0
        
    async def call(self, coro):
        """Вызов с rate limiting"""
        current_time = time.time()
        time_since_last = current_time - self.last_call
        
        if time_since_last < (1.0 / self.max_per_second):
            wait_time = (1.0 / self.max_per_second) - time_since_last
            await asyncio.sleep(wait_time)
        
        try:
            result = await coro
            self.last_call = time.time()
            return result
        except Exception as e:
            if "RetryAfter" in str(e):
                wait_match = re.search(r'(\d+)', str(e))
                if wait_match:
                    wait_time = int(wait_match.group(1))
                    logger.warning(f"Rate limit, waiting {wait_time}s")
                    await asyncio.sleep(wait_time)
                    return await self.call(coro)
            raise

telegram_limiter = TelegramRateLimiter(max_per_second=0.5)

# ====================== СОХРАНЕНИЕ ДАННЫХ ======================
def save_settings():
    """Сохранить настройки в файл"""
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump({str(k): v for k, v in user_settings.items()}, f, 
                     ensure_ascii=False, indent=2, default=str)
        logger.debug("Настройки сохранены")
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")

def load_settings():
    """Загрузить настройки из файла"""
    global user_settings
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                user_settings = {int(k): v for k, v in data.items()}
            total_alerts = sum(len(v) for v in user_settings.values())
            logger.info(f"Загружено {total_alerts} алертов")
        else:
            user_settings = {}
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")
        user_settings = {}

# ====================== УПРОЩЕННЫЙ HEARTBEAT ======================
async def simple_heartbeat():
    """Простой heartbeat без внешних запросов"""
    logger.info("❤️ Heartbeat запущен")
    heartbeat_count = 0
    
    while True:
        try:
            heartbeat_count += 1
            
            # Логирование каждые 10 минут
            if heartbeat_count % 10 == 0:
                try:
                    memory_mb = psutil.Process().memory_info().rss / 1024 / 1024
                    total_alerts = sum(len(alerts) for alerts in user_settings.values())
                    logger.info(f"Heartbeat: {memory_mb:.1f}MB RAM, {total_alerts} алертов")
                except:
                    pass
            
            # Автосохранение каждые 30 минут
            if heartbeat_count % 30 == 0:
                save_settings()
            
            await asyncio.sleep(60)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(30)

# ====================== СТАТУС УВЕДОМЛЕНИЯ КАЖДЫЕ 2 ЧАСА ======================
async def status_notifications(application: Application):
    """Отправка статусных уведомлений каждые 2 часа"""
    global _last_status_notification
    
    logger.info("📅 Статусные уведомления запущены")
    
    while True:
        try:
            current_time = time.time()
            
            if current_time - _last_status_notification >= 7200:  # 2 часа
                try:
                    total_alerts = sum(len(alerts) for alerts in user_settings.values())
                    uptime_seconds = int(current_time - _start_time)
                    hours = uptime_seconds // 3600
                    minutes = (uptime_seconds % 3600) // 60
                    
                    message = (
                        f"✅ <b>Статус бота</b> (каждые 2 часа)\n\n"
                        f"⏱ <b>Аптайм:</b> {hours}ч {minutes}м\n"
                        f"📊 <b>Пар доступно:</b> {len(ALL_SYMBOLS)}\n"
                        f"🔔 <b>Активных алертов:</b> {total_alerts}\n"
                        f"🔄 <b>Мониторинг:</b> Работает ✅\n"
                        f"📍 <b>Хост:</b> {'Render.com' if IS_RENDER else 'Локальный'}\n\n"
                        f"<i>Бот работает стабильно {datetime.now().strftime('%H:%M')}</i>"
                    )
                    
                    await telegram_limiter.call(
                        application.bot.send_message(
                            ALLOWED_USER_ID,
                            message,
                            parse_mode="HTML"
                        )
                    )
                    
                    _last_status_notification = current_time
                    logger.info("Статусное уведомление отправлено")
                    
                except Exception as e:
                    logger.error(f"Ошибка отправки статуса: {e}")
            
            await asyncio.sleep(300)  # Проверяем каждые 5 минут
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Status notifications error: {e}")
            await asyncio.sleep(60)

# ====================== КЛАВИАТУРЫ ======================
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить алерт", callback_data="add")],
        [InlineKeyboardButton("➕➕ Добавить несколько монет", callback_data="add_multiple")],
        [InlineKeyboardButton("📋 Мои алерты", callback_data="list")],
        [InlineKeyboardButton("❌ Удалить алерт", callback_data="delete")],
        [InlineKeyboardButton("🔄 Обновить пары", callback_data="refresh_symbols")],
        [InlineKeyboardButton("📊 Статус", callback_data="status")],
    ])

def intervals_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1m", callback_data="int_1m"),
            InlineKeyboardButton("5m", callback_data="int_5m"),
            InlineKeyboardButton("15m", callback_data="int_15m"),
        ],
        [
            InlineKeyboardButton("30m", callback_data="int_30m"),
            InlineKeyboardButton("1h", callback_data="int_1h"),
            InlineKeyboardButton("4h", callback_data="int_4h"),
        ],
        [
            InlineKeyboardButton("8h", callback_data="int_8h"),
            InlineKeyboardButton("1d", callback_data="int_1d"),
            InlineKeyboardButton("🔙 Назад", callback_data="back"),
        ],
    ])

def volume_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1000", callback_data="volbtn_1000"),
            InlineKeyboardButton("2000", callback_data="volbtn_2000"),
        ],
        [
            InlineKeyboardButton("5000", callback_data="volbtn_5000"),
            InlineKeyboardButton("10000", callback_data="volbtn_10000"),
        ],
        [
            InlineKeyboardButton("20000", callback_data="volbtn_20000"),
            InlineKeyboardButton("50000", callback_data="volbtn_50000"),
        ],
        [
            InlineKeyboardButton("✏️ Вручную", callback_data="vol_custom"),
            InlineKeyboardButton("🔙 Назад", callback_data="back"),
        ],
    ])

def list_kb(chat_id):
    sets = user_settings.get(chat_id, [])
    kb = []
    for i, s in enumerate(sets[:15]):  # Ограничиваем 15 алертами
        status = NOTIFY_EMOJI if s.get("notifications_enabled", True) else DISABLED_EMOJI
        text = f"{i+1}. {s['symbol']} {s['interval']} ≥{s['threshold']:,} {status}"
        if len(text) > 60:  # Обрезаем если слишком длинно
            text = text[:57] + "..."
        kb.append([InlineKeyboardButton(text, callback_data=f"alert_options_{i}")])
    
    if len(sets) > 15:
        kb.append([InlineKeyboardButton(f"... и еще {len(sets)-15} алертов", callback_data="list")])
    
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
                        symbols = {x["symbol"].replace("_USDT", "USDT") 
                                 for x in j["data"] if "_USDT" in x["symbol"]}
                        ALL_SYMBOLS = symbols
                        logger.info(f"Загружено {len(ALL_SYMBOLS)} пар")
                        return True
    except Exception as e:
        logger.error(f"Ошибка загрузки символов: {e}")
    
    # Fallback
    if len(ALL_SYMBOLS) < 50:
        ALL_SYMBOLS = {
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", 
            "XRPUSDT", "DOGEUSDT", "DOTUSDT", "AVAXUSDT", "LINKUSDT"
        }
        logger.info(f"Используется fallback список: {len(ALL_SYMBOLS)} пар")
    
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
    
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://contract.mexc.com/api/v1/contract/kline/{sym}",
                params={"symbol": sym, "interval": interval_map.get(interval, "Min1"), "limit": 1},
                headers=headers,
                timeout=ClientTimeout(total=5)
            ) as r:
                if r.status == 200:
                    j = await r.json()
                    if j.get("success") and j.get("data", {}).get("amount"):
                        amount = j["data"]["amount"][0]
                        if amount:
                            return int(float(amount))
    except Exception as e:
        logger.debug(f"Ошибка получения объёма {symbol}: {e}")
    
    return 0

# ====================== УПРОЩЕННЫЙ МОНИТОРИНГ ======================
async def safe_monitor_volumes(application: Application):
    """Безопасный мониторинг"""
    global _is_monitoring_running
    
    await asyncio.sleep(5)
    logger.info("📈 Мониторинг запущен")
    
    error_count = 0
    
    while _is_monitoring_running:
        try:
            notifications_sent = 0
            
            for chat_id, alerts in list(user_settings.items()):
                if not alerts:
                    continue
                    
                for alert in alerts[:50]:  # Ограничиваем 50 алертов на пользователя
                    if not alert.get("notifications_enabled", True):
                        continue
                    
                    try:
                        vol = await fetch_volume(alert["symbol"], alert["interval"])
                        threshold = alert["threshold"]
                        last_notified = alert.get("last_notified", 0)
                        
                        if vol >= threshold and vol != last_notified:
                            alert["last_notified"] = vol
                            notifications_sent += 1
                            
                            message = (
                                f"<b>🚨 ВСПЛЕСК ОБЪЁМА!</b>\n\n"
                                f"<b>Пара:</b> {alert['symbol']}\n"
                                f"<b>Таймфрейм:</b> {alert['interval']}\n"
                                f"<b>Порог:</b> {threshold:,} USDT\n"
                                f"<b>Текущий объем:</b> {vol:,} USDT\n"
                                f"<b>Превышение:</b> {(vol - threshold):,} USDT"
                            )
                            
                            url = f"https://www.mexc.com/ru-RU/futures/{alert['symbol'][:-4]}_USDT"
                            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📈 MEXC", url=url)]])
                            
                            await telegram_limiter.call(
                                application.bot.send_message(
                                    chat_id,
                                    message,
                                    parse_mode="HTML",
                                    reply_markup=kb
                                )
                            )
                            
                            logger.info(f"Уведомление: {alert['symbol']} - {vol:,} USDT")
                            
                    except Exception as e:
                        logger.debug(f"Ошибка в алерте: {e}")
                        continue
            
            if notifications_sent > 0:
                save_settings()
            
            error_count = 0
            await asyncio.sleep(30)
            
        except asyncio.CancelledError:
            logger.info("Мониторинг остановлен")
            break
        except Exception as e:
            error_count += 1
            logger.error(f"Ошибка мониторинга ({error_count}): {e}")
            
            if error_count >= 3:
                await asyncio.sleep(300)
                error_count = 0
            else:
                await asyncio.sleep(60)
    
    logger.info("Мониторинг завершен")

# ====================== УПРОЩЕННЫЙ ПОКАЗ АЛЕРТОВ ======================
async def show_alert_simple(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    """Упрощенный показ алерта без прогресс бара"""
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    
    if chat_id not in user_settings or idx >= len(user_settings[chat_id]):
        await telegram_limiter.call(
            q.edit_message_text("⚠️ Алерт не найден", reply_markup=main_menu())
        )
        return
    
    alert = user_settings[chat_id][idx]
    symbol = alert["symbol"]
    
    # Сразу показываем алерт
    status = NOTIFY_EMOJI if alert.get("notifications_enabled", True) else DISABLED_EMOJI
    
    text = (
        f"<b>📊 Алерт #{idx+1}</b>\n\n"
        f"<b>Пара:</b> {symbol}\n"
        f"<b>Таймфрейм:</b> {alert['interval']}\n"
        f"<b>Порог:</b> {alert['threshold']:,} USDT\n"
        f"<b>Уведомления:</b> {status}\n\n"
        f"<i>Загружаю текущий объем...</i>"
    )
    
    await telegram_limiter.call(
        q.edit_message_text(text, parse_mode="HTML")
    )
    
    # Загружаем объем асинхронно
    try:
        vol = await fetch_volume(symbol, alert["interval"])
        
        text = (
            f"<b>📊 Алерт #{idx+1}</b>\n\n"
            f"<b>Пара:</b> {symbol}\n"
            f"<b>Таймфрейм:</b> {alert['interval']}\n"
            f"<b>Порог:</b> {alert['threshold']:,} USDT\n"
            f"<b>Текущий объем:</b> {vol:,} USDT\n"
            f"<b>Уведомления:</b> {status}\n\n"
            f"{'🟢 Превышен порог!' if vol >= alert['threshold'] else '🔴 Ниже порога'}"
        )
        
    except Exception as e:
        text = (
            f"<b>📊 Алерт #{idx+1}</b>\n\n"
            f"<b>Пара:</b> {symbol}\n"
            f"<b>Таймфрейм:</b> {alert['interval']}\n"
            f"<b>Порог:</b> {alert['threshold']:,} USDT\n"
            f"<b>Уведомления:</b> {status}\n\n"
            f"<i>Не удалось загрузить текущий объем</i>"
        )
    
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 MEXC", url=f"https://www.mexc.com/ru-RU/futures/{symbol[:-4]}_USDT"),
            InlineKeyboardButton(f"{'🔔' if alert.get('notifications_enabled', True) else '🔕'} Увед.", 
                               callback_data=f"toggle_notify_{idx}")
        ],
        [
            InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_{idx}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"del_{idx}")
        ],
        [InlineKeyboardButton("🔙 Назад", callback_data="list")],
    ])
    
    await telegram_limiter.call(
        q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    )

# ====================== ДОБАВЛЕНИЕ НЕСКОЛЬКИХ МОНЕТ ======================
async def add_multiple_coins(update: Update, context: ContextTypes.DEFAULT_TYPE, symbols_text: str):
    """Добавление нескольких монет"""
    chat_id = update.effective_chat.id
    
    # Парсим символы
    symbols_list = []
    invalid_symbols = []
    
    for sym in symbols_text.upper().replace(',', ' ').replace('\n', ' ').split():
        sym = sym.strip()
        if not sym:
            continue
            
        if not sym.endswith("USDT"):
            sym += "USDT"
            
        if sym in ALL_SYMBOLS:
            symbols_list.append(sym)
        else:
            invalid_symbols.append(sym)
    
    if not symbols_list:
        await telegram_limiter.call(
            update.message.reply_text("❌ Не найдено валидных пар", reply_markup=main_menu())
        )
        return
    
    # Сохраняем символы
    user_temp[chat_id] = {"symbols": symbols_list}
    user_state[chat_id] = "wait_multiple_interval"
    
    # Показываем результат
    valid_count = len(symbols_list)
    invalid_count = len(invalid_symbols)
    
    message = f"✅ Найдено пар: <b>{valid_count}</b>\n"
    if invalid_count > 0:
        message += f"❌ Пропущено: <b>{invalid_count}</b>\n"
    
    if valid_count <= 10:
        message += f"<code>{', '.join(symbols_list)}</code>\n\n"
    else:
        message += f"<code>{', '.join(symbols_list[:10])}...</code>\n\n"
    
    message += "Выберите таймфрейм для всех пар:"
    
    await telegram_limiter.call(
        update.message.reply_text(message, parse_mode="HTML", reply_markup=intervals_kb())
    )

# ====================== ОБРАБОТЧИКИ ======================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    
    total_alerts = sum(len(alerts) for alerts in user_settings.values())
    user_alerts = len(user_settings.get(update.effective_chat.id, []))
    
    message = (
        f"🔥 <b>MEXC Volume Bot</b>\n\n"
        f"📍 <b>Хост:</b> {'Render.com' if IS_RENDER else 'Локальный'}\n"
        f"📊 <b>Пар:</b> {len(ALL_SYMBOLS)}\n"
        f"🔔 <b>Ваших алертов:</b> {user_alerts}\n"
        f"👥 <b>Всего алертов:</b> {total_alerts}\n\n"
        f"<b>Функции:</b>\n"
        f"• Отслеживание объемов 24/7\n"
        f"• Добавление одной или нескольких монет\n"
        f"• Статусные уведомления каждые 2 часа\n"
        f"• Надежное сохранение настроек\n\n"
        f"<i>Выберите действие:</i>"
    )
    
    await telegram_limiter.call(
        update.message.reply_text(message, parse_mode="HTML", reply_markup=main_menu())
    )

async def any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    chat_id = update.effective_chat.id
    user_settings.setdefault(chat_id, [])
    text = (update.message.text or "").strip()

    if not text or any(w in text.lower() for w in ["меню", "start", "привет", "/start"]):
        await start_command(update, context)
        return

    state = user_state.get(chat_id)
    
    if state == "wait_symbol":
        await add_single_coin(update, context, text)
        return
    
    elif state == "wait_multiple_symbols":
        await add_multiple_coins(update, context, text)
        return
    
    elif state in ["wait_threshold", "wait_threshold_custom", "edit_threshold", "edit_threshold_custom"]:
        await process_threshold(update, context, text)
        return

async def add_single_coin(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Добавление одной монеты"""
    chat_id = update.effective_chat.id
    sym = text.upper().strip()
    
    if not sym.endswith("USDT"):
        sym += "USDT"
    
    if sym not in ALL_SYMBOLS:
        # Поиск похожих пар
        suggestions = [s for s in ALL_SYMBOLS if sym[:-4].lower() in s.lower()][:5]
        suggestions_text = "\n".join(suggestions) if suggestions else "Нет похожих пар"
        
        await telegram_limiter.call(
            update.message.reply_text(
                f"⚠️ Пара <b>{sym}</b> не найдена\n\n"
                f"<b>Похожие пары:</b>\n{suggestions_text}",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
        )
        return
    
    user_temp[chat_id] = {"symbol": sym}
    user_state[chat_id] = "wait_interval"
    
    await telegram_limiter.call(
        update.message.reply_text(
            f"✅ Пара: <b>{sym}</b>\nВыберите таймфрейм:",
            parse_mode="HTML",
            reply_markup=intervals_kb()
        )
    )

async def process_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Обработка порога"""
    chat_id = update.effective_chat.id
    
    try:
        numbers = re.findall(r'\d+', text.replace(',', '').replace(' ', ''))
        if not numbers:
            raise ValueError
        
        threshold_value = int(numbers[0])
        if threshold_value < 1000:
            await telegram_limiter.call(
                update.message.reply_text("⚠️ Минимум 1000 USDT")
            )
            return
    except:
        await telegram_limiter.call(
            update.message.reply_text("⚠️ Введите число ≥ 1000")
        )
        return
    
    is_edit = user_state[chat_id] in ["edit_threshold", "edit_threshold_custom"]
    
    if "symbols" in user_temp.get(chat_id, {}):
        # Добавление нескольких алертов
        symbols = user_temp[chat_id]["symbols"]
        interval = user_temp[chat_id]["interval"]
        added_count = 0
        
        for sym in symbols:
            # Проверяем дубликаты
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
        
        message = (
            f"✅ Добавлено <b>{added_count}</b> алертов!\n\n"
            f"<b>Таймфрейм:</b> {interval}\n"
            f"<b>Порог:</b> {threshold_value:,} USDT\n"
            f"<b>Всего алертов:</b> {len(user_settings[chat_id])}"
        )
        
    elif is_edit:
        # Редактирование
        idx = user_temp[chat_id]["edit_idx"]
        user_settings[chat_id][idx]["threshold"] = threshold_value
        save_settings()
        
        alert = user_settings[chat_id][idx]
        message = (
            f"✅ Алерт обновлён!\n\n"
            f"<b>{alert['symbol']} {alert['interval']}</b>\n"
            f"<b>Порог:</b> {threshold_value:,} USDT"
        )
    else:
        # Один алерт
        alert = {
            "symbol": user_temp[chat_id]["symbol"],
            "interval": user_temp[chat_id]["interval"],
            "threshold": threshold_value,
            "last_notified": 0,
            "notifications_enabled": True,
        }
        user_settings[chat_id].append(alert)
        save_settings()
        
        message = (
            f"✅ Алерт добавлен!\n\n"
            f"<b>{alert['symbol']} {alert['interval']}</b>\n"
            f"<b>Порог:</b> {threshold_value:,} USDT\n"
            f"<b>Всего алертов:</b> {len(user_settings[chat_id])}"
        )
    
    await telegram_limiter.call(
        update.message.reply_text(message, parse_mode="HTML", reply_markup=main_menu())
    )
    
    user_state.pop(chat_id, None)
    user_temp.pop(chat_id, None)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    
    data = q.data
    chat_id = q.message.chat_id
    user_settings.setdefault(chat_id, [])
    
    # Основные кнопки
    if data == "back":
        user_state.pop(chat_id, None)
        user_temp.pop(chat_id, None)
        await telegram_limiter.call(
            q.edit_message_text("Главное меню", reply_markup=main_menu())
        )
        return
    
    elif data == "add":
        user_state[chat_id] = "wait_symbol"
        await telegram_limiter.call(
            q.edit_message_text(
                "Введите тикер монеты (например: BTC):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="back")]])
            )
        )
        return
    
    elif data == "add_multiple":
        user_state[chat_id] = "wait_multiple_symbols"
        user_temp[chat_id] = {}
        await telegram_limiter.call(
            q.edit_message_text(
                "Введите несколько тикеров через пробел или запятую:\n\n"
                "<i>Пример: BTC ETH SOL\nИли: BTC, ETH, SOL</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="back")]])
            )
        )
        return
    
    elif data == "refresh_symbols":
        await q.answer("Обновляем список пар...", show_alert=False)
        success = await load_symbols()
        message = f"✅ Пар доступно: {len(ALL_SYMBOLS)}" if success else "⚠️ Не удалось обновить"
        await telegram_limiter.call(
            q.edit_message_text(message, reply_markup=main_menu())
        )
        return
    
    elif data == "list":
        alerts_count = len(user_settings.get(chat_id, []))
        text = f"📋 Ваши алерты: {alerts_count}" if alerts_count > 0 else "ℹ️ Нет алертов"
        await telegram_limiter.call(
            q.edit_message_text(text, reply_markup=list_kb(chat_id))
        )
        return
    
    elif data == "delete":
        if not user_settings.get(chat_id):
            await telegram_limiter.call(
                q.edit_message_text("ℹ️ Нет алертов", reply_markup=main_menu())
            )
            return
        
        kb = []
        for i, s in enumerate(user_settings[chat_id][:15]):
            status = "🔔" if s.get("notifications_enabled", True) else "🔕"
            kb.append([InlineKeyboardButton(
                f"{i+1}. {s['symbol']} {s['interval']} ≥{s['threshold']:,} {status}", 
                callback_data=f"del_{i}"
            )])
        kb.append([InlineKeyboardButton("🔙 Назад", callback_data="list")])
        
        await telegram_limiter.call(
            q.edit_message_text("❌ Выберите алерт:", reply_markup=InlineKeyboardMarkup(kb))
        )
        return
    
    elif data == "status":
        total_alerts = sum(len(alerts) for alerts in user_settings.values())
        uptime_seconds = int(time.time() - _start_time)
        hours = uptime_seconds // 3600
        minutes = (uptime_seconds % 3600) // 60
        
        status_text = (
            f"<b>📊 Статус системы</b>\n\n"
            f"📍 <b>Хост:</b> {'Render.com' if IS_RENDER else 'Локальный'}\n"
            f"⏱ <b>Аптайм:</b> {hours}ч {minutes}м\n"
            f"📊 <b>Пар доступно:</b> {len(ALL_SYMBOLS)}\n"
            f"🔔 <b>Всего алертов:</b> {total_alerts}\n"
            f"👤 <b>Активных пользователей:</b> {len(user_settings)}\n"
            f"🔄 <b>Мониторинг:</b> Активен ✅\n"
            f"❤️ <b>Heartbeat:</b> Активен ✅\n\n"
            f"<i>Следующее статусное уведомление через "
            f"{max(0, 7200 - (time.time() - _last_status_notification)) // 3600}ч</i>"
        )
        
        await telegram_limiter.call(
            q.edit_message_text(status_text, parse_mode="HTML", reply_markup=main_menu())
        )
        return
    
    # Управление алертами
    elif data.startswith("alert_options_"):
        idx = int(data.split("_")[2])
        await show_alert_simple(update, context, idx)
        return
    
    elif data.startswith("toggle_notify_"):
        idx = int(data.split("_")[2])
        if idx < len(user_settings[chat_id]):
            alert = user_settings[chat_id][idx]
            alert["notifications_enabled"] = not alert.get("notifications_enabled", True)
            save_settings()
            await show_alert_simple(update, context, idx)
        return
    
    elif data.startswith("edit_"):
        idx = int(data.split("_")[1])
        if idx < len(user_settings[chat_id]):
            user_state[chat_id] = "edit_interval"
            user_temp[chat_id] = {"edit_idx": idx, "symbol": user_settings[chat_id][idx]["symbol"]}
            await telegram_limiter.call(
                q.edit_message_text(
                    f"✏️ Редактирование:\n{user_settings[chat_id][idx]['symbol']}\n\nВыберите таймфрейм:",
                    reply_markup=intervals_kb()
                )
            )
        return
    
    elif data.startswith("del_"):
        idx = int(data.split("_")[1])
        if idx < len(user_settings[chat_id]):
            deleted = user_settings[chat_id].pop(idx)
            save_settings()
            await telegram_limiter.call(
                q.edit_message_text(
                    f"✅ Удалено: {deleted['symbol']} {deleted['interval']}",
                    reply_markup=main_menu()
                )
            )
        return
    
    # Добавление алертов
    elif data.startswith("int_"):
        interval = data.split("_")[1]
        
        if "symbols" in user_temp.get(chat_id, {}):
            # Для нескольких монет
            user_temp[chat_id]["interval"] = interval
            user_state[chat_id] = "wait_threshold"
            
            count = len(user_temp[chat_id]["symbols"])
            await telegram_limiter.call(
                q.edit_message_text(
                    f"✅ Таймфрейм: {interval}\n"
                    f"<b>Количество пар:</b> {count}\n\n"
                    f"Выберите порог для всех {count} пар:",
                    parse_mode="HTML",
                    reply_markup=volume_kb()
                )
            )
        elif user_state.get(chat_id) == "edit_interval":
            # Редактирование
            idx = user_temp[chat_id]["edit_idx"]
            user_settings[chat_id][idx]["interval"] = interval
            user_state[chat_id] = "edit_threshold"
            user_temp[chat_id]["interval"] = interval
            
            await telegram_limiter.call(
                q.edit_message_text(
                    f"🆕 Таймфрейм: {interval}\n"
                    f"<b>Пара:</b> {user_temp[chat_id]['symbol']}\n\n"
                    f"Выберите порог:",
                    parse_mode="HTML",
                    reply_markup=volume_kb()
                )
            )
        else:
            # Одна монета
            user_temp[chat_id]["interval"] = interval
            user_state[chat_id] = "wait_threshold"
            
            await telegram_limiter.call(
                q.edit_message_text(
                    f"✅ Таймфрейм: {interval}\n"
                    f"<b>Пара:</b> {user_temp[chat_id]['symbol']}\n\n"
                    f"Выберите порог:",
                    parse_mode="HTML",
                    reply_markup=volume_kb()
                )
            )
        return
    
    elif data.startswith("volbtn_"):
        volume = int(data.split("_")[1])
        
        if "symbols" in user_temp.get(chat_id, {}):
            # Несколько монет
            symbols = user_temp[chat_id]["symbols"]
            interval = user_temp[chat_id]["interval"]
            added_count = 0
            
            for sym in symbols:
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
            
            message = (
                f"✅ Добавлено <b>{added_count}</b> алертов!\n\n"
                f"<b>Таймфрейм:</b> {interval}\n"
                f"<b>Порог:</b> {volume:,} USDT\n"
                f"<b>Всего алертов:</b> {len(user_settings[chat_id])}"
            )
            
            user_state.pop(chat_id, None)
            user_temp.pop(chat_id, None)
            
        elif user_state.get(chat_id) == "edit_threshold":
            # Редактирование
            idx = user_temp[chat_id]["edit_idx"]
            user_settings[chat_id][idx]["threshold"] = volume
            save_settings()
            
            alert = user_settings[chat_id][idx]
            message = f"✅ Обновлено: {alert['symbol']} {alert['interval']} ≥{volume:,}"
            
            user_state.pop(chat_id, None)
            user_temp.pop(chat_id, None)
        else:
            # Одна монета
            alert = {
                "symbol": user_temp[chat_id]["symbol"],
                "interval": user_temp[chat_id]["interval"],
                "threshold": volume,
                "last_notified": 0,
                "notifications_enabled": True,
            }
            user_settings[chat_id].append(alert)
            save_settings()
            
            message = (
                f"✅ Добавлен: {alert['symbol']} {alert['interval']} ≥{volume:,}\n"
                f"<b>Всего алертов:</b> {len(user_settings[chat_id])}"
            )
            
            user_state.pop(chat_id, None)
            user_temp.pop(chat_id, None)
        
        await telegram_limiter.call(
            q.edit_message_text(message, parse_mode="HTML", reply_markup=main_menu())
        )
        return
    
    elif data == "vol_custom":
        if "symbols" in user_temp.get(chat_id, {}):
            state = "wait_threshold_custom"
        elif user_state.get(chat_id) == "edit_threshold":
            state = "edit_threshold_custom"
        else:
            state = "wait_threshold_custom"
        
        user_state[chat_id] = state
        
        await telegram_limiter.call(
            q.edit_message_text(
                "Введите порог объема (например: 15000):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]])
            )
        )
        return
    
    elif data == "refresh_all":
        await q.answer("Обновление...", show_alert=False)
        await telegram_limiter.call(
            q.edit_message_text("🔄 Обновление...", reply_markup=list_kb(chat_id))
        )
        return

# ====================== POST_INIT И POST_STOP ======================
async def post_init(application: Application):
    """Инициализация после запуска"""
    global _monitor_task, _heartbeat_task, _status_task, _last_status_notification
    
    logger.info("=" * 50)
    logger.info("🚀 MEXC Bot запускается")
    logger.info(f"👤 User ID: {ALLOWED_USER_ID}")
    logger.info("=" * 50)
    
    load_settings()
    await load_symbols()
    
    # Запускаем задачи
    _monitor_task = asyncio.create_task(safe_monitor_volumes(application))
    _status_task = asyncio.create_task(status_notifications(application))
    
    if IS_RENDER:
        _heartbeat_task = asyncio.create_task(simple_heartbeat())
    
    # Отправляем стартовое сообщение
    try:
        total_alerts = sum(len(alerts) for alerts in user_settings.values())
        await telegram_limiter.call(
            application.bot.send_message(
                ALLOWED_USER_ID,
                f"🤖 <b>Бот запущен!</b>\n\n"
                f"⏰ <b>Время:</b> {datetime.now().strftime('%H:%M')}\n"
                f"📊 <b>Пар:</b> {len(ALL_SYMBOLS)}\n"
                f"🔔 <b>Алертов:</b> {total_alerts}\n\n"
                f"<i>Статусные уведомления каждые 2 часа</i>\n"
                f"<i>Поддерживается добавление нескольких монет</i>",
                parse_mode="HTML"
            )
        )
        _last_status_notification = time.time()
    except Exception as e:
        logger.error(f"Не удалось отправить стартовое сообщение: {e}")

async def post_stop(application: Application):
    """Корректная остановка"""
    logger.info("🛑 Останавливаем бота...")
    
    global _is_monitoring_running
    _is_monitoring_running = False
    
    # Останавливаем задачи
    tasks = [_monitor_task, _heartbeat_task, _status_task]
    for task in tasks:
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    
    save_settings()
    logger.info("✅ Бот остановлен")

# ====================== ВЕБ-СЕРВЕР ДЛЯ RENDER ======================
web_app = FastAPI()

@web_app.get("/")
async def root():
    total_alerts = sum(len(alerts) for alerts in user_settings.values())
    return {
        "status": "online",
        "service": "mexc-bot",
        "alerts": total_alerts,
        "symbols": len(ALL_SYMBOLS),
        "features": ["multiple-coins", "2h-status", "rate-limited"],
        "uptime": int(time.time() - _start_time)
    }

@web_app.get("/health")
async def health():
    """СУПЕР простой health check"""
    return {"status": "healthy", "timestamp": int(time.time())}

def run_web_server():
    """Запуск веб-сервера"""
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=port,
        log_level="error",
        access_log=False,
        timeout_keep_alive=5
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())

# ====================== ЗАПУСК БОТА ======================
def main():
    """Основная функция запуска"""
    try:
        # Инициализируем приложение
        application = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .post_init(post_init)
            .post_stop(post_stop)
            .concurrent_updates(True)
            .build()
        )
        
        # Добавляем обработчики
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_message))
        application.add_handler(CallbackQueryHandler(button_handler))
        
        # Запускаем веб-сервер если на Render
        if IS_RENDER:
            web_thread = threading.Thread(target=run_web_server, daemon=True)
            web_thread.start()
            logger.info(f"🌐 Веб-сервер запущен на порту {os.environ.get('PORT', 8000)}")
        
        # Запускаем бота
        logger.info("🤖 Бот запускается...")
        application.run_polling(
            drop_pending_updates=True,
            timeout=30,
            close_loop=False,
            poll_interval=0.5,
            bootstrap_retries=-1,
            allowed_updates=Update.ALL_TYPES
        )
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
        time.sleep(30)
        main()  # Перезапуск

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    main()



















#!/usr/bin/env python3
"""
Discord → Telegram бот-пересылатель
✅ User token (анти-бан браузерная эмуляция)
✅ Темы (threads) = отдельный channel_id (/add ID напрямую)
✅ Постоянная JSON-конфигурация
✅ Команды админа на русском
✅ Эмодзи/спецсимволы в названиях тем
✅ Серверные переменные окружения (без .env)
"""

import asyncio
import json
import logging
import os
import re
import time
import zlib
from datetime import datetime
from typing import Dict, Any, Optional

import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Конфигурация - читает серверные переменные окружения
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', '0'))  # 0 = без ограничений

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class DiscordGateway:
    def __init__(self):
        self.ws = None
        self.session = None
        self.heartbeat_interval = 41.25
        self.last_heartbeat = 0
        self.sequence = None
        self.session_id = None
        self.resume_url = None
        self.channels_config = self.load_config()
        self.running = False
        self.fingerprints = [  # 3 Chrome отпечатка для ротации
            {
                'browser': 'Chrome', 'browser_user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'os': 'Windows', 'os_version': '10', 'device': 'Windows Device', 'system_locale': 'ru-RU',
                'browser_version': '120.0.0.0', 'client_build_number': 9999999
            },
            {
                'browser': 'Chrome', 'browser_user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'os': 'Mac OS X', 'os_version': '10.15.7', 'device': 'Mac', 'system_locale': 'ru-RU',
                'browser_version': '120.0.0.0', 'client_build_number': 388668
            },
            {
                'browser': 'Chrome', 'browser_user_agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'os': 'Linux', 'os_version': '0', 'device': 'Linux Device', 'system_locale': 'ru-RU',
                'browser_version': '120.0.0.0', 'client_build_number': 9999999
            }
        ]
        self.current_fp_idx = 0

    def load_config(self) -> set:
        """Загрузка отслеживаемых каналов/тем из JSON"""
        try:
            with open('channels_config.json', 'r', encoding='utf-8') as f:
                return set(json.load(f))
        except FileNotFoundError:
            return set()

    def save_config(self):
        """Сохранение в JSON (выживает перезапуски)"""
        with open('channels_config.json', 'w', encoding='utf-8') as f:
            json.dump(list(self.channels_config), f, ensure_ascii=False, indent=2)

    def escape_markdown_v2(self, text: str) -> str:
        """Экранирование для Telegram MarkdownV2 (эмодзи/спецсимволы работают!)"""
        chars = r'_*[]()~`>#+-=|{}.!'
        for char in chars:
            text = text.replace(char, f'\\{char}')
        return text

    async def get_fp(self):
        """Ротация браузерных отпечатков"""
        fp = self.fingerprints[self.current_fp_idx]
        self.current_fp_idx = (self.current_fp_idx + 1) % 3
        return fp

    async def connect(self):
        """Подключение к Discord Gateway (браузерная эмуляция)"""
        try:
            self.session = aiohttp.ClientSession()
            fp = await self.get_fp()
            
            payload = {
                "op": 2, "d": {
                    "token": DISCORD_TOKEN, "properties": {
                        "$os": fp['os'], "$browser": fp['browser'], "$device": fp['device'],
                        "$system_locale": fp['system_locale'], "$browser_version": fp['browser_version'],
                        "$client_build_number": fp['client_build_number'], "os_version": fp['os_version'],
                        "referrer": "", "referring_domain": "", "release_channel": "stable"
                    }, "compress": True, "large_threshold": 250
                }
            }
            
            async with self.session.post('wss://gateway.discord.gg/?v=10&encoding=json', json=payload) as resp:
                gateway_url = (await resp.json())['d']['url']
            
            self.ws = await self.session.ws_connect(
                gateway_url + '?v=10&encoding=json&compress=zlib-stream',
                autoclose=False, heartbeat_timeout=60
            )
            
            logger.info("✅ Подключен к Discord Gateway")
            self.running = True
            await self.listen()
            
        except Exception as e:
            logger.error(f"❌ Ошибка подключения: {e}")
            await asyncio.sleep(5)
            await self.connect()

    async def send(self, data: Dict):
        """Отправка с лимитом (30/сек)"""
        if time.time() - self.last_heartbeat < 0.033:
            await asyncio.sleep(0.033)
        await self.ws.send_json(data)

    async def heartbeat(self):
        """Heartbeat с джиттером (анти-бан)"""
        while self.running:
            now = time.time()
            jitter = (hash(str(now)) % 5000) / 1000.0
            if now - self.last_heartbeat >= self.heartbeat_interval + jitter:
                await self.send({"op": 1, "d": None})
                self.last_heartbeat = now
            await asyncio.sleep(1)

    async def handle_message_create(self, data: Dict):
        """Обработка сообщений Discord"""
        channel_id = str(data['d']['channel_id'])
        if channel_id not in self.channels_config:
            return
            
        msg = data['d']
        author = msg['author']['username']
        content = msg['content'] or "🖼️ [фото/видео/файл]"
        timestamp = datetime.fromisoctime(msg['timestamp']).strftime("%H:%M")
        
        # Имя темы
        thread_name = ""
        if msg.get('message_reference', {}).get('channel_id'):
            thread_id = msg['message_reference']['channel_id']
            logger.info(f"🧵 Обнаружена тема (ID: {thread_id}) - добавьте: /add {thread_id}")
        elif msg.get('thread', {}).get('name'):
            thread_name = msg['thread']['name']
        
        # Формат: "НазваниеТемы: [HH:MM] User: сообщение"
        if thread_name:
            formatted = f"{thread_name}: [{timestamp}] {author}: {content}"
        else:
            formatted = f"[{timestamp}] {author}: {content}"
        
        await self.forward_to_telegram(formatted)

    async def forward_to_telegram(self, text: str):
        """Пересылка в Telegram"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                escaped = self.escape_markdown_v2(text)
                
                data = {
                    "chat_id": TELEGRAM_CHAT_ID, "text": escaped,
                    "parse_mode": "MarkdownV2", "disable_web_page_preview": True
                }
                
                async with session.post(url, json=data) as resp:
                    if resp.status != 200:
                        logger.warning(f"⚠️ Telegram ошибка: {await resp.text()}")
        except Exception as e:
            logger.error(f"❌ Telegram: {e}")

    async def listen(self):
        """Основной цикл прослушки"""
        heartbeat_task = asyncio.create_task(self.heartbeat())
        try:
            async for msg in self.ws:
                if not self.running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    self.sequence = data.get('s')
                    
                    if data['op'] == 10:  # Hello
                        self.heartbeat_interval = data['d']['heartbeat_interval'] / 1000
                        await self.send({"op": 1, "d": None})
                    elif data['op'] == 0 and data['t'] == 'MESSAGE_CREATE':
                        await self.handle_message_create(data)
                    elif data['op'] == 0 and data['t'] == 'READY':
                        logger.info("✅ Discord готов")
                        
        except Exception as e:
            logger.error(f"❌ Gateway: {e}")
        finally:
            heartbeat_task.cancel()
            self.running = False

    async def add_channel(self, channel_id: str):
        self.channels_config.add(channel_id)
        self.save_config()
        logger.info(f"➕ Добавлен: {channel_id}")

    async def remove_channel(self, channel_id: str):
        self.channels_config.discard(channel_id)
        self.save_config()
        logger.info(f"➖ Удален: {channel_id}")

# Глобальный экземпляр
discord_gateway = DiscordGateway()

# Telegram команды
async def check_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if ADMIN_USER_ID == 0 or update.effective_user.id == ADMIN_USER_ID:
        return True
    await update.message.reply_text("❌ Доступ запрещен")
    return False

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context): return
    if not context.args:
        await update.message.reply_text("ℹ️ `/add ID` - добавить канал/тему\nПример: `/add 123456789`")
        return
    for cid in context.args:
        discord_gateway.add_channel(cid)
    await update.message.reply_text(f"✅ Добавлено: `{', '.join(context.args)}`")

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context): return
    if not context.args:
        await update.message.reply_text("ℹ️ `/del ID` - удалить")
        return
    removed = [cid for cid in context.args if discord_gateway.remove_channel(cid)]
    await update.message.reply_text(f"✅ Удалено: `{', '.join(removed)}`")

async def cmd_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_add(update, context)

async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context): return
    if not discord_gateway.channels_config:
        await update.message.reply_text("📭 Нет каналов/тем")
    else:
        lst = '\n'.join(f"`{cid}`" for cid in sorted(discord_gateway.channels_config))
        await update.message.reply_text(f"📋 **Каналы/темы ({len(discord_gateway.channels_config)}):**\n```\n{lst}\n```")

async def cmd_threads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧵 **Как добавить темы:**\n\n"
        "1. Запустите бота\n"
        "2. Смотрите `bot.log` - найдите: `🧵 Обнаружена тема (ID: 123456789)`\n"
        "3. `/add 123456789`\n\n"
        "**Формат:** `📱Тема🎉: [14:35] User: текст`\n"
        "✅ Эмодзи и спецсимволы работают!"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context): return
    status = "🟢 РАБОТАЕТ" if discord_gateway.running else "🔴 ОСТАНОВЛЕН"
    count = len(discord_gateway.channels_config)
    await update.message.reply_text(f"📊 **Статус:** {status}\n📡 **Каналов/тем:** {count}")

async def main():
    # Проверка переменных
    required = {'DISCORD_TOKEN': DISCORD_TOKEN, 'TELEGRAM_TOKEN': TELEGRAM_TOKEN, 'TELEGRAM_CHAT_ID': TELEGRAM_CHAT_ID}
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error(f"❌ Нет переменных: {', '.join(missing)}")
        logger.info("💡 `export DISCORD_TOKEN=токен`")
        return
    
    logger.info("🚀 Запуск Discord→Telegram...")
    
    # Discord
    gateway_task = asyncio.create_task(discord_gateway.connect())
    
    # Telegram
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("target", cmd_target))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("threads", cmd_threads))
    app.add_handler(CommandHandler("status", cmd_status))
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    logger.info("✅ Готов! Команды: /add ID, /channels, /threads")
    
    try:
        await gateway_task
    except KeyboardInterrupt:
        pass
    finally:
        discord_gateway.running = False
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
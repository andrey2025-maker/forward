import asyncio
import json
import os
import random
import time
import logging
from datetime import datetime
from typing import Dict, List, Set
import aiohttp
import websockets
from fake_useragent import UserAgent
from dataclasses import dataclass
from dotenv import load_dotenv

# Загрузка .env
load_dotenv()

@dataclass
class Config:
    DISCORD_TOKEN: str = os.getenv('DISCORD_TOKEN', '')
    TELEGRAM_BOT_TOKEN: str = os.getenv('TELEGRAM_BOT_TOKEN', '')
    ADMIN_TG_ID: int = int(os.getenv('ADMIN_TG_ID') or '0')
    TARGET_TG_CHAT_ID: int = 0

config = Config()

# Файлы конфигурации
CONFIG_FILE = "bot_config.json"
CHANNELS_FILE = "channels_config.json"

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('discord_tg_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class DiscordForwarder:
    def __init__(self):
        self.ua = UserAgent()
        self.session = None
        self.gateway_url = None
        self.session_id = None
        self.sequence = 0
        self.discord_user_id = None
        self.guilds: Dict[str, dict] = {}
        self.channels: Dict[str, List[dict]] = {}
        self.active_channels: Set[str] = set()
        self.ws = None
        self.heartbeat_interval = 41250
        self.heartbeat_task = None
        
    async def init_session(self):
        """Инициализация Discord сессии"""
        if self.session:
            await self.session.close()
            self.session = None
        headers = {
            'Authorization': config.DISCORD_TOKEN,
            'User-Agent': self.ua.random,
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }
        
        timeout = aiohttp.ClientTimeout(total=120, connect=30)
        self.session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        
        try:
            # Gateway URL
            async with self.session.get('https://discord.com/api/v10/gateway') as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.error(f"Discord gateway: {resp.status} {data}")
                    raise RuntimeError(f"Discord API: {data.get('message', resp.status)}")
                self.gateway_url = data['url'] + '/?v=10&encoding=json'
            
            # User info
            async with self.session.get('https://discord.com/api/v10/users/@me') as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.error(f"Discord @me: {resp.status} {data}")
                    raise RuntimeError(f"Discord API: {data.get('message', 'Invalid token?')}")
                self.discord_user_id = data['id']
                logger.info(f"✅ Discord подключен: {data.get('username', '?')}#{data.get('discriminator', '0')}")
            
            await self.load_guilds_and_channels()
            
        except Exception:
            logger.exception("❌ Discord init error")
            if self.session:
                await self.session.close()
                self.session = None
            raise
    
    async def load_guilds_and_channels(self):
        """Загрузка серверов, каналов и тем (тредов)"""
        if not self.session:
            raise RuntimeError("Discord сессия не готова. Подожди запуска бота.")
        
        self.guilds.clear()
        self.channels.clear()
        
        async with self.session.get('https://discord.com/api/v10/users/@me/guilds') as resp:
            guilds_data = await resp.json()
            for guild in guilds_data:
                guild_id = guild['id']
                self.guilds[guild_id] = {
                    'name': guild['name'],
                    'icon': guild.get('icon')
                }
                self.channels[guild_id] = []
        
        sem = asyncio.Semaphore(5)
        
        async def fetch_channels(guild_id: str):
            async with sem:
                try:
                    async with self.session.get(
                        f'https://discord.com/api/v10/guilds/{guild_id}/channels'
                    ) as ch_resp:
                        if ch_resp.status != 200:
                            return
                        channels_data = await ch_resp.json()
                        for channel in channels_data:
                            ctype = channel.get('type', 0)
                            if ctype in (0, 5, 15):  # Text, Announcement, Forum
                                self.channels[guild_id].append({
                                    'id': channel['id'],
                                    'name': channel['name'],
                                    'parent_id': channel.get('parent_id'),
                                    'type': ctype
                                })
                                if ctype == 15:  # Forum — загружаем активные треды
                                    await self._fetch_forum_threads(guild_id, channel['id'])
                except Exception as e:
                    logger.debug(f"Guild {guild_id} channels: {e}")
        
        tasks = [fetch_channels(gid) for gid in self.guilds]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        total = sum(len(ch) for ch in self.channels.values())
        logger.info(f"📊 Загружено: {len(self.guilds)} серверов, {total} каналов/тем")
    
    async def _fetch_forum_threads(self, guild_id: str, channel_id: str):
        """Загрузка активных тредов из форум-канала"""
        try:
            async with self.session.get(
                f'https://discord.com/api/v10/channels/{channel_id}/threads/active'
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                threads = data.get('threads', [])
                for t in threads:
                    self.channels[guild_id].append({
                        'id': t['id'],
                        'name': f"  └ {t['name']}",
                        'parent_id': channel_id,
                        'type': 11
                    })
        except Exception as e:
            logger.debug(f"Forum threads {channel_id}: {e}")
    
    async def connect_gateway(self):
        """Подключение к Gateway"""
        headers = {
            'Authorization': config.DISCORD_TOKEN,
            'User-Agent': self.ua.random,
            'Origin': 'https://discord.com',
        }
        
        self.ws = await websockets.connect(
            self.gateway_url,
            extra_headers=headers,
            ping_interval=None
        )
        
        # Identify
        identify = {
            'op': 2,
            'd': {
                'token': config.DISCORD_TOKEN,
                'intents': 513,  # Guilds + Guild Messages
                'properties': {
                    'os': 'Windows',
                    'browser': 'Chrome',
                    'device': '',
                    'system_locale': 'en-US'
                }
            }
        }
        await self.ws.send(json.dumps(identify))
        logger.info("🔌 Discord Gateway подключен")
        
        await self.start_heartbeat()
        
        async for message in self.ws:
            data = json.loads(message)
            await self.handle_event(data)
    
    async def start_heartbeat(self):
        """Heartbeat"""
        async def beat():
            while self.ws and not self.ws.closed:
                try:
                    await asyncio.sleep(self.heartbeat_interval / 1000 + random.uniform(0, 0.5))
                    payload = {'op': 1, 'd': self.sequence}
                    await self.ws.send(json.dumps(payload))
                    self.sequence += 1
                except:
                    break
        self.heartbeat_task = asyncio.create_task(beat())
    
    async def handle_event(self, data):
        self.sequence = data.get('s', self.sequence)
        
        if data['op'] == 10:  # Hello
            self.heartbeat_interval = data['d']['heartbeat_interval']
        
        elif data['op'] == 0 and data['t'] == 'MESSAGE_CREATE':
            await self.handle_message_create(data['d'])
    
    async def handle_message_create(self, message):
        channel_id = message['channel_id']
        if channel_id not in self.active_channels:
            return
        
        content = message.get('content', '').strip()
        if not content or len(content) > 4000:
            return
        
        prefix = self.get_channel_prefix(channel_id) or f"{channel_id}: "
        author = message['author']['global_name'] or message['author']['username']
        timestamp = message['timestamp'][:19].replace('T', ' ')
        
        tg_message = f"{prefix}{content}\n\n👤 {author}\n📅 {timestamp}"
        logger.info(f"📤 Пересылка: {channel_id} -> TG")
        await send_to_telegram(tg_message)
    
    def get_channel_prefix(self, channel_id: str) -> str:
        """Получить название канала/темы"""
        for guild_id, channels in self.channels.items():
            for ch in channels:
                if ch['id'] == channel_id:
                    return f"{ch['name']}: "
        return ""

forwarder = DiscordForwarder()

async def send_to_telegram(message: str):
    if config.TARGET_TG_CHAT_ID == 0:
        logger.warning("TARGET_TG_CHAT_ID не задан — /target <chat_id>")
        return
    try:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=config.TARGET_TG_CHAT_ID,
            text=message,
            disable_web_page_preview=True
        )
        logger.info(f"✅ Отправлено в TG {config.TARGET_TG_CHAT_ID}")
    except Exception as e:
        logger.error(f"TG отправка ошибка: {e}")

# Конфигурация
def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                config.TARGET_TG_CHAT_ID = data.get('target_tg_chat_id', 0)
        except:
            pass

def save_config():
    data = {'target_tg_chat_id': config.TARGET_TG_CHAT_ID}
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_channels():
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE, 'r') as f:
                data = json.load(f)
                forwarder.active_channels = set(data.get('active_channels', []))
        except:
            pass

def save_channels():
    data = {'active_channels': list(forwarder.active_channels)}
    with open(CHANNELS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# Telegram Bot
from telegram import Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TG_ID:
        return
    await update.message.reply_text(
        "🤖 *Discord → Telegram Bot запущен!*\n\n"
        "📋 Команды:\n"
        "• `/channels` - все каналы Discord\n"
        "• `/add <channel_id>` - добавить канал\n"
        "• `/remove <channel_id>` - удалить\n"
        "• `/active` - активные каналы\n"
        "• `/target <chat_id>` - TG чат назначения\n"
        "• `/status` - статус",
        parse_mode='Markdown'
    )

def _split_message(text: str, max_len: int = 4000) -> list:
    """Разбить сообщение для лимита Telegram (4096). Режет по строкам, не ломая эмодзи."""
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        chunk = text[:max_len]
        last_nl = chunk.rfind('\n')
        if last_nl > max_len // 2:
            chunk, text = chunk[:last_nl + 1], text[last_nl + 1:]
        else:
            text = text[max_len:]
        parts.append(chunk)
    return parts

async def channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TG_ID:
        return
    
    if not forwarder.session:
        await update.message.reply_text(
            "⏳ Discord ещё подключается... Подожди 10 сек и попробуй снова."
        )
        return
    
    if not forwarder.channels:
        msg = await update.message.reply_text("🔄 Загрузка каналов и тем...")
        try:
            await forwarder.load_guilds_and_channels()
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}")
            return
    
    text = "📋 Каналы и темы Discord:\n\n"
    for guild_id, guild in forwarder.guilds.items():
        text += f"🏛️ {guild['name']}\n"
        if guild_id in forwarder.channels:
            for ch in forwarder.channels[guild_id]:
                status = "✅" if ch['id'] in forwarder.active_channels else "⭕"
                text += f"  {status} {ch['id']} — {ch['name']}\n"
        text += "\n"
    
    for part in _split_message(text):
        await update.message.reply_text(part)

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TG_ID or not context.args:
        return
    channel_id = context.args[0]
    forwarder.active_channels.add(channel_id)
    save_channels()
    await update.message.reply_text(f"✅ Канал `{channel_id}` добавлен", parse_mode='Markdown')

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TG_ID or not context.args:
        return
    channel_id = context.args[0]
    forwarder.active_channels.discard(channel_id)
    save_channels()
    await update.message.reply_text(f"✅ Канал `{channel_id}` удален", parse_mode='Markdown')

async def list_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TG_ID:
        return
    if not forwarder.active_channels:
        await update.message.reply_text("📭 Нет активных каналов")
        return
    text = "✅ Активные каналы:\n\n"
    for cid in forwarder.active_channels:
        found = False
        for gid, channels in forwarder.channels.items():
            for ch in channels:
                if ch['id'] == cid:
                    text += f"{cid} — {ch['name']} ({forwarder.guilds[gid]['name']})\n"
                    found = True
                    break
            if found: break
    await update.message.reply_text(text)

async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TG_ID or not context.args:
        return
    try:
        chat_id = int(context.args[0])
        config.TARGET_TG_CHAT_ID = chat_id
        save_config()
        await update.message.reply_text(f"✅ TG чат: `{chat_id}`", parse_mode='Markdown')
    except:
        await update.message.reply_text("❌ Неверный ID")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TG_ID:
        return
    text = f"""📊 *Статус:*

Discord: {'✅' if forwarder.discord_user_id else '❌'}
TG чат: `{config.TARGET_TG_CHAT_ID}`
Каналов: {len(forwarder.active_channels)}/{sum(len(v) for v in forwarder.channels.values())}"""
    await update.message.reply_text(text, parse_mode='Markdown')

def main():
    print("🚀 Discord → Telegram Bot")
    print("📁 .env:", "✅" if os.path.exists('.env') else "❌")
    
    # Проверка токенов
    missing = []
    if not config.DISCORD_TOKEN: missing.append("DISCORD_TOKEN")
    if not config.TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
    if config.ADMIN_TG_ID == 0: missing.append("ADMIN_TG_ID")
    
    if missing:
        print("\n❌ Заполни .env:")
        for m in missing: print(f"  {m}=...")
        return
    
    print("\n✅ Все токены OK!")
    load_config()
    load_channels()
    
    async def run():
        app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("channels", channels))
        app.add_handler(CommandHandler("add", add_channel))
        app.add_handler(CommandHandler("remove", remove_channel))
        app.add_handler(CommandHandler("active", list_active))
        app.add_handler(CommandHandler("target", set_target))
        app.add_handler(CommandHandler("status", status))
        
        await app.initialize()
        await app.start()
        
        discord_task = asyncio.create_task(start_discord_forwarder())
        tg_task = asyncio.create_task(app.updater.start_polling(drop_pending_updates=True))
        
        await asyncio.gather(discord_task, tg_task, return_exceptions=True)
    
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n🛑 Остановка...")

async def start_discord_forwarder():
    while True:
        try:
            await forwarder.init_session()
            await forwarder.connect_gateway()
        except Exception as e:
            logger.exception("Discord reconnect")
            if forwarder.session:
                await forwarder.session.close()
                forwarder.session = None
            await asyncio.sleep(5 + random.uniform(0, 5))

if __name__ == "__main__":
    main()
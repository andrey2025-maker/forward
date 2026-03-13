import os
import json
import asyncio
import logging
import re
import random
import time
from datetime import datetime
from dotenv import load_dotenv
import aiohttp
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.FileHandler('bot.log', encoding='utf-8'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

BROWSER_FINGERPRINTS = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "sec_ch_ua_platform": '"Windows"',
        "client_build": 314005
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="121", "Google Chrome";v="121"',
        "sec_ch_ua_platform": '"Windows"',
        "client_build": 320672
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "sec_ch_ua_platform": '"macOS"',
        "client_build": 314005
    }
]

class DiscordGatewayClient:
    def __init__(self, token, tg_bot):
        self.token = token
        self.tg_bot = tg_bot
        self.ws = None
        self.heartbeat_interval = None
        self.sequence = None
        self.session_id = None
        self.channels = self.load_channels()
        self.message_cache = {}
        self.running = False
        self.current_fp = random.choice(BROWSER_FINGERPRINTS)
        self.last_heartbeat = 0

    def load_channels(self):
        try:
            with open('channels_config.json', 'r', encoding='utf-8') as f:
                return set(json.load(f))
        except:
            return set()

    def save_channels(self):
        with open('channels_config.json', 'w', encoding='utf-8') as f:
            json.dump(list(self.channels), f, indent=2)

    def get_fresh_headers(self):
        fp = random.choice(BROWSER_FINGERPRINTS)
        self.current_fp = fp
        return {
            'Authorization': self.token,
            'User-Agent': fp['user_agent'],
            'Accept': '*/*',
            'Accept-Language': random.choice(['ru-RU,ru;q=0.9,en;q=0.8', 'en-US,en;q=0.9', 'ru-RU,en;q=0.8']),
            'Sec-Ch-Ua': fp['sec_ch_ua'],
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': fp['sec_ch_ua_platform'],
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'Origin': 'https://discord.com',
            'Referer': 'https://discord.com/channels/@me',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        }

    async def connect(self):
        for attempt in range(1, 6):
            try:
                await self._connect_once()
                logger.info("✅ Discord подключён")
                return
            except Exception as e:
                wait_time = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"❌ Подключение #{attempt} ошибка: {e}. Жду {wait_time:.1f}s")
                await asyncio.sleep(wait_time)

    async def _connect_once(self):
        async with aiohttp.ClientSession() as session:
            async with session.get('https://discord.com/api/v10/gateway', 
                                 headers=self.get_fresh_headers()) as resp:
                data = await resp.json()
                gateway_url = data['url'] + f"/?v=10&encoding=json&compress=zlib-stream"

            self.ws = await aiohttp.ClientSession().ws_connect(gateway_url)
            await self.identify()
            
            self.running = True
            asyncio.create_task(self.heartbeat_loop())
            asyncio.create_task(self.message_loop())

    async def identify(self):
        build_num = self.current_fp['client_build']
        payload = {
            "op": 2,
            "d": {
                "token": self.token,
                "capabilities": 1771412421886079,
                "properties": {
                    "os": "Windows",
                    "browser": "Chrome",
                    "device": "",
                    "system_locale": "ru-RU",
                    "browser_user_agent": self.current_fp['user_agent'],
                    "browser_version": "120.0.0.0", 
                    "os_version": "10",
                    "referrer": "https://discord.com",
                    "referring_domain": "discord.com",
                    "referrer_current": "",
                    "referring_domain_current": "",
                    "release_channel": "stable",
                    "client_build_number": build_num,
                    "client_event_source": None
                },
                "compress": True,
                "client_track": "cf66e09a-af4c-44ce-9f11-34686a6e6285"
            }
        }
        await self.ws.send_json(payload)

    async def heartbeat_loop(self):
        while self.running:
            now = time.time()
            if now - self.last_heartbeat > self.heartbeat_interval / 1000 * 0.8:
                await self.ws.send_json({"op": 1, "d": self.sequence})
                self.last_heartbeat = now
            
            jitter = random.uniform(0.8, 1.2)
            await asyncio.sleep(self.heartbeat_interval / 1000 * jitter)

    async def message_loop(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                await self.handle_event(data)
            elif msg.type == aiohttp.WSMsgType.CLOSED:
                logger.warning("⚠️ WS закрыт, переподключение...")
                await self.reconnect()

    async def reconnect(self):
        self.running = False
        await asyncio.sleep(random.uniform(1, 3))
        await self.connect()

    async def handle_event(self, data):
        self.sequence = data.get('s')
        
        if data.get('t') == 'READY':
            self.heartbeat_interval = data['d']['heartbeat_interval']
            self.session_id = data['d']['session_id']
            logger.info(f"✅ Готов: {data['d']['user']['username']} (build {self.current_fp['client_build']})")
        
        elif data.get('t') == 'MESSAGE_CREATE':
            await self.handle_message(data['d'])

    def escape_md2(self, text):
        chars = r'_*[]()~`>#+-=|{}.!\\/'
        return re.sub(re.escape(chars), r'\\\g<0>', str(text)[:4000])

    async def handle_message(self, msg):
        channel_id = str(msg['channel_id'])
        if channel_id not in self.channels:
            return

        msg_id = msg['id']
        cache_key = f"{channel_id}:{msg_id}"
        if cache_key in self.message_cache:
            return
        self.message_cache[cache_key] = time.time() + 300

        active = [k for k,v in self.message_cache.items() if v > time.time()]
        if len(active) > 30:
            await asyncio.sleep(0.1)
            return

        if msg['author']['bot'] or not msg['content'].strip():
            return

        content = msg['content'].strip()
        author = msg['author']['username']
        timestamp = datetime.fromisoformat(msg['timestamp'][:-1]).strftime("%H:%M")

        channel = msg.get('channel', {})
        is_thread = channel.get('type') in [10, 11, 12]
        
        if is_thread:
            thread_name = channel.get('name', 'Тема')
            text = f"{self.escape_md2(thread_name)}: \\[{timestamp}\\] {self.escape_md2(author)}: {self.escape_md2(content)}"
        else:
            text = f"\\[{timestamp}\\] {self.escape_md2(author)}: {self.escape_md2(content)}"

        for attempt in range(3):
            try:
                await self.tg_bot.send_message(self.tg_bot.target_chat_id, text)
                logger.info(f"✅ Переслано {channel_id[:10]}...")
                break
            except Exception as e:
                logger.error(f"TG retry {attempt+1}: {e}")
                await asyncio.sleep(0.5 ** attempt)

class TelegramBot:
    def __init__(self, token, admin_id):
        self.token = token
        self.admin_id = int(admin_id)
        self.target_chat_id = None
        self.discord = None
        self.app = None
        self.bot = Bot(token=token)

    async def send_message(self, chat_id, text, parse_mode=ParseMode.MARKDOWN_V2):
        try:
            await self.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        except:
            try:
                await self.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            except:
                await self.bot.send_message(chat_id=chat_id, text=text)

    async def start(self):
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("channels", self.cmd_channels))
        self.app.add_handler(CommandHandler("threads", self.cmd_threads))
        self.app.add_handler(CommandHandler("add", self.cmd_add))
        self.app.add_handler(CommandHandler("del", self.cmd_del))
        self.app.add_handler(CommandHandler("target", self.cmd_target))
        self.app.add_handler(CommandHandler("status", self.cmd_status))

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()

    async def restricted(self, update: Update):
        return update.effective_user.id == self.admin_id

    async def cmd_start(self, update: Update, context):
        if not await self.restricted(update): return
        await update.message.reply_text(
            "🤖 *Discord → TG Bot*\n\n"
            "/channels - список\n"
            "/add ID - добавить\n"
            "/del ID - удалить\n"
            "/target ID - TG чат\n"
            "/threads - как найти ID темы\n"
            "/status - статус"
        )

    async def cmd_channels(self, update: Update, context):
        if not await self.restricted(update): return
        channels = self.discord.channels if self.discord else []
        if not channels:
            await update.message.reply_text("📭 Каналов нет")
            return
        text = "📋 *Каналы:*\n" + "\n".join(f"• `{ch}`" for ch in sorted(channels))
        await update.message.reply_text(text, parse_mode='Markdown')

    async def cmd_threads(self, update: Update, context):
        if not await self.restricted(update): return
        await update.message.reply_text(
            "🔍 *ID темы (НЕ канала!):*\n\n"
            "1. Смотри bot.log\n"
            "2. Ищи: `channel_id: 987654321`\n"
            "3. `/add 987654321`\n\n"
            "*Формат:* `😎 Название: [12:34] User: текст`"
        )

    async def cmd_add(self, update: Update, context):
        if not await self.restricted(update): return
        if not context.args:
            return await update.message.reply_text("❌ `/add 123456789`")
        
        cid = context.args[0]
        self.discord.channels.add(cid)
        self.discord.save_channels()
        await update.message.reply_text(f"✅ Добавлен `{cid}`")

    async def cmd_del(self, update: Update, context):
        if not await self.restricted(update): return
        if not context.args:
            return await update.message.reply_text("❌ `/del 123456789`")
        
        cid = context.args[0]
        self.discord.channels.discard(cid)
        self.discord.save_channels()
        await update.message.reply_text(f"✅ Удалён `{cid}`")

    async def cmd_target(self, update: Update, context):
        if not await self.restricted(update): return
        if not context.args:
            return await update.message.reply_text("❌ `/target -1001234567890`")
        
        self.target_chat_id = context.args[0]
        await update.message.reply_text(f"✅ TG чат: `{self.target_chat_id}`")

    async def cmd_status(self, update: Update, context):
        if not await self.restricted(update): return
        status = f"""📊 *Статус:*
Discord: {"✅" if self.discord and self.discord.running else "❌"}
TG цель: {self.target_chat_id or "не установлена"}
Каналов: {len(self.discord.channels) if self.discord else 0}
Кэш: {len([k for k,v in self.discord.message_cache.items() if v > time.time()])}"""
        await update.message.reply_text(status)

async def main():
    DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    ADMIN_ID = os.getenv('ADMIN_ID')

    if not all([DISCORD_TOKEN, TELEGRAM_TOKEN, ADMIN_ID]):
        logger.error("❌ Заполни .env!")
        return

    tg_bot = TelegramBot(TELEGRAM_TOKEN, ADMIN_ID)
    
    discord_client = DiscordGatewayClient(DISCORD_TOKEN, tg_bot)
    tg_bot.discord = discord_client

    discord_task = asyncio.create_task(discord_client.connect())
    tg_task = asyncio.create_task(tg_bot.start())
    
    logger.info("🚀 Бот запущен!")
    await asyncio.gather(discord_task, tg_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Остановлен")
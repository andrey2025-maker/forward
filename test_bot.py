#!/usr/bin/env python3
"""
Тест функций Discord → Telegram бота.
Запуск: python test_bot.py
Требует .env с DISCORD_TOKEN, TELEGRAM_BOT_TOKEN, ADMIN_TG_ID
"""
import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Проверка .env
def check_env():
    token = os.getenv('DISCORD_TOKEN', '')
    if not token or token == 'твой_discord_user_token_здесь':
        print("❌ DISCORD_TOKEN не задан в .env")
        return False
    print("✅ DISCORD_TOKEN OK")
    return True

async def test_load_channels():
    """Тест загрузки каналов и тем"""
    from bot import forwarder, config
    
    print("\n" + "="*50)
    print("ТЕСТ: Загрузка каналов и тем")
    print("="*50)
    
    # Создаём сессию вручную (без полного запуска бота)
    import aiohttp
    from fake_useragent import UserAgent
    
    ua = UserAgent()
    headers = {
        'Authorization': config.DISCORD_TOKEN,
        'User-Agent': ua.random,
        'Accept': '*/*',
    }
    timeout = aiohttp.ClientTimeout(total=60)
    forwarder.session = aiohttp.ClientSession(headers=headers, timeout=timeout)
    
    try:
        print("Запрос к Discord API...")
        await forwarder.load_guilds_and_channels()
        
        total_ch = sum(len(ch) for ch in forwarder.channels.values())
        print(f"\n✅ Загружено: {len(forwarder.guilds)} серверов, {total_ch} каналов/тем\n")
        
        for guild_id, guild in forwarder.guilds.items():
            chans = forwarder.channels.get(guild_id, [])
            print(f"🏛️ {guild['name']} ({len(chans)} каналов/тем)")
            for ch in chans[:5]:  # первые 5
                type_name = {0: "текст", 5: "анонс", 15: "форум", 11: "тред"}.get(ch.get('type', 0), "?")
                print(f"   {ch['id']} | {ch['name']} [{type_name}]")
            if len(chans) > 5:
                print(f"   ... и ещё {len(chans)-5}")
            print()
        
        return True
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        if forwarder.session:
            await forwarder.session.close()
            forwarder.session = None

async def test_get_channel_prefix():
    """Тест получения префикса канала"""
    from bot import forwarder
    
    print("\n" + "="*50)
    print("ТЕСТ: get_channel_prefix")
    print("="*50)
    
    if not forwarder.channels:
        print("⚠️ Сначала запусти test_load_channels")
        return False
    
    # Берём первый канал
    for gid, chans in forwarder.channels.items():
        if chans:
            ch_id = chans[0]['id']
            prefix = forwarder.get_channel_prefix(ch_id)
            print(f"Канал {ch_id} → префикс: '{prefix}'")
            assert prefix, "Префикс не должен быть пустым"
            print("✅ OK")
            return True
    
    print("⚠️ Нет каналов для теста")
    return False

async def test_message_split():
    """Тест разбиения длинных сообщений"""
    from bot import _split_message
    
    print("\n" + "="*50)
    print("ТЕСТ: _split_message")
    print("="*50)
    
    short = "Короткое"
    assert len(_split_message(short)) == 1
    print("✅ Короткое сообщение: 1 часть")
    
    long = "x" * 5000
    parts = _split_message(long)
    assert len(parts) == 2
    assert len(parts[0]) == 4000 and len(parts[1]) == 1000
    print(f"✅ Длинное 5000 символов: {len(parts)} части")
    
    return True

async def main():
    print("🧪 Тесты Discord → Telegram Bot\n")
    
    if not check_env():
        sys.exit(1)
    
    results = []
    
    results.append(("_split_message", await test_message_split()))
    results.append(("load_channels", await test_load_channels()))
    if results[-1][1]:
        results.append(("get_channel_prefix", await test_get_channel_prefix()))
    
    print("\n" + "="*50)
    print("ИТОГИ")
    print("="*50)
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    
    all_ok = all(r[1] for r in results)
    sys.exit(0 if all_ok else 1)

if __name__ == "__main__":
    asyncio.run(main())

"""Одноразовый интерактивный вход в Telegram.

Создаёт файл(ы) сессии (*.session), чтобы потом bot.py мог запускаться
неинтерактивно (например, под systemd на VPS). Запустите один раз:

    python login.py

Введите номер телефона, код из Telegram и, если включена двухфакторная
аутентификация, пароль. Если задан BOT_TOKEN — заодно проверяется бот.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient
from telethon.errors import ApiIdInvalidError
from telethon.utils import get_display_name

from config import load_config


async def main() -> None:
    cfg = load_config()

    user = TelegramClient(cfg.session_name, cfg.api_id, cfg.api_hash)
    try:
        await user.start()  # интерактивно: номер -> код -> (2FA-пароль)
    except ApiIdInvalidError:
        print(
            "\n❌ Неверная пара API_ID / API_HASH.\n"
            "   Откройте env/.env и проверьте значения — возьмите их на\n"
            "   https://my.telegram.org -> API development tools (api_id — число,\n"
            "   api_hash — строка из 32 символов). Ничего не перепутайте местами."
        )
        await user.disconnect()
        raise SystemExit(1)
    me = await user.get_me()
    print(f"✅ Вход выполнен: {get_display_name(me)} (id {me.id})")
    await user.disconnect()

    if cfg.bot_token:
        bot = TelegramClient(cfg.session_name + "_bot", cfg.api_id, cfg.api_hash)
        await bot.start(bot_token=cfg.bot_token)
        bot_me = await bot.get_me()
        print(f"✅ Бот подключён: @{bot_me.username}")
        print("   Откройте этого бота в Telegram и нажмите Start, чтобы получать отчёты.")
        await bot.disconnect()
    else:
        print("ℹ BOT_TOKEN не задан — отчёты пойдут в LOG_CHAT (по умолчанию «Избранное»).")


if __name__ == "__main__":
    asyncio.run(main())

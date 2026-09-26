"""
USER_SESSION generator — Auto-Import userbot ke liye (docs/AUTO_IMPORT.md)
==========================================================================

Apne Telegram account ka Pyrogram StringSession banata hai jo bot ke
USER_SESSION env var me jaata hai.

Kaise chalaye (apne PC / Termux pe — bot ke server pe NAHI):
    pip install electrogram
    python tools/generate_session.py

Phir pucha gaya API_ID / API_HASH (my.telegram.org se) aur phone number +
login code daalo. Last me jo string print hogi wahi USER_SESSION hai.

⚠️ Ye string tumhare account ka full access hai — kisi se share mat karo.
"""
import asyncio
import os

try:
    from pyrogram import Client
except ImportError:
    raise SystemExit("Pehle install karo:  pip install electrogram")


async def main():
    api_id = os.environ.get("API_ID") or input("API_ID: ").strip()
    api_hash = os.environ.get("API_HASH") or input("API_HASH: ").strip()
    try:
        api_id = int(api_id)
    except ValueError:
        raise SystemExit("API_ID ek number hona chahiye (my.telegram.org se milta hai).")

    print("\n📱 Phone number (+91xxxxxxxxxx) aur login code Telegram se aayenge...\n")
    async with Client(
        name="session_generator",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
    ) as app:
        me = await app.get_me()
        session = await app.export_session_string()
        print("\n" + "=" * 60)
        print(f"✅ Login: {getattr(me, 'first_name', '')} (@{getattr(me, 'username', None) or 'no-username'})")
        print("\nYe string copy karo → hosting env var  USER_SESSION  me daalo:\n")
        print(session)
        print("=" * 60)
        print("\n⚠️ Secrecy note: ye string account ka full access hai.")
        print("   Bot restart ke baad test karo:  /autoimport on")


if __name__ == "__main__":
    asyncio.run(main())

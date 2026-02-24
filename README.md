## README.md (quick start)

1. Copy .env.example to .env and fill your BOT_TOKEN and ADMIN_IDS
2. Install dependencies: pip install -r requirements.txt
3. Run the bot: python bot.py

Admin usage:
 - To upload a card: send a photo to the bot (as admin) with caption: Name | Movie | rarity | price
   Example: Luffy | One Piece | legendary | 25000
 - In a group, run /setdrop <n> to set that every n messages the bot will drop a hidden card.
 - Users claim group drops by typing: /slime <character name>

Notes:
 - The bot stores data in sqlite `data.db` by default.
 - Extend the bot by adding more commands and improving UX (images, embeds, pagination styles).

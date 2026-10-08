AMAZON MEXICO BOT - HOW TO USE
==============================

Requirements: Windows 10/11 with Microsoft Edge (already on every Windows PC) or Google Chrome.

1. Double-click AmazonMXBot.exe.
2. Paste up to 5 amazon.com.mx product links. For each one, set:
   - Quantity: how many units to buy.
   - Max price (optional): the bot won't buy if the price is above this. Leave empty for no limit.
3. Click START. A browser window opens.
   - FIRST TIME ONLY: sign in to your Amazon account in that window (including the OTP code).
     The bot remembers your login after that.
4. The bot checks stock every 10-30 seconds. When a product is in stock, it buys it
   right away using your DEFAULT shipping address and payment method.
   Each product is bought once, then the bot stops watching it.
5. Click STOP at any time.

IMPORTANT
- Leave the browser window open while the bot runs (you can minimize it).
- If Amazon asks for a CAPTCHA or OTP, the bot beeps and shows "ACTION NEEDED".
  Complete it in the browser window and the bot continues automatically.
- Set your default address and payment method in Amazon before starting.
- Keep your Amazon cart empty. Some products only have "Add to cart" (no "Buy now"),
  and for those the bot checks out the whole cart.
- "Test mode" lets you run a test without buying anything: the bot goes to the final checkout
  page but does NOT click "Place order".
- Logs and screenshots of every order: click "Open logs folder".

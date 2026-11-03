"""
Telegram bot that tracks my crypto portfolio - cost basis (including fees),
live prices, margin if I sold right now, and price history per coin.

Usage: pip install python-telegram-bot[job-queue] requests Pillow
       set BOT_TOKEN below or as an env var, then run: python main.py
"""

import calendar
import json
import logging
import os
import time
from datetime import datetime, timezone

import requests
from PIL import Image, ImageDraw, ImageFont

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

TRADE_FEE_PCT = 0.001  # binance spot trading fee, 0.1%
CHECK_INTERVAL_SECONDS = 60 * 60

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HOLDINGS_FILE = os.path.join(DATA_DIR, "holdings.json")
CHATS_FILE = os.path.join(DATA_DIR, "chats.json")
COIN_ID_CACHE_FILE = os.path.join(DATA_DIR, "coin_ids.json")
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------- storage ----------
# holdings.json: {"BTC": {"quantity": 0.0071, "spent": 500.0, "fees": 12.80}}
# spent = cost of the coins only, fees = card fee + trading fee combined

def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return json.load(f)


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_holdings() -> dict:
    return _load_json(HOLDINGS_FILE, {})


def add_holding(coin: str, quantity: float, spent: float, fees: float) -> dict:
    coin = coin.upper()
    holdings = get_holdings()
    existing = holdings.get(coin, {"quantity": 0.0, "spent": 0.0, "fees": 0.0})
    holdings[coin] = {
        "quantity": existing["quantity"] + quantity,
        "spent": existing["spent"] + spent,
        "fees": existing["fees"] + fees,
    }
    _save_json(HOLDINGS_FILE, holdings)
    return holdings[coin]


def remove_holding(coin: str) -> bool:
    coin = coin.upper()
    holdings = get_holdings()
    if coin in holdings:
        del holdings[coin]
        _save_json(HOLDINGS_FILE, holdings)
        return True
    return False


def get_registered_chats() -> list:
    return _load_json(CHATS_FILE, [])


def register_chat(chat_id: int):
    chats = get_registered_chats()
    if chat_id not in chats:
        chats.append(chat_id)
        _save_json(CHATS_FILE, chats)


# ---------- prices ----------
# coingecko first, binance as backup. no api key needed for either.

_BUILTIN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "ADA": "cardano", "BNB": "binancecoin", "DOGE": "dogecoin",
    "TON": "the-open-network", "TRX": "tron", "USDT": "tether",
}


def _get_with_retry(url: str, params: dict, timeout: int, max_retries: int = 4):
    # coingecko's free tier throws 429s under load, back off and retry instead of failing
    delay = 2.0
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
            time.sleep(delay)
            delay *= 2
    raise last_exc or requests.RequestException("request failed after retries")


def resolve_coin_id(symbol: str):
    symbol = symbol.upper()
    if symbol in _BUILTIN_IDS:
        return _BUILTIN_IDS[symbol]

    cache = _load_json(COIN_ID_CACHE_FILE, {})
    if symbol in cache:
        return cache[symbol]

    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/search",
            params={"query": symbol}, timeout=10,
        )
        r.raise_for_status()
        coins = r.json().get("coins", [])
        exact = [c for c in coins if c.get("symbol", "").upper() == symbol]
        pick = exact or coins
        if not pick:
            return None
        coin_id = pick[0]["id"]
        cache[symbol] = coin_id
        _save_json(COIN_ID_CACHE_FILE, cache)
        return coin_id
    except requests.RequestException:
        return None


def _get_price_coingecko(coin_id: str):
    r = _get_with_retry(
        "https://api.coingecko.com/api/v3/simple/price",
        {"ids": coin_id, "vs_currencies": "usd"}, timeout=10,
    )
    return r.json()[coin_id]["usd"]


def _get_price_binance(symbol: str):
    r = requests.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": f"{symbol.upper()}USDT"}, timeout=10,
    )
    r.raise_for_status()
    return float(r.json()["price"])


def get_price_usd(symbol: str):
    symbol = symbol.upper()
    coin_id = resolve_coin_id(symbol)
    if coin_id:
        try:
            return _get_price_coingecko(coin_id)
        except (requests.RequestException, KeyError, ValueError):
            pass
    try:
        return _get_price_binance(symbol)
    except (requests.RequestException, KeyError, ValueError):
        return None


def get_prices_usd(symbols: list) -> dict:
    out = {}
    for s in symbols:
        price = get_price_usd(s)
        if price is not None:
            out[s.upper()] = price
    return out


# ---------- history ----------
# coingecko's free api only goes back 365 days, so that's the cap here.
# pulls the whole year in one request and matches months against it locally -
# doing 12 separate requests gets you rate limited pretty much every time.

def _months_ago(d, months: int):
    total = d.year * 12 + (d.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def _fmt_price(price: float) -> str:
    return _fmt_money(price) if price >= 1 else f"${price:.4f}"


def _coingecko_year_series(coin_id: str) -> list:
    try:
        r = _get_with_retry(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
            {"vs_currency": "usd", "days": 365},
            timeout=20,
        )
        raw = r.json().get("prices", [])
        return [(datetime.fromtimestamp(ts / 1000, tz=timezone.utc), price) for ts, price in raw]
    except (requests.RequestException, KeyError, ValueError):
        return []


def _closest_price(series: list, target_date):
    if not series:
        return None
    target_dt = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    closest = min(series, key=lambda point: abs((point[0] - target_dt).total_seconds()))
    if abs((closest[0] - target_dt).days) > 5:
        return None
    return closest[1]


def build_price_history_text(symbol: str):
    coin_id = resolve_coin_id(symbol)
    if coin_id is None:
        return None

    symbol = symbol.upper()
    today = datetime.now(timezone.utc).date()
    now_price = get_price_usd(symbol)
    series = _coingecko_year_series(coin_id)

    lines = [f"{symbol} price history"]
    lines.append(f"Now: {_fmt_price(now_price)}" if now_price is not None else "Now: unavailable")

    if not series:
        lines.append("(couldn't reach coingecko for the history, try again in a bit)")
        return "\n".join(lines)

    for months_back in range(1, 13):
        d = _months_ago(today, months_back)
        price = _closest_price(series, d)
        label = "1 month ago" if months_back == 1 else f"{months_back} months ago"
        date_str = d.strftime("%Y-%m-%d")
        price_str = _fmt_price(price) if price is not None else "not available"
        lines.append(f"{label} ({date_str}): {price_str}")

    return "\n".join(lines)


# ---------- render ----------
# draws the portfolio table as a png: coin/qty/buy price/fees/total on the
# left (what i paid), can sell/sell fee/margin on the right (what it's worth now)

WHITE, TEXT, MUTED = "#FFFFFF", "#1A1A1A", "#6B6B6B"
BORDER, BORDER_STRONG = "#D9D9D9", "#9A9A9A"
GREEN, RED = "#1A7F37", "#CF222E"

COL_KEYS = ["coin", "quantity", "buy_price", "fees", "total", "can_sell", "sell_fee", "margin"]
COL_LABELS = ["Coin", "Quantity", "Buy price", "Fees", "Total", "Can sell", "Sell fee", "Margin"]
COL_WIDTHS = [80, 100, 110, 85, 110, 115, 90, 120]
COL_ALIGN = ["left", "right", "right", "right", "right", "right", "right", "right"]
DIVIDER_AFTER = "total"

PAD_X, PAD_TOP, PAD_BOTTOM = 28, 24, 24
TITLE_H, GROUP_HEADER_H, COL_HEADER_H, ROW_H, FOOTER_H = 36, 26, 30, 44, 50
CELL_PAD = 10


def _find_font(bold: bool, size: int):
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "C:\\Windows\\Fonts\\arialbd.ttf", "C:\\Windows\\Fonts\\seguisb.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "C:\\Windows\\Fonts\\arial.ttf", "C:\\Windows\\Fonts\\segoeui.ttf"]
    )
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


FONT_TITLE = _find_font(True, 22)
FONT_GROUP = _find_font(False, 13)
FONT_HEADER = _find_font(False, 13)
FONT_CELL = _find_font(False, 15)
FONT_CELL_BOLD = _find_font(True, 15)
FONT_FOOTER_LABEL = _find_font(False, 14)
FONT_FOOTER_VALUE = _find_font(True, 18)


def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def _fmt_qty(x: float) -> str:
    if x < 1:
        return f"{x:.6f}".rstrip("0").rstrip(".")
    return f"{x:,.4f}".rstrip("0").rstrip(".")


def _col_x_positions():
    xs = [PAD_X]
    for w in COL_WIDTHS[:-1]:
        xs.append(xs[-1] + w)
    return xs


def compute_rows(holdings: dict, prices: dict) -> list:
    rows = []
    for coin, h in sorted(holdings.items()):
        price = prices.get(coin)
        qty, spent, fees = h["quantity"], h["spent"], h["fees"]
        total_cost = spent + fees
        if price is None:
            rows.append({
                "coin": coin, "quantity": qty, "buy_price": spent, "fees": fees,
                "total": total_cost, "can_sell": None, "sell_fee": None,
                "margin": None, "margin_pct": None,
            })
            continue
        can_sell = qty * price
        sell_fee = can_sell * TRADE_FEE_PCT
        margin = (can_sell - sell_fee) - total_cost
        margin_pct = (margin / total_cost * 100) if total_cost else 0.0
        rows.append({
            "coin": coin, "quantity": qty, "buy_price": spent, "fees": fees,
            "total": total_cost, "can_sell": can_sell, "sell_fee": sell_fee,
            "margin": margin, "margin_pct": margin_pct,
        })
    return rows


def render_portfolio_image(holdings: dict, prices: dict, timestamp_label: str) -> str:
    rows = compute_rows(holdings, prices)

    width = PAD_X * 2 + sum(COL_WIDTHS)
    height = (PAD_TOP + TITLE_H + GROUP_HEADER_H + COL_HEADER_H
              + ROW_H * max(len(rows), 1) + FOOTER_H + PAD_BOTTOM)
    img = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(img)
    xs = _col_x_positions()
    divider_idx = COL_KEYS.index(DIVIDER_AFTER)
    divider_x = xs[divider_idx] + COL_WIDTHS[divider_idx] + 6

    y = PAD_TOP
    draw.text((PAD_X, y), f"Portfolio \u2014 {timestamp_label}", font=FONT_TITLE, fill=TEXT)
    y += TITLE_H

    at_purchase_start = xs[COL_KEYS.index("quantity")]
    at_purchase_end = xs[COL_KEYS.index("total")] + COL_WIDTHS[COL_KEYS.index("total")]
    right_now_start = xs[COL_KEYS.index("can_sell")]
    right_now_end = width - PAD_X
    draw.text(((at_purchase_start + at_purchase_end) / 2, y), "At purchase",
              font=FONT_GROUP, fill=MUTED, anchor="ma")
    draw.text(((right_now_start + right_now_end) / 2, y), "Right now",
              font=FONT_GROUP, fill=MUTED, anchor="ma")
    y += GROUP_HEADER_H
    draw.line([(PAD_X, y - 4), (width - PAD_X, y - 4)], fill=BORDER_STRONG, width=1)

    header_y = y
    for i, label in enumerate(COL_LABELS):
        x = xs[i] + (COL_WIDTHS[i] - CELL_PAD if COL_ALIGN[i] == "right" else CELL_PAD)
        anchor = "ra" if COL_ALIGN[i] == "right" else "la"
        draw.text((x, header_y), label, font=FONT_HEADER, fill=MUTED, anchor=anchor)
    y += COL_HEADER_H
    draw.line([(PAD_X, y - 2), (width - PAD_X, y - 2)], fill=BORDER, width=1)

    table_top = y
    for row in rows:
        row_y = y + ROW_H / 2
        cells = {
            "coin": row["coin"],
            "quantity": _fmt_qty(row["quantity"]),
            "buy_price": _fmt_money(row["buy_price"]),
            "fees": _fmt_money(row["fees"]),
            "total": _fmt_money(row["total"]),
            "can_sell": _fmt_money(row["can_sell"]) if row["can_sell"] is not None else "--",
            "sell_fee": _fmt_money(row["sell_fee"]) if row["sell_fee"] is not None else "--",
        }
        for i, key in enumerate(COL_KEYS):
            if key == "margin":
                continue
            x = xs[i] + (COL_WIDTHS[i] - CELL_PAD if COL_ALIGN[i] == "right" else CELL_PAD)
            anchor = "rm" if COL_ALIGN[i] == "right" else "lm"
            font = FONT_CELL_BOLD if key in ("coin", "total") else FONT_CELL
            draw.text((x, row_y), cells[key], font=font, fill=TEXT, anchor=anchor)

        margin_idx = COL_KEYS.index("margin")
        margin_x = xs[margin_idx] + COL_WIDTHS[margin_idx] - CELL_PAD
        if row["margin"] is not None:
            color = GREEN if row["margin"] >= 0 else RED
            amount_text = f"{'+' if row['margin'] >= 0 else '-'}{_fmt_money(abs(row['margin']))}"
            pct_text = f"({row['margin_pct']:+.1f}%)"
            draw.text((margin_x, row_y - 10), amount_text, font=FONT_CELL_BOLD, fill=color, anchor="rm")
            draw.text((margin_x, row_y + 10), pct_text, font=FONT_CELL, fill=color, anchor="rm")
        else:
            draw.text((margin_x, row_y), "price unavailable", font=FONT_CELL, fill=MUTED, anchor="rm")

        y += ROW_H
        draw.line([(PAD_X, y), (width - PAD_X, y)], fill=BORDER, width=1)

    draw.line([(divider_x, table_top - 2), (divider_x, y)], fill=BORDER_STRONG, width=1)

    valid = [r for r in rows if r["margin"] is not None]
    total_cost = sum(r["total"] for r in rows)
    total_margin = sum(r["margin"] for r in valid)
    total_margin_pct = (total_margin / total_cost * 100) if total_cost else 0.0
    footer_y = y + FOOTER_H / 2
    draw.line([(PAD_X, y + 8), (width - PAD_X, y + 8)], fill=BORDER_STRONG, width=1)
    draw.text((PAD_X, footer_y), "Total margin", font=FONT_FOOTER_LABEL, fill=MUTED, anchor="lm")
    margin_color = GREEN if total_margin >= 0 else RED
    margin_text = f"{'+' if total_margin >= 0 else '-'}{_fmt_money(abs(total_margin))} ({total_margin_pct:+.1f}%)"
    draw.text((width - PAD_X, footer_y), margin_text, font=FONT_FOOTER_VALUE, fill=margin_color, anchor="rm")

    out_path = os.path.join(DATA_DIR, "portfolio.png")
    img.save(out_path)
    return out_path


# ---------- bot ----------
# all menu-driven. buttons that need more info set an "awaiting" state on
# user_data, and the next text message gets treated as the reply to it.

BTN_PORTFOLIO = "\U0001F4CA Portfolio"
BTN_ADD = "➕ Add coin"
BTN_REMOVE = "\U0001F5D1 Remove coin"
BTN_HISTORY = "\U0001F4C8 History"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_PORTFOLIO],
        [BTN_ADD, BTN_REMOVE],
        [BTN_HISTORY],
    ],
    resize_keyboard=True,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat(update.effective_chat.id)
    context.user_data["awaiting"] = None
    await update.message.reply_text(
        "Registered. You'll get a portfolio photo every "
        f"{CHECK_INTERVAL_SECONDS // 3600} hour(s).\n\n"
        "Use the buttons below for everything.",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == BTN_PORTFOLIO:
        context.user_data["awaiting"] = None
        await send_portfolio_to_chat(context, update.effective_chat.id)
        return

    if text == BTN_ADD:
        context.user_data["awaiting"] = "add"
        await update.message.reply_text(
            "Send: COIN QUANTITY SPENT FEES\n"
            "Example: BTC 0.0071 500 12.80\n"
            "(SPENT = money paid for the coins only, FEES = card fee + trading fee combined)"
        )
        return

    if text == BTN_REMOVE:
        context.user_data["awaiting"] = "remove"
        await update.message.reply_text("Which coin do you want to remove? Send the symbol, e.g. BTC")
        return

    if text == BTN_HISTORY:
        context.user_data["awaiting"] = "history"
        await update.message.reply_text("Which coin? Send the symbol, e.g. BTC (works for any coin, not just ones you hold)")
        return

    awaiting = context.user_data.get("awaiting")

    if awaiting == "add":
        parts = text.split()
        if len(parts) != 4:
            await update.message.reply_text(
                "Didn't quite get that. Send: COIN QUANTITY SPENT FEES\nExample: BTC 0.0071 500 12.80"
            )
            return
        coin, qty_s, spent_s, fees_s = parts
        try:
            qty, spent, fees = float(qty_s), float(spent_s), float(fees_s)
        except ValueError:
            await update.message.reply_text("Quantity, spent and fees all need to be numbers.")
            return
        if resolve_coin_id(coin) is None:
            await update.message.reply_text(f"Couldn't find a coin matching '{coin.upper()}'. Check the symbol and try again.")
            return
        holding = add_holding(coin, qty, spent, fees)
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            f"Added. {coin.upper()} position is now:\n"
            f"Quantity: {holding['quantity']}\n"
            f"Spent: ${holding['spent']:,.2f}\n"
            f"Fees: ${holding['fees']:,.2f}"
        )
        return

    if awaiting == "remove":
        coin = text.split()[0] if text.split() else text
        context.user_data["awaiting"] = None
        if remove_holding(coin):
            await update.message.reply_text(f"Removed {coin.upper()} from your holdings.")
        else:
            await update.message.reply_text(f"You don't have {coin.upper()} in your holdings.")
        return

    if awaiting == "history":
        coin = (text.split()[0] if text.split() else text).upper()
        context.user_data["awaiting"] = None
        await update.message.reply_text(f"Looking up {coin}'s history, one moment...")
        summary = build_price_history_text(coin)
        if summary is None:
            await update.message.reply_text(f"Couldn't find a coin matching '{coin}'.")
            return
        await update.message.reply_text(summary)
        return

    await update.message.reply_text("Use the buttons below to get started.", reply_markup=MAIN_KEYBOARD)


async def send_portfolio_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    holdings = get_holdings()
    if not holdings:
        await context.bot.send_message(chat_id, f"No holdings yet. Tap \"{BTN_ADD}\" to add one.")
        return

    coin_prices = get_prices_usd(list(holdings.keys()))

    timestamp_label = datetime.now(timezone.utc).astimezone().strftime("%H:%M")
    image_path = render_portfolio_image(holdings, coin_prices, timestamp_label)
    with open(image_path, "rb") as f:
        await context.bot.send_photo(chat_id, photo=f)


async def auto_update_job(context: ContextTypes.DEFAULT_TYPE):
    holdings = get_holdings()
    if not holdings:
        return
    for chat_id in get_registered_chats():
        try:
            await send_portfolio_to_chat(context, chat_id)
        except Exception:
            log.exception("failed to send portfolio update to chat %s", chat_id)


def main():
    if not BOT_TOKEN:
        raise SystemExit("set BOT_TOKEN (env var or edit the top of this file) before running")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_repeating(auto_update_job, interval=CHECK_INTERVAL_SECONDS, first=10)

    log.info("bot starting")
    app.run_polling()


if __name__ == "__main__":
    main()
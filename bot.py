import requests
import datetime
import os
import json
import subprocess

# Configuration
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")  # e.g. "username/coffee-bot"
PEOPLE = ["Fabio", "Gabri", "Bounk", "Bottaz"]
STATE_FILE = "state.json"

# ---------------------------------------------------------------------------
# State helpers (debt + offset stored in state.json in the repo)
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"debts": {p: 0 for p in PEOPLE}, "offset": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def commit_state():
    """Commit state.json back to the repo via git."""
    subprocess.run(["git", "config", "user.email", "coffee-bot@github-actions"], check=True)
    subprocess.run(["git", "config", "user.name", "Coffee Bot"], check=True)
    subprocess.run(["git", "add", STATE_FILE], check=True)
    result = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if result.returncode != 0:  # there are changes to commit
        subprocess.run(["git", "commit", "-m", "chore: update coffee state [skip ci]"], check=True)
        subprocess.run(["git", "push"], check=True)

# ---------------------------------------------------------------------------
# Rotation logic
# ---------------------------------------------------------------------------

def get_payer_for_date(date, offset=0):
    start_date = datetime.date(2024, 1, 1)
    weeks_passed = (date - start_date).days // 7
    is_thursday = 1 if date.weekday() == 3 else 0
    index = (weeks_passed * 2 + is_thursday + offset) % len(PEOPLE)
    return PEOPLE[index], index

def get_today_payer(offset=0):
    return get_payer_for_date(datetime.date.today(), offset)

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def send_message(text, parse_mode=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    requests.post(url, json=payload)

def get_updates(offset_id=None):
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    params = {"timeout": 30}
    if offset_id:
        params["offset"] = offset_id
    resp = requests.get(url, params=params, timeout=35)
    return resp.json().get("result", [])

def find_person(name_fragment):
    """Case-insensitive partial match against PEOPLE list."""
    name_fragment = name_fragment.strip().lstrip("@").lower()
    matches = [p for p in PEOPLE if p.lower().startswith(name_fragment)]
    return matches[0] if len(matches) == 1 else None

# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def handle_skip(args, state):
    """
    /skip [name]  →  [name] didn't show up; move to the next person in rotation.
    If no name given, assumes today's scheduled payer.
    """
    if args:
        skipper = find_person(args[0])
        if not skipper:
            send_message(f"❓ Non ho capito chi è '{args[0]}'. Persone valide: {', '.join(PEOPLE)}")
            return
    else:
        skipper, _ = get_today_payer(state["offset"])

    # Add debt to skipper
    state["debts"][skipper] = state["debts"].get(skipper, 0) + 1

    # Advance the rotation offset by 1 so the *next* person pays instead
    state["offset"] = (state["offset"] + 1) % len(PEOPLE)
    next_payer, _ = get_today_payer(state["offset"])

    save_state(state)
    commit_state()
    send_message(
        f"⏭️ {skipper} salta il turno (debito +1).\n"
        f"☕ Adesso tocca a *{next_payer}*!",
        parse_mode="Markdown"
    )

def handle_extra(state):
    """
    /extra  →  "We're having an extra coffee today, who pays?"
    Uses the same rotation but doesn't advance the regular schedule.
    """
    payer, _ = get_today_payer(state["offset"])
    send_message(
        f"☕ Caffè extra! Tocca a *{payer}* offrire. Paga brutto cane! 💸",
        parse_mode="Markdown"
    )

def handle_debt(state):
    """/debt  →  Show current debts."""
    lines = []
    for person in PEOPLE:
        debt = state["debts"].get(person, 0)
        icon = "🔴" if debt > 0 else "✅"
        lines.append(f"{icon} {person}: {debt} caffè da restituire")
    send_message("📊 *Debiti caffè:*\n" + "\n".join(lines), parse_mode="Markdown")

def handle_paid(args, state):
    """
    /paid [name]  →  [name] paid back one of their debts.
    """
    if not args:
        send_message("Usage: /paid [nome]")
        return
    person = find_person(args[0])
    if not person:
        send_message(f"❓ Non ho capito chi è '{args[0]}'. Persone valide: {', '.join(PEOPLE)}")
        return
    if state["debts"].get(person, 0) <= 0:
        send_message(f"✅ {person} non ha debiti da saldare!")
        return
    state["debts"][person] -= 1
    save_state(state)
    commit_state()
    remaining = state["debts"][person]
    msg = f"✅ {person} ha saldato un caffè!"
    if remaining > 0:
        msg += f" Ne deve ancora {remaining}."
    send_message(msg)

def handle_oh(state):
    """/who  →  Who pays today (or next scheduled day)."""
    payer, _ = get_today_payer(state["offset"])
    today = datetime.date.today()
    send_message(f"☕ Oggi ({today.strftime('%A %d/%m')}) tocca a *{payer}*!", parse_mode="Markdown")

def handle_index():
    """
    /index  →  Current market price of Arabica coffee futures (KC=F) from Yahoo Finance.
    Uses the Yahoo Finance v8 chart API — no API key required.
    """
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/KC=F"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        currency = meta.get("currency", "USD")
        name = meta.get("shortName", "Arabica Coffee Futures")
        ts = meta.get("regularMarketTime")
        dt_str = ""
        if ts:
            dt_str = datetime.datetime.utcfromtimestamp(ts).strftime("%d/%m/%Y %H:%M UTC")

        if price is None:
            send_message("⚠️ Prezzo non disponibile al momento. Riprova tra poco.")
            return

        # Price is in cents/pound — convert to $/pound and $/kg for context
        price_usd_lb = price / 100
        price_usd_kg = price_usd_lb * 2.20462

        change_str = ""
        if prev_close:
            change = price - prev_close
            pct = (change / prev_close) * 100
            arrow = "📈" if change >= 0 else "📉"
            sign = "+" if change >= 0 else ""
            change_str = f"\n{arrow} Variazione: {sign}{change:.2f}¢ ({sign}{pct:.2f}%)"

        msg = (
            f"☕ *Caffè Arabica — Futures ({name})*\n\n"
            f"💵 Prezzo: *{price:.2f}¢/lb* ({price_usd_lb:.2f} $/lb · {price_usd_kg:.2f} $/kg)"
            f"{change_str}\n"
            f"🕐 Aggiornato: {dt_str}"
        )
        send_message(msg, parse_mode="Markdown")

    except Exception as e:
        send_message(f"⚠️ Impossibile recuperare il prezzo del caffè: {e}")

def dispatch_command(text, state):
    parts = text.strip().split()
    # Strip bot mention if present (e.g. /skip@MyBot)
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    if cmd == "/skip":
        handle_skip(args, state)
    elif cmd == "/extra":
        handle_extra(state)
    elif cmd == "/debt" or cmd == "/debts":
        handle_debt(state)
    elif cmd == "/paid":
        handle_paid(args, state)
    elif cmd == "/oh":
        handle_oh(state)
    elif cmd == "/index":
        handle_index()
    elif cmd == "/help":
        send_message(
            "☕ *Coffee Bot comandi:*\n\n"
            "/oh — Chi paga oggi?\n"
            "/extra — Caffè extra: chi paga?\n"
            "/skip [nome] — Salta il turno di qualcuno (debito +1)\n"
            "/paid [nome] — Segna che qualcuno ha saldato un debito\n"
            "/debt — Mostra i debiti di tutti\n"
            "/index — Prezzo attuale del caffè Arabica (futures)",
            parse_mode="Markdown"
        )

# ---------------------------------------------------------------------------
# Modes: scheduled reminder vs. command polling
# ---------------------------------------------------------------------------

def run_scheduled():
    """Called by the cron workflow: send the daily reminder."""
    state = load_state()
    payer, _ = get_today_payer(state["offset"])
    message = f"☕ Oggi è il turno di {payer} offrire il caffè! Paga brutto cane! 💸"
    send_message(message)

def run_polling():
    """
    Called by the long-running listener workflow.
    Polls Telegram for commands and handles them.
    Exits after ~50 minutes so GitHub Actions can restart it before the 6h limit.
    """
    state = load_state()
    last_update_id = None
    deadline = datetime.datetime.utcnow() + datetime.timedelta(minutes=50)

    print("Coffee bot listener started, polling for commands…")
    while datetime.datetime.utcnow() < deadline:
        updates = get_updates(last_update_id)
        for update in updates:
            last_update_id = update["update_id"] + 1
            message = update.get("message", {})
            text = message.get("text", "")
            # Only handle commands
            if text.startswith("/"):
                # Reload state before each command in case it changed
                state = load_state()
                dispatch_command(text, state)

if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "scheduled"
    if mode == "poll":
        run_polling()
    else:
        run_scheduled()
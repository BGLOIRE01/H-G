import os
import psycopg2
import psycopg2.extras
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = 'hg_money_transfer_secret_key'

DATABASE_URL = os.environ.get('DATABASE_URL')

USERS = {
    "admin": {"password": generate_password_hash(os.environ.get("ADMIN_PASSWORD", "changeme")), "role": "admin"},
    "staff": {"password": generate_password_hash(os.environ.get("STAFF_PASSWORD", "changeme")), "role": "staff"}
}

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

# ──────────────────────────────────────────────
# BALANCES
# ──────────────────────────────────────────────

def load_balances():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM balances WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        "usd_balance": 0.0,
        "rwf_balance": 0.0,
        "cny_balance": 0.0,
        "cad_balance": 0.0,
        "usd_rwanda_balance": 0.0,
        "total_profit_rwf": 0.0,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

def save_balances(balances):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE balances SET
            usd_balance = %s,
            rwf_balance = %s,
            cny_balance = %s,
            cad_balance = %s,
            usd_rwanda_balance = %s,
            total_profit_rwf = %s,
            last_updated = %s
        WHERE id = 1
    """, (
        balances['usd_balance'],
        balances['rwf_balance'],
        balances['cny_balance'],
        balances['cad_balance'],
        balances['usd_rwanda_balance'],
        balances['total_profit_rwf'],
        balances['last_updated']
    ))
    conn.commit()
    conn.close()

# ──────────────────────────────────────────────
# RATES
# ──────────────────────────────────────────────

def load_rates():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rates WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            "USD": {"sell_rate": row['usd_sell'], "buy_rate": row['usd_buy']},
            "CNY": {"sell_rate": row['cny_sell'], "buy_rate": row['cny_buy']},
            "CAD": {"sell_rate": row['cad_sell'], "buy_rate": row['cad_buy']},
            "USD_CAD": {"sell_rate": row['usd_cad_sell'], "buy_rate": row['usd_cad_buy']},
            "USD_CNY": {"sell_rate": row['usd_cny_sell'], "buy_rate": row['usd_cny_buy']},
            "usd_transfer_fee": row['usd_transfer_fee'],
            "last_updated": row['last_updated']
        }
    return {
        "USD": {"sell_rate": 1440.0, "buy_rate": 1485.0},
        "CNY": {"sell_rate": 200.0, "buy_rate": 210.0},
        "CAD": {"sell_rate": 1050.0, "buy_rate": 1080.0},
        "USD_CAD": {"sell_rate": 135.0, "buy_rate": 145.0},
        "USD_CNY": {"sell_rate": 7.2, "buy_rate": 7.6},
        "usd_transfer_fee": 5.0,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

def save_rates(rates):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE rates SET
            usd_sell = %s, usd_buy = %s,
            cny_sell = %s, cny_buy = %s,
            cad_sell = %s, cad_buy = %s,
            usd_cad_sell = %s, usd_cad_buy = %s,
            usd_cny_sell = %s, usd_cny_buy = %s,
            usd_transfer_fee = %s,
            last_updated = %s
        WHERE id = 1
    """, (
        rates['USD']['sell_rate'], rates['USD']['buy_rate'],
        rates['CNY']['sell_rate'], rates['CNY']['buy_rate'],
        rates['CAD']['sell_rate'], rates['CAD']['buy_rate'],
        rates['USD_CAD']['sell_rate'], rates['USD_CAD']['buy_rate'],
        rates['USD_CNY']['sell_rate'], rates['USD_CNY']['buy_rate'],
        rates['usd_transfer_fee'],
        rates['last_updated']
    ))
    conn.commit()
    conn.close()

# ──────────────────────────────────────────────
# FIFO + DEBT HELPERS
# ──────────────────────────────────────────────

def get_total_debt(conn, currency):
    cur = conn.cursor()
    cur.execute("""
        SELECT SUM(remaining_debt) as total FROM currency_debts
        WHERE currency = %s AND remaining_debt > 0
    """, (currency,))
    result = cur.fetchone()
    return result['total'] if result['total'] else 0.0


def add_batch(conn, currency, amount, sell_rate, transaction_id):
    profit_from_debt = 0.0
    remaining_to_add = amount

    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM currency_debts
        WHERE currency = %s AND remaining_debt > 0
        ORDER BY id ASC
    """, (currency,))
    debts = cur.fetchall()

    for debt in debts:
        if remaining_to_add <= 0:
            break

        debt_id = debt['id']
        debt_remaining = debt['remaining_debt']
        buy_rate_at_debt = debt['buy_rate_at_debt']

        paid = min(debt_remaining, remaining_to_add)
        profit_from_debt += paid * (buy_rate_at_debt - sell_rate)

        if (debt_remaining - paid) <= 0:
            cur.execute("UPDATE currency_debts SET remaining_debt = 0 WHERE id = %s", (debt_id,))
        else:
            cur.execute("""
                UPDATE currency_debts SET remaining_debt = %s WHERE id = %s
            """, (debt_remaining - paid, debt_id))

        cur.execute("""
            INSERT INTO debt_payment_log (transaction_id, debt_id, paid_amount)
            VALUES (%s, %s, %s)
        """, (transaction_id, debt_id, paid))

        remaining_to_add -= paid

    if remaining_to_add > 0:
        cur.execute("""
            INSERT INTO currency_batches (transaction_id, timestamp, currency, original_amount, remaining, sell_rate)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (transaction_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              currency, remaining_to_add, remaining_to_add, sell_rate))

    return profit_from_debt


def consume_batches(conn, currency, amount_needed, buy_rate, transaction_id):
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM currency_batches
        WHERE currency = %s AND remaining > 0
        ORDER BY id ASC
    """, (currency,))
    batches = cur.fetchall()

    total_profit = 0.0
    remaining_needed = amount_needed

    for batch in batches:
        if remaining_needed <= 0:
            break

        batch_id = batch['id']
        batch_remaining = batch['remaining']
        batch_sell_rate = batch['sell_rate']

        consumed = min(batch_remaining, remaining_needed)
        total_profit += consumed * (buy_rate - batch_sell_rate)

        if (batch_remaining - consumed) <= 0:
            cur.execute("UPDATE currency_batches SET remaining = 0 WHERE id = %s", (batch_id,))
        else:
            cur.execute("""
                UPDATE currency_batches SET remaining = %s WHERE id = %s
            """, (batch_remaining - consumed, batch_id))

        cur.execute("""
            INSERT INTO batch_consumption_log (transaction_id, batch_id, consumed_amount)
            VALUES (%s, %s, %s)
        """, (transaction_id, batch_id, consumed))

        remaining_needed -= consumed

    if remaining_needed > 0:
        cur.execute("""
            INSERT INTO currency_debts (transaction_id, timestamp, currency, debt_amount, buy_rate_at_debt, remaining_debt)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            transaction_id,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            currency,
            remaining_needed,
            buy_rate,
            remaining_needed
        ))

    return total_profit

# ──────────────────────────────────────────────
# APP ROUTES
# ──────────────────────────────────────────────

@app.route('/')
def index():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = USERS.get(username)
        if user and check_password_hash(user['password'], password):
            session['user'] = username
            session['role'] = user['role']
            return redirect(url_for('dashboard'))
        flash('Invalid username or password', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ──────────────────────────────────────────────
# DASHBOARD
# ──────────────────────────────────────────────

@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect(url_for('login'))

    rates = load_rates()
    balances = load_balances()

    spreads = {
        'USD': rates['USD']['buy_rate'] - rates['USD']['sell_rate'],
        'CNY': rates['CNY']['buy_rate'] - rates['CNY']['sell_rate'],
        'CAD': rates['CAD']['buy_rate'] - rates['CAD']['sell_rate'],
        'USD_CAD': rates['USD_CAD']['buy_rate'] - rates['USD_CAD']['sell_rate'],
        'USD_CNY': rates['USD_CNY']['buy_rate'] - rates['USD_CNY']['sell_rate']
    }

    conn = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    cur = conn.cursor()

    cur.execute("""
        SELECT SUM(profit) as total_profit, COUNT(id) as total_count
        FROM transactions
        WHERE timestamp LIKE %s
    """, (f'{today}%',))
    stats = cur.fetchone()
    daily_profit = stats['total_profit'] if stats['total_profit'] else 0

    debts = {}
    for currency in ['USD', 'CNY', 'CAD']:
        debts[currency] = get_total_debt(conn, currency)

    cur.execute('SELECT * FROM transactions ORDER BY id DESC LIMIT 5')
    recent = cur.fetchall()
    conn.close()

    return render_template('dashboard.html',
                           rates=rates,
                           balances=balances,
                           spreads=spreads,
                           daily_profit=daily_profit,
                           debts=debts,
                           recent=recent)

# ──────────────────────────────────────────────
# CALCULATOR
# ──────────────────────────────────────────────

@app.route('/calculator', methods=['GET', 'POST'])
def calculator():
    if 'user' not in session:
        return redirect(url_for('login'))

    rates = load_rates()
    balances = load_balances()

    if request.method == 'POST':
        tx_type = request.form.get('type')
        amount = float(request.form.get('amount'))
        client_name = request.form.get('client_name', 'Walk-in')

        if amount <= 0:
            flash("Amount must be positive", "error")
            return redirect(url_for('calculator'))

        foreign_currency = ""
        foreign_amount = 0.0
        rwf_amount = 0.0
        rate = 0.0
        profit = 0.0
        fee = 0.0

        conn = get_db()
        cur = conn.cursor()

        # ── USD RWANDA ↔ RWF ──
        if tx_type == 'USD_RWA_TO_RWF':
            foreign_currency = 'USD_RWA'
            foreign_amount = amount
            rate = rates['USD']['sell_rate']
            rwf_amount = foreign_amount * rate

            cur.execute("""
                INSERT INTO transactions
                (timestamp, transaction_type, foreign_currency, foreign_amount, rwf_amount, rate_used, profit, fee, client_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tx_type, foreign_currency,
                  foreign_amount, rwf_amount, rate, 0.0, 0.0, client_name))
            tx_id = cur.fetchone()['id']

            profit = add_batch(conn, 'USD', foreign_amount, rate, tx_id)
            cur.execute('UPDATE transactions SET profit = %s WHERE id = %s', (profit, tx_id))

            balances['usd_rwanda_balance'] = float(balances['usd_rwanda_balance']) + foreign_amount
            balances['rwf_balance'] = float(balances['rwf_balance']) - rwf_amount
            balances['total_profit_rwf'] = float(balances.get('total_profit_rwf', 0)) + profit
            balances['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_balances(balances)
            conn.commit()
            conn.close()

            flash(f"Transaction successful for {client_name}! USD RWA → RWF", "success")
            return redirect(url_for('dashboard'))

        elif tx_type == 'RWF_TO_USD_RWA':
            foreign_currency = 'USD_RWA'
            rwf_amount = amount
            rate = rates['USD']['buy_rate']
            foreign_amount = rwf_amount / rate

            cur.execute("""
                INSERT INTO transactions
                (timestamp, transaction_type, foreign_currency, foreign_amount, rwf_amount, rate_used, profit, fee, client_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tx_type, foreign_currency,
                  foreign_amount, rwf_amount, rate, 0.0, 0.0, client_name))
            tx_id = cur.fetchone()['id']

            profit = consume_batches(conn, 'USD', foreign_amount, rate, tx_id)
            cur.execute('UPDATE transactions SET profit = %s WHERE id = %s', (profit, tx_id))

            balances['rwf_balance'] = float(balances['rwf_balance']) + rwf_amount
            balances['usd_rwanda_balance'] = float(balances['usd_rwanda_balance']) - foreign_amount
            balances['total_profit_rwf'] = float(balances.get('total_profit_rwf', 0)) + profit
            balances['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_balances(balances)
            conn.commit()
            conn.close()

            flash(f"Transaction successful for {client_name}! RWF → USD RWA", "success")
            return redirect(url_for('dashboard'))

        # ── RWF HUB PAIRS (USD, CNY, CAD ↔ RWF) ──
        elif '_TO_' in tx_type and 'RWF' in tx_type:
            parts = tx_type.split('_TO_')
            from_curr = parts[0]
            to_curr = parts[1]

            if to_curr == 'RWF':
                foreign_currency = from_curr
                foreign_amount = amount
                rate = rates[foreign_currency]['sell_rate']
                rwf_amount = foreign_amount * rate

                cur.execute("""
                    INSERT INTO transactions
                    (timestamp, transaction_type, foreign_currency, foreign_amount, rwf_amount, rate_used, profit, fee, client_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tx_type, foreign_currency,
                      foreign_amount, rwf_amount, rate, 0.0, fee, client_name))
                tx_id = cur.fetchone()['id']

                profit = add_batch(conn, foreign_currency, foreign_amount, rate, tx_id)
                cur.execute('UPDATE transactions SET profit = %s WHERE id = %s', (profit, tx_id))

                balances[f"{foreign_currency.lower()}_balance"] += foreign_amount
                balances['rwf_balance'] -= rwf_amount

            elif from_curr == 'RWF':
                foreign_currency = to_curr
                rwf_amount = amount
                rate = rates[foreign_currency]['buy_rate']
                foreign_amount = rwf_amount / rate

                cur.execute("""
                    INSERT INTO transactions
                    (timestamp, transaction_type, foreign_currency, foreign_amount, rwf_amount, rate_used, profit, fee, client_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tx_type, foreign_currency,
                      foreign_amount, rwf_amount, rate, 0.0, fee, client_name))
                tx_id = cur.fetchone()['id']

                profit = consume_batches(conn, foreign_currency, foreign_amount, rate, tx_id)
                cur.execute('UPDATE transactions SET profit = %s WHERE id = %s', (profit, tx_id))

                balances['rwf_balance'] += rwf_amount
                balances[f"{foreign_currency.lower()}_balance"] -= foreign_amount

            balances['total_profit_rwf'] = float(balances.get('total_profit_rwf', 0)) + profit
            balances['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_balances(balances)
            conn.commit()
            conn.close()

            flash(f"Transaction successful for {client_name}! {tx_type.replace('_', ' ')}", "success")
            return redirect(url_for('dashboard'))

        # ── USD ↔ CAD ──
        elif tx_type == 'USD_TO_CAD':
            rate = rates['USD_CAD']['sell_rate']
            foreign_amount = amount
            cad_to_deliver = amount * rate
            rwf_amount = cad_to_deliver
            foreign_currency = 'USD_CAD'
            balances['usd_balance'] += amount
            balances['cad_balance'] -= cad_to_deliver

        elif tx_type == 'CAD_TO_USD':
            rate = rates['USD_CAD']['buy_rate']
            foreign_amount = amount
            usd_to_deliver = amount / rate
            rwf_amount = usd_to_deliver
            foreign_currency = 'USD_CAD'
            balances['cad_balance'] += amount
            balances['usd_balance'] -= usd_to_deliver

        # ── USD ↔ CNY ──
        elif tx_type == 'USD_TO_CNY':
            rate = rates['USD_CNY']['sell_rate']
            foreign_amount = amount
            cny_to_deliver = amount * rate
            rwf_amount = cny_to_deliver
            foreign_currency = 'USD_CNY'
            balances['usd_balance'] += amount
            balances['cny_balance'] -= cny_to_deliver

        elif tx_type == 'CNY_TO_USD':
            rate = rates['USD_CNY']['buy_rate']
            foreign_amount = amount
            usd_to_deliver = amount / rate
            rwf_amount = usd_to_deliver
            foreign_currency = 'USD_CNY'
            balances['cny_balance'] += amount
            balances['usd_balance'] -= usd_to_deliver

        # ── USD US → USD RWANDA ──
        elif tx_type == 'USD_US_TO_USD_RWA':
            foreign_currency = 'USD'
            fee_rate = float(rates['usd_transfer_fee'])
            foreign_amount = float(amount)
            usd_sent = foreign_amount + fee_rate
            profit = 0.0
            fee = fee_rate
            rate = 0.0
            rwf_amount = 0.0
            balances['usd_balance'] = float(balances['usd_balance']) + usd_sent
            balances['usd_rwanda_balance'] = float(balances['usd_rwanda_balance']) - foreign_amount

        # ── USD RWANDA → USD US ──
        elif tx_type == 'USD_RWA_TO_USD_US':
            foreign_currency = 'USD'
            fee_rate = float(rates['usd_transfer_fee'])
            foreign_amount = float(amount)
            usd_rwa_received = foreign_amount + fee_rate
            profit = 0.0
            fee = fee_rate
            rate = 0.0
            rwf_amount = 0.0
            balances['usd_rwanda_balance'] = float(balances['usd_rwanda_balance']) + usd_rwa_received
            balances['usd_balance'] = float(balances['usd_balance']) - foreign_amount

        balances['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_balances(balances)

        cur.execute("""
            INSERT INTO transactions
            (timestamp, transaction_type, foreign_currency, foreign_amount, rwf_amount, rate_used, profit, fee, client_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            tx_type, foreign_currency, foreign_amount,
            rwf_amount, rate, profit, fee, client_name
        ))
        conn.commit()
        conn.close()

        flash(f"Transaction successful for {client_name}! {tx_type.replace('_', ' ')}", "success")
        return redirect(url_for('dashboard'))

    conn = get_db()
    debts = {}
    for currency in ['USD', 'CNY', 'CAD']:
        debts[currency] = get_total_debt(conn, currency)
    conn.close()

    return render_template('calculator.html', rates=rates, balances=balances, debts=debts)

# ──────────────────────────────────────────────
# RATES MANAGEMENT
# ──────────────────────────────────────────────

@app.route('/rates', methods=['GET', 'POST'])
def rates_settings():
    if 'user' not in session or session['role'] != 'admin':
        flash("Admin access required", "error")
        return redirect(url_for('dashboard'))

    rates = load_rates()

    if request.method == 'POST':
        for curr in ['USD', 'CNY', 'CAD', 'USD_CAD', 'USD_CNY']:
            buy = float(request.form.get(f'{curr.lower()}_buy_rate'))
            sell = float(request.form.get(f'{curr.lower()}_sell_rate'))

            if buy < sell:
                flash(f"Warning: {curr} Buy rate is lower than Sell rate. Profit will be negative!", "warning")

            rates[curr]['buy_rate'] = buy
            rates[curr]['sell_rate'] = sell

        rates['usd_transfer_fee'] = float(request.form.get('usd_transfer_fee'))
        rates['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_rates(rates)
        flash("Exchange rates and fees updated", "success")
        return redirect(url_for('rates_settings'))

    conn = get_db()
    cur = conn.cursor()
    debts = {}
    batches = {}
    for currency in ['USD', 'CNY', 'CAD']:
        debts[currency] = get_total_debt(conn, currency)
        cur.execute("""
            SELECT * FROM currency_batches
            WHERE currency = %s AND remaining > 0
            ORDER BY id ASC
        """, (currency,))
        batches[currency] = [dict(b) for b in cur.fetchall()]
    conn.close()

    return render_template('rates.html', rates=rates, debts=debts, batches=batches)

# ──────────────────────────────────────────────
# INVENTORY + PROFIT ADJUSTMENT
# ──────────────────────────────────────────────

@app.route('/inventory/adjust', methods=['POST'])
def adjust_inventory():
    if 'user' not in session or session['role'] != 'admin':
        flash("Admin access required", "error")
        return redirect(url_for('dashboard'))

    currency = request.form.get('currency')
    action = request.form.get('action')
    amount = float(request.form.get('amount'))

    if amount <= 0:
        flash("Amount must be positive", "error")
        return redirect(url_for('rates_settings'))

    balances = load_balances()
    rates = load_rates()

    if currency == 'PROFIT_RWF':
        current = float(balances.get('total_profit_rwf', 0))
        if action == 'ADD':
            balances['total_profit_rwf'] = current + amount
        else:
            balances['total_profit_rwf'] = current - amount
        balances['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_balances(balances)
        flash(f"Profit adjusted: {action} {amount:,.0f} RWF", "success")
        return redirect(url_for('rates_settings'))

    if currency in ['USD', 'CNY', 'CAD', 'RWF', 'USD_RWANDA']:
        balance_key = f"{currency.lower()}_balance"
        if currency == 'USD_RWANDA':
            balance_key = 'usd_rwanda_balance'

        if action == 'ADD':
            balances[balance_key] += amount
            if currency in ['USD', 'CNY', 'CAD']:
                conn = get_db()
                sell_rate = rates.get(currency, {}).get('sell_rate', 0)
                add_batch(conn, currency, amount, sell_rate, transaction_id=0)
                conn.commit()
                conn.close()
        else:
            balances[balance_key] -= amount

    balances['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_balances(balances)
    flash(f"Inventory updated: {action} {amount} {currency}", "success")
    return redirect(url_for('rates_settings'))

# ──────────────────────────────────────────────
# TRANSACTIONS HISTORY
# ──────────────────────────────────────────────

@app.route('/transactions')
def transactions_history():
    if 'user' not in session:
        return redirect(url_for('login'))

    date_filter = request.args.get('date')
    currency_filter = request.args.get('currency')

    conn = get_db()
    cur = conn.cursor()
    query = 'SELECT * FROM transactions WHERE 1=1'
    params = []

    if date_filter:
        query += ' AND timestamp LIKE %s'
        params.append(f'{date_filter}%')

    if currency_filter:
        query += ' AND foreign_currency = %s'
        params.append(currency_filter)

    query += ' ORDER BY id DESC'
    cur.execute(query, params)
    transactions = cur.fetchall()
    total_profit = sum(t['profit'] for t in transactions)
    conn.close()

    return render_template('transactions.html',
                           transactions=transactions,
                           total_profit=total_profit,
                           selected_date=date_filter,
                           selected_currency=currency_filter)

# ──────────────────────────────────────────────
# UNDO TRANSACTION
# ──────────────────────────────────────────────

@app.route('/transactions/<int:tx_id>/undo', methods=['POST'])
def undo_transaction(tx_id):
    if 'user' not in session or session['role'] != 'admin':
        flash("Admin access required", "error")
        return redirect(url_for('transactions_history'))

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM transactions WHERE id = %s", (tx_id,))
    tx = cur.fetchone()
    if not tx:
        flash("Transaction not found.", "error")
        conn.close()
        return redirect(url_for('transactions_history'))

    tx_type = tx['transaction_type']
    foreign_currency = tx['foreign_currency']
    foreign_amount = float(tx['foreign_amount'])
    rwf_amount = float(tx['rwf_amount'])
    profit = float(tx['profit'])
    fee = float(tx['fee'])

    balances = load_balances()

    try:
        if '_TO_' in tx_type and 'RWF' in tx_type:
            parts = tx_type.split('_TO_')
            from_curr = parts[0]
            to_curr = parts[1]

            if to_curr == 'RWF':
                # add_batch was called: may have created a batch and/or paid debts
                cur.execute("SELECT * FROM currency_batches WHERE transaction_id = %s", (tx_id,))
                created_batch = cur.fetchone()

                if created_batch:
                    original = float(created_batch['original_amount'])
                    remaining = float(created_batch['remaining'])
                    if abs(remaining - original) > 0.001:
                        flash(
                            f"Cannot undo #{tx_id}: {remaining:,.4f} of {original:,.4f}"
                            f" {foreign_currency} batch has already been consumed by later transactions.",
                            "error"
                        )
                        conn.close()
                        return redirect(url_for('transactions_history'))
                    cur.execute("DELETE FROM currency_batches WHERE id = %s", (created_batch['id'],))

                cur.execute("SELECT * FROM debt_payment_log WHERE transaction_id = %s", (tx_id,))
                debt_payments = cur.fetchall()
                for dp in debt_payments:
                    paid = float(dp['paid_amount'])
                    cur.execute("SELECT remaining_debt FROM currency_debts WHERE id = %s", (dp['debt_id'],))
                    debt = cur.fetchone()
                    if debt:
                        cur.execute(
                            "UPDATE currency_debts SET remaining_debt = %s WHERE id = %s",
                            (float(debt['remaining_debt']) + paid, dp['debt_id'])
                        )
                cur.execute("DELETE FROM debt_payment_log WHERE transaction_id = %s", (tx_id,))

                _bal_key = 'usd_rwanda_balance' if foreign_currency == 'USD_RWA' else f"{foreign_currency.lower()}_balance"
                balances[_bal_key] -= foreign_amount
                balances['rwf_balance'] += rwf_amount

            elif from_curr == 'RWF':
                # consume_batches was called: may have consumed batches and/or created a debt
                cur.execute("SELECT * FROM batch_consumption_log WHERE transaction_id = %s", (tx_id,))
                consumptions = cur.fetchall()

                for c in consumptions:
                    cur.execute("""
                        SELECT COUNT(*) as cnt FROM batch_consumption_log
                        WHERE batch_id = %s AND transaction_id > %s
                    """, (c['batch_id'], tx_id))
                    later = cur.fetchone()
                    if later['cnt'] > 0:
                        flash(
                            f"Cannot undo #{tx_id}: consumed inventory has been re-used by later transactions.",
                            "error"
                        )
                        conn.close()
                        return redirect(url_for('transactions_history'))

                cur.execute("SELECT * FROM currency_debts WHERE transaction_id = %s", (tx_id,))
                created_debt = cur.fetchone()
                if created_debt:
                    cur.execute(
                        "SELECT SUM(paid_amount) as total FROM debt_payment_log WHERE debt_id = %s",
                        (created_debt['id'],)
                    )
                    result = cur.fetchone()
                    if result['total'] and float(result['total']) > 0.001:
                        flash(
                            f"Cannot undo #{tx_id}: the shortfall debt created by this transaction has already been partially settled.",
                            "error"
                        )
                        conn.close()
                        return redirect(url_for('transactions_history'))
                    cur.execute("DELETE FROM currency_debts WHERE id = %s", (created_debt['id'],))

                for c in consumptions:
                    cur.execute(
                        "UPDATE currency_batches SET remaining = remaining + %s WHERE id = %s",
                        (float(c['consumed_amount']), c['batch_id'])
                    )
                cur.execute("DELETE FROM batch_consumption_log WHERE transaction_id = %s", (tx_id,))

                balances['rwf_balance'] -= rwf_amount
                _bal_key = 'usd_rwanda_balance' if foreign_currency == 'USD_RWA' else f"{foreign_currency.lower()}_balance"
                balances[_bal_key] += foreign_amount

            balances['total_profit_rwf'] = float(balances.get('total_profit_rwf', 0)) - profit

        elif tx_type == 'USD_TO_CAD':
            balances['usd_balance'] -= foreign_amount
            balances['cad_balance'] += rwf_amount

        elif tx_type == 'CAD_TO_USD':
            balances['cad_balance'] -= foreign_amount
            balances['usd_balance'] += rwf_amount

        elif tx_type == 'USD_TO_CNY':
            balances['usd_balance'] -= foreign_amount
            balances['cny_balance'] += rwf_amount

        elif tx_type == 'CNY_TO_USD':
            balances['cny_balance'] -= foreign_amount
            balances['usd_balance'] += rwf_amount

        elif tx_type == 'USD_US_TO_USD_RWA':
            balances['usd_balance'] -= (foreign_amount + fee)
            balances['usd_rwanda_balance'] += foreign_amount

        elif tx_type == 'USD_RWA_TO_USD_US':
            balances['usd_rwanda_balance'] -= (foreign_amount + fee)
            balances['usd_balance'] += foreign_amount

        balances['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_balances(balances)

        cur.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
        conn.commit()
        conn.close()

        flash(f"Transaction #{tx_id} ({tx_type.replace('_', ' ')}) has been undone and all balances restored.", "success")

    except Exception as e:
        conn.rollback()
        conn.close()
        flash(f"Failed to undo transaction #{tx_id}: {str(e)}", "error")

    return redirect(url_for('transactions_history'))

# ──────────────────────────────────────────────
# REPORTS
# ──────────────────────────────────────────────

@app.route('/reports/monthly_reports/<filename>')
def serve_report(filename):
    if 'user' not in session:
        return redirect(url_for('login'))
    from flask import send_from_directory
    reports_dir = os.path.join('/app', 'reports', 'monthly_reports')
    return send_from_directory(reports_dir, filename)

@app.route('/reports')
def monthly_reports():
    if 'user' not in session:
        return redirect(url_for('login'))
    reports_dir = os.path.join('/app', 'reports', 'monthly_reports')
    os.makedirs(reports_dir, exist_ok=True)
    reports = [f for f in os.listdir(reports_dir) if f.endswith('.pdf')]
    return render_template('reports.html', reports=reports)

@app.route('/reports/generate', methods=['POST'])
def generate_report():
    if 'user' not in session or session['role'] != 'admin':
        flash("Admin access required", "error")
        return redirect(url_for('monthly_reports'))

    import subprocess
    script_path = os.path.join('/app', 'scripts', 'generate_monthly_report.py')

    try:
        script_env = os.environ.copy()
        result = subprocess.run(
            ['python3', script_path],
            capture_output=True,
            text=True,
            timeout=60,
            env=script_env
        )
        if result.returncode == 0:
            balances = load_balances()
            balances['total_profit_rwf'] = 0.0
            balances['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_balances(balances)
            flash("Monthly report generated successfully! Database reset for new month.", "success")
        else:
            flash(f"Report generation failed: {result.stderr}", "error")
    except Exception as e:
        flash(f"Error running report script: {str(e)}", "error")

    return redirect(url_for('monthly_reports'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
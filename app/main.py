import os
import json
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = 'hg_money_transfer_secret_key'

# Paths — fixed for Docker volume persistence
BASE_DIR = '/app'
DATA_DIR = '/app/data'
RATES_FILE = os.path.join(DATA_DIR, 'rates.json')
BALANCES_FILE = os.path.join(DATA_DIR, 'balances.json')
DB_FILE = os.path.join(DATA_DIR, 'transactions.db')

os.makedirs(DATA_DIR, exist_ok=True)

USERS = {
    "admin": {"password": generate_password_hash("admin123"), "role": "admin"},
    "staff": {"password": generate_password_hash("staff123"), "role": "staff"}
}

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'")
    table_exists = cursor.fetchone()

    if table_exists:
        cursor.execute("PRAGMA table_info(transactions)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'foreign_amount' not in columns or 'profit' not in columns or 'fee' not in columns:
            print("Old schema detected. Resetting database...")
            cursor.execute("DROP TABLE transactions")
            conn.commit()

    # Main transactions table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            transaction_type TEXT NOT NULL,
            foreign_currency TEXT,
            foreign_amount REAL NOT NULL,
            rwf_amount REAL NOT NULL,
            rate_used REAL NOT NULL,
            profit REAL NOT NULL,
            fee REAL DEFAULT 0 NOT NULL,
            client_name TEXT NOT NULL
        )
    ''')

    # FIFO batches table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS currency_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            currency TEXT NOT NULL,
            original_amount REAL NOT NULL,
            remaining REAL NOT NULL,
            sell_rate REAL NOT NULL
        )
    ''')

    # Debt table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS currency_debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            currency TEXT NOT NULL,
            debt_amount REAL NOT NULL,
            buy_rate_at_debt REAL NOT NULL,
            remaining_debt REAL NOT NULL
        )
    ''')

    # Batch consumption log
    conn.execute('''
        CREATE TABLE IF NOT EXISTS batch_consumption_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            batch_id INTEGER NOT NULL,
            consumed_amount REAL NOT NULL
        )
    ''')

    # Debt payment log
    conn.execute('''
        CREATE TABLE IF NOT EXISTS debt_payment_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            debt_id INTEGER NOT NULL,
            paid_amount REAL NOT NULL
        )
    ''')

    # Migrations
    cursor.execute("PRAGMA table_info(transactions)")
    columns = [column[1] for column in cursor.fetchall()]

    if 'foreign_currency' not in columns:
        cursor.execute("ALTER TABLE transactions ADD COLUMN foreign_currency TEXT")
    if 'foreign_amount' not in columns:
        if 'usd_amount' in columns:
            cursor.execute("ALTER TABLE transactions RENAME COLUMN usd_amount TO foreign_amount")
        else:
            cursor.execute("ALTER TABLE transactions ADD COLUMN foreign_amount REAL DEFAULT 0")

    cursor.execute("UPDATE transactions SET foreign_currency = 'USD' WHERE foreign_currency IS NULL")

    cursor.execute("PRAGMA table_info(transactions)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'staff_member' in columns and 'client_name' not in columns:
        cursor.execute("ALTER TABLE transactions RENAME COLUMN staff_member TO client_name")
        conn.commit()

    # Migrate currency_batches — add transaction_id if missing
    cursor.execute("PRAGMA table_info(currency_batches)")
    batch_cols = [col[1] for col in cursor.fetchall()]
    if 'transaction_id' not in batch_cols:
        cursor.execute("ALTER TABLE currency_batches ADD COLUMN transaction_id INTEGER NOT NULL DEFAULT 0")

    # Migrate currency_debts — add transaction_id if missing
    cursor.execute("PRAGMA table_info(currency_debts)")
    debt_cols = [col[1] for col in cursor.fetchall()]
    if 'transaction_id' not in debt_cols:
        cursor.execute("ALTER TABLE currency_debts ADD COLUMN transaction_id INTEGER NOT NULL DEFAULT 0")

    conn.commit()
    conn.close()

# ──────────────────────────────────────────────
# FIFO + DEBT HELPERS
# ──────────────────────────────────────────────

def get_total_debt(conn, currency):
    result = conn.execute('''
        SELECT SUM(remaining_debt) as total FROM currency_debts
        WHERE currency = ? AND remaining_debt > 0
    ''', (currency,)).fetchone()
    return result['total'] if result['total'] else 0.0


def add_batch(conn, currency, amount, sell_rate, transaction_id):
    """
    Pay off debts first, then create batch with remainder.
    Returns profit from debt repayment.
    """
    profit_from_debt = 0.0
    remaining_to_add = amount

    debts = conn.execute('''
        SELECT * FROM currency_debts
        WHERE currency = ? AND remaining_debt > 0
        ORDER BY id ASC
    ''', (currency,)).fetchall()

    for debt in debts:
        if remaining_to_add <= 0:
            break

        debt_id = debt['id']
        debt_remaining = debt['remaining_debt']
        buy_rate_at_debt = debt['buy_rate_at_debt']

        paid = min(debt_remaining, remaining_to_add)
        profit_from_debt += paid * (buy_rate_at_debt - sell_rate)

        conn.execute('''
            UPDATE currency_debts SET remaining_debt = ? WHERE id = ?
        ''', (debt_remaining - paid, debt_id))

        conn.execute('''
            INSERT INTO debt_payment_log (transaction_id, debt_id, paid_amount)
            VALUES (?, ?, ?)
        ''', (transaction_id, debt_id, paid))

        remaining_to_add -= paid

    if remaining_to_add > 0:
        conn.execute('''
            INSERT INTO currency_batches (transaction_id, timestamp, currency, original_amount, remaining, sell_rate)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (transaction_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              currency, remaining_to_add, remaining_to_add, sell_rate))

    return profit_from_debt


def consume_batches(conn, currency, amount_needed, buy_rate, transaction_id):
    """
    Consume FIFO batches oldest first.
    Create debt if batches exhausted.
    Returns total profit.
    """
    batches = conn.execute('''
        SELECT * FROM currency_batches
        WHERE currency = ? AND remaining > 0
        ORDER BY id ASC
    ''', (currency,)).fetchall()

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

        conn.execute('''
            UPDATE currency_batches SET remaining = ? WHERE id = ?
        ''', (batch_remaining - consumed, batch_id))

        conn.execute('''
            INSERT INTO batch_consumption_log (transaction_id, batch_id, consumed_amount)
            VALUES (?, ?, ?)
        ''', (transaction_id, batch_id, consumed))

        remaining_needed -= consumed

    if remaining_needed > 0:
        conn.execute('''
            INSERT INTO currency_debts (transaction_id, timestamp, currency, debt_amount, buy_rate_at_debt, remaining_debt)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            transaction_id,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            currency,
            remaining_needed,
            buy_rate,
            remaining_needed
        ))

    return total_profit

# ──────────────────────────────────────────────
# JSON HELPERS
# ──────────────────────────────────────────────

def load_json(filepath, defaults):
    if not os.path.exists(filepath):
        with open(filepath, 'w') as f:
            json.dump(defaults, f, indent=4)
        return defaults
    with open(filepath, 'r') as f:
        return json.load(f)

def save_json(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=4)

def load_rates():
    return load_json(RATES_FILE, {
        "USD": {"sell_rate": 1440.0, "buy_rate": 1485.0},
        "CNY": {"sell_rate": 200.0, "buy_rate": 210.0},
        "CAD": {"sell_rate": 1050.0, "buy_rate": 1080.0},
        "USD_CAD": {"sell_rate": 135.0, "buy_rate": 145.0},
        "USD_CNY": {"sell_rate": 7.2, "buy_rate": 7.6},
        "usd_transfer_fee": 5.0,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

def load_balances():
    return load_json(BALANCES_FILE, {
        "usd_balance": 10000.0,
        "rwf_balance": 15000000.0,
        "cny_balance": 0.0,
        "cad_balance": 0.0,
        "usd_rwanda_balance": 0.0,
        "total_profit_rwf": 0.0,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

# ──────────────────────────────────────────────
# APP SETUP
# ──────────────────────────────────────────────

@app.before_request
def setup():
    if not hasattr(app, '_initialized'):
        init_db()
        app._initialized = True

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

    stats = conn.execute('''
        SELECT SUM(profit) as total_profit, COUNT(id) as total_count
        FROM transactions
        WHERE timestamp LIKE ?
    ''', (f'{today}%',)).fetchone()

    daily_profit = stats['total_profit'] if stats['total_profit'] else 0

    debts = {}
    for currency in ['USD', 'CNY', 'CAD']:
        debts[currency] = get_total_debt(conn, currency)

    recent = conn.execute('SELECT * FROM transactions ORDER BY id DESC LIMIT 5').fetchall()
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

        # ── RWF HUB PAIRS (USD, CNY, CAD ↔ RWF) ──
        if '_TO_' in tx_type and 'RWF' in tx_type:
            parts = tx_type.split('_TO_')
            from_curr = parts[0]
            to_curr = parts[1]

            if to_curr == 'RWF':
                # FOREIGN → RWF
                foreign_currency = from_curr
                foreign_amount = amount
                rate = rates[foreign_currency]['sell_rate']
                rwf_amount = foreign_amount * rate

                cursor = conn.execute('''
                    INSERT INTO transactions
                    (timestamp, transaction_type, foreign_currency, foreign_amount, rwf_amount, rate_used, profit, fee, client_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tx_type, foreign_currency,
                      foreign_amount, rwf_amount, rate, 0.0, fee, client_name))
                tx_id = cursor.lastrowid

                profit = add_batch(conn, foreign_currency, foreign_amount, rate, tx_id)
                conn.execute('UPDATE transactions SET profit = ? WHERE id = ?', (profit, tx_id))

                balances[f"{foreign_currency.lower()}_balance"] += foreign_amount
                balances['rwf_balance'] -= rwf_amount

            elif from_curr == 'RWF':
                # RWF → FOREIGN
                foreign_currency = to_curr
                rwf_amount = amount
                rate = rates[foreign_currency]['buy_rate']
                foreign_amount = rwf_amount / rate

                cursor = conn.execute('''
                    INSERT INTO transactions
                    (timestamp, transaction_type, foreign_currency, foreign_amount, rwf_amount, rate_used, profit, fee, client_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tx_type, foreign_currency,
                      foreign_amount, rwf_amount, rate, 0.0, fee, client_name))
                tx_id = cursor.lastrowid

                profit = consume_batches(conn, foreign_currency, foreign_amount, rate, tx_id)
                conn.execute('UPDATE transactions SET profit = ? WHERE id = ?', (profit, tx_id))

                balances['rwf_balance'] += rwf_amount
                balances[f"{foreign_currency.lower()}_balance"] -= foreign_amount

            # Update cumulative profit in balances
            balances['total_profit_rwf'] = float(balances.get('total_profit_rwf', 0)) + profit
            balances['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_json(BALANCES_FILE, balances)
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

        balances['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_json(BALANCES_FILE, balances)

        conn.execute('''
            INSERT INTO transactions
            (timestamp, transaction_type, foreign_currency, foreign_amount, rwf_amount, rate_used, profit, fee, client_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
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
        save_json(RATES_FILE, rates)
        flash("Exchange rates and fees updated", "success")
        return redirect(url_for('rates_settings'))

    conn = get_db()
    debts = {}
    batches = {}
    for currency in ['USD', 'CNY', 'CAD']:
        debts[currency] = get_total_debt(conn, currency)
        currency_batches = conn.execute('''
            SELECT * FROM currency_batches
            WHERE currency = ? AND remaining > 0
            ORDER BY id ASC
        ''', (currency,)).fetchall()
        batches[currency] = [dict(b) for b in currency_batches]
    conn.close()

    return render_template('rates.html', rates=rates, debts=debts, batches=batches)
# ──────────────────────────────────────────────
# INVENTORY + PROFIT ADJUSTMENT (Admin only)
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

    # Handle profit adjustment separately
    if currency == 'PROFIT_RWF':
        current = float(balances.get('total_profit_rwf', 0))
        if action == 'ADD':
            balances['total_profit_rwf'] = current + amount
        else:
            balances['total_profit_rwf'] = current - amount
        balances['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_json(BALANCES_FILE, balances)
        flash(f"Profit adjusted: {action} {amount:,.0f} RWF", "success")
        return redirect(url_for('rates_settings'))

    # Handle regular currency inventory
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
    save_json(BALANCES_FILE, balances)
    flash(f"Inventory updated: {action} {amount} {currency}", "success")
    return redirect(url_for('rates_settings'))

# ──────────────────────────────────────────────
# TRANSACTION HISTORY (view only, no delete)
# ──────────────────────────────────────────────

@app.route('/transactions')
def transactions_history():
    if 'user' not in session:
        return redirect(url_for('login'))

    date_filter = request.args.get('date')
    currency_filter = request.args.get('currency')

    conn = get_db()
    query = 'SELECT * FROM transactions WHERE 1=1'
    params = []

    if date_filter:
        query += ' AND timestamp LIKE ?'
        params.append(f'{date_filter}%')

    if currency_filter:
        query += ' AND foreign_currency = ?'
        params.append(currency_filter)

    query += ' ORDER BY id DESC'
    transactions = conn.execute(query, params).fetchall()
    total_profit = sum(t['profit'] for t in transactions)
    conn.close()

    return render_template('transactions.html',
                           transactions=transactions,
                           total_profit=total_profit,
                           selected_date=date_filter,
                           selected_currency=currency_filter)

# ──────────────────────────────────────────────
# REPORTS
# ──────────────────────────────────────────────

@app.route('/reports/monthly_reports/<filename>')
def serve_report(filename):
    if 'user' not in session:
        return redirect(url_for('login'))
    reports_dir = os.path.join(BASE_DIR, 'reports', 'monthly_reports')
    from flask import send_from_directory
    return send_from_directory(reports_dir, filename)

@app.route('/reports')
def monthly_reports():
    if 'user' not in session:
        return redirect(url_for('login'))
    reports_dir = os.path.join(BASE_DIR, 'reports', 'monthly_reports')
    os.makedirs(reports_dir, exist_ok=True)
    reports = [f for f in os.listdir(reports_dir) if f.endswith('.pdf')]
    return render_template('reports.html', reports=reports)



@app.route('/reports/generate', methods=['POST'])
def generate_report():
    if 'user' not in session or session['role'] != 'admin':
        flash("Admin access required", "error")
        return redirect(url_for('monthly_reports'))

    import subprocess
    script_path = os.path.join(BASE_DIR, 'scripts', 'generate_monthly_report.py')
    
    try:
        script_env = os.environ.copy()
        script_env['DATA_DIR'] = DATA_DIR
        result = subprocess.run(
            ['python3', script_path],
            capture_output=True,
            text=True,
            timeout=60,
            env=script_env
        )
        if result.returncode == 0:
            # Reset cumulative profit directly from the Flask app
            # (the scripts/ dir is not volume-mounted, so the subprocess
            #  may run a stale copy that doesn't reset properly)
            balances = load_balances()
            balances['total_profit_rwf'] = 0.0
            balances['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_json(BALANCES_FILE, balances)

            flash("Monthly report generated successfully! Database reset for new month.", "success")
        else:
            flash(f"Report generation failed: {result.stderr}", "error")
    except Exception as e:
        flash(f"Error running report script: {str(e)}", "error")

    return redirect(url_for('monthly_reports'))
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

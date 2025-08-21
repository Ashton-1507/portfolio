
import sqlite3
import requests
import schedule
import threading
import time
import csv
from flask import Flask, render_template, request, g, redirect, url_for, session, flash, send_file
from werkzeug.security import generate_password_hash, check_password_hash
import os

DATABASE = 'crypto.db'
API_URL = 'https://api.coingecko.com/api/v3/coins/markets'
COIN_DETAIL_URL = 'https://api.coingecko.com/api/v3/coins/'
UPDATE_INTERVAL = 5  # minutes
PER_PAGE = 10

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- DB helpers ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS prices (
            id TEXT PRIMARY KEY,
            symbol TEXT,
            name TEXT,
            price REAL,
            last_updated TEXT,
            price_change_24h REAL,
            price_change_percentage_24h REAL,
            price_change_percentage_7d REAL,
            description TEXT,
            homepage TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS favorites (
            user TEXT,
            coin_id TEXT,
            PRIMARY KEY (user, coin_id)
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT
        )''')
        db.commit()

# --- API fetch ---
def fetch_prices():
    params = {
        'vs_currency': 'usd',
        'order': 'market_cap_desc',
        'per_page': 50,
        'page': 1,
        'sparkline': 'false',
        'price_change_percentage': '7d'
    }
    try:
        response = requests.get(API_URL, params=params)
        data = response.json()
        with app.app_context():
            db = get_db()
            cursor = db.cursor()
            for coin in data:
                # Fetch description and homepage
                desc = ''
                homepage = ''
                try:
                    detail = requests.get(COIN_DETAIL_URL + coin['id']).json()
                    desc = detail.get('description', {}).get('en', '')
                    homepage = detail.get('links', {}).get('homepage', [''])[0]
                except Exception:
                    pass
                cursor.execute('''REPLACE INTO prices (id, symbol, name, price, last_updated, price_change_24h, price_change_percentage_24h, price_change_percentage_7d, description, homepage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (coin['id'], coin['symbol'], coin['name'], coin['current_price'], coin['last_updated'], coin.get('price_change_24h', 0), coin.get('price_change_percentage_24h', 0), coin.get('price_change_percentage_7d_in_currency', 0), desc, homepage))
            db.commit()
    except Exception as e:
        print('Error fetching prices:', e)

# --- Scheduler ---
def run_scheduler():
    schedule.every(UPDATE_INTERVAL).minutes.do(fetch_prices)
    while True:
        schedule.run_pending()
        time.sleep(1)

def start_scheduler():
    t = threading.Thread(target=run_scheduler)
    t.daemon = True
    t.start()

# --- Helper functions ---
def get_favorites(user):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT coin_id FROM favorites WHERE user=?', (user,))
    return set(row[0] for row in cursor.fetchall())

def toggle_favorite(user, coin_id):
    db = get_db()
    cursor = db.cursor()
    favs = get_favorites(user)
    if coin_id in favs:
        cursor.execute('DELETE FROM favorites WHERE user=? AND coin_id=?', (user, coin_id))
    else:
        cursor.execute('INSERT OR IGNORE INTO favorites (user, coin_id) VALUES (?, ?)', (user, coin_id))
    db.commit()

# --- Routes ---
@app.route('/', methods=['GET'])
def index():
    query = request.args.get('q', '').lower()
    page = int(request.args.get('page', 1))
    show_favs = request.args.get('favorites', '') == '1'
    db = get_db()
    cursor = db.cursor()
    user = session.get('username', '')
    favs = get_favorites(user) if user else set()
    sql = 'SELECT * FROM prices'
    params = []
    if query:
        sql += ' WHERE name LIKE ? OR symbol LIKE ?'
        params += [f'%{query}%', f'%{query}%']
    if show_favs and user:
        sql += ' AND id IN ({})'.format(','.join('?'*len(favs))) if query else ' WHERE id IN ({})'.format(','.join('?'*len(favs)))
        params += list(favs)
    sql += ' ORDER BY price DESC LIMIT ? OFFSET ?'
    params += [PER_PAGE, PER_PAGE*(page-1)]
    cursor.execute(sql, params)
    coins = cursor.fetchall()
    cursor.execute('SELECT COUNT(*) FROM prices')
    total = cursor.fetchone()[0]
    return render_template('index.html', coins=coins, query=query, page=page, per_page=PER_PAGE, total=total, favorites=favs, show_favs=show_favs, user=user)

@app.route('/coin/<coin_id>')
def coin_detail(coin_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM prices WHERE id=?', (coin_id,))
    coin = cursor.fetchone()
    return render_template('coin_detail.html', coin=coin)

@app.route('/favorite/<coin_id>', methods=['POST'])
def favorite(coin_id):
    user = session.get('username', '')
    if not user:
        flash('Login required to favorite coins.')
        return redirect(url_for('login'))
    toggle_favorite(user, coin_id)
    return redirect(request.referrer or url_for('index'))

@app.route('/export')
def export():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM prices')
    coins = cursor.fetchall()
    filename = 'crypto_prices.csv'
    with open(filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['ID', 'Symbol', 'Name', 'Price', 'Last Updated', '24h Change', '24h %', '7d %', 'Description', 'Homepage'])
        for coin in coins:
            writer.writerow(coin)
    return send_file(filename, as_attachment=True)

# --- Auth routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT password FROM users WHERE username=?', (username,))
        row = cursor.fetchone()
        if row and check_password_hash(row[0], password):
            session['username'] = username
            return redirect(url_for('index'))
        else:
            flash('Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('index'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT * FROM users WHERE username=?', (username,))
        if cursor.fetchone():
            flash('Username already exists')
        else:
            cursor.execute('INSERT INTO users (username, password) VALUES (?, ?)', (username, generate_password_hash(password)))
            db.commit()
            flash('Registration successful. Please log in.')
            return redirect(url_for('login'))
    return render_template('register.html')

if __name__ == '__main__':
    init_db()
    fetch_prices()  # Initial fetch
    start_scheduler()
    app.run(debug=True)

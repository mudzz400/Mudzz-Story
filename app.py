import os, sqlite3, secrets, uuid, time, re, hmac, hashlib, smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from decimal import Decimal, InvalidOperation
import requests
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
from flask import Flask, request, redirect, url_for, session, flash, render_template, abort, send_from_directory, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
try:
    from authlib.integrations.flask_client import OAuth
except ImportError:
    OAuth = None

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE, '.env')

# Load this project's .env explicitly. If python-dotenv is not installed,
# parse simple KEY=VALUE lines as a fallback so SMTP settings still load.
if load_dotenv:
    load_dotenv(ENV_FILE, override=False)
elif os.path.isfile(ENV_FILE):
    with open(ENV_FILE, 'r', encoding='utf-8') as env_handle:
        for raw_line in env_handle:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
                value = value[1:-1]
            os.environ.setdefault(key, value)
SITE_URL = os.environ.get('SITE_URL', '').strip().rstrip('/')
DB = os.path.join(BASE, 'store.db')
UPLOAD_DIR = os.path.join(BASE, 'static', 'uploads')
DIGITAL_DIR = os.path.join(BASE, 'private_downloads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DIGITAL_DIR, exist_ok=True)
QR_DIR = os.path.join(BASE, 'static', 'qr_deposits')
os.makedirs(QR_DIR, exist_ok=True)
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 55 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = UPLOAD_DIR
app.config['PREFERRED_URL_SCHEME'] = 'https' if SITE_URL.startswith('https://') else 'http'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = SITE_URL.startswith('https://')

ALLOWED = {'png','jpg','jpeg','webp','gif'}
DIGITAL_ALLOWED = {'zip','pdf','txt','py','json','csv','xlsx','docx','pptx','mp3','mp4','png','jpg','jpeg','webp','apk','rar','7z'}
NASIONALPAY_URL = 'https://merchant.nasionalpay.com'


def db():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con

def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', balance INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, email TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '', price INTEGER NOT NULL CHECK(price>=0), stock INTEGER NOT NULL DEFAULT -1, image TEXT DEFAULT '', digital_file TEXT DEFAULT '', digital_filename TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), product_id INTEGER NOT NULL REFERENCES products(id), amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', payment_method TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS deposits(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), atlantic_id TEXT, reff_id TEXT UNIQUE NOT NULL, nominal INTEGER NOT NULL, fee INTEGER DEFAULT 0, get_balance INTEGER DEFAULT 0, qr_image TEXT DEFAULT '', qr_string TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', expired_at TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, provider TEXT NOT NULL DEFAULT 'nasionalpay', payment_url TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), delta INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL UNIQUE, note TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), product_id INTEGER NOT NULL REFERENCES products(id), order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id), rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5), comment TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, product_id));
        CREATE TABLE IF NOT EXISTS service_orders(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), atlantic_id TEXT, reff_id TEXT UNIQUE NOT NULL, service_code TEXT NOT NULL, service_name TEXT NOT NULL, target TEXT NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', sn TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), category TEXT NOT NULL DEFAULT 'Bug', subject TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Baru', admin_note TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS email_otps(email TEXT NOT NULL, purpose TEXT NOT NULL, otp_hash TEXT NOT NULL, username TEXT DEFAULT '', password_hash TEXT DEFAULT '', user_id INTEGER DEFAULT NULL, expires_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(email,purpose));
        CREATE TABLE IF NOT EXISTS community_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id), username TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS ai_chat_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id), role TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        ''')
        deposit_cols = {r['name'] for r in c.execute('PRAGMA table_info(deposits)').fetchall()}
        if 'qr_string' not in deposit_cols: c.execute("ALTER TABLE deposits ADD COLUMN qr_string TEXT DEFAULT ''")
        if 'provider' not in deposit_cols: c.execute("ALTER TABLE deposits ADD COLUMN provider TEXT NOT NULL DEFAULT 'nasionalpay'")
        if 'payment_url' not in deposit_cols: c.execute("ALTER TABLE deposits ADD COLUMN payment_url TEXT DEFAULT ''")
        user_cols = {r['name'] for r in c.execute('PRAGMA table_info(users)').fetchall()}
        if 'email' not in user_cols: c.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
        if 'active' not in user_cols: c.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email) WHERE email IS NOT NULL AND email != ''")
        product_cols = {r['name'] for r in c.execute('PRAGMA table_info(products)').fetchall()}
        if 'digital_file' not in product_cols: c.execute("ALTER TABLE products ADD COLUMN digital_file TEXT DEFAULT ''")
        if 'digital_filename' not in product_cols: c.execute("ALTER TABLE products ADD COLUMN digital_filename TEXT DEFAULT ''")
        admin_user = os.environ.get('ADMIN_USERNAME', 'admin').strip() or 'admin'
        admin_pass = os.environ.get('ADMIN_PASSWORD', '').strip()
        row = c.execute('SELECT id FROM users WHERE username=?', (admin_user,)).fetchone()
        if not row:
            if len(admin_pass) < 12:
                admin_pass = secrets.token_urlsafe(18)
                print('\n' + '='*58 + '\nADMIN PERTAMA MUDZSTORY\nUsername: ' + admin_user + '\nPassword sementara: ' + admin_pass + '\nSimpan password ini sekarang; password hanya ditampilkan saat akun admin dibuat.\n' + '='*58 + '\n')
            c.execute('INSERT INTO users(username,password,role) VALUES(?,?,?)', (admin_user, generate_password_hash(admin_pass), 'admin'))

def login_required(fn):
    @wraps(fn)
    def wrapped(*a, **kw):
        if not session.get('uid'):
            flash('Login dulu untuk melanjutkan.', 'error'); return redirect(url_for('login'))
        with db() as c:
            account = c.execute('SELECT active FROM users WHERE id=?', (session['uid'],)).fetchone()
        if not account or not account['active']:
            session.clear()
            flash('Akun ini sudah dinonaktifkan. Hubungi admin jika perlu bantuan.', 'error')
            return redirect(url_for('login'))
        return fn(*a, **kw)
    return wrapped

def admin_required(fn):
    @wraps(fn)
    def wrapped(*a, **kw):
        if not session.get('uid'):
            return redirect(url_for('login'))
        with db() as c: u = c.execute('SELECT role FROM users WHERE id=?', (session['uid'],)).fetchone()
        if not u or u['role'] != 'admin': abort(403)
        return fn(*a, **kw)
    return wrapped

def current_user():
    if not session.get('uid'): return None
    with db() as c: return c.execute('SELECT * FROM users WHERE id=? AND active=1', (session['uid'],)).fetchone()

def money(v): return 'Rp ' + f'{int(v):,}'.replace(',', '.')

def deposit_expiry():
    return (datetime.now() + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')

def normalize_expiry(value, min_seconds=180):
    """Normalize provider expiry strings/Unix timestamps; ignore malformed or nearly expired values."""
    now = datetime.now()
    parsed = None
    if value not in (None, ''):
        try:
            if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
                stamp = float(value)
                if stamp > 10_000_000_000: stamp /= 1000
                parsed = datetime.fromtimestamp(stamp)
            else:
                raw = str(value).strip().replace('Z', '+00:00')
                parsed = datetime.fromisoformat(raw)
                if parsed.tzinfo is not None: parsed = parsed.astimezone().replace(tzinfo=None)
        except (ValueError, TypeError, OverflowError, OSError):
            parsed = None
    # A broken/past timestamp from the API was causing new invoices to show as expired instantly.
    if parsed is None or (parsed - now).total_seconds() < min_seconds:
        parsed = now + timedelta(minutes=15)
    return parsed.strftime('%Y-%m-%d %H:%M:%S')

def deposit_is_expired(value):
    if not value: return False
    try:
        raw = str(value).strip().replace('Z', '+00:00')
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is not None: parsed = parsed.astimezone().replace(tzinfo=None)
        return datetime.now() >= parsed
    except (ValueError, TypeError): return False
app.jinja_env.filters['money'] = money
oauth = OAuth(app) if OAuth else None
if oauth and os.environ.get('GOOGLE_CLIENT_ID') and os.environ.get('GOOGLE_CLIENT_SECRET'):
    oauth.register(name='google', server_metadata_url='https://accounts.google.com/.well-known/openid-configuration', client_id=os.environ['GOOGLE_CLIENT_ID'], client_secret=os.environ['GOOGLE_CLIENT_SECRET'], client_kwargs={'scope':'openid email profile'})

def nasionalpay_post(endpoint, payload):
    """Call NasionalPay REST API. Store key is sent server-side in JSON."""
    key = os.environ.get('NASIONALPAY_STORE_KEY', '').strip()
    if not key:
        raise RuntimeError('NASIONALPAY_STORE_KEY belum diatur di environment Termux.')
    body = dict(payload)
    body['key'] = key
    try:
        r = requests.post(NASIONALPAY_URL + endpoint, json=body, timeout=25)
    except requests.RequestException as exc:
        raise RuntimeError('Koneksi ke NasionalPay gagal: ' + str(exc)) from exc
    try:
        data = r.json()
    except ValueError:
        excerpt = (r.text or '')[:300]
        raise RuntimeError(f'NasionalPay HTTP {r.status_code}: respons bukan JSON: {excerpt}')
    if r.status_code >= 400:
        msg = data.get('message') or data.get('error') or str(data)
        raise RuntimeError(f'NasionalPay HTTP {r.status_code}: {msg}')
    ok = data.get('success', data.get('status', False))
    if ok not in (True, 1, 'true', 'success', 'ok'):
        raise RuntimeError(str(data.get('message') or data.get('error') or 'NasionalPay menolak permintaan.'))
    return data


def nasionalpay_data(response):
    return response.get('data') if isinstance(response.get('data'), dict) else response


def telegram_notify(text, button_text='Buka MudzStory'):
    """Best-effort notification; no effect if Telegram credentials are not configured."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat_id:
        return False
    site = SITE_URL or request.url_root.rstrip('/')
    payload = {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True,
               'reply_markup': {'inline_keyboard': [[{'text': button_text, 'url': site}]]}}
    try:
        r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage', json=payload, timeout=8)
        return r.ok and bool(r.json().get('ok'))
    except Exception:
        app.logger.warning('Telegram notification failed')
        return False


def credit_deposit(provider, transaction_id, provider_data=None):
    """Credit a pending deposit once after provider status has been verified."""
    transaction_id = str(transaction_id or '').strip()
    if not transaction_id: raise ValueError('ID transaksi pembayaran kosong.')
    provider_data = provider_data or {}
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        dep = c.execute('SELECT * FROM deposits WHERE atlantic_id=? AND provider=?', (transaction_id, provider)).fetchone()
        if not dep: raise ValueError('Invoice tidak ditemukan di database MudzStory.')
        status = str(provider_data.get('status') or '').lower()
        if status not in {'success','sukses','paid','completed'}:
            c.execute('UPDATE deposits SET status=? WHERE id=?', (status or 'pending', dep['id']))
            return False
        amount_value = provider_data.get('amount')
        if amount_value not in (None, ''):
            reported = int(Decimal(str(amount_value)))
            allowed = {int(dep['nominal'])}
            if provider == 'nasionalpay': allowed.add(int(dep['nominal']) + int(dep['fee'] or 0))
            if reported not in allowed: raise ValueError('Nominal transaksi tidak cocok dengan invoice MudzStory.')
        if str(dep['status']).lower() in {'success','sukses','paid','completed'}: return False
        cur = c.execute('INSERT OR IGNORE INTO ledger(user_id,delta,kind,ref,note) VALUES(?,?,?,?,?)',
                        (dep['user_id'], int(dep['nominal']), 'deposit', 'deposit-'+str(dep['id']),
                         'Deposit '+provider.title()+' '+str(dep['reff_id'])))
        if cur.rowcount == 1:
            c.execute('UPDATE users SET balance=balance+? WHERE id=?', (int(dep['nominal']), dep['user_id']))
        c.execute("UPDATE deposits SET status='success' WHERE id=?", (dep['id'],))
        if cur.rowcount == 1:
            telegram_notify('✅ Deposit berhasil\nProvider: '+provider.title()+'\nUser ID: '+str(dep['user_id'])+'\nNominal: '+money(dep['nominal'])+'\nRef: '+str(dep['reff_id']))
        return cur.rowcount == 1

def credit_nasionalpay_deposit(transaction_id, provider_data=None):
    return credit_deposit('nasionalpay', transaction_id, provider_data)

@app.context_processor
def inject_globals():
    cart = session.get('cart', {})
    cart_count = sum(int(v) for v in cart.values() if str(v).isdigit()) if isinstance(cart, dict) else 0
    return {'me': current_user(), 'cart_count': cart_count, 'whatsapp_channel_url': os.environ.get('WHATSAPP_CHANNEL_URL', '').strip(), 'telegram_channel_url': os.environ.get('TELEGRAM_CHANNEL_URL', '').strip()}

@app.route('/')
def index():
    if not session.get('uid'):
        return render_template('index.html', products=[], q='', stats=None)
    with db() as c:
        u = c.execute('SELECT id,username,balance FROM users WHERE id=?', (session['uid'],)).fetchone()
        if not u:
            session.clear()
            return redirect(url_for('login'))
        stats = {
            'transactions': c.execute('SELECT COUNT(*) FROM ledger WHERE user_id=?', (u['id'],)).fetchone()[0] + c.execute("SELECT COUNT(*) FROM deposits WHERE user_id=? AND status NOT IN ('success','sukses','paid','completed')", (u['id'],)).fetchone()[0],
            'balance': int(u['balance'] or 0),
            'files_bought': c.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status IN ('paid','fulfilled','completed')", (u['id'],)).fetchone()[0],
        }
        recent = c.execute("""SELECT kind,delta,note,created_at FROM ledger WHERE user_id=?
            UNION ALL
            SELECT 'deposit_pending' AS kind, 0 AS delta,
                   'Deposit ' || reff_id || ' · ' || status AS note, created_at
            FROM deposits WHERE user_id=? AND status NOT IN ('success','sukses','paid','completed')
            ORDER BY created_at DESC LIMIT 6""", (u['id'], u['id'])).fetchall()
    return render_template('index.html', products=[], q='', stats=stats, recent=recent)

@app.route('/shop')
@login_required
def shop():
    q = request.args.get('q','').strip()
    with db() as c:
        if q:
            products = c.execute("SELECT * FROM products WHERE active=1 AND name LIKE ? ORDER BY id DESC", ('%'+q+'%',)).fetchall()
        else:
            products = c.execute('SELECT * FROM products WHERE active=1 ORDER BY id DESC').fetchall()
    return render_template('shop.html', products=products, q=q)

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        submitted_otp = request.form.get('otp', '').strip()
        pending = session.get('pending_registration') or {}
        if submitted_otp:
            email = str(pending.get('email') or '').strip().lower()
            with db() as c:
                otp_row = c.execute("SELECT * FROM email_otps WHERE email=? AND purpose='register'", (email,)).fetchone() if email else None
            if not otp_row or int(otp_row['expires_at']) < int(time.time()) or int(otp_row['attempts']) >= 5:
                with db() as c:
                    if email: c.execute("DELETE FROM email_otps WHERE email=? AND purpose='register'", (email,))
                session.pop('pending_registration', None)
                flash('Kode OTP kedaluwarsa atau tidak tersedia. Daftar ulang untuk meminta kode baru.', 'error')
            elif not hmac.compare_digest(hashlib.sha256(submitted_otp.encode()).hexdigest(), otp_row['otp_hash']):
                with db() as c:
                    c.execute("UPDATE email_otps SET attempts=attempts+1 WHERE email=? AND purpose='register'", (email,))
                flash('Kode OTP salah. Periksa email dan coba lagi.', 'error')
            else:
                try:
                    with db() as c:
                        c.execute('BEGIN IMMEDIATE')
                        latest = c.execute("SELECT * FROM email_otps WHERE email=? AND purpose='register'", (email,)).fetchone()
                        if not latest or int(latest['expires_at']) < int(time.time()) or int(latest['attempts']) >= 5:
                            raise ValueError('Kode OTP sudah kedaluwarsa atau terlalu banyak percobaan.')
                        c.execute('INSERT INTO users(username,password,role,email) VALUES(?,?,?,?)',
                                  (latest['username'], latest['password_hash'], 'user', email))
                        c.execute("DELETE FROM email_otps WHERE email=? AND purpose='register'", (email,))
                    session.pop('pending_registration', None)
                    telegram_notify('🆕 Akun baru terverifikasi melalui email\\nUsername: '+str(latest['username']))
                    flash('Email terverifikasi dan akun berhasil dibuat. Silakan login.', 'ok')
                    return redirect(url_for('login'))
                except sqlite3.IntegrityError:
                    session.pop('pending_registration', None)
                    flash('Username atau email sudah dipakai. Silakan daftar dengan data lain.', 'error')
                except ValueError as exc:
                    flash(str(exc), 'error')
        else:
            username = request.form.get('username','').strip()
            email = request.form.get('email','').strip().lower()
            password = request.form.get('password','')
            valid_email = len(email) <= 254 and '@' in email and '.' in email.rsplit('@', 1)[-1]
            if len(username) < 3 or len(username) > 30 or not re.fullmatch(r'[A-Za-z0-9_.-]+', username) or not valid_email or len(password) < 8:
                flash('Email valid, username 3–30 karakter (huruf/angka/._-), dan password minimal 8 karakter diperlukan.', 'error')
            elif not all((os.environ.get(k) or '').strip() for k in ('SMTP_HOST','SMTP_PORT','SMTP_USER','SMTP_PASSWORD','SMTP_FROM')):
                flash('Verifikasi email belum dikonfigurasi. Isi SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, dan SMTP_FROM di .env.', 'error')
            else:
                with db() as c:
                    exists = c.execute('SELECT 1 FROM users WHERE username=? OR lower(email)=lower(?)', (username,email)).fetchone()
                if exists:
                    flash('Username atau email sudah dipakai.', 'error')
                else:
                    otp = f'{secrets.randbelow(1000000):06d}'
                    password_hash = generate_password_hash(password)
                    with db() as c:
                        c.execute("INSERT OR REPLACE INTO email_otps(email,purpose,otp_hash,username,password_hash,user_id,expires_at,attempts) VALUES(?,?,?,?,?,?,?,0)",
                                  (email,'register',hashlib.sha256(otp.encode()).hexdigest(),username,password_hash,None,int(time.time())+600))
                    msg = EmailMessage()
                    msg['Subject'] = 'Kode OTP pendaftaran MudzzStory'
                    msg['From'] = os.environ['SMTP_FROM']
                    msg['To'] = email
                    msg.set_content(f'Halo {username},\\n\\nKode OTP pendaftaran kamu: {otp}\\nKode berlaku 10 menit. Jangan bagikan kode ini kepada siapa pun.\\n')
                    try:
                        with smtplib.SMTP(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT','587')), timeout=20) as smtp:
                            smtp.starttls()
                            smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
                            smtp.send_message(msg)
                        session['pending_registration'] = {'email': email}
                        flash('Kode OTP telah dikirim ke email yang didaftarkan. Periksa folder Spam juga.', 'ok')
                    except Exception:
                        with db() as c: c.execute("DELETE FROM email_otps WHERE email=? AND purpose='register'", (email,))
                        app.logger.exception('Email OTP delivery failed')
                        flash('OTP gagal dikirim. Periksa konfigurasi SMTP server.', 'error')
    pending = session.get('pending_registration') or {}
    email = str(pending.get('email') or '').strip().lower()
    with db() as c:
        otp_row = c.execute("SELECT expires_at FROM email_otps WHERE email=? AND purpose='register'", (email,)).fetchone() if email else None
    otp_sent = bool(otp_row and int(otp_row['expires_at']) >= int(time.time()))
    if not otp_sent: session.pop('pending_registration', None)
    return render_template('auth.html', mode='register', otp_sent=otp_sent)

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        login_value = request.form.get('username','').strip()
        with db() as c: u = c.execute('SELECT * FROM users WHERE (username=? OR lower(email)=lower(?)) AND active=1', (login_value, login_value)).fetchone()
        if u and check_password_hash(u['password'], request.form.get('password','')):
            session.clear(); session['uid'] = u['id']; return redirect(url_for('admin') if u['role']=='admin' else url_for('index'))
        flash('Username atau password salah.', 'error')
    return render_template('auth.html', mode='login')

@app.route('/lupa-password', methods=['GET','POST'])
def forgot_password():
    if request.method == 'GET' and request.args.get('new') == '1':
        session.pop('pending_password_reset', None)
    if request.method == 'POST':
        pending = session.get('pending_password_reset') or {}
        submitted_otp = request.form.get('otp', '').strip()
        new_password = request.form.get('new_password', '')
        if pending.get('email') and submitted_otp:
            email = str(pending.get('email') or '').strip().lower()
            with db() as c:
                otp_row = c.execute("SELECT * FROM email_otps WHERE email=? AND purpose='reset'", (email,)).fetchone()
            if not otp_row or int(otp_row['expires_at']) < int(time.time()) or int(otp_row['attempts']) >= 5:
                with db() as c:
                    if email: c.execute("DELETE FROM email_otps WHERE email=? AND purpose='reset'", (email,))
                session.pop('pending_password_reset', None)
                flash('Kode reset kedaluwarsa atau tidak tersedia. Minta kode baru.', 'error')
            elif not hmac.compare_digest(hashlib.sha256(submitted_otp.encode()).hexdigest(), otp_row['otp_hash']):
                with db() as c:
                    c.execute("UPDATE email_otps SET attempts=attempts+1 WHERE email=? AND purpose='reset'", (email,))
                flash('Kode verifikasi salah.', 'error')
            elif len(new_password) < 8:
                flash('Password baru minimal 8 karakter.', 'error')
            else:
                with db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    latest = c.execute("SELECT * FROM email_otps WHERE email=? AND purpose='reset'", (email,)).fetchone()
                    if not latest or int(latest['expires_at']) < int(time.time()) or int(latest['attempts']) >= 5:
                        flash('Kode reset sudah kedaluwarsa atau terlalu banyak percobaan.', 'error')
                    else:
                        c.execute('UPDATE users SET password=? WHERE id=? AND active=1',
                                  (generate_password_hash(new_password), latest['user_id']))
                        c.execute("DELETE FROM email_otps WHERE email=? AND purpose='reset'", (email,))
                        session.pop('pending_password_reset', None)
                        session.pop('uid', None)
                        flash('Password berhasil diubah. Silakan login.', 'ok')
                        return redirect(url_for('login'))
        else:
            email = request.form.get('email', '').strip().lower()
            with db() as c:
                u = c.execute('SELECT id,username,email FROM users WHERE lower(email)=lower(?) AND active=1', (email,)).fetchone() if email else None
            configured = all((os.environ.get(k) or '').strip() for k in ('SMTP_HOST','SMTP_PORT','SMTP_USER','SMTP_PASSWORD','SMTP_FROM'))
            if u and configured and len(email) <= 254:
                otp = f'{secrets.randbelow(1000000):06d}'
                with db() as c:
                    c.execute("INSERT OR REPLACE INTO email_otps(email,purpose,otp_hash,username,password_hash,user_id,expires_at,attempts) VALUES(?,?,?,?,?,?,?,0)",
                              (email,'reset',hashlib.sha256(otp.encode()).hexdigest(),'', '', u['id'],int(time.time())+600))
                msg = EmailMessage()
                msg['Subject'] = 'Kode reset password MudzzStory'
                msg['From'] = os.environ['SMTP_FROM']
                msg['To'] = email
                msg.set_content(f'Halo {u["username"]},\\n\\nKode reset password kamu: {otp}\\nKode berlaku 10 menit. Jika kamu tidak meminta reset, abaikan email ini.')
                try:
                    with smtplib.SMTP(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT','587')), timeout=20) as smtp:
                        smtp.starttls()
                        smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
                        smtp.send_message(msg)
                except Exception:
                    with db() as c: c.execute("DELETE FROM email_otps WHERE email=? AND purpose='reset'", (email,))
                    app.logger.exception('Password reset email delivery failed')
            # The form state and message are the same whether the email exists or not.
            session['pending_password_reset'] = {'email': email}
            flash('Jika email terdaftar dan SMTP aktif, kode reset dikirim. Periksa folder Spam.', 'ok')
    return render_template('reset_password.html', otp_sent=bool(session.get('pending_password_reset')))

@app.route('/profil')
@login_required
def profile():
    u = current_user()
    with db() as c:
        count = c.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status IN ('paid','fulfilled')", (session['uid'],)).fetchone()[0]
        reports = c.execute('SELECT * FROM reports WHERE user_id=? ORDER BY id DESC LIMIT 20', (session['uid'],)).fetchall()
    return render_template('profile.html', u=u, order_count=count, reports=reports)

@app.route('/login/google')
def google_login():
    if not oauth or not os.environ.get('GOOGLE_CLIENT_ID') or not os.environ.get('GOOGLE_CLIENT_SECRET'):
        flash('Login Google belum dikonfigurasi. Gunakan login email/username dahulu.', 'error')
        return redirect(url_for('login'))
    redirect_uri = url_for('google_callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route('/auth/google/callback')
def google_callback():
    if not oauth or not os.environ.get('GOOGLE_CLIENT_ID') or not os.environ.get('GOOGLE_CLIENT_SECRET'):
        return redirect(url_for('login'))
    try:
        token = oauth.google.authorize_access_token()
        info = token.get('userinfo') or oauth.google.userinfo()
        email = str(info.get('email') or '').strip().lower()
        if not email or not info.get('email_verified', False):
            raise ValueError('Google tidak memberikan email terverifikasi.')
        with db() as c:
            u = c.execute('SELECT * FROM users WHERE lower(email)=lower(?) AND active=1', (email,)).fetchone()
            if not u:
                prefix = (email.split('@')[0] or 'google_user')[:20]
                username = prefix
                while c.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                    username = prefix[:16] + secrets.token_hex(2)
                cur = c.execute('INSERT INTO users(username,password,role,email) VALUES(?,?,?,?)', (username, generate_password_hash(secrets.token_urlsafe(32)), 'user', email))
                uid = cur.lastrowid
            else:
                uid = u['id']
        session.clear(); session['uid'] = uid
        return redirect(url_for('index'))
    except Exception as exc:
        app.logger.warning('Google OAuth login failed: %s', type(exc).__name__)
        flash('Login Google gagal. Periksa konfigurasi OAuth dan callback URL.', 'error')
        return redirect(url_for('login'))

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('index'))

@app.route('/layanan')
def services():
    return render_template('services.html', items=[], q='', error='Layanan provider eksternal sedang kami kembangkan agar dapat digunakan kembali dengan lebih baik.')

@app.route('/layanan/beli', methods=['POST'])
@login_required
def buy_service():
    flash('Pembelian layanan eksternal dinonaktifkan. Silakan pilih produk digital di katalog toko.', 'error')
    return redirect(url_for('index'))

@app.route('/layanan/status/<int:oid>', methods=['POST'])
@login_required
def service_status(oid):
    flash('Pengecekan status layanan eksternal dinonaktifkan.', 'error')
    return redirect(url_for('orders'))

@app.route('/product/<int:pid>')
def product(pid):
    with db() as c:
        p = c.execute('SELECT * FROM products WHERE id=? AND active=1', (pid,)).fetchone()
        reviews = c.execute('SELECT reviews.*, users.username FROM reviews JOIN users ON users.id=reviews.user_id WHERE product_id=? ORDER BY reviews.id DESC LIMIT 30', (pid,)).fetchall()
        rating = c.execute('SELECT AVG(rating) avg_rating, COUNT(*) total FROM reviews WHERE product_id=?', (pid,)).fetchone()
        eligible = False
        if session.get('uid'):
            eligible = c.execute("SELECT 1 FROM orders WHERE user_id=? AND product_id=? AND status IN ('paid','fulfilled') LIMIT 1", (session['uid'],pid)).fetchone() is not None
            already = c.execute('SELECT 1 FROM reviews WHERE user_id=? AND product_id=?', (session['uid'],pid)).fetchone() is not None
            eligible = eligible and not already
    if not p: abort(404)
    return render_template('product.html', p=p, reviews=reviews, rating=rating, eligible=eligible)

@app.route('/product/<int:pid>/review', methods=['POST'])
@login_required
def add_review(pid):
    try:
        rating = int(request.form.get('rating','0'))
        comment = request.form.get('comment','').strip()[:1000]
        if rating < 1 or rating > 5: raise ValueError('Pilih rating 1 sampai 5 bintang.')
        with db() as c:
            order = c.execute("SELECT id FROM orders WHERE user_id=? AND product_id=? AND status IN ('paid','fulfilled') ORDER BY id DESC LIMIT 1", (session['uid'],pid)).fetchone()
            if not order: raise ValueError('Ulasan hanya bisa dikirim setelah membeli produk.')
            c.execute('INSERT INTO reviews(user_id,product_id,order_id,rating,comment) VALUES(?,?,?,?,?)', (session['uid'],pid,order['id'],rating,comment))
        flash('Terima kasih, ulasan berhasil dikirim.', 'ok')
    except sqlite3.IntegrityError:
        flash('Lu sudah pernah memberi ulasan untuk produk ini.', 'error')
    except Exception as e:
        flash(str(e), 'error')
    return redirect(url_for('product',pid=pid))

@app.route('/pesanan/<int:oid>/download')
@login_required
def download_order_file(oid):
    with db() as c:
        order = c.execute("SELECT orders.id,orders.status,products.digital_file,products.digital_filename,products.name FROM orders JOIN products ON products.id=orders.product_id WHERE orders.id=? AND orders.user_id=?", (oid,session['uid'])).fetchone()
    if not order: abort(404)
    if order['status'] not in ('paid','fulfilled'):
        abort(403)
    filename = os.path.basename(order['digital_file'] or '')
    if not filename or not os.path.isfile(os.path.join(DIGITAL_DIR,filename)):
        flash('File digital belum tersedia. Hubungi admin toko.', 'error')
        return redirect(url_for('orders'))
    return send_from_directory(DIGITAL_DIR, filename, as_attachment=True, download_name=order['digital_filename'] or order['name'])

@app.route('/cart')
def cart_view():
    cart = session.get('cart', {})
    items, total = [], 0
    if isinstance(cart, dict) and cart:
        ids = [int(k) for k, v in cart.items() if str(k).isdigit() and str(v).isdigit() and int(v) > 0]
        if ids:
            with db() as c:
                for pid in ids:
                    p = c.execute('SELECT * FROM products WHERE id=? AND active=1', (pid,)).fetchone()
                    if not p: continue
                    qty = min(int(cart.get(str(pid), 1)), 99)
                    if p['stock'] >= 0: qty = min(qty, max(0, int(p['stock'])))
                    if qty <= 0: continue
                    item = dict(p); item['qty'] = qty; item['subtotal'] = int(p['price']) * qty
                    items.append(item); total += item['subtotal']
    return render_template('cart.html', items=items, total=total)

@app.route('/cart/add/<int:pid>', methods=['POST'])
def cart_add(pid):
    with db() as c:
        p = c.execute('SELECT id, name, stock, active FROM products WHERE id=?', (pid,)).fetchone()
    if not p or not p['active']:
        flash('Produk tidak tersedia.', 'error'); return redirect(url_for('shop'))
    try: qty = int(request.form.get('quantity', '1'))
    except ValueError: qty = 1
    qty = max(1, min(qty, 99))
    if p['stock'] == 0:
        flash('Stok produk habis.', 'error'); return redirect(url_for('product', pid=pid))
    cart = session.get('cart', {})
    if not isinstance(cart, dict): cart = {}
    current = int(cart.get(str(pid), 0) or 0)
    qty += current
    if p['stock'] > 0: qty = min(qty, int(p['stock']))
    cart[str(pid)] = min(qty, 99)
    session['cart'] = cart; session.modified = True
    flash('Produk dimasukkan ke keranjang kuning 🛒.', 'ok')
    return redirect(url_for('cart_view'))

@app.route('/cart/update', methods=['POST'])
def cart_update():
    cart = session.get('cart', {})
    if not isinstance(cart, dict): cart = {}
    action = request.form.get('action', 'update')
    pid = request.form.get('pid', '')
    if action == 'clear':
        session['cart'] = {}; session.modified = True
        flash('Keranjang dikosongkan.', 'ok')
        return redirect(url_for('cart_view'))
    if not pid.isdigit(): return redirect(url_for('cart_view'))
    if action == 'remove': cart.pop(pid, None)
    else:
        try: qty = int(request.form.get('quantity', '1'))
        except ValueError: qty = 1
        if qty <= 0: cart.pop(pid, None)
        else:
            with db() as c: product_row = c.execute('SELECT stock,active FROM products WHERE id=?', (int(pid),)).fetchone()
            if not product_row or not product_row['active']: cart.pop(pid, None)
            else:
                qty = min(qty, 99)
                if product_row['stock'] >= 0: qty = min(qty, int(product_row['stock']))
                if qty > 0: cart[pid] = qty
                else: cart.pop(pid, None)
    session['cart'] = cart; session.modified = True
    return redirect(url_for('cart_view'))

@app.route('/cart/checkout', methods=['POST'])
@login_required
def cart_checkout():
    cart = session.get('cart', {})
    if not isinstance(cart, dict) or not cart:
        flash('Keranjang masih kosong.', 'error'); return redirect(url_for('shop'))
    con = db()
    try:
        con.execute('BEGIN IMMEDIATE')
        u = con.execute('SELECT * FROM users WHERE id=? AND active=1', (session['uid'],)).fetchone()
        if not u: raise ValueError('Akun tidak aktif. Silakan login ulang.')
        total = 0; lines = []
        for key, raw_qty in cart.items():
            if not str(key).isdigit() or not str(raw_qty).isdigit(): continue
            pid, qty = int(key), int(raw_qty)
            if qty < 1 or qty > 99: raise ValueError('Jumlah item tidak valid.')
            product_row = con.execute('SELECT * FROM products WHERE id=? AND active=1', (pid,)).fetchone()
            if not product_row: raise ValueError('Salah satu produk sudah tidak tersedia. Perbarui keranjang.')
            if product_row['stock'] == 0 or (product_row['stock'] > 0 and qty > product_row['stock']):
                raise ValueError('Stok '+product_row['name']+' tidak mencukupi. Perbarui jumlah di keranjang.')
            total += int(product_row['price']) * qty
            lines.append((product_row, qty))
        if not lines: raise ValueError('Keranjang masih kosong.')
        if int(u['balance']) < total: raise ValueError('Saldo tidak cukup. Isi saldo dulu, lalu kembali ke keranjang.')
        con.execute('UPDATE users SET balance=balance-? WHERE id=?', (total, u['id']))
        order_ids=[]
        for product_row, qty in lines:
            if product_row['stock'] > 0:
                con.execute('UPDATE products SET stock=stock-? WHERE id=? AND stock>=?', (qty, product_row['id'], qty))
                if con.execute('SELECT changes()').fetchone()[0] != 1: raise ValueError('Stok '+product_row['name']+' baru saja berubah. Coba lagi.')
            for _ in range(qty):
                cur = con.execute("INSERT INTO orders(user_id,product_id,amount,status,payment_method) VALUES(?,?,?,'paid','wallet')", (u['id'], product_row['id'], int(product_row['price'])))
                order_ids.append(cur.lastrowid)
            con.execute('INSERT INTO ledger(user_id,delta,kind,ref,note) VALUES(?,?,?,?,?)', (u['id'], -(int(product_row['price'])*qty), 'purchase', 'cart-'+uuid.uuid4().hex, product_row['name']+' × '+str(qty)))
        con.commit()
        session['cart'] = {}; session.modified = True
        summary = '\n'.join(p['name']+' × '+str(qty) for p, qty in lines)
        telegram_notify('🛒 Pembelian keranjang baru\nUser: '+u['username']+'\n'+summary+'\nTotal: '+money(total)+'\nPesanan: '+str(len(order_ids)))
        flash('Checkout berhasil! Total '+money(total)+'. Pesanan tercatat di riwayat.', 'ok')
        return redirect(url_for('orders'))
    except ValueError as e:
        con.rollback(); flash(str(e), 'error'); return redirect(url_for('cart_view'))
    except Exception:
        con.rollback(); raise
    finally: con.close()

@app.route('/buy/<int:pid>', methods=['POST'])
@login_required
def buy(pid):
    # Legacy links are redirected into cart flow; purchases are no longer immediate.
    cart = session.get('cart', {})
    if not isinstance(cart, dict): cart = {}
    cart[str(pid)] = max(1, min(int(request.form.get('quantity', '1') or 1), 99))
    session['cart'] = cart
    return redirect(url_for('cart_view'))

def make_local_qr(qr_string, reff):
    """Create a local PNG from provider QR payload if qr_image URL is absent."""
    if not qr_string:
        return ''
    try:
        import qrcode
        filename = re.sub(r'[^A-Za-z0-9_-]', '', reff) + '.png'
        path = os.path.join(QR_DIR, filename)
        image = qrcode.make(qr_string)
        image.save(path)
        return url_for('static', filename='qr_deposits/' + filename)
    except Exception as exc:
        # The deposit page has a browser-side QR fallback for existing/new invoices.
        app.logger.warning('Local QR image generation failed (%s); using browser fallback when available.', type(exc).__name__)
        return ''


@app.route('/saldo', methods=['GET','POST'])
@login_required
def saldo():
    if request.method == 'POST':
        try:
            nominal = int(request.form.get('nominal','0'))
            provider = 'nasionalpay'
            if nominal < 2000 or nominal > 5000000:
                raise ValueError('Nominal deposit website dibatasi Rp2.000–Rp5.000.000.')
            if provider != 'nasionalpay': raise ValueError('Metode pembayaran tidak valid.')
            reff = 'WEB' + uuid.uuid4().hex[:20].upper()
            transaction_id = ''
            qr_string = ''; qr_image = ''; fee = 0; expired_at = ''; payment_url = ''
            if provider == 'nasionalpay':
                with db() as c:
                    account = c.execute('SELECT username FROM users WHERE id=?', (session['uid'],)).fetchone()
                player_username = str(account['username'] if account else session['uid'])
                res = nasionalpay_post('/api/payment/generate', {'channel':'QRIS','amount':nominal,'player_username':player_username})
                d = nasionalpay_data(res)
                transaction_id = str(d.get('transaction_id') or d.get('trx_id') or d.get('id') or '').strip()
                qr_string = str(d.get('qris_data') or d.get('qr_string') or '').strip()
                qr_image = str(d.get('qris_image') or d.get('qr_image') or '').strip()
                fee = int(d.get('fee_mdr') or d.get('fee') or 0)
                expired_at = normalize_expiry(d.get('expired_at'))
                initial_status = str(d.get('status') or 'pending').lower()
                if initial_status in {'success','sukses','paid','completed'}: initial_status = 'pending'  # verify via status endpoint/webhook before credit
                if not transaction_id: raise RuntimeError('NasionalPay tidak mengembalikan ID transaksi.')
                if not qr_image and qr_string: qr_image = make_local_qr(qr_string, reff)
                if not qr_image and not qr_string: raise RuntimeError('NasionalPay tidak mengembalikan QRIS.')
            # Use provider expiry when supplied; otherwise allow a 15-minute payment window.
            if not expired_at:
                expired_at = deposit_expiry()
            with db() as c:
                c.execute('INSERT INTO deposits(user_id,atlantic_id,reff_id,nominal,fee,get_balance,qr_image,qr_string,status,expired_at,provider,payment_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                          (session['uid'], transaction_id, reff, nominal, fee, nominal, qr_image, qr_string, initial_status, expired_at, provider, payment_url))
            telegram_notify('💳 Deposit baru dibuat\nProvider: '+provider.title()+'\nNominal: '+money(nominal)+'\nRef: '+reff+'\nStatus: menunggu pembayaran', 'Buka MudzStory')
            flash('Invoice '+provider.title()+' dibuat. Selesaikan pembayaran lalu cek status.', 'ok')
            return redirect(url_for('deposit_detail', reff=reff))
        except Exception as e: flash('Gagal membuat pembayaran: ' + str(e), 'error')
    with db() as c:
        u = c.execute('SELECT * FROM users WHERE id=?', (session['uid'],)).fetchone()
        deposits = c.execute('SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 20', (session['uid'],)).fetchall()
    return render_template('saldo.html', u=u, deposits=deposits)

@app.route('/deposit/<reff>', methods=['GET','POST'])
@login_required
def deposit_detail(reff):
    with db() as c:
        d = c.execute('SELECT * FROM deposits WHERE reff_id=? AND user_id=?', (reff, session['uid'])).fetchone()
    if not d:
        abort(404)
    if request.method == 'POST':
        action = request.form.get('action', 'check').strip().lower()
        try:
            # Verify with NasionalPay before trusting a status transition.
            res = nasionalpay_post('/api/payment/status', {'transaction_id': d['atlantic_id']})
            data = nasionalpay_data(res)
            status = str(data.get('status') or 'pending').lower()
            if status in {'success','sukses','paid','completed'}:
                credit_deposit('nasionalpay', d['atlantic_id'], data)
                flash('Pembayaran terverifikasi. Saldo diproses satu kali.', 'ok')
            elif status in {'failed','cancelled','canceled','expired','refunded'}:
                with db() as c:
                    c.execute("UPDATE deposits SET status=? WHERE id=? AND status NOT IN ('success','sukses','paid','completed')", (status, d['id']))
                flash('Status pembayaran: ' + status, 'ok')
            elif action == 'cancel':
                # Close locally; do not claim the gateway invoice was cancelled.
                with db() as c:
                    c.execute("UPDATE deposits SET status='cancelled' WHERE id=? AND status NOT IN ('success','sukses','paid','completed')", (d['id'],))
                flash('Deposit ditutup di website. Gateway tidak menyediakan pembatalan melalui integrasi ini; jangan bayar QR lama.', 'ok')
            elif deposit_is_expired(d['expired_at']):
                with db() as c:
                    c.execute("UPDATE deposits SET status='expired' WHERE id=? AND status NOT IN ('success','sukses','paid','completed')", (d['id'],))
                flash('Waktu pembayaran habis dan provider belum mengonfirmasi pembayaran.', 'error')
            else:
                with db() as c:
                    c.execute("UPDATE deposits SET status='pending' WHERE id=? AND status NOT IN ('success','sukses','paid','completed')", (d['id'],))
                flash('Pembayaran masih menunggu. Coba cek lagi setelah membayar.', 'ok')
        except Exception as e:
            flash('Gagal memverifikasi status: ' + str(e), 'error')
        return redirect(url_for('deposit_detail', reff=reff))
    with db() as c:
        d = c.execute('SELECT * FROM deposits WHERE reff_id=? AND user_id=?', (reff,session['uid'])).fetchone()
    return render_template('deposit.html', d=d, payment_url='')

@app.route('/webhooks/nasionalpay', methods=['POST'])
def nasionalpay_webhook():
    raw = request.get_data(cache=True)
    store_key = os.environ.get('NASIONALPAY_STORE_KEY', '').strip()
    signature = request.headers.get('X-NasionalPay-Signature', '').strip()
    if not store_key or not signature: abort(401)
    expected = hmac.new(store_key.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.lower().strip()): abort(401)
    try:
        payload = request.get_json(silent=False)
        if not isinstance(payload, dict): abort(400)
        data = payload.get('data') if isinstance(payload.get('data'), dict) else payload
        if payload.get('success') is False: return {'status':'ignored'}, 200
        status = str(data.get('status') or '').lower()
        transaction_id = str(data.get('transaction_id') or data.get('trx_id') or '').strip()
        if not transaction_id:
            return {'status':'invalid transaction'}, 400
        if status in {'success','sukses','paid','completed'}:
            if data.get('amount') in (None, ''):
                return {'status':'amount required'}, 400
            with db() as c:
                dep = c.execute("SELECT deposits.*, users.username FROM deposits JOIN users ON users.id=deposits.user_id WHERE deposits.atlantic_id=? AND deposits.provider='nasionalpay'", (transaction_id,)).fetchone()
            if not dep:
                return {'status':'invoice not found'}, 404
            if int(Decimal(str(data['amount']))) not in {int(dep['nominal']), int(dep['nominal']) + int(dep['fee'] or 0)}:
                return {'status':'amount mismatch'}, 400
            callback_username = str(data.get('username') or data.get('player_username') or '').strip()
            if callback_username and callback_username != str(dep['username']):
                return {'status':'username mismatch'}, 400
            credit_deposit('nasionalpay', transaction_id, data)
        else:
            with db() as c:
                c.execute("UPDATE deposits SET status=? WHERE atlantic_id=? AND provider='nasionalpay' AND status NOT IN ('success','sukses','paid','completed')", (status or 'pending', transaction_id))
        return {'status':'ok'}, 200
    except (ValueError, TypeError): return {'status':'invalid callback'}, 400

@app.route('/pesanan')
@login_required
def orders():
    with db() as c:
        rows = c.execute('SELECT orders.*, products.name, products.image, products.digital_file, products.digital_filename FROM orders JOIN products ON products.id=orders.product_id WHERE orders.user_id=? ORDER BY orders.id DESC', (session['uid'],)).fetchall()
        service_rows = c.execute('SELECT * FROM service_orders WHERE user_id=? ORDER BY id DESC', (session['uid'],)).fetchall()
    return render_template('orders.html', rows=rows, service_rows=service_rows)

@app.route('/admin')
@admin_required
def admin():
    with db() as c:
        products = c.execute('SELECT * FROM products ORDER BY id DESC').fetchall()
        orders = c.execute('SELECT orders.*,users.username,products.name FROM orders JOIN users ON users.id=orders.user_id JOIN products ON products.id=orders.product_id ORDER BY orders.id DESC LIMIT 30').fetchall()
        reports = c.execute("SELECT reports.*, users.username, users.email FROM reports JOIN users ON users.id=reports.user_id ORDER BY CASE reports.status WHEN 'Baru' THEN 0 WHEN 'Diproses' THEN 1 ELSE 2 END, reports.id DESC LIMIT 100").fetchall()
        users = c.execute('SELECT id,username,email,balance,role,active FROM users ORDER BY id DESC LIMIT 300').fetchall()
        deposits = c.execute('SELECT deposits.*,users.username FROM deposits JOIN users ON users.id=deposits.user_id ORDER BY deposits.id DESC LIMIT 50').fetchall()
        stats = {'users':c.execute('SELECT COUNT(*) FROM users').fetchone()[0], 'products':c.execute('SELECT COUNT(*) FROM products').fetchone()[0], 'sales':c.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid'").fetchone()[0], 'reports':c.execute("SELECT COUNT(*) FROM reports WHERE status!='Selesai'").fetchone()[0]}
    telegram_configured = bool(os.environ.get('TELEGRAM_BOT_TOKEN','').strip() and os.environ.get('TELEGRAM_CHAT_ID','').strip())
    return render_template('admin.html', products=products, orders=orders, reports=reports, stats=stats, users=users, deposits=deposits, telegram_configured=telegram_configured)

@app.route('/admin/deposit/<int:deposit_id>/confirm-paid', methods=['POST'])
@admin_required
def admin_confirm_deposit_paid(deposit_id):
    """Manual reconciliation for a deposit after the owner verifies payment in provider dashboard."""
    if request.form.get('verified') != 'yes':
        flash('Centang konfirmasi setelah pembayaran benar-benar terlihat berhasil di dashboard provider.', 'error')
        return redirect(url_for('admin') + '#deposit-admin')
    with db() as c:
        dep = c.execute('SELECT * FROM deposits WHERE id=?', (deposit_id,)).fetchone()
    if not dep:
        flash('Deposit tidak ditemukan.', 'error')
        return redirect(url_for('admin') + '#deposit-admin')
    try:
        # This is an explicit admin reconciliation path for provider APIs that no longer
        # expose a usable status endpoint. The admin must verify the real payment first.
        credited = credit_deposit(dep['provider'] or 'nasionalpay', dep['atlantic_id'], {
            'status': 'completed', 'amount': int(dep['nominal'])
        })
        flash('Deposit ditandai berhasil dan saldo dikreditkan.' if credited else 'Deposit sudah pernah diproses; saldo tidak ditambahkan lagi.', 'ok')
    except Exception as exc:
        flash('Rekonsiliasi deposit gagal: ' + str(exc), 'error')
    return redirect(url_for('admin') + '#deposit-admin')


@app.route('/admin/balance', methods=['POST'])
@admin_required
def admin_balance():
    try:
        target = request.form.get('target','').strip()
        amount = int(request.form.get('amount','0'))
        note = request.form.get('note','Penyesuaian saldo oleh admin').strip()[:180]
        if amount == 0 or abs(amount) > 100000000: raise ValueError('Nominal harus bukan nol dan maksimal Rp100.000.000.')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            if target == 'all':
                rows = c.execute("SELECT id,balance FROM users WHERE role!='admin'").fetchall()
                if not rows: raise ValueError('Tidak ada pengguna untuk diproses.')
                if amount < 0 and any(int(row['balance']) < abs(amount) for row in rows):
                    raise ValueError('Pengurangan saldo massal dibatalkan karena ada pengguna yang saldonya tidak mencukupi.')
                for row in rows:
                    c.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount,row['id']))
                    ref = 'adminbal-'+uuid.uuid4().hex
                    c.execute('INSERT INTO ledger(user_id,delta,kind,ref,note) VALUES(?,?,?,?,?)',(row['id'],amount,'admin_adjustment',ref,note))
                target_label = f'{len(rows)} pengguna'
            else:
                uid = int(target)
                user = c.execute("SELECT id,username,balance FROM users WHERE id=? AND role!='admin'", (uid,)).fetchone()
                if not user: raise ValueError('Pengguna tidak ditemukan atau target adalah admin.')
                if amount < 0 and int(user['balance']) < abs(amount): raise ValueError('Saldo pengguna tidak mencukupi untuk dikurangi sebesar itu.')
                c.execute('UPDATE users SET balance=balance+? WHERE id=?', (amount,uid))
                c.execute('INSERT INTO ledger(user_id,delta,kind,ref,note) VALUES(?,?,?,?,?)',(uid,amount,'admin_adjustment','adminbal-'+uuid.uuid4().hex,note))
                target_label = '@'+user['username']
        telegram_notify('🧾 Penyesuaian saldo admin\nTarget: '+target_label+'\nPerubahan: '+('+' if amount>0 else '')+money(amount)+'\nCatatan: '+note)
        flash('Saldo berhasil diperbarui untuk '+target_label+'.','ok')
    except Exception as e: flash('Gagal mengubah saldo: '+str(e),'error')
    return redirect(url_for('admin')+'#saldo-admin')

@app.route('/admin/user/<int:uid>/delete', methods=['POST'])
@admin_required
def admin_delete_user(uid):
    with db() as c:
        actor = c.execute('SELECT id FROM users WHERE id=?', (session['uid'],)).fetchone()
        target = c.execute('SELECT id,username,role,active FROM users WHERE id=?', (uid,)).fetchone()
        if not target:
            flash('Akun tidak ditemukan.', 'error')
        elif target['role'] == 'admin' or (actor and int(actor['id']) == uid):
            flash('Akun admin dan akun yang sedang dipakai tidak bisa dihapus dari sini.', 'error')
        elif not target['active']:
            flash('Akun tersebut sudah dinonaktifkan.', 'error')
        else:
            # Preserve financial/audit records while anonymizing and disabling access.
            c.execute('UPDATE users SET active=0, username=?, email=?, password=? WHERE id=?',
                      ('deleted_' + str(uid), '', generate_password_hash(secrets.token_urlsafe(32)), uid))
            flash('Akun dinonaktifkan dan data identitas login dianonimkan. Catatan transaksi tetap disimpan untuk audit.', 'ok')
    return redirect(url_for('admin') + '?tab=akun#akun-admin')


@app.route('/admin/broadcast', methods=['POST'])
@admin_required
def admin_broadcast():
    message = request.form.get('message','').strip()[:3500]
    if not message:
        flash('Pesan broadcast tidak boleh kosong.','error')
    elif not telegram_notify('📣 BROADCAST ADMIN\n\n'+message, 'Buka MudzStory'):
        flash('Broadcast gagal. Periksa TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, dan izin bot di grup/channel.','error')
    else:
        flash('Broadcast berhasil dikirim ke Telegram.','ok')
    return redirect(url_for('admin')+'#telegram-admin')

def save_image(file):
    if not file or not file.filename: return ''
    file.stream.seek(0, os.SEEK_END); size = file.stream.tell(); file.stream.seek(0)
    if size > 5 * 1024 * 1024: raise ValueError('Foto maksimal 5 MB.')
    filename = secure_filename(file.filename)
    ext = filename.rsplit('.',1)[-1].lower() if '.' in filename else ''
    if ext not in ALLOWED: raise ValueError('Foto harus PNG, JPG, JPEG, WEBP, atau GIF.')
    name = secrets.token_hex(12)+'.'+ext
    file.save(os.path.join(UPLOAD_DIR,name))
    return 'uploads/'+name

def save_digital_file(file):
    if not file or not file.filename:
        return ('', '')
    file.stream.seek(0, os.SEEK_END); size = file.stream.tell(); file.stream.seek(0)
    if size > 50 * 1024 * 1024: raise ValueError('File digital maksimal 50 MB.')
    original = secure_filename(file.filename)
    if not original or '.' not in original:
        raise ValueError('Nama file digital tidak valid.')
    ext = original.rsplit('.',1)[-1].lower()
    if ext not in DIGITAL_ALLOWED:
        raise ValueError('Format file belum didukung. Gunakan ZIP, PDF, TXT, PY, JSON, DOCX, XLSX, APK, dan format umum lainnya.')
    stored = secrets.token_hex(16) + '.' + ext
    file.save(os.path.join(DIGITAL_DIR, stored))
    return stored, original


@app.route('/laporan', methods=['POST'])
@login_required
def submit_report():
    category = request.form.get('category', 'Bug').strip()[:40]
    subject = request.form.get('subject', '').strip()[:120]
    message = request.form.get('message', '').strip()[:3000]
    if category not in {'Bug', 'Masalah akun', 'Pembayaran', 'Produk/pesanan', 'Keamanan & privasi', 'Lainnya'}:
        category = 'Lainnya'
    if len(subject) < 3 or len(message) < 10:
        flash('Judul minimal 3 karakter dan laporan minimal 10 karakter.', 'error')
        return redirect(url_for('profile') + '#laporan')
    with db() as c:
        c.execute('INSERT INTO reports(user_id,category,subject,message) VALUES(?,?,?,?)', (session['uid'], category, subject, message))
    flash('Laporan terkirim. Admin bisa melihat dan memprosesnya di panel admin.', 'ok')
    return redirect(url_for('profile') + '#laporan')

@app.route('/admin/report/<int:rid>', methods=['POST'])
@admin_required
def update_report(rid):
    status = request.form.get('status', 'Diproses')
    note = request.form.get('admin_note', '').strip()[:2000]
    if status not in {'Baru', 'Diproses', 'Selesai'}:
        status = 'Baru'
    with db() as c:
        cur = c.execute('UPDATE reports SET status=?, admin_note=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', (status, note, rid))
        if cur.rowcount == 0: abort(404)
    flash('Status laporan diperbarui.', 'ok')
    return redirect(url_for('admin') + '#laporan-admin')

@app.route('/admin/product/add', methods=['POST'])
@admin_required
def add_product():
    try:
        name=request.form.get('name','').strip(); desc=request.form.get('description','').strip(); price=int(request.form.get('price','0')); stock=int(request.form.get('stock','-1'))
        if not name or price < 0 or stock < -1: raise ValueError('Nama, harga, atau stok tidak valid. Stok -1 berarti tidak terbatas.')
        image=save_image(request.files.get('image'))
        digital_file, digital_filename = save_digital_file(request.files.get('digital_file'))
        with db() as c: c.execute('INSERT INTO products(name,description,price,stock,image,digital_file,digital_filename) VALUES(?,?,?,?,?,?,?)',(name,desc,price,stock,image,digital_file,digital_filename))
        telegram_notify('🆕 Produk baru ditambahkan\nProduk: '+name+'\nHarga: '+money(price)+'\nStok: '+('Unlimited' if stock == -1 else str(stock)))
        flash('Produk berhasil ditambahkan.','ok')
    except Exception as e: flash('Gagal tambah produk: '+str(e),'error')
    return redirect(url_for('admin'))

@app.route('/admin/product/<int:pid>/delete', methods=['POST'])
@admin_required
def delete_product(pid):
    with db() as c: c.execute('UPDATE products SET active=0 WHERE id=?',(pid,))
    flash('Produk disembunyikan dari toko.','ok'); return redirect(url_for('admin'))

@app.route('/admin/product/<int:pid>/edit', methods=['POST'])
@admin_required
def edit_product(pid):
    try:
        name=request.form.get('name','').strip(); desc=request.form.get('description','').strip(); price=int(request.form.get('price','0')); stock=int(request.form.get('stock','-1'))
        if not name or price < 0 or stock < -1: raise ValueError('Data produk tidak valid.')
        image=save_image(request.files.get('image'))
        digital_file, digital_filename = save_digital_file(request.files.get('digital_file'))
        with db() as c:
            if image and digital_file: c.execute('UPDATE products SET name=?,description=?,price=?,stock=?,image=?,digital_file=?,digital_filename=?,active=1 WHERE id=?',(name,desc,price,stock,image,digital_file,digital_filename,pid))
            elif image: c.execute('UPDATE products SET name=?,description=?,price=?,stock=?,image=?,active=1 WHERE id=?',(name,desc,price,stock,image,pid))
            elif digital_file: c.execute('UPDATE products SET name=?,description=?,price=?,stock=?,digital_file=?,digital_filename=?,active=1 WHERE id=?',(name,desc,price,stock,digital_file,digital_filename,pid))
            else: c.execute('UPDATE products SET name=?,description=?,price=?,stock=?,active=1 WHERE id=?',(name,desc,price,stock,pid))
        telegram_notify('📦 Produk/stok diperbarui\nProduk: '+name+'\nHarga: '+money(price)+'\nStok: '+('Unlimited' if stock == -1 else str(stock)))
        flash('Produk diperbarui.','ok')
    except Exception as e: flash('Gagal edit: '+str(e),'error')
    return redirect(url_for('admin'))

@app.route('/admin/order/<int:oid>/fulfill', methods=['POST'])
@admin_required
def fulfill(oid):
    # For manual/digital products, admin marks delivery after fulfilling it.
    with db() as c: c.execute("UPDATE orders SET status='fulfilled' WHERE id=? AND status='paid'",(oid,))
    flash('Pesanan ditandai selesai.','ok'); return redirect(url_for('admin'))

@app.route('/komunitas')
@login_required
def community():
    return render_template('community.html')

@app.route('/api/komunitas', methods=['GET', 'POST'])
@login_required
def community_api():
    uid = int(session['uid'])
    if request.method == 'POST':
        payload = request.get_json(silent=True) or request.form
        message = re.sub(r'\s+', ' ', str(payload.get('message', '')).strip())[:500]
        if not message:
            return jsonify({'ok': False, 'error': 'Pesan tidak boleh kosong.'}), 400
        if time.time() - float(session.get('community_last_post', 0)) < 1.2:
            return jsonify({'ok': False, 'error': 'Tunggu sebentar sebelum mengirim lagi.'}), 429
        with db() as c:
            user = c.execute('SELECT username,active FROM users WHERE id=?', (uid,)).fetchone()
            if not user or not user['active']:
                return jsonify({'ok': False, 'error': 'Akun tidak aktif.'}), 403
            cur = c.execute('INSERT INTO community_messages(user_id,username,message) VALUES(?,?,?)', (uid, user['username'], message))
            mid = cur.lastrowid
            row = c.execute('SELECT id,username,message,created_at FROM community_messages WHERE id=?', (mid,)).fetchone()
        session['community_last_post'] = time.time()
        return jsonify({'ok': True, 'message': dict(row)})
    after = max(0, int(request.args.get('after', '0') or 0))
    with db() as c:
        rows = c.execute('SELECT id,username,message,created_at FROM community_messages WHERE id>? ORDER BY id ASC LIMIT 80', (after,)).fetchall()
    return jsonify({'ok': True, 'messages': [dict(r) for r in rows]})

@app.route('/api/mudzz-story-ai', methods=['GET', 'POST'])
@login_required
def mudzz_story_ai():
    uid = int(session['uid'])
    if request.method == 'GET':
        with db() as c:
            rows = c.execute("SELECT role,message,created_at FROM ai_chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 30", (uid,)).fetchall()
        return jsonify({'ok': True, 'messages': [dict(r) for r in reversed(rows)]})
    payload = request.get_json(silent=True) or {}
    message = re.sub(r'\s+', ' ', str(payload.get('message', '')).strip())[:1200]
    if not message:
        return jsonify({'ok': False, 'error': 'Tulis pesan dulu ya.'}), 400
    with db() as c:
        c.execute("INSERT INTO ai_chat_messages(user_id,role,message) VALUES(?,?,?)", (uid, 'user', message))
        history = c.execute("SELECT role,message FROM ai_chat_messages WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    history = list(reversed(history))
    answer = None
    api_url = os.environ.get('AI_API_URL', '').strip()
    api_key = os.environ.get('AI_API_KEY', '').strip()
    model = os.environ.get('AI_MODEL', 'gpt-4o-mini').strip()
    gemini_key = os.environ.get('GEMINI_API_KEY', '').strip()
    if gemini_key:
        try:
            contents = []
            for item in history:
                role = 'model' if item['role'] == 'assistant' else 'user'
                contents.append({'role': role, 'parts': [{'text': item['message']}]})
            gemini_model = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash').strip()
            endpoint = 'https://generativelanguage.googleapis.com/v1beta/models/' + gemini_model + ':generateContent'
            resp = requests.post(endpoint, params={'key': gemini_key}, json={
                'systemInstruction': {'parts': [{'text':'Kamu adalah Mudzz Story AI, asisten ramah berbahasa Indonesia untuk toko digital Mudzz Story. Bantu menjelaskan katalog, keranjang, akun, pesanan, dan deposit. Jangan mengaku bisa melihat data akun pribadi atau mengubah transaksi. Jangan pernah meminta password atau OTP; arahkan kendala transaksi ke admin.'}]},
                'contents': contents,
                'generationConfig': {'temperature': 0.7, 'maxOutputTokens': 500}
            }, timeout=25)
            resp.raise_for_status()
            answer = str(resp.json()['candidates'][0]['content']['parts'][0]['text']).strip()[:4000]
        except Exception:
            app.logger.exception('Gemini Mudzz Story AI provider failed')
    elif api_url and api_key:
        try:
            msgs = [{'role':'system','content':'Kamu adalah Mudzz Story AI, asisten ramah berbahasa Indonesia untuk membantu pengguna memahami toko digital Mudzz Story, akun, pesanan, saldo, dan pertanyaan umum. Jangan mengaku bisa melihat data akun pribadi atau mengubah transaksi. Arahkan masalah transaksi ke admin.'}]
            msgs += [{'role': r['role'], 'content': r['message']} for r in history]
            resp = requests.post(api_url, headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'}, json={'model':model,'messages':msgs,'temperature':0.7,'max_tokens':500}, timeout=25)
            resp.raise_for_status()
            answer = str(resp.json()['choices'][0]['message']['content']).strip()[:4000]
        except Exception:
            app.logger.exception('Mudzz Story AI provider failed')
    if not answer:
        low = message.lower()
        if any(w in low for w in ['saldo','deposit','qris','top up','topup']):
            answer = 'Untuk isi saldo Mudzz Story, buka menu SALDO / DEPOSIT setelah login, masukkan nominal, lalu selesaikan pembayaran pada QR/invoice yang aktif. Kalau status belum berubah, simpan nomor referensi dan hubungi admin. Jangan kirim password atau OTP ke siapa pun.'
        elif any(w in low for w in ['pesanan','order','beli','produk','stok','keranjang']):
            answer = 'Untuk belanja, buka SHOP, pilih produk dan jumlahnya, lalu masukkan ke keranjang dan checkout. Cek stok serta total sebelum konfirmasi. Status pembelian bisa dilihat di PESANAN KU.'
        elif any(w in low for w in ['akun','login','password','daftar','otp','email']):
            answer = 'Untuk akun, gunakan LOGIN atau DAFTAR dan pastikan email benar. Jangan bagikan kata sandi atau kode OTP. Jika lupa password, gunakan menu Lupa Password. Untuk kendala berlanjut, hubungi admin Mudzz Story.'
        else:
            answer = 'Halo! Aku Mudzz Story AI 🤖. Aku bisa bantu menjelaskan cara memakai toko, keranjang, pesanan, dan deposit. Untuk jawaban AI generatif penuh, admin perlu mengatur AI_API_URL, AI_API_KEY, dan AI_MODEL di environment server. Kamu juga bisa ngobrol dengan pengguna lain lewat KOMUNITAS PUBLIK.'
    with db() as c:
        c.execute("INSERT INTO ai_chat_messages(user_id,role,message) VALUES(?,?,?)", (uid, 'assistant', answer))
    return jsonify({'ok': True, 'answer': answer, 'mode': 'ai' if (gemini_key or (api_url and api_key)) else 'help'})

@app.errorhandler(403)
def forbidden(e): return render_template('message.html', title='Akses ditolak', message='Halaman ini khusus admin.'),403
@app.errorhandler(413)
def too_large(e): return 'Total ukuran upload terlalu besar. Foto maksimal 5 MB dan file digital maksimal 50 MB.',413

init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT','5001')), debug=False)

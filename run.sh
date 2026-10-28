#!/data/data/com.termux/files/usr/bin/bash
set -e
cd "$(dirname "$0")"
pkg install python -y >/dev/null 2>&1 || true
if [ ! -d .venv ]; then python -m venv .venv; fi
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; echo "File .env dibuat. Isi kredensial NasionalPay dan SMTP sebelum dipakai." ; fi
if ! grep -q '^FLASK_SECRET_KEY=.' .env; then
  SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
  sed -i "s/^FLASK_SECRET_KEY=.*/FLASK_SECRET_KEY=$SECRET/" .env
fi
python app.py

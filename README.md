# Mudzz Story — Termux / Android

Toko digital berbasis Flask + SQLite, disiapkan untuk berjalan di Termux tanpa root.

## Fitur
- Daftar akun dengan OTP email, login, dan reset password lewat email.
- Dashboard pengguna: All TRX, saldo saya, file dibeli, dan aktivitas terbaru.
- Banner promosi pembayaran dengan QRIS serta aplikasi yang mendukung QRIS (DANA, GoPay, OVO, ShopeePay, dan lainnya).
- Shop terpisah, keranjang belanja kuning, pengaturan jumlah per produk, checkout sekaligus dengan saldo, riwayat pesanan dan download file digital.
- Rating bintang 1–5 dan ulasan setelah pembelian, dengan ringkasan rating di halaman produk.
- Deposit QRIS NasionalPay: buat invoice, QR payload/image, countdown, cek status, tutup invoice, webhook, dan kredit saldo idempoten.
- Panel admin: tambah/edit/nonaktifkan produk, upload foto dan file digital, penyesuaian saldo, pengelolaan pesanan/laporan, broadcast Telegram opsional.
- Menu hamburger lebih ringkas dengan nama MUDZSTORY lebih jelas, welcome mengetik dan suara “Welcome to Mudzz Story” di halaman login/daftar, serta kontak Telegram/WhatsApp di footer.

## Instalasi di Termux
1. Ekstrak ZIP ke folder home Termux.
2. Jalankan:
   ```bash
   cd ~/Mudzz-Story-NasionalPay
   cp .env.example .env
   nano .env
   bash run.sh
   ```
3. Saat pertama kali database dibuat, terminal menampilkan username dan password admin sementara bila `ADMIN_PASSWORD` belum diisi dengan password minimal 12 karakter. Simpan password tersebut.
4. Buka browser HP ke `http://127.0.0.1:5000`.

Kalau port 5000 sudah dipakai:
```bash
PORT=5001 bash run.sh
```
Lalu buka `http://127.0.0.1:5001`.

## Keranjang dan ulasan
- Tambahkan produk dari halaman detail ke keranjang, atur jumlah, hapus item, lalu checkout dengan total yang dihitung ulang di server.
- Stok dicek kembali pada saat checkout agar jumlah melebihi stok tidak bisa dibeli. Setiap unit dicatat sebagai item pesanan sehingga riwayat dan unduhan tetap berjalan.
- Rating/ulasan hanya bisa diberikan setelah pesanan berhasil, satu ulasan per produk per akun.

## Email OTP
Isi `SMTP_USER`, `SMTP_PASSWORD`, dan `SMTP_FROM`. Untuk Gmail, gunakan Google App Password khusus aplikasi, bukan kata sandi Gmail biasa. Jangan membagikan `.env`.

## NasionalPay
- Isi `NASIONALPAY_STORE_KEY` dengan kunci dari akun merchant milikmu. Timestamp kedaluwarsa dari API dinormalisasi; bila formatnya salah atau waktunya sudah lewat/hampir habis saat invoice baru dibuat, halaman memakai fallback 15 menit. Status pembayaran tetap harus diverifikasi oleh provider.
- Konfigurasikan callback/webhook di dashboard NasionalPay ke URL publik HTTPS:
  `https://DOMAIN-PUBLIK-KAMU/webhooks/nasionalpay`
- `SITE_URL` sebaiknya berisi URL HTTPS publik yang sama.
- API key/Store Key tidak boleh ditaruh di JavaScript atau dibagikan ke pengguna.
- Pastikan dokumentasi merchant menyatakan format signature `X-NasionalPay-Signature` yang dipakai endpoint webhook ini sesuai dengan akunmu. Bila skema signature dari dashboard berbeda, sesuaikan kode verifikasinya dengan dokumentasi resmi.
- QRIS DANA/GoPay/OVO/ShopeePay di banner hanya ilustrasi metode yang dapat memindai QRIS; bukan bukti bahwa tiap provider terhubung sebagai kanal pembayaran terpisah.
- Pembatalan invoice di halaman web menutup invoice lokal. Integrasi yang disalin tidak memanggil endpoint cancel gateway; jangan lanjutkan pembayaran setelah invoice ditutup.
- Saldo hanya ditambah setelah status terverifikasi dari provider atau rekonsiliasi admin yang benar-benar memeriksa transaksi pada dashboard merchant. Webhook/status harus cocok dengan ID dan nominal, dan ledger mencegah pengkreditan ganda.

Untuk kontak admin, isi `TELEGRAM_CHANNEL_URL` dengan tautan `https://t.me/username` dan `WHATSAPP_CHANNEL_URL` dengan tautan `https://wa.me/62...` di `.env`. Tombol suara memakai fitur text-to-speech browser; kalau browser memblokir suara otomatis, tekan “Putar suara”.

## Keamanan sebelum dipublikasikan
- Ganti semua kredensial yang pernah tertulis dalam arsip sumber, termasuk Store Key NasionalPay, token Telegram, dan Gmail App Password.
- Gunakan HTTPS publik, password admin kuat, backup database, serta tinjau aturan webhook dan kebijakan provider sebelum menerima uang sungguhan.
- Jangan gunakan `store.db` dari komputer lain tanpa sengaja; ZIP bersih ini tidak menyertakan database maupun file produk pribadi.


## Chat AI & komunitas publik
- Tombol **CHAT MUDZZ STORY** muncul di kanan bawah hanya untuk pengguna yang sudah login; tidak muncul di halaman awal pengunjung, login, atau daftar.
- Tab **CHAT AI** menyediakan bantuan dasar tanpa API key. Untuk AI generatif, isi `GEMINI_API_KEY` (dan opsional `GEMINI_MODEL`, default `gemini-2.5-flash`) dari Google AI Studio; alternatifnya atur `AI_API_URL`, `AI_API_KEY`, dan `AI_MODEL` untuk endpoint kompatibel OpenAI. Kuota gratis bergantung pada kebijakan provider. Di hosting, isi variabel ini lewat dashboard Environment, jangan unggah API key ke GitHub.
- Tab **KOMUNITAS PUBLIK** dan halaman `/komunitas` adalah ruang obrolan bersama untuk semua pengguna yang login. Pesan disimpan di database.

## Hosting publik
### Render (paling mudah, paket gratis)
1. Upload proyek ke repository GitHub milikmu tanpa `.env`, `store.db`, atau file rahasia.
2. Di Render pilih **New + → Blueprint**, sambungkan repository tersebut, lalu deploy `render.yaml`; atau buat Web Service dengan Build Command `pip install -r requirements.txt` dan Start Command `gunicorn app:app --workers 1 --threads 4 --timeout 120`.
3. Isi environment variables seperti `NASIONALPAY_STORE_KEY`, SMTP, link kontak, dan konfigurasi AI lewat dashboard Render, bukan di source code.
4. URL `https://nama-service.onrender.com` bisa dibuka orang lain.

**Batasan penting hosting gratis:** Render Free dapat tidur setelah tidak ada trafik dan disk lokalnya tidak persisten. Karena itu layanan gratis ini tidak menjamin 24/7, dan SQLite/file upload/riwayat bisa hilang saat instance diganti. Untuk toko dengan transaksi sungguhan, gunakan hosting dengan persistent disk + database PostgreSQL dan storage file persisten. Untuk benar-benar aktif 24 jam dengan biaya $0, Oracle Cloud Always Free VPS bisa jadi pilihan jika akun dan resource gratis tersedia, tetapi setup lebih teknis dan mungkin meminta verifikasi pembayaran. Jangan gunakan Render Free untuk transaksi produksi sebelum database, file storage, dan webhook pembayaran dibuat persisten.

## Tetap jalan di Termux
```bash
cd ~/Mudzz-Story-NasionalPay
bash run.sh
```
Termux dan hosting publik adalah dua deployment terpisah. Keduanya bisa memakai source code yang sama, tetapi masing-masing memiliki database dan konfigurasi sendiri kecuali sengaja disambungkan ke layanan database/storage bersama.

### Tentang data & uptime
Render Free menggunakan filesystem sementara untuk service gratis. Database SQLite, upload produk, file digital, dan gambar QR dapat hilang saat restart/deploy. Service juga dapat tidur saat sepi. Ini cocok untuk demo/testing, bukan jaminan toko uang sungguhan aktif 24/7. Untuk produksi, pindahkan database ke PostgreSQL dan file ke persistent storage/object storage, lalu gunakan host yang tidak sleep. Jangan memasukkan kunci API atau kata sandi ke repository publik.

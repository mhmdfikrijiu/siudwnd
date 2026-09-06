# NartoDrama Downloader

## Struktur proyek

```
src/
  bot/main.py                 # UI Telegram: link -> pilih episode -> kirim file
  core/config.py              # seluruh konfigurasi environment
  services/telegram_downloads.py # validasi, unduh sementara, dan pembersihan
app.py                         # web UI Flask lama (tetap kompatibel)
nartodrama_downloader.py       # adapter sumber video/HLS
templates/ dan static/         # UI web
```

## Menjalankan bot Telegram

1. Salin `.env.example` menjadi `.env`, lalu isi token BotFather.
2. Jalankan `docker compose up -d --build`.
3. Buka bot dan kirim `/start`, lalu kirim link `https://narto-drama.com/detail/watch/...`.

Untuk file FULL di atas batas Bot API cloud, gunakan [LOCAL_BOT_API.md](LOCAL_BOT_API.md). Konfigurasinya berjalan sebagai service dan volume Docker terpisah.

Bot menyediakan episode tunggal dan tombol FULL untuk menggabungkan seluruh episode menjadi satu MP4. FULL mengunduh episode secara paralel terbatas (maksimal 2 worker), lalu semua berkas job dihapus setelah terkirim atau gagal. Saat boot, job sisa berumur lebih dari satu jam juga dibersihkan.

## Kebijakan storage VPS yang disarankan

- Gunakan volume cache maksimal **1 GB** dan TTL **2 jam** untuk web (`NARTO_CACHE_MAX_MB=1024`).
- Bot tidak memakai cache episode: setiap file dibuat di job folder unik, dikirim, lalu dihapus.
- Batasi unduhan bot menjadi 2 (`TELEGRAM_MAX_CONCURRENT_DOWNLOADS=2`) agar CPU, bandwidth, dan disk sementara terukur.
- Hindari ZIP/FULL di bot karena mengadakan salinan kedua/ketiga dari video. Bila diperlukan, jadikan fitur admin-only dan hapus hasil maksimal 10 menit.
- Pasang alert ketika volume `/data` melewati 70%; pembersihan berbasis TTL bukan pengganti monitoring disk.

Pastikan Anda memiliki izin untuk mengunduh dan mendistribusikan materi yang diminta melalui bot.

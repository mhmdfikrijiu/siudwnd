# Deploy ke VPS

## Prasyarat

- Docker Engine dan Docker Compose plugin tersedia di VPS.
- Nama domain/reverse proxy opsional untuk web UI. Port web hanya di-bind ke `127.0.0.1:5000`, jadi tidak terbuka langsung ke internet.
- Sisakan minimal 4 GB disk kosong bila ingin mengirim FULL sampai sekitar 1 GB: episode dan hasil gabungan hidup bersamaan selama proses.

## Konfigurasi rahasia

Di folder proyek pada VPS, buat `.env` dari template tanpa memasukkan nilainya ke Git atau chat:

```bash
cp .env.vps.example .env
chmod 600 .env
nano .env
```

Isi `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, dan `TELEGRAM_API_HASH` yang masih aktif. Jika token/hash pernah tampil di log atau chat, buat yang baru sebelum deploy.

## Menjalankan stack terpisah

```bash
set -a; . ./.env; set +a
curl --fail-with-body "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/logOut"
docker compose -f docker-compose.yml -f docker-compose.local-api.yml pull
docker compose -f docker-compose.yml -f docker-compose.local-api.yml up -d
docker compose -f docker-compose.yml -f docker-compose.local-api.yml ps
```

`logOut` perlu dilakukan satu kali sebelum bot pindah dari API cloud ke Local Bot API. Jangan jalankan dua instance bot dengan token yang sama.

Service yang berjalan:

- `narto`: web UI pada `127.0.0.1:5000`.
- `telegram-bot`: bot downloader dan UI Telegram.
- `telegram-api`: Local Bot API; tidak memiliki port publik dan hanya dapat diakses oleh bot melalui jaringan Compose.

## Pemeriksaan dan operasi

```bash
docker compose -f docker-compose.yml -f docker-compose.local-api.yml logs -f telegram-bot
curl http://127.0.0.1:5000/health
docker system df
```

Untuk deploy perubahan baru, jalankan kembali `docker compose ... pull` lalu `docker compose ... up -d`. Untuk rollback aplikasi ke image terakhir yang masih tersimpan, gunakan `docker compose ... down` lalu jalankan tag/image sebelumnya sesuai kebijakan rilis Anda; jangan hapus volume `narto_cache`, `telegram_jobs`, atau `telegram_api_data` kecuali memang ingin menghapus seluruh cache/data Bot API.

## Batas storage

- Cache web dibatasi 1 GB dan dibersihkan setelah 2 jam.
- Folder job Telegram dibersihkan sesudah kirim dan dipindai tiap 30 menit; job tua lebih dari 180 menit dihapus.
- `telegram_api_data` adalah volume terpisah. Pantau dengan `docker system df`; jangan menjalankan prune volume secara buta.

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

Isi `TELEGRAM_BOT_TOKEN`, `TELEGRAM_FULL_BOT_TOKEN`, `TELEGRAM_API_ID`, dan `TELEGRAM_API_HASH` yang masih aktif. `TELEGRAM_FULL_BOT_TOKEN` harus token bot kedua khusus tombol FULL. Jika token/hash pernah tampil di log atau chat, buat yang baru sebelum deploy.

## Menjalankan stack terpisah

```bash
set -a; . ./.env; set +a
curl --fail-with-body "https://api.telegram.org/bot${TELEGRAM_FULL_BOT_TOKEN}/logOut"
docker compose -f docker-compose.yml -f docker-compose.local-api.yml pull
docker compose -f docker-compose.yml -f docker-compose.local-api.yml up --build -d
docker compose -f docker-compose.yml -f docker-compose.local-api.yml ps
```

`logOut` perlu dilakukan satu kali untuk token **FULL** sebelum bot tersebut pindah dari API cloud ke Local Bot API. Jangan jalankan dua instance dengan token yang sama.

Service yang berjalan:

- `narto`: web UI pada `127.0.0.1:5000`.
- `telegram-bot`: bot utama lewat Cloud Bot API; episode satuan maksimal 49 MB.
- `telegram-full-bot`: bot kedua khusus FULL, lewat Local Bot API hingga 1.9 GB.
- `telegram-api`: Local Bot API tanpa port publik; hanya dapat diakses oleh `telegram-full-bot` melalui jaringan Compose.

Gunakan bot utama untuk episode satuan dan bot kedua untuk FULL. Token bot utama selalu memakai Cloud Bot API; konfigurasi Compose mengosongkan `TELEGRAM_API_BASE_URL` untuk service tersebut agar tidak ikut memakai endpoint lokal.

## Pemeriksaan dan operasi

```bash
docker compose -f docker-compose.yml -f docker-compose.local-api.yml logs -f telegram-bot
curl http://127.0.0.1:5000/health
docker system df
```

Untuk deploy perubahan baru, jalankan kembali `docker compose ... pull` lalu `docker compose ... up --build -d`. Build hanya untuk image Python proyek; `telegram-api` tetap memakai image siap pakai. Untuk rollback aplikasi ke image terakhir yang masih tersimpan, gunakan `docker compose ... down` lalu jalankan tag/image sebelumnya sesuai kebijakan rilis Anda; jangan hapus volume `narto_cache`, `telegram_jobs`, atau `telegram_api_data` kecuali memang ingin menghapus seluruh cache/data Bot API.

## Batas storage

- Cache web dibatasi 1 GB dan dibersihkan setelah 2 jam.
- Folder job Telegram dibersihkan sesudah kirim dan dipindai tiap 30 menit; job tua lebih dari 180 menit dihapus.
- `telegram_api_data` adalah volume terpisah. Pantau dengan `docker system df`; jangan menjalankan prune volume secara buta.

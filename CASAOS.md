# Instalasi dan upgrade V2.5.4.2 di CasaOS

Dokumen ini khusus server CasaOS. Petunjuk Windows berada di `README.md`.

## Struktur folder

Gunakan source dan data persisten yang terpisah:

```text
/DATA/AppData/rsi-martingale-webui/
├── source/            # isi paket V2.5.4.2
└── data/
    └── state.json     # akun, token, EA, parameter, jadwal
```

Jangan meletakkan `state.json` di dalam folder release yang akan diganti.

## Instalasi baru

1. Ekstrak paket V2.5.4.2 di komputer, lalu unggah isi folder `V2.5.4.2` ke:

   ```text
   /DATA/AppData/rsi-martingale-webui/source
   ```

2. Buka terminal CasaOS dan buat folder data:

   ```bash
   mkdir -p /DATA/AppData/rsi-martingale-webui/data
   ```

3. Build dan jalankan:

   ```bash
   cd /DATA/AppData/rsi-martingale-webui/source
   docker compose -f docker-compose.casaos.yml up -d --build
   docker compose -f docker-compose.casaos.yml ps
   curl http://127.0.0.1:8000/api/health
   ```

4. Health check harus menampilkan `"version":"2.5.4.2"`.

5. Buka WebUI dari LAN, misalnya:

   ```text
   http://10.0.0.15:8000
   ```

6. Buat administrator pertama dan simpan token EA yang ditampilkan.

## Upgrade dari V2.5.4.1 atau versi sebelumnya

Misalnya paket ZIP diunggah ke `/DATA/AppData/rsi-martingale-webui/releases/V2.5.4.2.zip`.

1. Masuk melalui SSH/terminal CasaOS.

2. Hentikan server lama:

   ```bash
   cd /DATA/AppData/rsi-martingale-webui/source
   docker compose -f docker-compose.casaos.yml down
   ```

3. Backup state:

   ```bash
   mkdir -p /DATA/AppData/rsi-martingale-webui/backups
   cp /DATA/AppData/rsi-martingale-webui/data/state.json /DATA/AppData/rsi-martingale-webui/backups/state-before-v2.5.4.2.json
   ```

   Jika `state.json` belum ada, perintah `cp` boleh dilewati.

4. Ekstrak release ke folder sementara:

   ```bash
   mkdir -p /DATA/AppData/rsi-martingale-webui/releases/V2.5.4.2
   unzip -o /DATA/AppData/rsi-martingale-webui/releases/V2.5.4.2.zip -d /DATA/AppData/rsi-martingale-webui/releases/V2.5.4.2
   ```

5. Cari folder yang berisi `Dockerfile`. Paket resmi memiliki folder teratas `V2.5.4.2`:

   ```bash
   find /DATA/AppData/rsi-martingale-webui/releases/V2.5.4.2 -maxdepth 2 -name Dockerfile -print
   ```

6. Ganti source. Perintah berikut mengasumsikan hasilnya berada di `.../V2.5.4.2/V2.5.4.2`:

   ```bash
   rm -rf /DATA/AppData/rsi-martingale-webui/source.new
   cp -a /DATA/AppData/rsi-martingale-webui/releases/V2.5.4.2/V2.5.4.2 /DATA/AppData/rsi-martingale-webui/source.new
   mv /DATA/AppData/rsi-martingale-webui/source /DATA/AppData/rsi-martingale-webui/source.old
   mv /DATA/AppData/rsi-martingale-webui/source.new /DATA/AppData/rsi-martingale-webui/source
   ```

   Folder `/DATA/AppData/rsi-martingale-webui/data` tidak disentuh.

7. Build ulang:

   ```bash
   cd /DATA/AppData/rsi-martingale-webui/source
   docker compose -f docker-compose.casaos.yml up -d --build
   docker compose -f docker-compose.casaos.yml ps
   curl http://127.0.0.1:8000/api/health
   ```

8. Periksa log:

   ```bash
   docker compose -f docker-compose.casaos.yml logs --tail=100 webui
   ```

9. Login dan pastikan user, EA ID, dan token hint lama masih ada. User lama tanpa masa berlaku tetap aktif dan ditandai **Masa berlaku belum diatur**. Tetapkan waktunya melalui **Kelola User**, lalu lakukan hard refresh browser (`Ctrl+F5`).

10. Setelah verifikasi berhasil, `source.old` boleh disimpan sementara sebagai rollback. Jangan menghapus backup `state-before-v2.5.4.2.json` sebelum sistem stabil.

## Rollback source

Jika container V2.5.4.2 gagal hidup, hentikan lalu kembalikan source lama:

```bash
cd /DATA/AppData/rsi-martingale-webui/source
docker compose -f docker-compose.casaos.yml down
mv /DATA/AppData/rsi-martingale-webui/source /DATA/AppData/rsi-martingale-webui/source.failed
mv /DATA/AppData/rsi-martingale-webui/source.old /DATA/AppData/rsi-martingale-webui/source
cd /DATA/AppData/rsi-martingale-webui/source
docker compose -f docker-compose.casaos.yml up -d --build
```

Jangan mengembalikan state lama kecuali benar-benar diperlukan; rollback state dapat menghilangkan user/EA baru yang dibuat setelah upgrade.

## Multi-EA per akun

Pada **Kelola User**:

1. buat user dan EA pertama seperti biasa;
2. pada **Tambah EA ke akun**, pilih username;
3. isi EA ID baru dan IP/CIDR;
4. tekan **Tambah EA ID**;
5. simpan token yang muncul khusus untuk EA tersebut.

Setiap EA ID mempunyai token dan status koneksi terpisah. User melihat pemilih EA pada dashboard bila akunnya mempunyai lebih dari satu EA. Admin melihat semua kombinasi username–EA.

## Memilih jenis EA

Hanya administrator yang dapat menentukan jenis EA untuk setiap EA ID:

1. Pastikan status EA ID tersebut **offline** pada Kelola User.
2. Pada kolom **Jenis EA**, pilih **Regular** atau **Trailing**.
3. EA ID dan token tetap sama; hanya file `.ex5` dan field parameter WebUI yang berubah.
4. Pasang file sesuai pilihan: `RSI_Martingale_Web_Control.ex5` untuk Regular atau `RSI_Martingale_Web_Control_Trailing.ex5` untuk Trailing.

User biasa tidak melihat atau dapat mengubah pemilih jenis EA.

## Hubungkan MT5

Isi setiap instance EA dengan pasangan EA ID dan token yang sesuai:

```text
ApiBaseUrl    = https://ea.agoy507.my.id/
EaId           = ea-user-01
ApiToken       = token-khusus-ea-user-01
ApiPollSeconds = 0.5
```

Di MT5, tambahkan `https://ea.agoy507.my.id` pada **Tools → Options → Expert Advisors → Allow WebRequest for listed URL**.

EA V2.5.3 dan V2.5.4 tetap kompatibel sebagai jenis Regular. Untuk memakai EA Trailing, administrator memilih jenis **Trailing** di **Kelola User** saat EA ID offline, lalu gunakan file `RSI_Martingale_Web_Control_Trailing.ex5` dari paket V2.5.4.2. EA ID dan token tidak perlu diganti.

## Masa berlaku user

- Masa berlaku wajib diisi saat membuat user baru dan menggunakan zona waktu Asia/Jakarta.
- Hanya administrator yang dapat menetapkan atau memperpanjangnya melalui **Kelola User**.
- Setelah kedaluwarsa, perpanjang tanggal lalu tekan **Aktifkan**. Token EA tidak berubah.
- Masa berlaku satu akun mencakup semua EA ID milik akun itu.
- Dashboard user menampilkan tanggal berakhir dan hitung mundur.
- Akun lama dari V2.5.4 tidak langsung dinonaktifkan ketika upgrade. Administrator dapat menetapkan masa berlakunya setelah server baru berhasil diverifikasi.

## Jaringan, tunnel, dan cookie

- Gunakan IP LAN CasaOS yang tetap. Jika CasaOS berada di Proxmox, gunakan bridge LAN seperti `vmbr0`, bukan IP Docker `172.x.x.x`.
- Apache tidak diperlukan; container menjalankan Uvicorn sendiri.
- Untuk HTTPS melalui tunnel/reverse proxy, ubah `COOKIE_SECURE` menjadi `"1"` pada Compose.
- Untuk HTTP LAN, biarkan `COOKIE_SECURE: "0"`; cookie Secure tidak dikirim melalui HTTP.
- Jangan membuka port 8000 langsung ke internet tanpa HTTPS/VPN dan kontrol akses.
- Proxy/tunnel harus mengizinkan Server-Sent Events dan tidak melakukan buffering agar indikator terasa realtime. Polling 500 ms tetap menjadi cadangan.

## Perintah operasional

Restart tanpa rebuild:

```bash
cd /DATA/AppData/rsi-martingale-webui/source
docker compose -f docker-compose.casaos.yml restart
```

Rebuild setelah source berubah:

```bash
cd /DATA/AppData/rsi-martingale-webui/source
docker compose -f docker-compose.casaos.yml up -d --build
```

Log realtime:

```bash
cd /DATA/AppData/rsi-martingale-webui/source
docker compose -f docker-compose.casaos.yml logs -f webui
```

Backup state berkala:

```bash
cp /DATA/AppData/rsi-martingale-webui/data/state.json /DATA/AppData/rsi-martingale-webui/backups/state-$(date +%F-%H%M%S).json
```

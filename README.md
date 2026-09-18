# RSI Martingale Multiuser Control V2.5.4.2

V2.5.4.2 adalah rilis terpisah dari V2.5.4.1 yang mendukung dua jenis EA dalam satu WebUI: **Regular** dan **Trailing**. Server tetap dapat mengontrol beberapa akun user dan beberapa EA ID pada setiap akun. Setiap EA ID mempunyai token, aturan IP, konfigurasi, status START/STOP, Stop Behavior, jadwal, Entry Mode aktif, dan telemetry sendiri.

Folder serta paket V2.5.3 dan V2.5.4 tidak diubah.

## Perubahan V2.5.4.2

- Administrator memilih jenis EA **Regular** atau **Trailing** untuk setiap EA ID pada halaman **Kelola User**. User biasa tidak dapat mengubah pilihan ini.
- Penggantian jenis hanya diizinkan saat EA tersebut offline. EA ID dan token tidak berubah.
- WebUI menampilkan field parameter sesuai jenis EA yang dipilih.
- EA Trailing memakai algoritma RSI dan martingale yang sama untuk entry, jarak layer, serta tangga lot absolut milik EA Regular.
- Pada EA Trailing, hanya layer pertama yang memiliki TP. TP itu mengikuti harga rata-rata (BE) ditambah/dikurangi parameter `TP layer pertama dari BE` selama trailing belum aktif; layer kedua dan seterusnya selalu TP `0`. TP layer pertama bergeser setiap kali layer baru mengubah BE. Ketika syarat trailing terpenuhi, TP dihapus dan exit dikelola oleh trailing SL gabungan.
- EA Trailing juga memiliki proteksi retcode `10026`: entry awal dan layer martingale dijeda eksponensial 1, 2, 4, 8, 16, 32, hingga maksimal 60 menit. Pengelolaan trailing, SL, dan Close All posisi aktif tetap berjalan.

## Fitur V2.5.4.1 yang tetap tersedia

- Setiap user baru selain administrator wajib memiliki tanggal dan jam masa berlaku.
- Masa berlaku hanya dapat dibuat atau diperpanjang administrator melalui **Kelola User**.
- Masa berlaku berlaku untuk seluruh EA ID pada akun tersebut.
- Saat waktunya habis, akun otomatis nonaktif, sesi WebUI diputus, token seluruh EA akun itu ditolak, dan status kontrol server menjadi STOP.
- Memperpanjang masa berlaku tidak otomatis mengaktifkan akun. Setelah memperpanjang, administrator menekan **Aktifkan** agar pemeriksaan dapat dilakukan terlebih dahulu.
- Mengaktifkan kembali akun tidak mengganti token EA. Token hanya berubah jika tombol **Token baru** digunakan.
- Tanggal berakhir dan hitung mundur ditampilkan pada dashboard user. Pada dashboard administrator, kartu tersebut mengikuti pemilik EA yang sedang dipilih.
- Dropdown **EA Saya** selalu tampil pada dashboard user dan menunjukkan EA ID yang sedang dikelola. Isinya hanya EA milik akun tersebut, termasuk ketika akun baru mempunyai satu EA.
- EA melindungi broker dari pengulangan order setelah menerima retcode `10026`: entry awal dan layer martingale dijeda secara eksponensial selama 1, 2, 4, 8, 16, 32, lalu maksimal 60 menit. Satu percobaan baru diizinkan setelah jeda; keberhasilan mereset urutan jeda.
- Akun administrator tidak memiliki masa kedaluwarsa.
- Saat upgrade, user lama yang aktif dan belum mempunyai data masa berlaku tetap aktif. Kelola User menandainya **Masa berlaku belum diatur** agar administrator dapat menetapkannya tanpa memutus koneksi pada saat upgrade.

## Perubahan utama V2.5.4

- Satu akun WebUI dapat mempunyai lebih dari satu EA ID.
- Setiap EA ID memperoleh token unik; token EA pertama tidak berlaku untuk EA kedua.
- User biasa dapat memilih semua EA yang terdaftar pada akunnya dari dashboard.
- Administrator dapat memilih dan mengontrol seluruh EA milik seluruh user.
- Form **Tambah EA ke akun** tersedia pada halaman **Kelola User**.
- Saldo, profit hari ini, status algoritma, identitas koneksi, IP, dan token tersamarkan ditampilkan per EA.
- Katalog profil **Entry Mode / Profil RSI global** dipindahkan dari dashboard admin ke halaman **Kelola User**.
- Indikator BUY/SELL V2.5.4 memakai RSI candle berjalan khusus untuk tampilan. Keputusan entry tetap memakai RSI candle selesai agar algoritma trading tidak berubah.
- EA V2.5.4 menggabungkan pengiriman telemetry dan penerimaan konfigurasi menjadi satu WebRequest setiap heartbeat. Server mendorong perubahan dashboard melalui SSE dengan polling 500 ms sebagai cadangan.
- Profit harian dicache dan dihitung ulang sekitar setiap 5 detik agar heartbeat 500 ms tidak terhambat pemindaian histori transaksi.

## Kompatibilitas

- `data/state.json` V2.5.3 dapat dipakai langsung. Akun, password, token, EA ID, IP, parameter, jadwal, status kontrol, dan Entry Mode aktif dipertahankan.
- EA ID lama otomatis menjadi EA pertama pada akun pemiliknya.
- File MQ5/EX5 V2.5.3 tetap dapat terhubung sebagai jenis **Regular**, tetapi belum memiliki proteksi retry retcode 10026.
- Pasang EA V2.5.4 bila ingin indikator RSI live dan jalur heartbeat satu permintaan. EA V2.5.3 tetap menampilkan RSI candle selesai dan memakai dua permintaan per siklus polling.
- Algoritma lot dan aturan trading V2.5.3 tidak diubah pada V2.5.4.

## Perilaku strategi

- BUY awal ketika RSI M1 berada di bawah level beli.
- SELL awal ketika RSI M1 berada di atas level jual.
- Tidak membuka arah berlawanan selama satu siklus aktif.
- Martingale ditambah setelah harga bergerak berlawanan sejauh jarak yang ditentukan.
- TP memakai harga rata-rata berbobot lot dan SL memakai posisi terakhir.
- Posisi dipisahkan berdasarkan `Symbol + MagicNumber`.
- Akun MT5 wajib menggunakan mode hedging.
- EA mencegah siklus baru saat kontrol server belum pernah sinkron atau heartbeat kontrol telah kedaluwarsa. Siklus yang sudah aktif tetap dikelola sesuai Stop Behavior.

Lot setiap posisi dihitung dengan `Lot awal × Pengali^tahap`. Entry awal adalah tahap 0. Layer tambahan dibulatkan naik ke step volume broker dan dibatasi Lot maksimal. Karena pembulatan dilakukan terhadap rumus absolut tiap tahap, beberapa tahap dapat menghasilkan lot yang sama.

## Perilaku EA Trailing

EA Trailing memakai sinyal RSI, penguncian siklus, jarak martingale, perhitungan lot, batas lot, Magic Number, fail-safe koneksi, dan Stop Behavior yang sama dengan EA Regular. Perbedaannya hanya pada cara exit:

1. Selama trailing belum aktif, hanya posisi pertama/tertua satu arah yang menerima TP `harga rata-rata berbobot lot ± TP layer pertama dari BE`. Saat layer baru dibuka, BE dan TP posisi pertama berpindah bersama-sama; layer kedua dan seterusnya memiliki TP `0`.
2. Setelah harga bergerak dari BE minimal `Trailing mulai`, TP seluruh posisi dihapus.
3. EA kemudian memasang satu trailing SL untuk seluruh posisi arah tersebut, berjarak `Trailing jarak` dari harga berjalan dan hanya bergeser minimal `Trailing geser`. SL tidak pernah dilonggarkan.
4. Jika harga menyentuh trailing SL, EA mencoba menutup seluruh posisi miliknya pada arah tersebut. Tombol Stop **Tutup semua posisi EA** tetap menutup seluruh arah dan seluruh posisi milik Symbol + Magic Number EA.

Parameter khusus Trailing adalah `SL awal cycle`, `TP layer pertama dari BE`, `Trailing mulai`, `Trailing jarak`, dan `Trailing geser`. Parameter Regular `SL dari posisi terakhir` dan `TP dari harga rata-rata` tidak ditampilkan untuk jenis Trailing.

## Instalasi Windows

Persyaratan: Windows 10/11 atau Windows Server dan Python 3.12.

```powershell
cd E:\EA\V2.5.4.2
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\run_server.ps1
```

Atau klik dua kali `START_WEBUI.bat`. Buka `http://127.0.0.1:8000`.

Launcher bawaan mengikat server ke localhost. Untuk akses dari perangkat LAN:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

Izinkan port `8000/TCP` pada Windows Firewall hanya untuk jaringan yang diperlukan.

## Instalasi dan upgrade CasaOS

Petunjuk terpisah ada di [CASAOS.md](CASAOS.md). Ringkasnya, source berada di `/DATA/AppData/rsi-martingale-webui/source`, sedangkan state persisten berada di `/DATA/AppData/rsi-martingale-webui/data/state.json`.

```bash
cd /DATA/AppData/rsi-martingale-webui/source
docker compose -f docker-compose.casaos.yml up -d --build
curl http://127.0.0.1:8000/api/health
```

Health check harus mengembalikan `"version":"2.5.4.2"`.

## Setup administrator dan pengelolaan EA

Pada instalasi baru, buka WebUI dan buat administrator pertama. Simpan token yang ditampilkan karena token lengkap hanya terlihat sekali.

Pada halaman **Kelola User**, administrator dapat:

- membuat user beserta EA pertamanya;
- menambah EA ID kedua dan seterusnya ke akun yang dipilih;
- melihat setiap EA sebagai baris terpisah;
- mengubah IP/CIDR setiap EA;
- memilih **Regular** atau **Trailing** untuk setiap EA ID yang offline;
- membuka dashboard setiap EA;
- merotasi token khusus EA yang dipilih;
- menonaktifkan/mengaktifkan user biasa;
- menetapkan atau memperpanjang masa berlaku user biasa tanpa mengganti token EA;
- menghapus user biasa beserta seluruh EA server-side miliknya;
- membuat, mengubah, dan menghapus katalog global Profil RSI.

EA ID harus unik di seluruh server. Satu EA ID hanya menjalankan satu jenis EA pada suatu waktu. Untuk setiap EA baru, salin pasangan berikut secara persis ke satu instance MT5:

```text
EaId     = ea-id-yang-dibuat
ApiToken = token-khusus-ea-tersebut
```

Menghapus user tidak menutup posisi MT5. Penghapusan ditolak bila salah satu EA milik user masih online. Akun administrator tidak dapat dihapus.

## Pengaturan akun user

Jika sebuah akun mempunyai beberapa EA, pemilik akun melihat pemilih **Pilih EA** pada dashboard. Semua operasi parameter, START/STOP, Stop Behavior, jadwal, kalkulator, dan pilihan Entry Mode diarahkan ke EA yang sedang dipilih.

Di **Pengaturan Akun**, user dapat memilih EA miliknya lalu:

- menyalin EA ID;
- melihat token aktif secara tersamarkan dan enam karakter terakhir;
- membuat token baru khusus EA terpilih setelah mengonfirmasi password;
- mengganti password login.

Rotasi token hanya menonaktifkan token lama milik EA terpilih. Token EA lain pada akun yang sama tidak berubah. Masukkan token baru ke `ApiToken` MT5 dan inisialisasi ulang EA tersebut.

## Masa berlaku akun

- Form **Tambah user** memakai waktu Asia/Jakarta.
- Administrator dapat mengubah waktunya pada baris pertama akun di **Kelola User**. EA kedua dan seterusnya mengikuti akun yang sama.
- User dapat melihat tanggal berakhir dan hitung mundur pada kartu **Masa Berlaku Akun** di dashboard, tetapi tidak dapat mengubahnya.
- Setelah akun kedaluwarsa, perpanjang tanggalnya terlebih dahulu lalu tekan **Aktifkan**.
- Token semua EA tetap sama setelah perpanjangan dan aktivasi ulang.
- Server memeriksa kedaluwarsa setiap detik dan juga pada setiap login, sesi, dashboard, daftar user, serta permintaan EA.

Kedaluwarsa menghentikan kontrol server dan mencegah heartbeat/token berikutnya. EA yang sedang mempunyai posisi mengikuti fail-safe lokal yang sudah ada: siklus aktif tetap dikelola, sedangkan siklus baru tidak boleh dimulai setelah kontrol server stale. Jika posisi harus ditutup tepat sebelum akun habis, gunakan Stop Behavior **Tutup semua posisi EA** terlebih dahulu.

## Entry Mode / Profil RSI

Administrator mengelola katalog profil pada **Kelola User → Profil RSI global**. Jumlah profil tidak dibatasi aplikasi. Setiap profil berisi nama, periode RSI, level BUY, dan level SELL.

User biasa hanya melihat nama profil dan memilihnya untuk EA yang sedang aktif; nilai RSI profil tidak ditampilkan. Pilihan bersifat per EA, sehingga dua EA pada satu akun dapat memakai profil berbeda. Parameter lain, jadwal, dan Stop Behavior tetap dapat diubah pemilik akun. Administrator tetap dapat mengubah seluruh pengaturan operasional EA mana pun.

Ukuran area hijau dan merah indikator BUY/SELL mengikuti level profil/parameter sebenarnya. Angka RSI live pada EA V2.5.4 hanya untuk telemetry visual; logika entry tetap memakai candle selesai.

## Memasang EA di MT5

Di **Kelola User**, pilih dulu jenis EA pada baris EA ID saat statusnya offline. Lalu salin file yang sesuai ke folder `MQL5/Experts` dan refresh Navigator atau restart MT5:

- **Regular**: `ea/RSI_Martingale_Web_Control.ex5`
- **Trailing**: `ea/RSI_Martingale_Web_Control_Trailing.ex5`

Pasang pada chart M1 dan isi:

```text
ApiBaseUrl            = https://ea.agoy507.my.id/
EaId                   = ea-id-yang-terdaftar
ApiToken               = token-khusus-ea-id
ApiPollSeconds         = 0.5
ApiStaleTimeoutSeconds = 10.0
```

Di MT5 buka **Tools → Options → Expert Advisors**, aktifkan **Allow WebRequest for listed URL**, lalu tambahkan:

```text
https://ea.agoy507.my.id
```

Alamat server sama untuk semua user. Kombinasi EA ID dan token yang membedakan koneksi. Satu EA ID hanya boleh dipakai satu instance MT5 aktif; instance kedua menerima HTTP 409. Setelah memindahkan EA ke terminal lain, tunggu sekitar delapan detik atau rotasi token.

MT5 dan tombol **Algo Trading** harus tetap aktif agar EA berjalan. Browser WebUI tidak harus terbuka; jadwal dan kontrol disimpan di server.

## Telemetry dan refresh

- Harga, RSI live, posisi, saldo, dan floating dikirim sesuai `ApiPollSeconds` (default 500 ms).
- Browser menerima pembaruan melalui koneksi SSE; polling HTTP 500 ms otomatis menjadi cadangan bila tunnel/proxy memutus SSE.
- Profit hari ini adalah profit trading seluruh akun, termasuk transaksi manual dan EA lain, tanpa deposit/withdrawal. Nilai ini dihitung ulang sekitar setiap 5 detik.
- Mata uang saldo, floating, profit, dan kalkulator mengikuti mata uang akun MT5 aktif.
- Baris admin dan dashboard selalu dibaca berdasarkan EA ID yang tepat; heartbeat satu EA tidak menandai EA lain sebagai online.

## Stop Behavior

- **Pause**: mencegah siklus baru; siklus aktif tetap dilindungi dan dikelola.
- **Hapus pending**: menghapus pending milik EA, mencegah siklus baru, dan tetap mengelola siklus aktif.
- **Tutup semua posisi EA**: menghapus pending serta menutup seluruh posisi dengan Symbol + MagicNumber milik EA.

STOP dapat dikirim berulang. Bila Close All gagal pada sebagian posisi, EA terus mencoba dan tidak menambah layer baru.

## Upgrade aman dari V2.5.4

1. Backup `/DATA/AppData/rsi-martingale-webui/data/state.json` atau folder data Windows.
2. Hentikan container/server lama.
3. Ganti hanya folder source dengan isi V2.5.4.2; jangan hapus atau timpa folder data persisten.
4. Rebuild dan hidupkan server.
5. Pastikan health check menunjukkan V2.5.4.2.
6. Login dan pastikan semua akun, EA ID, serta token hint lama tetap ada.
7. Tetapkan masa berlaku user lama yang ditandai **Masa berlaku belum diatur**. User lama tetap aktif selama langkah ini.
8. Lakukan hard refresh browser.
9. EA V2.5.3 dan V2.5.4 tetap kompatibel sebagai Regular. Untuk Trailing, pilih jenis Trailing di Kelola User lalu gunakan MQ5/EX5 Trailing V2.5.4.2 dengan EA ID/token yang sama.

## Keamanan

- Jangan membagikan password atau token melalui kanal publik.
- Batasi akses file `data/state.json`; file ini berisi hash kredensial dan secret sesi.
- Gunakan IP/CIDR spesifik bila memungkinkan. Nilai `*` mengizinkan semua alamat sumber.
- Gunakan HTTPS atau VPN untuk akses internet.
- Atur `COOKIE_SECURE: "1"` hanya bila WebUI benar-benar diakses melalui HTTPS.
- Backup state sebelum setiap upgrade.

## Pengujian

```powershell
cd E:\EA\V2.5.4.2
.\.venv\Scripts\python.exe -m pytest -q
```

EA Regular dan Trailing V2.5.4.2 memiliki proteksi retry retcode 10026 untuk entry baru. Versi internal yang tampil pada properti dan telemetry MT5 adalah `2.542`.

> Martingale dapat meningkatkan eksposur dengan cepat. Uji seluruh perubahan pada akun demo sebelum digunakan pada akun riil.

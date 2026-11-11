"""
Sistem Informasi Akuntansi TK Mount Pati (Streamlit + SQLite)
Database: PostgreSQL (mis. Neon/Supabase gratis). Alamat database diisi di
  - Streamlit Cloud: Settings -> Secrets:  DATABASE_URL = "postgresql://..."
  - atau environment variable DATABASE_URL
Jalankan: streamlit run app.py
Login awal -> username: admin | password: admin123 (segera ganti di menu sidebar)

Peran:
  admin -> semua menu (termasuk Kelola Akun)
  guru  -> hanya Perkembangan Anak
  ortu  -> hanya Portal Orang Tua (hanya data anaknya sendiri)
"""
import hashlib
import hmac
import os
import sqlite3  # hanya untuk membaca tk.db lama saat impor
import tempfile
from datetime import date

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extensions import AsIs, register_adapter
import streamlit as st
import streamlit.components.v1 as components

NAMA_TK = "TK Mount Pati"

TARIF = {"Uang Pendaftaran": 500_000, "SPP": 300_000,
         "Seragam": 250_000, "Kegiatan": 100_000}
NILAI = ["BB (Belum Berkembang)", "MB (Mulai Berkembang)",
         "BSH (Berkembang Sesuai Harapan)", "BSB (Berkembang Sangat Baik)"]

# Menu yang boleh dibuka tiap peran
MENU_PERAN = {
    "admin": ["Data Siswa", "Tagihan & Pembayaran", "Penggajian", "Pengeluaran",
              "Perkembangan Anak", "Portal Orang Tua", "Laporan Keuangan", "Kelola Akun"],
    "guru": ["Perkembangan Anak"],
    "ortu": ["Portal Orang Tua"],
}
MENU_PERAN["admin"].append("Impor Data Lama")


# ---------- Helper database (PostgreSQL) ----------
register_adapter(np.int64, AsIs)
register_adapter(np.int32, AsIs)

TABEL_DATA = {  # tabel -> kolom (urutan untuk impor dari tk.db lama)
    "siswa": ["id", "nama", "kelompok", "wali", "no_hp"],
    "tagihan": ["id", "siswa_id", "jenis", "bulan", "jumlah", "tanggal"],
    "pembayaran": ["id", "tagihan_id", "tanggal", "jumlah", "metode"],
    "guru": ["id", "nama", "jabatan", "gaji_pokok", "tunjangan"],
    "gaji": ["id", "guru_id", "bulan", "gaji_pokok", "tunjangan", "potongan", "bersih", "tanggal"],
    "pengeluaran": ["id", "tanggal", "pos", "keterangan", "jumlah"],
    "perkembangan": ["id", "siswa_id", "periode", "motorik", "bahasa", "sosial", "catatan"],
}


def db_url():
    try:
        return st.secrets["DATABASE_URL"]
    except Exception:
        url = os.environ.get("DATABASE_URL")
        if not url:
            st.error("DATABASE_URL belum diisi. Isi di Settings -> Secrets (Streamlit Cloud) "
                     "atau environment variable.")
            st.stop()
        return url


@st.cache_resource
def _koneksi():
    con = psycopg2.connect(db_url(), connect_timeout=15)
    con.autocommit = True
    return con


def _q(sql):
    return sql.replace("?", "%s")


def _eksekusi(sql, params, ambil):
    for percobaan in (1, 2):
        try:
            with _koneksi().cursor() as cur:
                cur.execute(_q(sql), params)
                if ambil:
                    kolom = [d[0] for d in cur.description]
                    return pd.DataFrame(cur.fetchall(), columns=kolom)
                return None
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            _koneksi.clear()  # koneksi putus (mis. database tidur) -> sambung ulang
            if percobaan == 2:
                raise


def read(sql, params=()):
    return _eksekusi(sql, params, True)


def run(sql, params=()):
    _eksekusi(sql, params, False)


def transaksi(daftar):
    """Jalankan beberapa perintah sekaligus; semua berhasil atau semua dibatalkan."""
    con = psycopg2.connect(db_url(), connect_timeout=15)
    try:
        with con:
            with con.cursor() as cur:
                for sql, params in daftar:
                    cur.execute(_q(sql), params)
    finally:
        con.close()


@st.cache_resource
def init_db():
    tabel = [
        "CREATE TABLE IF NOT EXISTS siswa (id SERIAL PRIMARY KEY, nama TEXT, kelompok TEXT, wali TEXT, no_hp TEXT)",
        "CREATE TABLE IF NOT EXISTS tagihan (id SERIAL PRIMARY KEY, siswa_id INTEGER, jenis TEXT, bulan TEXT, jumlah BIGINT, tanggal TEXT)",
        "CREATE TABLE IF NOT EXISTS pembayaran (id SERIAL PRIMARY KEY, tagihan_id INTEGER, tanggal TEXT, jumlah BIGINT, metode TEXT)",
        "CREATE TABLE IF NOT EXISTS guru (id SERIAL PRIMARY KEY, nama TEXT, jabatan TEXT, gaji_pokok BIGINT, tunjangan BIGINT)",
        "CREATE TABLE IF NOT EXISTS gaji (id SERIAL PRIMARY KEY, guru_id INTEGER, bulan TEXT, gaji_pokok BIGINT, tunjangan BIGINT, potongan BIGINT, bersih BIGINT, tanggal TEXT)",
        "CREATE TABLE IF NOT EXISTS pengeluaran (id SERIAL PRIMARY KEY, tanggal TEXT, pos TEXT, keterangan TEXT, jumlah BIGINT)",
        "CREATE TABLE IF NOT EXISTS perkembangan (id SERIAL PRIMARY KEY, siswa_id INTEGER, periode TEXT, motorik TEXT, bahasa TEXT, sosial TEXT, catatan TEXT)",
        "CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT, role TEXT NOT NULL DEFAULT 'admin')",
        # akun orang tua -> anak yang boleh dilihat
        "CREATE TABLE IF NOT EXISTS user_siswa (username TEXT, siswa_id INTEGER, PRIMARY KEY (username, siswa_id))",
    ]
    for t in tabel:
        run(t)
    if read("SELECT COUNT(*) n FROM users")["n"][0] == 0:
        run("INSERT INTO users (username, password, role) VALUES (?,?,?)",
            ("admin", hash_pw("admin123"), "admin"))
    return True


# ---------- Login & hak akses ----------
def hash_pw(pw):
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 100_000)
    return salt.hex() + "$" + h.hex()


def cek_pw(pw, tersimpan):
    salt, h = tersimpan.split("$")
    baru = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100_000)
    return hmac.compare_digest(baru.hex(), h)


def peran():
    return st.session_state.get("role")


def wajib(*boleh):
    """Hentikan halaman bila peran tidak berhak (lapisan pengaman kedua)."""
    if peran() not in boleh:
        st.error("Anda tidak punya akses ke halaman ini.")
        st.stop()


def anak_saya():
    """ID anak yang boleh dilihat akun orang tua yang sedang login."""
    df = read("SELECT siswa_id FROM user_siswa WHERE username=?", (st.session_state["user"],))
    return [int(i) for i in df["siswa_id"]]


def halaman_login():
    _, tengah, _ = st.columns([1, 1.2, 1])
    with tengah:
        st.markdown(f"<h1 style='text-align:center'>🏫 {NAMA_TK}</h1>"
                    "<p style='text-align:center'>Sistem Informasi Akuntansi</p>",
                    unsafe_allow_html=True)
        with st.form("login"):
            user = st.text_input("Username")
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Masuk", width="stretch"):
                df = read("SELECT password, role FROM users WHERE username=?", (user.strip(),))
                if not df.empty and cek_pw(pw, df["password"][0]):
                    st.session_state["user"] = user.strip()
                    st.session_state["role"] = df["role"][0]
                    st.rerun()
                else:
                    st.error("Username atau password salah.")


def ganti_password():
    with st.sidebar.expander("Ganti password"):
        lama = st.text_input("Password lama", type="password", key="pw_lama")
        baru = st.text_input("Password baru (min. 6 karakter)", type="password", key="pw_baru")
        if st.button("Simpan password"):
            u = st.session_state["user"]
            tersimpan = read("SELECT password FROM users WHERE username=?", (u,))["password"][0]
            if not cek_pw(lama, tersimpan):
                st.error("Password lama salah.")
            elif len(baru) < 6:
                st.error("Password baru terlalu pendek.")
            else:
                run("UPDATE users SET password=? WHERE username=?", (hash_pw(baru), u))
                st.success("Password diganti.")


# ---------- Tampilan (background interaktif) ----------
CSS = """
<style>
html, body, .stApp, [data-testid="stHeader"] { background: transparent !important; }
body {
  background: linear-gradient(-45deg, #ffe3ec, #e3f2ff, #e6ffe9, #fff4d6) fixed !important;
  background-size: 400% 400% !important;
  animation: geser 18s ease infinite;
}
@keyframes geser { 0%{background-position:0% 50%} 50%{background-position:100% 50%} 100%{background-position:0% 50%} }
.block-container {
  background: rgba(255,255,255,0.88); color: #1f2937;
  border-radius: 18px; padding: 2rem 2.5rem !important; margin-top: 1rem;
  box-shadow: 0 8px 30px rgba(0,0,0,0.08);
}
.block-container h1, .block-container h2, .block-container h3,
.block-container p, .block-container label { color: #1f2937 !important; }
[data-testid="stSidebar"] { background: rgba(255,255,255,0.75) !important; backdrop-filter: blur(6px); }
</style>
"""

# Gelembung warna yang melayang & menghindari kursor; klik untuk memunculkan gelembung baru
JS = """
<script>
(function () {
  const d = window.parent.document;
  if (d.getElementById('bg-canvas')) return;
  const c = d.createElement('canvas');
  c.id = 'bg-canvas';
  c.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;z-index:-1;pointer-events:none';
  d.body.appendChild(c);
  const x = c.getContext('2d');
  const W = () => (c.width = window.parent.innerWidth);
  const H = () => (c.height = window.parent.innerHeight);
  W(); H();
  window.parent.addEventListener('resize', () => { W(); H(); });
  const warna = ['255,138,174', '110,180,255', '120,220,150', '255,200,90', '190,150,255'];
  const buat = (px, py) => ({
    x: px ?? Math.random() * c.width, y: py ?? Math.random() * c.height,
    r: 14 + Math.random() * 38, vx: (Math.random() - .5) * .6, vy: (Math.random() - .5) * .6,
    w: warna[Math.floor(Math.random() * warna.length)]
  });
  const b = Array.from({ length: 28 }, () => buat());
  let m = { x: -999, y: -999 };
  d.addEventListener('mousemove', e => { m.x = e.clientX; m.y = e.clientY; });
  d.addEventListener('click', e => {
    for (let i = 0; i < 4; i++) b.push(buat(e.clientX, e.clientY));
    while (b.length > 60) b.shift();
  });
  (function loop() {
    if (!d.getElementById('bg-canvas')) return;
    x.clearRect(0, 0, c.width, c.height);
    for (const o of b) {
      const dx = o.x - m.x, dy = o.y - m.y, j = Math.hypot(dx, dy);
      if (j < 160) { o.vx += dx / j * .25; o.vy += dy / j * .25; }
      o.vx *= .985; o.vy *= .985;
      o.x += o.vx; o.y += o.vy - .15;
      if (o.x < -o.r) o.x = c.width + o.r; if (o.x > c.width + o.r) o.x = -o.r;
      if (o.y < -o.r) o.y = c.height + o.r;
      const g = x.createRadialGradient(o.x - o.r / 3, o.y - o.r / 3, 2, o.x, o.y, o.r);
      g.addColorStop(0, 'rgba(255,255,255,.75)');
      g.addColorStop(1, 'rgba(' + o.w + ',.35)');
      x.fillStyle = g; x.beginPath(); x.arc(o.x, o.y, o.r, 0, 7); x.fill();
    }
    requestAnimationFrame(loop);
  })();
})();
</script>
"""


def pasang_tampilan():
    st.markdown(CSS, unsafe_allow_html=True)
    components.html(JS, height=0)


# ---------- Helper umum ----------
def rp(x):
    return f"Rp {x:,.0f}".replace(",", ".")


def today():
    return date.today().isoformat()


def pilih_siswa(label="Pilih siswa", key=None):
    df = read("SELECT id, nama, kelompok FROM siswa ORDER BY nama")
    if df.empty:
        st.info("Belum ada data siswa. Isi dulu di menu Data Siswa.")
        return None
    nama = {r.id: f"{r.nama} (Kel. {r.kelompok})" for r in df.itertuples()}
    return st.selectbox(label, df["id"].tolist(), format_func=lambda i: nama[i], key=key)


def tagihan_status():
    return read("""
        SELECT t.id, t.siswa_id, s.nama, t.jenis, t.bulan, t.jumlah,
               COALESCE(SUM(p.jumlah), 0)::bigint AS dibayar,
               t.jumlah - COALESCE(SUM(p.jumlah), 0)::bigint AS sisa
        FROM tagihan t
        JOIN siswa s ON s.id = t.siswa_id
        LEFT JOIN pembayaran p ON p.tagihan_id = t.id
        GROUP BY t.id, s.nama ORDER BY t.id DESC""")


def hapus_siswa(sid):
    """Hapus siswa beserta tagihan, pembayaran, dan perkembangannya (satu transaksi)."""
    transaksi([
        ("DELETE FROM pembayaran WHERE tagihan_id IN (SELECT id FROM tagihan WHERE siswa_id=?)", (sid,)),
        ("DELETE FROM tagihan WHERE siswa_id=?", (sid,)),
        ("DELETE FROM perkembangan WHERE siswa_id=?", (sid,)),
        ("DELETE FROM user_siswa WHERE siswa_id=?", (sid,)),
        ("DELETE FROM siswa WHERE id=?", (sid,)),
    ])


def hapus_guru(gid):
    """Hapus guru beserta riwayat gajinya (satu transaksi)."""
    transaksi([
        ("DELETE FROM gaji WHERE guru_id=?", (gid,)),
        ("DELETE FROM guru WHERE id=?", (gid,)),
    ])


# ---------- Halaman ----------
def hal_siswa():
    wajib("admin")
    st.header("Data Siswa")
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))

    with st.form("f_siswa", clear_on_submit=True):
        nama = st.text_input("Nama anak")
        kelompok = st.selectbox("Kelompok", ["A", "B"])
        wali = st.text_input("Nama orang tua/wali")
        hp = st.text_input("No. HP")
        if st.form_submit_button("Simpan"):
            if nama.strip() and wali.strip():
                run("INSERT INTO siswa (nama, kelompok, wali, no_hp) VALUES (?,?,?,?)",
                    (nama.strip(), kelompok, wali.strip(), hp))
                st.success("Siswa tersimpan.")
            else:
                st.error("Nama anak dan wali wajib diisi.")

    st.dataframe(read("SELECT nama, kelompok, wali, no_hp FROM siswa ORDER BY nama"),
                 width="stretch")

    with st.expander("🗑️ Hapus data siswa"):
        sid = pilih_siswa("Pilih siswa yang akan dihapus", key="hapus_siswa")
        if sid:
            st.warning("Semua tagihan, pembayaran, dan laporan perkembangan anak ini "
                       "ikut terhapus dan tidak bisa dikembalikan.")
            yakin = st.checkbox("Saya yakin ingin menghapus", key="yakin_hapus")
            if st.button("Hapus siswa", disabled=not yakin):
                hapus_siswa(int(sid))
                st.session_state["flash"] = "Data siswa berhasil dihapus."
                st.rerun()


def hal_tagihan():
    wajib("admin")
    st.header("Tagihan & Pembayaran")
    tab1, tab2 = st.tabs(["Buat tagihan", "Catat pembayaran"])

    with tab1:
        sid = pilih_siswa(key="tg_siswa")
        if sid:
            jenis = st.selectbox("Jenis biaya", list(TARIF))
            bulan = st.text_input("Periode (mis. 2026-10)", value=today()[:7])
            st.write(f"Jumlah tagihan: **{rp(TARIF[jenis])}**")
            if st.button("Terbitkan tagihan"):
                run("INSERT INTO tagihan (siswa_id, jenis, bulan, jumlah, tanggal) VALUES (?,?,?,?,?)",
                    (sid, jenis, bulan, TARIF[jenis], today()))
                st.success("Tagihan diterbitkan.")

    with tab2:
        df = tagihan_status()
        belum = df[df["sisa"] > 0]
        if belum.empty:
            st.info("Tidak ada tagihan yang belum lunas.")
        else:
            label = {r.id: f"#{r.id} {r.nama} - {r.jenis} {r.bulan} (sisa {rp(r.sisa)})"
                     for r in belum.itertuples()}
            tid = st.selectbox("Pilih tagihan", belum["id"].tolist(), format_func=lambda i: label[i])
            sisa = int(belum.loc[belum["id"] == tid, "sisa"].iloc[0])
            jumlah = st.number_input("Jumlah dibayar", min_value=0, value=sisa, step=1000)
            metode = st.selectbox("Metode", ["Tunai", "Transfer"])
            if st.button("Simpan pembayaran"):
                if jumlah <= 0 or jumlah > sisa:
                    st.error("Jumlah harus > 0 dan tidak boleh melebihi sisa tagihan.")
                else:
                    run("INSERT INTO pembayaran (tagihan_id, tanggal, jumlah, metode) VALUES (?,?,?,?)",
                        (int(tid), today(), int(jumlah), metode))
                    st.success(f"Pembayaran {rp(jumlah)} tercatat.")

    st.subheader("Daftar tagihan")
    st.dataframe(tagihan_status().drop(columns=["siswa_id"]), width="stretch")


def hal_gaji():
    wajib("admin")
    st.header("Penggajian Guru & Staf")
    with st.expander("Tambah guru/staf"):
        with st.form("f_guru", clear_on_submit=True):
            nama = st.text_input("Nama")
            jabatan = st.text_input("Jabatan")
            pokok = st.number_input("Gaji pokok", min_value=0, step=50_000)
            tunj = st.number_input("Tunjangan", min_value=0, step=50_000)
            if st.form_submit_button("Simpan guru") and nama.strip():
                run("INSERT INTO guru (nama, jabatan, gaji_pokok, tunjangan) VALUES (?,?,?,?)",
                    (nama.strip(), jabatan, int(pokok), int(tunj)))
                st.success("Guru tersimpan.")

    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))

    guru = read("SELECT * FROM guru ORDER BY nama")
    if guru.empty:
        st.info("Belum ada data guru.")
        return

    with st.expander("🗑️ Hapus data guru/staf"):
        nama_guru = {r.id: f"{r.nama} ({r.jabatan})" for r in guru.itertuples()}
        hid = st.selectbox("Pilih guru yang akan dihapus", guru["id"].tolist(),
                           format_func=lambda i: nama_guru[i], key="hapus_guru")
        st.warning("Riwayat gaji guru ini ikut terhapus dan tidak bisa dikembalikan. "
                   "Laporan keuangan akan berubah.")
        yakin = st.checkbox("Saya yakin ingin menghapus", key="yakin_hapus_guru")
        if st.button("Hapus guru", disabled=not yakin):
            hapus_guru(int(hid))
            st.session_state["flash"] = "Data guru berhasil dihapus."
            st.rerun()

    info = {r.id: r for r in guru.itertuples()}
    gid = st.selectbox("Pilih guru", guru["id"].tolist(), format_func=lambda i: info[i].nama)
    g = info[gid]
    bulan = st.text_input("Periode gaji", value=today()[:7])
    potongan = st.number_input("Potongan (BPJS/pajak)", min_value=0, step=10_000)
    bersih = g.gaji_pokok + g.tunjangan - potongan
    st.write(f"Gaji pokok {rp(g.gaji_pokok)} + tunjangan {rp(g.tunjangan)} "
             f"- potongan {rp(potongan)} = **{rp(bersih)}**")
    if st.button("Proses gaji"):
        ada = read("SELECT id FROM gaji WHERE guru_id=? AND bulan=?", (int(gid), bulan))
        if not ada.empty:
            st.error("Gaji guru ini untuk periode tersebut sudah diproses.")
        elif bersih < 0:
            st.error("Potongan melebihi gaji.")
        else:
            run("INSERT INTO gaji (guru_id, bulan, gaji_pokok, tunjangan, potongan, bersih, tanggal) VALUES (?,?,?,?,?,?,?)",
                (int(gid), bulan, int(g.gaji_pokok), int(g.tunjangan), int(potongan), int(bersih), today()))
            st.success("Gaji diproses.")

    st.subheader("Riwayat gaji")
    st.dataframe(read("""SELECT g.bulan, u.nama, g.gaji_pokok, g.tunjangan, g.potongan, g.bersih
                         FROM gaji g JOIN guru u ON u.id = g.guru_id ORDER BY g.id DESC"""),
                 width="stretch")


def hal_pengeluaran():
    wajib("admin")
    st.header("Pengeluaran Operasional")
    with st.form("f_keluar", clear_on_submit=True):
        pos = st.selectbox("Pos biaya", ["ATK", "Listrik & Air", "Snack/Makan",
                                         "Alat Bermain", "Kegiatan", "Lainnya"])
        ket = st.text_input("Keterangan")
        jumlah = st.number_input("Jumlah", min_value=0, step=1000)
        if st.form_submit_button("Simpan"):
            if jumlah > 0:
                run("INSERT INTO pengeluaran (tanggal, pos, keterangan, jumlah) VALUES (?,?,?,?)",
                    (today(), pos, ket, int(jumlah)))
                st.success("Pengeluaran tercatat.")
            else:
                st.error("Jumlah harus lebih dari 0.")

    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))

    daftar = read("SELECT id, tanggal, pos, keterangan, jumlah FROM pengeluaran ORDER BY id DESC")
    st.dataframe(daftar[["tanggal", "pos", "keterangan", "jumlah"]], width="stretch")

    if not daftar.empty:
        with st.expander("🗑️ Hapus data pengeluaran"):
            label = {r.id: f"{r.tanggal} | {r.pos} | {r.keterangan} | {rp(r.jumlah)}"
                     for r in daftar.itertuples()}
            hid = st.selectbox("Pilih pengeluaran yang akan dihapus", daftar["id"].tolist(),
                               format_func=lambda i: label[i], key="hapus_keluar")
            st.warning("Data yang dihapus tidak bisa dikembalikan. "
                       "Saldo kas dan laporan keuangan akan berubah.")
            yakin = st.checkbox("Saya yakin ingin menghapus", key="yakin_hapus_keluar")
            if st.button("Hapus pengeluaran", disabled=not yakin):
                run("DELETE FROM pengeluaran WHERE id=?", (int(hid),))
                st.session_state["flash"] = "Data pengeluaran berhasil dihapus."
                st.rerun()


def hal_perkembangan():
    wajib("admin", "guru")
    st.header("Input Perkembangan Anak (Guru)")
    sid = pilih_siswa(key="pk_siswa")
    if sid:
        with st.form("f_pk", clear_on_submit=True):
            periode = st.text_input("Periode (mis. Semester 1 2026)")
            motorik = st.selectbox("Motorik", NILAI)
            bahasa = st.selectbox("Bahasa", NILAI)
            sosial = st.selectbox("Sosial-emosional", NILAI)
            catatan = st.text_area("Catatan guru")
            if st.form_submit_button("Simpan") and periode.strip():
                run("INSERT INTO perkembangan (siswa_id, periode, motorik, bahasa, sosial, catatan) VALUES (?,?,?,?,?,?)",
                    (int(sid), periode, motorik, bahasa, sosial, catatan))
                st.success("Laporan perkembangan tersimpan.")


def hal_portal():
    wajib("admin", "ortu")
    st.header("Portal Orang Tua")

    if peran() == "ortu":
        ids = anak_saya()
        if not ids:
            st.info("Akun Anda belum dihubungkan ke data anak. Hubungi admin sekolah.")
            return
        q = ",".join("?" * len(ids))
        anak = read(f"SELECT id, nama, kelompok FROM siswa WHERE id IN ({q}) ORDER BY nama", tuple(ids))
        if anak.empty:
            st.info("Data anak tidak ditemukan. Hubungi admin sekolah.")
            return
        if len(anak) == 1:
            sid = int(anak["id"][0])
            st.subheader(f"{anak['nama'][0]} (Kel. {anak['kelompok'][0]})")
        else:
            nama = {r.id: f"{r.nama} (Kel. {r.kelompok})" for r in anak.itertuples()}
            sid = st.selectbox("Pilih anak", anak["id"].tolist(), format_func=lambda i: nama[i])
    else:
        st.caption("Pratinjau admin: pilih nama anak untuk melihat tampilan orang tua.")
        sid = pilih_siswa(key="pt_siswa")
    if not sid:
        return

    tg = tagihan_status()
    st.subheader("Tagihan")
    st.dataframe(tg[tg["siswa_id"] == int(sid)][["jenis", "bulan", "jumlah", "dibayar", "sisa"]],
                 width="stretch")
    st.subheader("Riwayat pembayaran")
    st.dataframe(read("""SELECT p.tanggal, t.jenis, t.bulan, p.jumlah, p.metode
                         FROM pembayaran p JOIN tagihan t ON t.id = p.tagihan_id
                         WHERE t.siswa_id=? ORDER BY p.id DESC""", (int(sid),)),
                 width="stretch")
    st.subheader("Laporan perkembangan")
    st.dataframe(read("""SELECT periode, motorik, bahasa, sosial, catatan
                         FROM perkembangan WHERE siswa_id=? ORDER BY id DESC""", (int(sid),)),
                 width="stretch")


def buat_jurnal():
    rows = []
    for r in read("""SELECT t.tanggal, s.nama, t.jenis, t.jumlah FROM tagihan t
                     JOIN siswa s ON s.id = t.siswa_id""").itertuples():
        rows += [(r.tanggal, f"Tagihan {r.jenis} - {r.nama}", "Piutang Siswa", r.jumlah, 0),
                 (r.tanggal, "", f"Pendapatan {r.jenis}", 0, r.jumlah)]
    for r in read("""SELECT p.tanggal, s.nama, t.jenis, p.jumlah FROM pembayaran p
                     JOIN tagihan t ON t.id = p.tagihan_id
                     JOIN siswa s ON s.id = t.siswa_id""").itertuples():
        rows += [(r.tanggal, f"Terima bayar {r.jenis} - {r.nama}", "Kas", r.jumlah, 0),
                 (r.tanggal, "", "Piutang Siswa", 0, r.jumlah)]
    for r in read("""SELECT g.tanggal, u.nama, g.gaji_pokok, g.tunjangan, g.potongan, g.bersih
                     FROM gaji g JOIN guru u ON u.id = g.guru_id""").itertuples():
        rows += [(r.tanggal, f"Gaji {r.nama}", "Beban Gaji", r.gaji_pokok + r.tunjangan, 0),
                 (r.tanggal, "", "Kas", 0, r.bersih),
                 (r.tanggal, "", "Utang Potongan Gaji", 0, r.potongan)]
    for r in read("SELECT tanggal, pos, keterangan, jumlah FROM pengeluaran").itertuples():
        rows += [(r.tanggal, r.keterangan, f"Beban {r.pos}", r.jumlah, 0),
                 (r.tanggal, "", "Kas", 0, r.jumlah)]
    df = pd.DataFrame(rows, columns=["Tanggal", "Keterangan", "Akun", "Debit", "Kredit"])
    return df.sort_values("Tanggal", kind="stable") if not df.empty else df


def hal_laporan():
    wajib("admin")
    st.header(f"Laporan Keuangan {NAMA_TK}")
    pendapatan = read("SELECT COALESCE(SUM(jumlah),0)::bigint v FROM tagihan")["v"][0]
    kas_masuk = read("SELECT COALESCE(SUM(jumlah),0)::bigint v FROM pembayaran")["v"][0]
    beban_gaji = read("SELECT COALESCE(SUM(gaji_pokok+tunjangan),0)::bigint v FROM gaji")["v"][0]
    kas_gaji = read("SELECT COALESCE(SUM(bersih),0)::bigint v FROM gaji")["v"][0]
    beban_ops = read("SELECT COALESCE(SUM(jumlah),0)::bigint v FROM pengeluaran")["v"][0]
    piutang = pendapatan - kas_masuk
    kas = kas_masuk - kas_gaji - beban_ops
    surplus = pendapatan - beban_gaji - beban_ops

    c1, c2 = st.columns(2)
    c1.metric("Saldo kas", rp(kas))
    c2.metric("Piutang SPP & biaya", rp(piutang))
    c1.metric("Total pendapatan", rp(pendapatan))
    c2.metric("Surplus/(defisit)", rp(surplus))

    st.subheader("Laporan surplus/defisit")
    lap = pd.DataFrame({"Pos": ["Pendapatan", "Beban gaji", "Beban operasional", "Surplus/(defisit)"],
                        "Jumlah": [rp(pendapatan), rp(beban_gaji), rp(beban_ops), rp(surplus)]})
    st.table(lap)

    st.subheader("Pendapatan per jenis biaya")
    per_jenis = read("SELECT jenis, SUM(jumlah)::bigint AS total FROM tagihan GROUP BY jenis")
    if not per_jenis.empty:
        st.bar_chart(per_jenis.set_index("jenis"))

    st.subheader("Jurnal umum (otomatis)")
    st.dataframe(buat_jurnal(), width="stretch")


def hal_akun():
    """Admin membuat akun guru/orang tua dan menghubungkan orang tua ke anaknya."""
    wajib("admin")
    st.header("Kelola Akun")
    if "flash" in st.session_state:
        st.success(st.session_state.pop("flash"))

    st.subheader("Buat akun baru")
    with st.form("f_akun", clear_on_submit=True):
        user = st.text_input("Username")
        pw = st.text_input("Password awal (min. 6 karakter)", type="password")
        role = st.selectbox("Peran", ["ortu", "guru", "admin"],
                            format_func=lambda r: {"ortu": "Orang tua", "guru": "Guru", "admin": "Admin"}[r])
        anak = read("SELECT id, nama, kelompok, wali FROM siswa ORDER BY nama")
        label = {r.id: f"{r.nama} (Kel. {r.kelompok}, wali: {r.wali})" for r in anak.itertuples()}
        pilih = st.multiselect("Anak yang boleh dilihat (khusus orang tua)",
                               anak["id"].tolist(), format_func=lambda i: label[i])
        if st.form_submit_button("Buat akun"):
            user = user.strip()
            if not user or len(pw) < 6:
                st.error("Username wajib diisi dan password minimal 6 karakter.")
            elif not read("SELECT 1 FROM users WHERE username=?", (user,)).empty:
                st.error("Username sudah dipakai.")
            elif role == "ortu" and not pilih:
                st.error("Akun orang tua harus dihubungkan ke minimal satu anak.")
            else:
                run("INSERT INTO users (username, password, role) VALUES (?,?,?)",
                    (user, hash_pw(pw), role))
                if role == "ortu":
                    for sid in pilih:
                        run("INSERT INTO user_siswa VALUES (?,?) ON CONFLICT DO NOTHING", (user, int(sid)))
                st.session_state["flash"] = f"Akun {user} dibuat."
                st.rerun()

    st.subheader("Daftar akun")
    st.dataframe(read("""SELECT u.username, u.role AS peran,
                                COALESCE(STRING_AGG(s.nama, ', '), '') AS anak
                         FROM users u
                         LEFT JOIN user_siswa us ON us.username = u.username
                         LEFT JOIN siswa s ON s.id = us.siswa_id
                         GROUP BY u.username ORDER BY u.role, u.username"""),
                 width="stretch")

    akun = read("SELECT username FROM users ORDER BY username")["username"].tolist()
    with st.expander("🔑 Reset password akun"):
        target = st.selectbox("Pilih akun", akun, key="reset_user")
        baru = st.text_input("Password baru (min. 6 karakter)", type="password", key="reset_pw")
        if st.button("Reset password"):
            if len(baru) < 6:
                st.error("Password terlalu pendek.")
            else:
                run("UPDATE users SET password=? WHERE username=?", (hash_pw(baru), target))
                st.success(f"Password {target} diganti.")

    with st.expander("🗑️ Hapus akun"):
        lain = [a for a in akun if a != st.session_state["user"]]
        if not lain:
            st.info("Tidak ada akun lain.")
        else:
            target = st.selectbox("Pilih akun yang akan dihapus", lain, key="hapus_akun")
            yakin = st.checkbox("Saya yakin ingin menghapus akun ini", key="yakin_hapus_akun")
            if st.button("Hapus akun", disabled=not yakin):
                run("DELETE FROM user_siswa WHERE username=?", (target,))
                run("DELETE FROM users WHERE username=?", (target,))
                st.session_state["flash"] = f"Akun {target} dihapus."
                st.rerun()


def hal_impor():
    """Salin data dari tk.db (SQLite) lama ke database online. Sekali saja."""
    wajib("admin")
    st.header("Impor Data Lama")
    st.write("Pindahkan data dari file **tk.db** lama ke database online. "
             "Akun login tidak ikut dipindah (buat ulang di menu Kelola Akun).")
    ada = sum(int(read(f"SELECT COUNT(*) n FROM {t}")["n"][0]) for t in TABEL_DATA)
    if ada:
        st.warning("Database online sudah berisi data, impor dinonaktifkan agar data tidak dobel.")
        return
    berkas = st.file_uploader("Pilih file tk.db", type=["db"])
    if berkas and st.button("Impor sekarang"):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            tmp.write(berkas.getvalue()); tmp.flush()
            lite = sqlite3.connect(tmp.name)
            try:
                perintah, jumlah = [], {}
                for tabel, kolom in TABEL_DATA.items():
                    daftar = ", ".join(kolom)
                    rows = lite.execute(f"SELECT {daftar} FROM {tabel}").fetchall()
                    jumlah[tabel] = len(rows)
                    isi = ", ".join(["?"] * len(kolom))
                    for r in rows:
                        perintah.append((f"INSERT INTO {tabel} ({daftar}) VALUES ({isi})", r))
                for tabel in TABEL_DATA:  # sinkronkan penomoran otomatis
                    perintah.append((f"SELECT setval(pg_get_serial_sequence('{tabel}','id'), "
                                     f"GREATEST((SELECT COALESCE(MAX(id),1) FROM {tabel}),1))", ()))
                transaksi(perintah)
            except Exception as e:
                st.error(f"Impor gagal, tidak ada data yang masuk: {e}")
                return
            finally:
                lite.close()
        st.success("Impor selesai: " + ", ".join(f"{t} {n}" for t, n in jumlah.items()))


# ---------- Main ----------
def main():
    st.set_page_config(page_title=f"SIA {NAMA_TK}", page_icon="🏫", layout="wide")
    init_db()
    pasang_tampilan()

    if "user" not in st.session_state:
        halaman_login()
        return

    # Ambil peran terbaru dari database (jika akun dihapus/diubah, sesi ikut tidak berlaku)
    df = read("SELECT role FROM users WHERE username=?", (st.session_state["user"],))
    if df.empty:
        st.session_state.clear()
        st.rerun()
    st.session_state["role"] = df["role"][0]

    semua = {
        "Data Siswa": hal_siswa,
        "Tagihan & Pembayaran": hal_tagihan,
        "Penggajian": hal_gaji,
        "Pengeluaran": hal_pengeluaran,
        "Perkembangan Anak": hal_perkembangan,
        "Portal Orang Tua": hal_portal,
        "Laporan Keuangan": hal_laporan,
        "Kelola Akun": hal_akun,
        "Impor Data Lama": hal_impor,
    }
    menu = {nama: semua[nama] for nama in MENU_PERAN.get(peran(), [])}

    st.sidebar.title(f"🏫 {NAMA_TK}")
    st.sidebar.caption(f"Masuk sebagai: {st.session_state['user']} ({peran()})")
    pilihan = st.sidebar.radio("Menu", list(menu))
    ganti_password()
    if st.sidebar.button("Keluar"):
        st.session_state.clear()
        st.rerun()
    menu[pilihan]()


main()

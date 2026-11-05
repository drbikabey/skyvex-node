import os
import sys
import time
import json
import hashlib
import socket
import subprocess
import threading
import logging
import re
import platform
import shutil
import urllib.request
import zipfile
import tarfile
import secrets
import queue
from datetime import datetime

import psutil
import requests
from flask import Flask, jsonify, request

# ANSI renk/escape kodları konsolda görünmesin
ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# ====================== YAPILANDIRMA ======================
def env_str(name, default=""):
    """Boş/None env değerlerini yok say (CI secret'ları boş gelebilir)."""
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def env_int(name, default):
    v = os.environ.get(name)
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Master Panel (SkyVex)
MASTER_PANEL_URL = env_str("MASTER_PANEL_URL", "http://skyvex.duckdns.org:15576").rstrip("/")
HEARTBEAT_ENDPOINT = "/api/node_report"
NODE_SECRET = env_str("NODE_SECRET", "skyvex_secret_123")
HEARTBEAT_INTERVAL = env_int("HEARTBEAT_INTERVAL", 30)
NODE_NAME = env_str("NODE_NAME", f"SkyVex-Node-{secrets.token_hex(3)}")

# Node HTTP API portu (master /exec ile buraya bağlanır)
NODE_PORT = env_int("PORT", env_int("NODE_PORT", 7860))

# Node'un dışarıdan erişilebilir taban URL'si (örn: https://node.onrender.com veya http://IP:7860)
PUBLIC_URL = env_str("PUBLIC_URL", "").rstrip("/")

# Tünel modu:
#   fixed  -> sabit IP/port (VPS için önerilir, tünel yok)
#   bore   -> kullanıcı sunucuları için bore.pub tüneli (public IP gerekmez)
#   playit -> node API'si için playit.gg tüneli (deneysel)
#   auto   -> kullanıcılar için önce bore, olmazsa fixed
TUNNEL_MODE = env_str("TUNNEL_MODE", "auto").lower()

# Playit.gg (yalnızca node API tüneli için, opsiyonel)
PLAYIT_TOKEN = env_str("PLAYIT_TOKEN", "")
_PLAYIT_VERSION = env_str("PLAYIT_VERSION", "v1.0.10")
_PLAYIT_ARCH = "playit-linux-amd64"
if platform.system() == "Windows":
    _PLAYIT_ARCH = "playit-windows-x86_64.exe"
elif platform.machine().lower() in ("aarch64", "arm64"):
    _PLAYIT_ARCH = "playit-linux-aarch64"
PLAYIT_DOWNLOAD_URL = env_str(
    "PLAYIT_DOWNLOAD_URL",
    f"https://github.com/playit-cloud/playit-agent/releases/download/{_PLAYIT_VERSION}/{_PLAYIT_ARCH}",
)
BIN_DIR = os.path.join(BASE_DIR, "bin")
PLAYIT_BIN = os.path.join(BIN_DIR, "playit.exe" if platform.system() == "Windows" else "playit")
BORE_BIN = os.path.join(BIN_DIR, "bore.exe" if platform.system() == "Windows" else "bore")

# Sabit IP / Port
FIXED_IP = env_str("FIXED_IP", "")
FIXED_PORT = env_int("FIXED_PORT", 25565)

# Minecraft
SERVER_JAR_NAME = env_str("SERVER_JAR", "server.jar")
SERVER_RAM = env_str("SERVER_RAM", "2G")
JAVA_PATH = env_str("JAVA_PATH", "java")
START_MC_SERVER = env_str("START_MC_SERVER", "false").lower() == "true"

# Dizinler
DATA_DIR = os.path.join(BASE_DIR, "data")
FILES_DIR = os.path.join(BASE_DIR, "files")
LOG_DIR = os.path.join(BASE_DIR, "logs")
for _d in (DATA_DIR, FILES_DIR, LOG_DIR, BIN_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception:
        pass

LOG_FILE = os.path.join(LOG_DIR, "sistem.log")

# Node ID (NODE_NAME'den türetilir; job yenilenince değişmez)
NODE_ID_FILE = os.path.join(DATA_DIR, ".node_id")
NODE_ID = hashlib.md5(NODE_NAME.encode("utf-8")).hexdigest()[:8]
try:
    with open(NODE_ID_FILE, "w") as f:
        f.write(NODE_ID)
except Exception:
    pass


# ====================== LOG ======================
# Windows konsolunda emoji/UTF-8 hatasını önle
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("sistem")


# ====================== GLOBAL DURUM ======================
app = Flask(__name__)
user_processes = {}        # username -> Popen
user_tunnels = {}          # username -> Popen (bore)
user_tunnel_urls = {}      # username -> "host:port"
user_ports = {}            # username -> port (heartbeat adresi için)
user_console_queues = {}   # username -> queue.Queue
user_console_histories = {}  # username -> list[str]

playit_proc = None
playit_api_address = None
api_tunnel_proc = None
api_tunnel_address = None
state_lock = threading.Lock()
running = True


# ====================== SİSTEM BİLGİSİ ======================
def get_public_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=5).text.strip()
    except Exception:
        try:
            return requests.get("https://ifconfig.me/ip", timeout=5).text.strip()
        except Exception:
            return socket.gethostbyname(socket.gethostname())


def get_system_info():
    ram = psutil.virtual_memory()
    cpu_cores = psutil.cpu_count(logical=True) or 1
    try:
        disk = psutil.disk_usage(BASE_DIR)
    except Exception:
        disk = psutil.disk_usage("/")
    return {
        "ip": get_public_ip(),
        "ram_total_mb": int(ram.total / (1024 * 1024)),
        "ram_available_mb": int(ram.available / (1024 * 1024)),
        "ram_used_mb": int(ram.used / (1024 * 1024)),
        "cpu_cores": cpu_cores,
        "cpu_usage_percent": round(psutil.cpu_percent(interval=1), 1),
        "disk_total_gb": int(disk.total / (1024 ** 3)),
        "disk_free_gb": int(disk.free / (1024 ** 3)),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
    }


def get_node_public_url():
    """Master'ın /exec için kullanacağı taban URL."""
    if PUBLIC_URL:
        return PUBLIC_URL
    with state_lock:
        api_addr = api_tunnel_address or playit_api_address
    if api_addr:
        return f"http://{api_addr}"
    return f"http://{get_public_ip()}:{NODE_PORT}"


# ====================== JAVA ======================
def _java_major_version(java_bin):
    try:
        out = subprocess.check_output([java_bin, "-version"], stderr=subprocess.STDOUT, timeout=10)
        text = out.decode("utf-8", errors="replace")
        m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
        if m:
            major = int(m.group(1))
            if major == 1 and m.group(2):
                return int(m.group(2))
            return major
    except Exception:
        pass
    return None


def auto_setup_java(preferred_version=21):
    """Sistemde uygun Java yoksa Temurin JRE indir."""
    sys_java = shutil.which("java") or shutil.which("java.exe")
    if sys_java:
        major = _java_major_version(sys_java)
        if major and major >= preferred_version:
            log.info(f"Sistem Java {major} bulundu: {sys_java}")
            return sys_java
        log.warning(f"Sistem Java sürümü düşük ({major}), Java {preferred_version} indirilecek.")

    for p in ("/usr/bin/java", "/usr/local/bin/java", "/opt/java/bin/java"):
        if os.path.exists(p):
            major = _java_major_version(p)
            if major and major >= preferred_version:
                return p

    jre_dir = os.path.join(FILES_DIR, "jre")
    version_file = os.path.join(jre_dir, "version.txt")
    java_bin = os.path.join(jre_dir, "bin", "java")
    if os.name == "nt":
        java_bin += ".exe"

    if os.path.exists(java_bin) and os.path.exists(version_file):
        try:
            with open(version_file, "r") as f:
                if f.read().strip() == str(preferred_version):
                    return java_bin
        except Exception:
            pass

    try:
        os.makedirs(jre_dir, exist_ok=True)
        if preferred_version == 21:
            if os.name == "nt":
                url = "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.1%2B12/OpenJDK21U-jre_x64_windows_hotspot_21.0.1_12.zip"
                archive_name = "jre21.zip"
            else:
                url = "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.1%2B12/OpenJDK21U-jre_x64_linux_hotspot_21.0.1_12.tar.gz"
                archive_name = "jre21.tar.gz"
        else:
            if os.name == "nt":
                url = "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.9%2B9/OpenJDK17U-jre_x64_windows_hotspot_17.0.9_9.zip"
                archive_name = "jre17.zip"
            else:
                url = "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.9%2B9/OpenJDK17U-jre_x64_linux_hotspot_17.0.9_9.tar.gz"
                archive_name = "jre17.tar.gz"

        archive_path = os.path.join(DATA_DIR, archive_name)
        log.info(f"Java {preferred_version} indiriliyor: {url}")
        urllib.request.urlretrieve(url, archive_path)

        if archive_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(jre_dir)
        else:
            with tarfile.open(archive_path, "r:gz") as tf:
                tf.extractall(jre_dir)

        # İç içe klasörü düzelt
        extracted = [d for d in os.listdir(jre_dir) if os.path.isdir(os.path.join(jre_dir, d))]
        if extracted:
            inner = os.path.join(jre_dir, extracted[0])
            for item in os.listdir(inner):
                shutil.move(os.path.join(inner, item), jre_dir)
            try:
                os.rmdir(inner)
            except Exception:
                pass

        with open(version_file, "w") as f:
            f.write(str(preferred_version))
        if os.name != "nt":
            os.chmod(java_bin, 0o755)
        try:
            os.remove(archive_path)
        except Exception:
            pass
        log.info(f"Java {preferred_version} kuruldu: {java_bin}")
        return java_bin
    except Exception as e:
        log.error(f"Java kurulum hatası: {e}")
        return None


# ====================== TÜNELLER ======================
def download_bore_if_needed():
    if os.path.exists(BORE_BIN):
        if os.name != "nt":
            try:
                os.chmod(BORE_BIN, 0o755)
            except Exception:
                pass
        return True
    try:
        log.info("Bore tünel agent indiriliyor...")
        url = "https://github.com/ekzhang/bore/releases/download/v0.5.1/bore-v0.5.1-x86_64-unknown-linux-musl.tar.gz"
        if os.name == "nt":
            url = "https://github.com/ekzhang/bore/releases/download/v0.5.1/bore-v0.5.1-x86_64-pc-windows-msvc.zip"
        archive_path = os.path.join(DATA_DIR, "bore_arch")
        urllib.request.urlretrieve(url, archive_path)
        if url.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(BIN_DIR)
        else:
            with tarfile.open(archive_path, "r:gz") as tf:
                tf.extractall(BIN_DIR)
        if os.name != "nt":
            os.chmod(BORE_BIN, 0o755)
        try:
            os.remove(archive_path)
        except Exception:
            pass
        return True
    except Exception as e:
        log.error(f"Bore indirme hatası: {e}")
        return False


def start_user_tunnel(username, port):
    """Kullanıcı sunucusu için bore tüneli başlat (public IP yoksa)."""
    if username in user_tunnels:
        try:
            user_tunnels[username].terminate()
        except Exception:
            pass
        user_tunnels.pop(username, None)

    # Eski tünel adresi geçersiz; yeni adres okunacak
    user_tunnel_urls.pop(username, None)

    if not download_bore_if_needed():
        return False

    try:
        log_path = os.path.join(DATA_DIR, f"bore_{username}.log")
        try:
            os.remove(log_path)
        except Exception:
            pass
        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(
                [BORE_BIN, "local", str(port), "--to", "bore.pub"],
                stdout=log_file,
                stderr=log_file,
            )
        user_tunnels[username] = proc
        log.info(f"Bore tüneli başlatıldı: {username} -> port {port}")
        return True
    except Exception as e:
        log.error(f"Bore tünel hatası ({username}): {e}")
        return False


def get_user_tunnel_address(username):
    log_path = os.path.join(DATA_DIR, f"bore_{username}.log")
    for _ in range(20):
        if os.path.exists(log_path):
            try:
                with open(log_path, "r") as f:
                    content = f.read()
                m = re.search(r"bore\.pub:[0-9]+", content)
                if m:
                    return m.group(0)
            except Exception:
                pass
        time.sleep(0.5)
    return None


def resolve_user_address(username, port):
    """Kullanıcının bağlanacağı adres: tünel varsa onu, yoksa sabit IP:port."""
    if TUNNEL_MODE in ("bore", "auto"):
        addr = user_tunnel_urls.get(username) or get_user_tunnel_address(username)
        if addr:
            user_tunnel_urls[username] = addr
            return addr
    if FIXED_IP:
        return f"{FIXED_IP}:{port}"
    return f"{get_public_ip()}:{port}"


def download_playit():
    if os.path.exists(PLAYIT_BIN):
        if os.name != "nt":
            try:
                os.chmod(PLAYIT_BIN, 0o755)
            except Exception:
                pass
        return True
    if not PLAYIT_DOWNLOAD_URL:
        log.error("PLAYIT_DOWNLOAD_URL boş, Playit kurulmayacak.")
        return False
    dl = os.path.join(BIN_DIR, "playit_download")
    try:
        log.info(f"Playit.gg indiriliyor: {PLAYIT_DOWNLOAD_URL}")
        urllib.request.urlretrieve(PLAYIT_DOWNLOAD_URL, dl)
        is_archive = PLAYIT_DOWNLOAD_URL.lower().endswith((".zip", ".tgz", ".tar.gz"))
        if not is_archive:
            with open(dl, "rb") as f:
                head = f.read(2)
            is_archive = head in (b"PK", b"\x1f\x8b")
        if is_archive:
            if PLAYIT_DOWNLOAD_URL.lower().endswith(".zip"):
                with zipfile.ZipFile(dl, "r") as zf:
                    zf.extractall(BIN_DIR)
            else:
                with tarfile.open(dl, "r:gz") as tf:
                    tf.extractall(BIN_DIR)
            cands = [os.path.join(BIN_DIR, n) for n in os.listdir(BIN_DIR) if n.startswith("playit") and not n.endswith((".zip", ".tgz"))]
            if cands and PLAYIT_BIN not in cands:
                shutil.move(cands[0], PLAYIT_BIN)
        else:
            if os.path.exists(PLAYIT_BIN):
                os.remove(PLAYIT_BIN)
            shutil.move(dl, PLAYIT_BIN)
        if os.path.exists(dl):
            try:
                os.remove(dl)
            except Exception:
                pass
        if os.name != "nt":
            os.chmod(PLAYIT_BIN, 0o755)
        return True
    except Exception as e:
        log.error(f"Playit indirme hatası: {e}")
        return False


def stream_playit_output(proc):
    global playit_api_address
    buf = ""
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            buf += line + "\n"
            m = re.search(r"([a-zA-Z0-9-]+\.playit\.gg:\d+)", buf) or re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d+)", buf)
            if m:
                with state_lock:
                    playit_api_address = m.group(1)
                log.info(f"🌐 Playit API adresi: {m.group(1)}")
    except Exception as e:
        log.error(f"Playit çıktı hatası: {e}")


def start_playit_api_tunnel():
    global playit_proc
    if not download_playit():
        return False
    env = os.environ.copy()
    if PLAYIT_TOKEN:
        env["PLAYIT_TOKEN"] = PLAYIT_TOKEN
    try:
        playit_proc = subprocess.Popen(
            [PLAYIT_BIN],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            env=env,
            cwd=BIN_DIR,
        )
        threading.Thread(target=stream_playit_output, args=(playit_proc,), daemon=True).start()
        return True
    except Exception as e:
        log.error(f"Playit başlatma hatası: {e}")
        return False


def _parse_bore_log(log_path, attempts=30):
    """bore logundan 'bore.pub:port' adresini oku."""
    for _ in range(attempts):
        if os.path.exists(log_path):
            try:
                with open(log_path, "r") as f:
                    content = f.read()
                m = re.search(r"bore\.pub:[0-9]+", content)
                if m:
                    return m.group(0)
            except Exception:
                pass
        time.sleep(0.5)
    return None


def start_api_tunnel():
    """Node HTTP API'sini bore ile dışarı aç (GitHub Actions / NAT için).

    Böylece master panel, node'un private IP'sine değil,
    http://bore.pub:<port> adresine /exec atabilir.
    """
    global api_tunnel_proc, api_tunnel_address
    if PUBLIC_URL:
        return True
    if not download_bore_if_needed():
        log.warning("Bore indirilemedi; API tüneli açılamadı.")
        return False
    try:
        log_path = os.path.join(DATA_DIR, "bore_api.log")
        try:
            os.remove(log_path)
        except Exception:
            pass
        with open(log_path, "w") as lf:
            proc = subprocess.Popen(
                [BORE_BIN, "local", str(NODE_PORT), "--to", "bore.pub"],
                stdout=lf,
                stderr=lf,
            )
        api_tunnel_proc = proc
        addr = _parse_bore_log(log_path)
        if addr:
            with state_lock:
                api_tunnel_address = addr
            log.info(f"🌐 Node API tüneli hazır: http://{addr}")
            return True
        log.warning("API tünel adresi alınamadı; sabit IP kullanılacak.")
        return False
    except Exception as e:
        log.error(f"API tüneli başlatma hatası: {e}")
        return False


# ====================== SUNUCU YÖNETİMİ ======================
def update_server_properties(server_dir, port):
    prop_path = os.path.join(server_dir, "server.properties")
    props = {}
    if os.path.exists(prop_path):
        try:
            with open(prop_path, "r") as f:
                for line in f:
                    if "=" in line and not line.startswith("#"):
                        k, v = line.strip().split("=", 1)
                        props[k] = v
        except Exception:
            pass
    props["server-port"] = str(port)
    props["query.port"] = str(port)
    try:
        with open(prop_path, "w") as f:
            for k, v in props.items():
                f.write(f"{k}={v}\n")
    except Exception as e:
        log.error(f"server.properties yazılamadı: {e}")


def enqueue_output(stream, q, username):
    try:
        for line in iter(stream.readline, b""):
            text = line.decode("utf-8", errors="replace")
            # Renk kodlarını ve satır başı (CR) karakterlerini temizle
            text = ANSI_ESCAPE_RE.sub("", text).replace("\r\n", "\n").replace("\r", "")
            q.put(text)
            user_console_histories.setdefault(username, []).append(text)
            if len(user_console_histories[username]) > 500:
                user_console_histories[username].pop(0)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _sync_server_files(server_dir, username, data):
    """Master'dan sunucu dosyalarını indir."""
    sync_url = data.get("sync_url")
    fallback = data.get("sync_fallback_urls", [])
    urls = ([sync_url] if sync_url else []) + list(fallback)
    if not urls:
        log.warning(f"sync_url yok, mevcut dosyalarla devam: {username}")
        return True

    for url in urls:
        try:
            log.info(f"Dosya sync: {username} <- {url}")
            zip_path = os.path.join(DATA_DIR, f"{username}_sync.zip")
            resp = requests.get(url, timeout=300, stream=True, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                raise Exception(f"HTTP {resp.status_code}")
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            with zipfile.ZipFile(zip_path, "r") as zf:
                base = os.path.realpath(server_dir)
                safe_members = []
                for member in zf.namelist():
                    target = os.path.realpath(os.path.join(server_dir, member))
                    if not (target == base or target.startswith(base + os.sep)):
                        log.warning(f"ZIP güvenlik ihlali atlandı: {member}")
                        continue
                    safe_members.append(member)
                zf.extractall(server_dir, members=safe_members)
            try:
                os.remove(zip_path)
            except Exception:
                pass
            log.info(f"Dosya sync tamam: {username}")
            return True
        except Exception as e:
            log.warning(f"Sync URL başarısız ({url}): {e}")
    return False


def start_user_server(username, data):
    if user_processes.get(username) and user_processes[username].poll() is None:
        return {"success": True, "message": "Sunucu zaten çalışıyor"}

    java_path = auto_setup_java(21)
    if not java_path:
        return {"success": False, "error": "Java kurulamadı"}, 500

    cmd = data.get("cmd", [])
    port = int(data.get("port", FIXED_PORT))
    server_dir = os.path.join(FILES_DIR, username)
    os.makedirs(server_dir, exist_ok=True)

    if not _sync_server_files(server_dir, username, data):
        return {"success": False, "error": "Dosya senkronizasyonu başarısız"}, 500

    update_server_properties(server_dir, port)

    if TUNNEL_MODE in ("bore", "auto"):
        start_user_tunnel(username, port)

    if cmd and cmd[0].endswith("java"):
        cmd[0] = java_path
    if not cmd:
        cmd = [java_path, f"-Xms{SERVER_RAM}", f"-Xmx{SERVER_RAM}", "-jar", SERVER_JAR_NAME, "nogui"]

    try:
        log.info(f"Sunucu başlatılıyor: {username} | {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            cwd=server_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            bufsize=1,
        )
        user_processes[username] = proc
        user_ports[username] = port
        user_console_queues[username] = queue.Queue()
        user_console_histories[username] = []
        threading.Thread(target=enqueue_output, args=(proc.stdout, user_console_queues[username], username), daemon=True).start()

        address = resolve_user_address(username, port)
        return {"success": True, "message": "Server started", "address": address, "node_id": NODE_ID}
    except Exception as e:
        log.error(f"Sunucu başlatma hatası ({username}): {e}")
        return {"success": False, "error": str(e)}, 500


# ====================== FLASK API ======================
@app.route("/")
def root():
    return jsonify({
        "status": "online",
        "type": "compute_node",
        "node_id": NODE_ID,
        "node_name": NODE_NAME,
        "public_url": get_node_public_url(),
        "info": get_system_info(),
    })


@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/exec", methods=["POST"])
def execute_command():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != NODE_SECRET:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    action = data.get("action")
    username = data.get("username")

    if action == "start":
        result = start_user_server(username, data)
        return result if isinstance(result, tuple) else jsonify(result)

    if action == "stop":
        proc = user_processes.get(username)
        if proc:
            try:
                if proc.poll() is None:
                    proc.stdin.write(b"stop\n")
                    proc.stdin.flush()
                    try:
                        proc.wait(timeout=15)
                    except Exception:
                        proc.kill()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        tun = user_tunnels.pop(username, None)
        if tun:
            try:
                tun.terminate()
            except Exception:
                pass
        user_processes.pop(username, None)
        user_tunnel_urls.pop(username, None)
        user_ports.pop(username, None)
        return jsonify({"success": True, "message": "Server stopped"})

    if action == "get_console":
        return jsonify({"success": True, "history": user_console_histories.get(username, [])})

    if action == "get_stats":
        proc = user_processes.get(username)
        if proc and proc.poll() is None:
            try:
                p = psutil.Process(proc.pid)
                cpu = p.cpu_percent(interval=0.1) / (psutil.cpu_count() or 1)
                return jsonify({
                    "success": True,
                    "online": True,
                    "cpu_percent": round(cpu, 1),
                    "ram_mb": int(p.memory_info().rss / (1024 * 1024)),
                    "threads": p.num_threads(),
                })
            except Exception:
                pass
        return jsonify({"success": True, "online": False})

    if action == "send_command":
        proc = user_processes.get(username)
        if not proc or proc.poll() is not None:
            return jsonify({"success": False, "error": "Sunucu kapalı"}), 400
        command = (data.get("command") or "").strip()
        if not command:
            return jsonify({"success": False, "error": "Komut boş"}), 400
        try:
            proc.stdin.write((command + "\n").encode("utf-8"))
            proc.stdin.flush()
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({"success": False, "error": "Unknown action"}), 400


# ====================== HEARTBEAT ======================
def send_heartbeat():
    failures = 0
    while running:
        try:
            info = get_system_info()
            info.update({
                "secret": NODE_SECRET,
                "node_id": NODE_ID,
                "node_name": NODE_NAME,
                "status": "online",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "tunnel_mode": TUNNEL_MODE,
                "public_url": get_node_public_url(),
                "fixed_ip": FIXED_IP,
                "fixed_port": FIXED_PORT,
                "port": NODE_PORT,
            })

            running_users = []
            tunnels = {}
            for user, proc in list(user_processes.items()):
                try:
                    alive = proc is not None and proc.poll() is None
                except Exception:
                    alive = False
                if alive:
                    running_users.append(user)
                    addr = user_tunnel_urls.get(user)
                    if not addr:
                        addr = resolve_user_address(user, user_ports.get(user, FIXED_PORT))
                        user_tunnel_urls[user] = addr
                    if addr:
                        tunnels[user] = addr
                else:
                    user_processes.pop(user, None)
                    user_tunnel_urls.pop(user, None)
                    user_ports.pop(user, None)
                    tun = user_tunnels.pop(user, None)
                    if tun:
                        try:
                            tun.terminate()
                        except Exception:
                            pass

            info["running_users"] = running_users
            info["tunnels"] = tunnels

            url = f"{MASTER_PANEL_URL}{HEARTBEAT_ENDPOINT}"
            timeout = 30 if url.startswith("https") else 20
            try:
                resp = requests.post(url, json=info, timeout=timeout, headers={"User-Agent": "SkyVex-Node/1.1"})
                if resp.status_code == 200:
                    log.info(f"✅ Heartbeat kabul edildi: {MASTER_PANEL_URL}")
                    failures = 0
                else:
                    log.warning(f"⚠️ Master {resp.status_code}: {resp.text[:120]}")
                    failures += 1
            except Exception as e:
                log.warning(f"Heartbeat hatası: {e}")
                failures += 1

            if failures > 5:
                wait = min(failures * 10, 300)
                log.warning(f"⏳ {wait}s bekleniyor (art arda hata)...")
                time.sleep(wait)
        except Exception as e:
            log.error(f"Heartbeat döngü hatası: {e}")
        time.sleep(HEARTBEAT_INTERVAL)


# ====================== ANA ======================
def supervisor_loop():
    """Çökmüş tünelleri/sunucuları izle."""
    while running:
        time.sleep(10)
        global playit_proc
        if TUNNEL_MODE == "playit":
            if playit_proc is not None and playit_proc.poll() is not None:
                log.warning("Playit API tüneli durdu, yeniden başlatılıyor...")
                start_playit_api_tunnel()
        elif TUNNEL_MODE in ("bore", "auto") and not PUBLIC_URL:
            if api_tunnel_proc is not None and api_tunnel_proc.poll() is not None:
                log.warning("Bore API tüneli durdu, yeniden başlatılıyor...")
                start_api_tunnel()


def main():
    global running
    log.info("=" * 60)
    log.info(f"🚀 SkyVex Node başlatıldı: {NODE_NAME} (ID: {NODE_ID})")
    log.info(f"📡 Master: {MASTER_PANEL_URL}")
    log.info(f"🔐 Tünel modu: {TUNNEL_MODE}")
    log.info(f"🌐 Node API: 0.0.0.0:{NODE_PORT} | public: {get_node_public_url()}")
    log.info(f"💾 Dosyalar: {FILES_DIR}")
    log.info("=" * 60)

    if START_MC_SERVER:
        start_user_server("default", {"port": FIXED_PORT, "cmd": []})

    if TUNNEL_MODE == "playit":
        start_playit_api_tunnel()
    elif TUNNEL_MODE in ("bore", "auto") and not PUBLIC_URL:
        start_api_tunnel()

    threading.Thread(target=send_heartbeat, daemon=True).start()
    threading.Thread(target=supervisor_loop, daemon=True).start()

    try:
        app.run(host="0.0.0.0", port=NODE_PORT, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        log.info("🛑 Durduruldu.")
    finally:
        running = False
        for user, proc in list(user_processes.items()):
            try:
                proc.kill()
            except Exception:
                pass
        with state_lock:
            if playit_proc is not None:
                try:
                    playit_proc.terminate()
                except Exception:
                    pass
        if api_tunnel_proc is not None:
            try:
                api_tunnel_proc.terminate()
            except Exception:
                pass
        log.info("✅ Node durduruldu.")


if __name__ == "__main__":
    main()

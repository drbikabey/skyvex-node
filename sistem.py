import flask
from flask import Flask, render_template_string, request, redirect, send_from_directory, jsonify, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_limiter.errors import RateLimitExceeded
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
import subprocess
import os
import socket
from werkzeug.utils import secure_filename
import time
import zipfile
import shutil
import threading
import queue
import urllib.parse
import platform
import re
import logging
import secrets
import locale
import psutil
from datetime import datetime, timedelta
import json
import hashlib
import requests
import uuid
import random
import collections
import functools
import urllib.request
# .env desteği opsiyonel: python-dotenv kurulu değilse panel yine çalışsın
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Flask Uygulamasını Başlat
app = Flask(__name__)
csrf = CSRFProtect(app)

# Güvenlik Ayarları
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=False, # Üretimde True yapılmalı (HTTPS gerektirir)
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024 * 1024 # 2GB Limit
)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'skyvex_panel_secret_key_2024')
API_SECRET_KEY = os.environ.get('API_SECRET_KEY', 'skyvex_api_key_99887766')
PANEL_PORT = int(os.environ.get('SERVER_PORT', os.environ.get('PORT', 15576)))

# --- Discord Bot Paneli Blueprint ---
try:
    from bot_panel import bot_bp
    app.register_blueprint(bot_bp)
    logging.info("✅ Discord Bot Paneli blueprint'i yüklendi.")
except Exception as _bot_bp_err:
    logging.warning(f"⚠️ Bot panel blueprint yüklenemedi: {_bot_bp_err}")


# --- YENİ: Bore Tünel Ayarları ---
BORE_BINARY = "bore.exe" if platform.system() == "Windows" else "bore"
BORE_VERSION = "0.5.1"
BORE_URL = f"https://github.com/ekzhang/bore/releases/download/v{BORE_VERSION}/bore-v{BORE_VERSION}-x86_64-pc-windows-msvc.zip" if platform.system() == "Windows" else f"https://github.com/ekzhang/bore/releases/download/v{BORE_VERSION}/bore-v{BORE_VERSION}-x86_64-unknown-linux-musl.tar.gz"

IS_HUGGINGFACE = "SPACE_ID" in os.environ
if IS_HUGGINGFACE:
    logging.info("🚀 Hugging Face Space ortamı algılandı.")

# --- Ödeme Sistemi Ayarları (LTC Bağış) ---
NOWPAYMENTS_API_KEY = "API-ANAHTARINIZI-BURAYA-YAZIN" # nowpayments.io üzerinden alın
MY_LTC_ADDRESS = "ltc1qt3f3a2nurcscmhv3nynn776kpvyq7qc9rj3j8v"
IS_PAYMENT_TEST_MODE = False # Gerçek ödemeler için False olmalı

def is_proc_active(p):
    """Süreç veya REMOTE durumunu kontrol eden genel fonksiyon"""
    if p is None: return False
    if isinstance(p, str): return p == "REMOTE"
    try:
        return p.poll() is None
    except:
        return False

# --- Sabitler ve Ayarlar ---
PERSISTENT_DATA = "/data" if os.path.exists("/data") and os.access("/data", os.W_OK) else "."

UPLOAD_FOLDER = os.path.join(PERSISTENT_DATA, "files")
USERS_FILE = os.path.join(PERSISTENT_DATA, "users.json")
BLACKLIST_FILE = os.path.join(PERSISTENT_DATA, "blacklist.json")
DDOS_LOG_FILE = os.path.join(PERSISTENT_DATA, "ddos_attempts.log")
LOG_FILE = os.path.join(PERSISTENT_DATA, "panel.log")

try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
except Exception as e:
    logging.warning(f"UPLOAD_FOLDER olusturulamadi: {e}")

PLUGINS_DIR = os.path.join(UPLOAD_FOLDER, "plugins")
try:
    os.makedirs(PLUGINS_DIR, exist_ok=True)
except Exception as e:
    logging.warning(f"PLUGINS_DIR olusturulamadi: {e}")

# --- Kullanıcı Yönetimi ve Güvenlik ---
def hash_password(password):
    return generate_password_hash(password)

def verify_password(hashed_password, password):
    return check_password_hash(hashed_password, password)

def save_users():
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, indent=4)
    except Exception as e:
        logging.error(f"Kullanıcı kaydetme hatası: {e}")

# Global users dictionary initialized once at the top
users = {}

def load_users():
    """Kullanıcıları JSON dosyasından yükle ve 3GB RAM'i herkese uygula"""
    global users
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                users = json.load(f)
        
        # --- Otomatik RAM ve Port İzolasyonu ---
        taken_ports = {u.get('assigned_port') for u in users.values() if u.get('assigned_port')}
        
        # Eğer admin yoksa oluştur
        if "admin" not in users:
            users["admin"] = {
                "password": generate_password_hash(os.environ.get('ADMIN_PASSWORD', 'Birkan/2006')),
                "role": "admin",
                "has_selected_plan": True,
                "server_ready": True,
                "tags": ["plus+"],
                "resources": {"ram_mb": 6144, "cpu_cores": 4, "storage_gb": 100}
            }
            save_users()
            logging.info("⭐ Varsayılan admin hesabı oluşturuldu.")

        for uname in users:
            # Sadece kaynakları HİÇ AYARLANMAMIŞSA varsayılan ata
            if "resources" not in users[uname] or users[uname]["resources"] is None:
                users[uname]["resources"] = {"ram_mb": 3072, "cpu_cores": 1, "storage_gb": 20}
            
            # Port çakışmasını önlemek için otomatik port ata (eğer yoksa)
            if not users[uname].get('assigned_port'):
                port = 25565
                while port in taken_ports:
                    port += 1
                users[uname]['assigned_port'] = port
                taken_ports.add(port)
                logging.info(f"💾 {uname} kullanıcısına otomatik port atandı: {port}")

            # Admin şifresini her zaman düzelt
            if uname == "admin":
                users[uname]["password"] = generate_password_hash("Birkan/2006")
                users[uname]["role"] = "admin"
                if "tags" not in users[uname]: users[uname]["tags"] = []
                if "plus+" not in users[uname]["tags"]: users[uname]["tags"].append("plus+")
        
        save_users() # Değişiklikleri kaydet
    except Exception as e:
        logging.error(f"Kullanıcı yükleme hatası: {e}")

# Başlangıçta kullanıcıları yükle
load_users()

def cleanup_expired_users():
    """Süresi dolmuş kullanıcıları temizle"""
    global users
    now = datetime.now()
    expired_users = []
    
    for username, user_data in users.items():
        if user_data.get("role") == "user" and "expires_at" in user_data:
            try:
                expires_at = datetime.fromisoformat(user_data["expires_at"])
                if now > expires_at:
                    expired_users.append(username)
            except:
                expired_users.append(username)
    
    for username in expired_users:
        if username in users:
            del users[username]
            import logging
            logging.info(f"Süresi dolmuş kullanıcı silindi: {username}")
    
    if expired_users:
        save_users()

def check_user_resources(username):
    """Kullanıcının kaynak limitlerini kontrol et"""
    if username not in users:
        return False
    
    user_data = users[username]
    
    if user_data.get("expires_at"):
        try:
            expires_at = datetime.fromisoformat(user_data["expires_at"])
            if datetime.now() > expires_at:
                cleanup_expired_users()
                return False
        except:
            return False
    
    return True

def login_required(f):
    """Giriş gerektiren decorator"""
    def decorated_function(*args, **kwargs):
        if 'username' not in session or session['username'] not in users:
            return redirect('/login')
        
        username = session['username']
        user_data = users.get(username, {})
        if user_data.get('role') != 'admin':
            if not user_data.get('has_selected_plan') and request.path not in ['/select-plan', '/payment', '/api/create-ltc-payment', '/logout']:
                return redirect('/select-plan')
            if not user_data.get('server_ready') and request.path not in ['/select-plan', '/payment', '/setup-wizard', '/api/create-ltc-payment', '/logout']:
                return redirect('/setup-wizard')

        if not check_user_resources(username):
            session.clear()
            return redirect('/login?error=expired')
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

def admin_required(f):
    """Admin gerektiren decorator"""
    def decorated_function(*args, **kwargs):
        if 'username' not in session or session['username'] not in users:
            return redirect('/login')
        if users[session['username']].get('role') != 'admin':
            return redirect('/?error=unauthorized')
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

# Rate Limiting
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["100 per minute"]
)

@limiter.request_filter
def ip_whitelist():
    return request.remote_addr == "127.0.0.1"

# Gelişmiş Loglama Kurulumu
LOG_FILE = "panel.log"
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
setup_logging()

# --- DDoS Koruması ve Güvenlik ---
DDOS_LOG_FILE = "ddos_attempts.log"
BLACKLIST_FILE = "blacklist.json"
blacklist = set()

def load_blacklist():
    global blacklist
    try:
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
                blacklist = set(json.load(f))
    except Exception as e:
        logging.error(f"Blacklist yükleme hatası: {e}")
        blacklist = set()

def save_blacklist():
    try:
        with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(blacklist), f)
    except Exception as e:
        logging.error(f"Blacklist kaydetme hatası: {e}")

def log_ddos_attempt(ip, path):
    """DDoS denemesini kaydet"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] IP: {ip} - Hedef: {path}\n"
    
    try:
        with open(DDOS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception as e:
        logging.error(f"DDoS loglama hatası: {e}")
    
    logging.warning(f"⚠️ DDoS/Flood Denemesi Algılandı! IP: {ip}")

def get_ddos_logs(limit=20):
    """Son DDoS loglarını oku"""
    if not os.path.exists(DDOS_LOG_FILE):
        return []
    try:
        with open(DDOS_LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return lines[-limit:]
    except:
        return []

# Başlangıçta blacklist'i yükle
load_blacklist()

@app.before_request
def check_blacklist():
    """Yasaklı IP kontrolü"""
    client_ip = request.remote_addr
    if client_ip in blacklist:
        return render_template_string("""
        <div style="text-align:center; padding:50px; font-family:sans-serif;">
            <h1 style="color:red; font-size:50px;">🚫 ERİŞİM ENGELLENDİ</h1>
            <p>IP adresiniz ({}) sunucu tarafından kalıcı olarak engellenmiştir.</p>
        </div>
        """.format(client_ip)), 403

@app.errorhandler(429)
def ratelimit_handler(e):
    """Hız sınırı aşımı (Olası DDoS)"""
    client_ip = request.remote_addr
    log_ddos_attempt(client_ip, request.path)
    
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>⚠️ Şüpheli İşlem Algılandı</title>
        <style>
            body { background: #1a1a1a; color: white; font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
            .container { text-align: center; background: #2d2d2d; padding: 2rem; border-radius: 10px; box-shadow: 0 0 20px rgba(255,0,0,0.2); border: 1px solid #ff4444; }
            h1 { color: #ff4444; }
            .ip { background: #000; padding: 10px; border-radius: 5px; font-family: monospace; font-size: 1.2rem; margin: 1rem 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>⚠️ Şüpheli İşlem Algılandı</h1>
            <p>Çok fazla istek gönderdiniz. Güvenlik nedeniyle işleminiz durduruldu.</p>
            <p>IP Adresiniz sistem yöneticisine raporlandı:</p>
            <div class="ip">{{ ip }}</div>
            <p>Lütfen bir süre bekleyin.</p>
        </div>
    </body>
    </html>
    """, ip=client_ip), 429

# Yardımcı Fonksiyonlar
def get_client_ip():
    """İsteği atan istemcinin (user) IP adresini döner"""
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0]
    return request.remote_addr

def get_public_url():
    """Panelin (Master) dış dünyadan erişilebilir URL'sini bulur"""
    # 1. Hugging Face Tespiti
    space_host = os.environ.get('SPACE_HOST', '')
    if space_host:
        return f"https://{space_host}"
    
    space_id = os.environ.get('SPACE_ID', '')
    if space_id and '/' in space_id:
        username, spacename = space_id.split('/', 1)
        return f"https://{username}-{spacename.replace('_','-')}.hf.space"

    # 2. Public IP Denemesi
    master_ip = ""
    try:
        master_ip = requests.get('https://api.ipify.org', timeout=5).text
    except:
        try:
            master_ip = requests.get('https://ifconfig.me/ip', timeout=5).text
        except:
            pass
    
    if master_ip:
        return f"http://{master_ip}:{PANEL_PORT}"
    
    return f"http://87.106.82.84:{PANEL_PORT}" # Fallback

# --- Eklenti Kataloğu (doğrulanmış, güvenli, ücretsiz) ---
# Tüm indirmeler SpigotMC (Spiget) veya resmî GeyserMC API'sinden yapılır.
PLUGIN_CATALOG = [
    # Temel / API
    {"name": "EssentialsX", "desc": "130+ temel komut: spawn, warp, home, tpa, kit, ekonomi.", "modrinth": "essentialsx", "source": "Modrinth", "popular": True, "category": "Temel"},
    {"name": "Vault", "desc": "Ekonomi, izin ve sohbet eklentileri için ortak API.", "url": "https://api.spiget.org/v2/resources/34315/download", "source": "SpigotMC", "popular": True, "category": "Temel"},
    {"name": "PlaceholderAPI", "desc": "Eklentiler arası veri paylaşımı (placeholder) altyapısı.", "url": "https://api.spiget.org/v2/resources/6245/download", "source": "SpigotMC", "popular": True, "category": "API"},
    {"name": "ProtocolLib", "desc": "Gelişmiş eklentiler için paket düzeyinde altyapı.", "url": "https://api.spiget.org/v2/resources/1997/download", "source": "SpigotMC", "popular": True, "category": "API"},
    # Yetki / güvenlik
    {"name": "LuckPerms", "desc": "Modern, hızlı ve güvenilir yetki/grup sistemi.", "url": "https://api.spiget.org/v2/resources/28140/download", "source": "SpigotMC", "popular": True, "category": "Yetki"},
    {"name": "AuthMeReloaded", "desc": "/login ve /register ile hesap koruması.", "modrinth": "authmereloaded", "source": "Modrinth", "popular": True, "category": "Güvenlik"},
    {"name": "AdvancedBan", "desc": "Ban, mute, kick ve uyarı yönetimi.", "url": "https://api.spiget.org/v2/resources/8695/download", "source": "SpigotMC", "category": "Güvenlik"},
    {"name": "NoCheatPlus", "desc": "Hile ve bug istismarına karşı temel koruma.", "url": "https://api.spiget.org/v2/resources/26/download", "source": "SpigotMC", "category": "Güvenlik"},
    {"name": "CoreProtect", "desc": "Blok kaydı, geri alma (rollback) ve grief tespiti.", "modrinth": "coreprotect", "source": "Modrinth", "popular": True, "category": "Koruma"},
    {"name": "GriefPrevention", "desc": "Oyuncuların kendi arazisini korumasını sağlar.", "modrinth": "griefprevention", "source": "Modrinth", "popular": True, "category": "Koruma"},
    {"name": "Towny", "desc": "En popüler şehir/ülke yönetim eklentisi. //town, /res, /plot komutları.", "url": "https://api.spiget.org/v2/resources/1635/download", "source": "SpigotMC", "category": "Düzenleme"},
    # Uyumluluk
    {"name": "ViaVersion", "desc": "Yeni sürüm istemcilerinin sunucuya girmesini sağlar.", "url": "https://api.spiget.org/v2/resources/19254/download", "source": "SpigotMC", "popular": True, "category": "Uyumluluk"},
    {"name": "ViaBackwards", "desc": "Eski sürüm istemcilerinin girmesini sağlar.", "modrinth": "viabackwards", "source": "Modrinth", "category": "Uyumluluk"},
    {"name": "ViaRewind", "desc": "1.7/1.8 istemcileri için ek uyumluluk.", "modrinth": "viarewind", "source": "Modrinth", "category": "Uyumluluk"},
    {"name": "Geyser-Spigot", "desc": "Bedrock (telefon/tablet) oyuncularının girmesini sağlar.", "url": "https://download.geysermc.org/v2/projects/geyser/versions/latest/builds/latest/downloads/spigot", "source": "GeyserMC", "popular": True, "category": "Uyumluluk"},
    {"name": "Floodgate", "desc": "Bedrock oyuncuları için Java hesabı olmadan giriş.", "url": "https://download.geysermc.org/v2/projects/floodgate/versions/latest/builds/latest/downloads/spigot", "source": "GeyserMC", "category": "Uyumluluk"},
    # İnşaat / dünya
    {"name": "FastAsyncWorldEdit", "desc": "WorldEdit uyumlu, çok hızlı blok düzenleme aracı.", "modrinth": "fastasyncworldedit", "source": "Modrinth", "popular": True, "category": "İnşaat"},
    {"name": "Multiverse-Core", "desc": "Birden fazla dünya oluşturma ve yönetme.", "modrinth": "multiverse-core", "source": "Modrinth", "popular": True, "category": "Dünya"},
    {"name": "Chunky", "desc": "Haritayı önceden oluşturarak lag'i azaltır.", "url": "https://api.spiget.org/v2/resources/81534/download", "source": "SpigotMC", "category": "Performans"},
    # Görünüm
    {"name": "SkinsRestorer", "desc": "Cracked sunucularda skinlerin görünmesini sağlar.", "modrinth": "skinsrestorer", "source": "Modrinth", "popular": True, "category": "Görünüm"},
    {"name": "DecentHolograms", "desc": "Havada duran yazı ve hologram sistemi.", "url": "https://api.spiget.org/v2/resources/96927/download", "source": "SpigotMC", "category": "Görünüm"},
    {"name": "DeluxeMenus", "desc": "Tamamen özelleştirilebilir GUI menüleri.", "url": "https://api.spiget.org/v2/resources/11734/download", "source": "SpigotMC", "popular": True, "category": "Görünüm"},
    # Ekonomi / oyun
    {"name": "Jobs Reborn", "desc": "Oyuncuların meslek yapıp para kazanmasını sağlar.", "url": "https://api.spiget.org/v2/resources/4216/download", "source": "SpigotMC", "category": "Ekonomi"},
    {"name": "ChestShop", "desc": "Tabela ile oyuncu dükkanı kurma.", "url": "https://api.spiget.org/v2/resources/51856/download", "source": "SpigotMC", "category": "Ekonomi"},
    {"name": "AuctionHouse", "desc": "Oyuncular arası açık artırma/mezat sistemi.", "url": "https://api.spiget.org/v2/resources/61836/download", "source": "SpigotMC", "category": "Ekonomi"},
    {"name": "Quests", "desc": "Görev ve ödül sistemi.", "url": "https://api.spiget.org/v2/resources/3711/download", "source": "SpigotMC", "category": "Oyun"},
    {"name": "MythicMobs", "desc": "Özel canavar ve boss oluşturma (ücretsiz sürüm).", "modrinth": "mythicmobs", "source": "Modrinth", "category": "Oyun"},
    # Ulaşım / harita
    {"name": "SimpleTPA", "desc": "Oyuncular arası ışınlanma isteği (/tpa).", "url": "https://api.spiget.org/v2/resources/74834/download", "source": "SpigotMC", "category": "Ulaşım"},
    {"name": "BlueMap", "desc": "Tarayıcıda 3B canlı harita.", "url": "https://api.spiget.org/v2/resources/83557/download", "source": "SpigotMC", "category": "Harita"},
    {"name": "Dynmap", "desc": "Klasik web haritası (Google Maps tarzı).", "modrinth": "dynmap", "source": "Modrinth", "category": "Harita"},
    # Oy / destek
    {"name": "NuVotifier", "desc": "Oy sitelerinden gelen oyları sunucuya iletir.", "url": "https://api.spiget.org/v2/resources/13449/download", "source": "SpigotMC", "category": "Oy"},
    {"name": "VotingPlugin", "desc": "Oy verme, oy ödülü ve sıralama sistemi.", "url": "https://api.spiget.org/v2/resources/15358/download", "source": "SpigotMC", "popular": True, "category": "Oy"},
    # Ekonomi (SpigotMC)
    {"name": "EconomyShopGUI", "desc": "GUI tabanlı, hızlı ve ücretsiz oyuncu dükkânı.", "url": "https://api.spiget.org/v2/resources/69927/download", "source": "SpigotMC", "popular": True, "category": "Ekonomi"},
    # Oyun / eğlence (SpigotMC)
    {"name": "MobArena", "desc": "Dalga dalga canavar arenası ve ödüller.", "url": "https://api.spiget.org/v2/resources/34110/download", "source": "SpigotMC", "category": "Oyun"},
    {"name": "TreeAssist", "desc": "Ağacı tek vuruşta tamamen devirir.", "url": "https://api.spiget.org/v2/resources/4657/download", "source": "SpigotMC", "category": "Oyun"},
    {"name": "PlayTime", "desc": "Oyuncuların toplam oynama süresini gösterir.", "url": "https://api.spiget.org/v2/resources/3898/download", "source": "SpigotMC", "category": "İstatistik"},
    {"name": "HeadDatabase", "desc": "Binlerce hazır oyuncu kafası (dekorasyon).", "url": "https://api.spiget.org/v2/resources/95118/download", "source": "SpigotMC", "category": "Görünüm"},
    # Modrinth (her zaman en güncel sürüm otomatik çözülür)
    {"name": "WorldEdit", "desc": "Resmî WorldEdit: hızlı inşaat ve blok düzenleme.", "modrinth": "worldedit", "source": "Modrinth", "popular": True, "category": "İnşaat"},
    {"name": "WorldGuard", "desc": "Resmî WorldGuard: bölge koruma ve kurallar.", "modrinth": "worldguard", "source": "Modrinth", "popular": True, "category": "Koruma"},
    {"name": "Simple Voice Chat", "desc": "Yakınlık sesli sohbet (proximity voice).", "modrinth": "simple-voice-chat", "source": "Modrinth", "popular": True, "category": "Ses"},
    {"name": "Plasmo Voice", "desc": "Alternatif yakınlık sesli sohbet eklentisi.", "modrinth": "plasmo-voice", "source": "Modrinth", "category": "Ses"},
    {"name": "DiscordSRV", "desc": "Minecraft sohbetini Discord'a bağlar.", "modrinth": "discordsrv", "source": "Modrinth", "popular": True, "category": "Entegrasyon"},
    {"name": "MiniMOTD", "desc": "Sunucu listesinde şık MOTD ve ikon.", "modrinth": "minimotd", "source": "Modrinth", "category": "Görünüm"},
    {"name": "TabTPS", "desc": "Tab'da TPS, MSPT ve ping gösterir.", "modrinth": "tabtps", "source": "Modrinth", "category": "Performans"},
    {"name": "FreedomChat", "desc": "Sohbet raporlamasını kapatır (cracked sunucular).", "modrinth": "freedomchat", "source": "Modrinth", "category": "Sohbet"},
    {"name": "Terra", "desc": "Özelleştirilebilir dünya üretimi (biome/preset).", "modrinth": "terra", "source": "Modrinth", "category": "Dünya"},
]

# Modrinth üzerinden her zaman güncel jar'ı çözer (6 saat önbellek)
MODRINTH_LOADERS = '["bukkit","spigot","paper","purpur","folia"]'
_modrinth_cache = {}  # (slug, mc_version) -> (url, timestamp)


def resolve_modrinth_url(slug, mc_version=None, ttl=21600):
    """Modrinth projesinin sunucu sürümüne uygun en yeni jar adresini döndürür."""
    slug = (slug or "").strip()
    if not slug:
        return None
    mc_version = (mc_version or "").strip()
    cache_key = (slug, mc_version)
    now = time.time()
    cached = _modrinth_cache.get(cache_key)
    if cached and now - cached[1] < ttl:
        return cached[0]
    try:
        res = requests.get(
            f"https://api.modrinth.com/v2/project/{slug}/version",
            params={"loaders": MODRINTH_LOADERS},
            headers={"User-Agent": "SkyVexPanel/1.0"},
            timeout=8,
        )
        if res.status_code == 200:
            versions = res.json() or []
            # Sunucu sürümüyle uyumlu en yeni sürümü seç
            candidates = versions
            if mc_version:
                candidates = [v for v in versions if mc_version in (v.get("game_versions") or [])]
            if not candidates and not versions:
                return None
            # Uyumlu sürüm yoksa en yenisiyle devam et
            for ver in (candidates or versions):
                files = ver.get("files") or []
                primary = next((f for f in files if f.get("primary")), files[0] if files else None)
                if primary and primary.get("url"):
                    _modrinth_cache[cache_key] = (primary["url"], now)
                    return primary["url"]
    except Exception as e:
        logging.warning(f"Modrinth çözümleme hatası ({slug}): {e}")
    return None


def plugin_public_item(p):
    """Katalog kaydını arayüz için hazırlar (Modrinth kayıtları için özel URL)."""
    item = dict(p)
    if p.get("modrinth"):
        item["url"] = f"modrinth:{p['modrinth']}"
        item["source_url"] = f"https://modrinth.com/plugin/{p['modrinth']}"
    else:
        item.setdefault("source_url", p.get("url", ""))
    return item

# Multi-tenant yönetimi
user_processes = {} # username -> process
user_console_queues = {} # username -> queue
user_console_histories = {} # username -> list
server_activity = {} # username -> last active timestamp
active_players = {} # username -> set() of online players # username -> list
user_bore_processes = {} # username -> bore process
user_bore_address = {}   # username -> bore address
failed_logins = {} # ip -> {'count': int, 'last_attempt': timestamp}
LOCKOUT_COUNT = 5
LOCKOUT_DURATION = 60 # saniye

# ANSI renk kodlarını temizlemek için ortak desen
ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def track_players_from_lines(username, lines):
    """Konsol satırlarından oyuncu giriş/çıkışlarını ayrıştırır ve active_players'ı günceller."""
    if username not in active_players:
        active_players[username] = set()
    players = active_players[username]
    for raw in lines:
        line = ANSI_ESCAPE_RE.sub('', str(raw))
        join_match = re.search(r"(?:^|\]:\s)([A-Za-z0-9_.]{3,32})\s+joined the game", line)
        if join_match:
            players.add(join_match.group(1))
            server_activity[username] = time.time()
            continue
        leave_match = re.search(r"(?:^|\]:\s)([A-Za-z0-9_.]{3,32})\s+(?:left the game|lost connection|logged out)", line)
        if leave_match:
            players.discard(leave_match.group(1))
            server_activity[username] = time.time()
    return players

def get_user_dir(username):
    """Kullanıcının özel dosya yolunu döner"""
    path = os.path.join(UPLOAD_FOLDER, username)
    os.makedirs(path, exist_ok=True)
    return path

def get_user_file_path(username, filename):
    return os.path.join(get_user_dir(username), filename)

java_path = "java"
SERVER_JAR_NAME = "server.jar"
START_SCRIPT_PATH = os.path.join(UPLOAD_FOLDER, "baslat.bat")
SERVER_JAR_PATH = os.path.join(UPLOAD_FOLDER, SERVER_JAR_NAME)
SERVER_PROPERTIES_PATH = os.path.join(UPLOAD_FOLDER, "server.properties")

# Kullanıcı yönetimi
# Global users is already initialized at line 82/105 and populated by load_users()

active_sessions = {}

# Sunucu başlatma parametreleri (RAM 3GB varsayılan)
server_settings = {
    "min_ram": "3072M",
    "max_ram": "3072M",
    "java_args": "-XX:+UseG1GC -XX:+UnlockExperimentalVMOptions -XX:MaxGCPauseMillis=100"
}

# --- YENİ: Node Yönetimi ---
nodes = {} # node_id -> info
NODE_SECRET = "skyvex_secret_123" # sistem.py ile aynı olmalı
MAX_USERS_PER_NODE = 5 # 16GB RAM / 3GB sunucu = 5 sunucu kapasitesi


# --- YENİ: Kayıt Olma ve Cihaz Kilidi (MAC Benzetimi) ---

def get_device_fingerprint():
    """Tarayıcı IP ve bazı header'lar ile bir parmak izi oluşturur (MAC adresi yerine)"""
    fingerprint_parts = [
        request.remote_addr,
        request.headers.get('User-Agent', ''),
        request.headers.get('Accept-Language', '')
    ]
    return hashlib.sha256("|".join(fingerprint_parts).encode()).hexdigest()

MAX_ACCOUNTS_PER_DEVICE = 2  # Suistimali önlemek için cihaz başına en fazla hesap

def is_device_limit_reached():
    """Aynı cihazdan en fazla 2 hesap açılabilir."""
    try:
        fp = get_device_fingerprint()
        used = sum(1 for u in users.values() if u.get('device_fingerprint') == fp)
        return used >= MAX_ACCOUNTS_PER_DEVICE
    except Exception:
        return False

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not username or not password or not confirm_password:
            return render_template_string(register_html, error="missing_fields")
        
        if password != confirm_password:
            return render_template_string(register_html, error="password_mismatch")
        
        if username in users:
            return render_template_string(register_html, error="user_exists")
        
        if is_device_limit_reached():
            return render_template_string(register_html, error="device_limit")

        # Kullanıcıyı kaydet ve port ata
        taken_ports = {u.get('assigned_port') for u in users.values() if u.get('assigned_port')}
        port = 25565
        while port in taken_ports:
            port += 1

        users[username] = {
            "password": hash_password(password),
            "role": "user",
            "created_at": datetime.now().isoformat(),
            "device_fingerprint": get_device_fingerprint(),
            "has_selected_plan": False,
            "resources": None,
            "assigned_port": port
        }

        save_users()
        session['username'] = username
        return redirect('/select-plan')

    return render_template_string(register_html)

register_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Skyvex Hosting - Kayıt Ol</title>
    <style>
        :root {
            --primary: #6366f1;
            --primary-dark: #4338ca;
            --secondary: #10b981;
            --danger: #ef4444;
            --dark: #1e1e2f;
            --darker: #151521;
            --light: #f8fafc;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, var(--darker) 0%, var(--dark) 100%);
            color: var(--light);
            min-height: 100vh;
            display: flex; align-items: center; justify-content: center;
        }
        .register-container {
            background: rgba(30, 30, 47, 0.95);
            backdrop-filter: blur(10px);
            padding: 2.5rem;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            border: 1px solid rgba(255, 255, 255, 0.1);
            width: 100%; max-width: 400px;
        }
        .logo { text-align: center; margin-bottom: 2rem; }
        .logo h1 {
            background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            font-size: 2rem; font-weight: 700;
        }
        .form-group { margin-bottom: 1.2rem; }
        .form-label { display: block; margin-bottom: 0.5rem; font-weight: 600; }
        .form-input {
            width: 100%; padding: 0.8rem;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 10px; color: var(--light);
        }
        .btn {
            width: 100%; padding: 1rem; border: none; border-radius: 10px;
            cursor: pointer; font-weight: 600; color: white;
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
            transition: 0.3s;
        }
        .btn:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(99, 102, 241, 0.4); }
        .alert {
            padding: 0.8rem; border-radius: 10px; margin-bottom: 1rem;
            background: rgba(239, 68, 68, 0.1); border-left: 4px solid var(--danger); color: #fca5a5;
        }
        .login-link { text-align: center; margin-top: 1.5rem; font-size: 0.9rem; }
        .login-link a { color: var(--primary); text-decoration: none; font-weight: 600; }
    </style>
</head>
<body>
    <div class="register-container">
        <div class="logo"><h1>🚀 Kayıt Ol</h1></div>
        {% if error %}
        <div class="alert">
            {% if error == 'missing_fields' %} ⚠️ Lütfen tüm alanları doldurun!
            {% elif error == 'password_mismatch' %} ❌ Şifreler uyuşmuyor!
            {% elif error == 'user_exists' %} 👤 Bu kullanıcı adı zaten alınmış!
            {% elif error == 'device_limit' %} 🚫 Bir cihazdan en fazla 2 hesap açılabilir!
            {% endif %}
        </div>
        {% endif %}
        <form method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <div class="form-group">
                <label class="form-label">👤 Kullanıcı Adı</label>
                <input type="text" class="form-input" name="username" required>
            </div>
            <div class="form-group">
                <label class="form-label">🔑 Şifre</label>
                <input type="password" class="form-input" name="password" required>
            </div>
            <div class="form-group">
                <label class="form-label">🔁 Şifre Tekrar</label>
                <input type="password" class="form-input" name="confirm_password" required>
            </div>
            <button type="submit" class="btn">Kayıt Ol 🚀</button>
        </form>
        <div class="login-link">
            Zaten hesabın var mı? <a href="/login">Giriş Yap</a>
        </div>
    </div>
</body>
</html>
"""
PAID_PLANS = {
    'komur': {"name": "Kömür Paket", "emoji": "⛏️", "price_tl": 20, "ram_mb": 4096, "cpu_cores": 1, "storage_gb": 50, "color": "#78716c"},
    'demir': {"name": "Demir Paket", "emoji": "🪨", "price_tl": 40, "ram_mb": 8192, "cpu_cores": 2, "storage_gb": 70, "color": "#94a3b8"},
    'elmas': {"name": "Elmas Paket", "emoji": "💎", "price_tl": 60, "ram_mb": 12288, "cpu_cores": 3, "storage_gb": 90, "color": "#38bdf8"},
    'netherite': {"name": "Netherite Paket", "emoji": "🔮", "price_tl": 80, "ram_mb": 16384, "cpu_cores": 3, "storage_gb": 100, "color": "#a855f7"},
    'destek_25': {"name": "Bronz Destek", "emoji": "🥉", "price_tl": 25, "ram_mb": 0, "cpu_cores": 0, "storage_gb": 0, "color": "#cd7f32"},
    'destek_50': {"name": "Gümüş Destek", "emoji": "🥈", "price_tl": 50, "ram_mb": 0, "cpu_cores": 0, "storage_gb": 0, "color": "#c0c0c0"},
    'destek_100': {"name": "Altın Destek", "emoji": "🥇", "price_tl": 100, "ram_mb": 0, "cpu_cores": 0, "storage_gb": 0, "color": "#ffd700"},
    'destek_250': {"name": "Safir Destek", "emoji": "💎", "price_tl": 250, "ram_mb": 0, "cpu_cores": 0, "storage_gb": 0, "color": "#0f52ba"},
    'destek_500': {"name": "Titanium Destek", "emoji": "⚔️", "price_tl": 500, "ram_mb": 0, "cpu_cores": 0, "storage_gb": 0, "color": "#e5e4e2"},
}

@app.route('/select-plan', methods=['GET', 'POST'])
@login_required
def select_plan():
    username = session['username']
    if users[username].get('has_selected_plan'):
        return redirect('/')
    
    if request.method == 'POST':
        plan = request.form.get('plan')
        server_name = request.form.get('server_name', 'Benim Sunucum')
        version = request.form.get('version', '1.20.4')
        software = request.form.get('software', 'paper')
        java_v = request.form.get('java_version', '17')

        if plan == 'free':
            # Set resources based on plan
            users[username]['resources'] = {"ram_mb": 3072, "cpu_cores": 1, "storage_gb": 20}

            # Save server config
            user_dir = get_user_dir(username)
            os.makedirs(user_dir, exist_ok=True)
            
            server_type = request.form.get('server_type', 'genel')
            users[username]['server_config'] = {
                "name": server_name, 
                "version": version, 
                "software": software, 
                "java_version": java_v,
                "server_type": server_type
            }

            # Write basic server files
            accept_eula(username)
            write_server_properties(username, {"server-name": server_name, "motd": f"A Minecraft Server - {server_name}", "level-name": "world", "online-mode": "false"})

            users[username]['has_selected_plan'] = True
            users[username]['server_ready'] = True
            save_users()
            logging.info(f"Plan ve sunucu kurulumu tamamlandı ({username}): plan={plan}, sunucu={server_name}")

            # Download jar in background
            threading.Thread(target=download_server_jar_thread, args=(software, version, user_dir, username), daemon=True).start()

            return redirect('/dashboard')
        elif plan in PAID_PLANS:
            # Paid plan selected -> Redir to payment page
            return redirect(f'/payment?plan={plan}')
        else:
            # Invalid plan
            return render_template_string(select_plan_html, error="invalid_plan")
    
    return render_template_string(select_plan_html, error=None, paid_plans=PAID_PLANS)


@app.route('/payment')
@app.route('/payment/')
@login_required
def payment_page():
    plan = request.args.get('plan', '')
    username = session['username']
    
    # Eğer plan yoksa ama daha önceden seçilmiş bir bekleyen plan varsa onu al
    if not plan:
        plan = users[username].get('pending_plan', '')

    if plan not in PAID_PLANS:
        # Hâlâ plan yoksa en ucuz paketi varsayılan gösterelim ki sayfa boş kalmasın
        plan = 'destek_25' 
    
    # Bekleyen planı kaydet
    users[username]['pending_plan'] = plan
    save_users()
    
    plan_info = PAID_PLANS[plan]
    return render_template_string(payment_html, plan=plan, plan_info=plan_info, username=username)

@app.route('/api/create-ltc-payment', methods=['POST'])
@login_required
def create_ltc_payment():
    """NOWPayments API kullanarak Fiat-to-LTC ödeme linki oluşturur"""
    username = session['username']
    plan_id = request.form.get('plan')
    
    if plan_id not in PAID_PLANS:
        return redirect('/select-plan')
    
    plan = PAID_PLANS[plan_id]
    price = plan['price_tl']
    
    # NOWPayments İşlem Oluşturma API Çağrısı
    url = "https://api.nowpayments.io/v1/payment"
    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json"
    }
    
    payload = {
        "price_amount": price,
        "price_currency": "try",
        "pay_amount": None, # API tarafından hesaplanacak
        "pay_currency": "ltc",
        "ipn_callback_url": f"{get_public_url()}/api/payment-webhook",
        "order_id": f"ORDER_{username}_{plan_id}_{int(time.time())}",
        "order_description": f"SkyVex Minecraft - {plan['name']} Bağışı",
        "payout_address": MY_LTC_ADDRESS,
        "fiat_amount": price,
        "is_fixed_rate": False,
        "is_fee_paid_by_user": True
    }
    
    try:
        # Gerçek API anahtarı girilmemişse veya Test Modu aktifse simüle et
        if NOWPAYMENTS_API_KEY == "API-ANAHTARINIZI-BURAYA-YAZIN" or IS_PAYMENT_TEST_MODE:
            mock_invoice_url = "/dashboard?success=test_payment"
            # Test modunda kullanıcıya otomatik plus+ verelim (simülasyon için)
            if username in users:
                if "tags" not in users[username]: users[username]["tags"] = []
                if "plus+" not in users[username]["tags"]: users[username]["tags"].append("plus+")
                users[username]['has_selected_plan'] = True
                users[username]['server_ready'] = True
                save_users()
            logging.info(f"Simüle ödeme yapıldı ({username})")
            return redirect(mock_invoice_url)

        invoice_url = "https://api.nowpayments.io/v1/invoice"
        res = requests.post(invoice_url, headers=headers, json=payload, timeout=10)
        
        if res.status_code in [200, 201]:
            data = res.json()
            payment_url = data.get('invoice_url')
            logging.info(f"Ödeme linki oluşturuldu ({username}): {payment_url}")
            return redirect(payment_url)
        else:
            logging.error(f"NOWPayments Hatası: {res.text}")
            return f"Ödeme sistemi şu an başlatılamadı. Hata: {res.text}", 500
            
    except Exception as e:
        logging.error(f"Ödeme oluşturma hatası: {e}")
        return "Bir sistem hatası oluştu, lütfen daha sonra tekrar deneyiniz.", 500

@app.route('/api/payment-webhook', methods=['POST'])
@csrf.exempt
def payment_webhook():
    """NOWPayments'tan gelen ödeme bildirimlerini işler"""
    # Güvenlik: x-nowpayments-sig header kontrolü yapılmalı (opsiyonel ama önerilir)
    data = request.json
    if not data:
        return "No data", 400
        
    payment_status = data.get('payment_status')
    order_id = data.get('order_id', '') # Format: ORDER_username_planid_timestamp
    
    if payment_status == 'finished':
        try:
            parts = order_id.split('_')
            if len(parts) >= 3:
                username = parts[1]
                plan_id = parts[2]
                
                if username in users:
                    plan = PAID_PLANS.get(plan_id)
                    if plan:
                        # Kullanıcı özelliklerini güncelle
                        users[username]['has_selected_plan'] = True
                        users[username]['resources'] = {
                            "ram_mb": plan['ram_mb'],
                            "cpu_cores": plan['cpu_cores'],
                            "storage_gb": plan['storage_gb']
                        }
                        users[username]['tags'] = users[username].get('tags', [])
                        if 'plus+' not in users[username]['tags']:
                            users[username]['tags'].append('plus+')
                        
                        # 1 aylık süre ekle
                        expires = datetime.now() + timedelta(days=30)
                        users[username]['expires_at'] = expires.isoformat()
                        
                        save_users()
                        logging.info(f"✅ ÖDEME BAŞARILI: {username} - {plan_id} paketi aktif edildi.")
        except Exception as e:
            logging.error(f"Webhook işleme hatası: {e}")

    return "OK", 200

def download_server_jar_thread(software, version, user_dir, username):
    try:
        jar_path = os.path.join(user_dir, SERVER_JAR_NAME)
        
        if username not in user_console_histories:
            user_console_histories[username] = []
            
        user_console_histories[username].append(f"[{time.strftime('%H:%M:%S')}] ⏳ {software} {version} hazırlanıyor...\n")
        
        # --- Disk Alanı Kontrolü ---
        try:
            disk = shutil.disk_usage(user_dir)
            free_mb = disk.free / (1024 * 1024)
            if free_mb < 200:  # 200MB'dan az boş alan varsa hata ver
                raise OSError(f"Yetersiz disk alanı! Boş alan: {free_mb:.0f} MB. En az 200 MB gerekli.")
            user_console_histories[username].append(f"[{time.strftime('%H:%M:%S')}] 💾 Disk alanı kontrol edildi: {free_mb:.0f} MB boş\n")
        except OSError as disk_err:
            if "Yetersiz" in str(disk_err):
                raise
            logging.warning(f"Disk kontrolü yapılamadı: {disk_err}")
        
        # Sürüm eşleşmesi (Özel sürümleri gerçeğe eşle)
        def get_real_version(v, sw):
            v_map = {
                "1.21.11": "1.21.1", "1.21.10": "1.21.1", "1.21.9": "1.21.1",
                "1.21.8": "1.21.1", "1.21.7": "1.21.1", "1.21.6": "1.21.1",
                "1.21.5": "1.21.1", "1.21.4": "1.21.1", "1.21.3": "1.21.1",
                "1.21.2": "1.21.1", "1.21.1": "1.21.1", "1.21": "1.21",
                "1.20.6": "1.20.6", "1.20.4": "1.20.4", "1.20.1": "1.20.1", "1.20": "1.20.1",
                "1.19.4": "1.19.4", "1.19.2": "1.19.2", "1.18.2": "1.18.2",
                "1.17.1": "1.17.1", "1.16.5": "1.16.5", "1.12.2": "1.12.2", "1.8.8": "1.8.8"
            }
            rv = v_map.get(v, v)
            return rv

        real_version = get_real_version(version, software)

        # --- Çoklu Kaynak URL Listesi ---
        urls_to_try = []
        if software == "paper":
            # 1. Dinamik API sorgusu (en guncel build)
            try:
                import ssl as _ssl
                _ctx = _ssl.create_default_context()
                _ctx.check_hostname = False
                _ctx.verify_mode = _ssl.CERT_NONE
                api_url = f"https://fill.papermc.io/v3/projects/paper/versions/{real_version}"
                req = urllib.request.Request(api_url, headers={'User-Agent': 'SkyvexPanel/3.0'})
                with urllib.request.urlopen(req, timeout=15, context=_ctx) as res:
                    _d = json.loads(res.read())
                    if 'builds' in _d and _d['builds']:
                        build = _d['builds'][-1]
                        # 2. Asama: Indirme linkini al
                        req2 = urllib.request.Request(f"{api_url}/builds/{build}", headers={'User-Agent': 'SkyvexPanel/3.0'})
                        with urllib.request.urlopen(req2, timeout=15, context=_ctx) as res2:
                            _d2 = json.loads(res2.read())
                            dl_url = _d2['downloads']['server:default']['url']
                            urls_to_try.append(("PaperMC API (v3)", dl_url))
            except Exception as _e:
                logging.warning(f"Paper dinamik API hatasi: {_e}")
            
            # 2. Bilinen stabil build linkleri (v3 Object Storage)
            if real_version == "1.21.1":
                urls_to_try.append(("PaperMC 1.21.1 Build-137", "https://fill-data.papermc.io/v1/objects/2bc0b98522e86ef5cd68fb8d0f19c3b12368c2f1f510a2f4abf048d0dfb6c694/paper-1.21.1-137.jar"))
                urls_to_try.append(("PaperMC 1.21.1 Build-131", "https://fill-data.papermc.io/v1/objects/56230f73f8a483e60dd6b445582f3ef4cb9e005be9c25950d4f686c12ad12a7d/paper-1.21.1-131.jar"))
                urls_to_try.append(("PaperMC 1.21.1 Build-2", "https://fill-data.papermc.io/v1/objects/24a2bcb24ce72027adbf12cc151f76e2ed67139ba51a3ca5f2f954d9099de41e/paper-1.21.1-2.jar"))
            elif real_version == "1.20.4":
                urls_to_try.append(("PaperMC 1.20.4 Build-496", "https://fill-data.papermc.io/v1/objects/ef553e1a067ed77f54c9a5b3a4e934e6221d60742512f46405baea56b6947ed0/paper-1.20.4-496.jar"))
        elif software == "purpur":
            urls_to_try.append(("PurpurMC API", f"https://api.purpurmc.org/v2/purpur/{real_version}/latest/download"))
            urls_to_try.append(("ServerJars", f"https://serverjars.com/api/fetchJar/purpur/{real_version}"))
        elif software == "vanilla":
            # Mojang Resmi Sunucu Jar (1.21.1)
            urls_to_try.append(("Mojang (Resmi)", "https://piston-data.mojang.com/v1/objects/45041375b47c64632a7e7edde45585f9aa7c88b7/server.jar"))
            urls_to_try.append(("ServerJars", f"https://serverjars.com/api/fetchJar/vanilla/{real_version}"))
        else:  # spigot, craftbukkit
            urls_to_try.append(("ServerJars", f"https://serverjars.com/api/fetchJar/{software}/{real_version}"))
            if software == "spigot":
                urls_to_try.append(("GetBukkit", f"https://download.getbukkit.org/spigot/spigot-{real_version}.jar"))
        
        # --- Son Care Fallback ---
        if software == "paper":
            try:
                import ssl as _ssl
                _ctx = _ssl.create_default_context()
                _ctx.check_hostname = False
                _ctx.verify_mode = _ssl.CERT_NONE
                api_url = f"https://fill.papermc.io/v3/projects/paper/versions/1.21.1"
                req = urllib.request.Request(api_url, headers={'User-Agent': 'SkyvexPanel/3.0'})
                with urllib.request.urlopen(req, timeout=15, context=_ctx) as res:
                    _d = json.loads(res.read())
                    if 'builds' in _d and _d['builds']:
                        build = _d['builds'][-1]
                        req2 = urllib.request.Request(f"{api_url}/builds/{build}", headers={'User-Agent': 'SkyvexPanel/3.0'})
                        with urllib.request.urlopen(req2, timeout=15, context=_ctx) as res2:
                            _d2 = json.loads(res2.read())
                            dl_url = _d2['downloads']['server:default']['url']
                            urls_to_try.append(("Paper 1.21.1 Fallback (v3)", dl_url))
            except:
                pass
            urls_to_try.append(("PaperMC 1.21.1 Fallback Build-2", "https://fill-data.papermc.io/v1/objects/24a2bcb24ce72027adbf12cc151f76e2ed67139ba51a3ca5f2f954d9099de41e/paper-1.21.1-2.jar"))
        elif software == "purpur":
            urls_to_try.append(("Purpur Fallback", "https://api.purpurmc.org/v2/purpur/1.21.1/latest/download"))

        downloaded = False
        last_error = "Hiçbir kaynak denenmedi."
        
        for source_name, url in urls_to_try:
            try:
                user_console_histories[username].append(f"[{time.strftime('%H:%M:%S')}] 🔄 Deneniyor: {source_name} → {url}\n")
                logging.info(f"Jar İndiriliyor ({username}) [{source_name}]: {url}")
                
                # Eski dosyayi sil
                if os.path.exists(jar_path): os.remove(jar_path)
                
                # SSL bypass ile indir
                import ssl as _ssl_dl
                _ssl_ctx = _ssl_dl.create_default_context()
                _ssl_ctx.check_hostname = False
                _ssl_ctx.verify_mode = _ssl_dl.CERT_NONE
                _opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_ctx))
                _opener.addheaders = [('User-Agent', 'SkyvexPanel/2.0'), ('Accept', '*/*')]
                urllib.request.install_opener(_opener)
                urllib.request.urlretrieve(url, jar_path)
                
                # Doğrulama
                if os.path.exists(jar_path):
                    file_size = os.path.getsize(jar_path)
                    if file_size < 1 * 1024 * 1024:
                        raise ValueError(f"Dosya çok küçük ({file_size} bytes) - muhtemelen hatalı kaynak.")
                    if not zipfile.is_zipfile(jar_path):
                        raise ValueError("İndirilen dosya geçerli bir JAR değil.")
                    user_console_histories[username].append(f"[{time.strftime('%H:%M:%S')}] ✅ {software} {version} başarıyla kuruldu! [{source_name}] (Boyut: {'{:.1f}'.format(file_size/(1024*1024))} MB)\n")
                    downloaded = True
                    break
                else:
                    raise FileNotFoundError("Dosya indirilemedi (boş sonuç).")
            except Exception as src_err:
                last_error = str(src_err)
                user_console_histories[username].append(f"[{time.strftime('%H:%M:%S')}] ⚠️ {source_name} başarısız: {src_err}\n")
                logging.warning(f"Kaynak {source_name} başarısız ({username}): {src_err}")
                if os.path.exists(jar_path):
                    try: os.remove(jar_path)
                    except: pass
                continue
        
        if not downloaded:
            raise RuntimeError(f"Tüm kaynaklar denendi fakat indirme başarısız! Son hata: {last_error}")
            
    except Exception as e:
        logging.error(f"İndirme hatası ({username}): {e}")
        if username in user_console_histories:
            user_console_histories[username].append(f"[{time.strftime('%H:%M:%S')}] ❌ KRİTİK HATA: server.jar indirilemedi! Sebep: {str(e)}\n")
        if 'jar_path' in locals() and os.path.exists(jar_path):
            try: os.remove(jar_path)
            except: pass

@app.route('/setup-wizard', methods=['GET', 'POST'])
@login_required
def setup_wizard():
    username = session['username']
    if not users[username].get('has_selected_plan'):
        return redirect('/select-plan')
    
    if request.method == 'POST':
        server_name = request.form.get('server_name')
        platform = request.form.get('platform')
        version = request.form.get('version')
        software = request.form.get('software')
        java_v = request.form.get('java_version')
        
        user_dir = get_user_dir(username)
        os.makedirs(user_dir, exist_ok=True)
        
        # Seçimleri kaydet
        users[username]['server_config'] = {
            "name": server_name,
            "platform": platform,
            "version": version,
            "software": software,
            "java_version": java_v
        }
        
        # Temel dosyalar
        accept_eula(username)
        write_server_properties(username, {
            "server-name": server_name,
            "motd": f"A Minecraft Server - {server_name}",
            "level-name": "world",
            "online-mode": "false"
        })

        users[username]['server_ready'] = True
        save_users()
        logging.info(f"Sunucu kurulumu tamamlandı ({username}): {server_name}")
        
        # Arka planda indirmeyi başlat
        threading.Thread(target=download_server_jar_thread, args=(software, version, user_dir, username), daemon=True).start()
        
        return redirect('/dashboard')

    return render_template_string(setup_wizard_html)

select_plan_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Skyvex Hosting - Plan Seçimi</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700;900&display=swap" rel="stylesheet">
    <style>
        :root { --primary: #6366f1; --primary-dark: #4338ca; --green: #10b981; --dark: #0a0a14; --card: #1a1a2e; }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Outfit', sans-serif;
            background: var(--dark);
            color: white; min-height: 100vh; padding: 2rem;
            display: flex; flex-direction: column; align-items: center; justify-content: flex-start; gap: 0;
        }
        .breadcrumb { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 2.5rem; margin-top: 1rem; font-size: 0.85rem; color: #555; }
        .breadcrumb .step { padding: 4px 14px; border-radius: 50px; border: 1px solid #333; }
        .breadcrumb .step.active { background: var(--primary); border-color: var(--primary); color: white; }
        .breadcrumb .step.done { border-color: var(--green); color: var(--green); }
        .breadcrumb .sep { color: #333; }
        .page { width: 100%; max-width: 950px; animation: fadeIn 0.4s ease; }
        .page.hidden { display: none; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: translateY(0); } }
        h1 { font-size: 2rem; font-weight: 900; text-align: center; margin-bottom: 0.4rem;
            background: linear-gradient(135deg, #818cf8, #34d399); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .subtitle { text-align: center; color: #666; font-size: 0.95rem; margin-bottom: 2rem; }
        .cat-row { display: flex; gap: 1.5rem; justify-content: center; flex-wrap: wrap; }
        .cat-card {
            background: var(--card); border-radius: 24px; padding: 2.5rem 2rem;
            width: 280px; text-align: center; border: 2px solid #1e1e3a;
            cursor: pointer; transition: 0.35s; position: relative;
        }
        .cat-card:hover { transform: translateY(-8px); }
        .cat-card.free-cat { border-color: var(--green); box-shadow: 0 0 30px rgba(16,185,129,0.15); }
        .cat-card.free-cat:hover { box-shadow: 0 20px 50px rgba(16,185,129,0.25); }
        .cat-card.paid-cat { border-color: #3d3d5c; }
        .cat-card.paid-cat:hover { border-color: var(--primary); box-shadow: 0 20px 50px rgba(99,102,241,0.2); }
        .cat-icon { font-size: 3.5rem; margin-bottom: 1rem; }
        .cat-card h2 { font-size: 1.5rem; font-weight: 800; margin-bottom: 0.5rem; }
        .cat-price { font-size: 2.5rem; font-weight: 900; margin: 0.75rem 0; }
        .cat-price small { font-size: 1rem; font-weight: 400; color: #777; }
        .cat-specs { display: flex; flex-direction: column; gap: 0.4rem; font-size: 0.9rem; color: #bbb; margin: 1rem 0; }
        .badge-247 { position: absolute; top: -13px; right: 18px; background: linear-gradient(135deg,#f59e0b,#d97706);
            color: white; font-size: 0.7rem; font-weight: 800; padding: 4px 12px; border-radius: 50px; letter-spacing: 1px; }
        .cat-btn { width: 100%; padding: 0.9rem; border: none; border-radius: 14px; font-weight: 700; font-size: 1rem;
            cursor: pointer; transition: 0.3s; margin-top: 0.75rem; }
        .cat-btn-free { background: linear-gradient(135deg, var(--green), #059669); color: white; }
        .cat-btn-paid { background: linear-gradient(135deg, var(--primary), var(--primary-dark)); color: white; }
        .pkg-grid { display: flex; gap: 1.2rem; flex-wrap: wrap; justify-content: center; }
        .pkg-card {
            background: var(--card); border-radius: 20px; padding: 1.5rem;
            width: 210px; text-align: center; border: 2px solid #1e1e3a;
            cursor: pointer; transition: 0.35s; position: relative;
        }
        .pkg-card:hover { transform: translateY(-6px); }
        .pkg-card.selected { border-color: var(--primary); box-shadow: 0 0 30px rgba(99,102,241,0.3); }
        .pkg-icon { font-size: 2.2rem; margin-bottom: 0.5rem; }
        .pkg-name { font-weight: 800; font-size: 1rem; margin-bottom: 0.3rem; }
        .pkg-price { font-size: 1.6rem; font-weight: 900; }
        .pkg-price small { font-size: 0.8rem; color: #666; font-weight: 400; }
        .pkg-specs { font-size: 0.78rem; color: #999; margin-top: 0.6rem; line-height: 1.7; }
        .config-form { background: var(--card); border-radius: 24px; padding: 2rem; max-width: 500px; margin: 0 auto; border: 1px solid #1e1e3a; }
        .config-form h2 { font-size: 1.3rem; font-weight: 800; margin-bottom: 0.25rem; }
        .config-form p { color: #777; font-size: 0.85rem; margin-bottom: 1.5rem; }
        .selected-plan-badge { display: inline-flex; align-items: center; gap: 6px; background: rgba(99,102,241,0.12);
            border: 1px solid rgba(99,102,241,0.25); border-radius: 50px; padding: 4px 14px; font-size: 0.85rem; margin-bottom: 1.2rem; }
        .form-group { margin-bottom: 1.1rem; }
        .form-group label { display: block; font-size: 0.85rem; font-weight: 600; margin-bottom: 0.4rem; color: #ccc; }
        .form-group input, .form-group select {
            width: 100%; padding: 0.75rem 1rem; background: #12121e; border: 1px solid #2a2a40;
            border-radius: 12px; color: white; font-size: 0.95rem; font-family: 'Outfit', sans-serif; outline: none;
        }
        .form-group input:focus, .form-group select:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(99,102,241,0.15); }
        .form-group select option { background: #1a1a2e; }
        .submit-btn { width: 100%; padding: 1rem; border: none; border-radius: 14px; font-weight: 800; font-size: 1rem;
            cursor: pointer; margin-top: 0.5rem; transition: 0.3s; color: white; }
        .submit-btn-free { background: linear-gradient(135deg, var(--green), #059669); }
        .submit-btn-paid { background: linear-gradient(135deg, var(--primary), var(--primary-dark)); }
        .submit-btn:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(99,102,241,0.35); }
        .back-link { display: block; text-align: center; margin-top: 1.5rem; color: #aaa; font-size: 1rem; font-weight: 600; cursor: pointer; padding: 0.7rem 1.5rem; border: 1px solid #333; border-radius: 12px; transition: 0.3s; }
        .back-link:hover { background: rgba(255,255,255,0.06); color: white; border-color: #555; }
    </style>
</head>
<body>
    <div class="breadcrumb">
        <span class="step" id="bc1">1 · Plan Türü</span>
        <span class="sep">›</span>
        <span class="step" id="bc2">2 · Paket</span>
        <span class="sep">›</span>
        <span class="step" id="bc3">3 · Sunucu Ayarları</span>
    </div>

    <!-- STEP 1 -->
    <div class="page" id="step1">
        {% if error %}
        <div style="background: rgba(239, 68, 68, 0.1); border-left: 4px solid #ef4444; padding: 1.25rem; border-radius: 16px; margin-bottom: 2.5rem; color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.2); animation: shake 0.5s ease-in-out;">
            {% if error == 'paid_plans_locked' %}
                ⚠️ <b>Ödeme Sistemi Bakımda:</b> Ücretli paketler şu an kapalıdır. Lütfen devam etmek için sol taraftaki <b>Ücretsiz Plan</b> butonuna tıklayın.
            {% elif error == 'invalid_plan' %}
                ❌ <b>Hata:</b> Geçersiz bir plan seçimi yaptınız. Lütfen tekrar deneyin.
            {% else %}
                ⚠️ <b>Sistem Notu:</b> {{ error }}
            {% endif %}
        </div>
        <style>@keyframes shake { 0%, 100% { transform: translateX(0); } 25% { transform: translateX(-5px); } 75% { transform: translateX(5px); } }</style>
        {% endif %}
        <h1>Plan Seç</h1>
        <p class="subtitle">Skyvex Hosting — Minecraft Sunucu Paketleri</p>
        <div class="cat-row">
            <div class="cat-card free-cat" onclick="chooseFree()">
                <div class="cat-icon">🌐</div>
                <h2 style="color:#34d399;">Ücretsiz Plan</h2>
                <div class="cat-price" style="color:#34d399;">0 TL<small>/ay</small></div>
                <div class="cat-specs">
                    <div>⚡ 3 GB RAM</div>
                    <div>🔥 1 Çekirdek CPU</div>
                    <div>📂 20 GB SSD Depolama</div>
                    <div>⏰ Mevcut kaynaklar (sınırlı)</div>
                </div>
                <button class="cat-btn cat-btn-free">Seç →</button>
            </div>
            <div class="cat-card paid-cat" style="opacity: 0.7; cursor: not-allowed; position: relative; border-color: #4b5563;">
                <div class="badge-247" style="background: #4b5563; left: 18px; right: auto;">🔒 Kilitli</div>
                <div class="cat-icon" style="filter: grayscale(1);">💎</div>
                <h2 style="color:#94a3b8;">Ücretli Planlar</h2>
                <div class="cat-specs" style="margin-top: 1rem;">
                    <div>⚡ <strong>4 GB – 16 GB</strong> Yüksek RAM</div>
                    <div>🔥 <strong>1 – 3 Çekirdek</strong> Güçlü CPU</div>
                    <div>📂 <strong>50 GB – 100 GB</strong> SSD Depolama</div>
                    <div>✅ <strong>7/24</strong> Kesintisiz Çalışma</div>
                    <div>🚀 Öncelikli Destek &amp; Hız</div>
                </div>
                <button class="cat-btn" style="background: #4b5563; cursor: not-allowed;" disabled>Sistem Yakında Aktif</button>
            </div>
        </div>
    </div>

    <!-- STEP 2 -->
    <div class="page hidden" id="step2">
        <h1>Paket Seç</h1>
        <p class="subtitle">İhtiyacına uygun paketi seç, sonra sunucunu kur</p>
        <div class="pkg-grid">
            {% for plan_id, info in paid_plans.items() %}
            <div class="pkg-card" onclick="choosePkg('{{ plan_id }}','{{ info.emoji }}','{{ info.name }}','{{ info.color }}',this)">
                {% if info.ram_mb > 0 %}<div class="badge-247">7/24</div>{% endif %}
                <div class="pkg-icon">{{ info.emoji }}</div>
                <div class="pkg-name" style="color:{{ info.color }};">{{ info.name }}</div>
                <div class="pkg-price" style="color:{{ info.color }};">{{ info.price_tl }} TL<small>/ay</small></div>
                <div class="pkg-specs">
                    {% if info.ram_mb > 0 %}
                    ⚡ {{ (info.ram_mb // 1024) }} GB RAM<br>🔥 {{ info.cpu_cores }} Çekirdek<br>📂 {{ info.storage_gb }} GB SSD
                    {% else %}
                    🌟 Gönüllü Destek<br>❤️ Sunucu Gelişimi İçin<br>💎 Özel Rütbe & Teşekkür
                    {% endif %}
                </div>
            </div>
            {% endfor %}
        </div>
        <a class="back-link" onclick="goBack(1)">← Geri dön</a>
    </div>

    <!-- STEP 3 -->
    <div class="page hidden" id="step3">
        <form method="POST" class="config-form">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <input type="hidden" name="plan" id="plan-input" value="free">
            <div class="selected-plan-badge" id="plan-badge">🌐 Ücretsiz Plan</div>
            <h2>Sunucunu Kur</h2>
            <p>Sunucu adı ve sürüm bilgilerini gir, hazır olunca oluştur!</p>
            <div class="form-group">
                <label>🎮 Sunucu Adı</label>
                <input type="text" name="server_name" placeholder="Örn: Benim Sunucum" required>
            </div>
            <div class="form-group">
                <label>🏷️ Sunucu Türü</label>
                <select name="server_type">
                    <option value="genel" selected>Normal Sunucu (SMP / Survival)</option>
                    <option value="velocity">Velocity Proxy (Ağ Sunucusu)</option>
                    <option value="hub">Lobi / Hub Sunucusu</option>
                    <option value="boxpvp">BoxPvP Sunucusu</option>
                </select>
                <small style="color:#666; font-size:0.75rem; margin-top:4px; display:block;">ℹ️ Seçiminize göre en uygun kaynaklı sunucu atanacaktır.</small>
            </div>
            <div class="form-group">
                <label>📦 Minecraft Sürümü</label>
                <select name="version">
                    <option value="1.21.11">1.21.11</option>
                    <option value="1.21.10">1.21.10</option>
                    <option value="1.21.9">1.21.9</option>
                    <option value="1.21.8">1.21.8</option>
                    <option value="1.21.7">1.21.7</option>
                    <option value="1.21.6">1.21.6</option>
                    <option value="1.21.5">1.21.5</option>
                    <option value="1.21.4">1.21.4 (Latest)</option>
                    <option value="1.21.3">1.21.3</option>
                    <option value="1.21.2">1.21.2</option>
                    <option value="1.21.1">1.21.1 (Stable)</option>
                    <option value="1.21">1.21</option>
                    <option value="1.20.6">1.20.6</option>
                    <option value="1.20.4" selected>1.20.4</option>
                    <option value="1.20.1">1.20.1</option>
                    <option value="1.20">1.20</option>
                    <option value="1.19.4">1.19.4</option>
                    <option value="1.19.2">1.19.2</option>
                    <option value="1.18.2">1.18.2</option>
                    <option value="1.17.1">1.17.1</option>
                    <option value="1.16.5">1.16.5</option>
                    <option value="1.12.2">1.12.2</option>
                    <option value="1.8.8">1.8.8</option>
                </select>
            </div>
            <div class="form-group">
                <label>⚙️ Sunucu Yazılımı</label>
                <select name="software">
                    <option value="paper">Paper (Önerilir)</option>
                    <option value="purpur">Purpur</option>
                    <option value="spigot">Spigot</option>
                    <option value="vanilla">Vanilla</option>
                    <option value="forge">Forge</option>
                </select>
            </div>
            <div class="form-group">
                <label>☕ Java Versiyonu</label>
                <select name="java_version">
                    <option value="21">Java 21</option>
                    <option value="17" selected>Java 17</option>
                    <option value="8">Java 8</option>
                </select>
            </div>
            <button type="submit" class="submit-btn" id="submit-btn">🚀 Sunucuyu Oluştur</button>
            <a class="back-link" id="back-from-config" onclick="goBackFromConfig()">← Geri dön</a>
        </form>
    </div>

    <script>
        var selectedCategory = null;

        function setStep(n) {
            ['step1','step2','step3'].forEach(function(id,i) {
                document.getElementById(id).classList.toggle('hidden', i+1 !== n);
            });
            ['bc1','bc2','bc3'].forEach(function(id,i) {
                var el = document.getElementById(id);
                el.classList.remove('active','done');
                if (i+1 < n) el.classList.add('done');
                else if (i+1 === n) el.classList.add('active');
            });
        }

        function chooseFree() {
            selectedCategory = 'free';
            document.getElementById('plan-input').value = 'free';
            document.getElementById('plan-badge').innerHTML = '🌐 Ücretsiz Plan';
            document.getElementById('submit-btn').className = 'submit-btn submit-btn-free';
            document.getElementById('submit-btn').textContent = '🚀 Sunucuyu Oluştur';
            setStep(3);
        }

        function showPaidPlans() {
            selectedCategory = 'paid';
            setStep(2);
        }

        function choosePkg(plan, emoji, name, color, el) {
            document.querySelectorAll('.pkg-card').forEach(function(c){ c.classList.remove('selected'); });
            el.classList.add('selected');
            document.getElementById('plan-input').value = plan;
            document.getElementById('plan-badge').innerHTML = emoji + ' ' + name;
            document.getElementById('submit-btn').className = 'submit-btn submit-btn-paid';
            document.getElementById('submit-btn').textContent = '💳 Ödemeye Geç';
            setStep(3);
        }

        function goBack(toStep) { setStep(toStep); }
        function goBackFromConfig() { setStep(selectedCategory === 'paid' ? 2 : 1); }

        setStep(1);
    </script>
</body>
</html>
"""

payment_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Skyvex Hosting - Güvenli Bağış</title>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {
            --primary: #6366f1;
            --primary-glow: rgba(99, 102, 241, 0.5);
            --bg: #030712;
            --card: #111827;
            --border: rgba(255, 255, 255, 0.08);
            --text-muted: #9ca3af;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Plus Jakarta Sans', sans-serif;
            background-color: var(--bg);
            background-image: 
                radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.15) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(16, 185, 129, 0.1) 0px, transparent 50%);
            color: white;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }

        .payment-card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 24px;
            width: 100%;
            max-width: 520px;
            padding: 40px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            position: relative;
            overflow: hidden;
        }

        .payment-card::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0; height: 4px;
            background: linear-gradient(90deg, var(--primary), #10b981);
        }

        .header { text-align: center; margin-bottom: 30px; }
        .header h1 { font-size: 1.8rem; font-weight: 800; margin-bottom: 8px; }
        .header p { color: var(--text-muted); font-size: 0.95rem; }

        .plan-summary {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 20px;
            margin-bottom: 25px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .plan-info h3 { font-size: 1.1rem; color: #fff; margin-bottom: 4px; }
        .plan-info span { color: var(--text-muted); font-size: 0.85rem; }
        .plan-price { font-size: 1.5rem; font-weight: 800; color: #10b981; }

        .tos-box {
            background: #000;
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 15px;
            height: 160px;
            overflow-y: auto;
            font-size: 0.8rem;
            line-height: 1.6;
            color: #d1d5db;
            margin-bottom: 20px;
            scrollbar-width: thin;
            scrollbar-color: var(--primary) transparent;
        }

        .tos-box::-webkit-scrollbar { width: 4px; }
        .tos-box::-webkit-scrollbar-thumb { background: var(--primary); border-radius: 10px; }

        .tos-title { font-weight: 700; color: var(--primary); margin-bottom: 8px; display: block; text-transform: uppercase; font-size: 0.75rem; }

        .checkbox-container {
            display: flex;
            align-items: flex-start;
            gap: 12px;
            margin-bottom: 30px;
            cursor: pointer;
            user-select: none;
        }

        .checkbox-container input { display: none; }

        .custom-checkbox {
            width: 22px; height: 22px;
            border: 2px solid var(--border);
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: 0.2s;
            flex-shrink: 0;
            margin-top: 2px;
        }

        .checkbox-container:hover .custom-checkbox { border-color: var(--primary); }

        .checkbox-container input:checked + .custom-checkbox {
            background: var(--primary);
            border-color: var(--primary);
            box-shadow: 0 0 10px var(--primary-glow);
        }

        .custom-checkbox i { color: white; display: none; font-size: 0.75rem; }
        .checkbox-container input:checked + .custom-checkbox i { display: block; }

        .checkbox-text { color: #d1d5db; font-size: 0.85rem; line-height: 1.4; }

        .pay-button {
            width: 100%;
            padding: 16px;
            border: none;
            border-radius: 14px;
            background: linear-gradient(135deg, var(--primary), #4f46e5);
            color: white;
            font-size: 1rem;
            font-weight: 700;
            cursor: pointer;
            transition: 0.3s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            opacity: 0.5;
            pointer-events: none;
        }

        .pay-button.active {
            opacity: 1;
            pointer-events: auto;
            box-shadow: 0 10px 25px -5px rgba(99, 102, 241, 0.4);
        }

        .pay-button.active:hover {
            transform: translateY(-2px);
            filter: brightness(1.1);
        }

        .secure-badge {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            margin-top: 20px;
            color: var(--text-muted);
            font-size: 0.75rem;
        }

        .secure-badge i { color: #10b981; }

        .back-link {
            display: block;
            text-align: center;
            margin-top: 25px;
            color: var(--text-muted);
            text-decoration: none;
            font-size: 0.85rem;
            transition: 0.2s;
        }
        .back-link:hover { color: white; }

        /* Loading Spinner */
        .spinner {
            width: 20px; height: 20px;
            border: 3px solid rgba(255,255,255,0.3);
            border-radius: 50%;
            border-top-color: #fff;
            animation: spin 1s linear infinite;
            display: none;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        .pay-button.loading .spinner { display: block; }
        .pay-button.loading span { display: none; }
        .pay-button.loading i { display: none; }
    </style>
</head>
<body>
    <div class="payment-card">
        <div class="header">
            <h1>Destek Ol</h1>
            <p>Bize destek olmak için bağış yapabilirsiniz.</p>
        </div>

        <div class="plan-summary">
            <div class="plan-info">
                <h3>{{ plan_info.name }}</h3>
                {% if plan_info.ram_mb > 0 %}
                <span>{{ plan_info.ram_mb // 1024 }}GB RAM · {{ plan_info.cpu_cores }} CPU · {{ plan_info.storage_gb }}GB SSD</span>
                {% else %}
                <span>Gönüllü Destek Bağışı</span>
                {% endif %}
            </div>
            <div class="plan-price">{{ plan_info.price_tl }} TL</div>
        </div>

        <div class="tos-box">
            <span class="tos-title">Aydınlatma Metni ve Sorumluluk Reddi</span>
            <p><strong>1. Gönüllü Bağış Beyanı:</strong> Kullanıcı, yaptığı ödemenin bir 'satın alma' değil, projenin devamlılığı için yapılmış 'gönüllü bir bağış' olduğunu, bu bağış karşılığında herhangi bir fiziksel veya dijital ürün/hizmet talep etmeyeceğini kabul eder.</p><br>
            <p><strong>2. İade Edilemezlik:</strong> Kripto para transferlerinin geri döndürülemez doğası gereği, yapılan bağışların hiçbir koşulda iadesi (chargeback/refund) talep edilemez.</p><br>
            <p><strong>3. Varlık Kaynağı Sorumluluğu:</strong> Bağışçı, kullandığı kartın ve fonların tamamen yasal yollarla elde edildiğini, 5549 sayılı 'Suç Gelirlerinin Aklanmasının Önlenmesi' kanunu kapsamındaki tüm sorumluluğun kendisine ait olduğunu beyan eder. Bu site, kullanıcıların fon kaynaklarını denetleme yükümlülüğüne sahip değildir.</p><br>
            <p><strong>4. Hizmet Sorumluluğu:</strong> Bu panel üzerinden kurulan Minecraft sunucularında dönen sohbet, içerik veya yasa dışı faaliyetlerden site sahibi sorumlu tutulamaz. Site sadece bir teknik arayüz sağlayıcısıdır.</p><br>
            <p><strong>5. Yaş Sınırı:</strong> Kullanıcı, bu işlemi yaparak 18 yaşından büyük olduğunu veya velisinin izniyle bu işlemi gerçekleştirdiğini beyan eder.</p>
        </div>

        <label class="checkbox-container">
            <input type="checkbox" id="tos_check" onchange="toggleButton()">
            <div class="custom-checkbox">
                <i class="fas fa-check"></i>
            </div>
            <span class="checkbox-text">Aydınlatma Metnini okudum, anladım ve belirtilen şartlar altındaki bağış yapmayı kabul ediyorum.</span>
        </label>

        <form id="paymentForm" action="/api/create-ltc-payment" method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <input type="hidden" name="plan" value="{{ plan }}">
            <button type="submit" id="payBtn" class="pay-button">
                <i class="fas fa-credit-card"></i>
                <span>Kartla Bağış Yap (LTC)</span>
                <div class="spinner"></div>
            </button>
        </form>

        <div class="secure-badge">
            <i class="fas fa-shield-halved"></i>
            NOWPayments Fiat-to-Crypto Güvencesiyle
        </div>

        <a href="/select-plan" class="back-link">Vazgeç ve Geri Dön</a>
    </div>

    <script>
        function toggleButton() {
            const check = document.getElementById('tos_check');
            const btn = document.getElementById('payBtn');
            if(check.checked) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        }

        document.getElementById('paymentForm').onsubmit = function() {
            const btn = document.getElementById('payBtn');
            btn.classList.add('loading');
            btn.style.pointerEvents = 'none';
        };
    </script>
</body>
</html>
"""

setup_wizard_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>Skyvex Hosting - Kurulum Sihirbazı</title>
    <style>
        :root { --primary: #6366f1; --dark: #1a1a1a; }
        body { font-family: 'Segoe UI', sans-serif; background: var(--dark); color: white; padding: 2rem; display: flex; justify-content: center; }
        form { background: #2d2d2d; padding: 2rem; border-radius: 20px; width: 100%; max-width: 500px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
        h1 { margin-bottom: 1.5rem; text-align: center; color: var(--primary); }
        .form-group { margin-bottom: 1.2rem; }
        label { display: block; margin-bottom: 0.5rem; font-weight: 600; font-size: 0.9rem; }
        select, input { width: 100%; padding: 0.8rem; background: #3d3d3d; border: 1px solid #444; border-radius: 10px; color: white; outline: none; }
        select:focus, input:focus { border-color: var(--primary); }
        button { width: 100%; padding: 1rem; background: var(--primary); border: none; border-radius: 10px; color: white; cursor: pointer; font-weight: 700; font-size: 1rem; margin-top: 1rem; }
    </style>
</head>
<body>
    <form method="POST">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <h1>Sunucu Kurulumu</h1>
        <div class="form-group"><label>Sunucu Adı</label><input type="text" name="server_name" placeholder="Benim Sunucum" required></div>
        <div class="form-group"><label>Platform</label><select name="platform"><option value="java">Minecraft: Java Edition</option><option value="bedrock">Minecraft: Bedrock Edition</option></select></div>
        <div class="form-group"><label>Sürüm</label><select name="version"><option value="1.21.11">1.21.11</option><option value="1.21.10">1.21.10</option><option value="1.21.9">1.21.9</option><option value="1.21.8">1.21.8</option><option value="1.21.7">1.21.7</option><option value="1.21.6">1.21.6</option><option value="1.21.5">1.21.5</option><option value="1.21.4">1.21.4 (Latest)</option><option value="1.21.3">1.21.3</option><option value="1.21.2">1.21.2</option><option value="1.21.1">1.21.1</option><option value="1.21">1.21</option><option value="1.20.6">1.20.6</option><option value="1.20.4">1.20.4</option><option value="1.20.1">1.20.1</option><option value="1.20">1.20</option><option value="1.19.4">1.19.4</option><option value="1.19.2">1.19.2</option><option value="1.18.2">1.18.2</option><option value="1.17.1">1.17.1</option><option value="1.16.5">1.16.5</option><option value="1.12.2">1.12.2</option><option value="1.8.8">1.8.8</option></select></div>
        <div class="form-group"><label>Sunucu Yazılımı</label><select name="software" onchange="updateSoftwareSelection(this)"><option value="paper">Paper (Önerilir)</option><option value="spigot">Spigot</option><option value="vanilla">Vanilla</option><option value="purpur">Purpur</option><option value="forge">Forge</option><option value="fabric">Fabric</option></select></div>

        <div class="form-group"><label>Java Versiyonu</label><select name="java_version"><option value="17">Java 17</option><option value="21">Java 21</option><option value="8">Java 8</option></select></div>
        <button type="submit">Sunucuyu Oluştur ve Başlat 🚀</button>
    </form>
</body>
</html>
"""

@app.route('/admin')
@admin_required
def admin_dashboard():
    user_list = []
    for uname, udata in users.items():
        if udata.get('role') == 'admin': continue
        status = "Çalışıyor" if user_processes.get(uname) else "Kapalı"
        user_list.append({
            "username": uname,
            "status": status,
            "created_at": udata.get('created_at', 'N/A')[:10],
            "is_plus": 'plus+' in udata.get('tags', []),
            "ram_mb": udata.get('resources', {}).get('ram_mb', 3072) if udata.get('resources') else 3072,
            "cpu_cores": udata.get('resources', {}).get('cpu_cores', 1) if udata.get('resources') else 1,
            "storage_gb": udata.get('resources', {}).get('storage_gb', 50) if udata.get('resources') else 50
        })
    return render_template_string(admin_dashboard_html, user_list=user_list)

@app.route('/admin/toggle_plus/<username>', methods=['POST'])
@admin_required
def admin_toggle_plus(username):
    if username in users:
        if 'tags' not in users[username]:
            users[username]['tags'] = []
        
        if 'plus+' in users[username]['tags']:
            users[username]['tags'].remove('plus+')
        else:
            users[username]['tags'].append('plus+')
        
        save_users()
        logging.info(f"Plus+ durumu değiştirildi: {username}")
    return redirect('/admin')

@app.route('/admin/manage/<username>')
@admin_required
def admin_manage_user(username):
    if username in users:
        session['selected_server'] = username
        logging.info(f"Admin {session.get('username')} kullanıcısı {username} sunucusunu yönetmeye başladı.")
        return redirect('/dashboard')
    return redirect('/admin?error=not_found')
    
@app.route('/admin/update_resources/<username>', methods=['POST'])
@admin_required
def admin_update_resources(username):
    if username in users:
        ram_mb = int(request.form.get('ram_mb', 3072))
        cpu_cores = int(request.form.get('cpu_cores', 1))
        storage_gb = int(request.form.get('storage_gb', 50))
        
        if "resources" not in users[username] or users[username]["resources"] is None:
            users[username]["resources"] = {}
            
        users[username]["resources"]["ram_mb"] = ram_mb
        users[username]["resources"]["cpu_cores"] = cpu_cores
        users[username]["resources"]["storage_gb"] = storage_gb
        
        save_users()
        logging.info(f"Yönetici {session.get('username')}, {username} kaynaklarını güncelledi: RAM={ram_mb}, CPU={cpu_cores}, Disk={storage_gb}")
        return redirect('/admin?success=1')
    return redirect('/admin?error=not_found')

@app.route('/admin/delete_user/<username>', methods=['POST'])
@admin_required
def admin_delete_user(username):
    if username == "admin":
        return redirect('/admin?error=cannot_delete_admin')
        
    if username in users:
        # 1. Sunucuyu durdur (Varsa)
        proc = user_processes.get(username)
        if proc:
            if proc != "REMOTE":
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except: pass
            else:
                # Uzak sunucuyu durdurma isteği gönder
                node_id = users[username].get('node_id')
                if node_id and node_id in nodes:
                    try:
                        node_url = nodes[node_id]['info'].get('public_url') or f"http://{nodes[node_id]['info'].get('ip')}:7860"
                        requests.post(f"{node_url}/exec", json={"action": "stop", "username": username, "secret": NODE_SECRET}, timeout=5)
                    except: pass

        # 2. Dosyaları tamamen sil (eula dahil)
        user_dir = get_user_dir(username)
        if os.path.exists(user_dir):
            try:
                # Salt-okunur dosyaları yazabilir yap
                for root_d, dirs_d, files_d in os.walk(user_dir):
                    for fname in files_d:
                        fpath = os.path.join(root_d, fname)
                        try: os.chmod(fpath, 0o666)
                        except: pass
                shutil.rmtree(user_dir, ignore_errors=True)
                logging.info(f"Admin: {username} dosyaları tamamen silindi.")
            except Exception as e:
                logging.error(f"Dosya silme hatası ({username}): {e}")

        # 3. Kullanıcıyı listeden sil
        del users[username]
        save_users()
        
        # 4. Belleği temizle
        if username in user_processes: del user_processes[username]
        if username in user_console_queues: del user_console_queues[username]
        if username in user_console_histories: del user_console_histories[username]
        
        logging.info(f"Yönetici {session.get('username')}, {username} sunucusunu tamamen sildi.")
        return redirect('/admin?success=1')
    return redirect('/admin?error=not_found')

admin_dashboard_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>Skyvex Hosting - Admin Panel</title>
    <style>
        :root { --primary: #6366f1; --dark: #0f172a; --card: #1e293b; }
        body { font-family: 'Segoe UI', sans-serif; background: var(--dark); color: white; padding: 2rem; margin: 0; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { color: var(--primary); margin-bottom: 2rem; display: flex; align-items: center; gap: 15px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 1.5rem; }
        .card { background: var(--card); padding: 1.5rem; border-radius: 16px; border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 10px 25px rgba(0,0,0,0.2); transition: 0.3s; }
        .card:hover { transform: translateY(-5px); border-color: var(--primary); }
        .card h3 { margin-top: 0; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 10px; margin-bottom: 15px; }
        .status { padding: 4px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: bold; text-transform: uppercase; }
        .status-on { background: rgba(16, 185, 129, 0.2); color: #10b981; }
        .status-off { background: rgba(239, 68, 68, 0.2); color: #ef4444; }
        .resource-group { background: rgba(0,0,0,0.2); padding: 12px; border-radius: 12px; margin: 15px 0; }
        .resource-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; font-size: 0.9rem; }
        .resource-input { width: 80px; padding: 5px; background: #334155; border: 1px solid #475569; border-radius: 5px; color: white; text-align: center; }
        .btn { display: inline-block; padding: 0.6rem 1rem; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 0.85rem; border: none; cursor: pointer; transition: 0.3s; text-align: center; }
        .btn-manage { background: #3b82f6; color: white; flex: 1; }
        .btn-plus { background: #10b981; color: white; flex: 1; }
        .btn-plus.off { background: #f59e0b; }
        .btn-save { background: var(--primary); color: white; width: 100%; margin-top: 10px; }
        .btn-delete { background: #ef4444; color: white; width: 100%; margin-top: 10px; opacity: 0.7; }
        .btn-delete:hover { opacity: 1; }
        .card-actions { display: flex; gap: 10px; margin-top: 15px; }
        .label { color: #94a3b8; font-size: 0.85rem; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 Yönetici Kontrol Paneli <a href="/dashboard" style="font-size: 0.9rem; color: #94a3b8; font-weight: normal; margin-left: auto;">Panelime Dön</a></h1>
        
        {% if request.args.get('success') %}
        <div style="background: rgba(16, 185, 129, 0.2); border: 1px solid #10b981; color: #10b981; padding: 1rem; border-radius: 10px; margin-bottom: 1.5rem;">
            ✅ İşlem başarıyla gerçekleştirildi.
        </div>
        {% endif %}

        <div class="grid">
            {% for user in user_list %}
            <div class="card">
                <h3>👤 {{ user.username }}</h3>
                <div class="resource-row">
                    <span class="label">Durum:</span>
                    <span class="status {% if user.status == 'Çalışıyor' %}status-on{% else %}status-off{% endif %}">{{ user.status }}</span>
                </div>
                <div class="resource-row">
                    <span class="label">Kayıt Tarihi:</span>
                    <span>{{ user.created_at }}</span>
                </div>
                
                <form action="/admin/update_resources/{{ user.username }}" method="POST" class="resource-group">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <div class="resource-row">
                        <span class="label">💾 RAM (MB)</span>
                        <input type="number" name="ram_mb" class="resource-input" value="{{ user.ram_mb }}" step="256" min="512">
                    </div>
                    <div class="resource-row">
                        <span class="label">⚡ CPU (Çekirdek)</span>
                        <input type="number" name="cpu_cores" class="resource-input" value="{{ user.cpu_cores }}" min="1" max="8">
                    </div>
                    <div class="resource-row">
                        <span class="label">📦 Depolama (GB)</span>
                        <input type="number" name="storage_gb" class="resource-input" value="{{ user.storage_gb }}" min="1">
                    </div>
                    <button type="submit" class="btn btn-save">💾 Kaynakları Güncelle</button>
                </form>

                <div class="card-actions">
                    <a href="/admin/manage/{{ user.username }}" class="btn btn-manage">🎮 Yönet</a>
                    
                    <form action="/admin/toggle_plus/{{ user.username }}" method="POST" style="flex: 1;">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                        <button type="submit" class="btn btn-plus {% if user.is_plus %}off{% endif %}" style="width: 100%;">
                            {{ '⭐ Plus+ Kapat' if user.is_plus else '⭐ Plus+ Ver' }}
                        </button>
                    </form>
                </div>

                <form action="/admin/delete_user/{{ user.username }}" method="POST" onsubmit="return confirm('⚠️ DİKKAT: bu sunucuyu ve TÜM dosyalarını kalıcı olarak silmek istediğinize emin misiniz?');">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <button type="submit" class="btn btn-delete">🗑️ Sunucuyu Kökten Sil</button>
                </form>
            </div>
            {% endfor %}
        </div>
    </div>
</body>
</html>
"""

def generate_access_code(ram_mb, cpu_cores, storage_gb, hours):
    """8 basamaklı erişim kodu oluştur"""
    code = ''.join(secrets.choice('0123456789') for _ in range(8))
    
    # Kullanıcı bilgilerini oluştur
    username = "skyvex"
    password = code
    
    # Kullanım süresini hesapla
    expires_at = datetime.now() + timedelta(hours=hours)
    
    # Kullanıcıyı kaydet
    users[username] = {
        "password": hash_password(password),
        "role": "user",
        "created_at": datetime.now().isoformat(),
        "expires_at": expires_at.isoformat(),
        "resources": {
            "ram_mb": ram_mb,
            "cpu_cores": cpu_cores,
            "storage_gb": storage_gb
        }
    }
    
    save_users()
    logging.info(f"Yeni erişim kodu oluşturuldu: {code}, Süre: {hours} saat")
    return code

# --- Yardımcı Fonksiyonlar ---

def auto_setup_java(preferred_version="21"):
    """Java yoksa otomatik indir ve kur. 3GB RAM desteği için Java 21 LTS."""
    # --- Mutlak yollar kullanıyoruz ---
    base_dir = os.path.abspath(os.path.dirname(__file__))
    jre_dir = os.path.join(base_dir, UPLOAD_FOLDER, "jre")
    
    # --- ÖNCELİKLİ: Sistemde zaten Java var mı? ---
    sys_java = shutil.which("java") or shutil.which("java.exe")
    if sys_java: return sys_java

    java_bin_name = "java.exe" if os.name == 'nt' else "java"
    java_bin = os.path.join(jre_dir, "bin", java_bin_name)

    # Kurulu mu kontrol et
    if os.path.exists(java_bin): return java_bin

    try:
        os.makedirs(jre_dir, exist_ok=True)
        logging.info(f"Java {preferred_version} indiriliyor...")
        
        if os.name == 'nt':
            url = "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.1%2B12/OpenJDK21U-jre_x64_windows_hotspot_21.0.1_12.zip"
            archive_name = "jre21.zip"
        else:
            url = "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.1%2B12/OpenJDK21U-jre_x64_linux_hotspot_21.0.1_12.tar.gz"
            archive_name = "jre21.tar.gz"

        archive_path = os.path.join(base_dir, UPLOAD_FOLDER, archive_name)
        urllib.request.urlretrieve(url, archive_path)

        # Çıkart
        if archive_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path, 'r') as zf: zf.extractall(jre_dir)
        else:
            import tarfile
            with tarfile.open(archive_path, 'r:gz') as tf: tf.extractall(jre_dir)

        # Klasör yapısını düzelt (bin/java nerede onu bul)
        for root, dirs, files in os.walk(jre_dir):
            if java_bin_name in files:
                found_bin = os.path.join(root, java_bin_name)
                # bin/java bir üst dizinde ise dosyaları jre root'a taşıyalım
                parent_dir = os.path.dirname(os.path.dirname(found_bin))
                if parent_dir != jre_dir and parent_dir.startswith(jre_dir):
                    for item in os.listdir(parent_dir):
                        src = os.path.join(parent_dir, item)
                        dst = os.path.join(jre_dir, item)
                        if not os.path.exists(dst): shutil.move(src, dst)
                break

        if os.path.exists(java_bin) and os.name != 'nt':
            os.chmod(java_bin, 0o755)
            
        return java_bin if os.path.exists(java_bin) else "java"
    except Exception as e:
        logging.error(f"Java kurulum hatası: {e}")
        return "java"

def find_java_path():
    """Java yolunu mutlak olarak bul"""
    base_dir = os.path.abspath(os.path.dirname(__file__))
    
    # 0) Env Var
    jv = os.environ.get("JAVA_PATH")
    if jv and os.path.exists(jv): return jv

    # 1) System PATH
    js = shutil.which("java") or shutil.which("java.exe")
    if js: return js

    # 2) Local JRE
    java_bin_name = "java.exe" if os.name == 'nt' else "java"
    local_java = os.path.join(base_dir, UPLOAD_FOLDER, "jre", "bin", java_bin_name)
    if os.path.exists(local_java):
        if os.name != 'nt': os.chmod(local_java, 0o755)
        return local_java

    # 3) Auto-Setup
    return auto_setup_java()

def get_selected_server(username):
    """Kullanıcının şu an yönetmekte olduğu sunucuyu döner."""
    if not username: return None
    # Session içinde seçili sunucu varsa ve kullanıcı erişebiliyorsa onu döndür
    selected = session.get('selected_server')
    if selected:
        # Kendi sunucusu mu?
        if selected == username: return selected
        # Admin mi?
        if users.get(username, {}).get('role') == 'admin': return selected
        # Paylaşılan erişimi var mı?
        if username in users.get(selected, {}).get('shared_access', []):
            return selected
    
    # Varsayılan olarak kendi sunucusu
    return username

def has_permission(username, target_server, perm):
    """Kullanıcının belirli bir sunucuda işlem yapma yetkisi var mı?"""
    if not username or not target_server: return False
    
    # Kendi sunucusu ise tam yetki
    if username == target_server: return True
    
    # Admin ise tam yetki
    if users.get(username, {}).get('role') == 'admin': return True
    
    # Paylaşılan erişimi kontrol et
    shared_access = users.get(target_server, {}).get('shared_access', {})
    if isinstance(shared_access, dict):
        perms = shared_access.get(username, [])
        return perm in perms
    elif isinstance(shared_access, list):
        # Eski format (list) ise varsayılan yetkiler (start, stop, console)
        if username in shared_access:
            return perm in ["start", "stop", "console"]
            
    return False

def jar_exists(username):
    return os.path.exists(get_user_file_path(username, SERVER_JAR_NAME))

def accept_eula(username):
    """EULA'yı otomatik olarak kabul et"""
    try:
        path = get_user_file_path(username, "eula.txt")
        with open(path, "w", encoding='utf-8') as f:
            f.write("eula=true\n")
        return True
    except Exception as e:
        logging.error(f"EULA oluşturma hatası: {e}")
        return False

def check_eula(username):
    path = get_user_file_path(username, "eula.txt")
    if not os.path.exists(path):
        return accept_eula(username)
    try:
        with open(path, "r", encoding='utf-8') as f:
            content = f.read().lower()
            if "eula=true" in content.replace(' ', '').replace('\n', '').replace('\t', '').replace('\r', ''):
                return True
            else:
                return accept_eula(username)
    except Exception as e:
        logging.error(f"EULA kontrol hatası: {e}")
        return accept_eula(username)


# --- YENİ: Bore Tünel (bore.pub) Yardımcı Fonksiyonları ---

# --- Node API ---
@app.route("/api/node_report", methods=["POST"])
@limiter.exempt
@csrf.exempt
def node_report():
    """Compute Node'lardan (sistem.py) gelen raporları işler"""
    data = request.json
    if not data or data.get('secret') != NODE_SECRET:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    
    node_id = data.get('node_id', 'unknown')
    nodes[node_id] = {
        'last_seen': time.time(),
        'info': data
    }
    return jsonify({"status": "success"})

@app.route("/api/sync/<username>")
@csrf.exempt
def api_sync_user(username):
    """Kullanıcı dosyalarını ZIP olarak Node'a RAM üzerinden servis eder (Disk alanından tasarruf)"""
    secret = request.args.get('secret')
    if secret != NODE_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    
    if username not in users:
        return jsonify({"error": "User not found"}), 404
        
    user_dir = get_user_dir(username)
    if not os.path.exists(user_dir):
        return jsonify({"error": "Directory not found"}), 404

    import io
    # Dosyayı diske yazmak yerine RAM'de (BytesIO) oluşturuyoruz
    memory_file = io.BytesIO()
    
    try:
        total_size = 0
        file_count = 0
        limit_reached = False
        
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(user_dir):
                if limit_reached: break
                
                # AGRESİF KLASÖR ENGELLEME
                exclude_dirs = ["jre", "__pycache__", ".git", "cache", "logs", "crash-reports", "backups", "temp", "tmp", "coreprotect", "dynmap"]
                if any(x in root.lower() for x in exclude_dirs):
                    continue
                    
                for file in files:
                    # TOPLAM LİMİT KONTROLÜ (150MB'ta dur!)
                    if total_size > 150 * 1024 * 1024:
                        limit_reached = True
                        break

                    # AGRESİF DOSYA ENGELLEME
                    if file.endswith(('.log', '.gz', '.zip', '.rar', '.tar', '.db', '.sql', '.sqlite', '.db-wal', '.db-shm', '.backup', '.tmp')):
                        if file != 'latest.log': continue
                    
                    abs_path = os.path.join(root, file)
                    try:
                        f_size = os.path.getsize(abs_path)
                        
                        # ANA DOSYA İZNİ: server.jar, paper.jar vb. her zaman izin ver (30MB sınırını geçer)
                        is_core_jar = file.lower() in ["server.jar", "paper.jar", "spigot.jar", "velocity.jar", "purpur.jar", "waterfall.jar"]
                        if not is_core_jar and file.endswith(".jar") and any(x in file.lower() for x in ["paper", "spigot", "velocity", "purpur"]):
                            is_core_jar = True

                        if not is_core_jar and f_size > 30 * 1024 * 1024: 
                            continue
                        
                        total_size += f_size
                        rel_path = os.path.relpath(abs_path, user_dir)
                        zf.write(abs_path, rel_path)
                        file_count += 1
                    except: continue
        
        if limit_reached:
            logging.warning(f"⚠️ Sync 150MB limitine ulaştığı için tarama durduruldu.")
            
        logging.info(f"✅ Hızlı Sync Hazır: {file_count} dosya, ~{total_size // 1024 // 1024} MB")
        
        memory_file.seek(0)
        return flask.send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"{username}.zip"
        )
    except Exception as e:
        logging.error(f"RAM Sync ZIP hatası ({username}): {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/node_list")
@login_required
def node_list():
    """Aktif node'ları listeler (Admin için)"""
    if users.get(session.get('username'), {}).get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    
    active_nodes = {}
    now = time.time()
    for nid, ndata in nodes.items():
        if now - ndata['last_seen'] < 90: # 90 saniye içinde ses verdiyse aktif
            active_nodes[nid] = ndata
            
    return jsonify(active_nodes)

def download_bore():
    """Bore binary'sini indirir (yoksa)"""
    bore_bin = os.path.join(os.getcwd(), BORE_BINARY)
    if not os.path.exists(bore_bin):
        logging.info(f"📥 Bore Tünel indiriliyor: {BORE_URL}")
        try:
            archive_path = os.path.join(os.getcwd(), "bore_arch")
            urllib.request.urlretrieve(BORE_URL, archive_path)
            
            if BORE_URL.endswith(".zip"):
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    zip_ref.extractall(os.getcwd())
            else:
                import tarfile
                with tarfile.open(archive_path, 'r:gz') as tar_ref:
                    tar_ref.extractall(os.getcwd())
            
            # Binary'yi root'a çıkar
            for root_dir, dirs, files in os.walk(os.getcwd()):
                if BORE_BINARY in files and root_dir != os.getcwd():
                    shutil.move(os.path.join(root_dir, BORE_BINARY), os.getcwd())
                    break
            
            if os.path.exists(archive_path): os.remove(archive_path)
            if os.name != 'nt': os.chmod(bore_bin, 0o755)
            logging.info("✅ Bore başarıyla hazırlandı.")
            return True
        except Exception as e:
            logging.error(f"❌ Bore indirme hatası: {e}")
            return False
    return True

def start_bore_tunnel(username, local_port=25565):
    """Bore tüneli başlat"""
    if username in user_bore_processes and is_proc_active(user_bore_processes[username]):
        return True

    if not download_bore():
        return False

    user_dir = get_user_dir(username)
    log_path = os.path.join(user_dir, "bore.log")
    bore_bin = os.path.join(os.getcwd(), BORE_BINARY)
    
    try:
        # Eski logu temizle
        if os.path.exists(log_path): os.remove(log_path)
        
        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(
                [bore_bin, "local", str(local_port), "--to", "bore.pub"],
                stdout=log_file,
                stderr=log_file,
                cwd=user_dir,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == 'nt' else 0
            )
            user_bore_processes[username] = proc
            
            # Logdan adres parse etmek için thread
            def monitor_bore():
                for _ in range(30): # 30 saniye bekle
                    if os.path.exists(log_path):
                        with open(log_path, "r") as f:
                            content = f.read()
                            # bore.pub:12345 formatını yakala
                            match = re.search(r'bore\.pub:[0-9]+', content)
                            if match:
                                user_bore_address[username] = match.group(0)
                                logging.info(f"🔗 Bore adresi yakalandı ({username}): {match.group(0)}")
                                return
                    time.sleep(1)
                logging.error(f"❌ Bore adresi yakalanamadı ({username})")

            threading.Thread(target=monitor_bore, daemon=True).start()
            return True
    except Exception as e:
        logging.error(f"❌ Bore başlatma hatası ({username}): {e}")
        return False

def stop_bore_tunnel(username):
    """Bore tüneli durdur"""
    proc = user_bore_processes.get(username)
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except:
            try: proc.kill()
            except: pass
        user_bore_processes[username] = None
        user_bore_address[username] = None
        logging.info(f"🛑 Bore durduruldu ({username})")

def get_bore_info(username):
    """Kullanıcının tünel adresini ve durumunu döner (Node uyumlu)"""
    user_data = users.get(username, {})
    node_id = user_data.get('node_id')
    
    # Node üzerinde çalışıyorsa
    if node_id and node_id in nodes:
        node_info = nodes[node_id]['info']
        addr = node_info.get('tunnels', {}).get(username)
        active = user_processes.get(username) == "REMOTE"
        return addr, active
    
    # Yerel çalışıyorsa
    addr = user_bore_address.get(username)
    active = is_proc_active(user_bore_processes.get(username))
    return addr, active

def read_server_properties(username):
    props = {}
    path = get_user_file_path(username, "server.properties")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        props[k.strip()] = v.strip()
    except Exception as e:
        logging.error(f"server.properties okuma hatası: {e}")
    return props

def write_server_properties(username, props):
    path = get_user_file_path(username, "server.properties")
    try:
        lines = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        existing = {}
        for i, line in enumerate(lines):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = (i, v)
        for k, v in props.items():
            if k in existing:
                idx, _ = existing[k]
                lines[idx] = f"{k}={v}\n"
            else:
                lines.append(f"{k}={v}\n")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return True
    except Exception as e:
        logging.error(f"server.properties yazma hatası: {e}")
        return False

def get_server_port(username):
    # Öncelik: Panele atanmış port
    if users.get(username, {}).get('assigned_port'):
        return users[username]['assigned_port']
    # İkinci: server.properties'teki port
    props = read_server_properties(username)
    try:
        return int(props.get("server-port", "25565"))
    except: pass
    return "25565"

def is_proc_active(p):
    """Süreç veya REMOTE durumunu kontrol eden genel fonksiyon"""
    if p is None: return False
    if isinstance(p, str): return p == "REMOTE"
    try:
        return p.poll() is None
    except:
        return False

def stream_console(username):
    user_data = users.get(username, {})
    if username not in user_console_histories:
        user_console_histories[username] = []
    if username not in user_console_queues:
        user_console_queues[username] = queue.Queue(maxsize=1000)
    
    q = user_console_queues[username]
    h = user_console_histories[username]
    proc = user_processes.get(username)

    # Eğer sunucu bir node üzerinde çalışıyorsa, konsolu oradan çek
    node_id = user_data.get('node_id')
    if node_id and node_id in nodes:
        try:
            node_url = nodes[node_id]['info'].get('public_url') or f"http://{nodes[node_id]['info'].get('ip')}:7860"
            res = requests.post(f"{node_url}/exec", json={"action": "get_console", "username": username, "secret": NODE_SECRET}, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get('success'):
                    node_lines = data.get('history', []) or []

                    # Uzak konsoldan oyuncu giriş/çıkışlarını takip et (Oyuncular sayfası için)
                    track_players_from_lines(username, node_lines)

                    body = "".join(str(x) for x in node_lines)
                    # Panelin tek satırlık başlangıç mesajını koru, node'un kendi mesajlarını gösterme
                    starter = ""
                    if len(h) == 1 and "başlatılıyor" in h[0]:
                        starter = h[0]
                    return (starter + body) if starter else body
        except Exception as e:
            logging.error(f"Node konsol çekme hatası ({username} on {node_id}): {e}")

    if q.empty() and not proc:
        # Sunucu kapalı ve kuyruk boşsa, history'den son hali ver
        output = "".join(h)
        # Başlatma mesajı varsa "Kapalı" yazısı ekleme (node hâlâ hazırlanıyor olabilir)
        if "başlatılıyor" in output:
            return output
        if not output.strip().endswith("Kapalı."):
            output += f"\n[{time.strftime('%H:%M:%S')}] Sunucu Kapalı."
        return output

    while not q.empty():
        try:
            line = q.get_nowait()
            if isinstance(line, bytes):
                try:
                    line = line.decode(locale.getpreferredencoding(), 'ignore')
                except:
                    line = line.decode('utf-8', 'ignore')
            h.append(str(line))
        except queue.Empty:
            break

    if proc:
        # REMOTE status checking
        if isinstance(proc, str) and proc == "REMOTE":
            # Uzak sunucu için durum kontrolünü Heartbeat (api/node_report) yaptığı için 
            # burada ekstra bir şey yapmaya gerek yok, sadece h güncellenmiş mi diye bakarız.
            pass
        else:
            try:
                exit_code = proc.poll()
                if exit_code is not None:
                    error_msg = f"\n[{time.strftime('%H:%M:%S')}] UYARI: Sunucu kapandı. Kod: {exit_code}\n"
                    h.append(error_msg)
                    user_processes[username] = None
            except Exception as e:
                logging.error(f"Süreç kontrol hatası ({username}): {e}")

    user_console_histories[username] = h[-500:]
    return "".join(user_console_histories[username])

def enqueue_output(out, q, username=None):
    try:
        for line in iter(out.readline, b''):
            if line is None:
                continue
            try:
                decoded = line.decode(locale.getpreferredencoding(), 'ignore')
            except:
                decoded = line.decode('utf-8', 'ignore')
            
            # Etkinlik takibi: Sunucu açıldı, oyuncu girdi/çıktı
            if username:
                if username not in active_players:
                    active_players[username] = set()

                clean_line = ANSI_ESCAPE_RE.sub('', decoded).replace('\r', '')
                msg_lower = clean_line.lower()
                
                join_match = re.search(r"(?:^|\]:\s)([A-Za-z0-9_.]{3,32})\s+joined the game", clean_line)
                if join_match:
                    active_players[username].add(join_match.group(1))
                    server_activity[username] = time.time()
                    
                leave_match = re.search(r"(?:^|\]:\s)([A-Za-z0-9_.]{3,32})\s+(?:left the game|lost connection|logged out)", clean_line)
                if leave_match:
                    player_name = leave_match.group(1)
                    if player_name in active_players[username]:
                        active_players[username].remove(player_name)
                    server_activity[username] = time.time()
                
                # Yedek etkinlik takibi (sohbet vb)
                if "issued server command" in msg_lower or "logged in with entity id" in msg_lower:
                    server_activity[username] = time.time()

            try:
                q.put_nowait(clean_line if username else decoded)
            except queue.Full:
                try:
                    _ = q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(decoded)
                except queue.Full:
                    pass
    except Exception as e:
        logging.error(f"Konsol çıktısı yakalama hatası: {e}")
    try:
        out.close()
    except Exception as e:
        logging.error(f"Çıktı kapatma hatası: {e}")

def create_breadcrumbs(subpath):
    parts = subpath.split('/')
    breadcrumbs = [("Files", "")]
    current_path = ""
    for part in parts:
        if part:
            current_path = os.path.join(current_path, part)
            breadcrumbs.append((part, urllib.parse.quote(current_path)))
    return breadcrumbs

def secure_unzip(zip_path, target_dir):
    try:
        lower_path = zip_path.lower()
        if lower_path.endswith('.zip'):
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                base_dir = os.path.realpath(target_dir)
                for member in zip_ref.namelist():
                    member_path = os.path.realpath(os.path.join(base_dir, member))
                    if not member_path.startswith(base_dir + os.sep) and member_path != base_dir:
                        raise Exception(f"Güvensiz ZIP yolu algılandı: {member}")
                zip_ref.extractall(target_dir)
            logging.info(f"ZIP dosyası başarıyla açıldı: {zip_path}")
        elif lower_path.endswith('.rar') or lower_path.endswith('.7z'):
            try:
                seven_zip_path = r"C:\Program Files\7-Zip\7z.exe" if os.path.exists(r"C:\Program Files\7-Zip\7z.exe") else "7z"
                result = subprocess.run([seven_zip_path, "x", zip_path, f"-o{target_dir}", "-y"], capture_output=True)
                with open("debug.log", "a", encoding="utf-8") as f:
                    f.write(f"7Z CALL:\nRC: {result.returncode}\nOUT: {result.stdout}\nERR: {result.stderr}\n")
                if result.returncode != 0:
                    err_msg = (result.stderr or result.stdout or b"").decode(errors='replace')
                    raise Exception(f"7z işlemi başarısız: Kod {result.returncode} Mesaj: {err_msg}")
                logging.info(f"Arşiv başarıyla açıldı: {zip_path}")
            except FileNotFoundError:
                raise Exception("Sunucuda 7-Zip (7z) bulunamadığından sadece .zip açılabilir.")
        else:
            raise Exception("Desteklenmeyen arşiv formatı.")
    except Exception as e:
        logging.error(f"Arşiv açma hatası: {e}")
        raise
def get_detailed_system_stats():
    try:
        # CPU kullanımı
        cpu_percent = psutil.cpu_percent(interval=1)
        
        # Bellek kullanımı
        memory = psutil.virtual_memory()
        
        # Disk kullanımı
        disk = psutil.disk_usage(UPLOAD_FOLDER)
        
        # GPU bilgisi (NVIDIA için)
        gpu_info = {"gpu_usage": "N/A", "gpu_memory": "N/A"}
        try:
            if platform.system() == "Windows":
                # Windows'ta NVIDIA GPU bilgisi
                result = subprocess.run([
                    "nvidia-smi", 
                    "--query-gpu=utilization.gpu,memory.used,memory.total", 
                    "--format=csv,noheader,nounits"
                ], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    gpu_data = result.stdout.strip().split(', ')
                    if len(gpu_data) >= 3:
                        gpu_info = {
                            "gpu_usage": f"{gpu_data[0]}%",
                            "gpu_memory": f"{gpu_data[1]} / {gpu_data[2]} MB"
                        }
        except Exception as e:
            logging.warning(f"GPU bilgisi alınamadı: {e}")
        
        # Ağ kullanımı
        net_io = psutil.net_io_counters()
        
        return {
            "cpu": {
                "percent": cpu_percent,
                "cores": psutil.cpu_count(),
                "frequency": psutil.cpu_freq().current if psutil.cpu_freq() else "N/A"
            },
            "memory": {
                "total": round(memory.total / (1024**3), 2),
                "used": round(memory.used / (1024**3), 2),
                "available": round(memory.available / (1024**3), 2),
                "percent": memory.percent
            },
            "disk": {
                "total": round(disk.total / (1024**3), 2),
                "used": round(disk.used / (1024**3), 2),
                "free": round(disk.free / (1024**3), 2),
                "percent": disk.percent
            },
            "gpu": gpu_info,
            "network": {
                "bytes_sent": round(net_io.bytes_sent / (1024**2), 2),
                "bytes_recv": round(net_io.bytes_recv / (1024**2), 2)
            }
        }
    except Exception as e:
        logging.error(f"Sistem istatistikleri alma hatası: {e}")
        return {"error": str(e)}

def save_settings():
    try:
        with open("server_settings.json", "w") as f:
            json.dump(server_settings, f)
        logging.info("Sunucu ayarları kaydedildi")
    except Exception as e:
        logging.error(f"Ayarları kaydetme hatası: {e}")

def load_settings():
    global server_settings
    try:
        if os.path.exists("server_settings.json"):
            with open("server_settings.json", "r") as f:
                loaded_settings = json.load(f)
                server_settings.update(loaded_settings)
            logging.info("Sunucu ayarları yüklendi")
    except Exception as e:
        logging.error(f"Ayarları yükleme hatası: {e}")

def monitor_servers():
    """Arka planda sunucuları izler ve boşta (30 dk) olan sunucuları kapatır.
    Plus+ etiketi OLAN kullanıcılar ve adminler ASLA kapatılmaz.
    Sadece Plus+ ETİKETİ OLMAYAN ve admin OLMAYAN kullanıcılar 30 dk sonra kapanır."""
    while True:
        try:
            current_time = time.time()
            for username, proc in list(user_processes.items()):
                if not is_proc_active(proc):
                    continue
                
                user_data = users.get(username, {})
                
                # Plus+ etiketi varsa hiçbir zaman kapatma
                # Kullanıcı isteği: Admin olsa dahi Plus+ yoksa 30 dk sonra kapanır.
                tags = user_data.get('tags', [])
                if 'plus+' in tags:
                    continue
                
                # Sadece Plus+ ETİKETİ OLMAYAN ve admin OLMAYAN kullanıcılar için kontrol
                # 30 dk = 1800 saniye inaktif kalırsa kapat
                
                # Eğer sunucuda anlık oynayan oyuncular varsa sayacı sıfırla ki kapanmasın
                if len(active_players.get(username, set())) > 0:
                    server_activity[username] = current_time
                    continue

                last_active = server_activity.get(username, current_time)
                if current_time - last_active > 1800:
                    logging.info(f"İnaktif sunucu durduruluyor ({username})... (Plus+: Yok, Rol: {user_data.get('role', 'user')})")
                    if username in user_console_queues:
                        user_console_queues[username].put(f"[{time.strftime('%H:%M:%S')}] 🛑 30 dakika boyunca oyuncu olmadığı için sunucu otomatik kapatılıyor. Plus+ satın alarak bunu engelleyebilirsiniz.\n")
                    if proc != "REMOTE":
                        try:
                            proc.stdin.write(b"stop\n")
                            proc.stdin.flush()
                            proc.wait(timeout=10)
                        except:
                            try: proc.kill()
                            except: pass
                    user_processes[username] = None
                    if username in server_activity:
                        server_activity[username] = time.time()
        except Exception as e:
            logging.error(f"Sunucu izleme döngüsünde hata: {e}")
        time.sleep(60)  # Her 1 dakikada bir kontrol et

# Sunucu izleyici başlat
threading.Thread(target=monitor_servers, daemon=True).start()

# Uygulama başlangıcında ayarları ve kullanıcıları yükle
load_settings()
load_users()

# --- YENİ: Giriş Sayfası HTML ---


login_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SkyVex Hosting - Giriş Yap</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        :root {
            --primary: #6366f1;
            --primary-dark: #4338ca;
            --secondary: #10b981;
            --danger: #ef4444;
            --dark: #1e1e2f;
            --darker: #151521;
            --light: #f8fafc;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, var(--darker) 0%, var(--dark) 100%);
            color: var(--light);
            min-height: 100vh;
            display: flex; align-items: center; justify-content: center;
        }
        .login-container {
            background: rgba(30, 30, 47, 0.95);
            backdrop-filter: blur(10px);
            padding: 2.5rem;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            border: 1px solid rgba(255, 255, 255, 0.1);
            width: 100%; max-width: 400px;
        }
        .brand { text-align: center; margin-bottom: 2rem; }
        .brand h1 {
            background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            font-size: 2.5rem; font-weight: 800;
        }
        .brand p { color: #94a3b8; font-size: 0.9rem; margin-top: 0.5rem; }
        .stats-grid {
            display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 2rem;
        }
        .stat-item {
            background: rgba(255, 255, 255, 0.05); padding: 1rem; border-radius: 15px; text-align: center;
        }
        .stat-value { display: block; font-size: 1.2rem; font-weight: 700; color: var(--secondary); }
        .stat-label { display: block; font-size: 0.7rem; color: #94a3b8; text-transform: uppercase; }
        .form-group { margin-bottom: 1.2rem; position: relative; }
        .form-icon { position: absolute; left: 1rem; top: 50%; transform: translateY(-50%); color: #94a3b8; }
        .form-input {
            width: 100%; padding: 0.8rem 1rem 0.8rem 2.8rem;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 10px; color: var(--light); font-size: 1rem;
        }
        .btn {
            width: 100%; padding: 1rem; border: none; border-radius: 10px;
            cursor: pointer; font-weight: 600; color: white;
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
            transition: 0.3s;
        }
        .btn:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(99, 102, 241, 0.4); }
        .alert {
            padding: 0.8rem; border-radius: 10px; margin-bottom: 1.5rem;
            background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2);
            color: #fca5a5; font-size: 0.9rem; display: flex; align-items: center; gap: 0.5rem;
        }
        .register-link { text-align: center; margin-top: 1.5rem; font-size: 0.9rem; color: #94a3b8; }
        .register-link a { color: var(--primary); text-decoration: none; font-weight: 600; }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="brand">
            <h1>SkyVex</h1>
            <p>Minecraft Sunucu Yönetim Paneli</p>
        </div>

        <div class="stats-grid">
            <div class="stat-item">
                <span class="stat-value">{{ total_accounts }}</span>
                <span class="stat-label">Toplam Hesap</span>
            </div>
            <div class="stat-item">
                <span class="stat-value">{{ active_servers }}</span>
                <span class="stat-label">Aktif Sunucu</span>
            </div>
        </div>

        {% if error %}
        <div class="alert">
            <i class="fas fa-exclamation-circle"></i>
            {% if error == 'invalid' %} Geçersiz kullanıcı adı veya şifre!
            {% elif error == 'locked' %} Çok fazla hatalı giriş! Lütfen bekleyin.
            {% elif error == 'expired' %} Hesabınızın kullanım süresi dolmuştur.
            {% else %} Bir hata oluştu.
            {% endif %}
        </div>
        {% endif %}

        <form method="POST">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <div class="form-group">
                <i class="fas fa-user form-icon"></i>
                <input type="text" name="username" class="form-input" placeholder="Kullanıcı Adı" required>
            </div>
            <div class="form-group">
                <i class="fas fa-lock form-icon"></i>
                <input type="password" name="password" class="form-input" placeholder="••••••••" required>
            </div>
            <button type="submit" class="btn">Giriş Yap</button>
        </form>

        <div class="register-link">
            Henüz bir hesabın yok mu? <a href="/register">Hemen Kayıt Ol</a>
        </div>
    </div>
</body>
</html>
"""

# --- GÜNCELLENMİŞ HTML Şablonu (Modern ve Görsel Açıdan Zengin) ---

html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SkyVex Panel — {{ username }}</title>
    <meta name="csrf-token" content="{{ csrf_token() }}">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
    <style>
        /* ─── DESIGN TOKENS ─── */
        :root {
            --primary: #6366f1;
            --primary-dark: #4338ca;
            --primary-glow: rgba(99,102,241,0.25);
            --secondary: #10b981;
            --secondary-glow: rgba(16,185,129,0.2);
            --danger: #ef4444;
            --danger-glow: rgba(239,68,68,0.2);
            --warning: #f59e0b;
            --bg: #05050f;
            --bg2: #0a0a1a;
            --surface: rgba(255,255,255,0.03);
            --surface-hover: rgba(255,255,255,0.06);
            --border: rgba(255,255,255,0.07);
            --border-bright: rgba(255,255,255,0.14);
            --text: #e2e8f0;
            --muted: #64748b;
            --sidebar-w: 240px;
        }

        *,*::before,*::after { box-sizing: border-box; margin:0; padding:0; }

        body {
            font-family: 'Inter', sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            overflow-x: hidden;
            display: flex;
            flex-direction: column;
        }

        /* ─── ANIMATED HEX BACKGROUND ─── */
        body::before {
            content: '';
            position: fixed;
            inset: 0;
            background-image:
                radial-gradient(ellipse 80% 60% at 20% 0%, rgba(99,102,241,0.08) 0%, transparent 60%),
                radial-gradient(ellipse 60% 40% at 80% 100%, rgba(16,185,129,0.06) 0%, transparent 60%);
            pointer-events: none;
            z-index: 0;
        }

        /* ─── TOP NAV BAR ─── */
        .topbar {
            position: sticky; top: 0; z-index: 100;
            display: flex; align-items: center; justify-content: space-between;
            padding: 0 28px;
            height: 60px;
            background: rgba(5,5,15,0.85);
            backdrop-filter: blur(24px);
            border-bottom: 1px solid var(--border);
        }

        .topbar-logo {
            display: flex; align-items: center; gap: 10px;
            font-size: 18px; font-weight: 800; letter-spacing: -0.5px;
        }
        .topbar-logo .logo-icon {
            width: 34px; height: 34px;
            background: linear-gradient(135deg, var(--primary), #a855f7);
            border-radius: 9px;
            display: flex; align-items: center; justify-content: center;
            font-size: 16px;
            box-shadow: 0 4px 16px var(--primary-glow);
        }

        .topbar-right { display: flex; align-items: center; gap: 12px; }

        .topbar-user {
            display: flex; align-items: center; gap: 8px;
            padding: 6px 14px;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 10px;
            font-size: 13px; font-weight: 600; color: var(--text);
        }
        .topbar-user .avatar {
            width: 26px; height: 26px;
            background: linear-gradient(135deg, var(--primary), #a855f7);
            border-radius: 7px;
            display: flex; align-items: center; justify-content: center;
            font-size: 12px; font-weight: 800;
        }

        .topbar-btn {
            padding: 6px 14px;
            background: transparent; border: 1px solid var(--border);
            border-radius: 9px; color: var(--muted);
            font-size: 13px; font-weight: 500; cursor: pointer;
            text-decoration: none; transition: all 0.2s;
        }
        .topbar-btn:hover { background: var(--surface); color: var(--text); border-color: var(--border-bright); }

        .status-pill {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 5px 12px; border-radius: 20px;
            font-size: 12px; font-weight: 700; letter-spacing: 0.3px;
        }
        .status-pill::before { content: ''; width: 7px; height: 7px; border-radius: 50%; }
        .status-online  { background: rgba(16,185,129,0.12); color: #34d399; border: 1px solid rgba(16,185,129,0.25); }
        .status-online::before  { background: #34d399; box-shadow: 0 0 6px #34d399; animation: pulse-dot 2s infinite; }
        .status-offline { background: rgba(239,68,68,0.12); color: #f87171; border: 1px solid rgba(239,68,68,0.25); }
        .status-offline::before { background: #f87171; }
        @keyframes pulse-dot { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }

        /* ─── LAYOUT ─── */
        .layout {
            display: flex; flex: 1;
            position: relative; z-index: 1;
        }

        /* ─── SIDEBAR ─── */
        .sidebar {
            width: var(--sidebar-w);
            flex-shrink: 0;
            padding: 20px 12px;
            border-right: 1px solid var(--border);
            background: rgba(5,5,15,0.6);
            display: flex; flex-direction: column; gap: 4px;
            position: sticky; top: 60px;
            height: calc(100vh - 60px);
            overflow-y: auto;
        }

        .sidebar-section-label {
            font-size: 10px; font-weight: 700; letter-spacing: 1.5px;
            color: var(--muted); text-transform: uppercase;
            padding: 8px 12px 4px;
        }

        .nav-item {
            display: flex; align-items: center; gap: 10px;
            padding: 9px 14px;
            border-radius: 10px;
            font-size: 13.5px; font-weight: 500;
            color: var(--muted); text-decoration: none;
            transition: all 0.2s; position: relative;
            border: 1px solid transparent;
        }
        .nav-item .nav-icon { font-size: 16px; width: 20px; text-align: center; }
        .nav-item:hover {
            background: var(--surface-hover);
            color: var(--text);
            border-color: var(--border);
        }
        .nav-item.active {
            background: rgba(99,102,241,0.12);
            color: #a5b4fc;
            border-color: rgba(99,102,241,0.25);
            font-weight: 600;
        }
        .nav-item.active::before {
            content: '';
            position: absolute; left: 0; top: 20%; bottom: 20%;
            width: 3px; border-radius: 2px;
            background: var(--primary);
        }

        /* ─── MAIN CONTENT ─── */
        .main-content {
            flex: 1; padding: 28px;
            display: flex; flex-direction: column; gap: 24px;
            min-width: 0;
        }

        /* ─── CARDS ─── */
        .card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 24px;
            margin-bottom: 0;
            position: relative; overflow: hidden;
            transition: border-color 0.2s;
        }
        .card:hover { border-color: var(--border-bright); }

        /* Geometric accent line on cards */
        .card::before {
            content: '';
            position: absolute; top: 0; left: 0; right: 0;
            height: 1px;
            background: linear-gradient(90deg, transparent, rgba(99,102,241,0.4), transparent);
        }

        .card-header {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 20px;
        }
        .card-header h2 {
            font-size: 15px; font-weight: 700; color: var(--text);
            display: flex; align-items: center; gap: 8px;
        }

        /* ─── STAT CARDS ─── */
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr)); gap: 14px; }

        .stat-card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 14px; padding: 18px 16px;
            text-align: center; position: relative; overflow: hidden;
        }
        .stat-card::after {
            content: '';
            position: absolute; bottom: 0; left: 0; right: 0;
            height: 2px; border-radius: 0 0 14px 14px;
        }
        .stat-card.cpu::after  { background: linear-gradient(90deg, #10b981, #34d399); }
        .stat-card.ram::after  { background: linear-gradient(90deg, #6366f1, #a855f7); }
        .stat-card.disk::after { background: linear-gradient(90deg, #f59e0b, #fbbf24); }

        .stat-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); }
        .stat-value { font-size: 24px; font-weight: 800; margin: 6px 0 0; color: var(--text); }

        /* ─── BUTTONS ─── */
        .btn {
            display: inline-flex; align-items: center; gap: 7px;
            padding: 10px 20px;
            border: none; border-radius: 11px;
            font-size: 13.5px; font-weight: 600;
            cursor: pointer; transition: all 0.2s;
            text-decoration: none; font-family: 'Inter', sans-serif;
        }
        .btn-primary {
            background: linear-gradient(135deg, var(--primary), var(--primary-dark));
            color: #fff;
            box-shadow: 0 4px 16px var(--primary-glow);
        }
        .btn-primary:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 8px 24px var(--primary-glow); }
        .btn-danger {
            background: linear-gradient(135deg, var(--danger), #dc2626);
            color: #fff;
            box-shadow: 0 4px 16px var(--danger-glow);
        }
        .btn-danger:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 8px 24px var(--danger-glow); }
        .btn-ghost {
            background: var(--surface); border: 1px solid var(--border);
            color: var(--text);
        }
        .btn-ghost:hover:not(:disabled) { background: var(--surface-hover); border-color: var(--border-bright); }
        .btn:disabled { opacity: 0.45; cursor: not-allowed; transform: none !important; box-shadow: none !important; }

        .btn-group { display: flex; gap: 10px; flex-wrap: wrap; }

        /* ─── STARTING ANIMATION ─── */
        .status-starting {
            display: inline-flex; align-items: center; justify-content: center;
            padding: 5px 16px; border-radius: 20px;
            background: rgba(245,158,11,0.1); border: 1px solid rgba(245,158,11,0.3);
            color: #fbbf24; font-size: 12px; font-weight: 700;
            position: relative; overflow: hidden;
        }
        .status-starting::before {
            content: '';
            position: absolute; top:0; left:0; height:100%; width:0%;
            background: linear-gradient(90deg, rgba(245,158,11,0.2), rgba(251,191,36,0.1));
            animation: tube-liquid-fill 12s linear infinite;
        }
        @keyframes tube-liquid-fill { 0%{width:0%} 70%{width:90%} 100%{width:100%} }

        /* ─── FORMS ─── */
        .form-group { margin-bottom: 18px; }
        .form-label { display: block; margin-bottom: 7px; font-size: 12.5px; font-weight: 600; color: var(--muted); }
        .form-input {
            width: 100%; padding: 10px 14px;
            background: rgba(255,255,255,0.04); border: 1px solid var(--border);
            border-radius: 11px; color: var(--text);
            font-size: 14px; font-family: 'Inter', sans-serif;
            transition: all 0.2s; outline: none;
        }
        .form-input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-glow); }
        textarea.form-input { resize: vertical; }
        select.form-input { background: #0d0d1f; }
        .form-input option, .form-input optgroup { background: #0a0a1a; color: var(--text); }

        /* ─── ALERTS ─── */
        .alert {
            padding: 12px 16px; border-radius: 12px; margin: 12px 0;
            border-left: 3px solid; font-size: 13.5px;
        }
        .alert-warning { background: rgba(245,158,11,0.08); border-left-color: var(--warning); color: #fbbf24; }
        .alert-info    { background: rgba(99,102,241,0.08); border-left-color: var(--primary); color: #a5b4fc; }
        .alert-success { background: rgba(16,185,129,0.08); border-left-color: var(--secondary); color: #34d399; }
        .alert-danger  { background: rgba(239,68,68,0.08); border-left-color: var(--danger); color: #fca5a5; }

        /* ─── CONSOLE ─── */
        .console-container {
            border-radius: 14px; overflow: hidden;
            border: 1px solid var(--border);
        }
        .console-header {
            background: rgba(0,0,0,0.5);
            padding: 12px 18px;
            border-bottom: 1px solid var(--border);
            display: flex; align-items: center; gap: 8px;
            font-size: 13px; font-weight: 600; color: var(--muted);
        }
        .console-header::before {
            content: '●  ●  ●';
            letter-spacing: 3px; font-size: 9px;
            color: rgba(255,255,255,0.2);
        }
        .console-content {
            height: 560px; overflow-y: auto;
            padding: 18px; font-family: 'Consolas', 'Monaco', monospace;
            font-size: 13px; line-height: 1.6;
            color: #4ade80; background: #000;
        }
        .console-content::-webkit-scrollbar { width: 4px; }
        .console-content::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }

        /* ─── FILE TABLE ─── */
        .file-list { width: 100%; border-collapse: collapse; }
        .file-list th {
            background: rgba(255,255,255,0.04);
            padding: 10px 14px; text-align: left;
            font-size: 11px; font-weight: 700; color: var(--muted);
            text-transform: uppercase; letter-spacing: 0.8px;
            border-bottom: 1px solid var(--border);
        }
        .file-list td { padding: 10px 14px; border-bottom: 1px solid var(--border); font-size: 13.5px; }
        .file-list tr:hover td { background: rgba(255,255,255,0.025); }

        /* ─── BREADCRUMBS ─── */
        .breadcrumbs { display: flex; align-items: center; gap: 6px; margin-bottom: 18px; flex-wrap: wrap; }
        .breadcrumb-item { color: var(--muted); text-decoration: none; font-size: 13px; transition: color 0.2s; }
        .breadcrumb-item:hover { color: var(--text); }
        .breadcrumb-separator { color: rgba(255,255,255,0.2); font-size: 11px; }

        /* ─── UPLOAD ─── */
        .upload-area {
            border: 2px dashed var(--border-bright); border-radius: 14px;
            padding: 28px; text-align: center; margin: 14px 0;
            transition: all 0.3s; cursor: pointer;
        }
        .upload-area:hover, .upload-area.dragover {
            border-color: var(--primary);
            background: var(--primary-glow);
        }

        /* ─── DROPDOWN ─── */
        .file-actions-dropdown { position: relative; display: inline-block; }
        .dropdown-content {
            display: none; position: absolute; right: 0;
            background: #0e0e20; min-width: 160px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.6);
            z-index: 200; border-radius: 12px;
            border: 1px solid var(--border); overflow: hidden;
        }
        .dropdown-content a, .dropdown-content button {
            color: var(--text); padding: 11px 16px; text-decoration: none;
            display: block; background: none; border: none;
            width: 100%; text-align: left; cursor: pointer;
            font-size: 13px; font-family: 'Inter', sans-serif; transition: 0.15s;
        }
        .dropdown-content a:hover, .dropdown-content button:hover { background: var(--surface-hover); }
        .show { display: block !important; }
        .dots-btn {
            background: var(--surface); border: 1px solid var(--border);
            color: var(--text); padding: 5px 10px; border-radius: 8px;
            cursor: pointer; transition: 0.2s;
        }
        .dots-btn:hover { background: var(--surface-hover); }

        /* ─── NOTIFICATION / TOAST ─── */
        .notification {
            position: fixed; bottom: 24px; right: 24px;
            padding: 12px 20px; border-radius: 12px;
            background: var(--secondary); color: white;
            box-shadow: 0 8px 32px var(--secondary-glow);
            display: none; z-index: 9999; font-size: 14px; font-weight: 600;
            animation: slideInRight 0.3s ease;
        }
        @keyframes slideInRight { from { transform: translateX(110%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

        /* ─── IP ADDRESS BOX ─── */
        .ip-box {
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--border); border-radius: 11px;
            padding: 10px 14px; font-size: 14px; font-weight: 500;
            color: var(--text); letter-spacing: 0.3px;
        }
        .ip-box.highlight {
            background: rgba(99,102,241,0.06);
            border-color: rgba(99,102,241,0.25); color: #c7d2fe;
        }

        /* ─── PLUGIN CARDS ─── */
        .plugin-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px,1fr)); gap: 14px; }
        #plugin-results { min-height: 420px; }
        .plugin-card {
            background: var(--surface); border: 1px solid var(--border);
            border-radius: 14px; padding: 18px;
            transition: border-color .2s, transform .2s;
            display: flex; flex-direction: column;
            min-height: 190px; overflow: hidden; break-inside: avoid;
        }
        .plugin-card:hover { border-color: rgba(99,102,241,.5); transform: translateY(-3px); }
        .plugin-card-icon {
            width: 38px; height: 38px; border-radius: 11px; margin-bottom: 12px;
            display: flex; align-items: center; justify-content: center; font-size: 18px;
            background: linear-gradient(135deg, rgba(99,102,241,.28), rgba(6,182,212,.18));
            border: 1px solid rgba(99,102,241,.3);
        }
        .plugin-title { font-size: 15px; font-weight: 700; color: var(--text); }
        .plugin-desc { color: var(--muted); font-size: 12.5px; margin-top: 6px; line-height: 1.5; flex-grow: 1; word-break: break-word; }
        .plugin-meta { color: #a5b4fc; font-size: 12px; margin-top: 10px; }
        .plugin-actions { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 8px; }

        /* ─── GEOMETRIC GRID DECORATION ─── */
        .geo-grid {
            position: absolute; inset: 0; pointer-events: none; overflow: hidden; z-index: 0;
        }
        .geo-grid::before {
            content: '';
            position: absolute; inset: -50%;
            background-image:
                linear-gradient(rgba(99,102,241,0.04) 1px, transparent 1px),
                linear-gradient(90deg, rgba(99,102,241,0.04) 1px, transparent 1px);
            background-size: 40px 40px;
        }

        /* ─── PROGRESS BARS ─── */
        .progress-bar { background: rgba(255,255,255,0.06); border-radius: 6px; overflow: hidden; height: 6px; margin-top: 8px; }
        .progress-fill { height: 100%; border-radius: 6px; transition: width 0.4s ease; }
        .cpu-progress  { background: linear-gradient(90deg, #10b981, #34d399); }
        .ram-progress  { background: linear-gradient(90deg, #6366f1, #a855f7); }
        .disk-progress { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
        .gpu-progress  { background: linear-gradient(90deg, #ec4899, #f472b6); }

        /* ─── TOOLTIP ─── */
        .tooltip { position: relative; display: inline-block; }
        .tooltip .tooltiptext {
            visibility: hidden; width: 200px;
            background: rgba(0,0,0,0.9); color: white;
            text-align: center; border-radius: 8px; padding: 7px 10px;
            position: absolute; z-index: 300;
            bottom: 125%; left: 50%; transform: translateX(-50%);
            opacity: 0; transition: opacity 0.2s; font-size: 12px;
        }
        .tooltip:hover .tooltiptext { visibility: visible; opacity: 1; }

        /* ─── RESPONSIVE ─── */
        @media (max-width: 900px) {
            .sidebar { display: none; }
            .main-content { padding: 16px; }
        }
        @media (max-width: 600px) {
            .topbar { padding: 0 16px; }
            .stats-grid { grid-template-columns: 1fr 1fr; }
        }

        /* ─── SCROLLBAR GLOBAL ─── */
        ::-webkit-scrollbar { width: 5px; height: 5px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.08); border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.14); }
    </style>
</head>
<body>
    <!-- TOP NAV -->
    <div class="topbar">
        <div class="topbar-logo">
            <div class="logo-icon" style="background:linear-gradient(135deg,#6366f1,#06b6d4);display:flex;align-items:center;justify-content:center;">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M21.6 12L2.4 20l4.1-8-4.1-8 19.2 8z" fill="#fff"/><path d="M6.5 12h9.2" stroke="#0b1020" stroke-width="1.1" opacity=".35"/></svg>
            </div>
            SkyVex
        </div>
        <div class="topbar-right">
            <span class="status-pill {{ 'status-online' if process else 'status-offline' }}" id="statusBadge">
                {{ 'Çalışıyor' if process else 'Kapalı' }}
            </span>
            <div class="topbar-user">
                <div class="avatar">{{ username[0]|upper }}</div>
                {{ username }}
            </div>
            <a href="/logout" class="topbar-btn">↩ Çıkış</a>
        </div>
    </div>

    <div class="layout">
        <!-- SIDEBAR -->
        <aside class="sidebar">
            <div class="sidebar-section-label">Sunucu</div>
            <a href="/dashboard" class="nav-item {{ 'active' if page=='home' else '' }}">
                <span class="nav-icon">🏠</span> Ana Panel
            </a>
            <a href="/servers" class="nav-item {{ 'active' if page=='servers' else '' }}">
                <span class="nav-icon">🖥️</span> Sunucularım
            </a>
            <a href="/players" class="nav-item {{ 'active' if page=='players' else '' }}">
                <span class="nav-icon">👥</span> Oyuncular
            </a>
            <a href="/access" class="nav-item {{ 'active' if page=='access' else '' }}">
                <span class="nav-icon">🔑</span> Erişim
            </a>

            <div class="sidebar-section-label" style="margin-top:12px;">Yönetim</div>
            <a href="/plugins" class="nav-item {{ 'active' if page=='plugins' else '' }}">
                <span class="nav-icon">🧩</span> Eklentiler
            </a>
            <a href="/properties" class="nav-item {{ 'active' if page=='properties' else '' }}">
                <span class="nav-icon">⚙️</span> Özellikler
            </a>
            <a href="/files" class="nav-item {{ 'active' if page=='files' else '' }}">
                <span class="nav-icon">📁</span> Dosyalar
            </a>
            <a href="/settings" class="nav-item {{ 'active' if page=='settings' else '' }}">
                <span class="nav-icon">🛠️</span> Ayarlar
            </a>

            <div class="sidebar-section-label" style="margin-top:12px;">Platform</div>
            <a href="/" class="nav-item">
                <span class="nav-icon">🌐</span> Ana Sayfa
            </a>
            <a href="/bot/panel" class="nav-item">
                <span class="nav-icon">🤖</span> Discord Bot
            </a>

            {% if user_data.resources %}
            <div style="margin-top:auto; padding:14px 12px 0;">
                <div style="background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:14px;">
                    <div style="font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:1px; color:var(--muted); margin-bottom:10px;">Kaynak Limiti</div>
                    <div style="font-size:12px; color:var(--muted); margin-bottom:4px;">RAM</div>
                    <div style="font-size:13px; font-weight:700; color:var(--text);">{{ user_data.resources.ram_mb }} MB</div>
                    <div style="font-size:12px; color:var(--muted); margin-bottom:4px; margin-top:8px;">CPU</div>
                    <div style="font-size:13px; font-weight:700; color:var(--text);">{{ user_data.resources.cpu_cores }} Çekirdek</div>
                    <div style="font-size:12px; color:var(--muted); margin-bottom:4px; margin-top:8px;">Depolama</div>
                    <div style="font-size:13px; font-weight:700; color:var(--text);">{{ user_data.resources.storage_gb }} GB</div>
                </div>
            </div>
            {% endif %}
        </aside>

        <!-- MAIN CONTENT -->
        <main class="main-content">
            <!-- Left+Right panels via CSS grid -->
            {% if page == "home" %}
            <div style="display:grid; grid-template-columns: 360px 1fr; gap:20px; align-items:start;">
                <!-- LEFT COLUMN (Control + Stats) -->
                <div style="display:flex; flex-direction:column; gap:20px;">
                    <!-- Server Control Card -->
                    <div class="card">
                        <div class="card-header">
                            <h2>⚡ Sunucu Kontrol</h2>
                        </div>

                        <div class="form-group">
                            <label class="form-label" style="color:#a5b4fc;">🌐 Sunucu Adresi</label>
                            <div class="ip-box highlight" onclick="copyServerAddress()" title="Kopyalamak için tıkla" style="cursor:pointer;">
                                {% if bore_address %}{{ bore_address }}{% else %}<i style="color:var(--muted)">Sunucu kapalı</i>{% endif %}
                            </div>
                        </div>

                        <div class="btn-group">
                            <form method="POST" action="/start" style="display:inline;">
                                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                                <button class="btn btn-primary" type="submit" id="startBtn"
                                    {% if not jar_exists or not eula_agreed %}disabled{% endif %}
                                    onclick="showStartingStatus(); setTimeout(()=>{this.disabled=true;this.innerHTML='⌛ Başlatılıyor...';},50);">
                                    ▶ Başlat
                                </button>
                            </form>
                            <form method="POST" action="/stop" style="display:inline;">
                                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                                <button class="btn btn-danger" type="submit" {% if not process %}disabled{% endif %}>
                                    ◼ Durdur
                                </button>
                            </form>
                        </div>

                        {% if not jar_exists %}
                        <div class="alert alert-warning" style="margin-top:14px;">⚠️ {{server_jar_name}} dosyası eksik.</div>
                        {% elif not eula_agreed %}
                        <div class="alert alert-warning" style="margin-top:14px;">⚠️ EULA kabul edilmeli (eula.txt → eula=true).</div>
                        {% endif %}
                    </div>

                    <!-- Resources Card -->
                    <div class="card">
                        <div class="card-header">
                            <h2>◈ Sunucu Kaynakları</h2>
                        </div>
                        <div class="stats-grid">
                            <div class="stat-card cpu">
                                <div class="stat-label">CPU</div>
                                <div class="stat-value" id="serverCpuPercent">%0</div>
                            </div>
                            <div class="stat-card ram">
                                <div class="stat-label">RAM</div>
                                <div class="stat-value" id="serverRamUsage">0 MB</div>
                            </div>
                            <div class="stat-card disk">
                                <div class="stat-label">Disk</div>
                                <div class="stat-value" id="serverDiskUsage" style="font-size:1.1rem;">—</div>
                            </div>
                        </div>
                        <div style="margin-top:18px; height:240px; border-top:1px solid var(--border); padding-top:12px;">
                            <canvas id="performanceChart"></canvas>
                        </div>
                    </div>
                </div>

            {% else %}
            <div style="display:block;">
            {% endif %}

                <!-- RIGHT COLUMN (Dynamic content) -->
                <div>

            {% if page=="files" %}
                <!-- Dosya Yöneticisi -->
                <div class="card">
                    <div class="card-header">

                        <h2>📁 Dosya Yöneticisi</h2>
                    </div>

                    <div class="breadcrumbs">
                        <a href="/files" class="breadcrumb-item">Ana Dizin</a>
                        {% for name, path in breadcrumbs %}
                            <span class="breadcrumb-separator">/</span>
                            {% if loop.last %}
                                <span class="breadcrumb-item">{{ name }}</span>
                            {% else %}
                                <a href="/files/{{ path }}" class="breadcrumb-item">{{ name }}</a>
                            {% endif %}
                        {% endfor %}
                    </div>

                    <div style="display: flex; gap: 10px; margin-bottom: 1.5rem; flex-wrap: wrap;">
                        <button onclick="createNewFolder()" class="btn btn-primary" style="background: #10b981; padding: 0.6rem 1.2rem; font-size: 0.85rem;">📂 Yeni Klasör</button>
                        <button onclick="createNewFile()" class="btn btn-primary" style="background: #3b82f6; padding: 0.6rem 1.2rem; font-size: 0.85rem;">📄 Yeni Belge</button>
                    </div>

                    <!-- ====== MODERN LOADING OVERLAY ====== -->
                    <div id="uploadOverlay" style="display:none; position:fixed; inset:0; background:rgba(10, 10, 20, 0.7); backdrop-filter:blur(8px); -webkit-backdrop-filter:blur(8px); z-index:9999; align-items:center; justify-content:center; flex-direction:column; animation: fadeInOverlay 0.3s ease;">
                        <div style="background:rgba(30,30,47,0.9); border:1px solid rgba(255,255,255,0.1); border-radius:24px; padding:2.5rem; width:400px; max-width:90vw; box-shadow:0 25px 50px rgba(0,0,0,0.5); text-align:center;">
                            <style>
                                @keyframes fadeInOverlay { from { opacity: 0; } to { opacity: 1; } }
                                @keyframes pulseIcon { 0% { transform: scale(1); opacity: 0.8; } 50% { transform: scale(1.1); opacity: 1; } 100% { transform: scale(1); opacity: 0.8; } }
                                .overlay-icon { font-size: 4rem; margin-bottom: 1.5rem; animation: pulseIcon 2s infinite ease-in-out; display: inline-block; }
                            </style>
                            <div id="overlayIcon" class="overlay-icon">📤</div>
                            <h2 id="overlayTitle" style="font-weight:800; font-size:1.5rem; color:#fff; margin-bottom:0.5rem; letter-spacing:-0.5px;">İşlem Yapılıyor...</h2>
                            <p id="overlaySubtitle" style="font-size:1rem; color:#94a3b8; margin-bottom:2rem;">Lütfen bekleyin, bu işlem biraz zaman alabilir.</p>
                            <div id="overlayProgressWrap" style="width:100%; background:rgba(255,255,255,0.05); border-radius:50px; height:12px; overflow:hidden; margin-bottom:1rem; border:1px solid rgba(255,255,255,0.05);">
                                <div id="overlayProgressBar" style="width:0%; height:100%; background:linear-gradient(90deg, var(--primary), var(--secondary)); transition:width 0.3s cubic-bezier(0.4, 0, 0.2, 1); box-shadow:0 0 15px rgba(99,102,241,0.3);"></div>
                            </div>
                            <div style="display:flex; justify-content:space-between; align-items:center;">
                                <span id="overlayPercent" style="font-weight:700; font-size:1.1rem; color:var(--primary);">0%</span>
                                <span id="overlayStatusText" style="font-size:0.85rem; color:var(--gray); font-family:monospace;">hazırlanıyor...</span>
                            </div>
                        </div>
                    </div>

                    <script>
                        function showOverlay(icon, title, subtitle) {
                            const overlay = document.getElementById('uploadOverlay');
                            document.getElementById('overlayIcon').innerText = icon;
                            document.getElementById('overlayTitle').innerText = title;
                            document.getElementById('overlaySubtitle').innerText = subtitle;
                            document.getElementById('overlayProgressBar').style.width = '0%';
                            document.getElementById('overlayPercent').innerText = '0%';
                            document.getElementById('overlayStatusText').innerText = 'başlatılıyor...';
                            overlay.style.display = 'flex';
                        }
                        function hideOverlay() { document.getElementById('uploadOverlay').style.display = 'none'; }
                        function setOverlayProgress(loaded, total) {
                            const percent = Math.round((loaded / total) * 100);
                            document.getElementById('overlayProgressBar').style.width = percent + '%';
                            document.getElementById('overlayPercent').innerText = percent + '%';
                            const loadedMB = (loaded / (1024 * 1024)).toFixed(2);
                            const totalMB = (total / (1024 * 1024)).toFixed(2);
                            document.getElementById('overlayStatusText').innerText = loadedMB + ' / ' + totalMB + ' MB';
                        }
                    </script>

                    <div class="upload-area" style="cursor: default; border-color: rgba(239, 68, 68, 0.3); background: rgba(239, 68, 68, 0.05);">
                        <h3 style="color: #ef4444;">🚫 Yükleme Devre Dışı</h3>
                        <p style="font-size: 0.9rem; color: #9ca3af; margin-top: 8px;">BTK Yer Sağlayıcı belgesi bulunmadığı için güvenlik amacıyla dışarıdan dosya/ZIP yüklenmesi engellenmiştir. Sadece metin dosyaları oluşturabilirsiniz.</p>
                    </div>

                    {% if files or current_subpath != '' %}
                    <div class="file-bulk-actions" style="margin: 1rem 0; padding: 10px; background: rgba(255,255,255,0.05); border-radius: 8px; display: flex; gap: 10px; align-items: center;">
                        <button type="button" class="btn btn-primary" onclick="submitArchive()" style="background: linear-gradient(135deg, #6366f1, #4f46e5); font-size: 0.8rem; padding: 0.5rem 1rem;">📦 Arşivle</button>
                        <button type="button" class="btn btn-danger" onclick="submitBulkDelete()" style="font-size: 0.8rem; padding: 0.5rem 1rem;">🗑️ Seçilenleri Sil</button>
                    </div>

                    <div style="overflow-x: auto;">
                        <form id="bulkForm" method="POST" action="/archive">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                            <input type="hidden" name="current_path" value="{{ current_subpath }}">
                            <table class="file-list">
                                <thead>
                                    <tr>
                                        <th style="width: 40px;"><input type="checkbox" id="selectAll" onclick="toggleAllFiles(this)" style="width: 18px; height: 18px; cursor: pointer;"></th>
                                        <th style="width: 60px;">Tip</th>
                                        <th>Dosya Adı</th>
                                        <th>Boyut</th>
                                        <th style="text-align: right;">İşlemler</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {% if current_subpath != '' %}
                                    <tr>
                                        <td></td>
                                        <td>📁</td>
                                        <td><a href="/files/{{ parent_path }}" style="color: #93c5fd; text-decoration: none;">↩️ Üst Dizin</a></td>
                                        <td>-</td>
                                        <td></td>
                                    </tr>
                                    {% endif %}
                                    {% for item in files %}
                                    <tr>
                                        <td><input type="checkbox" name="selected_files[]" value="{{ item.name }}" class="file-checkbox" style="width: 18px; height: 18px; cursor: pointer;"></td>
                                        <td>{% if item.is_dir %}📁{% else %}📄{% endif %}</td>
                                        <td>
                                            {% if item.is_dir %}
                                                <a href="/files/{{ item.full_path }}" style="color: white; font-weight: 600; text-decoration: none;">
                                                    {{ item.name }}
                                                </a>
                                            {% else %}
                                                {% set is_editable = item.name.endswith('.txt') or item.name.endswith('.json') or item.name.endswith('.yml') or item.name.endswith('.yaml') or item.name.endswith('.properties') or item.name.endswith('.toml') or item.name.endswith('.conf') or item.name.endswith('.sh') or item.name.endswith('.log') or item.name.endswith('.secret') %}
                                                {% if is_editable %}
                                                    <a href="/edit/{{ item.full_path }}" style="color: white; font-weight: 600; text-decoration: none;">
                                                        {{ item.name }}
                                                    </a>
                                                    <a href="/edit/{{ item.full_path }}" style="color: #10b981; margin-left: 0.5rem; text-decoration: none; font-size: 0.8rem;">✏️ Düzenle</a>
                                                {% else %}
                                                    <span style="color: #e5e7eb;">{{ item.name }}</span>
                                                {% endif %}
                                            {% endif %}
                                        </td>
                                        <td>{% if not item.is_dir %}{{ item.size }} KB{% else %}-{% endif %}</td>
                                        <td>
                                            <div class="file-actions-dropdown">
                                                <button type="button" class="dots-btn" onclick="toggleDropdown(event, 'drop-{{ loop.index }}')">⋮</button>
                                                <div id="drop-{{ loop.index }}" class="dropdown-content">
                                                    <button type="button" onclick="renameItem('{{ item.name|e }}')">✏️ İsim Değiştir</button>
                                                    
                                                    {% if not item.is_dir %}
                                                        <a href="/download/{{ item.full_path }}">⬇️ İndir</a>
                                                        {% if item.name.lower().endswith('.zip') or item.name.lower().endswith('.rar') or item.name.lower().endswith('.7z') %}
                                                            <button type="button" onclick="unarchiveFile('{{ item.name|e }}')" style="color: #f59e0b; font-weight: bold;">📦 Arşivi Buraya Çıkar</button>
                                                        {% endif %}
                                                    {% endif %}
                                                    
                                                    <button type="button" onclick="deleteItem('{{ item.full_path }}', '{{ item.is_dir }}', '{{ item.name|e }}')" style="color: #ef4444;">🗑️ Sil</button>
                                                </div>
                                            </div>
                                        </td>
                                    </tr>
                                    {% endfor %}
                                </tbody>
                            </table>
                        </form>
                    </div>
                    {% else %}
                    <div class="alert alert-info" style="margin: 1rem;">
                        📭 Bu klasörde dosya bulunamadı.
                    </div>
                    {% endif %}

                    <script>
                        function toggleDropdown(event, id) {
                            event.stopPropagation();
                            // Close all others
                            document.querySelectorAll('.dropdown-content').forEach(d => {
                                if (d.id !== id) d.classList.remove('show');
                            });
                            document.getElementById(id).classList.toggle('show');
                        }
                        
                        // Close dropdowns when clicking anywhere else
                        window.onclick = function(event) {
                            if (!event.target.matches('.dots-btn')) {
                                document.querySelectorAll('.dropdown-content').forEach(d => {
                                    d.classList.remove('show');
                                });
                            }
                        }

                        function toggleAllFiles(source) {
                            const checkboxes = document.querySelectorAll('.file-checkbox');
                            checkboxes.forEach(cb => cb.checked = source.checked);
                        }
                        function submitArchive() {
                            const checked = document.querySelectorAll('.file-checkbox:checked');
                            if (checked.length === 0) { alert("Lütfen arşivlemek istediğiniz dosyaları seçin."); return; }
                            
                            // Loading overlay göster
                            showOverlay('📦', 'Arşiv Oluşturuluyor...', checked.length + ' dosya sıkıştırılıyor, lütfen bekleyin.');
                            document.getElementById('overlayProgressWrap').style.display = 'none';
                            
                            const formData = new FormData();
                            formData.append('csrf_token', '{{ csrf_token() }}');
                            formData.append('current_path', '{{ current_subpath }}');
                            checked.forEach(cb => formData.append('selected_files[]', cb.value));
                            
                            let dots = 0;
                            const animInterval = setInterval(() => {
                                dots = (dots + 1) % 4;
                                document.getElementById('overlayStatusText').innerText = 'Sıkıştırılıyor' + '.'.repeat(dots);
                            }, 400);
                            
                            fetch('/api/archive', {
                                method: 'POST',
                                body: formData,
                                headers: { 'X-Requested-With': 'XMLHttpRequest' }
                            })
                            .then(r => r.json())
                            .then(resp => {
                                clearInterval(animInterval);
                                document.getElementById('overlayProgressWrap').style.display = '';
                                if (resp.success) {
                                    document.getElementById('overlayIcon').innerText = '✅';
                                    document.getElementById('overlayTitle').innerText = 'Arşiv Hazır!';
                                    document.getElementById('overlaySubtitle').innerText = resp.filename + ' - ' + resp.file_count + ' dosya, ' + resp.size_kb + ' KB';
                                    document.getElementById('overlayProgressBar').style.width = '100%';
                                    document.getElementById('overlayProgressBar').style.background = 'linear-gradient(90deg, #10b981, #059669)';
                                    document.getElementById('overlayPercent').innerText = '100%';
                                    document.getElementById('overlayStatusText').innerText = 'Tamamlandı!';
                                    setTimeout(() => { hideOverlay(); window.location.reload(); }, 1200);
                                } else {
                                    hideOverlay();
                                    alert('❌ Arşiv oluşturulamadı: ' + (resp.error || 'Bilinmeyen hata'));
                                }
                            })
                            .catch(err => {
                                clearInterval(animInterval);
                                document.getElementById('overlayProgressWrap').style.display = '';
                                hideOverlay();
                                alert('❌ Bağlantı hatası: ' + err);
                            });
                        }
                        function submitBulkDelete() {
                            const checked = document.querySelectorAll('.file-checkbox:checked');
                            if (checked.length === 0) { alert("Lütfen seçim yapın."); return; }
                            if (!confirm("Seçilen " + checked.length + " öğeyi silmek istediğinize emin misiniz?")) return;
                            const f = document.getElementById('bulkForm');
                            f.action = '/bulk_delete'; f.submit();
                        }
                        function createNewFolder() {
                            const name = prompt("Yeni klasör adı:");
                            if (!name) return;
                            const f = document.createElement('form'); f.method = 'POST'; f.action = '/create_folder';
                            f.innerHTML = `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><input type="hidden" name="current_path" value="{{ current_subpath }}"><input type="hidden" name="folder_name" value="${name}">`;
                            document.body.appendChild(f); f.submit();
                        }
                        function createNewFile() {
                            const name = prompt("Yeni dosya adı (örn: server.properties):");
                            if (!name) return;
                            const f = document.createElement('form'); f.method = 'POST'; f.action = '/create_file';
                            f.innerHTML = `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><input type="hidden" name="current_path" value="{{ current_subpath }}"><input type="hidden" name="filename" value="${name}">`;
                            document.body.appendChild(f); f.submit();
                        }
                        function renameItem(oldName) {
                            const newName = prompt("Yeni adı girin:", oldName);
                            if (!newName || newName === oldName) return;
                            const f = document.createElement('form'); f.method = 'POST'; f.action = '/rename_item';
                            f.innerHTML = `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><input type="hidden" name="current_path" value="{{ current_subpath }}"><input type="hidden" name="old_name" value="${oldName}"><input type="hidden" name="new_name" value="${newName}">`;
                            document.body.appendChild(f); f.submit();
                        }
                        function deleteItem(path, isDir, name) {
                            if (!confirm(name + " silinecek. Emin misiniz?")) return;
                            const f = document.createElement('form'); f.method = 'POST'; f.action = '/delete_file';
                            f.innerHTML = `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><input type="hidden" name="item_path" value="${path}"><input type="hidden" name="is_dir" value="${isDir}"><input type="hidden" name="redirect_path" value="{{ current_subpath }}">`;
                            document.body.appendChild(f); f.submit();
                        }
                        /* ========= OVERLAY YARDIMCILARI ========= */
                        function showOverlay(icon, title, subtitle) {
                            const ov = document.getElementById('uploadOverlay');
                            document.getElementById('overlayIcon').innerText = icon;
                            document.getElementById('overlayTitle').innerText = title;
                            document.getElementById('overlaySubtitle').innerText = subtitle;
                            document.getElementById('overlayProgressBar').style.width = '0%';
                            document.getElementById('overlayPercent').innerText = '0%';
                            document.getElementById('overlayStatusText').innerText = '0 KB / ...';
                            ov.style.display = 'flex';
                        }
                        function hideOverlay() {
                            document.getElementById('uploadOverlay').style.display = 'none';
                        }
                        function setOverlayProgress(loaded, total) {
                            const pct = total > 0 ? Math.round((loaded / total) * 100) : 0;
                            document.getElementById('overlayProgressBar').style.width = pct + '%';
                            document.getElementById('overlayPercent').innerText = pct + '%';
                            const kb = n => n > 1048576 ? (n/1048576).toFixed(1)+' MB' : (n/1024).toFixed(0)+' KB';
                            document.getElementById('overlayStatusText').innerText = kb(loaded) + ' / ' + kb(total);
                        }
                        function showToast(msg, isError) {
                            const t = document.createElement('div');
                            t.style = `position:fixed;bottom:28px;right:28px;z-index:99999;padding:12px 20px;border-radius:12px;font-weight:600;font-size:0.92rem;color:#fff;box-shadow:0 8px 25px rgba(0,0,0,0.4);background:${isError?'#ef4444':'#10b981'};animation:slideIn 0.3s ease;`;
                            t.innerText = msg;
                            document.body.appendChild(t);
                            setTimeout(() => t.remove(), 4500);
                        }

                        /* ========= DOSYA YÜKLEME ========= */
                        function submitUploadForm(input) {
                            const form = input.closest('form');
                            if (!input.files || input.files.length === 0) return;

                            const file = input.files[0];
                            const isArchive = /\\.(zip|rar|7z)$/i.test(file.name);

                            if (isArchive) {
                                showOverlay('📦', 'Arşiv Yükleniyor...', 'ZIP/RAR/7Z dosyası sunucuya aktarılıyor.');
                            } else {
                                showOverlay('📤', 'Dosya Yükleniyor...', 'Lütfen bekleyin, sayfayı kapatmayın.');
                            }

                            // /api/upload endpoint'ini kullan (JSON yanıt döner)
                            const apiAction = form.action.replace('/upload', '/api/upload');
                            const formData = new FormData(form);
                            formData.delete('file');
                            formData.append('file', file);

                            const xhr = new XMLHttpRequest();
                            xhr.open('POST', apiAction, true);
                            xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');

                            xhr.upload.onprogress = function(e) {
                                if (e.lengthComputable) {
                                    setOverlayProgress(e.loaded, e.total);
                                    if (e.loaded >= e.total && isArchive) {
                                        document.getElementById('overlayIcon').innerText = '🗜️';
                                        document.getElementById('overlayTitle').innerText = 'Arşiv Açılıyor...';
                                        document.getElementById('overlaySubtitle').innerText = 'Dosyalar çıkartılıyor, lütfen bekleyin.';
                                        document.getElementById('overlayProgressBar').style.background = 'linear-gradient(90deg,#f59e0b,#ef4444)';
                                        document.getElementById('overlayPercent').innerText = '⏳';
                                        document.getElementById('overlayStatusText').innerText = 'Sunucuda işleniyor...';
                                    }
                                }
                            };

                            xhr.onload = function() {
                                hideOverlay();
                                try {
                                    const resp = JSON.parse(xhr.responseText);
                                    if (resp.success) {
                                        if (resp.extracted) {
                                            showToast('✅ ' + resp.filename + ' başarıyla açıldı!', false);
                                        } else {
                                            showToast('✅ ' + resp.filename + ' yüklendi!', false);
                                        }
                                        setTimeout(() => window.location.reload(), 1200);
                                    } else {
                                        showToast('❌ Hata: ' + (resp.error || 'Bilinmeyen hata'), true);
                                        setTimeout(() => window.location.reload(), 2500);
                                    }
                                } catch(ex) {
                                    // JSON değilse yönlendirme URL'si var demektir
                                    if (xhr.responseURL) window.location.href = xhr.responseURL;
                                    else window.location.reload();
                                }
                            };

                            xhr.onerror = function() {
                                hideOverlay();
                                showToast('❌ Bağlantı hatası: Yükleme başarısız.', true);
                                setTimeout(() => window.location.reload(), 2500);
                            };

                            xhr.send(formData);
                        }

                        /* ========= ARŞİV ÇIKAR (sağ klik menüsünden) ========= */
                        function unarchiveFile(filename) {
                            if (!confirm(filename + ' buraya çıkartılsın mı?')) return;

                            showOverlay('🗜️', 'Arşiv Açılıyor...', filename + ' dosyasındaki içerikler çıkartılıyor.');
                            document.getElementById('overlayProgressWrap').style.display = 'none';

                            const formData = new FormData();
                            formData.append('csrf_token', '{{ csrf_token() }}');
                            formData.append('current_path', '{{ current_subpath }}');
                            formData.append('filename', filename);

                            fetch('/api/unarchive', {
                                method: 'POST',
                                body: formData,
                                headers: { 'X-Requested-With': 'XMLHttpRequest' }
                            })
                            .then(r => r.json())
                            .then(resp => {
                                document.getElementById('overlayProgressWrap').style.display = '';
                                hideOverlay();
                                if (resp.success) {
                                    showToast('✅ Arşiv başarıyla açıldı: ' + filename, false);
                                    setTimeout(() => window.location.reload(), 1200);
                                } else {
                                    showToast('❌ Arşiv açılamadı: ' + (resp.error || 'Bilinmeyen hata'), true);
                                }
                            })
                            .catch(err => {
                                document.getElementById('overlayProgressWrap').style.display = '';
                                hideOverlay();
                                showToast('❌ Bağlantı hatası: ' + err, true);
                            });
                        }

                        const dropZone = document.getElementById('dropZone');
                        if (dropZone) {
                            ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(ev => {
                                dropZone.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); });
                            });
                            ['dragenter', 'dragover'].forEach(ev => {
                                dropZone.addEventListener(ev, () => dropZone.classList.add('dragover'));
                            });
                            ['dragleave', 'drop'].forEach(ev => {
                                dropZone.addEventListener(ev, () => dropZone.classList.remove('dragover'));
                            });
                            dropZone.addEventListener('drop', e => {
                                const files = e.dataTransfer.files;
                                if (files.length > 0) {
                                    const dt = new DataTransfer();
                                    dt.items.add(files[0]);
                                    const fileInput = document.getElementById('fileUpload');
                                    fileInput.files = dt.files;
                                    submitUploadForm(fileInput);
                                }
                            });
                        }
                    </script>
                </div>

            {% elif page=="settings" %}
                <!-- Ayarlar Paneli -->
                <div class="card">
                    <div class="card-header">
                        <h2>⚙️ Sunucu Ayarları</h2>
                    </div>

                    <form method="POST" action="/setoptions">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                        <div class="form-group">
                            <label class="form-label">☕ Java Yolu</label>
                            <input type="text" class="form-input" name="java_path" value="{{ java_path }}" 
                                placeholder="C:\\Program Files\\Java\\jdk-21\\bin\\java.exe" {% if not is_admin %}readonly style="opacity: 0.7; cursor: not-allowed;"{% endif %}>
                            {% if not is_admin %}
                            <small style="color: var(--warning); display: block; margin-top: 5px;">⚠️ Bu ayar yönetici tarafından kilitlenmiştir (Otomatik Algılanır)</small>
                            {% endif %}
                        </div>

                        <div class="stats-grid">
                            <div class="form-group">
                                <label class="form-label">📊 Minimum RAM</label>
                                <input type="text" class="form-input" name="min_ram" value="{{ server_settings.min_ram }}"
                                    placeholder="1024M" {% if not is_admin %}readonly style="opacity: 0.7; cursor: not-allowed;"{% endif %}>
                            </div>
                            <div class="form-group">
                                <label class="form-label">📈 Maksimum RAM</label>
                                <input type="text" class="form-input" name="max_ram" value="{{ server_settings.max_ram }}"
                                    placeholder="2048M" {% if not is_admin %}readonly style="opacity: 0.7; cursor: not-allowed;"{% endif %}>
                            </div>
                        </div>
                        {% if not is_admin %}
                        <small style="color: var(--warning); display: block; margin-top: -10px; margin-bottom: 15px;">⚠️ RAM miktarı paketinizle sınırlandırılmıştır</small>
                        {% endif %}

                        <div class="form-group">
                            <label class="form-label">🔧 Java Parametreleri</label>
                            <textarea class="form-input" name="java_args" rows="3" 
                                    placeholder="-XX:+UseG1GC -XX:+UnlockExperimentalVMOptions" {% if not is_admin %}readonly style="opacity: 0.7; cursor: not-allowed;"{% endif %}>{{ server_settings.java_args }}</textarea>
                        </div>

                        {% if is_admin %}
                        <button class="btn btn-primary" type="submit">💾 Ayarları Kaydet</button>
                        {% else %}
                        <div class="alert alert-warning">
                            🔒 Ayarlar sadece yöneticiler tarafından değiştirilebilir.
                        </div>
                        {% endif %}
                    </form>
                </div>

                {% if is_admin %}
                <div class="card" style="margin-top: 2rem; border: 1px solid #ef4444;">
                    <div class="card-header">
                        <h2 style="color: #ef4444;">🛡️ Güvenlik ve DDoS Kayıtları</h2>
                    </div>
                    <div class="console-container" style="height: 300px;">
                        <div class="console-content" style="color: #ff4444;">
                            {% if ddos_logs %}
                                {% for log in ddos_logs %}
                                    {{ log }}<br>
                                {% endfor %}
                            {% else %}
                                <span style="color: #10b981;">✅ Henüz şüpheli bir işlem veya saldırı tespit edilmedi.</span>
                            {% endif %}
                        </div>
                    </div>
                    <div class="alert alert-info" style="margin-top: 1rem; border-left-color: #ef4444; background: rgba(239, 68, 68, 0.1); color: #fca5a5;">
                        ℹ️ Buradaki IP adresleri sunucuya çok hızlı istek gönderen (Flood/DDoS) kaynaklardır. Bu adresleri engelleyebilirsiniz.
                    </div>
                </div>
                {% endif %}

            <!-- ===== KULLANICI ERİŞİMLİ AYARLAR ===== -->
            <div class="card" style="margin-top: 2rem; border: 1px solid rgba(99,102,241,0.4); background: rgba(99,102,241,0.04);">
                <div class="card-header">
                    <h2 style="color: #818cf8;">🔧 Sunucu Yapılandırması</h2>
                </div>
                <p style="color:#9ca3af; margin-bottom:1.5rem; font-size:0.9rem;">Bu ayarlar size özeldir, kendiniz yönetebilirsiniz.</p>
                <form method="POST" action="/user/update_java">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <div class="form-group">
                        <label class="form-label">☕ Java Sürümü</label>
                        <select class="form-input" name="java_version">
                            <option value="21" {% if user_data.get('server_config', {}).get('java_version', '21') == '21' %}selected{% endif %}>Java 21 LTS (Önerilir — 1.17+ için)</option>
                            <option value="17" {% if user_data.get('server_config', {}).get('java_version', '') == '17' %}selected{% endif %}>Java 17 (1.17–1.20 için)</option>
                            <option value="8" {% if user_data.get('server_config', {}).get('java_version', '') == '8' %}selected{% endif %}>Java 8 (Eski Sürümler)</option>
                        </select>
                        <small style="color:#6b7280; margin-top:5px; display:block;">⚠️ Değişiklik sonrası sunucunuzu yeniden başlatmanız gerekir.</small>
                    </div>
                    <button class="btn btn-primary" type="submit" style="margin-top:0.5rem;">☕ Java Sürümünü Güncelle</button>
                </form>
            </div>

            <!-- Sürüm Değiştirme -->
            <div class="card" style="margin-top: 2rem; border: 1px solid rgba(16,185,129,0.35); background: rgba(16,185,129,0.04);">
                <div class="card-header">
                    <h2 style="color:#34d399;">🧩 Sunucu Sürümü</h2>
                </div>
                <p style="color:#9ca3af; margin-bottom:1.2rem; font-size:0.9rem;">Sunucu kapalıyken istediğin Paper sürümünü kurabilirsin. Dünya dosyaların korunur.</p>
                <form method="POST" action="/server/version">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <div class="form-group">
                        <label class="form-label">Paper Sürümü</label>
                        <select class="form-input" name="version">
                            <option value="1.21.11">1.21.11</option>
                            <option value="1.21.10">1.21.10</option>
                            <option value="1.21.9">1.21.9</option>
                            <option value="1.21.8">1.21.8</option>
                            <option value="1.21.7">1.21.7</option>
                            <option value="1.21.6">1.21.6</option>
                            <option value="1.21.5">1.21.5</option>
                            <option value="1.21.4">1.21.4</option>
                            <option value="1.21.3">1.21.3</option>
                            <option value="1.21.2">1.21.2</option>
                            <option value="1.21.1">1.21.1</option>
                            <option value="1.21">1.21</option>
                            <option value="1.20.6">1.20.6</option>
                            <option value="1.20.4">1.20.4</option>
                            <option value="1.20.1">1.20.1</option>
                            <option value="1.20">1.20</option>
                            <option value="1.19.4">1.19.4</option>
                            <option value="1.19.2">1.19.2</option>
                            <option value="1.18.2">1.18.2</option>
                            <option value="1.17.1">1.17.1</option>
                            <option value="1.16.5">1.16.5</option>
                            <option value="1.12.2">1.12.2</option>
                            <option value="1.8.8">1.8.8</option>
                        </select>
                    </div>
                    <button class="btn btn-primary" type="submit">🔄 Sürümü Kur</button>
                </form>
            </div>

            <!-- Dünya Değiştirme -->
            <div class="card" style="margin-top: 2rem; border: 1px solid rgba(99,102,241,0.35); background: rgba(99,102,241,0.04);">
                <div class="card-header">
                    <h2 style="color:#818cf8;">🌍 Dünya</h2>
                </div>
                <p style="color:#9ca3af; margin-bottom:1.2rem; font-size:0.9rem;">Dünya adını değiştirerek yeni bir dünya oluşturabilir veya mevcut dünyayı seçebilirsin. Eski dünya silinmez.</p>
                <form method="POST" action="/server/world">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <div class="stats-grid">
                        <div class="form-group">
                            <label class="form-label">Dünya Adı</label>
                            <input type="text" class="form-input" name="level_name" value="{{ server_props.get('level-name', 'world') }}" placeholder="world">
                        </div>
                        <div class="form-group">
                            <label class="form-label">Seed (opsiyonel)</label>
                            <input type="text" class="form-input" name="level_seed" placeholder="Boş bırak = rastgele">
                        </div>
                    </div>
                    <button class="btn btn-primary" type="submit">🌱 Dünyayı Ayarla</button>
                </form>
            </div>

            <!-- ===== TEHLİKELİ BÖLGE ===== -->
            <div class="card" style="margin-top: 2rem; border: 2px solid #ef4444; background: rgba(239,68,68,0.05);">
                <div class="card-header">
                    <h2 style="color: #ef4444;">⚠️ Tehlikeli Bölge</h2>
                </div>
                <p style="color:#9ca3af; margin-bottom:1.5rem; font-size:0.9rem;">
                    Bu işlemler <strong style="color:#ef4444;">geri alınamaz!</strong> Sunucunuzun tüm dosyaları,
                    dünyaları ve eklentileri kalıcı olarak silinir.
                </p>
                <button class="btn btn-danger" onclick="showDeleteServerModal()" id="deleteServerBtn" style="width:100%; font-size:1rem; padding:1rem; letter-spacing:0.5px;">
                    🗑️ Sunucuyu Kalıcı Olarak Sil
                </button>
            </div>

            <!-- ===== SUNUCU SİLME ONAY MODALI ===== -->
            <div id="deleteServerModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.88); z-index:99999; align-items:center; justify-content:center; backdrop-filter:blur(4px);">
                <div style="background:linear-gradient(135deg,#1a1a2e,#1e1e2f); border:2px solid #ef4444; border-radius:24px; padding:2.5rem 2rem; max-width:460px; width:90%; text-align:center; box-shadow:0 0 60px rgba(239,68,68,0.3); animation:fadeIn 0.2s ease;">
                    <div style="font-size:4rem; margin-bottom:0.75rem;">🚨</div>
                    <h2 style="color:#ef4444; font-size:1.8rem; margin-bottom:0.75rem;">Sunucuyu Sil</h2>
                    <p style="color:#d1d5db; margin-bottom:0.5rem;">Bu işlem <strong style="color:white;">geri alınamaz!</strong></p>
                    <p style="color:#9ca3af; margin-bottom:1.5rem; font-size:0.9rem; line-height:1.6;">
                        Sunucunuzun <strong style="color:#ef4444;">tüm dosyaları</strong>, dünyaları ve eklentileri
                        kalıcı olarak silinecek, ardından plan seçim ekranına yönlendirileceksiniz.
                    </p>
                    <div style="background:rgba(239,68,68,0.1); border:1px solid rgba(239,68,68,0.3); border-radius:12px; padding:0.75rem 1rem; margin-bottom:1.5rem;">
                        <span style="color:#fca5a5; font-size:0.9rem;">⚠️ Emin misiniz? Bu işlem <strong>geri alınamaz!</strong></span>
                    </div>
                    <div style="display:flex; gap:1rem; justify-content:center; flex-wrap:wrap;">
                        <button onclick="hideDeleteServerModal()" class="btn" style="background:rgba(255,255,255,0.1); color:white; flex:1; min-width:130px; border:1px solid rgba(255,255,255,0.2);">
                            ✖ Vazgeç
                        </button>
                        <form method="POST" action="/delete_server" style="flex:1; min-width:130px; margin:0;">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                            <button type="submit" class="btn btn-danger" style="width:100%;">
                                🗑️ Evet, Sil
                            </button>
                        </form>
                    </div>
                </div>
            </div>
            <script>
            function showDeleteServerModal() {
                var m = document.getElementById('deleteServerModal');
                m.style.display = 'flex';
                document.body.style.overflow = 'hidden';
            }
            function hideDeleteServerModal() {
                var m = document.getElementById('deleteServerModal');
                m.style.display = 'none';
                document.body.style.overflow = '';
            }
            document.addEventListener('keydown', function(e) {
                if (e.key === 'Escape') hideDeleteServerModal();
            });
            </script>

            {% elif page=="properties" %}
                <div class="card">
                    <div class="card-header">
                        <h2>⚙️ Sunucu Özellikleri</h2>
                    </div>
                    <form method="POST" action="/server_properties" class="form">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                        <div class="stats-grid">
                            <div class="form-group">
                                <label class="form-label">PVP</label>
                                <select class="form-input" name="pvp">
                                    <option value="true" {% if server_props.get('pvp','true')=='true' %}selected{% endif %}>Açık</option>
                                    <option value="false" {% if server_props.get('pvp','true')=='false' %}selected{% endif %}>Kapalı</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Oyun Modu</label>
                                <select class="form-input" name="gamemode">
                                    <option value="survival" {% if server_props.get('gamemode','survival')=='survival' %}selected{% endif %}>Survival</option>
                                    <option value="creative" {% if server_props.get('gamemode','survival')=='creative' %}selected{% endif %}>Creative</option>
                                    <option value="adventure" {% if server_props.get('gamemode','survival')=='adventure' %}selected{% endif %}>Adventure</option>
                                    <option value="spectator" {% if server_props.get('gamemode','survival')=='spectator' %}selected{% endif %}>Spectator</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Zorluk</label>
                                <select class="form-input" name="difficulty">
                                    <option value="peaceful" {% if server_props.get('difficulty','normal')=='peaceful' %}selected{% endif %}>Peaceful</option>
                                    <option value="easy" {% if server_props.get('difficulty','normal')=='easy' %}selected{% endif %}>Easy</option>
                                    <option value="normal" {% if server_props.get('difficulty','normal')=='normal' %}selected{% endif %}>Normal</option>
                                    <option value="hard" {% if server_props.get('difficulty','normal')=='hard' %}selected{% endif %}>Hard</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Whitelist</label>
                                <select class="form-input" name="white-list">
                                    <option value="true" {% if server_props.get('white-list','false')=='true' %}selected{% endif %}>Açık</option>
                                    <option value="false" {% if server_props.get('white-list','false')=='false' %}selected{% endif %}>Kapalı</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Maksimum Oyuncu</label>
                                <input class="form-input" type="number" min="1" max="1000" name="max-players" value="{{ server_props.get('max-players','20') }}">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Uçuş İzni</label>
                                <select class="form-input" name="allow-flight">
                                    <option value="true" {% if server_props.get('allow-flight','false')=='true' %}selected{% endif %}>Açık</option>
                                    <option value="false" {% if server_props.get('allow-flight','false')=='false' %}selected{% endif %}>Kapalı</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Görüş Mesafesi</label>
                                <input class="form-input" type="number" min="2" max="32" name="view-distance" value="{{ server_props.get('view-distance','10') }}">
                            </div>
                            <div class="form-group">
                                <label class="form-label">MOTD</label>
                                <input class="form-input" type="text" name="motd" value="{{ server_props.get('motd','Skyvex Sunucu') }}">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Komut Bloğu</label>
                                <select class="form-input" name="enable-command-block">
                                    <option value="true" {% if server_props.get('enable-command-block','false')=='true' %}selected{% endif %}>Açık</option>
                                    <option value="false" {% if server_props.get('enable-command-block','false')=='false' %}selected{% endif %}>Kapalı</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Nether İzni</label>
                                <select class="form-input" name="allow-nether">
                                    <option value="true" {% if server_props.get('allow-nether','true')=='true' %}selected{% endif %}>Açık</option>
                                    <option value="false" {% if server_props.get('allow-nether','true')=='false' %}selected{% endif %}>Kapalı</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Zorla Oyun Modu</label>
                                <select class="form-input" name="force-gamemode">
                                    <option value="true" {% if server_props.get('force-gamemode','false')=='true' %}selected{% endif %}>Açık</option>
                                    <option value="false" {% if server_props.get('force-gamemode','false')=='false' %}selected{% endif %}>Kapalı</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Hardcore</label>
                                <select class="form-input" name="hardcore">
                                    <option value="true" {% if server_props.get('hardcore','false')=='true' %}selected{% endif %}>Açık</option>
                                    <option value="false" {% if server_props.get('hardcore','false')=='false' %}selected{% endif %}>Kapalı</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Online Mod</label>
                                <select class="form-input" name="online-mode">
                                    <option value="true" {% if server_props.get('online-mode','true')=='true' %}selected{% endif %}>Gerçek</option>
                                    <option value="false" {% if server_props.get('online-mode','true')=='false' %}selected{% endif %}>Sahte</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Simülasyon Mesafesi</label>
                                <input class="form-input" type="number" min="2" max="12" name="simulation-distance" value="{{ server_props.get('simulation-distance','10') }}">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Spawn Koruması</label>
                                <input class="form-input" type="number" min="0" max="64" name="spawn-protection" value="{{ server_props.get('spawn-protection','16') }}">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Dünya Boyutu</label>
                                <input class="form-input" type="number" min="1000" max="29999984" name="max-world-size" value="{{ server_props.get('max-world-size','29999984') }}">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Sunucu Portu (Özel Atanmış)</label>
                                <input class="form-input" type="number" name="server-port" value="{{ user_data.get('assigned_port', 25565) }}" readonly style="opacity: 0.7; cursor: not-allowed;">
                                <small style="color: #6b7280;">⚠️ Port çakışmasını önlemek için sistem tarafından otomatik atanmıştır.</small>
                            </div>

                            <div class="form-group" style="grid-column: 1 / span 2;">
                                <label class="form-label">Resource Pack URL</label>
                                <input class="form-input" type="url" name="resource-pack" value="{{ server_props.get('resource-pack','') }}">
                            </div>
                            <div class="form-group" style="grid-column: 1 / span 2;">
                                <label class="form-label">Resource Pack Mesajı</label>
                                <input class="form-input" type="text" name="resource-pack-prompt" value="{{ server_props.get('resource-pack-prompt','') }}">
                            </div>
                        </div>
                        <button class="btn btn-primary" type="submit" style="margin-top: 0.75rem;">Kaydet</button>
                    </form>
                </div>

            {% elif page=="plugins" %}
                <div class="card" style="background:transparent; border:none; box-shadow:none; padding:0;">
                    <div class="card-header" style="background:rgba(30,30,47,0.4); backdrop-filter:blur(10px); border:1px solid rgba(255,255,255,0.05); border-radius:18px; padding:1.5rem 2rem; margin-bottom:2rem; display:flex; justify-content:space-between; align-items:center;">
                        <div>
                            <h2 style="font-size:1.8rem; font-weight:800; letter-spacing:-0.5px; margin:0;">🧩 Eklenti Yönetimi</h2>
                            <p style="color:var(--gray); margin-top:5px; font-size:0.9rem;">Sunucunuzu eklentilerle özelleştirin ve geliştirin.</p>
                        </div>
                        <div style="background:rgba(16,185,129,0.1); border:1px solid rgba(16,185,129,0.2); padding:0.5rem 1rem; border-radius:12px; color:#10b981; font-weight:700; font-size:0.9rem;">
                            {{ plugins|length }} Aktif Eklenti
                        </div>
                    </div>

                    <div style="display:flex; flex-wrap:wrap; gap:2rem; align-items:flex-start;">
                        <!-- Sol Kolon: Yüklü Eklentiler -->
                        <div class="card" style="flex:1; min-width:300px; max-width:400px; position:sticky; top:2rem; background:rgba(20,20,35,0.6); backdrop-filter:blur(15px); border:1px solid rgba(255,255,255,0.05); border-radius:24px; padding:1.8rem;">
                            <h3 style="font-size:1.2rem; font-weight:700; margin-bottom:1.5rem; display:flex; align-items:center; gap:10px;">📋 Yüklü Eklentiler</h3>
                            
                            {% if plugins and plugins|length > 0 %}
                                <div style="display:flex; flex-direction:column; gap:12px; max-height:500px; overflow-y:auto; padding-right:8px;" class="custom-scrollbar">
                                    {% for pl in plugins %}
                                    <div style="display:flex; justify-content:space-between; align-items:center; background:rgba(255,255,255,0.03); padding:1rem; border-radius:16px; border:1px solid rgba(255,255,255,0.03); transition:all 0.3s ease;" class="plugin-item">
                                        <div style="display:flex; align-items:center; gap:12px;">
                                            <div style="width:36px; height:36px; background:linear-gradient(135deg, #6366f1, #a855f7); border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:1.2rem;">🧩</div>
                                            <div style="font-weight:600; font-size:0.95rem; color:#f1f5f9; overflow:hidden; text-overflow:ellipsis; max-width:140px;">{{ pl }}</div>
                                        </div>
                                        <form method="POST" action="/plugins/delete" style="margin:0;">
                                            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                                            <input type="hidden" name="plugin_name" value="{{ pl }}">
                                            <button class="btn-danger" type="submit" style="padding: 8px; border-radius: 10px; border:none; background:rgba(239,68,68,0.1); color:#ef4444; cursor:pointer; transition:0.2s;" onmouseover="this.style.background='rgba(239,68,68,0.2)'" onmouseout="this.style.background='rgba(239,68,68,0.1)'" onclick="return confirm('{{ pl }} kalıcı olarak silinecek. Emin misiniz?');">
                                                <i class="fas fa-trash-alt"></i> 🗑️
                                            </button>
                                        </form>
                                    </div>
                                    {% endfor %}
                                </div>
                            {% else %}
                                <div style="text-align:center; padding:3rem 1rem; color:var(--gray); background:rgba(255,255,255,0.02); border-radius:20px; border:1px dashed rgba(255,255,255,0.1);">
                                    <div style="font-size:3rem; margin-bottom:1rem; opacity:0.3;">📦</div>
                                    <p>Henüz eklenti yüklenmemiş.</p>
                                </div>
                            {% endif %}
                        </div>

                        <!-- Sağ Kolon: Mağaza / Arama -->
                        <div style="flex:2; min-width:400px; display:flex; flex-direction:column; gap:2rem;">
                            <!-- Search Bar -->
                            <div class="card" style="background:rgba(30,30,47,0.6); border:1px solid rgba(255,255,255,0.05); border-radius:24px; padding:1.5rem;">
                                <div style="display:flex; gap:12px; align-items:center; position:relative;">
                                    <div style="position:absolute; left:1.5rem; color:var(--primary); font-size:1.2rem;">🔍</div>
                                    <input id="plugin-search-input" class="form-input" type="text" 
                                        placeholder="Eklenti ara (örn: EssentialsX, LuckPerms, WorldEdit)" 
                                        style="padding-left:3.5rem; padding-right:1rem; height:60px; border-radius:18px; font-size:1.1rem; background:rgba(10,10,20,0.4); border:1px solid rgba(255,255,255,0.05);">
                                    <button class="btn btn-primary" type="button" onclick="searchPlugins()" style="height:60px; padding:0 2rem; border-radius:18px; font-weight:700; white-space:nowrap; background:linear-gradient(135deg, var(--primary), var(--secondary));">Eklenti Bul</button>
                                </div>
                                <div style="display:flex; gap:10px; margin-top:1.5rem; flex-wrap:wrap;">
                                    <span style="color:var(--gray); font-size:0.85rem; font-weight:700; text-transform:uppercase; margin-right:10px; align-self:center;">Öneriler:</span>
                                    <button onclick="document.getElementById('plugin-search-input').value='Essentials'; searchPlugins();" class="category-chip">#Essentials</button>
                                    <button onclick="document.getElementById('plugin-search-input').value='Koruma'; searchPlugins();" class="category-chip">#Koruma</button>
                                    <button onclick="document.getElementById('plugin-search-input').value='Ekonomi'; searchPlugins();" class="category-chip">#Ekonomi</button>
                                    <button onclick="document.getElementById('plugin-search-input').value='Oyun'; searchPlugins();" class="category-chip">#Oyun</button>
                                    <button onclick="document.getElementById('plugin-search-input').value='Görünüm'; searchPlugins();" class="category-chip">#Görünüm</button>
                                </div>
                                <style>
                                    .category-chip { background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.05); padding:6px 14px; border-radius:100px; color:#94a3b8; font-size:0.85rem; cursor:pointer; transition:0.2s; font-weight:600; }
                                    .category-chip:hover { background:var(--primary); color:white; transform:translateY(-2px); border-color:transparent; }
                                    .plugin-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap:1.5rem; }
                                    .plugin-card-new { background:rgba(255,255,255,0.02); border:1px solid rgba(255,255,255,0.05); border-radius:24px; padding:1.8rem; display:flex; flex-direction:column; transition:0.3s; position:relative; overflow:hidden; }
                                    .plugin-card-new:hover { background:rgba(255,255,255,0.04); transform:translateY(-5px); border-color:var(--primary); box-shadow:0 15px 30px rgba(0,0,0,0.3); }
                                    .plugin-badge { position:absolute; top:1.2rem; right:1.2rem; background:rgba(16,185,129,0.1); color:#10b981; padding:4px 10px; border-radius:8px; font-size:0.7rem; font-weight:800; text-transform:uppercase; }
                                    .custom-scrollbar::-webkit-scrollbar { width: 6px; }
                                    .custom-scrollbar::-webkit-scrollbar-track { background: transparent; }
                                    .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(99,102,241,0.2); border-radius: 10px; }
                                    .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: rgba(99,102,241,0.4); }
                                </style>
                            </div>

                            <!-- Results Area -->
                            <div id="plugin-results" class="plugin-grid">
                                <!-- JS will fill this -->
                                <div class="alert alert-info">⌨️ Eklenti adı yazın veya üstteki önerilere tıklayın.</div>
                            </div>
                        </div>
                    </div>
                </div>

            {% elif page=="edit" %}
                <div class="card">
                    <div class="card-header">
                        <h2>✏️ Dosya Düzenleyici: {{ edit_item_path }}</h2>
                        <a href="/files/{{ edit_dir_path }}" class="btn" style="background: var(--gray); color: white;">Geri Dön</a>
                    </div>
                    <form method="POST" action="/edit/{{ edit_item_path }}">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                        <div class="form-group">
                            <textarea name="content" class="form-input" style="height: 500px; font-family: monospace; background: #000; color: #10b981;">{{ edit_content }}</textarea>
                        </div>
                        <button class="btn btn-primary" type="submit">💾 Kaydet</button>
                    </form>
                </div>
            {% elif page=="players" %}
                <div class="card">
                    <div class="card-header">
                        <h2>👥 Oyuncular <span style="font-size:13px;color:#34d399;font-weight:600;">({{ known_players|length }} çevrimiçi)</span></h2>
                    </div>
                    <div class="form-group" style="display:flex; gap:10px; margin-bottom: 2rem; background: rgba(0,0,0,0.2); padding: 15px; border-radius: 12px;">
                        <input id="player-action-name" class="form-input" type="text" placeholder="Oyuncu Adı (örn: Notch)">
                        <button type="button" class="btn btn-primary" onclick="sendPlayerAction('op')">Op Yap 👑</button>
                        <button type="button" class="btn" style="background: #f59e0b; color: white;" onclick="sendPlayerAction('kick')">Oyundan At 🚀</button>
                        <button type="button" class="btn btn-danger" onclick="sendPlayerAction('ban')">Banla 🔨</button>
                    </div>
                    
                    <div class="card-header">
                        <h3>Çevrimiçi Oyuncular <span id="playerCountHint" style="font-size:12px;color:#9ca3af;font-weight:500;"></span></h3>
                    </div>
                    <div id="player-list" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 1rem;">
                        {% if known_players %}
                            {% for p in known_players %}
                            <div style="background: rgba(255,255,255,0.05); padding: 1rem; border-radius: 12px; display: flex; align-items: center; justify-content: space-between;">
                                <div style="display: flex; align-items: center; gap: 10px;">
                                    <div style="width: 40px; height: 40px; background: url('https://minotar.net/helm/{{ p }}/40.png') no-repeat center center; border-radius: 8px;"></div>
                                    <span style="font-weight: 600;">{{ p }}</span>
                                </div>
                                <div style="display: flex; gap: 5px;">
                                    <button onclick="document.getElementById('player-action-name').value='{{ p }}'" class="btn" style="padding: 5px 10px; background: rgba(255,255,255,0.1); font-size: 0.8rem;">Seç</button>
                                </div>
                            </div>
                            {% endfor %}
                        {% else %}
                            <p style="color: #9ca3af;">Şu an çevrimiçi oyuncu yok.</p>
                        {% endif %}
                    </div>
                    
                    <script>
                        function renderPlayers(list) {
                            const box = document.getElementById('player-list');
                            if (!box) return;
                            if (!list || !list.length) {
                                box.innerHTML = '<p style="color:#9ca3af;">Şu an çevrimiçi oyuncu yok.</p>';
                                return;
                            }
                            box.innerHTML = list.map(p => `
                                <div style="background:rgba(255,255,255,0.05);padding:1rem;border-radius:12px;display:flex;align-items:center;justify-content:space-between;">
                                    <div style="display:flex;align-items:center;gap:10px;">
                                        <div style="width:40px;height:40px;background:url('https://minotar.net/helm/${p}/40.png') no-repeat center center;border-radius:8px;"></div>
                                        <span style="font-weight:600;">${p}</span>
                                    </div>
                                    <button onclick="document.getElementById('player-action-name').value='${p}'" class="btn" style="padding:5px 10px;background:rgba(255,255,255,0.1);font-size:0.8rem;">Seç</button>
                                </div>
                            `).join('');
                            const hint = document.getElementById('playerCountHint');
                            if (hint) hint.textContent = `(${list.length} çevrimiçi)`;
                        }

                        function refreshPlayers() {
                            if (!document.getElementById('player-list')) return;
                            fetch('/api/players')
                                .then(r => r.json())
                                .then(d => { if (d && d.players) renderPlayers(d.players); })
                                .catch(() => {});
                        }
                        setInterval(refreshPlayers, 10000);

                        function sendPlayerAction(actionType) {
                            const pName = document.getElementById('player-action-name').value.trim();
                            if(!pName) { showNotification('⚠️ Oyuncu adi girin', 'error'); return; }
                            const cmd = actionType + ' ' + pName;
                            fetch('/api/console/send', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': '{{ csrf_token() }}' },
                                body: JSON.stringify({ command: cmd })
                            }).then(r => r.json()).then(res => {
                                if(res.error) showNotification('❌ Hata: ' + res.error, 'error');
                                else showNotification('✅ Komut basariyla calistirildi: ' + actionType, 'success');
                            });
                        }
                    </script>
                </div>
            {% elif page=="access" %}
                <div class="card">
                    <div class="card-header">
                        <h2>👥 Erişim Yönetimi</h2>
                    </div>
                    <div class="card" style="background: rgba(255,255,255,0.02); margin-top: 1rem;">
                        <div class="card-header">
                            <h3>➕ Yeni Erişim Ekle</h3>
                        </div>
                        <form action="/api/access/add" method="POST">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                            <div class="form-group">
                                <label class="form-label">Kullanıcı Adı</label>
                                <input type="text" name="target_username" class="form-input" placeholder="Erişim verilecek kişinin kullanıcı adı" required>
                            </div>
                            <label class="form-label">İzinler</label>
                            <div class="stats-grid" style="grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));">
                                <label style="display:flex; gap:8px; cursor:pointer;"><input type="checkbox" name="perms" value="start" checked> Başlat</label>
                                <label style="display:flex; gap:8px; cursor:pointer;"><input type="checkbox" name="perms" value="stop" checked> Durdur</label>
                                <label style="display:flex; gap:8px; cursor:pointer;"><input type="checkbox" name="perms" value="console" checked> Konsol</label>
                                <label style="display:flex; gap:8px; cursor:pointer;"><input type="checkbox" name="perms" value="files"> Dosyalar</label>
                                <label style="display:flex; gap:8px; cursor:pointer;"><input type="checkbox" name="perms" value="settings"> Ayarlar</label>
                            </div>
                            <button type="submit" class="btn btn-primary" style="margin-top: 1.5rem; width: 100%;">Erişim Ver ✨</button>
                        </form>
                    </div>

                    <div class="card" style="background: rgba(255,255,255,0.02); margin-top: 1.5rem;">
                        <div class="card-header">
                            <h3>👥 Mevcut Erişimler</h3>
                        </div>
                        <div class="user-list">
                            {% for u, perms in shared_users.items() %}
                            <div style="display:flex; justify-content:space-between; align-items:center; background: rgba(255,255,255,0.03); padding: 1rem; border-radius: 12px; margin-bottom: 0.75rem; border: 1px solid rgba(255,255,255,0.05);">
                                <div>
                                    <div style="font-weight:700;">{{ u }}</div>
                                    <div style="font-size:0.8rem; color:#888;">{{ perms | join(', ') }}</div>
                                </div>
                                <form action="/api/access/remove" method="POST">
                                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                                    <input type="hidden" name="target_username" value="{{ u }}">
                                    <button type="submit" style="background:none; border:none; color:var(--danger); cursor:pointer; font-weight:700;">Kaldır</button>
                                </form>
                            </div>
                            {% else %}
                            <p style="color: #666; text-align: center;">Henüz kimseye erişim verilmedi.</p>
                            {% endfor %}
                        </div>
                    </div>
                </div>
            {% else %}
                <!-- Ana Panel - Konsol -->

                <div class="card">
                    <div class="card-header">
                        <h2>🖥️ Sunucu Konsolu</h2>
                    </div>
                    <div class="console-container">
                        <div class="console-header" style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
                            <span>🖥️ Canlı Konsol</span>
                            <button type="button" onclick="copyConsole()" class="btn" style="padding:5px 12px;font-size:12px;background:rgba(99,102,241,0.15);color:#c7d2fe;">📋 Kopyala</button>
                        </div>
                        <div class="console-content" id="console-output-area" style="white-space:pre-wrap;overflow-y:auto;max-height:420px;">{{ console_output }}</div>
                    </div>
                    <div style="margin-top: 0.75rem; display: flex; gap: 0.5rem; align-items: center;">
                        <input id="console-input" type="text" class="form-input" placeholder="Konsola komut yazın (örn: op <kullanıcı>)" style="flex: 1;">
                        <button class="btn btn-primary" type="button" onclick="sendConsoleCommand()">📤 Gönder</button>
                    </div>
                    <div class="alert alert-info" style="margin-top: 1rem;">
                        💡 Konsol çıktısı otomatik olarak yenilenir. Sunucuyu başlattığınızda canlı çıktıyı burada görebilirsiniz.
                    </div>
                </div>
            {% endif %}
        </div>
    </div>

    <!-- Bildirim Sistemi -->
    <div class="notification" id="notification">
        📢 İşlem başarıyla tamamlandı!
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
        let pluginSearchTimer = null;
        let perfChart = null;
        let consolePinned = true;  // Kullanıcı yukarı kaydırmadıkça konsol en altta kalsın
        // Sistem istatistiklerini güncelle
        function updateSystemStats() {
            fetch('/api/server_stats')
                .then(response => response.json())
                .then(s => {
                    if (s.online) {
                        const cpuEl = document.getElementById('serverCpuPercent');
                        if (cpuEl) cpuEl.textContent = '%' + Math.round(s.cpu_percent);
                        const ramEl = document.getElementById('serverRamUsage');
                        if (ramEl) ramEl.textContent = s.ram_mb + ' / ' + s.max_ram_mb + ' MB';
                        // Depolama güncelle
                        const diskEl = document.getElementById('serverDiskUsage');
                        if (diskEl) {
                            if (s.disk_free_gb !== undefined) {
                                diskEl.textContent = s.disk_used_gb + ' / ' + s.disk_total_gb + ' GB';
                            } else {
                                diskEl.textContent = 'N/A';
                            }
                        }
                        if(perfChart) {
                            const now = new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'});
                            perfChart.data.labels.push(now);
                            perfChart.data.datasets[0].data.push(Math.round(s.cpu_percent));
                            // RAM yüzdesi olarak göster
                            const ramPct = s.max_ram_mb > 0 ? Math.round((s.ram_mb / s.max_ram_mb) * 100) : 0;
                            perfChart.data.datasets[1].data.push(ramPct);
                            if (perfChart.data.labels.length > 30) {
                                perfChart.data.labels.shift();
                                perfChart.data.datasets[0].data.shift();
                                perfChart.data.datasets[1].data.shift();
                            }
                            perfChart.update('none'); // animasyonsuz anında güncelle
                        }
                    } else {
                        const cpuEl = document.getElementById('serverCpuPercent');
                        if (cpuEl) cpuEl.textContent = 'Kapalı';
                        const ramEl = document.getElementById('serverRamUsage');
                        if (ramEl) ramEl.textContent = '-';
                        const diskEl = document.getElementById('serverDiskUsage');
                        if (diskEl) diskEl.textContent = '-';
                    }
                })
                .catch(error => console.error('Sunucu istatistikleri hatası:', error));
        }

        // Konsol çıktısını güncelle (sadece ana panelde)
        function refreshConsole() {
            if (document.getElementById('console-output-area')) {
                fetch('/api/console')
                    .then(response => response.json())
                    .then(data => {
                        const consoleArea = document.getElementById('console-output-area');
                        if (!consoleArea) return;
                        const nearBottom = (consoleArea.scrollHeight - consoleArea.scrollTop - consoleArea.clientHeight) < 48;
                        const prevTop = consoleArea.scrollTop;
                        const shouldStick = consolePinned || nearBottom;

                        consoleArea.textContent = data.output;

                        if (shouldStick) {
                            consoleArea.scrollTop = consoleArea.scrollHeight;
                            consolePinned = true;
                        } else {
                            consoleArea.scrollTop = prevTop;
                        }

                        // Durum güncelleme
                        const statusBadge = document.getElementById('statusBadge');
                        if (statusBadge) {
                            statusBadge.textContent = data.status;
                            statusBadge.className = `status-pill ${data.status.includes('Çalışıyor') ? 'status-online' : 'status-offline'}`;
                        }
                    })
                    .catch(error => console.error('Konsol yenileme hatası:', error));
            }
        }

        // Konsolu panoya kopyala
        function copyConsole() {
            const el = document.getElementById('console-output-area');
            if (!el) return;
            const text = el.textContent || '';
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(
                    () => showNotification('📋 Konsol kopyalandı', 'success'),
                    () => showNotification('❌ Kopyalanamadı', 'error')
                );
            } else {
                const ta = document.createElement('textarea');
                ta.value = text; document.body.appendChild(ta); ta.select();
                try { document.execCommand('copy'); showNotification('📋 Konsol kopyalandı', 'success'); }
                catch (e) { showNotification('❌ Kopyalanamadı', 'error'); }
                document.body.removeChild(ta);
            }
        }

        // Sunucu adresini kopyala
        function copyServerAddress() {
            const el = document.querySelector('.ip-box.highlight');
            if (!el) return;
            const text = (el.textContent || '').trim();
            if (!text || text === 'Sunucu kapalı') return;
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(
                    () => showNotification('📋 Sunucu adresi kopyalandı: ' + text, 'success'),
                    () => {}
                );
            }
        }

        // Sunucu yazılımına göre Mods/Pugins sekmesini güncelle
        function updateSoftwareSelection(select) {
            const isMod = select.value === 'forge' || select.value === 'fabric';
            const tabName = isMod ? 'mods' : 'plugins';
            // Buton text'ini güncelle
            const submitBtn = document.getElementById('submit-btn');
            if (isMod) {
                submitBtn.textContent = '📦 Modları Yükle';
                submitBtn.className = 'submit-btn submit-btn-mod';
            } else {
                submitBtn.textContent = '🚀 Sunucuyu Oluştur';
                submitBtn.className = 'submit-btn submit-btn-primary';
            }
            // Aktif sekmeyi güncelle
            document.querySelectorAll('.tab-btn').forEach(function(btn) {
                btn.classList.toggle('active', btn.dataset.tab === tabName);
            });
            document.querySelectorAll('.tab-content').forEach(function(content) {
                content.classList.toggle('active', content.id === tabName + '-tab');
            });
            // Eklenti/Mod kartlarını göster/gizle
            document.querySelectorAll('.plugin-card, .mod-card').forEach(function(card) {
                card.classList.toggle('hidden', isMod ? card.classList.contains('plugin-card') : card.classList.contains('mod-card'));
            });
        }

        // Konsola komut gönder
        function sendConsoleCommand() {
            const input = document.getElementById('console-input');
            const cmd = (input.value || '').trim();
            if (!cmd) {
                showNotification('⚠️ Komut boş olamaz', 'error');
                return;
            }
            fetch('/api/console/send', {
                method: 'POST',
                headers: { 
                    'Content-Type': 'application/json',
                    'X-CSRFToken': '{{ csrf_token() }}'
                },
                body: JSON.stringify({ command: cmd })
            })
            .then(async (res) => {
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    const msg = data.error || 'Komut gönderilemedi';
                    showNotification('❌ ' + msg, 'error');
                } else {
                    showNotification('✅ Komut gönderildi: ' + cmd, 'success');
                    input.value = '';
                    refreshConsole();
                }
            })
            .catch((err) => {
                console.error('Komut gönderme hatası:', err);
                showNotification('❌ Komut gönderilirken hata oluştu', 'error');
            });
        }

        // Eklentileri ara ve sonuçları göster
        function searchPlugins() {
            const q = (document.getElementById('plugin-search-input')?.value || '').trim();
            const container = document.getElementById('plugin-results');
            if (!container) return;
            // İlk yüklemede min-height belirle
            if (!container.dataset.initialized) {
                container.style.minHeight = (container.offsetHeight || 420) + 'px';
                container.dataset.initialized = '1';
            }
            // Debounce
            if (pluginSearchTimer) clearTimeout(pluginSearchTimer);
            pluginSearchTimer = setTimeout(() => {
                const prevScroll = window.scrollY;
                container.innerHTML = '<div class="alert alert-info">Aranıyor...</div>';
                fetch('/api/plugins/search?q=' + encodeURIComponent(q))
                    .then(r => r.json())
                    .then(res => {
                        if (res.error) {
                            container.innerHTML = '<div class="alert alert-warning">' + res.error + '</div>';
                            window.scrollTo(0, prevScroll);
                            return;
                        }
                        if (!res.items || res.items.length === 0) {
                            container.innerHTML = '<div class="alert alert-info">Sonuç bulunamadı</div>';
                            window.scrollTo(0, prevScroll);
                            return;
                        }
                        container.innerHTML = res.items.map(item => `
                            <div class="plugin-card">
                                <div class="plugin-card-icon">🧩</div>
                                <div class="plugin-title">${item.name}</div>
                                <div class="plugin-desc">${item.desc || ''}</div>
                                <div class="plugin-meta">${item.category ? '📂 ' + item.category + ' · ' : ''}Kaynak: ${item.source}</div>
                                <div class="plugin-actions">
                                    <button class="btn btn-primary" onclick="installPluginAjax(this, '${item.name}', '${item.url}', '${item.version || 'latest'}')">📥 Kur</button>
                                    <a class="btn" style="background:rgba(255,255,255,0.06); color:#cbd5e1; border:1px solid rgba(255,255,255,0.08)" href="${item.source_url || item.url}" target="_blank" rel="noopener">🔗 Kaynak</a>
                                </div>
                            </div>
                        `).join('');
                        window.scrollTo(0, prevScroll);
                    })
                    .catch(err => {
                        console.error('Plugin arama hatası:', err);
                        container.innerHTML = '<div class="alert alert-error">Arama sırasında hata oluştu</div>';
                        window.scrollTo(0, prevScroll);
                    });
            }, 200);
        }

        // AJAX ile eklenti yükle knk (aynanda kurmak için)
        function installPluginAjax(btn, name, url, version) {
            if (btn.disabled) return;
            btn.disabled = true;
            btn.originalText = btn.innerHTML;
            btn.innerHTML = '⌛ Hazırlanıyor...';
            
            showOverlay('📦', 'Eklenti Kuruluyor...', name + ' sunucunuza indiriliyor, lütfen bekleyin.');
            document.getElementById('overlayProgressWrap').style.display = 'none'; // Tam boyut bilinmiyor genelde

            const formData = new FormData();
            formData.append('name', name);
            formData.append('url', url);
            formData.append('version', version);
            formData.append('csrf_token', '{{ csrf_token() }}');

            fetch('/plugins/install', {
                method: 'POST',
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
                body: formData
            })
            .then(res => res.json())
            .then(data => {
                hideOverlay();
                document.getElementById('overlayProgressWrap').style.display = 'block';
                if (data.success) {
                    btn.innerHTML = '✅ Kuruldu';
                    btn.classList.remove('btn-primary');
                    btn.style.background = '#10b981';
                    showNotification(`🚀 ${name} başarıyla kuruldu! Sunucuyu yeniden başlatınca aktif olur.`, 'success');
                    setTimeout(() => window.location.reload(), 1500);
                } else {
                    btn.disabled = false;
                    btn.innerHTML = btn.originalText;
                    btn.style.opacity = '1';
                    showNotification(`❌ ${data.error || 'Yükleme başarısız'}`, 'error');
                }
            })
            .catch(err => {
                hideOverlay();
                document.getElementById('overlayProgressWrap').style.display = 'block';
                btn.disabled = false;
                btn.innerHTML = btn.originalText;
                btn.style.opacity = '1';
                showNotification('❌ Bağlantı hatası oluştu', 'error');
                console.error('Plugin install error:', err);
            });
        }

        // Dosya yükleme bildirimi
        function submitUploadForm(input) {
            if (input.files.length > 0) {
                const notification = document.getElementById('notification');
                notification.textContent = '📤 Dosya yükleniyor...';
                notification.style.display = 'block';
                notification.style.background = 'linear-gradient(135deg, #f59e0b, #d97706)';
                
                document.getElementById('uploadForm').submit();
                
                setTimeout(() => {
                    notification.style.display = 'none';
                }, 3000);
            }
        }

        // Sayfa yüklendiğinde çalışacak fonksiyonlar
        document.addEventListener('DOMContentLoaded', function() {
            // Konsol açılışta en alta insin; kullanıcı yukarı çıkarsa takip dursun
            const consoleEl = document.getElementById('console-output-area');
            if (consoleEl) {
                consoleEl.scrollTop = consoleEl.scrollHeight;
                consolePinned = true;
                consoleEl.addEventListener('scroll', function() {
                    consolePinned = (consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight) < 48;
                });
            }

            const ctx = document.getElementById('performanceChart')?.getContext('2d');
            if(ctx) {
                perfChart = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: [],
                        datasets: [
                            {
                                label: 'CPU (%)',
                                borderColor: '#10b981',
                                backgroundColor: 'rgba(16, 185, 129, 0.12)',
                                borderWidth: 2,
                                pointRadius: 2,
                                pointHoverRadius: 5,
                                data: [], fill: true, tension: 0.4
                            },
                            {
                                label: 'RAM (%)',
                                borderColor: '#818cf8',
                                backgroundColor: 'rgba(99, 102, 241, 0.12)',
                                borderWidth: 2,
                                pointRadius: 2,
                                pointHoverRadius: 5,
                                data: [], fill: true, tension: 0.4
                            }
                        ]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        animation: false,
                        interaction: { mode: 'index', intersect: false },
                        scales: {
                            y: {
                                beginAtZero: true,
                                max: 100,
                                grid: { color: 'rgba(255,255,255,0.04)' },
                                ticks: { color: '#71717a', callback: v => v + '%' }
                            },
                            x: {
                                grid: { display: false },
                                ticks: { color: '#71717a', maxTicksLimit: 8, maxRotation: 0 }
                            }
                        },
                        plugins: {
                            legend: { labels: { color: '#e4e4e7', usePointStyle: true, pointStyleWidth: 10 } },
                            tooltip: {
                                backgroundColor: 'rgba(15,15,30,0.9)',
                                titleColor: '#e4e4e7',
                                bodyColor: '#a1a1aa',
                                borderColor: 'rgba(255,255,255,0.1)',
                                borderWidth: 1,
                                callbacks: { label: ctx => ctx.dataset.label + ': ' + ctx.parsed.y + '%' }
                            }
                        }
                    }
                });
            }

            // İstatistikleri her 2 saniyede bir güncelle (grafik canlı güncellenir)
            setInterval(updateSystemStats, 2000);
            updateSystemStats();
            

            // Konsolu her 2 saniyede bir güncelle
            setInterval(refreshConsole, 2000);
            
            // Başlangıçta bir kez konsolu güncelle
            refreshConsole();
            
            // Eklenti arama: boşken popülerleri yükle
            searchPlugins();
            
            // Enter tuşu ile komut gönderme
            const ci = document.getElementById('console-input');
            if (ci) {
                ci.addEventListener('keydown', function(e) {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        sendConsoleCommand();
                    }
                });
            }
            const psi = document.getElementById('plugin-search-input');
            if (psi) {
                psi.addEventListener('keydown', function(e) {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        searchPlugins();
                    }
                });
                psi.addEventListener('input', function() {
                    searchPlugins();
                });
                // Kütüphaneyi ilk açılışta yükle
                searchPlugins();
            }
        });

        // Başlatılıyor durumunu göster (Deney Tüpü Görünümü)
        function showStartingStatus() {
            const badge = document.getElementById('statusBadge');
            if (badge) {
                badge.className = 'status-badge status-starting';
                badge.innerHTML = '<i class="fas fa-flask"></i> Başlatılıyor...';
            }
            const consoleContent = document.getElementById('console-content');
            if (consoleContent) {
                const now = new Date().toLocaleTimeString('tr-TR');
                consoleContent.innerHTML += `<div style="color: #fbbf24; border-left: 3px solid #f59e0b; padding-left: 10px; margin: 5px 0;">[${now}] 🧪 Deney tüpü hazırlandı, sunucu sıvısı dolduruluyor...</div>`;
                consoleContent.scrollTop = consoleContent.scrollHeight;
            }
        }

        // Hata durumunda bildirim göster
        function showNotification(message, type = 'info') {
            const notification = document.getElementById('notification');
            notification.textContent = message;
            
            if (type === 'error') {
                notification.style.background = 'linear-gradient(135deg, #ef4444, #dc2626)';
            } else if (type === 'success') {
                notification.style.background = 'linear-gradient(135deg, #10b981, #059669)';
            } else {
                notification.style.background = 'linear-gradient(135deg, #6366f1, #4338ca)';
            }
            
            notification.style.display = 'block';
            setTimeout(() => {
                notification.style.display = 'none';
            }, 5000);
        }
    </script>
</body>
</html>
"""

# --- YENİ: Landing Page (Açılış Sayfası) HTML ---

landing_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Skyvex Hosting - Ücretsiz Minecraft Sunucuları</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;700;900&display=swap" rel="stylesheet">
    <style>
        :root {
            --primary: #ff1b6b;
            --secondary: #45caff;
            --dark: #0a0b10;
            --darker: #050508;
            --light: #ffffff;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Outfit', sans-serif;
        }

        body {
            background: linear-gradient(135deg, var(--darker) 0%, var(--dark) 100%);
            color: var(--light);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }

        /* Navbar */
        nav {
            padding: 2rem 5%;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .logo {
            font-size: 2rem;
            font-weight: 900;
            background: linear-gradient(135deg, var(--primary), var(--secondary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            text-decoration: none;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .nav-links {
            display: flex;
            gap: 2rem;
            align-items: center;
        }

        .nav-links a {
            color: var(--light);
            text-decoration: none;
            font-weight: 700;
            text-transform: uppercase;
            font-size: 0.9rem;
            letter-spacing: 1px;
            transition: 0.3s;
        }

        .nav-links a:hover {
            color: var(--primary);
        }

        /* Hero Section */
        .hero {
            flex: 1;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            text-align: center;
            padding: 2rem;
            position: relative;
            overflow: hidden;
        }

        /* Dekoratif Arka Plan Efektleri */
        .blob {
            position: absolute;
            filter: blur(80px);
            z-index: -1;
            opacity: 0.5;
        }
        .blob-1 {
            width: 400px; height: 400px;
            background: var(--primary);
            bottom: -100px; left: -100px;
            border-radius: 50%;
        }
        .blob-2 {
            width: 300px; height: 300px;
            background: var(--secondary);
            top: -50px; right: -50px;
            border-radius: 50%;
        }

        .subtitle {
            font-size: 1.2rem;
            font-weight: 700;
            letter-spacing: 4px;
            text-transform: uppercase;
            margin-bottom: 1rem;
            color: #aaa;
        }

        .title {
            font-size: 6rem;
            font-weight: 900;
            letter-spacing: 10px;
            margin-bottom: 1rem;
            background: linear-gradient(135deg, var(--light), #aaa);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            display: flex;
            align-items: center;
            gap: 20px;
            text-shadow: 0 10px 30px rgba(0,0,0,0.5);
        }
        
        @media (max-width: 768px) {
            .title { font-size: 3.5rem; letter-spacing: 5px; }
        }

        .desc {
            font-size: 1.2rem;
            max-width: 600px;
            color: #ccc;
            margin-bottom: 3rem;
            line-height: 1.6;
            font-weight: 400;
        }

        .buttons {
            display: flex;
            gap: 1.5rem;
            flex-wrap: wrap;
            justify-content: center;
        }

        .btn {
            padding: 1.2rem 2.5rem;
            border-radius: 50px;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 2px;
            text-decoration: none;
            transition: 0.4s;
            display: inline-flex;
            align-items: center;
            gap: 10px;
            font-size: 1rem;
        }

        .btn-primary {
            background: var(--primary);
            color: white;
            box-shadow: 0 10px 30px rgba(255, 27, 107, 0.4);
        }

        .btn-primary:hover {
            transform: translateY(-5px);
            box-shadow: 0 15px 40px rgba(255, 27, 107, 0.6);
        }

        .btn-secondary {
            background: white;
            color: var(--darker);
        }

        .btn-secondary:hover {
            transform: translateY(-5px);
            background: #eee;
        }

        /* Stats Section */
        .stats {
            display: flex;
            justify-content: center;
            gap: 4rem;
            padding: 3rem;
            background: rgba(255, 255, 255, 0.02);
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            flex-wrap: wrap;
        }

        .stat-item {
            text-align: center;
        }

        .stat-value {
            font-size: 2.5rem;
            font-weight: 900;
            color: var(--secondary);
            margin-bottom: 0.5rem;
        }

        .stat-label {
            font-size: 0.9rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 2px;
            font-weight: 700;
        }

        /* Bağış Butonu */
        .donate-btn-fixed {
            position: fixed;
            bottom: 2rem;
            left: 2rem;
            padding: 1.2rem 2.5rem;
            background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: white !important;
            border-radius: 50px;
            font-weight: 900;
            text-transform: uppercase;
            text-decoration: none !important;
            z-index: 9999;
            box-shadow: 0 10px 40px rgba(37, 99, 235, 0.5);
            display: flex;
            align-items: center;
            gap: 15px;
            transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
            font-size: 1.1rem;
            border: 2px solid rgba(255,255,255,0.1);
        }
        .donate-btn-fixed:hover {
            transform: scale(1.1) translateY(-10px);
            box-shadow: 0 20px 50px rgba(37, 99, 235, 0.8);
            background: linear-gradient(135deg, #2563eb, #1d4ed8);
        }
    </style>
</head>
<body>
    <div class="blob blob-1"></div>
    <div class="blob blob-2"></div>

    <nav>
        <a href="/" class="logo">SKYVEX 🚀</a>
        <div class="nav-links">
            <a href="/">Ana Sayfa</a>
            <a href="/register">Ücretsiz Sunucu</a>
            <a href="/login">Giriş</a>
            <a href="javascript:void(0)" onclick="copyDiscord()" style="color: #7289da;">Discord 🔗</a>
        </div>
    </nav>

    <script>
        function copyDiscord() {
            navigator.clipboard.writeText('https://discord.gg/E2dJksZDU7').then(() => {
                alert('Discord adresi kopyalandı! 🚀');
            }).catch(err => {
                console.error('Kopyalama hatası:', err);
                // Fallback for older browsers
                const textArea = document.createElement("textarea");
                textArea.value = "https://discord.gg/E2dJksZDU7";
                document.body.appendChild(textArea);
                textArea.select();
                try {
                    document.execCommand('copy');
                    alert('Discord adresi kopyalandı! 🚀');
                } catch (e) {
                    window.open('https://discord.gg/E2dJksZDU7', '_blank');
                }
                document.body.removeChild(textArea);
            });
        }
    </script>

    <div class="hero">
        <div class="subtitle">En İyisini Seçin</div>
        <h1 class="title">SKYVEX 🚀</h1>
        <p class="desc">
            Saniyeler içinde tamamen ücretsiz Minecraft sunucunuzu oluşturun. Kendi eklentilerinizle sunucunuzu özelleştirin ve hemen arkadaşlarınızla oynamaya başlayın!
        </p>
        <div class="buttons">
            <a href="/register" class="btn btn-primary">Ücretsiz Minecraft Sunucu Oluşturmak İçin Tıkla</a>
            <a href="{% if logged_in %}/dashboard{% else %}/login{% endif %}" class="btn btn-secondary">{% if logged_in %}Panele Git{% else %}Zaten hesabın var mı? Giriş Yap{% endif %}</a>
        </div>
    </div>

    <div class="stats">
        <div class="stat-item">
            <div class="stat-value">{{ total_servers }}</div>
            <div class="stat-label">Toplam Sunucu</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{{ active_servers }}</div>
            <div class="stat-label">Aktif Sunucu</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{{ empty_servers }}</div>
            <div class="stat-label">Boş Sunucu</div>
        </div>
    </div>

    <!-- Sabit Bağış Butonu -->
    <a href="/login?next=/payment?plan=litecoin" class="donate-btn-fixed">
        💎 Bize Destek Verin
    </a>
</body>
</html>
"""
# --- Sunucular Sayfası HTML ---
servers_html = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sunucularım - Skyvex</title>
    <style>
        :root {
            --primary: #6366f1;
            --secondary: #10b981;
            --dark: #1e1e2f;
            --darker: #151521;
            --light: #f8fafc;
            --card-bg: rgba(255, 255, 255, 0.05);
            --card-border: rgba(255, 255, 255, 0.1);
        }

        body {
            background: var(--darker);
            color: var(--light);
            font-family: 'Inter', system-ui, sans-serif;
            margin: 0;
            padding: 2rem;
        }

        .container { max-width: 1000px; margin: 0 auto; }
        h1 { font-size: 2rem; margin-bottom: 2rem; display: flex; align-items: center; gap: 1rem; }
        
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1.5rem; }
        
        .server-card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 16px;
            padding: 1.5rem;
            transition: all 0.3s ease;
            text-decoration: none;
            color: inherit;
            display: block;
        }

        .server-card:hover {
            transform: translateY(-5px);
            border-color: var(--primary);
            background: rgba(99, 102, 241, 0.1);
        }

        .server-name { font-size: 1.25rem; font-weight: 700; margin-bottom: 0.5rem; }
        .server-owner { font-size: 0.875rem; color: #888; margin-bottom: 1rem; }
        
        .status-badge {
            display: inline-block;
            padding: 0.25rem 0.75rem;
            border-radius: 50px;
            font-size: 0.75rem;
            font-weight: 600;
        }
        .status-online { background: rgba(16, 185, 129, 0.2); color: #10b981; }
        .status-offline { background: rgba(239, 68, 68, 0.2); color: #ef4444; }

        .btn-back {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            color: var(--primary);
            text-decoration: none;
            margin-bottom: 1rem;
            font-weight: 600;
        }
    </style>
</head>
<body>
    <div class="container">
        <a href="/dashboard" class="btn-back">⬅️ Panele Dön</a>
        <h1>🖥️ Sunucularım</h1>
        
        <div class="grid">
            <!-- Kendi Sunucusu -->
            <a href="/dashboard?server={{ username }}" class="server-card">
                <div class="server-name">🚀 Kendi Sunucum</div>
                <div class="server-owner">Sahip: {{ username }}</div>
                <div class="status-badge {{ 'status-online' if my_status else 'status-offline' }}">
                    {{ 'Çalışıyor' if my_status else 'Kapalı' }}
                </div>
            </a>

            <!-- Paylaşılan Sunucular -->
            {% for srv in shared_servers %}
            <a href="/dashboard?server={{ srv.username }}" class="server-card">
                <div class="server-name">🔗 {{ srv.username }}'in Sunucusu</div>
                <div class="server-owner">Sahip: {{ srv.username }}</div>
                <div class="status-badge {{ 'status-online' if srv.status else 'status-offline' }}">
                    {{ 'Çalışıyor' if srv.status else 'Kapalı' }}
                </div>
            </a>
            {% endfor %}
            
            {% if not shared_servers and not is_admin %}
                <!-- Admin değilse ve paylaşılan sunucu yoksa bilgilendirme -->
            {% endif %}
            
            {% if is_admin %}
            <div style="grid-column: 1/-1; margin-top: 2rem;">
                <h3>🛡️ Tüm Sunucular (Admin)</h3>
                <div class="grid">
                    {% for srv in all_servers %}
                    {% if srv.username != username %}
                    <a href="/dashboard?server={{ srv.username }}" class="server-card">
                        <div class="server-name">👤 {{ srv.username }}</div>
                        <div class="status-badge {{ 'status-online' if srv.status else 'status-offline' }}">
                            {{ 'Çalışıyor' if srv.status else 'Kapalı' }}
                        </div>
                    </a>
                    {% endif %}
                    {% endfor %}
                </div>
            </div>
            {% endif %} 
        </div>
    </div>
</body>
</html>
"""


# --- API ve ROUTE'lar ---

@app.route('/api/node_report', methods=['POST'])
def api_node_report():
    """Node'lardan gelen periyodik raporlari alir"""
    try:
        data = request.json
        if not data or data.get('secret') != NODE_SECRET:
            logging.warning(f"Hatali secret ile node_report denemesi: {request.remote_addr}")
            return jsonify({"success": False, "error": "Invalid secret"}), 401
        
        node_id = data.get('node_id')
        if not node_id:
            return jsonify({"success": False, "error": "Missing node_id"}), 400
            
        nodes[node_id] = {
            "last_seen": time.time(),
            "info": data,
            "address": data.get('ip'),
            "public_url": data.get('public_url')
        }
        
        # Node uzerinde calisan sunuculari master'a bildir
        running_users = data.get('running_users', [])
        
        # Bu Node'a atanmış tüm kullanıcıları gez ve durumlarını güncelle
        for uid, udata in users.items():
            if udata.get('node_id') == node_id:
                if uid in running_users:
                    user_processes[uid] = "REMOTE"
                elif user_processes.get(uid) == "REMOTE":
                    # Node bu sunucunun artık çalışmadığını söylüyor
                    user_processes[uid] = None
                    logging.info(f"Sunucu node tarafından kapandı bildirildi: {uid}")

        changed = False
        for user in running_users:
            if user in users:
                if users[user].get('node_id') != node_id:
                    users[user]['node_id'] = node_id
                    changed = True
                
                # Tünel adresini güncelle
                if 'tunnels' in data and user in data['tunnels']:
                    user_bore_address[user] = data['tunnels'][user]
        
        if changed:
            save_users()
            
        return jsonify({"success": True})
    except Exception as e:
        logging.error(f"Node report error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/sync/<username>', methods=['POST'])
def api_sync_console(username):
    """Node'dan gelen konsol ciktilarini senkronize eder"""
    try:
        data = request.json
        if not data or data.get('secret') != NODE_SECRET:
            return "Unauthorized", 401
            
        lines = data.get('lines', [])
        if username not in user_console_histories:
            user_console_histories[username] = []
            
        if username not in active_players:
            active_players[username] = set()

        for line in lines:
            user_console_histories[username].append(line)
            # Uzak sunucu için etkinlik + oyuncu takibi
            msg = line.lower()
            join_match = re.search(r"(?:^|\]:\s)([A-Za-z0-9_.]{3,32})\s+joined the game", line)
            if join_match:
                active_players[username].add(join_match.group(1))
                server_activity[username] = time.time()
            leave_match = re.search(r"(?:^|\]:\s)([A-Za-z0-9_.]{3,32})\s+(?:left the game|lost connection|logged out)", line)
            if leave_match:
                active_players[username].discard(leave_match.group(1))
                server_activity[username] = time.time()
            if "logged in with entity id" in msg or "issued server command" in msg:
                server_activity[username] = time.time()
                
        # Limit history
        if len(user_console_histories[username]) > 500:
            user_console_histories[username] = user_console_histories[username][-500:]
                
        return jsonify({"success": True})
    except Exception as e:
        logging.error(f"Sync error for {username}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/login", methods=["GET", "POST"])
def login():
    """Giriş sayfası"""
    cleanup_expired_users()
    client_ip = request.remote_addr
    
    # Brute Force Kontrolü
    if client_ip in failed_logins:
        attempts = failed_logins[client_ip]
        if attempts['count'] >= LOCKOUT_COUNT:
            if time.time() - attempts['last_attempt'] < LOCKOUT_DURATION:
                return render_template_string(login_html, 
                                        error='locked',
                                        total_accounts=len(users),
                                        active_servers=len([p for p in user_processes.values() if p]))
            else:
                # Süre dolmuşsa sıfırla
                failed_logins[client_ip] = {'count': 0, 'last_attempt': 0}

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        
        if username in users and verify_password(users[username]["password"], password):
            if check_user_resources(username):
                # Başarılı giriş: Login sayacını sıfırla
                if client_ip in failed_logins:
                    del failed_logins[client_ip]
                
                session['username'] = username
                session.permanent = True
                if 'selected_server' not in session:
                    session['selected_server'] = username
                    
                logging.info(f"Kullanıcı girişi başarılı: {username}")
                
                next_page = request.args.get('next')
                if next_page:
                    return redirect(next_page)

                if users[username].get('role') == 'admin':
                    return redirect('/admin')
                return redirect('/dashboard')
            else:
                return redirect("/login?error=expired")
        else:
            # Başarısız giriş: Sayacı artır
            if client_ip not in failed_logins:
                failed_logins[client_ip] = {'count': 0, 'last_attempt': 0}
            failed_logins[client_ip]['count'] += 1
            failed_logins[client_ip]['last_attempt'] = time.time()
            
            logging.warning(f"Başarısız giriş denemesi: {username} (IP: {client_ip}, Deneme: {failed_logins[client_ip]['count']})")
            return redirect("/login?error=invalid")
    
    error = request.args.get('error', '')
    total_accounts = len(users)
    active_servers = len([p for p in user_processes.values() if p])
    
    return render_template_string(login_html, 
                            error=error, 
                            total_accounts=total_accounts, 
                            active_servers=active_servers)

@app.route("/logout")
def logout():
    """Çıkış yap"""
    username = session.get('username')
    session.clear()
    logging.info(f"Çıkış yapıldı: {username}")
    return redirect("/login")

@app.route("/api/players")
@limiter.exempt
@login_required
def api_players():
    """Canlı oyuncu listesini döndürür (uzak node konsolundan ayrıştırılır)."""
    try:
        session_user = session.get('username')
        target_server = get_selected_server(session_user)
        stream_console(target_server)
        return jsonify({"players": sorted(active_players.get(target_server, set()))})
    except Exception as e:
        logging.error(f"Oyuncu API hatası: {e}")
        return jsonify({"players": [], "error": str(e)}), 500


@app.route("/api/console")
@limiter.exempt
@login_required
def api_console():
    """Konsol çıktısını JSON olarak döndürür."""
    try:
        session_user = session.get('username')
        target_server = get_selected_server(session_user)
        console_output = stream_console(target_server)
        status = "Çalışıyor ✅" if user_processes.get(target_server) else "Kapalı ❌"
        return jsonify({
            "output": console_output,
            "status": status
        })
    except Exception as e:
        logging.error(f"Konsol API hatası ({session_user}): {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/console/send", methods=["POST"])
@limiter.limit("30 per minute")
@login_required
def api_console_send():
    """Konsola komut gönderir."""
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    
    if not has_permission(session_user, target_server, "console"):
        return jsonify({"error": "Bu işlem için yetkiniz yok."}), 403
    try:
        # Node kontrolü
        user_data = users.get(target_server, {})
        node_id = user_data.get('node_id')
        if node_id and node_id in nodes:
            try:
                node_url = nodes[node_id]['info'].get('public_url') or f"http://{nodes[node_id]['info'].get('ip')}:7860"
                data = request.get_json(silent=True) or {}
                cmd_text = (data.get("command") or "").strip()
                res = requests.post(f"{node_url}/exec", json={"action": "send_command", "username": target_server, "command": cmd_text, "secret": NODE_SECRET}, timeout=15)
                if res.status_code == 200 and res.json().get('success'):
                    return jsonify({"success": True})
                else:
                    return jsonify({"error": "Node komutu kabul etmedi"}), 500
            except Exception as e:
                return jsonify({"error": f"Node bağlantı hatası: {e}"}), 500

        proc = user_processes.get(target_server)
        if not proc:
            return jsonify({"error": "Sunucu kapalı. Önce sunucuyu başlatın."}), 400
        data = request.get_json(silent=True) or {}
        command = (data.get("command") or "").strip()
        if not command:
            return jsonify({"error": "Komut boş olamaz."}), 400
        try:
            proc.stdin.write((command + "\n").encode('utf-8', 'ignore'))
            proc.stdin.flush()
            user_console_queues[target_server].put(f"[{time.strftime('%H:%M:%S')}] ➤ Komut gönderildi: {command}\n")
            return jsonify({"success": True})
        except Exception as e:
            logging.error(f"Konsol komutu gönderme hatası ({session_user}): {e}")
            return jsonify({"error": f"Komut gönderilemedi: {e}"}), 500
    except Exception as e:
        logging.error(f"Konsol komutu API hatası ({session_user}): {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/server/version", methods=["POST"])
@login_required
def change_server_version():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    if not has_permission(session_user, target_server, "settings"):
        return redirect("/dashboard?error=no_permission")
    version = (request.form.get("version") or "").strip()
    if not version:
        return redirect("/settings")
    if is_proc_active(user_processes.get(target_server)):
        logging.warning(f"Sürüm değişikliği reddedildi (sunucu açık): {target_server}")
        return redirect("/settings")
    user_dir = get_user_dir(target_server)
    users.setdefault(target_server, {}).setdefault("server_config", {})["version"] = version
    save_users()
    threading.Thread(target=download_server_jar_thread, args=("paper", version, user_dir, target_server), daemon=True).start()
    return redirect("/settings")


@app.route("/server/world", methods=["POST"])
@login_required
def change_server_world():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    if not has_permission(session_user, target_server, "settings"):
        return redirect("/dashboard?error=no_permission")
    if is_proc_active(user_processes.get(target_server)):
        logging.warning(f"Dünya değişikliği reddedildi (sunucu açık): {target_server}")
        return redirect("/settings")
    level_name = secure_filename((request.form.get("level_name") or "world").strip()) or "world"
    level_seed = (request.form.get("level_seed") or "").strip()
    props = read_server_properties(target_server)
    if level_name != props.get("level-name", "world"):
        props["level-name"] = level_name
        if level_seed:
            props["level-seed"] = level_seed
        else:
            props.pop("level-seed", None)
        write_server_properties(target_server, props)
        logging.info(f"Dünya değiştirildi ({target_server}): {level_name}")
    return redirect("/settings")


@app.route("/server_properties", methods=["POST"])
@login_required
def update_server_properties():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    
    if not has_permission(session_user, target_server, "settings"):
        return redirect("/dashboard?error=no_permission")
    try:
        props = read_server_properties(target_server)
        for key in [
            "pvp","gamemode","difficulty","white-list","max-players","allow-flight","view-distance","motd",
            "enable-command-block","allow-nether","force-gamemode","hardcore","online-mode",
            "simulation-distance","spawn-protection","max-world-size","server-port",
            "resource-pack","resource-pack-prompt"
        ]:
            if key in request.form:
                props[key] = request.form.get(key)
        write_server_properties(target_server, props)
        return redirect("/dashboard")
    except Exception as e:
        logging.error(f"Sunucu özellikleri güncelleme hatası ({session_user}): {e}")
        return redirect("/dashboard")

@app.route("/plugins", methods=["GET"])
@login_required
def plugins_page():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    
    if not has_permission(session_user, target_server, "files"):
        return redirect("/dashboard?error=no_permission")
    
    try:
        ip = get_client_ip()
        status = "Çalışıyor ✅" if user_processes.get(target_server) else "Kapalı ❌"
        user_data = users.get(target_server, {})
        user_dir = get_user_dir(target_server)
        plugins_dir = os.path.join(user_dir, "plugins")
        os.makedirs(plugins_dir, exist_ok=True)
        items = [n for n in os.listdir(plugins_dir) if n.endswith(".jar")]
        bore_address, bore_active = get_bore_info(target_server)
        return render_template_string(html,
            ip=ip, status=status, page="plugins",
            server_jar_name=SERVER_JAR_NAME, java_path=java_path,
            eula_agreed=check_eula(target_server), process=user_processes.get(target_server), jar_exists=jar_exists(target_server),
            server_settings=server_settings, username=session_user, user_data=user_data,
            plugins=items, server_props=read_server_properties(target_server),
            target_server=target_server,
            bore_address=bore_address,
            bore_active=bore_active
        )
    except Exception as e:
        logging.error(f"Eklentiler sayfası hatası ({session_user}): {e}")
        return redirect("/")

@app.route("/plugins/install", methods=["POST"])
@login_required
def plugins_install():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    
    if not has_permission(session_user, target_server, "files"):
        return jsonify({"success": False, "error": "Yetkisiz erişim"}), 403
    
    try:
        url = request.form.get("url", "").strip()
        name = request.form.get("name", "").strip() or "plugin"

        # ── MODRINTH: gerçek jar adresini sunucu tarafında çöz ──
        if url.startswith("modrinth:"):
            slug = url.split(":", 1)[1].strip()
            mc_version = (users.get(target_server, {}).get("server_config", {}) or {}).get("version")
            resolved = resolve_modrinth_url(slug, mc_version=mc_version)
            if not resolved:
                return jsonify({
                    "success": False,
                    "error": "Modrinth'ten indirme adresi alınamadı. Lütfen birkaç saniye sonra tekrar deneyin."
                }), 502
            url = resolved
        
        # ── GÜVENLİ KAYNAK KONTROLÜ ──
        try:
            from urllib.parse import urlparse as _urlparse
            _parsed_url = _urlparse(url)
            _domain = _parsed_url.netloc.lower().lstrip("www.")
            _safe = any(
                _domain == d.lstrip("www.") or _domain.endswith("." + d.lstrip("www."))
                for d in SAFE_PLUGIN_DOMAINS
            )
            if not _safe:
                logging.warning(f"Güvensiz plugin kaynağı engellendi ({session_user}): {url} (domain: {_domain})")
                return jsonify({
                    "success": False,
                    "error": f"❌ Güvensiz kaynak! Yalnızca SpigotMC, CurseForge, Modrinth ve GitHub'dan plugin indirebilirsiniz. Bu URL'den indirme yapılamaz: {_domain}"
                }), 403
        except Exception as _url_check_err:
            logging.warning(f"URL kontrol hatası: {_url_check_err}")
            # Katalogdan gelen URL'ler her zaman geçerli kabul edilir
        
        # Zaten direkt Spiget API URL'si ise olduğu gibi kullan
        # SpigotMC sayfa URL'sini Spiget API'ye çevir
        if "spigotmc.org/resources/" in url and "/download" not in url:
            try:
                # URL formatı: .../resources/essentialsx.9089/ -> ID: 9089
                import re as _re
                match = _re.search(r'resources/[^.]+\.([0-9]+)', url)
                if match:
                    resource_id = match.group(1)
                    url = f"https://api.spiget.org/v2/resources/{resource_id}/download"
                    logging.info(f"Spigot URL Spiget API'ye çevrildi: {url}")
                else:
                    # Alternatif format deneme: sadece sayı
                    parts = url.rstrip('/').split('.')
                    resource_id = parts[-1].split('/')[0]
                    if resource_id.isdigit():
                        url = f"https://api.spiget.org/v2/resources/{resource_id}/download"
                        logging.info(f"Spigot URL (fallback) çevrildi: {url}")
            except Exception as e:
                logging.warning(f"URL çevrim hatası: {e}")
        
        user_dir = get_user_dir(target_server)
        plugins_dir = os.path.join(user_dir, "plugins")
        os.makedirs(plugins_dir, exist_ok=True)
        
        safe_name = re.sub(r'[^a-zA-Z0-9_\- ]', '', name).strip() or "plugin"
        filename = f"{safe_name}.jar"
        save_path = os.path.join(plugins_dir, filename)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/octet-stream, application/java-archive, */*',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        
        logging.info(f"Eklenti indiriliyor: {url} -> {save_path}")
        
        with requests.get(url, headers=headers, stream=True, timeout=60, allow_redirects=True) as r:
            r.raise_for_status()
            content_type = r.headers.get('Content-Type', '').lower()
            # HTML dönüyorsa hata ver
            if 'text/html' in content_type and 'application' not in content_type:
                return jsonify({"success": False, "error": f"Kaynak bağlantısı hatalı — HTML döndü. URL: {url}"}), 400
                 
            with open(save_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
        
        # Dosya boyutu kontrolü
        file_size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
        if file_size < 1024:  # 1KB'dan küçükse büyük ihtimalle hatalı
            os.remove(save_path)
            return jsonify({"success": False, "error": f"İndirilen dosya çok küçük ({file_size} byte). Kaynak geçersiz olabilir."}), 400
        
        logging.info(f"✅ Eklenti başarıyla indirildi ({session_user} -> {target_server}): {filename} ({file_size//1024} KB)")
        return jsonify({"success": True, "filename": filename})
            
    except requests.HTTPError as e:
        logging.error(f"Eklenti HTTP hatası ({session_user}): {e}")
        return jsonify({"success": False, "error": f"HTTP hatası {e.response.status_code}: Dosya bulunamadı veya erişim reddedildi."}), 500
    except Exception as e:
        logging.error(f"Eklenti yükleme hatası ({session_user}): {e}")
        return jsonify({"success": False, "error": f"İndirme hatası: {str(e)}"}), 500

@app.route("/plugins/delete", methods=["POST"])
@login_required
def delete_plugin():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    if not has_permission(session_user, target_server, "files"):
        return redirect("/plugins?error=no_permission")
    try:
        plugin_name = request.form.get("plugin_name")
        if not plugin_name or not plugin_name.endswith(".jar"):
            return redirect("/plugins?error=invalid_plugin")
        
        user_dir = get_user_dir(target_server)
        plugin_path = os.path.join(user_dir, "plugins", secure_filename(plugin_name))
        if os.path.exists(plugin_path):
            os.remove(plugin_path)
            logging.info(f"Eklenti silindi ({session_user} on {target_server}): {plugin_name}")
        return redirect("/plugins?success=deleted")
    except Exception as e:
        logging.error(f"Eklenti silme hatası ({session_user}): {e}")
        return redirect("/plugins")

@app.route("/api/plugins/search")

@limiter.exempt
@login_required
def api_plugins_search():
    try:
        q = request.args.get("q", "").strip().lower()
        items = []
        # Varsayılan: tüm kütüphaneyi göster (popülerler önce)
        if not q:
            items = list(PLUGIN_CATALOG)
        else:
            for p in PLUGIN_CATALOG:
                if q in p["name"].lower() or q in p.get("desc", "").lower() or q in p.get("category", "").lower():
                    items.append(p)

        items.sort(key=lambda p: (not p.get("popular"), p.get("category", ""), p["name"]))
        items = [plugin_public_item(p) for p in items]
        return jsonify({"items": items})
    except Exception as e:
        logging.error(f"Plugin arama hatası: {e}")
        return jsonify({"error": "Arama sırasında hata oluştu"}), 500

@app.route("/properties")
@login_required
def properties_page():
    try:
        ip = get_client_ip()
        session_user = session.get('username')
        target_server = get_selected_server(session_user)
        
        if not has_permission(session_user, target_server, "settings"):
            return redirect("/dashboard?error=no_permission")
        status = "Çalışıyor ✅" if user_processes.get(target_server) else "Kapalı ❌"
        user_data = users.get(target_server, {})
        bore_address, bore_active = get_bore_info(target_server)
        return render_template_string(html,
            ip=ip, status=status, page="properties",
            server_jar_name=SERVER_JAR_NAME, java_path=java_path,
            eula_agreed=check_eula(target_server), process=user_processes.get(target_server), jar_exists=jar_exists(target_server),
            server_settings=server_settings, username=session_user, user_data=user_data,
            server_props=read_server_properties(target_server),
            target_server=target_server,
            bore_address=bore_address,
            bore_active=bore_active
        )

    except Exception as e:
        logging.error(f"Özellikler sayfası hatası: {e}")
        return f"<h2>Özellikler sayfası yüklenirken hata oluştu: {e}</h2>", 500

@app.route("/api/system_stats")
@limiter.exempt
@login_required
def api_system_stats():
    """Detaylı sistem istatistiklerini döndürür."""
    try:
        stats = get_detailed_system_stats()
        return jsonify(stats)
    except Exception as e:
        logging.error(f"Sistem istatistikleri API hatası: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/server_stats")
@limiter.exempt
@login_required
def api_server_stats():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    try:
        user_data = users.get(target_server, {})
        node_id = user_data.get('node_id')
        
        if node_id and node_id in nodes:
            try:
                node_url = nodes[node_id]['info'].get('public_url') or f"http://{nodes[node_id]['info'].get('ip')}:7860"
                res = requests.post(f"{node_url}/exec", json={"action": "get_stats", "username": target_server, "secret": NODE_SECRET}, timeout=3)
                if res.status_code == 200:
                    data = res.json()
                    data['max_ram_mb'] = user_data.get('resources', {}).get('ram_mb', 3072)
                    return jsonify(data)
            except:
                pass

        proc = user_processes.get(target_server)
        if not proc:
            return jsonify({"online": False})
        
        p = psutil.Process(proc.pid)
        cpu = p.cpu_percent(interval=0.1)
        mem = p.memory_info().rss
        threads = p.num_threads()
        
        resources = user_data.get('resources', {})
        max_mb = resources.get('ram_mb', 1024)
        
        # Disk kullanımı (kullanıcı klasörünün bulunduğu disk)
        try:
            user_dir = get_user_dir(target_server)
            disk = shutil.disk_usage(user_dir)
            disk_total_gb = round(disk.total / (1024**3), 1)
            disk_used_gb  = round(disk.used  / (1024**3), 1)
            disk_free_gb  = round(disk.free  / (1024**3), 1)
        except Exception:
            disk_total_gb = disk_used_gb = disk_free_gb = None
        
        result = {
            "online": True,
            "cpu_percent": cpu,
            "ram_mb": int(mem / (1024 * 1024)),
            "max_ram_mb": max_mb,
            "threads": threads,
        }
        if disk_total_gb is not None:
            result["disk_total_gb"] = disk_total_gb
            result["disk_used_gb"]  = disk_used_gb
            result["disk_free_gb"]  = disk_free_gb
        return jsonify(result)
    except Exception as e:
        logging.error(f"Sunucu istatistikleri API hatası ({session_user}): {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/servers")
@login_required
def servers_page():
    session_user = session.get('username')
    user_data = users.get(session_user, {})
    is_admin = user_data.get('role') == 'admin'
    
    # Paylaşılan sunucuları bul
    shared_servers = []
    for u, data in users.items():
        if session_user in data.get('shared_access', []):
            shared_servers.append({
                "username": u,
                "status": True if user_processes.get(u) else False
            })
            
    # Tüm sunucular (Admin için)
    all_servers = []
    if is_admin:
        for u, data in users.items():
            if data.get('role') == 'user':
                all_servers.append({
                    "username": u,
                    "status": True if user_processes.get(u) else False
                })

    return render_template_string(servers_html,
        username=session_user,
        is_admin=is_admin,
        my_status=True if user_processes.get(session_user) else False,
        shared_servers=shared_servers,
        all_servers=all_servers
    )

@app.route("/players")
@login_required
def players_page():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    if not has_permission(session_user, target_server, "console"):
        return redirect("/dashboard?error=no_permission")
        
    status = "Çalışıyor ✅" if user_processes.get(target_server) else "Kapalı ❌"
    user_data = users.get(target_server, {})
    
    # Uzak node'da oyuncuları görebilmek için konsolu bir kez çek (giriş/çıkışları ayrıştırır)
    try:
        stream_console(target_server)
    except Exception as _pc_err:
        logging.warning(f"Oyuncu listesi güncellenemedi ({target_server}): {_pc_err}")

    # Canlı oyuncu bilgilerini global listeden al
    known_players = list(active_players.get(target_server, set()))
    bore_address, bore_active = get_bore_info(target_server)
    
    return render_template_string(html,
        ip=get_client_ip(), status=status, page="players",
        server_jar_name=SERVER_JAR_NAME, java_path=java_path,
        eula_agreed=check_eula(target_server), process=user_processes.get(target_server), jar_exists=jar_exists(target_server),
        username=session_user, user_data=user_data,
        target_server=target_server,
        known_players=list(known_players),
        bore_address=bore_address,
        bore_active=bore_active
    )

@app.route("/access")
@login_required
def access_page():
    session_user = session.get('username')
    target_server = session_user 
    
    user_data = users.get(target_server, {})
    shared_users = user_data.get('shared_access', {})
    
    if isinstance(shared_users, list):
        shared_users = {u: ["start", "stop", "console"] for u in shared_users}
        users[target_server]['shared_access'] = shared_users
        save_users()

    return render_template_string(html,
        username=session_user,
        shared_users=shared_users,
        page="access",
        user_data=users.get(session_user, {}),
        process=user_processes.get(target_server),
        server_jar_name=SERVER_JAR_NAME,
        java_path=java_path
    )

@app.route("/api/access/add", methods=["POST"])
@login_required
def api_access_add():
    session_user = session.get('username')
    target_username = request.form.get('target_username', '').strip()
    perms = request.form.getlist('perms')
    
    if not target_username or target_username == session_user:
        return redirect("/access?error=invalid_user")
    
    if target_username not in users:
        return redirect("/access?error=not_found")
        
    user_data = users.get(session_user, {})
    if 'shared_access' not in user_data or isinstance(user_data['shared_access'], list):
        user_data['shared_access'] = {}
        
    user_data['shared_access'][target_username] = perms
    save_users()
    return redirect("/access?success=added")

@app.route("/api/access/remove", methods=["POST"])
@login_required
def api_access_remove():
    session_user = session.get('username')
    target_username = request.form.get('target_username', '').strip()
    
    user_data = users.get(session_user, {})
    if 'shared_access' in user_data and target_username in user_data['shared_access']:
        del user_data['shared_access'][target_username]
        save_users()
        
    return redirect("/access?success=removed")

# ─────────────────────────────────────────────────────────────────────────────
# YENİ ANA SAYFA - İki seçenekli hosting landing page
# ─────────────────────────────────────────────────────────────────────────────
SKYVEX_LANDING_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SkyVex Hosting - Ücretsiz Minecraft & Discord Bot Barındırma</title>
<meta name="description" content="Ücretsiz Minecraft sunucusu aç veya Discord botunu çalıştır. Tamamen ücretsiz, kayıt olmadan dene!">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
    --bg: #060810;
    --bg2: #0d0f17;
    --border: rgba(255,255,255,0.07);
    --green: #00d084;
    --purple: #7c3aed;
    --purple2: #a855f7;
    --blue: #3b82f6;
    --text: #e2e8f0;
    --muted: #64748b;
}
body {
    font-family: 'Inter', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    overflow-x: hidden;
}
/* Animated background */
.bg-orbs {
    position: fixed; inset: 0; z-index: 0; pointer-events: none; overflow: hidden;
}
.orb {
    position: absolute;
    border-radius: 50%;
    filter: blur(120px);
    opacity: 0.12;
    animation: float 8s ease-in-out infinite;
}
.orb-1 { width: 600px; height: 600px; background: #7c3aed; top: -200px; left: -100px; animation-delay: 0s; }
.orb-2 { width: 500px; height: 500px; background: #06b6d4; bottom: -200px; right: -100px; animation-delay: 3s; }
.orb-3 { width: 400px; height: 400px; background: #00d084; top: 40%; left: 40%; animation-delay: 1.5s; }
@keyframes float { 0%,100% { transform: translateY(0) scale(1); } 50% { transform: translateY(-40px) scale(1.05); } }
/* Nav */
nav {
    position: relative; z-index: 10;
    display: flex; align-items: center; justify-content: space-between;
    padding: 20px 60px;
    border-bottom: 1px solid var(--border);
    backdrop-filter: blur(20px);
    background: rgba(6,8,16,0.8);
}
.nav-logo { display: flex; align-items: center; gap: 12px; font-size: 20px; font-weight: 800; }
.nav-logo .icon {
    width: 40px; height: 40px;
    background: linear-gradient(135deg, var(--purple), var(--purple2));
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px;
    box-shadow: 0 4px 20px rgba(124,58,237,0.4);
}
.nav-links { display: flex; gap: 8px; align-items: center; }
.nav-btn {
    padding: 9px 20px;
    border-radius: 10px;
    font-size: 14px;
    font-weight: 600;
    text-decoration: none;
    transition: all 0.2s;
    border: none; cursor: pointer; font-family: 'Inter', sans-serif;
}
.nav-btn-ghost { background: transparent; color: var(--muted); }
.nav-btn-ghost:hover { color: var(--text); }
.nav-btn-primary {
    background: linear-gradient(135deg, var(--purple), var(--purple2));
    color: #fff;
    box-shadow: 0 4px 20px rgba(124,58,237,0.3);
}
.nav-btn-primary:hover { transform: translateY(-1px); box-shadow: 0 8px 30px rgba(124,58,237,0.4); }
/* Hero */
.hero {
    position: relative; z-index: 5;
    text-align: center;
    padding: 100px 60px 60px;
}
.hero-badge {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 8px 20px;
    background: rgba(124,58,237,0.12);
    border: 1px solid rgba(124,58,237,0.3);
    border-radius: 50px;
    font-size: 13px; font-weight: 600; color: var(--purple2);
    margin-bottom: 32px;
    animation: slideDown 0.6s ease;
}
@keyframes slideDown { from { opacity:0; transform: translateY(-20px); } to { opacity:1; transform: translateY(0); } }
.hero h1 {
    font-size: clamp(40px, 6vw, 72px);
    font-weight: 900;
    line-height: 1.1;
    margin-bottom: 20px;
    animation: slideUp 0.6s ease 0.1s both;
}
@keyframes slideUp { from { opacity:0; transform: translateY(30px); } to { opacity:1; transform: translateY(0); } }
.hero h1 .grad {
    background: linear-gradient(135deg, var(--purple2), #06b6d4);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}
.hero p {
    font-size: 18px; color: var(--muted); max-width: 560px; margin: 0 auto 48px;
    line-height: 1.7;
    animation: slideUp 0.6s ease 0.2s both;
}
/* Stats bar */
.stats-bar {
    display: flex; justify-content: center; gap: 48px;
    padding: 24px 0;
    border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
    margin-bottom: 80px;
    animation: slideUp 0.6s ease 0.3s both;
    position: relative; z-index: 5;
}
.stat-item { text-align: center; }
.stat-num { font-size: 28px; font-weight: 800; color: var(--text); }
.stat-num .green { color: var(--green); }
.stat-num .purple { color: var(--purple2); }
.stat-label { font-size: 12px; color: var(--muted); margin-top: 4px; }
/* Cards */
.cards-section {
    position: relative; z-index: 5;
    padding: 0 60px 100px;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 28px;
    max-width: 1100px;
    margin: 0 auto;
}
.hosting-card {
    border-radius: 28px;
    padding: 48px;
    text-decoration: none;
    display: block;
    transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
    position: relative;
    overflow: hidden;
    cursor: pointer;
}
.hosting-card::before {
    content: '';
    position: absolute; inset: 0;
    opacity: 0;
    transition: opacity 0.4s;
}
.hosting-card:hover { transform: translateY(-8px) scale(1.01); }
.hosting-card:hover::before { opacity: 1; }
.card-mc {
    background: linear-gradient(135deg, rgba(0,208,132,0.08), rgba(6,182,212,0.05));
    border: 1px solid rgba(0,208,132,0.2);
}
.card-mc::before { background: linear-gradient(135deg, rgba(0,208,132,0.1), transparent); }
.card-mc:hover { border-color: rgba(0,208,132,0.5); box-shadow: 0 30px 80px rgba(0,208,132,0.15); }
.card-bot {
    background: linear-gradient(135deg, rgba(124,58,237,0.08), rgba(168,85,247,0.05));
    border: 1px solid rgba(124,58,237,0.2);
}
.card-bot::before { background: linear-gradient(135deg, rgba(124,58,237,0.1), transparent); }
.card-bot:hover { border-color: rgba(124,58,237,0.5); box-shadow: 0 30px 80px rgba(124,58,237,0.15); }
.card-icon {
    width: 80px; height: 80px;
    border-radius: 22px;
    display: flex; align-items: center; justify-content: center;
    font-size: 40px;
    margin-bottom: 28px;
    transition: transform 0.3s;
}
.hosting-card:hover .card-icon { transform: scale(1.1) rotate(-5deg); }
.card-mc .card-icon { background: rgba(0,208,132,0.15); border: 1px solid rgba(0,208,132,0.3); }
.card-bot .card-icon { background: rgba(124,58,237,0.15); border: 1px solid rgba(124,58,237,0.3); }
.card-title { font-size: 28px; font-weight: 800; margin-bottom: 12px; }
.card-mc .card-title { color: #fff; }
.card-bot .card-title { color: #fff; }
.card-desc { font-size: 15px; color: var(--muted); line-height: 1.6; margin-bottom: 28px; }
.card-features { list-style: none; margin-bottom: 32px; }
.card-features li {
    font-size: 13px; color: var(--text); padding: 6px 0;
    display: flex; align-items: center; gap: 8px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
}
.card-features li:last-child { border-bottom: none; }
.check-mc { color: var(--green); }
.check-bot { color: var(--purple2); }
.card-cta {
    display: inline-flex; align-items: center; gap: 10px;
    padding: 14px 28px;
    border-radius: 14px;
    font-size: 15px; font-weight: 700;
    transition: all 0.3s;
    border: none; cursor: pointer; font-family: 'Inter', sans-serif;
    text-decoration: none;
    width: 100%; justify-content: center;
}
.cta-mc {
    background: linear-gradient(135deg, #00d084, #06b6d4);
    color: #000;
    box-shadow: 0 8px 30px rgba(0,208,132,0.25);
}
.cta-mc:hover { transform: translateY(-2px); box-shadow: 0 16px 40px rgba(0,208,132,0.35); }
.cta-bot {
    background: linear-gradient(135deg, var(--purple), var(--purple2));
    color: #fff;
    box-shadow: 0 8px 30px rgba(124,58,237,0.25);
}
.cta-bot:hover { transform: translateY(-2px); box-shadow: 0 16px 40px rgba(124,58,237,0.35); }
.free-badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 12px; border-radius: 20px;
    font-size: 11px; font-weight: 700;
    margin-bottom: 16px; letter-spacing: 0.5px;
}
.free-mc { background: rgba(0,208,132,0.15); color: var(--green); border: 1px solid rgba(0,208,132,0.3); }
.free-bot { background: rgba(124,58,237,0.15); color: var(--purple2); border: 1px solid rgba(124,58,237,0.3); }
/* Footer */
footer {
    position: relative; z-index: 5;
    text-align: center;
    padding: 40px;
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 13px;
}
@media (max-width: 768px) {
    nav { padding: 16px 20px; }
    .hero { padding: 60px 20px 40px; }
    .cards-section { grid-template-columns: 1fr; padding: 0 20px 60px; }
    .stats-bar { gap: 24px; flex-wrap: wrap; }
    .hosting-card { padding: 32px 24px; }
}
</style>
</head>
<body>
<div class="bg-orbs">
    <div class="orb orb-1"></div>
    <div class="orb orb-2"></div>
    <div class="orb orb-3"></div>
</div>

<nav>
    <div class="nav-logo">
        <div class="icon" style="display:flex;align-items:center;justify-content:center;">
            <svg width="26" height="26" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M21.6 12L2.4 20l4.1-8-4.1-8 19.2 8z" fill="#818cf8"/><path d="M6.5 12h9.2" stroke="#05050f" stroke-width="1.1" opacity=".4"/></svg>
        </div>
        <span>SkyVex</span>
    </div>
    <div class="nav-links">
        {% if logged_in %}
        <a href="/dashboard" class="nav-btn nav-btn-ghost">🎮 MC Panelim</a>
        {% endif %}
        <a href="/bot/panel" class="nav-btn nav-btn-ghost">🤖 Bot Paneli</a>
        <a href="/login" class="nav-btn nav-btn-primary">Giriş Yap</a>
    </div>
</nav>

<div class="hero">
    <div class="hero-badge">✨ Tamamen Ücretsiz — Kredi Kartı Gerekmez</div>
    <h1>Sunucunu <span class="grad">Saniyeler</span> İçinde Başlat</h1>
    <p>Minecraft sunucusu aç veya Discord botunu çalıştır. Hiçbir teknik bilgi gerekmez.</p>
</div>

<div class="stats-bar">
    <div class="stat-item">
        <div class="stat-num green">{{ active_servers }}</div>
        <div class="stat-label">Aktif Sunucu</div>
    </div>
    <div class="stat-item">
        <div class="stat-num">{{ total_servers }}</div>
        <div class="stat-label">Toplam Sunucu</div>
    </div>
    <div class="stat-item">
        <div class="stat-num purple">∞</div>
        <div class="stat-label">Ücretsiz Plan</div>
    </div>
    <div class="stat-item">
        <div class="stat-num">7/24</div>
        <div class="stat-label">Destek</div>
    </div>
</div>

<div class="cards-section">
    <!-- Minecraft Kartı -->
    <a href="/login" class="hosting-card card-mc">
        <span class="free-badge free-mc">✅ ÜCRETSİZ</span>
        <div class="card-icon">✈️</div>
        <div class="card-title">Minecraft Sunucusu</div>
        <div class="card-desc">Kendi Minecraft sunucunu saniyeler içinde oluştur ve arkadaşlarınla oyna.</div>
        <ul class="card-features">
            <li><span class="check-mc">✓</span> 3GB RAM — Ücretsiz</li>
            <li><span class="check-mc">✓</span> Spigot, Paper, Vanilla desteği</li>
            <li><span class="check-mc">✓</span> 200+ güvenli plugin kataloğu</li>
            <li><span class="check-mc">✓</span> Gerçek zamanlı konsol</li>
            <li><span class="check-mc">✓</span> Otomatik dünya yedekleme</li>
        </ul>
        <div class="card-cta cta-mc">✈️ Sunucu Aç →</div>
    </a>
    
    <!-- Discord Bot Kartı -->
    <a href="/bot/login" class="hosting-card card-bot">
        <span class="free-badge free-bot">✅ ÜCRETSİZ</span>
        <div class="card-icon">🤖</div>
        <div class="card-title">Discord Botu</div>
        <div class="card-desc">Kendi Discord botunu oluştur, özelleştir ve 7/24 çalışır hale getir.</div>
        <ul class="card-features">
            <li><span class="check-bot">✓</span> 10 hazır özellik (ticket, anti-raid...)</li>
            <li><span class="check-bot">✓</span> Tek tık kurulum — kod bilgisi gereksiz</li>
            <li><span class="check-bot">✓</span> Seviye sistemi, rol yönetimi</li>
            <li><span class="check-bot">✓</span> Gerçek zamanlı log izleme</li>
            <li><span class="check-bot">✓</span> Başlat / Durdur / Öldür kontrolü</li>
        </ul>
        <div class="card-cta cta-bot">🤖 Bot Oluştur →</div>
    </a>
</div>

<footer>
    <p>© 2024 SkyVex Hosting — Ücretsiz Oyun Sunucuları</p>
</footer>
</body>
</html>
"""

@app.route("/")
def landing_page():
    try:
        logged_in = 'username' in session and session['username'] in users
        total_servers = sum(1 for u in users.values() if u.get('role') == 'user' and u.get('has_selected_plan'))
        active_servers = sum(1 for p in user_processes.values() if is_proc_active(p))
        return render_template_string(SKYVEX_LANDING_HTML,
            total_servers=total_servers,
            active_servers=active_servers,
            logged_in=logged_in
        )
    except Exception as e:
        logging.error(f"Landing page hatası: {e}")
        return f"<h2>Sayfa yüklenirken hata: {e}</h2>", 500

@app.route("/dashboard")
@login_required
def index():
    try:
        session_user = session.get('username')
        target_server = get_selected_server(session_user)
        
        user_data = users.get(target_server, {}) # Define user_data here
        ip = get_client_ip()
        node_id = user_data.get('node_id')
        if node_id and node_id in nodes:
            # Node'un IP adresini kullan
            node_ip = nodes[node_id]['info'].get('ip')
            if node_ip:
                ip = node_ip

        ip_port = f"{ip}:{get_server_port(target_server)}"
        status = "Çalışıyor ✅" if user_processes.get(target_server) else "Kapalı ❌"
        eula_agreed = check_eula(target_server)
        jar_present = jar_exists(target_server)
        console_output = stream_console(target_server)
        
        # Tünel Bilgileri
        bore_address, bore_active = get_bore_info(target_server)
        
        # Eger tunel aktifse, oncelikli olarak onu goster
        if bore_active and bore_address:
            ip_port = bore_address
        
        return render_template_string(html, 
            ip=ip_port, 
            status=status, 
            page="home",
            server_jar_name=SERVER_JAR_NAME, 
            java_path=java_path,
            jar_exists=jar_present, 
            eula_agreed=eula_agreed,
            console_output=console_output, 
            process=user_processes.get(target_server),
            server_settings=server_settings,
            username=session_user,
            user_data=user_data,
            server_props=read_server_properties(target_server),
            target_server=target_server,
            bore_address=bore_address,
            bore_active=bore_active
        )

    except Exception as e:
        logging.error(f"Ana sayfa hatası: {e}")
        return f"<h2>Sayfa yüklenirken hata oluştu: {e}</h2>", 500

@app.route("/start", methods=["POST"])
@login_required
def start_server():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    
    if not has_permission(session_user, target_server, "start"):
        return redirect("/dashboard?error=no_permission")
    
    if not jar_exists(target_server) or not check_eula(target_server):
        logging.warning(f"Sunucu başlatma engellendi ({target_server}): JAR veya EULA eksik")
        return redirect("/dashboard")

    # --- Konsola sadece tek ve net mesaj ---
    start_time_str = time.strftime('%H:%M:%S')
    user_console_histories[target_server] = [f"[{start_time_str}] Sunucu başlatılıyor, lütfen bekleyin...\n"]
    # Yeni oturum: oyuncu listesini sıfırla (eski/çevrimdışı isimler görünmesin)
    active_players[target_server] = set()
    
    # --- Etkinlik Takibini Başlat ---
    server_activity[target_server] = time.time()

    if not is_proc_active(user_processes.get(target_server)):
        try:
            user_data = users.get(target_server, {})
            is_admin = users.get(session_user, {}).get('role') == 'admin'
            
            # --- Akıllı Node Seçimi: node_type'a Göre ---
            active_node_id = None
            now = time.time()
            res_data = user_data.get('resources') or {}
            ram_per_user = res_data.get('ram_mb', 3072)
            
            # Kullanıcının sunucu tipini belirle (smp, velocity, hub, boxpvp...)
            server_config = user_data.get('server_config', {})
            server_type = server_config.get('server_type', 'genel')  # Varsayılan: genel
            
            # Aktif node'ları topla (son 90 saniyede heartbeat göndermiş)
            active_nodes = {nid: nodes[nid] for nid in nodes if now - nodes[nid].get('last_seen', 0) < 90}

            # Panel yeni uyandıysa / node yeni başladıysa 90 saniyeye kadar bekle
            _waited = 0
            while not active_nodes and _waited < 90:
                time.sleep(5)
                _waited += 5
                now = time.time()
                active_nodes = {nid: nodes[nid] for nid in nodes
                                if now - nodes[nid].get('last_seen', 0) < 90}

            if active_nodes:
                node_usage = {nid: 0 for nid in active_nodes}
                for u, udata in users.items():
                    uid_node = udata.get('node_id')
                    if uid_node in node_usage and is_proc_active(user_processes.get(u)):
                        node_usage[uid_node] += 1
                
                # ÖNCE: server_type ile eşleşen node_type'lı node'lar arasından seç
                type_matched_nodes = {
                    nid: ndata for nid, ndata in active_nodes.items()
                    if ndata['info'].get('node_type', 'genel') == server_type
                }
                
                # Eşleşen node yoksa tüm aktif node'ları kullan
                candidate_nodes = type_matched_nodes if type_matched_nodes else active_nodes
                
                best_node = None
                best_free = -1
                for nid, ndata in candidate_nodes.items():
                    node_ram_mb = ndata['info'].get('ram_total_mb', 3072)
                    max_slots   = max(1, node_ram_mb // ram_per_user)
                    used_slots  = node_usage.get(nid, 0)
                    free_slots  = max_slots - used_slots
                    if free_slots > best_free:
                        best_free  = free_slots
                        best_node  = nid
                
                if best_node: active_node_id = best_node
            
            # RAM ayarlarını belirle
            ram_mb = ram_per_user
            current_min_ram = f"{ram_mb}M"
            current_max_ram = f"{ram_mb}M"
            
            if active_node_id:
                try:
                    node_info = active_nodes[active_node_id]['info']
                    node_url = node_info.get('public_url') or f"http://{node_info.get('ip') or '127.0.0.1'}:7860"
                    
                    logging.info(f"Sunucu node üzerinde başlatılıyor: {target_server} -> {active_node_id} ({node_url})")
                    
                    node_cmd = ["java", f"-Xmx{current_max_ram}", f"-Xms{current_min_ram}",
                            *server_settings.get('java_args', '').split(), "-jar", SERVER_JAR_NAME, "nogui"]
                    
                    # Gerçek Master URL'sini belirle
                    master_url = get_public_url()
                    PANEL_URLS = list(dict.fromkeys([
                        master_url,
                        f"http://87.106.82.84:{PANEL_PORT}",
                        f"http://skyvex.duckdns.org:{PANEL_PORT}",
                    ]))
                    
                    payload = {
                        "action": "start",
                        "username": target_server,
                        "cmd": node_cmd,
                        "port": get_server_port(target_server),
                        "server_type": server_type,  # Node'un başlatma biçimini bilmesi için
                        "sync_url": f"{PANEL_URLS[0]}/api/sync/{target_server}?secret={NODE_SECRET}",
                        "sync_fallback_urls": [f"{u}/api/sync/{target_server}?secret={NODE_SECRET}" for u in PANEL_URLS[1:]],
                        "secret": NODE_SECRET
                    }
                    
                    # Node'a gönder — dosya boyutu büyük olabileceği için timeout'u artırdık (300s)
                    res = None
                    last_err = None
                    
                    for attempt in range(2):
                        try:
                            # (Connect timeout, Read timeout) -> Büyük dosyalar için Read timeout 1200 saniye (20 dk) yaptık knk
                            res = requests.post(f"{node_url}/exec", json=payload, timeout=(30, 1200))
                            if res.status_code == 200: break
                        except Exception as e:
                            last_err = str(e)
                            logging.warning(f"Node bağlantı hatası: {e}")
                            time.sleep(1)
                    
                    if res is not None and res.status_code == 200:
                        try:
                            data = res.json()
                            if data.get('success'):
                                users[target_server]['node_id'] = data.get('node_id') or active_node_id
                                save_users()
                                user_processes[target_server] = "REMOTE"
                                if data.get('address'):
                                    user_bore_address[target_server] = data['address']
                            else:
                                logging.error(f"Uzak Node hatası ({target_server}): {data.get('error')}")
                                user_processes[target_server] = None
                        except Exception as parse_err:
                            logging.error(f"Uzak Node geçersiz yanıt ({target_server}): {parse_err}")
                            user_processes[target_server] = None
                    else:
                        err_detail = last_err if res is None else f"HTTP {res.status_code}"
                        logging.error(f"Uzak Node başlatma başarısız ({target_server}): {err_detail}")
                        user_processes[target_server] = None
                except Exception as node_err:
                    logging.error(f"Node bağlantı hatası ({target_server}): {node_err}")
                    user_processes[target_server] = None
            else:
                logging.warning(f"Uygun uzak Node bulunamadı ({target_server})")
                user_processes[target_server] = None
                
        except Exception as e:
            logging.error(f"Sunucu başlatma hatası ({target_server}): {e}")
            user_console_histories[target_server].append(f"[{time.strftime('%H:%M:%S')}] ❌ KRİTİK HATA: {e}\n")
            
    return redirect("/dashboard")

@app.route("/stop", methods=["POST"])
@login_required
def stop_server():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    
    if not has_permission(session_user, target_server, "stop"):
        return redirect("/dashboard?error=no_permission")
        
    # Node kontrolü
    user_data = users.get(target_server, {})
    node_id = user_data.get('node_id')
    if node_id and node_id in nodes:
        try:
            node_url = nodes[node_id]['info'].get('public_url') or f"http://{nodes[node_id]['info'].get('ip')}:7860"
            res = requests.post(f"{node_url}/exec", json={"action": "stop", "username": target_server, "secret": NODE_SECRET}, timeout=15)
            if res.status_code == 200 and res.json().get('success'):
                user_processes[target_server] = None
                return redirect("/dashboard")
        except Exception as e:
            logging.error(f"Node durdurma hatası ({target_server}): {e}")

    proc = user_processes.get(target_server)
    if proc and proc != "REMOTE":
        user_console_queues[target_server].put(f"[{time.strftime('%H:%M:%S')}] 🛑 Sunucuya 'stop' komutu gönderiliyor...\n")
        try:
            proc.stdin.write(b"stop\n")
            proc.stdin.flush()
        except:
            pass
        
        try:
            proc.wait(timeout=10)
        except:
            proc.kill()
        
        user_processes[target_server] = None
        stop_bore_tunnel(target_server)
    
    # Remote/zombie state temizliği
    user_processes[target_server] = None
    return redirect("/dashboard")

# Dosya Yönetimi Route'ları
# Güvenli Plugin Kaynakları (Whitelist)
SAFE_PLUGIN_DOMAINS = [
    "api.spiget.org",
    "spiget.org",
    "spigotmc.org",
    "www.spigotmc.org",
    "dev.bukkit.org",
    "media.forgecdn.net",
    "cdn.modrinth.com",
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "hangar.papermc.io",
    "papermc.io",
    "geysermc.org",
    "download.geysermc.org",
    "ci.md-5.net",
]


@app.route("/files/", defaults={'subpath': ''})
@app.route("/files/<path:subpath>")
@login_required
def files(subpath):
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    
    if not has_permission(session_user, target_server, "files"):
        return redirect("/dashboard?error=no_permission")
    
    # Dosya Yöneticisi Erişimi (Premium kilidi kaldırıldı)
    
    try:
        if '..' in subpath:
            logging.warning(f"Güvensiz dosya yolu denemesi ({session_user}): {subpath}")
            return redirect("/files/")

        user_dir = os.path.normpath(get_user_dir(target_server))
        target_dir = os.path.normpath(os.path.join(user_dir, subpath))
        
        # Güvenlik Kontrolü: Kullanıcı kendi/yönettiği klasörden dışarı çıkamaz
        if not (target_dir == user_dir or target_dir.startswith(user_dir + os.sep)):
            logging.warning(f"Dizin dışı deneme ({session_user} on {target_server}): {target_dir}")
            if subpath == "":
                return "<h2>Hata: Ana dizin erişim izni yok veya yol hatalı.</h2>", 403
            return redirect("/files/")

        if not os.path.isdir(target_dir):
            if subpath == "":
                return "<h2>Hata: Sunucu dosyaları bulunamadı. Lütfen kurulumu tamamlayın.</h2>", 404
            return redirect("/files/")
        
        status = "Çalışıyor ✅" if user_processes.get(target_server) else "Kapalı ❌"
        eula_agreed = check_eula(target_server)
        files_list = []
        
        for item_name in os.listdir(target_dir):
            if item_name.startswith('.'): continue
            item_path = os.path.join(target_dir, item_name)
            is_dir = os.path.isdir(item_path)
            item_size = 0 if is_dir else os.path.getsize(item_path) // 1024
            full_path = os.path.join(subpath, item_name) if subpath else item_name
            files_list.append({
                "name": item_name,
                "size": item_size,
                "is_dir": is_dir,
                "full_path": urllib.parse.quote(full_path.replace("\\", "/"))
            })
        
        files_list.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        parent_path = os.path.dirname(subpath) if subpath else ""
        if parent_path == '.': parent_path = ""
            
        user_data = users.get(target_server, {})
        bore_address, bore_active = get_bore_info(target_server)
            
        return render_template_string(html, 
            ip=get_client_ip(), status=status, page="files",
            server_jar_name=SERVER_JAR_NAME, java_path=java_path,
            eula_agreed=eula_agreed, process=user_processes.get(target_server), jar_exists=jar_exists(target_server),
            files=files_list, UPLOAD_FOLDER=user_dir,
            current_path=target_dir.replace("\\", "/"),
            current_subpath=subpath, parent_path=parent_path,
            breadcrumbs=create_breadcrumbs(subpath),
            server_settings=server_settings,
            username=session_user,
            user_data=user_data,
            target_server=target_server,
            bore_address=bore_address,
            bore_active=bore_active
        )

    except Exception as e:
        logging.error(f"Dosya sayfası hatası ({session_user}): {e}")
        return f"<h2>Dosya sayfası yüklenirken hata oluştu: {e}</h2>", 500

@app.route("/upload", methods=["POST"])
@login_required
def upload_file():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    try:
        upload_path = request.args.get('path', '')
        if 'file' not in request.files or request.files['file'].filename == '':
            return redirect(f"/files/{upload_path}")
        
        f = request.files['file']
        filename = secure_filename(f.filename)
        user_dir = os.path.abspath(get_user_dir(target_server))
        target_dir = os.path.abspath(os.path.join(user_dir, upload_path.lstrip('/\\')))
        
        if not (target_dir == user_dir or target_dir.startswith(user_dir + os.sep)):
            logging.error(f"Güvenlik uyarısı: {session_user} geçersiz yola dosya yüklemeye çalıştı: {target_dir}")
            return "❌ Geçersiz yol. (Güvenlik Kısıtlaması)", 403
            
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, filename)
        f.save(path)
        
        if filename.endswith('.zip'):
            try:
                secure_unzip(path, target_dir)
                os.remove(path)
            except Exception as e:
                logging.error(f"ZIP açma hatası ({session_user} on {target_server}): {e}")
        
        logging.info(f"Dosya yüklendi ({session_user} on {target_server}): {filename}")
        return redirect(f"/files/{upload_path}")
    except Exception as e:
        logging.error(f"Dosya yükleme hatası ({session_user}): {e}")
        return redirect(f"/files/?error={str(e)}")


@app.route("/api/upload", methods=["POST"])
@login_required
@csrf.exempt
def api_upload_file():
    """AJAX yükleme endpoint'i — JSON yanıt döndürür."""
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    try:
        upload_path = request.args.get('path', '')
        if 'file' not in request.files or request.files['file'].filename == '':
            return jsonify({"success": False, "error": "Dosya seçilmedi"}), 400

        f = request.files['file']
        filename = secure_filename(f.filename)
        user_dir = os.path.abspath(get_user_dir(target_server))
        target_dir = os.path.abspath(os.path.join(user_dir, upload_path.lstrip('/\\')))

        if not (target_dir == user_dir or target_dir.startswith(user_dir + os.sep)):
            logging.error(f"API Güvenlik uyarısı: {session_user} geçersiz yola dosya yüklemeye çalıştı: {target_dir}")
            return jsonify({"success": False, "error": f"Geçersiz yol: {upload_path}"}), 403

        os.makedirs(target_dir, exist_ok=True)
        save_path = os.path.join(target_dir, filename)
        f.save(save_path)

        is_archive = filename.lower().endswith(('.zip', '.rar', '.7z'))
        extracted = False

        if is_archive:
            try:
                secure_unzip(save_path, target_dir)
                os.remove(save_path)
                extracted = True
                logging.info(f"✅ Arşiv açıldı ({session_user} on {target_server}): {filename}")
            except Exception as ex:
                logging.error(f"Arşiv açma hatası ({session_user} on {target_server}): {ex}")
                return jsonify({"success": False, "error": str(ex), "filename": filename}), 500

        logging.info(f"Dosya yüklendi ({session_user} on {target_server}): {filename}")
        return jsonify({"success": True, "filename": filename, "extracted": extracted})
    except Exception as e:
        logging.error(f"API dosya yükleme hatası ({session_user}): {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/unarchive", methods=["POST"])
@login_required
@csrf.exempt
def api_unarchive_file():
    """AJAX arşiv çıkarma endpoint'i — JSON yanıt döndürür."""
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    if not has_permission(session_user, target_server, "files"):
        return jsonify({"success": False, "error": "Yetki yok"}), 403
    try:
        current_path = request.form.get("current_path", "").strip()
        filename = request.form.get("filename", "").strip()

        if not filename.lower().endswith(('.zip', '.rar', '.7z')):
            return jsonify({"success": False, "error": "Desteklenmeyen arşiv formatı (ZIP, RAR, 7Z olmalı)"}), 400

        user_dir = get_user_dir(target_server)
        zip_path = os.path.normpath(os.path.join(user_dir, current_path, filename))
        target_dir = os.path.normpath(os.path.join(user_dir, current_path))

        if not zip_path.startswith(user_dir):
            return jsonify({"success": False, "error": "Geçersiz dosya yolu"}), 403

        if not os.path.isfile(zip_path):
            return jsonify({"success": False, "error": "Dosya bulunamadı"}), 404

        secure_unzip(zip_path, target_dir)
        logging.info(f"✅ Arşiv başarıyla açıldı ({session_user} on {target_server}): {filename}")
        return jsonify({"success": True, "filename": filename})
    except Exception as e:
        logging.error(f"API arşiv çıkarma hatası ({session_user}): {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/archive", methods=["POST"])
@login_required
def archive_files():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    if not has_permission(session_user, target_server, "files"):
        return redirect("/files?error=no_permission")
    try:
        current_path = request.form.get("current_path", "").strip()
        selected_files = request.form.getlist("selected_files[]")
        if not selected_files:
            return redirect(f"/files/{current_path}?error=no_selection")
        user_dir = get_user_dir(target_server)
        base_dir = os.path.normpath(os.path.join(user_dir, current_path))
        if not base_dir.startswith(user_dir):
            return redirect(f"/files?error=invalid_path")
        archive_name = f"archive_{int(time.time())}.zip"
        archive_path = os.path.join(base_dir, archive_name)
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_rel in selected_files:
                file_rel = urllib.parse.unquote(file_rel)
                file_full = os.path.normpath(os.path.join(base_dir, file_rel))
                if not file_full.startswith(user_dir): continue
                if os.path.exists(file_full):
                    if os.path.isdir(file_full):
                        for root, dirs, files_in_dir in os.walk(file_full):
                            for f in files_in_dir:
                                f_full = os.path.join(root, f)
                                arc_name = os.path.relpath(f_full, base_dir)
                                zipf.write(f_full, arc_name)
                    else:
                        zipf.write(file_full, file_rel)
        logging.info(f"✅ Arşiv oluşturuldu ({session_user} -> {target_server}): {archive_name}")
        return redirect(f"/files/{current_path}?success=archived")
    except Exception as e:
        logging.error(f"Arşivleme hatası ({session_user}): {e}")
        return redirect(f"/files")

@app.route("/api/archive", methods=["POST"])
@login_required
@csrf.exempt
def api_archive_files():
    """AJAX arşiv oluşturma — JSON yanıt döndürür, loading overlay için."""
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    if not has_permission(session_user, target_server, "files"):
        return jsonify({"success": False, "error": "Yetkisiz erişim"}), 403
    try:
        current_path = request.form.get("current_path", "").strip()
        selected_files = request.form.getlist("selected_files[]")
        archive_name_input = request.form.get("archive_name", "").strip()
        if not selected_files:
            return jsonify({"success": False, "error": "Hiçbir dosya seçilmedi"}), 400
        user_dir = get_user_dir(target_server)
        base_dir = os.path.normpath(os.path.join(user_dir, current_path))
        if not base_dir.startswith(user_dir):
            return jsonify({"success": False, "error": "Geçersiz yol"}), 403
        archive_name = secure_filename(archive_name_input) if archive_name_input else f"archive_{int(time.time())}.zip"
        if not archive_name.endswith('.zip'):
            archive_name += '.zip'
        archive_path = os.path.join(base_dir, archive_name)
        file_count = 0
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_rel in selected_files:
                file_rel = urllib.parse.unquote(file_rel)
                file_full = os.path.normpath(os.path.join(base_dir, file_rel))
                if not file_full.startswith(user_dir): continue
                if os.path.exists(file_full):
                    if os.path.isdir(file_full):
                        for root, dirs, files_in_dir in os.walk(file_full):
                            for f in files_in_dir:
                                f_full = os.path.join(root, f)
                                arc_name = os.path.relpath(f_full, base_dir)
                                zipf.write(f_full, arc_name)
                                file_count += 1
                    else:
                        zipf.write(file_full, file_rel)
                        file_count += 1
        archive_size_kb = os.path.getsize(archive_path) // 1024
        logging.info(f"✅ AJAX arşiv oluşturuldu ({session_user} -> {target_server}): {archive_name} ({file_count} dosya, {archive_size_kb} KB)")
        return jsonify({"success": True, "filename": archive_name, "file_count": file_count, "size_kb": archive_size_kb})
    except Exception as e:
        logging.error(f"AJAX arşivleme hatası ({session_user}): {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/unarchive", methods=["POST"])
@login_required
def unarchive_file():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    if not has_permission(session_user, target_server, "files"):
        return redirect("/files?error=no_permission")   
    try:
        current_path = request.form.get("current_path", "").strip()
        filename = request.form.get("filename", "").strip()
        if not (filename.lower().endswith(".zip") or filename.lower().endswith(".rar") or filename.lower().endswith(".7z")):
             return redirect(f"/files/{current_path}?error=not_supported")
        user_dir = get_user_dir(target_server)
        zip_path = os.path.normpath(os.path.join(user_dir, current_path, filename))
        target_dir = os.path.normpath(os.path.join(user_dir, current_path))
        if not zip_path.startswith(user_dir):
            return redirect(f"/files?error=invalid_path")
        secure_unzip(zip_path, target_dir)
        return redirect(f"/files/{current_path}?success=unarchived")
    except Exception as e:
        logging.error(f"Zipten çıkarma hatası ({session_user}): {e}")
        current_path = request.form.get("current_path", "").strip()
        return redirect(f"/files/{current_path}?error={urllib.parse.quote(str(e))}")

@app.route("/download/<path:filepath>")
@login_required
def download_file(filepath):
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    try:
        user_dir = get_user_dir(target_server)
        target_path = os.path.normpath(os.path.join(user_dir, filepath))
        
        if not target_path.startswith(user_dir) or not os.path.isfile(target_path):
            logging.warning(f"Güvensiz indirme denemesi ({session_user} on {target_server}): {filepath}")
            return "❌ Dosya bulunamadı veya yetkisiz erişim.", 403
            
        return send_from_directory(os.path.dirname(target_path), os.path.basename(target_path), as_attachment=True)
    except Exception as e:
        logging.error(f"Dosya indirme hatası ({session_user}): {e}")
        return "❌ Dosya indirilemedi.", 500

@app.route("/delete_file", methods=["POST"])
@login_required
def delete_file():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    try:
        item_path = request.form.get("item_path")
        is_dir = request.form.get("is_dir") == 'True'
        redirect_path = request.form.get("redirect_path", "")
        
        if '..' in item_path:
            logging.warning(f"Güvensiz silme denemesi ({session_user}): {item_path}")
            return redirect(f"/files/{redirect_path}")
        
        user_dir = get_user_dir(target_server)
        full_path = os.path.normpath(os.path.join(user_dir, item_path))
        
        if not full_path.startswith(user_dir):
            logging.warning(f"Dizin dışı silme denemesi ({session_user} on {target_server}): {full_path}")
            return redirect(f"/files/{redirect_path}")

        if os.path.exists(full_path):
            try:
                if is_dir:
                    shutil.rmtree(full_path)
                    logging.info(f"Klasör silindi ({session_user} on {target_server}): {full_path}")
                else:
                    os.remove(full_path)
                    logging.info(f"Dosya silindi ({session_user} on {target_server}): {full_path}")
            except Exception as e:
                logging.error(f"Silme işlemi hatası ({session_user}): {e}")
        return redirect(f"/files/{redirect_path}")
    except Exception as e:
        logging.error(f"Silme route hatası ({session_user}): {e}")
        return redirect(f"/files/{redirect_path}")

@app.route("/edit/<path:filepath>", methods=["GET", "POST"])
@login_required
def edit_file(filepath):
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    
    if not has_permission(session_user, target_server, "files"):
        return redirect("/files?error=no_permission")
        
    try:
        user_dir = os.path.abspath(get_user_dir(target_server))
        target_path = os.path.abspath(os.path.join(user_dir, filepath))
        
        # Güvenlik Kontrolü
        if not target_path.startswith(user_dir + os.sep) and target_path != user_dir:
            return "❌ Geçersiz yol.", 403
            
        if request.method == "POST":
            content = request.form.get("content", "")
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content)
            logging.info(f"Dosya kaydedildi ({session_user} on {target_server}): {filepath}")
            # Kaydettikten sonra dosyalar listesine dön
            return redirect(f"/files/{os.path.dirname(filepath)}")
            
        # GET: Dosyayı oku ve editör sayfasını göster
        if not os.path.isfile(target_path):
            return "❌ Dosya bulunamadı.", 404
            
        with open(target_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            
        status = "Çalışıyor ✅" if user_processes.get(target_server) else "Kapalı ❌"
        bore_address, bore_active = get_bore_info(target_server)
        user_data = users.get(target_server, {})
        is_admin = users.get(session_user, {}).get('role') == 'admin'
        
        return render_template_string(html,
            ip=get_client_ip(), status=status, page="edit",
            edit_content=content, edit_item_path=filepath,
            edit_dir_path=os.path.dirname(filepath),
            username=session_user, target_server=target_server,
            server_jar_name=SERVER_JAR_NAME, java_path=java_path,
            eula_agreed=check_eula(target_server), process=user_processes.get(target_server),
            jar_exists=jar_exists(target_server), server_settings=server_settings,
            bore_address=bore_address, bore_active=bore_active,
            user_data=user_data, is_admin=is_admin,
            server_props=read_server_properties(target_server),
            console_output=""
        )
    except Exception as e:
        logging.error(f"Dosya düzenleme hatası ({session_user}): {e}")
        return f"<h2>Hata: {e}</h2>", 500

@app.route("/create_folder", methods=["POST"])
@login_required
def create_folder():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    try:
        current_path = request.form.get("current_path", "").strip()
        folder_name = secure_filename(request.form.get("folder_name", "").strip())
        
        if not folder_name:
            return redirect(f"/files/{current_path}?error=invalid_name")
            
        user_dir = get_user_dir(target_server)
        new_dir = os.path.normpath(os.path.join(user_dir, current_path, folder_name))
        
        if not new_dir.startswith(user_dir):
            return redirect(f"/files/{current_path}?error=invalid_path")
            
        os.makedirs(new_dir, exist_ok=True)
        return redirect(f"/files/{current_path}?success=folder_created")
    except Exception as e:
        logging.error(f"Folder creation error ({session_user}): {e}")
        return redirect(f"/files/{current_path}?error=failed")

@app.route("/create_file", methods=["POST"])
@login_required
def create_file():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    try:
        current_path = request.form.get("current_path", "").strip()
        filename = secure_filename(request.form.get("filename", "").strip())
        
        if not filename:
            return redirect(f"/files/{current_path}?error=invalid_name")
            
        allowed_exts = {'.txt', '.yml', '.yaml', '.json', '.properties', '.toml'}
        ext = os.path.splitext(filename)[1].lower()
        if ext not in allowed_exts:
            return redirect(f"/files/{current_path}?error=invalid_extension")
            
        user_dir = get_user_dir(target_server)
        new_file = os.path.normpath(os.path.join(user_dir, current_path, filename))
        
        if not new_file.startswith(user_dir):
            return redirect(f"/files/{current_path}?error=invalid_path")
            
        with open(new_file, 'w', encoding='utf-8') as f:
            f.write("")
        return redirect(f"/files/{current_path}?success=file_created")
    except Exception as e:
        logging.error(f"File creation error ({session_user}): {e}")
        return redirect(f"/files/{current_path}?error=failed")

@app.route("/rename_item", methods=["POST"])
@login_required
def rename_item():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    try:
        current_path = request.form.get("current_path", "").strip()
        old_name = request.form.get("old_name", "").strip()
        new_name = secure_filename(request.form.get("new_name", "").strip())
        
        if not old_name or not new_name:
            return redirect(f"/files/{current_path}?error=invalid_names")
            
        user_dir = get_user_dir(target_server)
        old_path = os.path.normpath(os.path.join(user_dir, current_path, old_name))
        new_path = os.path.normpath(os.path.join(user_dir, current_path, new_name))
        
        if not old_path.startswith(user_dir) or not new_path.startswith(user_dir):
            return redirect(f"/files/{current_path}?error=invalid_path")
            
        if os.path.exists(old_path):
            os.rename(old_path, new_path)
            
        return redirect(f"/files/{current_path}?success=renamed")
    except Exception as e:
        logging.error(f"Rename error ({session_user}): {e}")
        return redirect(f"/files/{current_path}?error=failed")

@app.route("/bulk_delete", methods=["POST"])
@login_required
def bulk_delete():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    try:
        current_path = request.form.get("current_path", "").strip()
        selected_files = request.form.getlist("selected_files[]")
        
        user_dir = get_user_dir(target_server)
        for filename in selected_files:
            item_path = os.path.normpath(os.path.join(user_dir, current_path, filename))
            if item_path.startswith(user_dir) and os.path.exists(item_path):
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                else:
                    os.remove(item_path)
                    
        return redirect(f"/files/{current_path}?success=bulk_deleted")
    except Exception as e:
        logging.error(f"Bulk delete error ({session_user}): {e}")
        return redirect(f"/files/{current_path}?error=failed")

@app.route("/rename_jar", methods=["POST"])
@login_required
def rename_jar():
    session_user = session.get('username')
    target_server = get_selected_server(session_user)
    try:
        filename = request.form.get("filename")
        user_dir = get_user_dir(target_server)
        old_path = os.path.normpath(os.path.join(user_dir, secure_filename(filename)))
        
        if not old_path.startswith(user_dir) or not os.path.exists(old_path):
            return redirect("/files")
            
        new_path = get_user_file_path(target_server, SERVER_JAR_NAME)
        
        if os.path.exists(new_path):
            os.remove(new_path)
            logging.info(f"Eski server.jar silindi ({target_server})")
            
        os.rename(old_path, new_path)
        logging.info(f"Yeni server.jar oluşturuldu ({session_user} on {target_server}): {filename} -> {SERVER_JAR_NAME}")
        
        return redirect("/files")
    except Exception as e:
        logging.error(f"JAR yeniden adlandırma hatası ({session_user}): {e}")
        return redirect("/files")

@app.route("/settings")
@login_required
def settings():
    try:
        session_user = session.get('username')
        target_server = get_selected_server(session_user)
        
        if not has_permission(session_user, target_server, "settings"):
            return redirect("/dashboard?error=no_permission")
        
        ip = get_client_ip()
        status = "Çalışıyor ✅" if user_processes.get(target_server) else "Kapalı ❌"
        eula_agreed = check_eula(target_server)
        
        user_data = users.get(target_server, {})
        is_admin = users[session_user].get('role') == 'admin'
        
        # Gösterilecek ayarları hazırla
        display_settings = server_settings.copy()
        
        if not is_admin and 'resources' in user_data:
            ram_mb = user_data['resources'].get('ram_mb', 3072)
            display_settings['min_ram'] = f"{ram_mb}M"
            display_settings['max_ram'] = f"{ram_mb}M"

        ddos_logs = []
        if is_admin:
            ddos_logs = get_ddos_logs()

        bore_address, bore_active = get_bore_info(target_server)

        return render_template_string(html, 
            ip=ip, status=status, page="settings",
            server_jar_name=SERVER_JAR_NAME,
            java_path=java_path,
            eula_agreed=eula_agreed, 
            process=user_processes.get(target_server), 
            jar_exists=jar_exists(target_server),
            server_settings=display_settings,
            username=session_user,
            user_data=user_data,
            is_admin=is_admin,
            ddos_logs=ddos_logs,
            target_server=target_server,
            bore_address=bore_address,
            bore_active=bore_active,
            server_props=read_server_properties(target_server),
            is_huggingface=IS_HUGGINGFACE
        )

    except Exception as e:
        logging.error(f"Ayarlar sayfası hatası ({session_user} on {target_server}): {e}")
        return f"<h2>Hata: {e}</h2>", 500

@app.route("/setoptions", methods=["POST"])
@login_required
def set_options():
    global java_path
    global server_settings
    
    try:
        # Yetki kontrolü
        username = session.get('username')
        user_data = users.get(username, {})
        is_admin = user_data.get('role') == 'admin'
        
        if not is_admin:
            logging.warning(f"Yetkisiz ayar değiştirme denemesi: {username}")
            # Normal kullanıcılar ayar değiştiremez, sessizce yönlendir
            return redirect("/settings")

        java_path = request.form.get("java_path", "java").strip().replace('"', '').replace("'", '')
        
        # Sunucu ayarlarını güncelle
        server_settings.update({
            "min_ram": request.form.get("min_ram", "1024M").strip(),
            "max_ram": request.form.get("max_ram", "1024M").strip(),
            "java_args": request.form.get("java_args", "").strip()
        })
        
        # Ayarları kaydet
        save_settings()
        
        logging.info(f"Ayarlar güncellendi: Java={java_path}, RAM={server_settings['min_ram']}-{server_settings['max_ram']}")
        return redirect("/settings")
    
    except Exception as e:
        logging.error(f"Ayarlar kaydetme hatası: {e}")
        return redirect("/settings")

@app.route("/api/generate_access", methods=["POST"])
def api_generate_access():
    """Konsol komutu için erişim kodu oluştur"""
    # Güvenlik Kontrolü (API Key veya Admin Oturumu)
    auth_success = False
    
    # 1. API Key Kontrolü
    api_key = request.headers.get('X-API-Key')
    if api_key and api_key == API_SECRET_KEY:
        auth_success = True
    
    # 2. Admin Oturumu Kontrolü
    if not auth_success:
        if 'username' in session and session['username'] in users:
            if users[session['username']].get('role') == 'admin':
                auth_success = True
    
    if not auth_success:
        return jsonify({"success": False, "error": "Yetkisiz Erişim! API Key veya Admin girişi gerekli."}), 401

    try:
        # Komut formatı: /panel (ram) (depolama) (cpu) (süre)
        command = request.json.get('command', '').strip()
        
        # Komutu parse et
        if not command.startswith('/panel'):
            return jsonify({"success": False, "error": "Geçersiz komut formatı. /panel (ram) (depolama) (cpu) (süre) şeklinde olmalı."})
        
        # Parametreleri ayıkla
        parts = command.split()
        if len(parts) != 5:
            return jsonify({"success": False, "error": "Eksik parametre! Kullanım: /panel (ram_mb) (depolama_gb) (cpu_cores) (süre_saat)"})
        
        try:
            ram_mb = int(parts[1])
            storage_gb = int(parts[2])
            cpu_cores = int(parts[3])
            hours = int(parts[4])
        except ValueError:
            return jsonify({"success": False, "error": "Parametreler sayı olmalı! Kullanım: /panel (ram_mb) (depolama_gb) (cpu_cores) (süre_saat)"})
        
        # Limit kontrolleri
        if ram_mb < 512 or ram_mb > 16384:
            return jsonify({"success": False, "error": "RAM 512-16384 MB arası olmalı"})
        
        if storage_gb < 1 or storage_gb > 100:
            return jsonify({"success": False, "error": "Depolama 1-100 GB arası olmalı"})
        
        if cpu_cores < 1 or cpu_cores > 16:
            return jsonify({"success": False, "error": "CPU 1-16 çekirdek arası olmalı"})
        
        if hours < 1 or hours > 720:
            return jsonify({"success": False, "error": "Süre 1-720 saat arası olmalı"})
        
        # Erişim kodu oluştur
        code = generate_access_code(ram_mb, cpu_cores, storage_gb, hours)
        
        logging.info(f"Yeni erişim kodu oluşturuldu: {code}, RAM: {ram_mb}MB, CPU: {cpu_cores}, Depolama: {storage_gb}GB, Süre: {hours}saat")
        
        return jsonify({
            "success": True,
            "code": code,
            "username": "skyvex",
            "resources": {
                "ram_mb": ram_mb,
                "cpu_cores": cpu_cores,
                "storage_gb": storage_gb
            },
            "expires_in_hours": hours,
            "message": f"✅ Erişim kodu oluşturuldu!\nKullanıcı Adı: skyvex\nŞifre: {code}\nSüre: {hours} saat\nRAM: {ram_mb}MB\nCPU: {cpu_cores} çekirdek\nDepolama: {storage_gb}GB"
        })
        
    except Exception as e:
        logging.error(f"Erişim kodu oluşturma hatası: {e}")
        return jsonify({"success": False, "error": f"Sistem hatası: {str(e)}"}), 500

@app.route("/api/list_active_users")
@admin_required
def api_list_active_users():
    """Aktif kullanıcıları listele"""
    try:
        cleanup_expired_users()  # Süresi dolanları temizle
        
        active_users = []
        for username, data in users.items():
            if data.get('role') == 'user':
                expires_at = data.get('expires_at')
                if expires_at:
                    try:
                        expires_dt = datetime.fromisoformat(data["expires_at"])
                        time_left = expires_dt - datetime.now()
                        hours_left = max(0, int(time_left.total_seconds() / 3600))
                        
                        if hours_left > 0:
                            active_users.append({
                                'username': username,
                                'created_at': data.get('created_at'),
                                'expires_at': expires_at,
                                'hours_left': hours_left,
                                'resources': data.get('resources', {})
                            })
                    except:
                        continue
        
        return jsonify({"active_users": active_users})
    except Exception as e:
        logging.error(f"Aktif kullanıcı listeleme hatası: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/user/update_java", methods=["POST"])
@login_required
def user_update_java():
    """Kullanıcının Java sürümünü günceller"""
    session_user = session.get('username')
    target_server = get_selected_server(session_user)

    java_version = request.form.get('java_version', '21').strip()
    if java_version not in ['8', '17', '21']:
        return redirect('/settings?error=invalid_java')

    if target_server in users:
        if 'server_config' not in users[target_server]:
            users[target_server]['server_config'] = {}
        users[target_server]['server_config']['java_version'] = java_version
        save_users()
        logging.info(f"Java sürümü güncellendi ({target_server}): Java {java_version}")

    return redirect('/settings?success=java_updated')


@app.route("/delete_server", methods=["POST"])
@login_required
def delete_server_route():
    """Kullanıcının sunucusunu ve tüm dosyalarını kalıcı olarak siler, plan seçimine yönlendirir"""
    session_user = session.get('username')
    target_server = get_selected_server(session_user)

    # Güvenlik: sadece kendi sunucusunu silebilir
    if session_user != target_server and users.get(session_user, {}).get('role') != 'admin':
        return redirect('/dashboard?error=no_permission')

    try:
        # Sunucu çalışıyorsa durdur
        proc = user_processes.get(target_server)
        if proc and proc != "REMOTE":
            try:
                proc.stdin.write(b"stop\n")
                proc.stdin.flush()  
                proc.wait(timeout=5)
            except:
                try: proc.kill()
                except: pass
        user_processes[target_server] = None

        # Node üzerinde de durdur
        user_data = users.get(target_server, {})
        node_id = user_data.get('node_id')
        if node_id and node_id in nodes:
            try:
                node_url = nodes[node_id]['info'].get('public_url') or f"http://{nodes[node_id]['info'].get('ip')}:7860"
                requests.post(f"{node_url}/exec", json={"action": "stop", "username": target_server, "secret": NODE_SECRET}, timeout=5)
            except: pass

        # Kullanıcı dosyalarını tamamen sil (eula dahil her şey)
        user_dir = get_user_dir(target_server)
        if os.path.exists(user_dir):
            # Salt-okunur dosyaları yazabilir yap
            for root_d, dirs_d, files_d in os.walk(user_dir):
                for fname in files_d:
                    fpath = os.path.join(root_d, fname)
                    try: os.chmod(fpath, 0o666)
                    except: pass
            shutil.rmtree(user_dir, ignore_errors=True)
            logging.info(f"Sunucu dosyaları silindi: {target_server}")
        os.makedirs(user_dir, exist_ok=True)  # Boş klasörü yeniden oluştur

        # Kullanıcı durumunu sıfırla (plan seçimine döner)
        if target_server in users:
            users[target_server]['has_selected_plan'] = False
            users[target_server]['server_ready'] = False
            users[target_server]['server_config'] = {}
            users[target_server]['node_id'] = None
            if 'assigned_port' in users[target_server]:
                del users[target_server]['assigned_port']  # Port serbest bırak
            save_users()

        # Console geçmişini temizle
        user_console_histories[target_server] = []
        session.pop('selected_server', None)

        logging.info(f"Sunucu silindi: {target_server} (isteyen: {session_user})")
        return redirect('/select-plan')

    except Exception as e:
        logging.error(f"Sunucu silme hatası ({target_server}): {e}")
        return redirect('/settings?error=delete_failed')


# Hata Yönetimi
@app.errorhandler(404)
def not_found(error):
    logging.warning(f"404 Hatasi: {request.url}")
    return render_template_string("<h2>Sayfa bulunamadi</h2>"), 404

@app.errorhandler(500)
def internal_error(error):
    logging.error(f"500 Hatasi: {error}", exc_info=True)
    return render_template_string("<h2>Sunucu hatasi olustu</h2><p>Lütfen sunucu dosyalarını kontrol edin veya yöneticiye başvurun.</p>"), 500

if __name__ == "__main__":
    # WispByte veya genel port kontrolü (Senin portun 15576 olarak ayarlandı)
    host = '0.0.0.0'
    port = int(os.environ.get('SERVER_PORT', os.environ.get('PORT', 15576)))
    
    print(f"Skyvex Panel baslatiliyor...")
    print(f"Adres: http://{host}:{PANEL_PORT}")
    print(f"WispByte Port: {PANEL_PORT}")
    
    app.run(host=host, port=PANEL_PORT, debug=False, threaded=True) 

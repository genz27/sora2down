import sqlite3
import os
from datetime import datetime

DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(__file__))
DB_PATH = os.path.join(DATA_DIR, 'sora_manager.db')
PROXY_FILE = os.path.join(DATA_DIR, 'proxy.txt')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # SQLite 性能优化
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA cache_size=10000')
    conn.execute('PRAGMA temp_store=MEMORY')
    return conn

def init_db():
    """初始化数据库表"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Sora账号表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sora_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            access_token TEXT,
            refresh_token TEXT,
            client_id TEXT DEFAULT 'app_OHnYmJt5u1XEdhDUx0ig1ziv',
            enabled INTEGER DEFAULT 1,
            last_used_at TEXT,
            request_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 代理池表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS proxies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy_url TEXT NOT NULL UNIQUE,
            enabled INTEGER DEFAULT 1,
            last_used_at TEXT,
            success_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 请求日志表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            proxy_id INTEGER,
            video_id TEXT,
            success INTEGER,
            error_msg TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 系统配置表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 初始化默认配置
    default_settings = [
        ('proxy_enabled', '0'),
        ('proxy_pool_enabled', '0'),
        ('retry_on_429', '1'),
        ('retry_on_403', '1'),
        ('max_retries', '3'),
        ('retry_delay', '2'),
        ('admin_password', 'admin123'),
        ('api_token', ''),
    ]
    for key, value in default_settings:
        cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
    
    conn.commit()
    conn.close()

# ========== 账号管理 ==========
def get_all_accounts():
    conn = get_db()
    accounts = conn.execute('SELECT * FROM sora_accounts ORDER BY id').fetchall()
    conn.close()
    return [dict(a) for a in accounts]

def get_enabled_accounts():
    conn = get_db()
    accounts = conn.execute('SELECT * FROM sora_accounts WHERE enabled=1 ORDER BY last_used_at ASC NULLS FIRST').fetchall()
    conn.close()
    return [dict(a) for a in accounts]

def get_account_by_id(account_id):
    conn = get_db()
    account = conn.execute('SELECT * FROM sora_accounts WHERE id=?', (account_id,)).fetchone()
    conn.close()
    return dict(account) if account else None

def add_account(name, access_token, refresh_token, client_id=None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO sora_accounts (name, access_token, refresh_token, client_id)
        VALUES (?, ?, ?, ?)
    ''', (name, access_token, refresh_token, client_id or 'app_OHnYmJt5u1XEdhDUx0ig1ziv'))
    conn.commit()
    account_id = cursor.lastrowid
    conn.close()
    return account_id

def update_account(account_id, **kwargs):
    conn = get_db()
    fields = []
    values = []
    for k, v in kwargs.items():
        if k in ['name', 'access_token', 'refresh_token', 'client_id', 'enabled']:
            fields.append(f'{k}=?')
            values.append(v)
    if fields:
        fields.append('updated_at=?')
        values.append(datetime.now().isoformat())
        values.append(account_id)
        conn.execute(f'UPDATE sora_accounts SET {",".join(fields)} WHERE id=?', values)
        conn.commit()
    conn.close()

def delete_account(account_id):
    conn = get_db()
    conn.execute('DELETE FROM sora_accounts WHERE id=?', (account_id,))
    conn.commit()
    conn.close()

def update_account_usage(account_id, success=True, new_access_token=None, new_refresh_token=None):
    conn = get_db()
    now = datetime.now().isoformat()
    if success:
        conn.execute('UPDATE sora_accounts SET last_used_at=?, request_count=request_count+1 WHERE id=?', (now, account_id))
    else:
        conn.execute('UPDATE sora_accounts SET last_used_at=?, error_count=error_count+1 WHERE id=?', (now, account_id))
    
    if new_access_token and new_refresh_token:
        conn.execute('UPDATE sora_accounts SET access_token=?, refresh_token=?, updated_at=? WHERE id=?',
                     (new_access_token, new_refresh_token, now, account_id))
    conn.commit()
    conn.close()

# ========== 代理管理 ==========
def get_all_proxies():
    conn = get_db()
    proxies = conn.execute('SELECT * FROM proxies ORDER BY id').fetchall()
    conn.close()
    return [dict(p) for p in proxies]

def get_enabled_proxies():
    conn = get_db()
    proxies = conn.execute('SELECT * FROM proxies WHERE enabled=1 ORDER BY last_used_at ASC NULLS FIRST').fetchall()
    conn.close()
    return [dict(p) for p in proxies]

def add_proxy(proxy_url):
    # 标准化代理格式
    proxy_url = normalize_proxy(proxy_url)
    if not proxy_url:
        return False
    conn = get_db()
    try:
        conn.execute('INSERT INTO proxies (proxy_url) VALUES (?)', (proxy_url,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def replace_all_proxies(proxy_list):
    """清空并替换所有代理"""
    conn = get_db()
    conn.execute('DELETE FROM proxies')
    count = 0
    for proxy_str in proxy_list:
        proxy_url = normalize_proxy(proxy_str)
        if proxy_url:
            try:
                conn.execute('INSERT INTO proxies (proxy_url) VALUES (?)', (proxy_url,))
                count += 1
            except sqlite3.IntegrityError:
                pass  # 重复的跳过
    conn.commit()
    conn.close()
    return count

def update_proxy(proxy_id, **kwargs):
    conn = get_db()
    fields = []
    values = []
    for k, v in kwargs.items():
        if k in ['proxy_url', 'enabled']:
            fields.append(f'{k}=?')
            values.append(v)
    if fields:
        values.append(proxy_id)
        conn.execute(f'UPDATE proxies SET {",".join(fields)} WHERE id=?', values)
        conn.commit()
    conn.close()

def delete_proxy(proxy_id):
    conn = get_db()
    conn.execute('DELETE FROM proxies WHERE id=?', (proxy_id,))
    conn.commit()
    conn.close()

def update_proxy_usage(proxy_id, success=True):
    conn = get_db()
    now = datetime.now().isoformat()
    if success:
        conn.execute('UPDATE proxies SET last_used_at=?, success_count=success_count+1 WHERE id=?', (now, proxy_id))
    else:
        conn.execute('UPDATE proxies SET last_used_at=?, fail_count=fail_count+1 WHERE id=?', (now, proxy_id))
    conn.commit()
    conn.close()

# ========== 设置管理 ==========
def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else default

def get_all_settings():
    conn = get_db()
    rows = conn.execute('SELECT key, value FROM settings').fetchall()
    conn.close()
    return {row['key']: row['value'] for row in rows}

def set_setting(key, value):
    conn = get_db()
    conn.execute('''
        INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?
    ''', (key, value, datetime.now().isoformat(), value, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def set_settings(settings_dict):
    conn = get_db()
    now = datetime.now().isoformat()
    for key, value in settings_dict.items():
        conn.execute('''
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?
        ''', (key, value, now, value, now))
    conn.commit()
    conn.close()

# ========== 代理池文件 ==========
def load_proxies_from_file():
    """从 proxy.txt 文件加载代理到数据库"""
    if not os.path.exists(PROXY_FILE):
        return 0
    
    count = 0
    with open(PROXY_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # 标准化代理格式
            proxy_url = normalize_proxy(line)
            if proxy_url and add_proxy(proxy_url):
                count += 1
    return count

def normalize_proxy(proxy_str):
    """标准化代理格式"""
    proxy_str = proxy_str.strip()
    if not proxy_str:
        return None
    
    # 已经是完整格式
    if proxy_str.startswith(('http://', 'https://', 'socks5://', 'socks4://')):
        return proxy_str
    
    # ip:port 格式
    if ':' in proxy_str:
        parts = proxy_str.split(':')
        if len(parts) == 2:
            return f'http://{proxy_str}'
        elif len(parts) == 4:
            # ip:port:user:pass 格式
            return f'http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}'
    
    return None

# ========== 日志 ==========
def add_log(account_id, proxy_id, video_id, success, error_msg=None):
    conn = get_db()
    conn.execute('''
        INSERT INTO request_logs (account_id, proxy_id, video_id, success, error_msg)
        VALUES (?, ?, ?, ?, ?)
    ''', (account_id, proxy_id, video_id, 1 if success else 0, error_msg))
    conn.commit()
    conn.close()

def get_recent_logs(limit=100):
    conn = get_db()
    logs = conn.execute('''
        SELECT l.*, a.name as account_name, p.proxy_url
        FROM request_logs l
        LEFT JOIN sora_accounts a ON l.account_id = a.id
        LEFT JOIN proxies p ON l.proxy_id = p.id
        ORDER BY l.created_at DESC LIMIT ?
    ''', (limit,)).fetchall()
    conn.close()
    return [dict(l) for l in logs]

def get_stats():
    """获取统计数据"""
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) as cnt FROM request_logs').fetchone()['cnt']
    success = conn.execute('SELECT COUNT(*) as cnt FROM request_logs WHERE success=1').fetchone()['cnt']
    conn.close()
    return {'total': total, 'success': success}

# 初始化
init_db()

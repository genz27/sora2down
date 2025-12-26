import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'sora_manager.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    conn = get_db()
    try:
        conn.execute('INSERT INTO proxies (proxy_url) VALUES (?)', (proxy_url,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

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

# 初始化
init_db()

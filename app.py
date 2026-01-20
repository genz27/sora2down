import os
import re
import time
import threading
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from curl_cffi.requests import Session, errors
from dotenv import load_dotenv
import database as db

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'sora-studio-pro-secret-key-2024')

# 轮询索引
account_index = 0
proxy_index = 0
index_lock = threading.Lock()


# 缓存
_settings_cache = {'data': None, 'expires': 0}
_accounts_cache = {'data': None, 'expires': 0}
_proxies_cache = {'data': None, 'expires': 0}

_thread_local = threading.local()
_SESSION_CACHE_MAX = 20


def get_settings():
    """获取所有设置（带缓存，30秒TTL）"""
    now = time.time()
    if _settings_cache['data'] is not None and now < _settings_cache['expires']:
        return _settings_cache['data']
    
    settings = db.get_all_settings()
    _settings_cache['data'] = settings
    _settings_cache['expires'] = now + 30
    return settings


def invalidate_settings_cache():
    """清除设置缓存"""
    _settings_cache['data'] = None
    _settings_cache['expires'] = 0


def get_admin_password():
    """获取管理员密码"""
    settings = get_settings()
    return settings.get('admin_password') or os.getenv('ADMIN_PASSWORD', 'admin123')


def get_api_token():
    """获取 API 访问令牌"""
    settings = get_settings()
    return settings.get('api_token') or os.getenv('APP_ACCESS_TOKEN') or ''


def get_next_account():
    """轮询获取下一个可用账号（带缓存，10秒TTL）"""
    global account_index
    now = time.time()
    
    # 使用缓存的账号列表
    if _accounts_cache['data'] is None or now >= _accounts_cache['expires']:
        _accounts_cache['data'] = db.get_enabled_accounts()
        _accounts_cache['expires'] = now + 10
    
    accounts = _accounts_cache['data']
    if not accounts:
        return None
    with index_lock:
        account_index = account_index % len(accounts)
        account = accounts[account_index]
        account_index += 1
    return account


def invalidate_accounts_cache():
    """清除账号缓存"""
    _accounts_cache['data'] = None
    _accounts_cache['expires'] = 0


def invalidate_proxies_cache():
    """清除代理缓存"""
    _proxies_cache['data'] = None
    _proxies_cache['expires'] = 0


def _get_cached_proxies():
    now = time.time()
    if _proxies_cache['data'] is None or now >= _proxies_cache['expires']:
        _proxies_cache['data'] = db.get_enabled_proxies()
        _proxies_cache['expires'] = now + 10
    return _proxies_cache['data']


def _trim_sessions(sessions):
    overflow = len(sessions) - _SESSION_CACHE_MAX
    if overflow <= 0:
        return
    oldest = sorted(sessions.items(), key=lambda item: item[1]['last_used'])[:overflow]
    for key, info in oldest:
        try:
            info['session'].close()
        except Exception:
            pass
        sessions.pop(key, None)


def get_http_session(proxy=None, timeout=15):
    """获取 HTTP Session，带连接超时控制"""
    proxy_url = proxy.get('proxy_url') if isinstance(proxy, dict) else proxy
    key = proxy_url or 'direct'
    sessions = getattr(_thread_local, 'sessions', None)
    if sessions is None:
        sessions = {}
        _thread_local.sessions = sessions

    entry = sessions.get(key)
    if entry:
        entry['last_used'] = time.time()
        return entry['session']

    proxies = {}
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}

    # 设置连接超时，避免代理连接卡住
    sess = Session(impersonate="chrome110", proxies=proxies, timeout=timeout)
    sessions[key] = {'session': sess, 'last_used': time.time()}
    _trim_sessions(sessions)
    return sess


def get_next_proxy():
    """轮询获取下一个可用代理"""
    global proxy_index
    settings = get_settings()
    
    # 检查是否启用代理
    if settings.get('proxy_enabled') != '1':
        return None
    
    # 检查是否启用代理池
    if settings.get('proxy_pool_enabled') == '1':
        proxies = _get_cached_proxies()
        if proxies:
            with index_lock:
                proxy_index = proxy_index % len(proxies)
                proxy = proxies[proxy_index]
                proxy_index += 1
            return proxy
    
    return None


def refresh_token(account, proxy=None):
    """刷新账号的 access_token"""
    sess = get_http_session(proxy)
    url = "https://auth.openai.com/oauth/token"
    payload = {
        "client_id": account.get('client_id', 'app_OHnYmJt5u1XEdhDUx0ig1ziv'),
        "grant_type": "refresh_token",
        "redirect_uri": "com.openai.sora://auth.openai.com/android/com.openai.sora/callback",
        "refresh_token": account['refresh_token']
    }
    
    response = sess.post(url, json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()
    
    db.update_account_usage(
        account['id'], 
        success=True,
        new_access_token=data['access_token'],
        new_refresh_token=data['refresh_token']
    )
    
    return data['access_token'], data['refresh_token']


def make_sora_api_call(video_id, account, proxy=None):
    """执行 Sora API 请求"""
    sess = get_http_session(proxy)
    api_url = f"https://sora.chatgpt.com/backend/project_y/post/{video_id}"
    
    headers = {
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip',
        'oai-package-name': 'com.openai.sora',
        'authorization': f'Bearer {account["access_token"]}',
        'User-Agent': 'Sora/1.2025.308'
    }
    
    response = sess.get(api_url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


def process_sora_request(video_id, account, proxy, proxy_id):
    """处理 Sora 请求，包含重试逻辑"""
    settings = get_settings()
    max_retries = int(settings.get('max_retries', '3'))
    retry_delay = int(settings.get('retry_delay', '2'))
    retry_on_429 = settings.get('retry_on_429') == '1'
    retry_on_403 = settings.get('retry_on_403') == '1'
    
    last_error = None
    
    for attempt in range(max_retries + 1):
        try:
            response_data = make_sora_api_call(video_id, account, proxy)
            download_link = response_data['post']['attachments'][0]['encodings']['source']['path']
            return {'success': True, 'download_link': download_link}
            
        except errors.RequestsError as e:
            last_error = str(e)
            status_code = e.response.status_code if e.response else None
            
            # 429 Too Many Requests
            if status_code == 429 and retry_on_429:
                print(f"[429] attempt {attempt + 1}/{max_retries + 1}")
                if attempt < max_retries:
                    # 切换代理并重试
                    proxy = get_next_proxy()
                    proxy_id = proxy['id'] if proxy else None
                    time.sleep(retry_delay * (attempt + 1))
                    continue
            
            # 403 Forbidden
            if status_code == 403 and retry_on_403:
                print(f"[403] attempt {attempt + 1}/{max_retries + 1}")
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
            
            # 401 尝试刷新 token
            if status_code == 401:
                try:
                    new_access, new_refresh = refresh_token(account, proxy)
                    account['access_token'] = new_access
                    continue
                except Exception as refresh_error:
                    last_error = f"Token 刷新失败: {refresh_error}"
            
            break
            
        except (KeyError, IndexError):
            last_error = "无法从API响应中找到下载链接"
            break
        except Exception as e:
            last_error = str(e)
            break
    
    return {'success': False, 'error': last_error, 'proxy_id': proxy_id}


# ========== 页面路由 ==========
@app.route('/')
def index():
    api_token = get_api_token()
    auth_required = bool(api_token)
    return render_template('index.html', auth_required=auth_required)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == get_admin_password():
            session['admin_logged_in'] = True
            return redirect(url_for('manage'))
        return render_template('login.html', error='密码错误')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('index'))


@app.route('/manage')
def manage():
    if not session.get('admin_logged_in'):
        return redirect(url_for('login'))
    return render_template('manage.html')


# ========== API 路由 ==========
@app.route('/get-sora-link', methods=['POST'])
def get_sora_link():
    account = get_next_account()
    if not account:
        return jsonify({"error": "没有可用的 Sora 账号，请在管理后台添加。"}), 500

    api_token = get_api_token()
    if api_token:
        if request.json.get('token') != api_token:
            return jsonify({"error": "无效或缺失的访问令牌。"}), 401

    sora_url = request.json.get('url')
    if not sora_url:
        return jsonify({"error": "未提供 URL"}), 400

    match = re.search(r'sora\.chatgpt\.com/p/([a-zA-Z0-9_]+)', sora_url)
    if not match:
        return jsonify({"error": "无效的 Sora 链接格式。请发布后复制分享链接"}), 400

    video_id = match.group(1)
    proxy = get_next_proxy()
    proxy_id = proxy['id'] if proxy else None

    result = process_sora_request(video_id, account, proxy, proxy_id)
    
    if result['success']:
        db.update_account_usage(account['id'], success=True)
        if proxy_id:
            db.update_proxy_usage(proxy_id, success=True)
        db.add_log(account['id'], proxy_id, video_id, success=True)
        return jsonify({"download_link": result['download_link']})
    else:
        db.update_account_usage(account['id'], success=False)
        if result.get('proxy_id'):
            db.update_proxy_usage(result['proxy_id'], success=False)
        db.add_log(account['id'], result.get('proxy_id'), video_id, success=False, error_msg=result['error'])
        return jsonify({"error": f"请求失败: {result['error']}"}), 500


# ========== 管理 API ==========
def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return jsonify({"error": "未授权"}), 401
        return f(*args, **kwargs)
    return decorated


# 账号管理
@app.route('/api/accounts', methods=['GET'])
@admin_required
def api_get_accounts():
    return jsonify(db.get_all_accounts())


@app.route('/api/accounts/<int:account_id>', methods=['GET'])
@admin_required
def api_get_account(account_id):
    account = db.get_account_by_id(account_id)
    if not account:
        return jsonify({"error": "账号不存在"}), 404
    return jsonify(account)


@app.route('/api/accounts', methods=['POST'])
@admin_required
def api_add_account():
    data = request.json
    account_id = db.add_account(
        name=data.get('name', '未命名'),
        access_token=data.get('access_token'),
        refresh_token=data.get('refresh_token'),
        client_id=data.get('client_id')
    )
    invalidate_accounts_cache()  # 清除缓存
    return jsonify({"id": account_id, "success": True})


@app.route('/api/accounts/<int:account_id>', methods=['PUT'])
@admin_required
def api_update_account(account_id):
    data = request.json
    db.update_account(account_id, **data)
    invalidate_accounts_cache()  # 清除缓存
    return jsonify({"success": True})


@app.route('/api/accounts/<int:account_id>', methods=['DELETE'])
@admin_required
def api_delete_account(account_id):
    db.delete_account(account_id)
    invalidate_accounts_cache()  # 清除缓存
    return jsonify({"success": True})


# 代理管理
@app.route('/api/proxies', methods=['GET'])
@admin_required
def api_get_proxies():
    return jsonify(db.get_all_proxies())


@app.route('/api/proxies', methods=['POST'])
@admin_required
def api_add_proxy():
    data = request.json
    success = db.add_proxy(data.get('proxy_url'))
    if success:
        invalidate_proxies_cache()
    return jsonify({"success": success})


@app.route('/api/proxies/<int:proxy_id>', methods=['PUT'])
@admin_required
def api_update_proxy(proxy_id):
    data = request.json
    db.update_proxy(proxy_id, **data)
    invalidate_proxies_cache()
    return jsonify({"success": True})


@app.route('/api/proxies/<int:proxy_id>', methods=['DELETE'])
@admin_required
def api_delete_proxy(proxy_id):
    db.delete_proxy(proxy_id)
    invalidate_proxies_cache()
    return jsonify({"success": True})


@app.route('/api/proxies/replace', methods=['POST'])
@admin_required
def api_replace_proxies():
    """替换所有代理"""
    data = request.json
    proxies = data.get('proxies', [])
    count = db.replace_all_proxies(proxies)
    invalidate_proxies_cache()
    return jsonify({"success": True, "count": count})


@app.route('/api/proxies/reload', methods=['POST'])
@admin_required
def api_reload_proxies():
    """从 proxy.txt 重新加载代理"""
    count = db.load_proxies_from_file()
    invalidate_proxies_cache()
    return jsonify({"success": True, "loaded": count})


# 设置管理
@app.route('/api/settings', methods=['GET'])
@admin_required
def api_get_settings():
    return jsonify(db.get_all_settings())


@app.route('/api/settings', methods=['PUT'])
@admin_required
def api_update_settings():
    data = request.json
    db.set_settings(data)
    invalidate_settings_cache()  # 清除缓存
    return jsonify({"success": True})


# 日志
@app.route('/api/logs', methods=['GET'])
@admin_required
def api_get_logs():
    return jsonify(db.get_recent_logs(100))


# 统计
@app.route('/api/stats', methods=['GET'])
@admin_required
def api_get_stats():
    return jsonify(db.get_stats())


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)

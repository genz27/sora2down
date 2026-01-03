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

# 配置
APP_ACCESS_TOKEN = os.getenv('APP_ACCESS_TOKEN')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')

# 轮询索引
account_index = 0
proxy_index = 0
index_lock = threading.Lock()


class CloudflareState:
    """
    全局 Cloudflare 状态管理器
    - cf_clearance cookies 和 user_agent 全局共享
    - 所有请求自动使用相同凭据，直到遇到新的 429/403
    - 线程安全
    """
    
    def __init__(self):
        self._lock = threading.RLock()  # 使用 RLock 避免重入死锁
        self._cf_clearance = None
        self._user_agent = None
        self._cookies = {}
        self._expires_at = 0
        self._is_valid = False
    
    @property
    def cf_clearance(self):
        with self._lock:
            return self._cf_clearance
    
    @property
    def user_agent(self):
        with self._lock:
            return self._user_agent
    
    @property
    def cookies(self):
        with self._lock:
            return self._cookies.copy()
    
    @property
    def is_valid(self):
        """检查当前凭据是否有效"""
        with self._lock:
            if not self._is_valid or not self._cf_clearance:
                return False
            if time.time() > self._expires_at:
                return False
            return True
    
    def update(self, cf_clearance, user_agent, cookies=None, ttl=600):
        """
        更新 CF 凭据
        :param cf_clearance: cf_clearance cookie 值
        :param user_agent: 对应的 User-Agent
        :param cookies: 其他 cookies (可选)
        :param ttl: 有效期秒数，默认 10 分钟
        """
        with self._lock:
            self._cf_clearance = cf_clearance
            self._user_agent = user_agent
            self._cookies = cookies or {}
            if cf_clearance:
                self._cookies['cf_clearance'] = cf_clearance
            self._expires_at = time.time() + ttl
            self._is_valid = True
            print(f"[CloudflareState] Updated: clearance={cf_clearance[:20] if cf_clearance else None}..., ua={user_agent[:30] if user_agent else None}...")
    
    def invalidate(self):
        """标记当前凭据无效（遇到 429/403 时调用）"""
        with self._lock:
            self._is_valid = False
            print("[CloudflareState] Invalidated due to 429/403")
    
    def clear(self):
        """清除所有凭据"""
        with self._lock:
            self._cf_clearance = None
            self._user_agent = None
            self._cookies = {}
            self._expires_at = 0
            self._is_valid = False
            print("[CloudflareState] Cleared")
    
    def get_request_data(self):
        """
        获取请求所需的数据
        :return: dict with 'user_agent' and 'cookies', or None if invalid
        """
        with self._lock:
            if not self._is_valid or not self._cf_clearance:
                return None
            if time.time() > self._expires_at:
                self._is_valid = False
                return None
            return {
                'user_agent': self._user_agent,
                'cookies': self._cookies.copy(),
                'cf_clearance': self._cf_clearance
            }
    
    def get_status(self):
        """获取当前状态信息"""
        with self._lock:
            remaining = max(0, int(self._expires_at - time.time())) if self._is_valid else 0
            return {
                'is_valid': self._is_valid and self._cf_clearance is not None,
                'has_clearance': self._cf_clearance is not None,
                'user_agent': self._user_agent[:50] + '...' if self._user_agent and len(self._user_agent) > 50 else self._user_agent,
                'expires_in_seconds': remaining,
                'cookies_count': len(self._cookies)
            }


# 全局 CF 状态实例
cf_state = CloudflareState()

# 缓存
_settings_cache = {'data': None, 'expires': 0}
_accounts_cache = {'data': None, 'expires': 0}


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


def get_next_proxy():
    """轮询获取下一个可用代理"""
    global proxy_index
    settings = get_settings()
    
    # 检查是否启用代理
    if settings.get('proxy_enabled') != '1':
        return None
    
    # 检查是否启用代理池
    if settings.get('proxy_pool_enabled') == '1':
        proxies = db.get_enabled_proxies()
        if proxies:
            with index_lock:
                proxy_index = proxy_index % len(proxies)
                proxy = proxies[proxy_index]
                proxy_index += 1
            return proxy
    
    return None


def solve_cf_challenge(proxy=None):
    """调用外部 CF Solver 服务获取 cf_clearance"""
    settings = get_settings()
    
    if settings.get('cf_solver_enabled') != '1':
        return None
    
    solver_url = settings.get('cf_solver_url', 'http://localhost:8000/v1/challenge')
    
    try:
        params = {'url': 'https://sora.chatgpt.com', 'skip_cache': 'true'}
        if proxy:
            params['proxy'] = proxy.get('proxy_url') if isinstance(proxy, dict) else proxy
        
        sess = Session(impersonate="chrome110", timeout=120)
        response = sess.get(solver_url, params=params)
        
        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                result = {
                    'cf_clearance': data.get('cf_clearance'),
                    'user_agent': data.get('user_agent'),
                    'cookies': data.get('cookies', {})
                }
                # 更新全局状态
                cf_state.update(
                    cf_clearance=result['cf_clearance'],
                    user_agent=result['user_agent'],
                    cookies=result['cookies']
                )
                return result
    except Exception as e:
        print(f"CF Solver error: {e}")
    
    return None


def get_cf_clearance(proxy=None, force_refresh=False):
    """获取 CF clearance，优先使用全局缓存"""
    global cf_state
    
    # 如果不强制刷新且全局状态有效，直接返回
    if not force_refresh and cf_state.is_valid:
        return cf_state.get_request_data()
    
    # 获取新的 clearance
    result = solve_cf_challenge(proxy)
    if result:
        return cf_state.get_request_data()
    
    return None


def refresh_token(account, proxy=None):
    """刷新账号的 access_token"""
    proxies = {}
    if proxy:
        proxy_url = proxy.get('proxy_url') if isinstance(proxy, dict) else proxy
        proxies = {"http": proxy_url, "https": proxy_url}
    
    sess = Session(impersonate="chrome110", proxies=proxies)
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
    """执行 Sora API 请求，自动使用全局 CF 凭据"""
    proxies = {}
    if proxy:
        proxy_url = proxy.get('proxy_url') if isinstance(proxy, dict) else proxy
        proxies = {"http": proxy_url, "https": proxy_url}
    
    sess = Session(impersonate="chrome110", proxies=proxies)
    api_url = f"https://sora.chatgpt.com/backend/project_y/post/{video_id}"
    
    # 获取全局 CF 凭据
    cf_data = cf_state.get_request_data()
    
    headers = {
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip',
        'oai-package-name': 'com.openai.sora',
        'authorization': f'Bearer {account["access_token"]}'
    }
    
    # 使用全局 CF User-Agent，否则使用默认
    if cf_data and cf_data.get('user_agent'):
        headers['User-Agent'] = cf_data['user_agent']
    else:
        headers['User-Agent'] = 'Sora/1.2025.308'
    
    # 使用全局 CF cookies
    cookies = {}
    if cf_data and cf_data.get('cookies'):
        cookies = cf_data['cookies']
    
    response = sess.get(api_url, headers=headers, cookies=cookies, timeout=20)
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
    
    # 首次请求前，如果启用了 CF Solver 且没有有效凭据，先获取
    if settings.get('cf_solver_enabled') == '1' and not cf_state.is_valid:
        get_cf_clearance(proxy, force_refresh=True)
    
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
                # 标记 CF 凭据无效
                cf_state.invalidate()
                
                if attempt < max_retries:
                    # 切换代理并刷新 CF 凭据
                    proxy = get_next_proxy()
                    proxy_id = proxy['id'] if proxy else None
                    if settings.get('cf_solver_enabled') == '1':
                        get_cf_clearance(proxy, force_refresh=True)
                    time.sleep(retry_delay * (attempt + 1))
                    continue
            
            # 403 Forbidden (可能是 CF 挑战)
            if status_code == 403 and retry_on_403:
                print(f"[403] attempt {attempt + 1}/{max_retries + 1}")
                # 标记 CF 凭据无效
                cf_state.invalidate()
                
                if attempt < max_retries:
                    # 强制刷新 CF clearance
                    if settings.get('cf_solver_enabled') == '1':
                        get_cf_clearance(proxy, force_refresh=True)
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
    auth_required = APP_ACCESS_TOKEN is not None and APP_ACCESS_TOKEN != ""
    return render_template('index.html', auth_required=auth_required)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == ADMIN_PASSWORD:
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

    if APP_ACCESS_TOKEN:
        if request.json.get('token') != APP_ACCESS_TOKEN:
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
    return jsonify({"success": success})


@app.route('/api/proxies/<int:proxy_id>', methods=['PUT'])
@admin_required
def api_update_proxy(proxy_id):
    data = request.json
    db.update_proxy(proxy_id, **data)
    return jsonify({"success": True})


@app.route('/api/proxies/<int:proxy_id>', methods=['DELETE'])
@admin_required
def api_delete_proxy(proxy_id):
    db.delete_proxy(proxy_id)
    return jsonify({"success": True})


@app.route('/api/proxies/reload', methods=['POST'])
@admin_required
def api_reload_proxies():
    """从 proxy.txt 重新加载代理"""
    count = db.load_proxies_from_file()
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


# CF Solver 测试
@app.route('/api/cf-test', methods=['POST'])
@admin_required
def api_cf_test():
    """测试 CF Solver"""
    proxy = get_next_proxy()
    result = solve_cf_challenge(proxy)
    if result:
        return jsonify({"success": True, "data": result})
    return jsonify({"success": False, "error": "CF Solver 调用失败"})


# CF 状态查询
@app.route('/api/cf-status', methods=['GET'])
@admin_required
def api_cf_status():
    """获取当前 CF 状态"""
    return jsonify(cf_state.get_status())


# CF 状态清除
@app.route('/api/cf-clear', methods=['POST'])
@admin_required
def api_cf_clear():
    """清除 CF 凭据"""
    cf_state.clear()
    return jsonify({"success": True})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)

import os
import re
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


def get_next_account():
    """轮询获取下一个可用账号"""
    global account_index
    accounts = db.get_enabled_accounts()
    if not accounts:
        return None
    with index_lock:
        account_index = account_index % len(accounts)
        account = accounts[account_index]
        account_index += 1
    return account


def get_next_proxy():
    """轮询获取下一个可用代理"""
    global proxy_index
    proxies = db.get_enabled_proxies()
    if not proxies:
        return None
    with index_lock:
        proxy_index = proxy_index % len(proxies)
        proxy = proxies[proxy_index]
        proxy_index += 1
    return proxy


def refresh_token(account):
    """刷新账号的 access_token"""
    proxy = get_next_proxy()
    proxies = {}
    if proxy:
        proxies = {"http": proxy['proxy_url'], "https": proxy['proxy_url']}
    
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
    
    # 更新数据库中的 token
    db.update_account_usage(
        account['id'], 
        success=True,
        new_access_token=data['access_token'],
        new_refresh_token=data['refresh_token']
    )
    
    return data['access_token'], data['refresh_token']


def make_sora_api_call(video_id, account, proxy=None):
    """执行 Sora API 请求"""
    proxies = {}
    if proxy:
        proxies = {"http": proxy['proxy_url'], "https": proxy['proxy_url']}
    
    sess = Session(impersonate="chrome110", proxies=proxies)
    api_url = f"https://sora.chatgpt.com/backend/project_y/post/{video_id}"
    headers = {
        'User-Agent': 'Sora/1.2025.308',
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip',
        'oai-package-name': 'com.openai.sora',
        'authorization': f'Bearer {account["access_token"]}'
    }
    
    response = sess.get(api_url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


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
    # 获取可用账号
    account = get_next_account()
    if not account:
        return jsonify({"error": "没有可用的 Sora 账号，请在管理后台添加。"}), 500

    # 应用访问权限验证
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

    try:
        response_data = make_sora_api_call(video_id, account, proxy)
        download_link = response_data['post']['attachments'][0]['encodings']['source']['path']
        
        # 记录成功
        db.update_account_usage(account['id'], success=True)
        if proxy_id:
            db.update_proxy_usage(proxy_id, success=True)
        db.add_log(account['id'], proxy_id, video_id, success=True)
        
        return jsonify({"download_link": download_link})
        
    except errors.RequestsError as e:
        error_msg = str(e)
        
        # 如果是 401/403，尝试刷新 token
        if e.response is not None and e.response.status_code in [401, 403]:
            try:
                new_access, new_refresh = refresh_token(account)
                account['access_token'] = new_access
                
                # 重试
                response_data = make_sora_api_call(video_id, account, proxy)
                download_link = response_data['post']['attachments'][0]['encodings']['source']['path']
                
                db.update_account_usage(account['id'], success=True)
                if proxy_id:
                    db.update_proxy_usage(proxy_id, success=True)
                db.add_log(account['id'], proxy_id, video_id, success=True)
                
                return jsonify({"download_link": download_link})
                
            except Exception as refresh_error:
                error_msg = f"Token 刷新失败: {refresh_error}"
        
        # 记录失败
        db.update_account_usage(account['id'], success=False)
        if proxy_id:
            db.update_proxy_usage(proxy_id, success=False)
        db.add_log(account['id'], proxy_id, video_id, success=False, error_msg=error_msg)
        
        return jsonify({"error": f"请求失败: {error_msg}"}), 500
        
    except (KeyError, IndexError) as e:
        error_msg = "无法从API响应中找到下载链接"
        db.update_account_usage(account['id'], success=False)
        db.add_log(account['id'], proxy_id, video_id, success=False, error_msg=error_msg)
        return jsonify({"error": error_msg}), 500
        
    except Exception as e:
        error_msg = str(e)
        db.update_account_usage(account['id'], success=False)
        if proxy_id:
            db.update_proxy_usage(proxy_id, success=False)
        db.add_log(account['id'], proxy_id, video_id, success=False, error_msg=error_msg)
        return jsonify({"error": f"发生错误: {error_msg}"}), 500


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
    return jsonify({"id": account_id, "success": True})


@app.route('/api/accounts/<int:account_id>', methods=['PUT'])
@admin_required
def api_update_account(account_id):
    data = request.json
    db.update_account(account_id, **data)
    return jsonify({"success": True})


@app.route('/api/accounts/<int:account_id>', methods=['DELETE'])
@admin_required
def api_delete_account(account_id):
    db.delete_account(account_id)
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


# 日志
@app.route('/api/logs', methods=['GET'])
@admin_required
def api_get_logs():
    return jsonify(db.get_recent_logs(100))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)

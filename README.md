b# Sora2Down

Sora 视频批量下载工具，支持多账号轮询、代理池管理，内置管理后台与现代化 UI。

## 功能

- 批量解析 Sora 视频链接
- 批量并发处理（前端并发可调）
- 多账号轮询请求
- 代理池轮询支持
- 自动 Token 刷新
- 429/403 自动重试
- 管理后台
- Docker 部署

## 快速开始

### Docker 部署 (推荐)

```bash
docker build -t sora2down .
docker run -d -p 5001:5001 -v ./data:/app/data sora2down
```

### 本地运行

```bash
pip install -r requirements.txt
python app.py
```

访问 http://localhost:5001

## 配置

复制 `.env.example` 为 `.env` 并修改：

```env
ADMIN_PASSWORD=your_password
SECRET_KEY=your_secret_key
```

## 管理后台

访问 `/login` 进入管理后台，默认密码 `admin123`

### 账号管理
- 添加 Sora 账号 (Access Token + Refresh Token)
- 支持多账号轮询

### 代理池配置

**方式一：后台添加**

在管理后台 -> 代理池 -> 添加代理

**方式二：文件配置**

在 `data/proxy.txt` 中配置代理列表，每行一个：

```
# 支持格式
http://ip:port
http://user:pass@ip:port
socks5://ip:port
ip:port
ip:port:user:pass
```

然后在管理后台点击"从文件重载"

### 系统设置

- **启用代理**: 开启后请求通过代理发送
- **启用代理池轮询**: 自动轮询使用代理池中的代理
- **429 自动重试**: 遇到 429 时自动切换代理重试
- **403 自动重试**: 遇到 403 时自动重试

## 性能与并发

- 前端默认并发为 4，可在 `templates/index.html` 中修改 `MAX_CONCURRENCY`。
- 后端请求会话复用与代理列表缓存已启用，降低频繁连接和读取带来的开销。

## License

MIT

## GitHub

https://github.com/genz27/sora2down

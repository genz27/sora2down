FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 数据目录
RUN mkdir -p /app/data
VOLUME /app/data

# 环境变量
ENV PYTHONUNBUFFERED=1

EXPOSE 5001

CMD ["python", "app.py"]

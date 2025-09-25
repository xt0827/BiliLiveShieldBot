import asyncio
from bilibili_api import live, Credential, Danmaku
from pathlib import Path
import yaml
import os
import sys
import logging
from datetime import datetime, timedelta
import time
import re
import json
import pickle
from collections import defaultdict, deque
from flask import Flask, request, jsonify
import threading
from queue import Queue

restart_requested = False
danmaku_room = None
danmaku_messages = Queue(maxsize=1000)

class BanManager:
    def __init__(self, room, config):
        self.room = room
        self.config = config
        self.banned_users = self._load_data("banned_users.pkl", {})
        self.ban_history = self._load_data("ban_history.json", [])
        self.lock = threading.Lock()
        self.last_update = time.time()
    
    def _load_data(self, filename, default):
        try:
            if os.path.exists(filename):
                if filename.endswith('.pkl'):
                    with open(filename, 'rb') as f:
                        data = pickle.load(f)
                        if isinstance(data, dict):
                            for uid, (name, ban_time_str) in data.items():
                                data[uid] = (name, datetime.fromisoformat(ban_time_str))
                        return data
                else:
                    with open(filename, 'r', encoding='utf-8') as f:
                        return json.load(f)
        except Exception as e:
            print(f"加载{filename}失败: {e}")
        return default
    
    def _save_data(self, filename, data):
        try:
            with self.lock:
                if filename.endswith('.pkl'):
                    save_data = {}
                    for uid, (name, ban_time) in data.items():
                        save_data[uid] = (name, ban_time.isoformat())
                    with open(filename, 'wb') as f:
                        pickle.dump(save_data, f)
                else:
                    with open(filename, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                self.last_update = time.time()
        except Exception as e:
            print(f"保存{filename}失败: {e}")
    
    async def ban_user(self, user_uid, user_name):
        ban_hours = self.config.get("禁言时长", 2)
        
        try:
            result = await self.room.ban_user(uid=user_uid, hour=ban_hours)
            ban_time = datetime.now()
            
            self.banned_users[user_uid] = (user_name, ban_time)
            
            ban_record = {
                "user_uid": user_uid,
                "user_name": user_name,
                "ban_time": ban_time.isoformat(),
                "ban_hours": ban_hours,
                "unban_time": (ban_time + timedelta(hours=ban_hours)).isoformat(),
                "reason": "关键词刷屏"
            }
            self.ban_history.append(ban_record)
            
            asyncio.create_task(self._async_save())
            
            print(f"已禁言用户: {user_name}，时长{ban_hours}小时")
            return True
            
        except Exception as e:
            print(f"禁言失败 {user_name}: {e}")
            return False
    
    async def _async_save(self):
        await asyncio.get_event_loop().run_in_executor(None, self._sync_save)
    
    def _sync_save(self):
        self._save_data("banned_users.pkl", self.banned_users)
        self._save_data("ban_history.json", self.ban_history)
    
    async def check_unbans(self):
        current_time = datetime.now()
        ban_hours = self.config.get("禁言时长", 2)
        users_to_unban = []
        
        for user_uid, (user_name, ban_time) in list(self.banned_users.items()):
            if current_time - ban_time >= timedelta(hours=ban_hours):
                users_to_unban.append((user_uid, user_name))
        
        if not users_to_unban:
            return
        
        tasks = []
        for user_uid, user_name in users_to_unban:
            tasks.append(self._unban_user(user_uid, user_name, current_time))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success_count = sum(1 for r in results if r is True)
        
        if success_count > 0:
            await self._async_save()
            print(f"自动解禁完成: {success_count}个用户")
    
    async def _unban_user(self, user_uid, user_name, current_time):
        try:
            await self.room.unban_user(uid=user_uid)
            
            if user_uid in self.banned_users:
                del self.banned_users[user_uid]
            
            for record in self.ban_history:
                if record["user_uid"] == user_uid and "actual_unban_time" not in record:
                    record["actual_unban_time"] = current_time.isoformat()
                    record["status"] = "已解禁"
                    break
            
            print(f"已解禁用户: {user_name}")
            return True
            
        except Exception as e:
            print(f"解禁失败 {user_name}: {e}")
            return False
    
    def get_ranking(self, limit=20):
        ban_count = defaultdict(int)
        total_hours = defaultdict(int)
        
        for record in self.ban_history:
            uid = record["user_uid"]
            ban_count[uid] += 1
            total_hours[uid] += record["ban_hours"]
        
        ranking = []
        for uid, count in ban_count.items():
            user_name = next((r["user_name"] for r in self.ban_history if r["user_uid"] == uid), "未知用户")
            ranking.append({
                "user_uid": uid,
                "user_name": user_name,
                "ban_count": count,
                "total_hours": total_hours[uid]
            })
        
        ranking.sort(key=lambda x: x["ban_count"], reverse=True)
        return ranking[:limit]
    
    def get_data_hash(self):
        import hashlib
        data_str = json.dumps({
            'banned_count': len(self.banned_users),
            'history_count': len(self.ban_history),
            'last_update': self.last_update
        }, sort_keys=True)
        return hashlib.md5(data_str.encode()).hexdigest()

class SpamDetector:
    def __init__(self, config):
        self.config = config
        self.time_window = config.get("刷屏检测时间窗口", 10)
        self.keyword_patterns = [re.compile(kw) for kw in config.get("关键词列表", [])]
        self.user_messages = defaultdict(lambda: deque(maxlen=10))
        self.warnings = defaultdict(int)
    
    def check_spam(self, user_id, message):
        if not any(pattern.search(message) for pattern in self.keyword_patterns):
            return False
        
        current_time = time.time()
        user_queue = self.user_messages[user_id]
        
        while user_queue and current_time - user_queue[0] > self.time_window:
            user_queue.popleft()
        
        user_queue.append(current_time)
        
        max_messages = self.config.get("关键词最大消息数", 3) - 1
        if len(user_queue) > max_messages:
            self.warnings[user_id] += 1
            return True
        
        return False
    
    def get_warning_count(self, user_id):
        return self.warnings.get(user_id, 0)
    
    def cleanup(self):
        current_time = time.time()
        for user_id in list(self.user_messages.keys()):
            queue = self.user_messages[user_id]
            while queue and current_time - queue[0] > self.time_window:
                queue.popleft()
            if not queue:
                del self.user_messages[user_id]

class WebInterface:
    def __init__(self, port=5000):
        self.port = port
        self.app = Flask(__name__)
        self.ban_manager = None
        self.setup_routes()
    
    def setup_routes(self):
        @self.app.route('/')
        def index():
            return '''
            <!DOCTYPE html>
            <html>
            <head>
                <title>直播间管理</title>
                <meta charset="utf-8">
                <style>
                    * {
                        margin: 0;
                        padding: 0;
                        box-sizing: border-box;
                    }
                    
                    body {
                        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                        background-color: #f5f5f5;
                        color: #333;
                        line-height: 1.6;
                    }
                    
                    .container {
                        max-width: 1200px;
                        margin: 0 auto;
                        background: white;
                        min-height: 100vh;
                        box-shadow: 0 0 20px rgba(0,0,0,0.1);
                    }
                    
                    .header {
                        background: #2c3e50;
                        color: white;
                        padding: 2rem;
                        border-bottom: 1px solid #34495e;
                    }
                    
                    .header h1 {
                        font-size: 1.8rem;
                        font-weight: 300;
                        margin-bottom: 0.5rem;
                    }
                    
                    .header p {
                        color: #bdc3c7;
                        font-size: 0.9rem;
                    }
                    
                    .status-bar {
                        background: #ecf0f1;
                        padding: 1rem 2rem;
                        border-bottom: 1px solid #bdc3c7;
                        display: flex;
                        justify-content: space-between;
                        align-items: center;
                        font-size: 0.9rem;
                        color: #7f8c8d;
                    }
                    
                    .nav {
                        display: flex;
                        background: #34495e;
                        border-bottom: 1px solid #2c3e50;
                    }
                    
                    .nav-btn {
                        padding: 1rem 2rem;
                        background: none;
                        border: none;
                        color: #ecf0f1;
                        cursor: pointer;
                        transition: background 0.2s;
                        font-size: 0.9rem;
                    }
                    
                    .nav-btn:hover {
                        background: #3d566e;
                    }
                    
                    .nav-btn.active {
                        background: #3498db;
                        color: white;
                    }
                    
                    .content {
                        padding: 2rem;
                        min-height: 400px;
                    }
                    
                    .send-form {
                        background: #f8f9fa;
                        padding: 1.5rem;
                        border-radius: 4px;
                        margin-bottom: 2rem;
                        border-left: 4px solid #3498db;
                    }
                    
                    .send-form input {
                        padding: 0.75rem;
                        border: 1px solid #ddd;
                        border-radius: 4px;
                        width: 300px;
                        margin-right: 0.5rem;
                        font-size: 0.9rem;
                    }
                    
                    .send-form button {
                        padding: 0.75rem 1.5rem;
                        background: #3498db;
                        color: white;
                        border: none;
                        border-radius: 4px;
                        cursor: pointer;
                        font-size: 0.9rem;
                        transition: background 0.2s;
                    }
                    
                    .send-form button:hover {
                        background: #2980b9;
                    }
                    
                    table {
                        width: 100%;
                        border-collapse: collapse;
                        margin: 1rem 0;
                        background: white;
                        border: 1px solid #ddd;
                    }
                    
                    th {
                        background: #f8f9fa;
                        padding: 1rem;
                        text-align: left;
                        font-weight: 600;
                        border-bottom: 2px solid #ddd;
                        color: #2c3e50;
                    }
                    
                    td {
                        padding: 1rem;
                        border-bottom: 1px solid #eee;
                    }
                    
                    tr:hover {
                        background: #f8f9fa;
                    }
                    
                    .stats {
                        display: grid;
                        grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                        gap: 1rem;
                        margin: 1rem 0;
                    }
                    
                    .stat-card {
                        background: white;
                        padding: 1.5rem;
                        border: 1px solid #ddd;
                        border-radius: 4px;
                        text-align: center;
                    }
                    
                    .stat-number {
                        font-size: 2rem;
                        font-weight: 300;
                        color: #2c3e50;
                        margin-bottom: 0.5rem;
                    }
                    
                    .stat-label {
                        color: #7f8c8d;
                        font-size: 0.9rem;
                    }
                    
                    .rank-1 {
                        background: #f8f9fa;
                        font-weight: 600;
                    }
                    
                    .rank-2 {
                        background: #fafafa;
                    }
                    
                    .rank-3 {
                        background: #fcfcfc;
                    }
                    
                    .rank-badge {
                        display: inline-block;
                        width: 24px;
                        height: 24px;
                        background: #95a5a6;
                        color: white;
                        border-radius: 50%;
                        text-align: center;
                        line-height: 24px;
                        font-size: 0.8rem;
                        margin-right: 0.5rem;
                    }
                    
                    .rank-1 .rank-badge {
                        background: #7f8c8d;
                    }
                    
                    .update-info {
                        background: #ecf0f1;
                        padding: 0.75rem;
                        border-radius: 4px;
                        margin: 1rem 0;
                        font-size: 0.9rem;
                        color: #7f8c8d;
                    }
                    
                    .empty-state {
                        text-align: center;
                        padding: 3rem;
                        color: #7f8c8d;
                    }
                    
                    .empty-state .icon {
                        font-size: 3rem;
                        margin-bottom: 1rem;
                        opacity: 0.5;
                    }
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h1>直播间管理</h1>
                    </div>
                    
                    <div class="status-bar">
                        <div id="lastUpdate">最后更新: <span id="updateTime">加载中...</span></div>
                    </div>
                    
                    <div class="nav">
                        <button class="nav-btn active" onclick="showPage('banned')">当前禁言</button>
                        <button class="nav-btn" onclick="showPage('history')">封禁记录</button>
                        <button class="nav-btn" onclick="showPage('ranking')">封禁排行</button>
                    </div>
                    
                    <div class="content">
                        <div class="send-form">
                            <form action="/send" method="post" onsubmit="return sendMessage(this)">
                                <input type="text" name="message" placeholder="输入弹幕内容" required>
                                <button type="submit">发送弹幕</button>
                            </form>
                        </div>
                        <div id="contentArea">
                            <div class="empty-state">
                                <div class="icon">⚙️</div>
                                <h3>系统就绪</h3>
                                <p>请选择上方菜单开始使用</p>
                            </div>
                        </div>
                    </div>
                </div>

                <script>
                    let currentPage = 'banned';
                    let lastDataHash = '';
                    
                    function showPage(page) {
                        currentPage = page;
                        document.querySelectorAll('.nav-btn').forEach(btn => {
                            btn.classList.remove('active');
                        });
                        event.target.classList.add('active');
                        loadPageData(page);
                    }
                    
                    function loadPageData(page) {
                        fetch('/api/' + page)
                            .then(response => response.json())
                            .then(data => {
                                document.getElementById('contentArea').innerHTML = data.html;
                                document.getElementById('updateTime').textContent = data.timestamp;
                                lastDataHash = data.data_hash;
                            })
                            .catch(error => {
                                document.getElementById('contentArea').innerHTML = '<div class="empty-state"><div class="icon">❌</div><h3>加载失败</h3><p>' + error + '</p></div>';
                            });
                    }
                    
                    function checkForUpdates() {
                        if (!currentPage) return;
                        
                        fetch('/api/check_update?page=' + currentPage + '&hash=' + lastDataHash)
                            .then(response => response.json())
                            .then(data => {
                                if (data.updated) {
                                    loadPageData(currentPage);
                                }
                                document.getElementById('updateTime').textContent = data.timestamp;
                            });
                    }
                    
                    function sendMessage(form) {
                        const message = form.message.value.trim();
                        if (!message) return false;
                        
                        fetch('/send', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/x-www-form-urlencoded',
                            },
                            body: 'message=' + encodeURIComponent(message)
                        })
                        .then(response => response.text())
                        .then(result => {
                            alert(result);
                            form.reset();
                        })
                        .catch(error => {
                            alert('发送失败: ' + error);
                        });
                        
                        return false;
                    }
                    
                    setInterval(checkForUpdates, 2000);
                    
                    window.onload = function() {
                        showPage('banned');
                    };
                </script>
            </body>
            </html>
            '''
        
        @self.app.route('/api/banned')
        def api_banned():
            if not self.ban_manager:
                return jsonify({'html': '系统未就绪', 'timestamp': get_timestamp(), 'data_hash': ''})
            
            try:
                current_time = datetime.now()
                banned_count = len(self.ban_manager.banned_users)
                
                html = [f'''
                <div class="stats">
                    <div class="stat-card">
                        <div class="stat-number">{banned_count}</div>
                        <div class="stat-label">当前禁言用户</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">{len(self.ban_manager.ban_history)}</div>
                        <div class="stat-label">总封禁记录</div>
                    </div>
                </div>
                <h3>当前禁言用户</h3>
                ''']
                
                if banned_count > 0:
                    html.append('<table>')
                    html.append('''
                    <tr>
                        <th>用户ID</th>
                        <th>用户名</th>
                        <th>禁言时间</th>
                        <th>剩余时间</th>
                    </tr>
                    ''')
                    
                    for uid, (name, ban_time) in self.ban_manager.banned_users.items():
                        ban_hours = self.ban_manager.config.get("禁言时长", 2)
                        remaining = timedelta(hours=ban_hours) - (current_time - ban_time)
                        remaining_str = str(remaining).split('.')[0] if remaining.total_seconds() > 0 else "已解禁"
                        
                        html.append(f'''
                        <tr>
                            <td><code>{uid}</code></td>
                            <td>{name}</td>
                            <td>{ban_time.strftime("%m-%d %H:%M")}</td>
                            <td>{remaining_str}</td>
                        </tr>
                        ''')
                    
                    html.append('</table>')
                else:
                    html.append('''
                    <div class="empty-state">
                        <div class="icon">✅</div>
                        <h3>当前没有禁言用户</h3>
                        <p>直播间秩序良好</p>
                    </div>
                    ''')
                
                return jsonify({
                    'html': ''.join(html),
                    'timestamp': get_timestamp(),
                    'data_hash': self.ban_manager.get_data_hash()
                })
                
            except Exception as e:
                return jsonify({'html': f'<div class="empty-state"><div class="icon">❌</div><h3>错误</h3><p>{e}</p></div>', 'timestamp': get_timestamp(), 'data_hash': ''})
        
        @self.app.route('/api/history')
        def api_history():
            if not self.ban_manager:
                return jsonify({'html': '系统未就绪', 'timestamp': get_timestamp(), 'data_hash': ''})
            
            try:
                history = self.ban_manager.ban_history[-50:][::-1]
                
                html = [f'''
                <div class="stats">
                    <div class="stat-card">
                        <div class="stat-number">{len(history)}</div>
                        <div class="stat-label">最近记录</div>
                    </div>
                </div>
                <h3>封禁记录</h3>
                ''']
                
                if history:
                    html.append('<table>')
                    html.append('''
                    <tr>
                        <th>用户名</th>
                        <th>禁言时间</th>
                        <th>解禁时间</th>
                        <th>时长</th>
                        <th>状态</th>
                    </tr>
                    ''')
                    
                    for record in history:
                        ban_time = record["ban_time"][:16].replace('T', ' ')
                        unban_time = record["unban_time"][:16].replace('T', ' ')
                        actual_unban = record.get("actual_unban_time", "")
                        if actual_unban:
                            actual_unban = actual_unban[:16].replace('T', ' ')
                        
                        status = "已解禁" if actual_unban else "禁言中"
                        display_unban = actual_unban if actual_unban else unban_time
                        
                        html.append(f'''
                        <tr>
                            <td>{record["user_name"]}</td>
                            <td>{ban_time}</td>
                            <td>{display_unban}</td>
                            <td>{record["ban_hours"]}小时</td>
                            <td>{status}</td>
                        </tr>
                        ''')
                    
                    html.append('</table>')
                else:
                    html.append('''
                    <div class="empty-state">
                        <div class="icon">📝</div>
                        <h3>暂无封禁记录</h3>
                        <p>还没有用户被禁言过</p>
                    </div>
                    ''')
                
                return jsonify({
                    'html': ''.join(html),
                    'timestamp': get_timestamp(),
                    'data_hash': self.ban_manager.get_data_hash()
                })
                
            except Exception as e:
                return jsonify({'html': f'<div class="empty-state"><div class="icon">❌</div><h3>错误</h3><p>{e}</p></div>', 'timestamp': get_timestamp(), 'data_hash': ''})
        
        @self.app.route('/api/ranking')
        def api_ranking():
            if not self.ban_manager:
                return jsonify({'html': '系统未就绪', 'timestamp': get_timestamp(), 'data_hash': ''})
            
            try:
                ranking = self.ban_manager.get_ranking(20)
                
                html = [f'''
                <div class="stats">
                    <div class="stat-card">
                        <div class="stat-number">{len(ranking)}</div>
                        <div class="stat-label">上榜用户</div>
                    </div>
                </div>
                <h3>封禁排行榜</h3>
                ''']
                
                if ranking:
                    html.append('<table>')
                    html.append('''
                    <tr>
                        <th>排名</th>
                        <th>用户名</th>
                        <th>封禁次数</th>
                        <th>总禁言时长</th>
                    </tr>
                    ''')
                    
                    for i, user in enumerate(ranking, 1):
                        row_class = f'rank-{i}' if i <= 3 else ''
                        
                        html.append(f'''
                        <tr class="{row_class}">
                            <td><span>{i}</span></td>
                            <td>{user["user_name"]}</td>
                            <td>{user["ban_count"]}次</td>
                            <td>{user["total_hours"]}小时</td>
                        </tr>
                        ''')
                    
                    html.append('</table>')
                else:
                    html.append('''
                    <div class="empty-state">
                        <div class="icon">📊</div>
                        <h3>暂无排行榜数据</h3>
                        <p>还没有用户被禁言过</p>
                    </div>
                    ''')
                
                return jsonify({
                    'html': ''.join(html),
                    'timestamp': get_timestamp(),
                    'data_hash': self.ban_manager.get_data_hash()
                })
                
            except Exception as e:
                return jsonify({'html': f'<div class="empty-state"><div class="icon">❌</div><h3>错误</h3><p>{e}</p></div>', 'timestamp': get_timestamp(), 'data_hash': ''})
        
        @self.app.route('/api/check_update')
        def api_check_update():
            page = request.args.get('page', 'banned')
            client_hash = request.args.get('hash', '')
            
            if not self.ban_manager:
                return jsonify({'updated': False, 'timestamp': get_timestamp()})
            
            current_hash = self.ban_manager.get_data_hash()
            updated = current_hash != client_hash
            
            return jsonify({
                'updated': updated,
                'timestamp': get_timestamp(),
                'data_hash': current_hash
            })
        
        @self.app.route('/send', methods=['POST'])
        def send_danmaku():
            message = request.form.get('message', '').strip()
            if not message:
                return "消息不能为空"
            
            global danmaku_room
            if not danmaku_room:
                return "直播间未连接"
            
            try:
                async def send():
                    danmaku_obj = Danmaku(message)
                    await danmaku_room.send_danmaku(danmaku_obj)
                    return True
                
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(send())
                loop.close()
                
                return "弹幕发送成功" if result else "发送失败"
                
            except Exception as e:
                return f"发送失败: {e}"
    
    def run(self):
        self.app.run(host='0.0.0.0', port=self.port, debug=False, threaded=True)
    
    def start(self):
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()

def get_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

async def main():
    global restart_requested, danmaku_room
    
    # 启动Web界面
    web = WebInterface(5000)
    web.start()
    print(f"Web界面已启动: http://localhost:5000")
    
    while True:
        try:
            if restart_requested:
                await asyncio.sleep(5)
                break
            
            # 加载配置
            config = yaml.safe_load(open("config.yml", 'r', encoding='utf-8'))
            
            # 创建凭证
            cred = Credential(
                sessdata=config["sessdata"],
                bili_jct=config["bili_jct"],
                buvid3=config["buvid3"],
                dedeuserid=config["dedeuserid"]
            )
            
            # 初始化组件
            room = live.LiveRoom(room_display_id=config["room"], credential=cred)
            danmaku_room = room
            
            ban_manager = BanManager(room, config)
            spam_detector = SpamDetector(config)
            web.ban_manager = ban_manager
            
            # 连接弹幕
            danmaku = live.LiveDanmaku(room_display_id=config["room"], credential=cred)
            
            @danmaku.on('DANMU_MSG')
            async def on_danmaku(event):
                user_uid = event["data"]["info"][2][0]
                user_name = event["data"]["info"][2][1]
                message = event["data"]["info"][1]
                
                print(f"{user_name}: {message}")
                
                # 检测刷屏
                if spam_detector.check_spam(user_uid, message):
                    warning_count = spam_detector.get_warning_count(user_uid)
                    if warning_count >= 2:
                        await ban_manager.ban_user(user_uid, user_name)
            
            # 启动维护任务
            async def maintenance():
                while True:
                    await asyncio.sleep(300)
                    await ban_manager.check_unbans()
                    spam_detector.cleanup()
            
            maintenance_task = asyncio.create_task(maintenance())
            
            # 主循环
            while not restart_requested:
                await danmaku.connect()
                await asyncio.sleep(1)
                
        except Exception as e:
            print(f"错误: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
    
    while True:
        asyncio.run(main())
        print("重启中...")
        time.sleep(2)

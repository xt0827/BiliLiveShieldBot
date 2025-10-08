import asyncio, requests
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
from flask import Flask, request, render_template_string
import threading
from queue import Queue

restart_requested = False
danmaku_room = None
danmaku_messages = Queue(maxsize=1000)

class PersistentUnbanManager:
    def __init__(self, room, config, data_file="banned_users.pkl", ban_history_file="ban_history.json"):
        self.room = room
        self.config = config
        self.data_file = data_file
        self.ban_history_file = ban_history_file
        self.banned_users = self.load_banned_users()
        self.ban_history = self.load_ban_history()

    def load_banned_users(self):
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'rb') as f:
                    data = pickle.load(f)
                    for uid, (name, ban_time_str) in data.items():
                        data[uid] = (name, datetime.fromisoformat(ban_time_str))
                    return data
        except Exception as e:
            print(f"[错误] 加载禁言列表失败: {e}")
        return {}

    def load_ban_history(self):
        try:
            if os.path.exists(self.ban_history_file):
                with open(self.ban_history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"[错误] 加载封禁历史失败: {e}")
        return []

    def save_banned_users(self):
        try:
            save_data = {}
            for uid, (name, ban_time) in self.banned_users.items():
                save_data[uid] = (name, ban_time.isoformat())

            with open(self.data_file, 'wb') as f:
                pickle.dump(save_data, f)
        except Exception as e:
            print(f"[错误] 保存禁言列表失败: {e}")

    def save_ban_history(self):
        try:
            with open(self.ban_history_file, 'w', encoding='utf-8') as f:
                json.dump(self.ban_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[错误] 保存封禁历史失败: {e}")

    async def ban_user_with_auto_unban(self, user_uid, user_name):
        ban_hours = self.config.get("禁言时长", 2)
        result = await self.room.ban_user(uid=user_uid, hour=ban_hours)
        ban_time = datetime.now()
        self.banned_users[user_uid] = (user_name, ban_time)
        self.save_banned_users()

        ban_record = {
            "user_uid": user_uid,
            "user_name": user_name,
            "ban_time": ban_time.isoformat(),
            "ban_hours": ban_hours,
            "unban_time": (ban_time + timedelta(hours=ban_hours)).isoformat(),
            "reason": "关键词刷屏"
        }
        self.ban_history.append(ban_record)
        self.save_ban_history()

        print(f"[禁言] 已禁言用户: {user_name}，将在{ban_hours}小时后自动解禁")
        return result

    async def check_and_unban(self):
        current_time = datetime.now()
        users_to_unban = []
        ban_hours = self.config.get("禁言时长", 2)

        for user_uid, (user_name, ban_time) in list(self.banned_users.items()):
            if current_time - ban_time >= timedelta(hours=ban_hours):
                users_to_unban.append((user_uid, user_name))

        for user_uid, user_name in users_to_unban:
            try:
                await self.room.unban_user(uid=user_uid)
                print(f"[解禁] 已自动解禁用户: {user_name} (UID: {user_uid})")
                del self.banned_users[user_uid]

                for record in self.ban_history:
                    if record["user_uid"] == user_uid and "actual_unban_time" not in record:
                        record["actual_unban_time"] = current_time.isoformat()
                        record["status"] = "已解禁"
                        break
                self.save_ban_history()

            except Exception as e:
                print(f"[解禁错误] 解禁用户 {user_name} 失败: {e}")

        if users_to_unban:
            self.save_banned_users()

    async def sync_banned_status(self):
        current_time = datetime.now()
        users_to_remove = []
        ban_hours = self.config.get("禁言时长", 2)

        for user_uid, (user_name, ban_time) in list(self.banned_users.items()):
            if current_time - ban_time >= timedelta(hours=ban_hours):
                users_to_remove.append((user_uid, user_name))

        for user_uid, user_name in users_to_remove:
            try:
                await self.room.unban_user(uid=user_uid)
                print(f"[解禁] 用户 {user_name} 禁言时间已到，已解禁")
                del self.banned_users[user_uid]

                for record in self.ban_history:
                    if record["user_uid"] == user_uid and "actual_unban_time" not in record:
                        record["actual_unban_time"] = current_time.isoformat()
                        record["status"] = "已解禁"
                        break
                self.save_ban_history()

            except Exception as e:
                print(f"[解禁错误] 用户 {user_name} 解禁失败: {e}")

        if users_to_remove:
            self.save_banned_users()

    def get_ban_history(self, limit=100):
        return self.ban_history[-limit:][::-1]

    def get_ban_ranking(self, limit=20):
        ban_count = defaultdict(int)
        total_ban_hours = defaultdict(int)
        last_ban_time = {}

        for record in self.ban_history:
            user_uid = record["user_uid"]
            user_name = record["user_name"]
            ban_hours = record["ban_hours"]

            ban_count[user_uid] += 1
            total_ban_hours[user_uid] += ban_hours
            last_ban_time[user_uid] = record["ban_time"]

        ranking = []
        for user_uid, count in ban_count.items():
            ranking.append({
                "user_uid": user_uid,
                "user_name": next((r["user_name"] for r in self.ban_history if r["user_uid"] == user_uid), "未知用户"),
                "ban_count": count,
                "total_hours": total_ban_hours[user_uid],
                "last_ban_time": last_ban_time[user_uid]
            })

        ranking.sort(key=lambda x: x["ban_count"], reverse=True)
        return ranking[:limit]

class SimpleWebConfig:
    def __init__(self, config_path, port=5000):
        self.config_path = Path(config_path)
        self.port = port
        self.app = Flask(__name__)
        self.setup_routes()

    def setup_routes(self):
        # 提供主页面
        @self.app.route('/')
        def index():
            return """
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

        .heimu, .heimu a, a .heimu, .heimu a.new {
            background-color: #252525;
            color: #252525;
            text-shadow: none;
        }

        .heimu:hover, .heimu:active,
        .heimu:hover .heimu, .heimu:active .heimu {
            color: white !important;
        }
        .heimu:hover a, a:hover .heimu,
        .heimu:active a, a:active .heimu {
            color: lightblue !important;
        }
        .heimu:hover .new, .heimu .new:hover, .new:hover .heimu,
        .heimu:active .new, .heimu .new:active, .new:active .heimu {
            color: #BA0000 !important;
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
            <div id="lastUpdate">最后更新: <span id="updateTime">加载中...</span></div><span class="heimu" title="你知道的太多了">其实我是人类</span>
        </div>

        <div class="nav">
            <button class="nav-btn active" onclick="showPage('banned')">当前禁言</button>
            <button class="nav-btn" onclick="showPage('history')">封禁记录</button>
            <button class="nav-btn" onclick="showPage('ranking')">封禁排行</button>
        </div>

        <div class="content">
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

        setInterval(checkForUpdates, 2000);

        window.onload = function() {
            showPage('banned');
        };
    </script>
</body>
</html>
            """

        # API 路由 - 当前禁言用户
        @self.app.route('/api/banned')
        def api_banned():
            try:
                banned_data = {}
                if os.path.exists("banned_users.pkl"):
                    with open("banned_users.pkl", 'rb') as f:
                        banned_data = pickle.load(f)

                current_time = datetime.now()
                html = ""

                if banned_data:
                    # 统计信息
                    total_banned = len(banned_data)
                    html += f"""
                    <div class="stats">
                        <div class="stat-card">
                            <div class="stat-number">{total_banned}</div>
                            <div class="stat-label">当前禁言人数</div>
                        </div>
                    </div>
                    """

                    html += """
                    <table>
                        <thead>
                            <tr>
                                <th>用户ID</th>
                                <th>用户名</th>
                                <th>禁言时间</th>
                                <th>剩余时间</th>
                            </tr>
                        </thead>
                        <tbody>
                    """

                    for uid, (name, ban_time_str) in banned_data.items():
                        ban_time = datetime.fromisoformat(ban_time_str)
                        ban_hours = 2
                        remaining = timedelta(hours=ban_hours) - (current_time - ban_time)

                        if remaining.total_seconds() > 0:
                            hours = int(remaining.total_seconds() // 3600)
                            minutes = int((remaining.total_seconds() % 3600) // 60)
                            remaining_str = f"{hours}小时{minutes}分钟"
                        else:
                            remaining_str = "已解禁"

                        html += f"""
                            <tr>
                                <td>{uid}</td>
                                <td>{name}</td>
                                <td>{ban_time.strftime('%Y-%m-%d %H:%M:%S')}</td>
                                <td>{remaining_str}</td>
                            </tr>
                        """

                    html += """
                        </tbody>
                    </table>
                    """
                else:
                    html = """
                    <div class="empty-state">
                        <div class="icon">✅</div>
                        <h3>暂无禁言用户</h3>
                        <p>当前没有用户被禁言</p>
                    </div>
                    """

                return {
                    "html": html,
                    "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "data_hash": str(hash(str(banned_data)))
                }

            except Exception as e:
                return {
                    "html": f'<div class="empty-state"><div class="icon">❌</div><h3>加载失败</h3><p>{e}</p></div>',
                    "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "data_hash": "error"
                }

        # API 路由 - 封禁记录
        @self.app.route('/api/history')
        def api_history():
            try:
                history_data = []
                if os.path.exists("ban_history.json"):
                    with open("ban_history.json", 'r', encoding='utf-8') as f:
                        history_data = json.load(f)

                html = ""

                if history_data:
                    # 统计信息
                    total_records = len(history_data)
                    active_bans = len([r for r in history_data if not r.get("actual_unban_time")])

                    html += f"""
                    <div class="stats">
                        <div class="stat-card">
                            <div class="stat-number">{total_records}</div>
                            <div class="stat-label">总封禁记录</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-number">{active_bans}</div>
                            <div class="stat-label">当前禁言中</div>
                        </div>
                    </div>
                    """

                    html += """
                    <table>
                        <thead>
                            <tr>
                                <th>用户ID</th>
                                <th>用户名</th>
                                <th>禁言时间</th>
                                <th>解禁时间</th>
                                <th>禁言时长</th>
                                <th>状态</th>
                                <th>原因</th>
                            </tr>
                        </thead>
                        <tbody>
                    """

                    for record in history_data[-100:][::-1]:  # 显示最近100条
                        user_uid = record.get("user_uid", "")
                        user_name = record.get("user_name", "")
                        ban_time = record.get("ban_time", "")[:19]
                        unban_time = record.get("unban_time", "")[:19]
                        actual_unban_time = record.get("actual_unban_time", "")
                        if actual_unban_time:
                            actual_unban_time = actual_unban_time[:19]
                        ban_hours = record.get("ban_hours", 2)
                        reason = record.get("reason", "关键词刷屏")

                        status = "已解禁" if record.get("actual_unban_time") else "禁言中"
                        status_class = "status-unbanned" if status == "已解禁" else "status-banned"
                        display_unban_time = actual_unban_time if actual_unban_time else unban_time

                        html += f"""
                            <tr>
                                <td>{user_uid}</td>
                                <td>{user_name}</td>
                                <td>{ban_time}</td>
                                <td>{display_unban_time}</td>
                                <td>{ban_hours}小时</td>
                                <td><span class="{status_class}">{status}</span></td>
                                <td>{reason}</td>
                            </tr>
                        """

                    html += """
                        </tbody>
                    </table>
                    """
                else:
                    html = """
                    <div class="empty-state">
                        <div class="icon">📝</div>
                        <h3>暂无封禁记录</h3>
                        <p>还没有封禁记录</p>
                    </div>
                    """

                return {
                    "html": html,
                    "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "data_hash": str(hash(str(history_data)))
                }

            except Exception as e:
                return {
                    "html": f'<div class="empty-state"><div class="icon">❌</div><h3>加载失败</h3><p>{e}</p></div>',
                    "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "data_hash": "error"
                }

        # API 路由 - 封禁排行榜
        @self.app.route('/api/ranking')
        def api_ranking():
            try:
                history_data = []
                if os.path.exists("ban_history.json"):
                    with open("ban_history.json", 'r', encoding='utf-8') as f:
                        history_data = json.load(f)

                html = ""

                if history_data:
                    ban_count = defaultdict(int)
                    total_ban_hours = defaultdict(int)
                    last_ban_time = {}

                    for record in history_data:
                        user_uid = record["user_uid"]
                        user_name = record["user_name"]
                        ban_hours = record["ban_hours"]

                        ban_count[user_uid] += 1
                        total_ban_hours[user_uid] += ban_hours
                        last_ban_time[user_uid] = record["ban_time"]

                    ranking = []
                    for user_uid, count in ban_count.items():
                        ranking.append({
                            "user_uid": user_uid,
                            "user_name": next((r["user_name"] for r in history_data if r["user_uid"] == user_uid), "未知用户"),
                            "ban_count": count,
                            "total_hours": total_ban_hours[user_uid],
                            "last_ban_time": last_ban_time[user_uid][:19]
                        })

                    ranking.sort(key=lambda x: x["ban_count"], reverse=True)

                    # 统计信息
                    total_users = len(ranking)
                    total_bans = sum(user["ban_count"] for user in ranking)

                    html += f"""
                    <div class="stats">
                        <div class="stat-card">
                            <div class="stat-number">{total_users}</div>
                            <div class="stat-label">被封禁用户数</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-number">{total_bans}</div>
                            <div class="stat-label">总封禁次数</div>
                        </div>
                    </div>
                    """

                    html += """
                    <table class="ranking-table">
                        <thead>
                            <tr>
                                <th>排名</th>
                                <th>用户ID</th>
                                <th>用户名</th>
                                <th>封禁次数</th>
                                <th>总禁言时长(小时)</th>
                                <th>最后封禁时间</th>
                            </tr>
                        </thead>
                        <tbody>
                    """

                    for i, user in enumerate(ranking[:20], 1):
                        rank_class = ""
                        if i == 1:
                            rank_class = "rank-1"
                        elif i == 2:
                            rank_class = "rank-2"
                        elif i == 3:
                            rank_class = "rank-3"

                        html += f"""
                            <tr class="{rank_class}">
                                <td><strong>{i}</strong></td>
                                <td>{user['user_uid']}</td>
                                <td>{user['user_name']}</td>
                                <td>{user['ban_count']}</td>
                                <td>{user['total_hours']}</td>
                                <td>{user['last_ban_time']}</td>
                            </tr>
                        """

                    html += """
                        </tbody>
                    </table>
                    """
                else:
                    html = """
                    <div class="empty-state">
                        <div class="icon">🏆</div>
                        <h3>暂无排行榜数据</h3>
                        <p>还没有封禁记录</p>
                    </div>
                    """

                return {
                    "html": html,
                    "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "data_hash": str(hash(str(history_data)))
                }

            except Exception as e:
                return {
                    "html": f'<div class="empty-state"><div class="icon">❌</div><h3>加载失败</h3><p>{e}</p></div>',
                    "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "data_hash": "error"
                }

        # API 路由 - 检查更新
        @self.app.route('/api/check_update')
        def api_check_update():
            page = request.args.get('page', 'banned')
            current_hash = request.args.get('hash', '')

            try:
                if page == 'banned':
                    banned_data = {}
                    if os.path.exists("banned_users.pkl"):
                        with open("banned_users.pkl", 'rb') as f:
                            banned_data = pickle.load(f)
                    new_hash = str(hash(str(banned_data)))

                elif page == 'history':
                    history_data = []
                    if os.path.exists("ban_history.json"):
                        with open("ban_history.json", 'r', encoding='utf-8') as f:
                            history_data = json.load(f)
                    new_hash = str(hash(str(history_data)))

                elif page == 'ranking':
                    history_data = []
                    if os.path.exists("ban_history.json"):
                        with open("ban_history.json", 'r', encoding='utf-8') as f:
                            history_data = json.load(f)
                    new_hash = str(hash(str(history_data)))

                return {
                    "updated": new_hash != current_hash,
                    "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }

            except Exception as e:
                return {
                    "updated": False,
                    "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }

    def run(self):
        print(f"直播间管理: http://localhost:{self.port}")
        import logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
        self.app.run(host='0.0.0.0', port=self.port, debug=False, use_reloader=False)

    def start_in_background(self):
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        return thread

class ConsoleToLogHandler:
    def __init__(self, logger, log_level=logging.INFO):
        self.logger = logger
        self.log_level = log_level
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr

    def write(self, message):
        if message.strip():
            self.logger.log(self.log_level, message.strip())

    def flush(self):
        pass

def setup_universal_logging(log_dir="logs"):
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("B站直播监控")
    logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler(
        f"{log_dir}/{datetime.now().strftime('%Y-%m-%d')}.log",
        encoding='utf-8'
    )
    file_handler.setFormatter(
        logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    )
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console_handler)
    sys.stdout = ConsoleToLogHandler(logger, logging.INFO)
    sys.stderr = ConsoleToLogHandler(logger, logging.ERROR)
    return logger

class SpamDetector:
    def __init__(self, config):
        self.config = config
        self.time_window = config.get("刷屏检测时间窗口", 10)
        self.max_messages = config.get("刷屏检测最大消息数", 5)
        self.user_messages = defaultdict(deque)
        self.spam_warnings = defaultdict(int)
        self.keyword_messages = defaultdict(deque)
        self.keyword_warnings = defaultdict(int)
        self.keyword_patterns = self._compile_keyword_patterns()

    def _compile_keyword_patterns(self):
        keywords = self.config.get("关键词列表", ["喝", "思考", "惊讶", "疑惑"])
        patterns = []
        for keyword in keywords:
            try:
                pattern = re.compile(keyword)
                patterns.append(pattern)
            except re.error:
                pattern = re.compile(re.escape(keyword))
                patterns.append(pattern)
        return patterns

    def check_keyword_spam(self, user_id: str, message: str) -> bool:
        matched = False
        for pattern in self.keyword_patterns:
            if pattern.search(message):
                matched = True
                break

        if not matched:
            return False

        current_time = time.time()
        user_queue = self.keyword_messages[user_id]
        while user_queue and current_time - user_queue[0] > self.time_window:
            user_queue.popleft()
        user_queue.append(current_time)

        max_keyword_messages = (self.config.get("关键词最大消息数", 3) - 1)
        if len(user_queue) > max_keyword_messages:
            self.keyword_warnings[user_id] += 1
            return True
        return False

    def get_warning_count(self, user_id: str) -> int:
        return self.keyword_warnings.get(user_id, 0)

    def clear_old_entries(self):
        current_time = time.time()

        for user_id, timestamps in self.keyword_messages.items():
            while timestamps and current_time - timestamps[0] > self.time_window:
                timestamps.popleft()

class AnnouncementManager:
    def __init__(self, room, config):
        self.room = room
        self.config = config
        self.last_announcement_time = 0
        self.announcement_interval = config.get("公告发送间隔", 900)

    async def send_ban_announcement(self, user_name, ban_hours):
        announcement = f"用户 {user_name} 因刷屏已被禁言 {ban_hours} 小时，请遵守直播间规则"
        try:
            danmaku_obj = Danmaku(announcement)
            await self.room.send_danmaku(danmaku_obj)
            print(f"[公告] 已发送封禁提醒: {announcement}")
        except Exception as e:
            print(f"[公告错误] 发送封禁提醒失败: {e}")

    async def send_regular_announcement(self):
        current_time = time.time()
        if current_time - self.last_announcement_time >= self.announcement_interval:
            announcement_content = self.config.get("公告内容", "直播间刷屏自动禁言，2小时自动解除")
            try:
                danmaku_obj = Danmaku(announcement_content)
                await self.room.send_danmaku(danmaku_obj)
                self.last_announcement_time = current_time
                print(f"[定时公告] 已发送: {announcement_content}")
            except Exception as e:
                print(f"[定时公告错误] 发送失败: {e}")

def load_config() -> dict:
    config_path = Path("config.yml")
    if not config_path.exists():
        default_config = {
            "debug": False,
            "sessdata": "",
            "bili_jct": "",
            "buvid3": "",
            "dedeuserid": "",
            "ac_time_value": "",
            "room": "",
            "uid": "",
            "刷屏检测时间窗口": 10,
            "刷屏检测最大消息数": 5,
            "关键词最大消息数": 3,
            "禁言时长": 2,
            "公告内容": "直播间刷屏自动禁言，2小时自动解除",
            "公告发送间隔": 900,
            "关键词列表": ["喝", "思考", "惊讶", "疑惑"]
        }
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(default_config, f, allow_unicode=True, default_flow_style=False)

    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

async def main():
    global config, restart_requested, danmaku_room

    web_ui = SimpleWebConfig("config.yml", port=5000)
    web_ui.start_in_background()

    while True:
        try:
            if restart_requested:
                print("检测到重启请求，准备重启...")
                restart_requested = False
                await asyncio.sleep(5)
                break

            config = load_config()
            cookies = {
                "buvid3": config["buvid3"],
                "SESSDATA": config["sessdata"],
                "bili_jct": config["bili_jct"],
                "DedeUserID": config["dedeuserid"]
            }

            cred = Credential(
                sessdata=config["sessdata"],
                bili_jct=config["bili_jct"],
                buvid3=config["buvid3"],
                dedeuserid=config["dedeuserid"],
                ac_time_value=config["ac_time_value"]
            )

            room_id = config["room"]
            spam_detector = SpamDetector(config)

            danmaku = live.LiveDanmaku(
                room_display_id=room_id,
                debug=config["debug"],
                credential=cred
            )
            room = live.LiveRoom(room_display_id=room_id, credential=cred)
            danmaku_room = room

            unban_manager = PersistentUnbanManager(room, config)
            announcement_manager = AnnouncementManager(room, config)

            await unban_manager.sync_banned_status()

            async def handle_spam(user_uid, user_name):
                warning_count = spam_detector.get_warning_count(user_uid)

                if warning_count >= 2:
                    result = await unban_manager.ban_user_with_auto_unban(user_uid, user_name)
                    ban_hours = config.get("禁言时长", 2)
                    await announcement_manager.send_ban_announcement(user_name, ban_hours)
                    print(f"[刷屏处理] 已处理刷屏用户: {user_name}，警告次数: {warning_count}")

            @danmaku.on('DANMU_MSG')
            async def on_danmaku(event):
                user_uid = event["data"]["info"][2][0]
                user_name = event["data"]["info"][0][15]["user"]["base"]["name"]
                user_danmaku = event["data"]["info"][1]

                if spam_detector.check_keyword_spam(user_uid, user_danmaku):
                    await handle_spam(user_uid, user_name)

                print(f"[弹幕] {user_name} (UID: {user_uid})：{user_danmaku}")

                danmaku_data = {
                    'time': datetime.now().strftime('%H:%M:%S'),
                    'user': user_name,
                    'message': user_danmaku
                }

                if danmaku_messages.full():
                    danmaku_messages.get()
                danmaku_messages.put(danmaku_data)

            async def cleanup_spam_records():
                while True:
                    await asyncio.sleep(60)
                    spam_detector.clear_old_entries()

            async def auto_unban_check():
                while True:
                    await asyncio.sleep(300)
                    await unban_manager.check_and_unban()

            async def regular_announcement():
                while True:
                    await asyncio.sleep(60)
                    await announcement_manager.send_regular_announcement()

            cleanup_task = asyncio.create_task(cleanup_spam_records())
            unban_task = asyncio.create_task(auto_unban_check())
            announcement_task = asyncio.create_task(regular_announcement())

            while not restart_requested:
                await danmaku.connect()
                await asyncio.sleep(1)

        except Exception as e:
            print(f"主循环错误: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    logger = setup_universal_logging()
    while True:
        asyncio.run(main())
        print("程序重启中...")
        time.sleep(2)

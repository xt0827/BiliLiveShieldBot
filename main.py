import asyncio, requests
from bilibili_api import live, Credential, sync
from pathlib import Path
import yaml
import os
import sys
import logging
from datetime import datetime, timedelta
import time
import re
import json
from collections import defaultdict, deque


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
        """编译关键词的正则表达式模式"""
        keywords = self.config.get("关键词列表", ["喝", "思考", "惊讶", "疑惑"])
        patterns = []
        for keyword in keywords:
            try:
                pattern = re.compile(keyword)
                patterns.append(pattern)
                print(f"[正则编译] 成功编译正则模式: {keyword}")
            except re.error as e:
                pattern = re.compile(re.escape(keyword))
                patterns.append(pattern)
                print(f"[正则编译] 将关键词作为普通文本处理: {keyword}")
        return patterns

    def check_general_spam(self, user_id: str) -> bool:
        current_time = time.time()
        user_queue = self.user_messages[user_id]
        while user_queue and current_time - user_queue[0] > self.time_window:
            user_queue.popleft()
        user_queue.append(current_time)
        if len(user_queue) > self.max_messages:
            self.spam_warnings[user_id] += 1
            return True
        return False

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
        
        max_keyword_messages = (self.config.get("关键词最大消息数", 3)-1)
        if len(user_queue) > max_keyword_messages:
            self.keyword_warnings[user_id] += 1
            return True
        return False

    def get_warning_count(self, user_id: str) -> int:
        return self.spam_warnings.get(user_id, 0) + self.keyword_warnings.get(user_id, 0)

    def clear_old_entries(self):
        current_time = time.time()
        users_to_remove = []
        
        for user_id, timestamps in self.user_messages.items():
            while timestamps and current_time - timestamps[0] > self.time_window:
                timestamps.popleft()
            if not timestamps:
                users_to_remove.append(user_id)
        
        for user_id in users_to_remove:
            if user_id in self.user_messages:
                del self.user_messages[user_id]
            if user_id in self.spam_warnings:
                del self.spam_warnings[user_id]
        
        users_to_remove = []
        for user_id, timestamps in self.keyword_messages.items():
            while timestamps and current_time - timestamps[0] > self.time_window:
                timestamps.popleft()
            if not timestamps:
                users_to_remove.append(user_id)
        
        for user_id in users_to_remove:
            if user_id in self.keyword_messages:
                del self.keyword_messages[user_id]
            if user_id in self.keyword_warnings:
                del self.keyword_warnings[user_id]

class LiveMessageSender:
    def __init__(self, config, cookies):
        self.config = config
        self.cookies = cookies
        self.room_id = config["room"]
        self.csrf = config["bili_jct"]
        self.last_send_time = 0
    
    async def send_danmaku(self, message):
        try:
            current_time = time.time()
            if current_time - self.last_send_time < 30:
                print(f"[频率限制] 请等待 {30 - int(current_time - self.last_send_time)} 秒后再发送")
                return False
            
            headers = {
                "Referer": f"https://live.bilibili.com/{self.room_id}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Origin": "https://live.bilibili.com",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive"
            }
            
            form_data = {
                "msg": message,
                "roomid": self.room_id,
                "rnd": int(time.time()),
                "fontsize": 25,
                "color": 16777215,
                "mode": 1,
                "bubble": 0,
                "room_type": 0,
                "jumpfrom": 0,
                "reply_mid": 0,
                "reply_attr": 0,
                "reply_uname": "",
                "replay_dmid": "",
                "statistics": '{"appId":1,"version":"1.0.0","platform":3}',
                "csrf": self.csrf,
                "csrf_token": self.csrf
            }
            
            response = requests.post(
                "https://api.live.bilibili.com/msg/send",
                headers=headers,
                cookies=self.cookies,
                data=form_data,
                timeout=10
            )
            
            result = response.json()
            if result.get("code") == 0:
                print(f"[弹幕发送成功] {message}")
                self.last_send_time = current_time
                return True
            else:
                error_msg = result.get('message', '未知错误')
                print(f"[弹幕发送失败] 错误码: {result.get('code')}, 消息: {error_msg}")
                return False
                
        except Exception as e:
            print(f"[弹幕发送异常] {e}")
            return False

class UnbanManager:
    def __init__(self, room, config, message_sender):
        self.room = room
        self.config = config
        self.message_sender = message_sender
        self.banned_users = {}
    
    async def ban_user_with_auto_unban(self, user_uid, user_name):
        ban_hours = self.config.get("禁言时长", 2)
        result = await self.room.ban_user(uid=user_uid, hour=1)
        self.banned_users[user_uid] = (user_name, datetime.now())
        
        print(f"[禁言] 已永久禁言用户: {user_name}，将在{ban_hours}小时后自动解禁")
        return result
    
    async def check_and_unban(self):
        current_time = datetime.now()
        users_to_unban = []
        ban_hours = self.config.get("禁言时长", 2)
        
        for user_uid, (user_name, ban_time) in self.banned_users.items():
            if current_time - ban_time >= timedelta(hours=ban_hours):
                users_to_unban.append((user_uid, user_name))
        
        for user_uid, user_name in users_to_unban:
            try:
                await self.room.unban_user(uid=user_uid)
                print(f"[解禁] 已自动解禁用户: {user_name} (UID: {user_uid})")
                del self.banned_users[user_uid]
            except Exception as e:
                print(f"[解禁错误] 解禁用户 {user_name} 失败: {e}")

def load_config() -> dict:
    config_path = Path("config.yml")
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

async def main():
    global config
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
    up_uid = config["uid"]
    spam_detector = SpamDetector(config)

    message_sender = LiveMessageSender(config, cookies)

    def ban_user(blacklist_uid, blacklist_name):
        headers = {
            "Referer": f"https://live.bilibili.com/{room_id}",
            "User-Agent": "Mozilla/5.0"
        }
        data = {
            "tuid": blacklist_uid,
            "anchor_id": up_uid,
            "spmid": "444.8.0.0",
            "csrf_token": config["bili_jct"],
            "csrf": config["bili_jct"],
            "visit_id": ""
        }
        response = requests.post(
            "https://api.live.bilibili.com/xlive/app-ucenter/v2/xbanned/banned/AddBlack",
            headers=headers,
            cookies=cookies,
            data=data
        )
        print(f'成功拉黑水友：{blacklist_name}，uid：{blacklist_uid}')
        return response.json()

    danmaku = live.LiveDanmaku(
        room_display_id=room_id,
        debug=config["debug"],
        credential=cred
    )
    room = live.LiveRoom(room_display_id=room_id, credential=cred)
    unban_manager = UnbanManager(room, config, message_sender)

    async def handle_spam(user_uid, user_name, is_keyword_spam=False):
        warning_count = spam_detector.get_warning_count(user_uid)
        
        if warning_count == 1:
            spam_type = "关键词" if is_keyword_spam else "普通"
            print(f"[刷屏警告] 用户 {user_name} (UID: {user_uid}) {spam_type}刷屏，已警告1次")
        elif warning_count >= 2:
            spam_action = config.get("刷屏处理方式", "禁言")
            if spam_action == "禁言":
                result = await unban_manager.ban_user_with_auto_unban(user_uid, user_name)
                spam_type = "关键词" if is_keyword_spam else "普通"
                print(f"[刷屏处理] 已处理{spam_type}刷屏用户: {user_name}，警告次数: {warning_count}")
                
                # 发送封禁通知弹幕
                ban_message = f"用户 {user_name} 因刷屏已被禁言"
                await message_sender.send_danmaku(ban_message)
                
            elif spam_action == "拉黑":
                ban_response = ban_user(user_uid, user_name)
                spam_type = "关键词" if is_keyword_spam else "普通"
                print(f"[刷屏处理] 已拉黑{spam_type}刷屏用户: {user_name}，警告次数: {warning_count}")

    @danmaku.on('DANMU_MSG')
    async def on_danmaku(event):
        user_uid = event["data"]["info"][2][0]
        user_name = event["data"]["info"][0][15]["user"]["base"]["name"]
        user_danmaku = event["data"]["info"][1]
        
        keyword_spam_detection = config.get("开启关键词刷屏检测", True)
        if keyword_spam_detection:
            if spam_detector.check_keyword_spam(user_uid, user_danmaku):
                await handle_spam(user_uid, user_name, is_keyword_spam=True)
        
        general_spam_detection = config.get("开启普通刷屏检测", False)
        if general_spam_detection:
            if spam_detector.check_general_spam(user_uid):
                await handle_spam(user_uid, user_name, is_keyword_spam=False)
        
        print(f"[弹幕] {user_name} (UID: {user_uid})：{user_danmaku}")

    async def cleanup_spam_records():
        while True:
            await asyncio.sleep(60)
            spam_detector.clear_old_entries()

    async def auto_unban_check():
        while True:
            await asyncio.sleep(300)
            await unban_manager.check_and_unban()

    async def 定时发送公告():
        announcement_interval = config.get("公告发送间隔", 600)  
        announcement_message = config.get("公告内容", "直播间刷屏自动禁言，2小时自动解除")
        
        while True:
            await asyncio.sleep(announcement_interval)
            try:
                success = await message_sender.send_danmaku(announcement_message)
                if success:
                    print(f"[定时公告] 公告发送: {announcement_message}")
                else:
                    print("[定时公告] 公告发送失败")
            except Exception as e:
                print(f"[定时公告异常] {e}")

    cleanup_task = asyncio.create_task(cleanup_spam_records())
    unban_task = asyncio.create_task(auto_unban_check())
    announcement_task = asyncio.create_task(定时发送公告())

    while True:
        if sync(cred.check_refresh()):
            sync(cred.refresh())
        print("正在连接直播间...")
        await danmaku.connect()
        await asyncio.sleep(1)

if __name__ == "__main__":
    logger = setup_universal_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已停止监听")

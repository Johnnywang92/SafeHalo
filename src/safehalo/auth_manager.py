"""
SafeHalo 用户认证与权限管理模块 (auth_manager.py)
专为边缘路由器与远程访问设计:
1. 零第三方依赖: 使用 Python 原生 hashlib (PBKDF2-HMAC-SHA256) 与 secrets，强抗彩虹表攻击与时序攻击。
2. 双重鉴权支持: 支持 HTTP Cookie (safehalo_session) 与 Authorization Bearer Token (移动端与API)。
3. 基于角色的权限访问控制 (RBAC):
   - admin: 超级管理员 (全权管理成员、更改敏感系统参数、清空日志、触发激振排查)
   - family: 普通家庭成员 (查看实时生理遥测、接收报警通知、查看日志与回放，不可增删成员)
4. 持久化存储: 本地轻量 JSON 数据存储 (data/users.json)，首次运行自动初始化管理员。
"""

import os
import json
import time
import secrets
import hashlib
from typing import Dict, List, Optional, Tuple, Any
from .logger import logger

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
USERS_FILE = os.path.join(DATA_DIR, "users.json")

def hash_password(password: str, salt_hex: Optional[str] = None) -> Tuple[str, str]:
    """
    使用 PBKDF2-HMAC-SHA256 算法加盐哈希密码 (100,000次迭代)
    """
    if not salt_hex:
        salt_bytes = secrets.token_bytes(16)
        salt_hex = salt_bytes.hex()
    else:
        salt_bytes = bytes.fromhex(salt_hex)

    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_bytes,
        iterations=100000
    )
    return derived.hex(), salt_hex

def verify_password(password: str, stored_hash: str, salt_hex: str) -> bool:
    """常量时间比较验证密码，防止旁路时序攻击"""
    computed_hash, _ = hash_password(password, salt_hex)
    return secrets.compare_digest(stored_hash, computed_hash)

def password_policy_error(password: str) -> Optional[str]:
    """返回密码策略错误信息；合规时返回 None。"""
    if not password or len(password) < 8:
        return "密码长度至少需 8 个字符"
    if password == "admin123":
        return "不能使用默认密码 admin123，请设置一个新密码"
    return None

class AuthManager:
    _instance = None

    def __init__(self, data_file: str = USERS_FILE):
        self.data_file = data_file
        os.makedirs(os.path.dirname(self.data_file), exist_ok=True)

        # 内存 Session 表: token -> { "username": ..., "role": ..., "created_at": ..., "expires_at": ... }
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        self.session_duration_seconds = 7 * 24 * 3600  # 默认免登录保持 7 天

        # 加载用户或初始化默认管理员
        self._load_or_init_users()

    @classmethod
    def get_instance(cls, data_file: str = USERS_FILE):
        if cls._instance is None:
            cls._instance = AuthManager(data_file)
        return cls._instance

    def _load_or_init_users(self):
        if not os.path.exists(self.data_file):
            # 初始化默认 admin 用户
            default_pwd = "admin123"
            pwd_hash, salt = hash_password(default_pwd)
            initial_users = {
                "admin": {
                    "username": "admin",
                    "display_name": "系统管理员",
                    "role": "admin",
                    "password_hash": pwd_hash,
                    "salt": salt,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "last_login": None,
                    "must_change_password": True
                }
            }
            self._save_users(initial_users)
            logger.info("已生成默认管理员账号: admin，初始密码: admin123 (请首次登录后立即修改)")

    def _read_users(self) -> Dict[str, Dict]:
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"读取用户数据失败: {e}")
            return {}

    def _save_users(self, users: Dict[str, Dict]):
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存用户数据失败: {e}")

    # 认证与会话管理
    def login(self, username: str, password: str, client_ip: str = "") -> Tuple[bool, str, Optional[Dict]]:
        """
        验证用户凭据并生成 Session Token
        """
        users = self._read_users()
        user = users.get(username)
        if not user:
            logger.warning(f"登录失败: 用户不存在 [{username}] (来源: {client_ip})")
            return False, "用户名或密码错误", None

        if not verify_password(password, user["password_hash"], user["salt"]):
            logger.warning(f"登录失败: 密码错误 [{username}] (来源: {client_ip})")
            return False, "用户名或密码错误", None

        # 认证成功，生成高熵安全 Token
        token = secrets.token_urlsafe(32)
        now = time.time()
        session_info = {
            "username": username,
            "display_name": user.get("display_name", username),
            "role": user["role"],
            "must_change_password": user.get("must_change_password", False),
            "created_at": now,
            "expires_at": now + self.session_duration_seconds
        }
        self.active_sessions[token] = session_info

        # 更新用户最后登录时间
        user["last_login"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save_users(users)

        logger.info(f"用户成功登录: [{username}] (角色: {user['role']}, 来源: {client_ip})")
        return True, token, session_info

    def logout(self, token: str):
        if token in self.active_sessions:
            username = self.active_sessions[token]["username"]
            del self.active_sessions[token]
            logger.info(f"用户已登出: [{username}]")

    def validate_session(self, token: Optional[str]) -> Optional[Dict]:
        """
        检验 Session 是否有效且未过期
        """
        if not token or token not in self.active_sessions:
            return None

        sess = self.active_sessions[token]
        if time.time() > sess["expires_at"]:
            del self.active_sessions[token]
            return None

        return sess

    # 用户管理 CRUD (仅管理员权限)
    def list_users(self) -> List[Dict]:
        """列出所有成员 (脱敏，过滤 hash 与 salt)"""
        users = self._read_users()
        result = []
        for u in users.values():
            result.append({
                "username": u["username"],
                "display_name": u.get("display_name", u["username"]),
                "role": u["role"],
                "created_at": u.get("created_at", "-"),
                "last_login": u.get("last_login", "从未登录"),
                "must_change_password": u.get("must_change_password", False)
            })
        return result

    def create_user(self, username: str, password: str, role: str = "family", display_name: str = "") -> Tuple[bool, str]:
        """创建新家庭成员账号"""
        username = username.strip()
        if not username or len(username) < 3:
            return False, "用户名长度至少需 3 个字符"
        policy_err = password_policy_error(password)
        if policy_err:
            return False, policy_err
        if role not in ("admin", "family"):
            return False, "非法角色，仅限 admin 或 family"

        users = self._read_users()
        if username in users:
            return False, f"用户 [{username}] 已存在"

        pwd_hash, salt = hash_password(password)
        users[username] = {
            "username": username,
            "display_name": display_name.strip() or username,
            "role": role,
            "password_hash": pwd_hash,
            "salt": salt,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_login": None,
            "must_change_password": False
        }
        self._save_users(users)
        logger.info(f"已创建新成员账号: [{username}] (角色: {role})")
        return True, "创建成功"

    def update_password(self, username: str, old_password: str, new_password: str, is_admin_override: bool = False) -> Tuple[bool, str]:
        """修改密码"""
        policy_err = password_policy_error(new_password)
        if policy_err:
            return False, policy_err
        if new_password == old_password:
            return False, "新密码不能与原密码相同"

        users = self._read_users()
        user = users.get(username)
        if not user:
            return False, "用户不存在"

        if not is_admin_override:
            if not verify_password(old_password, user["password_hash"], user["salt"]):
                return False, "原密码错误"

        pwd_hash, salt = hash_password(new_password)
        user["password_hash"] = pwd_hash
        user["salt"] = salt
        user["must_change_password"] = False
        self._save_users(users)

        # 密码修改后注销该用户其他所有旧会话
        for t, s in list(self.active_sessions.items()):
            if s["username"] == username:
                del self.active_sessions[t]

        logger.info(f"用户已成功修改密码: [{username}]")
        return True, "密码修改成功，请重新登录"

    def delete_user(self, username: str, operator_username: str) -> Tuple[bool, str]:
        """删除用户"""
        if username == operator_username:
            return False, "无法删除当前登录的账号"

        users = self._read_users()
        if username not in users:
            return False, "用户不存在"

        # 检查是否是最后一个 admin
        admin_count = sum(1 for u in users.values() if u["role"] == "admin")
        if users[username]["role"] == "admin" and admin_count <= 1:
            return False, "系统必须保留至少一个管理员账号"

        del users[username]
        self._save_users(users)

        # 清除其会话
        for t, s in list(self.active_sessions.items()):
            if s["username"] == username:
                del self.active_sessions[t]

        logger.info(f"管理员 [{operator_username}] 删除了用户账号: [{username}]")
        return True, "用户已成功删除"

auth_manager = AuthManager.get_instance()

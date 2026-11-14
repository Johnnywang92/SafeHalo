#!/usr/bin/env python3
"""
SafeHalo 数据脱敏与合规诊断工具 (sanitize_logs.py)
用于试点家庭日志外送、技术支持排查及云端算法训练前的隐私合规脱敏。

功能与脱敏范围:
1. MAC / BSSID 地址脱敏:
   - 提取 6 字节 MAC 地址 (XX:XX:XX:XX:XX:XX 或 XX-XX-XX-XX-XX-XX)
   - 基于会话级 Salt 进行可追踪假名化 (如 [MAC:a1b2c3d4])，既保护真实 BSSID，
     又保持同一日志内多次出现的节点相对关系一致，便于多径分析与排障。
2. 移动端推送凭证与系统密码脱敏:
   - pushplus_token, serverchan_key, bark_key, miniprogram_token, api_token
   - 密码字段 password, password_hash, salt
   - Webhook URL 内嵌入的 Secret Key 自动脱敏
3. 个人隐私与联系方式 (PII) 脱敏:
   - 手机号码 (11 位大陆手机号)
   - 守护长辈姓名/昵称 (care_nickname)
4. 支持单文件处理、整目录递归脱敏以及合规审计检查 (--check 模式)
"""

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

# 正则表达式
MAC_PATTERN = re.compile(r"\b([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})\b")
PHONE_PATTERN = re.compile(r"\b(1[3-9]\d)(\d{4})(\d{4})\b")

# JSON 敏感字段脱敏
SENSITIVE_JSON_KEYS = [
    "pushplus_token",
    "serverchan_key",
    "bark_key",
    "miniprogram_token",
    "api_token",
    "password",
    "password_hash",
    "salt",
]

# Webhook 敏感 URL 模式
WEBHOOK_URL_PATTERNS = [
    (re.compile(r"(https?://sctapi\.ftqq\.com/)([^./\s]+)(\.send)"), r"\1[REDACTED_SERVERCHAN_KEY]\3"),
    (re.compile(r"(https?://api\.day\.app/)([^./\s?#]+)"), r"\1[REDACTED_BARK_KEY]"),
    (re.compile(r"(https?://oapi\.dingtalk\.com/robot/send\?access_token=)([^&\s]+)"), r"\1[REDACTED_DINGTALK_TOKEN]"),
    (re.compile(r"(https?://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=)([^&\s]+)"), r"\1[REDACTED_WECHAT_KEY]"),
    (re.compile(r"(https?://open\.feishu\.cn/open-apis/bot/v2/hook/)([^/?\s]+)"), r"\1[REDACTED_FEISHU_KEY]"),
]


class LogSanitizer:
    def __init__(self, salt: str = "SafeHaloSanitizerSalt2026", care_nickname: Optional[str] = None):
        self.salt = salt
        self.care_nickname = care_nickname.strip() if care_nickname else None
        self._mac_cache: Dict[str, str] = {}

    def pseudonymize_mac(self, mac_str: str) -> str:
        """对 MAC 地址进行确定性假名化，保持会话内一致性"""
        normalized = mac_str.lower().replace("-", ":")
        if normalized not in self._mac_cache:
            digest = hashlib.sha256(f"{self.salt}:{normalized}".encode("utf-8")).hexdigest()
            # 格式化为假名化标识，例如 MAC:ab12cd34
            short_id = digest[:8]
            self._mac_cache[normalized] = f"[MAC:{short_id}]"
        return self._mac_cache[normalized]

    def sanitize_text(self, text: str) -> str:
        """对通用文本或日志行进行综合脱敏"""
        # 1. 假名化 MAC 地址
        def _replace_mac(match):
            return self.pseudonymize_mac(match.group(0))

        text = MAC_PATTERN.sub(_replace_mac, text)

        # 2. 脱敏手机号 (保留前3位与后4位，中间4位掩码)
        text = PHONE_PATTERN.sub(r"\1****\3", text)

        # 3. 脱敏 Webhook URL 中的 Secret Token
        for pattern, replacement in WEBHOOK_URL_PATTERNS:
            text = pattern.sub(replacement, text)

        # 4. 脱敏长辈姓名/昵称
        if self.care_nickname and len(self.care_nickname) > 1:
            text = text.replace(self.care_nickname, "[长辈]")

        # 5. 脱敏常见 JSON 键值对中的敏感凭证
        for key in SENSITIVE_JSON_KEYS:
            # 匹配 "key": "value" 格式
            pattern = re.compile(rf'("{re.escape(key)}"\s*:\s*)"([^"]*)"')
            text = pattern.sub(rf'\1"[REDACTED_{key.upper()}]"', text)

        return text

    def sanitize_json_object(self, obj):
        """对解析后的字典/列表递归进行合规字段清洗"""
        if isinstance(obj, dict):
            new_dict = {}
            for k, v in obj.items():
                if k in SENSITIVE_JSON_KEYS and isinstance(v, str) and v:
                    new_dict[k] = f"[REDACTED_{k.upper()}]"
                elif isinstance(v, (dict, list)):
                    new_dict[k] = self.sanitize_json_object(v)
                elif isinstance(v, str):
                    new_dict[k] = self.sanitize_text(v)
                else:
                    new_dict[k] = v
            return new_dict
        elif isinstance(obj, list):
            return [self.sanitize_json_object(item) for item in obj]
        elif isinstance(obj, str):
            return self.sanitize_text(obj)
        else:
            return obj

    def sanitize_file(self, src_path: str, dst_path: str):
        """对单个文件执行脱敏，智能处理 JSON/JSONL/普通文本"""
        os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
        filename = os.path.basename(src_path)

        if filename.endswith(".json"):
            try:
                with open(src_path, "r", encoding="utf-8") as f:
                    content = f.read()
                data = json.loads(content)
                sanitized_data = self.sanitize_json_object(data)
                with open(dst_path, "w", encoding="utf-8") as f:
                    json.dump(sanitized_data, f, ensure_ascii=False, indent=2)
                return
            except Exception:
                pass  # 回退到流式文本处理

        with open(src_path, "r", encoding="utf-8", errors="ignore") as f_in, \
             open(dst_path, "w", encoding="utf-8") as f_out:
            for line in f_in:
                # 针对 JSONL 进行每行处理
                line_stripped = line.strip()
                if line_stripped.startswith("{") and line_stripped.endswith("}"):
                    try:
                        parsed = json.loads(line_stripped)
                        sanitized_obj = self.sanitize_json_object(parsed)
                        f_out.write(json.dumps(sanitized_obj, ensure_ascii=False) + "\n")
                        continue
                    except Exception:
                        pass
                f_out.write(self.sanitize_text(line))

    def sanitize_directory(self, src_dir: str, dst_dir: str) -> List[Tuple[str, str]]:
        """递归清洗整个目录，返回已处理的文件列表"""
        processed = []
        for root, _, files in os.walk(src_dir):
            for file in sorted(files):
                if file.startswith("."):
                    continue
                src_file = os.path.join(root, file)
                rel_path = os.path.relpath(src_file, src_dir)
                dst_file = os.path.join(dst_dir, rel_path)
                self.sanitize_file(src_file, dst_file)
                processed.append((src_file, dst_file))
        return processed

    def check_file_compliance(self, file_path: str) -> List[str]:
        """检查文件中是否存在未脱敏的敏感信息，返回违规问题列表"""
        issues = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line_no, line in enumerate(f, 1):
                    # 检查未脱敏 MAC 地址
                    mac_matches = MAC_PATTERN.findall(line)
                    if mac_matches:
                        issues.append(f"行 {line_no}: 发现原始 MAC 地址: {mac_matches[0]}")

                    # 检查未脱敏手机号
                    phone_matches = PHONE_PATTERN.findall(line)
                    if phone_matches:
                        issues.append(f"行 {line_no}: 发现原始手机号码")

                    # 检查未脱敏的配置敏感 Key
                    for key in SENSITIVE_JSON_KEYS:
                        pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*"([^"]+)"')
                        m = pattern.search(line)
                        if m and not m.group(1).startswith("[REDACTED"):
                            issues.append(f"行 {line_no}: 敏感凭证字段未脱敏: {key}")
        except Exception as e:
            issues.append(f"读取文件失败: {e}")
        return issues


def main():
    parser = argparse.ArgumentParser(description="SafeHalo 数据合规与脱敏工具")
    parser.add_argument("--input", "-i", help="待脱敏的输入文件路径")
    parser.add_argument("--output", "-o", help="脱敏后的输出文件路径")
    parser.add_argument("--input-dir", help="待脱敏的输入目录 (递归处理)")
    parser.add_argument("--output-dir", help="脱敏后的输出目录")
    parser.add_argument("--in-place", action="store_true", help="就地覆盖脱敏 (谨慎使用)")
    parser.add_argument("--check", help="仅执行合规性审计检查，发现未脱敏敏感数据返回非零退出码")
    parser.add_argument("--care-nickname", help="守护长辈姓名/昵称 (予以抹除)")
    parser.add_argument("--salt", default="SafeHaloSanitizerSalt2026", help="MAC 假名化哈希盐值")

    args = parser.parse_args()
    sanitizer = LogSanitizer(salt=args.salt, care_nickname=args.care_nickname)

    if args.check:
        target = args.check
        if os.path.isfile(target):
            issues = sanitizer.check_file_compliance(target)
            if issues:
                print(f"❌ 合规审计失败 [{target}]:")
                for issue in issues[:10]:
                    print(f"   - {issue}")
                sys.exit(1)
            else:
                print(f"✅ 合规审计通过: {target} 未发现明文敏感数据")
                sys.exit(0)
        elif os.path.isdir(target):
            all_issues = {}
            for root, _, files in os.walk(target):
                for file in files:
                    p = os.path.join(root, file)
                    issues = sanitizer.check_file_compliance(p)
                    if issues:
                        all_issues[p] = issues
            if all_issues:
                print(f"❌ 目录合规审计失败: {len(all_issues)} 个文件存在明文隐私")
                for p, iss in list(all_issues.items())[:5]:
                    print(f"   [{p}]:")
                    for is_item in iss[:3]:
                        print(f"      - {is_item}")
                sys.exit(1)
            else:
                print(f"✅ 目录合规审计通过: {target} 内所有文件已安全脱敏")
                sys.exit(0)
        else:
            print(f"错误: 检查路径不存在: {target}")
            sys.exit(2)

    if args.input_dir and args.output_dir:
        res = sanitizer.sanitize_directory(args.input_dir, args.output_dir)
        print(f"✅ 成功脱敏目录: {args.input_dir} -> {args.output_dir} (共处理 {len(res)} 个文件)")
        sys.exit(0)

    if args.input:
        if args.in_place:
            dst = args.input + ".tmp_sanitized"
            sanitizer.sanitize_file(args.input, dst)
            os.replace(dst, args.input)
            print(f"✅ 就地脱敏完成: {args.input}")
            sys.exit(0)
        elif args.output:
            sanitizer.sanitize_file(args.input, args.output)
            print(f"✅ 脱敏完成: {args.input} -> {args.output}")
            sys.exit(0)
        else:
            # 标准输出
            with open(args.input, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    sys.stdout.write(sanitizer.sanitize_text(line))
            sys.exit(0)

    parser.print_help()


if __name__ == "__main__":
    main()

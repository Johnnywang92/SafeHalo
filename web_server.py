#!/usr/bin/env python3
"""
SafeHalo Web 控制台入口程序 (web_server.py)
用法:
  python3 web_server.py
  python3 web_server.py --port 8080
"""

import argparse
from src.safehalo.web_portal import start_portal_server

def main():
    parser = argparse.ArgumentParser(description="SafeHalo 路由器 Web 配置与监控控制台")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址 (默认 0.0.0.0 支持远程与穿透)")
    parser.add_argument("--port", type=int, default=8080, help="Web 控制台端口 (默认 8080)")
    args = parser.parse_args()

    start_portal_server(args.port, host=args.host)

if __name__ == "__main__":
    main()

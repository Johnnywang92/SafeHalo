#!/usr/bin/env python3
"""
SafeHalo 实验数据实时打标小工具
用法: python3 scripts/tagger.py [输出文件.csv]
通过按键快速切换当前动作状态，记录带毫秒级时间戳的事件标签。
"""

import sys
import time
from datetime import datetime

DEFAULT_OUTPUT = "experiments/tags.csv"

ACTIONS = {
    "1": ("empty", "空房间无人基线"),
    "2": ("static_sit", "有人静止(坐姿/呼吸)"),
    "3": ("static_lie", "有人静止(平躺/呼吸)"),
    "4": ("moving_walk", "有人走动"),
    "5": ("moving_action", "有人大幅度活动/做家务"),
    "6": ("interference", "环境干扰源(风扇/开窗/宠物)"),
    "7": ("fall_down", "模拟跌倒动作中"),
    "8": ("post_fall_static", "跌倒后倒地不动"),
    "0": ("stop", "标记暂停/过渡中"),
}

def main():
    out_file = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUTPUT
    print(f"=== SafeHalo 实验实时打标器 ===")
    print(f"标签记录将保存至: {out_file}\n")
    print("快捷键列表:")
    for k, (name, desc) in ACTIONS.items():
        print(f"  [{k}] {name:<18} : {desc}")
    print("  [q] 退出程序\n")

    with open(out_file, "a", encoding="utf-8") as f:
        # 如果文件为空则写入表头
        if f.tell() == 0:
            f.write("timestamp_iso,epoch_s,label,description\n")
            f.flush()

        current_label = None
        while True:
            try:
                cmd = input(f"当前状态 [{current_label}]. 请输入按键编号: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n打标结束。")
                break

            if cmd == "q":
                print("打标结束。")
                break

            if cmd in ACTIONS:
                label, desc = ACTIONS[cmd]
                now_epoch = time.time()
                now_iso = datetime.fromtimestamp(now_epoch).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                f.write(f"{now_iso},{now_epoch:.3f},{label},{desc}\n")
                f.flush()
                current_label = label
                print(f"-> 记录成功: [{now_iso}] {label} ({desc})")
            else:
                print("无效输入，请重新输入编号或按 q 退出。")

if __name__ == "__main__":
    main()

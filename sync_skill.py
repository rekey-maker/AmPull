# -*- coding: utf-8 -*-
"""sync_skill.py — AmPull 技能分发（r38 起）：SKILL.md + ampull.py 成对
同步到全部 agent 技能目录（成对分发是 v5.19 部署形态的关键——技能目录
里的 ampull.py 拷贝就是运行本体，单拷 SKILL.md 会让脚本路径断链）。

部署位：
  1. <桌面>/Skills/ampull/            （历史部署位）
  2. ~/.zcode/skills/ampull/          （ZCode 加载位）
  3. ~/.workbuddy/skills/ampull/      （WorkBuddy 加载位）

用法：python sync_skill.py          （同步 + 打印结果）
     python sync_skill.py --check  （仅校验漂移，任一副本漂移=exit 1）
"""
import hashlib
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PAIRS = ["SKILL.md", "ampull.py"]
DSTS = [
    os.path.join(os.path.dirname(ROOT), "Skills", "ampull"),
    os.path.join(os.path.expanduser("~"), ".zcode", "skills", "ampull"),
    os.path.join(os.path.expanduser("~"), ".workbuddy", "skills", "ampull"),
]


def h(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def main(argv):
    for name in PAIRS:
        if not os.path.isfile(os.path.join(ROOT, name)):
            print("[FAIL] 母本不存在：%s" % name)
            return 1
    rc = 0
    for dst_dir in DSTS:
        for name in PAIRS:
            dst = os.path.join(dst_dir, name)
            src = os.path.join(ROOT, name)
            drifted = (not os.path.isfile(dst)) or h(src) != h(dst)
            if argv[1:] == ["--check"]:
                print("drift=%s  %s  %s" % (drifted, name, dst_dir))
                rc = 1 if drifted else rc
                continue
            if drifted:
                os.makedirs(dst_dir, exist_ok=True)
                shutil.copyfile(src, dst)
                print("[OK] synced %s -> %s" % (name, dst_dir))
            else:
                print("[OK] already in sync  %s  %s" % (name, dst_dir))
    if argv[1:] == ["--check"] and rc == 0:
        print("[OK] %d 个部署位均无漂移" % len(DSTS))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""產生管理員密碼的 scrypt 雜湊（單行），輸出到 stdout。

用法：python3.12 deploy/make_password_hash.py > admin.hash
密碼以 getpass 互動輸入，不經命令列參數、不進 shell history。
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from profile_manager.auth import hash_password  # noqa: E402

pw = getpass.getpass("管理員密碼：")
if len(pw) < 12 or pw != getpass.getpass("再輸入一次："):
    sys.exit("密碼至少 12 字元且兩次需相同")
print(hash_password(pw))

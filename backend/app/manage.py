# Manage development accounts without printing or accepting command-line passwords.
# 管理开发账号，不打印密码，也不通过命令行参数接收密码。

import argparse
import getpass
import os
import re

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import User
from app.security import hash_password


# Create or update an account; bootstrap leaves existing passwords unchanged.
# 创建或更新账号；初始化时不改变已有账号密码。
def save_user(username: str, password: str, *, bootstrap=False, reset=False):
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", username):
        raise ValueError("Username must use 1-64 ASCII letters, numbers, dots, hyphens or underscores")
    if len(password) < 12 or not password.isascii():
        raise ValueError("Password must contain at least 12 ASCII characters for HTTP Basic authentication")
    with SessionLocal.begin() as session:
        user = session.scalar(select(User).where(User.username == username).with_for_update())
        if user:
            if bootstrap:
                # Do not reset an existing account during a container restart.
                # 容器重启时不重置已有账号。
                return
            if not reset:
                raise ValueError("User already exists; use set-password to change its password")
            user.password_hash = hash_password(password)
            user.updated_at = func.clock_timestamp()
        elif reset:
            raise ValueError("User does not exist")
        else:
            session.add(User(username=username, display_name=username, password_hash=hash_password(password)))


# Read administrator passwords interactively, or from the bootstrap environment.
# 交互式读取管理员设置的密码，或从初始化环境变量读取。
def main():
    parser = argparse.ArgumentParser(description="Manage development accounts; passwords are never printed.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap")
    for command in ("create-user", "set-password"):
        commands.add_parser(command).add_argument("username")
    args = parser.parse_args()
    try:
        if args.command == "bootstrap":
            password = os.environ.get("BOOTSTRAP_PASSWORD", "")
            if password == "GENERATE_WITH_INIT_ENV":
                raise ValueError("Run scripts/init_env.py before starting the services")
            save_user(os.environ.get("BOOTSTRAP_USERNAME", "developer"), password, bootstrap=True)
        else:
            password = getpass.getpass("New password (12+ characters): ")
            if password != getpass.getpass("Confirm password: "):
                raise ValueError("Passwords do not match")
            save_user(args.username, password, reset=args.command == "set-password")
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    print("Account initialization/update completed. No credentials were printed.")


if __name__ == "__main__":
    main()

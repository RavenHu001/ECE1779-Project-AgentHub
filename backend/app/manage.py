# Manage development accounts and seed orders for the refund demo.
# 管理开发账号，并为退款演示初始化订单。

import argparse
import getpass
import os
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.db import SessionLocal
from app.models import Order, User
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


def seed_demo_orders(primary_username: str, secondary_username: str):
    """Seed four orders for two existing accounts without rewriting saved orders.
    为两个已有账号初始化四笔订单，保留已有订单的数据。
    """
    primary = primary_username.strip().lower()
    secondary = secondary_username.strip().lower()
    if primary == secondary:
        raise ValueError("Choose two different accounts for the demo orders")

    with SessionLocal.begin() as session:
        users = dict(
            session.execute(
                select(User.username, User.id).where(User.username.in_((primary, secondary)), User.is_active)
            ).all()
        )
        if len(users) != 2:
            raise ValueError("Both demo accounts must exist and be active")
        seeded_at = session.scalar(select(func.clock_timestamp()))
        samples = (
            (1001, users[primary], 12999, 5),
            (1002, users[primary], 4500, 45),
            (1003, users[primary], 7999, None),
            (1005, users[secondary], 10000, 3),
        )
        rows = [
            {
                "id": order_id,
                "assigned_user_id": user_id,
                "payment_status": "PAID",
                "fulfillment_status": "DELIVERED",
                "paid_total_minor": amount,
                "currency": "CAD",
                "delivered_at": seeded_at - timedelta(days=days_ago) if days_ago is not None else None,
            }
            for order_id, user_id, amount, days_ago in samples
        ]
        # Ignore existing rows, then verify ownership even after a concurrent seed.
        # 跳过已有记录，再校验归属，兼顾并发初始化的情况。
        session.execute(insert(Order).values(rows).on_conflict_do_nothing(index_elements=[Order.id]))
        expected_owners = {order_id: user_id for order_id, user_id, _, _ in samples}
        actual_owners = dict(
            session.execute(
                select(Order.id, Order.assigned_user_id).where(Order.id.in_(tuple(expected_owners)))
            ).all()
        )
        if actual_owners != expected_owners:
            raise ValueError("Demo order IDs already belong to different accounts")


def main():
    """Run account commands or seed the refund demo after accounts exist.
    管理开发账号，或在账号创建后初始化退款演示订单。
    """
    parser = argparse.ArgumentParser(description="Manage development accounts and demo orders.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap")
    for command in ("create-user", "set-password"):
        commands.add_parser(command).add_argument("username")
    seed = commands.add_parser("seed-demo-orders")
    seed.add_argument("primary_username")
    seed.add_argument("secondary_username")
    args = parser.parse_args()
    try:
        if args.command == "seed-demo-orders":
            seed_demo_orders(args.primary_username, args.secondary_username)
            print("Demo orders are ready.")
            return
        if args.command == "bootstrap":
            # Bootstrap uses the environment, while account changes prompt for passwords.
            # 初始化从环境读取密码，其他账号变更通过交互输入密码。
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

# Manage development accounts and seed orders and tickets for the refund demo.
# 管理开发账号，并为退款演示初始化订单和工单。

import argparse
import getpass
import os
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.db import SessionLocal
from app.models import Order, Ticket, User
from app.security import hash_password


def save_user(username: str, password: str, *, bootstrap=False, reset=False):
    """Save a development account with a non-empty ASCII password.
    保存使用非空 ASCII 密码的开发账号，初始化时保留已有密码。
    """
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", username):
        raise ValueError("Username must use 1-64 ASCII letters, numbers, dots, hyphens or underscores")
    if not password or not password.isascii():
        raise ValueError("Password must be non-empty and use only ASCII characters")
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


def seed_demo_orders(primary_username: str, secondary_username: str, *, with_tickets: bool = False):
    """Seed four orders and optionally their tickets without rewriting saved data.
    初始化四笔订单及可选的关联工单，保留已有数据。
    """
    primary = primary_username.strip().lower()
    secondary = secondary_username.strip().lower()
    if primary == secondary:
        raise ValueError("Choose two different accounts for the demo orders")

    with SessionLocal.begin() as session:
        # Resolve both account assignments before generating any demo data.
        # 先确认两个账号的归属，再生成演示数据。
        users = dict(
            session.execute(
                select(User.username, User.id).where(User.username.in_((primary, secondary)), User.is_active)
            ).all()
        )
        if len(users) != 2:
            raise ValueError("Both demo accounts must exist and be active")
        # Use one database timestamp for every relative delivery date.
        # 所有相对交付日期共用同一个数据库时刻。
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

        if with_tickets:
            # Seed the links only after verifying ownership of every referenced order.
            # 先核对所有关联订单的归属，再初始化工单关联。
            links = {391: 1001, 392: 1002, 393: 1003, 395: 1005}
            expected_links = {
                ticket_id: (order_id, expected_owners[order_id]) for ticket_id, order_id in links.items()
            }
            tickets = [
                {"id": ticket_id, "order_id": order_id, "assigned_user_id": user_id}
                for ticket_id, (order_id, user_id) in expected_links.items()
            ]
            session.execute(insert(Ticket).values(tickets).on_conflict_do_nothing(index_elements=[Ticket.id]))

            # Preserve existing ticket status and reject conflicting owners or links.
            # 保留已有工单状态，拒绝归属或关联订单冲突。
            actual_links = {
                ticket_id: (order_id, user_id)
                for ticket_id, order_id, user_id in session.execute(
                    select(Ticket.id, Ticket.order_id, Ticket.assigned_user_id).where(
                        Ticket.id.in_(tuple(links))
                    )
                )
            }
            if actual_links != expected_links:
                raise ValueError("Demo ticket IDs already have different accounts or order links")


def main():
    """Dispatch account and demo data commands from the development CLI.
    从开发命令行分发账号管理和演示数据初始化命令。
    """
    parser = argparse.ArgumentParser(description="Manage development accounts and demo data.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap")
    for command in ("create-user", "set-password"):
        commands.add_parser(command).add_argument("username")
    # Both seed commands take the same account assignments.
    # 两个初始化命令接收相同的账号归属参数。
    for command in ("seed-demo-orders", "seed-demo-tickets"):
        seed = commands.add_parser(command)
        seed.add_argument("primary_username")
        seed.add_argument("secondary_username")
    args = parser.parse_args()
    try:
        if args.command in ("seed-demo-orders", "seed-demo-tickets"):
            # Keep the original order-only command available for existing callers.
            # 保留原有的仅初始化订单命令，兼容现有调用方。
            with_tickets = args.command == "seed-demo-tickets"
            seed_demo_orders(args.primary_username, args.secondary_username, with_tickets=with_tickets)
            print("Demo orders and tickets are ready." if with_tickets else "Demo orders are ready.")
            return
        if args.command == "bootstrap":
            # Bootstrap uses the environment, while account changes prompt for passwords.
            # 初始化从环境读取密码，其他账号变更通过交互输入密码。
            password = os.environ.get("BOOTSTRAP_PASSWORD", "")
            if password == "GENERATE_WITH_INIT_ENV":
                raise ValueError("Run scripts/init_env.py before starting the services")
            save_user(os.environ.get("BOOTSTRAP_USERNAME", "developer"), password, bootstrap=True)
        else:
            password = getpass.getpass("New password: ")
            if password != getpass.getpass("Confirm password: "):
                raise ValueError("Passwords do not match")
            save_user(args.username, password, reset=args.command == "set-password")
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    print("Account initialization/update completed. No credentials were printed.")


if __name__ == "__main__":
    main()

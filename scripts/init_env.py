# Generate private development settings without changing an existing environment file.
# 生成私有开发配置，不修改已有环境文件。

"""Create ignored development configuration without printing passwords."""

import argparse
from pathlib import Path
import re
import secrets


# Validate the environment namespace and create independent random credentials.
# 校验环境命名空间，并分别生成随机凭据。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-name", default="agenthub-dev")
    parser.add_argument("--api-port", type=int, default=8000)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.project_name):
        parser.error("project-name must use lowercase letters, numbers, - or _")
    if not 1 <= args.api_port <= 65535:
        parser.error("api-port must be between 1 and 65535")
    root = Path(__file__).resolve().parents[1]
    target = root / ".env"
    content = (root / ".env.example").read_text(encoding="utf-8")
    content = content.replace("COMPOSE_PROJECT_NAME=agenthub-dev", f"COMPOSE_PROJECT_NAME={args.project_name}")
    content = content.replace("API_PORT=8000", f"API_PORT={args.api_port}")
    content = content.replace("POSTGRES_PASSWORD=GENERATE_WITH_INIT_ENV", "POSTGRES_PASSWORD=" + secrets.token_urlsafe(32))
    content = content.replace("BOOTSTRAP_PASSWORD=GENERATE_WITH_INIT_ENV", "BOOTSTRAP_PASSWORD=" + secrets.token_urlsafe(24))
    try:
        with target.open("x", encoding="utf-8") as config_file:
            config_file.write(content)
        target.chmod(0o600)
    except FileExistsError:
        parser.exit(message=".env already exists; no configuration was changed.\n")
    print("Created ignored .env. Open it privately to obtain the bootstrap login.")


if __name__ == "__main__":
    main()

# Exercise the live API and worker using English demonstration input.
# 使用英文演示输入验证实际 API 和 worker 链路。

"""Exercise the live API and separate worker without printing credentials."""

import argparse
import base64
import json
import os
import time
import urllib.request
from uuid import uuid4


# Reuse bootstrap credentials internally and print only non-secret verification results.
# 在内部使用初始化凭据，只打印不含秘密的验证结果。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-conversation", help="Only check a previously printed conversation UUID")
    args = parser.parse_args()
    credentials = f"{os.environ['BOOTSTRAP_USERNAME']}:{os.environ['BOOTSTRAP_PASSWORD']}"
    authorization = "Basic " + base64.b64encode(credentials.encode()).decode()

    # Send credentials in the authorization header, never in the URL.
    # 通过身份验证请求头发送凭据，不将凭据放进地址。
    def call(path, body=None):
        request = urllib.request.Request(
            "http://api:8000" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": authorization, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    if args.verify_conversation:
        messages = call(f"/conversations/{args.verify_conversation}/messages")
        if len(messages) < 2:
            raise RuntimeError("Saved request/reply not found")
        print("Persisted conversation and reply verified.")
        return
    conversation = call("/conversations", {"title": "Container smoke check"})["id"]
    payload = {"content": "Hello", "submission_key": str(uuid4())}
    receipt = call(f"/conversations/{conversation}/messages", payload)
    if call(f"/conversations/{conversation}/messages", payload) != receipt:
        raise RuntimeError("Duplicate submission did not return the original receipt")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = call(f"/jobs/{receipt['job_id']}")
        if job["status"] == "SUCCEEDED":
            break
        if job["status"] == "FAILED":
            raise RuntimeError("Worker reported failure")
        time.sleep(0.5)
    else:
        raise RuntimeError("Timed out waiting for the worker")
    messages = call(f"/conversations/{conversation}/messages")
    if len(messages) != 2 or messages[1]["reply_to_message_id"] != receipt["message_id"]:
        raise RuntimeError("Expected one request and its single reply")
    print(f"API, database, worker and duplicate submission OK. conversation_id={conversation}")


if __name__ == "__main__":
    main()

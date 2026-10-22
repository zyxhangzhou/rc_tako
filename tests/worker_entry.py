"""测试专用 worker 入口：关闭地址校验，才能投递到 127.0.0.1 的假下游。

生产入口（python -m notifier.worker）没有任何"允许内网"的开关。
"""

import sys

from notifier.worker import main

if __name__ == "__main__":
    sys.exit(main(guard=lambda url: None))

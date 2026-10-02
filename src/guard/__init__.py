"""赛事消费异常联防后端（仅标准库依赖）。

模块：
- ``db``       SQLite 存储 + WAL + 领域写操作
- ``engine``   政策时效、预警评估、承担方推导
- ``server``   HTTP API、RBAC、幂等重放
"""

from __future__ import annotations

__version__ = "0.2.0"

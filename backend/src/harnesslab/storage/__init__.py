"""业务仓储与检查点接线，隐藏具体数据库接口。"""

from .db import Database, get_database

__all__ = ["Database", "get_database"]

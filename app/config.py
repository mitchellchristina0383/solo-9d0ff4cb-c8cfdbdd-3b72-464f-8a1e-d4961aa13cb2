"""运行期配置：数据库路径等均可通过环境变量覆盖。"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# SQLite 数据库文件位置；容器内默认放到挂载卷 /data 下
DB_PATH = os.environ.get(
    "ENERGY_DB_PATH",
    os.path.join(BASE_DIR, "data", "energy.db"),
)

# 仅在自测时打开的故障注入开关（默认关闭，不影响正常使用）
ENABLE_FAULT_INJECTION = os.environ.get("ENABLE_FAULT_INJECTION", "") == "1"

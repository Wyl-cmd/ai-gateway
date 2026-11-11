"""pytest 全局配置。

把 src/ 加进 sys.path —— 这样即使没跑 `pip install -e .` 也能 import gateway。
装了 editable 包时这句是冗余但无害的。
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

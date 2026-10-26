"""quality 包：代码整洁度的确定性度量。

三个子能力：
- metrics：AST 指标 + smell 检出 + 0-100 评分
- clones：跨文件结构重复检测
- polish：（见 polish.py）整洁度收敛循环
"""

from cforge.quality.clones import CloneReport, detect_clones, scan as scan_clones
from cforge.quality.metrics import (
    FileMetrics,
    QualityReport,
    Smell,
    analyze_source,
    diff_reports,
    evaluate,
)

__all__ = [
    "evaluate", "analyze_source", "Smell", "QualityReport", "FileMetrics",
    "diff_reports", "scan_clones", "detect_clones", "CloneReport",
]

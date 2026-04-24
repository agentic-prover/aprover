"""
GRACE Evaluation Infrastructure (Phase 4).

Provides tools for running GRACE on a corpus of C programs,
comparing against baselines, and generating research-ready metrics.
"""

from amc.evaluation.corpus import Corpus, CorpusEntry, GroundTruthBug
from amc.evaluation.baselines import (
    BaselineResult,
    CBMCAloneBaseline,
    AMCAblationBaseline,
)
from amc.evaluation.metrics import (
    DriverMetrics,
    EvaluationSummary,
    MetricsCollector,
)
from amc.evaluation.report import ReportGenerator
from amc.evaluation.runner import EvaluationRunner

__all__ = [
    "Corpus",
    "CorpusEntry",
    "GroundTruthBug",
    "BaselineResult",
    "CBMCAloneBaseline",
    "AMCAblationBaseline",
    "DriverMetrics",
    "EvaluationSummary",
    "MetricsCollector",
    "ReportGenerator",
    "EvaluationRunner",
]

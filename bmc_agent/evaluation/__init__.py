"""
BMC-Agent Evaluation Infrastructure (Phase 4).

Provides tools for running BMC-Agent on a corpus of C programs,
comparing against baselines, and generating research-ready metrics.
"""

from bmc_agent.evaluation.corpus import Corpus, CorpusEntry, GroundTruthBug
from bmc_agent.evaluation.baselines import (
    BaselineResult,
    CBMCAloneBaseline,
    AMCAblationBaseline,
)
from bmc_agent.evaluation.metrics import (
    DriverMetrics,
    EvaluationSummary,
    MetricsCollector,
)
from bmc_agent.evaluation.report import ReportGenerator
from bmc_agent.evaluation.runner import EvaluationRunner

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

from .backtester import backtest, BacktestResult
from .metrics import compute_metrics
from .optimizer import optimize, Candidate, param_grid
from .claude_analyst import ClaudeAnalyst, VetResult, ResearchProposal

__all__ = ["backtest", "BacktestResult", "compute_metrics", "optimize", "Candidate", "param_grid",
           "ClaudeAnalyst", "VetResult", "ResearchProposal"]

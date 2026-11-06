"""Fast engine: SCAN -> DETECT -> COST -> RISK -> BUY -> MANAGE -> SELL -> RECORD, every few seconds, paper only.

Built on the read-only scanner (detection, filters, size-specific quotes, costs) plus the existing independent
RiskEngine, KillSwitch, PaperBroker and PortfolioBook. It has its own ledger file (data/fast.db) so its trades never
mix with the 4h engine's paper record or count toward the live gate. Live execution is not wired: the broker is
always the PaperBroker, and `mode=live` is refused at construction.
"""

"""TRADEBOT signer: a separate, restricted process that holds the bot wallet's key and signs only what its own
policy, activation and simulation allow. The trading process never sees the key; it asks over an authenticated
loopback API and receives a signed transaction or a refusal.

Run it as a different OS user from the trading process, with its home directory readable only by that user.
See docs/LIVE_EXECUTION_ADAPTER.md.
"""

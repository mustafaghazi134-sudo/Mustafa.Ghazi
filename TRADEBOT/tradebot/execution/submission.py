"""The build-level switch for real mainnet transaction submission.

`MAINNET_SUBMISSION_ENABLED` is a code constant, deliberately not a config value or environment variable: turning it
on requires a reviewed code change, committed with the owner's explicit written authorisation. While it is False:

  * `JupiterExecutionAdapter` refuses to ask the signer for a signature and never calls `/execute`;
  * the signer service (`tradebot_signer`) refuses every signing request, even when activated;
  * `tests/test_live_adapter.py::test_submission_is_disabled_in_this_build` pins the value.

Read-only preflight and the dry-run path (quote, verify, simulate without a signature) work regardless.
"""
MAINNET_SUBMISSION_ENABLED = False

DISABLED_REASON = ("real transaction submission is disabled in this build "
                   "(tradebot/execution/submission.py: MAINNET_SUBMISSION_ENABLED = False)")


def submission_enabled() -> bool:
    # Looked up at call time (not import time) so a test can exercise the gated paths with a patched module.
    from . import submission
    return bool(submission.MAINNET_SUBMISSION_ENABLED)

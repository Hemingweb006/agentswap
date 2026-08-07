"""Allow `python -m agentswap` as well as `python -m agentswap.cli`.

Without this, running the package directly fails with "'agentswap' is a package
and cannot be directly executed", which is the first thing anyone tries.
"""

from .cli import main

raise SystemExit(main())

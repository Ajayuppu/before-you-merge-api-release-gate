"""Local reference implementation of the API change reviewer.

The package runs without AWS credentials. Cloud adapters live beside it and are
not invoked unless a caller explicitly constructs them.
"""

__version__ = "1.0.0"

LOCAL_SIMULATION_BANNER = (
    "SIMULATION: this process is not production authentication and is not AWS IAM. "
    "Identity is taken from an explicit local principal, never from a reviewer field "
    "in the request body."
)

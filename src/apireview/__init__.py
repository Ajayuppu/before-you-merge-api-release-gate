"""Local reference implementation of the API change reviewer.

Importing the package does not contact AWS. Handlers build SDK clients only
when they run inside Lambda and the caller did not inject one.
"""

__version__ = "1.0.0"

LOCAL_SIMULATION_BANNER = (
    "SIMULATION: this process is not production authentication and is not AWS IAM. "
    "Identity is taken from an explicit local principal, never from a reviewer field "
    "in the request body."
)

"""Where a session is told the dashboard is: its address, and never its credential.

A leaf, imported by the launch that exports the address and by whatever reads
it back, so neither pulls the dashboard's server in to spell one variable.
"""

from pydantic import Field
from pydantic_settings import BaseSettings

# lup: ignore[constant-declaration] — the launch that exports it and the session
# that reads it are different processes, so the name is an identity, not a preference
DASHBOARD_URL_ENV = "LUP_DASHBOARD_URL"
"""Environment variable carrying the dashboard's address to every launched session.

The address alone: the capability that opens the page stays in the host's
private state, so a session knows where the operator reviews and cannot
review for them.
"""


class AdvertisedDashboard(BaseSettings):
    """The dashboard address a launch handed this process, where one did."""

    url: str = Field(default="", validation_alias=DASHBOARD_URL_ENV)

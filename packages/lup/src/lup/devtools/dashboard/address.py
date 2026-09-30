"""Where a session is told the dashboard is: its address, and never its credential.

A leaf, imported by the launch that exports the address and by whatever reads
it back, so neither pulls the dashboard's server in to spell one variable. The
address's variable is the policy's, whose hooks read it too
(:data:`lup.policy.identity.DASHBOARD_URL_ENV`); the pulse's is the
dashboard's own (:data:`lup.devtools.dashboard.pulse.DASHBOARD_PULSE_ENV`).
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from lup.devtools.dashboard.pulse import DASHBOARD_PULSE_ENV
from lup.policy.identity import DASHBOARD_URL_ENV


class AdvertisedDashboard(BaseSettings):
    """The dashboard a launch handed this process, where one did."""

    url: str = Field(default="", validation_alias=DASHBOARD_URL_ENV)
    pulse: str = Field(default="", validation_alias=DASHBOARD_PULSE_ENV)
    """The file the dashboard publishes what it counts in, lent read-only."""

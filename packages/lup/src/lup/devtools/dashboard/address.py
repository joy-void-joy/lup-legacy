"""Where a session is told the dashboard is: its address, and never its credential.

A leaf, imported by the launch that exports the address and by whatever reads
it back, so neither pulls the dashboard's server in to spell one variable. The
variable's name is the policy's, whose hooks read it too
(:data:`lup.policy.identity.DASHBOARD_URL_ENV`).
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from lup.policy.identity import DASHBOARD_URL_ENV


class AdvertisedDashboard(BaseSettings):
    """The dashboard address a launch handed this process, where one did."""

    url: str = Field(default="", validation_alias=DASHBOARD_URL_ENV)

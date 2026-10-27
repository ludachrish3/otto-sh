"""Docker's own output, verbatim, for tests that feed it to otto's parsers and retries.

One sample per shape, shared, so two entry points that must react the same
way are driven with the same input.
"""

NETWORK_RACE = (
    " Network otto-project1-x_default  Created\n"
    " Container otto-project1-x-api-1  Starting\n"
    "Error response from daemon: failed to set up container networking: "
    "network otto-project1-x_default not found\n"
)
"""compose's libnetwork race: the network is Created, the container Created, then
attaching it at "Starting" fails because the just-created network is not yet
visible to the daemon's networking setup."""

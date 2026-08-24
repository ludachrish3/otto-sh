import logging

from otto.cli.run import instruction

logger = logging.getLogger(__name__)


@instruction()
async def use_beetroot():
    """Import otto_fixture_beetroot at CALL time -- the lazy failure shape.

    Without the preflight this is an ImportError mid-run, after the lab is up
    and hosts may already have been touched. Plan 3's preflight is what turns
    it into a refusal before anything is contacted.
    """
    import otto_fixture_beetroot

    logger.info(f"the fixture package says: {otto_fixture_beetroot.beet()}")

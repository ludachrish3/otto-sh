# Ensure that the OttoLogger is initialized correctly before anything else happens
from otto.logger import getOttoLogger as getOttoLogger

getOttoLogger()

from otto.cli import app

from otto.configmodule.repo import Repo


class MockRepo(Repo):

    async def setCommitHash(self):
        self._gitHash = '9575739d490dc5e29e0eb985ca6f1f26ca4e65b3'

    async def setGitDescription(self):
        self._gitDescription = 'Mock description'

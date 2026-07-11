from otto.config.repo import Repo


class MockRepo(Repo):
    async def set_commit_hash(self):
        self._git_hash = "9575739d490dc5e29e0eb985ca6f1f26ca4e65b3"

    async def set_git_description(self):
        self._git_description = "Mock description"

import os


class OsEnvironmentReader:
    def get(self, name: str) -> str | None:
        return os.environ.get(name)

class DomainError(Exception):
    """Stable public failure; messages must not include credentials or wire data."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

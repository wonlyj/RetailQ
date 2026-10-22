class ServiceError(Exception):
    """An application-owned error safe to expose at the API boundary."""

    def __init__(self, code: str, status_code: int, public_message: str):
        super().__init__(public_message)
        self.code = code
        self.status_code = status_code
        self.public_message = public_message

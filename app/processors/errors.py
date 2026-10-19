class PermanentError(Exception):
    """The input can never be processed (corrupt file, unsupported codec). Do not retry."""

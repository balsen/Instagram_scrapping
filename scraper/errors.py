class ScraperError(Exception):
    pass


class ConfigurationError(ScraperError):
    pass


class InvalidUsername(ScraperError, ValueError):
    pass


class ProfileNotFound(ScraperError):
    pass


class AccessBlocked(ScraperError):
    pass


class RateLimited(AccessBlocked):
    pass


class NavigationFailed(ScraperError):
    pass


class ExtractionFailed(ScraperError):
    pass

from ..common import DomainError


class ProviderNotConfigured(DomainError):
    def __init__(self):
        super().__init__("provider_not_configured", "実APIは未接続です。後続I15で対応します。")


def create(*args, **kwargs):
    raise ProviderNotConfigured()

import ipaddress

from app.core.config import get_settings

_allowed_nets: list[ipaddress._BaseNetwork] | None = None


def parse_allowed_networks(raw: str) -> list[ipaddress._BaseNetwork]:
    nets: list[ipaddress._BaseNetwork] = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            nets.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue
    return nets


def get_allowed_nets() -> list[ipaddress._BaseNetwork]:
    global _allowed_nets
    if _allowed_nets is None:
        _allowed_nets = parse_allowed_networks(get_settings().ALLOWED_IPS)
    return _allowed_nets


def is_ip_allowed(client_ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(addr in net for net in get_allowed_nets())

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Mapping

from .sources import load_configuration


DEFAULT_BIND_HOST = "127.0.0.1"
BIND_HOST_ENV = "MPV_WEBD_HOST"
AUTH_ENABLED_ENV = "MPV_WEBD_AUTH_ENABLED"
UNSAFE_DEVELOPMENT_OVERRIDE_ENV = "MPV_WEBD_UNSAFE_DEV_ALLOW_REMOTE_NO_AUTH"
TRUSTED_LAN_CIDR_ENV = "MPV_WEBD_TRUSTED_LAN_CIDR"
_RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class NetworkConfigurationError(ValueError):
    """Raised when listener settings would expose an unauthenticated API."""


@dataclass(frozen=True, slots=True)
class NetworkSettings:
    """Fail-closed bind/auth settings shared by service launchers."""

    bind_host: str
    auth_enabled: bool
    unsafe_development_override: bool
    trusted_lan_network: ipaddress.IPv4Network | None = None

    @property
    def is_loopback(self) -> bool:
        return ipaddress.ip_address(self.bind_host).is_loopback

    @property
    def is_trusted_lan(self) -> bool:
        return self.trusted_lan_network is not None

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "NetworkSettings":
        values = load_configuration(environ)
        raw_host = values.get(BIND_HOST_ENV, DEFAULT_BIND_HOST)
        if not isinstance(raw_host, str) or not raw_host or raw_host.strip() != raw_host:
            raise NetworkConfigurationError(
                f"{BIND_HOST_ENV} must be a non-empty IP address without whitespace"
            )
        try:
            address = ipaddress.ip_address(raw_host)
        except ValueError as error:
            raise NetworkConfigurationError(
                f"{BIND_HOST_ENV} must be an IPv4 or IPv6 address literal"
            ) from error

        auth_enabled = _environment_bool(values, AUTH_ENABLED_ENV, default=False)
        unsafe_override = _environment_bool(
            values, UNSAFE_DEVELOPMENT_OVERRIDE_ENV, default=False
        )
        trusted_lan_network = None
        raw_trusted_lan = values.get(TRUSTED_LAN_CIDR_ENV)
        if raw_trusted_lan is not None:
            if (
                not isinstance(raw_trusted_lan, str)
                or not raw_trusted_lan
                or raw_trusted_lan.strip() != raw_trusted_lan
            ):
                raise NetworkConfigurationError(
                    f"{TRUSTED_LAN_CIDR_ENV} must be an explicit IPv4 CIDR"
                )
            try:
                candidate_network = ipaddress.ip_network(raw_trusted_lan, strict=True)
            except ValueError as error:
                raise NetworkConfigurationError(
                    f"{TRUSTED_LAN_CIDR_ENV} must be an explicit IPv4 CIDR"
                ) from error
            if (
                not isinstance(candidate_network, ipaddress.IPv4Network)
                or not any(candidate_network.subnet_of(private) for private in _RFC1918_NETWORKS)
                or address not in candidate_network
                or address.is_unspecified
                or address.is_multicast
            ):
                raise NetworkConfigurationError(
                    f"{TRUSTED_LAN_CIDR_ENV} must be an RFC1918 IPv4 network containing "
                    f"the non-wildcard {BIND_HOST_ENV} address"
                )
            trusted_lan_network = candidate_network

        if (
            not address.is_loopback
            and not auth_enabled
            and not unsafe_override
        ):
            raise NetworkConfigurationError(
                "non-loopback bind requires MPV_WEBD_AUTH_ENABLED=true; "
                f"{UNSAFE_DEVELOPMENT_OVERRIDE_ENV}=true is an explicit unsafe development override, "
                "or use loopback-only binding"
            )
        return cls(
            bind_host=address.compressed,
            auth_enabled=auth_enabled,
            unsafe_development_override=unsafe_override,
            trusted_lan_network=trusted_lan_network,
        )


def _environment_bool(
    values: Mapping[str, str], name: str, *, default: bool
) -> bool:
    raw_value = values.get(name)
    if raw_value is None:
        return default
    if not isinstance(raw_value, str):
        raise NetworkConfigurationError(f"{name} must be true or false")
    normalized = raw_value.casefold()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise NetworkConfigurationError(f"{name} must be true or false")

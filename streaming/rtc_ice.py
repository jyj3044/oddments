"""STUN/TURN 만 구성하는 RTCConfiguration (무거운 remote_host 로딩 없이 aiortc만 사용)."""

from __future__ import annotations

from aiortc import RTCConfiguration, RTCIceServer


def rtc_configuration_from_stun_turn(
    *,
    stun_urls: str,
    turn_uri: str,
    turn_username: str,
    turn_password: str,
) -> RTCConfiguration:
    servers: list[RTCIceServer] = []
    raw = (stun_urls or "").replace(",", "\n")
    for line in raw.splitlines():
        u = line.strip()
        if u:
            servers.append(RTCIceServer(urls=[u]))
    tu = (turn_uri or "").strip()
    if tu:
        servers.append(
            RTCIceServer(
                urls=[tu],
                username=(turn_username or None),
                credential=(turn_password or None),
            )
        )
    return RTCConfiguration(iceServers=servers)

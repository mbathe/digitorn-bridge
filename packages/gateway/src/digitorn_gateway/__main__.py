"""Entry point: `python -m digitorn_gateway` or `digitorn-gateway`."""

from __future__ import annotations


def main() -> None:
    import uvicorn

    from digitorn_gateway.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "digitorn_gateway.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()

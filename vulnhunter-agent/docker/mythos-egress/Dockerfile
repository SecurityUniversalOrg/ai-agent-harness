# syntax=docker/dockerfile:1.7
ARG SQUID_BASE=debian:bookworm-slim
FROM ${SQUID_BASE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates squid \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/spool/squid /var/log/squid \
    && chown -R proxy:proxy /var/spool/squid /var/log/squid

COPY vulnhunter-agent/docker/mythos-egress/squid.conf /etc/squid/squid.conf
RUN chown root:root /etc/squid/squid.conf && chmod 0444 /etc/squid/squid.conf

USER proxy
EXPOSE 3128
ENTRYPOINT ["/usr/sbin/squid", "-N", "-f", "/etc/squid/squid.conf"]

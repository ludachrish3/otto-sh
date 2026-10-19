# CentOS 7 (multi-arch; runs natively on arm64 peers) as the old-OS tunnel
# endpoint: bash 4.2, procps-ng 3.3.10, NO systemd inside -> the tunnel
# launch exercises the setsid fallback by construction (spec #2b §13).
FROM centos:7
# CentOS 7 is EOL: point yum at the vault archive before installing.
RUN sed -i -e 's|^mirrorlist=|#mirrorlist=|' \
        -e 's|^#\?baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|' \
        /etc/yum.repos.d/CentOS-Base.repo \
    && yum install -y socat && yum clean all
CMD ["sh", "-c", "while sleep 3600; do :; done"]

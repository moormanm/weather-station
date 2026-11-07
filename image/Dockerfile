FROM debian:trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates parted fdisk e2fsprogs util-linux xz-utils \
    qemu-user arch-test python3 && rm -rf /var/lib/apt/lists/*

COPY customize-base.sh 00-run.sh 00-packages /tools/
ENTRYPOINT ["bash", "/tools/customize-base.sh"]

# Builder image for the TrimUI Brick emulator.
# Ubuntu 24.04 to match the host, so QEMU built here runs natively on the host.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git ca-certificates curl wget xz-utils bzip2 python3 python3-venv \
      ninja-build meson pkg-config flex bison bc cpio kmod libssl-dev libelf-dev \
      gcc-aarch64-linux-gnu \
      libglib2.0-dev libpixman-1-dev libgtk-3-dev libepoxy-dev libgbm-dev libdrm-dev \
      libslirp-dev libasound2-dev libpulse-dev libvirglrenderer-dev libfdt-dev zlib1g-dev \
      libsdl2-dev \
      dosfstools mtools exfatprogs e2fsprogs gdisk fakeroot \
    && rm -rf /var/lib/apt/lists/*

# PkgTool (PS4 PKG extraction) is a .NET build that needs OpenSSL 1.1. Only these two
# libraries are copied, into a private directory used for that tool alone.
FROM debian:bullseye-slim@sha256:e5b6442dd2e9684cf5e87d8338b5968f3b348636fc0be6d7850a381e3731a2bd AS compat-ssl

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# The fixed reviewed profile builds an extractor; arbitrary Docker recipes stay off.
# Xvfb and Mesa let official GUI runtimes generate game archives headlessly.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential cmake ninja-build zlib1g-dev patch git lsb-release ca-certificates \
       xvfb xauth libgl1 libopengl0 libglx-mesa0 libgl1-mesa-dri libegl1 libglib2.0-0t64 \
    && python -c 'import re, subprocess; v = re.search(r"cmake version (\d+)\.(\d+)", subprocess.check_output(["cmake", "--version"], text=True)); assert v and tuple(map(int, v.groups())) >= (3, 26), "The profile runner needs CMake 3.26+"' \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home portkeeper \
    && mkdir /data /inputs /library \
    && chown portkeeper:portkeeper /data /library

COPY --from=compat-ssl /usr/lib/x86_64-linux-gnu/libssl.so.1.1 /usr/lib/x86_64-linux-gnu/libcrypto.so.1.1 /opt/portkeeper/compat-ssl/
COPY catalog ./catalog
COPY portkeeper ./portkeeper
COPY static ./static
COPY tools ./tools
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Unraid starts containers as root and passes PUID/PGID (default nobody:users);
# the entrypoint drops to that user. Compose sets `user:` and skips the drop.
ENV PUID=99 PGID=100
EXPOSE 8787
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "portkeeper", "--host", "0.0.0.0", "--data-dir", "/data", "--input-root", "/inputs", "--library-root", "/library", "--enable-profile-builds"]

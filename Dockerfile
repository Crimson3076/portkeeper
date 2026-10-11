FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# The fixed reviewed profile builds an extractor; arbitrary Docker recipes stay off.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential cmake ninja-build zlib1g-dev patch git lsb-release ca-certificates \
    && python -c 'import re, subprocess; v = re.search(r"cmake version (\d+)\.(\d+)", subprocess.check_output(["cmake", "--version"], text=True)); assert v and tuple(map(int, v.groups())) >= (3, 26), "The profile runner needs CMake 3.26+"' \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home portkeeper \
    && mkdir /data /inputs /library \
    && chown portkeeper:portkeeper /data /library

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

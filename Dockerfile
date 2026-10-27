# PkgTool (PS4 PKG extraction) is a .NET build that needs OpenSSL 1.1. Only these two
# libraries are copied, into a private directory used for that tool alone.
FROM debian:bullseye-slim@sha256:e5b6442dd2e9684cf5e87d8338b5968f3b348636fc0be6d7850a381e3731a2bd AS compat-ssl
RUN mkdir /compat \
    && cp /usr/lib/*-linux-gnu/libssl.so.1.1 /usr/lib/*-linux-gnu/libcrypto.so.1.1 /compat/

# Official subscription CLIs, with every npm archive pinned by package-lock.json.
# Use the same official Node LTS binary at build time and runtime. Native optional
# packages select the target architecture; none of the vendor CLIs is patched.
FROM node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS subscription-clis
WORKDIR /opt/portkeeper/agents
COPY tools/subscription-agents/package.json tools/subscription-agents/package-lock.json ./
# Headless drafting doesn't need Gemini's optional shell/keychain native builds.
# Run only the official Claude postinstall, which links its locked native binary.
RUN npm ci --ignore-scripts --include=optional --omit=dev --no-audit --no-fund \
    && node -e 'require.resolve("@anthropic-ai/claude-code-linux-" + process.arch + "/package.json")' \
    && node node_modules/@anthropic-ai/claude-code/install.cjs \
    && node_modules/.bin/codex --version \
    && node_modules/.bin/claude --version \
    && node_modules/.bin/gemini --version \
    && chmod -R a+rX /opt/portkeeper/agents \
    && npm cache clean --force

FROM python:3.13-slim

LABEL net.unraid.docker.icon="https://raw.githubusercontent.com/Crimson3076/portkeeper/main/static/portkeeper-fruit.png"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PATH="/opt/portkeeper/agents/node_modules/.bin:${PATH}" DISABLE_AUTOUPDATER=1 DISABLE_UPDATES=1
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
    && mkdir /data /storage /inputs /library \
    && chown portkeeper:portkeeper /data /storage /library

COPY --from=compat-ssl /compat/ /opt/portkeeper/compat-ssl/
COPY --from=subscription-clis /usr/local/bin/node /usr/local/bin/node
COPY --from=subscription-clis /opt/portkeeper/agents /opt/portkeeper/agents
COPY catalog ./catalog
COPY portkeeper ./portkeeper
COPY static ./static
COPY tools ./tools
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Checkouts can have private file modes. Application code must be readable by
# the configured Unraid UID as well as the Compose user; data stays private.
RUN chmod -R a+rX /app

# Unraid starts containers as root and passes PUID/PGID (default nobody:users);
# the entrypoint drops to that user. Compose sets `user:` and skips the drop.
ENV PUID=99 PGID=100
EXPOSE 8787
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "portkeeper", "--host", "0.0.0.0", "--data-dir", "/data", "--input-root", "/inputs", "--library-root", "/library", "--enable-profile-builds"]

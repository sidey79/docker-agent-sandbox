# The agent image. Deliberately generic: it knows how to bring up a devcontainer
# and how to report a result, but no agent CLI is baked in — see docs/PLAN.md §6.
#
# The Docker CLI is copied in rather than installed from a repository, so that
# Renovate tracks it as the same image the rest of the stack pins.
FROM docker:29.8.1-cli@sha256:6602978e2be3c20e530e33773b8cadcef5fe998a71a534ee24516f1176973cdf AS docker-cli

FROM node:24.21.0-bookworm-slim@sha256:713cfbf4a0ac19f40e1bb9919893e126b74a5c8cf5d0623c9f89515c8f74c6fa

# git to fetch the repository under test, jq for the entrypoint, tini so that a
# cancelled job does not leave zombies behind.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        jq \
        openssh-client \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# jsonc-parser is used by the entrypoint to read a repository's devcontainer.json,
# which is JSON with comments and therefore not jq's business.
RUN npm install -g @devcontainers/cli@0.89.0 jsonc-parser@3.3.1 \
    && npm cache clean --force
ENV NODE_PATH=/usr/local/lib/node_modules

# The job volume is mounted here. Creating the directory with the right owner
# matters: Docker copies the image's ownership onto a fresh named volume, which
# is how the unprivileged `node` user ends up able to write to it.
RUN mkdir -p /workspace && chown node:node /workspace

COPY --chmod=0755 entrypoint.sh /usr/local/bin/agent-entrypoint
COPY --chmod=0755 devcontainer-config.mjs /usr/local/lib/agent/devcontainer-config.mjs

USER node
WORKDIR /workspace

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/agent-entrypoint"]

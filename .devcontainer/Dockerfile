# A container that can actually run this project.
#
# The previous one was built for a C++ program that is not in this repository:
# build-essential, libcurl, libsecret, and no Python at all. Opening the repo in
# a Codespace produced an environment that could not run a single test.

FROM mcr.microsoft.com/devcontainers/python:3.12

# ffmpeg and ffprobe. `requirements.txt` also brings the ffmpeg-binaries wheel,
# which the package prefers because distro builds sometimes ship without
# libx264, xfade or loudnorm — but a system ffmpeg is a useful fallback and
# makes the container usable for looking at the output by hand.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

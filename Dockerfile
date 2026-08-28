# The whole app, at a real address.
#
# `auteur serve` has always bound 0.0.0.0 and taken a --port, so it has always
# been deployable and there has never been anything to deploy it with. The gap
# that leaves is not academic: the only link this project could hand anybody
# was the published single-page build, which has no server behind it and so
# cannot have the feed, the messages, the schedule or the plan board. Somebody
# following that link sees the making half and reasonably concludes that is the
# app.
#
# This is the other link. It runs the same server the phone talks to over wifi,
# so everything works: accounts, the feed that ranks on what was watched,
# messages, projects, the schedule.
#
# ffmpeg is the only system dependency and it is not optional — it is the thing
# that renders. `ffmpeg-binaries` in requirements.txt supplies one for local
# use, but a distribution build is smaller and faster than shipping a wheel's
# copy, so the image gets it from the package manager and `auteur.ffmpeg` finds
# whichever is present.

FROM python:3.12-slim

# ffmpeg for rendering; the rest is what Pillow needs to open a JPEG.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so a code change does not reinstall numpy.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Where the films live. `serve --out` rather than an environment variable,
# because there is no AUTEUR_WORKSPACE — the first draft of this file invented
# one, which would have quietly written everything into the image and lost it
# on every restart. A volume rather than a layer: a container that forgets the
# footage when it restarts is not somewhere to keep anything.
VOLUME ["/data"]

# Hosts hand the port in as $PORT and vary on whether they set it at all.
ENV PORT=8000
EXPOSE 8000

# Not root. The server writes uploads to disk and runs ffmpeg over them, which
# is the pair of facts that makes running as root a bad idea rather than a
# style preference.
RUN useradd --create-home --uid 10001 auteur \
    && mkdir -p /data \
    && chown -R auteur:auteur /data /app
USER auteur

# `sh -c` so $PORT expands. `--host 0.0.0.0` is the default and is stated
# anyway: a container that binds loopback is a container nothing can reach, and
# the failure looks like the app being down rather than misconfigured.
CMD ["sh", "-c", "python -m auteur serve --host 0.0.0.0 --port ${PORT} --out /data"]

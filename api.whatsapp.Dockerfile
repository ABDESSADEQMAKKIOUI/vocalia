# Thin derived image for the feature/whatsapp-channel local run.
# The published dograh-api image already has every heavy dependency installed;
# it only lags the pipecat submodule (missing the newer `flux` module that the
# current api code imports). Reinstall pipecat from our pinned submodule commit
# (no-deps, so the existing heavy extras stay) plus the one new runtime dep.
# Build context must be dograh-src (so `pipecat/` is available).
FROM ghcr.io/dograh-hq/dograh-api:latest

USER root
COPY pipecat /opt/pipecat-src
RUN pip install --no-deps /opt/pipecat-src \
 && pip install num2words
USER dograh

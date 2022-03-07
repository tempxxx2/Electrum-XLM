FROM ubuntu:20.04
LABEL maintainer "Andriy Khavryuchenko <akhavr@khavr.com>"

ARG USER_ID
ARG GROUP_ID

ENV HOME /builder
ENV TZ Europe/Kiev
env DEBIAN_FRONTEND noninteractive

ENV USER_ID ${USER_ID:-1000}
ENV GROUP_ID ${GROUP_ID:-1000}
RUN groupadd -g ${GROUP_ID} builder
RUN useradd -u ${USER_ID} -g builder -s /bin/bash -m -d /builder builder
RUN chown builder:builder -R /builder

ENV LANG "en_US.UTF-8"
ENV LANGUAGE ="en_US.UTF-8"
ENV LC_ALL "en_US.UTF-8"

RUN apt-get update -y -qq > /dev/null \
    && apt-get install -qq --yes --no-install-recommends \
        locales build-essential \
        gnupg pbuilder ubuntu-dev-tools apt-file \
        python3-software-properties \
        software-properties-common \
    && apt-add-repository -y ppa:akhavr/dash-electrum-builddeps \
    && apt-get install -y --no-install-recommends \
        debhelper python3-all python3-dev \
        python3-pip python3-virtualenv python3-setuptools cython3 \
        libusb-1.0-0-dev libudev-dev libgmp-dev libsecp256k1-0 \
        dh-python dh-virtualenv \
        electrum-dash-pypi-bdeps electrum-dash-pypi-bdeps-updates \
    && locale-gen en_US.UTF-8 \
    && dpkg-reconfigure locales

USER builder
WORKDIR /builder/build

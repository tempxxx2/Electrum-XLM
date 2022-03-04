#!/bin/bash

docker build \
    --build-arg USER_ID=$(echo $UID) \
    --build-arg GROUP_ID=$(echo $GID) \
    -t build-ppa-docker .

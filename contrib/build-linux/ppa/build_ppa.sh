#!/bin/bash

GPG_PRESET_PP="/usr/lib/gnupg2/gpg-preset-passphrase"

CMD="read PASSPHRASE"

CMD="${CMD}&& gpg -q --list-keys"
CMD="${CMD}&& gpg -q --import pub.gpg"
CMD="${CMD}&& gpg -q --batch --passphrase \"\${PASSPHRASE}\" --import priv.gpg"

CMD="${CMD}&& GPG_OPTS=\"gpg --batch --passphrase \"\${PASSPHRASE}\" --pinentry-mode loopback\""
CMD="${CMD}&& cd Dash* && debuild -S -p\"\${GPG_OPTS}\""

read PASSPHRASE
echo "${PASSPHRASE}" | docker run -i --rm \
    -v $(pwd)/..:/builder/build \
    -h ppa-builder \
    build-ppa-docker bash -c "$CMD"
